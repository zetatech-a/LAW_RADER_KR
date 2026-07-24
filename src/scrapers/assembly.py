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
"""
from __future__ import annotations

import json
import logging
import os

from ..models import Post
from .base import BaseScraper, clean_text

log = logging.getLogger(__name__)

_DETAIL = "https://likms.assembly.go.kr/bill/billDetail.do?billId={bill_id}"

# 응답 레코드에서 값을 찾을 때 시도할 필드명 후보(명세서 출력값 기준 + 변형 대비)
_ID_FIELDS = ("BILL_ID", "billId", "BILL_NO", "billNo")
_NAME_FIELDS = ("BILL_NAME", "BILL_NM", "billName", "TITLE", "billNm")
_DATE_FIELDS = ("PROPOSE_DT", "PPSL_DT", "proposeDt", "PROC_DT", "REGIST_DT")
_PROPOSER_FIELDS = ("PROPOSER", "RST_PROPOSER", "proposer", "PPSR")
_URL_FIELDS = ("LINK_URL", "linkUrl", "DETAIL_URL")


class AssemblyBillScraper(BaseScraper):
    PAGE_PARAM = None  # Open API 는 pIndex 로 직접 페이지네이션
    SUPPORTS_PAGINATION = True
    # 계류의안은 가변 멤버십 목록(처리되면 빠짐) → 최초 기준선을 현재 전체로 깊게 기록
    FULL_BASELINE = True

    def __init__(self, source, fetcher):
        super().__init__(source, fetcher)
        ex = source.extra or {}
        self.endpoint = ex.get("api_endpoint", "https://open.assembly.go.kr/portal/openapi")
        self.service = ex.get("api_service", "")
        # AGE 는 계류의안 서비스의 요청인자가 아니므로 명시 설정된 경우에만 전송
        self.age = str(ex["age"]) if ex.get("age") not in (None, "") else ""
        self.page_size = int(ex.get("page_size", 100))
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
            "pSize": str(min(self.page_size, limit) if limit else self.page_size),
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
            url = link if link.startswith("http") else _DETAIL.format(bill_id=bill_id)
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
        return posts[:limit]

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

    def enrich(self, post: Post) -> None:
        # 의안은 제목·의안번호·제안자·상세링크가 핵심이라 별도 본문 수집은 생략.
        return
