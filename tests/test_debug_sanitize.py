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
  <meta name="_csrf_parameter" content="_csrf">
  <meta name="authorization_header" content="Bearer META_HEADER_SECRET">
  <meta name="access_token_parameter" content="META_PARAM_SECRET">
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
  <div data-auth="Bearer QUOTED_SECRET"></div>
  <object data="/cb?access_token=OBJECT_SECRET&amp;lawreqIdx=5449"></object>
  <img srcset="/small.jpg?token=SRCSET_A 1x, /large.jpg?token=SRCSET_B 2x">
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
    # 이름이 비밀이면서 'header'/'param' 부분 문자열 예외에도 걸리던 meta. 넓은 예외가
    # 앞선 비밀 이름 판정을 덮어써서 content 가 통째로 남았다.
    "META_HEADER_SECRET",
    "META_PARAM_SECRET",
    # 따옴표 안에 공백이 있는 값. 선택적 따옴표 그룹이 빈 대안을 골라 값 문자
    # 클래스가 여는 따옴표에서 멈추면 이름만 지우고 값이 남는다.
    "QUOTED_SECRET",
    # 요소별로만 URL 인 속성(object@data). 전역 URL 속성 목록에 없어 URL 규칙을
    # 타지 않았고, 마지막 텍스트 패스는 바깥 data="…" 를 통째로 소비해 버린다.
    "OBJECT_SECRET",
    # srcset 은 단일 URL 이 아니라 후보 목록이라 URL 규칙이 닿지 않았다.
    "SRCSET_A",
    "SRCSET_B",
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
    # 공개 CSRF descriptor 는 값까지 남는다(정확 일치 면제).
    assert 'content="_csrf"' in out
    # object/srcset 은 값만 지우고 경로·디스크립터 구조는 남긴다.
    assert 'data="/cb?access_token=REDACTED&amp;lawreqIdx=5449"' in out
    assert "/small.jpg?token=REDACTED 1x" in out
    assert "/large.jpg?token=REDACTED 2x" in out
    # 비밀 이름 속성은 값만 사라지고 이름과 따옴표 표기는 남는다.
    assert 'data-auth="REDACTED"' in out


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


# ============================================================================
# 정화기 보안 계약 매트릭스
#
# Codex 리뷰가 revision 을 거듭하며 'HTML 의 어느 자리에 비밀이 실릴 수 있는가' 를
# 하나씩 찾아내고 있다. 새 프레임워크를 만들 것 없이, 지금까지 닫은 문맥과 각각의
# 기대 동작을 한자리에 적어 다음 회귀를 줄인다. 아래 각 행은 이 파일 어딘가의
# 실제 테스트로 잠겨 있고, test_security_contract_matrix 가 한 번에 다시 확인한다.
#
#   문맥                              기대 동작
#   --------------------------------------------------------------
#   meta/input 비밀 이름의 값          REDACTED
#   알려진 CSRF descriptor            값 보존(정확 일치만)
#   화면 텍스트의 secret=value        값 REDACTED
#   따옴표 친 secret="a b"            따옴표 안 전체 REDACTED
#   단일 URL 속성(href/src/action …)   URL 규칙으로 정화
#   object@data                       URL 규칙으로 정화(요소 한정)
#   img/source@srcset, link@imagesrcset  후보마다 URL 정화 / 해석 불가면 fail-closed
#   meta http-equiv=refresh 의 대상    URL 규칙으로 정화 / 해석 불가면 fail-closed
#   og:url 계열 URL metadata          URL 규칙으로 정화(allow-set 정확 일치)
#   URL fragment / userinfo           통째로 제거
#   인라인 script                     내용 제거
#   요청 예외 메시지                   애초에 출력하지 않는다(capture 스크립트)
#   공개 식별자(lawreqIdx …)           값까지 보존
# ============================================================================
def test_security_contract_matrix():
    """위 매트릭스를 한 번에 확인한다 — 행 하나가 무너지면 여기서 걸린다."""
    out = redact_debug_html(HOSTILE_HTML)

    # redact 되어야 하는 것
    for secret in _SECRETS:
        assert secret not in out, secret

    # 보존되어야 하는 것
    for public in (
        "X-CSRF-TOKEN",                     # 알려진 CSRF descriptor
        'content="_csrf"',                  # 알려진 CSRF descriptor
        "lawreqIdx=5449",                   # 공개 식별자
        'name="sessionId"',                 # 필드 이름
        "1x",                               # srcset 디스크립터
        "/small.jpg",                       # srcset 경로
        "example.com/detail",               # host/path
        'content="금융규제 법령해석 안내"',    # 일반 meta@content
        "여신전문금융회사가 신기술사업자에 투자하는 경우",   # 공개 제목
        "토큰 발급 절차에 관한 질의입니다",                  # 한국어 자연어
    ):
        assert public in out, public


