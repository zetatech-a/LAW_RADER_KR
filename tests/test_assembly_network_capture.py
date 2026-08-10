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
    _has_summary,
    _markers_in,
    _sanitize_har,
    _scrub_headers,
    _scrub_body,
    _scrub_free_text,
    _scrub_html,
    _scrub_json_text,
    _scrub_post_data,
    _scrub_text,
    _scrub_url,
    _summary_text,
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


# --- URL 을 값으로 갖는 헤더(Referer·Location)도 URL 규칙으로 정화한다 ---
#
# Referer 는 보통 **그 페이지의 전체 URL** 을 그대로 담는다. 그 쿼리에 UUID·Base64
# 자격증명이 있으면 _scrub_text 는 쿼리 파라미터 '이름'을 모르므로 값이 살아남고,
# 헤더 이름('referer') 자체는 비밀이 아니라 REDACTED 대상도 아니다.

_SECRET_REFERER = (
    "https://likms.assembly.go.kr/bill/bi/billDetailPage.do"
    "?billId=PRC_A1&authToken=3f2a91c4-8b7d-4e16-9a02-5c6d1e8f0b34"
)


def test_scrub_headers_redacts_credentials_inside_referer():
    out = _scrub_headers({"Referer": _SECRET_REFERER})
    assert "3f2a91c4-8b7d-4e16" not in out["Referer"]
    assert "authToken=" in out["Referer"]        # 이름은 남는다
    assert "billId=PRC_A1" in out["Referer"]     # 공개 식별자는 값까지


@pytest.mark.parametrize("name", ["Referer", "referer", "Location", "Content-Location"])
def test_url_valued_headers_all_go_through_url_rules(name):
    out = _scrub_headers({name: _SECRET_REFERER})
    assert "3f2a91c4-8b7d-4e16" not in out[name], name


def test_scrub_headers_redacts_session_id_in_referer_path():
    out = _scrub_headers(
        {"Referer": "https://likms.assembly.go.kr/bill/x.do;jsessionid=ABC999?billId=PRC_A1"}
    )
    assert "ABC999" not in out["Referer"]
    assert "billId=PRC_A1" in out["Referer"]


def test_ordinary_referer_is_left_intact():
    """정화가 진단을 망치면 안 된다 — 평범한 Referer 는 그대로다."""
    url = "https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId=PRC_A1"
    assert _scrub_headers({"Referer": url})["Referer"] == url


def test_non_url_headers_are_unaffected():
    out = _scrub_headers(
        {"X-Requested-With": "XMLHttpRequest", "Content-Type": "application/json"}
    )
    assert out["X-Requested-With"] == "XMLHttpRequest"
    assert out["Content-Type"] == "application/json"


def test_secret_named_headers_still_win_over_url_handling():
    out = _scrub_headers({"X-CSRF-TOKEN": _SECRET_REFERER, "Cookie": "JSESSIONID=A1"})
    assert out["X-CSRF-TOKEN"] == REDACTED
    assert out["Cookie"] == REDACTED


def test_sanitize_har_redacts_credentials_inside_referer_and_location():
    """HAR 의 요청·응답 헤더도 같은 규칙을 거친다."""
    har = _har()
    entry = har["log"]["entries"][0]
    entry["request"]["headers"].append({"name": "Referer", "value": _SECRET_REFERER})
    entry["response"]["headers"].append({"name": "Location", "value": _SECRET_REFERER})
    out = json.dumps(_sanitize_har(har), ensure_ascii=False)

    assert "3f2a91c4-8b7d-4e16" not in out
    assert "Referer" in out and "Location" in out       # 이름은 남는다
    assert "billId=PRC_A1" in out


# --- HTML 의 URL 속성(action·href·src)도 URL 규칙으로 정화한다 ---
#
# 필드 값·헤더와 같은 이유다. 속성 이름은 비밀이 아니라 마스킹 대상이 아니고,
# _scrub_text 는 쿼리 파라미터 '이름'을 모른다. 외부 script@src 는 위에서 일부러
# 남기므로 특히 위험하다.

_ATTR_HTML = (
    "<html><body>"
    f'<form id="form" action="/bill/bi/detail.do?billId=PRC_A1&csrfToken={_UUID_TOKEN}">'
    '<input type="hidden" name="billId" value="PRC_A1"/></form>'
    f'<a href="https://likms.assembly.go.kr/x.do?sessionKey={_UUID_TOKEN}">링크</a>'
    f'<script src="/static/billDetail.js?authToken={_UUID_TOKEN}"></script>'
    '<a class="sns" href="#">공유</a>'
    '<a href="/bill/plain.do">보통 링크</a>'
    "</body></html>"
)


def test_scrub_html_redacts_secrets_in_url_attributes():
    out = _scrub_html(_ATTR_HTML)
    assert _UUID_TOKEN not in out


def test_scrub_html_keeps_url_attribute_names_and_public_params():
    out = _scrub_html(_ATTR_HTML)
    assert "csrfToken=" in out and "sessionKey=" in out and "authToken=" in out
    assert "billId=PRC_A1" in out
    assert "/static/billDetail.js" in out       # 어떤 스크립트였는지는 남아야 한다


