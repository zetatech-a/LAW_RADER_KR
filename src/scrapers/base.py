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

    # 목록 페이지네이션에 쓰는 쿼리 파라미터명. 하위 클래스가 지정하면 collect() 가
    # 여러 페이지를 넘겨가며 수집한다. None 이면 1페이지만.
    # 주의: 실제 파라미터명은 사이트마다 다를 수 있어 라이브에서 검증이 필요하다.
    PAGE_PARAM: str | None = None

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
    ) -> tuple[list[Post], bool]:
        """이미 본 글(seen_ids) 경계에 닿을 때까지 페이지를 넘겨가며 목록을 모은다.

        반환: (수집된 글, 경계도달여부).
        경계도달여부=False 는 max_pages 를 다 쓰고도 경계에 닿지 못했다는 뜻(백로그
        잔여 가능)으로, 호출부가 다음 실행에 backfill=True 로 이어받게 한다.

        중단 조건(누락 방지 + 무한루프 방지):
          - 이번 페이지의 글이 '전부' 이미 본 것이면(경계 통과) 중단.
            ※ 상단 고정(pinned)공지는 매 페이지에 seen 으로 남으므로 '하나라도 seen'
              이 아니라 '전부 seen' 이어야 멈춘다.
          - 새 글이 더 늘지 않으면(페이지 파라미터 무시 등) 중단
          - max_pages 도달 시 중단(경계 미도달로 간주)

        backfill=True(직전 실행이 cap 에 걸려 백로그가 남은 경우): 이미 본 prefix
        (전부 seen 인 앞쪽 페이지들)는 멈추지 않고 건너뛰며, 아직 신규를 하나도
        못 만난 동안의 '전부 seen' 페이지는 경계로 보지 않는다. 신규를 만난 뒤의
        '전부 seen' 페이지에서 비로소 경계로 판단해 멈춘다.
        """
        collected: list[Post] = []
        collected_ids: set[str] = set()
        saw_unseen = False
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
            page_all_seen = all(p.post_id in seen_ids for p in batch)
            # 백필 중 아직 신규를 못 만난 '전부 seen' 페이지 = 이미 본 prefix → 예산 없이 스킵
            skip_prefix = page_all_seen and backfill and not saw_unseen
            for p in fresh:
                collected_ids.add(p.post_id)  # no-progress 가드용으로 항상 기록
                if not skip_prefix:
                    collected.append(p)
                    if p.post_id not in seen_ids:
                        saw_unseen = True
            if page_all_seen:
                if skip_prefix:
                    continue  # prefix 통과 중 → 계속
                hit_boundary = True  # 신규 이후의 '전부 seen' = 경계 통과
                break
            # 신규가 있는 페이지 → 이번 실행 수집 예산 차감
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
        return collected, hit_boundary

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
