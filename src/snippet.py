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


def strip_html(text: str) -> str:
    """본문에 HTML 이 남아 있으면 기존 프로젝트 방식(BeautifulSoup+lxml)으로 태그를 제거."""
    if not text or not _HTML_TAG.search(text):
        return text or ""
    soup = BeautifulSoup(text, "lxml")
    for el in soup(["script", "style"]):
        el.decompose()
    return soup.get_text("\n")


# ── 2) 공백·엔티티 정규화 ───────────────────────────────────────────────────
# 눈에 안 보이거나 폭만 다른 공백류. 그대로 두면 제목 비교와 길이 계산이 어긋난다.
_SPACE_LIKE = re.compile(
    "[\u00a0\u1680\u2000-\u200d\u202f\u205f\u3000\ufeff]"
)
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
    text = _SPACE_LIKE.sub(" ", text)

    lines = []
    for raw in text.splitlines():
        line = _HORIZONTAL_WS.sub(" ", raw).strip()
        if line:
            lines.append(line)
    return lines


# ── 3) 제목 중복 판정 ───────────────────────────────────────────────────────
# 제목 비교용으로 지우는 문자는 공백과 괄호·따옴표·구분자뿐이다. 글자와 숫자는 남기므로
# "은행법 개정" 과 "은행법 개정안" 은 여전히 다른 것으로 본다.
_TITLE_TRIVIA = re.compile(
    r"[\s\[\]()（）〔〕【】<>《》「」『』｢｣\"'“”‘’`·・ㆍ:：;,./|~\-–—_!?]+"
)
# 제목이 이 정도로 짧으면 우연히 겹칠 수 있어 중복 판정에 쓰지 않는다.
_MIN_TITLE_KEY_CHARS = 2


def _title_key(text: str) -> str:
    return _TITLE_TRIVIA.sub("", text or "")


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
_DECORATION = re.compile(r"^[\s\-=_~*·・ㆍ‧∙•▪◦□■○●◇◆※☞▶▷]+$")

# 첨부파일 목록 줄. "보도자료.hwp (188 KB)" 처럼 파일명(+크기)만 있는 줄만 지운다.
_ATTACHMENT_FILE = re.compile(
    r"^.{1,120}\.(?:hwp|hwpx|pdf|docx?|xlsx?|pptx?|zip|jpe?g|png|gif|bmp|txt|csv)"
    r"(?:\s*\(?\s*[\d.,]+\s*[KMGkmg]?B\s*\)?)?$",
    re.IGNORECASE,
)

# 페이지 하단 만족도 조사.
_SURVEY = re.compile(r"만족도\s*(?:조사|평가)|만족하[십셨]")

# 보도자료 배포 안내(엠바고·출처 표기 요청).
_PRESS_NOTICE = re.compile(
    r"배포\s*즉시\s*보도|즉시\s*보도\s*(?:하여|해)?\s*주시|인용하여\s*보도"
    r"|출처를?\s*(?:표기|명시|밝혀)|엠바고"
)

# 자바스크립트 사용 안내. 두 조건을 함께 만족할 때만 지운다.
_JS_WORD = re.compile(r"자바\s*스크립트|javascript", re.IGNORECASE)
_JS_HINT = re.compile(r"지원|사용|활성|허용|enable", re.IGNORECASE)


def _nav_key(line: str) -> str:
    return _NAV_TRIM.sub("", line).lower()


def _looks_like_meta_value(line: str) -> bool:
    """라벨만 있는 줄 바로 뒤에 오는 값 줄(부서명·날짜·조회수 등)로 볼 수 있는가."""
    return 0 < len(line) <= _META_VALUE_MAX_CHARS and not _SENTENCE_TAIL.search(line)


def is_boilerplate(line: str) -> bool:
    """수집 결과에서 반복적으로 확인된 상용구·메타데이터 줄인가(확실한 패턴만)."""
    if not line:
        return True
    return bool(
        _DECORATION.match(line)
        or _nav_key(line) in _NAV_WORDS
        or _BREADCRUMB.match(line)
        or _LABEL_LINE.match(line)
        or _ATTACHMENT_FILE.match(line)
        or _SURVEY.search(line)
        or _PRESS_NOTICE.search(line)
        or (_JS_WORD.search(line) and _JS_HINT.search(line))
    )


def strip_edge_noise(lines: list[str], title: str = "") -> list[str]:
    """본문 앞뒤에 붙은 제목 중복·상용구·메타데이터 줄을 걷어낸다.

    앞에서부터, 그리고 뒤에서부터 '확실한 군더더기'만 벗겨 내고 실제 내용을 만나면
    즉시 멈춘다. 가운데를 훑지 않으므로 본문 문장이 사라질 여지가 없다.
    """
    def noise(line: str) -> bool:
        return is_duplicate_title(line, title) or is_boilerplate(line)

    start = 0
    while start < len(lines):
        line = lines[start]
        if not noise(line):
            break
        # 라벨만 있는 줄(<dt>) 다음의 값 줄(<dd>)까지 한 줄 더 걷어낸다.
        if (
            _LABEL_ONLY.match(line)
            and start + 1 < len(lines)
            and _looks_like_meta_value(lines[start + 1])
        ):
            start += 1
        start += 1

    end = len(lines)
    while end > start:
        line = lines[end - 1]
        if noise(line):
            end -= 1
            continue
        # 꼬리말이 "담당부서 / 기업회계팀" 처럼 라벨+값 두 줄로 끝나는 경우.
        if (
            end - 1 > start
            and _LABEL_ONLY.match(lines[end - 2])
            and _looks_like_meta_value(line)
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
