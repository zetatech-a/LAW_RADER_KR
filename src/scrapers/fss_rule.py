"""금감원 '세칙 제·개정 예고' 상세에서 원문 항목을 그대로 뽑는다(AI 요약·재작성 없음).

메일에 싣는 항목은 넷이다.
  2. 규정의 제·개정 취지 / 3. 규정의 제·개정 내용 /
  5. 의견제출시한(도착기준) / 6. 접수부서, 담당자, 전화번호, 팩스번호
1. 규정의 명칭과 4. 기타 필요사항은 싣지 않지만 **경계로는 쓴다** — 이웃 항목의 시작을
알아야 2번이 1번으로, 3번이 4번으로 번지지 않는다.

실제 #4405 상세(tests/fixtures/fss_rule/live/)는 div.krds-reg > dl.form-group >
dt.form-tit(번호+제목) / dd.form-conts(값) 구조다. 그래도 그 class 이름에 기대지 않는다 —
2026-08 개편 때처럼 class 가 바뀌면 조용히 빈 메일이 나가기 때문이다. 대신
  (1) 페이지를 블록 단위 줄로 펼쳐 항목 제목 줄을 찾고,
  (2) 그 제목 요소들의 가장 가까운 공통 조상을 컨테이너로 삼아(메뉴·꼬리말 배제),
  (3) 제목이 th/dt 라벨이면 짝지어진 td/dd 를 그대로 값으로 쓰고(구조적 짝),
      아니면 컨테이너 안에서 '다음 항목 제목' 직전까지를 값으로 쓴다.

제목은 '번호 + 정해진 문구'로만 인정한다. 3번 안의 '1. …', '2. …' 하위 목록은 문구가
달라 경계가 되지 않으므로 잘리지 않는다. 페이지에 번호 붙은 제목이 하나도 없을 때만
번호 없는 제목(줄 전체가 제목이거나 제목 뒤에 콜론)을 받는다.

같은 1~6번 양식을 쓰는 행정지도 예고(src/scrapers/fss_admin_guidance.py)도 이 엔진을
그대로 쓴다 — 게시판마다 다른 것은 항목 제목표(extract_sections 의 sections)와 컨테이너
확장 단계(widen)뿐이다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, NavigableString, Tag
from bs4.element import CData, Comment, Declaration, Doctype, ProcessingInstruction

from ..models import FSS_RULE_SECTION_TITLES

_PURPOSE, _CONTENT, _DEADLINE, _CONTACT = FSS_RULE_SECTION_TITLES

# 제·개정 사이 가운뎃점 표기 변형(·∙・ㆍ•‧, 마침표·하이픈). 없어도 된다(제개정).
_DOT = r"[·∙・ㆍ•‧.\-]?"
_REVISE = rf"제\s*{_DOT}\s*개정"
# 6번 제목의 나열 구분자. 공백만으로 이어 붙이지 않는다 — '6. 접수부서' 다음 줄의
# '담당자 : 홍길동' 을 제목으로 먹어 원문 라벨을 잃지 않게 하기 위함이다.
_LIST_SEP = r"(?:\s*[,，、·ㆍ/]\s*(?:및\s*)?|\s*및\s*)"

# 세칙·행정지도 예고가 같은 문구로 쓰는 4·5·6번 제목.
OTHER_MATTERS_HEADING = r"기타\s*필요\s*(?:한\s*)?사항"
DEADLINE_HEADING = r"의견\s*제출\s*[시기]한(?:\s*[(（]\s*도착\s*기준\s*[)）])?"
CONTACT_HEADING = (
    rf"접수\s*부서(?:{_LIST_SEP}(?:담당자|전화\s*번호|팩스\s*번호|연락처"
    rf"|이메일(?:\s*주소)?|전자\s*우편))*"
)

# 항목 제목표: (메일 표시 제목 — None 이면 싣지 않고 경계로만 쓴다, 원문 제목 정규식).
# 원문 순서대로이며 위치(0~5)+1 이 원문의 항목 번호다.
Sections = tuple[tuple[str | None, re.Pattern], ...]


def compile_sections(table: tuple[tuple[str | None, str], ...]) -> Sections:
    return tuple((title, re.compile(pattern)) for title, pattern in table)


_SECTIONS = compile_sections((
    (None, r"규정\s*의\s*명칭"),
    (_PURPOSE, rf"규정\s*의\s*{_REVISE}\s*취지"),
    (_CONTENT, rf"규정\s*의\s*{_REVISE}\s*내용"),
    (None, OTHER_MATTERS_HEADING),
    (_DEADLINE, DEADLINE_HEADING),
    (_CONTACT, CONTACT_HEADING),
))

# 항목 번호: '2.' '2)' '(2)' '②', 또는 숫자 하나 뒤 공백('<span>5</span> 의견…').
_NUMBER = re.compile(
    r"\s*(?:(?P<d>[1-9])\s*[.．)]|\(\s*(?P<p>[1-9])\s*\)|(?P<c>[①-⑨])|(?P<s>[1-9])(?=\s))\s*"
)
_CIRCLED = "①②③④⑤⑥⑦⑧⑨"
# 제목 뒤 꼬리: 나열 쉼표, 그리고 값 앞 구분자(콜론·대시).
_TRAIL = re.compile(r"[\s,，]*(?:([:：\-–—])\s*)?")

# 블록 경계로 줄을 나누는 요소. 나머지(span·strong·a 등)는 같은 줄에 이어 붙인다.
_BLOCK_TAGS = frozenset(
    "address article aside blockquote caption dd div dl dt fieldset figcaption figure "
    "footer form h1 h2 h3 h4 h5 h6 header hr li main nav ol p pre section table tbody "
    "td tfoot th thead tr ul".split()
)
# 화면 글자가 아니거나 조작 요소인 것 — 원문으로 싣지 않는다.
_SKIP_TAGS = frozenset(
    "script style noscript template head title button select option textarea iframe svg".split()
)
_NON_TEXT = (Comment, CData, Declaration, Doctype, ProcessingInstruction)
_NEWLINE = re.compile(r"\r\n|\r|\n")

# 마지막 항목(6번) 뒤에 붙는 페이지 요소. 줄 전체가 이 문구일 때만 끝으로 본다.
_PAGE_CHROME = frozenset(
    {"첨부파일", "첨부 파일", "첨부", "목록", "목록으로", "이전글", "다음글", "이전 글", "다음 글"}
)


@dataclass
class RuleExtraction:
    """추출 결과. sections 는 찾은 대상 항목만 표시 순서대로 담는다."""

    sections: list[tuple[str, str]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    # 전부 실패했을 때의 원인(로그용). 성공하면 빈 문자열.
    reason: str = ""


def has_all_sections(details: list[tuple[str, str]], titles: tuple[str, ...]) -> bool:
    """titles 의 대상 항목이 모두 원문 값과 함께 있는가(상세 수집 성공 판정)."""
    found = {label for label, value in details if value}
    return all(title in found for title in titles)


def is_complete(details: list[tuple[str, str]]) -> bool:
    """세칙 예고의 4개 대상 항목이 모두 원문 값과 함께 있는가."""
    return has_all_sections(details, FSS_RULE_SECTION_TITLES)


# ── 블록 단위 줄 펼치기 ───────────────────────────────────────────────────────


class _Lines:
    """요소를 화면에 보이는 줄로 펼친다. 각 줄이 시작된 블록 요소도 함께 기록한다."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.blocks: list[Tag | None] = []
        self._parts: list[str] = []
        self._block: Tag | None = None
        self._has_text = False

    def _append(self, piece: str, block: Tag) -> None:
        if not piece:
            return
        if not self._has_text and piece.strip():
            self._block = block
            self._has_text = True
        self._parts.append(piece)

    def text(self, raw: str, block: Tag) -> None:
        # 공백뿐인 텍스트(태그 사이 들여쓰기)는 HTML 렌더링과 같이 띄어쓰기 하나다.
        if not raw.strip():
            self._append(" ", block)
            return
        # 가장자리 공백·개행도 띄어쓰기로 본다. 글자 사이의 개행만 원문 줄바꿈으로
        # 지킨다(DB 원문을 pre-line 으로 내보내는 경우 목록 번호가 한 줄로 합쳐지지 않게).
        if raw[0].isspace():
            self._append(" ", block)
        for k, piece in enumerate(_NEWLINE.split(raw.strip())):
            if k:
                self.hard()
            self._append(piece, block)
        if raw[-1].isspace():
            self._append(" ", block)

    def soft(self) -> None:
        """블록 경계 — 내용이 있을 때만 줄을 끝낸다(빈 줄을 만들지 않는다)."""
        if self._has_text:
            self.hard()
        else:
            self._parts = []

    def hard(self) -> None:
        """<br>·원문 개행 — 내용이 없어도 줄을 끝낸다(원문의 빈 줄 = 문단 구분)."""
        line = " ".join("".join(self._parts).split())
        self.lines.append(line)
        self.blocks.append(self._block if line else None)
        self._parts = []
        self._block = None
        self._has_text = False


