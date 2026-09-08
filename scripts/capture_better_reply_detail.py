"""회신사례(better.fsc.go.kr) 상세 페이지 DOM 진단 캡처 — **진단 전용** 스크립트.

운영 코드가 아니다. 파서를 고치기 전에 '실제 상세 페이지에서 정식 사건 제목이 어떤
element 에 있는가' 를 눈으로 확인하기 위한 도구다(추측한 selector 를 넣지 않기 위함).

하는 일:
  1. 생산 코드와 **같은 방식**으로 상세 URL 을 만든다(BetterReplyScraper._detail_url).
  2. 생산 Fetcher 로 GET 한다(목록 URL 을 Referer 로).
  3. 응답 HTML 원본만 debug/ 에 저장한다(헤더·쿠키는 저장하지 않는다).
  4. 파서 판단에 필요한 구조만 stdout 으로 출력한다:
     - heading(h1~h6) 목록과 조상 경로
     - tr 의 th/td 짝, dl 의 dt/dd 짝
     - --expect-title 로 준 문자열과 **정확히** 같은 텍스트를 가진 element 전부
       (= 정식 사건 제목의 structural source)
     - 첨부(/file/displayFile.do) 앵커
     - 본문 outline(자기 텍스트가 있는 element 만)

사용:
  python scripts/capture_better_reply_detail.py --idx 5449 --expect-title "…"
  python scripts/capture_better_reply_detail.py --idx 5450 --gubun 법령해석
"""
import argparse
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup  # noqa: E402

from src.config import SourceConfig  # noqa: E402
from src.fetcher import Fetcher  # noqa: E402
from src.scrapers.better_fsc import BetterReplyScraper  # noqa: E402

LIST_URL = (
    "https://better.fsc.go.kr/fsc_new/replyCase/TotalReplyList.do"
    "?stNo=11&muNo=117&muGpNo=75"
)
_WS = re.compile(r"\s+")
_SKIP = ("script", "style", "noscript")


def _norm(s: str) -> str:
    return _WS.sub(" ", s or "").strip()


def _path(el) -> str:
    """조상 경로를 tag#id.class 로 표기(구조 파악용)."""
    parts = []
    for p in list(el.parents)[:6]:
        if not getattr(p, "name", None) or p.name in ("[document]", "html"):
            continue
        parts.append(_desc(p))
    return " < ".join(parts)


def _desc(el) -> str:
    bits = el.name
    if el.get("id"):
        bits += "#" + el["id"]
    cls = el.get("class") or []
    if cls:
        bits += "." + ".".join(cls)
    return bits


def _own_text(el) -> str:
    """자식 element 를 제외한, 이 element 가 직접 가진 텍스트."""
    return _norm("".join(str(c) for c in el.children if getattr(c, "name", None) is None))


def _scraper() -> BetterReplyScraper:
    src = SourceConfig(
        key="better_reply",
        name="금융규제포털 · 법령해석·비조치의견서 회신사례",
        type="better_reply",
        list_url=LIST_URL,
    )
    return BetterReplyScraper(src, Fetcher(timeout=30.0, delay=0.5))


