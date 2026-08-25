"""LAW_RADER_KR 실행 진입점.

흐름:
  1) config + state 로드 (+ 의안 상세 재조회 큐 스냅샷)
  2) 소스별로 목록 수집 → 신규 판별
     - 해당 소스가 처음이면(baseline 미수립) 신규를 메일로 보내지 않고 기준선만 기록
  3) 신규 게시글의 상세/첨부 수집(enrich)
  4) 발송 가능성이 있으면 SMTP 선점검(요약에 할당량을 쓰기 전에)
  5) 기존 의안 상세 재조회 큐 처리(제안이유가 등록됐는지 다시 확인)
  6) 신규 + 복구분을 한 번의 LLM 경로로 요약
  7) 신규/상세 업데이트를 다이제스트 메일 **1통**으로 발송
  8) 발송 성공 후 state 확정 저장(seen / 재조회 큐 등록·제거)

의안 상세 재조회(Phase 2):
  최초 알림 시점에 '제안이유 및 주요내용'이 아직 등록되지 않았거나(PENDING) 수집이
  실패한(ERROR/UNKNOWN) 의안은 seen 으로 확정되는 동시에 state 의 pending_detail
  큐에 등록된다. seen 이므로 다음 실행의 목록 수집에는 신규로 잡히지 않지만, 이 큐를
  통해 저장된 스냅샷으로 상세만 다시 조회한다. 나중에 원문이 등록되면 '신규'가 아닌
  '의안 상세 업데이트'로 정확히 한 번 알리고 큐에서 지운다. 자세한 배경은
  src/detail_retry.py 와 src/state.py 참고.

사용:
  python -m src.main            # 정상 실행(메일 발송)
  python -m src.main --dry-run  # 수집만 하고 메일은 보내지 않음(출력만)
  python -m src.main --debug    # 상세 로그
  python -m src.main --only fss_press,fsc_press   # 특정 소스만
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from dataclasses import dataclass

from .config import load_config
from .detail_retry import (
    NOTE_MAX_CHARS,
    DueSelection,
    RetryOutcome,
    load_queue,
    log_long_waiters,
    retry_bills,
    retry_limits,
    select_due,
)
from .fetcher import Fetcher
from .models import ASSEMBLY_SOURCE_KEY, Post, ProposalContentStatus
from .notifier import missing_email_settings, send_digest, verify_smtp_login
from .scrapers import build_scraper
from .state import State, utcnow_iso
from .summarizer import ai_target_count, summarize_posts

log = logging.getLogger("law_rader")

# 이보다 오래 큐에 남은 항목은 경고로 드러낸다. **삭제하지는 않는다** — 자동 만료는
# 명시적 운영 정책이 정해지기 전까지 하지 않는다(알림 기회를 조용히 지우지 않는다).
_QUEUE_WARN_AFTER_SEC = 7 * 24 * 3600


@dataclass
class _DetailStats:
    """상세(enrich) 수집 집계.

    '등록 대기(pending)'는 실패가 아니다. 갓 접수된 의안은 원문이 아직 공개되지
    않아 본문이 비는 것이 정상이고, 그것을 실패로 세면 매 실행마다 거짓 실패가
    쌓여 진짜 고장을 가린다. 그래서 succeeded/pending/failed 를 따로 센다.
    """

    attempted: int = 0
    succeeded: int = 0
    pending: int = 0

    @property
    def failed(self) -> int:
        return self.attempted - self.succeeded - self.pending

    def count(self, ok: bool, pending: bool = False) -> None:
        self.attempted += 1
        if pending:
            self.pending += 1
        elif ok:
            self.succeeded += 1


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="한국 금융 규제·입법 모니터링")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--state", default="state/seen.json")
    p.add_argument("--dry-run", action="store_true", help="메일 발송 없이 수집 결과만 출력")
    p.add_argument("--debug", action="store_true", help="디버그 로그")
    p.add_argument("--only", default="", help="쉼표로 구분한 소스 key 만 실행")
    p.add_argument(
        "--no-llm", action="store_true", help="LLM 본문 요약을 건너뛴다(원문 발췌로 발송)"
    )
    return p.parse_args(argv)


def run(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config(args.config)
    _log_llm_settings(cfg.llm)
    state = State(args.state)
    fetcher = Fetcher(
        timeout=cfg.fetch.timeout_sec,
        delay=cfg.fetch.delay_sec,
        max_download_bytes=cfg.email.max_attach_bytes,
    )

    only = {s.strip() for s in args.only.split(",") if s.strip()}

    # 의안 상세 재조회 큐는 **실행 시작 시점의 스냅샷**을 쓴다. 이번 실행에서 새로
    # 등록될 항목(최초 알림이 성공한 PENDING/ERROR)은 이 목록에 없으므로, 방금 상세를
    # 조회한 의안을 같은 실행에서 곧바로 다시 조회하는 일이 구조적으로 일어나지 않는다.
    assembly_queue = load_queue(state, ASSEMBLY_SOURCE_KEY)
    # 재조회는 이번 실행에서 신규 의안 상세를 수집한 것과 **같은 스크래퍼 인스턴스**로
    # 수행한다 — detail_budget_sec 와 연속실패 브레이커가 '신규 + 재조회'를 합친 의안
    # 상세 작업 전체를 제한해야 하기 때문이다(따로 만들면 예산을 두 배로 쓴다).
    assembly_scraper = None
    assembly_source_name = ""
    assembly_selected = False

    posts_by_source: dict[str, list[Post]] = {}
    # 발송 대상 신규 ID (소스별). 메일 성공 후에만 seen 처리한다. 발송 전에 미리
    # 기록하면, 뒤 소스의 기준선 즉시저장이 이 ID들까지 디스크에 써버려 메일 실패 시
    # 재발송 없이 영구 누락된다.
    pending_seen: list[tuple[str, list[str]]] = []
    errors: list[str] = []
    selected = 0   # 실행 대상(활성+선택) 소스 수
    succeeded = 0  # 목록 수집에 성공한 소스 수
    # 상세(enrich) 집계 — 실행 종료 시 한 번에 보고한다. 상세 수집이 전멸하면
    # 메일은 제목·링크만 남은 채로 계속 나가므로, 로그를 보지 않으면 알아채기 어렵다.
    #
    # 의안은 따로 센다. 의안 상세(제안이유)는 폼을 발견해 되쏘는 방식이라 다른 소스와
    # 독립적으로 깨질 수 있는데, 전체 합계만 보면 금융위·금감원이 성공하는 한 의안
    # 전면 실패가 묻힌다.
    detail = _DetailStats()
    assembly_detail = _DetailStats()

    for src in cfg.sources:
        if not src.enabled:
            continue
        if only and src.key not in only:
            continue
        selected += 1
        if src.key == ASSEMBLY_SOURCE_KEY:
            # 사용자가 의안을 실행 대상에 포함했다는 사실 자체를 기록한다. 이것이
            # 거짓이면 재조회 큐도 건드리지 않는다(--only/disabled 존중).
            assembly_selected = True
            assembly_source_name = src.name

        log.info("[%s] 수집 시작 (%s)", src.key, src.name)
        try:
            scraper = build_scraper(src, fetcher)
            if src.key == ASSEMBLY_SOURCE_KEY:
                # 목록 수집이 실패해도 상세 재조회는 할 수 있다(목록은 Open API,
                # 상세는 의안정보시스템으로 서로 다른 서비스다). 그래서 collect 성패와
                # 무관하게 스크래퍼를 먼저 잡아 둔다.
                assembly_scraper = scraper
            baselined = state.is_baselined(src.key)
            if baselined:
                # 운영: 이미 아는 것과 대조해 신규만, 경계까지 최대 max_pages 페이지 훑음.
                result = scraper.collect(
                    cfg.fetch.list_limit,
                    _known_ids(state, src.key, assembly_queue),
                    cfg.fetch.max_pages,
                )
            else:
                # 최초 기준선: 얕게(baseline_pages) 기록. 이미 있는 글을 신규로 오인하지
                # 않을 버퍼면 충분하고, 전체 아카이브를 훑어 실행이 폭주하지 않도록 한다.
                result = scraper.collect(
                    cfg.fetch.list_limit, set(), cfg.fetch.baseline_pages
                )
        except Exception as e:  # noqa: BLE001  — 한 소스 실패가 전체를 막지 않도록
            log.error("[%s] 목록 수집 실패: %s", src.key, e)
            errors.append(f"{src.key}: {e}")
            continue

        # HTTP 는 성공했지만 파서가 0건을 반환하면(마크업/AJAX 변경 등) '성공'으로 세지
        # 않는다. 그래야 전 소스가 빈 결과인 파싱 전면장애를 all-failed 가드가 잡아낸다.
        if result.scanned > 0:
            succeeded += 1
        else:
            log.warning("[%s] HTTP 성공했으나 파싱 0건 — 파서/마크업 확인 필요", src.key)

        # 최초 실행: 기준선만 기록하고 메일 생략. 단, 0건이면(일시 오류·셀렉터 불일치·
        # AJAX 미로딩 가능) 기준선을 잡지 않는다(잘못된 빈 기준선은 이후 폭탄이 된다).
        if not baselined:
            if result.scanned == 0:
                log.warning(
                    "[%s] 최초 실행인데 0건 — 기준선 보류(다음 실행에 재시도). "
                    "지속되면 debug 덤프로 셀렉터/AJAX 확인 필요.",
                    src.key,
                )
            else:
                log.info("[%s] 최초 실행 — 기준선 %d건 기록(메일 생략)", src.key, result.scanned)
                state.mark_seen(
                    src.key, [p.post_id for p in result.posts], baselined=True
                )
                # 기준선은 메일과 무관하므로 소스마다 즉시 저장한다(뒤 소스가 실패·취소돼도
                # 앞 기준선 보존 → 최초 실행 반복 방지). 단 dry-run 에서는 저장하지 않는다.
                if not args.dry_run:
                    state.save()
            continue

        new_posts = result.posts  # collect 가 이미 seen 과 대조해 신규만 반환

        if not new_posts:
            log.info("[%s] 신규 없음 (조회 %d건)", src.key, result.scanned)
            continue

        # 폭주 안전장치: 한 번에 비정상적으로 많은 신규가 잡히면(상태 불일치 등) 수백 건의
        # 상세/첨부를 내려받아 실행이 폭주하는 것을 막는다. 최신 cap 건만 상세수집·발송하고,
        # 나머지는 seen 처리한다(전부 seen 처리하므로 다음 실행에 재발생하지 않음).
        cap = cfg.fetch.max_new_per_source
        all_new_ids = [p.post_id for p in new_posts]
        if len(new_posts) > cap:
            log.warning(
                "[%s] 신규 %d건 — 비정상적 대량(상태 불일치 의심). 최신 %d건만 발송하고 "
                "나머지는 seen 처리합니다.",
                src.key,
                len(new_posts),
                cap,
            )
            new_posts = new_posts[:cap]

        # 상세를 수집할 수 없는 소스(회신사례처럼 상세가 JS 팝업인 곳)는 본문이 비는
        # 것이 정상이다. 이런 소스를 '시도했으나 실패'로 세면, 그 소스만 신규가 들어온
        # 실행이 성공률 0% 로 잡혀 파서 장애 경보가 거짓으로 울린다.
        supports_enrich = getattr(scraper, "SUPPORTS_ENRICH", True)
        if not supports_enrich:
            log.info(
                "[%s] 신규 %d건 발견 — 상세 수집 대상 아님(제목·링크만 통지)",
                src.key,
                len(new_posts),
            )
            pending_seen.append((src.key, all_new_ids))
            posts_by_source[src.name] = new_posts
            continue

        log.info("[%s] 신규 %d건 발견 — 상세 수집", src.key, len(new_posts))
        for p in new_posts:
            try:
                scraper.enrich(p)
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] enrich 실패 %s: %s", src.key, p.url, e)
            # 스크래퍼 대부분은 실패를 내부에서 삼키므로(한 건 실패가 나머지를 막지
            # 않도록) 예외 유무가 아니라 '무언가 채워졌는지'로 성공을 센다.
            # verify_sources.py 의 enrich_ok 와 같은 기준이다.
            ok_detail = bool(p.body or p.details or p.attachments)
            # 등록 대기는 실패가 아니다(원문이 아직 공개되지 않았을 뿐).
            is_pending = (
                p.source_key == ASSEMBLY_SOURCE_KEY
                and p.proposal_status is ProposalContentStatus.PENDING
            )
            detail.count(ok_detail, pending=is_pending)
            # 의안 통계는 의안 게시물로만 만든다. 다른 소스(상세가 원래 비는 게시판
            # 등)의 실패가 섞이면 있지도 않은 의안 장애를 보고하게 된다.
            if p.source_key == ASSEMBLY_SOURCE_KEY:
                assembly_detail.count(ok_detail, pending=is_pending)

        # 신규(초과분 포함)는 '메일 성공 후'에만 seen 처리하도록 보류한다.
        pending_seen.append((src.key, all_new_ids))
        posts_by_source[src.name] = new_posts

    total = sum(len(v) for v in posts_by_source.values())
    log.info("총 신규 %d건", total)

    # 수집 집계는 여기서 남긴다. 아래에는 조기 종료가 여럿(전 소스 실패, 메일 설정
    # 누락, SMTP 인증 실패) 있고, 그 상황일수록 수집 진단이 필요하기 때문이다.
    _log_detail_summary(detail, assembly_detail)

    # 선택된 소스가 있는데 하나도 목록 수집에 성공하지 못했는가.
    # (전면 장애를 초록불로 숨기지 않기 위함. 부분 실패는 그대로 격리·진행)
    #
    # **여기서 곧바로 종료하지 않는다.** 목록(열린국회 Open API)과 상세 재조회
    # (의안정보시스템 LIKMS)는 서로 다른 서비스이고, 재조회는 저장된 스냅샷만으로
    # 기존 enrich() 를 부르므로 목록에 의존하지 않는다. 즉시 return 하면 목록 쪽
    # 장애나 설정 오류가 지속되는 동안 due 상태인 큐가 **영구히** 처리되지 않는다.
    # 그래서 '전면 실패'를 control flow 가 아니라 실행의 최종 상태로 기억하고,
    # 할 수 있는 운영 작업은 계속 수행한 뒤 마지막 반환값에 반영한다.
    collection_failed_globally = selected > 0 and succeeded == 0
    if collection_failed_globally:
        log.error(
            "선택된 소스 %d개가 모두 목록 수집 실패 — 실행 결과는 실패로 보고합니다. 오류: %s",
            selected,
            "; ".join(errors),
        )

    # ── 기존 의안 상세 재조회 큐: 이번 실행에서 처리할 대상 선정 ────────────────
    #
    # 사용자가 --only 나 enabled:false 로 의안을 실행 대상에서 제외했다면 큐도 건드리지
    # 않는다. 제외했는데 배경에서 몰래 의안정보시스템을 두드리면 안 된다.
    selection = DueSelection()
    if assembly_selected and assembly_scraper is not None:
        now_ts = time.time()
        interval_sec, max_per_run = retry_limits(assembly_scraper)
        selection = select_due(
            assembly_queue,
            now_ts=now_ts,
            interval_sec=interval_sec,
            max_per_run=max_per_run,
        )
        log_long_waiters(
            assembly_queue, now_ts=now_ts, warn_after_sec=_QUEUE_WARN_AFTER_SEC
        )
    elif assembly_queue:
        if assembly_selected:
            # 소스는 선택했지만 스크래퍼를 만들지 못했다(설정 오류 등). 이때는 재조회할
            # 수단 자체가 없다 — 큐는 그대로 두고 다음 실행에 다시 시도한다.
            log.warning(
                "의안 스크래퍼를 만들지 못해 상세 재조회 큐 %d건을 처리하지 못했습니다.",
                len(assembly_queue),
            )
        else:
            log.info(
                "의안이 이번 실행 대상이 아니므로(--only/enabled) 상세 재조회 큐 "
                "%d건을 건너뜁니다.",
                len(assembly_queue),
            )

    if collection_failed_globally and not selection.selected:
        # 목록이 전멸했고 이번에 처리할 상세 재조회도 없다 — 더 할 수 있는 일이 없으므로
        # 예전과 똑같이 즉시 실패 종료한다(SMTP·Gemini 를 불필요하게 부르지 않는다).
        log.error("처리할 상세 재조회도 없어 실패 종료합니다.")
        return 1

    # 어차피 못 보낼 메일이면 LLM 을 호출하기 전에 멈춘다. 발송 실패는 신규를 seen 으로
    # 확정하지 않으므로, 발송이 막힌 채 방치되면 매 실행이 같은 글을 다시 요약하며
    # 무료 할당량과 시간예산만 반복 소모한다. (--dry-run 은 원래 발송하지 않으므로 제외)
    #
    # 신규가 하나도 없어도 재조회 대상이 있으면 이번 실행에서 '의안 상세 업데이트'가
    # 만들어질 수 있으므로 같은 선점검을 거친다. 메일을 못 보내는 상태에서 재조회를
    # 강행하면 복구된 본문을 전달하지 못한 채 사이트만 두드리게 된다(그리고 본문을
    # state 에 쌓아 두는 우회는 하지 않는다).
    if (total > 0 or selection.selected) and not args.dry_run:
        missing = missing_email_settings(cfg.email)
        if missing:
            log.error(
                "메일 설정 누락(%s) — 발송이 불가능하므로 요약·발송·상세 재조회를 "
                "건너뜁니다. 신규 %d건과 재조회 대상 %d건은 미확정으로 남아 설정 후 "
                "다음 실행에 처리됩니다.",
                ", ".join(missing),
                total,
                len(selection.selected),
            )
            return 1
        # 값이 채워져 있어도 앱 비밀번호 폐기·호스트 도달 불가면 발송은 실패한다.
        # 요약에 할당량을 쓰기 전에 실제로 로그인해 본다.
        try:
            verify_smtp_login(cfg.email)
        except Exception as e:  # noqa: BLE001
            log.error(
                "SMTP 연결/인증 실패(%s: %s) — 발송이 불가능하므로 요약·발송·상세 "
                "재조회를 건너뜁니다. 신규 %d건과 재조회 대상 %d건은 미확정으로 남아 "
                "복구 후 다음 실행에 처리됩니다.",
                type(e).__name__,
                e,
                total,
                len(selection.selected),
            )
            return 1

    # ── 큐 재조회 실행 ─────────────────────────────────────────────────────────
    retry = RetryOutcome()
    if selection.selected:
        # 신규 의안 상세수집이 끝난 뒤 여기까지 흘러간 시간은 의안 상세 작업이 아니다 —
        # config 순서에 따라 뒤 소스의 목록/상세 수집, 집계 로깅, 큐 선정, SMTP 선점검이
        # 끼어든다. 그 시간이 상세 시간예산을 갉아먹으면 LIKMS 요청을 한 번도 보내지
        # 않았는데 예산이 소진되어 큐 항목이 전부 요청 없이 ERROR 로 떨어진다.
        # 마지막 상세 작업 이후의 간격 전체를 여기서 한 번에 뺀다(예산 재시작이 아니라
        # 마감시각을 그만큼 뒤로 미는 것이며, 실제 상세 작업 시간은 그대로 누적된다).
        #
        # 스크래퍼가 교체되어 이 API 가 없으면(테스트 대역 등) 조용히 건너뛴다 —
        # 시간예산 보정이 없다고 실행을 실패시킬 이유는 없다.
        resume_budget = getattr(assembly_scraper, "resume_detail_budget", None)
        if callable(resume_budget):
            resume_budget()
        retry = retry_bills(
            assembly_scraper,
            selection,
            source_key=ASSEMBLY_SOURCE_KEY,
            source_name=assembly_source_name,
        )

    # 복구된 의안은 '신규'가 아니라 별도 카테고리로 싣는다(이미 알린 의안이다).
    detail_updates: dict[str, list[Post]] = (
        {assembly_source_name: retry.recovered} if retry.recovered else {}
    )

    # 본문이 있는 글은 LLM 으로 3줄 요약해 메일에 싣는다. 요약이 실패하면 summary 가
    # 빈 채로 남고 notifier 가 기존 원문 발췌로 되돌아가므로, 발송 자체는 막지 않는다.
    #
    # 신규 의안과 복구 의안을 **하나의** 요약 입력으로 합친다. 따로 부르면 같은 실행에서
    # 의안 배치 경로가 두 번 돌아 Gemini 요청이 불필요하게 늘어난다.
    llm_input = _llm_input(posts_by_source, detail_updates)
    if (total > 0 or retry.recovered) and not args.no_llm:
        try:
            summarize_posts(cfg.llm, llm_input)
        except Exception as e:  # noqa: BLE001
            log.warning("LLM 요약 단계 실패 — 원문 발췌로 발송합니다: %s", e)
    elif args.no_llm:
        log.info("--no-llm: LLM 요약 생략")

    # AI 집계는 요약 단계를 지난 뒤, 발송 직전에 남긴다(발송 성패와 무관하게 기록).
    _log_ai_summary(cfg, llm_input)

    if args.dry_run:
        _print_dry_run(posts_by_source, detail_updates)
        log.info("--dry-run: 메일 미발송, state 미저장")
        # dry-run 이라고 목록 전면 장애를 0 으로 덮어쓰지 않는다.
        return 1 if collection_failed_globally else 0

    log.info(
        "다이제스트 — new=%d detail_updates=%d", total, len(retry.recovered)
    )

    if total > 0 or detail_updates:
        try:
            # 상세 업데이트가 없으면 기존과 **완전히 같은 호출**을 유지한다.
            if detail_updates:
                send_digest(
                    cfg.email, posts_by_source, detail_updates_by_source=detail_updates
                )
            else:
                send_digest(cfg.email, posts_by_source)
        except Exception as e:  # noqa: BLE001
            log.error(
                "메일 발송 실패 — 신규·복구분 미확정, 다음 실행에 재시도: %s", e
            )
            # 사용자에게 전달했다고 간주하는 변화(seen 확정, 신규 큐 등록, 복구분 큐
            # 제거)는 하나도 하지 않는다. 다만 '여전히 미확보'인 항목의 재시도 진단
            # 메타데이터는 알림 트랜잭션과 무관하므로 남긴다 — 그러지 않으면 발송이
            # 막힌 동안 같은 의안을 매 실행 무제한으로 다시 두드리게 된다.
            if _record_retry_metadata(state, retry):
                state.save()
            return 1

    # 메일 성공(또는 보낼 것이 없음) 후에 비로소 사용자 알림과 state 를 일치시킨다.
    for key, ids in pending_seen:
        state.mark_seen(key, ids)
    # 최초 알림을 실제로 보낸 신규 의안 중 상세 미확보분을 큐에 등록한다(§ 발송 성공
    # 후에만 확정). max_new_per_source 초과로 애초에 발송되지 않은 항목과 baseline 으로
    # seen 처리된 과거 의안은 posts_by_source 에 없으므로 자연히 대상이 아니다.
    _queue_new_pending(state, posts_by_source)
    _record_retry_metadata(state, retry)
    # 복구되어 이번 다이제스트에 실린 항목만 큐에서 지운다.
    #
    # **seen 갱신이 큐 제거보다 먼저다.** 큐에서만 빠지고 seen 에도 없으면(오래 대기하다
    # seen 상한 MAX_PER_SOURCE 에서 밀려난 경우) 그 의안이 목록에 남아 있는 한 다음
    # 실행에서 '신규'로 다시 잡혀 최초 알림이 중복된다. mark_seen 은 기존 중복 제거·
    # 상한 규칙을 그대로 쓰며, 재등록이므로 목록 앞쪽으로 올라간다.
    recovered_ids = [p.post_id for p in retry.recovered]
    if recovered_ids:
        state.mark_seen(ASSEMBLY_SOURCE_KEY, recovered_ids)
        for post_id in recovered_ids:
            state.unqueue_detail(ASSEMBLY_SOURCE_KEY, post_id)
    state.save()
    log.info("state 저장 완료")

    if errors:
        log.warning("일부 소스 오류: %s", "; ".join(errors))
    # 상세 재조회·업데이트 발송이 성공했더라도 목록 전면 장애는 그대로 실패로 보고한다
    # (Actions 를 초록불로 만들어 list 쪽 장애를 숨기지 않는다).
    return 1 if collection_failed_globally else 0


def _known_ids(state: State, source_key: str, assembly_queue: list) -> set[str]:
    """목록 수집에 넘길 '이미 아는' ID 집합.

    의안만 재조회 큐의 BILL_ID 를 함께 넘긴다. 큐에 있다는 것은 **최초 알림을 이미
    보냈다**는 뜻이므로 신규가 아니다. 그런데 seen 은 MAX_PER_SOURCE(5000) 상한이 있고
    큐는 의도적으로 자동 만료하지 않으므로, 오래 대기한 의안이 상한에서 밀려난 뒤에도
    계류의안 목록에 남아 있으면 신규로 오인되어 최초 알림이 중복된다(같은 실행에서
    큐가 만든 상세 업데이트와 겹칠 수도 있다).

    다른 소스의 신규 판정 의미는 건드리지 않는다. State.seen_ids() 자체가 모든 소스에서
    큐를 암묵적으로 합치게 만들지도 않는다 — 큐는 의안 전용 개념이다.
    """
    known = state.seen_ids(source_key)
    if source_key == ASSEMBLY_SOURCE_KEY and assembly_queue:
        known = known | {item.bill_id for item in assembly_queue}
    return known


def _llm_input(
    posts_by_source: dict[str, list[Post]],
    detail_updates: dict[str, list[Post]],
) -> dict[str, list[Post]]:
    """신규 + 복구를 합친 요약 입력.

    Post 는 mutable 이고 요약 결과는 객체에 채워지므로, 여기서 합쳐 한 번만 요약해도
    notifier 는 각 카테고리의 원래 리스트를 그대로 쓰면 된다.

    신규를 앞에 둔다 — 배치 상한(max_bills)에 걸릴 때 이번에 처음 알리는 의안이
    먼저 요약되는 편이 낫다.
    """
    if not detail_updates:
        return posts_by_source
    merged = dict(posts_by_source)
    for source_name, posts in detail_updates.items():
        if not posts:
            continue
        merged[source_name] = list(merged.get(source_name, [])) + list(posts)
    return merged


def _queue_new_pending(state: State, posts_by_source: dict[str, list[Post]]) -> None:
    """최초 알림에 성공한 의안 중 제안이유를 확보하지 못한 것을 재조회 큐에 넣는다.

    AVAILABLE 이 아닌 모든 상태(PENDING/ERROR/UNKNOWN)를 등록한다. UNKNOWN 을 버리면
    main 단계의 예상 못 한 예외로 상태가 판정되지 않은 채 남은 의안이 그대로 영구
    누락된다 — 그런 것이야말로 다시 확인해야 할 대상이다.
    """
    now = utcnow_iso()
    queued = 0
    for posts in posts_by_source.values():
        for p in posts:
            if p.source_key != ASSEMBLY_SOURCE_KEY:
                continue
            if p.proposal_status is ProposalContentStatus.AVAILABLE:
                continue
            state.queue_detail(
                ASSEMBLY_SOURCE_KEY,
                p.post_id,
                title=p.title,
                url=p.url,
                date=p.date,
                status=p.proposal_status.value,
                note=_note(p.proposal_note),
                now=now,
            )
            queued += 1
    if queued:
        log.info("의안 상세 재조회 큐 등록 %d건(최초 알림 성공, 제안이유 미확보)", queued)


def _record_retry_metadata(state: State, retry: RetryOutcome) -> bool:
    """재조회 후에도 미확보인 항목의 진단 메타데이터를 갱신한다(큐에는 남긴다).

    반환값은 '갱신한 항목이 있는지'. 알림 없이 이것만 저장해야 하는 경우가 있다.
    """
    now = utcnow_iso()
    for p in retry.still_queued:
        state.record_detail_attempt(
            ASSEMBLY_SOURCE_KEY,
            p.post_id,
            status=p.proposal_status.value,
            note=_note(p.proposal_note),
            now=now,
        )
    return bool(retry.still_queued)


def _note(raw: str) -> str:
    """state 에 남길 진단 문구 — 한 줄로 접고 길이를 제한한다."""
    text = " ".join((raw or "").split())
    return text[:NOTE_MAX_CHARS]


def _log_llm_settings(llm) -> None:
    """실행 초기에 '어느 모델을 부를 것인가'를 INFO 로 못박는다.

    장애가 났을 때 Actions 로그만 보고 실제 호출 모델을 확인할 수 있어야 한다.
    예전에는 성공한 뒤에야 모델명이 INFO 로 찍혀서, 전 요청이 실패한 실행에서는
    어느 모델을 불렀는지 로그에 남지 않았다.

    API key 는 절대 찍지 않는다 — 설정 여부만 남긴다.
    """
    if not llm.enabled:
        log.info("LLM 요약 비활성(config.yaml llm.enabled=false)")
        return
    log.info(
        "LLM 설정 — primary=%s, fallback=%s (primary 출처: %s, API key %s)",
        llm.model,
        ", ".join(llm.fallback_models) or "없음",
        llm.model_source,
        "설정됨" if llm.api_key else "없음",
    )


def _log_detail_summary(
    detail: "_DetailStats", assembly_detail: "_DetailStats"
) -> None:
    """상세 수집(전체/의안) 집계. **수집 루프가 끝나면 무조건** 남긴다.

    AI 집계와 분리한 이유: 메일 설정 누락·SMTP 인증 실패로 run() 이 조기 종료하면
    집계가 통째로 사라졌다. 상세 요청은 이미 다 돌아 통계가 손에 있는데, 하필 장애
    상황에서 진단 로그가 없어지는 셈이었다. 수집 결과는 발송 성패와 무관하므로
    발송 판단보다 먼저 기록한다.

    상세 수집 성공률 0% 는 파서·마크업이 통째로 어긋났다는 뜻이라 ERROR 로 남긴다.
    전체와 의안을 따로 판정하는 이유는, 금융위·금감원이 정상이면 합계는 멀쩡해 보여
    의안 전면 실패가 묻히기 때문이다.

    어느 ERROR 도 발송을 막지 않는다 — 본문 없이라도 제목·링크가 담긴 알림이 나가는
    편이 알림 자체가 끊기는 것보다 낫다(전체 설계 원칙).
    """
    log.info(
        "상세 수집 집계(전체) — 시도 %d건 / 성공 %d건 / 등록대기 %d건 / 실패 %d건",
        detail.attempted,
        detail.succeeded,
        detail.pending,
        detail.failed,
    )
    # 의안은 요구된 이름 그대로 attempted / available / pending / failed 를 남긴다.
    log.info(
        "의안 제안이유 집계 — attempted %d / available %d / pending %d / failed %d",
        assembly_detail.attempted,
        assembly_detail.succeeded,
        assembly_detail.pending,
        assembly_detail.failed,
    )
    # 등록 대기는 애초에 성공할 수 없는 시도이므로 분모에서 뺀 뒤 성공률을 본다.
    # 'pending 이 하나라도 있으면 판정하지 않는다'로 두면, 갓 접수된 의안 한 건 때문에
    # 나머지 소스의 전면 파서 장애가 통째로 묻힌다(등록 대기와 무관한 실패인데도).
    expected = detail.attempted - detail.pending
    if expected > 0 and detail.succeeded == 0:
        log.error(
            "상세 수집 성공률 0%% (등록 대기를 뺀 시도 %d건 전부 실패) — 파서/마크업 "
            "확인 필요. 메일은 제목·링크만으로 계속 발송합니다. debug/ 덤프를 확인하세요.",
            expected,
        )
    # 전체 ERROR 와 별개로 판정한다. 다른 소스가 성공해 위 조건이 거짓이어도
    # 의안만 전멸했다면 반드시 드러나야 한다.
    if (
        assembly_detail.attempted > 0
        and assembly_detail.succeeded == 0
        and assembly_detail.pending == 0
    ):
        log.error(
            "의안 제안이유 수집 available 0건 (시도 %d건 전부 실패, 등록대기 0건) — "
            "상세 페이지 구조/endpoint 변경 의심. 의안은 요약 없이 제목·링크만 "
            "발송됩니다. debug/assembly_bill_detail_*.txt 를 확인하고 "
            "scripts/capture_assembly_network.py 로 실제 XHR 계약을 확인하세요.",
            assembly_detail.attempted,
        )
    elif assembly_detail.failed > 0:
        # available=0 이어도 pending>0 이면 전면 실패로 보지 않는다(요구사항).
        # 다만 실패가 섞여 있다면 조용히 넘기지 않고 경고로 남긴다.
        log.warning(
            "의안 제안이유 수집 실패 %d건 (available %d / pending %d) — "
            "debug/assembly_bill_detail_*.txt 확인.",
            assembly_detail.failed,
            assembly_detail.succeeded,
            assembly_detail.pending,
        )


def _log_ai_summary(cfg, posts_by_source: dict[str, list[Post]]) -> None:
    """AI 요약 집계. 요약 단계를 실제로 지난 뒤에만 의미가 있어 따로 남긴다."""
    targets = ai_target_count(cfg.llm, posts_by_source)
    summarized = sum(
        1 for posts in posts_by_source.values() for p in posts if p.summary
    )
    log.info(
        "AI 요약 집계 — 대상 %d건 / 요약 %d건 / 발췌 폴백 %d건",
        targets,
        summarized,
        max(targets - summarized, 0),
    )


def _print_dry_run(
    posts_by_source: dict[str, list[Post]],
    detail_updates: dict[str, list[Post]] | None = None,
) -> None:
    """수집 결과 출력. 신규(NEW)와 상세 업데이트(DETAIL UPDATE)를 구분해 보여준다."""
    _print_group("NEW", posts_by_source)
    if detail_updates:
        _print_group("DETAIL UPDATE", detail_updates)


def _print_group(label: str, posts_by_source: dict[str, list[Post]]) -> None:
    for source_name, posts in posts_by_source.items():
        print(f"\n=== [{label}] {source_name} ({len(posts)}건) ===")
        for p in posts:
            print(f"  [{p.date or '날짜미상'}] {p.title}")
            if p.details:
                for label_, value in p.details:
                    print(f"      {label_}: {value}")
            elif p.summary:
                for s in p.summary:
                    print(f"      · {s}")
            elif p.body:
                print(f"      (요약 없음) {p.body[:120]}")
            print(f"    {p.url}")
            if p.attachments:
                print(f"    첨부 {len(p.attachments)}개: " + ", ".join(a.filename for a in p.attachments))


if __name__ == "__main__":
    sys.exit(run())
