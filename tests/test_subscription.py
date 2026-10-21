"""구독자별 수신 기관 선택 — 단위 테스트(설정·식별자·API 계약·그룹화·필터·notifier).

실제 HTTP·SMTP 는 부르지 않는다. run() 통합 시나리오는 test_subscription_orchestration.py.
"""
import hashlib
import hmac
import json
import logging
import os
import smtplib
import sys

import pytest
import requests
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import subscription as sub_mod
from src.config import (
    AgencyConfig,
    Config,
    EmailConfig,
    SubscriptionConfig,
    load_config,
    validate_agency_mapping,
)
from src.models import ASSEMBLY_SOURCE_KEY, Post
from src.notifier import build_html, build_text, send_digest
from src.subscription import (
    API_VERSION,
    Preference,
    RecipientGroup,
    SubscriptionError,
    fetch_preferences,
    filter_posts,
    group_recipients,
    missing_subscription_settings,
    normalize_email,
    parse_response,
    resolve_recipient_groups,
    send_grouped,
    subscriber_hash,
)

KEY = "k" * 40
ENV_KEYS = (
    "SUBSCRIPTION_API_URL",
    "SUBSCRIPTION_API_TOKEN",
    "SUBSCRIBER_HASH_KEY",
    "SUBSCRIPTION_PAGE_URL",
)
AGENCIES = (
    AgencyConfig("fsc", "금융위원회", ("fsc_press", "fsc_legislation")),
    AgencyConfig(
        "fss",
        "금융감독원",
        ("fss_press", "fss_admin_guidance", "fss_rule_amendment", "fss_sanction",
         "fss_mgmt_notice"),
    ),
    AgencyConfig("better", "금융규제·법령해석포털", ("better_reply",)),
    AgencyConfig("pipc", "개인정보보호위원회", ("pipc_notice", "pipc_press")),
    AgencyConfig("assembly", "의안정보시스템", (ASSEMBLY_SOURCE_KEY,)),
)
AGENCY_ORDER = tuple(a.id for a in AGENCIES)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


def _sub(**over):
    base = dict(
        enabled=True,
        api_url="https://law-rader.example.workers.dev/internal/sync-and-resolve",
        api_token="tok-secret-123",
        subscriber_hash_key=KEY,
        timeout_sec=5.0,
        agencies=AGENCIES,
    )
    base.update(over)
    return SubscriptionConfig(**base)


def _email(recipients=("a@example.com",), **over):
    base = dict(
        recipients=list(recipients),
        from_name="LAW RADER",
        subject_prefix="[LAW RADER]",
        max_attach_mb=15,
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="sender@example.com",
        smtp_password="pw",
        mail_from="sender@example.com",
        preferences_url="https://law-rader.example.workers.dev/",
    )
    base.update(over)
    return EmailConfig(**base)


def _post(key, pid="1", name=None):
    return Post(
        source_key=key, source_name=name or key, post_id=pid,
        title=f"{key} {pid}", url=f"https://e.com/{key}/{pid}", date="2026-10-21",
    )


def _h(addr):
    return subscriber_hash(addr, KEY)


def _write_config(tmp_path, mutate):
    raw = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    mutate(raw)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


