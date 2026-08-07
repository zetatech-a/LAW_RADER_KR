"""의안 상세페이지가 실제로 보내는 XHR/fetch 요청을 브라우저로 캡처한다(진단 전용).

왜 필요한가
-----------
2026-08 라이브 확인 결과, 의안 상세페이지의 초기 HTML 에는 '제안이유 및 주요내용'이
없다. pre#prntSummary·#summaryContentDiv 도 없고, 심사정보 탭(#tab_billInfo_sect)이
빈 채로 있다가 JS(app.Tab.tabClick)가 채운다. 즉 requests 로 받는 HTML 만 봐서는
어떤 endpoint 가 본문을 주는지 알 수 없다.

HTML 에서 폼을 찾아 되쏘는 방식도 실패했다 — 기본 GET 폼(id="form")을 잘못 골라
billId 가 중복되면서 HTTP 400 이 났다. 그 폼이 제안이유 조회용이라는 근거도 없다.

그래서 endpoint 를 **추측하지 않고**, 브라우저가 실제로 보내는 요청을 그대로 기록한다.
이 스크립트는 진단 전용이며 생산 스크래퍼는 이것을 임포트하지 않는다(Playwright 는
생산 의존성이 아니다 — requirements.txt 에 넣지 않고 CI 캡처 모드에서만 설치한다).

사용
----
    python -m pip install playwright
    python -m playwright install --with-deps chromium
    python scripts/capture_assembly_network.py \
        --url "https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId=PRC_..."
    python scripts/capture_assembly_network.py --bill-id PRC_...

종료 코드
--------
    0  렌더링 HTML 또는 어떤 XHR 응답에서 제안이유/주요내용을 찾았다 → 계약 확정 가능
    2  못 찾았다. 아티팩트(HAR·XHR 본문·JS·콘솔)를 보고 다음 단서를 잡아야 한다
    3  Playwright 가 없거나 브라우저를 띄우지 못했다(환경 문제)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUT_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

EXIT_OK = 0
EXIT_NO_SUMMARY = 2
EXIT_ENV = 3

TARGET_HOST = "likms.assembly.go.kr"
DETAIL_TEMPLATE = (
    "https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId={bill_id}"
)

# 심사정보 탭. 라이브 HTML 에서 확인된 선택자.
TAB_SELECTOR = '[data-tabnm="billInfo"]'
TAB_PANE_SELECTOR = "#tab_billInfo_sect"

# 응답 본문에서 찾을 표식. 하나라도 있으면 '제안이유를 주는 응답'으로 본다.
SUMMARY_MARKERS = ("prntSummary", "제안이유", "주요내용")

# 페이지가 로드하는 상세 스크립트(라이브 확인). 없으면 조용히 건너뛴다.
BILL_DETAIL_JS_HINT = "billDetail.js"


# ── 보안: 아티팩트에서 지울 값 ────────────────────────────────────────────────
#
# 원칙: **이름은 남기고 값만 지운다.** 헤더 이름·폼 키 이름이야말로 우리가 확정하려는
# 계약이고, 값(세션·토큰)은 저장소·아티팩트에 남아서는 안 된다.
_SECRET_HEADERS = {
    "cookie", "set-cookie", "authorization", "proxy-authorization",
    "x-csrf-token", "x-xsrf-token", "csrf-token", "x-auth-token",
}
# 이름에 이 조각이 들어간 헤더/쿼리/폼 키는 값을 지운다(위 목록에 없어도).
_SECRET_NAME_HINTS = (
    "csrf", "xsrf", "token", "session", "jsessionid", "wmonid",
    "auth", "secret", "passwd", "password", "nonce", "sid",
)
# 값 자체로 알아볼 수 있는 것들
_SECRET_VALUE_PATTERNS = (
    re.compile(r"JSESSIONID=[^;\"'\s&]+", re.I),
    re.compile(r"WMONID=[^;\"'\s&]+", re.I),
    re.compile(r"\b[0-9a-f]{32,}\b", re.I),
    re.compile(r"\b\d{6}-[1-4]\d{6}\b"),                # 주민등록번호 형태
    re.compile(r"\b01[016-9]-?\d{3,4}-?\d{4}\b"),       # 휴대전화
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),             # 이메일
)
REDACTED = "REDACTED"

# 공개 식별자라 값을 남겨도 되는 키(계약 확인에 필요하다). 이름 힌트보다 우선한다.
_PUBLIC_QUERY_KEYS = {"billid", "bill_id", "billno", "agefrom", "ageto", "age", "tabnm"}


def _is_secret_name(name: str) -> bool:
    low = (name or "").lower()
    if low in _SECRET_HEADERS:
        return True
    if low in _PUBLIC_QUERY_KEYS:
        return False
    return any(h in low for h in _SECRET_NAME_HINTS)


def _scrub_text(text: str) -> str:
    """본문 문자열에서 값 패턴을 지운다."""
    out = text or ""
    for pat in _SECRET_VALUE_PATTERNS:
        out = pat.sub(REDACTED, out)
    return out


def _scrub_html(html: str) -> str:
    """HTML 본문을 파싱해 **이름이 비밀인 필드의 값**부터 지우고 _scrub_text 를 돌린다.

    값 패턴만으로는 부족하다. _SECRET_VALUE_PATTERNS 는 32자 이상 연속 16진수만 잡는데,
    Spring 기본 CSRF 토큰은 하이픈이 섞인 UUID 라 그 패턴에 걸리지 않는다. 즉
    meta[name="_csrf"] 의 content 나 hidden input 의 토큰 값이 그대로 아티팩트에 실려
    업로드된다. 그래서 fixture 캡처(capture_assembly_fixture._sanitize)와 같은 기준으로
    이름을 보고 값을 지운다.

    구조와 이름은 반드시 남긴다 — 우리가 확정하려는 계약이 바로 그 이름들이다.
    파싱에 실패하면 원문을 흘리지 않고 값 패턴 정화라도 반드시 적용한다(fail-safe).
    """
    text = html or ""
    try:
        soup = BeautifulSoup(text, "lxml")
    except Exception:  # noqa: BLE001 — 진단 도구가 파서 문제로 죽지 않게 한다
        return _scrub_text(text)

    for el in soup.find_all(["input", "meta"]):
        name = (el.get("name") or el.get("id") or "").lower()
        attr = "value" if el.name == "input" else "content"
        if not el.get(attr) or not _is_secret_name(name):
            continue
        # _csrf_header / _csrf_parameter 의 content 는 토큰이 아니라 '이름'이다
        # (예: X-CSRF-TOKEN). 계약 확인에 필요하고 비밀이 아니므로 남긴다.
        if el.name == "meta" and ("header" in name or "param" in name):
            continue
        el[attr] = REDACTED

    # 인라인 JS 에 토큰이 박혀 오는 경우가 흔하다. 계약 분석은 별도로 내려받는
    # billDetail.js 로 하므로, 문서에 인라인된 스크립트 본문은 비워도 잃을 게 없다.
    for el in soup.find_all("script"):
        if not (el.get("src") or "").strip():
            el.string = ""

    return _scrub_text(str(soup))


def _scrub_json_value(value):
    """JSON 을 재귀로 훑어 **이름이 비밀인 키의 값**을 지운다.

    HTML 과 같은 이유다 — 값 패턴은 UUID·Base64 토큰을 잡지 못하므로, 응답 JSON 에
    csrfToken·sessionId·authToken 같은 필드가 있으면 값이 그대로 아티팩트에 실린다.
    키 이름은 계약의 일부이므로 남기고 값만 지운다. 중첩된 객체·배열도 훑는다.
    """
    if isinstance(value, dict):
        return {
            k: (REDACTED if _is_secret_name(str(k)) else _scrub_json_value(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub_json_value(v) for v in value]
    if isinstance(value, str):
        return _scrub_text(value)
    return value                      # 숫자·불리언·null 은 그대로


def _scrub_json_text(raw: str) -> str:
    """JSON 문자열을 정화해 돌려준다. 파싱 실패 시 값 패턴 정화라도 적용한다."""
    text = raw or ""
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return _scrub_text(text)
    return json.dumps(_scrub_json_value(parsed), ensure_ascii=False, indent=2)


def _scrub_body(text: str, *, is_json: bool) -> str:
    """응답 본문 정화 — 종류에 맞는 이름 기준 마스킹을 먼저 돌린다."""
    return _scrub_json_text(text) if is_json else _scrub_html(text)


def _scrub_headers(headers: dict) -> dict:
    """헤더는 이름을 모두 남기고, 비밀인 것만 값을 REDACTED 로."""
    return {
        k: (REDACTED if _is_secret_name(k) else _scrub_text(str(v)))
        for k, v in (headers or {}).items()
    }


def _scrub_url(url: str) -> str:
    """URL 쿼리에서 세션·토큰 '값'만 지운다. 키 이름과 공개 BILL_ID 는 남긴다.

    주의: 조립이 끝난 URL 문자열에 _scrub_text 를 다시 돌리면 안 된다. JSESSIONID
    패턴이 'jsessionid=REDACTED' 를 통째로 잡아먹어 **키 이름까지 사라진다**. 키 이름은
    우리가 확정하려는 계약의 일부이므로 값만 따로 정화한다.
    """
    try:
        parts = urlparse(url)
    except ValueError:
        return REDACTED
    # 경로 파라미터(;jsessionid=...)를 잊으면 안 된다. 쿠키가 막힌 클라이언트에 대고
    # 서블릿 컨테이너가 세션 ID 를 이 자리에 붙여 주는데(likms 가 그 형태다),
    # urlparse 는 그것을 path 가 아니라 params 로 떼어 놓는다. path 만 정화하면
    # 세션 ID 가 그대로 살아남는다.
    params = _scrub_path_params(parts.params)
    if not parts.query:
        return urlunparse(
            parts._replace(path=_scrub_text(parts.path), params=params)
        )
    pairs = [
        (k, REDACTED if _is_secret_name(k) else _scrub_text(v))
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunparse(
        parts._replace(
            path=_scrub_text(parts.path), params=params, query=urlencode(pairs)
        )
    )


def _scrub_path_params(params: str) -> str:
    """경로 파라미터(;a=b;c=d)에서 비밀 이름의 값만 지운다. 이름은 남긴다."""
    if not params:
        return ""
    out = []
    for chunk in params.split(";"):
        if "=" not in chunk:
            out.append(_scrub_text(chunk))
            continue
        name, _, value = chunk.partition("=")
        out.append(
            f"{name}={REDACTED if _is_secret_name(name) else _scrub_text(value)}"
        )
    return ";".join(out)


def _manifest_url(url: str) -> str:
    """manifest·표준출력에 남기는 URL. **아티팩트로 나가는 URL 은 전부 이걸 거친다.**

    requests_log 의 URL 은 처음부터 _scrub_url 을 거쳤는데 requested_url·final_url 만
    원본이었다. 입력 URL(워크플로 입력)에 비밀 쿼리가 있거나 redirect 가 세션값이 붙은
    주소로 끌고 가면 그 값이 그대로 실린다. 규칙에 이름을 붙여 빠뜨리지 않게 한다.
    """
    return _scrub_url(url)


def _scrub_post_data(raw: str | None) -> dict:
    """요청 본문을 '키 이름 + 값 마스킹' 형태로 바꾼다.

    JSON 을 먼저 본다 — parse_qsl 은 '=' 이 없는 문자열도 (전체, '') 한 쌍으로 받아
    주므로, JSON 본문을 form 으로 오인해 본문 전체가 '키 이름'이 되어 버린다.
    """
    if not raw:
        return {"present": False}

    stripped = raw.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {"present": True, "kind": "raw", "length": len(raw)}
        if isinstance(parsed, dict):
            # 최상위 키만 보면 안 된다. {"payload": {"csrfToken": "<UUID>"}} 처럼
            # 비밀이 아닌 키 아래에 토큰이 들어 있으면 값이 통째로 복사되어
            # manifest 와 정화 HAR 양쪽에 실린다. 남기는 값은 재귀 정화를 거친다.
            return {
                "present": True,
                "kind": "json",
                "keys": sorted(parsed),
                "masked": _scrub_json_value(parsed),
            }
        return {"present": True, "kind": "json", "keys": [], "length": len(raw)}

    # form-urlencoded: '=' 이 하나라도 있어야 키/값 구조로 본다.
    if "=" in raw:
        try:
            pairs = parse_qsl(raw, keep_blank_values=True)
        except ValueError:
            pairs = []
        if pairs and all(k for k, _ in pairs):
            return {
                "present": True,
                "kind": "form",
                "keys": sorted({k for k, _ in pairs}),
                "masked": {
                    k: (REDACTED if _is_secret_name(k) else _scrub_text(v))
                    for k, v in pairs
                },
            }
    return {"present": True, "kind": "raw", "length": len(raw)}


def _sanitize_har(har: dict) -> dict:
    """HAR 에서 쿠키·인증 헤더·쿼리 토큰 값을 제거한다(이름은 유지)."""
    entries = ((har.get("log") or {}).get("entries")) or []
    for entry in entries:
        req = entry.get("request") or {}
        res = entry.get("response") or {}
        req["url"] = _scrub_url(req.get("url", ""))
        for holder in (req, res):
            holder["cookies"] = []          # 쿠키는 통째로 버린다
            holder["headers"] = [
                {
                    "name": h.get("name", ""),
                    "value": REDACTED
                    if _is_secret_name(h.get("name", ""))
                    else _scrub_text(str(h.get("value", ""))),
                }
                for h in (holder.get("headers") or [])
            ]
        for q in req.get("queryString") or []:
            if _is_secret_name(q.get("name", "")):
                q["value"] = REDACTED
        post = req.get("postData") or {}
        if post.get("text"):
            post["text"] = json.dumps(
                _scrub_post_data(post["text"]), ensure_ascii=False
            )
        for p in post.get("params") or []:
            if _is_secret_name(p.get("name", "")):
                p["value"] = REDACTED
        content = res.get("content") or {}
        if content.get("text"):
            # HAR 에 박제된 응답 본문도 같은 위험을 갖는다(HTML 이면 필드 값에, JSON
            # 이면 키 값에 토큰이 들어 있다). mimeType 을 못 믿을 때를 대비해 본문
            # 모양으로도 한 번 더 본다.
            content["text"] = _scrub_response_body(
                content["text"], content.get("mimeType") or ""
            )
    return har


def _scrub_response_body(body: str, mime: str) -> str:
    """mimeType 과 본문 모양으로 종류를 정하고 그에 맞는 정화를 적용한다.

    어느 쪽으로도 단정할 수 없으면 HTML 로 본다 — HTML 정화는 파싱에 실패해도 값
    패턴 정화로 물러나므로, 판단이 틀렸을 때 잃는 것이 없다(fail-safe).
    """
    text = body or ""
    low = (mime or "").lower()
    head = text.lstrip()[:200].lower()
    if "json" in low or head.startswith(("{", "[")):
        return _scrub_json_text(text)
    return _scrub_html(text)


# ── JS 정적 분석 ─────────────────────────────────────────────────────────────
_JS_PATTERNS = {
    "do_urls": re.compile(r"""['"]([^'"\s]*\.do(?:\?[^'"\s]*)?)['"]"""),
    "ajax": re.compile(r"\$\.ajax|\$\.post|\$\.get\b|XMLHttpRequest"),
    "axios": re.compile(r"\baxios\b"),
    "fetch": re.compile(r"\bfetch\s*\("),
    "tabClick": re.compile(r"tabClick"),
    "billInfo": re.compile(r"billInfo"),
    "prntSummary": re.compile(r"prntSummary"),
}


