"""Playwright 캡처 도구의 실제 브라우저 동작 테스트.

로컬 HTTP 서버로 의안 상세페이지의 구조를 흉내낸다(실제 사이트에 접속하지 않는다):
  - 초기 HTML 에는 제안이유가 없고 #tab_billInfo_sect 가 비어 있다
  - [data-tabnm="billInfo"] 탭을 누르면 JS 가 XHR 로 내용을 받아 채운다
  - billDetail.js 를 따로 로드한다
이 구조는 2026-08 라이브 확인에서 드러난 것과 같다.

Playwright 나 Chromium 이 없으면 skip 된다(생산 의존성이 아니다). CI 의 캡처 모드에서는
설치되므로 실제로 돈다.
"""
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scripts.capture_assembly_network as cn


def _browser_path() -> str | None:
    """쓸 수 있는 Chromium 경로. 기본 설치본이 맞으면 빈 문자열, 없으면 None."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    # 1) playwright 가 관리하는 기본 브라우저
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(args=["--no-sandbox"])
            b.close()
        return ""
    except Exception:  # noqa: BLE001 - 버전 불일치 등
        pass
    # 2) 이미지에 사전 설치된 Chromium
    import glob

    for pat in (
        "/opt/pw-browsers/chromium-*/chrome-linux/chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
    ):
        for path in sorted(glob.glob(pat), reverse=True):
            try:
                with sync_playwright() as pw:
                    b = pw.chromium.launch(
                        args=["--no-sandbox"], executable_path=path
                    )
                    b.close()
                return path
            except Exception:  # noqa: BLE001
                continue
    return None


_BROWSER = _browser_path()
_SKIP = pytest.mark.skipif(
    _BROWSER is None,
    reason="Playwright/Chromium 없음 — 진단 전용 의존성이라 생산 환경에는 없다. "
    "CI 캡처 모드(python -m playwright install chromium)에서는 실행된다.",
)

_REASON = (
    "제안이유 및 주요내용<br>현행법은 가상자산사업자의 이용자 예치금 보호 의무를 "
    "명확히 규정하고 있지 아니함."
)
_JS = """
app = { Tab: { tabClick: function(el){
  var id = new URLSearchParams(location.search).get("billId");
  fetch("/bill/bi/billInfoDetail.do?billId="+id,
        {headers:{"X-Requested-With":"XMLHttpRequest"}})
    .then(r=>r.text())
    .then(t=>{ document.querySelector("#tab_billInfo_sect").innerHTML = t; });
}}};
console.log("billDetail.js loaded");
"""
_PAGE = """<html><head><meta charset="utf-8"><title>의안상세</title>
<script src="/bill/static/js/bi/bill/billDetail.js?ver=1.0.0"></script></head><body>
<form id="form" action="/bill/bi/billDetailPage.do" method="get">
  <input type="hidden" name="billId" value="PRC_X"/></form>
