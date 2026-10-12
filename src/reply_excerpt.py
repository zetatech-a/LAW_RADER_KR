"""금융규제포털의 원문 질의/회답/이유를 보존하는 출력 전용 발췌."""
from __future__ import annotations

import re

from .snippet import _cut_at_word, _key_sentences, is_meaningful, normalize_lines

_LABELS = ("질의요지", "회답", "이유")
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

    scraper의 세 섹션 모두 있어야 body를 채우는 계약은 그대로다. 비정상/과거
    데이터에 일부 섹션만 있으면 그 항목만 내보내고, 마커가 없으면 []를 반환해
    notifier의 안전 발췌로 넘긴다. 회답은 600자, 질의/이유는 각 300자 이내다.
    require_complete=True는 소스 key와 무관한 자동 판별용이다. 수집기의 정확한
    시작 표식·세 단독 라벨의 순서·비어 있지 않은 내용을 모두 요구한다.
    """
    sections: dict[str, list[str]] = {}
    current = ""
    lines = normalize_lines(body)
    if require_complete and (
        not lines or lines[0] != "[질의요지]"
        or [m[1] for line in lines if (m := _SECTION.match(line))]
        != list(_LABELS)
        or any(m[2] for line in lines if (m := _SECTION.match(line)))
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
    return [] if require_complete and len(rows) != len(_LABELS) else rows
