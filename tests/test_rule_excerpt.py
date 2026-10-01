"""AI 요약 실패 후 표시하는 규칙 기반 발췌 회귀 테스트."""
import pytest
import json
from pathlib import Path

from src.snippet import build_key_excerpt_lines, build_rule_excerpt_rows
from src.notifier import build_html, build_text
from test_summarizer import _post


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
    assert "신설한다." in lines[0]
    assert "1,234억원" in lines[1]
    assert "[붙임]" in lines[2]
    assert "파일다운로드" not in " ".join(lines)


def test_inline_stars_do_not_detach_the_action_from_its_subject():
    body = "금융회사의\n보고 의무\n*\n를 폐지한다\n.\n*\n가상 규정 부칙"
    lines = build_key_excerpt_lines(body)
    assert lines[0] == "금융회사의 보고 의무 * 를 폐지한다."


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


def test_concatenated_attachment_labels_and_sentences_are_handled():
    body = ("은행의 보고 의무를 신설한다.보고 기준은 1,234억원이다.2026.10.15.부터 적용한다.\n"
            "첨부파일 (2)첨부파일 목록 report.hwpx 파일다운로드 report.pdf 파일뷰어")
    lines = build_key_excerpt_lines(body)
    assert len(lines) == 3
    assert "첨부" not in " ".join(lines)
    assert "2026.10.15." in lines[2]


def test_policy_roles_are_displayed_in_question_order_not_source_order():
    body = ("개정 규정은 2027년 1월 1일부터 시행한다. "
            "적용 대상은 은행과 보험회사이다. "
            "금융회사의 보고 의무를 신설한다.")
    rows = build_rule_excerpt_rows(body, "보고 의무 신설")
    assert [role for role, _ in rows] == ["변경 내용", "대상·조건", "시행·기한"]
    assert [text for _, text in rows] == [
        "금융회사의 보고 의무를 신설한다.", "적용 대상은 은행과 보험회사이다.",
        "개정 규정은 2027년 1월 1일부터 시행한다."]


def test_current_rule_does_not_displace_new_rule():
    body = ("현행법은 은행에 보고 의무를 적용하고 신용정보 제공을 금지하고 있다. "
            "보고 대상은 보험회사이다. 개정 규정은 2027년부터 시행한다. "
            "보험회사의 보고 의무를 신설한다.")
    rows = build_rule_excerpt_rows(body, "보고 의무 신설")
    assert rows[0] == ("변경 내용", "보험회사의 보고 의무를 신설한다.")
    assert all("현행법" not in text for _, text in rows)


def test_exception_is_kept_with_change_and_timing():
    body = ("모든 금융회사에 보고 의무를 신설한다. "
            "보고 대상은 은행과 보험회사이다. "
            "다만 자산 100억원 미만인 회사는 적용 대상에서 제외한다. "
            "개정 규정은 2027년부터 시행한다.")
    rows = build_rule_excerpt_rows(body, "보고 의무 신설")
    assert rows[1] == ("대상·조건", "다만 자산 100억원 미만인 회사는 적용 대상에서 제외한다.")


def test_meeting_date_does_not_become_effective_date():
    body = ("9월 30일 회의를 개최하였다. 금융회사의 보고 의무를 신설한다. "
            "의견은 10월 10일까지 제출한다.")
    rows = build_rule_excerpt_rows(body, "보고 의무 신설")
    assert ("시행·기한", "의견은 10월 10일까지 제출한다.") in rows
    assert all(role != "시행·기한" or "개최" not in text for role, text in rows)


def test_statistics_get_fact_roles_and_sales_schedule():
    body = ("펀드의 목표 모집액은 6,000억원이다. "
            "첫날 판매액은 2,140억원으로 목표의 35.7%이다. "
            "온라인 판매는 10월 8일부터 재개할 예정이다.")
    rows = build_rule_excerpt_rows(body, "펀드 판매 결과")
    assert [role for role, _ in rows] == ["주요 현황", "세부 수치", "후속 일정"]
    assert all(text in body for _, text in rows)


