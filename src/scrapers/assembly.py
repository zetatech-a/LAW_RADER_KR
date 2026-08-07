"""의안정보시스템 계류의안 스크래퍼 — 열린국회정보 Open API 사용.

계류의안 목록은 웹페이지가 AJAX 로 렌더링해 정적 HTML 로는 수집이 어렵다.
그래서 열린국회정보(open.assembly.go.kr) Open API 를 사용한다.

필요:
  - 환경변수/Secret: ASSEMBLY_API_KEY  (열린국회정보에서 발급받은 인증키)
  - config.yaml 의 assembly 소스 extra 로 엔드포인트/서비스/대수 조정 가능:
      api_endpoint: 기본 https://open.assembly.go.kr/portal/openapi
      api_service:  의안 목록 서비스명 (발급 API 에 맞게)
      age:          대수 (기본 22)
      page_size:    페이지당 건수 (기본 30)

응답(JSON) 표준 형식:
  { "<service>": [ {"head":[...]}, {"row":[ {..필드..}, ... ]} ] }
필드명은 서비스마다 다를 수 있어 흔한 후보를 순서대로 시도하고, 파싱 실패 시
원본 JSON 을 debug/ 에 덤프한다(→ 한 번의 verify 로 정확한 필드 확정).

enrich() 는 상세 페이지에서 '제안이유 및 주요내용'을 가져와 post.body 에 담는다.
이 본문은 의안 전용 배치 요약(src/assembly_summary.py)의 입력이 되고, 요약이 실패하면
메일에서 발췌 폴백으로 쓰인다.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ..models import Post, ProposalContentStatus
from .base import BaseScraper, clean_text

log = logging.getLogger(__name__)

# 상세 URL 템플릿.
#
# 2026-08 라이브 확인 결과:
#   - Open API 의 LINK_URL 은 아직 구 경로 /bill/billDetail.do 를 돌려주는데, 최신
#     의안에서 그 경로는 "해당 의안 정보가 존재하지 않습니다" 를 응답한다(redirect 없음).
#   - 현재 경로 /bill/bi/billDetailPage.do 는 HTTP 200 이다.
# 그래서 LINK_URL 을 그대로 믿지 않고 BILL_ID 로 현재 경로를 다시 만든다(canonicalize).
# config.yaml 의 assembly 소스에 detail_url 로 덮어쓸 수 있다.
_DETAIL = "https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId={bill_id}"

# LINK_URL 이 이 경로들로 오면 무시하고 위 템플릿으로 다시 만든다. 경로 목록을 좁게
# 유지하는 이유: 우리가 모르는 다른 상세 경로(예: 예산안·청원 전용)가 오면 그것은
# 그대로 써야 하기 때문이다. '아는 죽은 경로'만 갈아끼운다.
_LEGACY_DETAIL_PATHS = frozenset(
    {"/bill/billdetail.do", "/bill/jsp/billdetail.jsp"}
)

# --- 제안이유 및 주요내용 수집 (2026-08 Playwright 캡처로 확정된 계약) ---
#
#   1) 상세 GET  : billDetailPage.do?billId=...  (초기 HTML 에는 제안이유가 없다)
#   2) 후속 POST : billInfo.do
#        payload : 상세 HTML form#form 의 named hidden input 전체(URL-encoded)
#        header  : X-CSRF-TOKEN = meta[name="_csrf"], Referer = 상세 URL
#        쿠키    : 같은 세션(Fetcher 가 requests.Session 을 공유한다)
#        응답    : HTML, 본문은 pre#prntSummary
#
# form#form 은 payload '원천'일 뿐이다 — 그 폼의 빈 action·기본 GET 을 replay 하면
# 안 된다. JavaScript 가 폼을 serialize 한 뒤 별도 endpoint 로 POST 한다.
_BILLINFO_ENDPOINT = "https://likms.assembly.go.kr/bill/bi/bill/detail/billInfo.do"
_PAYLOAD_FORM_SELECTOR = "form#form"
_CSRF_META_SELECTOR = 'meta[name="_csrf"]'
_CSRF_HEADER = "X-CSRF-TOKEN"

# billInfo.do 응답에서 인정하는 **유일한** 본문 selector(확정 계약, fail-closed).
_BILLINFO_SUMMARY_SELECTOR = "pre#prntSummary"

# 초기 상세 GET 에만 쓰는 selector 목록. 구형(inline) 페이지 지원용 폴백을 포함한다.
_SUMMARY_SELECTORS = (_BILLINFO_SUMMARY_SELECTOR, "#prntSummary", "#summaryContentDiv")


class SummaryRequestError(ValueError):
    """확정된 계약대로 요청을 만들 수 없음(form#form·CSRF·billId 문제).

    이 예외가 나면 **요청을 보내지 않고** ERROR 로 처리한다. 근거가 어긋난 요청을
    보내면 남의 의안 본문을 받거나 서버에 400 을 반복해서 던지게 된다.
    """


# 상세 페이지에는 값이 채워지기 전의 빈 컨테이너가 먼저 있을 수 있다. 공백·안내문
# 수준의 짧은 텍스트를 '수집 성공'으로 보면 요약도 발췌도 못 쓰는 본문이 실린다.
_MIN_SUMMARY_CHARS = 20

# 상세 페이지가 '그 의안이 없다'고 답하는 경우(죽은 구 경로 등). 이건 등록 대기가
# 아니라 우리가 잘못된 주소를 부른 것이므로 ERROR 다.
_NOT_FOUND_MARKERS = (
    "의안정보가존재하지않",
    "의안정보가없습니다",
    "해당의안정보가존재하지않",
    "페이지를찾을수없",
)

_WS_ALL = re.compile(r"\s+")


class SummaryProbe(str, Enum):
    """제안이유 컨테이너를 들여다본 결과.

    이 세 값이 곧 상태 판정의 근거다:
      FOUND      쓸 만한 본문이 있다            → AVAILABLE
      EMPTY      컨테이너는 있는데 비어 있다     → PENDING (구조는 멀쩡함이 확인됨)
      MISSING    컨테이너 자체가 없다            → ERROR (셀렉터/구조 변경)
      UNEXPECTED 컨테이너에 알 수 없는 내용      → ERROR (예상하지 못한 응답)
    """

    FOUND = "found"
    EMPTY = "empty"
    MISSING = "missing"
    UNEXPECTED = "unexpected"


# 상세 URL canonicalize 에 쓰는 호스트.
_ALLOWED_HOST = "likms.assembly.go.kr"

# 디버그 덤프 파일명에 bill_id 를 그대로 쓰면 경로 조작이 될 수 있다.
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def _canonical_detail_url(link: str, bill_id: str, template: str) -> str:
    """상세 URL 을 현재 유효한 경로로 맞춘다.

    Open API 의 LINK_URL 이 죽은 구 경로(/bill/billDetail.do)를 돌려주므로, 그 경로가
    오면 BILL_ID 로 현재 경로를 다시 만든다. 그 외의 LINK_URL 은 공식 값이므로
    존중한다 — 우리가 모르는 상세 경로를 임의로 갈아끼우면 오히려 깨진다.
    """
    link = (link or "").strip()
    if not link.lower().startswith("http"):
        return template.format(bill_id=bill_id)
    parts = urlparse(link)
    if parts.hostname == _ALLOWED_HOST and parts.path.lower() in _LEGACY_DETAIL_PATHS:
        return template.format(bill_id=bill_id)
    return link


def _is_not_found_page(html: str) -> bool:
    """상세 페이지가 '해당 의안 정보가 존재하지 않습니다' 류를 응답했는지."""
    squished = _WS_ALL.sub("", html or "")
    return any(m in squished for m in _NOT_FOUND_MARKERS)


def _probe_summary(soup: BeautifulSoup) -> tuple[SummaryProbe, str]:
    """제안이유 컨테이너를 들여다보고 (판정, 텍스트) 를 돌려준다.

    컨테이너가 여럿 걸리면 가장 좋은 결과를 쓴다(FOUND > EMPTY > UNEXPECTED). 하나가
    비어 있어도 다른 하나에 본문이 있으면 본문을 쓴다는 뜻이다.
    """
    best = SummaryProbe.MISSING
    best_text = ""
    rank = {
        SummaryProbe.MISSING: 0,
        SummaryProbe.UNEXPECTED: 1,
        SummaryProbe.EMPTY: 2,
        SummaryProbe.FOUND: 3,
    }
    for sel in _SUMMARY_SELECTORS:
        el = soup.select_one(sel)
        if el is None:
            continue                      # 이 셀렉터는 없음 — 다음 후보로
        text = clean_text(el.get_text("\n"))
        if len(text) >= _MIN_SUMMARY_CHARS:
            probe, value = SummaryProbe.FOUND, text
        elif not text:
            probe, value = SummaryProbe.EMPTY, ""
        else:
            # 짧은데 본문이라 하기엔 모자란다 — 뭘 받은 건지 알 수 없다.
            probe, value = SummaryProbe.UNEXPECTED, text
        if rank[probe] > rank[best]:
            best, best_text = probe, value
    return best, best_text


def _probe_billinfo(soup: BeautifulSoup) -> tuple[SummaryProbe, str]:
    """billInfo.do 응답 전용 판정 — **확정된 selector 하나만** 인정한다(fail-closed).

    초기 상세 GET 은 구형 페이지 지원을 위해 폴백 selector 를 두지만, 확정된 계약을
    가진 이 응답에는 폴백을 두지 않는다. #summaryContentDiv 나 #prntSummary 만 있고
    pre#prntSummary 가 없다면 응답 구조가 바뀐 것이므로 ERROR 여야 한다 — 폴백으로
    받아 주면 구조 변경이 조용히 통과한다.

    PENDING 은 pre#prntSummary 가 **존재하고 완전히 비어 있을 때만**이다. 짧은 안내
    문구는 실제 PENDING fixture 로 정확한 문구를 확인하기 전까지 ERROR 로 둔다 —
    정규식으로 추정하면 구조 변경을 '등록 대기'로 위장하게 된다.
    """
    el = soup.select_one(_BILLINFO_SUMMARY_SELECTOR)
    if el is None:
        return SummaryProbe.MISSING, ""
    text = clean_text(el.get_text("\n"))
    if len(text) >= _MIN_SUMMARY_CHARS:
        return SummaryProbe.FOUND, text
    if not text:
        return SummaryProbe.EMPTY, ""
    return SummaryProbe.UNEXPECTED, text


def _extract_summary(soup: BeautifulSoup) -> str:
    """'제안이유 및 주요내용' 텍스트. 못 찾거나 사실상 비어 있으면 빈 문자열."""
    probe, text = _probe_summary(soup)
    return text if probe is SummaryProbe.FOUND else ""


def _hidden_inputs(form) -> dict[str, str]:
    """폼의 hidden input 전체(name → value). CSRF 토큰도 보통 여기 들어 있다."""
    out: dict[str, str] = {}
    for el in form.find_all("input"):
        if (el.get("type") or "").strip().lower() != "hidden":
            continue
        name = (el.get("name") or "").strip()
        if name:
            out[name] = el.get("value") or ""
    return out


# form#form 안에서 의안 ID 를 담는 hidden input 이름(소문자 비교).
_BILL_ID_FIELDS = ("billid", "bill_id")


@dataclass(frozen=True)
class SummaryRequest:
    """제안이유를 받아오기 위해 보낼 요청(확정된 계약대로 조립된 것)."""

    method: str              # 확정 계약상 항상 "post"
    action: str              # billInfo.do 절대 URL
    data: dict[str, str]     # form#form 의 named hidden input 전체
    headers: dict[str, str]  # {"X-CSRF-TOKEN": meta[name="_csrf"] 값}

    def shape(self) -> dict:
        """값을 뺀 요청 '형태'. 캡처 도구가 저장소에 기록하는 용도.

        토큰·세션값은 담지 않는다 — 이름만으로 회귀 검증이 가능하고, 값은 저장소에
        남겨서는 안 되는 비밀이다.
        """
        return {
            "method": self.method,
            "action": self.action,
            "data_keys": sorted(self.data),
            "header_keys": sorted(self.headers),
        }


# 응답 레코드에서 값을 찾을 때 시도할 필드명 후보(명세서 출력값 기준 + 변형 대비)
_ID_FIELDS = ("BILL_ID", "billId", "BILL_NO", "billNo")
_NAME_FIELDS = ("BILL_NAME", "BILL_NM", "billName", "TITLE", "billNm")
_DATE_FIELDS = ("PROPOSE_DT", "PPSL_DT", "proposeDt", "PROC_DT", "REGIST_DT")
_PROPOSER_FIELDS = ("PROPOSER", "RST_PROPOSER", "proposer", "PPSR")
_URL_FIELDS = ("LINK_URL", "linkUrl", "DETAIL_URL")


class AssemblyBillScraper(BaseScraper):
    PAGE_PARAM = None  # Open API 는 pIndex 로 직접 페이지네이션
    SUPPORTS_PAGINATION = True

    def __init__(self, source, fetcher):
        super().__init__(source, fetcher)
        ex = source.extra or {}
        self.endpoint = ex.get("api_endpoint", "https://open.assembly.go.kr/portal/openapi")
        self.service = ex.get("api_service", "")
        # AGE 는 계류의안 서비스의 요청인자가 아니므로 명시 설정된 경우에만 전송
        self.age = str(ex["age"]) if ex.get("age") not in (None, "") else ""
        self.page_size = int(ex.get("page_size", 100))
        # LINK_URL 이 없는 레코드에만 쓰는 상세 URL 폴백. 사이트가 경로를 옮기면
        # 코드 배포 없이 config 로 고칠 수 있게 덮어쓰기를 허용한다.
        self.detail_url = str(ex.get("detail_url") or _DETAIL)
        self.api_key = os.environ.get("ASSEMBLY_API_KEY", "")

    def fetch_list(self, limit: int, page: int = 1) -> list[Post]:
        if not self.api_key:
            log.warning(
                "[%s] ASSEMBLY_API_KEY 미설정 — 계류의안 수집을 건너뜁니다.", self.key
            )
            return []
        if not self.service:
            log.warning(
                "[%s] api_service 미설정 — config.yaml 의 assembly 소스에 api_service 를 지정하세요.",
                self.key,
            )
            return []

        url = f"{self.endpoint.rstrip('/')}/{self.service}"
        params = {
            "KEY": self.api_key,
            "Type": "json",
            "pIndex": str(max(1, page)),
            # 페이지당 건수는 이 소스의 page_size 를 그대로 쓴다. (전역 list_limit=30 으로
            # 캡하면 요청 수가 3배로 늘어 대량 조회 시 느려진다)
            "pSize": str(self.page_size),
        }
        if self.age:
            params["AGE"] = self.age
        try:
            resp = self.fetcher.get(url, params=params, referer="https://open.assembly.go.kr/")
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            # 전송/JSON 오류는 예외로 전파해야 한다. [] 로 삼키면 collect 가 '목록 끝'으로
            # 오인해 백필 커서를 초기화하고 이후 페이지를 영구히 건너뛴다.
            # 단, 원본 예외 문자열에는 요청 URL(= KEY 로 인증키 포함)이 들어갈 수 있으므로
            # 인증키가 없는 sanitize 된 예외로 바꿔 던진다(from None 으로 원본 체인 숨김).
            raise RuntimeError(f"Open API 요청 실패: {type(e).__name__}") from None

        # RESULT 코드 확인: 정상(INFO-000)·데이터없음(INFO-200)만 빈 목록으로 허용하고,
        # 인증키/서비스/쿼터 등 에러 코드는 예외로 전파한다([] 로 삼키면 collect 가
        # '목록 끝'으로 오인한다).
        code = self._result_code(data)
        if code and code not in ("INFO-000", "INFO-200"):
            self._dump_debug("list", json.dumps(data, ensure_ascii=False, indent=2))
            raise RuntimeError(f"Open API 오류 응답(RESULT={code})")

        rows = self._rows(data)
        if not rows:
            # 인증키/서비스명이 틀리면 흔히 여기로 온다(코드 없이 빈 응답). 진단용 덤프.
            log.warning(
                "[%s] Open API 응답에서 목록을 찾지 못함(RESULT=%s) — 서비스명/인증키 확인. 디버그 덤프.",
                self.key,
                code or "없음",
            )
            self._dump_debug("list", json.dumps(data, ensure_ascii=False, indent=2))
            return []

        posts: list[Post] = []
        seen: set[str] = set()
        for row in rows:
            bill_id = self._first(row, _ID_FIELDS)
            name = clean_text(self._first(row, _NAME_FIELDS))
            if not bill_id or not name or bill_id in seen:
                continue
            seen.add(bill_id)
            proposer = clean_text(self._first(row, _PROPOSER_FIELDS))
            title = f"{name} ({proposer})" if proposer else name
            # LINK_URL 이 죽은 구 경로면 BILL_ID 로 현재 경로를 다시 만든다.
            link = clean_text(self._first(row, _URL_FIELDS))
            url = _canonical_detail_url(link, bill_id, self.detail_url)
            posts.append(
                Post(
                    source_key=self.key,
                    source_name=self.name,
                    post_id=bill_id,
                    title=title,
                    url=url,
                    date=clean_text(self._first(row, _DATE_FIELDS)),
                )
            )
        return posts[: self.page_size]

    @staticmethod
    def _result_code(data) -> str:
        """응답 봉투 어디에 있든 RESULT.CODE 를 찾아 반환(없으면 '')."""
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                res = node.get("RESULT")
                if isinstance(res, dict) and res.get("CODE"):
                    return str(res["CODE"])
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        return ""

    @staticmethod
    def _rows(data) -> list:
        """열린국회 Open API 표준 봉투에서 'row' 리스트를 추출."""
        if isinstance(data, dict):
            # { "<service>": [ {"head":...}, {"row":[...]} ] }
            for v in data.values():
                if isinstance(v, list):
                    for item in v:
                        if isinstance(item, dict) and isinstance(item.get("row"), list):
                            return item["row"]
            # 혹시 평평한 형태
            for key in ("row", "data", "list", "items"):
                if isinstance(data.get(key), list):
                    return data[key]
        return []

    @staticmethod
    def _first(row: dict, fields) -> str:
        for f in fields:
            v = row.get(f)
            if v not in (None, ""):
                return str(v)
        return ""

    # --- 상세: 제안이유 및 주요내용 ---
    def enrich(self, post: Post) -> None:
        """상세 페이지에서 '제안이유 및 주요내용'을 가져와 post.body 에 담는다.

        본문을 못 채워도 그 사유를 post.proposal_status 로 남긴다 — '아직 등록 안 됨
        (PENDING)'과 '수집 실패(ERROR)'는 다른 사건이고, 메일 문구도 집계도 달라진다.

        어떤 실패도 밖으로 던지지 않는다 — 의안 한 건의 수집 실패가 다른 의안이나
        메일 발송을 막아서는 안 된다.
        """
        try:
            self._fill_proposal_reason(post)
        except Exception as e:  # noqa: BLE001
            # 네트워크·HTTP 오류는 '등록 대기'가 아니다. 반드시 ERROR 로 센다.
            self._set_status(
                post, ProposalContentStatus.ERROR, f"{type(e).__name__}: {e}"
            )
            log.warning(
                "[%s] 제안이유 수집 실패(%s) %s: %s",
                self.key,
                post.post_id,
                post.url,
                e,
            )

    def _set_status(
        self, post: Post, status: ProposalContentStatus, note: str = ""
    ) -> None:
        post.proposal_status = status
        post.proposal_note = note

    def _fill_proposal_reason(self, post: Post) -> None:
        resp = self.fetcher.get(post.url, referer=self.list_url)
        html = self.fetcher.text(resp)

        # 0) 상세 페이지 자체가 유효한가. '해당 의안 정보가 존재하지 않습니다' 는
        #    등록 대기가 아니라 우리가 잘못된 주소를 부른 것이다.
        if _is_not_found_page(html):
            self._set_status(
                post,
                ProposalContentStatus.ERROR,
                "상세 페이지가 '의안 정보 없음'을 응답 — 상세 URL(detail_url) 확인 필요",
            )
            self._dump(post, html)
            return

        soup = BeautifulSoup(html, "lxml")

        # 1) 구형 페이지 지원: 상세 HTML 에 제안이유가 **실제로 실려 있으면** 쓴다.
        #    비어 있다는 이유로 PENDING 으로 넘기지 않는다 — 현행 페이지는 초기 HTML 의
        #    컨테이너가 비어 있는 것이 정상이고, 내용은 billInfo.do 가 준다.
        inline_probe, inline_text = _probe_summary(soup)
        if inline_probe is SummaryProbe.FOUND:
            post.body = inline_text
            self._set_status(post, ProposalContentStatus.AVAILABLE, "상세 HTML 에 포함")
            return

        # 2) 확정된 계약대로 billInfo.do 요청을 만든다.
        #    만들 수 없으면(폼·CSRF·billId 문제) **요청을 보내지 않고** ERROR 다.
        try:
            req = self.build_summary_request(soup, post)
        except SummaryRequestError as e:
            self._fail(post, str(e), html)
            return

        # 3) 전송. 네트워크·HTTP 오류는 enrich 가 ERROR 로 받는다(2xx 만 여기 도달).
        resp = self.fetcher.post(
            req.action, data=req.data, referer=post.url, headers=req.headers
        )
        body_html = self.fetcher.text(resp)
        if _is_not_found_page(body_html):
            self._fail(post, "billInfo.do 가 '의안 정보 없음'을 응답", body_html)
            return

        # 4) 확정된 selector(pre#prntSummary)만으로 판정한다(fail-closed).
        probe, text = _probe_billinfo(BeautifulSoup(body_html, "lxml"))
        if probe is SummaryProbe.FOUND:
            post.body = text
            self._set_status(post, ProposalContentStatus.AVAILABLE, "billInfo.do 응답")
            return
        if probe is SummaryProbe.EMPTY:
            # 확정된 endpoint 가 2xx 정상 HTML 을 줬고 알려진 selector 도 있는데
            # 내용만 비어 있다 = 원문이 아직 등록되지 않음.
            self._set_status(
                post,
                ProposalContentStatus.PENDING,
                "billInfo.do 응답의 pre#prntSummary 가 비어 있음",
            )
            return

        # selector 자체가 없거나 읽을 수 없는 내용 → 구조 변경. PENDING 으로 넘기면
        # 고장이 '정상'으로 위장된다.
        note = (
            f"billInfo.do 응답이 예상과 다름: {text[:80]}"
            if probe is SummaryProbe.UNEXPECTED
            else f"billInfo.do 응답에 {_BILLINFO_SUMMARY_SELECTOR} 가 없음"
            "(selector/계약 확인 필요 — 폴백 selector 는 인정하지 않는다)"
        )
        self._fail(post, note, body_html)

    def _fail(self, post: Post, note: str, dump: str) -> None:
        """ERROR 로 판정하고 진단용 덤프를 남긴다."""
        self._set_status(post, ProposalContentStatus.ERROR, note)
        log.info("[%s] 제안이유 미확보(%s): %s", self.key, post.post_id, note)
        self._dump(post, dump)

    def _dump(self, post: Post, content: str) -> None:
        self._dump_debug(f"detail_{_UNSAFE_NAME.sub('_', post.post_id)}", content)

    def build_summary_request(
        self, soup: BeautifulSoup, post: Post
    ) -> SummaryRequest:
        """확정된 계약대로 billInfo.do POST 요청을 만든다(전송은 하지 않는다).

        2026-08 Playwright 캡처로 확정된 계약:
            POST https://likms.assembly.go.kr/bill/bi/bill/detail/billInfo.do
            payload : 상세 HTML form#form 의 named hidden input 전체(URL-encoded)
            header  : X-CSRF-TOKEN = meta[name="_csrf"] 의 content
                      Referer     = 해당 상세 URL
            쿠키    : 상세 GET 과 **같은 세션**(Fetcher 가 세션을 공유한다)
            응답    : HTML, 본문은 pre#prntSummary

        form#form 은 payload '원천'일 뿐이다. 그 폼의 빈 action 과 기본 GET 을 그대로
        replay 하면 안 된다 — JavaScript 가 폼을 serialize 한 뒤 별도 endpoint 로
        POST 하기 때문이다(그렇게 replay 했다가 billId 가 중복되어 HTTP 400 을 받았다).

        전송과 분리해 둔 이유는 캡처 도구가 '실제로 보낼 요청'의 형태를 기록해야 하기
        때문이다. 도구가 조립 규칙을 따로 구현하면 본 코드와 어긋난다.

        요청을 만들 수 없으면 SummaryRequestError — 호출자는 **전송하지 않고** ERROR 로
        처리한다. 근거가 어긋난 요청을 보내는 것보다 보내지 않는 편이 안전하다.
        """
        form = soup.select_one(_PAYLOAD_FORM_SELECTOR)
        if form is None:
            raise SummaryRequestError(
                f"상세 HTML 에 {_PAYLOAD_FORM_SELECTOR} 가 없음(페이지 구조 변경)"
            )

        data = _hidden_inputs(form)
        if not data:
            raise SummaryRequestError(
                f"{_PAYLOAD_FORM_SELECTOR} 에 name 있는 hidden input 이 없음"
            )

        # 폼의 billId 가 이 의안의 것인지 확인한다. 없거나 다르면 남의 의안을 조회하게
        # 되므로 절대 보내지 않는다(A 의안 메일에 B 의안 본문이 실리는 최악의 오류).
        form_bill_id = ""
        for name, value in data.items():
            if name.lower() in _BILL_ID_FIELDS:
                form_bill_id = (value or "").strip()
                break
        if not form_bill_id:
            raise SummaryRequestError(
                f"{_PAYLOAD_FORM_SELECTOR} 에 billId 가 없음"
            )
        if form_bill_id != post.post_id:
            raise SummaryRequestError(
                "form#form 의 billId 가 목록의 BILL_ID 와 다름 "
                f"(form={form_bill_id!r} 목록={post.post_id!r})"
            )

        meta = soup.select_one(_CSRF_META_SELECTOR)
        token = (meta.get("content") or "").strip() if meta is not None else ""
        if not token:
            raise SummaryRequestError(
                f'{_CSRF_META_SELECTOR} 토큰이 없음 — 세션/페이지 구조 확인 필요'
            )

        return SummaryRequest(
            method="post",
            action=_BILLINFO_ENDPOINT,
            data=data,
            headers={_CSRF_HEADER: token},
        )
