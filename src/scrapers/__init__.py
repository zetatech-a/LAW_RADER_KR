"""스크래퍼 레지스트리: config 의 type 문자열을 스크래퍼 클래스로 매핑."""
from __future__ import annotations

from ..config import SourceConfig
from .assembly import AssemblyBillScraper
from .base import BaseScraper
from .better_fsc import BetterReplyScraper
from .fsc import FscBoardScraper
from .fss import FssBoardScraper

_REGISTRY: dict[str, type[BaseScraper]] = {
    "fsc_board": FscBoardScraper,
    "fss_board": FssBoardScraper,
    "better_reply": BetterReplyScraper,
    "assembly_bill": AssemblyBillScraper,
}


def build_scraper(source: SourceConfig, fetcher) -> BaseScraper:
    cls = _REGISTRY.get(source.type)
    if cls is None:
        raise ValueError(f"알 수 없는 소스 type: {source.type!r} (key={source.key})")
    return cls(source, fetcher)
