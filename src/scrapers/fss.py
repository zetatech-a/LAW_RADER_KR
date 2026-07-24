"""금융감독원 (fss.or.kr) eGovFrame 게시판 스크래퍼.

대상 5종은 구조가 조금씩 다르다(라이브 HTML 기준):
- 보도자료 /fss/bbs/B0000188/     : td.title 링크=제목, ID=nttId, 첨부는 목록에 노출
- 행정지도 예고 /fss/job/admnPrvntc/     : td.title 링크=제목, ID=seqno
- 세칙 예고 /fss/job/lrgRegItnPrvntc/    : td.title 링크=제목, ID=lrgSlno, 첨부 목록 노출
- 검사결과 제재 /fss/job/openInfo/       : 제목=회사명 셀, 앵커는 '내용보기'(view.do), ID=examMgmtNo
- 경영유의 공시 /fss/job/openInfoImpr/   : 제목=회사명 셀, '내용보기'가 PDF 직접다운, ID=파일명

그래서 (1) 제목은 td.title 앵커가 있으면 그 title, 없으면 숫자·날짜가 아닌 첫 셀(회사명),
(2) 상세 URL 은 앵커 href 를 그대로 사용, (3) ID 는 href 쿼리의 안정적 키에서 뽑는다.
"""
from __future__ import annotations

import logging
import os
import re
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from ..fetcher import AttachmentTooLarge
from ..models import Attachment, Post
from .base import BaseScraper, clean_text

log = logging.getLogger(__name__)

# 상세가 곧 파일 다운로드임을 나타내는 URL 조각
_FILE_HINT = ("hpdownload", "filedown", "getfile", "/cmmn/file/", "/file")
# 안정적 게시글 ID 로 쓸 쿼리 키(우선순위)
_ID_KEYS = ("nttId", "seqno", "lrgSlno")
_DATE_ONLY = re.compile(r"^\s*(20\d{2})[.\-/]?(\d{1,2})[.\-/]?(\d{1,2})")


