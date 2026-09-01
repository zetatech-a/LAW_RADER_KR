"""실행 종료 집계 로그(상세 수집 / AI 요약)와 상세 URL 폴백 설정 테스트."""
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import AssemblyBatchConfig, LLMConfig, SourceConfig
from src.main import _DetailStats, _log_ai_summary, _log_detail_summary
from src.models import Post
from src.summarizer import ai_target_count


def _log_run_summary(cfg, posts, detail, assembly) -> None:
    """두 집계를 이어서 부르는 테스트 편의 함수.

    run() 은 시점을 나눠 부른다(수집 집계는 발송 판단 전, AI 집계는 요약 후). 로그
    '내용' 검증은 순서와 무관하므로 여기서는 한 번에 부른다.
    """
    _log_detail_summary(detail, assembly)
    _log_ai_summary(cfg, posts)


def _stats(attempted: int, succeeded: int) -> _DetailStats:
    return _DetailStats(attempted=attempted, succeeded=succeeded)


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
        _log_run_summary(cfg, posts, _stats(5, 3), _stats(2, 1))

    text = caplog.text
    assert "상세 수집 집계(전체) — 시도 5건 / 성공 3건 / 등록대기 0건 / 실패 2건" in text
    assert "의안 제안이유 집계 — attempted 2 / available 1 / pending 0 / failed 1" in text
    assert "AI 요약 집계 — 대상 2건 / 요약 1건 / 발췌 폴백 1건" in text


def test_zero_detail_success_logs_error(caplog):
    cfg = _Cfg(_llm())
    posts = {"금융위 · 보도자료": [_p(body="")]}
    with caplog.at_level(logging.INFO, logger="law_rader"):
        _log_run_summary(cfg, posts, _stats(7, 0), _stats(0, 0))

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert "성공률 0%" in errors[0].getMessage()
    assert "의안" not in errors[0].getMessage()   # 의안 시도가 0이면 의안 ERROR 는 없다
    # 집계 로그는 그대로 남는다(ERROR 가 나머지를 삼키지 않는다)
    assert "상세 수집 집계(전체) — 시도 7건 / 성공 0건 / 등록대기 0건 / 실패 7건" in caplog.text


def test_no_error_when_nothing_was_attempted(caplog):
    # 신규가 없으면 상세 수집 시도도 0이다. 이때는 장애가 아니므로 ERROR 를 내지 않는다.
    cfg = _Cfg(_llm())
    with caplog.at_level(logging.INFO, logger="law_rader"):
        _log_run_summary(cfg, {}, _stats(0, 0), _stats(0, 0))
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert "상세 수집 집계(전체) — 시도 0건 / 성공 0건 / 등록대기 0건 / 실패 0건" in caplog.text
    assert "의안 제안이유 집계 — attempted 0 / available 0 / pending 0 / failed 0" in caplog.text


def test_no_error_when_some_details_succeeded(caplog):
    cfg = _Cfg(_llm())
    with caplog.at_level(logging.INFO, logger="law_rader"):
        _log_run_summary(cfg, {}, _stats(10, 1), _stats(4, 1))
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


# --- 의안 전용 집계: 다른 소스에 가려지면 안 된다 ---


def test_assembly_total_failure_is_reported_even_when_others_succeed(caplog):
    # 금융위·금감원이 성공하면 합계는 멀쩡해 보인다. 의안 전면 실패가 묻히면 안 된다.
    cfg = _Cfg(_llm())
    with caplog.at_level(logging.INFO, logger="law_rader"):
        _log_run_summary(cfg, {}, _stats(20, 17), _stats(3, 0))

    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert "의안 제안이유 수집 available 0건" in errors[0]
    assert "시도 3건" in errors[0]


