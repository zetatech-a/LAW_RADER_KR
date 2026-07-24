"""스크래퍼 베이스 클래스와 공통 유틸."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

from ..config import SourceConfig
from ..fetcher import Fetcher
from ..models import Post

log = logging.getLogger(__name__)


@dataclass
class CollectResult:
    """collect() 결과.

    - posts: 이번에 새로 발견한(seen 에 없던) 글들(메일 대상).
    - reached_boundary: 이미 본 글 경계(또는 목록 끝)에 도달했는지. False 면 한 번의
      실행으로 감당할 수 없는 대량 신규(장기 미가동 후 등)라는 뜻 — 경고를 남긴다.
    - scanned: 이번에 실제로 조회한 글 수(중복 제외). 0 이면 파싱 실패 신호.
    """

    posts: list[Post] = field(default_factory=list)
    reached_boundary: bool = True
    scanned: int = 0

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
    def collect(self, limit: int, seen_ids: set[str], max_pages: int) -> CollectResult:
        """1페이지부터 최대 max_pages 페이지를 훑어 '신규(seen 에 없는)' 글을 모은다.

        한 번의 실행에서 이미 본 글 경계까지 훑는다. 평상시엔 신규가 없거나 소수라
        1~2페이지에서 곧 멈춘다(max_pages 는 실질적으로 '장기 미가동 후 따라잡기'
        깊이 상한일 뿐, 평상시 비용은 없다).

        중단(경계 도달):
          - 목록 끝(빈 페이지)                → reached_boundary=True
          - 페이지 전체가 이미 본 글(seen)     → reached_boundary=True
          - 진전 없음(페이지 파라미터 무시 등) → reached_boundary=True
        max_pages 안에 경계에 못 닿으면 reached_boundary=False 로, 한 실행으로 감당 못 할
        대량 신규(장기 미가동 등)라는 뜻이며 경고를 남긴다. (설계상 상한: 30분 간격의
        저빈도 게시판에서 한 실행 범위를 넘는 신규 유입은 사실상 발생하지 않는다.)

        주의: 페이지 fetch 실패는 예외로 전파되어야 한다(빈 결과로 삼키면 '목록 끝'으로
        오인한다). API 스크래퍼는 오류 시 [] 대신 예외를 던진다.
        """
        posts: list[Post] = []
        scanned_ids: set[str] = set()
        reached_boundary = False
        for page in range(1, max(1, max_pages) + 1):
            batch = self.fetch_list(limit, page=page)
            if not batch:
                reached_boundary = True  # 목록 끝
                break
            new_on_page = [p for p in batch if p.post_id not in scanned_ids]
            if not new_on_page:
                reached_boundary = True  # 같은 목록 반복(파라미터 무시 등) = 진전 없음
                break
            for p in new_on_page:
                scanned_ids.add(p.post_id)
                if p.post_id not in seen_ids:
                    posts.append(p)
            if all(p.post_id in seen_ids for p in batch):
                reached_boundary = True  # 페이지 전체가 이미 본 글 → 경계 통과
                break
        if not reached_boundary:
            log.warning(
                "[%s] max_pages(%d) 내에 경계 미도달 — 한 실행 범위를 넘는 대량 신규. "
                "장기 미가동 후라면 config 의 max_pages 를 일시 상향해 재실행하세요.",
                self.key,
                max_pages,
            )
        return CollectResult(
            posts=posts, reached_boundary=reached_boundary, scanned=len(scanned_ids)
        )

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