# --- Codex P2 #1: Restrict the meta exemption to known CSRF descriptors ---
#
# 예전에는 meta 이름에 'header' 나 'param' 이 들어가기만 하면 앞선 비밀 이름 판정을
# 통째로 덮어썼다. authorization_header 는 'auth' 로 비밀이면서 'header' 로 면제,
# access_token_parameter 는 'token' 으로 비밀이면서 'param' 으로 면제라 값이 남았다.
# 면제는 실제로 공개 descriptor 임이 확인된 이름의 정확 일치로만 둔다.
def test_csrf_header_descriptor_value_survives():
    """1. _csrf_header 의 content 는 토큰이 아니라 보낼 헤더 '이름'이다."""
    out = redact_debug_html('<meta name="_csrf_header" content="X-CSRF-TOKEN">')
    assert "X-CSRF-TOKEN" in out


def test_csrf_parameter_descriptor_value_survives():
    """2. _csrf_parameter 의 content 는 필드 '이름'이다."""
    assert 'content="_csrf"' in redact_debug_html('<meta name="_csrf_parameter" content="_csrf">')


def test_authorization_header_meta_is_redacted():
    """3. Codex repro — 'auth' 로 비밀인데 'header' 로 면제되던 자리."""
    out = redact_debug_html('<meta name="authorization_header" content="Bearer VERY_SECRET">')
    assert "VERY_SECRET" not in out and "Bearer" not in out
    assert 'content="REDACTED"' in out
    assert 'name="authorization_header"' in out      # 이름은 진단에 필요하다


def test_access_token_parameter_meta_is_redacted():
    """4. Codex repro — 'token' 으로 비밀인데 'param' 으로 면제되던 자리."""
    out = redact_debug_html('<meta name="access_token_parameter" content="VERY_SECRET">')
    assert "VERY_SECRET" not in out
    assert 'content="REDACTED"' in out


def test_session_header_meta_is_redacted():
    """5. 'session' + 'header' 조합도 마찬가지다."""
    assert "SESSION_SECRET" not in redact_debug_html(
        '<meta name="session_header" content="SESSION_SECRET">'
    )


def test_plain_csrf_meta_is_still_redacted():
    """6. 회귀 — 면제 목록에 없는 _csrf 는 기존대로 지운다."""
    assert "CSRF_SECRET" not in redact_debug_html('<meta name="_csrf" content="CSRF_SECRET">')


def test_safe_meta_descriptor_names_match_exactly():
    """7. 면제는 정확 일치다. 접두·접미가 붙은 이름은 봐주지 않는다."""
    for name, secret in (
        ("_csrf_header_extra", "EXTRA_SECRET"),
        ("foo_csrf_parameter", "FOO_SECRET"),
        ("x_csrf_header", "PREFIXED_SECRET"),
    ):
        out = redact_debug_html(f'<meta name="{name}" content="{secret}">')
        assert secret not in out, name


def test_secret_input_value_is_still_redacted_and_public_input_survives():
    """input 은 면제 대상이 아니다. 이름 판정은 _is_secret_field 로 통일돼 있다."""
    out = redact_debug_html(
        '<input type="hidden" name="_csrf_header" value="INPUT_SECRET">'
        '<input type="hidden" name="lawreqIdx" value="5449">'
    )
    assert "INPUT_SECRET" not in out          # descriptor 면제는 meta 에만 적용된다
    assert 'value="5449"' in out              # 공개 식별자는 값까지 남는다


# --- Codex P2 #2: Redact the complete contents of quoted secret assignments ---
#
# 따옴표를 optional 그룹 하나로 두면 정규식이 '따옴표 없음' 분기를 골라, 값 문자
# 클래스가 여는 따옴표에서 즉시 멈춘다. 그러면 이름만 REDACTED 로 바뀌고 값은 뒤에
# 그대로 남는다 — 지운 것처럼 보이는데 안 지워진, 가장 나쁜 형태다. 그래서 닫힌
# 따옴표 / 닫히지 않은 따옴표 / 따옴표 없음을 서로 다른 대안으로 파싱한다.
def test_quoted_secret_assignment_loses_the_whole_value():
    """1. Codex repro. 따옴표 표기는 남기고 안쪽 전체를 지운다."""
    out = redact_debug_text('authToken="Bearer VERY_SECRET"')
    assert "VERY_SECRET" not in out and "Bearer" not in out
    assert out == 'authToken="REDACTED"'


def test_single_quoted_secret_value_with_spaces_is_removed():
    """2. 홑따옴표도 같고, 원래 따옴표 종류를 유지한다."""
    assert redact_debug_text("sessionId='abc def ghi'") == "sessionId='REDACTED'"