def _js_report(js: str) -> str:
    """JS 에서 endpoint 단서를 뽑아 사람이 읽을 보고서로."""
    lines = js.splitlines()
    out: list[str] = ["=" * 70, "billDetail.js 정적 분석", "=" * 70]

    urls = sorted(set(_JS_PATTERNS["do_urls"].findall(js)))
    out.append(f"\n[.do URL 문자열] {len(urls)}개")
    out.extend(f"  {u}" for u in urls)

    for name in ("ajax", "axios", "fetch", "tabClick", "billInfo", "prntSummary"):
        pat = _JS_PATTERNS[name]
        hits = [i for i, line in enumerate(lines) if pat.search(line)]
        out.append(f"\n[{name}] {len(hits)}곳")
        for i in hits[:20]:                      # 주변 코드 함께
            lo, hi = max(0, i - 4), min(len(lines), i + 6)
            out.append(f"  --- line {i + 1} ---")
            out.extend(f"  {n + 1:>6}: {lines[n][:200]}" for n in range(lo, hi))
        if len(hits) > 20:
            out.append(f"  ... 외 {len(hits) - 20}곳")
    return "\n".join(out)


# ── 캡처 ─────────────────────────────────────────────────────────────────────
def capture(
    url: str, out_dir: Path, timeout_ms: int, browser_path: str = ""
) -> dict:
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    # 원본 HAR 은 **업로드되는 어떤 경로에도 두지 않는다.**
    #
    # record_har_content="embed" 라 원본에는 쿠키·Authorization·CSRF 토큰이 응답 본문째
    # 들어 있다. 예전에는 out_dir(tests/fixtures/) 안에 두고 캡처가 끝난 뒤 지웠는데,
    # 그 사이 어디서든 예외가 나면 파일이 남고 워크플로가 tests/fixtures/ 를 통째로
    # 아티팩트에 올린다(캡처 단계는 continue-on-error 라 실패해도 업로드까지 간다).
    # 임시 디렉터리에 두고 finally 로 지우면, 지우기에 실패해도 업로드 경로 밖이다.
    har_dir = Path(tempfile.mkdtemp(prefix="assembly-har-"))
    har_raw = har_dir / "assembly_network.raw.har"
    console_lines: list[str] = []
    requests_log: list[dict] = []
    responses: list[dict] = []

    result: dict = {
        "requested_url": _manifest_url(url),
        "final_url": "",
        "tab_found": False,
        "tab_clicked": False,
        "tab_pane_filled": False,
        "summary_in_rendered_html": False,
        "summary_in_xhr": False,
        "summary_sources": [],
        "errors": [],
    }

    launch_kw: dict = {"args": ["--no-sandbox"]}
    if browser_path:
        # playwright install 을 돌릴 수 없는 환경(오프라인·사전설치 이미지)에서
        # 이미 있는 Chromium 을 그대로 쓴다.
        launch_kw["executable_path"] = browser_path

    try:
        manifest = _capture_with_browser(
            sync_playwright, PWTimeout, launch_kw, url, out_dir, timeout_ms,
            har_raw, result, console_lines, requests_log, responses,
        )
    finally:
        # 정화 성공 여부와 무관하게 원본은 반드시 없앤다. 임시 디렉터리째 지우므로
        # 캡처가 중간에 죽어 HAR 이 flush 된 경우에도 남지 않는다.
        shutil.rmtree(har_dir, ignore_errors=True)

    result["requests"] = requests_log
    result["responses"] = manifest
    _write(out_dir / "browser_console.txt", _scrub_text("\n".join(console_lines)))
    _write(
        out_dir / "assembly_xhr_manifest.json",
        json.dumps(result, ensure_ascii=False, indent=2),
    )
    return result


