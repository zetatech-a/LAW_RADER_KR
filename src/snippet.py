"""LLM 요약이 없을 때 메일에 싣는 '원문 발췌'를 규칙 기반으로 다듬는다.

요약은 API 키 없음·호출 실패·타임아웃·한도 초과·서킷 브레이커 작동으로 언제든 비어
있을 수 있고, 그때 notifier 는 본문 앞부분을 잘라 싣는다. 그런데 상세 페이지 본문에는
메일에 이미 표시된 제목이 한 번 더 반복되고, 담당부서·등록일·조회수·첨부파일 안내 같은
머리말이 먼저 오는 경우가 많다. 그대로 220자를 자르면 정작 내용은 한 줄도 안 보인다.

여기서는 외부 호출이나 추가 의존성 없이, 결정적인 규칙만으로 그 군더더기를 걷어낸다.

원칙:
  - 걷어내는 것은 본문 **앞뒤**에 붙은 줄뿐이다. 실제 내용을 만나면 즉시 멈추므로
    본문 가운데 문장은 어떤 경우에도 사라지지 않는다.
  - 판단은 줄 단위로, 확실한 패턴에만 적용한다(라벨+구분자, 정확히 일치하는 메뉴어 등).
    "담당자는 …" 처럼 라벨로 시작하기만 하는 정상 문장은 지우지 않는다.
  - 결과가 비거나 지나치게 짧으면 기존 방식(원본 220자 절단)으로 되돌린다.
  - 문자 단위 절삭(lstrip 등)은 하지 않는다 — "-3.5%", "△2조원" 처럼 부호가 붙은
    수치가 훼손되면 손실이 이익으로 뒤집힌다(summarizer 의 목록표식 처리와 같은 이유).
"""
from __future__ import annotations

import html as html_lib
import re

from bs4 import BeautifulSoup

# 메일 카드에 싣는 발췌 길이(기존 동작과 동일한 기본값).
SNIPPET_LIMIT = 220


# ── 1) HTML 제거 ────────────────────────────────────────────────────────────
# 스크래퍼가 get_text() 로 뽑아 주므로 보통 태그는 없지만, 셀렉터가 바뀌거나 소스가
# 늘면 태그가 섞여 들어올 수 있다. 한국어 본문에서 흔한 <붙임>, <표 1> 같은 표기를
# 태그로 오인하지 않도록 실제 태그 이름이 보일 때만 파싱한다.
_HTML_TAG = re.compile(
    r"</?(?:p|br|hr|div|span|table|thead|tbody|tfoot|tr|td|th|caption|ul|ol|li"
    r"|dl|dt|dd|a|img|h[1-6]|strong|em|b|i|u|font|pre|code|blockquote|section"
    r"|article|header|footer|nav|body|html|script|style)\b[^>]*>",
    re.IGNORECASE,
)


# 줄바꿈을 만드는 태그(블록 요소와 <br>). 나머지(strong·em·span 등 인라인)는 텍스트를
# 그대로 이어 붙인다. get_text("\n") 으로 뭉뚱그리면 인라인 태그 경계마다 줄이 갈려
# "과징금은 1,<strong>234</strong>억원이다" 가 "과징금은 1, 234 억원이다" 로, "<strong>
# 금융위원회</strong>는" 이 "금융위원회 는" 으로 어긋난다 — 수치와 어절이 훼손된다.
_BLOCK_TAGS = (
    "p", "br", "hr", "div", "table", "thead", "tbody", "tfoot", "tr", "td", "th",
    "caption", "ul", "ol", "li", "dl", "dt", "dd", "h1", "h2", "h3", "h4", "h5",
    "h6", "pre", "blockquote", "section", "article", "header", "footer", "nav",
)