def test_missing_schedule_and_subject_are_not_invented():
    rows = build_rule_excerpt_rows("보고 의무를 신설한다.", "보고 의무 신설")
    assert rows == [("변경 내용", "보고 의무를 신설한다.")]
    assert build_rule_excerpt_rows("서식이 공개되었습니다.") == [("주요 내용", "서식이 공개되었습니다.")]


def test_role_labels_are_shared_and_ai_card_is_unchanged():
    post = _post(body=BODY, title="보고 의무 신설")
    for rendered in (build_html({post.source_name: [post]}), build_text({post.source_name: [post]})):
        for label in ("변경 내용:", "대상·조건:", "시행·기한:"):
            assert label in rendered
    post.summary = ["AI 첫 번째", "AI 두 번째", "AI 세 번째"]
    for rendered in (build_html({post.source_name: [post]}), build_text({post.source_name: [post]})):
        assert "AI 3줄 요약" in rendered
        assert "원문 발췌" not in rendered
        assert "변경 내용:" not in rendered
    assert post.body == BODY  # 출력 처리는 Gemini에 전달할 원문을 변경하지 않는다.


def test_percent_is_not_a_date_and_future_sales_get_schedule_role():
    body = ("첫날 목표액의 35.7%인 약 2,140억원이 판매되었습니다. "
            "전체 모집 목표는 6,000억원입니다. "
            "온라인 판매는 10월 8일부터 재개될 예정입니다.")
    rows = build_rule_excerpt_rows(body, "판매 결과")
    assert rows[-1] == ("후속 일정", "온라인 판매는 10월 8일부터 재개될 예정입니다.")
    assert not any(role == "후속 일정" and "35.7%" in text for role, text in rows)


def test_fragmented_change_predicate_beats_generic_future_discussion():
    body = ("주요 현안의 공급 확대를 위한 논의를 지속해갈 계획이다. "
            "IPO 대상 법인에 대한 공표 의무를 3년간 매년 2회 이상으로 강화 한다.")
    rows = build_rule_excerpt_rows(body, "리서치보고서 개선 방안")
    assert "강화 한다" in rows[0][1]
    assert rows[0][0] == "변경 내용"
    assert all(role != "시행·기한" for role, _ in rows)


def test_todays_discussion_does_not_invent_an_effective_date():
    body = ("보고 의무를 신설한다. "
            "오늘 함께 논의된 리서치 제도 종합 개선방안이 현장에서 차질없이 시행되고 안착될 수 있도록 소통할 예정이다.")
    rows = build_rule_excerpt_rows(body, "보고 의무 신설")
    assert all(role != "시행·기한" for role, _ in rows)


def test_assembly_empty_selection_keeps_bounded_fallback():
    from src.models import ASSEMBLY_SOURCE_KEY
    post = _post(body="가. 나.")
    post.source_key = ASSEMBLY_SOURCE_KEY
    for rendered in (build_html({post.source_name: [post]}), build_text({post.source_name: [post]})):
        assert "가. 나." in rendered
        assert "제안이유 및 주요내용 발췌" in rendered


def test_empty_filtered_general_body_has_no_false_selection_notice():
    post = _post(body="금융회사 통계 " + " ".join(str(i) for i in range(30)))
    for rendered in (build_html({post.source_name: [post]}), build_text({post.source_name: [post]})):
        assert post.url in rendered
        assert "원문 발췌" not in rendered
        assert "자동으로 선별" not in rendered


@pytest.mark.parametrize("ending", ["재개됐다.", "재개되었습니다.", "재개되었다.", "시작했다.", "종료하였습니다."])
def test_completed_sales_are_not_followup_schedules(ending):
    body = "온라인 판매는 10월 8일부터 " + ending
    assert all(role != "후속 일정" for role, _ in build_rule_excerpt_rows(body, "판매 결과"))


