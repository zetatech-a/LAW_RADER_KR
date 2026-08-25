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
  - 결과에 실질적인 문자나 숫자가 없으면 기존 방식(원본 220자 절단)으로 되돌린다.
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


# 파싱을 시작할지 가르는 근거. 스크래퍼의 get_text() 는 "&lt;br&gt;" 같은 예시를
# 이미 글자 그대로의 "<br>" 로 풀어 본문에 넣는다. 태그처럼 보인다고 무조건 파싱하면
# "공시서식에는 <br> 태그를 사용할 수 없다" 가 "공시서식에는 태그를 사용할 수 없다" 가
# 되어 문장의 주어가 사라진다. 속성 유무도 근거가 못 된다 — 예시에도 속성이 붙는다
# ("<img src=\"x\">"). 그래서 다음 중 하나가 있을 때만 마크업으로 본다.
#   ① 문서 골격(<!DOCTYPE>·<html>·<head>·<body>)
#   ② 본문 전체가 블록 요소 하나로 감싸여 있다("<p>…</p>")
# 근거가 없으면 파싱하지 않고 글자 그대로 둔다. 태그 표기가 메일에 보이는 것이,
# 문장 일부가 소리 없이 사라지는 것보다 낫다.
_DOC_SKELETON = re.compile(r"<(?:!DOCTYPE|html|head|body)\b", re.IGNORECASE)
_WRAPPED_IN_BLOCK = re.compile(
    r"^\s*<(p|div|section|article|table|tbody|tr|td|th|ul|ol|li|dl|blockquote)\b"
    r"[^>]*>.*</\1\s*>\s*$",
    re.IGNORECASE | re.DOTALL,
)
def looks_like_markup(text: str) -> bool:
    """본문에 남은 것이 '잔여 마크업' 이라고 볼 근거가 있는가."""
    if not text or not _HTML_TAG.search(text):
        return False
    if _DOC_SKELETON.search(text):
        return True
    return bool(_WRAPPED_IN_BLOCK.match(text))


def strip_html(text: str) -> str:
    """잔여 마크업이면 기존 프로젝트 방식(BeautifulSoup+lxml)으로 태그를 제거.

    블록 요소·<br> 자리에만 줄바꿈을 넣고 인라인 노드는 공백 없이 이어 붙인다.
    """
    if not looks_like_markup(text):
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

# 세미콜론으로 끝나는 '분명한' 엔티티만 푼다. html.unescape 는 "&reg" 처럼 세미콜론
# 없는 옛 표기까지 풀어 버려서, 스크래퍼가 이미 텍스트로 뽑아 둔 본문의
# "…/?a=1&reg=2" 같은 URL 이 "…/?a=1®=2" 로 깨진다(&copy·&sect·&times 도 같다).
_ENTITY = re.compile(r"&(?:#\d{1,7}|#[xX][0-9a-fA-F]{1,6}|[a-zA-Z][a-zA-Z0-9]{1,31});")


def _unescape_entities(text: str) -> str:
    """세미콜론으로 끝나는 HTML 엔티티만 해제한다."""
    return _ENTITY.sub(lambda m: html_lib.unescape(m.group(0)), text)


def normalize_lines(body: str) -> list[str]:
    """태그·엔티티를 정리하고 줄 단위로 공백을 정규화한 뒤 빈 줄을 버린다."""
    text = body or ""
    if looks_like_markup(text):
        # BeautifulSoup 이 태그와 엔티티를 함께 풀어 준다. 여기서 unescape 를 한 번 더
        # 부르면 "&amp;amp;" 처럼 이중 인코딩된 문자가 과하게 풀리므로 부르지 않는다.
        text = strip_html(text)
    else:
        text = _unescape_entities(text)
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
    r"[\s\[\]()（）〔〕【】《》「」『』｢｣\"'“”‘’`·・ㆍ;|_!?]+"
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
# 첨부 파일명 조각(값 모양 판정과 첨부 목록 판정이 함께 쓴다).
_FILE_EXT = r"(?:hwp|hwpx|pdf|docx?|xlsx?|pptx?|zip|jpe?g|png|gif|bmp|txt|csv)"
_FILE_SIZE = r"\s*\(?\s*[\d.,]+\s*[KMGkmg]?B\s*\)?"

