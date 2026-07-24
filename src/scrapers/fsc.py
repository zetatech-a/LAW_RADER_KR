"""금융위원회 (fsc.go.kr) 게시판 스크래퍼.

대상:
- 보도자료:            https://www.fsc.go.kr/no010101   (상세: /no010101/{번호})
- 입법예고/규정변경예고: https://www.fsc.go.kr/po040301   (상세: /po040301/view?noticeId=…)

두 게시판 모두 목록이 `<li> … <div class="subject"><a title="제목" href="상세"> …`
구조이고, 첨부는 목록 `<div class="file">` 안에 노출된다(라이브 HTML 기준). 날짜는
목록에 없어(담당부서/조회수만) 비워 둔다.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from ..fetcher import AttachmentTooLarge
from ..models import Attachment, Post
from .base import BaseScraper, clean_text

log = logging.getLogger(__name__)

_ID_IN_PATH = re.compile(r"/(\d{3,})(?:[/?#]|$)")
# 상세 URL 쿼리에서 찾을 안정적 ID 키
_ID_KEYS = ("noticeId", "no", "idx", "seq", "bbsId", "nttId", "boardId")
_FILE_HINT = ("getfile", "download", "filedown", "/file", "atchfile")


class FscBoardScraper(BaseScraper):
    PAGE_PARAM = "curPage"

    def _parse_list(self, soup: BeautifulSoup) -> list[Post]:
        posts: list[Post] = []
        seen: set[str] = set()

        # 제목 링크는 .subject 안의 앵커. (없으면 예전 방식으로 폴백)
        anchors = soup.select(".subject a[href]") or soup.select("a[href]")
        for a in anchors:
            href = a.get("href", "").strip()
            if not href or href.startswith("#") or href.lower().startswith("javascript"):
                continue
            url = urljoin(self.list_url, href)
            post_id = self._extract_id(url)
            if not post_id or post_id in seen:
                continue
            title = self._title(a)
            if not title or len(title) < 2:
                continue
            seen.add(post_id)
            post = Post(
                source_key=self.key,
                source_name=self.name,
                post_id=post_id,
                title=title,
                url=url,
            )
            self._collect_list_attachments(a, post)
            posts.append(post)
        return posts

    @staticmethod
    def _title(anchor) -> str:
        """title 속성을 우선 사용(뒤에 붙는 '금일 등록된 게시글' 등 잡텍스트 회피)."""
        t = clean_text(anchor.get("title") or "")
        if t:
            return t
        a = anchor
        for sp in a.select("span"):
            sp.extract()
        return clean_text(a.get_text())

    def _extract_id(self, url: str) -> str:
        parsed = urlparse(url)
        board_path = urlparse(self.list_url).path.rstrip("/")
        if board_path and not parsed.path.startswith(board_path):
            return ""
        qs = parse_qs(parsed.query)
        for k in _ID_KEYS:
            if qs.get(k):
                return f"{k}:{qs[k][0]}"
        m = _ID_IN_PATH.search(parsed.path)
        if m:
            return f"path:{m.group(1)}"
        return ""

    def _collect_list_attachments(self, anchor, post: Post) -> None:
        """목록 항목(li) 안의 첨부를 수집.

        FSC 두 게시판은 파일 구조가 다르다:
          - 보도자료: 파일명이 다운로드 앵커 안(span.name/title)에 있음
          - 입법예고: 파일명이 형제 span.name 에 있고 뒤에 '(42 KB)' 크기표기가 붙음
        그래서 .file-list 단위로 다운로드 링크 1개 + 대표 파일명을 뽑는다.
        """
        li = anchor.find_parent("li")
        if not li:
            return
        seen_href: set[str] = set()
        blocks = li.select(".file-list")
        if not blocks:
            return
        for fl in blocks:
            dl = None
            for a in fl.find_all("a", href=True):
                href = a["href"]
                if href.lower().startswith("javascript"):
                    continue  # 문서뷰어(fn_fileViewer) 등 제외
                if any(h in href.lower() for h in _FILE_HINT):
                    dl = a
                    break
            if not dl:
                continue
            url = urljoin(self.list_url, dl["href"])
            if url in seen_href:
                continue
            seen_href.add(url)
            fname = self._filelist_name(fl) or url.rsplit("/", 1)[-1]
            post.attachments.append(Attachment(filename=fname, url=url))

    @staticmethod
    def _filelist_name(file_list) -> str:
        """.file-list 안의 span.name 중 실제 파일명을 고르고 '(42 KB)' 크기표기를 제거."""
        names = [clean_text(s.get_text()) for s in file_list.select("span.name")]
        names = [n for n in names if n]
        size_only = re.compile(r"^\(?\s*[\d.]+\s*[KMG]?B\s*\)?$")
        cand = [n for n in names if not size_only.match(n)] or names
        if not cand:
            return ""
        name = max(cand, key=len)
        return re.sub(r"\s*\(\s*[\d.]+\s*[KMG]?B\s*\)\s*$", "", name).strip()

    def enrich(self, post: Post) -> None:
        # 본문은 상세 페이지에서 best-effort 로 채운다(첨부는 이미 목록에서 수집).
        try:
            resp = self.fetcher.get(post.url, referer=self.list_url)
            html = self.fetcher.text(resp)
            soup = BeautifulSoup(html, "lxml")
            # FSC 상세는 .board-view-wrap 안에 header/body/foot 구조. 본문은 .body.
            body_el = (
                soup.select_one(".board-view-wrap .body")
                or soup.select_one(".board-view-wrap .cont")
                or soup.select_one(".view-cont")
                or soup.select_one("#content")
            )
            if body_el:
                post.body = clean_text(body_el.get_text("\n"))
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] 상세 본문 로드 실패 %s: %s", self.key, post.url, e)

        for a in post.attachments:
            if a.data is not None:
                continue
            try:
                a.data = self.fetcher.download(a.url, referer=post.url)
            except AttachmentTooLarge as e:
                log.info("[%s] 첨부 용량 초과 — 링크만 첨부 %s: %s", self.key, a.filename, e)
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] 첨부 다운로드 실패 %s: %s", self.key, a.url, e)
