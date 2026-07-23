"""기본 스모크 테스트: 임포트/모델/상태/이메일 렌더가 깨지지 않는지 확인."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import load_config
from src.models import Attachment, Post
from src.notifier import build_html, build_text
from src.state import State


def test_config_loads():
    cfg = load_config("config.yaml")
    assert cfg.sources, "소스 목록이 비어 있으면 안 됨"
    keys = {s.key for s in cfg.sources}
    # 9개 대상이 모두 정의돼 있는지
    assert len(keys) == 9
    assert "fss_press" in keys
    assert "assembly_bill" in keys


def test_state_baseline_and_new(tmp_path):
    st = State(tmp_path / "s.json")
    assert not st.is_baselined("x")
    st.mark_seen("x", ["1", "2"], baselined=True)
    assert st.is_baselined("x")
    assert not st.is_new("x", "1")
    assert st.is_new("x", "3")
    st.save()
    # 재로딩 유지
    st2 = State(tmp_path / "s.json")
    assert not st2.is_new("x", "2")


def test_email_render():
    p = Post(
        source_key="fss_press",
        source_name="금감원 · 보도자료",
        post_id="123",
        title="테스트 보도자료 제목",
        url="https://example.com/view?nttId=123",
        date="2026-07-23",
        body="본문 내용 예시",
        attachments=[Attachment(filename="붙임.pdf", url="https://example.com/f.pdf")],
    )
    grouped = {p.source_name: [p]}
    html = build_html(grouped)
    text = build_text(grouped)
    assert "테스트 보도자료 제목" in html
    assert "붙임.pdf" in html
    assert "example.com" in text
