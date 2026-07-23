"""금융규제·법령해석포털 (better.fsc.go.kr) 회신사례 스크래퍼.

목록: replyCase/TotalReplyList.do?stNo=11&muNo=117&muGpNo=75

이 포털은 목록을 AJAX(POST) 로 불러오는 경우가 있다. 우선 GET 으로 서버렌더
HTML 을 시도하고, 행이 안 잡히면 디버그 덤프를 남긴다(→ 필요 시 POST 엔드포인트로 조정).
상세는 대개 TotalReplyView.do 계열이며 회신 일련번호(replyRegSn 등)가 고유 ID.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup

from ..models import Attachment, Post
from .base import BaseScraper, clean_text

log = logging.getLogger(__name__)

_NUM = re.compile(r"\d{3,}")
_ID_KEYS = ("replyRegSn", "replySn", "sn", "seq", "idx", "no", "muNo")


class BetterReplyScraper(BaseScraper):
    def __init__(self, source, fetcher):
        super().__init__(source, fetcher)
        self._view_url = self.list_url.split("?")[0].replace(
            "TotalReplyList.do", "TotalReplyView.do"
        )

    def fetch_list(self, limit: int) -> list[Post]:
        resp = self.fetcher.get(self.list_url)
        html = self.fetcher.text(resp)
        soup = BeautifulSoup(html, "lxml")
        posts = self._parse_list(soup)
        if not posts:
            log.warning(
                "[%s] 목록 파싱 0건 — AJAX 로드일 수 있음. 디버그 덤프 후 POST 엔드포인트 확인 필요.",
                self.key,
            )
            self._dump_debug("list", html)
        return posts[:limit]

    def _parse_list(self, soup: BeautifulSoup) -> list[Post]:
        posts: list[Post] = []
        seen: set[str] = set()
        rows = (
            soup.select("table tbody tr")
            or soup.select("ul.list li")
            or soup.select(".board-list li")
        )
        for row in rows:
            a = row.find("a")
            if not a:
                continue
            pid = self._extract_id(a)
            if not pid or pid in seen:
                continue
            title = clean_text(a.get("title") or a.get_text())
            if not title:
                continue
            seen.add(pid)
            posts.append(
                Post(
                    source_key=self.key,
                    source_name=self.name,
                    post_id=pid,
                    title=title,
                    url=self._build_view_url(a, pid),
                    date=self._row_date(row),
                )
            )
        return posts

    def _extract_id(self, anchor) -> str:
        href = anchor.get("href", "")
        onclick = anchor.get("onclick", "")
        # href 쿼리 우선
        if href and not href.lower().startswith("javascript"):
            qs = parse_qs(urlparse(urljoin(self.list_url, href)).query)
            for k in _ID_KEYS:
                if qs.get(k):
                    return f"{k}:{qs[k][0]}"
        # onclick 숫자 인자
        nums = _NUM.findall(onclick or href)
        if nums:
            return f"js:{max(nums, key=len)}"
        return ""

    def _build_view_url(self, anchor, pid: str) -> str:
        href = anchor.get("href", "")
        if href and not href.lower().startswith("javascript"):
            return urljoin(self.list_url, href)
        # onclick 기반이면 상세 URL 을 정확히 복원하기 어려우므로 목록 URL 을 링크로.
        return self.list_url

    @staticmethod
    def _row_date(row) -> str:
        text = row.get_text(" ", strip=True)
        m = re.search(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        return ""

    def enrich(self, post: Post) -> None:
        if post.url == self.list_url:
            return  # 상세 URL 복원 불가 — 제목/링크만 통지
        try:
            resp = self.fetcher.get(post.url, referer=self.list_url)
            html = self.fetcher.text(resp)
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] 상세 로드 실패 %s: %s", self.key, post.url, e)
            return
        soup = BeautifulSoup(html, "lxml")
        body_el = soup.select_one(".view-cont") or soup.select_one("#content")
        if body_el:
            post.body = clean_text(body_el.get_text("\n"))
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if any(k in href.lower() for k in ("download", "filedown", "atchfile", "/file")):
                url = urljoin(post.url, href)
                fname = clean_text(a.get_text()) or url.rsplit("/", 1)[-1]
                post.attachments.append(Attachment(filename=fname, url=url))
        for a in post.attachments:
            try:
                a.data = self.fetcher.download(a.url, referer=post.url)
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] 첨부 다운로드 실패 %s: %s", self.key, a.url, e)
