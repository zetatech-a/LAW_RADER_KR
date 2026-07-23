"""코덱스 리뷰 지적사항 수정에 대한 회귀 테스트."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import SourceConfig
from src.fetcher import AttachmentTooLarge, Fetcher
from src.models import Post
from src.scrapers.base import BaseScraper


class _PagedScraper(BaseScraper):
    """페이지별로 미리 정해진 Post 목록을 돌려주는 테스트용 스크래퍼."""

    PAGE_PARAM = "pageIndex"

    def __init__(self, pages):
        src = SourceConfig(key="t", name="t", type="t", list_url="http://x/list")
        super().__init__(src, fetcher=None)
        self._pages = pages

    def fetch_list(self, limit, page=1):
        data = self._pages[page - 1] if page - 1 < len(self._pages) else []
        return [
            Post(source_key="t", source_name="t", post_id=i, title=i, url=f"http://x/{i}")
            for i in data
        ][:limit]


def test_collect_stops_at_seen_id():
    # 1페이지에 이미 본 'b' 가 있으면 2페이지로 넘어가지 않는다
    sc = _PagedScraper([["a", "b"], ["c", "d"]])
    got = [p.post_id for p in sc.collect(limit=10, seen_ids={"b"}, max_pages=5)]
    assert got == ["a", "b"]


def test_collect_paginates_until_seen():
    # 1페이지가 전부 신규면 다음 페이지까지 넘겨서 누락을 막는다
    sc = _PagedScraper([["a", "b"], ["c", "seen"], ["e"]])
    got = [p.post_id for p in sc.collect(limit=10, seen_ids={"seen"}, max_pages=5)]
    assert got == ["a", "b", "c", "seen"]


def test_collect_stops_when_no_progress():
    # 페이지 파라미터가 무시되어 같은 목록만 반복돼도 무한루프에 빠지지 않는다
    sc = _PagedScraper([["a", "b"], ["a", "b"], ["a", "b"]])
    got = [p.post_id for p in sc.collect(limit=10, seen_ids=set(), max_pages=10)]
    assert got == ["a", "b"]


def test_collect_respects_max_pages():
    pages = [[f"p{n}i{j}" for j in range(2)] for n in range(10)]  # 전부 신규
    sc = _PagedScraper(pages)
    got = sc.collect(limit=10, seen_ids=set(), max_pages=3)
    assert len(got) == 6  # 3페이지 * 2건


def test_list_page_url_appends_param():
    sc = _PagedScraper([[]])
    assert sc._list_page_url(1) == "http://x/list"
    assert "pageIndex=3" in sc._list_page_url(3)


class _FakeResp:
    def __init__(self, chunks, content_length=None):
        self._chunks = chunks
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def iter_content(self, chunk_size=65536):
        return iter(self._chunks)

    def close(self):
        pass


def test_download_rejects_by_content_length(monkeypatch):
    f = Fetcher(delay=0, max_download_bytes=100)
    monkeypatch.setattr(f, "get", lambda *a, **k: _FakeResp([b"x"], content_length=999))
    with pytest.raises(AttachmentTooLarge):
        f.download("http://x/big")


def test_download_rejects_by_streamed_size(monkeypatch):
    # Content-Length 가 없어도 청크 누적으로 상한을 넘으면 중단
    f = Fetcher(delay=0, max_download_bytes=100)
    monkeypatch.setattr(f, "get", lambda *a, **k: _FakeResp([b"x" * 60, b"y" * 60]))
    with pytest.raises(AttachmentTooLarge):
        f.download("http://x/big")


def test_download_ok_within_limit(monkeypatch):
    f = Fetcher(delay=0, max_download_bytes=100)
    monkeypatch.setattr(f, "get", lambda *a, **k: _FakeResp([b"x" * 40], content_length=40))
    assert f.download("http://x/ok") == b"x" * 40
