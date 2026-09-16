"""개인정보보호위원회 (pipc.go.kr) 게시판 스크래퍼.

대상 2종 — 둘 다 같은 `/np/cop/bbs/` 게시판 엔진이므로 한 클래스로 처리한다.
- 공지사항  selectBoardList.do?bbsId=BS061&mCode=C010010000
- 보도자료  selectBoardList.do?bbsId=BS074&mCode=C020010000

개발 환경 제약: `pipc.go.kr` 은 이 저장소의 개발/에이전트 환경에서 **egress 정책에
의해 차단**되어 라이브 HTML 을 확인하지 못했다(`likms.assembly.go.kr` 과 같은 상황 —
tests/fixtures/README.md 참고). 그래서 이 파서는 '클래스 이름 추정'에 의존하지 않고,
**과제에서 사실로 주어진 URL 계약**만을 목록 파싱의 근거로 삼는다.

  목록 : .../np/cop/bbs/selectBoardList.do?bbsId=…&mCode=…
  상세 : .../np/cop/bbs/selectBoardArticle.do?bbsId=…&mCode=…&nttId=…

즉 목록 행을 `table tbody tr` 이나 `.board_list li` 같은 **추정 셀렉터로 찾지 않는다.**
문서 안의 모든 앵커 중 위 상세 endpoint 와 `nttId` 를 가진 것만 고르고, 행(날짜)은
그 앵커의 조상에서 역으로 찾는다. 표 기반이든 목록 기반이든 같은 코드로 동작하고,
사이트가 레이아웃만 바꿔도 깨지지 않는다.

반면 **상세 본문 컨테이너와 첨부 endpoint 는 URL 계약으로 확정할 수 없다.** 이 둘은
아래 `_BODY_SELECTORS` / `_FILE_HINT` 의 후보 목록으로 시도하되,

  - 어느 후보도 맞지 않으면 **조용히 넘어가지 않는다** — 경고 + debug 덤프를 남기고
    `enrich_succeeded()` 가 False 를 돌려주므로 운영 집계·verify_sources 에 드러난다.
  - 코드 수정 없이 고칠 수 있도록 `config.yaml` 의 소스별 `body_selectors` 로
    덮어쓸 수 있다(의안의 `detail_url` 오버라이드와 같은 방식).

`enrich_succeeded()` 를 override 하는 이유: 이 두 소스는 AI 3줄 요약이 목적이고
요약 입력은 본문이다. 기본 판정('본문·구조화항목·첨부 중 하나라도 있으면 성공')을
그대로 쓰면 본문 셀렉터가 깨져도 첨부만으로 '성공'이 되어 고장이 통계에 묻힌다.
"""
from __future__ import annotations

import logging
import re
from datetime import date as _date
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, NavigableString

from ..fetcher import AttachmentTooLarge
from ..models import Attachment, Post
from .base import BaseScraper, clean_text

log = logging.getLogger(__name__)

# --- URL 계약 (라이브 예시로 확정된 사실) ---
_ARTICLE_ENDPOINT = "selectboardarticle.do"
# 게시글 식별자. 숫자만 허용한다(조작된/깨진 링크로 엉뚱한 post_id 를 만들지 않도록).
_NTT_ID = re.compile(r"^\d+$")
# 상세 URL 에 남길 파라미터와 그 순서. 나머지(pageIndex·searchCnd 등 휘발성 값)는
# 버려야 같은 글이 페이지마다 다른 URL 로 보이지 않는다.
_CANONICAL_KEYS = ("bbsId", "mCode", "nttId")

