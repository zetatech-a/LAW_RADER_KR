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
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..models import Post
from .base import BaseScraper, clean_text

log = logging.getLogger(__name__)

# 상세 URL 폴백. 평상시엔 쓰이지 않는다 — Open API 응답의 LINK_URL(공식 상세 주소)이
# 있으면 그것을 그대로 쓰고, 없을 때만 이 템플릿으로 URL 을 만든다.
#
# 주의: 이 경로는 라이브 확인이 필요하다. 의안정보시스템이 상세 경로를
# /bill/billDetail.do 에서 /bill/bi/billDetailPage.do 로 옮겼다는 보고가 있고, 구 경로가
# redirect 되지 않으면 LINK_URL 이 빈 레코드에서만 조용히 404 가 난다(다른 레코드는
# 정상이라 전면 실패로 드러나지 않는다). 확인 전까지 기본값을 바꾸지 않는 이유는,
# 현재 경로가 살아 있는데 성급히 바꾸면 멀쩡한 폴백까지 깨지기 때문이다.
# 확인 후에는 코드 수정 없이 config.yaml 의 assembly 소스에 detail_url 로 덮어쓸 수 있다:
#   detail_url: "https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId={bill_id}"
_DETAIL = "https://likms.assembly.go.kr/bill/billDetail.do?billId={bill_id}"

# --- 제안이유 및 주요내용 수집 ---
#
# 상세 페이지의 '제안이유 및 주요내용'은 페이지에 함께 실려 오기도 하고, 별도 요청으로
# 채워지기도 한다. 어느 쪽인지는 사이트 개편에 따라 바뀌므로 두 경로를 모두 지원한다.
#
# 두 번째 경로(별도 요청)에서 POST URL·필드명(CSRF 포함)을 코드에 박아 두지 않는다.
# 사이트가 필드명을 바꾸면 조용히 빈 본문만 남기 때문이다. 대신 받은 HTML 에서 폼을
# 찾아 그 폼의 action·method·hidden input 을 '그대로' 되돌려 보낸다 — CSRF 토큰도
# hidden input 이면 자동으로 포함되고, meta 태그로 오는 경우만 따로 보탠다.
_SUMMARY_SELECTORS = ("pre#prntSummary", "#prntSummary", "#summaryContentDiv")

# 상세 페이지에는 값이 채워지기 전의 빈 컨테이너가 먼저 있을 수 있다. 공백·안내문
# 수준의 짧은 텍스트를 '수집 성공'으로 보면 요약도 발췌도 못 쓰는 본문이 실린다.
_MIN_SUMMARY_CHARS = 20

# 폼 action 으로 허용하는 호스트. 상세 페이지에는 외부 링크 폼(검색·공유 등)이 섞일 수
# 있고, 그 action 으로 hidden input(세션 식별자·CSRF 포함)을 보내면 안 된다.
_ALLOWED_HOST = "likms.assembly.go.kr"

# 디버그 덤프 파일명에 bill_id 를 그대로 쓰면 경로 조작이 될 수 있다.
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")

def _allowed_action(url: str) -> bool:
    """폼 action 이 의안정보시스템의 HTTPS 주소인지."""
    parts = urlparse(url)
    return parts.scheme == "https" and parts.hostname == _ALLOWED_HOST


def _extract_summary(soup: BeautifulSoup) -> str:
    """'제안이유 및 주요내용' 텍스트. 못 찾거나 사실상 비어 있으면 빈 문자열."""
    for sel in _SUMMARY_SELECTORS:
        el = soup.select_one(sel)
        if el is None:
            continue
        text = clean_text(el.get_text("\n"))
        if len(text) >= _MIN_SUMMARY_CHARS:
            return text
    return ""


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


def _csrf_from_meta(soup: BeautifulSoup) -> tuple[str, str, str]:
    """meta 태그로 실려 오는 CSRF 정보 (토큰, 헤더명, 파라미터명).

    필드명을 추측하지 않는다 — 이름에 'csrf' 가 든 meta 만 보고, 그 이름이 header/
    parameter 중 무엇을 뜻하는지도 meta 이름 자체로 판단한다.
    """
    token = header = param = ""
    for m in soup.find_all("meta"):
        name = (m.get("name") or "").strip().lower()
        content = (m.get("content") or "").strip()
        if not content or "csrf" not in name:
            continue
        if "header" in name:
            header = header or content
        elif "param" in name:
            param = param or content
        else:
            token = token or content
    return token, header, param


