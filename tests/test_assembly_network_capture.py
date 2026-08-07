"""Playwright 네트워크 진단 도구의 정화(sanitize)·정적분석 테스트.

브라우저를 띄우지 않는다 — 순수 함수만 검증한다. 캡처 자체는 네트워크가 필요하므로
GitHub Actions 의 'Verify sources (live)' 워크플로에서 수행한다.

핵심 요구: **값은 지우고 이름은 남긴다.** 헤더 이름·쿼리 키·폼 키야말로 우리가
확정하려는 endpoint 계약이고, 값(쿠키·토큰·세션)은 아티팩트에 남으면 안 된다.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.capture_assembly_network import (
    REDACTED,
    _is_secret_name,
    _js_report,
    _manifest_url,
    _markers_in,
    _sanitize_har,
    _scrub_headers,
    _scrub_body,
    _scrub_html,
    _scrub_json_text,
    _scrub_post_data,
    _scrub_text,
    _scrub_url,
)


# --- 이름 판정 ---


@pytest.mark.parametrize(
    "name",
    ["Cookie", "set-cookie", "Authorization", "X-CSRF-TOKEN", "JSESSIONID",
     "_csrf", "sessionKey", "wmonid", "authToken", "SID", "password"],
)
def test_secret_names_detected(name):
    assert _is_secret_name(name)


@pytest.mark.parametrize(
    "name",
    ["billId", "bill_id", "ageFrom", "tabNm", "Content-Type", "Referer",
     "User-Agent", "Accept", "billNo"],
)
def test_public_names_kept(name):
    assert not _is_secret_name(name)


# --- URL ---


def test_scrub_url_keeps_query_key_names():
    """회귀: 조립 후 다시 정화하면 'jsessionid=REDACTED' 가 통째로 지워져 키가 사라졌다."""
    url = (
        "https://likms.assembly.go.kr/bill/x.do"
        "?billId=PRC_A1&jsessionid=ABCD1234EFGH&_csrf=deadbeefdeadbeefdeadbeefdeadbeef"
    )
    out = _scrub_url(url)
    # 값은 사라지고
    assert "ABCD1234EFGH" not in out
    assert "deadbeef" not in out
    # 키 이름은 남는다(계약의 일부)
    assert "jsessionid=" in out
    assert "_csrf=" in out
    # 공개 식별자는 값까지 남는다
    assert "billId=PRC_A1" in out


def test_scrub_url_keeps_public_bill_id():
    url = "https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId=PRC_A1&ageFrom=22"
    assert _scrub_url(url) == url


def test_scrub_url_without_query():
    url = "https://likms.assembly.go.kr/bill/static/js/bi/bill/billDetail.js"
    assert _scrub_url(url) == url


# --- 헤더 ---


def test_scrub_headers_keeps_all_names():
    headers = {
        "Cookie": "JSESSIONID=ABCD1234",
        "X-CSRF-TOKEN": "tok-secret",
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId=PRC_A1",
        "X-Requested-With": "XMLHttpRequest",
    }
    out = _scrub_headers(headers)
    assert set(out) == set(headers)                 # 이름은 전부 유지
    assert out["Cookie"] == REDACTED
    assert out["X-CSRF-TOKEN"] == REDACTED
    assert out["Content-Type"] == "application/x-www-form-urlencoded"
    assert out["X-Requested-With"] == "XMLHttpRequest"   # XHR 판별에 필요


# --- 요청 본문 ---


def test_scrub_post_data_form_keeps_keys():
    out = _scrub_post_data("billId=PRC_A1&_csrf=abcdef&tabNm=billInfo")
    assert out["kind"] == "form"
    assert out["keys"] == ["_csrf", "billId", "tabNm"]
    assert out["masked"]["billId"] == "PRC_A1"
    assert out["masked"]["tabNm"] == "billInfo"
    assert out["masked"]["_csrf"] == REDACTED


def test_scrub_post_data_json_is_not_parsed_as_form():
    """회귀: parse_qsl 이 '=' 없는 문자열도 받아 JSON 본문 전체가 '키'가 됐다."""
    out = _scrub_post_data('{"billId":"PRC_A1","sessionKey":"zzz"}')
    assert out["kind"] == "json"
    assert out["keys"] == ["billId", "sessionKey"]
    assert out["masked"]["billId"] == "PRC_A1"
    assert out["masked"]["sessionKey"] == REDACTED


def test_scrub_post_data_json_with_equals_inside():
    out = _scrub_post_data('{"q":"a=b","billId":"PRC_A1"}')
    assert out["kind"] == "json"
    assert out["keys"] == ["billId", "q"]


def test_scrub_post_data_raw_and_empty():
    assert _scrub_post_data(None) == {"present": False}
    assert _scrub_post_data("")["present"] is False
    out = _scrub_post_data("아무 구조 없는 본문")
    assert out["kind"] == "raw" and "length" in out
    assert "masked" not in out          # 구조를 모르면 아무것도 노출하지 않는다


# --- 본문 텍스트 ---


def test_scrub_text_removes_personal_and_session_values():
    text = (
        "담당자 010-1234-5678 hong@example.com 900101-1234567 "
        "JSESSIONID=ABCD1234 WMONID=xyz9 "
        "deadbeefdeadbeefdeadbeefdeadbeef"
    )
    out = _scrub_text(text)
    for secret in ("010-1234-5678", "hong@example.com", "900101-1234567",
                   "ABCD1234", "xyz9", "deadbeef"):
        assert secret not in out, secret
    assert "담당자" in out


# --- HTML 본문: 이름이 비밀인 필드의 값까지 지운다 ---

# 실제 형태의 CSRF 토큰(UUID). 하이픈이 섞여 있어 값 패턴(\b[0-9a-f]{32,}\b)에
# 걸리지 않는다 — 이름 기준 마스킹이 없으면 아티팩트에 그대로 실린다.
_UUID_TOKEN = "3f2a91c4-8b7d-4e16-9a02-5c6d1e8f0b34"
_HTML = (
    "<html><head>"
    f'<meta name="_csrf" content="{_UUID_TOKEN}"/>'
    '<meta name="_csrf_header" content="X-CSRF-TOKEN"/>'
    '<meta name="_csrf_parameter" content="_csrf"/>'
    "</head><body>"
    '<form id="form" action="">'
    '<input type="hidden" name="billId" value="PRC_A1"/>'
    f'<input type="hidden" name="_csrf" value="{_UUID_TOKEN}"/>'
    '<input type="hidden" name="ageFrom" value="22"/>'
    "</form>"
    f'<script>var token = "{_UUID_TOKEN}";</script>'
    '<pre id="prntSummary">제안이유 및 주요내용 본문</pre>'
    "</body></html>"
)


def test_scrub_html_redacts_secret_named_field_values():
    out = _scrub_html(_HTML)
    assert _UUID_TOKEN not in out                 # meta·input·인라인 JS 어디에도 없다
    assert out.count(REDACTED) >= 2


def test_scrub_html_keeps_field_names_and_public_values():
    out = _scrub_html(_HTML)
    # 이름은 우리가 확정하려는 계약이다 — 반드시 남는다.
    for keep in ('name="_csrf"', 'name="billId"', 'id="form"', 'id="prntSummary"'):
        assert keep in out, keep
    # 공개 식별자와 본문은 그대로 둔다(fixture 재현·계약 검증에 필요).
    assert 'value="PRC_A1"' in out
    assert 'value="22"' in out
    assert "제안이유 및 주요내용 본문" in out


def test_scrub_html_keeps_csrf_header_and_parameter_names():
    # _csrf_header / _csrf_parameter 의 content 는 토큰이 아니라 '이름'이다.
    out = _scrub_html(_HTML)
    assert "X-CSRF-TOKEN" in out
    assert 'content="_csrf"' in out


def test_scrub_html_still_applies_value_patterns():
    out = _scrub_html(
        '<html><body><p>hong@example.com JSESSIONID=ABCD1234</p></body></html>'
    )
    assert "hong@example.com" not in out
    assert "ABCD1234" not in out


def test_scrub_html_handles_empty_and_non_html():
    assert _scrub_html("") == ""
    assert "ABCD1234" not in _scrub_html("JSESSIONID=ABCD1234")


def test_sanitize_har_redacts_tokens_in_html_response_bodies():
    """HAR 에 박제된 HTML 응답 본문에도 이름 기준 마스킹이 적용돼야 한다."""
    har = _har()
    entry = har["log"]["entries"][0]
    entry["response"]["content"] = {"mimeType": "text/html", "text": _HTML}
    out = json.dumps(_sanitize_har(har), ensure_ascii=False)
    assert _UUID_TOKEN not in out
    assert 'name=\\"_csrf\\"' in out          # 이름은 남는다


# --- HAR ---


def _har():
    return {
        "log": {
            "entries": [
                {
                    "request": {
                        "url": "https://likms.assembly.go.kr/a.do?billId=PRC_A1&token=xyz9",
                        "cookies": [{"name": "JSESSIONID", "value": "ABCD1234"}],
                        "headers": [
                            {"name": "Cookie", "value": "JSESSIONID=ABCD1234"},
                            {"name": "Content-Type", "value": "application/json"},
                        ],
                        "queryString": [
                            {"name": "billId", "value": "PRC_A1"},
                            {"name": "token", "value": "xyz9"},
                        ],
                        "postData": {
                            "text": "billId=PRC_A1&_csrf=aaabbb",
                            "params": [{"name": "_csrf", "value": "aaabbb"}],
                        },
                    },
                    "response": {
                        "cookies": [{"name": "JSESSIONID", "value": "ABCD1234"}],
                        "headers": [
                            {"name": "Set-Cookie", "value": "JSESSIONID=ABCD1234; Path=/"}
                        ],
                        "content": {"text": "본문 hong@example.com JSESSIONID=ABCD1234"},
                    },
                }
            ]
        }
    }


def test_sanitize_har_removes_values_keeps_names():
    blob = json.dumps(_sanitize_har(_har()), ensure_ascii=False)
    for secret in ("ABCD1234", "xyz9", "aaabbb", "hong@example.com"):
        assert secret not in blob, secret
    for name in ("billId", "PRC_A1", "Cookie", "Set-Cookie", "Content-Type", "_csrf", "token"):
        assert name in blob, name


def test_sanitize_har_drops_cookie_arrays_entirely():
    out = _sanitize_har(_har())
    entry = out["log"]["entries"][0]
    assert entry["request"]["cookies"] == []
    assert entry["response"]["cookies"] == []


def test_sanitize_har_handles_missing_sections():
    # 실제 HAR 에는 postData 나 content 가 없는 항목이 흔하다 — 죽지 않아야 한다.
    minimal = {"log": {"entries": [{"request": {"url": "https://likms.assembly.go.kr/a"},
                                    "response": {}}]}}
    out = _sanitize_har(minimal)
    assert out["log"]["entries"][0]["request"]["cookies"] == []
    assert _sanitize_har({}) == {}


# --- 표식 탐지 ---


def test_markers_in():
    assert _markers_in('<pre id="prntSummary">x</pre>') == ["prntSummary"]
    assert _markers_in("제안이유 및 주요내용") == ["제안이유", "주요내용"]
    assert _markers_in("관련 없는 본문") == []


# --- JS 정적 분석 ---


def test_js_report_extracts_endpoints_and_context():
    js = "\n".join(
        [
            'function loadBillInfo(){',
            '  $.ajax({url:"/bill/bi/billInfoDetail.do", data:{billId:billId}});',
            '}',
            'app.Tab.tabClick = function(el){',
            '  if(el.dataset.tabnm==="billInfo") loadBillInfo();',
            '};',
            'var s = document.querySelector("#prntSummary");',
            'fetch("/bill/bi/summary.do?billId="+id);',
        ]
    )
    report = _js_report(js)
    assert "/bill/bi/billInfoDetail.do" in report
    assert "/bill/bi/summary.do?billId=" in report
    for section in ("[ajax]", "[fetch]", "[tabClick]", "[billInfo]", "[prntSummary]"):
        assert section in report
    # 주변 코드가 함께 실려야 계약을 읽을 수 있다
    assert "loadBillInfo" in report


def test_js_report_survives_empty_input():
    report = _js_report("")
    assert "[.do URL 문자열] 0개" in report


# --- JSON 응답: 이름이 비밀인 키의 값도 지운다 ---
#
# HTML 과 같은 이유다. _SECRET_VALUE_PATTERNS 는 32자 이상 연속 16진수만 잡으므로
# UUID·Base64 형 토큰이 JSON 필드 값으로 오면 그대로 아티팩트에 실린다.

_JSON_BODY = json.dumps(
    {
        "csrfToken": "3f2a91c4-8b7d-4e16-9a02-5c6d1e8f0b34",
        "sessionId": "SVNTRVNTSU9OLUlELVZBTFVF",
        "billId": "PRC_A1",
        "result": {
            "authToken": "bearer-abc.def.ghi",
            "billNo": "2200123",
            "rows": [
                {"billId": "PRC_A2", "csrf": "aaaa-bbbb-cccc", "title": "법률안"},
            ],
        },
        "count": 3,
        "ok": True,
        "empty": None,
    },
    ensure_ascii=False,
)


def test_scrub_json_redacts_secret_named_values():
    out = _scrub_json_text(_JSON_BODY)
    for secret in ("3f2a91c4-8b7d-4e16", "SVNTRVNTSU9OLUlELVZBTFVF",
                   "bearer-abc.def.ghi", "aaaa-bbbb-cccc"):
        assert secret not in out, secret


def test_scrub_json_keeps_key_names_and_public_values():
    out = _scrub_json_text(_JSON_BODY)
    parsed = json.loads(out)
    # 키 이름은 계약의 일부다 — 반드시 남는다.
    for key in ("csrfToken", "sessionId", "billId", "result", "count"):
        assert key in out, key
    assert parsed["billId"] == "PRC_A1"           # 공개 식별자는 그대로
    assert parsed["result"]["billNo"] == "2200123"
    assert parsed["csrfToken"] == REDACTED


def test_scrub_json_recurses_into_nested_objects_and_arrays():
    parsed = json.loads(_scrub_json_text(_JSON_BODY))
    row = parsed["result"]["rows"][0]
    assert row["csrf"] == REDACTED
    assert row["billId"] == "PRC_A2"              # 중첩 안에서도 공개 값은 남는다
    assert row["title"] == "법률안"


def test_scrub_json_preserves_non_string_scalars():
    parsed = json.loads(_scrub_json_text(_JSON_BODY))
    assert parsed["count"] == 3
    assert parsed["ok"] is True
    assert parsed["empty"] is None


def test_scrub_json_falls_back_to_value_patterns_on_broken_json():
    out = _scrub_json_text('{"a": 1, ')     # 깨진 JSON
    assert "쓰레기" not in out
    broken = _scrub_json_text('not json at all JSESSIONID=ABCD1234')
    assert "ABCD1234" not in broken


def test_scrub_body_picks_the_right_sanitizer():
    assert json.loads(_scrub_body(_JSON_BODY, is_json=True))["csrfToken"] == REDACTED
    html = '<html><body><meta name="_csrf" content="abc-def-ghi"/></body></html>'
    assert "abc-def-ghi" not in _scrub_body(html, is_json=False)


def test_sanitize_har_redacts_tokens_in_json_response_bodies():
    """HAR 에 박제된 JSON 응답 본문에도 이름 기준 마스킹이 적용돼야 한다."""
    har = _har()
    har["log"]["entries"][0]["response"]["content"] = {
        "mimeType": "application/json", "text": _JSON_BODY,
    }
    out = json.dumps(_sanitize_har(har), ensure_ascii=False)
    assert "3f2a91c4-8b7d-4e16" not in out
    assert "SVNTRVNTSU9OLUlELVZBTFVF" not in out
    assert "csrfToken" in out                      # 키 이름은 남는다


def test_sanitize_har_detects_json_without_mimetype():
    """mimeType 이 빠지거나 틀려도 본문 모양으로 JSON 을 알아본다."""
    har = _har()
    har["log"]["entries"][0]["response"]["content"] = {"text": _JSON_BODY}
    out = json.dumps(_sanitize_har(har), ensure_ascii=False)
    assert "3f2a91c4-8b7d-4e16" not in out


# --- 원본 HAR 은 업로드 경로 밖에 두고 반드시 지운다 ---
#
# record_har_content="embed" 라 원본에는 쿠키·Authorization·CSRF 가 응답 본문째 들어
# 있다. 워크플로는 tests/fixtures/ 를 통째로 업로드하고, 캡처 단계는 continue-on-error
# 라 실패해도 업로드까지 간다.


@pytest.fixture
def fake_playwright(monkeypatch):
    """capture() 안의 playwright import 만 통과시키는 최소 스텁."""
    import types

    mod = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.TimeoutError = type("TimeoutError", (Exception,), {})
    sync_api.sync_playwright = lambda: None
    mod.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", mod)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    return sync_api


def _har_files(root):
    return sorted(p.name for p in root.rglob("*.har"))


def test_raw_har_is_removed_when_capture_aborts(tmp_path, monkeypatch, fake_playwright):
    """캡처 도중 예외가 나도 원본 HAR 이 출력 디렉터리에 남으면 안 된다."""
    import scripts.capture_assembly_network as cap

    seen = {}

    def _boom(*a, **k):
        # 실제 브라우저가 하듯 HAR 을 흘려 두고 죽는다.
        har_raw = a[6] if len(a) > 6 else k["har_raw"]
        seen["har_raw"] = har_raw
        har_raw.parent.mkdir(parents=True, exist_ok=True)
        har_raw.write_text('{"log": {"entries": []}}', encoding="utf-8")
        raise RuntimeError("page.content() 실패")

    monkeypatch.setattr(cap, "_capture_with_browser", _boom)
    with pytest.raises(RuntimeError):
        cap.capture("https://likms.assembly.go.kr/x", tmp_path, 1000)

    assert not seen["har_raw"].exists()            # 지워졌다
    assert not seen["har_raw"].parent.exists()     # 임시 디렉터리째 사라졌다
    assert _har_files(tmp_path) == []              # 업로드 경로에는 아무것도 없다


def test_raw_har_is_never_written_inside_the_output_dir(
    tmp_path, monkeypatch, fake_playwright
):
    """설령 지우기에 실패해도 안전하도록, 원본은 애초에 out_dir 밖에 만든다."""
    import scripts.capture_assembly_network as cap

    seen = {}

    def _record(*a, **k):
        seen["har_raw"] = a[6] if len(a) > 6 else k["har_raw"]
        return []

    monkeypatch.setattr(cap, "_capture_with_browser", _record)
    cap.capture("https://likms.assembly.go.kr/x", tmp_path, 1000)

    har_raw = seen["har_raw"]
    assert tmp_path.resolve() not in har_raw.resolve().parents
    assert _har_files(tmp_path) == []


def test_raw_har_is_removed_on_success(tmp_path, monkeypatch, fake_playwright):
    import scripts.capture_assembly_network as cap

    seen = {}

    def _ok(*a, **k):
        har_raw = a[6] if len(a) > 6 else k["har_raw"]
        seen["har_raw"] = har_raw
        har_raw.parent.mkdir(parents=True, exist_ok=True)
        har_raw.write_text('{"log": {"entries": []}}', encoding="utf-8")
        return []

    monkeypatch.setattr(cap, "_capture_with_browser", _ok)
    cap.capture("https://likms.assembly.go.kr/x", tmp_path, 1000)
    assert not seen["har_raw"].parent.exists()


# --- 요청 본문: 중첩된 비밀도 지운다 ---


def test_scrub_post_data_redacts_nested_secrets():
    """회귀: 비밀이 아닌 최상위 키 아래의 토큰이 통째로 복사됐다.

    manifest 와 정화 HAR 이 둘 다 _scrub_post_data 를 쓰므로, 중첩 UUID·Base64
    자격증명이 업로드되는 tests/fixtures/ 아티팩트까지 그대로 흘러갔다.
    """
    raw = json.dumps(
        {
            "billId": "PRC_A1",
            "payload": {
                "csrfToken": "3f2a91c4-8b7d-4e16-9a02-5c6d1e8f0b34",
                "tabNm": "billInfo",
                "nested": [{"sessionId": "SVNTRVNTSU9O", "billNo": "2200123"}],
            },
        },
        ensure_ascii=False,
    )
    out = _scrub_post_data(raw)
    blob = json.dumps(out, ensure_ascii=False)

    assert "3f2a91c4-8b7d-4e16" not in blob
    assert "SVNTRVNTSU9O" not in blob
    # 구조·이름·공개 값은 그대로 — 계약 확인에 필요하다
    assert out["keys"] == ["billId", "payload"]
    assert out["masked"]["billId"] == "PRC_A1"
    assert out["masked"]["payload"]["csrfToken"] == REDACTED
    assert out["masked"]["payload"]["tabNm"] == "billInfo"
    assert out["masked"]["payload"]["nested"][0]["billNo"] == "2200123"


def test_scrub_post_data_top_level_secret_still_masked():
    out = _scrub_post_data('{"billId":"PRC_A1","sessionKey":"zzz"}')
    assert out["masked"]["sessionKey"] == REDACTED
    assert out["masked"]["billId"] == "PRC_A1"


def test_sanitize_har_redacts_nested_secrets_in_post_data():
    """HAR 의 postData 도 같은 정화를 거친다."""
    har = _har()
    har["log"]["entries"][0]["request"]["postData"] = {
        "text": json.dumps({"payload": {"authToken": "bearer-aaa-bbb-ccc"}}),
        "params": [],
    }
    out = json.dumps(_sanitize_har(har), ensure_ascii=False)
    assert "bearer-aaa-bbb-ccc" not in out
    assert "authToken" in out


# --- manifest 에 남는 URL 은 전부 정화된다 ---


_SECRET_URL = (
    "https://likms.assembly.go.kr/bill/bi/billDetailPage.do"
    "?billId=PRC_A1&jsessionid=ABCD1234EFGH&authToken=aaaa-bbbb-cccc"
)


def test_manifest_url_redacts_values_and_keeps_names():
    out = _manifest_url(_SECRET_URL)
    assert "ABCD1234EFGH" not in out
    assert "aaaa-bbbb-cccc" not in out
    assert "jsessionid=" in out and "authToken=" in out   # 키 이름은 남는다
    assert "billId=PRC_A1" in out                          # 공개 식별자는 값까지


def test_capture_manifest_does_not_store_the_raw_requested_url(
    tmp_path, monkeypatch, fake_playwright
):
    """회귀: 입력 URL(워크플로 입력)의 비밀 쿼리가 manifest 에 원본으로 실렸다."""
    import scripts.capture_assembly_network as cap

    monkeypatch.setattr(cap, "_capture_with_browser", lambda *a, **k: [])
    cap.capture(_SECRET_URL, tmp_path, 1000)

    manifest = (tmp_path / "assembly_xhr_manifest.json").read_text(encoding="utf-8")
    assert "ABCD1234EFGH" not in manifest
    assert "aaaa-bbbb-cccc" not in manifest
    assert "PRC_A1" in manifest            # 진단에 필요한 식별자는 남는다


def test_final_url_goes_through_the_same_rule():
    """redirect 로 세션값이 붙은 주소에 끌려가도 manifest 에는 정화본만 남는다."""
    redirected = (
        "https://likms.assembly.go.kr/bill/bi/billDetailPage.do"
        ";jsessionid=ZZZZ9999?billId=PRC_A1&sessionKey=leaked-token-value"
    )
    out = _manifest_url(redirected)
    assert "leaked-token-value" not in out
    assert "ZZZZ9999" not in out


def test_scrub_url_redacts_path_parameter_session_id():
    """경로 파라미터(;jsessionid=...)의 세션 ID 도 지운다.

    쿠키가 막힌 클라이언트에 대고 서블릿 컨테이너가 세션 ID 를 URL 경로에 붙인다
    (likms 가 그 형태다). urlparse 는 그것을 path 가 아니라 params 로 떼어 놓기 때문에
    path 만 정화하면 살아남는다.
    """
    url = (
        "https://likms.assembly.go.kr/bill/bi/billDetailPage.do"
        ";jsessionid=ZZZZ9999AAAA?billId=PRC_A1"
    )
    out = _scrub_url(url)
    assert "ZZZZ9999AAAA" not in out
    assert "jsessionid=" in out           # 이름은 남는다
    assert "billId=PRC_A1" in out


def test_scrub_url_redacts_path_parameter_without_query():
    url = (
        "https://likms.assembly.go.kr/bill/bi/billDetailPage.do"
        ";jsessionid=ZZZZ9999AAAA"
    )
    out = _scrub_url(url)
    assert "ZZZZ9999AAAA" not in out
    assert "jsessionid=" in out


def test_scrub_url_keeps_public_path_parameters():
    url = "https://likms.assembly.go.kr/bill/x.do;billId=PRC_A1?ageFrom=22"
    out = _scrub_url(url)
    assert "billId=PRC_A1" in out
    assert "ageFrom=22" in out
