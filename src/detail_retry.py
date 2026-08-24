"""의안 상세(제안이유 및 주요내용) 재조회 큐 처리.

왜 필요한가
-----------
신규 의안은 최초 알림이 성공하면 seen 으로 확정된다. 그러면 다음 실행의 목록 수집은
그 의안을 신규로 돌려주지 않으므로 상세 수집도 다시 일어나지 않는다. 최초 알림 시점에
제안이유가 아직 등록되지 않았거나(PENDING) 수집이 실패했다면(ERROR) 나중에 원문이
올라와도 영영 가져오지 못한다. 그렇다고 PENDING/ERROR 를 seen 처리하지 않으면 같은
의안이 15분마다 '신규'로 다시 발송된다.

그래서 '알렸다(seen)'와 '상세를 확보했다'를 분리하고, 후자를 state 의
pending_detail 큐로 남겨 이 모듈이 다음 실행부터 다시 조회한다.

무엇을 하지 않는가
------------------
- 목록(Open API)에 다시 나타나기를 기다리지 않는다. 이미 seen 인 의안은 목록 수집
  결과에 신규로 잡히지 않기 때문이다. 저장해 둔 스냅샷(BILL_ID/제목/URL/날짜)으로
  Post 를 최소 재구성해 **기존** AssemblyBillScraper.enrich() 를 그대로 부른다.
- 새로운 LIKMS parser 나 HTTP 계약을 만들지 않는다. canonical URL·CSRF·같은 세션·
  billInfo POST·의안 동일성 검증·fail-closed 판정·PENDING/ERROR 구분은 전부
  scraper 쪽 기존 구현을 재사용한다.
- 큐를 자동으로 만료·삭제하지 않는다. 오래 남은 항목은 로그로 드러내되, 명시적
  운영 정책 없이 알림 기회를 지우지 않는다(LAW_RADER 의 목적은 누락 방지다).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .models import Post, ProposalContentStatus
from .scrapers.assembly import (
    DETAIL_RETRY_INTERVAL_SEC,
    DETAIL_RETRY_MAX_PER_RUN,
    _DETAIL,
    _canonical_detail_url,
)
from .state import State, parse_ts

log = logging.getLogger("law_rader")


@dataclass(frozen=True)
class QueuedBill:
    """state 에 저장된 재조회 대기 항목 하나(읽기 전용 뷰)."""

    bill_id: str
    title: str = ""
    url: str = ""
    date: str = ""
    status: str = ""
    attempts: int = 0
    first_seen_at: str = ""
    last_attempt_at: str = ""
    note: str = ""

    @property
    def last_attempt_ts(self) -> float | None:
        return parse_ts(self.last_attempt_at)


def load_queue(state: State, source_key: str) -> list[QueuedBill]:
    """state 의 pending_detail 을 QueuedBill 목록으로. 깨진 항목은 조용히 건너뛴다."""
    out: list[QueuedBill] = []
    for bill_id, raw in state.pending_detail(source_key).items():
        out.append(
            QueuedBill(
                bill_id=bill_id,
                title=_text(raw.get("title")),
                url=_text(raw.get("url")),
                date=_text(raw.get("date")),
                status=_text(raw.get("status")),
                attempts=_int(raw.get("attempts")),
                first_seen_at=_text(raw.get("first_seen_at")),
                last_attempt_at=_text(raw.get("last_attempt_at")),
                note=_text(raw.get("note")),
            )
        )
    return out


@dataclass
class DueSelection:
    """이번 실행에서 실제로 재조회할 항목과 그 선정 근거(로그용)."""

    selected: list[QueuedBill] = field(default_factory=list)
    queued: int = 0                 # 큐 전체 건수
    due: int = 0                    # 간격 조건을 통과한 건수
    deferred_by_interval: int = 0   # 간격 미충족으로 이번엔 건너뛴 건수

    @property
    def capped(self) -> int:
        """건수 상한에 걸려 다음 실행으로 미룬 건수."""
        return max(self.due - len(self.selected), 0)


def select_due(
    queue: list[QueuedBill],
    *,
    now_ts: float,
    interval_sec: float,
    max_per_run: int,
) -> DueSelection:
    """재조회 대상 선정.

    - interval_sec: 같은 의안을 다시 시도하기 전 최소 간격(초). 0 이면 매 실행 허용.
    - max_per_run:  한 실행에서 처리할 최대 건수. 0 이면 건수 상한 없음
                    (그래도 scraper 의 detail_budget_sec / 연속실패 브레이커는 적용된다).

    순서는 결정적이다: 마지막 시도가 오래된 것 먼저, 같으면 BILL_ID 사전순.
    시각을 읽을 수 없는 항목은 '가장 오래된 것'으로 보아 먼저 처리한다 — 판독 불가를
    이유로 재조회를 영구히 미루면 누락 방지라는 목적에 반한다.
    """
    due: list[QueuedBill] = []
    deferred = 0
    for item in queue:
        last = item.last_attempt_ts
        if interval_sec > 0 and last is not None and (now_ts - last) < interval_sec:
            deferred += 1
            continue
        due.append(item)

    due.sort(key=lambda q: (q.last_attempt_ts if q.last_attempt_ts is not None else float("-inf"), q.bill_id))
    selected = due if max_per_run <= 0 else due[:max_per_run]
    return DueSelection(
        selected=list(selected),
        queued=len(queue),
        due=len(due),
        deferred_by_interval=deferred,
    )


def retry_limits(scraper) -> tuple[float, int]:
    """(재시도 최소 간격 초, 실행당 최대 건수).

    값은 스크래퍼 인스턴스가 config 에서 읽어 둔 것을 쓴다. 스크래퍼가 교체되어 이
    속성이 없으면(테스트 대역 등) 코드 기본값으로 되돌린다 — 기본 숫자는
    src/scrapers/assembly.py 한 곳에만 있다.
    """
    interval = getattr(scraper, "detail_retry_interval_sec", DETAIL_RETRY_INTERVAL_SEC)
    per_run = getattr(scraper, "detail_retry_max_per_run", DETAIL_RETRY_MAX_PER_RUN)
    return float(interval), int(per_run)


def build_post(
    item: QueuedBill,
    *,
    source_key: str,
    source_name: str,
    detail_url_template: str = _DETAIL,
) -> Post:
    """저장된 스냅샷으로 상세 재조회용 Post 를 최소 재구성한다.

    url 이 비어 있거나 죽은 구 경로면 BILL_ID 로 현재 상세 경로를 다시 만든다
    (scraper 가 목록 수집에서 쓰는 것과 **같은** canonicalize 함수).
    """
    return Post(
        source_key=source_key,
        source_name=source_name,
        post_id=item.bill_id,
        title=item.title or item.bill_id,
        url=_canonical_detail_url(item.url, item.bill_id, detail_url_template),
        date=item.date,
    )


@dataclass
class RetryOutcome:
    """재조회 결과. 알림/state 확정은 호출자(main)가 트랜잭션에 맞춰 결정한다."""

    # 제안이유를 새로 확보한 항목 — '의안 상세 업데이트' 알림 후보.
    recovered: list[Post] = field(default_factory=list)
    # 여전히 미확보인 항목 — 큐에 남기고 진단 메타데이터만 갱신한다.
    still_queued: list[Post] = field(default_factory=list)

    @property
    def attempted(self) -> int:
        return len(self.recovered) + len(self.still_queued)

    @property
    def pending(self) -> int:
        return sum(
            1
            for p in self.still_queued
            if p.proposal_status is ProposalContentStatus.PENDING
        )

    @property
    def failed(self) -> int:
        return len(self.still_queued) - self.pending


def retry_bills(
    scraper,
    selection: DueSelection,
    *,
    source_key: str,
    source_name: str,
) -> RetryOutcome:
    """선정된 항목을 기존 enrich() 경로로 다시 조회한다.

    scraper 는 이번 실행에서 신규 의안 상세를 수집한 것과 **같은 인스턴스**여야 한다.
    detail_budget_sec 와 연속실패 브레이커가 '신규 + 재조회'를 합친 의안 상세 작업
    전체를 제한해야 하기 때문이다(따로 만들면 사실상 예산을 두 배로 쓴다).
    """
    outcome = RetryOutcome()
    if not selection.selected:
        log.info(
            "의안 상세 재조회 — queued=%d due=%d selected=0 deferred_by_interval=%d",
            selection.queued,
            selection.due,
            selection.deferred_by_interval,
        )
        return outcome

    log.info(
        "의안 상세 재조회 — queued=%d due=%d selected=%d deferred_by_interval=%d",
        selection.queued,
        selection.due,
        len(selection.selected),
        selection.deferred_by_interval,
    )
    detail_url = getattr(scraper, "detail_url", _DETAIL) or _DETAIL

    for item in selection.selected:
        post = build_post(
            item,
            source_key=source_key,
            source_name=source_name,
            detail_url_template=detail_url,
        )
        log.info(
            "의안 상세 재조회 — bill=%s previous=%s attempt=%d",
            item.bill_id,
            item.status or "unknown",
            item.attempts + 1,
        )
        try:
            scraper.enrich(post)
        except Exception as e:  # noqa: BLE001 — 한 건의 실패가 나머지를 막지 않는다
            # AssemblyBillScraper.enrich 는 원래 예외를 밖으로 내보내지 않지만,
            # 계약이 바뀌어도 재조회 루프가 통째로 죽지 않도록 방어한다.
            post.proposal_status = ProposalContentStatus.ERROR
            post.proposal_note = f"{type(e).__name__}: {e}"
            log.warning("의안 상세 재조회 실패 bill=%s: %s", item.bill_id, e)

        if post.proposal_status is ProposalContentStatus.AVAILABLE:
            log.info("의안 상세 재조회 — AVAILABLE 복구 bill=%s", item.bill_id)
            outcome.recovered.append(post)
        else:
            log.info(
                "의안 상세 재조회 — 여전히 %s bill=%s",
                post.proposal_status.value,
                item.bill_id,
            )
            outcome.still_queued.append(post)

    log.info(
        "의안 상세 재조회 집계 — queued=%d attempted=%d recovered=%d pending=%d "
        "failed=%d deferred_by_interval=%d cap_deferred=%d",
        selection.queued,
        outcome.attempted,
        len(outcome.recovered),
        outcome.pending,
        outcome.failed,
        selection.deferred_by_interval,
        selection.capped,
    )
    return outcome


def log_long_waiters(queue: list[QueuedBill], *, now_ts: float, warn_after_sec: float) -> None:
    """오래 남은 큐 항목을 드러낸다. **삭제하지 않는다.**

    자동 만료는 이번 범위 밖이다(알림 기회를 조용히 지우지 않는다). 다만 계속 쌓이는
    항목이 있으면 운영자가 알아볼 수 있어야 하므로 경고로 남긴다.
    """
    if warn_after_sec <= 0:
        return
    stale = []
    for item in queue:
        first = parse_ts(item.first_seen_at)
        if first is not None and (now_ts - first) >= warn_after_sec:
            stale.append(item)
    if not stale:
        return
    log.warning(
        "의안 상세 재조회 큐에 오래 남은 항목 %d건(가장 오래된 등록 %s) — 자동 삭제하지 "
        "않습니다. 대상: %s",
        len(stale),
        min(item.first_seen_at for item in stale),
        ", ".join(f"{item.bill_id}({item.status or 'unknown'})" for item in stale[:5]),
    )


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
