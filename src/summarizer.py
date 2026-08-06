"""수집한 게시글 본문을 LLM(Gemini)으로 3줄 요약.

메일 본문에 '앞에서 220자 자른 원문'을 그대로 붙이면 상세 페이지 머리말·안내문만
보이고 정작 무슨 내용인지 알 수 없다. 그래서 본문이 있는 게시글은 LLM 에 넘겨
핵심 3줄로 요약하고, 그 요약을 메일에 싣는다.

설계 원칙:
  - 실패해도 메일은 나간다. 요약이 비면 notifier 가 기존 원문 발췌로 되돌아간다.
    (LLM 장애로 규제 알림 자체가 끊기는 것이 가장 나쁜 결과)
  - 무료 티어(분당 요청수 제한)를 전제로 호출 간격을 두고, 429/5xx 는 백오프 재시도.
  - 본문이 없는 소스(금융규제포털 회신사례 등)는 애초에 호출하지 않는다.

경로가 둘이다:
  - 일반 게시물(금융위·금감원 등): 여기서 1건당 1회 호출한다(기존 동작 그대로).
  - 의안(assembly_bill): src/assembly_summary.py 가 최대 25건씩 배치로 호출한다.
    의안은 하루 신규가 수십 건이라 1건당 1회로는 무료 티어 한도를 곧바로 태운다.
    두 경로는 상한(max_posts / assembly_batch.max_bills)과 시간예산을 따로 쓴다.

필요 환경변수: GEMINI_API_KEY  (https://aistudio.google.com/apikey 에서 무료 발급)
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from itertools import zip_longest

import requests

from .config import LLMConfig
from .models import ASSEMBLY_SOURCE_KEY, Post

log = logging.getLogger(__name__)

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# 이보다 적게 남은 시간예산으로는 호출을 시작하지 않는다(연결조차 못 맺고 끝난다).
_MIN_CALL_SEC = 2.0

# 연결 단계에 몰아줄 시간 상한(초). 연결이 막힌 환경에서 오래 매달리지 않게 한다.
_CONNECT_TIMEOUT_CAP = 8.0


def _split_timeout(budget: float) -> tuple[float, float]:
    """예산을 (연결, 응답대기) 로 쪼갠다.

    requests 에 스칼라 타임아웃을 주면 연결과 응답대기에 '각각' 그 값이 적용되어
    실제 소요가 최대 2배까지 늘어난다(예산 10초 → 연결 10초 + 대기 10초). 두 단계의
    합이 예산을 넘지 않도록 나눠서 넘긴다.
    """
    connect = min(_CONNECT_TIMEOUT_CAP, budget / 2)
    return connect, max(budget - connect, 0.1)

_PROMPT = """당신은 한국 금융규제 담당 실무자를 돕는 요약가입니다.
아래는 금융위원회·금융감독원·금융규제포털·의안정보시스템 등에서 수집한 게시물입니다.
본문에는 사이트 메뉴, 담당부서 안내, 첨부파일 목록 같은 군더더기가 섞여 있을 수 있습니다.

규칙:
- 군더더기는 무시하고 실제 내용만 요약합니다.
- 정확히 {lines}개의 문장으로 요약합니다. 각 문장은 한국어 {max_chars}자 이내입니다.
- 본문에 없는 내용은 절대 지어내지 않습니다.
- 무엇을·누구에게·언제부터 적용되는지 등 실무자가 알아야 할 사실을 우선합니다.
- 각 문장은 개조식(명사형 종결, 예: "~함", "~예정")으로 씁니다.
- "이 글은", "본 자료는" 같은 서두나 불릿기호는 넣지 않습니다.
- 본문이 요약할 만한 내용이 없으면 빈 배열을 반환합니다.