# --- 제목 정리 ---
# title 속성이 제목이 아니라 UI 안내문인 경우. 이때는 앵커 텍스트를 쓴다.
_GENERIC_TITLE_ATTR = frozenset(
    {"상세보기", "자세히보기", "자세히 보기", "새창열림", "새 창 열림", "본문보기", "view", "detail"}
)
# 제목 앵커 안에 섞이는 '화면 전용' 요소의 class 토큰. 부분문자열이 아니라 토큰으로
# 맞춘다 — 'file' 을 부분문자열로 보면 'profile' 이, 'new' 를 그렇게 보면 'news' 가 걸린다.
_NOISE_CLASS = frozenset(
    {
        "blind", "sr-only", "screen-out", "screen_out", "offscreen", "hidden", "ir",
        "skip", "new", "newicon", "new-icon", "ico", "icon", "badge", "tag", "flag",
        "label", "mark", "state", "status", "notice-mark", "file", "files", "attach",
        "atch", "atchfile", "atch-file", "num", "no",
    }
)
# 그 자체로 제목이 될 수 없는 UI 표식. **별도 요소**로 들어 있을 때만 떼어낸다.
# ('공지' 처럼 제목 첫 단어일 수도 있는 말은 아래 접두/접미 정리에서는 건드리지 않는다)
_UI_TOKENS = frozenset(
    {
        "n", "new", "hot", "update", "새글", "신규", "공지", "답변", "첨부", "첨부파일",
        "파일첨부", "다운로드", "내려받기", "상세보기", "새창열림", "새 창 열림",
    }
)
# 제목 텍스트의 맨 앞/뒤에 남은 표식 정리. 뜻이 겹칠 여지가 없는 것만 넣는다
# ('공지'·'첨부' 는 제목의 첫 단어일 수 있으므로 여기 없다).
_EDGE_TOKENS = ("NEW", "New", "new", "N", "새글", "신규", "첨부파일")
_LEADING_TOKEN = re.compile(r"^(?:" + "|".join(_EDGE_TOKENS) + r")(?=\s)\s*")
_TRAILING_TOKEN = re.compile(r"\s*(?<=\s)(?:" + "|".join(_EDGE_TOKENS) + r")$")

# --- 게시일 ---
# 셀/요소 전체가 날짜 하나일 때만 인정한다(제목·조회수에 섞인 숫자를 날짜로 읽지 않도록).
_DATE_EXACT = re.compile(
    r"^(20\d{2})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})\s*일?\.?$"
)
_DATE_COMPACT = re.compile(r"^(20\d{2})(\d{2})(\d{2})$")
# 행 텍스트 안에서 날짜를 찾는 마지막 폴백.
_DATE_ANYWHERE = re.compile(r"(20\d{2})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})")
_DATE_CLASS = re.compile(r"^(?:date|day|regdate|regdt|writedate|wrtdate|postdate|createdate)$")
# 목록 '행'으로 볼 조상 태그. 표/리스트/정의목록 어느 마크업이든 받는다.
_ROW_TAGS = ("tr", "li", "dl")
# 앵커에서 행까지 거슬러 올라갈 최대 깊이(무한 상향 방지).
_ROW_MAX_DEPTH = 8

# --- 상세 본문 ---
# **라이브 미검증 후보 목록.** 좁은 것부터 시도하고, 아래 `_is_bodylike` 가 링크 밀도로
# 메뉴·네비게이션 컨테이너를 걸러낸다. config 의 `body_selectors` 로 덮어쓸 수 있다.
_BODY_SELECTORS = (
    ".bbs-view-cont", ".bbs_view_cont", ".bbsViewCont",
    ".board-view-cont", ".board_view_cont",
    ".view-cont", ".view_cont", ".viewCont",
    ".board-contents", ".board_contents",
    ".bbs-content", ".bbs_content",
    ".article-cont", ".article_cont",
    ".n-dbdata",
    ".board-view .cont", ".bbs-view .cont", ".view-wrap .cont",
)
# 본문 컨테이너 안에서 먼저 지워야 하는 영역(메타·첨부·이전다음글·만족도·버튼 등).
_BODY_DROP_TAGS = ("script", "style", "nav", "header", "footer", "form", "button", "iframe")
_BODY_DROP_CLASS = frozenset(
    {
        "file", "files", "filelist", "file-list", "file_list", "attach", "attachfile",
        "atch", "atchfile", "atch-file", "addfile", "add-file",
        "prev", "next", "prevnext", "prev-next", "prev_next", "updown",
        "paging", "pagination", "btn", "btns", "btn-area", "btn_area", "board-btn",
        "satisfaction", "survey", "poll", "sns", "share", "blind", "sr-only",
        "breadcrumb", "location", "lnb", "gnb", "snb", "util", "skip",
        "board-view-info", "view-info", "info", "meta", "board-info",
    }
)
# 블록의 텍스트가 이 말로 **시작하면** 그 블록은 본문이 아니다(이전/다음글·만족도·첨부 안내).
_BODY_DROP_PREFIXES = (
    "이전글", "다음글", "이전 글", "다음 글", "윗글", "아랫글",
    "첨부파일", "붙임파일", "만족도", "이 페이지에서 제공하는 정보",
    "담당부서", "담당자", "문의처", "조회수",
)
# 링크가 이만큼 이상을 차지하면 본문이 아니라 메뉴·목록이다.
_MAX_LINK_RATIO = 0.5