def test_no_assembly_error_when_only_other_sources_failed(caplog):
    # 상세 본문이 원래 없는 소스(회신사례 등)가 실패해도 의안 ERROR 를 내면 안 된다.
    cfg = _Cfg(_llm())
    with caplog.at_level(logging.INFO, logger="law_rader"):
        _log_run_summary(cfg, {}, _stats(10, 2), _stats(0, 0))
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_both_errors_are_emitted_when_everything_failed(caplog):
    # 전부 실패면 두 ERROR 가 모두 나온다(하나가 다른 하나를 대체하지 않는다).
    cfg = _Cfg(_llm())
    with caplog.at_level(logging.INFO, logger="law_rader"):
        _log_run_summary(cfg, {}, _stats(5, 0), _stats(5, 0))

    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 2
    assert any("의안 제안이유 수집 available 0건" in m for m in errors)
    assert any(m.startswith("상세 수집 성공률 0%") for m in errors)


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

    assert "상세 수집 집계(전체) — 시도 3건 / 성공 1건 / 등록대기 0건 / 실패 2건" in caplog.text
    # 의안이 섞이지 않은 실행이므로 의안 집계는 0이고 의안 ERROR 도 없다
    assert "의안 제안이유 집계 — attempted 0 / available 0 / pending 0 / failed 0" in caplog.text
    # 일부라도 성공하면 ERROR 는 없다
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


# --- 혼합 소스 실행 회귀 ---