def strip_html(text: str) -> str:
    """본문에 HTML 이 남아 있으면 기존 프로젝트 방식(BeautifulSoup+lxml)으로 태그를 제거.

    블록 요소·<br> 자리에만 줄바꿈을 넣고 인라인 노드는 공백 없이 이어 붙인다.
    """
    if not text or not _HTML_TAG.search(text):
        return text or ""
    soup = BeautifulSoup(text, "lxml")
    for el in soup(["script", "style"]):
        el.decompose()
    for el in soup.find_all(_BLOCK_TAGS):
        # 앞뒤 **양쪽**에 경계를 넣는다. 뒤에만 넣으면 블록이 텍스트 뒤에 중첩된
        # "<div>머리말<ul><li>첫 항목</li></ul></div>" 이 "머리말첫 항목" 으로,
        # "<div><strong>주의</strong><p>본문…</p></div>" 이 "주의본문…" 으로 붙는다.
        el.insert_before("\n")
        el.insert_after("\n")
    return soup.get_text("")


# ── 2) 공백·엔티티 정규화 ───────────────────────────────────────────────────
# 폭만 다른 공백류(줄바꿈 없는 공백·전각 공백 등)는 보통 공백으로 바꾼다. 그대로 두면
# 제목 비교와 길이 계산이 어긋난다.
_SPACE_LIKE = re.compile("[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]")
# 폭이 없는 문자(제로폭 공백·BOM 등)는 공백으로 바꾸지 않고 **지운다**. 그대로 두면
# "1,\u200b234" 가 "1, 234" 로 갈라져 수치가 훼손된다(인라인 태그 줄바꿈과 같은 문제).
_ZERO_WIDTH = re.compile("[\u200b-\u200f\u2060\ufeff]")
_HORIZONTAL_WS = re.compile(r"[ \t\r\f\v]+")


def normalize_lines(body: str) -> list[str]:
    """태그·엔티티를 정리하고 줄 단위로 공백을 정규화한 뒤 빈 줄을 버린다."""
    text = body or ""
    if _HTML_TAG.search(text):
        # BeautifulSoup 이 태그와 엔티티를 함께 풀어 준다. 여기서 unescape 를 한 번 더
        # 부르면 "&amp;amp;" 처럼 이중 인코딩된 문자가 과하게 풀리므로 부르지 않는다.
        text = strip_html(text)
    else:
        text = html_lib.unescape(text)
    text = _ZERO_WIDTH.sub("", _SPACE_LIKE.sub(" ", text))

    lines = []
    for raw in text.splitlines():
        line = _HORIZONTAL_WS.sub(" ", raw).strip()
        if line:
            lines.append(line)
    return lines


# ── 3) 제목 중복 판정 ───────────────────────────────────────────────────────
# 제목 비교에서 무시하는 '형식' 문자 — 공백·괄호·따옴표·중점처럼 수치와 무관한 것뿐이다.
# 글자와 숫자는 남기므로 "은행법 개정" 과 "은행법 개정안" 은 여전히 다른 것으로 본다.
_TITLE_FORMAT = re.compile(
    r"[\s\[\]()（）〔〕【】<>《》「」『』｢｣\"'“”‘’`·・ㆍ;|_!?]+"
)
# 소수점·자릿점·부호·날짜·시각 구분자로도 쓰이는 문자. 숫자에 붙어 있지 않을 때만
# 무시한다. 무조건 지우면 "수익률 3.5% 증가"와 "수익률 35% 증가", "-3.5%"와 "3.5%",
# "2026-07-24"와 "20260724" 가 같은 제목으로 판정되어, 제목과 수치가 다른 본문
# 문장이 '제목 중복'으로 지워진다(발췌에서 사실이 통째로 사라진다).
#
# 뒤쪽 판정에 \s* 를 둔 이유: 원문이 "수익률 - 3.5% 기록" 처럼 부호와 숫자를 공백으로
# 띄어 쓰는 경우가 있다. 공백만 보고 형식 문자로 지우면 "수익률 3.5% 기록" 과 같은
# 키가 되어 부호가 반대인 줄이 '제목 중복'으로 사라진다. 반대로 뒤가 숫자가 아니면
# ("외부감사 규정 - 개정안") 단순 구분자이므로 그대로 무시한다.
_TITLE_SEPARATOR = re.compile(r"(?<![0-9])[.,:：/~\-–—+±]+(?!\s*[0-9])")
# 제목이 이 정도로 짧으면 우연히 겹칠 수 있어 중복 판정에 쓰지 않는다.
_MIN_TITLE_KEY_CHARS = 2


