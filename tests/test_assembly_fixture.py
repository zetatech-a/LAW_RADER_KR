"""실제 의안정보시스템 응답 fixture 기반 회귀 테스트.

여기 테스트는 **추정으로 만든 HTML 이 아니라 실제 응답**을 대상으로 한다.
fixture 는 아래 명령으로 한 번 캡처해 커밋한다(한국 네트워크 또는 GitHub Actions):

    python scripts/capture_assembly_fixture.py --bill-id PRC_XXXXXXXXXXXX
    python scripts/capture_assembly_fixture.py --url "https://likms.assembly.go.kr/..."

fixture 가 없으면 이 파일의 **fixture 의존 테스트 5건이 skip** 된다. 즉 "초록불 =
검증 완료"가 아니라 "초록불이지만 skip 이 남아 있으면 아직 미검증"이다. 캡처가
끝나면 skip 은 0 이 된다 — 캡처 스크립트가 HTML 과 meta JSON 을 항상 함께 쓰므로,
fixture 가 있는데 meta 가 없으면 skip 이 아니라 '깨진 fixture'로 실패시킨다.
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import SourceConfig
from src.models import Post
from src.scrapers.assembly import AssemblyBillScraper

FIXTURES = Path(__file__).parent / "fixtures"
GET_HTML = FIXTURES / "assembly_detail_get.html"
POST_HTML = FIXTURES / "assembly_detail_post.html"
META = FIXTURES / "assembly_capture_meta.json"

HAS_FIXTURE = GET_HTML.exists()

_SKIP = pytest.mark.skipif(
    not HAS_FIXTURE,
    reason=(
        "실제 응답 fixture 미캡처 — 라이브 검증이 아직 끝나지 않았다. "
        "한국 네트워크나 GitHub Actions 에서 "
        "`python scripts/capture_assembly_fixture.py --bill-id <BILL_ID>` 를 실행해 "
        "tests/fixtures/ 를 커밋하면 이 테스트들이 활성화된다."
    ),
)

# fixture 에 실제 세션값·토큰이 남아 커밋되는 것을 막는 가드. 캡처 스크립트가
# 치환하지만, 사람이 손으로 넣은 파일에도 걸리도록 테스트로 한 번 더 검사한다.
_MUST_NOT_APPEAR = ("JSESSIONID=", "WMONID=")

# meta JSON 이 가져도 되는 키. 값(토큰·세션)을 담는 키가 새로 생기면 실패한다.
_ALLOWED_CSRF_META_KEYS = {"has_token", "header", "parameter"}
_ALLOWED_REQUEST_KEYS = {"method", "action", "data_keys", "header_keys"}


def _scraper(fetcher):
    return AssemblyBillScraper(
        SourceConfig(
            key="assembly_bill",
            name="의안정보시스템 · 계류의안",
            type="assembly_bill",
            list_url="https://likms.assembly.go.kr/bill/bi/bill/state/mooringBillPage.do",
            extra={},
        ),
        fetcher=fetcher,
    )


class _FixtureFetcher:
    """캡처한 실제 응답만 돌려주는 fetcher(네트워크 없음).

    실제로 나간 요청의 method·URL·필드명·헤더명을 기록해 meta 와 대조할 수 있게 한다.
    """

    def __init__(self, get_html, post_html=""):
        self._get_html = get_html
        self._post_html = post_html
        self.requests = []            # 상세 GET 이후의 '후속' 요청만 담는다
        self._n_get = 0

    def get(self, url, referer=None, params=None, headers=None):
        self._n_get += 1
        if self._n_get == 1:
            return ("get", 0)         # 상세 페이지
        self.requests.append(
            {
                "method": "get",
                "action": url,
                "data_keys": sorted(params or {}),
                "header_keys": sorted(headers or {}),
            }
        )
        return ("follow", 0)

    def post(self, url, referer=None, data=None, headers=None):
        self.requests.append(
            {
                "method": "post",
                "action": url,
                "data_keys": sorted(data or {}),
                "header_keys": sorted(headers or {}),
            }
        )
        return ("follow", 0)

    def text(self, resp):
        return self._get_html if resp[0] == "get" else self._post_html


def _meta() -> dict:
    """캡처 meta. fixture 가 있는데 meta 가 없으면 '깨진 fixture'로 실패시킨다."""
    assert META.exists(), (
        "assembly_detail_get.html 은 있는데 assembly_capture_meta.json 이 없습니다. "
        "캡처 스크립트는 둘을 항상 함께 씁니다 — 손으로 넣었다면 스크립트로 다시 캡처하세요."
    )
    return json.loads(META.read_text(encoding="utf-8"))


def _detail_url(meta: dict) -> str:
    """상세 URL 은 캡처 당시 '최종' URL 을 쓴다(구 fallback 경로 하드코딩 금지).

    redirect 가 있었다면 final_url 이 현재 유효한 경로다. 여기에 옛 경로를 박아 두면
    사이트가 경로를 옮겨도 테스트는 계속 통과해 변경을 놓친다.
    """
    url = (meta.get("final_url") or meta.get("requested_url") or "").strip()
    assert url, "meta 에 final_url·requested_url 이 모두 없습니다"
    return url


def _post(meta: dict) -> Post:
    url = _detail_url(meta)
    bill_id = url.split("billId=")[1].split("&")[0] if "billId=" in url else "PRC_FIXTURE"
    return Post(
        source_key="assembly_bill",
        source_name="의안정보시스템 · 계류의안",
        post_id=bill_id,
        title="(fixture)",
        url=url,
    )


def _replay():
    """fixture 로 enrich 를 재생하고 (post, fetcher) 를 돌려준다."""
    meta = _meta()
    f = _FixtureFetcher(
        GET_HTML.read_text(encoding="utf-8"),
        POST_HTML.read_text(encoding="utf-8") if POST_HTML.exists() else "",
    )
    p = _post(meta)
    _scraper(f).enrich(p)
    return meta, p, f


# --- 캡처 스크립트의 정화(sanitize) 동작 — fixture 없이도 검증 가능 ---


def test_sanitizer_removes_secrets_but_keeps_structure():
    """커밋될 fixture 에 세션값·토큰·개인정보가 남지 않는지 미리 못박는다.

    동시에 테스트가 검증하는 대상(필드'명'·action·셀렉터)은 반드시 보존되어야 한다 —
    이름까지 지우면 fixture 로 아무것도 확인할 수 없다.
    """
    from scripts.capture_assembly_fixture import _sanitize

    dirty = (
        '<html><head>'
        '<meta name="_csrf" content="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"/>'
        '<meta name="_csrf_header" content="X-CSRF-TOKEN"/>'
        '<meta name="_csrf_parameter" content="_csrf"/>'
        '</head><body>'
        '<script>document.cookie="JSESSIONID=ABCD1234EFGH";</script>'
        '<form id="summaryForm" action="/bill/summaryPopup.do" method="post">'
        '<input type="hidden" name="billId" value="PRC_A1"/>'
        '<input type="hidden" name="OWASP_CSRFTOKEN" value="tok-secret-999"/>'
        '<input type="hidden" name="jsessionid" value="ABCD1234EFGH"/>'
        '</form>'
        '<p>담당자 홍길동 010-1234-5678 hong@example.com 900101-1234567</p>'
        '<pre id="prntSummary">제안이유 및 주요내용 본문</pre>'
        '</body></html>'
    )
    out = _sanitize(dirty)

    for secret in (
        "tok-secret-999", "ABCD1234EFGH", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
        "hong@example.com", "010-1234-5678", "900101-1234567",
    ):
        assert secret not in out, secret

    for kept in (
        'name="billId"', 'name="OWASP_CSRFTOKEN"', 'name="jsessionid"',
        'action="/bill/summaryPopup.do"', 'id="prntSummary"', "제안이유 및 주요내용 본문",
    ):
        assert kept in out, kept

    # CSRF 헤더·파라미터'명'은 비밀이 아니고, 지우면 요청 형태를 재현할 수 없다
    assert "X-CSRF-TOKEN" in out
    assert 'content="_csrf"' in out


def test_fixture_presence_is_reported():
    """fixture 유무를 항상 드러낸다 — 미캡처를 '통과'로 오해하지 않도록."""
    if not HAS_FIXTURE:
        pytest.skip(
            "라이브 미검증: tests/fixtures/assembly_detail_get.html 이 없습니다. "
            "scripts/capture_assembly_fixture.py 로 캡처해 커밋하세요."
        )
    assert GET_HTML.stat().st_size > 0


# --- 실제 응답 기반 검증 ---


@_SKIP
def test_fixture_and_meta_contain_no_secret_values():
    """fixture·meta 어디에도 토큰/세션 '값'이 남아 있으면 안 된다."""
    for path in (GET_HTML, POST_HTML):
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for marker in _MUST_NOT_APPEAR:
            assert marker not in text, f"{path.name} 에 세션값이 남아 있음: {marker}"

    meta = _meta()
    # meta 는 '이름'과 '유무'만 담는다. 값을 담는 키가 늘어나면 여기서 걸린다.
    assert set(meta.get("csrf_meta", {})) <= _ALLOWED_CSRF_META_KEYS
    req = meta.get("follow_up_request")
    if req is not None:
        assert set(req) <= _ALLOWED_REQUEST_KEYS, set(req)
    blob = json.dumps(meta, ensure_ascii=False)
    for marker in _MUST_NOT_APPEAR:
        assert marker not in blob


@_SKIP
def test_real_response_fills_proposal_reason_into_body():
    """실제 응답으로 post.body 에 '제안이유 및 주요내용'이 저장되어야 한다."""
    _, p, _f = _replay()

    assert p.body, (
        "실제 응답에서 제안이유를 뽑지 못했다. tests/fixtures/assembly_capture_meta.json 의 "
        "prnt_summary_selector_hits / forms 를 보고 셀렉터·폼 판정을 고쳐야 한다."
    )
    assert len(p.body) >= 20
    # 제안이유는 body 에만 담고 details 에는 담지 않는다
    assert p.details == []


@_SKIP
def test_detail_url_matches_captured_final_url():
    """상세 URL 은 캡처 당시 최종 URL 이어야 한다(구 경로 하드코딩 금지)."""
    meta = _meta()
    url = _detail_url(meta)
    assert url.startswith("https://likms.assembly.go.kr/"), url
    # redirect 가 있었다면 최종 URL 이 요청 URL 과 다르다 — 그 사실을 드러낸다.
    if meta.get("redirects"):
        assert url == meta["final_url"], (
            f"redirect 가 있었다: {meta['redirects']}. config.yaml 의 detail_url 을 "
            f"{url} 기준으로 맞추세요."
        )


@_SKIP
def test_replayed_request_matches_captured_shape():
    """재생한 요청의 method·action·필드명·헤더명이 캡처 기록과 일치해야 한다."""
    meta, _p, f = _replay()

    if meta.get("summary_in_get_html"):
        # 상세 HTML 에 이미 있으면 후속 요청을 하지 않아야 한다
        assert f.requests == [], f"불필요한 후속 요청: {f.requests}"
        assert meta.get("follow_up_request") is None
        return

    captured = meta.get("follow_up_request")
    assert captured, (
        "캡처 당시 후속 요청을 보내지 않았다(연관 폼 미발견). "
        "meta 의 forms 를 보고 _summary_form 판정을 넓혀야 한다."
    )
    assert len(f.requests) == 1, f"후속 요청이 1회여야 한다: {f.requests}"
    replayed = f.requests[0]

    assert replayed["method"] == captured["method"]
    assert replayed["action"] == captured["action"]
    assert replayed["data_keys"] == captured["data_keys"]
    assert replayed["header_keys"] == captured["header_keys"]

    # action 은 반드시 HTTPS + 의안정보시스템 호스트여야 한다
    assert replayed["action"].startswith("https://likms.assembly.go.kr/")