@pytest.mark.parametrize("history", [
    "현행법은 은행의 판매를 금지한다.",
    "기존에는 은행의 판매를 금지한다.",
    "현행법은 새로 가입한 은행의 판매를 금지한다.",
])
def test_present_tense_current_law_is_not_a_change(history):
    rows = build_rule_excerpt_rows(history + " 보험회사에 월별 보고 의무를 신설한다.", "보고 의무 신설")
    assert rows[0] == ("변경 내용", "보험회사에 월별 보고 의무를 신설한다.")
    assert all(role != "변경 내용" or text != history for role, text in rows)


def test_current_law_with_explicit_new_clause_keeps_the_actual_change():
    text = "현행법은 은행의 판매를 금지한다. 그러나 개정안에서는 보고 의무를 신설한다."
    assert build_rule_excerpt_rows(text, "보고 의무 신설")[0] == (
        "변경 내용", "그러나 개정안에서는 보고 의무를 신설한다.")


def test_genuine_new_clause_in_background_section_is_still_selected():
    body = "추진 배경\n현행법은 은행의 판매를 금지한다. 그러나 개정안에서는 보고 의무를 신설한다."
    assert build_rule_excerpt_rows(body, "보고 의무 신설")[0] == (
        "변경 내용", "그러나 개정안에서는 보고 의무를 신설한다.")


@pytest.mark.parametrize("close", ["”", "’", "\"", "'", ")", "]", "」", "』"])
def test_quoted_or_bracketed_sentence_endings_are_separate(close):
    body = f"은행의 보고 의무를 신설한다.{close} 적용 대상은 보험회사이다. 2027년부터 시행한다."
    lines = build_key_excerpt_lines(body, "보고 의무 신설")
    assert len(lines) == 3
    assert lines[0] == f"은행의 보고 의무를 신설한다.{close}"
    assert "보험회사" in lines[1]
    assert "2027년" in lines[2]


@pytest.mark.parametrize("marker", ["Ⅰ.", "Ⅱ.", "Ⅴ.", "Ⅵ.", "①", "➌"])
def test_enumeration_preserves_the_policy_content(marker):
    assert build_key_excerpt_lines(marker + " 은행의 보고 의무를 신설한다.") == [
        "은행의 보고 의무를 신설한다."]


def test_headings_supply_context_and_separate_unpunctuated_items():
    body = ("Ⅰ. 추진 배경\n현행법은 은행의 판매를 금지한다.\n"
            "Ⅱ. 변경 내용\n은행의 보고 범위 확대\n"
            "적용 대상:\n은행과 보험회사\n"
            "시행 일정\n2027년 1월 1일부터\n")
    assert build_rule_excerpt_rows(body, "보고 제도 안내") == [
        ("변경 내용", "은행의 보고 범위 확대"),
        ("대상·조건", "은행과 보험회사"),
        ("시행·기한", "2027년 1월 1일부터")]


def test_headings_do_not_delete_substantive_lines_containing_heading_words():
    body = "Ⅰ. 적용 대상은 은행이며 보고 의무를 신설한다.\nⅡ. 시행 일정은 2027년부터이다."
    lines = build_key_excerpt_lines(body)
    assert any("보고 의무를 신설한다" in line for line in lines)
    assert any("2027년" in line for line in lines)


def test_roman_new_section_does_not_inherit_background_role():
    body = "추진 배경\n현행법은 은행의 판매를 금지한다.\nⅡ. 보험회사의 보고 의무를 신설한다."
    assert build_rule_excerpt_rows(body, "보고 의무 신설")[0] == (
        "변경 내용", "보험회사의 보고 의무를 신설한다.")