def _walk(node: Tag, out: _Lines, block: Tag) -> None:
    ordinal = 0
    if node.name == "ol":
        start = str(node.get("start") or "1").strip()
        ordinal = int(start) if start.isdigit() and int(start) > 0 else 1
    for child in node.children:
        if isinstance(child, NavigableString):
            if not isinstance(child, _NON_TEXT):
                out.text(str(child), block)
            continue
        if not isinstance(child, Tag):
            continue
        name = (child.name or "").lower()
        if name in _SKIP_TAGS:
            continue
        if name == "br":
            out.hard()
        elif name in _BLOCK_TAGS:
            out.soft()
            if name == "li" and ordinal:
                # <ol> 번호는 브라우저가 그리므로 텍스트에 없다. 화면에 보이는 하위 목록
                # 번호를 지우지 않기 위해 그대로 붙인다.
                out.text(f"{ordinal}. ", child)
                ordinal += 1
            _walk(child, out, child)
            out.soft()
        else:
            _walk(child, out, block)


def _lines_of(el: Tag) -> _Lines:
    out = _Lines()
    _walk(el, out, el)
    out.soft()
    return out


def _tidy(lines: list[str]) -> str:
    """앞뒤 빈 줄을 걷고 연속된 빈 줄은 하나로. 줄 안의 글자는 바꾸지 않는다."""
    kept: list[str] = []
    for line in lines:
        if not line and (not kept or not kept[-1]):
            continue
        kept.append(line)
    while kept and not kept[-1]:
        kept.pop()
    return "\n".join(kept)


