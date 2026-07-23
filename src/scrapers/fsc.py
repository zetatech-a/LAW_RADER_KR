"""금융위원회 (fsc.go.kr) 게시판 스크래퍼.

대상 예:
- 보도자료:            https://www.fsc.go.kr/no010101
- 입법예고/규정변경예고: https://www.fsc.go.kr/po040301

※ FSC 신 사이트는 게시판마다 메뉴 경로(no010101, po040301 …)가 다르지만
  목록/상세 렌더링 구조는 동일 계열이다. 아래 파서는 그 공통 구조를 가정한다.
  실제 마크업과 다르면 _parse_list 의 셀렉터만 조정하면 된다(--debug 로 원본 확인).
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup

from ..fetcher import AttachmentTooLarge
from ..models import Attachment, Post
from .base import BaseScraper, clean_text

log = logging.getLogger(__name__)

_ID_IN_PATH = re.compile(r"/(\d{3,})(?:[/?#]|$)")


class FscBoardScraper(BaseScraper):
    # FSC 신 사이트 목록 페이지 번호 파라미터(라이브 검증 필요)
    PAGE_PARAM = "curPage"

    def fetch_list(self, limit: int, page: int = 1) -> list[Post]:
        resp = self.fetcher.get(self._list_page_url(page))
        html = self.fetcher.text(resp)
        soup = BeautifulSoup(html, "lxml")
        posts = self._parse_list(soup)
        if not posts:
            log.warning("[%s] 목록 파싱 결과 0건 — 마크업 변경 가능성. 디버그 덤프.", self.key)
            self._dump_debug("list", html)
        return posts[:limit]

    def _parse_list(self, soup: BeautifulSoup) -> list[Post]:
        posts: list[Post] = []
        base = self.list_url

        # 게시판 목록 후보 영역. FSC 는 board-list / bo_list / table.board 계열을 쓴다.
        container = (
            soup.select_one(".board-list")
            or soup.select_one(".bd-list")
            or soup.select_one("table.board")
            or soup.select_one("div.board")
            or soup
        )

        # 각 행에서 제목 링크를 찾는다. 게시판 상세로 향하는 앵커만 채택.
        seen_ids: set[str] = set()
        for a in container.select("a[href]"):
            href = a.get("href", "").strip()
            if not href or href.startswith("#") or href.lower().startswith("javascript"):
                continue
            url = urljoin(base, href)
            post_id = self._extract_id(url)
            if not post_id or post_id in seen_ids:
                continue
            title = clean_text(a.get_text())
            if not title or len(title) < 2:
                continue
            date = self._nearby_date(a)
            seen_ids.add(post_id)
            posts.append(
                Post(
                    source_key=self.key,
                    source_name=self.name,
                    post_id=post_id,
                    title=title,
                    url=url,
                    date=date,
                )
            )
        return posts

    def _extract_id(self, url: str) -> str:
        """상세 URL 에서 안정적인 게시글 ID 추출."""
        parsed = urlparse(url)
        # 같은 게시판 경로여야 한다 (다른 메뉴/외부 링크 배제)
        board_path = urlparse(self.list_url).path.rstrip("/")
        if board_path and not parsed.path.startswith(board_path):
            return ""
        # 1) 쿼리의 흔한 ID 파라미터
        qs = parse_qs(parsed.query)
        for k in ("no", "idx", "seq", "bbsId", "nttId", "boardId"):
            if k in qs and qs[k]:
                return f"{k}:{qs[k][0]}"
        # 2) 경로 끝 숫자
        m = _ID_IN_PATH.search(parsed.path)
        if m:
            return f"path:{m.group(1)}"
        return ""

    @staticmethod
    def _nearby_date(anchor) -> str:
        """앵커 주변에서 YYYY-MM-DD / YYYY.MM.DD 형태의 날짜를 찾는다."""
        row = anchor.find_parent(["tr", "li", "div"])
        text = row.get_text(" ", strip=True) if row else ""
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

        # 본문 후보 영역
        body_el = (
            soup.select_one(".board-view .content")
            or soup.select_one(".view-cont")
            or soup.select_one(".bd-view")
            or soup.select_one("#content")
        )
        if body_el:
            post.body = clean_text(body_el.get_text("\n"))

        # 첨부파일: 다운로드 링크 수집
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if any(k in href.lower() for k in ("download", "filedown", "/file", "atchfile")):
                url = urljoin(post.url, href)
                fname = clean_text(a.get_text()) or url.rsplit("/", 1)[-1]
                post.attachments.append(Attachment(filename=fname, url=url))

        self._download_attachments(post)

    def _download_attachments(self, post: Post) -> None:
        for a in post.attachments:
            try:
                a.data = self.fetcher.download(a.url, referer=post.url)
            except AttachmentTooLarge as e:
                log.info("[%s] 첨부 용량 초과 — 링크만 첨부 %s: %s", self.key, a.filename, e)
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] 첨부 다운로드 실패 %s: %s", self.key, a.url, e)
