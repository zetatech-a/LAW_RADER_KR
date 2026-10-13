"""원문 발췌 줄 맨 앞의 장식용 기호 불릿 정리.

메일은 줄마다 기관색 • 를 붙이므로, 원문 문장 앞의 □·○·■ 같은 기호가 남으면 불릿이
두 번 보인다. 기호 불릿만 벗기고 번호·날짜·음수·익명 표기(○○은행)·문장 중간 기호는
그대로 둔다. 이 파일의 본문은 모두 합성 예시이며 운영 원문이 아니다.
"""
import copy
import html
import re

import pytest

from src import snippet
from src.models import FSS_RULE_SECTION_TITLES, FSS_RULE_SOURCE_KEY, Post
from src.notifier import build_html, build_text
from src.snippet import build_full_excerpt_lines, build_rule_excerpt_rows

# 보고된 사례('□ '27.11부터 K-IFRS...')를 금융위 보도자료 수집 형태(get_text("\n"))로
# 재구성한 합성 본문. 기호가 별도 줄, 문장 중간에서 시작하는 '• □', 여러 기호 연속을 담는다.
KIFRS_TITLE = "K-IFRS 제1118호 '재무제표 표시와 공시' 도입 준비 현황 점검"
KIFRS_BODY = "\n".join([
    KIFRS_TITLE,
    "□",
    "'27.11부터 K-IFRS 제1118호 '재무제표 표시와 공시'에 따른 첫 분기보고서 공시가 시작됩니다.",
    "금융위원회와 금융감독원은 상장회사의 도입 준비 상황을 점검하였습니다. "
    "• □ 금융회사는 손익계산서의 영업·투자·재무 범주 분류 기준을 '26년 말까지 확정하여야 합니다.",
    "■ 경영진이 정의한 성과측정치(MPM)를 공시하는 회사는 주석에 산출 근거를 함께 기재하여야 합니다.",
    "• □ ○ 금융감독원은 '27.3월까지 업종별 모범사례를 배포할 예정입니다.",
    "○○은행 관련 조치는 별도로 안내할 예정입니다.",
    "① 제출서류는 감사인 검토를 거쳐야 합니다.",
    "1. 제도 개선 과제는 회계기준원과 협의하여 정합니다.",
    "-3.5% 감소한 영업이익률은 기존 기준으로 산정한 수치입니다.",
] + [f"○ 참고로 설명회 {i}회차는 업권별 일반 안내 위주로 진행된 바 있습니다." for i in range(1, 9)])

KIFRS_EXPECTED = [
    "'27.11부터 K-IFRS 제1118호 '재무제표 표시와 공시'에 따른 첫 분기보고서 공시가 시작됩니다.",
    "금융위원회와 금융감독원은 상장회사의 도입 준비 상황을 점검하였습니다.",
    "금융회사는 손익계산서의 영업·투자·재무 범주 분류 기준을 '26년 말까지 확정하여야 합니다.",
]

BULLETS = "□■○●▪▫•"
LEADING_BULLET = re.compile(rf"^[{BULLETS}◦◆◇☐ㅇ▶▷]")


def _post(body, title="보고제도 안내", key="fsc_press", **extra):
    return Post(source_key=key, source_name="금융위 · 보도자료", post_id="b1", title=title,
                url="https://example.com/b1", date="2026-10-13", body=body, **extra)


def _render(post):
    grouped = {post.source_name: [post]}
    return build_html(grouped), build_text(grouped)


def _html_rows(html_out):
    cells = re.findall(r"<td style='padding:2px 0;font-size:13px;line-height:1\.6;"
                       r"color:#334155'>(.*?)</td>", html_out)
    return [html.unescape(cell) for cell in cells]


def _text_rows(text_out):
    return [line[len("      · "):] for line in text_out.splitlines() if line.startswith("      · ")]


# ── 기호 불릿 정리 단위 계약 ───────────────────────────────────────────────────

