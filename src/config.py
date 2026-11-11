"""config.yaml + 환경변수 로딩."""
from __future__ import annotations

import math
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
    # 메일 하단 '수신 기관 설정' 링크. 구독 기능이 켜졌을 때만 채워지며, 비어 있으면
    # 링크를 싣지 않는다(= 기존 메일 출력 그대로).
    preferences_url: str = ""

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
_DEFAULT_FALLBACK_MODELS = ("gemini-3.6-flash", "gemini-3.8-flash")

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
    # 의안 배치 요약 단계 전체 시간예산(초, 0=무제한). 이 값은 **전체 상한**이지
    # "이만큼 계속 재시도한다"는 뜻이 아니다. top-level 배치마다 아래
    # request_timeout_sec 안에서 공정 분배된 window 를 받는다(assembly_summary).
    budget_sec: float = 120.0
    # 의안 배치 요청 하나에 허용할 시간(초). None/미지정이면 llm.timeout_sec 을 쓴다
    # (Phase 2 까지의 동작). 의안 배치는 25건을 한 번에 보내므로 일반 단건 요약보다
    # 응답이 오래 걸린다 — 일반 경로의 45초를 늘리지 않고 여기만 늘리기 위한 값이다.
    request_timeout_sec: float | None = None
    # 응답 생성 단계의 타임아웃(requests.ReadTimeout · 자체 wall-clock 마감)에서
    # **같은 요청을 다시 보낼지**. 기본 True 는 Phase 2 까지의 재시도 계약 그대로다.
    #
    # False 로 두면 그 호출은 즉시 실패로 접는다(실패 종류는 여전히 TRANSIENT 다 —
    # 배치 브레이커의 의미를 바꾸지 않는다). 25건짜리 요청이 90초를 넘겼다면 같은
    # 25건을 다시 보내도 또 넘길 뿐이고, 그 사이 뒤 배치의 시간을 통째로 먹는다.
    # 연결 실패·5xx 같은 진짜 일시 장애의 bounded retry 는 이 값과 무관하게 유지된다.
    retry_response_timeout: bool = True
    # 의안 배치 요청에만 붙일 thinking 수준(예: "minimal"). None/미지정이면 기존
    # generation payload 를 그대로 보낸다. 실제 전송 여부는 모델이 그 값을 지원하는
    # 것으로 확인된 경우로 한정된다(src/summarizer.py 의 allowlist).
    thinking_level: str | None = None
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
    # 일반 + 의안 Gemini 단계가 **한 실행에서 합쳐** 쓸 수 있는 상위 시간 상한(초).
    #
    # 위 budget_sec(일반)과 assembly_batch.budget_sec(의안)은 각 단계가 단독으로
    # 돌 때 필요한 ceiling이라 낮출 수 없다. 그런데 두 단계는 연달아 실행되므로
    # 단순 합(240+300=540)이 그대로 최악값이 되고, 의안 상세수집 예산까지 더하면
    # 15분 monitor 주기를 통째로 먹는다(워크플로는 cancel-in-progress:false 라
    # 다음 실행이 큐에 쌓인다). 그래서 두 단계가 **같은 절대 마감**을 공유하도록
    # 상위 envelope 를 씌운다.
    #
    # 0 = 비활성(공유 상한 없음) — 새 키를 생략한 기존 custom config 는 예전
    # 동작 그대로다. 실제 단계 마감은 min(단계 마감, 공유 마감) 이다.
    total_budget_sec: float = 0.0
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


@dataclass(frozen=True)
class AgencyConfig:
    """구독자가 고르는 기관 1개와 그 기관에 속한 소스 key 들(config.yaml 이 단일 원천)."""

    id: str
    name: str
    sources: tuple[str, ...]