def test_fragment_joining_improves_readability_without_changing_input():
    body = ("은행\n은\n보고 의무를\n신설\n한다\n.\n"
            "보고 기준\n은\n1,\n234억원\n이며\n수익률은\n-3.5%\n이다\n.\n"
            "개정 규정은\n2027년\n부터\n시행\n된다\n.")
    lines = build_key_excerpt_lines(body, "보고 의무 신설")
    assert lines == ["은행은 보고 의무를 신설한다.",
                     "보고 기준은 1,234억원이며 수익률은 -3.5%이다.",
                     "개정 규정은 2027년부터 시행된다."]
    assert "\n" in body  # 정돈은 출력에만 적용한다.


def test_number_cells_and_negation_are_not_joined_or_removed():
    body = "자산은\n100\n200\n억원이며\n-3.5%\n이하인 회사에는 적용하지 않는다."
    line = build_key_excerpt_lines(body)[0]
    assert "100 200" in line
    assert "-3.5%" in line
    assert "적용하지 않는다" in line


def test_plan_heading_does_not_turn_todays_discussion_into_schedule():
    body = ("변경 내용\n은행의 보고 의무를 신설한다.\n향후 계획\n"
            "오늘 함께 논의된 리서치 제도 종합 개선방안이 현장에서 차질없이 시행되고 안착될 수 있도록 소통할 예정이다.")
    assert all(role != "시행·기한" for role, _ in build_rule_excerpt_rows(body))


def test_background_under_a_long_heading_does_not_become_target_conditions():
    body = ("Ⅱ.\n리서치의 독립성 강화\n그간 매도의견은 1% 미만으로 낮은 신뢰도가 지적되어 왔다.\n"
            "➌\nIPO 대상 법인의 리서치 공표 의무를 3년간 매년 2회 이상으로 강화한다.")
    rows = build_rule_excerpt_rows(body, "리서치 의무 강화")
    assert not any(role == "대상·조건" and "낮은 신뢰도" in text for role, text in rows)
    assert any("3년간" in text for _, text in rows)


def test_title_anchored_compound_restoration_does_not_join_arbitrary_words():
    body = "국민참여\n성장펀드\n의\n잔\n여물량은\n6,000억원이다."
    line = build_key_excerpt_lines(body, "국민참여성장펀드 잔여물량")[0]
    assert "국민참여성장펀드의" in line
    assert "잔여물량은" in line
    assert "6,000억원" in line
    assert "시장 상황" in build_key_excerpt_lines("시장\n상황을 점검한다.")[0]


def _fsc_corpus():
    return json.loads((Path(__file__).parent / 'fixtures/excerpts/fsc_posts.json').read_text(encoding='utf-8'))


def test_real_fsc_sales_retains_facts_and_improves_fragmented_words():
    post = next(p for p in _fsc_corpus() if p['post_id'] == '87825')
    rows = build_rule_excerpt_rows(post['body'], post['title'])
    assert [role for role, _ in rows] == ['주요 현황', '세부 수치', '후속 일정']
    text = ' '.join(s for _, s in rows)
    for fact in ('6,000억원', '2,140억원', '35.7%', '60%', '10.7일', '10.8일'):
        assert fact in text
    assert '국민참여성장펀드' in text
    assert '출시되었습니다' in text
    assert '잔여물량 현황은' in text
    assert '파일다운로드' not in text
    assert all(len(s) <= 300 for _, s in rows)


def test_real_fsc_research_retains_obligation_and_statistics_without_false_date():
    post = next(p for p in _fsc_corpus() if p['post_id'] == '87831')
    rows = build_rule_excerpt_rows(post['body'], post['title'])
    text = ' '.join(s for _, s in rows)
    for fact in ('3년간 매년 2회', '1년간 2회', '11.6조원', '2.0조원', '20.4%'):
        assert fact in text
    assert any(role == '대상·조건' and 'IPO' in s for role, s in rows)
    assert all(role != '시행·기한' for role, _ in rows)
    assert '협회규정 ➌' not in text
    assert '그간 증권사' not in text
