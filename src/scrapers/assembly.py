"""의안정보시스템 (likms.assembly.go.kr) 계류의안 스크래퍼.

목록: bill/bi/bill/state/mooringBillPage.do

주의: 이 페이지의 의안 목록은 대개 AJAX(POST) 로 로드되어 순수 GET HTML 만으로는
행이 안 잡힐 수 있다. 아래는 GET HTML 파싱을 시도하되, 실패하면 디버그 덤프를 남긴다.
그 경우 다음 중 하나로 조정하는 것을 권장한다:
  (A) 국회 의안정보 Open API (open.assembly.go.kr) 사용 — 가장 안정적. API 키 필요.
  (B) mooring 목록 POST 엔드포인트를 브라우저 개발자도구로 확인해 _fetch_via_post 에 반영.
  (C) Playwright 로 렌더 후 파싱.
의안 상세는 billDetail.do?billId=... 이며 billId 가 고유 ID.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup

from ..models import Post
from .base import BaseScraper, clean_text

log = logging.getLogger(__name__)

_DETAIL_BASE = "https://likms.assembly.go.kr/bill/billDetail.do"


class AssemblyBillScraper(BaseScraper):
    def fetch_list(self, limit: int) -> list[Post]:
        resp = self.fetcher.get(self.list_url)
        html = self.fetcher.text(resp)
        soup = BeautifulSoup(html, "lxml")
        posts = self._parse_list(soup)
        if not posts:
            log.warning(
                "[%s] 목록 파싱 0건 — 계류의안은 AJAX 로드일 가능성이 높습니다. "
                "디버그 덤프를 확인하고 Open API 또는 POST 엔드포인트로 조정하세요.",
                self.key,
            )
            self._dump_debug("list", html)
        return posts[:limit]

    def _parse_list(self, soup: BeautifulSoup) -> list[Post]:
        posts: list[Post] = []
        seen: set[str] = set()

        # billId 를 담은 링크(href 또는 onclick)를 찾는다.
        for a in soup.select("a[href], a[onclick]"):
            bill_id = self._extract_bill_id(a)
            if not bill_id or bill_id in seen:
                continue
            title = clean_text(a.get("title") or a.get_text())
            if not title or len(title) < 2:
                continue
            seen.add(bill_id)
            posts.append(
                Post(
                    source_key=self.key,
                    source_name=self.name,
                    post_id=bill_id,
                    title=title,
                    url=f"{_DETAIL_BASE}?billId={bill_id}",
                    date=self._row_date(a),
                )
            )
        return posts

    @staticmethod
    def _extract_bill_id(anchor) -> str:
        href = anchor.get("href", "")
        onclick = anchor.get("onclick", "")
        blob = f"{href} {onclick}"
        # billId 는 보통 PRC_ 로 시작하는 문자열 또는 긴 영숫자
        if "billId" in href:
            qs = parse_qs(urlparse(href).query)
            if qs.get("billId"):
                return qs["billId"][0]
        m = re.search(r"billId['\"=,\s]+['\"]?([A-Za-z0-9_]{6,})", blob)
        if m:
            return m.group(1)
        m = re.search(r"(PRC_[A-Za-z0-9]+)", blob)
        if m:
            return m.group(1)
        return ""

    @staticmethod
    def _row_date(anchor) -> str:
        row = anchor.find_parent(["tr", "li", "div"])
        text = row.get_text(" ", strip=True) if row else ""
        m = re.search(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        return ""

    def enrich(self, post: Post) -> None:
        # 계류의안은 상세 본문보다 제목·의안번호·링크가 핵심이라 본문 수집은 생략.
        # 필요 시 billDetail.do 를 파싱해 제안이유 등을 채우도록 확장 가능.
        return