@dataclass
class SubscriptionConfig:
    """구독자별 수신 기관 선택(구독 API) 설정. 기본은 비활성 — 기존 발송 그대로.

    토큰과 HMAC 키는 환경변수/Secret 으로만 주입하고, repr 에도 나오지 않게 한다
    (설정 객체가 로그·예외 메시지에 찍혀도 비밀이 새지 않도록).
    """

    enabled: bool = False
    api_url: str = ""
    api_token: str = field(default="", repr=False)
    subscriber_hash_key: str = field(default="", repr=False)
    timeout_sec: float = 10.0
    agencies: tuple[AgencyConfig, ...] = ()
    # 운영 보호장치(GitHub Variable SUBSCRIPTION_REQUIRED). true 인데 enabled 가 false 면
    # main 이 실행을 중단한다 — 활성화 후 플래그 실수로 전체 발송이 재개되는 것을 막는다.
    required: bool = False
    # 운영 D1 식별자(GitHub Variable SUBSCRIPTION_DB_INSTANCE, 32자 hex). 있으면 Worker 의 D1 이
    # 같은 데이터인지 매번 확인한다 — D1 이 재생성·교체되면 설정이 전부 mode=all 로 보이는 대신 발송을 멈춘다.
    db_instance: str = ""


@dataclass
class Config:
    email: EmailConfig
    fetch: FetchConfig
    llm: LLMConfig
    sources: list[SourceConfig]
    subscription: SubscriptionConfig = field(default_factory=SubscriptionConfig)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


# 기관 ID 는 API·D1 에 그대로 저장되는 식별자라 형식을 좁게 묶는다.
_AGENCY_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def _load_agencies(raw) -> tuple[AgencyConfig, ...]:
    """subscription.agencies 를 읽는다. 형식 오류는 기능이 꺼져 있어도 즉시 알린다."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("subscription.agencies 는 목록이어야 합니다")
    agencies: list[AgencyConfig] = []
    for i, item in enumerate(raw):
        where = f"subscription.agencies[{i}]"
        if not isinstance(item, dict):
            raise ValueError(f"{where} 는 id/name/sources 를 가진 항목이어야 합니다")
        agency_id = item.get("id")
        if not isinstance(agency_id, str) or not _AGENCY_ID_RE.match(agency_id):
            raise ValueError(f"{where}.id 형식이 올바르지 않습니다 — 받은 값: {agency_id!r}")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{where}.name 이 비어 있습니다")
        sources = item.get("sources")
        if (
            not isinstance(sources, list)
            or not sources
            or not all(isinstance(s, str) and s.strip() for s in sources)
        ):
            raise ValueError(f"{where}.sources 는 비어 있지 않은 source key 목록이어야 합니다")
        agencies.append(
            AgencyConfig(
                id=agency_id,
                name=name.strip(),
                sources=tuple(s.strip() for s in sources),
            )
        )
    return tuple(agencies)


def validate_agency_mapping(
    agencies: tuple[AgencyConfig, ...],
    sources: list[SourceConfig],
    *,
    require_complete: bool,
) -> None:
    """기관 ↔ source_key 매핑 검증.

    항상 보는 것: 기관 ID 중복, 한 소스가 두 기관에 속함, 존재하지 않는 source key.
    require_complete 이면 추가로 **활성화된 모든 소스가 정확히 한 기관에** 속하는지 본다
    — 빠진 소스는 'selected' 구독자 누구에게도 가지 않는 조용한 누락이 되기 때문이다.
    """
    known = {s.key for s in sources}
    seen_ids: set[str] = set()
    owner: dict[str, str] = {}
    for agency in agencies:
        if agency.id in seen_ids:
            raise ValueError(f"subscription.agencies 의 기관 ID 가 중복됩니다: {agency.id}")
        seen_ids.add(agency.id)
        for key in agency.sources:
            if key not in known:
                raise ValueError(
                    f"subscription.agencies[{agency.id}] 에 없는 source key 가 있습니다: {key}"
                )
            if key in owner:
                raise ValueError(
                    f"source key {key} 가 두 기관({owner[key]}, {agency.id})에 속합니다"
                )
            owner[key] = agency.id
    if require_complete:
        unmapped = [s.key for s in sources if s.enabled and s.key not in owner]
        if unmapped:
            raise ValueError(
                "구독 기능을 켜려면 활성 소스가 모두 기관에 매핑되어야 합니다 — 누락: "
                + ", ".join(unmapped)
            )


def _env_required_flag(name: str) -> bool:
    """엄격한 true/false 환경변수. 비어 있으면 false, 그 밖의 값은 설정 오류.

    오타('ture', 'yes', '1')를 조용히 false 로 읽으면 보호장치가 꺼진 채 전체 발송이
    재개될 수 있으므로 true/false 외에는 받지 않는다.
    """
    raw = _env(name).strip().lower()
    if raw in ("", "false"):
        return False
    if raw == "true":
        return True
    raise ValueError(f"{name} 는 true 또는 false 여야 합니다(비워 두면 false)")


_DB_INSTANCE_RE = re.compile(r"^[0-9a-f]{32}$")


def _env_db_instance(name: str) -> str:
    """D1 식별자 환경변수. 비어 있으면 미설정, 형식이 틀리면 설정 오류(조용히 무시하지 않음)."""
    raw = _env(name).strip()
    if raw and not _DB_INSTANCE_RE.match(raw):
        raise ValueError(f"{name} 는 32자 소문자 hex 여야 합니다(비워 두면 미설정)")
    return raw


def _load_subscription(raw, sources: list[SourceConfig]) -> SubscriptionConfig:
    """subscription 블록 + 환경변수. 블록이 없으면 비활성 기본값."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("subscription 설정은 항목(dict)이어야 합니다")
    enabled = _as_bool(raw.get("enabled"), "subscription.enabled", False)
    timeout = _as_optional_positive_float(raw.get("timeout_sec"), "subscription.timeout_sec")
    agencies = _load_agencies(raw.get("agencies"))
    validate_agency_mapping(agencies, sources, require_complete=enabled)
    return SubscriptionConfig(
        enabled=enabled,
        api_url=_env("SUBSCRIPTION_API_URL").strip(),
        api_token=_env("SUBSCRIPTION_API_TOKEN").strip(),
        subscriber_hash_key=_env("SUBSCRIBER_HASH_KEY").strip(),
        timeout_sec=timeout if timeout is not None else SubscriptionConfig.timeout_sec,
        agencies=agencies,
        required=_env_required_flag("SUBSCRIPTION_REQUIRED"),
        db_instance=_env_db_instance("SUBSCRIPTION_DB_INSTANCE"),
    )