<ul><li><a href="#" data-tabnm="billInfo" onclick="app.Tab.tabClick(this)">심사정보</a></li></ul>
<div id="tab_billInfo_sect"></div></body></html>"""


class _Server:
    """의안 상세페이지 구조를 흉내내는 로컬 서버."""

    def __init__(self, serve_summary: bool):
        self.serve_summary = serve_summary
        xhr_body = (
            f'<div class="tbl"><pre id="prntSummary">{_REASON}</pre></div>'
            if serve_summary
            else "<div>표시할 내용이 없습니다</div>"
        )

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.startswith("/bill/static"):
                    body, ctype = _JS.encode(), "application/javascript"
                elif self.path.startswith("/bill/bi/billInfoDetail.do"):
                    body, ctype = xhr_body.encode(), "text/html; charset=utf-8"
                else:
                    body, ctype = _PAGE.encode(), "text/html; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                # 정화 대상: 실제 사이트처럼 세션 쿠키를 내려준다
                self.send_header("Set-Cookie", "JSESSIONID=SECRET123ABC; Path=/")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._srv = HTTPServer(("127.0.0.1", 0), _H)
        self.port = self._srv.server_address[1]

    def __enter__(self):
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._srv.shutdown()
        self._srv.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/bill/bi/billDetailPage.do?billId=PRC_X"


def _run(tmp_path, monkeypatch, serve_summary: bool):
    # 로컬 서버 응답도 '대상 호스트'로 취급하도록 바꾼다.
    monkeypatch.setattr(cn, "TARGET_HOST", "127.0.0.1")
    with _Server(serve_summary) as srv:
        rc = cn.main(
            ["--url", srv.url, "--out", str(tmp_path), "--timeout-ms", "8000"]
            + (["--browser-path", _BROWSER] if _BROWSER else [])
        )
    manifest = json.loads(
        (tmp_path / "assembly_xhr_manifest.json").read_text(encoding="utf-8")
    )
    return rc, manifest


@_SKIP
def test_tab_click_triggers_xhr_and_summary_is_found(tmp_path, monkeypatch):
    rc, man = _run(tmp_path, monkeypatch, serve_summary=True)

    assert rc == cn.EXIT_OK
    assert man["tab_found"] is True
    assert man["tab_clicked"] is True
    assert man["tab_pane_filled"] is True
    assert man["summary_in_xhr"] is True
    assert man["summary_in_rendered_html"] is True

    # 제안이유를 준 응답이 정확히 지목되어야 한다 — 이것이 확정할 endpoint 다.
    assert man["summary_sources"], man
    src = man["summary_sources"][0]
    assert "billInfoDetail.do" in src["url"]
    hit = next(r for r in man["responses"] if r.get("file") == src["file"])
    assert hit["markers"] == ["prntSummary", "제안이유", "주요내용"]
    assert hit["has_prntSummary"] is True

    # XHR 요청의 헤더 이름이 남아 있어야 계약을 읽을 수 있다
    xhr_reqs = [r for r in man["requests"] if "billInfoDetail.do" in r["url"]]
    assert xhr_reqs, man["requests"]
    assert "x-requested-with" in {k.lower() for k in xhr_reqs[0]["headers"]}


@_SKIP
def test_all_diagnostic_artifacts_are_written(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, serve_summary=True)
    for name in (
        "assembly_network.har",
        "assembly_xhr_manifest.json",
        "assembly_rendered.html",
        "assembly_billDetail.js",
        "assembly_js_report.txt",
        "browser_console.txt",
        "xhr_response_001.html",
    ):
        assert (tmp_path / name).exists(), name
    # 콘솔 로그가 수집된다
    assert "billDetail.js loaded" in (tmp_path / "browser_console.txt").read_text(
        encoding="utf-8"
    )
    # JS 보고서가 endpoint 단서를 담는다
    js_report = (tmp_path / "assembly_js_report.txt").read_text(encoding="utf-8")
    assert "/bill/bi/billInfoDetail.do" in js_report
    assert "[tabClick]" in js_report


@_SKIP
def test_session_values_are_removed_from_artifacts(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, serve_summary=True)
    for path in sorted(tmp_path.iterdir()):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "SECRET123ABC" not in text, f"{path.name} 에 세션 쿠키 값이 남음"
    # 원본 HAR 은 남기지 않는다
    assert not list(tmp_path.glob("*.raw.har"))
    # 그러나 헤더 이름은 살아 있어야 한다
    har = (tmp_path / "assembly_network.har").read_text(encoding="utf-8")
    assert "Set-Cookie" in har or "set-cookie" in har


@_SKIP
def test_exit_2_when_summary_is_never_found(tmp_path, monkeypatch):
    rc, man = _run(tmp_path, monkeypatch, serve_summary=False)

    assert rc == cn.EXIT_NO_SUMMARY
    assert man["summary_in_xhr"] is False
    assert man["summary_in_rendered_html"] is False
    # 실패해도 진단 자료는 남는다
    assert (tmp_path / "assembly_network.har").exists()
    assert (tmp_path / "assembly_xhr_manifest.json").exists()
    assert (tmp_path / "assembly_rendered.html").exists()
    # XHR 은 캡처되어 있어야 다음 단서를 잡을 수 있다
    assert any("billInfoDetail.do" in (r.get("url") or "") for r in man["responses"])


def test_missing_playwright_reports_env_error(monkeypatch):
    """Playwright 가 없으면 진단 실패(2)가 아니라 환경 오류(3)로 구분한다."""
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def _no_playwright(name, *a, **k):
        if name == "playwright":
            raise ImportError("no playwright")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", _no_playwright)
    assert cn.main(["--bill-id", "PRC_X"]) == cn.EXIT_ENV


def test_requires_bill_id_or_url():
    with pytest.raises(SystemExit):
        cn.main([])
