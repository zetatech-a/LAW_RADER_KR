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


def test_collect_stops_when_page_fully_seen():
    # 1페이지 전체가 이미 본 글이면(경계 통과) 다음 페이지로 넘어가지 않는다
    sc = _PagedScraper([["a", "b"], ["c", "d"]])
    posts, reached = sc.collect(limit=10, seen_ids={"a", "b"}, max_pages=5)
    assert [p.post_id for p in posts] == ["a", "b"]
    assert reached is True


def test_collect_paginates_until_page_fully_seen():
    # 1페이지에 신규가 하나라도 있으면 계속 넘겨서, 전부 seen 인 페이지에서 멈춘다
    sc = _PagedScraper([["a", "b"], ["c", "d"], ["seen1", "seen2"]])
    posts, reached = sc.collect(limit=10, seen_ids={"seen1", "seen2"}, max_pages=5)
    assert [p.post_id for p in posts] == ["a", "b", "c", "d", "seen1", "seen2"]
    assert reached is True


def test_collect_does_not_stop_on_pinned_seen():
    # 고정공지 'pin' 이 매 페이지 상단에 seen 으로 있어도 페이지네이션이 멈추면 안 된다.
    # (P1) 예전 'any seen' 조건이면 1페이지에서 멈춰 2페이지 신규 'c','d' 를 놓쳤다.
    sc = _PagedScraper([["pin", "a", "b"], ["pin", "c", "d"], ["pin", "old1", "old2"]])
    posts, _ = sc.collect(limit=10, seen_ids={"pin", "old1", "old2"}, max_pages=5)
    got = [p.post_id for p in posts]
    # 'c','d' 까지 도달해야 하고, 전부 seen 인 3페이지에서 멈춘다
    assert "c" in got and "d" in got


def test_collect_stops_when_no_progress():
    # 페이지 파라미터가 무시되어 같은 목록만 반복돼도 무한루프에 빠지지 않는다
    sc = _PagedScraper([["a", "b"], ["a", "b"], ["a", "b"]])
    posts, _ = sc.collect(limit=10, seen_ids=set(), max_pages=10)
    assert [p.post_id for p in posts] == ["a", "b"]


def test_collect_respects_max_pages():
    pages = [[f"p{n}i{j}" for j in range(2)] for n in range(10)]  # 전부 신규
    sc = _PagedScraper(pages)
    posts, reached = sc.collect(limit=10, seen_ids=set(), max_pages=3)
    assert len(posts) == 6  # 3페이지 * 2건
    assert reached is False  # cap 도달, 경계 미도달 → 백필 필요


def test_backfill_resumes_after_cap():
    # 시나리오 C: 백로그가 cap 을 넘어 앞부분만 수집·seen 처리된 뒤,
    # 다음 실행에 이미 본 prefix 를 건너뛰고 남은 백로그를 이어서 수집한다.
    # 5페이지(각 2건): 1~2p 는 이번에 볼 신규, 3~4p 는 아직 못 본 백로그, 5p 는 경계(old).
    pages = [["n1", "n2"], ["n3", "n4"], ["b1", "b2"], ["b3", "b4"], ["old1", "old2"]]
    sc = _PagedScraper(pages)

    # 1차 실행: max_pages=2 로 앞 2페이지만 수집, 경계 미도달
    seen = set()
    posts1, reached1 = sc.collect(limit=10, seen_ids=seen, max_pages=2)
    assert [p.post_id for p in posts1] == ["n1", "n2", "n3", "n4"]
    assert reached1 is False  # 백필 필요
    seen |= {p.post_id for p in posts1}  # 수집분을 seen 처리(=메일 후 mark_seen)

    # 2차 실행: backfill=True → seen prefix(1~2p) 건너뛰고 3p 부터 이어서 수집
    posts2, reached2 = sc.collect(limit=10, seen_ids=seen, max_pages=2, backfill=True)
    ids2 = [p.post_id for p in posts2]
    assert "b1" in ids2 and "b4" in ids2      # 백로그를 놓치지 않고 수집
    assert "n1" not in ids2                    # 이미 본 prefix 는 신규로 재발송 안 됨(state.is_new 로도 걸러짐)


def test_list_page_url_appends_param():
    sc = _PagedScraper([[]])
    assert sc._list_page_url(1) == "http://x/list"
    assert "pageIndex=3" in sc._list_page_url(3)


class _EmptyScraper(BaseScraper):
    """HTTP 는 성공했지만 파싱이 0건인 상황을 흉내낸다."""

    def __init__(self, source, fetcher):
        super().__init__(source, fetcher)

    def fetch_list(self, limit, page=1):
        return []


def test_all_empty_parse_fails_run(tmp_path, monkeypatch):
    # 모든 소스가 HTTP 성공하지만 파싱 0건이면 all-failed 가드가 실패(exit 1)로 잡아야 한다.
    import src.main as main_mod

    # 대상 소스를 미리 baseline 처리(빈 기준선 방지 로직을 우회해 '정상 운영 중'을 흉내)
    from src.state import State

    state_path = tmp_path / "seen.json"
    st = State(state_path)
    st.mark_seen("fss_press", ["old1", "old2"], baselined=True)
    st.save()

    monkeypatch.setattr(main_mod, "build_scraper", lambda src, fetcher: _EmptyScraper(src, fetcher))

    rc = main_mod.run(
        ["--only", "fss_press", "--state", str(state_path), "--dry-run"]
    )
    assert rc == 1


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
