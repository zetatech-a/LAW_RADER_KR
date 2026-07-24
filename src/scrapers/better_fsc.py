"""금융규제·법령해석포털 (better.fsc.go.kr) 회신사례 스크래퍼.

목록은 DataTables 서버사이드 방식(POST → JSON)이다(라이브 확인).
  엔드포인트: /fsc_new/replyCase/selectReplyCaseTotalReplyList.do  (POST, JSON)
  레코드 필드: rownumber, pastreqType(구분), title, replyRegDate, dataIdx(안정 ID)
상세는 JS 함수 openReplyCasePastReqDetail(dataIdx, gubun) 로 열려 단순 URL 이 없으므로,
링크는 목록 페이지로 두고 제목·구분·일자만 통지한다(첨부/본문 수집 없음).
"""
from __future__ import annotations

import hashlib
import json
import logging

from ..models import Post
from .base import BaseScraper, clean_text

log = logging.getLogger(__name__)

_AJAX_URL = "https://better.fsc.go.kr/fsc_new/replyCase/selectReplyCaseTotalReplyList.do"


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
                    url=self.list_url,  # 상세가 JS 라 목록 페이지로 링크
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

    def enrich(self, post: Post) -> None:
        # 상세가 JS 함수라 단순 수집 불가 — 제목/구분/일자/링크만 통지
        return
