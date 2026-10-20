"""금감원 '행정지도 예고' 상세에서 원문 항목을 그대로 뽑는다(AI 요약·재작성 없음).

메일에 싣는 항목은 넷이다.
  2. 행정지도의 신규·변경·연장의 취지 / 3. 행정지도의 내용 /
  5. 의견제출시한(도착기준) / 6. 접수부서, 담당자, 전화번호, 팩스번호
1. 행정지도의 명칭과 4. 기타 필요사항은 싣지 않지만 **경계로는 쓴다**.

실제 seqno=280 상세(tests/fixtures/fss_admin_guidance/live/)는 세칙 예고(dt/dd 짝)와 달리
div.krds-bd-view > div.n-dbdata 한 요소 안에 1~6번이 글자와 <br> 로만 이어진다. 추출은
세칙 예고 엔진(src/scrapers/fss_rule.py)을 그대로 쓴다 — 페이지를 블록·<br> 단위 줄로
펼쳐 '번호 + 정해진 문구' 제목 줄을 찾고, 제목들의 공통 조상(여기서는 n-dbdata 자체)
안에서 다음 제목 직전까지를 값으로 쓴다. 그래서 제목·첨부 상자·안내문·목록 버튼은
값에 섞이지 않고, 3번 안의 '1. …' 하위 목록이나 번호가 어긋난 제목 문구는 경계가 아니다.
이 모듈은 항목 제목표만 정의한다.
"""
from __future__ import annotations

from bs4 import BeautifulSoup

from ..models import FSS_ADMIN_GUIDANCE_SECTION_TITLES
from .fss_rule import (
    CONTACT_HEADING,
    DEADLINE_HEADING,
    OTHER_MATTERS_HEADING,
    RuleExtraction,
    compile_sections,
    extract_sections,
    has_all_sections,
)

_PURPOSE, _CONTENT, _DEADLINE, _CONTACT = FSS_ADMIN_GUIDANCE_SECTION_TITLES

_GUIDANCE = r"행정\s*지도\s*(?:의\s*)?"
# 2번 제목의 '신규·변경·연장' — 가운뎃점 표기 변형·쉼표·빗금, 일부만 적은 경우('연장의 취지')도 받는다.
_KIND = r"(?:신규|변경|연장)"
_KINDS = rf"{_KIND}(?:\s*[·∙・ㆍ•‧.,，、/]?\s*{_KIND})*"

_SECTIONS = compile_sections((
    (None, rf"{_GUIDANCE}명칭"),
    (_PURPOSE, rf"{_GUIDANCE}{_KINDS}\s*(?:의\s*)?취지"),
    (_CONTENT, rf"{_GUIDANCE}(?:주요\s*)?내용"),
    (None, OTHER_MATTERS_HEADING),
    (_DEADLINE, DEADLINE_HEADING),
    (_CONTACT, CONTACT_HEADING),
))


def is_complete(details: list[tuple[str, str]]) -> bool:
    """4개 대상 항목이 모두 원문 값과 함께 있는가(상세 수집 성공 판정)."""
    return has_all_sections(details, FSS_ADMIN_GUIDANCE_SECTION_TITLES)


def extract_admin_guidance_sections(
    soup: BeautifulSoup, *, stop_lines: tuple[str, ...] = ()
) -> RuleExtraction:
    """행정지도 예고 상세에서 대상 4개 항목의 원문을 뽑는다(fss_rule.extract_sections 참고).

    컨테이너는 넓히지 않는다(widen=0). 제목과 값이 모두 n-dbdata 한 요소 안에 있어 넓힐 일이
    없고, 값이 모두 빈 페이지에서 넓히면 div.contents 까지 올라가 6번 값에 '목록 행정지도에
    대한 의견제출'·'정보관리 담당부서 안내' 같은 페이지 문구가 실린다(실제 seqno=280 구조로
    확인). 그런 페이지는 전체 추출 실패로 드러낸다.
    """
    return extract_sections(
        soup, _SECTIONS, stop_lines=stop_lines, hint="행정지도의 신규·변경·연장의 취지", widen=0
    )