# --- 첨부 ---
# 다운로드 endpoint 의 '모양'으로 찾는다(파일 확장자만으로 아무 링크나 첨부로 보지 않는다).
_FILE_HINT = (
    "filedown", "file_down", "filedownload", "downloadfile", "getfile",
    "/cmm/fms/", "atchfile", "/download", "download.do", "fileidx", "filesn",
)
# href 에 이 파라미터가 있으면 파일 링크로 본다(endpoint 이름이 달라도 잡힌다).
_FILE_QUERY_KEYS = ("atchFileId", "atchfileid", "fileSn", "filesn", "fileId", "fileid")
# 파일명이 담길 수 있는 쿼리 파라미터.
_FILENAME_QUERY_KEYS = ("orignFileNm", "fileNm", "fileName", "filename", "orgFileNm")
# eGovFrame 표준 다운로드 스크립트. href 가 javascript 인 게시판을 위한 것이다.
# (표준 프레임워크 규약이며 PIPC 라이브로는 확인하지 못했다 — 맞지 않으면 첨부가
#  잡히지 않을 뿐, 본문·메일 발송은 그대로 진행된다)
_JS_FILE_DOWN = re.compile(
    r"(?:fn_egov_downFile|fnFileDown|fn_file_down|fn_download)\s*\(\s*"
    r"['\"]([^'\"]+)['\"]\s*,\s*['\"]?(\d+)['\"]?",
    re.IGNORECASE,
)
_EGOV_FILE_DOWN_PATH = "/cmm/fms/FileDown.do"
# 파일명 뒤에 붙는 크기 표기와 안내문.
_SIZE_SUFFIX = re.compile(r"\s*[\(\[]?\s*[\d.,]+\s*(?:KB|MB|GB|B|바이트)\s*[\)\]]?\s*$", re.IGNORECASE)
_DOWNLOAD_WORDS = ("다운로드", "내려받기", "바로보기", "미리보기", "새창열림", "새 창 열림")
# '파일명처럼 보이는가'. 확장자를 특정 목록으로 제한하지 않는다(PDF·HWP·HWPX·ZIP…).
_HAS_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,8}$")
# debug 덤프 파일명에 쓸 수 없는 문자.
_UNSAFE_NAME = re.compile(r"[^0-9A-Za-z._-]+")


def _class_tokens(el) -> set[str]:
    return {c.lower() for c in (el.get("class") or [])}


def _first_query(qs: dict, *keys: str) -> str:
    for key in keys:
        values = qs.get(key)
        if values and values[0].strip():
            return values[0].strip()
    return ""


def _normalize_date(raw: str, *, allow_compact: bool = False) -> str:
    """값이 통째로 날짜 하나일 때만 YYYY-MM-DD 로. 아니면 빈 문자열."""
    text = clean_text(raw or "")
    m = _DATE_EXACT.match(text)
    if not m and allow_compact:
        m = _DATE_COMPACT.match(text)
    if not m:
        return ""
    return _iso(*(int(g) for g in m.groups()))


def _iso(year: int, month: int, day: int) -> str:
    try:
        return _date(year, month, day).isoformat()
    except ValueError:  # 2026-13-45 처럼 달력에 없는 값
        return ""