# ── 라벨별 '값 모양' ───
# 메타데이터는 라벨만 있는 줄이거나 "라벨 : 값" 형태다. 값을 아무 텍스트로 받으면
#   - "문의 : 대출 상한은 어떻게 해야 합니까?" 같은 실제 문장이 지워지고,
#   - 값이 빈 라벨 뒤에 오는 소제목("담당부서 / 대출한도 1억원 상향")까지 값으로 먹는다.
# 그래서 라벨마다 기대되는 값 모양을 정해 두고, 그 모양일 때만 군더더기로 본다.
# 부서명은 조직 접미사로, 날짜·전화·조회수는 형식으로 판정한다.
#
# 조직 접미사가 '과·국·팀' 처럼 한 음절이면 소제목 끝 음절과 구분되지 않는다
# ("추진 성과" 의 '과'). 그래서 한 음절 접미사는 **붙어 있는 앞 글자가 2자 이상**일
# 때만 조직명으로 본다("은행과"·"기업회계팀" ○, "추진 성과"·"주요 경과" ×).
# '위원회·감독원·본부' 처럼 여러 음절인 말은 그 자체로 증거라 길이를 따지지 않는다.
_ORG_WORD = r"(?:위원회|감독원|연구원|담당관|본부|센터|사무국|지원단|추진단)"
_ORG_UNIT = r"(?:팀|과|국|실|부|처|청)"
_ORG = (
    rf"[가-힣A-Za-z0-9()·\-\s]{{0,20}}"
    rf"(?:{_ORG_WORD}|[가-힣A-Za-z0-9]{{2,}}{_ORG_UNIT})"
)
_RANK = (
    r"(?:사무관|주사보|주사|주임|팀장|과장|국장|실장|부장|본부장|서기관|조사관"
    r"|연구관|검사역|수석|선임|담당자)"
)
_PERSON = r"[가-힣]{2,4}"
_PHONE = r"\(?0?\d{1,4}\)?[-\s.]?\d{3,4}[-\s.]?\d{4}(?:\s*\(?(?:내선\s*)?\d{1,5}\)?)?"
# "2026-07-24", "2026. 7. 24.", "2026년 7월 24일", "2026-07-24 15:30" 모두 값이다.
# (마침표로 끝나는 날짜 표기가 흔해 문장 종결로 오인하면 안 된다.)
_DATE = (
    r"\d{4}\s*[-./년]\s*\d{1,2}\s*[-./월]\s*\d{1,2}\s*[.일]?\s*"
    r"(?:\(?[월화수목금토일]\)?)?\s*(?:\d{1,2}:\d{2})?"
)
_COUNT = r"[\d,]+\s*(?:회|건|명)?"

_ORG_STAFF = rf"{_ORG}(?:\s+{_PERSON}(?:\s*{_RANK})?)?"

_VALUE_ORG = re.compile(rf"^{_ORG}$")
_VALUE_CONTACT = re.compile(rf"^(?:{_PHONE}|{_ORG}(?:\s*\(?{_PHONE}\)?)?)$")
_VALUE_DATE = re.compile(rf"^{_DATE}$")
_VALUE_COUNT = re.compile(rf"^{_COUNT}$")
_VALUE_FILE = re.compile(
    rf"^[^:：]{{1,120}}\.{_FILE_EXT}(?:{_FILE_SIZE})?$", re.IGNORECASE
)
# 담당자 값은 두 벌이다. 사람 이름만으로는 소제목("추진배경"·"주요내용"·"검토의견")과
# 구분되지 않기 때문이다.
#   - 인라인("담당자 : 홍길동"): 구분자 자체가 메타데이터라는 증거라 이름만으로 충분.
#   - 라벨만 있는 줄의 '다음 줄': 그 줄이 본문 첫 줄일 수도 있으므로 직급·조직명·
#     전화번호 같은 사람 고유의 증거를 요구한다(맨 이름은 값으로 삼지 않는다).
_VALUE_STAFF = re.compile(rf"^(?:{_PERSON}(?:\s*{_RANK})?|{_ORG_STAFF})$")
_VALUE_STAFF_STRICT = re.compile(
    rf"^(?:{_PERSON}\s*{_RANK}|{_ORG_STAFF}|{_PERSON}\s*\(?{_PHONE}\)?)$"
)