# ── 항목 제목 판정 ────────────────────────────────────────────────────────────


def _title_at(text: str, numbered: bool, sections: Sections) -> tuple[int, int] | None:
    """text 맨 앞이 항목 제목이면 (항목 index, 제목이 끝난 위치).

    번호가 있으면 그 번호가 원문 항목 번호와 같아야 한다. 3번 하위 목록에 '5. 의견제출
    기한 단축'·'1. 규정의 명칭 변경' 같은 줄이 있어도 최상위 제목으로 오인하지 않기 위함이다.
    번호 체계가 바뀌면 조용히 잘못 자르지 않고 '항목 누락'으로 드러난다.
    """
    m = _NUMBER.match(text)
    if numbered and not m:
        return None
    if m:
        digit = m.group("d") or m.group("p") or m.group("s")
        number = int(digit) if digit else _CIRCLED.index(m.group("c")) + 1
        candidates = [number - 1] if number <= len(sections) else []
        start = m.end()
    else:
        candidates = list(range(len(sections)))
        start = 0
    for idx in candidates:
        tm = sections[idx][1].match(text, start)
        if tm:
            return idx, tm.end()
    return None


def _heading(
    lines: list[str], i: int, numbered: bool, sections: Sections
) -> tuple[int, int, str] | None:
    """lines[i] 에서 시작하는 항목 제목 → (항목 index, 제목이 차지한 줄 수, 같은 줄의 값).

    제목이 줄바꿈으로 갈라졌으면(th 안 <br>, '2.' 과 제목이 다른 요소 등) 다음 두 줄까지
    이어 본다. 이어 붙인 줄은 **통째로 제목이어야** 한다 — 값의 첫 줄을 제목으로 먹지 않는다.
    """
    best: tuple[int, int, str] | None = None
    joined = ""
    for span in range(1, 4):
        if i + span > len(lines) or not lines[i + span - 1]:
            break
        prev_len = len(joined)
        joined = f"{joined} {lines[i + span - 1]}" if joined else lines[i + span - 1]
        hit = _title_at(joined, numbered, sections)
        if hit is None:
            if best is not None:
                break
            continue  # '2.' 만 있는 줄 등 — 다음 줄과 이어 다시 본다
        idx, end = hit
        if best is not None and end <= prev_len:
            break  # 제목이 앞 줄에서 이미 끝났다
        tail = joined[end:]
        tm = _TRAIL.match(tail)
        rest, sep = tail[tm.end():], tm.group(1)
        colon = sep in (":", "：")
        if best is not None and rest and not colon:
            break  # 제목 뒤에 이어 붙인 줄에 제목 아닌 글자가 섞였다
        if rest:
            # 제목 뒤 같은 줄의 값은 구분자(번호 없는 제목은 콜론) 뒤에 와야 한다. 다만
            # 날짜처럼 숫자로 시작하는 값은 공백만으로도 받는다. '5. 의견제출기한 단축
            # 사유 명확화' 같은 3번 하위 항목을 최상위 제목으로 오인하지 않기 위함이다.
            allowed = bool(sep or rest[0].isdigit()) if numbered else colon
            if not allowed:
                break
        best = (idx, span, rest)
        if rest:
            break  # 같은 줄에 값이 이어진다 — 더 붙이지 않는다
    return best


