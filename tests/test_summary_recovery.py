"""2026-10-01 한도/과부하 장애와 API 없는 핵심 발췌 회귀 테스트."""
import json
import time

import pytest

from src.snippet import build_key_excerpt_lines
from src.notifier import build_html, build_text
from src.summarizer import LLMCallError, LLMErrorKind, Summarizer, _quota_info
from test_summarizer import _cfg, _post, _FakeResponse, _stub, _envelope


def quota_response(*, daily=False, scoped=True, limit="10", delay="60s"):
    violation = {"quotaMetric": "generate_content_free_tier_requests", "quotaValue": limit,
                 "quotaId": "GenerateRequestsPer" + ("Day" if daily else "Minute")
                 + "PerProject" + ("PerModel" if scoped else "") + "-FreeTier",
                 "quotaDimensions": {"model": "gemini-3.6-flash"} if scoped else {}}
    payload = {"error": {"status": "RESOURCE_EXHAUSTED", "message": "quota exceeded",
                         "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                      "violations": [violation]},
                                     {"@type": "type.googleapis.com/google.rpc.RetryInfo",
                                      "retryDelay": delay}]}}
    return _FakeResponse(429, payload, json.dumps(payload))


def success():
    return _FakeResponse(200, _envelope('{"summary":["변경함","대상임","시행함"]}'))


def test_daily_model_quota_skips_retries_and_recovers_on_fallback():
    s = Summarizer(_cfg(model="gemini-3.6-flash", fallback_models=["gemini-3.8-flash"],
                        max_retries=2))
    session = _stub(s, [quota_response(daily=True), success(), success()])
    assert s.summarize(_post()) == ["변경함", "대상임", "시행함"]
    assert s._active_model == "gemini-3.8-flash"
    assert "gemini-3.6-flash" in s._quota_blocked
    s.summarize(_post())
    assert len(session.sent) == 3
    assert not s._unavailable


def test_successful_fallback_preserves_assembly_phase(monkeypatch):
    s = Summarizer(_cfg(model="gemini-3.6-flash", fallback_models=["gemini-3.8-flash"]))
    _stub(s, [quota_response(daily=True), success()])
    calls = []
    monkeypatch.setattr("src.assembly_summary.summarize_assembly_bills",
                        lambda *args: calls.append(args) or 0)
    assert s.summarize_all({"source": [_post()]}) == 1
    assert len(calls) == 1
    assert s._terminal_failure is None


def test_retry_info_is_respected(monkeypatch):
    waits = []
    monkeypatch.setattr("src.summarizer.time.sleep", waits.append)
    s = Summarizer(_cfg(max_retries=1))
    _stub(s, [quota_response(delay="57.5s"), success()])
    s.summarize(_post())
    assert waits == [57.5]


def test_retry_after_header_takes_longer_of_server_hints():
    response = quota_response(delay="12s")
    response.headers = {"Retry-After": "65"}
    assert _quota_info(response).retry_after_sec == 65


def test_retry_hint_outside_deadline_uses_next_model_without_shortening_wait():
    s = Summarizer(_cfg(fallback_models=["gemini-3.8-flash"], max_retries=2))
    session = _stub(s, [quota_response(delay="600s"), success()])
    assert s.summarize(_post(), time.monotonic() + 30)
    assert len(session.sent) == 2


@pytest.mark.parametrize("scoped", [False, True])
def test_zero_or_daily_quota_is_not_retried(scoped):
    s = Summarizer(_cfg(max_retries=2))
    session = _stub(s, [quota_response(scoped=scoped, limit="0")])
    with pytest.raises(LLMCallError) as caught:
        s.summarize(_post())
    assert caught.value.kind is LLMErrorKind.RATE_LIMIT
    assert len(session.sent) == 1


def test_project_quota_does_not_switch_models():
    s = Summarizer(_cfg(fallback_models=["gemini-3.8-flash"], max_retries=0))
    session = _stub(s, [quota_response(scoped=False)])
    with pytest.raises(LLMCallError):
        s.summarize(_post())
    assert len(session.sent) == 1


def test_all_limited_models_keep_rate_limit_semantics_and_stop_once():
    s = Summarizer(_cfg(fallback_models=["gemini-3.8-flash"], max_retries=2))
    session = _stub(s, [quota_response(daily=True), quota_response(daily=True)])
    assert s.summarize_all({"source": [_post(), _post()]}) == 0
    assert len(session.sent) == 2
    assert s._terminal_failure is LLMErrorKind.RATE_LIMIT