[기관] {source}
[제목] {title}
[본문]
{body}
"""

# 폴백 파싱에서 떼어낼 목록 표식만 좁게 매칭한다.
#   "- ", "• ", "1. ", "2) ", "(3) "  →  제거
# 문자 단위 lstrip 을 쓰면 "2026년 1월부터", "1조원", "3.5%" 같은 실제 수치가 잘려
# 금액·시행일이 훼손되므로 반드시 접두사 패턴으로만 제거한다.
#
# 하이픈류(- – —)는 음수 부호와 생김새가 같으므로 **뒤에 공백이 있을 때만** 표식으로
# 본다. 그러지 않으면 "-3.5% 감소함" → "3.5% 감소함", "-2조원 순손실" → "2조원 순손실"
# 처럼 부호가 사라져 손실이 이익으로 뒤집힌다.
# 반면 • * · ▪ ◦ 는 부호로 쓰이지 않으므로 공백 없이 붙어도("•3.5% 감소함") 안전하다.
# 유니코드 마이너스(−, U+2212)는 애초에 표식 목록에 넣지 않는다.
_LIST_PREFIX = re.compile(r"^(?:[-–—]+\s+|[•*·▪◦]+\s*|\(?\d{1,2}[.)]\s+)")


# 인증·한도·서버 장애는 모델 문제가 아니므로 다른 모델을 연쇄 호출하지 않는다.
# (기존 재시도·시간예산·서킷 브레이커 정책을 그대로 따른다)
_NEVER_CHAIN_STATUS = frozenset({401, 403, 429})

# "이 모델은 없거나 이 프로젝트에 제공되지 않는다"는 뜻의 응답 메시지 조각.
# 404/NOT_FOUND 로 안 오고 400 으로 오는 경우가 있어 메시지도 함께 본다.
_MODEL_GONE_PATTERNS = (
    "no longer available",
    "is not found",
    "not found for api version",
    "model not found",
    "does not exist",
    "is not available",
    "not available for your",
    "unsupported model",
    "is not supported for generatecontent",
)


class _ModelUnavailable(Exception):
    """이 모델은 쓸 수 없다(다음 모델로 넘어가야 함). 내부 제어용."""


# 세대 종속적인 생성 옵션을 붙일 모델(= Gemini 2.5 계열)만 좁게 매칭한다.
#
# thinkingConfig.thinkingBudget 은 Gemini 2.5 계열의 필드다. Gemini 3 계열은 이를
# thinkingLevel(minimal/low/medium/high) 로 대체했고 생각을 끌 수 없어서,
# thinkingBudget: 0 을 보내면 400 INVALID_ARGUMENT 로 거절한다. 그런데 응답 message 가
# "Request contains an invalid argument." 한 줄뿐인 경우가 있어 어느 필드가 문제인지
# 알려주지 않는다 — 그래서 '보내고 메시지 보고 고치기'가 아니라 애초에 보내지 않는다.
#
# gemini-flash-latest 같은 alias 는 요청 시점에 어느 세대를 가리키는지 알 수 없으므로
# (현재는 Gemini 3 계열) 여기에 걸리지 않는다. thinkingLevel 을 대신 보내지도 않는다 —
# 지원하는 값이 모델마다 달라(예: 일부 flash-lite 는 minimal/medium 불가) alias 에
# 보내면 같은 400 을 다른 필드로 다시 만들 뿐이다.
_GEMINI_25 = re.compile(r"^gemini-2\.5-")


def _is_gemini_25(model: str) -> bool:
    return bool(_GEMINI_25.match((model or "").strip().lower()))


# 생각을 끈 모델의 출력 상한. 3줄 요약에는 넉넉하다.
_MAX_OUTPUT_TOKENS = 1024
# 생각을 끌 수 없는 모델(Gemini 3 계열)의 출력 상한. 이 상한은 '생각 토큰 + 응답 토큰'
# 합계에 걸리므로 1024 로 두면 생각만 하다 MAX_TOKENS 로 잘려 본문이 빈 응답이 온다
# (그러면 _parse 가 폐기 → 서킷 브레이커가 열려 전 건이 원문 발췌로 나간다).
_MAX_OUTPUT_TOKENS_THINKING = 4096

# 생성 온도. Gemini 3 계열에는 보내지 않는다 — 공식 가이드가 기본값 1.0 유지를 권하고,
# 낮추면 반복 루프·품질 저하가 생길 수 있다고 명시한다.
_TEMPERATURE = 0.2


def _error_fields(resp) -> tuple[str, str]:
    """응답에서 (error.status, error.message) 를 뽑는다. JSON 이 아니면 ('', 본문 일부)."""
    try:
        err = (resp.json() or {}).get("error") or {}
    except ValueError:
        return "", (resp.text or "")[:200]
    return str(err.get("status") or ""), str(err.get("message") or "")


def _model_unavailable_reason(resp) -> str:
    """모델 자체를 쓸 수 없다는 신호면 사유 문자열, 아니면 빈 문자열.

    여기서 참을 돌려주면 '다음 모델로 전환'하므로, 모델과 무관한 실패(인증·한도·5xx)를
    잘못 포함시키면 한 번의 장애로 설정된 모델을 전부 소진해 버린다. 그래서 그 상태
    코드들은 먼저 배제한다.
    """
    if resp.status_code in _NEVER_CHAIN_STATUS or resp.status_code >= 500:
        return ""
    status, message = _error_fields(resp)
    if resp.status_code == 404 or status == "NOT_FOUND":
        return f"HTTP {resp.status_code} {status} {message}".strip()
    if any(p in message.lower() for p in _MODEL_GONE_PATTERNS):
        return f"HTTP {resp.status_code} {message}".strip()
    return ""


class SummaryUnavailable(RuntimeError):
    """호출은 됐지만 쓸 수 있는 요약을 얻지 못함(차단·잘림·스키마 위반).

    서킷 브레이커가 '실패'로 세도록 예외로 올린다 — 전량 차단 같은 상황에서 성공으로
    세면 브레이커가 열리지 않는다.
    """


def _unfence(text: str) -> str:
    """마크다운 코드펜스(```json … ```)를 벗긴다.

    닫는 펜스가 없는(=잘린) 응답도 처리해야 한다. 펜스 표식이 남으면 폴백 파싱에서
    그대로 메일 본문에 실린다.
    """
    s = (text or "").strip()
    if not s.startswith("```"):
        return s
    s = s.split("\n", 1)[1] if "\n" in s else ""
    s = s.rstrip()
    if s.endswith("```"):
        s = s[:-3]
    return s.strip()


def _interleave(groups: list[list[Post]]) -> list[Post]:
    """소스별 목록을 번갈아 한 줄로 편다(각 소스 안에서는 원래 순서=최신순 유지).

    단순히 이어붙이면 요약 상한이 config 의 소스 순서대로 소진되어, 앞쪽 소스 하나가
    상한을 다 먹으면 뒤쪽 소스는 글이 더 최신이어도 전부 원문 발췌로 나간다.
    (게시일 문자열은 소스마다 형식이 달라 전역 최신순 정렬은 신뢰할 수 없으므로,
     소스 간에는 라운드로빈으로 고르게 배분한다.)
    """
    out: list[Post] = []
    for row in zip_longest(*groups):
        out.extend(p for p in row if p is not None)
    return out


def _as_sentences(raw) -> list[str] | None:
    """요청한 스키마(문자열 배열)일 때만 문장 리스트로 받아들인다.

    아니면 None — 호출자는 요약을 버리고 원문 발췌로 되돌린다. 느슨하게 순회하면
    {"summary": {"first": "금리 인하"}} 같은 응답에서 dict 의 '키'("first")가 AI
    요약문인 양 메일에 실리고, [1, None, {...}] 같은 배열도 "1"·"None" 으로 찍힌다.
    """
    # 배열 대신 문자열 하나만 온 경우: 스키마 위반이지만 내용 자체는 모델이 쓴 문장이라
    # (키 이름 같은 구조적 잡음이 섞일 여지가 없어) 그대로 한 줄로 받는다.
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list) and all(isinstance(x, str) for x in raw):
        return raw
    return None

# 구조화 출력(JSON) 스키마 — 줄바꿈·불릿 파싱에 의존하지 않도록 배열로 받는다.
_SCHEMA = {
    "type": "OBJECT",
    "properties": {"summary": {"type": "ARRAY", "items": {"type": "STRING"}}},
    "required": ["summary"],
}


class Summarizer:
    """Gemini REST API 로 게시글 본문을 요약한다(requests 만 사용, 추가 의존성 없음)."""

    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.session = requests.Session()
        self._last_call = 0.0
        # 무료 티어 분당 요청수(RPM) 를 지키기 위한 최소 호출 간격
        self._min_interval = 60.0 / cfg.rpm if cfg.rpm > 0 else 0.0
        # 시도할 모델 목록(primary → fallback, 중복 제거).
        self._models = cfg.model_chain
        # 성공한 모델. 한 번 성공하면 이 프로세스 동안 계속 이 모델만 쓴다.
        self._active_model: str | None = None
        # 사용 불가로 확인된 모델(404 등). 이후 게시글에서 다시 시도하지 않는다.
        self._unavailable: set[str] = set()
        # thinkingConfig 를 받지 않는 모델. 400 을 한 번 겪으면 그 모델에는 빼고 보낸다
        # (모델별로 따로 기억한다). 애초에 Gemini 2.5 계열에만 보내므로 평상시엔 빈 집합.
        self._thinking_unsupported: set[str] = set()

    @property
    def available(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.api_key)

    # --- 공개 API ---
    def summarize_all(self, posts_by_source: dict[str, list[Post]]) -> int:
        """소스별 게시글 묶음을 순회하며 본문이 있는 글의 summary 를 채운다.

        일반 게시물(금융위·금감원 등)은 예전 그대로 1건당 1회 호출하고, 의안만
        따로 모아 배치로 요약한다. 반환값은 두 경로에서 요약에 성공한 글 수의 합.
        실패는 로그만 남기고 넘어간다(빈 summary → 메일에서 기존 발췌로 표시).
        """
        ok = self._summarize_general(posts_by_source)
        try:
            # 지연 임포트: assembly_summary 는 이 모듈을 임포트한다(순환 방지).
            from .assembly_summary import summarize_assembly_bills

            ok += summarize_assembly_bills(self, posts_by_source)
        except Exception as e:  # noqa: BLE001 — 의안 요약 실패가 메일을 막지 않는다
            log.warning("의안 배치 요약 단계 실패 — 발췌로 발송합니다: %s", e)
        return ok

    def _summarize_general(self, posts_by_source: dict[str, list[Post]]) -> int:
        """일반 게시물 전용 경로 — 기존 동작 그대로 1건당 1회 요약한다.

        의안(assembly_bill)은 여기서 반드시 제외한다. 배치 경로가 따로 처리하므로
        여기 남겨 두면 같은 글을 두 번 호출하고 max_posts 상한까지 잠식한다.
        """
        # 상한을 적용하기 '전에' 실제 호출 대상만 남긴다. 공백뿐이거나 너무 짧아
        # 어차피 호출하지 않을 글이 앞자리를 차지하면, 정작 요약이 필요한 뒤쪽 글이
        # 할당량을 남겨두고도 요약되지 않는다.
        groups = [
            [
                p
                for p in posts
                if p.source_key != ASSEMBLY_SOURCE_KEY and self._prepared_body(p)
            ]
            for posts in posts_by_source.values()
        ]
        groups = [g for g in groups if g]
        if not groups:
            log.info("일반 요약 대상 없음(요약할 만한 본문이 있는 신규 글 없음)")
            return 0

        # 소스별로 번갈아 뽑는다. 그냥 이어붙이면 config 앞쪽 소스가 상한을 다 먹어,
        # 뒤쪽 소스는 글이 더 최신이어도 요약이 하나도 없는 채로 발송된다.
        targets = _interleave(groups)

        if len(targets) > self.cfg.max_posts:
            # 무료 티어 일일 한도 보호. 초과분은 요약 없이(원문 발췌로) 발송된다.
            log.warning(
                "요약 대상 %d건 — 상한(%d)을 넘어 소스별로 고르게 %d건만 요약합니다.",
                len(targets),
                self.cfg.max_posts,
                self.cfg.max_posts,
            )
            targets = targets[: self.cfg.max_posts]

        # 서킷 브레이커: Gemini 가 통째로 죽었을 때 모든 글에 같은 실패를 반복하면
        # (타임아웃 × 재시도 × 건수) 메일이 크게 지연된다. 연속 실패가 쌓이거나 총
        # 시간예산을 넘기면 남은 글은 호출 없이 원문 발췌로 넘긴다.
        deadline = (
            time.monotonic() + self.cfg.budget_sec if self.cfg.budget_sec > 0 else None
        )
        breaker = self.cfg.max_consecutive_failures

        ok = 0
        consecutive = 0
        attempted = 0
        for i, post in enumerate(targets):
            if breaker > 0 and consecutive >= breaker:
                log.warning(
                    "연속 %d회 실패 — LLM 장애로 판단해 중단합니다. 남은 %d건은 "
                    "원문 발췌로 발송됩니다.",
                    consecutive,
                    len(targets) - i,
                )
                break
            # 예산이 한 번의 호출을 담기에도 모자라면 아예 시작하지 않는다. 시작한
            # 호출은 아래 summarize→_generate 가 남은 예산으로 타임아웃·재시도를
            # 제한하므로, 요약 단계 전체가 budget_sec 을 넘지 않는다.
            if deadline is not None and deadline - time.monotonic() < _MIN_CALL_SEC:
                log.warning(
                    "요약 시간예산(%.0f초) 소진 — 중단합니다. 남은 %d건은 원문 발췌로 "
                    "발송됩니다.",
                    self.cfg.budget_sec,
                    len(targets) - i,
                )
                break

            attempted += 1
            try:
                lines = self.summarize(post, deadline)
            except Exception as e:  # noqa: BLE001 — 요약 실패가 메일 발송을 막지 않는다
                consecutive += 1
                log.warning("[%s] 요약 실패 %s: %s", post.source_key, post.url, e)
                continue
            consecutive = 0
            if lines:
                post.summary = lines
                ok += 1
            else:
                log.info("[%s] 요약 결과 비어 있음 — 원문 발췌 사용: %s", post.source_key, post.url)

        log.info(
            "일반 게시물 요약 완료 %d/%d건 (시도 %d건, 사용 모델=%s)",
            ok,
            len(targets),
            attempted,
            self._active_model or "없음",
        )
        if ok < len(targets):
            log.info("요약 없이 원문 발췌로 발송되는 글 %d건", len(targets) - ok)
        return ok

    def _prepared_body(self, post: Post) -> str:
        """호출에 쓸 본문(공백 정규화 + 길이 절단). 요약 대상이 아니면 빈 문자열."""
        body = " ".join((post.body or "").split())
        if len(body) < self.cfg.min_body_chars:
            return ""
        return body[: self.cfg.max_input_chars]

    def summarize(self, post: Post, deadline: float | None = None) -> list[str]:
        """게시글 1건을 요약해 문장 리스트를 돌려준다(실패 시 예외).

        deadline 은 time.monotonic() 기준 마감 시각. 주어지면 이 호출의 타임아웃과
        재시도가 남은 시간 안으로 제한된다.
        """
        body = self._prepared_body(post)
        if not body:
            return []

        prompt = _PROMPT.format(
            lines=self.cfg.lines,
            max_chars=self.cfg.max_line_chars,
            source=post.source_name,
            title=post.title,
            body=body,
        )
        data = self._generate(prompt, deadline)
        return self._parse(data)

    # --- 내부 ---
    def _model_candidates(self) -> list[str]:
        """이번 호출에 시도할 모델 순서.

        성공한 모델이 있으면 그것부터(요구사항: 성공 모델 캐시), 사용 불가로 확인된
        모델은 제외한다. 활성 모델이 뒤늦게 사라져도 같은 호출에서 남은 모델로 넘어간다.
        """
        out: list[str] = []
        if self._active_model and self._active_model not in self._unavailable:
            out.append(self._active_model)
        for name in self._models:
            if name not in self._unavailable and name not in out:
                out.append(name)
        return out

    def _generate(
        self,
        prompt: str,
        deadline: float | None = None,
        *,
        schema: dict | None = None,
        max_output_tokens: int | None = None,
    ) -> dict:
        """모델 목록을 순서대로 시도한다. 404 계열에서만 다음 모델로 넘어간다.

        schema·max_output_tokens 는 호출별 구조화 출력 설정이다. 생략하면 기존 단건
        요약과 완전히 같은 payload 를 보낸다(의안 배치 요약만 값을 넘긴다).
        """
        candidates = self._model_candidates()
        if not candidates:
            raise SummaryUnavailable(
                f"설정된 모델({', '.join(self._models) or '없음'})이 모두 사용 불가 — "
                "원문 발췌를 사용합니다"
            )

        last_reason = ""
        for i, model in enumerate(candidates):
            try:
                data = self._generate_with(
                    model,
                    prompt,
                    deadline,
                    schema=schema,
                    max_output_tokens=max_output_tokens,
                )
            except _ModelUnavailable as e:
                last_reason = str(e)
                self._unavailable.add(model)
                if self._active_model == model:
                    self._active_model = None
                rest = [m for m in candidates[i + 1 :] if m not in self._unavailable]
                if rest:
                    log.warning(
                        "모델 %s 사용 불가(%s) — 다음 모델 %s 로 전환합니다",
                        model,
                        last_reason,
                        rest[0],
                    )
                continue

            if self._active_model != model:
                log.info("요약 모델 확정: %s", model)
                self._active_model = model
            return data

        log.error(
            "설정된 모델(%s)이 모두 사용 불가 — 원문 발췌를 사용합니다. 마지막 사유: %s",
            ", ".join(candidates),
            last_reason,
        )
        raise SummaryUnavailable(f"사용 가능한 모델 없음: {last_reason}")

    def _generation_config(
        self,
        model: str,
        send_thinking: bool,
        schema: dict | None = None,
        max_output_tokens: int | None = None,
    ) -> dict:
        """모델이 받아들이는 옵션만 담은 generationConfig.

        구조화 출력(responseMimeType·responseSchema)은 모든 대상 모델에서 지원되므로
        항상 보낸다. 세대에 따라 뜻이 달라지거나 아예 없는 옵션(thinkingConfig,
        temperature)은 그 세대의 모델에만 붙인다.

        schema·max_output_tokens 를 생략하면 기존 단건 요약의 기본값을 그대로 쓴다.
        """
        gc: dict = {
            "responseMimeType": "application/json",
            "responseSchema": schema or _SCHEMA,
            # 생각을 못 끄는 모델에서는 이 값이 생각 토큰까지 함께 덮는다.
            "maxOutputTokens": max_output_tokens
            or (_MAX_OUTPUT_TOKENS if send_thinking else _MAX_OUTPUT_TOKENS_THINKING),
        }
        if send_thinking:
            # 단순 요약이라 생각은 필요 없다. 생각을 켠 채 출력 상한이 낮으면 본문이
            # 빈 채로 MAX_TOKENS 로 끝난다.
            gc["thinkingConfig"] = {"thinkingBudget": 0}
        if _is_gemini_25(model):
            gc["temperature"] = _TEMPERATURE
        return gc

    def _generate_with(
        self,
        model: str,
        prompt: str,
        deadline: float | None = None,
        *,
        schema: dict | None = None,
        max_output_tokens: int | None = None,
    ) -> dict:
        """모델 하나로 호출한다. 그 모델을 쓸 수 없으면 _ModelUnavailable."""
        send_thinking = (
            _is_gemini_25(model) and model not in self._thinking_unsupported
        )
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": self._generation_config(
                model, send_thinking, schema, max_output_tokens
            ),
        }

        log.debug("Gemini 호출 (model=%s)", model)
        url = _ENDPOINT.format(model=model)
        headers = {
            "x-goog-api-key": self.cfg.api_key,
            "Content-Type": "application/json",
        }

        last_error = ""
        attempt = 0
        while attempt <= self.cfg.max_retries:
            # RPM 간격 대기를 '먼저' 한다. 이 대기가 예산을 다 먹을 수 있으므로,
            # 타임아웃은 반드시 대기가 끝난 뒤의 남은 시간으로 계산해야 한다.
            self._throttle(deadline)

            # 대기 중에 마감이 지났으면 새 소켓을 열지 않는다.
            budget = self.cfg.timeout_sec
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(last_error or "요약 시간예산 소진")
                budget = min(budget, remaining)

            self._last_call = time.monotonic()
            resp = self._post_within(url, headers, payload, budget)
            if resp.status_code == 200:
                return resp.json()

            last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"

            # 모델 자체가 없거나 이 프로젝트에 제공되지 않음 → 다음 모델로 넘긴다.
            # (재시도해도 같은 결과이므로 여기서 즉시 빠진다)
            reason = _model_unavailable_reason(resp)
            if reason:
                raise _ModelUnavailable(reason)

            # thinkingConfig 미지원 모델 → 빼고 즉시 재시도. 설정 교정이지 장애가
            # 아니므로 재시도 횟수를 소모하지 않는다(max_retries=0 에서도 동작).
            #
            # 메시지 본문을 조건에 넣지 않는다. 실제 거절 응답이 어느 필드가 문제인지
            # 알려주지 않는 경우("Request contains an invalid argument." 뿐)가 있어,
            # "thinking" 문자열을 찾는 방식은 영원히 걸리지 않는다.
            # 대신 '우리가 이 요청에 붙인 유일한 선택 옵션이 thinkingConfig 이고 400 을
            # 받았다'는 사실만으로 그 옵션을 떼고 한 번만 재시도한다(send_thinking 이
            # False 가 되므로 두 번은 없다). 400 에서 다른 모델로 넘어가지는 않는다.
            if resp.status_code == 400 and send_thinking:
                log.info("모델 %s 는 thinkingConfig 미지원 — 제외하고 재시도", model)
                self._thinking_unsupported.add(model)
                send_thinking = False
                payload["generationConfig"] = self._generation_config(
                    model, False, schema, max_output_tokens
                )
                continue

            # 한도 초과/일시 장애만 재시도. 400·401·403 은 설정 문제라 즉시 실패.
            if resp.status_code not in (429, 500, 502, 503, 504):
                break
            if attempt < self.cfg.max_retries:
                wait = self.cfg.retry_backoff_sec * (2**attempt)
                # 백오프 대기 + 최소한의 재요청 시간이 예산 밖이면 재시도를 포기한다.
                if (
                    deadline is not None
                    and time.monotonic() + wait + _MIN_CALL_SEC > deadline
                ):
                    log.info("남은 요약 시간예산이 부족해 재시도를 생략합니다")
                    break
                log.info("Gemini %s — %.1f초 후 재시도(%d/%d)",
                         resp.status_code, wait, attempt + 1, self.cfg.max_retries)
                time.sleep(wait)
            attempt += 1

        raise RuntimeError(last_error or "Gemini 호출 실패")

    def _post_within(self, url: str, headers: dict, payload: dict, budget: float):
        """요청 전체를 budget(초) 안으로 가둔다.

        requests 의 read 타임아웃은 '소켓 읽기 사이의 무응답 시간'마다 새로 적용되므로,
        응답 바이트를 read 상한보다 짧은 간격으로 조금씩 흘려보내는 서버(또는 중간
        프록시)에는 총 소요시간을 제한하지 못한다. (connect, read) 로 나눠 줘도 마찬가지다
        — 두 값은 '한 번의' 연결·읽기에만 걸리기 때문이다. 그래서 요청을 데몬 스레드에
        맡기고 단조시계 마감까지만 기다린다.

        스레드는 데몬이라 남아 있어도 프로세스 종료를 막지 않고, 소켓 타임아웃이 걸리면
        스스로 끝난다. (ThreadPoolExecutor 는 종료 시 워커를 join 하므로 쓰지 않는다 —
        멈춘 워커 하나가 워크플로 전체를 붙잡는다.)
        """
        box: dict = {}

        def _work() -> None:
            try:
                box["resp"] = self.session.post(
                    url, headers=headers, json=payload, timeout=_split_timeout(budget)
                )
            except BaseException as e:  # noqa: BLE001 — 호출자에게 원형 그대로 전달
                box["error"] = e

        worker = threading.Thread(target=_work, daemon=True)
        worker.start()
        worker.join(timeout=budget)
        if worker.is_alive():
            # 소켓은 살아 있을 수 있지만 우리는 더 기다리지 않는다(메일 지연 방지).
            raise SummaryUnavailable(f"요청이 시간예산({budget:.1f}초)을 초과함")
        if "error" in box:
            raise box["error"]
        return box["resp"]

    def _throttle(self, deadline: float | None = None) -> None:
        """무료 티어 RPM 을 넘지 않도록 직전 호출로부터 간격을 벌린다.

        간격 대기도 시간예산 안에서만 한다 — 예산이 6초 남았는데 RPM 간격으로
        6초를 통째로 자 버리면 정작 호출은 못 한다. 대기 후 예산이 남았는지는
        호출자(_generate)가 다시 확인한다.

        _last_call 은 실제로 요청을 보낼 때만 갱신한다. 여기서 갱신해 버리면
        예산 초과로 요청을 포기한 경우에도 다음 호출이 공연히 한 간격을 더 기다린다.
        """
        if self._min_interval <= 0 or not self._last_call:
            return
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_call)
        if deadline is not None:
            wait = min(wait, deadline - now)
        if wait > 0:
            time.sleep(wait)

    def _parse(self, data: dict) -> list[str]:
        """응답에서 요약 문장 리스트를 뽑는다.

        쓸 만한 요약을 얻지 못하면 SummaryUnavailable 을 던진다. 호출은 됐지만 결과가
        없는 상태(차단·잘림·스키마 위반)를 '성공'으로 세면, 서비스가 모든 요청을
        막을 때 서킷 브레이커가 열리지 않아 상한(max_posts)까지 전부 태우게 된다.
        어느 경우든 post.summary 는 비어 있으므로 메일은 원문 발췌로 나간다.
        """
        # 정상 종료(STOP)가 '확인된' 응답만 받는다. SAFETY(안전필터 차단)나
        # MAX_TOKENS(토큰 초과)는 물론, finishReason 자체가 없는 응답도 버린다 —
        # 종료가 확인되지 않은 응답의 텍스트는 조각일 수 있고, 이를 통과시키면
        # 유효한 JSON 이나 산문처럼 보이기만 하면 그대로 발송되며 실패로도 세지 않아
        # 서킷 브레이커까지 리셋된다.
        reason = self._finish_reason(data)
        if reason != "STOP":
            raise SummaryUnavailable(
                f"응답이 정상 종료되지 않음(finishReason={reason or '없음'})"
            )

        body = _unfence(self._response_text(data))
        if not body:
            raise SummaryUnavailable("응답 본문이 비어 있음")

        # 마크다운 펜스를 벗긴 뒤에도 JSON 으로 시작하면 구조화 출력 시도로 본다.
        looks_json = body.startswith(("{", "["))
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            if looks_json:
                # 잘린 구조화 출력('{"summary": ["첫째 문장,' 등)을 줄 단위로 폴백하면
                # JSON 조각이 그대로 요약문으로 메일에 실린다. 버린다.
                raise SummaryUnavailable(f"구조화 응답이 깨짐: {body[:120]}") from None
            # 애초에 JSON 이 아니면 평문 요약일 가능성이 높다 — 줄 단위로 폴백 파싱.
            return self._clean(body.splitlines())

        raw = parsed.get("summary") if isinstance(parsed, dict) else parsed
        lines = _as_sentences(raw)
        if lines is None:
            # JSON 이긴 한데 요청한 스키마(문자열 배열)가 아니다. 무턱대고 순회하면
            # dict 의 '키'가 요약문인 양 발행된다.
            raise SummaryUnavailable(f"요청 스키마(문자열 배열)와 다름: {body[:120]}")
        return self._clean(lines)

    @staticmethod
    def _finish_reason(data: dict) -> str:
        for cand in data.get("candidates") or []:
            return str(cand.get("finishReason") or "")
        return ""

    def _clean(self, lines: list[str]) -> list[str]:
        """공백 정리 + 목록 표식 제거 + 설정된 줄 수로 절단."""
        out: list[str] = []
        for line in lines:
            s = " ".join(line.split())
            s = _LIST_PREFIX.sub("", s, count=1).strip()
            if s:
                out.append(s)
        return out[: self.cfg.lines]

    @staticmethod
    def _response_text(data: dict) -> str:
        for cand in data.get("candidates") or []:
            parts = (cand.get("content") or {}).get("parts") or []
            joined = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
            if joined.strip():
                return joined.strip()
        return ""


def summarize_posts(cfg: LLMConfig, posts_by_source: dict[str, list[Post]]) -> int:
    """main 에서 부르는 진입점. 비활성/키 없음이면 조용히 건너뛴다."""
    s = Summarizer(cfg)
    if not s.available:
        if cfg.enabled:
            log.warning(
                "GEMINI_API_KEY 가 없어 LLM 요약을 건너뜁니다 — 메일에는 원문 발췌가 실립니다."
            )
        else:
            log.info("LLM 요약 비활성(config.yaml llm.enabled=false)")
        return 0
    return s.summarize_all(posts_by_source)
