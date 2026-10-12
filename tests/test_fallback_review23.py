"""Note formatting must not suppress existing substantive role rules."""
import pytest

from src.snippet import _role_strength, build_rule_excerpt_rows


@pytest.mark.parametrize("target", [
    "※ 적용 대상은 은행이다.", "※ 다만 소규모 회사는 제외한다.",
])
def test_note_target_survives_three_row_selection_with_original_text(target):
    change = "보고 의무를 신설한다."
    schedule = "2027년부터 시행한다."
    body = f"{change}\n2025년 판매액은 100억원이었다.\n{target}\n{schedule}"
    assert build_rule_excerpt_rows(body) == [
        ("변경 내용", change), ("대상·조건", target), ("시행·기한", schedule)]


@pytest.mark.parametrize("target", [
    "※ 적용 대상은 은행이다.", "※ 다만 소규모 회사는 제외한다.",
])
def test_note_marker_uses_existing_condition_strength(target):
    assert _role_strength("대상·조건", target) == _role_strength("대상·조건", target[1:].strip()) > 0


@pytest.mark.parametrize("noise", ["※", "※ 첨부파일 참조"])
def test_note_noise_stays_out_of_candidates(noise):
    assert build_rule_excerpt_rows(noise) == []
    body = "보고 의무를 신설한다.\n적용 대상은 은행이다.\n2027년부터 시행한다."
    assert build_rule_excerpt_rows(body + "\n" + noise) == build_rule_excerpt_rows(body)
    for role in ("변경 내용", "대상·조건", "시행·기한"):
        assert _role_strength(role, noise) == 0


@pytest.mark.parametrize("sentence, role", [
    ("※ 다만, 현행법은 소규모 회사에 예외를 인정한다.", "대상·조건"),
    ("※ 2027년부터 시행하지 않는다.", "시행·기한"),
])
def test_note_view_preserves_existing_history_and_negation_guards(sentence, role):
    assert _role_strength(role, sentence) == 0
