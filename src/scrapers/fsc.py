"""금융위원회 (fsc.go.kr) 게시판 스크래퍼.

대상:
- 보도자료:            https://www.fsc.go.kr/no010101   (상세: /no010101/{번호})
- 입법예고/규정변경예고: https://www.fsc.go.kr/po040301   (상세: /po040301/view?noticeId=…)

두 게시판 모두 목록이 `<li> … <div class="subject"><a title="제목" href="상세"> …`
구조이고, 첨부는 목록 `<div class="file">` 안에 노출된다(라이브 HTML 기준). 게시일은
전용 요소(`<time>`, '등록일' 라벨의 값, day/date 계열 클래스)에서만 읽는다. 목록
항목에 그런 요소가 없는 레이아웃이면 상세 머리말(`.board-view-wrap .header`)에서
보강한다 — `_list_date` / `_detail_date` 주석 참고.
"""
from __future__ import annotations

import logging
import re
from datetime import date as _date
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

# --- 게시일 추출용 ---
# 값이 '통째로' 날짜 하나일 때만 인정한다. 범위('2026-07-24 ~ 2026-08-13')나 부가
# 텍스트가 섞이면 매치되지 않는다.
_DATE_VALUE = re.compile(r"^(20\d{2})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})\s*일?\.?$")
_ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T")
# 클래스 토큰(비알파벳 제거 후)이 '게시일 전용'을 뜻하는지.
_DATE_CLASS = re.compile(
    r"^(?:day|date"
    r"|(?:reg|regist|register|write|wrt|post|board|bbs|notice|create|created)(?:ed)?date"
    r"|regdt)$"
)
# 값을 게시일로 받아들일 라벨.
_POST_DATE_LABELS = frozenset(
    {
        "등록일", "등록일자", "게시일", "게시일자", "작성일", "작성일자",
        "배포일", "보도일", "공고일", "일자", "날짜",
    }
)
# 값 앞에 붙은 라벨('등록일 : 2026-07-24')을 떼어낸다. 인정하는 라벨과 떼어내는 라벨이
# 어긋나면 '날짜: 2026-07-24' 같은 값이 통째로 버려지므로, 목록에서 직접 만든다.
# (긴 라벨 우선 — '등록일자'가 '등록일'보다 먼저 매치되어야 '자'가 남지 않는다.)
_DATE_LABEL_PREFIX = re.compile(
    r"^(?:" + "|".join(sorted(_POST_DATE_LABELS, key=len, reverse=True)) + r")\s*[:：]?\s*"
)
# 게시일이 아닌 '기간' 표기 — 입법예고의 예고기간(시작일·종료일) 등.
_PERIOD_MARKERS = ("기간", "기한", "마감", "시행일", "제출", "~", "∼", "〜")
# 날짜 후보에서 통째로 제외할 영역(첨부파일명·제목).
_EXCLUDED_CLASS = ("file", "atch", "subject")
# 목록에 게시일 전용 요소가 없을 때 상세 페이지에서 볼 머리말/꼬리말 영역.
_DETAIL_DATE_SCOPES = (
    ".board-view-wrap .header",
    ".board-view-wrap .info",
    ".board-view-wrap .foot",
    ".board-view .header",
    ".board-view .info",
    ".view-head",
)


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
                date=self._list_date(a),
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

    # --- 게시일 ---
    def _list_date(self, anchor) -> str:
        """목록 항목의 '게시일 전용' 요소에서 게시일을 읽어 YYYY-MM-DD 로 돌려준다.

        항목 전체 텍스트에서 첫 날짜를 긁으면 안 된다. 입법예고 목록은 예고기간
        ('2026-07-24 ~ 2026-08-13')이 게시일보다 앞에 오고, 보도자료 목록에는
        첨부파일명('2025년 계획.hwp')·공고번호('제2026-15호')처럼 날짜로 보이는
        숫자가 섞여 있어 엉뚱한 값을 게시일로 잡는다.

        그래서 (1) <time>, (2) '등록일/게시일' 라벨의 값, (3) day/date 계열 클래스
        요소 순으로 후보를 좁히고, 그 값이 통째로 유효한 날짜일 때만 인정한다.
        전용 요소가 없거나 형식이 어긋나면 빈 문자열을 유지하고, 상세를 읽는
        enrich() 단계에서 `_detail_date` 로 한 번 더 시도한다.
        """
        li = anchor.find_parent("li")
        if li is None:
            return ""
        return self._scope_date(li)

    @classmethod
    def _scope_date(cls, scope) -> str:
        """주어진 영역(목록 항목 <li> 또는 상세 머리말) 안에서 게시일을 찾는다."""
        for raw in cls._date_candidates(scope):
            parsed = cls._normalize_date(raw)
            if parsed:
                return parsed
        return ""

    @classmethod
    def _date_candidates(cls, scope):
        """게시일 후보 텍스트를 신뢰도 순으로 낸다.

        첨부·제목 영역과 '예고기간' 블록은 세 경로 모두에서 똑같이 배제한다. <time>
        이라고 예외를 두면 <div class="period">…<time datetime="2026-07-24"> 같은
        마크업에서 예고기간 시작일이 그대로 게시일로 올라온다.
        """
        # (1) 시맨틱 마크업이 있으면 가장 믿을 만하다.
        for t in scope.find_all("time"):
            if cls._excluded(t, scope) or cls._in_period_block(t, scope):
                continue
            text = clean_text(t.get_text(" "))
            if cls._has_period_marker(text):
                continue
            yield t.get("datetime", "")
            yield text

        # (2) '등록일/게시일' 라벨의 값. '예고기간' 등 기간 라벨은 후보가 되지 않는다.
        for label in scope.find_all(["dt", "th", "strong", "b", "em", "i", "span", "p"]):
            if cls._excluded(label, scope) or not cls._is_post_date_label(label.get_text()):
                continue
            yield from cls._label_values(label)

        # (3) day/date 계열 클래스를 가진 전용 요소.
        for el in scope.find_all(True):
            if cls._excluded(el, scope) or not cls._has_date_class(el):
                continue
            text = clean_text(el.get_text(" "))
            if cls._has_period_marker(text) or cls._in_period_block(el, scope):
                continue
            yield _DATE_LABEL_PREFIX.sub("", text)

    @staticmethod
    def _label_values(label):
        """라벨 요소 뒤의 값 후보: 바로 뒤 텍스트 노드 → 다음 형제 요소."""
        parts = []
        for node in label.next_siblings:
            if getattr(node, "name", None):
                break
            parts.append(str(node))
        yield clean_text(" ".join(parts))
        sib = label.find_next_sibling()
        if sib is not None:
            yield clean_text(sib.get_text(" "))

    @staticmethod
    def _is_post_date_label(text: str) -> bool:
        label = clean_text(text).rstrip(" :：")
        if not label or len(label) > 6:
            return False
        if any(m in label for m in _PERIOD_MARKERS):
            return False
        return label in _POST_DATE_LABELS

    @staticmethod
    def _has_period_marker(text: str) -> bool:
        return any(m in text for m in _PERIOD_MARKERS)

    @classmethod
    def _in_period_block(cls, el, scope) -> bool:
        """'예고기간' 같은 기간 표기가 붙은 블록 안의 날짜는 게시일이 아니다."""
        node = el.parent
        for _ in range(2):
            if node is None or node is scope or getattr(node, "name", None) is None:
                return False
            if cls._has_period_marker(clean_text(node.get_text(" "))):
                return True
            node = node.parent
        return False

    @staticmethod
    def _has_date_class(el) -> bool:
        for name in el.get("class") or []:
            if _DATE_CLASS.match(re.sub(r"[^a-z]", "", name.lower())):
                return True
        return False

    @staticmethod
    def _excluded(el, scope) -> bool:
        """첨부(.file/.file-list)·제목(.subject) 영역은 날짜 후보가 아니다."""
        node = el
        while node is not None and node is not scope and getattr(node, "name", None) is not None:
            for name in node.get("class") or []:
                low = name.lower()
                if any(bad in low for bad in _EXCLUDED_CLASS):
                    return True
            node = node.parent
        return False

    @classmethod
    def _detail_date(cls, soup) -> str:
        """상세 페이지 머리말/꼬리말의 게시일 전용 요소에서 게시일을 읽는다.

        FSC 상세는 .board-view-wrap 안 header/body/foot 구조이고 담당부서·등록일은
        본문(.body) 밖 머리말·꼬리말에 있다. 본문 전체는 훑지 않는다 — 보도자료
        본문에는 '2026-07-24 중 배포'처럼 게시일이 아닌 날짜가 흔하다.
        """
        for selector in _DETAIL_DATE_SCOPES:
            for scope in soup.select(selector):
                parsed = cls._scope_date(scope)
                if parsed:
                    return parsed
        return ""

    @staticmethod
    def _normalize_date(raw: str) -> str:
        """값이 통째로 날짜 하나일 때만 YYYY-MM-DD 로. 아니면 ''(빈 문자열 유지)."""
        text = clean_text(raw or "")
        if _ISO_DATETIME.match(text):
            text = text.split("T", 1)[0]
        m = _DATE_VALUE.match(text)
        if not m:
            return ""
        year, month, day = (int(g) for g in m.groups())
        try:
            return _date(year, month, day).isoformat()
        except ValueError:  # 2026-13-45 처럼 달력에 없는 값
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
            # 목록에 게시일 전용 요소가 없는 레이아웃이면 상세 머리말에서 보강한다.
            # (목록에서 이미 얻었으면 덮어쓰지 않는다.)
            if not post.date:
                post.date = self._detail_date(soup)
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
