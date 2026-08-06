"""실제 의안정보시스템 응답 fixture 기반 회귀 테스트.

여기 테스트는 **추정으로 만든 HTML 이 아니라 실제 응답**을 대상으로 한다.
fixture 는 아래 명령으로 한 번 캡처해 커밋한다(한국 네트워크 또는 GitHub Actions):

    python scripts/capture_assembly_fixture.py --bill-id PRC_XXXXXXXXXXXX

fixture 가 없으면 이 파일의 테스트는 **skip** 된다. 즉 "초록불 = 검증 완료"가 아니라
"초록불이지만 skip 이면 아직 미검증"이다. 미검증 상태를 통과로 착각하지 않도록
test_fixture_presence_is_reported 가 상태를 항상 출력한다.
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

_SKIP = pytest.mark.skipif(
    not GET_HTML.exists(),
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
    """캡처한 실제 응답만 돌려주는 fetcher(네트워크 없음)."""

    def __init__(self, get_html, post_html=""):
        self._get_html = get_html
        self._post_html = post_html
        self.gets = []
        self.posts = []

    def get(self, url, referer=None, params=None, headers=None):
        self.gets.append({"url": url, "params": params, "headers": headers or {}})
        # 후속 GET(폼 method=get)이면 후속 응답을 준다
        if len(self.gets) > 1 and self._post_html:
            return ("follow", 0)
        return ("get", 0)

    def post(self, url, referer=None, data=None, headers=None):
        self.posts.append({"url": url, "data": data or {}, "headers": headers or {}})
        return ("follow", 0)

    def text(self, resp):
        return self._get_html if resp[0] == "get" else self._post_html


def _bill_id() -> str:
    if META.exists():
        meta = json.loads(META.read_text(encoding="utf-8"))
        url = meta.get("final_url") or meta.get("requested_url") or ""
        if "billId=" in url:
            return url.split("billId=")[1].split("&")[0]
    return "PRC_FIXTURE"


def _post() -> Post:
    bid = _bill_id()
    return Post(
        source_key="assembly_bill",
        source_name="의안정보시스템 · 계류의안",
        post_id=bid,
        title="(fixture)",
        url=f"https://likms.assembly.go.kr/bill/billDetail.do?billId={bid}",
    )


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
    if not GET_HTML.exists():
        pytest.skip(
            "라이브 미검증: tests/fixtures/assembly_detail_get.html 이 없습니다. "
            "scripts/capture_assembly_fixture.py 로 캡처해 커밋하세요."
        )
    assert GET_HTML.stat().st_size > 0


@_SKIP
def test_fixture_contains_no_session_values():
    for path in (GET_HTML, POST_HTML):
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for marker in _MUST_NOT_APPEAR:
            assert marker not in text, f"{path.name} 에 세션값이 남아 있음: {marker}"


@_SKIP
def test_real_response_fills_proposal_reason_into_body():
    """실제 응답으로 post.body 에 '제안이유 및 주요내용'이 저장되어야 한다."""
    get_html = GET_HTML.read_text(encoding="utf-8")
    post_html = POST_HTML.read_text(encoding="utf-8") if POST_HTML.exists() else ""
    f = _FixtureFetcher(get_html, post_html)
    p = _post()

    _scraper(f).enrich(p)

    assert p.body, (
        "실제 응답에서 제안이유를 뽑지 못했다. tests/fixtures/assembly_capture_meta.json 의 "
        "prnt_summary_selector_hits / forms 를 보고 셀렉터·폼 판정을 고쳐야 한다."
    )
    assert len(p.body) >= 20
    # 제안이유는 body 에만 담고 details 에는 담지 않는다
    assert p.details == []


@_SKIP
def test_real_response_request_shape_matches_capture():
    """캡처 당시 확인된 요청 형태(추가 요청 유무·POST URL)와 일치해야 한다."""
    if not META.exists():
        pytest.skip("assembly_capture_meta.json 이 없어 요청 형태를 대조할 수 없다")
    meta = json.loads(META.read_text(encoding="utf-8"))

    get_html = GET_HTML.read_text(encoding="utf-8")
    post_html = POST_HTML.read_text(encoding="utf-8") if POST_HTML.exists() else ""
    f = _FixtureFetcher(get_html, post_html)
    _scraper(f).enrich(_post())

    if meta.get("summary_in_get_html"):
        # 상세 HTML 에 이미 있으면 추가 요청을 하지 않아야 한다
        assert f.posts == []
        assert len(f.gets) == 1
    else:
        # 별도 요청이 필요한 구조라면 캡처된 action 으로 보내야 한다
        expected = meta.get("chosen_form_action") or ""
        sent = (f.posts[0]["url"] if f.posts else
                (f.gets[1]["url"] if len(f.gets) > 1 else ""))
        assert sent, "추가 요청이 필요한 구조인데 아무 요청도 보내지 않았다"
        if expected:
            assert sent == expected