# ==========================================================================
# 설정
# ==========================================================================
def test_shipped_config_is_disabled_and_maps_every_enabled_source_once(monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_PAGE_URL", "https://x.workers.dev/")
    cfg = load_config("config.yaml")
    assert cfg.subscription.enabled is False
    # 꺼져 있으면 env 가 있어도 링크를 싣지 않는다(설정이 반영되지 않는 링크 금지).
    assert cfg.email.preferences_url == ""
    assert [a.id for a in cfg.subscription.agencies] == list(AGENCY_ORDER)
    # 켜는 순간 요구되는 완전성 검증을 출고 설정이 이미 통과해야 한다.
    validate_agency_mapping(cfg.subscription.agencies, cfg.sources, require_complete=True)
    owners = {}
    for a in cfg.subscription.agencies:
        for key in a.sources:
            owners.setdefault(key, []).append(a.id)
    for s in cfg.sources:
        if s.enabled:
            assert len(owners.get(s.key, [])) == 1, s.key


def test_shipped_mapping_matches_requested_agencies():
    cfg = load_config("config.yaml")
    got = {a.id: set(a.sources) for a in cfg.subscription.agencies}
    assert got == {a.id: set(a.sources) for a in AGENCIES}


def test_config_without_subscription_block_is_disabled(tmp_path):
    path = _write_config(tmp_path, lambda raw: raw.pop("subscription"))
    cfg = load_config(path)
    assert cfg.subscription == SubscriptionConfig()
    assert cfg.subscription.enabled is False


def test_dataclass_constructors_stay_backward_compatible():
    # 기존 테스트처럼 새 필드 없이 직접 생성해도 된다.
    email = EmailConfig(
        recipients=["a@example.com"], from_name="x", subject_prefix="[x]",
        max_attach_mb=1, smtp_host="h", smtp_port=1, smtp_user="u",
        smtp_password="p", mail_from="m",
    )
    assert email.preferences_url == ""
    cfg = Config(email=email, fetch=None, llm=None, sources=[])
    assert cfg.subscription.enabled is False


def test_enabled_reads_env_and_sets_preferences_url(tmp_path, monkeypatch):
    path = _write_config(tmp_path, lambda raw: raw["subscription"].update(enabled=True))
    monkeypatch.setenv("SUBSCRIPTION_API_URL", "https://w.workers.dev/internal/sync-and-resolve")
    monkeypatch.setenv("SUBSCRIPTION_API_TOKEN", "tok")
    monkeypatch.setenv("SUBSCRIBER_HASH_KEY", KEY)
    monkeypatch.setenv("SUBSCRIPTION_PAGE_URL", "https://w.workers.dev/")
    cfg = load_config(path)
    assert cfg.subscription.enabled is True
    assert cfg.subscription.api_token == "tok"
    assert cfg.email.preferences_url == "https://w.workers.dev/"


def test_secrets_are_not_in_repr():
    text = repr(_sub())
    assert "tok-secret-123" not in text and KEY not in text


def test_enabled_requires_every_enabled_source_mapped(tmp_path):
    def mutate(raw):
        raw["subscription"]["enabled"] = True
        raw["subscription"]["agencies"][3]["sources"] = ["pipc_notice"]  # pipc_press 누락
    with pytest.raises(ValueError, match="pipc_press"):
        load_config(_write_config(tmp_path, mutate))


def test_incomplete_mapping_is_allowed_while_disabled(tmp_path):
    def mutate(raw):
        raw["subscription"]["agencies"][3]["sources"] = ["pipc_notice"]
    assert load_config(_write_config(tmp_path, mutate)).subscription.enabled is False


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda a: a[1]["sources"].append("fsc_press"), "두 기관"),
        (lambda a: a[0]["sources"].append("nope_source"), "없는 source key"),
        (lambda a: a[1].update(id="fsc"), "중복"),
        (lambda a: a[0].update(id="FSC!"), "형식"),
        (lambda a: a[0].update(sources=[]), "sources"),
        (lambda a: a[0].update(name=""), "name"),
    ],
)
def test_malformed_agencies_fail_loudly(tmp_path, mutate, match):
    path = _write_config(tmp_path, lambda raw: mutate(raw["subscription"]["agencies"]))
    with pytest.raises(ValueError, match=match):
        load_config(path)


def test_missing_settings_lists_names_only():
    missing = missing_subscription_settings(
        _sub(api_url="http://insecure", api_token="", subscriber_hash_key="short", agencies=()),
        _email(preferences_url=""),
    )
    assert missing == [
        "SUBSCRIPTION_API_URL(https://)",
        "SUBSCRIPTION_API_TOKEN",
        "SUBSCRIBER_HASH_KEY(32자 이상)",
        "SUBSCRIPTION_PAGE_URL(https://)",
        "config.yaml subscription.agencies",
    ]
    assert missing_subscription_settings(_sub(), _email()) == []


