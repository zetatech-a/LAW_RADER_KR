"""config.yaml + 환경변수 로딩."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class EmailConfig:
    recipients: list[str]
    from_name: str
    subject_prefix: str
    max_attach_mb: int
    # 아래는 환경변수에서 주입 (민감정보)
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    mail_from: str

    @property
    def max_attach_bytes(self) -> int:
        return self.max_attach_mb * 1024 * 1024


@dataclass
class FetchConfig:
    delay_sec: float
    timeout_sec: float
    list_limit: int
    max_pages: int
    baseline_pages: int
    max_new_per_source: int


# 기본 모델은 특정 버전을 고정하지 않고 공식 'flash latest' alias 를 쓴다. 특정 버전을
# 박아두면 그 버전이 수명 종료된 날부터 전 요청이 404 로 죽는다(실제로 겪었다).
_DEFAULT_MODEL = "gemini-flash-latest"
# alias 자체가 막히거나 대상 버전이 사라졌을 때 넘어갈 검증된 stable 모델(순서 보존).
_DEFAULT_FALLBACK_MODELS = ("gemini-3.6-flash", "gemini-3.5-flash-lite")

# primary 모델을 덮어쓸 환경변수(앞이 우선). GitHub Actions 의 repository Variable
# `MODEL` 을 workflow 가 GEMINI_MODEL 로 넘겨주므로, 운영자가 코드를 고치지 않고
# 모델을 바꿀 수 있다. MODEL 도 함께 보는 이유는 로컬 실행/기존 설정 호환성이다.
#
# 여기서는 '어느 변수를 보는지'만 정한다. 모델명 자체는 아래 resolve_model 의 순서에
# 따라 환경변수 → config.yaml → _DEFAULT_MODEL 중 하나에서 온다.
_MODEL_ENV_VARS = ("GEMINI_MODEL", "MODEL")


def _model_from_env() -> tuple[str, str]:
    """(모델명, 출처) — 앞선 환경변수가 우선. 빈 문자열/공백은 미지정으로 본다."""
    for name in _MODEL_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value, f"{name} 환경변수"
    return "", ""


def resolve_model(yaml_model) -> tuple[str, str]:
    """primary 모델과 그 출처를 정한다.

    precedence:  GEMINI_MODEL > MODEL > config.yaml 의 llm.model > 코드 기본값.
    어느 단계든 빈 문자열/공백은 '지정되지 않음'으로 보고 다음으로 넘어간다.
    """
    env_model, source = _model_from_env()
    if env_model:
        return env_model, source
    configured = str(yaml_model or "").strip()
    if configured:
        return configured, "config.yaml llm.model"
    return _DEFAULT_MODEL, "코드 기본값"


@dataclass
class AssemblyBatchConfig:
    """의안(계류의안) 전용 배치 요약 설정.

    일반 게시물은 1건당 1회 호출을 유지하고, 의안만 여러 건을 한 번에 묶어 보낸다.
    의안은 신규가 하루에도 수십 건씩 쏟아져 1건당 1회로는 무료 티어 한도를 곧바로
    태우기 때문이다. 그래서 상한(max_posts)·시간예산도 일반 경로와 따로 둔다.
    """

    enabled: bool = True
    # 한 번의 요청에 담을 최대 의안 수
    batch_size: int = 25
    # 한 번의 실행에서 요약할 최대 의안 수(초과분은 발췌로 발송)
    max_bills: int = 50
    # 의안 1건의 제안이유를 프롬프트에 담을 최대 길이(초과분 절단)
    max_input_chars_per_bill: int = 20000
    # 한 요청의 입력 총량 상한. batch_size 와 함께 지켜진다(둘 중 먼저 걸리는 쪽).
    max_batch_chars: int = 250000
    # 배치 응답은 의안 수만큼 길어지므로 단건보다 큰 출력 상한이 필요하다.
    max_output_tokens: int = 16384
    # 의안 배치 요약 단계 전체 시간예산(초, 0=무제한)
    budget_sec: float = 120.0
    # 응답에서 빠진 의안이 있으면 그 ID 만 한 번 다시 요청할지
    retry_missing_once: bool = True
    # 서킷 브레이커 — 일시 장애(TRANSIENT: 5xx·타임아웃 등)로 배치 호출이 **연속으로**
    # 이만큼 실패하면 남은 배치는 호출하지 않고 발췌로 넘긴다(0=무제한).
    #
    # 1회로 두면 예전 동작(첫 배치 503 → 전량 중단)으로 되돌아간다. 한 번의 일시 장애가
    # 나머지 의안을 전부 발췌로 떨어뜨리지 않도록 다음 배치를 한 번은 더 두드려 본다.
    # 반대로 인증(AUTH)·한도(RATE_LIMIT)·잘못된 요청(BAD_REQUEST)·모델 부재처럼 다음
    # 배치도 똑같이 실패할 실패는 이 카운터와 무관하게 즉시 브레이커를 연다.
    max_consecutive_transient_failures: int = 2


@dataclass
class LLMConfig:
    """본문 3줄 요약용 LLM(Gemini) 설정. api_key 는 환경변수에서 주입."""

    enabled: bool
    model: str
    lines: int              # 요약 문장 수(기본 3줄)
    max_line_chars: int     # 문장 1개 길이 상한(프롬프트 지시용)
    min_body_chars: int     # 이보다 짧은 본문은 요약하지 않고 그대로 보여준다
    max_input_chars: int    # 모델에 넣을 본문 최대 길이(초과분 절단)
    # 한 번의 실행에서 요약할 최대 '일반' 게시글 수(무료 티어 보호).
    # 의안(assembly_bill)은 이 상한을 쓰지 않는다 — assembly_batch.max_bills 로 따로 센다.
    max_posts: int
    rpm: int                # 분당 요청 상한(0=제한 없음). 이 간격만큼 호출을 벌린다
    timeout_sec: float
    max_retries: int
    retry_backoff_sec: float
    # 서킷 브레이커: 연속 실패가 이만큼 쌓이면 남은 글은 호출 없이 원문 발췌로 넘긴다
    # (0=무제한). LLM 전면 장애 때 메일이 크게 지연되는 것을 막는다.
    max_consecutive_failures: int = 3
    # 요약 단계 전체 시간예산(초, 0=무제한). 초과하면 남은 글은 원문 발췌로 넘긴다.
    budget_sec: float = 240.0
    # model 이 사용 불가(404/NOT_FOUND)일 때 이 순서로 넘어갈 대체 모델들.
    fallback_models: list[str] = field(default_factory=list)
    api_key: str = ""
    # 의안 전용 배치 요약 설정(일반 게시물 경로에는 영향 없음).
    assembly_batch: AssemblyBatchConfig = field(default_factory=AssemblyBatchConfig)
    # primary 모델이 어디서 왔는지(운영 로그용). 장애 때 "어느 모델을 왜 불렀나"를
    # Actions 로그만 보고 판단할 수 있어야 한다.
    model_source: str = "config.yaml llm.model"

    @property
    def model_chain(self) -> list[str]:
        """시도할 모델 목록(순서 보존, 중복 제거). primary → fallback 순."""
        chain: list[str] = []
        for name in [self.model, *self.fallback_models]:
            name = (name or "").strip()
            if name and name not in chain:
                chain.append(name)
        return chain


@dataclass
class SourceConfig:
    key: str
    name: str
    type: str
    list_url: str
    enabled: bool = True
    extra: dict = field(default_factory=dict)


@dataclass
class Config:
    email: EmailConfig
    fetch: FetchConfig
    llm: LLMConfig
    sources: list[SourceConfig]


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def load_config(path: str | Path = "config.yaml") -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))

    em = raw.get("email", {})
    # 수신자: MAIL_TO 환경변수(쉼표/세미콜론 구분)가 있으면 그것을 우선 사용하고,
    # 없으면 config.yaml 의 recipients 를 쓴다.
    mail_to_env = _env("MAIL_TO")
    if mail_to_env:
        recipients = [x.strip() for x in re.split(r"[,;]", mail_to_env) if x.strip()]
    else:
        recipients = em.get("recipients", [])

    email = EmailConfig(
        recipients=recipients,
        from_name=em.get("from_name", "LAW RADER KR"),
        subject_prefix=em.get("subject_prefix", "[LAW RADER]"),
        max_attach_mb=int(em.get("max_attach_mb", 15)),
        smtp_host=_env("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(_env("SMTP_PORT", "587")),
        smtp_user=_env("SMTP_USER"),
        smtp_password=_env("SMTP_PASSWORD"),
        # MAIL_FROM 미지정 시 SMTP_USER 를 발신주소로 사용
        mail_from=_env("MAIL_FROM") or _env("SMTP_USER"),
    )

    fe = raw.get("fetch", {})
    fetch = FetchConfig(
        delay_sec=float(fe.get("delay_sec", 1.0)),
        timeout_sec=float(fe.get("timeout_sec", 30.0)),
        list_limit=int(fe.get("list_limit", 30)),
        max_pages=int(fe.get("max_pages", 10)),
        baseline_pages=int(fe.get("baseline_pages", 3)),
        max_new_per_source=int(fe.get("max_new_per_source", 50)),
    )

    lm = raw.get("llm", {})
    # 키가 아예 없으면 기본 대체 목록을 쓰고, 빈 목록을 명시하면 대체를 끈다.
    raw_fallbacks = lm.get("fallback_models")
    if raw_fallbacks is None:
        raw_fallbacks = list(_DEFAULT_FALLBACK_MODELS)
    ab = lm.get("assembly_batch") or {}
    _ab_default = AssemblyBatchConfig()
    assembly_batch = AssemblyBatchConfig(
        enabled=bool(ab.get("enabled", _ab_default.enabled)),
        batch_size=int(ab.get("batch_size", _ab_default.batch_size)),
        max_bills=int(ab.get("max_bills", _ab_default.max_bills)),
        max_input_chars_per_bill=int(
            ab.get("max_input_chars_per_bill", _ab_default.max_input_chars_per_bill)
        ),
        max_batch_chars=int(ab.get("max_batch_chars", _ab_default.max_batch_chars)),
        max_output_tokens=int(ab.get("max_output_tokens", _ab_default.max_output_tokens)),
        budget_sec=float(ab.get("budget_sec", _ab_default.budget_sec)),
        retry_missing_once=bool(
            ab.get("retry_missing_once", _ab_default.retry_missing_once)
        ),
        max_consecutive_transient_failures=int(
            ab.get(
                "max_consecutive_transient_failures",
                _ab_default.max_consecutive_transient_failures,
            )
        ),
    )
    # primary 모델: GEMINI_MODEL > MODEL > config.yaml > 코드 기본값.
    model, model_source = resolve_model(lm.get("model"))
    llm = LLMConfig(
        enabled=bool(lm.get("enabled", True)),
        model=model,
        model_source=model_source,
        fallback_models=[
            str(m).strip() for m in raw_fallbacks if str(m).strip()
        ],
        lines=int(lm.get("lines", 3)),
        max_line_chars=int(lm.get("max_line_chars", 90)),
        min_body_chars=int(lm.get("min_body_chars", 80)),
        max_input_chars=int(lm.get("max_input_chars", 8000)),
        max_posts=int(lm.get("max_posts", 40)),
        rpm=int(lm.get("rpm", 10)),
        timeout_sec=float(lm.get("timeout_sec", 45)),
        max_retries=int(lm.get("max_retries", 2)),
        retry_backoff_sec=float(lm.get("retry_backoff_sec", 5)),
        max_consecutive_failures=int(lm.get("max_consecutive_failures", 3)),
        budget_sec=float(lm.get("budget_sec", 240)),
        api_key=_env("GEMINI_API_KEY"),
        assembly_batch=assembly_batch,
    )

    sources = []
    for s in raw.get("sources", []):
        known = {"key", "name", "type", "list_url", "enabled"}
        sources.append(
            SourceConfig(
                key=s["key"],
                name=s["name"],
                type=s["type"],
                list_url=s["list_url"],
                enabled=s.get("enabled", True),
                extra={k: v for k, v in s.items() if k not in known},
            )
        )

    return Config(email=email, fetch=fetch, llm=llm, sources=sources)
