"""실행 종료 집계 로그(상세 수집 / AI 요약)와 상세 URL 폴백 설정 테스트."""
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import AssemblyBatchConfig, Config, LLMConfig, SourceConfig
from src.main import _log_run_summary
from src.models import Post
from src.summarizer import ai_target_count


def _llm(**over) -> LLMConfig:
    base = dict(
        enabled=True,
        model="m",
        lines=3,
        max_line_chars=90,
        min_body_chars=80,
        max_input_chars=8000,
        max_posts=40,
        rpm=0,
        timeout_sec=5,
        max_retries=0,
        retry_backoff_sec=0,
        assembly_batch=AssemblyBatchConfig(),
    )
    base.update(over)
    return LLMConfig(**base)


class _Cfg:
    def __init__(self, llm):
        self.llm = llm


def _p(source_key="fsc_press", body="", summary=None, details=None, **over):
    kw = dict(
        source_key=source_key,
        source_name="금융위 · 보도자료",
        post_id="1",
        title="제목",
        url="https://example.com/1",
        body=body,
    )
    kw.update(over)
    p = Post(**kw)
    p.summary = summary or []
    p.details = details or []
    return p


_LONG = "가" * 200   # min_body_chars(80) 를 넘는 본문


# --- ai_target_count: 요약 경로와 같은 규칙을 써야 한다 ---


def test_target_count_uses_min_body_chars_for_general_posts():
    cfg = _llm(min_body_chars=80)
    posts = {
        "금융위 · 보도자료": [
            _p(body=_LONG),        # 대상
            _p(body="너무 짧음"),   # min_body_chars 미달 → 대상 아님
            _p(body=""),           # 본문 없음 → 대상 아님
        ]
    }
    assert ai_target_count(cfg, posts) == 1


def test_target_count_ignores_min_body_chars_for_bills():
    # 의안 배치는 min_body_chars 를 쓰지 않는다(본문 유무만 본다).
    cfg = _llm(min_body_chars=80)
    posts = {
        "의안정보시스템 · 계류의안": [
            _p(source_key="assembly_bill", body="짧은 제안이유"),   # 대상
            _p(source_key="assembly_bill", body="   "),           # 공백뿐 → 아님
        ]
    }
    assert ai_target_count(cfg, posts) == 1


def test_target_count_excludes_structured_detail_posts():
    # details 가 있는 글(검사결과 제재)은 애초에 요약 경로를 타지 않는다.
    cfg = _llm()
    posts = {
        "금감원 · 검사결과 제재": [
            _p(source_key="fss_sanction", body=_LONG, details=[("금융기관명", "A은행")])
        ]
    }
    assert ai_target_count(cfg, posts) == 0


def test_target_count_includes_posts_beyond_the_cap():
    # 상한(max_posts)에 걸려 요약되지 못한 글도 발췌로 나가므로 '대상'에 포함한다.
    cfg = _llm(max_posts=2)
    posts = {"금융위 · 보도자료": [_p(body=_LONG) for _ in range(5)]}
    assert ai_target_count(cfg, posts) == 5


# --- 집계 로그 ---


def test_logs_detail_and_ai_counts(caplog):
    cfg = _Cfg(_llm())
    posts = {
        "금융위 · 보도자료": [
            _p(body=_LONG, summary=["a", "b", "c"]),
            _p(body=_LONG),                              # 요약 실패 → 발췌
        ]
    }
    with caplog.at_level(logging.INFO, logger="law_rader"):
        _log_run_summary(cfg, posts, detail_attempted=5, detail_succeeded=3)

    text = caplog.text
    assert "상세 수집 집계 — 시도 5건 / 성공 3건 / 실패 2건" in text
    assert "AI 요약 집계 — 대상 2건 / 요약 1건 / 발췌 폴백 1건" in text


def test_zero_detail_success_logs_error(caplog):
    cfg = _Cfg(_llm())
    posts = {"금융위 · 보도자료": [_p(body="")]}
    with caplog.at_level(logging.INFO, logger="law_rader"):
        _log_run_summary(cfg, posts, detail_attempted=7, detail_succeeded=0)

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert "성공률 0%" in errors[0].getMessage()
    # 집계 로그는 그대로 남는다(ERROR 가 나머지를 삼키지 않는다)
    assert "상세 수집 집계 — 시도 7건 / 성공 0건 / 실패 7건" in caplog.text


def test_no_error_when_nothing_was_attempted(caplog):
    # 신규가 없으면 상세 수집 시도도 0이다. 이때는 장애가 아니므로 ERROR 를 내지 않는다.
    cfg = _Cfg(_llm())
    with caplog.at_level(logging.INFO, logger="law_rader"):
        _log_run_summary(cfg, {}, detail_attempted=0, detail_succeeded=0)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert "시도 0건 / 성공 0건 / 실패 0건" in caplog.text


def test_no_error_when_some_details_succeeded(caplog):
    cfg = _Cfg(_llm())
    with caplog.at_level(logging.INFO, logger="law_rader"):
        _log_run_summary(cfg, {}, detail_attempted=10, detail_succeeded=1)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


# --- 0% 여도 메일 발송은 계속된다 (main.run 통합) ---