# ==========================================================================
# 식별자
# ==========================================================================
def test_normalize_and_hash_are_case_insensitive_and_match_reference():
    assert normalize_email("  User.Name@Example.COM ") == "user.name@example.com"
    expected = hmac.new(KEY.encode(), b"user.name@example.com", hashlib.sha256).hexdigest()
    assert subscriber_hash("User.Name@Example.COM", KEY) == expected
    assert subscriber_hash("user.name@example.com", "x" * 40) != expected


@pytest.mark.parametrize(
    "bad", ["", "no-at-sign", "a@b@c", "@example.com", "a@", "a b@example.com",
            "홍길동@example.com", "Name <a@example.com>"],
)
def test_invalid_addresses_raise_without_echoing_them(bad):
    with pytest.raises(ValueError) as ei:
        normalize_email(bad)
    if bad:
        assert bad not in str(ei.value)


# ==========================================================================
# API 요청·응답 계약
# ==========================================================================
def _resp(status=200, body=None, raw=None):
    r = requests.Response()
    r.status_code = status
    r._content = raw if raw is not None else json.dumps(body).encode("utf-8")
    return r


def _ok_body(entries):
    return {
        "api_version": API_VERSION,
        "subscribers": [
            {"subscriber": sid, "mode": mode, "agencies": list(ags)}
            for sid, mode, ags in entries
        ],
    }


def test_request_payload_contains_hashes_and_catalog_only(monkeypatch):
    captured = {}

    def fake_post(url, **kw):
        captured.update(url=url, **kw)
        return _resp(body=_ok_body([(_h("b@example.com"), "all", []),
                                    (_h("a@example.com"), "all", [])]))

    monkeypatch.setattr(sub_mod.requests, "post", fake_post)
    ids = [_h("b@example.com"), _h("a@example.com"), _h("a@example.com")]
    fetch_preferences(_sub(), ids)
    payload = captured["json"]
    assert payload["api_version"] == API_VERSION
    assert payload["subscribers"] == sorted(set(ids))       # 정렬·중복 제거
    assert payload["agencies"] == [{"id": a.id, "name": a.name} for a in AGENCIES]
    assert "@" not in json.dumps(payload, ensure_ascii=False)  # 평문 주소 없음
    assert captured["headers"]["Authorization"] == "Bearer tok-secret-123"
    assert captured["timeout"] == 5.0
    assert captured["allow_redirects"] is False


def test_never_sends_an_empty_subscriber_list(monkeypatch):
    monkeypatch.setattr(sub_mod.requests, "post", lambda *a, **k: pytest.fail("호출 금지"))
    with pytest.raises(SubscriptionError):
        resolve_recipient_groups(_sub(), _email(recipients=()))


def test_invalid_mail_to_address_fails_without_leaking(monkeypatch):
    monkeypatch.setattr(sub_mod.requests, "post", lambda *a, **k: pytest.fail("호출 금지"))
    with pytest.raises(SubscriptionError) as ei:
        resolve_recipient_groups(_sub(), _email(recipients=("ok@example.com", "Bad Name")))
    assert "Bad Name" not in str(ei.value) and "ok@example.com" not in str(ei.value)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 502, 503])
def test_http_errors_raise(monkeypatch, status):
    monkeypatch.setattr(sub_mod.requests, "post", lambda *a, **k: _resp(status, {"x": 1}))
    with pytest.raises(SubscriptionError, match=f"HTTP {status}"):
        fetch_preferences(_sub(), [_h("a@example.com")])