# Gemini 가 정의한 thinking 수준. 여기 없는 값은 설정 오타로 보고 즉시 실패한다 —
# 모르는 값을 그대로 보내면 400 INVALID_ARGUMENT 가 되어 그날 의안 요약이 통째로
# 발췌로 떨어진다(설정 한 줄의 오타가 조용히 기능을 끄면 안 된다).
_THINKING_LEVELS = frozenset({"minimal", "low", "medium", "high"})

# YAML 이 아닌 경로(.env·환경변수 주입 등)로 들어온 문자열 불리언까지 받아 준다.
_TRUE_WORDS = frozenset({"true", "yes", "on", "1"})
_FALSE_WORDS = frozenset({"false", "no", "off", "0"})


def _as_bool(raw, key: str, default: bool) -> bool:
    """불리언 설정값. 지정되지 않았으면 default.

    기존 키들은 bool(...) 을 그대로 쓰지만(호환), 새 키는 "false" 같은 문자열이
    조용히 True 가 되지 않도록 명시적으로 해석한다. 해석할 수 없는 값은 설정 오타이므로
    조용히 기본값으로 넘어가지 않고 즉시 알린다.
    """
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        word = raw.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
    raise ValueError(f"{key} 는 true/false 여야 합니다 — 받은 값: {raw!r}")


