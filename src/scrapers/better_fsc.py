"""금융규제·법령해석포털 (better.fsc.go.kr) 회신사례 스크래퍼.

목록은 DataTables 서버사이드 방식(POST → JSON)이다(라이브 확인).
  엔드포인트: /fsc_new/replyCase/selectReplyCaseTotalReplyList.do  (POST, JSON)
  레코드 필드: rownumber, pastreqType(구분), title, replyRegDate, dataIdx(안정 ID)

상세는 목록에서 JS 함수(openReplyCasePastReqDetail)로 열리지만, 그 함수가 결국
여는 페이지는 구분(pastreqType)별로 아래 GET 주소이며 목록의 dataIdx 를 그대로
쓴다. 두 주소 모두 공개 색인된 실제 URL 로 확인했다.
  법령해석      /fsc_new/replyCase/LawreqDetail.do?...&lawreqIdx={dataIdx}
  비조치의견서  /fsc_new/replyCase/OpinionDetail.do?...&opinionIdx={dataIdx}
그 밖의 구분('현장건의 과제' 등)은 상세 주소가 확인되지 않았으므로 **추측하지 않고**
예전처럼 통합조회 목록 URL 을 링크로 둔다(본문·첨부 없이 제목만 통지).

상세 페이지에서는 질의요지·회답·이유를 뽑아 post.body 에 담는다. details 에는 담지
않는다 — notifier 는 details 가 있으면 그것만 싣고(_summary_block), summarizer 는
body 길이로만 요약 대상을 고르므로(_prepare_body) details 를 채우면 이 소스가 기존
Gemini 3줄 요약 경로에서 빠진다.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterator
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from ..fetcher import AttachmentTooLarge
from ..models import Attachment, Post
from .base import BaseScraper, clean_text

log = logging.getLogger(__name__)

_AJAX_URL = "https://better.fsc.go.kr/fsc_new/replyCase/selectReplyCaseTotalReplyList.do"

# 목록의 구분(pastreqType) → (상세 페이지 파일명, dataIdx 를 실어 보낼 파라미터명).
# 여기 없는 구분은 상세 주소가 확인되지 않은 것이므로 URL 을 만들지 않는다.
_DETAIL_ENDPOINTS: dict[str, tuple[str, str]] = {
    "법령해석": ("LawreqDetail.do", "lawreqIdx"),
    "비조치의견서": ("OpinionDetail.do", "opinionIdx"),
}
_DETAIL_FILES = frozenset(f.lower() for f, _ in _DETAIL_ENDPOINTS.values())

# 상세 URL 에 그대로 옮겨 실을 목록 URL 의 내비게이션 파라미터(순서 유지).
# 목록 URL(config.yaml)에 있는 값만 옮긴다 — 없는 값을 지어내지 않는다.
_NAV_PARAMS = ("stNo", "muNo", "muGpNo")

# 상세에서 뽑을 항목. 리스트 순서가 곧 body 출력 순서다.
_BODY_LABELS = ("질의요지", "회답", "이유")

# 포털의 공식 첨부 다운로드 경로(이 계열만 내려받는다).
_FILE_PATH = "/file/displayfile.do"

# 정상 콘텐츠 대신 포털 오류 페이지가 온 경우의 표식. 이 문구가 body 로 저장되어
# Gemini 요약 입력이 되면 안 된다.
_ERROR_MARKERS = (
    "ERROR PAGE",
    "요청하신 페이지는 사용할 수 없거나 찾을 수 없는 페이지",
)

_WS_ALL = re.compile(r"\s+")
# 라벨 앞뒤의 번호·불릿·괄호·구분기호. '□ 질의요지', '[회답]', '2. 이유' 를 모두 같은
# 라벨로 보기 위한 정리용이다.
_LABEL_LEAD = re.compile(r"^[\[\(<{【《〔◇□■○●▶▷▣※◎·•*\-–—.,0-9]+")
_LABEL_TAIL = re.compile(r"[\]\)>}】》〕:：.,·]+$")
# 첨부 앵커 텍스트 뒤에 붙는 크기 표기('(42 KB)').
_SIZE_SUFFIX = re.compile(r"\s*[\(\[]\s*[\d.,]+\s*[KMG]?B\s*[\)\]]\s*$", re.IGNORECASE)


def _norm_label(text: str) -> str:
    """라벨 정규화 — 공백·번호·불릿·괄호를 걷어내 정확 비교할 수 있게 만든다."""
    s = _WS_ALL.sub("", text or "")
    s = _LABEL_LEAD.sub("", s)
    return _LABEL_TAIL.sub("", s)


class BetterReplyScraper(BaseScraper):
    # POST(start 오프셋) 기반 페이지네이션 — PAGE_PARAM 은 없지만 page 인자로 페이지네이션.
    PAGE_PARAM = None
    SUPPORTS_PAGINATION = True

    def fetch_list(self, limit: int, page: int = 1) -> list[Post]:
        start = (max(1, page) - 1) * limit
        # DataTables 서버사이드 표준 파라미터 + 이 화면의 커스텀 검색 파라미터
        payload = {
            "draw": "1",
            "start": str(start),
            "length": str(limit),
            "searchKeyword": "",
            "searchCondition": "",
            "searchType": "",
        }
        try:
            resp = self.fetcher.post(_AJAX_URL, data=payload, referer=self.list_url)
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            # 전송/JSON 오류는 예외로 전파(빈 결과로 삼키면 collect 가 '목록 끝'으로
            # 오인해 백필 커서를 초기화하고 이후 페이지를 영구히 건너뛴다).
            raise RuntimeError(f"회신사례 목록 POST 실패: {type(e).__name__}") from e

        records = self._records(data)
        if not records:
            log.warning("[%s] JSON 파싱 0건 — 응답 구조 확인 필요. 디버그 덤프.", self.key)
            self._dump_debug("list", json.dumps(data, ensure_ascii=False, indent=2))
            return []

        posts: list[Post] = []
        seen: set[str] = set()
        for rec in records:
            pid = self._record_id(rec)
            title = clean_text(str(rec.get("title", "")))
            if not pid or not title or pid in seen:
                continue
            seen.add(pid)
            gubun = clean_text(str(rec.get("pastreqType", ""))).replace("(2014이전)", "")
            posts.append(
                Post(
                    source_key=self.key,
                    source_name=self.name,
                    post_id=pid,
                    title=f"[{gubun}] {title}" if gubun else title,
                    # 상세 주소가 확인된 구분만 상세로, 나머지는 종전대로 목록으로.
                    url=self._detail_url(rec) or self.list_url,
                    date=clean_text(str(rec.get("replyRegDate", ""))),
                )
            )
        return posts[:limit]

    @staticmethod
    def _records(data) -> list:
        if isinstance(data, dict):
            for key in ("data", "aaData", "list", "rows", "resultList"):
                if isinstance(data.get(key), list):
                    return data[key]
        if isinstance(data, list):
            return data
        return []

    @staticmethod
    def _record_id(rec: dict) -> str:
        for k in ("dataIdx", "replyRegSn", "sn", "seq", "idx"):
            v = rec.get(k)
            if v not in (None, ""):
                return f"{k}:{v}"
        # 폴백: 제목+일자의 '결정적' 해시. Python 의 내장 hash() 는 프로세스마다 시드가
        # 달라(PYTHONHASHSEED) 실행 때마다 값이 바뀌므로, 같은 글이 매번 신규로 오인된다.
        t = str(rec.get("title", "")).strip()
        d = str(rec.get("replyRegDate", "")).strip()
        if not t:
            return ""
        digest = hashlib.md5(f"{t}|{d}".encode("utf-8")).hexdigest()[:16]
        return f"h:{digest}"

    # --- 상세 URL ---
    def _detail_url(self, rec: dict) -> str:
        """구분별 상세 페이지 URL. 확인되지 않은 구분이면 빈 문자열.

        '(2014이전)' 이 붙은 구분은 같은 dataIdx 가 같은 상세 화면을 여는지 확인되지
        않았으므로 대상에서 뺀다(제목 표기용 gubun 과 달리 원본 값으로 정확 비교).
        """
        endpoint = _DETAIL_ENDPOINTS.get(clean_text(str(rec.get("pastreqType", ""))))
        if not endpoint:
            return ""
        idx = clean_text(str(rec.get("dataIdx", "")))
        if not idx.isdigit():
            return ""
        filename, param = endpoint
        query = parse_qs(urlparse(self.list_url).query)
        params = [(k, query[k][-1]) for k in _NAV_PARAMS if query.get(k)]
        params.append((param, idx))
        return f"{urljoin(self.list_url, filename)}?{urlencode(params)}"

    @staticmethod
    def _is_detail_url(url: str) -> bool:
        """상세 수집 대상인 URL 인지(목록 URL 폴백이면 False)."""
        return urlparse(url or "").path.rsplit("/", 1)[-1].lower() in _DETAIL_FILES

    # --- 상세 수집 ---
    def enrich(self, post: Post) -> None:
        """상세에서 질의요지·회답·이유와 첨부를 채운다.

        한 건의 실패가 나머지 수집을 막지 않도록 모든 실패는 로그로만 남기고 넘어간다
        (본문 없이 제목·링크만 나가는 것이 최악의 결과가 아니다).
        """
        if not self._is_detail_url(post.url):
            return  # 상세 주소가 확인되지 않은 구분 — 목록 링크로만 통지(기존 동작)

        try:
            resp = self.fetcher.get(post.url, referer=self.list_url)
            html = self.fetcher.text(resp)
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] 상세 로드 실패 %s: %s", self.key, post.url, e)
            return

        soup = BeautifulSoup(html, "lxml")
        if self._is_error_page(soup):
            # 오류 문구가 body 가 되어 Gemini 요약 입력이 되면 안 된다.
            log.warning("[%s] 상세가 포털 ERROR PAGE — 본문 없이 진행: %s", self.key, post.url)
            return

        sections = self._sections(soup)
        if sections:
            post.body = "\n\n".join(f"[{label}]\n{text}" for label, text in sections)
        else:
            log.warning(
                "[%s] 상세에서 %s 를 찾지 못함 — 마크업 확인 필요: %s",
                self.key,
                "·".join(_BODY_LABELS),
                post.url,
            )

        self._collect_attachments(soup, post)
        for att in post.attachments:
            if att.data is not None:
                continue
            try:
                att.data = self.fetcher.download(att.url, referer=post.url)
            except AttachmentTooLarge as e:
                # 용량 초과는 실패가 아니다 — 파일명·링크는 그대로 남겨 메일에 안내한다.
                log.info("[%s] 첨부 용량 초과 — 링크만 유지 %s: %s", self.key, att.filename, e)
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] 첨부 다운로드 실패 %s: %s", self.key, att.url, e)

    @staticmethod
    def _is_error_page(soup: BeautifulSoup) -> bool:
        text = clean_text(soup.get_text(" "))
        return any(m in text for m in _ERROR_MARKERS)

    # --- 본문 추출 ---
    def _sections(self, soup: BeautifulSoup) -> list[tuple[str, str]]:
        """질의요지·회답·이유를 (라벨, 본문) 으로. 못 찾은 항목은 뺀다.

        마크업을 라이브로 확인할 수 없어 두 배치를 모두 본다.
          1) 구조적 라벨-값 짝(tr 안의 th/td, dt+dd)
          2) 라벨만 든 요소 뒤에 본문이 형제로 오는 배치
        먼저 찾은 값을 쓴다.
        """
        found: dict[str, str] = {}
        for label, value in self._label_pairs(soup):
            key = _norm_label(label)
            if key in _BODY_LABELS and key not in found and value:
                found[key] = value
        if len(found) < len(_BODY_LABELS):
            for key, value in self._heading_sections(soup):
                if key not in found and value:
                    found[key] = value
        return [(label, found[label]) for label in _BODY_LABELS if label in found]

    @staticmethod
    def _label_pairs(soup: BeautifulSoup) -> Iterator[tuple[str, str]]:
        """구조적으로 짝지어진 (라벨 텍스트, 값 텍스트) 만 낸다.

        페이지 전체에서 '라벨 근처 텍스트'를 훑지 않는다 — 중첩 표의 셀이 바깥 행의
        짝으로 끼어들지 않도록 각 tr 직속 셀만 본다.
        """
        for row in soup.find_all("tr"):
            cells = [c for c in row.find_all(["th", "td"]) if c.find_parent("tr") is row]
            heads = [c for c in cells if c.name == "th"]
            values = [c for c in cells if c.name == "td"]
            for th, td in zip(heads, values):
                yield th.get_text(" "), clean_text(td.get_text("\n"))
        for dt in soup.find_all("dt"):
            dd = dt.find_next_sibling()
            if dd is not None and dd.name == "dd":
                yield dt.get_text(" "), clean_text(dd.get_text("\n"))

    @staticmethod
    def _heading_sections(soup: BeautifulSoup) -> Iterator[tuple[str, str]]:
        """텍스트가 라벨 '뿐'인 요소를 찾아, 다음 라벨 전까지의 형제를 본문으로 낸다."""
        for el in soup.find_all(
            ["h2", "h3", "h4", "h5", "h6", "strong", "b", "p", "span", "div", "li", "dt", "th"]
        ):
            key = _norm_label(el.get_text(" "))
            if key not in _BODY_LABELS:
                continue
            parts: list[str] = []
            for sib in el.next_siblings:
                if getattr(sib, "name", None) is None:
                    text = clean_text(str(sib))
                    if text:
                        parts.append(text)
                    continue
                if _norm_label(sib.get_text(" ")) in _BODY_LABELS:
                    break  # 다음 항목의 라벨 = 이 항목의 끝
                text = clean_text(sib.get_text("\n"))
                if text:
                    parts.append(text)
            body = clean_text("\n".join(parts))
            if body:
                yield key, body

    # --- 첨부 ---
    def _collect_attachments(self, soup: BeautifulSoup, post: Post) -> None:
        """상세의 공식 첨부 링크(/file/displayFile.do)만 수집. 중복·외부 origin 제외."""
        origin = urlparse(self.list_url).netloc.lower()
        seen = {a.url for a in post.attachments}
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if not href or href.lower().startswith("javascript"):
                continue
            url = urljoin(post.url, href)
            parsed = urlparse(url)
            if not parsed.path.lower().endswith(_FILE_PATH):
                continue
            if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != origin:
                log.warning("[%s] 외부 origin 첨부 링크 무시: %s", self.key, url)
                continue
            if url in seen:
                continue
            seen.add(url)
            post.attachments.append(
                Attachment(filename=self._filename(anchor), url=url)
            )

    @staticmethod
    def _filename(anchor) -> str:
        """앵커가 제공하는 정보에서 파일명을 뽑는다(크기 표기 '(42 KB)' 는 제거)."""
        for raw in (anchor.get_text(" "), anchor.get("title") or "", anchor.get("download") or ""):
            name = _SIZE_SUFFIX.sub("", clean_text(raw)).strip()
            if name:
                return name
        return "첨부파일"