# 값이 비어 있으면 의안 ID 를 채워 줄 hidden input 이름(소문자 비교).
# '없는 필드를 만들어 보내는' 것이 아니라, 폼에 이미 있는 빈 칸만 채운다.
_BILL_ID_FIELDS = ("billid", "bill_id")


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
            # 공식 상세 URL(LINK_URL)이 있으면 사용, 없으면 billDetail 로 구성
            link = clean_text(self._first(row, _URL_FIELDS))
            url = (
                link
                if link.startswith("http")
                else self.detail_url.format(bill_id=bill_id)
            )
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

        어떤 실패도 밖으로 던지지 않는다 — 의안 한 건의 수집 실패가 다른 의안이나
        메일 발송을 막아서는 안 된다. 본문이 비면 메일에는 제목·링크만 실린다.
        """
        try:
            self._fill_proposal_reason(post)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "[%s] 제안이유 수집 실패(%s) %s: %s",
                self.key,
                post.post_id,
                post.url,
                e,
            )

    def _fill_proposal_reason(self, post: Post) -> None:
        resp = self.fetcher.get(post.url, referer=self.list_url)
        html = self.fetcher.text(resp)
        soup = BeautifulSoup(html, "lxml")

        # 1) 상세 HTML 에 이미 실려 있으면 그대로 쓴다(추가 요청 없음).
        text = _extract_summary(soup)
        if text:
            post.body = text
            return

        # 2) 별도 요청으로 채워지는 구조: 폼을 찾아 그대로 되돌려 보낸다.
        text, follow_html = self._request_summary(soup, post)
        if text:
            post.body = text
            return

        # 3) 그래도 없으면 셀렉터/폼 판정을 고칠 수 있도록 bill_id 별로 덤프한다.
        log.info(
            "[%s] 제안이유 및 주요내용을 찾지 못함(%s) — 디버그 덤프.", self.key, post.post_id
        )
        dump = html
        if follow_html:
            dump = f"{html}\n\n<!-- ===== 후속 응답 ===== -->\n{follow_html}"
        self._dump_debug(f"detail_{_UNSAFE_NAME.sub('_', post.post_id)}", dump)

    def _request_summary(self, soup: BeautifulSoup, post: Post) -> tuple[str, str]:
        """상세 페이지의 폼을 그대로 재전송해 제안이유를 받아온다.

        반환값은 (제안이유 텍스트, 후속 응답 HTML). 보낼 만한 폼이 없으면 ("", "").
        """
        found = self._summary_form(soup, post)
        if found is None:
            log.debug("[%s] 제안이유 요청에 쓸 폼을 찾지 못함(%s)", self.key, post.post_id)
            return "", ""
        form, action = found

        data = _hidden_inputs(form)
        # 폼에 이미 있는 빈 의안 ID 칸만 채운다(값이 있으면 건드리지 않는다).
        for name in list(data):
            if name.lower() in _BILL_ID_FIELDS and not data[name]:
                data[name] = post.post_id

        headers: dict[str, str] = {}
        token, header, param = _csrf_from_meta(soup)
        if token and header:
            headers[header] = token
        if token and param and not data.get(param):
            data[param] = token

        method = (form.get("method") or "post").strip().lower()
        if method == "get":
            resp = self.fetcher.get(action, params=data, referer=post.url, headers=headers)
        else:
            resp = self.fetcher.post(action, data=data, referer=post.url, headers=headers)
        html = self.fetcher.text(resp)
        return _extract_summary(BeautifulSoup(html, "lxml")), html

    def _summary_form(self, soup: BeautifulSoup, post: Post):
        """제안이유를 돌려줄 것으로 보이는 폼과 그 action URL.

        점수가 0 인(= 이 의안과의 연관 근거가 하나도 없는) 폼에는 보내지 않는다.
        상세 페이지에는 검색·통계 등 무관한 폼이 함께 있고, 그런 폼에 hidden input 을
        되쏘면 엉뚱한 요청이 된다.
        """
        best = None
        best_score = 0
        for form in soup.find_all("form"):
            action = urljoin(post.url, (form.get("action") or "").strip())
            if not _allowed_action(action):
                continue
            hidden = _hidden_inputs(form)
            blob = " ".join(
                (form.get("id") or "", form.get("name") or "", action)
            ).lower()
            score = 0
            if "summary" in blob:
                score += 4
            if post.post_id and post.post_id in hidden.values():
                score += 2
            if any(k.lower() in _BILL_ID_FIELDS for k in hidden):
                score += 1
            if score > best_score:
                best, best_score = (form, action), score
        return best
