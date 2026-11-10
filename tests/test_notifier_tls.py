"""SMTP STARTTLS 가 서버 인증서·호스트명을 검증하는 컨텍스트로 맺어지는지.

smtplib.SMTP.starttls() 를 context 없이 부르면 CPython 은 검증하지 않는
컨텍스트(CERT_NONE, check_hostname=False)를 쓴다. 그러면 경로 중간의 공격자가
SMTP 서버를 사칭해 앱 비밀번호와 메일 본문을 가로챌 수 있다. 로그인 확인
(verify_smtp_login)과 실제 발송(send_digest) 두 경로 모두 검증 컨텍스트를
넘기는지, 그리고 그 컨텍스트로 TLS 를 맺은 '뒤에만' 로그인하는지 확인한다.

네트워크에 접속하지 않도록 smtplib.SMTP 를 가짜로 바꿔 끼운다.
"""
import os
import ssl
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import notifier
from src.config import EmailConfig
from src.models import Post


def _cfg() -> EmailConfig:
    return EmailConfig(
        recipients=["to@example.com"],
        from_name="LAW RADER",
        subject_prefix="[LAW RADER]",
        max_attach_mb=15,
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="sender@example.com",
        smtp_password="pw",
        mail_from="sender@example.com",
    )


def _post() -> Post:
    return Post(
        source_key="fss_press",
        source_name="금감원 · 보도자료",
        post_id="1",
        title="테스트 보도자료",
        url="https://www.fss.or.kr/fss/bbs/B0000188/view.do?nttId=1",
        date="2026-11-10",
        body="본문",
        summary=["요약 한 줄"],
    )


class _FakeSMTP:
    """smtplib.SMTP 대역. 생성 인자와 호출 순서, starttls 의 context 를 기록한다."""

    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.calls: list[str] = []
        self.tls_context = None
        self.sent_to = None
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, *args, **kwargs):
        self.calls.append("starttls")
        self.tls_context = kwargs.get("context")

    def login(self, user, password):
        self.calls.append("login")

    def send_message(self, msg, to_addrs=None):
        self.calls.append("send_message")
        self.sent_to = to_addrs
        return {}


@pytest.fixture
def fake_smtp(monkeypatch):
    _FakeSMTP.instances = []
    monkeypatch.setattr(notifier.smtplib, "SMTP", _FakeSMTP)
    return _FakeSMTP


def _assert_verified_tls(server: _FakeSMTP) -> None:
    ctx = server.tls_context
    assert isinstance(ctx, ssl.SSLContext), "starttls 에 context 가 전달되지 않았다"
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    # 비밀번호는 검증된 TLS 위에서만 보내야 한다.
    assert server.calls.index("starttls") < server.calls.index("login")


def test_verify_smtp_login_uses_verified_tls_context(fake_smtp):
    notifier.verify_smtp_login(_cfg())

    assert len(fake_smtp.instances) == 1
    server = fake_smtp.instances[0]
    assert (server.host, server.port) == ("smtp.example.com", 587)
    assert server.calls == ["starttls", "login"]
    _assert_verified_tls(server)


def test_send_digest_uses_verified_tls_context(fake_smtp):
    cfg = _cfg()
    p = _post()
    notifier.send_digest(cfg, {p.source_name: [p]})

    assert len(fake_smtp.instances) == 1
    server = fake_smtp.instances[0]
    assert (server.host, server.port) == ("smtp.example.com", 587)
    assert server.calls == ["starttls", "login", "send_message"]
    assert server.sent_to == cfg.recipients
    _assert_verified_tls(server)