def test_redirect_is_not_followed_and_is_an_error(monkeypatch):
    monkeypatch.setattr(sub_mod.requests, "post", lambda *a, **k: _resp(302, {}))
    with pytest.raises(SubscriptionError, match="HTTP 302"):
        fetch_preferences(_sub(), [_h("a@example.com")])


@pytest.mark.parametrize(
    "exc, match",
    [(requests.Timeout("t"), "시간 초과"),
     (requests.ConnectionError("c"), "연결 실패"),
     (requests.exceptions.SSLError("s"), "연결 실패")],
)
def test_transport_errors_raise(monkeypatch, exc, match):
    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(sub_mod.requests, "post", boom)
    with pytest.raises(SubscriptionError, match=match) as ei:
        fetch_preferences(_sub(), [_h("a@example.com")])
    assert ei.value.__suppress_context__ is True


@pytest.mark.parametrize("raw", [b"not json", b"", b"\xff\xfe", b"[1,2"])
def test_invalid_json_raises(monkeypatch, raw):
    monkeypatch.setattr(sub_mod.requests, "post", lambda *a, **k: _resp(200, raw=raw))
    with pytest.raises(SubscriptionError, match="JSON"):
        fetch_preferences(_sub(), [_h("a@example.com")])


def test_deeply_nested_json_is_a_contract_error_not_a_crash(monkeypatch):
    # 1 MB 상한 안의 깊은 중첩은 json 이 RecursionError 를 낸다. SubscriptionError 로 바뀌어야
    # run() 이 정상 실패 경로(로그 + exit 1)를 탄다.
    monkeypatch.setattr(
        sub_mod.requests, "post", lambda *a, **k: _resp(200, raw=b"[" * 200_000)
    )
    with pytest.raises(SubscriptionError, match="JSON"):
        fetch_preferences(_sub(), [_h("a@example.com")])


def test_oversized_response_raises(monkeypatch):
    monkeypatch.setattr(
        sub_mod.requests, "post", lambda *a, **k: _resp(200, raw=b" " * 1_000_001)
    )
    with pytest.raises(SubscriptionError, match="너무 큽니다"):
        fetch_preferences(_sub(), [_h("a@example.com")])


A, B = _h("a@example.com"), _h("b@example.com")


def _parse(entries=None, data=None, requested=(A, B)):
    return parse_response(
        data if data is not None else _ok_body(entries), set(requested), AGENCY_ORDER
    )


def test_valid_response_parses_all_modes():
    prefs = _parse([(A, "all", []), (B, "selected", ["assembly", "fsc"])])
    assert prefs[A] == Preference("all", ())
    # 기관 순서는 카탈로그 순으로 정규화된다.
    assert prefs[B] == Preference("selected", ("fsc", "assembly"))


def test_empty_selection_is_a_valid_pause():
    assert _parse([(A, "all", []), (B, "selected", [])])[B] == Preference("selected", ())


@pytest.mark.parametrize(
    "data, match",
    [
        (_ok_body([(A, "all", [])]), "누락"),                                   # 응답 해시 누락
        (_ok_body([(A, "all", []), (B, "all", []), (A, "all", [])]), "중복된 구독자"),
        (_ok_body([(A, "all", []), (B, "selected", ["bank"])]), "알 수 없는 기관"),
        (_ok_body([(A, "all", []), (B, "selected", ["fsc", "fsc"])]), "agencies 에 중복"),
        (_ok_body([(A, "all", []), (B, "some", [])]), "알 수 없는 mode"),
        (_ok_body([(A, "ALL", []), (B, "all", [])]), "알 수 없는 mode"),
        (_ok_body([(A, "all", ["fsc"]), (B, "all", [])]), "mode=all"),
        (_ok_body([(A, "all", []), (B, "all", []), ("c" * 64, "all", [])]), "요청하지 않은"),
        (_ok_body([(A, "all", []), ("A" * 64, "all", [])]), "식별자 형식"),
        ({**_ok_body([(A, "all", []), (B, "all", [])]), "api_version": 2}, "버전"),
        ({**_ok_body([(A, "all", []), (B, "all", [])]), "api_version": "1"}, "버전"),
        ({**_ok_body([(A, "all", []), (B, "all", [])]), "api_version": True}, "버전"),
        ({"api_version": API_VERSION}, "subscribers"),
        ({"api_version": API_VERSION, "subscribers": {A: {"mode": "all"}}}, "subscribers"),
        ({"api_version": API_VERSION, "subscribers": [A]}, "항목 형식"),
        ({"api_version": API_VERSION,
          "subscribers": [{"subscriber": A, "mode": "all"}]}, "agencies 형식"),
        ([], "객체가 아닙니다"),
    ],
)
def test_contract_violations_are_errors(data, match):
    with pytest.raises(SubscriptionError, match=match):
        _parse(data=data)