def _headings(
    lines: list[str], numbered: bool, sections: Sections
) -> list[tuple[int, int, int, str]]:
    """제목으로 보이는 모든 줄 — (줄 번호, 항목 index, 제목 줄 수, 같은 줄의 값)."""
    found = []
    i = 0
    while i < len(lines):
        hit = _heading(lines, i, numbered, sections)
        if hit:
            found.append((i, *hit))
            i += hit[1]
        else:
            i += 1
    return found


def _in_order(raw: list[tuple[int, int, int, str]]) -> list[tuple[int, int, int, str]]:
    """원문 항목 순서(1→6)대로 나타나는 제목만 경계로 인정한다.

    3번 안에 '1. 규정의 명칭' 같은 줄이 다시 나와도 순서가 거꾸로라 경계가 아니다.
    """
    kept: list[tuple[int, int, int, str]] = []
    for head in raw:
        if not kept or head[1] > kept[-1][1]:
            kept.append(head)
    return kept


# ── 컨테이너·값 결정 ──────────────────────────────────────────────────────────


def _common_ancestor(els: list[Tag]) -> Tag | None:
    paths = [list(reversed([el, *el.parents])) for el in els]
    common = None
    for nodes in zip(*paths):
        if all(n is nodes[0] for n in nodes):
            common = nodes[0]
        else:
            break
    return common


def _paired_value(label: Tag | None) -> Tag | None:
    """제목 셀(th/dt)과 구조적으로 짝지어진 값 셀(같은 행의 td / 바로 뒤 dd)."""
    if label is None:
        return None
    if label.name == "th":
        return label.find_next_sibling("td")
    if label.name == "dt":
        sib = label.find_next_sibling(True)
        return sib if sib is not None and sib.name == "dd" else None
    return None


def _squash(text: str) -> str:
    return "".join(text.split())


def _is_stop(line: str, stop_lines: tuple[str, ...]) -> bool:
    if line in _PAGE_CHROME:
        return True
    return any(name and line.startswith(name) for name in stop_lines)


