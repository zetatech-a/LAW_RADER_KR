"""금융위원회 (fsc.go.kr) 게시판 스크래퍼.

대상:
- 보도자료:            https://www.fsc.go.kr/no010101   (상세: /no010101/{번호})
- 입법예고/규정변경예고: https://www.fsc.go.kr/po040301   (상세: /po040301/view?noticeId=…)

두 게시판 모두 목록이 `<li> … <div class="subject"><a title="제목" href="상세"> …`
구조이고, 첨부는 목록 `<div class="file">` 안에 노출된다(라이브 HTML 기준). 게시일은
전용 요소(`<time>`, '등록일' 라벨의 값, day/date 계열 클래스)에서만 읽는다. 목록
항목에 그런 요소가 없는 레이아웃이면 상세 머리말(`.board-view-wrap .header`)에서
보강한다 — `_list_date` / `_detail_date` 주석 참고.

게시일 관련 라이브 검증(2026-07-30, verify_sources):
- 두 게시판 모두 목록 단계에서 게시일이 채워진다(상세 폴백까지 가지 않는다).
  이 파일에 한때 '날짜는 목록에 없어 비워 둔다'고 적혀 있었으나 사실과 다르다.
- 보도자료 10건: 최신이 당일(2026-07-30), 글 번호 내림차순과 날짜 내림차순이 일치.
- 입법예고 10건: 예고기간이 아닌 게시일. 예고기간 종료일이었다면 미래 날짜가
  섞였을 텐데 전부 과거이고, 최신이 2026-07-24 로 게시 간격(3~9일)과도 맞는다.
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
# <time datetime> 의 날짜 부분. HTML 의 global date-and-time 은 날짜와 시각을 'T'
# 또는 공백으로 가르므로 둘 다 받는다.
_ISO_DATETIME = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ]")
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
# 게시일이 아닌 '기간' 라벨 — 입법예고의 예고기간(시작일·종료일) 등.
_PERIOD_LABELS = ("기간", "기한", "마감", "시행일", "제출")
# 날짜이긴 하나 게시일이 아닌 맥락. <time> 은 '이 값이 날짜'라는 것만 알려줄 뿐
# '게시일'이라는 역할까지 보장하지 않으므로, 라벨 없는 <time> 을 받기 전에 거른다.
_EVENT_LABELS = ("회의일", "행사일", "개최", "일시", "예정일", "발표일", "접수일")
_NON_POSTING_LABELS = _PERIOD_LABELS + _EVENT_LABELS
# 값 자체가 기간(범위)임을 드러내는 표기.
_RANGE_MARKERS = ("~", "∼", "〜")
# 라벨과 값 사이를 가르는 구분자만으로 이뤄진 조각('：', '|', '-' 등)은 라벨이 아니다.
# 범위 표기(~)는 뜻이 있으므로 여기 넣지 않는다.
_SEPARATOR_ONLY = re.compile(r"^[\s:：·ㆍ|/\\,.\-–—_>»«\[\](){}]+$")
# 텍스트 어딘가에 날짜가 들어 있는지(라벨인지 '값을 품은 블록'인지 가르는 데 쓴다).
_DATE_ANYWHERE = re.compile(r"(?:19|20)\d{2}\s*[-./년]\s*\d{1,2}\s*[-./월]\s*\d{1,2}")
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

        역할이 명시된 값이 먼저다. <time> 은 '이 값이 날짜'라는 것만 알려줄 뿐 그게
        게시일인지는 알려주지 않아서, 회의일·행사일처럼 무관한 날짜가 <time> 으로
        적히고 게시일은 '등록일' 라벨로 따로 있는 항목에서 엉뚱한 값을 집는다.

        첨부·제목 영역과 기간·행사 블록은 세 경로 모두에서 똑같이 배제한다.
        """
        # (1) '등록일/게시일' 라벨의 값 — 역할이 명시돼 있어 가장 믿을 만하다.
        for label in scope.find_all(["dt", "th", "strong", "b", "em", "i", "span", "p"]):
            if cls._excluded(label, scope) or not cls._is_post_date_label(label.get_text()):
                continue
            yield from cls._label_values(label)

        # (2) 라벨 없는 <time>. 기간·행사 맥락이면 받지 않는다.
        for t in scope.find_all("time"):
            if cls._excluded(t, scope) or cls._in_marked_block(t, scope, _NON_POSTING_LABELS):
                continue
            text = clean_text(t.get_text(" "))
            if cls._is_period_text(text):
                continue
            yield t.get("datetime", "")
            yield text

        # (3) day/date 계열 클래스를 가진 전용 요소.
        for el in scope.find_all(True):
            if cls._excluded(el, scope) or not cls._has_date_class(el):
                continue
            text = clean_text(el.get_text(" "))
            if cls._is_period_text(text) or cls._in_marked_block(el, scope, _NON_POSTING_LABELS):
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
        if any(m in label for m in _NON_POSTING_LABELS + _RANGE_MARKERS):
            return False
        return label in _POST_DATE_LABELS

    @staticmethod
    def _is_period_text(text: str) -> bool:
        """값 텍스트 자체가 기간을 나타내는지('2026-07-24 ~ 2026-08-13')."""
        return any(m in text for m in _PERIOD_LABELS + _RANGE_MARKERS)

    @classmethod
    def _in_marked_block(cls, el, scope, markers) -> bool:
        """후보가 속한 '가지'의 라벨이 게시일 아님을 가리키면 True.

        조상의 전체 텍스트를 보면 안 된다. 예고기간 블록과 게시일 블록은 보통 형제로
        나란히 놓이는데, 둘의 공용 래퍼(.cont)에 닿는 순간 그 텍스트에 '예고기간'이
        섞여 있어 멀쩡한 게시일까지 버려진다. 제목·첨부도 같은 이유로 오염원이다
        ('사업보고서 제출 기한 연장' 같은 제목 하나로 그 항목 날짜가 사라진다).

        그래서 scope 까지 조상을 올라가되, 각 단계에서 후보가 든 가지 '바로 앞'의
        라벨만 본다 — `_preceding_label` 참고.
        """
        reject = tuple(markers) + _RANGE_MARKERS
        node = el
        while node is not None and node is not scope:
            parent = node.parent
            if parent is None or getattr(parent, "name", None) is None:
                return False
            label = cls._preceding_label(node, scope)
            if label:
                if any(m in label for m in reject):
                    return True
                if cls._is_post_date_label(label):
                    return False  # 가장 가까운 라벨이 게시일이면 더 볼 것 없다
            node = parent
        return False

    @classmethod
    def _preceding_label(cls, node, scope) -> str:
        """node 바로 앞에서 이 값을 설명하는 라벨 텍스트. 없으면 ''.

        앞 형제가 '라벨'인지 '별개 메타데이터 블록'인지는 태그 이름으로 가를 수 없다.
        같은 <p>·<div> 가 한 곳에선 라벨('예고기간')이고 다른 곳에선 자기 값을 품은
        블록('예고기간 2026-07-24 ~ 2026-08-13')이다. 태그를 열거하면 한쪽을 고치는
        순간 다른 쪽이 뚫린다. 그래서 내용으로 가른다:

        - 구분자만 있는 조각(':', '|', '-' …)은 건너뛰고 실제 라벨을 계속 찾는다.
          단 '~'는 이 값이 범위의 꼬리라는 뜻이라 건너뛰지 않는다.
        - 날짜를 품고 있으면 자기 값을 가진 별개 블록이므로 경계로 본다(이 후보의
          라벨이 아니다).
        - 제목·첨부 영역도 라벨이 아니다('제출 기한 연장' 같은 제목이 기간 라벨로
          오인된다).
        """
        for prev in node.previous_siblings:
            if getattr(prev, "name", None) is None:
                text = clean_text(str(prev))
                if not text or _SEPARATOR_ONLY.match(text):
                    continue
                return text
            if cls._excluded(prev, scope):
                return ""
            text = clean_text(prev.get_text(" "))
            if not text or _SEPARATOR_ONLY.match(text):
                continue
            if _DATE_ANYWHERE.search(text):
                return ""  # 자기 값을 품은 블록 = 경계
            return text
        return ""

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
        iso = _ISO_DATETIME.match(text)
        if iso:
            text = iso.group(1)
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