def test_spacing_around_the_separator_is_preserved():
    """3. 표기(공백)는 진단 정보다 — 구조는 남기고 값만 지운다."""
    out = redact_debug_text('csrfToken = "abc def"')
    assert "abc def" not in out
    assert out == 'csrfToken = "REDACTED"'


def test_quoted_value_does_not_swallow_the_next_assignment():
    """4. 닫는 따옴표까지만 소비한다 — 그 뒤 공개 식별자는 살아남는다."""
    out = redact_debug_text("token=\"AAA BBB\"&lawreqIdx=5449&sessionId='CCC DDD'")
    assert "AAA BBB" not in out and "CCC DDD" not in out
    assert "lawreqIdx=5449" in out
    assert out == "token=\"REDACTED\"&lawreqIdx=5449&sessionId='REDACTED'"


def test_html_attribute_quoted_secret_is_removed():
    """5. 직렬화된 속성도 마지막 텍스트 패스가 같은 문법으로 읽는다."""
    out = redact_debug_html('<div data-auth="Bearer VERY_SECRET"></div>')
    assert "VERY_SECRET" not in out and "Bearer" not in out
    assert 'data-auth="REDACTED"' in out


def test_html_attribute_single_quoted_secret_is_removed():
    """6. 임의 속성을 _URL_VALUED_ATTRS 에 넣지 않는다 — URL 문맥이 아니다."""
    out = redact_debug_html("<div data-session-id='SESSION VALUE'></div>")
    assert "SESSION VALUE" not in out
    assert 'data-session-id="REDACTED"' in out


def test_non_secret_quoted_assignment_is_left_alone():
    """7. 이름이 비밀이 아니면 따옴표 안 내용을 건드리지 않는다."""
    text = 'title="Bearer is a public word"'
    assert redact_debug_text(text) == text


def test_unquoted_assignment_behaviour_is_unchanged():
    """8. 회귀 — 따옴표 없는 기존 동작 그대로."""
    assert redact_debug_text("access_token=abc") == "access_token=REDACTED"
    assert redact_debug_text("lawreqIdx=5449") == "lawreqIdx=5449"
    assert redact_debug_text("sessionId = pre-secret") == "sessionId = REDACTED"


def test_unterminated_quoted_secret_is_redacted_fail_safe():
    """닫는 따옴표가 없으면 값을 지우되, 닫아 주지는 않는다(망가진 것도 진단 정보).

    예전 문법에서는 여기서도 이름만 지우고 값이 남았다. 경계는 태그·줄 단위로
    좁게 둔다 — 직렬화된 HTML 은 한 줄로 나오는 일이 흔해서 경계가 없으면 따옴표
    하나가 문서 뒷부분을 통째로 삼킨다.
    """
    assert redact_debug_text('token="unterminated secret') == 'token="REDACTED'
    out = redact_debug_html('<pre>token="unterminated secret</pre><p>lawreqIdx=5449</p>')
    assert "unterminated secret" not in out
    assert "</pre>" in out and "lawreqIdx=5449" in out      # 구조와 공개 값은 남는다


def test_unterminated_quote_under_a_public_key_still_scans_inside():
    """닫히지 않은 따옴표 분기가 삼킨 부분도 다시 검사한다.

    이름이 비밀이 아니라고 매치 전체를 그대로 돌려주면, 그 안에 든 다른
    '비밀 이름=값' 이 검사 없이 통과한다.
    """
    out = redact_debug_html('<pre>title="oops sessionId=NESTED_SECRET</pre>')
    assert "NESTED_SECRET" not in out
    assert "</pre>" in out


def test_quoted_assignment_rule_leaves_korean_prose_alone():
    """회귀 — 키는 ASCII 식별자만 받는다. 한국어 문장은 손대지 않는다."""
    for prose in (
        "토큰 발급 절차에 관한 질의입니다",
        "회신일: 2026-09-07, 처리구분 완료",
        '판시사항은 "동일기능 동일규제" 원칙이다',
    ):
        assert redact_debug_text(prose) == prose


# --- Codex P2 #3: Sanitize the omitted standard URL attributes ---
#
# 전역 _URL_VALUED_ATTRS 에 없는 표준 URL 자리가 남아 있었다. 마지막 텍스트 패스는
# 직렬화된 data="…" / srcset="…" 를 '비밀 이름이 아닌 assignment' 하나로 통째로
# 소비해 버리므로 안쪽 access_token=… 을 다시 보지 않는다(og:url 때와 같은 구조).
# object@data 는 단일 URL, srcset 은 'URL [디스크립터]' 후보 목록이라 문법이 다르다.
def test_object_data_url_is_sanitized():
    """1. Codex repro — object@data 는 단일 URL 이다."""
    out = redact_debug_html('<object data="/cb?access_token=TOPSECRET"></object>')
    assert "TOPSECRET" not in out
    assert 'data="/cb?access_token=REDACTED"' in out