def test_error_messages_never_contain_identifiers():
    try:
        _parse(data=_ok_body([(A, "all", []), (B, "selected", ["bank"])]))
    except SubscriptionError as e:
        assert A not in str(e) and B not in str(e) and "bank" not in str(e)


# ==========================================================================
# 그룹화·필터
# ==========================================================================
def _groups(entries):
    """entries: [(addr, mode, agencies)] → group_recipients 결과."""
    pairs = [(addr, _h(addr)) for addr, _, _ in entries]
    prefs = {_h(addr): Preference(mode, tuple(a for a in AGENCY_ORDER if a in ags))
             for addr, mode, ags in entries}
    return group_recipients(pairs, prefs, AGENCIES)


def test_all_mode_is_one_group_with_every_source():
    groups = _groups([("a@x.com", "all", ()), ("b@x.com", "all", ())])
    assert groups == [RecipientGroup(None, (), ("a@x.com", "b@x.com"))]
    assert groups[0].label == "all"


def test_assembly_only_and_fsc_plus_assembly_and_paused():
    groups = _groups([
        ("a@x.com", "selected", ("assembly",)),
        ("b@x.com", "selected", ("fsc", "assembly")),
        ("c@x.com", "selected", ()),
        ("d@x.com", "all", ()),
        ("e@x.com", "selected", ("assembly",)),   # a 와 같은 설정 → 같은 BCC 그룹
    ])
    by_label = {g.label: g for g in groups}
    assert [g.label for g in groups] == [
        "all", "selected:일시중지", "selected:fsc+assembly", "selected:assembly",
    ]
    assert by_label["selected:assembly"].recipients == ("a@x.com", "e@x.com")
    assert by_label["selected:assembly"].sources == frozenset({ASSEMBLY_SOURCE_KEY})
    assert by_label["selected:fsc+assembly"].sources == frozenset(
        {"fsc_press", "fsc_legislation", ASSEMBLY_SOURCE_KEY}
    )
    assert by_label["selected:일시중지"].sources == frozenset()
    assert by_label["all"].recipients == ("d@x.com",)


def test_new_board_in_selected_agency_is_received_automatically():
    """기관을 고른 사용자는 그 기관에 나중에 추가된 게시판도 받는다(저장값은 기관 ID)."""
    agencies = AGENCIES[:1] + (AgencyConfig("fss", "금융감독원", ("fss_press", "fss_new_board")),)
    pairs = [("a@x.com", _h("a@x.com"))]
    groups = group_recipients(pairs, {_h("a@x.com"): Preference("selected", ("fss",))}, agencies)
    assert "fss_new_board" in groups[0].sources


def test_filter_uses_source_key_not_display_name():
    # 표시명 키가 '금융위'라도 실제 글의 source_key 가 의안이면 의안으로 걸러야 한다.
    misleading = {"금융위 · 보도자료": [_post(ASSEMBLY_SOURCE_KEY, "x"), _post("fsc_press", "y")]}
    out = filter_posts(misleading, frozenset({ASSEMBLY_SOURCE_KEY}))
    assert [p.post_id for p in out["금융위 · 보도자료"]] == ["x"]
    assert filter_posts(misleading, frozenset({"pipc_press"})) == {}


