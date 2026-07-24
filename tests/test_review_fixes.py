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
    r = sc.collect(limit=10, seen_ids={"a", "b"}, max_pages=5)
    assert [p.post_id for p in r.posts] == []   # a,b 는 seen → 신규 없음
    assert r.reached_boundary is True
    assert r.scanned == 2                        # 2페이지로 안 넘어감


def test_collect_paginates_until_page_fully_seen():
    # 1페이지에 신규가 하나라도 있으면 계속 넘겨서, 전부 seen 인 페이지에서 멈춘다
    sc = _PagedScraper([["a", "b"], ["c", "d"], ["seen1", "seen2"]])
    r = sc.collect(limit=10, seen_ids={"seen1", "seen2"}, max_pages=5)
    assert [p.post_id for p in r.posts] == ["a", "b", "c", "d"]  # 신규만
    assert r.reached_boundary is True


def test_collect_does_not_stop_on_pinned_seen():
    # 고정공지 'pin' 이 매 페이지 상단에 seen 으로 있어도 페이지네이션이 멈추면 안 된다.
    sc = _PagedScraper([["pin", "a", "b"], ["pin", "c", "d"], ["pin", "old1", "old2"]])
    r = sc.collect(limit=10, seen_ids={"pin", "old1", "old2"}, max_pages=5)
    got = [p.post_id for p in r.posts]
    assert "c" in got and "d" in got            # 2페이지 신규까지 도달
    assert r.reached_boundary is True           # 전부 seen 인 3페이지에서 멈춤


def test_collect_stops_when_no_progress():
    # 페이지 파라미터가 무시되어 같은 목록만 반복돼도 무한루프에 빠지지 않는다
    sc = _PagedScraper([["a", "b"], ["a", "b"], ["a", "b"]])
    r = sc.collect(limit=10, seen_ids=set(), max_pages=10)
    assert [p.post_id for p in r.posts] == ["a", "b"]
    assert r.reached_boundary is True


def test_collect_backlog_over_cap_warns_not_boundary():
    # max_pages 안에 경계 미도달(한 실행 범위 초과 대량 신규) → reached_boundary=False.
    pages = [[f"p{n}i{j}" for j in range(2)] for n in range(10)]  # 전부 신규
    sc = _PagedScraper(pages)
    r = sc.collect(limit=10, seen_ids=set(), max_pages=3)
    assert len(r.posts) == 6            # 3페이지 * 2건만 이번에 수집
    assert r.reached_boundary is False  # 경계 미도달(운영 경고 대상)


def test_collect_propagates_fetch_error():
    # 페이지 fetch 실패는 예외로 전파되어야 한다([]로 삼키면 '목록 끝'으로 오인).
    class _Failing(_PagedScraper):
        def fetch_list(self, limit, page=1):
            if page >= 2:
                raise RuntimeError("boom")
            return super().fetch_list(limit, page)

    sc = _Failing([["a", "b"], ["c", "d"]])
    with pytest.raises(RuntimeError):
        sc.collect(limit=10, seen_ids=set(), max_pages=5)


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


class _PagedFull(_PagedScraper):
    """가변 멤버십 소스(계류의안)처럼 최초 기준선을 전체로 잡아야 하는 스크래퍼."""

    FULL_BASELINE = True


def test_full_baseline_records_all_for_mutable_source(tmp_path, monkeypatch):
    # FULL_BASELINE 소스는 얕은 baseline_pages(3)가 아니라 목록 끝까지 전체를 기록한다.
    import src.main as main_mod
    from src.state import State

    state_path = tmp_path / "seen.json"  # 비어 있음 → 최초 실행(미baseline)
    pages = [[f"b{n}_{j}" for j in range(30)] for n in range(10)]  # 10페이지 × 30 = 300
    sc = _PagedFull(pages)
    monkeypatch.setattr(main_mod, "build_scraper", lambda src, fetcher: sc)

    rc = main_mod.run(["--only", "assembly_bill", "--state", str(state_path)])
    assert rc == 0
    st = State(state_path)
    assert st.is_baselined("assembly_bill")
    # 3페이지(90)가 아니라 전체 300건을 기준선으로 기록
    assert len(st.seen_ids("assembly_bill")) == 300


class _FloodScraper(BaseScraper):
    """baselined 소스에서 대량 신규가 잡히는 상황(상태 불일치 등)을 흉내낸다."""

    def __init__(self, source, fetcher):
        super().__init__(source, fetcher)
        self.enrich_calls = 0

    def fetch_list(self, limit, page=1):
        if page > 1:
            return []
        return [
            Post(source_key=self.key, source_name=self.name, post_id=f"n{i}",
                 title=f"t{i}", url=f"http://x/{i}")
            for i in range(200)
        ]

    def enrich(self, post):
        self.enrich_calls += 1


def test_flood_cap_limits_enrich(tmp_path, monkeypatch):
    # 대량 신규(200건)라도 상세수집/발송은 상한(max_new_per_source)까지만,
    # 단 신규 전체는 seen 처리되어 다음 실행에 재발생하지 않는다.
    import src.main as main_mod
    from src.state import State

    state_path = tmp_path / "seen.json"
    st = State(state_path)
    st.mark_seen("fss_press", ["seed"], baselined=True)  # 운영 중(baselined) 상태
    st.save()

    made = {}

    def _factory(src, fetcher):
        sc = _FloodScraper(src, fetcher)
        made[src.key] = sc
        return sc

    monkeypatch.setattr(main_mod, "build_scraper", _factory)
    rc = main_mod.run(["--only", "fss_press", "--state", str(state_path), "--dry-run"])
    assert rc == 0
    # 200건 신규라도 기본 상한(50건)까지만 상세수집(enrich) → 폭주 방지
    assert made["fss_press"].enrich_calls == 50


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
