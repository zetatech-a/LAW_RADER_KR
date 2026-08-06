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

# 종료 코드: 0 = 제안이유 확보(검증 성공), 2 = 응답은 받았으나 제안이유를 못 찾음.
# 2 를 쓰는 이유는 '캡처는 됐지만 파서가 못 뽑았다'를 CI 가 실패로 잡아야 하기
# 때문이다. fixture 와 meta 는 이 경우에도 반드시 저장된다(진단에 필요).
EXIT_OK = 0
EXIT_NO_SUMMARY = 2

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
    token, header, param = _csrf_from_meta(soup)
    # 토큰 '값'은 기록하지 않는다. 있는지 여부와 이름만 남긴다.
    meta["csrf_meta"] = {"has_token": bool(token), "header": header, "parameter": param}
    meta["prnt_summary_selector_hits"] = {
        sel: bool(soup.select_one(sel))
        for sel in ("pre#prntSummary", "#prntSummary", "#summaryContentDiv")
    }

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    follow_html = ""

    post = Post(
        source_key="assembly_bill",
        source_name=src.name,
        post_id=bill_id,
        title="(fixture capture)",
        url=resp.url,
    )

    # GET 에 제안이유가 없으면 실제 스크래퍼와 똑같이 폼을 재전송해 본다.
    # 여기서 예외가 나도 아래 저장은 반드시 수행한다(진단 자료가 가장 필요한 순간이다).
    if not meta["summary_in_get_html"]:
        req = scraper.build_summary_request(soup, post)
        meta["chosen_form_action"] = req.action if req else ""
        if req is None:
            print("  ! 제안이유 요청에 쓸 폼을 찾지 못했습니다.")
        else:
            # 값이 아니라 '형태'만 남긴다(토큰·세션값은 저장소에 들어가면 안 된다).
            meta["follow_up_request"] = req.shape()
            print(f"  {req.method.upper()} {req.action}")
            try:
                text, follow_html = scraper._request_summary(soup, post)
                # 요청을 '보냈는지'가 기준이다. 응답 HTML 이 비어도 보낸 것은 보낸 것.
                meta["follow_up_request_made"] = True
                meta["summary_in_follow_up"] = bool(text)
            except Exception as e:  # noqa: BLE001
                meta["follow_up_request_made"] = True
                meta["follow_up_error"] = f"{type(e).__name__}: {e}"
                print(f"  ! 후속 요청 실패: {type(e).__name__}: {e}")

    # --- 저장은 성공/실패와 무관하게 먼저 한다 ---
    _write(out_dir / "assembly_detail_get.html", _sanitize(get_html))
    if follow_html:
        _write(out_dir / "assembly_detail_post.html", _sanitize(follow_html))
    _write(
        out_dir / "assembly_capture_meta.json",
        json.dumps(meta, ensure_ascii=False, indent=2),
    )

    print("\n--- 확인 결과 ---")
    print(json.dumps(meta, ensure_ascii=False, indent=2))

    if meta["summary_in_get_html"] or meta["summary_in_follow_up"]:
        where = "상세 GET 응답" if meta["summary_in_get_html"] else "후속 요청 응답"
        print(f"\n✅ '제안이유 및 주요내용'을 {where}에서 확보했습니다.")
        print("※ 커밋 전에 fixture 를 눈으로 확인하세요. 자동 치환은 알려진 패턴만 덮습니다.")
        return EXIT_OK

    _print_diagnosis(meta)
    return EXIT_NO_SUMMARY


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
        if meta.get("chosen_form_action") == "":
            print(
                "  · 연관 폼을 고르지 못했습니다(점수 0). 아래 forms 를 보고 "
                "_summary_form 의 판정 조건을 넓히세요."
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
                "tests/fixtures/assembly_detail_post.html 을 열어 확인하세요."
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
