"""공통 진단 정화(src/debug_sanitize.py) 회귀 테스트.

debug/ 는 verify 워크플로가 아티팩트로 올리고, 캡처 스크립트는 같은 내용을 Actions
로그로도 찍는다. 둘 다 영속 경계이므로 응답 HTML 이 싣고 오는 비밀(CSRF 토큰, 세션
값, 인라인 토큰, URL 쿼리·경로 파라미터)이 평문으로 남으면 안 된다. 반대로 정화가
구조·필드 이름·공개 식별자를 지워 버리면 덤프의 진단 가치가 사라진다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.debug_sanitize import (  # noqa: E402
    _redact_attr_url,
    _redact_meta_refresh_content,
    redact_debug_html,
    redact_debug_text,
    redact_debug_url,
)

# Codex 리뷰가 지목한 hostile 덤프 HTML.
HOSTILE_HTML = """
<html>
<head>
  <meta name="_csrf" content="csrf-secret-123">
  <meta name="_csrf_header" content="X-CSRF-TOKEN">
  <meta http-equiv="refresh" content="0;url=/callback?access_token=refresh-secret&amp;lawreqIdx=5449">
  <meta property="og:url" content="https://example.test/callback?access_token=og-secret&amp;lawreqIdx=5449">
  <meta property="og:title" content="금융규제 법령해석">
  <meta name="description" content="금융규제 법령해석 안내">
</head>
<body>
  <form id="form" action="/reply/x.do?lawreqIdx=5449&amp;csrfToken=aaaabbbbccccddddeeeeffff00001111">
    <input type="hidden" name="_csrf" value="csrf-secret-456">
    <input type="hidden" name="sessionId" value="session-secret">
    <input type="hidden" name="lawreqIdx" value="5449">
  </form>
  <a href="/foo?lawreqIdx=5449&amp;sessionId=abc&amp;token=xyz">링크</a>
  <a href="/bar;jsessionid=SESSIONPATHVALUE?lawreqIdx=5449">경로 파라미터</a>
  <a href="/callback#access_token=fragment-secret">callback</a>
  <a href="#session=fragment-session">프래그먼트만</a>
  <a href="#">top</a>
  <form action="/cb#access_token=form-secret"></form>
  <script src="/x.js#token=script-secret"></script>
  <a href="https://alice:userinfo-secret@example.com/detail?lawreqIdx=5449">userinfo</a>
  <p>/callback?access_token=text-secret&amp;lawreqIdx=5449</p>
  <div>csrfToken="quoted-secret"</div>
  <pre>sessionId = pre-secret</pre>
  <p>토큰 발급 절차에 관한 질의입니다</p>
  <script>
    window.token = "super-secret";
  </script>
  <script src="/static/reply.js?authToken=zzzz"></script>
  <td class="subject">여신전문금융회사가 신기술사업자에 투자하는 경우 …</td>
  <p>□ 질의요지 본문입니다.</p>
