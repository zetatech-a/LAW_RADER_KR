"""의안(계류의안) 전용 배치 요약.

일반 게시물(금융위·금감원 등)은 예전 그대로 1건당 1회 호출한다. 의안만 여기서
여러 건을 한 번의 요청에 묶는다 — 계류의안은 하루에도 수십 건이 새로 올라와
1건당 1회로는 무료 티어 한도를 곧바로 태우기 때문이다.

설계 원칙(일반 경로와 공유):
  - 실패해도 메일은 나간다. summary 가 비면 notifier 가 '제안이유 및 주요내용 발췌'로
    되돌아간다. 어떤 실패도 이 모듈 밖으로 던지지 않는다.
  - Gemini 세션·모델 fallback·RPM throttle·재시도는 Summarizer 의 것을 그대로 쓴다.
    이 모듈은 프롬프트와 결과 검증만 담당한다.

응답 매핑은 **반드시 bill_id 기준**이다. 배열 순서를 믿고 zip 으로 붙이면 모델이 한
건을 빠뜨리거나 순서를 바꿨을 때 A 의안의 메일에 B 의안의 요약이 실린다 — 규제 알림에서
가장 나쁜 종류의 오류다. 그래서 요청에 없던 ID·중복 ID·형식 위반은 전부 버리고,
빠진 ID 만 한 번 다시 요청한다.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .models import ASSEMBLY_SOURCE_KEY, Post, ProposalContentStatus
from .summarizer import (
    _LIST_PREFIX,
    _MIN_CALL_SEC,
    _STOP_CALLING_KINDS,
    LLMErrorKind,
    _unfence,
    classify_error,
)

if TYPE_CHECKING:  # pragma: no cover - 타입 힌트 전용
    from .config import AssemblyBatchConfig
    from .summarizer import Summarizer

log = logging.getLogger(__name__)

# 문장에 실질 내용(한글·영문·숫자)이 하나라도 있는지. 밑줄은 내용으로 치지 않는다.
_HAS_CONTENT = re.compile(r"[^\W_]")

# 구조화 출력 스키마. 응답 매핑의 기준이 되는 bill_id 를 항목마다 필수로 받는다.
BATCH_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summaries": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "bill_id": {"type": "STRING"},
                    "summary": {"type": "ARRAY", "items": {"type": "STRING"}},
                },
                "required": ["bill_id", "summary"],
            },
        }
    },
    "required": ["summaries"],
}

_PROMPT = """당신은 한국 금융규제 담당 실무자를 돕는 요약가입니다.
아래는 대한민국 국회 의안정보시스템에서 수집한 계류의안 {count}건의
'제안이유 및 주요내용'입니다.

규칙:
- 의안 한 건마다 정확히 {lines}개의 문장으로 요약합니다. 각 문장은 한국어 {max_chars}자 이내입니다.
- 원문에 없는 내용은 절대 지어내지 않습니다.
- 무엇을·누구에게·어떻게 바꾸는지 등 실무자가 알아야 할 사실을 우선합니다.
- 각 문장은 개조식(명사형 종결, 예: "~함", "~예정")으로 씁니다.
- "이 법률안은", "본 개정안은" 같은 서두나 불릿기호는 넣지 않습니다.
- bill_id 는 아래 목록에 적힌 값을 **글자 그대로** 옮겨 적습니다. 목록에 없는
  bill_id 를 만들거나, 같은 bill_id 를 두 번 쓰지 않습니다.
- {count}건 모두에 대해 빠짐없이 한 항목씩 만듭니다.

출력은 다음 형태의 JSON 만 반환합니다:
{{"summaries": [{{"bill_id": "<목록의 값>", "summary": {example}}}]}}

