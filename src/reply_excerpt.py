"""금융규제포털의 원문 질의/회답/이유를 보존하는 출력 전용 발췌."""
from __future__ import annotations

import re

from .snippet import _cut_at_word, _key_sentences, is_meaningful, normalize_lines

_LABELS = ("질의요지", "회답", "이유")
_SECTION = re.compile(r"^\[(질의요지|회답|이유)\]\s*(.*)$")


def _bounded_section(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    # 온전한 문장을 우선한다. 하나의 문장이 너무 길면 조건/부정이 놓이기 쉬운
    # 꼬리도 남기고 중간 생략을 명시한다. 새 법률적 결론은 만들지 않는다.
    sentences = _key_sentences(text)
    if len(sentences[0]) <= limit - 2:
        kept = sentences[0]
        for sentence in sentences[1:]:
            if len(kept) + 1 + len(sentence) > limit - 2:
                break
            kept += " " + sentence
        return kept + " …"
    marker = " … "
    room = limit - len(marker)
    return (_cut_at_word(text, room // 2) + marker
            + _cut_at_word(text, room - room // 2, from_end=True))


def build_reply_excerpt_rows(body: str) -> list[tuple[str, str]]:
    """있는 섹션만 원문 순서로 표시한다. 회답은 점수/경쟁으로 탈락하지 않는다.

    scraper의 세 섹션 모두 있어야 body를 채우는 계약은 그대로다. 비정상/과거
    데이터에 일부 섹션만 있으면 그 항목만 내보내고, 마커가 없으면 []를 반환해
    notifier의 안전 발췌로 넘긴다. 회답은 600자, 질의/이유는 각 300자 이내다.
    """
    sections: dict[str, list[str]] = {}
    current = ""
    for line in normalize_lines(body):
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
    return rows