def _reject_bool(raw, key: str) -> None:
    """숫자 설정에 들어온 불리언을 거절한다.

    bool 은 int 의 하위형이라 float(True) == 1.0, float(False) == 0.0 으로 조용히
    통과한다. 그러면 `request_timeout_sec: true` 오타가 '1초 타임아웃'이 되어 25건
    배치가 매번 죽고, `budget_sec: false` 는 0 = '무제한'과 겹쳐 Assembly AI 전체
    시간 상한을 통째로 없앤다. 새 설정은 잘못된 값을 즉시 알린다는 계약을 지킨다.
    """
    if isinstance(raw, bool):
        raise ValueError(f"{key} 는 숫자여야 합니다(true/false 불가) — 받은 값: {raw!r}")


def _as_optional_positive_float(raw, key: str) -> float | None:
    """양수 설정값. 미지정(None)이면 None — 호출부가 기존 기본값으로 되돌아간다."""
    if raw is None:
        return None
    _reject_bool(raw, key)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{key} 는 숫자여야 합니다 — 받은 값: {raw!r}") from None
    # math.isfinite 로 NaN 과 ±무한대를 한 번에 거른다. 무한대는 범위 검사(> 0)를
    # 통과해 버리는데, 이 설정들은 애초에 실행 시간을 **제한하려고** 존재한다 —
    # `request_timeout_sec: .inf` 는 Thread.join(timeout=inf) 에서 OverflowError 가
    # 되어 의안 요약이 통째로 발췌로 떨어진다.
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{key} 는 0보다 큰 유한한 숫자여야 합니다 — 받은 값: {raw!r}")
    return value


def _as_non_negative_float(raw, key: str, default: float) -> float:
    """0 이상 설정값(0 = 무제한이라는 기존 의미를 유지)."""
    if raw is None:
        return default
    _reject_bool(raw, key)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{key} 는 숫자여야 합니다 — 받은 값: {raw!r}") from None
    # NaN·±무한대는 여기서도 거절한다. 무한대 예산은 '상한 없음'이 되어 이 설정이
    # 지키려는 시간 guard(공유 LLM envelope·의안 단계 예산)를 조용히 없앤다.
    # '무제한'을 원하면 기존 의미대로 0 을 쓴다.
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{key} 는 0 이상의 유한한 숫자여야 합니다 — 받은 값: {raw!r}")
    return value


def _as_thinking_level(raw, key: str) -> str | None:
    """thinking 수준. 미지정이면 None(= 기존 generation payload 유지)."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"{key} 는 문자열이어야 합니다 — 받은 값: {raw!r}")
    level = raw.strip().lower()
    if not level:
        return None
    if level not in _THINKING_LEVELS:
        allowed = ", ".join(sorted(_THINKING_LEVELS))
        raise ValueError(f"{key} 는 {allowed} 중 하나여야 합니다 — 받은 값: {raw!r}")
    return level


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
        budget_sec=_as_non_negative_float(
            ab.get("budget_sec"), "llm.assembly_batch.budget_sec", _ab_default.budget_sec
        ),
        request_timeout_sec=_as_optional_positive_float(
            ab.get("request_timeout_sec"), "llm.assembly_batch.request_timeout_sec"
        ),
        retry_response_timeout=_as_bool(
            ab.get("retry_response_timeout"),
            "llm.assembly_batch.retry_response_timeout",
            _ab_default.retry_response_timeout,
        ),
        thinking_level=_as_thinking_level(
            ab.get("thinking_level"), "llm.assembly_batch.thinking_level"
        ),
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
        # 새 키라 Phase 3 의 strict helper 를 쓴다(0 허용, true/false 는 거절).
        total_budget_sec=_as_non_negative_float(
            lm.get("total_budget_sec"), "llm.total_budget_sec", 0.0
        ),
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

    subscription = _load_subscription(raw.get("subscription"), sources)
    # 설정 링크는 기능이 켜졌을 때만 싣는다. 꺼진 상태에서 링크만 나가면 사용자가
    # 바꾼 설정이 발송에 반영되지 않아 혼란을 준다.
    if subscription.enabled:
        email.preferences_url = _env("SUBSCRIPTION_PAGE_URL").strip()

    return Config(
        email=email, fetch=fetch, llm=llm, sources=sources, subscription=subscription
    )