[의안 목록]
{bills}
"""

_BILL_BLOCK = """--- bill_id: {bill_id}
[의안명] {title}
[제안이유 및 주요내용]
{text}
"""


class _BatchFailed(RuntimeError):
    """이 배치는 실패했다. 분할해도 결과가 달라지지 않는다."""


class _Splittable(_BatchFailed):
    """응답 자체가 배치 크기 때문에 망가진 신호(잘림·깨진 JSON·스키마 위반).

    이때만 배치를 절반으로 한 번 나눠 다시 시도한다. HTTP 오류(401/403/429/5xx)나
    네트워크 오류에서는 절대 분할하지 않는다 — 같은 실패를 두 배로 늘릴 뿐이다.
    """


@dataclass
class _Item:
    """배치에 담을 의안 1건."""

    post: Post
    bill_id: str
    title: str
    text: str

    @property
    def chars(self) -> int:
        return len(self.bill_id) + len(self.title) + len(self.text)


# --- 공개 진입점 ---
def summarize_assembly_bills(
    summarizer: "Summarizer", posts_by_source: dict[str, list[Post]]
) -> int:
    """의안 게시물을 배치로 요약하고 성공 건수를 돌려준다."""
    cfg = summarizer.cfg.assembly_batch
    if not cfg.enabled:
        log.info("의안 배치 요약 비활성(llm.assembly_batch.enabled=false)")
        return 0

    items = _targets(posts_by_source, cfg)
    if not items:
        return 0

    deadline = time.monotonic() + cfg.budget_sec if cfg.budget_sec > 0 else None
    batches = _batches(items, cfg.batch_size, cfg.max_batch_chars)
    # 배치 구성을 운영 로그에서 그대로 볼 수 있어야 batch_size 를 데이터로 튜닝할 수 있다.
    log.info(
        "Assembly AI — target=%d batches=%d sizes=%s",
        len(items),
        len(batches),
        [len(b) for b in batches],
    )

    # 서킷 브레이커: 일시 장애(TRANSIENT)가 **연속으로** 쌓일 때만 남은 배치를 포기한다.
    # 한 배치가 503 으로 재시도를 소진했다는 이유만으로 전체를 접으면, 잠깐 흔들린
    # 서버 때문에 그날 의안 전부가 발췌로 나간다(실제 장애). 그래서 다음 배치를 한 번
    # 더 두드려 보고, 그것마저 일시 장애면 그때 브레이커를 연다.
    breaker = cfg.max_consecutive_transient_failures
    consecutive_transient = 0

    ok = 0
    for i, batch in enumerate(batches):
        if deadline is not None and deadline - time.monotonic() < _MIN_CALL_SEC:
            log.warning(
                "의안 요약 시간예산(%.0f초) 소진 — 남은 %d개 배치는 발췌로 발송됩니다.",
                cfg.budget_sec,
                len(batches) - i,
            )
            break
        log.info("Assembly AI batch — batch=%d/%d items=%d", i + 1, len(batches), len(batch))
        result = _run(
            summarizer, batch, deadline, cfg, allow_split=True, allow_retry=True
        )
        ok += result.ok

        if not result.call_failed:
            # 응답 내용이 문제였을 수는 있지만(_Splittable/_BatchFailed) 서비스는 살아
            # 있었다는 뜻이다 — 연속 카운터를 리셋한다.
            consecutive_transient = 0
            continue

        kind = result.failure_kind or LLMErrorKind.TRANSIENT
        if kind in _STOP_CALLING_KINDS:
            # 자격증명·한도·모델 부재·잘못된 요청·내부 오류: 다음 배치도 같은 자격증명으로
            # 같은 서비스를 부르거나 같은 버그를 밟으므로 결과가 달라지지 않는다.
            # 즉시 브레이커를 연다.
            log.warning(
                "의안 배치 호출이 %s 로 실패 — 남은 %d개 배치는 호출하지 않고 "
                "발췌로 발송합니다.",
                kind.value,
                len(batches) - i - 1,
            )
            break

        consecutive_transient += 1
        if breaker > 0 and consecutive_transient >= breaker:
            log.warning(
                "의안 배치가 일시 장애로 연속 %d회 실패 — 남은 %d개 배치는 호출하지 "
                "않고 발췌로 발송합니다.",
                consecutive_transient,
                len(batches) - i - 1,
            )
            break
        log.warning(
            "의안 배치 %d/%d 가 일시 장애로 실패(연속 %d/%d) — 이 배치는 발췌로 넘기고 "
            "다음 배치를 계속 시도합니다.",
            i + 1,
            len(batches),
            consecutive_transient,
            breaker if breaker > 0 else 0,
        )

    log.info("의안 배치 요약 완료 %d/%d건", ok, len(items))
    if ok < len(items):
        log.info("요약 없이 제안이유 발췌로 발송되는 의안 %d건", len(items) - ok)
    return ok


# --- 대상 선정 / 배치 구성 ---
def _targets(
    posts_by_source: dict[str, list[Post]], cfg: "AssemblyBatchConfig"
) -> list[_Item]:
    """source_key 가 의안이고 본문(제안이유)이 있는 글만 모은다."""
    items: list[_Item] = []
    seen: set[str] = set()
    pending = 0
    for posts in posts_by_source.values():
        for p in posts:
            if p.source_key != ASSEMBLY_SOURCE_KEY:
                continue
            # 등록 대기 의안은 요약할 원문이 아예 없다. 호출해도 지어낸 요약만
            # 나오므로 배치 대상에서 명시적으로 제외한다(본문이 비어 있어 어차피
            # 걸러지지만, 의도를 코드에 남긴다).
            if p.proposal_status is ProposalContentStatus.PENDING:
                pending += 1
                continue
            text = " ".join((p.body or "").split())
            # bill_id 가 없으면 결과를 되돌려 붙일 수 없다(순서 매핑은 쓰지 않는다).
            if not text or not p.post_id or p.post_id in seen:
                continue
            seen.add(p.post_id)
            items.append(
                _Item(
                    post=p,
                    bill_id=p.post_id,
                    title=" ".join((p.title or "").split()),
                    text=text[: cfg.max_input_chars_per_bill],
                )
            )
    if pending:
        log.info("등록 대기 의안 %d건은 요약 대상에서 제외(원문 미공개)", pending)
    if len(items) > cfg.max_bills:
        log.warning(
            "의안 요약 대상 %d건 — 상한(%d)을 넘어 최신 %d건만 요약합니다.",
            len(items),
            cfg.max_bills,
            cfg.max_bills,
        )
        items = items[: cfg.max_bills]
    return items


def _batches(items: list[_Item], batch_size: int, max_chars: int) -> list[list[_Item]]:
    """건수(batch_size)와 입력 총량(max_chars)을 **둘 다** 지키는 greedy 묶음.

    한 건만으로 max_chars 를 넘는 의안은 혼자 한 배치가 된다(버리지 않는다 —
    max_input_chars_per_bill 로 이미 절단되어 있다).
    """
    out: list[list[_Item]] = []
    cur: list[_Item] = []
    cur_chars = 0
    for it in items:
        if cur and (len(cur) >= max(1, batch_size) or cur_chars + it.chars > max_chars):
            out.append(cur)
            cur, cur_chars = [], 0
        cur.append(it)
        cur_chars += it.chars
    if cur:
        out.append(cur)
    return out


# --- 한 배치 실행 ---
@dataclass
class _Result:
    """배치 실행 결과.

    ok 만으로는 '요약을 못 받았다'와 '호출 자체가 죽었다'를 구분할 수 없다. 앞은 이
    배치의 문제라 다음 배치는 멀쩡할 수 있고, 뒤는 서비스·자격증명 문제라 다음 배치도
    똑같이 실패한다. 호출자가 남은 배치를 계속할지 정하려면 그 구분이 필요하다.
    """

    ok: int = 0
    call_failed: bool = False   # 인증·한도·5xx·네트워크 등 호출 계층의 실패
    # 그 실패의 의미(AUTH/RATE_LIMIT/TRANSIENT/…). 호출자가 '다음 배치를 불러도
    # 되는가'를 정할 때 쓴다. call_failed 가 False 면 의미가 없다.
    failure_kind: "LLMErrorKind | None" = None

    def __add__(self, other: "_Result") -> "_Result":
        return _Result(
            ok=self.ok + other.ok,
            call_failed=self.call_failed or other.call_failed,
            # 먼저 확인된 실패 원인을 유지한다(분할 앞 조각에서 죽으면 그것이 사유다).
            failure_kind=self.failure_kind or other.failure_kind,
        )


def _run(
    summarizer: "Summarizer",
    items: list[_Item],
    deadline: float | None,
    cfg: "AssemblyBatchConfig",
    *,
    allow_split: bool,
    allow_retry: bool,
) -> _Result:
    """배치 하나를 요약해 결과를 돌려준다. 예외를 밖으로 내보내지 않는다."""
    if not items:
        return _Result()
    if deadline is not None and deadline - time.monotonic() < _MIN_CALL_SEC:
        log.warning("의안 요약 시간예산 소진 — 의안 %d건은 발췌로 발송됩니다.", len(items))
        return _Result()

    try:
        mapping = _call(summarizer, items, deadline, cfg)
    except _Splittable as e:
        if allow_split and len(items) > 1:
            mid = len(items) // 2
            log.warning(
                "의안 배치(%d건) 응답이 온전치 않음(%s) — %d/%d 로 한 번 분할합니다.",
                len(items),
                e,
                mid,
                len(items) - mid,
            )
            # 분할한 조각은 다시 분할하지 않는다(한 번만 분할).
            first = _run(
                summarizer, items[:mid], deadline, cfg,
                allow_split=False, allow_retry=allow_retry,
            )
            # 앞 조각에서 호출이 죽었으면 뒤 조각도 같은 실패를 반복한다.
            if first.call_failed:
                log.warning("분할 앞 조각에서 호출이 실패 — 뒤 조각은 호출하지 않습니다.")
                return first
            return first + _run(
                summarizer, items[mid:], deadline, cfg,
                allow_split=False, allow_retry=allow_retry,
            )
        log.warning("의안 배치(%d건) 요약 실패 — 발췌로 발송합니다: %s", len(items), e)
        return _Result()
    except _BatchFailed as e:
        # 호출은 성공했고 응답 '내용'이 문제인 경우다(안전필터 차단 등 비정상 종료).
        # 분할해도 같은 결과지만, 그 판정은 이 배치에 담긴 의안 내용에 달린 것이므로
        # 다른 의안이 담긴 다음 배치는 통과할 수 있다. 브레이커를 올리면 안 된다.
        log.warning(
            "의안 배치(%d건) 응답이 쓸 수 없음 — 이 배치만 발췌로 발송합니다: %s",
            len(items),
            e,
        )
        return _Result()
    except Exception as e:  # noqa: BLE001 — 인증·한도·5xx·네트워크: 분할하지 않는다
        kind = classify_error(e)
        log.warning(
            "의안 배치(%d건) 호출 실패(%s / %s) — 분할하지 않고 발췌로 발송합니다: %s",
            len(items),
            kind.value,
            type(e).__name__,
            e,
        )
        return _Result(call_failed=True, failure_kind=kind)

    result = _Result(ok=_apply(items, mapping))
    missing = [it for it in items if it.bill_id not in mapping]
    if missing and cfg.retry_missing_once and allow_retry:
        log.info("의안 %d건이 응답에서 빠짐 — 해당 ID 만 한 번 다시 요청합니다.", len(missing))
        result = result + _run(
            summarizer, missing, deadline, cfg, allow_split=False, allow_retry=False
        )
    elif missing:
        log.info("의안 %d건은 요약을 받지 못함 — 발췌로 발송됩니다.", len(missing))
    return result


def _call(
    summarizer: "Summarizer",
    items: list[_Item],
    deadline: float | None,
    cfg: "AssemblyBatchConfig",
) -> dict[str, list[str]]:
    """한 번 호출하고 bill_id → 문장 리스트로 검증된 결과만 돌려준다."""
    data = summarizer._generate(
        _prompt(summarizer, items),
        deadline,
        schema=BATCH_SCHEMA,
        max_output_tokens=cfg.max_output_tokens,
    )
    return _parse(summarizer, data, {it.bill_id for it in items})


def _summary_example(lines: int) -> str:
    """출력 예시의 summary 배열. **반드시 설정된 줄 수와 같아야 한다.**

    예시를 3줄로 못박아 두면 llm.lines 를 3 이외로 바꿨을 때 프롬프트가 서로 어긋난다
    (규칙은 lines 개, 예시는 3개). 모델은 구체적인 예시를 따르기 쉬운데, 그러면
    _valid_lines 가 정확히 lines 개가 아니라는 이유로 **모든 의안을 버려** 배치 요약이
    통째로 발췌 폴백이 된다. 설정을 바꿨을 뿐인데 기능이 조용히 꺼지는 셈이다.
    """
    n = max(1, lines)
    return json.dumps([f"문장{i}" for i in range(1, n + 1)], ensure_ascii=False)


def _prompt(summarizer: "Summarizer", items: list[_Item]) -> str:
    bills = "\n".join(
        _BILL_BLOCK.format(bill_id=it.bill_id, title=it.title or "(제목 없음)", text=it.text)
        for it in items
    )
    return _PROMPT.format(
        count=len(items),
        lines=summarizer.cfg.lines,
        max_chars=summarizer.cfg.max_line_chars,
        example=_summary_example(summarizer.cfg.lines),
        bills=bills,
    )


# --- 응답 검증 ---
def _parse(
    summarizer: "Summarizer", data: dict, requested: set[str]
) -> dict[str, list[str]]:
    """응답을 bill_id → 문장 리스트로 바꾼다. 구조가 깨졌으면 _Splittable."""
    reason = summarizer._finish_reason(data)
    if reason == "MAX_TOKENS":
        # 배치가 커서 출력이 잘린 신호 — 절반으로 나누면 들어갈 수 있다.
        raise _Splittable("응답이 출력 상한에서 잘림(finishReason=MAX_TOKENS)")
    if reason != "STOP":
        # 안전필터 차단 등: 배치 크기와 무관하므로 나눠도 같은 결과다.
        raise _BatchFailed(f"응답이 정상 종료되지 않음(finishReason={reason or '없음'})")

    body = _unfence(summarizer._response_text(data))
    if not body:
        raise _Splittable("응답 본문이 비어 있음")

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        raise _Splittable(f"구조화 응답이 깨짐: {body[:120]}") from None

    rows = parsed.get("summaries") if isinstance(parsed, dict) else parsed
    if not isinstance(rows, list):
        raise _Splittable(f"요청 스키마(summaries 배열)와 다름: {body[:120]}")

    lines_n = summarizer.cfg.lines
    max_chars = summarizer.cfg.max_line_chars
    out: dict[str, list[str]] = {}
    seen: set[str] = set()       # 응답에 한 번이라도 등장한 요청 ID(요약 유효성과 무관)
    for row in rows:
        if not isinstance(row, dict):
            log.info("의안 요약 항목이 객체가 아님 — 버림: %r", row)
            continue
        bill_id = row.get("bill_id")
        if not isinstance(bill_id, str):
            log.info("의안 요약 항목의 bill_id 가 문자열이 아님 — 버림: %r", bill_id)
            continue
        bill_id = bill_id.strip()
        if bill_id not in requested:
            # 요청에 없던 ID. 모델이 지어냈거나 다른 배치의 것이다 — 절대 싣지 않는다.
            log.info("요청에 없던 bill_id 응답 — 버림: %s", bill_id)
            continue
        if bill_id in seen:
            # 같은 의안에 서로 다른 요약이 둘 이상. 어느 쪽이 맞는지 알 수 없으므로
            # 이미 담은 것까지 버리고 '누락'으로 돌린다(재요청 대상이 된다).
            log.info("중복 bill_id 응답 — 해당 의안을 통째로 버림: %s", bill_id)
            out.pop(bill_id, None)
            continue
        # 중복 판정은 요약 검증보다 **먼저** 확정한다. 첫 행이 형식 위반이라 버려졌다는
        # 이유로 등장 사실까지 잊으면, 뒤따르는 같은 ID 의 '멀쩡해 보이는' 행이 그대로
        # 실린다. 그 행의 내용이 실은 다른 의안의 것일 수 있어 잘못된 요약이 붙는다.
        seen.add(bill_id)
        lines = _valid_lines(row.get("summary"), lines_n, max_chars)
        if lines is None:
            log.info(
                "의안 %s 요약이 %d줄·%d자 이내 문자열 배열이 아님 — 버림(재요청 대상)",
                bill_id,
                lines_n,
                max_chars,
            )
            continue
        out[bill_id] = lines
    return out


def _valid_lines(raw, lines_n: int, max_chars: int) -> list[str] | None:
    """정확히 lines_n 개의, 설정 길이를 지킨 문장일 때만 리스트. 아니면 None.

    한 문장이라도 문자열이 아니거나, 정리 후 실질 내용이 없거나, max_line_chars 를
    넘으면 그 의안 전체를 버린다(= 누락으로 돌려 한 번 재요청한다). 절반만 실린 요약은
    '3줄 요약' 라벨과 어긋나고, 길이를 넘긴 줄은 메일 카드 레이아웃을 무너뜨린다.

    길이는 프롬프트로 '지시'만 해서는 지켜지지 않으므로 응답에서 다시 검사한다.
    max_chars <= 0 이면 길이 제한 없음.
    """
    if not isinstance(raw, list) or len(raw) != lines_n:
        return None
    out: list[str] = []
    for x in raw:
        if not isinstance(x, str):
            return None
        s = " ".join(x.split())
        s = _LIST_PREFIX.sub("", s, count=1).strip()
        # 글자도 숫자도 없는 줄("-", "•", "…" 등)은 빈 문장과 같다. 그대로 실으면
        # 메일에 내용 없는 불릿이 'AI 요약'으로 찍힌다.
        if not _HAS_CONTENT.search(s):
            return None
        # 길이는 공백 정리·목록표식 제거를 마친 '실제 발행될' 문자열로 잰다.
        if max_chars > 0 and len(s) > max_chars:
            return None
        out.append(s)
    return out


def _apply(items: list[_Item], mapping: dict[str, list[str]]) -> int:
    """검증을 통과한 요약을 해당 Post 에 바로 저장한다."""
    n = 0
    for it in items:
        lines = mapping.get(it.bill_id)
        if lines:
            it.post.summary = lines
            n += 1
    return n
