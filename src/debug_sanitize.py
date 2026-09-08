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


def redact_debug_text(text: str) -> str:
    """값 패턴만 지운다. HTML 로 파싱할 수 없는 문자열에 쓴다."""
    out = text or ""
    for pat in _SECRET_VALUE_PATTERNS:
        out = pat.sub(_REDACTED, out)
    return out


def redact_debug_url(url: str) -> str:
    """URL 의 쿼리·경로 파라미터에서 비밀 이름의 '값'만 지운다. 이름은 남긴다.

    값 패턴만으로는 부족하다 — 하이픈 섞인 UUID·Base64 토큰은 어느 패턴에도 걸리지
    않고, 패턴은 쿼리 파라미터 '이름'을 모른다. 경로 파라미터(;jsessionid=…)까지 보는
    이유는 쿠키가 막힌 클라이언트에 서블릿 컨테이너가 그 자리에 세션 ID 를 붙이기
    때문이다(likms 가 그 형태다).
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
    return urlunparse(
        parts._replace(
            path=redact_debug_text(parts.path),
            params=_values(parts.params, ";"),
            query=_values(parts.query, "&"),
        )
    )


def _redact_attr_url(value: str) -> str:
    """URL 속성값 정화. 정화할 파라미터가 없으면 구조를 그대로 둔다.

    이름 기준 정화가 필요한 것은 쿼리(?a=b)와 경로 파라미터(;a=b)뿐이다. 둘 다 없는
    값까지 재조립하면 정화와 무관한 곳이 바뀐다(href="#" → href="").
    """
    if "?" in value or ";" in value:
        return redact_debug_url(value)
    return redact_debug_text(value)


def redact_debug_html(html: str) -> str:
    """덤프용 정화: 이름이 비밀인 meta/input 값을 지우고 값 패턴도 지운다.

    _csrf_header / _csrf_parameter 의 content 는 토큰이 아니라 헤더 '이름'(예:
    X-CSRF-TOKEN)이라 남긴다 — 계약이 바뀌었는지 보려면 그 값이 필요하다.
    파싱이 실패해도 원문을 그대로 흘리지 않고 값 패턴 정화는 반드시 적용한다.

    제목·본문 같은 공개 텍스트와 태그·클래스 구조는 건드리지 않는다. 정화가 진단을
    망치면 덤프를 남기는 의미가 없다.
    """
    text = html or ""
    try:
        soup = BeautifulSoup(text, "lxml")
    except Exception:  # noqa: BLE001 — 진단 덤프가 파서 문제로 실패하면 안 된다
        return redact_debug_text(text)

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