def test_external_script_src_is_kept_but_sanitized():
    """외부 script@src 는 계약 분석에 필요해 남기므로, 반드시 정화되어야 한다."""
    out = _scrub_html(_ATTR_HTML)
    assert "<script" in out and "billDetail.js" in out
    assert _UUID_TOKEN not in out


def test_scrub_html_does_not_mangle_urls_without_parameters():
    """회귀: 파라미터가 없는 값까지 재조립하면 href="#" 이 href="" 가 된다.

    빈 fragment 는 urlunparse 에서 사라진다. 정화와 무관한 구조가 바뀌면 '값만 지우고
    구조는 남긴다'는 원칙에 어긋나고, 재캡처마다 무의미한 diff 도 생긴다.
    """
    out = _scrub_html(_ATTR_HTML)
    assert 'href="#"' in out
    assert 'href="/bill/plain.do"' in out


def test_scrub_html_redacts_session_id_in_path_parameter_attribute():
    html = '<html><body><a href="/bill/x.do;jsessionid=ABC999?billId=PRC_A1">L</a></body></html>'
    out = _scrub_html(html)
    assert "ABC999" not in out
    # 마무리 값 패턴 pass 가 `jsessionid=<값>` 을 통째로 치환하므로 그 키 이름은
    # 남지 않는다(쿠키 문자열용 패턴이라 그게 맞다). 중요한 것은 값이 사라지고
    # 아티팩트의 진단 가치인 공개 식별자가 살아남는 것이다.
    assert "billId=PRC_A1" in out


def test_session_pattern_stops_at_the_query_delimiter():
    """회귀: JSESSIONID 패턴이 '?' 를 삼켜 뒤따르는 billId 까지 지웠다.

    세션 쿠키 값에는 '?' 도 '&' 도 들어가지 않으므로 문자 클래스에서 뺀다.
    """
    out = _scrub_text("/bill/x.do;jsessionid=ABC999?billId=PRC_A1&ageFrom=22")
    assert "ABC999" not in out
    assert "billId=PRC_A1" in out
    assert "ageFrom=22" in out


def test_session_pattern_still_clears_cookie_strings():
    out = _scrub_text("Cookie: JSESSIONID=ABC999; WMONID=xyz9")
    assert "ABC999" not in out and "xyz9" not in out


# --- 예외 메시지·콘솔 로그에 박힌 URL ---
#
# Playwright 타임아웃 예외는 call log 에 `navigating to "<전체 URL>"` 을 담는다.
# requested_url·final_url 을 정화해 둬도 예외 문자열을 타고 그대로 실린다.

_PW_TIMEOUT = (
    "Timeout 30000ms exceeded.\n"
    "=========================== logs ===========================\n"
    'navigating to "https://likms.assembly.go.kr/bill/bi/billDetailPage.do'
    f'?billId=PRC_A1&authToken={_UUID_TOKEN}", waiting until "domcontentloaded"\n'
    "============================================================"
)


def test_scrub_free_text_redacts_urls_inside_exception_text():
    out = _scrub_free_text(f"goto timeout: {_PW_TIMEOUT}")
    assert _UUID_TOKEN not in out
    assert "authToken=" in out                 # 이름은 남는다
    assert "billId=PRC_A1" in out              # 진단에 필요한 식별자도
    assert "Timeout 30000ms exceeded" in out   # 진단 문구는 그대로


def test_scrub_free_text_handles_multiple_urls():
    text = (
        f"a https://likms.assembly.go.kr/1.do?sessionKey={_UUID_TOKEN} b "
        f"https://likms.assembly.go.kr/2.do?csrf={_UUID_TOKEN} c"
    )
    out = _scrub_free_text(text)
    assert _UUID_TOKEN not in out
    assert out.count("REDACTED") >= 2
    assert out.startswith("a ") and out.endswith(" c")


def test_scrub_free_text_still_applies_value_patterns():
    out = _scrub_free_text("cookie was JSESSIONID=ABCD1234 and mail hong@example.com")
    assert "ABCD1234" not in out
    assert "hong@example.com" not in out


def test_scrub_free_text_leaves_plain_messages_intact():
    assert _scrub_free_text("networkidle timeout") == "networkidle timeout"
    assert _scrub_free_text("") == ""


def test_capture_manifest_does_not_leak_urls_through_errors(
    tmp_path, monkeypatch, fake_playwright
):
    """manifest 의 errors 배열을 타고 자격증명이 나가면 안 된다."""
    import scripts.capture_assembly_network as cap

    def _with_error(*a, **k):
        result = a[7] if len(a) > 7 else k["result"]
        result["errors"].append(cap._scrub_free_text(f"goto timeout: {_PW_TIMEOUT}"))
        return []

    monkeypatch.setattr(cap, "_capture_with_browser", _with_error)
    cap.capture("https://likms.assembly.go.kr/x", tmp_path, 1000)

    manifest = (tmp_path / "assembly_xhr_manifest.json").read_text(encoding="utf-8")
    assert _UUID_TOKEN not in manifest
    assert "Timeout 30000ms exceeded" in manifest      # 진단은 남는다


