"""진단 덤프 정화 — debug/ 로 나가는 HTML·URL 에서 비밀 '값'만 지운다.

debug/ 는 verify 워크플로가 아티팩트로 업로드하고(보존 7일), 캡처 스크립트는 같은
내용을 Actions 로그로도 찍는다. 그런데 덤프가 남는 때는 정확히 '요청을 만들지
못했거나 응답이 이상한' 때이고, 그 원본 HTML 에는 살아 있는 세션의 CSRF 토큰이
meta[name="_csrf"] 와 hidden input 값으로, 세션 ID 가 URL 쿼리·경로 파라미터로,
토큰이 인라인 스크립트 문자열로 들어 있다. 요청 헤더·쿠키를 저장하지 않는 것만으로는
부족하다 — **응답 본문 자체가 비밀을 싣고 온다.**

진단에 필요한 것은 구조와 필드 '이름'이므로 값만 지우고 이름은 남긴다. 공개 식별자
(billId, lawreqIdx …)는 그 덤프가 어느 글의 것인지 알려 주는 유일한 단서라 남긴다.

이 모듈은 의안(assembly) 스크래퍼에서 검증된 구현을 그대로 옮긴 것이다. 회신사례
(better_reply)와 그 진단 스크립트가 같은 함수를 쓰게 해서 정화 로직이 두 벌로 갈라져
어긋나는 것을 막는다. 사이트에 종속된 부분은 _PUBLIC_URL_KEYS 뿐이며, 그 안에서
소스별 공개 키를 한곳에 모아 둔다.

공개 함수:
  redact_debug_html(html)  — 덤프/로그에 남길 HTML 의 안전한 사본
  redact_debug_url(url)    — 덤프/로그에 남길 URL 의 안전한 사본
  redact_debug_text(text)  — 값 패턴만 지운다(파싱 불가한 문자열용)

**파싱에는 원본을 쓴다.** 정화본은 영속화·출력 직전에 따로 만든다.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse

from bs4 import BeautifulSoup

_SECRET_FIELD_HINTS = (
    "csrf", "xsrf", "token", "session", "jsessionid", "wmonid",
    "auth", "secret", "passwd", "password", "nonce", "sid",
)
# 부분 문자열 힌트에 걸리지 않지만 이름 자체가 credential 인 필드. 힌트로 넓히면
# ('key' 를 힌트에 넣으면 menuNo… 는 아니어도 sessionKey·monkey 류까지) 오탐이
# 늘어나므로 **정확 일치**로만 둔다. URL 쿼리는 아래 zero-trust 정책이 따로 막으므로
# 이 집합은 화면 텍스트·JSON·meta/input 이름 판정에서 쓰인다.
#   'code' 는 여기에 넣지 않는다 — {"CODE":"INFO-000"} 같은 공개 상태 코드가 흔하다.
#   OAuth 의 ?code=… 는 URL zero-trust 가 이름을 몰라도 지운다.
_SECRET_EXACT_NAMES = frozenset({
    "key", "api_key", "apikey", "api-key",
    "service_key", "servicekey", "service-key",
    "access_key", "accesskey", "access-key",
    "signature", "sig",
    "x-amz-signature", "x-amz-credential", "x-amz-security-token",
    "credential", "credentials",
})
# 세션 쿠키 값에는 '?' 도 '&' 도 들어가지 않는다. 문자 클래스에서 둘 다 빼지 않으면
# 이미 구조가 있는 문자열에서 패턴이 URL 경계를 넘어 삼켜, 정화 뒤에 남아야 할 키
# 이름과 공개 식별자(billId)까지 지운다 — 덤프의 진단 가치가 바로 그 billId 다.
_SECRET_VALUE_PATTERNS = (
    re.compile(r"JSESSIONID=[^;\"'\s&?]+", re.I),
    re.compile(r"WMONID=[^;\"'\s&?]+", re.I),
    re.compile(r"\b[0-9a-f]{32,}\b", re.I),
)
_REDACTED = "REDACTED"

# 눈에 보이는 텍스트에 그대로 찍힌 'key=value'. 오류 페이지가 요청 URL 을 화면에
# echo 하거나 <pre> 로 파라미터를 늘어놓을 때 실제로 생긴다 — 그 자리는 속성도
# 스크립트도 아니라 위의 규칙이 하나도 닿지 않는다.
#
# 이름은 _is_secret_field 로 판정한다(문자열을 따로 나열하지 않는다). 그래야
# _PUBLIC_URL_KEYS 가 그대로 존중되어 lawreqIdx=5449 같은 공개 식별자가 살아남는다.
#   - 이름은 ASCII 식별자만 받는다. 한국어 문장('토큰 발급 절차…')을 건드리지 않는다.
#   - 값은 따옴표가 있으면 그 안까지, 없으면 다음 경계 전까지다. 경계를 좁게 잡지
#     않으면 뒤따르는 다른 파라미터나 HTML 태그까지 통째로 삼킨다.
# content 가 URL 인 것이 **표준으로 정해진** metadata 키. meta@content 자체는 URL 이
# 아니므로(description·og:title·keywords) _URL_VALUED_ATTRS 에 넣을 수 없고, 이름에
# 'url' 이 들어가는지 같은 추측도 하지 않는다. 아래 정확 일치만 URL 로 취급한다.
#   property / name 으로 오는 키(둘 다 실제로 쓰인다)
_URL_META_KEYS = frozenset({
    # Open Graph
    "og:url",
    "og:image", "og:image:url", "og:image:secure_url",
    "og:video", "og:video:url", "og:video:secure_url",
    "og:audio", "og:audio:url", "og:audio:secure_url",
    "og:see_also",
    # Twitter Card — URL 인 것이 확실한 키만
    "twitter:image", "twitter:image:src",
    "twitter:player", "twitter:player:stream",
    "twitter:app:url:iphone", "twitter:app:url:ipad", "twitter:app:url:googleplay",
})
#   itemprop 으로 오는 키. 값이 og:/twitter: 처럼 접두어가 없어 따로 둔다.
_URL_ITEMPROP_KEYS = frozenset({"url", "contenturl", "embedurl", "thumbnailurl"})

# <meta http-equiv="refresh" content="0;url=…"> 의 content 문법.
# meta@content 는 일반적으로 URL 이 아니므로(description·og:title …) _URL_VALUED_ATTRS
# 에 넣을 수 없다. http-equiv 가 refresh 인 meta 만 이 문법으로 따로 읽는다.
#   지연시간 ; [공백] url [공백] = [공백] 대상
# 첫 번째 ';' 만 구분자다 — 대상 URL 안의 ';jsessionid=…' 을 구분자로 오인하면 안 된다.
_REFRESH_DELAY = re.compile(r"^\s*\d*(?:\.\d+)?\s*$")
_REFRESH_TARGET = re.compile(r"^(?P<lead>\s*)(?P<kw>url)(?P<mid>\s*=\s*)(?P<target>.*)$", re.I | re.S)

# 따옴표가 '있을 수도 없을 수도' 있는 하나의 문법으로 쓰면 안 된다. 선택적 따옴표
# 그룹은 빈 대안을 고를 수 있고, 그러면 값 문자 클래스가 여는 따옴표에서 즉시 멈춰
# authToken="Bearer VERY_SECRET" 이 authToken=REDACTED"Bearer VERY_SECRET" 이 된다 —
# 이름만 지우고 값은 남는 최악의 결과다. 그래서 세 경우를 각각의 대안으로 나눈다.
#   1) 닫힌 따옴표  key="공백 포함 값"  → 값 전체가 하나
#   2) 닫히지 않은 따옴표 key="…        → 태그·줄 경계까지 fail-safe 로 지운다
#   3) 따옴표 없음  key=값             → 기존 동작 그대로
# 두 따옴표 대안 모두 '<' '>' 와 줄바꿈을 값에서 제외한다. 직렬화된 HTML 은 한 줄로
# 나오는 일이 흔해서, 경계를 두지 않으면 따옴표 하나가 뒤따르는 태그를 통째로 삼켜
# 덤프의 구조가 사라진다.
_SECRET_ASSIGNMENT = re.compile(
    r"""(?P<key>[A-Za-z][A-Za-z0-9_.\-]{0,63})
        (?P<sep>\s*=\s*)
        (?:
            (?P<quote>["'])(?P<quoted>[^"'<>\r\n]*)(?P=quote)
          | (?P<dquote>["'])(?P<dangling>[^"'<>\r\n]*)
          | (?P<unquoted>[^"'\s&;#<>]*)
        )""",
    re.VERBOSE,
)

# 오류 응답이 JSON 을 화면에 그대로 찍는 일이 있다(<pre>{"access_token":"…"}</pre>).
# 그 자리는 'key=value' 가 아니라 JSON member 라 위 assignment 문법이 닿지 않는다.
# 기존 문법을 ':' 까지 넓히지 않는다 — 그러면 'token: 이 단어는…' 같은 평범한 산문과
# CSS 선언(color:red)까지 assignment 로 오인한다. 대신 JSON member 만 보는 좁은 문법을
# 따로 둔다: **키는 반드시 따옴표로 감싸여 있어야 하고**, 콜론이 있어야 한다.
#   값은 JSON string 토큰으로 읽는다 — "(?:\\.|[^"\\])*" 로 이스케이프된 따옴표를
#   한 단위로 보지 않으면 {"access_token":"Bearer \\"inner\\" secret"} 에서 잘려
#   뒷부분이 남는다. 숫자·true/false/null 스칼라도 받는다(수치형 세션 ID 가 있다).
#   텍스트 전체에 json.loads 를 시도하지 않는다 — 여기 있는 것은 HTML 에 박힌 조각이다.
_JSON_STRING = r'"(?:\\.|[^"\\])*"'
_SECRET_JSON_MEMBER = re.compile(
    r'"(?P<key>(?:\\.|[^"\\]){1,64})"'
    r'(?P<sep>\s*:\s*)'
    r'(?P<value>' + _JSON_STRING + r'|-?\d[\d.eE+\-]*|true|false|null)'
)

# 값이 URL 인 HTML 속성. 문자열이 아니라 URL 로 취급해 정화한다.
_URL_VALUED_ATTRS = (
    "action", "formaction", "href", "src", "poster", "cite",
    "data-url", "data-src", "data-href", "data-action",
)

# 값이 URL 이지만 **특정 요소에서만** 그런 속성. 전역 목록에 넣으면 안 된다 —
# 아무 div/커스텀 요소의 data="…" 까지 URL 이라고 추측하게 된다.
_ELEMENT_URL_ATTRS = {
    "object": ("data",),          # <object data="/viewer.do?…">
}

# 값이 'URL [디스크립터], URL [디스크립터] …' 후보 목록인 속성. 단일 URL 이 아니라
# 통째로 redact_debug_url 에 넘기면 안 된다(쉼표·공백이 URL 문법이 아니다).
_ELEMENT_SRCSET_ATTRS = {
    "img": ("srcset",),
    "source": ("srcset",),
    "link": ("imagesrcset",),     # <link rel="preload" as="image" imagesrcset="…">
}

# 값이 '공백으로 구분된 URL 목록' 인 속성. 단일 URL 로 넘기면 목록 전체가 하나의
# 경로로 파싱돼 정화가 헛돈다. 문법이 srcset 보다 단순해(디스크립터가 없다) 토큰마다
# redact_debug_url 을 돌리는 것으로 끝난다.
_ELEMENT_URL_LIST_ATTRS = {
    "a": ("ping",),
    "area": ("ping",),
}

# srcset 디스크립터로 인정하는 형태: 1x, 2x, 1.5x, 320w. 진단에 쓸모 있는 공개 값이라
# 남긴다. 이 형태를 벗어나면 후보를 신뢰성 있게 쪼갠 것이 아니므로 fail-closed 한다.
_SRCSET_DESCRIPTOR = re.compile(r"^\d+(?:\.\d+)?[wx]$", re.I)

# 이름이 비밀 힌트에 걸리지만 content 가 토큰이 아니라 **공개 descriptor** 인 meta.
# 예: _csrf_header → "X-CSRF-TOKEN"(헤더 이름), _csrf_parameter → "_csrf"(필드 이름).
# 계약이 바뀌었는지 보려면 그 값이 필요하다.
#
# 정확 일치만 인정한다. 예전에는 'header'/'param' 부분 문자열 예외였는데, 그러면
# authorization_header 나 access_token_parameter 처럼 **이름이 비밀이면서 동시에**
# 예외에도 걸리는 meta 의 값이 통째로 살아남았다. endswith("_header") 같은 새 추측을
# 도입하지 않는다 — 실제로 공개 descriptor 임이 확인된 이름만 여기에 명시하고,
# 새로운 descriptor 가 관찰되면 그때 명시적으로 추가하는 편이 zero-trust 에 맞는다.
_SAFE_META_DESCRIPTOR_NAMES = frozenset({"_csrf_header", "_csrf_parameter"})

# 공개 식별자라 값을 남겨도 되는 키. 이름 힌트보다 우선한다 — 덤프가 어느 글의
# 것인지 알 수 없으면 진단 자료로서 쓸모가 없다.
#   의안(likms):      billId / billNo / age …
#   회신사례(better): lawreqIdx / opinionIdx / dataIdx
# 회신사례 키들은 현재 어떤 힌트에도 걸리지 않지만, 힌트가 늘어날 때 상세 식별자가
# 조용히 지워지지 않도록 명시해 둔다.
_PUBLIC_URL_KEYS = {
    # 의안(likms) 식별자·내비게이션
    "billid", "bill_id", "billno", "agefrom", "ageto", "age", "tabnm",
    # 회신사례(better.fsc) 상세 식별자
    "lawreqidx", "opinionidx", "dataidx",
    # 회신사례 목록 URL 의 내비게이션 파라미터(config.yaml 의 list_url 과
    # better_fsc._NAV_PARAMS 에서 확인). 어느 메뉴에서 연 상세인지가 진단 정보다.
    "stno", "muno", "mugpno",
    # 게시판 메뉴·글 식별자(config.yaml 의 fss list_url, src/scrapers/fsc.py)
    "menuno", "noticeid",
    # 의안 상세의 분류·탭 코드(tests/fixtures/assembly/*/detail.html 실측)
    "detailedtab", "procgbncd", "bdgcd", "badtlgbncd", "mainprocyn", "cntsdivcd",
    # 첨부 참조. 어느 파일을 받으려다 실패했는지가 첨부 진단의 전부다
    # (better_fsc/lawreq_54xx fixture, assembly detail fixture 실측).
    # sysFileName 의 32자 16진수 값은 값 패턴이 따로 지운다.
    "orgfilename", "sysfilename", "filepath", "fileoutname", "filename", "fileext",
    # 정적 자원 캐시 버스터. 모든 페이지 덤프에 깔려 있어 지우면 잡음만 는다.
    "ver",
    # 의안 Open API 의 공개 페이지네이션(src/scrapers/assembly.py 의 요청 params).
    # 같은 요청의 'KEY' 는 인증키라 **절대 여기에 넣지 않는다.**
    "type", "pindex", "psize",
}


def _is_secret_field(name: str) -> bool:
    low = (name or "").strip().lower()
    if low in _PUBLIC_URL_KEYS:
        return False
    if low in _SECRET_EXACT_NAMES:
        return True
    return any(h in low for h in _SECRET_FIELD_HINTS)


def _redact_assignment(m: "re.Match[str]") -> str:
    """'key=value' 한 건. 이름이 비밀이면 값만 지우고, 아니면 원문 그대로 둔다.

    따옴표 표기는 보존한다 — token="…" 는 token="REDACTED", sessionId='…' 는
    sessionId='REDACTED' 로 남아 덤프에서 원래 문법을 알아볼 수 있다. 닫히지 않은
    따옴표는 닫아 주지 않는다. 값이 망가져 있었다는 사실 자체가 진단 정보다.
    """
    key, sep = m.group("key"), m.group("sep")
    dangling = m.group("dangling")
    if not _is_secret_field(key):
        if dangling is None:
            return m.group(0)
        # 닫히지 않은 따옴표 대안은 뒤쪽까지 삼켰다. 이름이 비밀이 아니라고 그대로
        # 돌려주면 그 안에 든 다른 '비밀 이름=값' 이 검사 없이 통과한다. 삼킨 만큼만
        # 다시 돌린다(항상 더 짧은 문자열이라 재귀가 끝난다).
        return f"{key}{sep}{m.group('dquote')}{_SECRET_ASSIGNMENT.sub(_redact_assignment, dangling)}"
    if dangling is not None:
        return f"{key}{sep}{m.group('dquote')}{_REDACTED}"
    quote = m.group("quote") or ""
    return f"{key}{sep}{quote}{_REDACTED}{quote}"


def _redact_json_member(m: "re.Match[str]") -> str:
    """JSON object member 한 건. 이름이 비밀이면 값 토큰 전체를 지운다.

    값이 문자열이면 따옴표는 남기고 안쪽만 REDACTED 로 바꾼다(JSON 으로 계속
    읽히는 편이 진단에 낫다). 스칼라면 REDACTED 로 대체한다.
    """
    key = m.group("key")
    if not _is_secret_field(key):
        return m.group(0)
    value = m.group("value")
    safe = f'"{_REDACTED}"' if value.startswith('"') else _REDACTED
    return f'"{key}"{m.group("sep")}{safe}'


def redact_debug_text(text: str) -> str:
    """값 패턴, JSON member, 비밀 이름의 'key=value' 를 지운다. 이름·공개 값은 남긴다.

    값 패턴(JSESSIONID=…, 긴 16진수)만으로는 부족하다 — 화면에 그대로 찍힌
    'access_token=…' 은 속성도 스크립트도 아니라 다른 규칙이 닿지 않는다.
    이름 판정은 _is_secret_field 를 그대로 쓰므로 lawreqIdx 같은 공개 키는 값까지
    남는다.

    **JSON member 를 assignment 보다 먼저 돌린다.** 직렬화된 HTML 에서
    data-json='{"access_token":"…"}' 처럼 JSON 이 속성값 안에 들어 있으면, 바깥
    data-json='…' 이 먼저 매치돼 따옴표 안 전체를 소비해 버려서 안쪽 JSON 을 다시
    보지 않는다(바깥 키가 비밀이 아니라 그대로 반환된다). 순서는
    값 패턴 → JSON member → key=value 다.
    """
    out = text or ""
    for pat in _SECRET_VALUE_PATTERNS:
        out = pat.sub(_REDACTED, out)
    out = _SECRET_JSON_MEMBER.sub(_redact_json_member, out)
    return _SECRET_ASSIGNMENT.sub(_redact_assignment, out)


def redact_debug_url(url: str) -> str:
    """URL 의 쿼리·경로 파라미터 값을 zero-trust 로 지운다. 이름은 남긴다.

    **공개로 명시된 이름(_PUBLIC_URL_KEYS)만 값을 남기고, 나머지는 전부 지운다.**
    예전에는 반대로 '비밀이라고 알려진 이름'만 지웠는데, 그러면 목록에 없는
    credential 이 그대로 나간다 — ?api_key=… ?code=… ?X-Amz-Signature=… 가 모두
    공개 취급됐고, 이 저장소가 의안 Open API 를 인증하는 ?KEY=<인증키> 도 마찬가지였다.
    이름을 하나씩 발견해 추가하는 정책은 발견될 때까지 유출된다. 진단에 필요한 것은
    파라미터 '이름'과 URL 구조이지 모르는 파라미터의 값이 아니므로, 모르는 값은
    지우는 쪽으로 기운다(KEY=REDACTED 처럼 이름은 남는다).

    경로 파라미터(;jsessionid=…)까지 보는 이유는 쿠키가 막힌 클라이언트에 서블릿
    컨테이너가 그 자리에 세션 ID 를 붙이기 때문이다(likms 가 그 형태다).

    **URL 에 userinfo(user:pass@host)가 있으면 통째로 버린다.** 진단에 필요한 것은
    host/port 구조이지 자격 증명이 아니고, username 자체도 credential·PII 일 수 있어
    REDACTED 로 남길 이유가 없다. host 와 port 는 그대로 둔다(IPv6 대괄호 포함).

    **프래그먼트는 비어 있지 않으면 통째로 지운다.** 쿼리처럼 이름만 골라 남기지
    않는다 — 프래그먼트는 'key=value' 형태라는 보장이 없고(#opaque-token,
    #route/session/value, #/cb?access_token=…), OAuth implicit flow 에서는 액세스
    토큰 자체가 이 자리에 실린다. 이름 기반으로 고르려 하면 예상하지 못한 형태의
    credential 을 놓친다. 진단 아티팩트에서 프래그먼트 '값'은 없어도 되지만
    credential 이 남는 것은 안 되므로, 값을 잃는 쪽으로 안전하게 기운다.
    """
    try:
        parts = urlparse(url or "")
    except ValueError:
        return _REDACTED

    def _values(raw: str, sep: str) -> str:
        out = []
        for chunk in raw.split(sep):
            if not chunk:
                continue
            name, eq, value = chunk.partition("=")
            if not eq:
                out.append(redact_debug_text(chunk))
                continue
            # zero-trust: 공개로 **명시된** 이름만 값을 남긴다.
            if name.strip().lower() in _PUBLIC_URL_KEYS:
                out.append(f"{name}={redact_debug_text(value)}")
            else:
                out.append(f"{name}={_REDACTED}")
        return sep.join(out)

    # 값 패턴은 조각마다 적용한다. 조립이 끝난 URL 에 다시 돌리면 경계를 넘어 삼킨다.
    # 프래그먼트가 없으면 빈 문자열을 그대로 둔다 — _REDACTED 를 넣으면 '#' 이 없던
    # URL 에 '#REDACTED' 가 새로 붙는다.
    return urlunparse(
        parts._replace(
            # userinfo 는 마지막 '@' 앞까지다. netloc 이 없으면(상대 URL) 빈 문자열이라
            # 경로 안의 '@'(/mail/user@example.com)는 건드리지 않는다.
            netloc=parts.netloc.rsplit("@", 1)[-1],
            path=redact_debug_text(parts.path),
            params=_values(parts.params, ";"),
            query=_values(parts.query, "&"),
            fragment=_REDACTED if parts.fragment else "",
        )
    )


def _redact_attr_url(value: str) -> str:
    """URL 속성값 정화. 정화할 것이 없으면 구조를 그대로 둔다.

    정화가 필요한 것은 쿼리(?a=b)·경로 파라미터(;a=b)·**비어 있지 않은 프래그먼트**
    (#access_token=…)다. 프래그먼트만 있는 값(href="#access_token=…")은 '?' 도 ';' 도
    없어서 예전에는 URL 정화기를 아예 타지 않았고, 그 토큰이 아티팩트와 Actions
    로그에 평문으로 남았다.

    **userinfo(user:pass@host)도 마찬가지다.** 쿼리도 프래그먼트도 없는
    href="https://alice:secret@example.com/detail" 는 예전에는 URL 정화기를 타지
    않아 자격 증명이 그대로 남았다.

    넷 다 없는 값까지 재조립하면 정화와 무관한 곳이 바뀐다 — urlunparse 는 프래그먼트가
    빈 '#'/'…#' 에서 '#' 자체를 지우므로(href="#" → href=""), 그런 값은 여기서 걸러
    낸다. 값이 없는 프래그먼트에는 정화할 것도 없다.
    """
    _head, hashed, fragment = value.partition("#")
    if "?" in value or ";" in value or (hashed and fragment) or _has_userinfo(value):
        return redact_debug_url(value)
    return redact_debug_text(value)


def _has_userinfo(value: str) -> bool:
    """URL 의 authority 에 user[:pass]@ 가 붙어 있는지. 상대 URL 은 netloc 이 없다."""
    try:
        return "@" in urlparse(value).netloc
    except ValueError:
        return True          # 파싱조차 안 되는 값은 정화기로 보낸다(fail-safe)


def _redact_srcset(value: str) -> str:
    """srcset/imagesrcset 후보 목록을 후보마다 URL 규칙으로 정화한다.

    srcset 은 단일 URL 이 아니라 'URL [디스크립터]' 를 쉼표로 이은 목록이라
    redact_debug_url 에 통째로 넘길 수 없다. 그렇다고 WHATWG 파서를 새로 구현하지도
    않는다 — 이건 진단 덤프용 정화기다. 흔한 형태는 보존하며 정화하고, 문법을
    신뢰성 있게 쪼개지 못하면 fail-closed 한다.

    정책:
      - 후보 = 쉼표로 나눈 조각. 첫 토큰이 URL, 나머지가 디스크립터.
      - 디스크립터는 1x·2x·1.5x·320w 형태만 인정하고 그대로 남긴다(공개 값이라
        진단에 쓸모 있다). 그 형태를 벗어나면 그 **후보 전체를 지운다** — 쪼개기가
        맞았다고 확신할 수 없는데 URL 만 골라 정화하면 나머지에 credential 이 남는다.
      - 값 어디에든 'data:' 가 있으면 **srcset 전체를 지운다.** data URI 는 쉼표
        자체가 payload 문법이라 단순 쉼표 분리가 후보 경계를 틀리게 잡는다.
      - 빈 후보(후행 쉼표 등)는 그대로 둔다.

    진단 정보를 잃는 편이 credential 을 남기는 것보다 낫다.
    """
    raw = value or ""
    if "data:" in raw.lower():
        return _REDACTED

    out = []
    for candidate in raw.split(","):
        body = candidate.strip()
        if not body:
            out.append(candidate)
            continue
        lead = candidate[: len(candidate) - len(candidate.lstrip())]
        trail = candidate[len(candidate.rstrip()):]
        url, *descriptors = body.split()
        if not all(_SRCSET_DESCRIPTOR.match(d) for d in descriptors):
            out.append(f"{lead}{_REDACTED}{trail}")
            continue
        out.append(lead + " ".join([redact_debug_url(url), *descriptors]) + trail)
    return ",".join(out)


def _redact_url_list(value: str) -> str:
    """공백으로 구분된 URL 목록(a@ping)을 토큰마다 URL 규칙으로 정화한다.

    srcset 과 달리 디스크립터가 없어 모든 토큰이 URL 이다. 구분 공백은 하나로
    정규화한다 — 이 자리의 진단 가치는 '어디로 보내는가' 이지 여백이 아니다.
    """
    return " ".join(redact_debug_url(token) for token in (value or "").split())


def _meta_content_is_url(el) -> bool:
    """이 meta 의 content 가 URL 이라고 **구조적으로 확정되는지**.

    property/name 은 og:·twitter: 계열의 정확 일치로, itemprop 은 마이크로데이터의
    URL 속성 이름으로 판정한다. 목록에 없는 metadata(description·og:title·keywords …)
    는 URL 이 아니므로 건드리지 않는다 — 추측으로 넓히면 진단 자료가 망가진다.
    RDFa·마이크로데이터는 값을 공백으로 여러 개 적을 수 있어 토큰으로 쪼개 본다.
    """
    for attr, keys in (
        ("property", _URL_META_KEYS),
        ("name", _URL_META_KEYS),
        ("itemprop", _URL_ITEMPROP_KEYS),
    ):
        value = el.get(attr)
        if isinstance(value, list):                 # 파서가 다중값으로 준 경우
            value = " ".join(value)
        if not isinstance(value, str):
            continue
        if any(token.lower() in keys for token in value.split()):
            return True
    return False


def _redact_meta_refresh_content(content: str) -> str:
    """meta refresh 의 content 에서 대상 URL 만 뽑아 URL 규칙으로 정화한다.

    URL 문법을 새로 만들지 않는다 — 'delay ; url = TARGET' 에서 TARGET 을 떼어
    redact_debug_url 에 그대로 넘기고 같은 자리에 다시 끼운다. 대상 안의
    ';jsessionid=…' · '?token=…' · '#…' · 'user:pass@host' 는 URL 정화기가 이미 아는
    구조이므로 여기서 다시 쪼개지 않는다(첫 ';' 만 구분자로 본다).

    정책:
      - 'delay;url=TARGET'  → TARGET 만 정화하고 표기(대소문자·공백·따옴표)는 보존.
      - 숫자 지연만 있는 값('5') → URL 이 없으므로 그대로 둔다.
      - 그 밖의 해석 불가 값 → **content 전체를 지운다(fail-closed).** 진단에서
        망가진 refresh 문자열을 잃는 것보다 credential 이 남는 쪽이 위험하다.
    """
    raw = content or ""
    delay, semi, rest = raw.partition(";")
    if not semi:
        # 'url=' 이 없는 값. 순수 숫자 지연만 정상으로 인정한다.
        return raw if _REFRESH_DELAY.match(raw) else _REDACTED
    if not _REFRESH_DELAY.match(delay):
        return _REDACTED
    m = _REFRESH_TARGET.match(rest)
    if not m:
        return _REDACTED

    target = m.group("target").strip()
    quote = ""
    if len(target) >= 2 and target[0] == target[-1] and target[0] in "\"'":
        quote, target = target[0], target[1:-1]
    return (
        f"{delay};{m.group('lead')}{m.group('kw')}{m.group('mid')}"
        f"{quote}{redact_debug_url(target)}{quote}"
    )


def redact_debug_html(html: str) -> str:
    """덤프용 정화: 이름이 비밀인 meta/input 값을 지우고 값 패턴도 지운다.

    _csrf_header / _csrf_parameter 의 content 는 토큰이 아니라 헤더 '이름'(예:
    X-CSRF-TOKEN)이라 남긴다 — 계약이 바뀌었는지 보려면 그 값이 필요하다. 다만
    그 면제는 _SAFE_META_DESCRIPTOR_NAMES 의 **정확 일치**뿐이다. 이름에
    'header'/'param' 이 들어가기만 하면 봐주면 authorization_header 같은 meta 의
    값이 통째로 남는다.
    URL 이 실리는 자리는 전역 속성(_URL_VALUED_ATTRS) 외에 요소별로도 있다 —
    object@data 는 단일 URL 로, img/source@srcset 과 link@imagesrcset 은 후보
    목록이라 _redact_srcset 으로, a/area@ping 은 공백 구분 URL 목록이라
    _redact_url_list 로 따로 읽는다.
    meta@content 중 URL 을 싣는 자리만 따로 읽는다 — http-equiv="refresh" 는 별도
    문법이라 _redact_meta_refresh_content 로, og:url·twitter:image·itemprop="url" 처럼
    content 가 URL 로 정해진 키는 _meta_content_is_url 로 골라 redact_debug_url 로
    보낸다. 그 밖의 meta@content(description·og:title·keywords …)는 URL 이 아니므로
    건드리지 않는다.
    액티브 콘텐츠(인라인 script, <style>, style 속성, on* 이벤트 핸들러)는 값을
    고르지 않고 통째로 비운다 — 진단에 필요 없는데 credential 을 실어 나른다.
    파싱이 실패해도 원문을 그대로 흘리지 않고 값 패턴 정화는 반드시 적용한다.

    제목·본문 같은 공개 텍스트와 태그·클래스 구조는 건드리지 않는다. 정화가 진단을
    망치면 덤프를 남기는 의미가 없다.
    """
    text = html or ""
    try:
        soup = BeautifulSoup(text, "lxml")
    except Exception:  # noqa: BLE001 — 진단 덤프가 파서 문제로 실패하면 안 된다
        return redact_debug_text(text)

    # meta refresh 의 대상 URL. 아래 이름 기준 정화보다 **먼저** 돌린다 — 이름까지
    # 비밀인 이상한 meta 라면 그 뒤 규칙이 content 를 통째로 지워 더 안전한 쪽이 남는다.
    for el in soup.find_all("meta"):
        equiv = el.get("http-equiv")
        content = el.get("content")
        if not isinstance(equiv, str) or equiv.strip().lower() != "refresh":
            continue
        if isinstance(content, str) and content.strip():
            el["content"] = _redact_meta_refresh_content(content)

    # content 가 URL 인 것이 표준으로 정해진 metadata(og:url, twitter:image, itemprop=url
    # …). URL 임을 이미 알고 있으므로 _redact_attr_url 의 '정화할 게 있나' 판단을 거치지
    # 않고 redact_debug_url 을 바로 태운다 — userinfo·쿼리·경로 파라미터·프래그먼트를
    # 항상 구조적으로 본다. 이름 기준 정화보다 먼저 돌려 더 안전한 쪽이 남게 한다.
    for el in soup.find_all("meta"):
        content = el.get("content")
        if isinstance(content, str) and content.strip() and _meta_content_is_url(el):
            el["content"] = redact_debug_url(content)

    for el in soup.find_all(["input", "meta"]):
        name = (el.get("name") or el.get("id") or "").strip().lower()
        attr = "value" if el.name == "input" else "content"
        # 이름 판정은 _is_secret_field 로 통일한다 — _PUBLIC_URL_KEYS 가 그대로
        # 존중되어 공개 식별자 필드의 값이 조용히 지워지지 않는다.
        if not el.get(attr) or not _is_secret_field(name):
            continue
        if el.name == "meta" and name in _SAFE_META_DESCRIPTOR_NAMES:
            continue
        el[attr] = _REDACTED

    # 인라인 스크립트에 토큰이 박혀 오는 경우가 흔하다. 셀렉터 진단에는 쓰이지 않는다.
    for el in soup.find_all("script"):
        if not (el.get("src") or "").strip():
            el.string = ""

    # CSS 도 같은 이유로 통째로 비운다. style 속성 안의 url(/x?access_token=…) 은
    # URL 속성이 아니라서 URL 규칙이 닿지 않고, 마지막 텍스트 패스는 직렬화된
    # style="…" 을 '비밀 이름이 아닌 assignment' 하나로 소비해 안쪽을 보지 않는다.
    #
    # CSS url 파서를 만들지 않는다. url(…) / url("…") / url('…') / @import /
    # image-set(…) / 이스케이프 / data: 로 문법이 갈라져 정확히 쪼개기 어렵고, 진단에
    # 필요한 것은 태그·class/id·텍스트·URL 구조이지 시각 표현이 아니다. 스타일 정보를
    # 잃는 편이 credential 을 놓치는 것보다 낫다.
    #
    # <style> 은 스크립트와 같은 방식으로 **내용만** 비운다(decompose 하지 않는다) —
    # 요소가 있었다는 사실 자체는 DOM 구조 진단에 남겨 둔다.
    for el in soup.find_all("style"):
        el.string = ""

    # style 속성과 인라인 이벤트 핸들러(on*). 둘 다 액티브 콘텐츠라 진단에 쓰이지
    # 않으면서 URL·문자열을 실어 나른다. 여기서 JavaScript 정화기를 만들지 않는다 —
    # 통째로 지우는 것으로 끝낸다.
    for el in soup.find_all(True):
        if el.has_attr("style"):
            del el["style"]
        for attr in [a for a in el.attrs if a.lower().startswith("on")]:
            del el[attr]

    # 값이 URL 인 속성(form@action, a@href, script@src …)도 URL 규칙을 태운다.
    # 필드 값과 같은 이유다 — 속성 이름은 비밀이 아니라 마스킹 대상이 아니고,
    # 값 패턴은 쿼리 파라미터 '이름'을 모른다. 외부 script@src 는 위에서 일부러
    # 남기므로 특히 위험하다. 이 덤프는 debug/ 로 아티팩트에 올라간다.
    for el in soup.find_all(True):
        for attr in _URL_VALUED_ATTRS:
            value = el.get(attr)
            if isinstance(value, str) and value.strip():
                el[attr] = _redact_attr_url(value)

    # 요소별로만 URL 인 속성(object@data). 전역 목록에 "data" 를 넣으면 임의의
    # div/커스텀 요소의 data 속성까지 URL 로 오인한다.
    #
    # 이 자리가 필요한 이유는 og:url 때와 같다 — 마지막 텍스트 패스는 직렬화된
    # data="…" 를 '비밀 이름이 아닌 assignment' 하나로 통째로 소비해 버려서 그 안의
    # access_token=… 을 다시 보지 않는다.
    for name, attrs in _ELEMENT_URL_ATTRS.items():
        for el in soup.find_all(name):
            for attr in attrs:
                value = el.get(attr)
                if isinstance(value, str) and value.strip():
                    el[attr] = _redact_attr_url(value)

    for name, attrs in _ELEMENT_URL_LIST_ATTRS.items():
        for el in soup.find_all(name):
            for attr in attrs:
                value = el.get(attr)
                if isinstance(value, str) and value.strip():
                    el[attr] = _redact_url_list(value)

    for name, attrs in _ELEMENT_SRCSET_ATTRS.items():
        for el in soup.find_all(name):
            for attr in attrs:
                value = el.get(attr)
                if isinstance(value, str) and value.strip():
                    el[attr] = _redact_srcset(value)

    return redact_debug_text(str(soup))