def capture(idx: str, gubun: str, expect_title: str, outline_limit: int) -> int:
    sc = _scraper()
    url = sc._detail_url({"pastreqType": gubun, "dataIdx": idx})
    if not url:
        print(f"❌ 상세 URL 을 만들 수 없습니다(구분={gubun!r}).")
        return 2

    print("=" * 72)
    print(f"idx={idx}  구분={gubun}")
    print(f"URL: {url}")
    print("=" * 72)

    try:
        resp = sc.fetcher.get(url, referer=LIST_URL)
        html = sc.fetcher.text(resp)
    except Exception as e:  # noqa: BLE001
        print(f"❌ HTTP 실패: {type(e).__name__}: {e}")
        return 2

    print(f"HTTP {resp.status_code} / {len(html)} chars / encoding={resp.encoding}")

    d = Path("debug")
    d.mkdir(exist_ok=True)
    raw = d / f"better_reply_detail_{idx}.html"
    raw.write_text(html, encoding="utf-8")          # 응답 body 만 저장(헤더·쿠키 제외)
    print(f"raw HTML 저장: {raw}")

    soup = BeautifulSoup(html, "lxml")
    for t in soup.find_all(list(_SKIP)):
        t.decompose()

    print("\n--- <title> ---")
    print(repr(_norm(soup.title.get_text() if soup.title else "")))

    print("\n--- headings (h1~h6) ---")
    for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        print(f"  {_desc(h):<40} text={_norm(h.get_text(' '))[:90]!r}")
        print(f"      ancestors: {_path(h)}")

    print("\n--- tr 의 th/td 짝 ---")
    for row in soup.find_all("tr"):
        cells = [c for c in row.find_all(["th", "td"]) if c.find_parent("tr") is row]
        heads = [c for c in cells if c.name == "th"]
        vals = [c for c in cells if c.name == "td"]
        for th, td in zip(heads, vals):
            print(f"  th={_norm(th.get_text(' '))[:30]!r:<34} td={_norm(td.get_text(' '))[:90]!r}")
            print(f"      row ancestors: {_path(row)}")

    print("\n--- dt/dd 짝 ---")
    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling()
        if dd is not None and dd.name == "dd":
            print(f"  dt={_norm(dt.get_text(' '))[:30]!r:<34} dd={_norm(dd.get_text(' '))[:90]!r}")
            print(f"      dt ancestors: {_path(dt)}")

    if expect_title:
        want = _norm(expect_title)
        print(f"\n--- 정식 제목 {want[:60]!r} 과 텍스트가 정확히 같은 element ---")
        hits = 0
        for el in soup.find_all(True):
            if el.name in _SKIP:
                continue
            if _norm(el.get_text(" ")) == want:
                hits += 1
                print(f"  {_desc(el):<40} (own_text={_own_text(el)[:40]!r})")
                print(f"      ancestors: {_path(el)}")
        print(f"  → 일치 element {hits}개")

        print(f"\n--- 제목 문자열을 '포함' 하는 최말단 element ---")
        for el in soup.find_all(True):
            if el.name in _SKIP:
                continue
            if want in _norm(el.get_text(" ")) and not any(
                want in _norm(c.get_text(" ")) for c in el.find_all(True)
            ):
                print(f"  {_desc(el):<40} text={_norm(el.get_text(' '))[:120]!r}")
                print(f"      ancestors: {_path(el)}")

    print("\n--- 첨부(/file/displayFile.do) 앵커 ---")
    for a in soup.find_all("a", href=True):
        if "displayfile.do" in urlparse(a["href"]).path.lower():
            print(f"  text={_norm(a.get_text(' '))[:60]!r}  href={a['href'][:160]}")
            print(f"      ancestors: {_path(a)}")

    print(f"\n--- outline(자기 텍스트가 있는 element, 최대 {outline_limit}줄) ---")
    body = soup.body or soup
    n = 0
    for el in body.find_all(True):
        if el.name in _SKIP:
            continue
        own = _own_text(el)
        if not own:
            continue
        depth = sum(1 for _ in el.parents)
        print(f"  {'  ' * min(depth, 12)}{_desc(el)}: {own[:110]!r}")
        n += 1
        if n >= outline_limit:
            print(f"  … (outline {outline_limit}줄에서 절단)")
            break
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--idx", action="append", required=True, help="lawreqIdx/opinionIdx")
    ap.add_argument("--gubun", default="법령해석", choices=["법령해석", "비조치의견서"])
    ap.add_argument("--expect-title", action="append", default=[])
    ap.add_argument("--outline-limit", type=int, default=400)
    args = ap.parse_args(argv)

    rc = 0
    for i, idx in enumerate(args.idx):
        expect = args.expect_title[i] if i < len(args.expect_title) else ""
        rc |= capture(idx.strip(), args.gubun, expect.strip(), args.outline_limit)
        print()
    return rc


if __name__ == "__main__":
    sys.exit(main())
