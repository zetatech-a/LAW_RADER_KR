"""수집한 게시글 본문을 LLM(Gemini)으로 3줄 요약.

메일 본문에 '앞에서 220자 자른 원문'을 그대로 붙이면 상세 페이지 머리말·안내문만
보이고 정작 무슨 내용인지 알 수 없다. 그래서 본문이 있는 게시글은 LLM 에 넘겨
핵심 3줄로 요약하고, 그 요약을 메일에 싣는다.

설계 원칙:
  - 실패해도 메일은 나간다. 요약이 비면 notifier 가 기존 원문 발췌로 되돌아간다.
    (LLM 장애로 규제 알림 자체가 끊기는 것이 가장 나쁜 결과)
  - 무료 티어(분당 요청수 제한)를 전제로 호출 간격을 두고, 429/5xx 는 백오프 재시도.
  - 본문이 없는 소스(금융규제포털 회신사례, 의안정보시스템)는 애초에 호출하지 않는다.

필요 환경변수: GEMINI_API_KEY  (https://aistudio.google.com/apikey 에서 무료 발급)
"""
from __future__ import annotations

import json
import logging
import re
import time
from itertools import zip_longest

import requests

from .config import LLMConfig
from .models import Post

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
        # 모델이 thinkingConfig 를 모르면 400 이 난다. 한 번 겪으면 이후로는 빼고 보낸다.
        self._send_thinking_config = True

    @property
    def available(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.api_key)

    # --- 공개 API ---
    def summarize_all(self, posts_by_source: dict[str, list[Post]]) -> int:
        """소스별 게시글 묶음을 순회하며 본문이 있는 글의 summary 를 채운다.

        반환값은 요약에 성공한 글 수. 실패는 로그만 남기고 넘어간다(빈 summary →
        메일에서 기존 원문 발췌로 표시).
        """
        # 상한을 적용하기 '전에' 실제 호출 대상만 남긴다. 공백뿐이거나 너무 짧아
        # 어차피 호출하지 않을 글이 앞자리를 차지하면, 정작 요약이 필요한 뒤쪽 글이
        # 할당량을 남겨두고도 요약되지 않는다.
        groups = [
            [p for p in posts if self._prepared_body(p)]
            for posts in posts_by_source.values()
        ]
        groups = [g for g in groups if g]
        if not groups:
            log.info("요약 대상 없음(요약할 만한 본문이 있는 신규 글 없음)")
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
            "LLM 요약 완료 %d/%d건 (시도 %d건, model=%s)",
            ok,
            len(targets),
            attempted,
            self.cfg.model,
        )
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
    def _generate(self, prompt: str, deadline: float | None = None) -> dict:
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
                "responseSchema": _SCHEMA,
                "maxOutputTokens": 1024,
            },
        }
        if self._send_thinking_config:
            # 2.5 계열은 기본적으로 '생각'에 출력 토큰을 소진해 본문이 빈 채로 끝날 수
            # 있다. 단순 요약이라 생각 예산은 0 으로 둔다.
            payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}

        url = _ENDPOINT.format(model=self.cfg.model)
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
            resp = self.session.post(
                url, headers=headers, json=payload, timeout=_split_timeout(budget)
            )
            if resp.status_code == 200:
                return resp.json()

            last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"

            # thinkingConfig 미지원 모델(2.0 계열 등) → 빼고 즉시 재시도. 설정 교정이지
            # 장애가 아니므로 재시도 횟수를 소모하지 않는다(max_retries=0 에서도 동작).
            if (
                resp.status_code == 400
                and self._send_thinking_config
                and "thinking" in resp.text.lower()
            ):
                log.info("모델 %s 는 thinkingConfig 미지원 — 제외하고 재시도", self.cfg.model)
                self._send_thinking_config = False
                payload["generationConfig"].pop("thinkingConfig", None)
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
        reason = self._finish_reason(data)
        # STOP 이 아니면 안전필터 차단(SAFETY)이나 토큰 초과(MAX_TOKENS)로 중간에
        # 끊긴 응답이다. 남아 있는 텍스트는 조각이라 신뢰할 수 없다.
        if reason and reason != "STOP":
            raise SummaryUnavailable(f"응답이 정상 종료되지 않음(finishReason={reason})")

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