# 그 자체로 메타데이터임이 형식으로 드러나는 값들. 부서명·사람 이름 같은 자유 텍스트는
# 여기 들지 못한다 — "기대효과" 와 "은행과", "홍길동" 과 "추진배경" 은 생김새가 같아
# 정규식으로 가를 수 없기 때문이다(strip_edge_noise 가 문맥 증거를 따로 요구한다).
_SELF_EVIDENT_VALUES = (
    _VALUE_DATE,
    _VALUE_COUNT,
    _VALUE_FILE,
    re.compile(rf"^{_PHONE}$"),
    re.compile(rf"^{_PERSON}\s*{_RANK}$"),
)

# (라벨들, "라벨 : 값" 한 줄에서 허용하는 값, 라벨만 있는 줄의 '다음 줄'로 허용하는 값)
_LABEL_VALUE_RULES: tuple[tuple[tuple[str, ...], re.Pattern, re.Pattern], ...] = (
    (("담당부서", "주관부서", "작성부서", "부서"), _VALUE_ORG, _VALUE_ORG),
    (("담당자", "작성자", "담당"), _VALUE_STAFF, _VALUE_STAFF_STRICT),
    (("연락처", "전화번호", "전화", "문의처", "문의"), _VALUE_CONTACT, _VALUE_CONTACT),
    (
        (
            "등록일자", "등록일시", "등록일", "게시일자", "게시일", "작성일자",
            "작성일", "수정일자", "수정일", "배포일시", "배포일", "보도일시", "보도시점",
        ),
        _VALUE_DATE,
        _VALUE_DATE,
    ),
    (("조회수", "조회"), _VALUE_COUNT, _VALUE_COUNT),
    (("첨부파일", "첨부"), _VALUE_FILE, _VALUE_FILE),
)
# 값이 임의의 글 제목이라 모양을 못 박을 수 없는 라벨. 라벨 자체가 머리말·내비게이션
# 으로만 쓰이는 말이라 "라벨 : 값" 한 줄은 값을 보지 않고 지우되, 값이 빈 라벨 뒤의
# **다음 줄은 건드리지 않는다**(본문 첫 줄일 수 있다). '제목' 뒤의 제목 반복은
# is_duplicate_title 이 따로 걸러낸다.
_FREE_VALUE_LABELS = ("제목", "이전글", "다음글")

_META_LABELS = tuple(
    label for labels, *_ in _LABEL_VALUE_RULES for label in labels
) + _FREE_VALUE_LABELS
# 긴 라벨을 먼저 시도하도록 정렬한다(가독성 목적 — 역추적으로도 결과는 같다).
_LABEL_ALT = "|".join(sorted(_META_LABELS, key=len, reverse=True))
_LABEL_ONLY = re.compile(rf"^({_LABEL_ALT})\s*[:：]?$")
_LABEL_VALUE = re.compile(rf"^({_LABEL_ALT})\s*[:：]\s*(.*)$")