def _title_key(text: str) -> str:
    """제목 비교용 키. 형식 차이는 지우고 숫자에 붙은 부호·구분자는 남긴다."""
    # 구분자를 먼저 판정한다 — 공백을 지운 뒤에 보면 "수익률 - 3.5%" 의 하이픈이
    # 숫자에 붙은 부호처럼 보여 형식 차이를 무시하지 못한다.
    return _TITLE_FORMAT.sub("", _TITLE_SEPARATOR.sub("", text or ""))


def is_duplicate_title(line: str, title: str) -> bool:
    """본문 줄이 메일에 이미 표시된 제목과 실질적으로 같은가.

    앞뒤 공백·연속 공백·괄호/구분자 차이만 무시하고 나머지 글자는 그대로 비교한다.
    부분 일치가 아니라 **완전 일치**라서, "…입법예고" 제목과 "…입법예고에 따라 의견을
    받습니다" 같은 정상 문장은 키가 달라 지워지지 않는다.
    """
    key = _title_key(title)
    if len(key) < _MIN_TITLE_KEY_CHARS:
        return False
    return _title_key(line) == key


# ── 4) 상용구·메타데이터 판정 ───────────────────────────────────────────────
# 라벨만 있는 줄이거나 "라벨 : 값" 형태일 때만 지운다. 구분자 없이 라벨로 시작하기만
# 하는 문장("담당자는 …", "문의사항이 있으면 …")은 그대로 남는다.
_META_LABELS = (
    "담당부서", "주관부서", "작성부서", "담당자", "작성자", "담당", "부서",
    "연락처", "전화번호", "전화", "문의처", "문의",
    "등록일자", "등록일시", "등록일", "게시일자", "게시일", "작성일자", "작성일",
    "수정일자", "수정일", "배포일시", "배포일", "보도일시", "보도시점",
    "조회수", "조회",
    "제목", "첨부파일", "첨부",
    "이전글", "다음글",
)
_LABEL_ALT = "|".join(_META_LABELS)
_LABEL_LINE = re.compile(rf"^(?:{_LABEL_ALT})\s*(?:[:：]\s*.{{0,60}})?$")
_LABEL_ONLY = re.compile(rf"^(?:{_LABEL_ALT})\s*[:：]?$")

# 라벨만 있는 줄(<dt>담당부서</dt>) 바로 뒤에는 값 줄(<dd>기업회계팀</dd>)이 온다.
# 값은 짧고 문장으로 끝나지 않는다 — 그 조건을 만족할 때만 한 줄 더 걷어낸다.
_META_VALUE_MAX_CHARS = 30
_SENTENCE_TAIL = re.compile(r"(?:[.!?。]|니다|습니다|이다|한다|였다|했다|함|임|음)$")
# 다만 "2026. 7. 24." 처럼 마침표로 끝나는 날짜 표기가 흔하다. 문장 종결 검사에 걸려
# 값으로 인정되지 않으면 라벨만 지워지고 날짜부터 뒤 메타행 전부가 발췌 앞에 남는다.
_DATE_LIKE = re.compile(
    r"^\d{4}\s*[-./년]\s*\d{1,2}\s*[-./월]\s*\d{1,2}\s*[.일]?\s*"
    r"(?:\(?[월화수목금토일]\)?)?\s*(?:\d{1,2}:\d{2})?$"
)

