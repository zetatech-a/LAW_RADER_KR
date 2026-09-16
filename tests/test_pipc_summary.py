"""PIPC 두 소스가 **기존** AI 3줄 요약 파이프라인을 그대로 탄다는 회귀 테스트.

PIPC 전용 LLM 경로·프롬프트·모델 설정을 만들지 않았다는 것이 이 파일의 요지다.
그래서 여기서는 새로 만든 것이 하나도 없이, 기존 진입점만 부른다.

  src.summarizer.summarize_posts / Summarizer   — 일반 게시물 1건당 1회 요약
  src.summarizer.ai_target_count                — 요약 대상 집계(같은 판정 규칙)
  src.notifier.build_html / build_text          — 메일 렌더
  src.main.run                                  — --no-llm 포함 전체 흐름

네트워크는 부르지 않는다 — 기존 관행대로 `Summarizer._generate` 만 가짜 응답으로
대체한다(tests/test_summarizer.py 와 같은 방식).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import LLMConfig, SourceConfig
from src.models import Attachment, Post
from src.notifier import build_html, build_text
from src.scrapers.base import CollectResult
from src.scrapers.pipc import PipcBoardScraper
from src.state import State
from src.summarizer import Summarizer, ai_target_count, summarize_posts

PIPC_KEYS = ("pipc_notice", "pipc_press")
PIPC_NAMES = {"pipc_notice": "개인정보위 · 공지사항", "pipc_press": "개인정보위 · 보도자료"}
LINES = ["개인정보 보호법 시행령 개정안 입법예고함", "가명정보 결합 절차 간소화함", "의견제출 기한 10월 21일임"]

_LIST_URL = {
    "pipc_notice": "https://pipc.go.kr/np/cop/bbs/selectBoardList.do?bbsId=BS061&mCode=C010010000",
    "pipc_press": "https://pipc.go.kr/np/cop/bbs/selectBoardList.do?bbsId=BS074&mCode=C020010000",
}
BODY = (
    "개인정보보호위원회는 「개인정보 보호법 시행령」 일부개정령안을 마련하여 "
    "2026년 9월 11일부터 10월 21일까지 입법예고한다고 밝혔습니다. 이번 개정안은 "
    "가명정보 결합 절차를 간소화하고 공공기관의 개인정보 영향평가 대상 범위를 "
    "명확히 하는 내용을 담고 있습니다."
)


def _cfg(**over) -> LLMConfig:
    base = dict(
        enabled=True, model="gemini-flash-latest", lines=3, max_line_chars=90,
        min_body_chars=10, max_input_chars=8000, max_posts=10, rpm=0,
        timeout_sec=5, max_retries=0, retry_backoff_sec=0, api_key="test-key",
    )
    base.update(over)
    return LLMConfig(**base)


def _pipc_post(key, pid="12503", body=BODY, name=None) -> Post:
    return Post(
        source_key=key,
        source_name=name or PIPC_NAMES.get(key, key),
        post_id=f"nttId:{pid}",
        title="개인정보 보호법 시행령 일부개정령안 입법예고 안내",
        url=f"https://pipc.go.kr/np/cop/bbs/selectBoardArticle.do?nttId={pid}",
        date="2026-09-11",
        body=body,
    )


def _envelope(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]}


def _stub(summarizer, sink: list):
    """기존 LLM 경계(_generate)만 대체하고 프롬프트를 기록한다."""

    def _generate(prompt, deadline=None):
        sink.append(prompt)
        return _envelope(json.dumps({"summary": LINES}, ensure_ascii=False))

    summarizer._generate = _generate
    return summarizer


# --------------------------------------------------------- 요약 경로 진입 여부

def test_both_pipc_sources_are_ai_summary_targets():
    """요약 대상 판정(기존 규칙)에 PIPC 가 그대로 포함된다."""
    cfg = _cfg()
    posts = {PIPC_NAMES[k]: [_pipc_post(k)] for k in PIPC_KEYS}
    assert ai_target_count(cfg, posts) == 2


def test_pipc_posts_get_the_existing_three_line_summary():
    cfg = _cfg()
    prompts: list[str] = []
    posts = {PIPC_NAMES[k]: [_pipc_post(k)] for k in PIPC_KEYS}
    _stub(Summarizer(cfg), prompts).summarize_all(posts)

    flat = [p for group in posts.values() for p in group]
    assert len(flat) == 2
    for p in flat:
        # 기존 소스와 **같은** 저장 위치(Post.summary)에 같은 형식으로 담긴다.
        assert p.summary == LINES
        assert len(p.summary) == cfg.lines

    # 두 소스 모두 각자 한 번씩 호출됐고, 입력은 enrich 로 채운 post.body 다.
    assert len(prompts) == 2
    for prompt in prompts:
        assert BODY in prompt
        assert "[기관]" in prompt and "[본문]" in prompt   # 기존 공통 프롬프트


def test_summarize_posts_entrypoint_covers_pipc(monkeypatch):
    """main 이 부르는 진입점(summarize_posts)이 PIPC 를 요약한다."""
    prompts: list[str] = []
    real_init = Summarizer.__init__

    def _init(self, cfg):
        real_init(self, cfg)
        _stub(self, prompts)

    monkeypatch.setattr(Summarizer, "__init__", _init)
    posts = {PIPC_NAMES["pipc_press"]: [_pipc_post("pipc_press", "12500")]}
    assert summarize_posts(_cfg(), posts) == 1
    assert posts[PIPC_NAMES["pipc_press"]][0].summary == LINES
    assert len(prompts) == 1


def test_short_pipc_body_falls_back_to_excerpt_like_other_sources():
    """min_body_chars 미만은 기존 규칙대로 호출하지 않는다(PIPC 예외 없음)."""
    cfg = _cfg(min_body_chars=200)
    prompts: list[str] = []
    posts = {PIPC_NAMES["pipc_notice"]: [_pipc_post("pipc_notice", body="짧은 공지")]}
    assert _stub(Summarizer(cfg), prompts).summarize_all(posts) == 0
    assert prompts == []
    assert posts[PIPC_NAMES["pipc_notice"]][0].summary == []


# ------------------------------------------------------- 메일/알림 렌더 도달

def test_pipc_summary_reaches_email_rendering():
    cfg = _cfg()
    posts = {PIPC_NAMES[k]: [_pipc_post(k)] for k in PIPC_KEYS}
    _stub(Summarizer(cfg), []).summarize_all(posts)

    html = build_html(posts)
    text = build_text(posts)
    for rendered in (html, text):
        for source_name in PIPC_NAMES.values():
            assert source_name in rendered
        for line in LINES:
            assert line in rendered
        # 기존 소스와 동일한 라벨(의안 전용 접두어가 붙지 않는다)
        assert "AI 3줄 요약" in rendered
        assert "제안이유 및 주요내용 · AI 3줄 요약" not in rendered
    assert "https://pipc.go.kr/np/cop/bbs/selectBoardArticle.do?nttId=12503" in text


def test_pipc_without_summary_still_renders_body_excerpt():
    """요약이 실패해도 기존 폴백(원문 발췌)이 그대로 적용된다."""
    posts = {PIPC_NAMES["pipc_notice"]: [_pipc_post("pipc_notice")]}
    text = build_text(posts)
    assert "입법예고" in text
    assert "제안이유 및 주요내용 발췌" not in text   # 의안 전용 라벨이 아니다


# ------------------------------------------------- main 전체 흐름 (--no-llm 포함)

class _FakePipcScraper:
    """목록·상세를 대신하는 대역. enrich 계약(본문 채움)만 실제와 같게 흉내낸다."""

    SUPPORTS_ENRICH = True

    def __init__(self, key, name, posts):
        self.key, self.name = key, name
        self.posts = list(posts)

    def collect(self, limit, seen_ids, max_pages):
        fresh = [p for p in self.posts if p.post_id not in seen_ids]
        return CollectResult(posts=fresh, reached_boundary=True, scanned=max(len(fresh), 1))

    def enrich(self, post):
        post.body = BODY

    def enrich_succeeded(self, post):
        return bool((post.body or "").strip())


def _run_main(tmp_path, monkeypatch, *, no_llm: bool, only: str):
    from src import main as main_mod

    seen = {"summarize": [], "digests": []}

    state_path = tmp_path / "seen.json"
    st = State(state_path)
    for key in ("pipc_notice", "pipc_press", "fss_press"):
        st.mark_seen(key, ["seed-old"], baselined=True)   # 기준선은 이미 수립된 상태
    st.save()

    monkeypatch.setenv("SMTP_USER", "s@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    monkeypatch.setenv("MAIL_TO", "to@example.com")

    def _factory(src, fetcher):
        # 소스 key/name 은 config 의 것을 그대로 쓴다 — PIPC 와 기존 소스(fss_press)를
        # **같은 조건**에서 비교하기 위함이다.
        post = _pipc_post(src.key, pid=src.key, name=src.name)
        return _FakePipcScraper(src.key, src.name, [post])

    def _summarize(llm_cfg, posts_by_source):
        seen["summarize"].append({k: list(v) for k, v in posts_by_source.items()})
        for posts in posts_by_source.values():
            for p in posts:
                if p.body:
                    p.summary = list(LINES)
        return sum(len(v) for v in posts_by_source.values())

    def _send(cfg, posts_by_source, detail_updates_by_source=None):
        seen["digests"].append({k: list(v) for k, v in posts_by_source.items()})

    monkeypatch.setattr(main_mod, "build_scraper", _factory)
    monkeypatch.setattr(main_mod, "summarize_posts", _summarize)
    monkeypatch.setattr(main_mod, "send_digest", _send)
    monkeypatch.setattr(main_mod, "verify_smtp_login", lambda cfg: None)

    argv = ["--state", str(state_path), "--only", only]
    if no_llm:
        argv.append("--no-llm")
    seen["rc"] = main_mod.run(argv)
    return seen


def test_main_routes_pipc_posts_into_the_shared_summarizer(tmp_path, monkeypatch):
    seen = _run_main(tmp_path, monkeypatch, no_llm=False, only="pipc_notice,pipc_press")
    assert seen["rc"] == 0

    # 요약 입력에 두 소스가 모두 들어갔고, 요약 결과가 발송 대상에 그대로 실린다.
    assert len(seen["summarize"]) == 1
    summarized = [p for v in seen["summarize"][0].values() for p in v]
    assert {p.source_key for p in summarized} == set(PIPC_KEYS)
    assert all(p.body == BODY for p in summarized)

    assert len(seen["digests"]) == 1
    delivered = [p for v in seen["digests"][0].values() for p in v]
    assert {p.source_key for p in delivered} == set(PIPC_KEYS)
    assert all(p.summary == LINES for p in delivered)


def test_no_llm_skips_summarization_for_pipc_exactly_as_for_fss(tmp_path, monkeypatch):
    for only in ("pipc_notice,pipc_press", "fss_press"):
        seen = _run_main(tmp_path / only.replace(",", "_"), monkeypatch, no_llm=True, only=only)
        assert seen["rc"] == 0
        assert seen["summarize"] == []                    # LLM 호출 자체가 없다
        delivered = [p for v in seen["digests"][0].values() for p in v]
        assert delivered and all(p.summary == [] for p in delivered)
        assert all(p.body for p in delivered)             # 발송은 원문 발췌로 진행


def test_new_pipc_source_takes_the_generic_baseline_path(tmp_path, monkeypatch):
    """state 가 없는 최초 실행은 기준선만 기록하고 메일을 보내지 않는다."""
    from src import main as main_mod

    state_path = tmp_path / "seen.json"
    monkeypatch.setenv("SMTP_USER", "s@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    monkeypatch.setenv("MAIL_TO", "to@example.com")

    digests, summaries = [], []
    monkeypatch.setattr(
        main_mod, "build_scraper",
        lambda src, fetcher: _FakePipcScraper(
            src.key, src.name,
            [_pipc_post("pipc_notice", pid=f"{src.key}-{i}") for i in range(3)],
        ),
    )
    monkeypatch.setattr(main_mod, "send_digest", lambda *a, **k: digests.append(a))
    monkeypatch.setattr(main_mod, "summarize_posts", lambda *a, **k: summaries.append(a))
    monkeypatch.setattr(main_mod, "verify_smtp_login", lambda cfg: None)

    rc = main_mod.run(["--state", str(state_path), "--only", "pipc_notice,pipc_press"])
    assert rc == 0
    assert digests == []        # 기존 글이 '신규 알림'으로 나가지 않는다
    assert summaries == []      # 요약 할당량도 쓰지 않는다

    st = State(state_path)
    for key in PIPC_KEYS:
        assert st.is_baselined(key) is True
        assert len(st.seen_ids(key)) == 3

    # 두 번째 실행부터 진짜 신규만 발송된다.
    monkeypatch.setattr(
        main_mod, "build_scraper",
        lambda src, fetcher: _FakePipcScraper(
            src.key, src.name,
            [_pipc_post("pipc_notice", pid=f"{src.key}-new")]
            + [_pipc_post("pipc_notice", pid=f"{src.key}-{i}") for i in range(3)],
        ),
    )
    rc = main_mod.run(["--state", str(state_path), "--only", "pipc_notice,pipc_press"])
    assert rc == 0
    delivered = [p for a in digests for posts in a[1].values() for p in posts]
    assert sorted(p.post_id for p in delivered) == [
        "nttId:pipc_notice-new", "nttId:pipc_press-new",
    ]


def test_dry_run_does_not_persist_pipc_state(tmp_path, monkeypatch):
    from src import main as main_mod

    state_path = tmp_path / "seen.json"
    monkeypatch.setattr(
        main_mod, "build_scraper",
        lambda src, fetcher: _FakePipcScraper(
            src.key, src.name, [_pipc_post("pipc_notice", pid=src.key)]
        ),
    )
    sent = []
    monkeypatch.setattr(main_mod, "send_digest", lambda *a, **k: sent.append(a))

    rc = main_mod.run(
        ["--state", str(state_path), "--only", "pipc_notice,pipc_press", "--dry-run", "--no-llm"]
    )
    assert rc == 0
    assert sent == []
    assert not state_path.exists()          # dry-run 은 state 를 남기지 않는다


def test_pipc_scraper_declares_the_body_required_contract():
    """main / verify_sources 가 읽는 성공 판정 훅이 실제로 노출된다."""
    scraper = PipcBoardScraper(
        SourceConfig(
            key="pipc_notice", name="n", type="pipc_board", list_url=_LIST_URL["pipc_notice"]
        ),
        fetcher=None,
    )
    hook = getattr(scraper, "enrich_succeeded", None)
    assert callable(hook)
    with_body = _pipc_post("pipc_notice")
    only_attachment = _pipc_post("pipc_notice", body="")
    only_attachment.attachments.append(
        Attachment(
            filename="a.pdf",
            url="https://pipc.go.kr/cmm/fms/FileDown.do?atchFileId=A&fileSn=0",
        )
    )
    assert hook(with_body) is True
    assert hook(only_attachment) is False