def _capture_with_browser(
    sync_playwright, PWTimeout, launch_kw, url, out_dir, timeout_ms,
    har_raw, result, console_lines, requests_log, responses,
) -> list[dict]:
    """브라우저를 띄워 캡처하고 XHR manifest 를 돌려준다(HAR 정화까지)."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch_kw)
        context = browser.new_context(
            record_har_path=str(har_raw),
            record_har_content="embed",
            locale="ko-KR",
        )
        page = context.new_page()

        page.on(
            "console",
            lambda m: console_lines.append(f"[console.{m.type}] {m.text}"),
        )
        page.on("pageerror", lambda e: console_lines.append(f"[pageerror] {e}"))

        def _on_request(req):
            requests_log.append(
                {
                    "method": req.method,
                    "url": _scrub_url(req.url),
                    "resource_type": req.resource_type,
                    "headers": _scrub_headers(req.headers),
                    "post_data": _scrub_post_data(req.post_data),
                }
            )

        page.on("request", _on_request)
        page.on(
            "response",
            lambda r: responses.append(
                {"url": r.url, "status": r.status, "headers": dict(r.headers), "obj": r}
            ),
        )

        # 표준출력도 워크플로가 report 파일로 받아 업로드하므로 정화본을 찍는다.
        print(f"GET(browser) {_manifest_url(url)}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except PWTimeout as e:
            result["errors"].append(f"goto timeout: {e}")

        result["final_url"] = _manifest_url(page.url)
        print(f"  → 최종 URL {result['final_url']}")

        # 심사정보 탭 확인 → 필요하면 클릭
        try:
            tab = page.locator(TAB_SELECTOR).first
            if tab.count() > 0:
                result["tab_found"] = True
                print(f"  심사정보 탭 발견: {TAB_SELECTOR}")
                if not _pane_filled(page):
                    tab.click(timeout=timeout_ms)
                    result["tab_clicked"] = True
                    print("  탭 클릭됨 — 내용이 채워지길 대기")
            else:
                print(f"  ! 심사정보 탭({TAB_SELECTOR})을 찾지 못함")
        except Exception as e:  # noqa: BLE001
            result["errors"].append(f"tab click: {type(e).__name__}: {e}")

        # 패널이 채워지거나 네트워크가 잦아들 때까지 명시적 대기
        try:
            page.wait_for_function(
                "sel => { const el = document.querySelector(sel);"
                " return el && el.textContent.trim().length > 50; }",
                arg=TAB_PANE_SELECTOR,
                timeout=timeout_ms,
            )
            result["tab_pane_filled"] = True
            print(f"  {TAB_PANE_SELECTOR} 채워짐")
        except PWTimeout:
            result["errors"].append(f"{TAB_PANE_SELECTOR} 가 채워지지 않음(timeout)")
            print(f"  ! {TAB_PANE_SELECTOR} 가 비어 있음 — networkidle 로 대기")
        try:
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PWTimeout:
            result["errors"].append("networkidle timeout")

        # 렌더링 완료 HTML
        rendered = page.content()
        _write(out_dir / "assembly_rendered.html", _scrub_html(rendered))
        result["summary_in_rendered_html"] = _has_summary(rendered)

        # 응답 본문 저장 (likms 호스트의 document/xhr/fetch, html/json 만)
        manifest = _dump_responses(responses, out_dir, result)

        # billDetail.js 를 같은 세션으로 직접 내려받는다
        js_url = _find_js_url(requests_log)
        if js_url:
            try:
                got = context.request.get(js_url)
                js = got.text()
                _write(out_dir / "assembly_billDetail.js", _scrub_text(js))
                _write(out_dir / "assembly_js_report.txt", _js_report(js))
                result["billDetail_js"] = js_url
            except Exception as e:  # noqa: BLE001
                result["errors"].append(f"js download: {type(e).__name__}: {e}")
        else:
            print(f"  ! {BILL_DETAIL_JS_HINT} 요청을 보지 못함")

        context.close()   # HAR 은 여기서 flush 된다
        browser.close()

    # HAR sanitize — 정화본만 out_dir(업로드 경로)에 쓴다. 원본은 호출자가 지운다.
    if har_raw.exists():
        try:
            har = json.loads(har_raw.read_text(encoding="utf-8"))
            _write(
                out_dir / "assembly_network.har",
                json.dumps(_sanitize_har(har), ensure_ascii=False),
            )
        except Exception as e:  # noqa: BLE001
            result["errors"].append(f"har sanitize: {type(e).__name__}: {e}")

    return manifest


def _pane_filled(page) -> bool:
    try:
        el = page.locator(TAB_PANE_SELECTOR).first
        return el.count() > 0 and len((el.inner_text() or "").strip()) > 50
    except Exception:  # noqa: BLE001
        return False


def _has_summary(text: str) -> bool:
    return any(m in (text or "") for m in SUMMARY_MARKERS)


def _markers_in(text: str) -> list[str]:
    return [m for m in SUMMARY_MARKERS if m in (text or "")]


def _dump_responses(responses: list[dict], out_dir: Path, result: dict) -> list[dict]:
    """likms 호스트의 document/xhr/fetch 응답 본문을 파일로 저장하고 manifest 를 만든다."""
    manifest: list[dict] = []
    n = 0
    for r in responses:
        url = r["url"]
        if TARGET_HOST not in urlparse(url).netloc:
            continue
        ctype = (r["headers"].get("content-type") or "").lower()
        is_html = "text/html" in ctype
        is_json = "json" in ctype
        if not (is_html or is_json):
            continue
        try:
            body = r["obj"].text()
        except Exception as e:  # noqa: BLE001
            manifest.append({"url": _scrub_url(url), "error": f"{type(e).__name__}: {e}"})
            continue

        n += 1
        ext = "json" if is_json else "html"
        name = f"xhr_response_{n:03d}.{ext}"
        # HTML 이든 JSON 이든 값 패턴만으로는 UUID·Base64 토큰을 못 잡는다.
        # 종류에 맞게 이름 기준 마스킹을 먼저 돌린다.
        _write(out_dir / name, _scrub_body(body, is_json=is_json))

        markers = _markers_in(body)
        if markers:
            result["summary_in_xhr"] = True
            result["summary_sources"].append({"file": name, "url": _scrub_url(url)})
        manifest.append(
            {
                "file": name,
                "url": _scrub_url(url),
                "status": r["status"],
                "content_type": ctype,
                "length": len(body),
                "markers": markers,
                "has_prntSummary": "prntSummary" in body,
                "has_제안이유": "제안이유" in body,
                "has_주요내용": "주요내용" in body,
            }
        )
    return manifest


def _find_js_url(requests_log: list[dict]) -> str:
    for r in requests_log:
        if BILL_DETAIL_JS_HINT in r["url"]:
            return r["url"]
    return ""


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    print(f"저장: {path}")


def _print_diagnosis(result: dict) -> None:
    print("\n❌ 제안이유/주요내용을 어디에서도 찾지 못했습니다. 아티팩트는 저장했습니다.")
    print("\n[진단]")
    print(f"  · 심사정보 탭 발견: {result['tab_found']} / 클릭: {result['tab_clicked']}")
    print(f"  · {TAB_PANE_SELECTOR} 채워짐: {result['tab_pane_filled']}")
    print(f"  · 렌더링 HTML 에 표식: {result['summary_in_rendered_html']}")
    xhrs = result.get("responses") or []
    print(f"  · 저장한 likms 응답: {len(xhrs)}건")
    for m in xhrs:
        if "file" in m:
            print(
                f"    - {m['file']} {m['status']} {m['content_type']} "
                f"{m['length']}B markers={m['markers']} {m['url']}"
            )
    if result.get("errors"):
        print("  · 오류:")
        for e in result["errors"]:
            print(f"    - {e}")
    print(
        "\n다음 단서: assembly_js_report.txt 의 .do URL 목록과 tabClick/billInfo 주변 "
        "코드를 보고, 심사정보 탭이 부르는 endpoint 를 확정하세요.\n"
        "assembly_network.har 에는 모든 요청이 남아 있습니다(값은 마스킹됨)."
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bill-id", default="", help="의안 ID(BILL_ID)")
    ap.add_argument("--url", default="", help="상세 URL 직접 지정(--bill-id 대신)")
    ap.add_argument("--out", default=str(OUT_DIR), help="아티팩트 출력 디렉터리")
    ap.add_argument("--timeout-ms", type=int, default=20000, help="단계별 대기 상한")
    ap.add_argument(
        "--browser-path",
        default=os.environ.get("PLAYWRIGHT_CHROMIUM_PATH", ""),
        help="이미 설치된 Chromium 실행 파일 경로(playwright install 을 못 쓰는 환경용). "
        "환경변수 PLAYWRIGHT_CHROMIUM_PATH 로도 지정 가능.",
    )
    args = ap.parse_args(argv)

    if not args.bill_id and not args.url:
        ap.error("--bill-id 또는 --url 중 하나는 필요합니다")

    url = args.url or DETAIL_TEMPLATE.format(bill_id=args.bill_id)

    try:
        import playwright  # noqa: F401
    except ImportError:
        print(
            "Playwright 가 설치되어 있지 않습니다(진단 전용 의존성 — 생산 코드는 쓰지 않음).\n"
            "  python -m pip install playwright\n"
            "  python -m playwright install --with-deps chromium",
            file=sys.stderr,
        )
        return EXIT_ENV

    try:
        result = capture(url, Path(args.out), args.timeout_ms, args.browser_path)
    except Exception as e:  # noqa: BLE001
        print(f"브라우저 캡처 실패: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_ENV

    print("\n--- 요약 ---")
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in ("requests", "responses")},
            ensure_ascii=False,
            indent=2,
        )
    )

    if result["summary_in_rendered_html"] or result["summary_in_xhr"]:
        where = []
        if result["summary_in_rendered_html"]:
            where.append("렌더링 HTML")
        if result["summary_in_xhr"]:
            where.append(
                "XHR("
                + ", ".join(s["file"] for s in result["summary_sources"])
                + ")"
            )
        print(f"\n✅ 제안이유/주요내용을 찾았습니다: {', '.join(where)}")
        print("assembly_xhr_manifest.json 의 해당 요청이 확정할 endpoint 계약입니다.")
        return EXIT_OK

    _print_diagnosis(result)
    return EXIT_NO_SUMMARY


if __name__ == "__main__":
    sys.exit(main())