def _mixed_run(tmp_path, monkeypatch, caplog, *, assembly_ok, fss_ok):
    """금감원 + 의안을 함께 수집하는 실행. 각 소스의 enrich 성공 여부를 지정한다."""
    from src import main as main_mod
    from src.scrapers.base import CollectResult
    from src.state import State

    state_path = tmp_path / "seen.json"
    st = State(state_path)
    for key in ("fss_press", "assembly_bill"):
        st.mark_seen(key, ["old"], baselined=True)
    st.save()

    made = {
        "fss_press": [
            Post(source_key="fss_press", source_name="금감원 · 보도자료",
                 post_id=f"f{i}", title="보도자료", url=f"https://fss/{i}")
            for i in range(3)
        ],
        "assembly_bill": [
            Post(source_key="assembly_bill", source_name="의안정보시스템 · 계류의안",
                 post_id=f"PRC_{i}", title="법률안", url=f"https://likms/{i}")
            for i in range(2)
        ],
    }

    class _FakeScraper:
        def __init__(self, key):
            self.key = key

        def collect(self, limit, seen_ids, max_pages):
            posts = made[self.key]
            return CollectResult(posts=posts, reached_boundary=True, scanned=len(posts))

        def enrich(self, post):
            ok = assembly_ok if self.key == "assembly_bill" else fss_ok
            if ok:
                post.body = "본문 " * 50

    monkeypatch.setenv("SMTP_USER", "s@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    monkeypatch.setenv("MAIL_TO", "to@example.com")
    monkeypatch.setattr(main_mod, "verify_smtp_login", lambda cfg: None)
    monkeypatch.setattr(main_mod, "summarize_posts", lambda cfg, p: 0)
    sent = {"n": 0}
    monkeypatch.setattr(
        main_mod, "send_digest", lambda cfg, p: sent.__setitem__("n", sent["n"] + 1)
    )
    monkeypatch.setattr(
        main_mod, "build_scraper", lambda src, fetcher: _FakeScraper(src.key)
    )

    with caplog.at_level(logging.INFO, logger="law_rader"):
        rc = main_mod.run(
            ["--state", str(state_path), "--only", "fss_press,assembly_bill"]
        )
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    return rc, sent["n"], errors, caplog.text


def test_mixed_run_reports_assembly_failure_despite_other_source_success(
    tmp_path, monkeypatch, caplog
):
    # 핵심 회귀: 금감원 3건 성공 + 의안 2건 전멸 → 합계는 3/5 라 전체 ERROR 는 없지만
    # 의안 ERROR 는 반드시 나와야 한다.
    rc, sent, errors, text = _mixed_run(
        tmp_path, monkeypatch, caplog, assembly_ok=False, fss_ok=True
    )
    assert rc == 0 and sent == 1                       # 메일은 정상 발송
    assert "상세 수집 집계(전체) — 시도 5건 / 성공 3건 / 등록대기 0건 / 실패 2건" in text
    assert "의안 제안이유 집계 — attempted 2 / available 0 / pending 0 / failed 2" in text
    assert len(errors) == 1
    assert "의안 제안이유 수집 available 0건" in errors[0]


def test_mixed_run_stays_quiet_when_assembly_succeeds(tmp_path, monkeypatch, caplog):
    # 반대 방향: 의안만 성공하고 금감원이 실패해도 의안 ERROR 는 없어야 한다.
    rc, sent, errors, text = _mixed_run(
        tmp_path, monkeypatch, caplog, assembly_ok=True, fss_ok=False
    )
    assert rc == 0 and sent == 1
    assert "의안 제안이유 집계 — attempted 2 / available 2 / pending 0 / failed 0" in text
    assert errors == []          # 합계도 2/5 라 전체 ERROR 도 없다


def test_mixed_run_all_success_has_no_errors(tmp_path, monkeypatch, caplog):
    rc, sent, errors, text = _mixed_run(
        tmp_path, monkeypatch, caplog, assembly_ok=True, fss_ok=True
    )
    assert rc == 0 and sent == 1
    assert "상세 수집 집계(전체) — 시도 5건 / 성공 5건 / 등록대기 0건 / 실패 0건" in text
    assert errors == []


def test_mixed_run_all_failure_emits_both_errors(tmp_path, monkeypatch, caplog):
    rc, sent, errors, text = _mixed_run(
        tmp_path, monkeypatch, caplog, assembly_ok=False, fss_ok=False
    )
    assert rc == 0 and sent == 1          # 전면 실패여도 메일은 나간다
    assert len(errors) == 2
    assert any("의안 제안이유 수집 available 0건" in m for m in errors)


# --- 조기 종료해도 수집 집계는 남는다 ---


def _run_with_early_exit(tmp_path, monkeypatch, caplog, *, break_smtp):
    """의안 2건을 수집한 뒤 메일 단계에서 막히는 실행.

    break_smtp=False 면 메일 설정 자체가 없어 preflight 에서 멈춘다.
    """
    from src import main as main_mod
    from src.models import ProposalContentStatus
    from src.scrapers.base import CollectResult
    from src.state import State

    state_path = tmp_path / "seen.json"
    st = State(state_path)
    st.mark_seen("assembly_bill", ["old"], baselined=True)
    st.save()

    made = [
        Post(source_key="assembly_bill", source_name="의안정보시스템 · 계류의안",
             post_id=f"PRC_{i}", title="법률안", url=f"https://likms/{i}")
        for i in range(2)
    ]

    class _FakeScraper:
        def collect(self, limit, seen_ids, max_pages):
            return CollectResult(posts=made, reached_boundary=True, scanned=2)

        def enrich(self, post):
            # 한 건은 등록 대기, 한 건은 수집 실패 — 진단이 필요한 상황
            if post.post_id == "PRC_0":
                post.proposal_status = ProposalContentStatus.PENDING

    if break_smtp:
        monkeypatch.setenv("SMTP_USER", "s@example.com")
        monkeypatch.setenv("SMTP_PASSWORD", "pw")
        monkeypatch.setenv("MAIL_TO", "to@example.com")

        def _boom(cfg):
            raise RuntimeError("535 authentication failed")

        monkeypatch.setattr(main_mod, "verify_smtp_login", _boom)
    else:
        for var in ("SMTP_USER", "SMTP_PASSWORD", "MAIL_TO"):
            monkeypatch.delenv(var, raising=False)

    called = {"llm": 0, "sent": 0}
    monkeypatch.setattr(
        main_mod, "summarize_posts", lambda c, p: called.__setitem__("llm", 1)
    )
    monkeypatch.setattr(
        main_mod, "send_digest", lambda c, p: called.__setitem__("sent", 1)
    )
    monkeypatch.setattr(main_mod, "build_scraper", lambda src, fetcher: _FakeScraper())

    with caplog.at_level(logging.INFO, logger="law_rader"):
        rc = main_mod.run(["--state", str(state_path), "--only", "assembly_bill"])
    return rc, called, caplog.text


@pytest.mark.parametrize("break_smtp", [False, True])
def test_detail_summary_survives_mail_preflight_exit(
    tmp_path, monkeypatch, caplog, break_smtp
):
    """회귀: 메일 장애로 조기 종료해도 상세 수집 집계는 남아야 한다.

    상세 요청은 이미 다 돌아 통계가 손에 있는데, 하필 장애 상황에서 진단 로그가
    통째로 사라지면 무엇이 고장인지 알 방법이 없어진다.
    """
    rc, called, text = _run_with_early_exit(
        tmp_path, monkeypatch, caplog, break_smtp=break_smtp
    )
    assert rc == 1                       # 발송은 못 했다
    assert called == {"llm": 0, "sent": 0}   # 요약·발송에는 손대지 않았다
    assert "상세 수집 집계(전체) — 시도 2건 / 성공 0건 / 등록대기 1건 / 실패 1건" in text
    assert "의안 제안이유 집계 — attempted 2 / available 0 / pending 1 / failed 1" in text


def test_detail_summary_survives_total_collection_failure(tmp_path, monkeypatch, caplog):
    """전 소스 수집 실패로 rc=1 종료할 때도 집계는 남는다."""
    from src import main as main_mod
    from src.state import State

    state_path = tmp_path / "seen.json"
    State(state_path).save()

    class _Broken:
        def collect(self, limit, seen_ids, max_pages):
            raise RuntimeError("목록 500")

    monkeypatch.setattr(main_mod, "build_scraper", lambda src, fetcher: _Broken())
    with caplog.at_level(logging.INFO, logger="law_rader"):
        rc = main_mod.run(["--state", str(state_path), "--only", "assembly_bill"])

    assert rc == 1
    assert "상세 수집 집계(전체) — 시도 0건 / 성공 0건 / 등록대기 0건 / 실패 0건" in caplog.text


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


def _assembly_urls(monkeypatch, rows, extra=None):
    """rows 로 fetch_list 를 돌려 생성된 상세 URL 목록을 돌려준다."""
    from src.scrapers.assembly import AssemblyBillScraper

    monkeypatch.setenv("ASSEMBLY_API_KEY", "dummy")
    base = {"api_service": "svc"}
    base.update(extra or {})
    src = SourceConfig(
        key="assembly_bill", name="a", type="assembly_bill",
        list_url="https://likms.assembly.go.kr/bill/", extra=base,
    )
    sc = AssemblyBillScraper(src, fetcher=None)

    class _R:
        def json(self):
            return {"svc": [{"head": []}, {"row": rows}]}

    class _F:
        def get(self, *a, **k):
            return _R()

    sc.fetcher = _F()
    return [p.url for p in sc.fetch_list(30, page=1)]


_CANON = "https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId=PRC_A1"


def test_default_detail_url_is_current_path(monkeypatch):
    # LINK_URL 이 없으면 현재 경로(/bill/bi/billDetailPage.do)로 만든다.
    urls = _assembly_urls(monkeypatch, [{"BILL_ID": "PRC_A1", "BILL_NAME": "법률안"}])
    assert urls == [_CANON]


@pytest.mark.parametrize(
    "link",
    [
        "https://likms.assembly.go.kr/bill/billDetail.do?billId=PRC_A1",
        "https://likms.assembly.go.kr/bill/billDetail.do?billId=PRC_A1&ageFrom=22",
        "https://likms.assembly.go.kr/bill/BillDetail.do?billId=PRC_A1",   # 대소문자
        "https://likms.assembly.go.kr/bill/jsp/BillDetail.jsp?bill_id=PRC_A1",
    ],
)
def test_dead_legacy_link_url_is_canonicalized(monkeypatch, link):
    # 라이브 확인(2026-08): Open API 의 LINK_URL 은 아직 구 경로를 주는데, 최신 의안에서
    # 그 경로는 "해당 의안 정보가 존재하지 않습니다" 를 응답한다. BILL_ID 로 다시 만든다.
    urls = _assembly_urls(
        monkeypatch, [{"BILL_ID": "PRC_A1", "BILL_NAME": "법률안", "LINK_URL": link}]
    )
    assert urls == [_CANON]


def test_current_path_link_url_is_kept(monkeypatch):
    # 이미 현재 경로면 그대로 둔다(쿼리 파라미터도 보존).
    link = _CANON + "&ageFrom=22"
    urls = _assembly_urls(
        monkeypatch, [{"BILL_ID": "PRC_A1", "BILL_NAME": "법률안", "LINK_URL": link}]
    )
    assert urls == [link]


def test_unknown_link_url_path_is_respected(monkeypatch):
    # 우리가 모르는 상세 경로(예산안·청원 전용 등)는 공식 값이므로 존중한다.
    link = "https://likms.assembly.go.kr/bill/bi/budget/budgetDetail.do?billId=PRC_A1"
    urls = _assembly_urls(
        monkeypatch, [{"BILL_ID": "PRC_A1", "BILL_NAME": "법률안", "LINK_URL": link}]
    )
    assert urls == [link]


def test_detail_url_override_applies_to_canonicalized_links(monkeypatch):
    # config 로 덮어쓴 템플릿이 canonicalize 결과에도 적용된다.
    tmpl = "https://likms.assembly.go.kr/bill/bi/other.do?billId={bill_id}"
    urls = _assembly_urls(
        monkeypatch,
        [{"BILL_ID": "PRC_A1", "BILL_NAME": "법률안",
          "LINK_URL": "https://likms.assembly.go.kr/bill/billDetail.do?billId=PRC_A1"}],
        extra={"detail_url": tmpl},
    )
    assert urls == ["https://likms.assembly.go.kr/bill/bi/other.do?billId=PRC_A1"]


# --- 상세 수집이 원래 불가능한 소스는 실패로 세지 않는다 ---


def _run_with_scraper(tmp_path, monkeypatch, caplog, scraper_cls, keys):
    from src import main as main_mod
    from src.scrapers.base import CollectResult
    from src.state import State

    state_path = tmp_path / "seen.json"
    st = State(state_path)
    for key in keys:
        st.mark_seen(key, ["old"], baselined=True)
    st.save()

    made = {
        key: [
            Post(source_key=key, source_name=f"{key} 소스",
                 post_id=f"{key}-{i}", title="제목", url=f"https://x/{key}/{i}")
            for i in range(2)
        ]
        for key in keys
    }

    monkeypatch.setenv("SMTP_USER", "s@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    monkeypatch.setenv("MAIL_TO", "to@example.com")
    monkeypatch.setattr(main_mod, "verify_smtp_login", lambda cfg: None)
    monkeypatch.setattr(main_mod, "summarize_posts", lambda c, p: 0)
    sent = {"n": 0}
    monkeypatch.setattr(
        main_mod, "send_digest", lambda c, p: sent.__setitem__("n", sent["n"] + 1)
    )
    monkeypatch.setattr(
        main_mod, "build_scraper", lambda src, fetcher: scraper_cls(src.key, made)
    )

    with caplog.at_level(logging.INFO, logger="law_rader"):
        rc = main_mod.run(["--state", str(state_path), "--only", ",".join(keys)])
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    return rc, sent["n"], errors, caplog.text


class _NoEnrichScraper:
    """SUPPORTS_ENRICH=False 를 선언한(=상세를 수집할 수 없는) 가상의 소스."""

    SUPPORTS_ENRICH = False

    def __init__(self, key, made):
        self.key = key
        self._made = made

    def collect(self, limit, seen_ids, max_pages):
        from src.scrapers.base import CollectResult

        posts = self._made[self.key]
        return CollectResult(posts=posts, reached_boundary=True, scanned=len(posts))

    def enrich(self, post):        # pragma: no cover — 호출되지 않아야 한다
        raise AssertionError("상세 수집 대상이 아닌 소스에 enrich 를 부르면 안 된다")


def test_unenrichable_source_does_not_trigger_zero_success_error(
    tmp_path, monkeypatch, caplog
):
    """회귀: SUPPORTS_ENRICH=False 인 소스만 신규인 실행이 '성공률 0%' 장애 경보를
    내면 안 된다.

    그런 소스는 본문이 비는 것이 **정상**이다. 실패로 세면 정상 실행마다 거짓 ERROR 가
    찍혀 진짜 파서 장애가 그 잡음에 묻힌다. (키는 config 에 있는 아무 소스나 써도
    되며, 스크래퍼는 위 가짜 클래스로 대체된다.)
    """
    rc, sent, errors, text = _run_with_scraper(
        tmp_path, monkeypatch, caplog, _NoEnrichScraper, ["better_reply"]
    )
    assert rc == 0 and sent == 1                  # 메일은 정상 발송
    assert errors == []                           # 거짓 장애 경보 없음
    assert "상세 수집 집계(전체) — 시도 0건" in text   # 시도 자체를 세지 않는다
    assert "상세 수집 대상 아님" in text


def test_real_better_reply_scraper_now_supports_enrich():
    """회신사례는 법령해석·비조치의견서 상세를 수집한다 — 상세 통계에 포함되어야 한다.

    (예전에는 상세가 JS 팝업이라 SUPPORTS_ENRICH=False 였다. 상세 GET 주소가 확인되어
     본문·첨부를 수집하게 되었으므로, 이제는 수집 실패가 통계에 잡혀야 한다.)
    """
    from src.scrapers import build_scraper
    from src.scrapers.base import BaseScraper
    from src.scrapers.better_fsc import BetterReplyScraper

    assert BetterReplyScraper.SUPPORTS_ENRICH is True
    assert BaseScraper.SUPPORTS_ENRICH is True     # 기본은 '수집한다'
    src = SourceConfig(
        key="better_reply", name="회신사례", type="better_reply",
        list_url="https://better.fsc.go.kr/", extra={},
    )
    assert build_scraper(src, fetcher=None).SUPPORTS_ENRICH is True


class _PerPostNoEnrichScraper(_NoEnrichScraper):
    """소스는 상세 수집이 가능하지만 개별 글은 대상이 아닌 스크래퍼(회신사례 유형)."""

    SUPPORTS_ENRICH = True

    def supports_enrich(self, post):
        return False

    def enrich(self, post):        # pragma: no cover — 호출되지 않아야 한다
        raise AssertionError("per-post 훅이 False 인 글에 enrich 를 부르면 안 된다")


def test_per_post_unenrichable_posts_are_excluded_from_detail_stats(
    tmp_path, monkeypatch, caplog
):
    """회귀: 상세 주소가 없는 글만 신규인 실행이 '성공률 0%' 경보를 내면 안 된다.

    소스 단위 플래그로는 표현할 수 없는 경우다 — 같은 소스의 다른 글은 상세를 수집한다.
    """
    rc, sent, errors, text = _run_with_scraper(
        tmp_path, monkeypatch, caplog, _PerPostNoEnrichScraper, ["better_reply"]
    )
    assert rc == 0 and sent == 1
    assert errors == []
    assert "상세 수집 집계(전체) — 시도 0건" in text
    assert "상세 수집 대상 아님" in text


class _AttachmentOnlyScraper(_NoEnrichScraper):
    """첨부만 채우고 본문은 못 채우는 스크래퍼 — 계약상 실패를 선언한다."""

    SUPPORTS_ENRICH = True

    def enrich(self, post):
        from src.models import Attachment

        post.attachments.append(Attachment(filename="회신문.hwp", url="https://x/f"))

    def enrich_succeeded(self, post):
        return bool(post.body)      # 회신사례 계약: 본문이 있어야 성공


def test_attachment_only_counts_as_attempted_but_not_succeeded(
    tmp_path, monkeypatch, caplog
):
    """회귀: 첨부만 남고 본문 파서가 깨진 상태가 '성공'으로 잡히면 고장이 묻힌다."""
    rc, sent, errors, text = _run_with_scraper(
        tmp_path, monkeypatch, caplog, _AttachmentOnlyScraper, ["better_reply"]
    )
    assert rc == 0 and sent == 1        # 메일 발송·seen 정책은 그대로
    # 시도에는 포함되고 성공에는 포함되지 않는다.
    assert "상세 수집 집계(전체) — 시도 2건 / 성공 0건 / 등록대기 0건 / 실패 2건" in text


def test_enrichable_sources_still_counted(tmp_path, monkeypatch, caplog):
    """플래그가 없는(=수집 가능한) 소스는 종전대로 실패를 센다."""

    class _Broken(_NoEnrichScraper):
        SUPPORTS_ENRICH = True

        def enrich(self, post):
            return          # 아무것도 못 채움 = 실패

    rc, sent, errors, text = _run_with_scraper(
        tmp_path, monkeypatch, caplog, _Broken, ["fss_press"]
    )
    assert rc == 0 and sent == 1
    assert "상세 수집 집계(전체) — 시도 2건 / 성공 0건 / 등록대기 0건 / 실패 2건" in text
    assert any("성공률 0%" in m for m in errors)


# --- 등록 대기 한 건이 다른 소스의 전면 장애를 가리면 안 된다 ---


def test_pending_bill_does_not_mask_other_sources_total_failure(caplog):
    """회귀: 갓 접수된 의안 한 건 때문에 나머지 소스의 파서 전멸이 묻혔다.

    'pending 이 하나라도 있으면 판정하지 않는다' 로 두면, 등록 대기와 아무 관계 없는
    실패가 통째로 조용해진다. 등록 대기는 애초에 성공할 수 없는 시도이므로 분모에서
    빼고 성공률을 본다.
    """
    cfg = _Cfg(_llm())
    # 의안 1건 등록 대기(정상) + 다른 소스 9건 전부 실패
    overall = _DetailStats(attempted=10, succeeded=0, pending=1)
    assembly = _DetailStats(attempted=1, succeeded=0, pending=1)
    with caplog.at_level(logging.INFO, logger="law_rader"):
        _log_run_summary(cfg, {}, overall, assembly)

    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert errors[0].startswith("상세 수집 성공률 0%")
    assert "9건" in errors[0]                 # 등록 대기를 뺀 시도 건수로 보고한다
    # 의안 자체는 실패가 없으므로 의안 ERROR 는 나오지 않는다
    assert not any("의안 제안이유 수집 available 0건" in m for m in errors)


def test_all_attempts_pending_still_emits_no_error(caplog):
    """요구사항 유지: 시도가 전부 등록 대기면 장애가 아니다."""
    cfg = _Cfg(_llm())
    with caplog.at_level(logging.INFO, logger="law_rader"):
        _log_run_summary(
            cfg, {}, _DetailStats(4, 0, 4), _DetailStats(4, 0, 4)
        )
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_pending_plus_one_success_is_not_an_outage(caplog):
    """등록 대기를 뺀 시도 중 하나라도 성공하면 장애가 아니다."""
    cfg = _Cfg(_llm())
    with caplog.at_level(logging.INFO, logger="law_rader"):
        _log_run_summary(
            cfg, {}, _DetailStats(10, 1, 3), _DetailStats(4, 1, 3)
        )
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_assembly_pending_rule_is_unchanged(caplog):
    """의안 전용 판정은 문서화된 규칙 그대로 — available=0 이어도 pending>0 이면
    '전면 실패' ERROR 를 내지 않고 경고로 남긴다."""
    cfg = _Cfg(_llm())
    assembly = _DetailStats(attempted=5, succeeded=0, pending=1)   # failed 4
    with caplog.at_level(logging.INFO, logger="law_rader"):
        _log_run_summary(cfg, {}, _DetailStats(5, 0, 1), assembly)

    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert not any("의안 제안이유 수집 available 0건" in m for m in messages)
    warnings = [
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert any("의안 제안이유 수집 실패 4건" in m for m in warnings)