# 정확히 일치할 때만 지우는 메뉴·네비게이션 문구(공백 제거·소문자화 후 비교).
_NAV_WORDS = frozenset({
    "목록", "목록으로", "목록보기", "리스트",
    "이전", "다음", "이전글", "다음글", "위로", "맨위로", "top",
    "인쇄", "인쇄하기", "프린트", "공유", "공유하기", "url복사", "링크복사",
    "다운로드", "내려받기", "미리보기", "바로보기",
    "본문바로가기", "본문으로바로가기", "주메뉴바로가기", "메뉴바로가기", "전체메뉴",
    "홈", "home", "검색", "닫기",
    "트위터", "페이스북", "카카오톡", "카카오스토리", "네이버블로그", "네이버밴드",
})
_NAV_TRIM = re.compile(r"\s+")

# "홈 > 알림마당 > 보도자료" 형태의 breadcrumb 만 본다. 일반적인 구분자 나열까지
# 훑으면 실제 본문의 표 한 줄을 지울 수 있어 시작점을 '홈'으로 못 박는다.
_BREADCRUMB = re.compile(r"^(?:홈|HOME|Home)\s*[>›》＞]\s*\S")

# 줄 전체가 장식 기호뿐인 경우(구분선, 빈 불릿 등).
_DECORATION = re.compile(r"^[\s\-=_~*·・ㆍ‧∙•▪◦□■○●◇◆※☞▶▷─━│┃═╌┄]+$")

# 첨부파일 목록 줄. 파일명은 실제 수집 결과처럼 공백을 포함할 수 있어("1. 공고문
# 입법예고.hwpx", tests/test_parsers.py 참고) 파일명만으로는 산문과 구분되지 않는다.
# 그래서 두 갈래로 나눈다:
#   ① 크기 표기가 붙은 줄("보도자료.hwp (188 KB)") — 첨부 목록 행이 거의 확실해 단독 판정.
#   ② 크기 없는 파일명 줄 — '첨부파일' 라벨 바로 뒤(첨부 목록 문맥)에서만 지운다.
# ②를 단독으로 지우면 "제출 파일명은 report.pdf" 같은 본문 문장이 발췌에서 사라진다.
_FILE_EXT = r"(?:hwp|hwpx|pdf|docx?|xlsx?|pptx?|zip|jpe?g|png|gif|bmp|txt|csv)"
_FILE_SIZE = r"\s*\(?\s*[\d.,]+\s*[KMGkmg]?B\s*\)?"
# 콜론이 있으면 "신청 서식: 금융지원신청서.hwp" 처럼 설명문이라 파일명 줄로 보지 않는다.
_ATTACHMENT_WITH_SIZE = re.compile(
    rf"^[^:：]{{1,120}}\.{_FILE_EXT}{_FILE_SIZE}$", re.IGNORECASE
)
_BARE_FILENAME = re.compile(
    rf"^[^:：]{{1,120}}\.{_FILE_EXT}(?:{_FILE_SIZE})?$", re.IGNORECASE
)
# 첨부 목록 문맥을 만드는 라벨(_META_LABELS 의 부분집합).
_ATTACHMENT_LABEL_ONLY = re.compile(r"^(?:첨부파일|첨부)\s*[:：]?$")

# 페이지 하단 만족도 조사. **줄 전체**가 그 안내문일 때만 지운다 — "만족도 조사" 는
# 실제 보도자료 본문("금융감독원은 금융소비자 만족도 조사를 실시했다")에도 나오는 말이라
# 부분 일치로 보면 첫 문단이 통째로 군더더기로 분류된다.
_SURVEY = re.compile(
    r"^(?:만족도\s*(?:조사|평가)\s*[:：]?"          # 섹션 제목만 있는 줄
    r"|.{0,40}만족하[십셨]\S{0,4}\s*[?？])$"        # "…정보에 만족하십니까?" 안내문
)

