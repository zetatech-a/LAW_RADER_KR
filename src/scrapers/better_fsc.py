"""금융규제·법령해석포털 (better.fsc.go.kr) 회신사례 스크래퍼.

목록은 DataTables 서버사이드 방식(POST → JSON)이다(라이브 확인).
  엔드포인트: /fsc_new/replyCase/selectReplyCaseTotalReplyList.do  (POST, JSON)
  레코드 필드: rownumber, pastreqType(구분), title, replyRegDate, dataIdx(안정 ID)

상세는 목록에서 JS 함수(openReplyCasePastReqDetail)로 열리지만, 그 함수가 결국
여는 페이지는 구분(pastreqType)별로 아래 GET 주소다.
  법령해석      /fsc_new/replyCase/LawreqDetail.do?...&lawreqIdx={dataIdx}
  비조치의견서  /fsc_new/replyCase/OpinionDetail.do?...&opinionIdx={dataIdx}
두 주소의 GET 상세 조회와 질의요지·회답·이유 존재는 외부 리뷰에서 실제 페이지로
확인했다. 그 밖의 구분('현장건의 과제' 등)은 상세 주소가 확인되지 않았으므로
**추측하지 않고** 예전처럼 통합조회 목록 URL 을 링크로 둔다(제목만 통지).

아직 라이브로 확인하지 못한 가정 두 가지(머지 전 확인 대상 — 아래 가드는 이 가정을
검증해 주는 것이 아니라, 가정이 틀렸을 때 잘못된 콘텐츠가 발송되는 것을 막는다):
  A. 목록 JSON 의 dataIdx 가 lawreqIdx/opinionIdx 와 실제로 같은 값인지.
  B. OpinionDetail.do 에 통합조회 목록의 muNo=117 을 넣어도 정상 조회되는지
     (공개된 일반 비조치의견서 URL 에서는 muNo=86 도 쓰인다).

그래서 상세 응답을 받으면 본문·첨부를 붙이기 **전에** 목록 record 와 같은 글인지
확인한다(_identity_ok — 제목·회신일·구분↔endpoint). 확인되지 않으면 아무것도 붙이지
않고 제목·링크만 통지한다. 잘못된 회답을 다른 제목 밑에 보내는 것이 본문이 비는 것보다
훨씬 나쁘기 때문에 fail-open 하지 않는다.

상세 페이지에서는 질의요지·회답·이유를 뽑아 post.body 에 담는다. **세 항목이 모두
있을 때만** 담는다 — 회답이 빠진 질의만으로 3줄 요약을 만들면 결론을 지어낸 요약이
메일에 실린다. 하나라도 없으면 body 를 비워 두고(요약 대상에서 제외) 어떤 항목이
없는지 warning 을 남긴다. 첨부 수집은 그와 무관하게 계속한다.

details 에는 담지 않는다. summarizer 의 실제 호출 경로(_summarize_general)는 details
를 보지 않고 _prepare_body(=body 길이)만으로 대상을 고르므로 details 를 채워도 요약
호출 자체는 일어난다. 다만 notifier 가 details 를 summary 보다 먼저 렌더하므로
(_summary_block) 그 요약이 메일에 보이지 않고, 집계 로그의 ai_target_count 는 details
가 있는 글을 대상에서 빼 로그와 실제 호출이 어긋난다. 그래서 body 만 쓴다.
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

# 상세 URL 파일명 → 목록의 구분. 목록 record 와 상세 endpoint 가 같은 유형을 가리키는지
# 확인하는 데 쓴다(_identity_ok).
_FILE_TO_GUBUN = {f.lower(): g for g, (f, _) in _DETAIL_ENDPOINTS.items()}

# 동일 게시물 확인에 쓰는 회신일 라벨. 페이지 어딘가의 날짜를 주워 쓰지 않고, 이 라벨이
# 붙은 값만 본다(_reply_date).
_REPLY_DATE_LABELS = ("회신일", "회신일자")

# 라벨이 담길 만한 요소. 라벨 헤딩을 찾을 때 훑는 범위이며, 이 밖의 태그는 보지 않는다.
_LABEL_HOST_TAGS = (
    "h2", "h3", "h4", "h5", "h6", "strong", "b", "p", "span", "div", "li", "dt", "th"
)

# heading 폴백에서 본문 수집을 멈출 경계 태그.
#
# 실제 상세는 표 구조라 이 폴백은 보조 경로다. 그런데 마지막 항목('이유') 뒤에는 다음
# 라벨이 없어서, 같은 부모 아래 이어지는 첨부 목록·목록/이전글/다음글 버튼·URL 복사·
# 푸터가 통째로 '이유' 본문에 붙어 Gemini 에 법률적 이유로 전달된다. 그래서 fail-closed
# 로 끊는다 — 링크·폼·버튼·내비게이션/푸터가 들어 있는 형제를 만나면 그 앞에서 멈춘다.
# (회신문의 이유 서술에는 하이퍼링크·입력요소가 들어가지 않는다. 반면 뒤따르는 조작
#  영역은 예외 없이 링크나 버튼이다.) 클래스 이름은 추측하지 않는다.
_BOUNDARY_TAGS = (
    "a", "nav", "footer", "header", "form", "button", "input", "select", "textarea", "hr"
)

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


def _norm_ws(text: str) -> str:
    """공백만 정규화한다(NBSP 포함). 문자 자체는 건드리지 않는다.

    제목 대조용이다. 문장부호까지 지우면 서로 다른 게시물이 같은 제목으로 보일 수
    있으므로, 줄바꿈·중복 공백 같은 무해한 표기 차이만 흡수한다.
    (파이썬 정규식의 공백 클래스는 유니코드 모드라 NBSP·전각 공백도 포함한다.)
    """
    return _WS_ALL.sub(" ", text or "").strip()


def _date_digits(text: str) -> str:
    """날짜 문자열에서 YYYYMMDD 8자리만. 못 읽으면 빈 문자열.

    '2026-08-20' / '2026.08.20' / '2026년 8월 20일' 같은 표기 차이를 흡수한다.
    """
    digits = re.sub(r"\D", "", text or "")
    return digits[:8] if len(digits) >= 8 else ""


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

    def supports_enrich(self, post: Post) -> bool:
        """상세 주소가 확인된 구분(법령해석·비조치의견서)의 글만 상세 수집 대상이다.

        나머지 구분은 목록 URL 이 링크라 본문·첨부가 비는 것이 정상이므로, main 이
        상세 수집 통계에서 빼도록 False 를 돌려준다.
        """
        return self._is_detail_url(post.url)

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

        # 본문·첨부를 붙이기 **전에** 이 응답이 목록의 그 글이 맞는지 확인한다.
        # 확인되지 않으면 아무것도 붙이지 않고 끝낸다(첨부 다운로드도 하지 않는다).
        if not self._identity_ok(soup, post):
            return

        sections = self._sections(soup)
        missing = [label for label in _BODY_LABELS if label not in sections]
        if missing:
            # 부분 본문은 요약 입력으로 쓰지 않는다 — 회답 없는 질의만 넘기면 Gemini 가
            # 결론을 지어낸다. 본문을 비우면 기존 폴백(제목·링크)으로 안전하게 나간다.
            log.warning(
                "[%s] 상세에서 %s 를 찾지 못해 본문을 비웁니다 — 마크업 확인 필요: %s",
                self.key,
                "·".join(missing),
                post.url,
            )
        else:
            post.body = "\n\n".join(f"[{label}]\n{sections[label]}" for label in _BODY_LABELS)

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

    def enrich_succeeded(self, post: Post) -> bool:
        """이 소스의 계약상 상세 수집이 성공했는가 — 본문이 만들어졌을 때만 True.

        기본 판정(본문·첨부·구조화항목 중 하나)을 그대로 쓰면, 마크업이 바뀌어 세 항목을
        모두 놓쳤는데 첨부 링크만 살아 있는 상태가 '성공'으로 잡힌다. 그러면 질의·회답
        없이 제목과 파일만 실린 메일이 계속 나가는데도 상세 수집 통계는 초록불이라
        고장이 묻힌다. 이 소스는 세 항목이 모두 있을 때만 body 를 만들므로 body 유무가
        곧 계약 충족 여부다.
        """
        return bool(post.body)

    # --- 동일 게시물 검증 ---
    def _identity_ok(self, soup: BeautifulSoup, post: Post) -> bool:
        """상세 응답이 목록의 그 글이 맞는지 fail-closed 로 검증한다.

        목록 dataIdx 를 lawreqIdx/opinionIdx 로 쓰는 매핑은 아직 라이브로 확인되지
        않았다. 그 가정이 틀리거나 포털이 잘못된 idx 를 다른 정상 페이지로 돌려주면,
        목록 A 의 제목 밑에 상세 B 의 회답·첨부가 실린다. 법령해석 알림에서 그것은
        본문이 비는 것보다 훨씬 나쁘므로, 확인되지 않으면 통과시키지 않는다.

        검증은 지금 Post 가 이미 가진 값(title/date/url)만으로 한다.

        상세 페이지가 스스로 '법령해석/비조치의견서'라고 찍는지는 보지 않는다 —
        공개된 LawreqDetail URL 중에는 muNo 에 따라 '규제입증책임제' 같은 다른 메뉴
        맥락으로 렌더되는 것이 있어(검색 색인에서 확인) 유형 단어가 페이지에 없을 수
        있다. 대신 목록 구분(제목 접두어)과 URL endpoint 가 서로 맞는지를 본다.
        """
        filename = urlparse(post.url).path.rsplit("/", 1)[-1].lower()
        gubun = _FILE_TO_GUBUN.get(filename, "")
        if not gubun:
            return False  # 확인된 endpoint 가 아니면 검증 근거 자체가 없다

        title = clean_text(post.title)
        prefix = f"[{gubun}]"
        if title.startswith(prefix):
            title = title[len(prefix):].strip()      # 우리가 붙인 접두어만 제거
        elif title.startswith("["):
            log.warning(
                "[%s] 목록 구분과 상세 endpoint 가 어긋남(%s ↔ %s) — 건너뜁니다: %s",
                self.key, post.title, gubun, post.url,
            )
            return False
        if not title:
            return False

        page_text = _norm_ws(soup.get_text(" "))
        if _norm_ws(title) not in page_text:
            log.warning(
                "[%s] 상세에 목록 제목이 없음 — 다른 게시물일 수 있어 건너뜁니다: %s (%s)",
                self.key, title, post.url,
            )
            return False

        want = _date_digits(post.date)
        if want:
            got = self._reply_date(soup)
            if not got:
                log.warning(
                    "[%s] 상세에서 %s 를 찾지 못해 동일 게시물 확인 불가 — 건너뜁니다: %s",
                    self.key, "/".join(_REPLY_DATE_LABELS), post.url,
                )
                return False
            if got != want:
                log.warning(
                    "[%s] 상세 회신일(%s)이 목록(%s)과 다름 — 다른 게시물이므로 "
                    "건너뜁니다: %s",
                    self.key, got, want, post.url,
                )
                return False
        return True

    @classmethod
    def _reply_date(cls, soup: BeautifulSoup) -> str:
        """회신일 라벨이 붙은 값을 읽어 YYYYMMDD 로. 없으면 빈 문자열.

        페이지 전체에서 날짜를 긁지 않는다 — 푸터·본문에 우연히 있는 날짜가 통과하면
        검증이 무의미해진다. 본문 추출과 같은 두 배치만 본다: 구조적 라벨-값 짝(th/td,
        dt+dd)을 먼저 보고, 없으면 라벨만 든 요소의 바로 다음 형제를 본다.
        """
        for label, value in cls._label_pairs(soup):
            if _norm_label(label) in _REPLY_DATE_LABELS:
                digits = _date_digits(value)
                if digits:
                    return digits
        for el in soup.find_all(list(_LABEL_HOST_TAGS)):
            if _norm_label(el.get_text(" ")) not in _REPLY_DATE_LABELS:
                continue
            sib = el.find_next_sibling()
            if sib is None:
                continue
            digits = _date_digits(clean_text(sib.get_text(" ")))
            if digits:
                return digits
        return ""

    @staticmethod
    def _is_error_page(soup: BeautifulSoup) -> bool:
        text = clean_text(soup.get_text(" "))
        return any(m in text for m in _ERROR_MARKERS)

    # --- 본문 추출 ---
    def _sections(self, soup: BeautifulSoup) -> dict[str, str]:
        """찾아낸 {라벨: 본문}. 못 찾은 항목은 키 자체가 없다.

        세 항목이 실제 페이지에 있다는 것은 확인했지만 raw 마크업(태그 구조)까지는
        확인하지 못해 두 배치를 모두 본다.
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
        return found

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

    @classmethod
    def _heading_sections(cls, soup: BeautifulSoup) -> Iterator[tuple[str, str]]:
        """텍스트가 라벨 '뿐'인 요소를 찾아, 다음 경계 전까지의 형제를 본문으로 낸다.

        경계는 두 가지다: 다음 본문 라벨, 그리고 조작·내비게이션 영역(_is_boundary).
        마지막 항목('이유')에는 뒤따르는 라벨이 없으므로 후자가 없으면 첨부 목록·
        목록/이전글 버튼·푸터까지 법률적 '이유' 본문으로 끌려 들어간다.
        """
        for el in soup.find_all(list(_LABEL_HOST_TAGS)):
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
                if cls._is_boundary(sib):
                    break  # 첨부·버튼·내비게이션·푸터 = 본문의 끝
                text = clean_text(sib.get_text("\n"))
                if text:
                    parts.append(text)
            body = clean_text("\n".join(parts))
            if body:
                yield key, body

    @staticmethod
    def _is_boundary(el) -> bool:
        """이 형제부터는 본문이 아니라 조작·내비게이션 영역인지."""
        return el.name in _BOUNDARY_TAGS or el.find(list(_BOUNDARY_TAGS)) is not None

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