class FssBoardScraper(BaseScraper):
    PAGE_PARAM = "pageIndex"

    def _parse_list(self, soup: BeautifulSoup) -> list[Post]:
        posts: list[Post] = []
        seen: set[str] = set()

        for row in soup.select("table tbody tr"):
            # 상세/제목 링크 결정: td.title 앵커 우선, 없으면 행의 첫 실링크
            title_a = row.select_one("td.title a[href], td.subject a[href]")
            if title_a and self._usable(title_a.get("href")):
                detail_a = title_a
                title = clean_text(title_a.get("title") or title_a.get_text())
            else:
                detail_a = self._primary_link(row)
                if not detail_a:
                    continue
                title = self._text_title(row, detail_a)

            href = detail_a.get("href", "")
            if not self._usable(href):
                continue
            url = urljoin(self.list_url, href)
            pid = self._post_id(url)
            if not title or not pid or pid in seen:
                continue
            seen.add(pid)

            post = Post(
                source_key=self.key,
                source_name=self.name,
                post_id=pid,
                title=title,
                url=url,
                date=self._row_date(row),
            )
            self._collect_row_attachments(row, exclude=href, post=post)
            posts.append(post)
        return posts

    # --- 링크/제목/ID 헬퍼 ---
    @staticmethod
    def _usable(href) -> bool:
        return bool(href) and not href.strip().lower().startswith("javascript")

    @staticmethod
    def _primary_link(row):
        for a in row.find_all("a", href=True):
            if not a["href"].strip().lower().startswith("javascript"):
                return a
        return None

    def _text_title(self, row, primary_a) -> str:
        """제목이 링크가 아닌 게시판(제재/경영유의)에서 제목(회사명 등)을 고른다.
        번호 다음에 오는, 숫자·날짜가 아닌 첫 셀을 제목으로 본다."""
        for td in row.find_all("td", recursive=False):
            if primary_a in td.descendants:
                continue
            t = clean_text(td.get_text())
            if not t or t.isdigit() or _DATE_ONLY.match(t):
                continue
            return t
        return clean_text(primary_a.get_text())

    @staticmethod
    def _post_id(url: str) -> str:
        qs = parse_qs(urlparse(url).query)
        for k in _ID_KEYS:
            if qs.get(k):
                return f"{k}:{qs[k][0]}"
        if qs.get("examMgmtNo"):
            seq = qs.get("emOpenSeq", [""])[0]
            return f"exam:{qs['examMgmtNo'][0]}_{seq}"
        if qs.get("file"):
            return f"file:{os.path.basename(unquote(qs['file'][0]))}"
        # 폴백: 경로(휘발성 파라미터 sdate/edate/pageIndex 제외)
        return f"url:{urlparse(url).path}"

    @staticmethod
    def _row_date(row) -> str:
        text = row.get_text(" ", strip=True)
        # YYYY-MM-DD / YYYY.MM.DD / YYYYMMDD 모두 처리, 범위는 첫 날짜
        m = re.search(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
        if not m:
            m = re.search(r"(20\d{2})(\d{2})(\d{2})", text)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        return ""

    def _collect_row_attachments(self, row, exclude: str, post: Post) -> None:
        """행 안의 파일 링크(상세/제목 링크 제외)를 첨부로 수집."""
        seen: set[str] = set()
        for fa in row.find_all("a", href=True):
            href = fa["href"]
            if href == exclude or not any(h in href.lower() for h in _FILE_HINT):
                continue
            url = urljoin(self.list_url, href)
            if url in seen:
                continue
            seen.add(url)
            post.attachments.append(
                Attachment(filename=self._filename(fa, url), url=url)
            )

    @staticmethod
    def _filename(anchor, url: str) -> str:
        name_el = anchor.select_one("span.name") if anchor else None
        fname = clean_text(
            (name_el.get_text() if name_el else "") or (anchor.get("title") if anchor else "")
        )
        if fname:
            return fname.replace(" 다운로드", "")
        qs = parse_qs(urlparse(url).query)
        if qs.get("file"):
            return os.path.basename(unquote(qs["file"][0]))
        return unquote(url.rsplit("/", 1)[-1]) or "첨부파일"

    # --- 상세 ---
    def enrich(self, post: Post) -> None:
        # '내용보기'가 곧 파일인 게시판(경영유의 등): 상세 URL 자체를 첨부로.
        if any(h in post.url.lower() for h in ("hpdownload", "filedown", "getfile")):
            self._download_one(post, Attachment(filename=self._filename(None, post.url), url=post.url))
            return

        try:
            resp = self.fetcher.get(post.url, referer=self.list_url)
            html = self.fetcher.text(resp)
            soup = BeautifulSoup(html, "lxml")
            body_el = (
                soup.select_one(".view-cont")
                or soup.select_one(".board-view")
                or soup.select_one(".bbs-view")
                or soup.select_one(".cont")
                or soup.select_one("#content")
            )
            if body_el:
                post.body = clean_text(body_el.get_text("\n"))
            # 상세 페이지에만 있는 첨부 보강(목록에 없던 경우)
            existing = {a.url for a in post.attachments}
            for fa in soup.find_all("a", href=True):
                if any(h in fa["href"].lower() for h in _FILE_HINT):
                    url = urljoin(post.url, fa["href"])
                    if url not in existing:
                        existing.add(url)
                        post.attachments.append(
                            Attachment(filename=self._filename(fa, url), url=url)
                        )
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] 상세 로드 실패 %s: %s", self.key, post.url, e)

        for a in post.attachments:
            self._download_one(post, a)

    def _download_one(self, post: Post, att: Attachment) -> None:
        if att.data is not None:
            if att not in post.attachments:
                post.attachments.append(att)
            return
        if att not in post.attachments:
            post.attachments.append(att)
        try:
            att.data = self.fetcher.download(att.url, referer=post.url)
        except AttachmentTooLarge as e:
            log.info("[%s] 첨부 용량 초과 — 링크만 첨부 %s: %s", self.key, att.filename, e)
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] 첨부 다운로드 실패 %s: %s", self.key, att.url, e)
