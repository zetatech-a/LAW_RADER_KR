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
    _markers_in,
    _sanitize_har,
    _scrub_headers,
    _scrub_html,
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
