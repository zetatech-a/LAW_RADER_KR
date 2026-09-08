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
# <meta http-equiv="refresh" content="0;url=…"> 의 content 문법.
# meta@content 는 일반적으로 URL 이 아니므로(description·og:title …) _URL_VALUED_ATTRS
# 에 넣을 수 없다. http-equiv 가 refresh 인 meta 만 이 문법으로 따로 읽는다.
#   지연시간 ; [공백] url [공백] = [공백] 대상
# 첫 번째 ';' 만 구분자다 — 대상 URL 안의 ';jsessionid=…' 을 구분자로 오인하면 안 된다.
_REFRESH_DELAY = re.compile(r"^\s*\d*(?:\.\d+)?\s*$")
_REFRESH_TARGET = re.compile(r"^(?P<lead>\s*)(?P<kw>url)(?P<mid>\s*=\s*)(?P<target>.*)$", re.I | re.S)

_SECRET_ASSIGNMENT = re.compile(
    r"""(?P<key>[A-Za-z][A-Za-z0-9_.\-]{0,63})
        (?P<sep>\s*=\s*)
        (?P<quote>["']?)
        (?P<value>[^"'\s&;#<>]*)
        (?P=quote)""",
    re.VERBOSE,
)

# 값이 URL 인 HTML 속성. 문자열이 아니라 URL 로 취급해 정화한다.
_URL_VALUED_ATTRS = (
    "action", "formaction", "href", "src", "poster", "cite",
    "data-url", "data-src", "data-href", "data-action",
)

# 공개 식별자라 값을 남겨도 되는 키. 이름 힌트보다 우선한다 — 덤프가 어느 글의
# 것인지 알 수 없으면 진단 자료로서 쓸모가 없다.
#   의안(likms):      billId / billNo / age …
#   회신사례(better): lawreqIdx / opinionIdx / dataIdx
# 회신사례 키들은 현재 어떤 힌트에도 걸리지 않지만, 힌트가 늘어날 때 상세 식별자가
# 조용히 지워지지 않도록 명시해 둔다.
_PUBLIC_URL_KEYS = {
    "billid", "bill_id", "billno", "agefrom", "ageto", "age", "tabnm",
    "lawreqidx", "opinionidx", "dataidx",
}


def _is_secret_field(name: str) -> bool:
    low = (name or "").strip().lower()
    if low in _PUBLIC_URL_KEYS:
        return False
    return any(h in low for h in _SECRET_FIELD_HINTS)


def _redact_assignment(m: "re.Match[str]") -> str:
    """'key=value' 한 건. 이름이 비밀이면 값만 지우고, 아니면 원문 그대로 둔다."""
    if not _is_secret_field(m.group("key")):
        return m.group(0)
    quote = m.group("quote")
    return f"{m.group('key')}{m.group('sep')}{quote}{_REDACTED}{quote}"


def redact_debug_text(text: str) -> str:
    """값 패턴과 비밀 이름의 'key=value' 를 지운다. 이름·공개 값은 남긴다.

    값 패턴(JSESSIONID=…, 긴 16진수)만으로는 부족하다 — 화면에 그대로 찍힌
    'access_token=…' 은 속성도 스크립트도 아니라 다른 규칙이 닿지 않는다.
    이름 판정은 _is_secret_field 를 그대로 쓰므로 lawreqIdx 같은 공개 키는 값까지
    남는다.
    """
    out = text or ""
    for pat in _SECRET_VALUE_PATTERNS:
        out = pat.sub(_REDACTED, out)
    return _SECRET_ASSIGNMENT.sub(_redact_assignment, out)


def redact_debug_url(url: str) -> str:
    """URL 의 쿼리·경로 파라미터에서 비밀 이름의 '값'만 지운다. 이름은 남긴다.

    값 패턴만으로는 부족하다 — 하이픈 섞인 UUID·Base64 토큰은 어느 패턴에도 걸리지
    않고, 패턴은 쿼리 파라미터 '이름'을 모른다. 경로 파라미터(;jsessionid=…)까지 보는
    이유는 쿠키가 막힌 클라이언트에 서블릿 컨테이너가 그 자리에 세션 ID 를 붙이기
    때문이다(likms 가 그 형태다).

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
            out.append(
                f"{name}={_REDACTED if _is_secret_field(name) else redact_debug_text(value)}"
            )
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
    X-CSRF-TOKEN)이라 남긴다 — 계약이 바뀌었는지 보려면 그 값이 필요하다.
    http-equiv="refresh" 인 meta 의 content 만은 URL 을 싣는 자리이므로 따로 읽는다
    (_redact_meta_refresh_content). 그 밖의 meta@content(description·og:title …)는
    URL 이 아니므로 건드리지 않는다.
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

    for el in soup.find_all(["input", "meta"]):
        name = (el.get("name") or el.get("id") or "").lower()
        attr = "value" if el.name == "input" else "content"
        if not el.get(attr) or not any(h in name for h in _SECRET_FIELD_HINTS):
            continue
        if el.name == "meta" and ("header" in name or "param" in name):
            continue
        el[attr] = _REDACTED

    # 인라인 스크립트에 토큰이 박혀 오는 경우가 흔하다. 셀렉터 진단에는 쓰이지 않는다.
    for el in soup.find_all("script"):
        if not (el.get("src") or "").strip():
            el.string = ""

    # 값이 URL 인 속성(form@action, a@href, script@src …)도 URL 규칙을 태운다.
    # 필드 값과 같은 이유다 — 속성 이름은 비밀이 아니라 마스킹 대상이 아니고,
    # 값 패턴은 쿼리 파라미터 '이름'을 모른다. 외부 script@src 는 위에서 일부러
    # 남기므로 특히 위험하다. 이 덤프는 debug/ 로 아티팩트에 올라간다.
    for el in soup.find_all(True):
        for attr in _URL_VALUED_ATTRS:
            value = el.get(attr)
            if isinstance(value, str) and value.strip():
                el[attr] = _redact_attr_url(value)

    return redact_debug_text(str(soup))
