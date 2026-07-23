"""스크래퍼 베이스 클래스와 공통 유틸."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

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

    # --- 하위 클래스가 구현 ---
    def fetch_list(self, limit: int, page: int = 1) -> list[Post]:  # pragma: no cover
        raise NotImplementedError

    def enrich(self, post: Post) -> None:
        """상세 페이지에서 본문/첨부를 채운다. 기본은 아무것도 안 함."""
        return

    # --- 페이지네이션 수집 ---
    def collect(self, limit: int, seen_ids: set[str], max_pages: int) -> list[Post]:
        """이미 본 글(seen_ids)에 닿을 때까지 페이지를 넘겨가며 목록을 모은다.

        중단 조건(누락 방지 + 무한루프 방지):
          - 이번 페이지에 seen_ids 에 있는 글이 하나라도 있으면(그 아래는 이미 봄) 중단
          - 새 글이 더 늘지 않으면(페이지 파라미터 무시 등) 중단
          - max_pages 도달 시 중단
        """
        collected: list[Post] = []
        collected_ids: set[str] = set()
        for page in range(1, max(1, max_pages) + 1):
            batch = self.fetch_list(limit, page=page)
            if not batch:
                break
            fresh = [p for p in batch if p.post_id not in collected_ids]
            if not fresh:
                break  # 진전 없음
            for p in fresh:
                collected_ids.add(p.post_id)
                collected.append(p)
            if any(p.post_id in seen_ids for p in batch):
                break  # 이미 본 지점에 도달 → 더 볼 필요 없음
        return collected

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