# 보도자료 배포 안내(엠바고·출처 표기 요청). "출처를 표기"·"엠바고" 는 실제 규제 내용에도
# 나오는 말이라("온라인 광고에는 자료의 출처를 표기해야 한다") 부분 일치로 보면 규제 사실이
# 발췌에서 사라진다. 그래서 ① 줄 전체가 짧은 안내문이고 ② 요청·안내 종결("…주시기
# 바랍니다", "…할 수 있습니다")로 끝날 때만 군더더기로 본다. 서술형("…해야 한다",
# "…부과한다")은 본문으로 남긴다.
_PRESS_KEYWORD = (
    r"(?:즉시\s*보도|인용하여\s*보도|출처를?\s*(?:표기|명시|밝혀)|엠바고)"
)
_PRESS_REQUEST_TAIL = r"(?:바랍니다|바람|있습니다|주십시오|주세요|부탁드립니다)"
_PRESS_NOTICE = re.compile(
    rf"^(?=.{{0,140}}$)(?=.*{_PRESS_KEYWORD}).*{_PRESS_REQUEST_TAIL}\s*[.]?$"
)

# 자바스크립트 사용 안내. 보도 안내와 같은 방식으로, ① 줄 전체가 짧은 안내문이고
# ② 브라우저·설정 문맥을 갖추고 ③ 안내·요청 종결로 끝날 때만 지운다. 키워드가 함께
# 나오는 것만으로 판정하면 "금융회사의 홈페이지는 자바스크립트를 활성화하도록
# 규정하였다" 같은 규제 사실이 발췌에서 사라진다(서술형은 본문으로 남긴다).
_JS_WORD = r"(?:자바\s*스크립트|javascript)"
_JS_BROWSER = r"(?:브라우저|browser|설정)"
_JS_NOTICE_TAIL = r"(?:습니다|주세요|주십시오|하십시오|바랍니다|바람)"
_JS_NOTICE = re.compile(
    rf"^(?=.{{0,140}}$)(?=.*{_JS_WORD})(?=.*{_JS_BROWSER})"
    rf".*{_JS_NOTICE_TAIL}\s*[.]?$",
    re.IGNORECASE,
)


def _nav_key(line: str) -> str:
    return _NAV_TRIM.sub("", line).lower()


def _looks_like_meta_value(line: str) -> bool:
    """라벨만 있는 줄 바로 뒤에 오는 값 줄(부서명·날짜·조회수 등)로 볼 수 있는가.

    날짜 모양을 먼저 확인한 뒤에 문장 종결 검사를 적용한다("2026. 7. 24." 도 값이다).
    """
    if not 0 < len(line) <= _META_VALUE_MAX_CHARS:
        return False
    return bool(_DATE_LIKE.match(line)) or not _SENTENCE_TAIL.search(line)


def is_boilerplate(line: str) -> bool:
    """수집 결과에서 반복적으로 확인된 상용구·메타데이터 줄인가(확실한 패턴만)."""
    if not line:
        return True
    return bool(
        _DECORATION.match(line)
        or _nav_key(line) in _NAV_WORDS
        or _BREADCRUMB.match(line)
        or _LABEL_LINE.match(line)
        or _ATTACHMENT_WITH_SIZE.match(line)
        or _SURVEY.match(line)
        or _PRESS_NOTICE.match(line)
        or _JS_NOTICE.match(line)
    )


