"""실제 의안정보시스템 응답 fixture 기반 회귀 테스트(AVAILABLE / PENDING 분리).

여기 테스트는 **추정으로 만든 HTML 이 아니라 실제 응답**을 대상으로 한다.
fixture 는 아래 두 명령으로 각각 캡처해 커밋한다(한국 네트워크 또는 GitHub Actions):

    python scripts/capture_assembly_fixture.py --expect available --bill-id PRC_...
    python scripts/capture_assembly_fixture.py --expect pending   --bill-id PRC_...

디렉터리(서로 덮어쓰지 않는다):

    tests/fixtures/assembly/available/{detail.html,billinfo.html,meta.json}
    tests/fixtures/assembly/pending/{detail.html,billinfo.html,meta.json}

fixture 가 없으면 해당 상태의 테스트가 **skip** 된다. 즉 "초록불 = 검증 완료"가 아니라
"초록불이지만 skip 이 남아 있으면 아직 미검증"이다.

ASSEMBLY_FIXTURE_DIR 환경변수로 루트를 바꿀 수 있다(캡처 직후 임시 디렉터리로 검증).
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import SourceConfig
from src.models import Post, ProposalContentStatus as S
from src.scrapers.assembly import (
    _BILLINFO_ENDPOINT,
    _CSRF_HEADER,
    _MIN_SUMMARY_CHARS,
    AssemblyBillScraper,
)

FIXTURE_ROOT = Path(
    os.environ.get("ASSEMBLY_FIXTURE_DIR")
    or (Path(__file__).parent / "fixtures" / "assembly")
)
AVAILABLE_DIR = FIXTURE_ROOT / "available"
PENDING_DIR = FIXTURE_ROOT / "pending"

_CAPTURE_HINT = (
    "python scripts/capture_assembly_fixture.py --expect {state} --bill-id <BILL_ID> "
    "를 실행해 tests/fixtures/assembly/{state}/ 를 커밋하면 활성화된다."
)


def _has(state_dir: Path) -> bool:
    return (state_dir / "detail.html").exists()


_SKIP_AVAILABLE = pytest.mark.skipif(
    not _has(AVAILABLE_DIR),
    reason="실제 AVAILABLE fixture 미캡처 — " + _CAPTURE_HINT.format(state="available"),
)
_SKIP_PENDING = pytest.mark.skipif(
    not _has(PENDING_DIR),
    reason="실제 PENDING fixture 미캡처 — " + _CAPTURE_HINT.format(state="pending"),
)

# fixture 에 실제 세션값·토큰이 남아 커밋되는 것을 막는 가드.
_MUST_NOT_APPEAR = ("JSESSIONID=", "WMONID=")

# meta JSON 이 가져도 되는 키. 값(토큰·세션)을 담는 키가 새로 생기면 실패한다.
_ALLOWED_CSRF_META_KEYS = {"selector", "has_token"}
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

    def __init__(self, detail_html, billinfo_html=""):
        self._detail = detail_html
        self._billinfo = billinfo_html
        self.requests = []            # 상세 GET 이후의 '후속' 요청만 담는다
        self._n_get = 0

    def get(self, url, referer=None, params=None, headers=None):
        self._n_get += 1
        if self._n_get == 1:
            return ("get", 0)         # 상세 페이지
        # 확정 계약에서 후속 요청은 항상 POST 다. GET 이 기록되면 회귀다.
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
        return self._detail if resp[0] == "get" else self._billinfo


def _meta(state_dir: Path) -> dict:
    """캡처 meta. detail 은 있는데 meta 가 없으면 '깨진 fixture'로 실패시킨다."""
    path = state_dir / "meta.json"
    assert path.exists(), (
        f"{state_dir.name}/detail.html 은 있는데 meta.json 이 없습니다. "
        "캡처 스크립트는 항상 함께 씁니다 — 스크립트로 다시 캡처하세요."
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _detail_url(meta: dict) -> str:
    """상세 URL 은 캡처 당시 '최종' URL 을 쓴다(구 fallback 경로 하드코딩 금지)."""
    url = (meta.get("final_url") or meta.get("requested_url") or "").strip()
    assert url, "meta 에 final_url·requested_url 이 모두 없습니다"
    return url


def _replay(state_dir: Path):
    """fixture 로 enrich 를 재생하고 (meta, post, fetcher) 를 돌려준다."""
    meta = _meta(state_dir)
    billinfo = state_dir / "billinfo.html"
    f = _FixtureFetcher(
        (state_dir / "detail.html").read_text(encoding="utf-8"),
        billinfo.read_text(encoding="utf-8") if billinfo.exists() else "",
    )
    url = _detail_url(meta)
    bill_id = url.split("billId=")[1].split("&")[0] if "billId=" in url else "PRC_FIXTURE"
    p = Post(
        source_key="assembly_bill",
        source_name="의안정보시스템 · 계류의안",
        post_id=bill_id,
        title="(fixture)",
        url=url,
    )
    _scraper(f).enrich(p)
    return meta, p, f


def _assert_request_matches_contract(meta: dict, f: _FixtureFetcher):
    """재생한 요청이 캡처 기록·확정 계약과 모두 일치하는지."""
    captured = meta.get("follow_up_request")
    assert captured, (
        "캡처 당시 후속 요청을 보내지 않았다. meta 의 request_build_error 를 보고 "
        'form#form / meta[name="_csrf"] / billId 를 확인해야 한다.'
    )
    assert len(f.requests) == 1, f"후속 요청이 1회여야 한다: {f.requests}"
    replayed = f.requests[0]

    assert replayed["method"] == captured["method"]
    assert replayed["action"] == captured["action"]
    assert replayed["data_keys"] == captured["data_keys"]
    assert replayed["header_keys"] == captured["header_keys"]

    # 확정 계약과도 대조한다(캡처 meta 가 낡아도 계약 위반을 잡아내도록).
    assert replayed["method"] == "post"
    assert replayed["action"] == _BILLINFO_ENDPOINT
    assert replayed["header_keys"] == [_CSRF_HEADER]
    assert any(k.lower() in ("billid", "bill_id") for k in replayed["data_keys"])


# --- 캡처 스크립트의 정화(sanitize) 동작 — fixture 없이도 검증 가능 ---


def test_sanitizer_removes_secrets_but_keeps_structure():
    """커밋될 fixture 에 세션값·토큰·개인정보가 남지 않는지 미리 못박는다."""
    from scripts.capture_assembly_fixture import _sanitize

    dirty = (
        "<html><head>"
        '<meta name="_csrf" content="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"/>'
        "</head><body>"
        '<script>document.cookie="JSESSIONID=ABCD1234EFGH";</script>'
        '<form id="form" action="" method="get">'
        '<input type="hidden" name="billId" value="PRC_A1"/>'
        '<input type="hidden" name="ageFrom" value="22"/>'
        "</form>"
        "<p>담당자 홍길동 010-1234-5678 hong@example.com 900101-1234567</p>"
        '<pre id="prntSummary">제안이유 및 주요내용 본문</pre>'
        "</body></html>"
    )
    out = _sanitize(dirty)

    for secret in (
        "tok-secret-999", "ABCD1234EFGH", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
        "hong@example.com", "010-1234-5678", "900101-1234567",
    ):
        assert secret not in out, secret

    # 테스트가 검증하는 대상(필드명·selector)은 반드시 보존되어야 한다
    for kept in (
        'name="billId"', 'name="ageFrom"', 'id="form"',
        'id="prntSummary"', "제안이유 및 주요내용 본문",
    ):
        assert kept in out, kept


def test_fixture_presence_is_reported():
    """fixture 유무를 항상 드러낸다 — 미캡처를 '통과'로 오해하지 않도록."""
    missing = [d.name for d in (AVAILABLE_DIR, PENDING_DIR) if not _has(d)]
    if missing:
        pytest.skip(
            f"라이브 미검증: {', '.join(missing)} fixture 가 없습니다. "
            "scripts/capture_assembly_fixture.py --expect <state> 로 캡처해 커밋하세요."
        )
    assert (AVAILABLE_DIR / "detail.html").stat().st_size > 0
    assert (PENDING_DIR / "detail.html").stat().st_size > 0


def test_no_raw_har_is_committed():
    """원본 HAR 은 쿠키·토큰을 그대로 담는다 — 저장소에 들어오면 안 된다."""
    stray = list(FIXTURE_ROOT.rglob("*.raw.har")) + list(
        (Path(__file__).parent / "fixtures").glob("*.raw.har")
    )
    assert not stray, f"원본 HAR 이 남아 있습니다(삭제 필요): {[p.name for p in stray]}"


def test_committed_fixture_files_contain_no_session_values():
    """tests/fixtures/ 의 **모든** 텍스트 파일에 세션값이 없어야 한다."""
    root = Path(__file__).parent / "fixtures"
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "README.md":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for marker in _MUST_NOT_APPEAR:
            assert marker not in text, f"{path} 에 세션값이 남아 있음: {marker}"


# --- AVAILABLE fixture ---


@_SKIP_AVAILABLE
def test_available_fixture_yields_available_status_and_body():
    meta, p, _f = _replay(AVAILABLE_DIR)
    assert meta.get("expect") == "available", meta.get("expect")
    assert p.proposal_status is S.AVAILABLE, p.proposal_note
    assert len(p.body) >= _MIN_SUMMARY_CHARS
    assert p.details == []        # 제안이유는 body 에만 담는다


@_SKIP_AVAILABLE
def test_available_fixture_request_matches_contract():
    meta, _p, f = _replay(AVAILABLE_DIR)
    if meta.get("summary_in_get_html"):
        assert f.requests == [], f"불필요한 후속 요청: {f.requests}"
        return
    _assert_request_matches_contract(meta, f)


@_SKIP_AVAILABLE
def test_available_fixture_meta_has_no_secret_values():
    meta = _meta(AVAILABLE_DIR)
    assert set(meta.get("csrf_meta", {})) <= _ALLOWED_CSRF_META_KEYS
    req = meta.get("follow_up_request")
    if req is not None:
        assert set(req) <= _ALLOWED_REQUEST_KEYS, set(req)


# --- PENDING fixture ---


@_SKIP_PENDING
def test_pending_fixture_yields_pending_status():
    """등록 대기는 실패가 아니다 — PENDING 이어야 하고 본문은 비어 있어야 한다."""
    meta, p, f = _replay(PENDING_DIR)
    assert meta.get("expect") == "pending", meta.get("expect")
    assert p.proposal_status is S.PENDING, p.proposal_note
    assert p.body == ""
    assert len(f.requests) == 1        # 확정 endpoint 에 실제로 물어봤다


@_SKIP_PENDING
def test_pending_fixture_request_matches_contract():
    meta, _p, f = _replay(PENDING_DIR)
    _assert_request_matches_contract(meta, f)


@_SKIP_PENDING
def test_pending_fixture_meta_has_no_secret_values():
    meta = _meta(PENDING_DIR)
    assert set(meta.get("csrf_meta", {})) <= _ALLOWED_CSRF_META_KEYS
    req = meta.get("follow_up_request")
    if req is not None:
        assert set(req) <= _ALLOWED_REQUEST_KEYS, set(req)


# --- 두 fixture 가 서로를 덮어쓰지 않았는지 ---


@_SKIP_AVAILABLE
@_SKIP_PENDING
def test_available_and_pending_fixtures_are_distinct():
    a = (AVAILABLE_DIR / "billinfo.html").read_text(encoding="utf-8")
    p = (PENDING_DIR / "billinfo.html").read_text(encoding="utf-8")
    assert a != p, "available 과 pending fixture 가 같습니다 — 덮어썼는지 확인하세요"
