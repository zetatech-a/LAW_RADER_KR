"""Policy evidence priority and compact numbered-list structural regressions."""
import pytest

from src.snippet import build_key_excerpt_lines, build_rule_excerpt_rows


@pytest.mark.parametrize("title,change", [
    ("온라인 판매 허용방안", "앞으로 온라인 판매를 허용한다."),
    ("판매 제한조치", "앞으로 온라인 판매를 제한한다."),
])
def test_explicit_policy_change_wins_over_sales_title(title, change):
    body = ("지난해 온라인 판매액은 100억원이었다. 올해 온라인 판매액은 120억원이었다. "
            + change + " 적용 대상은 금융회사이다. 규정은 2027년부터 시행한다.")
    assert build_rule_excerpt_rows(body, title) == [
        ("변경 내용", change), ("대상·조건", "적용 대상은 금융회사이다."),
        ("시행·기한", "규정은 2027년부터 시행한다.")]


@pytest.mark.parametrize("history", [
    "", "지난해 판매 대상을 확대하였다. ", "판매 대상을 확대하였다. ",
])
def test_sales_results_and_completed_changes_keep_statistics_roles(history):
    body = (history + "온라인 판매액은 120억원으로 증가했다. "
            "판매 규모는 전월 대비 20억원 증가했다. 온라인 판매는 10월 8일부터 재개할 예정이다.")
    rows = build_rule_excerpt_rows(body, "8월 온라인 판매 실적")
    assert [label for label, _ in rows] == ["주요 현황", "세부 수치", "후속 일정"]
    assert rows[-1][1] == "온라인 판매는 10월 8일부터 재개할 예정이다."


@pytest.mark.parametrize("separator", [" ", "\n"])
@pytest.mark.parametrize("marker", ["{n}.", "{n})", "{n}. ", "{n}) "])
def test_numbered_items_keep_all_contents_without_number_only_rows(marker, separator):
    contents = ["안내 사항입니다.", "신청 방법입니다.", "문의처입니다."]
    body = separator.join(marker.format(n=i) + text for i, text in enumerate(contents, 1))
    assert build_key_excerpt_lines(body) == contents


@pytest.mark.parametrize("sentence", [
    "1.5% 증가했다.", "3.14를 기준으로 계산한다.",
    "2026.10.2.부터 시행한다.", "제3조제1항에 따라 적용한다.",
])
def test_compact_enumeration_does_not_strip_numeric_facts(sentence):
    assert set(build_key_excerpt_lines(sentence + " 다음 내용을 설명한다.")) == {
        sentence, "다음 내용을 설명한다."}