@pytest.mark.parametrize(("raw", "expected"), [
    ("□ '27.11부터 K-IFRS 제1118호가 시행됩니다.", "'27.11부터 K-IFRS 제1118호가 시행됩니다."),
    ("○ 금융회사는 공시하여야 합니다.", "금융회사는 공시하여야 합니다."),
    ("• □ 금융회사는 공시하여야 합니다.", "금융회사는 공시하여야 합니다."),
    ("• □ ○ 금융회사는 공시하여야 합니다.", "금융회사는 공시하여야 합니다."),
    ("•□ 금융회사는 공시하여야 합니다.", "금융회사는 공시하여야 합니다."),
    ("□'27.11부터 시행됩니다.", "'27.11부터 시행됩니다."),
    ("○ ○○은행 관련 조치", "○○은행 관련 조치"),
    *((f"{mark} 금융회사는 공시하여야 합니다.", "금융회사는 공시하여야 합니다.") for mark in BULLETS),
])
def test_symbol_bullet_strips_only_the_leading_decoration(raw, expected):
    assert snippet._SYMBOL_BULLET.sub("", raw, count=1).strip() == expected


@pytest.mark.parametrize("kept", [
    "① 제출서류는 감사인 검토를 거쳐야 합니다.",
    "1. 제도 개선 과제를 추진합니다.",
    "-3.5% 감소한 수치는 기존 기준입니다.",
    "- 3.5% 감소한 수치는 기존 기준입니다.",
    "○○은행 관련 조치",
    "ㅇㅇ은행 관련 조치",
    "10. 15.부터 시행합니다.",
    "'27.11부터 시행합니다.",
    "※ 자세한 내용은 첨부파일을 참고하시기 바랍니다.",
    "「자본시장법」 제178조 위반으로 조치하였습니다.",
    "평가 결과 적합(○) 항목과 부적합(●) 항목을 구분합니다.",
])
def test_symbol_bullet_keeps_meaningful_leading_text(kept):
    assert snippet._SYMBOL_BULLET.sub("", kept, count=1) == kept


# ── 짧은 본문 전문 경로 ────────────────────────────────────────────────────────

@pytest.mark.parametrize(("line", "expected"), [
    ("□ '27.11부터 K-IFRS 제1118호가 시행됩니다.", "'27.11부터 K-IFRS 제1118호가 시행됩니다."),
    ("○ 금융회사는 공시하여야 합니다.", "금융회사는 공시하여야 합니다."),
    ("• □ 금융회사는 공시하여야 합니다.", "금융회사는 공시하여야 합니다."),
    ("■ 감독당국은 공시 내용을 점검합니다.", "감독당국은 공시 내용을 점검합니다."),
    ("● 감독당국은 공시 내용을 점검합니다.", "감독당국은 공시 내용을 점검합니다."),
    ("▫ 감독당국은 공시 내용을 점검합니다.", "감독당국은 공시 내용을 점검합니다."),
    ("① 제출서류는 감사인 검토를 거쳐야 합니다.", "① 제출서류는 감사인 검토를 거쳐야 합니다."),
    ("1. 제도 개선 과제를 추진합니다.", "1. 제도 개선 과제를 추진합니다."),
    ("-3.5% 감소한 수치는 기존 기준입니다.", "-3.5% 감소한 수치는 기존 기준입니다."),
    ("○○은행 관련 조치는 별도 안내합니다.", "○○은행 관련 조치는 별도 안내합니다."),
    ("평가 결과 적합(○) 항목과 부적합(●) 항목을 구분합니다.",
     "평가 결과 적합(○) 항목과 부적합(●) 항목을 구분합니다."),
])
def test_short_body_line_cleanup(line, expected):
    assert build_full_excerpt_lines(line, "제목") == [expected]


def test_short_body_bullet_runs_split_items_and_keep_placeholders():
    body = "• □ ○ 금융회사는 준비합니다.\n○○은행 관련 조치\n■ 금융회사는 공시합니다."
    assert build_full_excerpt_lines(body, "제목") == [
        "금융회사는 준비합니다.", "○○은행 관련 조치", "금융회사는 공시합니다.",
    ]


# ── 장문 선별 경로(보고 사례 재현) ──────────────────────────────────────────────

