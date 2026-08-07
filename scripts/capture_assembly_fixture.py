"""의안 상세 응답을 받아 '민감값을 지운' 테스트 fixture 로 저장한다.

왜 필요한가
-----------
의안 상세 수집(src/scrapers/assembly.py 의 enrich)은 상세 HTML 에서 폼을 발견해
되돌려 보내는 방식이라, 실제 마크업으로 한 번은 검증해야 한다. 그런데 개발 환경에
따라 likms.assembly.go.kr 접속이 막혀 있을 수 있으므로(사내망/에이전트 프록시 등),
한국 네트워크나 GitHub Actions 러너에서 이 스크립트를 한 번 돌려 fixture 를 만들고
저장소에 커밋하면 이후에는 네트워크 없이 회귀 테스트가 돈다.

사용
----
  python scripts/capture_assembly_fixture.py --expect available --bill-id PRC_XXXX
  python scripts/capture_assembly_fixture.py --expect pending   --bill-id PRC_YYYY
  python scripts/capture_assembly_fixture.py --expect available \
      --url "https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId=PRC_XXXX"

--url 만 줘도 billId 를 URL 에서 뽑아 쓴다(HTTPS·호스트·billId 유일성을 요청 전에 검증).
--expect 로 지정한 상태가 실제 판정과 다르면 아티팩트는 저장하되 종료코드는 실패다.

결과(기대 상태별로 분리 저장 — 서로 덮어쓰지 않는다):
      tests/fixtures/assembly/<expect>/detail.html    (상세 GET 응답)
      tests/fixtures/assembly/<expect>/billinfo.html  (billInfo.do 응답, 보냈을 때만)
      tests/fixtures/assembly/<expect>/meta.json      (method/action/필드'명'만 기록)

요청은 **생산 코드의 빌더**(AssemblyBillScraper.build_summary_request)를 그대로 쓴다.
도구가 조립 규칙을 따로 구현하면 fixture 가 생산 동작을 검증하지 못한다.

저장 전에 세션값·토큰·개인정보를 제거한다(아래 _sanitize 참고). 그래도 커밋 전에
반드시 눈으로 한 번 확인하라 — 자동 치환은 알려진 패턴만 덮는다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup  # noqa: E402

from src.config import SourceConfig  # noqa: E402
from src.fetcher import Fetcher  # noqa: E402
from src.models import Post  # noqa: E402
from src.models import ProposalContentStatus  # noqa: E402
from src.scrapers.assembly import (  # noqa: E402
    _CSRF_META_SELECTOR,
    _MIN_SUMMARY_CHARS,
    _extract_summary,
    _hidden_inputs,
    AssemblyBillScraper,
    SummaryRequestError,
)

# 종료 코드: 0 = 제안이유 확보(검증 성공), 2 = 응답은 받았으나 제안이유를 못 찾음.
# 2 를 쓰는 이유는 '캡처는 됐지만 파서가 못 뽑았다'를 CI 가 실패로 잡아야 하기
# 때문이다. fixture 와 meta 는 이 경우에도 반드시 저장된다(진단에 필요).
EXIT_OK = 0
EXIT_NO_SUMMARY = 2
EXIT_BAD_TARGET = 3

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "assembly"

# 캡처 대상 호스트. 다른 호스트에 세션·토큰을 보내지 않는다.
ALLOWED_HOST = "likms.assembly.go.kr"

# 기대 상태별 fixture 디렉터리. 서로 덮어쓰지 않도록 분리한다.
EXPECT_CHOICES = ("available", "pending")


class CaptureTargetError(ValueError):
    """캡처 대상(billId/URL)이 잘못됨 — 요청을 보내기 전에 실패한다."""


def resolve_capture_target(bill_id: str, url: str, template: str) -> tuple[str, str]:
    """(bill_id, url) 을 확정한다. 잘못되면 CaptureTargetError.

    --url 만 주면 URL 에서 billId 를 뽑는다. 예전에는 이때 bill_id 가 "UNKNOWN" 이
    되어 상세 HTML form#form 의 billId 와 불일치했고, 생산 코드가 요청을 거부해
    캡처가 항상 실패했다.

    검증은 **요청 전에** 끝낸다 — 잘못된 호스트에 세션을 붙여 보내지 않기 위함이다.
    """
    bill_id = (bill_id or "").strip()
    url = (url or "").strip()
    if not bill_id and not url:
        raise CaptureTargetError("--bill-id 또는 --url 중 하나는 필요합니다")

    if not url:
        return bill_id, template.format(bill_id=bill_id)

    parts = urlparse(url)
    if parts.scheme != "https":
        raise CaptureTargetError(f"URL 이 HTTPS 가 아님: {url}")
    if parts.hostname != ALLOWED_HOST:
        raise CaptureTargetError(
            f"URL 호스트가 {ALLOWED_HOST} 가 아님: {parts.hostname!r}"
        )

    values = parse_qs(parts.query, keep_blank_values=True).get("billId", [])
    if not values:
        raise CaptureTargetError(f"URL 에 billId 쿼리가 없음: {url}")
    if len(values) > 1:
        raise CaptureTargetError(f"URL 에 billId 가 여러 개 있음: {values}")
    from_url = values[0].strip()
    if not from_url:
        raise CaptureTargetError("URL 의 billId 가 비어 있음")

    if bill_id and bill_id != from_url:
        raise CaptureTargetError(
            f"--bill-id({bill_id!r}) 와 URL 의 billId({from_url!r}) 가 다릅니다"
        )
    return from_url, url

# 지워야 할 값들.
#   - 세션/토큰: 이름에 아래 조각이 든 hidden input·meta·쿠키 값
#   - 개인정보: 의안 페이지에는 제안자 명단(실명)이 그대로 노출된다. 제안이유 본문
#     자체는 공개 문서라 남기지만, 명단 영역은 굳이 저장소에 담지 않는다.
_SECRET_NAME_HINTS = (
    "csrf", "token", "session", "jsessionid", "wmonid", "auth", "secret", "key", "nonce",
)
_PLACEHOLDER = "REDACTED-FOR-FIXTURE"

# 값 자체로 알아볼 수 있는 식별자(쿠키 문자열·긴 16진 해시 등)
_VALUE_PATTERNS = (
    re.compile(r"JSESSIONID=[^;\"'\s]+", re.I),
    re.compile(r"WMONID=[^;\"'\s]+", re.I),
    re.compile(r"\b[0-9a-f]{32,}\b", re.I),          # 긴 16진 토큰
    re.compile(r"\b\d{6}-[1-4]\d{6}\b"),             # 주민등록번호 형태
    re.compile(r"\b01[016-9]-?\d{3,4}-?\d{4}\b"),    # 휴대전화
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),          # 이메일
)


def _sanitize(html: str) -> str:
    """세션값·토큰·개인정보를 지운다. 구조(태그·필드명)는 그대로 둔다.

    필드'명'은 남겨야 한다 — 테스트가 검증하는 대상이 바로 그 이름들이기 때문이다.
    지우는 것은 값뿐이다.
    """
    soup = BeautifulSoup(html, "lxml")

    for el in soup.find_all(["input", "meta"]):
        name = (el.get("name") or el.get("id") or "").lower()
        attr = "value" if el.name == "input" else "content"
        if not el.get(attr) or not any(h in name for h in _SECRET_NAME_HINTS):
            continue
        # _csrf_header / _csrf_parameter 의 content 는 토큰이 아니라 '이름'이다
        # (예: X-CSRF-TOKEN). 비밀이 아니고, 이걸 지우면 fixture 가 실제 요청 형태를
        # 재현하지 못한다. 토큰 값만 지운다.
        if el.name == "meta" and ("header" in name or "param" in name):
            continue
        el[attr] = _PLACEHOLDER

    # 스크립트/스타일은 통째로 비운다. 토큰이 인라인 JS 에 박혀 있는 경우가 흔하고,
    # 파서는 어차피 보지 않는다.
    for el in soup.find_all(["script", "style"]):
        el.string = ""

    out = str(soup)
    for pat in _VALUE_PATTERNS:
        out = pat.sub(_PLACEHOLDER, out)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bill-id", default="", help="의안 ID(BILL_ID)")
    ap.add_argument("--url", default="", help="상세 URL 직접 지정(--bill-id 대신)")
    ap.add_argument("--out", default=str(FIXTURE_DIR), help="fixture 출력 루트")
    ap.add_argument(
        "--expect",
        choices=EXPECT_CHOICES,
        required=True,
        help="기대 상태. available/pending fixture 를 각각 다른 디렉터리에 저장한다",
    )
    args = ap.parse_args(argv)

    src = SourceConfig(
        key="assembly_bill",
        name="의안정보시스템 · 계류의안",
        type="assembly_bill",
        list_url="https://likms.assembly.go.kr/bill/bi/bill/state/mooringBillPage.do",
        extra={},
    )
    fetcher = Fetcher(timeout=30.0, delay=0.5)
    scraper = AssemblyBillScraper(src, fetcher)

    # 대상 확정과 검증을 **요청 전에** 끝낸다.
    try:
        bill_id, url = resolve_capture_target(
            args.bill_id, args.url, scraper.detail_url
        )
    except CaptureTargetError as e:
        print(f"캡처 대상이 잘못되었습니다: {e}", file=sys.stderr)
        return EXIT_BAD_TARGET

    print(f"기대 상태: {args.expect}")
    print(f"GET {url}")
    resp = fetcher.get(url, referer=src.list_url)
    print(f"  → HTTP {resp.status_code}, 최종 URL {resp.url}")
    if resp.history:
        for h in resp.history:
            print(f"  ↪ redirect {h.status_code} {h.url}")
    get_html = fetcher.text(resp)
    soup = BeautifulSoup(get_html, "lxml")

    meta = {
        "requested_url": url,
        "final_url": resp.url,
        "http_status": resp.status_code,
        "redirects": [{"status": h.status_code, "url": h.url} for h in resp.history],
        "summary_in_get_html": bool(_extract_summary(soup)),
        # 아래 셋은 후속 요청을 실제로 보냈을 때만 갱신된다.
        "follow_up_request_made": False,
        "summary_in_follow_up": False,
        "follow_up_request": None,   # 값 없는 요청 '형태'만 기록한다
        "forms": [],
    }
    for form in soup.find_all("form"):
        meta["forms"].append(
            {
                "id": form.get("id") or "",
                "name": form.get("name") or "",
                "action": form.get("action") or "",
                # 표준 기본값(GET)을 그대로 반영한다 — 스크래퍼와 같은 규칙.
                "method": (form.get("method") or "").strip().lower() or "get(기본값)",
                "hidden_input_names": sorted(_hidden_inputs(form)),
            }
        )
    # 토큰 '값'은 기록하지 않는다. 있는지 여부와 selector 만 남긴다.
    csrf_meta_el = soup.select_one(_CSRF_META_SELECTOR)
    meta["csrf_meta"] = {
        "selector": _CSRF_META_SELECTOR,
        "has_token": bool(csrf_meta_el and (csrf_meta_el.get("content") or "").strip()),
    }
    meta["prnt_summary_selector_hits"] = {
        sel: bool(soup.select_one(sel))
        for sel in ("pre#prntSummary", "#prntSummary", "#summaryContentDiv")
    }

    # 기대 상태별 하위 디렉터리 — available 과 pending fixture 가 서로 덮어쓰지 않는다.
    out_dir = Path(args.out) / args.expect
    out_dir.mkdir(parents=True, exist_ok=True)
    follow_html = ""

    post = Post(
        source_key="assembly_bill",
        source_name=src.name,
        post_id=bill_id,
        title="(fixture capture)",
        url=resp.url,
    )

    # GET 에 제안이유가 없으면 **생산 코드와 똑같은 요청 빌더**로 billInfo.do 를 부른다.
    # 도구가 조립 규칙을 따로 구현하면 fixture 가 생산 동작을 검증하지 못한다.
    # 여기서 예외가 나도 아래 저장은 반드시 수행한다(진단 자료가 가장 필요한 순간이다).
    if not meta["summary_in_get_html"]:
        try:
            req = scraper.build_summary_request(soup, post)
        except SummaryRequestError as e:
            req = None
            meta["request_build_error"] = str(e)
            print(f"  ! 요청을 만들 수 없음: {e}")
        if req is not None:
            # 값이 아니라 '형태'만 남긴다(토큰·세션값은 저장소에 들어가면 안 된다).
            meta["follow_up_request"] = req.shape()
            meta["chosen_form_action"] = req.action
            print(f"  {req.method.upper()} {req.action}")
            try:
                resp2 = fetcher.post(
                    req.action, data=req.data, referer=post.url, headers=req.headers
                )
                follow_html = fetcher.text(resp2)
                # 요청을 '보냈는지'가 기준이다. 응답 HTML 이 비어도 보낸 것은 보낸 것.
                meta["follow_up_request_made"] = True
                meta["follow_up_status"] = resp2.status_code
                meta["summary_in_follow_up"] = bool(
                    _extract_summary(BeautifulSoup(follow_html, "lxml"))
                )
            except Exception as e:  # noqa: BLE001
                meta["follow_up_request_made"] = True
                meta["follow_up_error"] = f"{type(e).__name__}: {e}"
                print(f"  ! 후속 요청 실패: {type(e).__name__}: {e}")

    # --- 저장은 성공/실패와 무관하게 먼저 한다 ---
    detail_fixture = _sanitize(get_html)
    billinfo_fixture = _sanitize(follow_html) if follow_html else ""
    _write(out_dir / "detail.html", detail_fixture)
    if billinfo_fixture:
        _write(out_dir / "billinfo.html", billinfo_fixture)

    # **저장된(정화된) fixture** 로 생산 enrich 를 재생해 상태를 확정한다.
    # 정화 후에도 생산 코드가 같은 판정을 내는지까지 여기서 확인된다.
    status, note, body_len = _replay_status(
        src, bill_id, resp.url, detail_fixture, billinfo_fixture
    )
    meta["expect"] = args.expect
    meta["status"] = status.value
    meta["status_note"] = note
    meta["body_length"] = body_len
    _write(out_dir / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2))

    print("\n--- 확인 결과 ---")
    print(json.dumps(meta, ensure_ascii=False, indent=2))

    if args.expect == "available":
        ok = status is ProposalContentStatus.AVAILABLE and body_len >= _MIN_SUMMARY_CHARS
        want = f"AVAILABLE(본문 {_MIN_SUMMARY_CHARS}자 이상)"
    else:
        ok = status is ProposalContentStatus.PENDING
        want = "PENDING(billInfo.do 응답의 pre#prntSummary 가 비어 있음)"

    if ok:
        print(f"\n✅ 기대 상태({want}) 와 일치합니다. 본문 {body_len}자.")
        print("※ 커밋 전에 fixture 를 눈으로 확인하세요. 자동 치환은 알려진 패턴만 덮습니다.")
        return EXIT_OK

    print(f"\n❌ 기대({want}) 와 다릅니다: status={status.value} 본문 {body_len}자")
    print(f"   note: {note}")
    print("   fixture 와 meta 는 저장했습니다(진단용).")
    _print_diagnosis(meta)
    return EXIT_NO_SUMMARY


class _FixtureFetcher:
    """저장된 fixture 만 돌려주는 fetcher(네트워크 없음). 재생 검증용."""

    def __init__(self, detail_html: str, billinfo_html: str):
        self._detail = detail_html
        self._billinfo = billinfo_html

    def get(self, url, referer=None, params=None, headers=None):
        return ("get", 0)

    def post(self, url, referer=None, data=None, headers=None):
        return ("post", 0)

    def text(self, resp):
        return self._detail if resp[0] == "get" else self._billinfo


def _replay_status(src, bill_id, url, detail_html, billinfo_html):
    """저장된 fixture 로 생산 enrich 를 재생해 (상태, 사유, 본문길이) 를 돌려준다."""
    scraper = AssemblyBillScraper(src, _FixtureFetcher(detail_html, billinfo_html))
    post = Post(
        source_key="assembly_bill",
        source_name=src.name,
        post_id=bill_id,
        title="(fixture replay)",
        url=url,
    )
    scraper.enrich(post)
    return post.proposal_status, post.proposal_note, len(post.body or "")


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    print(f"저장: {path}")


def _print_diagnosis(meta: dict) -> None:
    """제안이유를 못 찾았을 때, 무엇을 고쳐야 하는지 짚어 준다."""
    print("\n❌ '제안이유 및 주요내용'을 찾지 못했습니다. fixture 와 meta 는 저장했습니다.")
    print("\n[진단]")

    hits = meta.get("prnt_summary_selector_hits") or {}
    if not any(hits.values()):
        print(
            "  · 셀렉터 전부 불일치 "
            f"({', '.join(f'{k}={v}' for k, v in hits.items())})\n"
            "    → 저장된 HTML 에서 실제 컨테이너를 찾아 src/scrapers/assembly.py 의 "
            "_SUMMARY_SELECTORS 를 고치세요."
        )
    else:
        matched = [k for k, v in hits.items() if v]
        print(
            f"  · 셀렉터는 맞았으나({', '.join(matched)}) 내용이 비었거나 너무 짧습니다.\n"
            "    → 값이 별도 요청으로 채워지는 구조일 수 있습니다. 아래 폼 목록 확인."
        )

    if not meta.get("follow_up_request_made"):
        if meta.get("request_build_error"):
            print(f"  · 요청을 만들지 못했습니다: {meta['request_build_error']}")
            print(
                "    → form#form / meta[name=\"_csrf\"] / billId 를 확인하세요."
            )
        print("  · 후속 요청은 보내지 않았습니다.")
    else:
        req = meta.get("follow_up_request") or {}
        print(
            f"  · 후속 요청은 보냈습니다: {req.get('method', '?').upper()} "
            f"{req.get('action', '?')} (필드 {req.get('data_keys')})"
        )
        if meta.get("follow_up_error"):
            print(f"    → 요청 자체가 실패: {meta['follow_up_error']}")
        else:
            print(
                "    → 응답에 제안이유가 없습니다. "
                "저장된 billinfo.html 을 열어 확인하세요."
            )

    if meta.get("redirects"):
        print(f"  · redirect 발생: {meta['redirects']} → 최종 {meta['final_url']}")
        print("    → 상세 경로가 바뀌었다면 config.yaml 의 detail_url 을 고치세요.")

    print("\n[페이지의 폼 목록]")
    for f in meta.get("forms") or []:
        print(
            f"  - id={f['id']!r} name={f['name']!r} method={f['method']} "
            f"action={f['action']!r} hidden={f['hidden_input_names']}"
        )


if __name__ == "__main__":
    sys.exit(main())
