"""금융규제포털의 원문 질의/회답/이유를 보존하는 출력 전용 발췌."""
from __future__ import annotations

import re

from .snippet import _cut_at_word, _key_sentences, is_meaningful, normalize_lines

_LABELS = ("질의요지", "회답", "이유")
# 수집기(better_fsc)가 body 를 만드는 정상 구조 두 가지. 질의요지·회답은 필수, 이유는 선택.
_COMPLETE_ORDERS = (("질의요지", "회답"), _LABELS)
_SECTION = re.compile(r"^\[(질의요지|회답|이유)\]\s*(.*)$")


def _bounded_section(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    # 짧은 머리말 다음 문장이 길어도 결론이 있는 꼬리의 몫을 남긴다.
    # 앞 절반에는 가능한 한 온전한 문장을, 나머지에는 원문 끝을 그대로 싣는다.
    marker = " … "
    room = limit - len(marker)
    kept = ""
    for sentence in _key_sentences(text):
        candidate = f"{kept} {sentence}".strip()
        if len(candidate) > room // 2:
            break
        kept = candidate
    head = kept or _cut_at_word(text, room // 2)
    return head + marker + _cut_at_word(text, room - len(head), from_end=True)


def build_reply_excerpt_rows(body: str, *, require_complete: bool = False) -> list[tuple[str, str]]:
    """있는 섹션만 원문 순서로 표시한다. 회답은 점수/경쟁으로 탈락하지 않는다.

    scraper는 질의요지·회답이 모두 있어야 body를 채우고, 이유는 있을 때만 붙인다.
    비정상/과거 데이터에 일부 섹션만 있으면 그 항목만 내보내고, 마커가 없으면 []를
    반환해 notifier의 안전 발췌로 넘긴다. 회답은 600자, 질의/이유는 각 300자 이내다.
    require_complete=True는 소스 key와 무관한 자동 판별용이다. 수집기의 정확한
    시작 표식, 단독 라벨 순서가 [질의요지]→[회답] 또는 [질의요지]→[회답]→[이유] 중
    하나와 정확히 같을 것(누락·중복·순서 오류 거부), 표식이 붙은 모든 섹션에 실질
    내용이 있을 것(빈 [이유] 표식도 거부)을 요구한다.
    """
    sections: dict[str, list[str]] = {}
    current = ""
    lines = normalize_lines(body)
    markers = [m for line in lines if (m := _SECTION.match(line))]
    if require_complete and (
        not lines or lines[0] != "[질의요지]"
        or tuple(m[1] for m in markers) not in _COMPLETE_ORDERS
        or any(m[2] for m in markers)
    ):
        return []
    for line in lines:
        match = _SECTION.match(line)
        if match:
            current = match[1]
            sections.setdefault(current, [])
            line = match[2]
        if current and line:
            sections[current].append(line)
    rows = []
    for label in _LABELS:
        text = " ".join(sections.get(label, []))
        if is_meaningful(text):
            rows.append((label, _bounded_section(text, 600 if label == "회답" else 300)))
    # 표식이 붙은 섹션 중 하나라도 실질 내용이 없으면 완전한 구조가 아니다.
    return [] if require_complete and len(rows) != len(markers) else rows
