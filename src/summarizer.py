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
import time
from typing import Iterable

import requests

from .config import LLMConfig
from .models import Post

log = logging.getLogger(__name__)

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

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
        targets = [p for posts in posts_by_source.values() for p in posts if p.body]
        if not targets:
            log.info("요약 대상 없음(본문이 있는 신규 글 없음)")
            return 0

        if len(targets) > self.cfg.max_posts:
            # 무료 티어 일일 한도 보호. 초과분은 요약 없이(원문 발췌로) 발송된다.
            log.warning(
                "요약 대상 %d건 — 상한(%d)을 넘어 최신 %d건만 요약합니다.",
                len(targets),
                self.cfg.max_posts,
                self.cfg.max_posts,
            )
            targets = targets[: self.cfg.max_posts]

        ok = 0
        for post in targets:
            try:
                lines = self.summarize(post)
            except Exception as e:  # noqa: BLE001 — 요약 실패가 메일 발송을 막지 않는다
                log.warning("[%s] 요약 실패 %s: %s", post.source_key, post.url, e)
                continue
            if lines:
                post.summary = lines
                ok += 1
            else:
                log.info("[%s] 요약 결과 비어 있음 — 원문 발췌 사용: %s", post.source_key, post.url)

        log.info("LLM 요약 완료 %d/%d건 (model=%s)", ok, len(targets), self.cfg.model)
        return ok

    def summarize(self, post: Post) -> list[str]:
        """게시글 1건을 요약해 문장 리스트를 돌려준다(실패 시 예외)."""
        body = " ".join((post.body or "").split())
        if len(body) < self.cfg.min_body_chars:
            return []
        if len(body) > self.cfg.max_input_chars:
            body = body[: self.cfg.max_input_chars]

        prompt = _PROMPT.format(
            lines=self.cfg.lines,
            max_chars=self.cfg.max_line_chars,
            source=post.source_name,
            title=post.title,
            body=body,
        )
        data = self._generate(prompt)
        return self._parse(data)

    # --- 내부 ---
    def _generate(self, prompt: str) -> dict:
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
            self._throttle()
            resp = self.session.post(
                url, headers=headers, json=payload, timeout=self.cfg.timeout_sec
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
                log.info("Gemini %s — %.1f초 후 재시도(%d/%d)",
                         resp.status_code, wait, attempt + 1, self.cfg.max_retries)
                time.sleep(wait)
            attempt += 1

        raise RuntimeError(last_error or "Gemini 호출 실패")

    def _throttle(self) -> None:
        """무료 티어 RPM 을 넘지 않도록 호출 간격을 벌린다."""
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_call
        if self._last_call and elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.monotonic()

    def _parse(self, data: dict) -> list[str]:
        """응답 봉투에서 요약 문장 리스트를 뽑는다."""
        text = self._response_text(data)
        if not text:
            # 안전필터 차단 등으로 후보가 비는 경우
            reason = ""
            for cand in data.get("candidates") or []:
                reason = cand.get("finishReason") or ""
                break
            if reason and reason != "STOP":
                log.info("Gemini 응답 본문 없음 (finishReason=%s)", reason)
            return []

        lines: list[str] = []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                raw = parsed.get("summary")
            else:
                raw = parsed
            if isinstance(raw, str):
                raw = [raw]
            if isinstance(raw, Iterable):
                lines = [str(x) for x in raw]
        except (json.JSONDecodeError, TypeError):
            # 스키마를 지키지 못한 응답 — 줄 단위로 폴백 파싱
            lines = text.splitlines()

        out: list[str] = []
        for line in lines:
            s = " ".join(str(line).split()).lstrip("-•*·0123456789. ").strip()
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