def _split(
    container: Tag, numbered: bool, stop_lines: tuple[str, ...], sections: Sections
) -> dict[int, str]:
    """컨테이너 안에서 항목별 원문(항목 index → 값)."""
    out = _lines_of(container)
    lines, blocks = out.lines, out.blocks
    raw = _headings(lines, numbered, sections)
    heads = _in_order(raw)
    values: dict[int, str] = {}
    for n, (i, idx, span, rest) in enumerate(heads):
        # 제목 셀이 라벨 전체이면(th/dt 안에 제목만 있음) 짝지어진 값 셀을 그대로 쓴다.
        label = blocks[i]
        paired = None if rest else _paired_value(label)
        if paired is not None and _squash(" ".join(lines[i:i + span])) == _squash(
            label.get_text(" ")
        ):
            values[idx] = _tidy(_lines_of(paired).lines)
            continue
        last = n + 1 == len(heads)
        if not last:
            end = heads[n + 1][0]
        else:
            # 마지막 항목 뒤에 번호가 다시 시작되면(인쇄용 사본 등) 거기서 끝낸다.
            end = next((r[0] for r in raw if r[0] > i), len(lines))
        body = [rest] if rest else []
        for line in lines[i + span:end]:
            if last and _is_stop(line, stop_lines):
                break  # 마지막 항목 뒤 첨부 목록·목록 버튼 등은 싣지 않는다
            body.append(line)
        values[idx] = _tidy(body)
    return values


def extract_sections(
    soup: BeautifulSoup,
    sections: Sections,
    *,
    stop_lines: tuple[str, ...] = (),
    hint: str,
    widen: int = 2,
) -> RuleExtraction:
    """상세 페이지에서 sections 의 대상 항목 원문을 뽑는다. 못 찾은 항목은 missing·reason 으로.

    stop_lines: 마지막 항목 뒤에 이어질 수 있는 첨부파일명 등. 그 줄부터는 싣지 않는다.
    hint: 제목을 하나도 못 찾았을 때 원인 문구에 넣을 대표 제목(로그용).
    widen: 값이 하나도 없을 때 컨테이너를 바깥으로 넓혀 볼 최대 단계 수.
    """
    root = soup.body or soup
    page = _lines_of(root)
    numbered = bool(_headings(page.lines, True, sections))
    heads = _in_order(_headings(page.lines, numbered, sections))
    if not heads:
        return RuleExtraction(
            missing=[title for title, _ in sections if title],
            reason=f"항목 제목({hint} 등)을 찾지 못함 — 페이지 텍스트 {len(page.lines)}줄",
        )

    container = _common_ancestor([page.blocks[i] for i, *_ in heads]) or root
    values = _split(container, numbered, stop_lines, sections)
    # 제목 요소 하나뿐이면 그 요소 자체가 컨테이너가 되어 값이 빈다(예: <p>제목</p> 뒤
    # 형제 <p>값</p>). 값이 하나도 없을 때만 widen 단계(기본 2)까지 바깥으로 넓혀 본다.
    for _ in range(widen):
        if any(values.get(idx) for idx, (title, _) in enumerate(sections) if title):
            break
        if container is root or container.parent is None:
            break
        container = container.parent
        values = _split(container, numbered, stop_lines, sections)

    result = RuleExtraction()
    for idx, (title, _) in enumerate(sections):
        if title is None:
            continue
        if values.get(idx):
            result.sections.append((title, values[idx]))
        else:
            result.missing.append(title)
    if not result.sections:
        result.reason = "항목 제목은 찾았으나 대상 4개 항목의 내용이 모두 비어 있음"
    return result


def extract_rule_sections(
    soup: BeautifulSoup, *, stop_lines: tuple[str, ...] = ()
) -> RuleExtraction:
    """세칙 예고 상세에서 대상 4개 항목의 원문을 뽑는다(extract_sections 참고)."""
    return extract_sections(soup, _SECTIONS, stop_lines=stop_lines, hint="규정의 제·개정 취지")