def test_object_data_reuses_the_whole_url_contract():
    """2. userinfo·쿼리·프래그먼트를 URL 정화기가 그대로 처리한다."""
    out = redact_debug_html(
        '<object data="https://alice:PASSWORD@example.com/cb?lawreqIdx=5449#SECRET_FRAGMENT"></object>'
    )
    for secret in ("alice", "PASSWORD", "SECRET_FRAGMENT"):
        assert secret not in out, secret
    assert "example.com/cb" in out
    assert "lawreqIdx=5449" in out


def test_img_srcset_single_candidate_is_sanitized():
    """3. Codex repro — 디스크립터 없는 단일 후보."""
    out = redact_debug_html('<img srcset="/img?access_token=TOPSECRET">')
    assert "TOPSECRET" not in out
    assert 'srcset="/img?access_token=REDACTED"' in out


def test_img_srcset_multiple_candidates_keep_descriptors():
    """4. 후보마다 URL 만 정화하고 1x/2x 는 남긴다 — 공개 값이라 진단에 쓸모 있다."""
    out = redact_debug_html('<img srcset="/small.jpg?token=A 1x, /large.jpg?sessionId=B 2x">')
    assert "=A " not in out and "=B " not in out
    assert 'srcset="/small.jpg?token=REDACTED 1x, /large.jpg?sessionId=REDACTED 2x"' in out


def test_source_and_link_srcset_use_the_same_rule():
    """5. source@srcset 과 link@imagesrcset 도 같은 문법이다."""
    out = redact_debug_html(
        '<source srcset="/s.jpg?token=SOURCE_SECRET 1x">'
        '<link rel="preload" as="image" imagesrcset="/p.jpg?token=LINK_SECRET 2x">'
    )
    assert "SOURCE_SECRET" not in out and "LINK_SECRET" not in out
    assert "/s.jpg?token=REDACTED 1x" in out
    assert "/p.jpg?token=REDACTED 2x" in out


def test_public_srcset_keeps_its_meaning():
    """6. 정화할 것이 없는 srcset 은 의미가 그대로 남는다."""
    out = redact_debug_html('<img srcset="/a.jpg 1x, /b.jpg 2x">')
    assert 'srcset="/a.jpg 1x, /b.jpg 2x"' in out


def test_srcset_data_uri_is_redacted_whole_fail_closed():
    """7-a. data URI 는 쉼표가 payload 문법이라 후보 경계를 믿을 수 없다 → 전체 제거."""
    out = redact_debug_html('<img srcset="data:image/png;base64,AAAA?token=T 1x, /b.jpg 2x">')
    assert "AAAA" not in out and "token=T" not in out
    assert 'srcset="REDACTED"' in out


def test_srcset_unparsable_candidate_is_redacted_whole_fail_closed():
    """7-b. 디스크립터 형태를 벗어난 후보는 쪼개기를 믿을 수 없다 → 그 후보 제거."""
    out = redact_debug_html('<img srcset="/x?token=MAL weird stuff, /ok.jpg 2x">')
    assert "MAL" not in out and "weird" not in out
    assert "/ok.jpg 2x" in out                # 정상 후보는 살린다


def test_generic_data_attribute_is_not_treated_as_a_url():
    """8. object 한정 규칙이다 — 임의 요소의 data 속성까지 URL 로 추측하지 않는다."""
    out = redact_debug_html('<div data="그냥 공개 진단 값"></div>')
    assert 'data="그냥 공개 진단 값"' in out


def test_ping_url_list_is_sanitized_per_token():
    """a/area@ping 은 공백으로 구분된 URL 목록이다 — 단일 URL 로 넘기면 정화가 헛돈다.

    Codex #3 과 같은 부류('전역 URL 속성 목록에 없는 표준 URL 자리')이고 기존
    redact_debug_url 재사용만으로 닫히므로 함께 처리한다.
    """
    out = redact_debug_html(
        '<a ping="/p1?access_token=PING_A /p2?sessionId=PING_B" href="/x">t</a>'
        '<area ping="/p3?token=AREA_SECRET">'
    )
    for secret in ("PING_A", "PING_B", "AREA_SECRET"):
        assert secret not in out, secret
    assert "/p1?access_token=REDACTED" in out
    assert "/p2?sessionId=REDACTED" in out
    assert "/p3?token=REDACTED" in out
    assert 'href="/x"' in out            # 기존 단일 URL 규칙은 그대로


def test_public_ping_target_survives():
    """정화할 것이 없으면 목록은 그대로 남는다."""
    out = redact_debug_html('<a ping="/track?lawreqIdx=5449" href="/x">t</a>')
    assert 'ping="/track?lawreqIdx=5449"' in out