# 앞줄에서 낱말이 잘려 이어지는 줄. 조사 뒤에 공백을 요구해야 값 줄("은행과")을
# 조사("은")로 오인하지 않는다.
_CONTINUATION = re.compile(
    r"^(?:부터|까지|에서|으로|로서|로써|보다|처럼|마다|이라|라는|이란|이며|이고"
    r"|와|과|은|는|이|가|을|를|의|에|도|만|로)\s"
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
# 그래서 '첨부파일' 라벨 바로 뒤(첨부 목록 문맥)에서만 지운다.
# 파일 크기가 있어도 단독으로 지우면 "제출 파일은 report.pdf (10 MB)" 같은 정상 문장이
# 발췌에서 사라진다.
# 콜론이 있으면 "신청 서식: 금융지원신청서.hwp" 처럼 설명문이라 파일명 줄로 보지 않는다.
_BARE_FILENAME = _VALUE_FILE
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


def _value_rule(label: str, standalone: bool = False) -> re.Pattern | None:
    """라벨에 기대되는 값 모양. 값을 검사하지 않는 라벨(_FREE_VALUE_LABELS)은 None.

    standalone 은 '라벨만 있는 줄의 다음 줄' 을 검사할 때 True — 구분자라는 증거가
    없으므로 더 강한 값 모양을 요구한다.
    """
    for labels, inline, alone in _LABEL_VALUE_RULES:
        if label in labels:
            return alone if standalone else inline
    return None


def is_labelled_value(line: str) -> bool:
    """'라벨 : 값' 한 줄 — 구분자가 함께 있어 그 자체로 메타데이터인 줄인가.

    값이 있으면 라벨에 맞는 모양(부서명·날짜·전화·조회수·파일명)인지까지 확인한다.
    아무 텍스트나 값으로 받으면 "문의 : 대출 상한은 어떻게 해야 합니까?" 처럼 라벨을
    머리말로 쓴 실제 문장이 지워진다.
    """
    m = _LABEL_VALUE.match(line)
    if not m:
        return False
    label, value = m.group(1), m.group(2).strip()
    if not value:
        return True  # "담당부서 :" — 구분자가 있으니 값이 비어도 머리말이다
    rule = _value_rule(label)
    return rule is None or bool(rule.match(value))


def is_label_line(line: str) -> bool:
    """메타데이터 줄로 볼 수 있는가 — 이웃이 머리말인지 판단할 때 쓴다.

    구분자 없이 라벨 낱말만 있는 줄도 포함한다. 다만 그런 줄을 **그 자체로** 지울지는
    문맥을 봐야 하므로(is_boilerplate 에는 넣지 않는다) strip_edge_noise 가 따로 정한다.
    """
    return bool(_LABEL_ONLY.match(line)) or is_labelled_value(line)


def is_boilerplate(line: str) -> bool:
    """줄 하나만 보고도 군더더기라고 단정할 수 있는가(확실한 패턴만).

    구분자 없이 라벨 낱말만 있는 줄("등록일")은 여기 들지 않는다 — 인라인 강조 때문에
    본문 첫 단어가 떨어져 나온 것일 수 있어(strip_edge_noise 가 문맥을 보고 정한다).
    """
    if not line:
        return True
    return bool(
        _DECORATION.match(line)
        or _nav_key(line) in _NAV_WORDS
        or _BREADCRUMB.match(line)
        or is_labelled_value(line)
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

    def is_bare_meta_label(index: int) -> bool:
        """lines[index] 가 '값 없이 선 라벨' 인가(구분자 없는 라벨 낱말).

        스크래퍼의 get_text("\\n") 은 인라인 강조 경계에서도 줄을 나눈다. 그래서
        "<strong>등록일</strong>부터 30일 이내 …" 가 "등록일" / "부터 30일 이내 …" 두
        줄로 들어와, 본문 첫 낱말이 라벨처럼 보인다. 이때 뒷줄은 조사로 시작하는데,
        한국어에서 조사로 시작하는 줄은 블록의 시작일 수 없다 — 낱말이 잘린 것이다.
        그 경우에는 라벨로 보지 않는다(지우면 문장의 주어가 사라진다).
        """
        if not _LABEL_ONLY.match(lines[index]):
            return False
        nxt = index + 1
        return nxt >= len(lines) or not _CONTINUATION.match(lines[nxt])

    def is_value_of_label(label_line: str, index: int, neighbour: int) -> bool:
        """lines[index] 를 label_line(라벨만 있는 줄)의 값으로 볼 수 있는가.

        세 가지를 모두 만족해야 한다.
          ① 또 다른 라벨·군더더기가 아니다 — 값이 비어 라벨만 잇달아 나오는 경우
             ("첨부파일 / 등록일 / 2026-07-24")에 뒤 라벨을 먹어치우면 정작 지워야
             할 날짜가 발췌 맨 앞에 남는다.
          ② 그 라벨에 기대되는 값 모양이다.
          ③ 값이 형식으로 메타데이터임이 드러나거나(날짜·전화·조회수·파일명·직급),
             lines[neighbour] 가 또 다른 라벨이다.

        ③이 필요한 이유: 부서명·사람 이름은 본문 소제목과 생김새가 같아 정규식으로
        가를 수 없다("은행과"와 "기대효과"·"주요성과", "홍길동"과 "추진배경"). 그래서
        모양 대신 **구조**를 본다 — 라벨/값 쌍이 다른 라벨과 잇닿아 있으면 머리말
        블록이고, 본문 산문과 잇닿아 있으면 소제목이다. neighbour 는 그 '바깥쪽'
        이웃 줄(앞에서 훑을 땐 값 다음 줄, 뒤에서 훑을 땐 라벨 앞 줄)이다.
        """
        m = _LABEL_ONLY.match(label_line)
        if not m or not 0 <= index < len(lines):
            return False
        rule = _value_rule(m.group(1), standalone=True)
        value = lines[index]
        if rule is None or is_label_line(value) or noise(value) or not rule.match(value):
            return False
        if any(p.match(value) for p in _SELF_EVIDENT_VALUES):
            return True
        return 0 <= neighbour < len(lines) and is_label_line(lines[neighbour])

    def after_attachment_run(index: int) -> int:
        """'첨부파일' 라벨 뒤에 이어지는 파일명 줄들을 지나친 위치."""
        while index < len(lines) and _BARE_FILENAME.match(lines[index]):
            index += 1
        return index

    start = 0
    while start < len(lines):
        line = lines[start]
        if not (noise(line) or is_bare_meta_label(start)):
            break
        # '첨부파일' 라벨 뒤에는 크기 표기가 없는 파일명이 여러 줄 이어질 수 있다.
        if _ATTACHMENT_LABEL_ONLY.match(line):
            start = after_attachment_run(start + 1)
            continue
        # 라벨만 있는 줄(<dt>) 다음의 값 줄(<dd>)까지 한 줄 더 걷어낸다.
        # 바깥쪽 이웃은 값 다음 줄 — 거기가 또 라벨이면 머리말 블록이다.
        if is_value_of_label(line, start + 1, start + 2):
            start += 1
        start += 1

    end = len(lines)
    while end > start:
        line = lines[end - 1]
        if noise(line) or is_bare_meta_label(end - 1):
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
        # 값 다음 줄은 이미 잘려 나갔으므로, 바깥쪽 이웃은 라벨 앞줄이다.
        if end - 1 > start and is_value_of_label(lines[end - 2], end - 1, end - 3):
            end -= 2
            continue
        break

    return lines[start:end]


# ── 5) 발췌 조립 ────────────────────────────────────────────────────────────
_NON_WORD = re.compile(r"[^0-9A-Za-z가-힣ㄱ-ㅎㅏ-ㅣ一-龥]")


def is_meaningful(text: str) -> bool:
    """정제 결과에 한 글자 이상의 실질적인 문자 또는 숫자가 있는가."""
    return bool(_NON_WORD.sub("", text or ""))


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
    → 남은 내용을 limit 자까지 발췌. 정제 결과에 문자나 숫자가 없으면 정규화만 마친
    원본을 같은 규칙으로 자른다(기존 동작). 본문이 없으면 빈 문자열이다.
    """
    lines = normalize_lines(body)
    if not lines:
        return ""

    text = " ".join(strip_edge_noise(lines, title))
    if not is_meaningful(text):
        text = " ".join(lines)
    return truncate_snippet(text, limit)


# ── 6) 의안 전용 발췌 ───────────────────────────────────────────────────────
#
# 일반 게시물의 발췌(build_fallback_snippet)는 220자 한 줄이다. 상세 페이지 머리말을
# 걷어내고 '무슨 글인지'만 알리면 되기 때문이다. 의안은 사정이 다르다 — 본문이
# '제안이유 및 주요내용' 한 덩어리이고, 앞부분은 거의 항상 현행 제도 설명이라
# 220자에서는 "현행법은 …을 규정하고 있음."만 실리고 정작 **무엇을 바꾸는지**가
# 통째로 잘린다. AI 요약이 실패한 날 그 메일은 사실상 쓸모가 없다.
#
# 그래서 의안만 원문에서 서로 다른 의미 구간을 골라 여러 줄로 싣는다. **AI 요약이
# 아니다** — 원문 문장을 그대로 옮기는 결정적(deterministic) 발췌이고, 메일 라벨도
# '발췌'를 유지한다. 문장을 새로 쓰거나 번역·재작성하지 않는다.

# 의안 발췌 기본값. 카드 레이아웃을 무너뜨리지 않는 선에서 220자보다 넉넉하게 잡는다.
ASSEMBLY_FALLBACK_LINES = 3
ASSEMBLY_FALLBACK_CHARS = 900

# 생략 표시. 한 곳에서만 정의해 HTML/텍스트 파트가 같은 표기를 쓰게 한다.
ELLIPSIS = "…"
OMISSION_MARK = "[중략]"

# 문장 경계: '글자 + 문장부호 + 공백'일 때만 자른다.
# 앞에 글자를 요구하는 이유는 날짜·조문 번호("2026. 1. 1.", "제3조의2.")에서 잘못
# 끊지 않기 위함이다. 그런 자리는 마침표 앞이 숫자다.
_SENTENCE_SPLIT = re.compile(r"(?<=[^\W\d_][.!?…])\s+")

# 입법행위를 나타낼 가능성이 높은 표현. 의안 본문에서 '현행 제도 설명'과 '무엇을
# 바꾸는가'를 가르는 최소한의 단서만 둔다 — 목록을 키우면 첫 문장에서 바로 걸려
# 구간 선택이 무의미해진다.
_AMENDMENT_KEYWORDS = (
    "이에", "따라서", "개정", "신설", "삭제", "하도록", "하려는",
    "필요", "주요내용", "규정", "도입", "확대", "제한", "의무",
)

# 문장 분리가 통째로 실패했을 때(마침표 없는 한 덩어리) 쓰는 머리/꼬리 길이.
# 꼬리를 더 길게 잡는 이유는 의안 본문의 결론("이에 … 하려는 것임")이 끝에 있기 때문이다.
_GIANT_HEAD_CHARS = 350
_GIANT_TAIL_CHARS = 550


def _cut_at_word(text: str, limit: int, *, from_end: bool = False) -> str:
    """limit 자 이내로 자르되 가능하면 단어 중간을 끊지 않는다.

    한국어 본문에도 어절 공백이 있으므로 마지막(또는 첫) 공백까지만 취한다. 공백이
    없거나 너무 앞/뒤에서만 나오면 그냥 자른다 — 발췌가 비는 것보다는 낫다.
    """
    if limit <= 0 or len(text) <= limit:
        return text if len(text) <= limit else text[: max(limit, 0)]
    if from_end:
        piece = text[-limit:]
        space = piece.find(" ")
        # 잘린 앞부분이 통째로 사라지지 않을 정도로만 어절을 맞춘다.
        if 0 <= space <= limit // 4:
            return piece[space + 1 :]
        return piece
    piece = text[:limit]
    space = piece.rfind(" ")
    if space >= limit * 3 // 4:
        return piece[:space]
    return piece


def _assembly_sentences(text: str) -> list[str]:
    """의안 본문을 의미 있는 문장 단위로 나눈다(내용이 없는 조각은 버린다)."""
    return [
        s for s in (part.strip() for part in _SENTENCE_SPLIT.split(text)) if is_meaningful(s)
    ]


def _giant_sentence_lines(text: str, max_total_chars: int) -> list[str]:
    """문장 분리가 안 되는 한 덩어리 본문의 머리 + 중략 표시 + 꼬리."""
    marker = f"{OMISSION_MARK} "
    head_limit = min(_GIANT_HEAD_CHARS, max(max_total_chars - len(marker) - 1, 0))
    head = _cut_at_word(text, head_limit)
    used = len(head) + len(ELLIPSIS) + 1 + len(marker)
    tail_limit = min(_GIANT_TAIL_CHARS, max(max_total_chars - used, 0))
    if tail_limit <= 0:
        return [f"{head} {ELLIPSIS}"]
    tail = _cut_at_word(text, tail_limit, from_end=True)
    return [f"{head} {ELLIPSIS}", f"{marker}{tail}"]


def _pick_assembly_indexes(sentences: list[str], max_lines: int) -> list[int]:
    """싣을 문장의 위치를 고른다(원문 순서 보존, 중복 제거).

    1) 첫 의미 문장          — 배경 / 현행 제도
    2) 입법행위 문장         — 없으면 가운데 문장(서로 다른 구간을 확보하기 위함)
    3) 마지막 의미 문장      — "이에 … 하려는 것임" 계열 결론

    세 역할이 같은 문장을 가리키면 그만큼 줄 수가 줄어든다(억지로 채우지 않는다).
    """
    last = len(sentences) - 1
    picks = {0, last}
    middle = range(1, last)
    keyword_at = next(
        (
            i
            for i in middle
            if any(word in sentences[i] for word in _AMENDMENT_KEYWORDS)
        ),
        None,
    )
    if keyword_at is None and last >= 2:
        keyword_at = last // 2
    if keyword_at is not None:
        picks.add(keyword_at)
    return sorted(picks)[:max_lines]


def build_assembly_fallback_lines(
    text: str,
    *,
    max_lines: int = ASSEMBLY_FALLBACK_LINES,
    max_total_chars: int = ASSEMBLY_FALLBACK_CHARS,
) -> list[str]:
    """의안 '제안이유 및 주요내용'에서 뽑은 원문 발췌 줄들.

    **AI 요약이 아니다.** 원문 문장을 그대로 옮기는 결정적 발췌이므로 같은 입력에는
    항상 같은 출력이 나온다. 반환값은 이스케이프되지 않은 평문이다 — HTML 이스케이프는
    기존과 같이 notifier 의 책임으로 남긴다.
    """
    if max_lines <= 0 or max_total_chars <= 0:
        return []
    lines = normalize_lines(text)
    if not lines:
        return []
    body = " ".join(lines)
    if not is_meaningful(body):
        return []

    sentences = _assembly_sentences(body)
    if not sentences:
        return []
    if len(sentences) == 1 and len(sentences[0]) > max_total_chars:
        return _giant_sentence_lines(sentences[0], max_total_chars)

    if len(sentences) <= max_lines:
        chosen = sentences
    else:
        chosen = [sentences[i] for i in _pick_assembly_indexes(sentences, max_lines)]
    # 위치가 달라도 글자가 같으면 같은 줄이다. 메일에 같은 문장이 두 번 실리면 발췌가
    # 고장난 것처럼 보인다(원문이 같은 문구를 반복하는 의안은 드물지 않다).
    chosen = list(dict.fromkeys(chosen))

    return _assemble(chosen, max_total_chars)


def _assemble(chosen: list[str], max_total_chars: int) -> list[str]:
    """고른 문장들을 전체 상한 안에서 조립한다. **뒤 문장의 몫을 남긴다.**

    앞 문장에 남은 예산을 통째로 주면, 배경 문장 하나가 아주 긴 의안에서 그 문장이
    900자를 독점하고 개정 내용·결론이 통째로 사라진다 — Phase 3 이전의 '앞부분만
    보이는 발췌' 문제가 그대로 돌아온다. 그래서 줄마다 '남은 예산 ÷ 남은 줄 수'의
    공정 몫만 쓰게 하고, **잘린 뒤에도 다음 문장으로 계속 간다**.

    짧은 문장이 쓰지 않은 예산은 그대로 뒤 문장에게 넘어간다(고정 분할이 아니다):

      900자 / 3줄 → 첫 줄 상한 300
      첫 줄이 100자뿐이면 → 남은 800을 2줄이 나눠 각 400까지 가능

    말줄임표도 그 줄의 몫 안에서 센다. 전체 합은 언제나 max_total_chars 이하다.
    """
    out: list[str] = []
    used = 0
    for i, sentence in enumerate(chosen):
        remaining = max_total_chars - used
        if remaining <= 0:
            break
        # 마지막 줄(또는 한 줄짜리 발췌)에는 남은 예산을 전부 준다 — 남겨 둘 뒤 줄이
        # 없는데 아껴 봐야 발췌만 짧아진다.
        fair_cap = remaining // (len(chosen) - i)
        if len(sentence) <= fair_cap:
            out.append(sentence)
            used += len(sentence)
            continue
        # 잘렸다는 사실을 표시해 원문 확인을 유도한다. 표식(공백 + …)도 이 줄의 몫이다.
        room = fair_cap - len(ELLIPSIS) - 1
        if room <= 0:
            # 이 줄에 실을 만한 몫이 없다 — 건너뛰고 다음 줄에 기회를 준다.
            continue
        line = f"{_cut_at_word(sentence, room)} {ELLIPSIS}"
        out.append(line)
        used += len(line)
    return out
