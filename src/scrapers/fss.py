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

# --- 상세 본문 컨테이너 ---
# 2026-08 홈페이지 개편으로 보도자료 상세의 공식 웹 본문이 .krds-bd-view > .n-dbdata 로
# 옮겨졌다. 개편 후에는 기존 selector 가 하나도 맞지 않아 body 가 통째로 비었고,
# 그 결과 AI 3줄 요약 대상에서 빠졌다(요약은 본문이 있어야 돈다).
#
# 보도자료만 selector 목록을 따로 두는 이유:
#  - .n-dbdata 를 최우선으로 잡아야 제목·담당부서·문의전화·등록일·조회수·첨부파일명이
#    섞인 .krds-bd-view 전체를 본문으로 쓰지 않는다.
#  - 반대로 너무 넓은 #content 는 이 목록에서 뺀다. 개편된 페이지에서 #content 를
#    잡으면 좌측 메뉴·breadcrumb 같은 페이지 껍데기가 본문으로 들어가 요약이 망가진다.
#  - legacy selector 들은 구 홈페이지(및 아직 안 바뀐 페이지) 호환을 위해 뒤에 남긴다.
_PRESS_KEY = "fss_press"
_PRESS_BODY_SELECTORS = (".n-dbdata", ".view-cont", ".board-view", ".bbs-view")
# 보도자료 외 게시판은 기존 동작 그대로.
_BODY_SELECTORS = (".view-cont", ".board-view", ".bbs-view", ".cont", "#content")

# --- 검사결과 제재 상세의 구조화 추출 ---
# 이 게시판의 상세 페이지에서 눈에 보이는 정보는 사실상 아래 3개 항목뿐이고, 정작
# 제재 내용은 첨부 PDF 에 있다. 그런데 넓은 컨테이너(#content 등)를 통째로 긁으면
# 라벨·빈 제재내용 행·첨부 안내·주변 메뉴가 한 덩어리로 섞여, 메일 발췌가
# "금융기관명 ○○ 제재조치일 ... 관련부서 ... 제재조치내용 ..." 같은 잡음이 된다.
# 그래서 이 소스만 라벨-값 행에서 3개 항목을 그대로 뽑아 표로 보여준다.
_SANCTION_KEY = "fss_sanction"
_SANCTION_INSTITUTION = "금융기관명"
_SANCTION_DATE = "제재조치일"
_SANCTION_DEPT = "관련부서"
_SANCTION_LABELS = (_SANCTION_INSTITUTION, _SANCTION_DATE, _SANCTION_DEPT)
# 값 문자열 하나에서 날짜를 읽는다(20260723 / 2026-07-23 / 2026.07.23 / 2026년 7월 23일).
_VALUE_DATE = re.compile(r"(20\d{2})\D{0,2}(\d{1,2})\D{0,2}(\d{1,2})")
_WS_ALL = re.compile(r"\s+")


def _norm_label(s: str) -> str:
    """라벨 정규화 — 공백을 모두 지운 뒤 정확히 비교하기 위한 형태로 만든다."""
    return _WS_ALL.sub("", s or "")


def _iso_date(value: str) -> str:
    """값 문자열 하나에서 날짜를 읽어 YYYY-MM-DD 로. 못 읽으면 빈 문자열."""
    m = _VALUE_DATE.search(value or "")
    if not m:
        return ""
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return ""
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _body_text(soup: BeautifulSoup, selectors: tuple[str, ...]) -> str:
    """selectors 를 순서대로 시도해 처음으로 내용이 있는 컨테이너의 본문 텍스트.

    '맞는 요소'가 아니라 '내용이 있는 요소'를 고른다 — 앞선 selector 가 빈 껍데기로
    존재하기만 해도 뒤의 selector 를 보지 못하면 본문을 통째로 잃는다.
    """
    for sel in selectors:
        el = soup.select_one(sel)
        if el is None:
            continue
        text = clean_text(el.get_text("\n"))
        if text:
            return text
    return ""