</body>
</html>
"""

_SECRETS = (
    "csrf-secret-123",
    "csrf-secret-456",
    "session-secret",
    "super-secret",
    "SESSIONPATHVALUE",
    "aaaabbbbccccddddeeeeffff00001111",
    # URL 프래그먼트에 실린 credential. 쿼리와 문법이 달라 이름으로 고를 수 없다.
    "fragment-secret",
    "fragment-session",
    "form-secret",
    "script-secret",
    # URL authority 의 userinfo(user:pass@host). username 자체도 credential·PII 다.
    "userinfo-secret",
    "alice",
    # 화면에 그대로 찍힌 'key=value' — 속성도 스크립트도 아니라 다른 규칙이 닿지 않는다.
    "text-secret",
    "quoted-secret",
    "pre-secret",
    # meta refresh 의 대상 URL — content 는 URL 속성이 아니라 별도 문법이라 다른
    # 규칙이 하나도 닿지 않는다.
    "refresh-secret",
    # content 가 URL 로 정해진 metadata(og:url …). 직렬화된 속성 안의 중첩 비밀은
    # 마지막 텍스트 패스가 바깥 content="…" 를 통째로 소비해 버려 잡지 못한다.
    "og-secret",
)


def test_hostile_html_loses_every_secret_value():
    out = redact_debug_html(HOSTILE_HTML)
    for secret in _SECRETS:
        assert secret not in out, secret
    # 쿼리 파라미터 값도 이름 기준으로 지워진다.
    assert "=abc" not in out and "=xyz" not in out and "=zzzz" not in out


def test_hostile_html_keeps_public_url_paths_after_fragment_redaction():
    """4~6. href/action/src 어디에 실려도 프래그먼트 값은 사라지고 경로는 남는다."""
    out = redact_debug_html(HOSTILE_HTML)
    assert 'href="/callback#REDACTED"' in out
    assert 'href="#REDACTED"' in out
    assert 'action="/cb#REDACTED"' in out
    assert 'src="/x.js#REDACTED"' in out


def test_bare_local_anchor_is_left_alone():
    """7. 회귀 — 값 없는 프래그먼트는 정화할 것이 없다. href="#" → href="" 금지."""
    assert 'href="#"' in redact_debug_html(HOSTILE_HTML)
    assert _redact_attr_url("#") == "#"
    assert _redact_attr_url("/a#") == "/a#"
    assert _redact_attr_url("/plain") == "/plain"


# --- URL 프래그먼트 (Codex P2: Redact secret-bearing URL fragments) ---
#
# 프래그먼트는 쿼리와 문법이 다르고 'key=value' 라는 보장도 없다(#opaque-token,
# #route/session/value, #/cb?access_token=…). OAuth implicit flow 에서는 액세스 토큰
# 자체가 이 자리에 실린다. 이름으로 고르려 하면 예상 못 한 형태를 놓치므로, 비어 있지
# 않은 프래그먼트는 통째로 지운다 — 진단에서 그 '값'은 없어도 된다.
def test_1_access_token_fragment_is_redacted():
    out = redact_debug_url("/callback#access_token=very-secret")
    assert "very-secret" not in out
    assert "access_token=very-secret" not in out
    assert out == "/callback#REDACTED"


def test_2_fragment_only_session_is_redacted():
    out = redact_debug_url("#session=xyz")
    assert "xyz" not in out
    assert "REDACTED" in out


def test_3_public_query_id_survives_fragment_redaction():
    out = redact_debug_url("/reply.do?lawreqIdx=5449#access_token=very-secret")
    assert "lawreqIdx=5449" in out          # 프래그먼트를 지우느라 쿼리를 지우면 안 된다
    assert "very-secret" not in out
    assert out.endswith("#REDACTED")


def test_opaque_and_nested_fragments_are_redacted_whole():
    """이름이 없는 형태도 남기지 않는다 — 이번 정책의 핵심."""
    for dirty in ("#opaque-token", "#/callback?access_token=abc", "#route/session/value"):
        out = redact_debug_url(dirty)
        assert out == "#REDACTED", dirty


def test_url_without_fragment_does_not_gain_one():
    """'#' 이 없던 URL 에 '#REDACTED' 가 새로 붙으면 안 된다."""
    for clean in ("/plain", "https://x/y?lawreqIdx=5449", "/bar;jsessionid=A?opinionIdx=2324"):
        assert "#" not in redact_debug_url(clean), clean


def test_hostile_html_keeps_public_diagnostics_after_every_rule():
    """정화가 진단을 망치면 덤프를 남기는 의미가 없다 — 공개 정보는 남는다."""
    out = redact_debug_html(HOSTILE_HTML)
    assert "example.com/detail" in out              # host/path 구조는 유지
    assert "lawreqIdx=5449" in out
    assert 'csrfToken="REDACTED"' in out            # 이름은 남고 값만 사라진다
    assert "sessionId = REDACTED" in out            # 공백 표기도 보존
    assert "access_token=REDACTED" in out
    assert "토큰 발급 절차에 관한 질의입니다" in out   # 자연어는 손대지 않는다
    assert "0;url=/callback?access_token=REDACTED" in out   # refresh 는 구조를 유지
    assert 'content="금융규제 법령해석 안내"' in out        # 일반 meta@content 는 무변경
    assert 'content="금융규제 법령해석"' in out             # og:title 도 URL 이 아니다
    assert "https://example.test/callback?access_token=REDACTED" in out


def test_hostile_html_keeps_structure_and_public_content():
    out = redact_debug_html(HOSTILE_HTML)
    # 필드 이름·구조는 남는다 — 없으면 무엇이 바뀌었는지 진단할 수 없다.
    assert 'name="_csrf"' in out
    assert 'name="sessionId"' in out
    assert "sessionId=" in out and "token=" in out and "authToken=" in out
    assert 'class="subject"' in out
    assert "/static/reply.js" in out
    # 공개 상세 식별자는 값까지 남는다.
    assert out.count("lawreqIdx=5449") >= 3
    assert 'name="lawreqIdx"' in out and 'value="5449"' in out
    # 정상 공개 텍스트(제목·본문)는 그대로다.
    assert "여신전문금융회사가 신기술사업자에 투자하는 경우" in out
    assert "□ 질의요지 본문입니다." in out
    # _csrf_header 의 content 는 토큰이 아니라 헤더 '이름'이라 남긴다.
    assert "X-CSRF-TOKEN" in out


def test_url_redaction_keeps_names_and_public_ids():
    out = redact_debug_url("/foo?lawreqIdx=5449&sessionId=abc&token=xyz")
    assert "lawreqIdx=5449" in out
    assert "sessionId=REDACTED" in out and "token=REDACTED" in out
    assert "abc" not in out and "xyz" not in out


def test_url_redaction_handles_path_parameter_session_id():
    out = redact_debug_url("/bar;jsessionid=SESSIONPATHVALUE?opinionIdx=2324")
    assert "SESSIONPATHVALUE" not in out
    assert "opinionIdx=2324" in out          # 세션 패턴이 '?' 를 넘어 삼키지 않는다


def test_public_reply_ids_are_not_treated_as_secret_names():
    for name, value in (("lawreqIdx", "5449"), ("opinionIdx", "2324"), ("dataIdx", "5450")):
        assert f"{name}={value}" in redact_debug_url(f"/d.do?{name}={value}")


def test_unparsable_text_still_loses_value_patterns():
    dirty = "JSESSIONID=ABC123; WMONID=XYZ789; 0123456789abcdef0123456789abcdef"
    out = redact_debug_text(dirty)
    for secret in ("ABC123", "XYZ789", "0123456789abcdef0123456789abcdef"):
        assert secret not in out


def test_assembly_and_reply_share_one_implementation():
    """정화가 두 벌로 갈라지면 한쪽만 고쳐지고 다른 쪽에서 비밀이 샌다."""
    from src.scrapers import assembly, better_fsc

    assert assembly._redact_html is redact_debug_html
    assert assembly._redact_url is redact_debug_url
    assert assembly._redact_values is redact_debug_text
    assert better_fsc.redact_debug_html is redact_debug_html


# --- Codex: 화면 텍스트에 찍힌 'key=value' (속성·스크립트가 아닌 자리) ---
#
# 오류 페이지가 요청 URL 을 그대로 echo 하거나 <pre> 로 파라미터를 늘어놓으면
# input/meta·script·URL 속성 규칙이 하나도 닿지 않는다. 이름 판정은 _is_secret_field
# 를 그대로 재사용하므로 _PUBLIC_URL_KEYS 가 존중된다.
def test_text_secret_assignment_is_redacted_and_public_key_survives():
    out = redact_debug_text("/callback?access_token=very-secret&lawreqIdx=5449")
    assert "very-secret" not in out
    assert "access_token=REDACTED" in out
    assert "lawreqIdx=5449" in out


def test_text_session_assignment_is_redacted():
    assert "session-secret" not in redact_debug_text("sessionId=session-secret")
    # 공백을 둔 표기도 잡고, 표기 자체는 보존한다.
    assert redact_debug_text("csrfToken = abc") == "csrfToken = REDACTED"


def test_text_quoted_secret_value_is_redacted_without_swallowing_the_rest():
    out = redact_debug_text('csrfToken="quoted-secret" 뒤 문장은 남는다')
    assert "quoted-secret" not in out
    assert out.endswith("뒤 문장은 남는다")


def test_text_assignment_rule_leaves_prose_alone():
    """자연어까지 지우는 과도한 규칙이 되면 안 된다."""
    for prose in (
        "토큰 발급 절차에 관한 질의입니다",
        "token: 중요한 의미",
        "질의요지: 금리인하요구권 안내의무 적용 여부",
    ):
        assert redact_debug_text(prose) == prose


def test_html_text_node_secret_is_redacted():
    out = redact_debug_html("<p>/callback?access_token=html-secret&lawreqIdx=5449</p>")
    assert "html-secret" not in out
    assert "lawreqIdx=5449" in out


def test_public_ids_in_text_survive_the_assignment_rule():
    for pair in ("lawreqIdx=5449", "opinionIdx=2324", "dataIdx=5450", "billId=PRC_ABC"):
        assert pair in redact_debug_text(f"조회 파라미터: {pair}")


# --- Codex: URL authority 의 userinfo(user:pass@host) ---
#
# 진단에 필요한 것은 host/port 구조이지 자격 증명이 아니다. username 자체도
# credential·PII 일 수 있어 REDACTED 로 남길 이유가 없어 통째로 버린다.
def test_userinfo_is_dropped_and_host_survives():
    out = redact_debug_url("https://alice:very-secret@example.com/detail")
    assert out == "https://example.com/detail"


def test_username_only_userinfo_is_dropped():
    assert redact_debug_url("https://alice@example.com/detail") == "https://example.com/detail"


def test_userinfo_drop_keeps_host_and_port():
    assert redact_debug_url("//alice:secret@example.com:8443/path") == "//example.com:8443/path"


def test_userinfo_drop_keeps_ipv6_authority():
    out = redact_debug_url("https://alice:secret@[2001:db8::1]:8443/path")
    assert out == "https://[2001:db8::1]:8443/path"


def test_userinfo_query_and_fragment_are_handled_together():
    out = redact_debug_url(
        "https://alice:secret@example.com/detail?lawreqIdx=5449&sessionId=SESSION#TOKEN"
    )
    assert "alice" not in out and "secret" not in out and "SESSION" not in out
    assert "lawreqIdx=5449" in out
    assert out.endswith("#REDACTED")


def test_at_sign_in_a_relative_path_is_not_touched():
    """netloc 이 없는 상대 URL 의 '@' 는 userinfo 가 아니다."""
    assert redact_debug_url("/mail/user@example.com") == "/mail/user@example.com"


def test_html_attribute_userinfo_url_reaches_the_url_sanitizer():
    """쿼리도 프래그먼트도 없는 userinfo URL 은 예전에 정화기를 아예 타지 않았다."""
    out = redact_debug_html('<a href="https://alice:html-secret@example.com/detail">x</a>')
    assert "alice" not in out and "html-secret" not in out
    assert 'href="https://example.com/detail"' in out


# --- Codex: meta refresh 의 대상 URL ---
#
# <meta http-equiv="refresh" content="0;url=…"> 의 content 는 URL 을 싣지만
# _URL_VALUED_ATTRS 가 아니고, 'delay;url=TARGET' 이라는 별도 문법이라 마지막
# 텍스트 패스도 바깥 'url=…' 하나로 볼 뿐 그 안의 access_token 을 다시 URL 로 읽지
# 않는다. meta@content 전체를 URL 로 취급할 수는 없으므로(description·og:title)
# http-equiv=refresh 인 meta 만 따로 읽어 redact_debug_url 을 재사용한다.
def test_meta_refresh_target_url_is_sanitized():
    """1. Codex repro."""
    safe = redact_debug_html(
        '<meta http-equiv="refresh" '
        'content="0;url=/callback?access_token=TOPSECRET&lawreqIdx=5449">'
    )
    assert "TOPSECRET" not in safe
    assert "access_token=REDACTED" in safe
    assert "lawreqIdx=5449" in safe
    assert "/callback" in safe


def test_meta_refresh_is_case_and_whitespace_tolerant():
    """2. http-equiv 과 url 키워드는 대소문자를 가리지 않고, 공백 표기는 보존한다."""
    safe = redact_debug_html(
        '<meta http-equiv="Refresh" '
        'content="5 ; URL = /cb?sessionId=SESSION_SECRET&opinionIdx=2284">'
    )
    assert "SESSION_SECRET" not in safe
    assert "opinionIdx=2284" in safe
    assert "5 ; URL = " in safe


def test_meta_refresh_quoted_target_is_sanitized():
    """3. 따옴표로 감싼 대상도 벗겨 정화한 뒤 같은 따옴표로 되감는다."""
    for quote in ("'", "&quot;"):
        safe = redact_debug_html(
            f'<meta http-equiv="refresh" '
            f'content="0;url={quote}https://example.com/cb?token=QUOTED_SECRET{quote}">'
        )
        assert "QUOTED_SECRET" not in safe, quote
        assert "example.com/cb" in safe, quote


def test_meta_refresh_first_semicolon_only_is_the_separator():
    """4. 대상 안의 ';jsessionid=…' 을 refresh 구분자로 오인하면 안 된다."""
    safe = redact_debug_html(
        '<meta http-equiv="refresh" '
        'content="0;url=/cb;jsessionid=JS_SECRET?lawreqIdx=5449#access_token=FRAG_SECRET">'
    )
    assert "JS_SECRET" not in safe and "FRAG_SECRET" not in safe
    assert "lawreqIdx=5449" in safe
    assert "0;url=/cb;" in safe


def test_meta_refresh_target_userinfo_is_dropped():
    """5. 대상 URL 의 userinfo 도 URL 정화기가 그대로 처리한다."""
    safe = redact_debug_html(
        '<meta http-equiv="refresh" content="0;url=https://alice:PASSWORD@example.com/detail">'
    )
    assert "alice" not in safe and "PASSWORD" not in safe
    assert "https://example.com/detail" in safe


def test_malformed_meta_refresh_is_redacted_whole():
    """6. 해석할 수 없으면 content 를 통째로 지운다(fail-closed).

    망가진 refresh 문자열을 진단에서 잃는 것보다 credential 이 남는 쪽이 위험하다.
    """
    safe = redact_debug_html(
        '<meta http-equiv="refresh" content="weird access_token=MALFORMED_SECRET">'
    )
    assert "MALFORMED_SECRET" not in safe
    assert 'content="REDACTED"' in safe
    # 'url=' 이 없거나 지연시간 자리가 이상한 값도 마찬가지다.
    for bad in ("0;noturl=abc", "junk;url=/x?token=T"):
        assert _redact_meta_refresh_content(bad) == "REDACTED", bad


def test_delay_only_meta_refresh_is_left_alone():
    """URL 이 없는 순수 지연 refresh 는 비밀이 아니므로 그대로 둔다."""
    safe = redact_debug_html('<meta http-equiv="refresh" content="5">')
    assert 'content="5"' in safe
    assert _redact_meta_refresh_content(" 0.5 ") == " 0.5 "


def test_ordinary_meta_content_is_never_touched():
    """7. 회귀 방어 — meta@content 를 일반 URL 속성으로 취급하면 안 된다."""
    safe = redact_debug_html(
        '<meta name="description" content="금융규제 법령해석 안내">'
        '<meta property="og:title" content="게시물 제목">'
    )
    assert 'content="금융규제 법령해석 안내"' in safe
    assert 'content="게시물 제목"' in safe


def test_public_meta_refresh_target_survives():
    """8. 공개 식별자만 실린 refresh 는 구조까지 그대로 남는다."""
    safe = redact_debug_html(
        '<meta http-equiv="refresh" content="0;url=/detail?lawreqIdx=5449">'
    )
    assert 'content="0;url=/detail?lawreqIdx=5449"' in safe


# --- Codex: content 가 URL 로 정해진 metadata(og:url 계열) ---
#
# meta@content 자체는 URL 이 아니므로(description·og:title·keywords) 일반 URL 속성으로
# 넣을 수 없고, 이름에 'url' 이 들어가는지 같은 추측도 하지 않는다. 표준으로 URL 이라고
# 정해진 키만 정확 일치로 골라 기존 redact_debug_url 을 태운다. 마지막 텍스트 패스는
# 직렬화된 content="…" 를 바깥 assignment 하나로 소비해 그 안의 중첩 비밀을 보지 못한다.
def test_og_url_metadata_is_sanitized():
    """Codex exact repro."""
    safe = redact_debug_html(
        '<meta property="og:url" '
        'content="https://example.test/callback?access_token=VERY_SECRET&lawreqIdx=5449">'
    )
    assert "VERY_SECRET" not in safe
    assert "access_token=REDACTED" in safe
    assert "lawreqIdx=5449" in safe
    assert "example.test/callback" in safe


def test_open_graph_media_url_metadata_is_sanitized():
    safe = redact_debug_html(
        '<meta property="og:image" content="https://cdn.example/img.png?token=IMAGE_SECRET">'
    )
    assert "IMAGE_SECRET" not in safe
    assert "cdn.example/img.png" in safe

    safe = redact_debug_html(
        '<meta property="og:video:secure_url" '
        'content="https://cdn.example/video?sessionId=VIDEO_SECRET">'
    )
    assert "VIDEO_SECRET" not in safe
    assert "cdn.example/video" in safe


def test_twitter_card_url_metadata_is_sanitized():
    for key in ("twitter:image", "twitter:player", "twitter:app:url:iphone"):
        safe = redact_debug_html(
            f'<meta name="{key}" content="https://cdn.example/x?token=TWITTER_SECRET">'
        )
        assert "TWITTER_SECRET" not in safe, key
        assert "cdn.example/x" in safe, key


def test_itemprop_url_metadata_is_sanitized():
    for key in ("url", "contentUrl", "embedUrl", "thumbnailUrl"):
        safe = redact_debug_html(
            f'<meta itemprop="{key}" content="https://example.test/x?token=ITEMPROP_SECRET">'
        )
        assert "ITEMPROP_SECRET" not in safe, key
        assert "example.test/x" in safe, key


def test_url_metadata_reuses_the_whole_url_contract():
    """userinfo·쿼리·프래그먼트 정화를 metadata 에서도 그대로 받는다."""
    safe = redact_debug_html(
        '<meta property="og:url" '
        'content="https://alice:PASSWORD@example.test/detail?lawreqIdx=5449#FRAGMENT_SECRET">'
    )
    for secret in ("alice", "PASSWORD", "FRAGMENT_SECRET"):
        assert secret not in safe, secret
    assert "example.test/detail" in safe
    assert "lawreqIdx=5449" in safe


def test_url_metadata_keys_are_matched_exactly_not_by_substring():
    """이름에 'url' 이 들어간다고 URL 로 보지 않는다 — 표준 키만 정확 일치."""
    from src.debug_sanitize import _meta_content_is_url
    from bs4 import BeautifulSoup

    def _meta(markup):
        return BeautifulSoup(markup, "lxml").find("meta")

    assert _meta_content_is_url(_meta('<meta property="og:url" content="x">'))
    assert _meta_content_is_url(_meta('<meta itemprop="ThumbnailUrl" content="x">'))
    assert not _meta_content_is_url(_meta('<meta name="canonical-url-note" content="x">'))
    assert not _meta_content_is_url(_meta('<meta itemprop="name" content="x">'))
    assert not _meta_content_is_url(_meta('<meta property="og:title" content="x">'))
    # RDFa·마이크로데이터는 값을 공백으로 여러 개 적을 수 있다.
    assert _meta_content_is_url(_meta('<meta property="og:title og:url" content="x">'))


def test_ordinary_metadata_survives_the_url_metadata_rule():
    """URL metadata 규칙이 일반 metadata 로 번지면 안 된다."""
    safe = redact_debug_html(
        '<meta name="description" content="금융규제 포털 안내">'
        '<meta name="keywords" content="금융, 법령해석, 비조치의견서">'
        '<meta property="og:title" content="access_token이라는 용어에 관한 법령해석">'
    )
    assert 'content="금융규제 포털 안내"' in safe
    assert 'content="금융, 법령해석, 비조치의견서"' in safe
    # 'access_token=' assignment 가 아니라 그냥 낱말이므로 그대로 남는다.
    assert "access_token이라는 용어에 관한 법령해석" in safe