def strip_edge_noise(lines: list[str], title: str = "") -> list[str]:
    """본문 앞뒤에 붙은 제목 중복·상용구·메타데이터 줄을 걷어낸다.

    앞에서부터, 그리고 뒤에서부터 '확실한 군더더기'만 벗겨 내고 실제 내용을 만나면
    즉시 멈춘다. 가운데를 훑지 않으므로 본문 문장이 사라질 여지가 없다.
    """
    def noise(line: str) -> bool:
        return is_duplicate_title(line, title) or is_boilerplate(line)

    def is_value_of_label(index: int) -> bool:
        """lines[index] 를 바로 앞 라벨 줄의 값으로 볼 수 있는가.

        그 줄이 또 다른 라벨·군더더기면 값으로 삼지 않는다. 값이 비어 라벨만 잇달아
        나오는 경우("첨부파일 / 등록일 / 2026-07-24")에 뒤 라벨을 값으로 먹어치우면,
        정작 지워야 할 날짜가 발췌 맨 앞에 남는다.
        """
        if not 0 <= index < len(lines):
            return False
        return not noise(lines[index]) and _looks_like_meta_value(lines[index])

    def after_attachment_run(index: int) -> int:
        """'첨부파일' 라벨 뒤에 이어지는 파일명 줄들을 지나친 위치."""
        while index < len(lines) and _BARE_FILENAME.match(lines[index]):
            index += 1
        return index

    start = 0
    while start < len(lines):
        line = lines[start]
        if not noise(line):
            break
        # '첨부파일' 라벨 뒤에는 크기 표기가 없는 파일명이 여러 줄 이어질 수 있다.
        if _ATTACHMENT_LABEL_ONLY.match(line):
            start = after_attachment_run(start + 1)
            continue
        # 라벨만 있는 줄(<dt>) 다음의 값 줄(<dd>)까지 한 줄 더 걷어낸다.
        if _LABEL_ONLY.match(line) and is_value_of_label(start + 1):
            start += 1
        start += 1

    end = len(lines)
    while end > start:
        line = lines[end - 1]
        if noise(line):
            end -= 1
            continue
        # 꼬리말이 "첨부파일 / 보도자료.hwp / 별첨.pdf" 처럼 끝나는 경우.
        run = end
        while run > start and _BARE_FILENAME.match(lines[run - 1]):
            run -= 1
        if run < end and run > start and _ATTACHMENT_LABEL_ONLY.match(lines[run - 1]):
            end = run - 1
            continue
        # 꼬리말이 "담당부서 / 기업회계팀" 처럼 라벨+값 두 줄로 끝나는 경우.
        if (
            end - 1 > start
            and _LABEL_ONLY.match(lines[end - 2])
            and is_value_of_label(end - 1)
        ):
            end -= 2
            continue
        break

    return lines[start:end]


# ── 5) 발췌 조립 ────────────────────────────────────────────────────────────
# 정제 결과가 이보다 짧으면(글자·숫자 기준) 규칙이 과하게 걷어낸 것으로 보고 되돌린다.
_MIN_MEANINGFUL_CHARS = 10
_NON_WORD = re.compile(r"[^0-9A-Za-z가-힣ㄱ-ㅎㅏ-ㅣ一-龥]")


def is_meaningful(text: str) -> bool:
    """정제 결과를 그대로 실을 만한가. 기호만 남았거나 너무 짧으면 False."""
    return len(_NON_WORD.sub("", text or "")) >= _MIN_MEANINGFUL_CHARS


def truncate_snippet(text: str, limit: int = SNIPPET_LIMIT) -> str:
    """기존과 동일한 절단 규칙 — limit 를 넘을 때만 말줄임표를 붙인다."""
    s = " ".join((text or "").split())
    return s[:limit] + " …" if len(s) > limit else s


def build_fallback_snippet(
    body: str,
    title: str = "",
    limit: int = SNIPPET_LIMIT,
) -> str:
    """LLM 요약이 없을 때 메일에 실을 원문 발췌를 만든다.

    HTML 제거 → 엔티티 정리 → 공백 정규화 → 제목 중복 제거 → 상용구/메타데이터 제거
    → 남은 내용을 limit 자까지 발췌. 정제 결과가 비거나 너무 짧으면 정규화만 마친
    원본을 같은 규칙으로 자른다(기존 동작). 본문이 없으면 빈 문자열이다.
    """
    lines = normalize_lines(body)
    if not lines:
        return ""

    text = " ".join(strip_edge_noise(lines, title))
    if not is_meaningful(text):
        text = " ".join(lines)
    return truncate_snippet(text, limit)