def _label_value_pairs(soup: BeautifulSoup) -> dict[str, str]:
    """상세 페이지의 '라벨-값' 짝만 모은다: 한 tr 안의 th/td, 그리고 dt+dd.

    페이지 전체에서 '라벨 근처 텍스트'를 훑지 않는다 — 구조적으로 짝지어진 셀만
    본다. 같은 라벨이 여러 번 나오면 먼저 나온(=상세 표) 값을 쓴다.
    """
    pairs: dict[str, str] = {}

    def _put(label_el, value_el) -> None:
        label = _norm_label(label_el.get_text())
        if label and label not in pairs:
            pairs[label] = clean_text(value_el.get_text(" "))

    for row in soup.find_all("tr"):
        # 중첩 표의 셀이 바깥 행의 짝으로 끼어들지 않도록 이 tr 직속 셀만 쓴다.
        cells = [c for c in row.find_all(["th", "td"]) if c.find_parent("tr") is row]
        heads = [c for c in cells if c.name == "th"]
        values = [c for c in cells if c.name == "td"]
        for th, td in zip(heads, values):
            _put(th, td)

    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling()
        if dd is not None and dd.name == "dd":
            _put(dt, dd)

    return pairs


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
            # 검사결과 제재만: 라벨-값 3항목을 구조화해 담는다. 성공하면 body 는 비워
            # 두어(요약 대상에서 제외) 잡음 발췌도, LLM 할당량 소모도 없게 한다.
            # 실패하면 details 는 비고 아래의 기존 본문 추출로 되돌아간다.
            if post.source_key == _SANCTION_KEY:
                post.details = self._sanction_details(soup, post)
            if not post.details:
                is_press = post.source_key == _PRESS_KEY
                post.body = _body_text(
                    soup, _PRESS_BODY_SELECTORS if is_press else _BODY_SELECTORS
                )
                if is_press and not post.body:
                    # 상세 요청 자체는 성공했는데 신형·legacy selector 가 모두 빗나간
                    # 경우 = 또 한 번의 마크업 변경 신호. 진단할 수 있게 남기되,
                    # 발송 자체는 막지 않는다(기존 fail-soft 유지).
                    log.warning(
                        "[%s] 상세 본문 selector를 찾지 못함: post_id=%s url=%s",
                        self.key,
                        post.post_id,
                        post.url,
                    )
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

    def _sanction_details(self, soup: BeautifulSoup, post: Post) -> list[tuple[str, str]]:
        """검사결과 제재 상세에서 (금융기관명, 제재조치일, 관련부서)를 뽑는다.

        세 값을 모두 안전하게 만들 수 있을 때만 결과를 돌려준다. 하나라도 못 만들면
        빈 리스트 — 호출자가 기존 본문 추출로 되돌아가 정보를 잃지 않게 한다.

        금융기관명·제재조치일은 목록에서 이미 확인한 값(post.title/post.date)이 있으므로
        라벨이 없을 때 그대로 쓴다. 반면 관련부서는 목록 파싱이 보장하는 값이 아니라서
        반드시 라벨이 붙은 값에서만 가져온다(잘못된 셀을 부서로 싣지 않기 위함).
        """
        pairs = _label_value_pairs(soup)
        institution = pairs.get(_SANCTION_INSTITUTION, "") or post.title.strip()
        date = _iso_date(pairs.get(_SANCTION_DATE, "")) or _iso_date(post.date)
        dept = pairs.get(_SANCTION_DEPT, "")
        if not (institution and date and dept):
            log.info(
                "[%s] 제재 상세 구조화 실패(%s 중 누락) — 기존 본문 추출로 진행: %s",
                self.key,
                "/".join(_SANCTION_LABELS),
                post.url,
            )
            return []
        return [
            (_SANCTION_INSTITUTION, institution),
            (_SANCTION_DATE, date),
            (_SANCTION_DEPT, dept),
        ]

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