class PipcBoardScraper(BaseScraper):
    """PIPC `/np/cop/bbs/` 게시판(공지사항·보도자료) 공용 파서."""

    # eGovFrame 표준 게시판의 목록 페이지 파라미터. 라이브 확인은 하지 못했으나,
    # **틀려도 안전하다** — 2페이지가 1페이지와 같으면 BaseScraper.collect 가
    # '진전 없음'으로 보고 경계 도달 처리하므로 1페이지 수집으로 degrade 할 뿐이다.
    # verify_sources 가 2페이지를 실제로 요청해 이 값의 작동 여부를 보고한다.
    PAGE_PARAM = "pageIndex"

    def __init__(self, source, fetcher):
        super().__init__(source, fetcher)
        qs = parse_qs(urlparse(self.list_url).query)
        # 목록 URL 이 선언한 게시판. 상세 링크가 다른 게시판을 가리키면(관련글 위젯 등)
        # 이 소스의 글이 아니므로 버린다.
        self._bbs_id = _first_query(qs, "bbsId")
        self._m_code = _first_query(qs, "mCode")
        raw_selectors = source.extra.get("body_selectors")
        self._body_selectors: tuple[str, ...] = (
            tuple(str(s).strip() for s in raw_selectors if str(s).strip())
            if isinstance(raw_selectors, (list, tuple)) and raw_selectors
            else _BODY_SELECTORS
        )

    # ------------------------------------------------------------------ 목록
    def _parse_list(self, soup: BeautifulSoup) -> list[Post]:
        posts: list[Post] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            url, ntt_id = self._article_link(a)
            if not url:
                continue
            # 상단 고정(공지) 글은 여러 페이지에 반복 노출되고, 한 행에 제목 링크가
            # 둘(제목·썸네일) 달리기도 한다. nttId 로 dedup 하면 두 경우가 모두 잡힌다.
            # 페이지 간 중복은 BaseScraper.collect 의 scanned_ids 가 다시 거른다.
            if ntt_id in seen:
                continue
            title = self._title(a)
            if not title:
                continue
            seen.add(ntt_id)
            posts.append(
                Post(
                    source_key=self.key,
                    source_name=self.name,
                    post_id=f"nttId:{ntt_id}",
                    title=title,
                    url=url,
                    date=self._row_date(a),
                )
            )
        return posts

    def _article_link(self, anchor) -> tuple[str, str]:
        """앵커가 이 게시판의 상세 링크면 (정규화 URL, nttId). 아니면 ("", "")."""
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript"):
            return "", ""
        parsed = urlparse(urljoin(self.list_url, href))
        if parsed.path.rsplit("/", 1)[-1].lower() != _ARTICLE_ENDPOINT:
            return "", ""
        qs = parse_qs(parsed.query)
        ntt_id = _first_query(qs, "nttId")
        if not _NTT_ID.match(ntt_id):
            return "", ""
        bbs_id = _first_query(qs, "bbsId") or self._bbs_id
        if self._bbs_id and bbs_id != self._bbs_id:
            return "", ""  # 다른 게시판을 가리키는 링크(관련글·배너 등)
        m_code = _first_query(qs, "mCode") or self._m_code
        query = {"bbsId": bbs_id, "mCode": m_code, "nttId": ntt_id}
        canonical = urlencode(
            [(k, query[k]) for k in _CANONICAL_KEYS if query.get(k)]
        )
        return urlunparse(parsed._replace(query=canonical, fragment="")), ntt_id

    def _title(self, anchor) -> str:
        """실제 제목만. 'N'·new 배지, 첨부 아이콘 라벨, 스크린리더 전용 텍스트 제외."""
        attr = clean_text(anchor.get("title") or "")
        if attr and attr.lower() not in _GENERIC_TITLE_ATTR:
            return self._strip_edge_tokens(attr)
        return self._strip_edge_tokens(self._visible_text(anchor))

    @staticmethod
    def _visible_text(anchor) -> str:
        """앵커 안에서 '화면 전용' 요소를 뺀 텍스트."""
        parts: list[str] = []
        for node in anchor.descendants:
            if not isinstance(node, NavigableString):
                continue
            text = str(node).strip()
            if not text:
                continue
            if PipcBoardScraper._noise_ancestor(node, anchor):
                continue
            parts.append(text)
        return clean_text(" ".join(parts))

    @staticmethod
    def _noise_ancestor(node, anchor) -> bool:
        """이 텍스트 노드가 배지·아이콘·스크린리더 전용 요소 안에 있는지."""
        el = node.parent
        while el is not None and el is not anchor:
            if el.name in ("script", "style"):
                return True
            if _class_tokens(el) & _NOISE_CLASS:
                return True
            # class 가 없어도 요소 전체가 UI 표식 한 마디면 제목이 아니다.
            if clean_text(el.get_text()).strip().lower() in _UI_TOKENS:
                return True
            el = el.parent
        return False

    @staticmethod
    def _strip_edge_tokens(text: str) -> str:
        """맨 앞/뒤에 남은 UI 표식('제목 N', 'NEW 제목')을 떼어낸다."""
        prev = None
        while text and text != prev:
            prev = text
            text = _TRAILING_TOKEN.sub("", _LEADING_TOKEN.sub("", text)).strip()
        return text

    # --- 게시일 ---
    def _row_date(self, anchor) -> str:
        row = self._row(anchor)
        if row is None:
            return ""
        # (1) 날짜 전용 class 를 가진 요소 — 가장 믿을 만하다.
        for el in row.find_all(True):
            if anchor in el.descendants or el is anchor:
                continue
            if not any(_DATE_CLASS.match(re.sub(r"[^a-z]", "", c.lower())) for c in el.get("class") or []):
                continue
            parsed = _normalize_date(el.get_text(" "), allow_compact=True)
            if parsed:
                return parsed
        # (2) 제목 앵커를 품지 않은 셀/요소 전체가 날짜 하나인 경우.
        for el in row.find_all(["td", "dd", "span", "p", "em", "time", "div"]):
            if el is anchor or anchor in el.descendants:
                continue
            parsed = _normalize_date(
                el.get("datetime") or el.get_text(" "), allow_compact=True
            )
            if parsed:
                return parsed
        # (3) 마지막 폴백 — 행 텍스트(제목 제외)에서 첫 날짜.
        title_text = anchor.get_text(" ", strip=True)
        row_text = row.get_text(" ", strip=True).replace(title_text, " ")
        m = _DATE_ANYWHERE.search(row_text)
        return _iso(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else ""

    @staticmethod
    def _row(anchor):
        """제목 앵커가 속한 목록 '행'. 표/리스트/정의목록 어느 쪽이든 받는다."""
        el = anchor.parent
        for _ in range(_ROW_MAX_DEPTH):
            if el is None or getattr(el, "name", None) is None:
                return None
            if el.name in _ROW_TAGS:
                return el
            el = el.parent
        return None

    # ------------------------------------------------------------------ 상세
    def enrich(self, post: Post) -> None:
        try:
            resp = self.fetcher.get(post.url, referer=self.list_url)
            html = self.fetcher.text(resp)
        except Exception as e:  # noqa: BLE001 — 한 건 실패가 나머지를 막지 않는다
            log.warning(
                "[%s] 상세 로드 실패 post_id=%s url=%s: %s",
                self.key, post.post_id, post.url, e,
            )
            return

        soup = BeautifulSoup(html, "lxml")
        post.body = self._body(soup)
        if not post.body:
            # 조용히 넘기지 않는다. enrich_succeeded 가 False 가 되어 운영 집계와
            # verify_sources 에 드러나고, 덤프로 실제 셀렉터를 확인할 수 있다.
            log.warning(
                "[%s] 상세 본문 selector 를 찾지 못함 — post_id=%s url=%s "
                "(config 의 body_selectors 로 덮어쓸 수 있습니다)",
                self.key, post.post_id, post.url,
            )
            self._dump_debug(f"detail_{_UNSAFE_NAME.sub('_', post.post_id)}", html)

        self._collect_attachments(soup, post)
        for att in post.attachments:
            self._download_one(post, att)

    def enrich_succeeded(self, post: Post) -> bool:
        """PIPC 는 **본문이 있어야** 상세 수집 성공이다.

        기본 판정은 '본문·구조화항목·첨부 중 하나'라서, 본문 셀렉터가 깨져도 첨부만
        잡히면 성공으로 센다. 이 두 소스는 AI 3줄 요약이 목적이고 요약 입력이 본문이라,
        그 상태를 성공으로 두면 요약이 통째로 빠진 채 고장이 통계에 묻힌다.
        """
        return bool((post.body or "").strip())

    def _body(self, soup: BeautifulSoup) -> str:
        """기사 본문만. 메뉴·머리말 메타·첨부·이전다음글·만족도는 제외한다."""
        for selector in self._body_selectors:
            try:
                candidates = soup.select(selector)
            except Exception:  # noqa: BLE001 — config 로 들어온 잘못된 셀렉터
                log.warning("[%s] body_selectors 항목이 올바르지 않습니다: %r", self.key, selector)
                continue
            for el in candidates:
                # 원본 soup 을 건드리면 첨부 수집이 영향을 받는다. 사본에서 지운다.
                cleaned = self._without_noise(el)
                if cleaned is None:
                    continue
                text = clean_text(cleaned.get_text("\n"))
                if text and self._is_bodylike(cleaned, text):
                    return text
        return ""

    @staticmethod
    def _without_noise(el):
        """컨테이너 사본에서 본문이 아닌 하위 영역을 제거한다."""
        copy_soup = BeautifulSoup(str(el), "lxml")
        root = copy_soup.body or copy_soup
        for tag in root.find_all(_BODY_DROP_TAGS):
            tag.decompose()
        # **안쪽부터** 지운다. pre-order 를 뒤집으면 모든 자손이 조상보다 먼저 온다.
        # 바깥부터 보면 '머리말 메타로 시작하는' 바깥 컨테이너가 본문까지 통째로
        # 끌고 나간다(첫 자식이 담당부서 목록이라는 이유만으로 기사 전체가 사라진다).
        # 안쪽부터 지우면 조상은 자식이 빠진 뒤의 텍스트로 다시 판정된다.
        for tag in reversed(root.find_all(True)):
            if tag is root or tag.decomposed:
                continue
            if _class_tokens(tag) & _BODY_DROP_CLASS:
                tag.decompose()
                continue
            text = clean_text(tag.get_text(" "))
            if text and text.startswith(_BODY_DROP_PREFIXES):
                tag.decompose()
        return root

    @staticmethod
    def _is_bodylike(el, text: str) -> bool:
        """링크가 대부분을 차지하면 본문이 아니라 메뉴·목록이다."""
        link_chars = sum(len(clean_text(a.get_text(" "))) for a in el.find_all("a"))
        return link_chars <= len(text) * _MAX_LINK_RATIO

    # --- 첨부 ---
    def _collect_attachments(self, soup: BeautifulSoup, post: Post) -> None:
        existing = {a.url for a in post.attachments}
        for a in soup.find_all("a", href=True):
            url = self._file_url(a, post.url)
            if not url or url in existing:
                continue
            existing.add(url)
            post.attachments.append(Attachment(filename=self._filename(a, url), url=url))

    def _file_url(self, anchor, base_url: str) -> str:
        """앵커가 첨부 다운로드면 절대 URL. 아니면 빈 문자열."""
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith("#"):
            return ""
        if href.lower().startswith("javascript"):
            m = _JS_FILE_DOWN.search(href) or _JS_FILE_DOWN.search(
                anchor.get("onclick") or ""
            )
            if not m:
                return ""
            query = urlencode({"atchFileId": m.group(1), "fileSn": m.group(2)})
            return urljoin(base_url, f"{_EGOV_FILE_DOWN_PATH}?{query}")
        url = urljoin(base_url, href)
        lowered = url.lower()
        if any(hint in lowered for hint in _FILE_HINT):
            return url
        qs = parse_qs(urlparse(url).query)
        if _first_query(qs, *_FILE_QUERY_KEYS):
            return url
        return ""

    @staticmethod
    def _filename(anchor, url: str) -> str:
        """화면에 보이는 파일명을 그대로. PDF·HWP·HWPX 등 확장자를 가리지 않는다.

        후보가 여럿일 때는 **확장자를 가진 것**을 먼저 쓴다. title 속성이 파일명이
        아니라 안내문('보도자료 새창열림')인 게시판에서 앵커 텍스트의 진짜 파일명을
        놓치지 않기 위함이다.
        """
        qs = parse_qs(urlparse(url).query)
        raw_candidates = [
            anchor.get("title") or "",
            PipcBoardScraper._visible_text(anchor),
            unquote(_first_query(qs, *_FILENAME_QUERY_KEYS)),
            unquote(urlparse(url).path.rsplit("/", 1)[-1]),
        ]
        candidates: list[str] = []
        for raw in raw_candidates:
            name = _SIZE_SUFFIX.sub("", clean_text(raw or ""))
            for word in _DOWNLOAD_WORDS:
                name = name.replace(word, " ")
            name = clean_text(name)
            if name and not name.lower().endswith(".do"):
                candidates.append(name)
        for name in candidates:
            if _HAS_EXTENSION.search(name):
                return name
        return candidates[0] if candidates else "첨부파일"

    def _download_one(self, post: Post, att: Attachment) -> None:
        if att.data is not None:
            return
        try:
            att.data = self.fetcher.download(att.url, referer=post.url)
        except AttachmentTooLarge as e:
            log.info("[%s] 첨부 용량 초과 — 링크만 첨부 %s: %s", self.key, att.filename, e)
        except Exception as e:  # noqa: BLE001 — 첨부 하나가 글 전체를 날리지 않는다
            log.warning("[%s] 첨부 다운로드 실패 %s: %s", self.key, att.url, e)