def test_zero_detail_success_still_sends_mail(tmp_path, monkeypatch, caplog):
    from src import main as main_mod
    from src.scrapers.base import CollectResult
    from src.state import State

    state_path = tmp_path / "seen.json"
    st = State(state_path)
    st.mark_seen("fss_press", ["old"], baselined=True)
    st.save()

    # enrich 가 아무것도 채우지 못하는 상황(마크업 변경 등)
    fresh = Post(
        source_key="fss_press",
        source_name="금감원 · 보도자료",
        post_id="new1",
        title="제목",
        url="https://example.com/new1",
    )

    class _FakeScraper:
        def collect(self, limit, seen_ids, max_pages):
            return CollectResult(posts=[fresh], reached_boundary=True, scanned=1)

        def enrich(self, post):
            return None      # 본문·첨부·details 아무것도 채우지 못함

    sent = {"n": 0}
    monkeypatch.setenv("SMTP_USER", "sender@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    monkeypatch.setenv("MAIL_TO", "to@example.com")
    monkeypatch.setattr(main_mod, "verify_smtp_login", lambda cfg: None)
    monkeypatch.setattr(main_mod, "summarize_posts", lambda cfg, posts: 0)
    monkeypatch.setattr(
        main_mod, "send_digest", lambda cfg, posts: sent.__setitem__("n", sent["n"] + 1)
    )
    monkeypatch.setattr(main_mod, "build_scraper", lambda src, fetcher: _FakeScraper())

    with caplog.at_level(logging.INFO, logger="law_rader"):
        rc = main_mod.run(["--state", str(state_path), "--only", "fss_press"])

    assert rc == 0                 # 실패로 끝내지 않는다
    assert sent["n"] == 1          # 메일은 발송된다
    assert any("성공률 0%" in r.getMessage() for r in caplog.records
               if r.levelno >= logging.ERROR)


def test_detail_success_is_counted_from_filled_fields(tmp_path, monkeypatch, caplog):
    from src import main as main_mod
    from src.scrapers.base import CollectResult
    from src.state import State

    state_path = tmp_path / "seen.json"
    st = State(state_path)
    st.mark_seen("fss_press", ["old"], baselined=True)
    st.save()

    posts = [
        Post(source_key="fss_press", source_name="금감원 · 보도자료",
             post_id=f"n{i}", title="제목", url=f"https://example.com/{i}")
        for i in range(3)
    ]

    class _FakeScraper:
        def collect(self, limit, seen_ids, max_pages):
            return CollectResult(posts=posts, reached_boundary=True, scanned=3)

        def enrich(self, post):
            if post.post_id == "n0":
                post.body = "본문 채움"      # 성공
            elif post.post_id == "n1":
                raise RuntimeError("상세 500")  # 실패(예외)
            # n2 는 예외 없이 아무것도 못 채움 → 실패로 센다

    monkeypatch.setenv("SMTP_USER", "s@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    monkeypatch.setenv("MAIL_TO", "to@example.com")
    monkeypatch.setattr(main_mod, "verify_smtp_login", lambda cfg: None)
    monkeypatch.setattr(main_mod, "summarize_posts", lambda cfg, p: 0)
    monkeypatch.setattr(main_mod, "send_digest", lambda cfg, p: None)
    monkeypatch.setattr(main_mod, "build_scraper", lambda src, fetcher: _FakeScraper())

    with caplog.at_level(logging.INFO, logger="law_rader"):
        main_mod.run(["--state", str(state_path), "--only", "fss_press"])

    assert "상세 수집 집계 — 시도 3건 / 성공 1건 / 실패 2건" in caplog.text
    # 일부라도 성공하면 ERROR 는 없다
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


# --- 상세 URL 폴백 설정(요구 5의 교정 경로) ---


def test_detail_url_fallback_is_configurable(monkeypatch):
    from src.scrapers.assembly import AssemblyBillScraper

    monkeypatch.setenv("ASSEMBLY_API_KEY", "dummy")
    new_path = "https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId={bill_id}"
    src = SourceConfig(
        key="assembly_bill", name="a", type="assembly_bill",
        list_url="https://likms.assembly.go.kr/bill/",
        extra={"api_service": "svc", "detail_url": new_path},
    )
    sc = AssemblyBillScraper(src, fetcher=None)

    class _R:
        def json(self):
            return {"svc": [{"head": []}, {"row": [{"BILL_ID": "PRC_A1",
                                                    "BILL_NAME": "테스트법률안"}]}]}

    class _F:
        def get(self, *a, **k):
            return _R()

    sc.fetcher = _F()
    posts = sc.fetch_list(30, page=1)
    assert posts[0].url == (
        "https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId=PRC_A1"
    )


def test_link_url_from_api_still_wins_over_fallback(monkeypatch):
    # 공식 LINK_URL 이 있으면 폴백 템플릿을 쓰지 않는다(경로 변경에 자동으로 따라감).
    from src.scrapers.assembly import AssemblyBillScraper

    monkeypatch.setenv("ASSEMBLY_API_KEY", "dummy")
    src = SourceConfig(
        key="assembly_bill", name="a", type="assembly_bill",
        list_url="https://likms.assembly.go.kr/bill/",
        extra={"api_service": "svc", "detail_url": "https://example.com/{bill_id}"},
    )
    sc = AssemblyBillScraper(src, fetcher=None)
    official = "https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId=PRC_A1"

    class _R:
        def json(self):
            return {"svc": [{"head": []}, {"row": [
                {"BILL_ID": "PRC_A1", "BILL_NAME": "테스트법률안", "LINK_URL": official}
            ]}]}

    class _F:
        def get(self, *a, **k):
            return _R()

    sc.fetcher = _F()
    assert sc.fetch_list(30, page=1)[0].url == official
