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
</head>
<body>
  <form id="form" action="/reply/x.do?lawreqIdx=5449&amp;csrfToken=aaaabbbbccccddddeeeeffff00001111">
    <input type="hidden" name="_csrf" value="csrf-secret-456">
    <input type="hidden" name="sessionId" value="session-secret">
    <input type="hidden" name="lawreqIdx" value="5449">
  </form>
  <a href="/foo?lawreqIdx=5449&amp;sessionId=abc&amp;token=xyz">링크</a>
  <a href="/bar;jsessionid=SESSIONPATHVALUE?lawreqIdx=5449">경로 파라미터</a>
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
)


def test_hostile_html_loses_every_secret_value():
    out = redact_debug_html(HOSTILE_HTML)
    for secret in _SECRETS:
        assert secret not in out, secret
    # 쿼리 파라미터 값도 이름 기준으로 지워진다.
    assert "=abc" not in out and "=xyz" not in out and "=zzzz" not in out


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