def test_quota_log_preserves_metric_id_limit_and_delay(caplog):
    s = Summarizer(_cfg(max_retries=0))
    _stub(s, [quota_response(daily=True)])
    with pytest.raises(LLMCallError):
        s.summarize(_post())
    for text in ("GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                 "generate_content_free_tier_requests", '"limit": "10"', '"retry_after_sec": 60.0'):
        assert text in caplog.text
    assert "test-key" not in caplog.text


BODY = ("관계 기관은 간담회를 개최하였다. 참석자들은 정책 방향을 논의하였다. "
        "위원장은 참석자들에게 감사 인사를 전하였다. 기존 제도의 배경을 설명하였다. "
        "금융회사의 소비자 보호 보고 의무를 신설한다. "
        "보고 대상은 은행과 보험회사이며 보고 금액 기준은 1,234억원이다. "
        "개정 규정은 2026. 10. 15.부터 시행되며 의견은 10월 10일까지 제출한다.")


def test_excerpt_selects_changes_targets_and_timing_beyond_intro():
    lines = build_key_excerpt_lines(BODY, "소비자 보호 보고 의무 신설")
    assert len(lines) == 3
    assert all(line in BODY for line in lines)
    assert "신설" in lines[0]
    assert "1,234억원" in lines[1]
    assert "2026. 10. 15." in lines[2]
    assert "감사" not in " ".join(lines)


def test_excerpt_preserves_signs_and_distinct_numbers():
    body = "수익률은 -3.5% 감소하였다. 순손실은 △2조원이다. 수익률은 -35% 감소하였다."
    assert build_key_excerpt_lines(body) == [
        "수익률은 -3.5% 감소하였다.", "순손실은 △2조원이다.", "수익률은 -35% 감소하였다."]


def test_excerpt_omits_noise_and_does_not_pad_short_body():
    assert build_key_excerpt_lines("담당부서: 금융정책과\n대출 한도를 상향한다.\n목록") == ["대출 한도를 상향한다."]
    assert build_key_excerpt_lines("자세한 내용은 첨부파일을 참고하세요.") == []
    assert build_key_excerpt_lines("") == []


def test_excerpt_bullet_boundaries_and_negative_sign():
    body = "□ 은행 보고 의무를 신설함\n□ 적용 대상은 은행과 보험회사임\n□ 2027년부터 시행함"
    assert len(build_key_excerpt_lines(body)) == 3
    assert build_key_excerpt_lines("-3.5% 감소함") == ["-3.5% 감소함"]


def test_excerpt_shared_html_text_design_and_labels():
    post = _post(body=BODY, title="소비자 보호 보고 의무 신설")
    grouped = {post.source_name: [post]}
    for rendered in (build_html(grouped), build_text(grouped)):
        assert "원문 발췌 · 핵심 3줄" in rendered
        assert "원문 문장을 자동으로 선별했습니다." in rendered
        assert "AI 3줄 요약" not in rendered
        for line in build_key_excerpt_lines(BODY, post.title):
            assert line in rendered
    assert "border:1px solid #e2e8f0;border-radius:6px" in build_html(grouped)


def test_excerpt_html_is_escaped():
    post = _post(body='은행은 <img src="x" onerror="x"> 표기를 금지한다. 순손실은 -3.5%이다.')
    rendered = build_html({post.source_name: [post]})
    assert "<img" not in rendered
    assert "&lt;img" in rendered


def test_fragmented_site_body_ignores_attachment_list_and_preserves_reference():
    body = ("금융회사는\n보고 의무를\n신설한다\n.\n보고 기준은\n1,234억원\n이다 .\n"
            "개정 규정은\n[\n붙임\n]\n과 같이 2027년부터 시행한다.\n"
            "첨부파일 (2)\n첨부파일 목록\nreport.pdf\n파일다운로드")
    lines = build_key_excerpt_lines(body)
    assert len(lines) == 3
    assert "신설한다 ." in lines[0]
    assert "1,234억원" in lines[1]
    assert "[ 붙임 ]" in lines[2]
    assert "파일다운로드" not in " ".join(lines)


def test_inline_stars_do_not_detach_the_action_from_its_subject():
    body = "금융회사의\n보고 의무\n*\n를 폐지한다\n.\n*\n가상 규정 부칙"
    lines = build_key_excerpt_lines(body)
    assert lines[0] == "금융회사의 보고 의무 * 를 폐지한다 ."


def test_numeric_table_does_not_displace_policy_sentences():
    body = ("정책 추진 배경을 설명한다.\n□ 금융회사 보고 의무를 신설한다.\n"
            "□ 은행은 2027년부터 개정 규정을 적용한다.\n□ 보고 기준은 1,234억원이다.\n"
            "□ 금융회사 대상 과징금 현황 " + " ".join(str(x) for x in range(30)))
    lines = build_key_excerpt_lines(body)
    assert len(lines) == 3
    assert all("현황" not in line for line in lines)
    assert "1,234억원" in " ".join(lines)


def test_title_fragmented_across_spans_is_removed_only_on_exact_match():
    title = "금융회사 보고 의무 신설"
    body = "금융회사\n보고 의무\n신설\n은행의 월별 보고 의무를 신설한다."
    assert build_key_excerpt_lines(body, title) == ["은행의 월별 보고 의무를 신설한다."]
    assert build_key_excerpt_lines("수익률\n-3.5%\n감소하였다.", "수익률 3.5% 감소하였다.") == [
        "수익률 -3.5% 감소하였다."]
