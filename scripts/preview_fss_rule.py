"""금감원 세칙 제·개정 예고·행정지도 예고 — 게시물 1건 단건 점검·메일 미리보기.

이미 seen 으로 처리된 게시물(예: lrgSlno=4405, seqno=280)도 목록의 신규 판정을 거치지 않고
상세 1건만 수집해, 원문 4개 항목 추출 결과와 메일 HTML/텍스트 미리보기를 파일로 만든다.
소스는 --source 로 고른다. 지정하지 않으면 --url 의 게시판 경로(admnPrvntc = 행정지도 예고)로
정하고, 그 밖에는 세칙 예고다.

이 스크립트는
  - 메일을 보내지 않는다(SMTP 에 연결하지 않는다),
  - state/seen.json 을 읽지도 쓰지도 않는다,
  - Gemini 를 호출하지 않는다(요약 단계를 부르지 않는다 — 이 소스는 원래 AI 요약 대상이 아니다),
  - 첨부는 기본적으로 내려받지 않는다(미리보기에는 링크 칩으로 표시, --download 로 변경).

사용:
  # 라이브(인터넷 접속이 되는 환경) — 원본 HTML 을 fixture 로 저장
  python scripts/preview_fss_rule.py \\
    --url "https://www.fss.or.kr/fss/job/lrgRegItnPrvntc/view.do?lrgSlno=4405&menuNo=200489&pageIndex=1" \\
    --save-html tests/fixtures/fss_rule/live/lrgSlno_4405.html

  # 오프라인 — 저장해 둔 HTML 로
  python scripts/preview_fss_rule.py --html-file tests/fixtures/fss_rule/synthetic_inline.html

  # 행정지도 예고(오프라인)
  python scripts/preview_fss_rule.py --source fss_admin_guidance \
    --html-file tests/fixtures/fss_admin_guidance/live/seqno_280.html

결과: --out-dir(기본 debug/preview) 아래 fss_rule_<게시물ID>.html / .txt
      (행정지도 예고는 fss_admin_guidance_<게시물ID>.html / .txt)
종료 코드: 0 = 4개 항목 모두 추출 / 2 = 일부 누락 / 3 = 전체 실패 / 1 = 입력·접속 오류
"""
import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup

from src.config import SourceConfig, load_config
from src.fetcher import Fetcher
from src.models import (
    FSS_ADMIN_GUIDANCE_SOURCE_KEY,
    FSS_RULE_SOURCE_KEY,
    FSS_SECTION_TITLES_BY_SOURCE,
    Post,
)
from src.notifier import build_html, build_text
from src.scrapers.fss import FssBoardScraper

# 소스 → (기본 상세 URL, 결과 파일 접두어, config 가 없을 때의 이름·목록 URL)
_BOARDS = {
    FSS_RULE_SOURCE_KEY: (
        "https://www.fss.or.kr/fss/job/lrgRegItnPrvntc/view.do?lrgSlno=4405&menuNo=200489&pageIndex=1",
        "fss_rule",
        "금감원 · 세칙 제·개정 예고",
        "https://www.fss.or.kr/fss/job/lrgRegItnPrvntc/list.do?menuNo=200489",
    ),
    FSS_ADMIN_GUIDANCE_SOURCE_KEY: (
        "https://www.fss.or.kr/fss/job/admnPrvntc/view.do?seqno=280&menuNo=200491&pageIndex=1",
        "fss_admin_guidance",
        "금감원 · 행정지도 예고",
        "https://www.fss.or.kr/fss/job/admnPrvntc/list.do?menuNo=200491",
    ),
}


class _PageFetcher:
    """상세 HTML 은 이미 받은 것을 돌려주고, 첨부는 설정에 따라 내려받거나 건너뛴다."""

    def __init__(self, html: str, live: Fetcher | None, download: bool):
        self._html = html
        self._live = live
        self._download = download

    def get(self, url, referer=None, **kwargs):
        return None

    def text(self, resp):
        return self._html

    def download(self, url, referer=None):
        if self._download and self._live is not None:
            return self._live.download(url, referer=referer)
        return None  # 링크만(미리보기 메일에 '· 링크' 칩으로 표시)