def test_filter_all_returns_the_same_object():
    posts = {"x": [_post("fsc_press")]}
    assert filter_posts(posts, None) is posts


# ==========================================================================
# 그룹별 발송
# ==========================================================================
class _Recorder:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def __call__(self, cfg, posts, **kw):
        self.calls.append((list(cfg.recipients), posts, kw))
        if self.fail_on is not None and len(self.calls) == self.fail_on:
            raise RuntimeError("smtp down")


def test_send_grouped_filters_skips_empty_and_keeps_call_shape():
    groups = _groups([
        ("all@x.com", "all", ()),
        ("asm@x.com", "selected", ("assembly",)),
        ("pipc@x.com", "selected", ("pipc",)),    # 이번 실행엔 개인정보위 글 없음 → 생략
        ("off@x.com", "selected", ()),             # 일시중지 → 생략
    ])
    news = {"금융위": [_post("fsc_press")], "의안": [_post(ASSEMBLY_SOURCE_KEY, "n")]}
    send = _Recorder()
    send_grouped(send, _email(), groups, news, {})
    assert [c[0] for c in send.calls] == [["all@x.com"], ["asm@x.com"]]
    assert send.calls[0][1] is news and send.calls[0][2] == {}   # 기존 (cfg, posts) 형태
    assert send.calls[1][1] == {"의안": [news["의안"][0]]}


def test_send_grouped_filters_detail_updates_too():
    groups = _groups([("asm@x.com", "selected", ("assembly",)),
                      ("fsc@x.com", "selected", ("fsc",))])
    updates = {"의안": [_post(ASSEMBLY_SOURCE_KEY, "R1")]}
    send = _Recorder()
    send_grouped(send, _email(), groups, {}, updates)
    # 의안을 고르지 않은 사용자에게는 상세 업데이트도 가지 않는다.
    assert len(send.calls) == 1
    assert send.calls[0][0] == ["asm@x.com"]
    assert send.calls[0][2] == {"detail_updates_by_source": updates}


def test_send_grouped_failure_propagates_and_logs_no_addresses(caplog):
    groups = _groups([("first@x.com", "all", ()), ("second@x.com", "selected", ("fsc",))])
    send = _Recorder(fail_on=2)
    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError):
        send_grouped(send, _email(), groups, {"금융위": [_post("fsc_press")]}, {})
    text = caplog.text
    assert "중복 수신" in text and "그룹 1개" in text
    assert "@x.com" not in text and _h("first@x.com") not in text


# ==========================================================================
# notifier — 링크·개인정보
# ==========================================================================
def _news():
    return {"금융위 · 보도자료": [_post("fsc_press")]}


def test_footer_unchanged_without_url():
    assert build_html(_news()) == build_html(_news(), preferences_url="")
    assert build_text(_news()) == build_text(_news(), preferences_url="")
    assert "수신 기관 설정" not in build_html(_news())
    assert "수신 기관 설정" not in build_text(_news())


def test_footer_link_in_html_and_text():
    url = "https://law-rader.example.workers.dev/?a=1&b=2"
    html_out = build_html(_news(), preferences_url=url)
    assert "href='https://law-rader.example.workers.dev/?a=1&amp;b=2'" in html_out
    assert "수신 기관 설정</a>" in html_out
    # 기존 푸터 문구는 그대로 남는다.
    assert "RADER stands for Regulatory Alert Detection & Email Reporter" in html_out
    assert build_text(_news(), preferences_url=url).endswith(f"수신 기관 설정: {url}")


def _bodies(msg) -> str:
    """text/plain + text/html 파트를 디코딩해 합친다(전송 인코딩은 base64)."""
    return "\n".join(
        part.get_content() for part in msg.walk() if part.get_content_type().startswith("text/")
    )


