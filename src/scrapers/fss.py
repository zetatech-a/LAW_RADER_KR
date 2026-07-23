"""금융감독원 (fss.or.kr) eGovFrame 게시판 스크래퍼.

대상 5종 (list.do?menuNo=...):
- 보도자료, 행정지도 예고, 세칙 제·개정 예고, 검사결과 제재, 경영유의사항 공시

eGovFrame 게시판은 목록 앵커가 onclick JS(fn_view('nttId') 등)로 상세를 연다.
상세 URL 은 대개 같은 경로의 list.do → view.do 로 치환하고 nttId·menuNo 를 붙인다.
onclick/함수명이 사이트마다 조금씩 달라 nttId 추출은 여러 패턴을 시도한다.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse, parse_qs, urlencode

from bs4 import BeautifulSoup

from ..models import Attachment, Post
from .base import BaseScraper, clean_text

log = logging.getLogger(__name__)

_NUM = re.compile(r"\d{2,}")


class FssBoardScraper(BaseScraper):
    def __init__(self, source, fetcher):
        super().__init__(source, fetcher)
        parsed = urlparse(self.list_url)
        self._menu_no = parse_qs(parsed.query).get("menuNo", [""])[0]
        # list.do → view.do
        self._view_url = self.list_url.split("?")[0].replace("list.do", "view.do")

    def fetch_list(self, limit: int) -> list[Post]:
        resp = self.fetcher.get(self.list_url)
        html = self.fetcher.text(resp)
        soup = BeautifulSoup(html, "lxml")
        posts = self._parse_list(soup)
        if not posts:
            log.warning("[%s] 목록 파싱 0건 — 디버그 덤프.", self.key)
            self._dump_debug("list", html)
        return posts[:limit]

    def _parse_list(self, soup: BeautifulSoup) -> list[Post]:
        posts: list[Post] = []
        seen: set[str] = set()

        # eGov 게시판은 대개 table.board-list / .tbl 계열
        rows = soup.select("table tbody tr") or soup.select("ul.board-list li")
        for row in rows:
            a = row.find("a")
            if not a:
                continue
            ntt_id = self._extract_ntt_id(a)
            if not ntt_id or ntt_id in seen:
                continue
            title = clean_text(a.get("title") or a.get_text())
            if not title:
                continue
            date = self._row_date(row)
            seen.add(ntt_id)
            url = self._build_view_url(ntt_id)
            posts.append(
                Post(
                    source_key=self.key,
                    source_name=self.name,
                    post_id=ntt_id,
                    title=title,
                    url=url,
                    date=date,
                )
            )
        return posts

    def _extract_ntt_id(self, anchor) -> str:
        # 1) href 쿼리에 nttId
        href = anchor.get("href", "")
        if href and "nttId" in href:
            qs = parse_qs(urlparse(urljoin(self.list_url, href)).query)
            if qs.get("nttId"):
                return qs["nttId"][0]
        # 2) onclick JS 인자에서 숫자 추출 (가장 긴 숫자 = nttId 로 가정)
        onclick = anchor.get("onclick", "") or href
        nums = _NUM.findall(onclick)
        if nums:
            return max(nums, key=len)
        # 3) data-* 속성
        for k, v in anchor.attrs.items():
            if "ntt" in k.lower() and str(v).isdigit():
                return str(v)
        return ""

    def _build_view_url(self, ntt_id: str) -> str:
        params = {"nttId": ntt_id}
        if self._menu_no:
            params["menuNo"] = self._menu_no
        return f"{self._view_url}?{urlencode(params)}"

    @staticmethod
    def _row_date(row) -> str:
        text = row.get_text(" ", strip=True)
        m = re.search(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        return ""

    def enrich(self, post: Post) -> None:
        try:
            resp = self.fetcher.get(post.url, referer=self.list_url)
            html = self.fetcher.text(resp)
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] 상세 로드 실패 %s: %s", self.key, post.url, e)
            return
        soup = BeautifulSoup(html, "lxml")

        body_el = (
            soup.select_one(".view-cont")
            or soup.select_one(".board-view")
            or soup.select_one(".bbs-view")
            or soup.select_one("#content")
        )
        if body_el:
            post.body = clean_text(body_el.get_text("\n"))

        # 첨부: eGov 는 보통 /fss/... 다운로드 링크 또는 fn_download onclick
        for a in soup.select("a[href], a[onclick]"):
            href = a.get("href", "")
            onclick = a.get("onclick", "")
            blob = f"{href} {onclick}".lower()
            if any(k in blob for k in ("download", "filedown", "atchfile", "/file")):
                if href and not href.lower().startswith("javascript"):
                    url = urljoin(post.url, href)
                else:
                    # onclick 기반 다운로드는 URL 을 복원하기 어려워 링크만 남김
                    url = post.url
                fname = clean_text(a.get("title") or a.get_text()) or "첨부파일"
                post.attachments.append(Attachment(filename=fname, url=url))

        for a in post.attachments:
            if a.url == post.url:
                continue  # 복원 불가한 onclick 첨부는 다운로드 생략(링크만)
            try:
                a.data = self.fetcher.download(a.url, referer=post.url)
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] 첨부 다운로드 실패 %s: %s", self.key, a.url, e)