def _source_key(source: str | None, url: str | None) -> str:
    if source:
        return source
    if url and "/admnPrvntc/" in url:
        return FSS_ADMIN_GUIDANCE_SOURCE_KEY
    return FSS_RULE_SOURCE_KEY


def _rule_source(config_path: str, key: str) -> SourceConfig:
    try:
        for src in load_config(config_path).sources:
            if src.key == key:
                return src
    except FileNotFoundError:
        pass
    _, _, name, list_url = _BOARDS[key]
    return SourceConfig(key=key, name=name, type="fss_board", list_url=list_url)


def _page_title(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for sel in (".subject", "h3", "title"):
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            return " ".join(el.get_text(" ").split())
    return "(제목 미상)"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="세칙 제·개정 예고·행정지도 예고 단건 점검·메일 미리보기")
    ap.add_argument("--source", choices=sorted(_BOARDS),
                    help="대상 소스(기본: --url 의 게시판 경로로 판단, 그 밖에는 세칙 예고)")
    ap.add_argument("--url", help="상세 페이지 URL(게시물 ID 를 여기서 읽음, 기본: 소스별 표본)")
    ap.add_argument("--html-file", help="저장해 둔 상세 HTML(지정하면 네트워크를 쓰지 않음)")
    ap.add_argument("--save-html", help="라이브로 받은 원본 HTML 을 이 경로에 저장")
    ap.add_argument("--title", help="메일 카드 제목(기본: 페이지의 제목 요소)")
    ap.add_argument("--date", default="", help="메일 카드 게시일(YYYY-MM-DD)")
    ap.add_argument("--out-dir", default="debug/preview")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--download", action="store_true", help="첨부를 실제로 내려받는다(라이브)")
    args = ap.parse_args(argv)
    key = _source_key(args.source, args.url)
    default_url, stem_prefix, _, _ = _BOARDS[key]
    args.url = args.url or default_url

    live = None
    if args.html_file:
        html = Path(args.html_file).read_text(encoding="utf-8")
    else:
        live = Fetcher(timeout=30, delay=0.5)
        try:
            html = live.text(live.get(args.url))
        except Exception as e:  # noqa: BLE001
            print(f"상세 페이지를 받지 못했습니다: {type(e).__name__}: {e}")
            return 1
        if args.save_html:
            Path(args.save_html).parent.mkdir(parents=True, exist_ok=True)
            Path(args.save_html).write_text(html, encoding="utf-8")
            print(f"원본 HTML 저장: {args.save_html}")

    src = _rule_source(args.config, key)
    scraper = FssBoardScraper(src, fetcher=_PageFetcher(html, live, args.download))
    post = Post(
        source_key=src.key,
        source_name=src.name,
        post_id=scraper._post_id(args.url),
        title=args.title or _page_title(html),
        url=args.url,
        date=args.date,
    )
    scraper.enrich(post)

    titles = FSS_SECTION_TITLES_BY_SOURCE[key]
    found = dict(post.details)
    print(f"\n[{post.post_id}] {post.title}")
    for title in titles:
        value = found.get(title)
        print(f"\n• {title}" + ("" if value else "  — 추출 실패(누락)"))
        for line in (value or "").split("\n"):
            print(f"    {line}" if line else "")
    print(f"\n첨부 {len(post.attachments)}개: " + ", ".join(a.filename for a in post.attachments))

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{stem_prefix}_" + re.sub(r"[^\w.-]", "_", post.post_id)
    grouped = {post.source_name: [post]}
    page = (
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>메일 미리보기 · {post.post_id}</title></head>"
        f"<body style='margin:0;background:#f1f5f9'>{build_html(grouped)}</body></html>"
    )
    (out_dir / f"{stem}.html").write_text(page, encoding="utf-8")
    (out_dir / f"{stem}.txt").write_text(build_text(grouped), encoding="utf-8")
    print(f"\n미리보기: {out_dir / (stem + '.html')}  /  {out_dir / (stem + '.txt')}")

    if not post.details:
        print("결과: 전체 실패 — 메일에는 '상세 내용 추출 실패' 안내가 실립니다.")
        return 3
    if len(found) < len(titles):
        print("결과: 일부 누락 — 누락 항목은 메일에 안내가 실립니다.")
        return 2
    print("결과: 4개 항목 모두 추출")
    return 0


if __name__ == "__main__":
    sys.exit(main())