def test_sanitize_har_redacts_credentials_in_redirect_url():
    """HAR 은 redirect 목적지를 Location 헤더와 별도로 response.redirectURL 에도 담는다.

    회귀: 요청 URL 과 헤더만 정화하고 이 필드는 손대지 않아, redirect 대상에 실린
    자격증명이 정화 HAR 을 타고 그대로 아티팩트에 올라갔다.
    """
    har = _har()
    har["log"]["entries"][0]["response"]["redirectURL"] = _SECRET_REFERER
    out = json.dumps(_sanitize_har(har), ensure_ascii=False)

    assert "3f2a91c4-8b7d-4e16" not in out
    assert "redirectURL" in out                 # 필드 자체는 남는다
    assert "billId=PRC_A1" in out               # 진단 식별자도


def test_sanitize_har_redacts_session_id_in_redirect_url():
    har = _har()
    har["log"]["entries"][0]["response"]["redirectURL"] = (
        "https://likms.assembly.go.kr/bill/x.do;jsessionid=ABC999?billId=PRC_A1"
    )
    out = json.dumps(_sanitize_har(har), ensure_ascii=False)
    assert "ABC999" not in out
    assert "billId=PRC_A1" in out


def test_sanitize_har_leaves_empty_redirect_url_alone():
    """redirect 가 없으면 빈 문자열이다 — 없던 필드를 만들지도, 바꾸지도 않는다."""
    har = _har()
    har["log"]["entries"][0]["response"]["redirectURL"] = ""
    out = _sanitize_har(har)
    assert out["log"]["entries"][0]["response"]["redirectURL"] == ""
    # 필드가 아예 없는 HAR 도 그대로 통과해야 한다
    har2 = _har()
    assert "redirectURL" not in _sanitize_har(har2)["log"]["entries"][0]["response"]


# --- 성공 판정: 표식이 아니라 실제 본문을 요구한다 ---
#
# 이 스크립트의 존재 이유가 '그 endpoint 가 본문을 주는가'를 확정하는 것이다.
# 표식(substring)만으로 성공을 선언하면 없는 계약을 있다고 보고하게 된다.

_REAL_BODY = (
    "현행법은 지방자치단체가 여성기업ㆍ장애인기업 등과 계약을 체결할 때 "
    "수의계약 한도를 달리 정할 수 있도록 하고 있음."
)


def test_empty_pre_is_not_a_successful_capture():
    """회귀: 등록 전 응답의 빈 pre#prntSummary 만으로 성공이 선언됐다."""
    html = '<html><body><pre id="prntSummary"></pre></body></html>'
    assert _markers_in(html)                 # 표식은 있다
    assert _summary_text(html) == ""         # 그러나 본문은 없다
    assert _has_summary(html) is False


def test_generic_label_alone_is_not_a_successful_capture():
    """페이지 어딘가의 '주요내용' 라벨만으로 성공이 되면 안 된다."""
    html = "<html><body><h3>주요내용</h3><div>표로 안내합니다</div></body></html>"
    assert _markers_in(html)
    assert _has_summary(html) is False


def test_short_leftover_text_is_not_a_successful_capture():
    html = '<html><body><pre id="prntSummary">준비 중입니다</pre></body></html>'
    assert _has_summary(html) is False


def test_real_body_is_a_successful_capture():
    html = f'<html><body><pre id="prntSummary">{_REAL_BODY}</pre></body></html>'
    assert _has_summary(html) is True
    assert len(_summary_text(html)) >= 20


def test_summary_text_prefers_the_longest_container():
    """컨테이너가 겹칠 때 바깥쪽 빈 껍데기에 속지 않는다."""
    html = (
        "<html><body>"
        f'<div id="summaryContentDiv"><pre id="prntSummary">{_REAL_BODY}</pre></div>'
        "</body></html>"
    )
    assert _REAL_BODY.split(".")[0][:20] in _summary_text(html)


def test_fallback_selectors_are_accepted_for_legacy_pages():
    """구형 페이지의 폴백 컨테이너도 본문이 실려 있으면 성공이다."""
    for sel_id in ("prntSummary", "summaryContentDiv"):
        html = f'<html><body><div id="{sel_id}">{_REAL_BODY}</div></body></html>'
        assert _has_summary(html) is True, sel_id


def test_summary_text_survives_broken_html():
    assert _summary_text("") == ""
    assert _summary_text("본문만 있고 태그가 없음") == ""


def test_markers_stay_available_as_diagnostics():
    """표식 목록 자체는 계약을 좇는 단서라 manifest 에 남는다(판정 근거만 아닐 뿐)."""
    html = '<html><body><pre id="prntSummary"></pre></body></html>'
    assert _markers_in(html) == ["prntSummary"]