class _FakeSMTP:
    sent = []
    refused = {}
    error = None

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, *a):
        pass

    def send_message(self, msg, to_addrs):
        if _FakeSMTP.error is not None:
            raise _FakeSMTP.error
        _FakeSMTP.sent.append((msg, list(to_addrs)))
        return dict(_FakeSMTP.refused)


@pytest.fixture
def fake_smtp(monkeypatch):
    _FakeSMTP.sent, _FakeSMTP.refused, _FakeSMTP.error = [], {}, None
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    return _FakeSMTP


def test_send_digest_puts_link_from_cfg_and_logs_count_only(fake_smtp, caplog):
    cfg = _email(recipients=("a@example.com", "b@example.com"))
    with caplog.at_level(logging.INFO):
        send_digest(cfg, _news())
    msg, to = fake_smtp.sent[0]
    assert to == ["a@example.com", "b@example.com"]
    body = _bodies(msg)
    assert "href='https://law-rader.example.workers.dev/'" in body
    assert "수신 기관 설정: https://law-rader.example.workers.dev/" in body
    assert "수신자 2명" in caplog.text
    assert "a@example.com" not in caplog.text and "b@example.com" not in caplog.text


def test_send_digest_without_url_has_no_link(fake_smtp):
    send_digest(_email(preferences_url=""), _news())
    body = _bodies(fake_smtp.sent[0][0])
    assert "수신 기관 설정" not in body and "workers.dev" not in body


def test_partial_refusal_message_has_no_addresses(fake_smtp):
    fake_smtp.refused = {"gone@example.com": (550, b"5.1.1 <gone@example.com> no such user")}
    with pytest.raises(RuntimeError) as ei:
        send_digest(_email(recipients=("ok@example.com", "gone@example.com")), _news())
    assert "gone@example.com" not in str(ei.value)
    assert "1명" in str(ei.value) and "550" in str(ei.value)


@pytest.mark.parametrize(
    "error",
    [
        smtplib.SMTPRecipientsRefused({"a@example.com": (550, b"<a@example.com> rejected")}),
        smtplib.SMTPSenderRefused(553, b"<sender@example.com> denied", "sender@example.com"),
        smtplib.SMTPDataError(550, b"rejected for <a@example.com>"),
    ],
)
def test_smtp_exceptions_are_sanitized(fake_smtp, error):
    fake_smtp.error = error
    with pytest.raises(RuntimeError) as ei:
        send_digest(_email(), _news())
    assert "@example.com" not in str(ei.value)
    assert ei.value.__suppress_context__ is True     # 원래 예외(주소 포함)를 체인에서 끊음
    assert type(error).__name__ in str(ei.value)


# ==========================================================================
# 워크플로 — 구독 설정 주입
# ==========================================================================
def test_monitor_workflow_passes_subscription_settings():
    from pathlib import Path

    doc = yaml.safe_load(
        (Path(__file__).parent.parent / ".github" / "workflows" / "monitor.yml").read_text(
            encoding="utf-8"
        )
    )
    run_step = next(
        s for s in doc["jobs"]["monitor"]["steps"]
        if str(s.get("run", "")).strip() == "python -m src.main"
    )
    env = run_step["env"]
    # 토큰·HMAC 키·내부 API 주소는 Secret, 메일에 실리는 페이지 주소는 Variable.
    assert env["SUBSCRIPTION_API_URL"] == "${{ secrets.SUBSCRIPTION_API_URL }}"
    assert env["SUBSCRIPTION_API_TOKEN"] == "${{ secrets.SUBSCRIPTION_API_TOKEN }}"
    assert env["SUBSCRIBER_HASH_KEY"] == "${{ secrets.SUBSCRIBER_HASH_KEY }}"
    assert env["SUBSCRIPTION_PAGE_URL"] == "${{ vars.SUBSCRIPTION_PAGE_URL }}"