def test_kifrs_long_body_selected_rows_have_no_leading_decoration():
    html_out, text_out = _render(_post(KIFRS_BODY, KIFRS_TITLE))
    assert "[원문 발췌 · 3문장 선별]" in text_out
    assert _text_rows(text_out) == KIFRS_EXPECTED
    assert _html_rows(html_out) == KIFRS_EXPECTED


def test_selection_strips_bullet_runs_that_start_mid_paragraph():
    # 문단 중간에서 시작하는 문장은 문장 단계 한 번만 정리된다. '• □'가 한 번에 빠져야 한다.
    rows = [s for _, s in build_rule_excerpt_rows(KIFRS_BODY, KIFRS_TITLE, max_lines=20)]
    assert rows
    assert not [row for row in rows if LEADING_BULLET.match(row) and not row.startswith("○○")]
    assert "금융감독원은 '27.3월까지 업종별 모범사례를 배포할 예정입니다." in rows
    assert "○○은행 관련 조치는 별도로 안내할 예정입니다." in rows


def test_html_and_text_show_identical_cleaned_rows_for_short_body():
    body = "\n".join([
        "• □ 금융회사는 '27.11부터 새 기준에 따라 공시하여야 합니다.",
        "■ 감독당국은 공시 내용을 점검합니다.",
        "○○은행 관련 조치는 별도 안내합니다.",
        "① 제출서류는 감사인 검토를 거쳐야 합니다.",
        "1. 제도 개선 과제를 추진합니다.",
        "-3.5% 감소한 수치는 기존 기준입니다.",
    ])
    html_out, text_out = _render(_post(body))
    expected = [
        "금융회사는 '27.11부터 새 기준에 따라 공시하여야 합니다.",
        "감독당국은 공시 내용을 점검합니다.",
        "○○은행 관련 조치는 별도 안내합니다.",
        "① 제출서류는 감사인 검토를 거쳐야 합니다.",
        "1. 제도 개선 과제를 추진합니다.",
        "-3.5% 감소한 수치는 기존 기준입니다.",
    ]
    assert _text_rows(text_out) == expected
    assert _html_rows(html_out) == expected


def test_mail_keeps_its_own_accent_bullet():
    html_out, _ = _render(_post(KIFRS_BODY, KIFRS_TITLE))
    # 금융위 강조색(#1d4ed8) • 는 줄마다 그대로 붙는다.
    assert html_out.count("color:#1d4ed8'>•</td>") == len(KIFRS_EXPECTED)


# ── 원본 데이터와 다른 표시 경로는 바꾸지 않는다 ───────────────────────────────

def test_render_does_not_mutate_body_details_or_summary():
    posts = [
        _post(KIFRS_BODY, KIFRS_TITLE),
        _post("□ 원문", summary=["□ AI 요약 문장"]),
        _post("", details=[("구분", "□ 원문 항목")]),
    ]
    before = copy.deepcopy(posts)
    grouped = {"금융위 · 보도자료": posts}
    html_out, text_out = build_html(grouped), build_text(grouped)
    assert posts == before
    # AI 요약과 구조화 항목은 원문 그대로 싣는다.
    assert "      · □ AI 요약 문장" in text_out
    assert html.escape("□ AI 요약 문장") in html_out
    assert "      구분: □ 원문 항목" in text_out


def test_fss_rule_four_sections_keep_source_text():
    values = ["□ 보험회사의 공시 부담 완화", "○ 제5-1조 개정\n○ 별표 3 정비",
              "2026. 10. 27.", "보험감독국 02-3145-0000"]
    post = Post(source_key=FSS_RULE_SOURCE_KEY, source_name="금감원 · 세칙 제·개정 예고",
                post_id="r1", title="세칙 일부개정 예고", url="https://example.com/r1",
                details=list(zip(FSS_RULE_SECTION_TITLES, values)))
    html_out, text_out = _render(post)
    for value in values:
        for line in value.split("\n"):
            assert f"      {line}" in text_out
            assert html.escape(line) in html_out
