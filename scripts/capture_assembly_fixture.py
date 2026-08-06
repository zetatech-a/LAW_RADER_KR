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
  python scripts/capture_assembly_fixture.py --bill-id PRC_XXXXXXXXXXXX
  python scripts/capture_assembly_fixture.py --url "https://likms.assembly.go.kr/..."

결과: tests/fixtures/assembly_detail_get.html
      tests/fixtures/assembly_detail_post.html   (별도 요청이 있었던 경우에만)
      tests/fixtures/assembly_capture_meta.json  (요청 URL·method·필드명 기록)

저장 전에 세션값·토큰·개인정보를 제거한다(아래 _sanitize 참고). 그래도 커밋 전에
반드시 눈으로 한 번 확인하라 — 자동 치환은 알려진 패턴만 덮는다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup  # noqa: E402

from src.config import SourceConfig  # noqa: E402
from src.fetcher import Fetcher  # noqa: E402
from src.models import Post  # noqa: E402
from src.scrapers.assembly import (  # noqa: E402
    _csrf_from_meta,
    _extract_summary,
    _hidden_inputs,
    AssemblyBillScraper,
)

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

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
    ap.add_argument("--out", default=str(FIXTURE_DIR), help="fixture 출력 디렉터리")
    args = ap.parse_args(argv)

    if not args.bill_id and not args.url:
        ap.error("--bill-id 또는 --url 중 하나는 필요합니다")

    src = SourceConfig(
        key="assembly_bill",
        name="의안정보시스템 · 계류의안",
        type="assembly_bill",
        list_url="https://likms.assembly.go.kr/bill/bi/bill/state/mooringBillPage.do",
        extra={},
    )
    fetcher = Fetcher(timeout=30.0, delay=0.5)
    scraper = AssemblyBillScraper(src, fetcher)

    bill_id = args.bill_id or "UNKNOWN"
    url = args.url or scraper.detail_url.format(bill_id=bill_id)

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
        "redirects": [{"status": h.status_code, "url": h.url} for h in resp.history],
        "summary_in_get_html": bool(_extract_summary(soup)),
        "forms": [],
    }
    for form in soup.find_all("form"):
        meta["forms"].append(
            {
                "id": form.get("id") or "",
                "name": form.get("name") or "",
                "action": form.get("action") or "",
                "method": (form.get("method") or "get").lower(),
                "hidden_input_names": sorted(_hidden_inputs(form)),
            }
        )
    token, header, param = _csrf_from_meta(soup)
    meta["csrf_meta"] = {"has_token": bool(token), "header": header, "parameter": param}
    meta["prnt_summary_selector_hits"] = {
        sel: bool(soup.select_one(sel))
        for sel in ("pre#prntSummary", "#prntSummary", "#summaryContentDiv")
    }

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "assembly_detail_get.html").write_text(
        _sanitize(get_html), encoding="utf-8"
    )
    print(f"저장: {out_dir / 'assembly_detail_get.html'}")

    # GET 에 제안이유가 없으면 실제 스크래퍼와 똑같이 폼을 재전송해 본다.
    if not meta["summary_in_get_html"]:
        post = Post(
            source_key="assembly_bill",
            source_name=src.name,
            post_id=bill_id,
            title="(fixture capture)",
            url=resp.url,
        )
        text, follow_html = scraper._request_summary(soup, post)
        meta["follow_up_request_made"] = bool(follow_html)
        meta["summary_in_follow_up"] = bool(text)
        found = scraper._summary_form(soup, post)
        meta["chosen_form_action"] = found[1] if found else ""
        if follow_html:
            (out_dir / "assembly_detail_post.html").write_text(
                _sanitize(follow_html), encoding="utf-8"
            )
            print(f"저장: {out_dir / 'assembly_detail_post.html'}")

    (out_dir / "assembly_capture_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"저장: {out_dir / 'assembly_capture_meta.json'}")
    print("\n--- 확인 결과 ---")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    print(
        "\n※ 커밋 전에 fixture 를 눈으로 확인하세요. 자동 치환은 알려진 패턴만 덮습니다."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
