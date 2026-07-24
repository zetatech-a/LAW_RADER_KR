"""스크래퍼 베이스 클래스와 공통 유틸."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

from ..config import SourceConfig
from ..fetcher import Fetcher
from ..models import Post

log = logging.getLogger(__name__)

_WS = re.compile(r"[ \t\r\f\v]+")
_MULTINL = re.compile(r"\n{3,}")


def clean_text(s: str) -> str:
    """공백 정리."""
    if not s:
        return ""
    s = _WS.sub(" ", s)
    s = _MULTINL.sub("\n\n", s)
    return s.strip()


class BaseScraper:
    """모든 스크래퍼의 부모.

    하위 클래스는 최소한 fetch_list() 를 구현하고,
    필요하면 enrich() 로 상세 본문/첨부를 채운다.
    """

    # 목록 페이지네이션에 쓰는 쿼리 파라미터명. HTML 게시판이 지정한다. None 이면
    # 기본 fetch_list 가 1페이지만 요청. 주의: 파라미터명은 라이브 검증이 필요하다.
    PAGE_PARAM: str | None = None

    # 페이지네이션 지원 여부. None 이면 PAGE_PARAM 유무로 판단한다. POST/API 기반
    # 스크래퍼처럼 PAGE_PARAM 없이 page 인자로 페이지네이션하는 경우 True 로 명시하면
    # 라이브 검증기(verify_sources)가 2페이지까지 실제로 검사한다.
    SUPPORTS_PAGINATION: bool | None = None

    @property
    def paginates(self) -> bool:
        if self.SUPPORTS_PAGINATION is not None:
            return self.SUPPORTS_PAGINATION
        return bool(self.PAGE_PARAM)

    def __init__(self, source: SourceConfig, fetcher: Fetcher):
        self.source = source
        self.fetcher = fetcher
        self.key = source.key
        self.name = source.name
        self.list_url = source.list_url

    # --- HTML 게시판 공통 목록 수집 ---
    def fetch_list(self, limit: int, page: int = 1) -> list[Post]:
        """목록 페이지를 가져와 _parse_list 로 파싱.

        첫 요청이 0건이면(세션 쿠키 미설정으로 인터스티셜을 받는 등) 세션에 쿠키가
        붙은 상태로 1회 재시도한다. 이래도 0건이면 디버그 덤프를 남긴다.
        """
        posts: list[Post] = []
        html = ""
        for _attempt in range(2):
            resp = self.fetcher.get(self._list_page_url(page))
            html = self.fetcher.text(resp)
            posts = self._parse_list(BeautifulSoup(html, "lxml"))
            if posts:
                break
        if not posts:
            log.warning("[%s] 목록 파싱 0건 — 마크업/세션 확인 필요. 디버그 덤프.", self.key)
            self._dump_debug("list", html)
        return posts[:limit]

    def _parse_list(self, soup: BeautifulSoup) -> list[Post]:  # pragma: no cover
        raise NotImplementedError

    def enrich(self, post: Post) -> None:
        """상세 페이지에서 본문/첨부를 채운다. 기본은 아무것도 안 함."""
        return

    # --- 페이지네이션 수집 ---
    def collect(
        self,
        limit: int,
        seen_ids: set[str],
        max_pages: int,
        backfill: bool = False,
        anchor: str | None = None,
    ) -> tuple[list[Post], bool, str | None]:
        """이미 본 글(seen_ids) 경계에 닿을 때까지 페이지를 넘겨가며 목록을 모은다.

        반환: (수집된 글, 경계도달여부, 이번에 수집한 가장 오래된 신규 ID).
        경계도달여부=False 는 예산(max_pages)을 다 쓰고도 경계에 닿지 못했다는 뜻으로,
        호출부가 backfill_pending 과 앵커(가장 오래된 신규 ID)를 저장해 다음 실행에
        이어받게 한다.

        중단 조건(누락 방지 + 무한루프 방지):
          - 이번 페이지의 글이 '전부' 이미 본 것이고, 앵커를 이미 지났으면(또는 앵커가
            없으면) 경계로 보고 중단. ※ 상단 고정(pinned)공지는 매 페이지에 seen 으로
            남으므로 '하나라도 seen' 이 아니라 '전부 seen' 이어야 멈춘다.
          - 새 글이 더 늘지 않으면(페이지 파라미터 무시 등) 중단
          - 신규 수집 예산(max_pages 페이지) 소진 시 중단(경계 미도달)

        backfill=True 이고 anchor(직전 실행에서 마지막으로 수집한 가장 오래된 신규 ID)가
        주어지면: 앵커를 만나기 전까지의 '전부 seen' 페이지(이미 처리한 prefix)는 경계로
        보지 않고 예산 없이 건너뛴다. 앵커를 지난 뒤의 '전부 seen' 페이지에서 비로소
        경계로 판단한다. 이렇게 하면 백필 도중 상단에 새 글이 끼어들어도(saw_unseen 이
        일찍 켜져도) prefix 를 끝까지 건너뛰어 남은 백로그를 놓치지 않는다.
        """
        collected: list[Post] = []
        collected_ids: set[str] = set()
        oldest_unseen: str | None = None
        # 앵커가 없으면(일반 실행) 처음부터 경계 판정을 켠다.
        passed_anchor = anchor is None
        hit_boundary = False
        unseen_pages = 0
        budget = max(1, max_pages)
        # prefix 스킵을 포함한 절대 페이지 상한(무한루프 방지). 신규 수집 예산과 별개.
        hard_cap = budget * 10
        for page in range(1, hard_cap + 1):
            batch = self.fetch_list(limit, page=page)
            if not batch:
                hit_boundary = True  # 더 볼 페이지가 없음 = 백로그 소진
                break
            fresh = [p for p in batch if p.post_id not in collected_ids]
            if not fresh:
                hit_boundary = True  # 진전 없음(같은 목록 반복 등) = 사실상 소진
                break
            # 경계 판정은 '이 페이지 진입 시점'의 앵커통과 상태로 한다. 앵커가 들어있는
            # 페이지 자체는 아직 경계로 보지 않아야(앵커 '뒤'의 백로그를 놓치지 않도록)
            # 하므로, 앵커통과 표시는 이 페이지 처리 후 다음 페이지부터 반영한다.
            armed = passed_anchor
            page_all_seen = all(p.post_id in seen_ids for p in batch)
            page_had_unseen = False
            for p in fresh:
                collected_ids.add(p.post_id)
                collected.append(p)  # 현재 목록 갱신(refresh)용으로 seen 도 포함
                if p.post_id not in seen_ids:
                    oldest_unseen = p.post_id
                    page_had_unseen = True
            if not passed_anchor and anchor is not None and any(p.post_id == anchor for p in batch):
                passed_anchor = True  # 다음 페이지부터 경계 판정
            if page_all_seen:
                if not armed:
                    continue  # 앵커 이전(또는 앵커 페이지) prefix 통과 중 → 예산 없이 계속
                hit_boundary = True  # 경계 통과
                break
            if page_had_unseen:
                unseen_pages += 1
                if unseen_pages >= budget:
                    break  # 예산 소진(경계 미도달 → 다음 실행에 backfill 로 이어감)
        if not hit_boundary:
            log.warning(
                "[%s] 수집 예산(max_pages=%d) 소진, 경계 미도달 — 백로그 잔여. "
                "다음 실행에 이어서(backfill) 계속 수집합니다.",
                self.key,
                max_pages,
            )
        return collected, hit_boundary, oldest_unseen

    def _list_page_url(self, page: int) -> str:
        """PAGE_PARAM 을 이용해 list_url 에 페이지 번호를 붙인다."""
        if page <= 1 or not self.PAGE_PARAM:
            return self.list_url
        parts = urlparse(self.list_url)
        q = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        q[self.PAGE_PARAM] = str(page)
        return urlunparse(parts._replace(query=urlencode(q)))

    # --- 공통 유틸 ---
    def _dump_debug(self, tag: str, content: str) -> None:
        """셀렉터 튜닝용: 원본 HTML/JSON 을 debug/ 에 저장."""
        d = Path("debug")
        d.mkdir(exist_ok=True)
        p = d / f"{self.key}_{tag}.txt"
        p.write_text(content, encoding="utf-8")
        log.info("[%s] 디버그 덤프 저장: %s", self.key, p)
