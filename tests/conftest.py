"""테스트 공용 설정 — 출고 config.yaml 의 구독 기능이 켜진 뒤(Phase 4E)에도 시험이 안전하게 돌도록 한다.

1) 개발자 셸의 수신자·구독 환경변수 차단(모든 시험, 자동 적용)
   출고 설정이 subscription.enabled: true 이면 main.run 은 '보낼 것이 없는 실행'에서도 MAIL_TO 를
   구독 API 에 동기화한다(=Worker 의 D1 쓰기). 셸에 SUBSCRIPTION_API_URL·토큰·키·MAIL_TO 가 남아 있으면
   시험이 실제 Worker 를 부를 수 있으므로, 이 값들을 지운 상태로 시작한다. 필요한 시험은
   monkeypatch.setenv 로 직접 넣는다(시험은 네트워크를 쓰지 않는다 — AGENTS.md).

2) legacy_delivery 픽스처(모듈이 pytestmark 로 선택)
   구독과 무관한 기존 파이프라인 회귀(수집·요약·발송·state)는 기능을 끈 전체 발송 경로를 검증해 왔다.
   출고 설정이 켜진 뒤에도 같은 경로를 계속 검증하도록 main.run 이 읽은 설정에서만 구독 기능을 끈다.
   구독 경로는 test_subscription*.py 가 임시 설정 파일로 켜고 끄며 따로 검증한다.
"""
import dataclasses

import pytest

_SHELL_ENV = (
    "MAIL_TO",
    "SUBSCRIPTION_API_URL",
    "SUBSCRIPTION_API_TOKEN",
    "SUBSCRIBER_HASH_KEY",
    "SUBSCRIPTION_PAGE_URL",
    "SUBSCRIPTION_REQUIRED",
    "SUBSCRIPTION_DB_INSTANCE",
)


@pytest.fixture(autouse=True)
def _no_subscription_env_from_shell(monkeypatch):
    for name in _SHELL_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def legacy_delivery(monkeypatch):
    """main.run 을 구독 기능이 꺼진 기존 전체 발송 경로로 실행한다(설정 파일의 다른 값은 그대로)."""
    from src import main as main_mod

    load = main_mod.load_config

    def _load_without_subscription(path):
        cfg = load(path)
        cfg.subscription = dataclasses.replace(cfg.subscription, enabled=False)
        cfg.email.preferences_url = ""   # 꺼져 있으면 설정 링크도 싣지 않는다(load_config 와 같은 규칙)
        return cfg

    monkeypatch.setattr(main_mod, "load_config", _load_without_subscription)
