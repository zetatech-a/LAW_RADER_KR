"""신규 게시글을 요약한 다이제스트 이메일 발송 (SMTP)."""
from __future__ import annotations

import html
import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr

from .config import EmailConfig
from .models import ASSEMBLY_SOURCE_KEY, Post, ProposalContentStatus
from .snippet import build_assembly_fallback_lines, build_fallback_snippet

log = logging.getLogger(__name__)


def _esc(s: str) -> str:
    return html.escape(s or "")


# 기관별 강조색(제목 왼쪽 바·배지). 소스명에 포함된 키워드로 판정한다.
_ACCENTS: tuple[tuple[str, str], ...] = (
    ("금융위", "#1d4ed8"),   # 파랑
    ("금감원", "#0f766e"),   # 청록
    ("금융규제", "#7c3aed"),  # 보라
    ("의안", "#b45309"),     # 앰버
)
_DEFAULT_ACCENT = "#334155"

_FONT = (
    "-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo','Malgun Gothic',"
    "'Noto Sans KR',Roboto,'Helvetica Neue',Arial,sans-serif"
)


def _accent(source_name: str) -> str:
    for key, color in _ACCENTS:
        if key in source_name:
            return color
    return _DEFAULT_ACCENT


# HTML·텍스트 두 파트가 같은 문구를 쓰도록 한 곳에 둔다. text/plain 만 보는
# 수신자에게도 동일한 유의사항이 전달되어야 한다.
_AI_NOTICE = (
    "AI 요약은 생성형 AI가 원문을 정리한 것이라 부정확하거나 누락이 있을 수 있습니다. "
    "판단 전 반드시 원문을 확인하세요."
)


# 의안은 본문 전체가 아니라 '제안이유 및 주요내용'만 수집·요약한다. 무엇을 요약한
# 것인지가 메일에서 드러나야 원문 확인이 쉬우므로 라벨에 출처를 함께 적는다.
_ASSEMBLY_SUMMARY_PREFIX = "제안이유 및 주요내용 · "
_ASSEMBLY_BODY_LABEL = "제안이유 및 주요내용 발췌"
_BODY_LABEL = "원문 발췌"

# 등록 대기(PENDING): 원문이 아직 공개되지 않은 상태. 수집 실패와 다르므로 문구도
# 다르다 — 수신자가 '시스템 고장'이 아니라 '아직 안 올라옴'임을 알 수 있어야 한다.
_PENDING_LABEL = "제안이유 및 주요내용 · 등록 대기"
_PENDING_TEXT = "의안정보시스템에 제안이유 및 주요내용이 아직 공개되지 않았습니다."


# 상세 업데이트(복구) 알림 문구. 최초 알림 때 제안이유가 없던(PENDING) 또는 수집이
# 실패한(ERROR) 의안의 원문을 나중에 확보했을 때 쓴다. **'신규 게시물'과 반드시 구분
# 되어야 한다** — 같은 의안을 두 번 신규로 알리는 것처럼 보이면 수신자가 중복 발송으로
# 오해하고, 실제로는 이미 알린 의안의 후속 정보이기 때문이다.
_UPDATE_HEADING = "의안 상세 업데이트"
_UPDATE_NOTICE = (
    "기존에 등록 대기 또는 상세 수집 실패였던 의안의 "
    "제안이유 및 주요내용이 확인되었습니다."
)


def _is_pending(p: Post) -> bool:
    return (
        p.source_key == ASSEMBLY_SOURCE_KEY
        and p.proposal_status is ProposalContentStatus.PENDING
    )


def _summary_label(p: Post) -> str:
    """요약 블록 제목. 실제 줄 수를 그대로 표기한다(config 의 lines 를 바꿔도 맞음)."""
    label = f"AI {len(p.summary)}줄 요약"
    if p.source_key == ASSEMBLY_SOURCE_KEY:
        return _ASSEMBLY_SUMMARY_PREFIX + label
    return label


def _body_label(p: Post) -> str:
    """AI 요약이 없을 때 쓰는 발췌 블록 제목."""
    if p.source_key == ASSEMBLY_SOURCE_KEY:
        return _ASSEMBLY_BODY_LABEL
    return _BODY_LABEL


def _has_summary(*groups: "dict[str, list[Post]] | None") -> bool:
    """AI 요약이 하나라도 실렸는지(하단 유의사항 노출 여부).

    신규와 상세 업데이트를 **함께** 본다 — 업데이트만 있는 메일에도 AI 요약이 실리므로
    유의사항이 빠지면 안 된다.
    """
    return any(
        p.summary
        for group in groups
        if group
        for posts in group.values()
        for p in posts
    )


# 구조화 항목 블록의 제목. AI 요약이 아니라 원문 그대로의 항목임을 나타낸다.
_DETAILS_LABEL = "주요 정보"


def _assembly_excerpt(p: Post) -> list[str]:
    """의안 카드의 원문 발췌 줄들(HTML·텍스트 파트가 함께 쓴다).

    AI 요약이 실패한 날에도 '무엇을 바꾸는 법안인가'가 메일에서 보여야 한다. 220자
    한 줄로는 앞부분의 현행 제도 설명만 실리므로 의안만 여러 구간을 뽑는다.
    발췌 규칙이 아무것도 못 고르면 기존 220자 발췌로 되돌아간다.
    """
    lines = build_assembly_fallback_lines(p.body)
    if lines:
        return lines
    fallback = build_fallback_snippet(p.body, p.title)
    return [fallback] if fallback else []


def _details_block(p: Post) -> str:
    """상세 페이지에서 그대로 가져온 (라벨, 값) 표.

    AI 생성물이 아니므로 요약 라벨·유의사항을 붙이지 않는다. 항목 순서는 post.details
    순서를 그대로 따른다.
    """
    rows = "".join(
        "<tr class='lr-detail'>"
        "<td valign='top' style='padding:2px 14px 2px 0;font-size:12px;line-height:1.6;"
        f"color:#64748b;white-space:nowrap'>{_esc(label)}</td>"
        "<td style='padding:2px 0;font-size:13px;line-height:1.6;color:#0f172a'>"
        f"{_esc(value)}</td>"
        "</tr>"
        for label, value in p.details
    )
    return (
        "<div style='margin:10px 0 0;padding:10px 12px;background:#f8fafc;"
        "border:1px solid #e2e8f0;border-radius:6px'>"
        "<table role='presentation' cellpadding='0' cellspacing='0' "
        f"style='border-collapse:collapse'>{rows}</table>"
        "</div>"
    )


def _summary_block(p: Post, accent: str) -> str:
    """본문 영역. 구조화 항목 → LLM 요약 → 원문 발췌 순으로 보여준다.

    구조화 항목(details)은 상세 페이지의 라벨-값을 그대로 옮긴 것이라 요약보다 정확하고
    AI 유의사항 대상도 아니므로 가장 먼저 쓴다.
    요약은 LLM 장애·한도 초과로 비어 있을 수 있으므로 원문 발췌 폴백을 남겨 둔다.
    폴백은 제목 중복과 반복 안내문을 걷어낸 뒤 발췌한다(src/snippet.py).
    """
    if p.details:
        return _details_block(p)

    if p.summary:
        items = "".join(
            f"<tr>"
            f"<td valign='top' style='padding:2px 6px 0 0;font-size:13px;"
            f"line-height:1.6;color:{accent}'>&bull;</td>"
            f"<td style='font-size:13px;line-height:1.6;color:#334155'>{_esc(line)}</td>"
            f"</tr>"
            for line in p.summary
        )
        return (
            "<div style='margin:10px 0 0;padding:10px 12px;background:#f8fafc;"
            "border:1px solid #e2e8f0;border-radius:6px'>"
            f"<div style='margin:0 0 6px;font-size:10px;letter-spacing:.8px;"
            f"font-weight:700;color:{accent}'>{_esc(_summary_label(p))}</div>"
            "<table role='presentation' cellpadding='0' cellspacing='0' "
            f"style='border-collapse:collapse'>{items}</table>"
            "</div>"
        )

    if p.body:
        # 의안은 발췌의 출처(제안이유 및 주요내용)를 밝히고, 220자 한 줄 대신 원문에서
        # 고른 여러 구간을 싣는다. 그 외 소스는 기존과 같이 라벨 없이 한 줄 발췌만.
        if p.source_key == ASSEMBLY_SOURCE_KEY:
            lines = _assembly_excerpt(p)
            body_html = "".join(
                "<div style='margin:8px 0 0;font-size:13px;line-height:1.6;"
                f"color:#475569'>{_esc(line)}</div>"
                for line in lines
            )
            return (
                f"<div style='margin:10px 0 0;font-size:10px;letter-spacing:.8px;"
                f"font-weight:700;color:{accent}'>{_esc(_body_label(p))}</div>"
                f"{body_html}"
            )
        return (
            "<div style='margin:8px 0 0;font-size:13px;line-height:1.6;color:#475569'>"
            f"{_esc(build_fallback_snippet(p.body, p.title))}</div>"
        )

    # 원문이 아직 공개되지 않은 의안. 빈 카드로 두면 수집이 깨진 것처럼 보인다.
    if _is_pending(p):
        return (
            "<div style='margin:10px 0 0;padding:10px 12px;background:#f8fafc;"
            "border:1px solid #e2e8f0;border-radius:6px'>"
            f"<div style='margin:0 0 6px;font-size:10px;letter-spacing:.8px;"
            f"font-weight:700;color:{accent}'>{_esc(_PENDING_LABEL)}</div>"
            "<div style='font-size:13px;line-height:1.6;color:#475569'>"
            f"{_esc(_PENDING_TEXT)}</div></div>"
        )
    return ""


def _card(p: Post, accent: str) -> str:
    """게시글 1건 카드."""
    rows = [
        # 제목
        f"<a href='{_esc(p.url)}' style='display:block;font-size:15px;font-weight:600;"
        f"line-height:1.45;color:#0f172a;text-decoration:none'>{_esc(p.title)}</a>"
    ]

    if p.date:
        rows.append(
            f"<div style='margin:6px 0 0;font-size:12px;color:#94a3b8'>"
            f"{_esc(p.date)}</div>"
        )

    block = _summary_block(p, accent)
    if block:
        rows.append(block)

    if p.attachments:
        chips = []
        for a in p.attachments:
            note = "" if a.data else " · 링크"
            chips.append(
                f"<a href='{_esc(a.url)}' style='display:inline-block;margin:4px 6px 0 0;"
                f"padding:4px 10px;background:#f1f5f9;border:1px solid #e2e8f0;"
                f"border-radius:999px;font-size:12px;color:#334155;text-decoration:none'>"
                f"📎 {_esc(a.filename)}{note}</a>"
            )
        rows.append("<div style='margin:8px 0 0'>" + "".join(chips) + "</div>")

    rows.append(
        f"<div style='margin:10px 0 0'>"
        f"<a href='{_esc(p.url)}' style='font-size:12px;font-weight:600;color:{accent};"
        f"text-decoration:none'>원문 보기 &rsaquo;</a></div>"
    )

    return (
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' "
        "style='border-collapse:separate;margin:0 0 10px'><tr>"
        f"<td style='padding:14px 16px;background:#ffffff;border:1px solid #e2e8f0;"
        f"border-left:3px solid {accent};border-radius:8px'>"
        + "".join(rows)
        + "</td></tr></table>"
    )


def _agency(source_name: str) -> str:
    """소스명('금감원 · 검사결과 제재')에서 기관명만 뽑는다."""
    return source_name.split("·")[0].strip() or source_name


def _source_sections(posts_by_source: dict[str, list[Post]]) -> list[str]:
    """소스별 헤더 + 카드 묶음. 신규와 상세 업데이트가 같은 렌더링을 공유한다."""
    parts: list[str] = []
    for source_name, posts in posts_by_source.items():
        if not posts:
            continue
        accent = _accent(source_name)
        # ── 기관 헤더
        parts.append(
            "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' "
            "style='border-collapse:collapse;margin:10px 0 8px'><tr>"
            f"<td style='font-size:13px;font-weight:700;color:{accent}'>"
            f"{_esc(source_name)}</td>"
            f"<td align='right' style='font-size:11px;font-weight:600;color:{accent}'>"
            f"{len(posts)}건</td></tr>"
            f"<tr><td colspan='2' style='padding-top:6px;border-bottom:1px solid #e2e8f0'>"
            "</td></tr></table>"
        )
        for p in posts:
            parts.append(_card(p, accent))
    return parts


def _header_lines(total: int, updates: int, src_count: int) -> tuple[str, str]:
    """메일 상단의 (제목, 부제). 신규가 하나도 없으면 '신규'라고 쓰지 않는다.

    상세 업데이트는 이미 알린 의안의 후속 정보이므로, 그것만 있는 메일을 '신규 게시물'
    이라고 표시하면 거짓말이 된다.
    """
    # 업데이트가 없으면 신규가 0건이어도 기존 문구를 그대로 쓴다(빈 묶음으로 부르는
    # 기존 호출자의 출력이 달라지지 않도록).
    if updates <= 0:
        return (
            f"신규 게시물 {total}건",
            f"{src_count}개 기관에서 새로 등록되었습니다",
        )
    if total > 0:
        return (
            f"신규 게시물 {total}건 · {_UPDATE_HEADING} {updates}건",
            f"{src_count}개 기관에서 새로 등록되었습니다",
        )
    return f"{_UPDATE_HEADING} {updates}건", _UPDATE_NOTICE


def build_html(
    posts_by_source: dict[str, list[Post]],
    detail_updates_by_source: dict[str, list[Post]] | None = None,
) -> str:
    """다이제스트 HTML.

    detail_updates_by_source 는 **이미 알린** 의안의 제안이유를 나중에 확보해 보내는
    '상세 업데이트' 묶음이다. 생략하면(기본) 기존과 완전히 같은 출력이 나온다.
    """
    updates = detail_updates_by_source or {}
    total = sum(len(v) for v in posts_by_source.values())
    updates_total = sum(len(v) for v in updates.values())
    # 게시판 수가 아니라 '기관' 수를 센다(같은 기관의 게시판 여러 개 = 1개 기관).
    agencies = {_agency(name) for name, posts in posts_by_source.items() if posts}
    src_count = len(agencies)
    head_title, head_sub = _header_lines(total, updates_total, src_count)

    parts = [
        f"<div style=\"margin:0;padding:20px 16px;background:#f1f5f9;font-family:{_FONT}\">",
        "<table role='presentation' width='100%' cellpadding='0' "
        "cellspacing='0' style='max-width:900px;border-collapse:collapse'>",
        # ── 헤더
        "<tr><td style='padding:22px 24px;background:#0f172a;border-radius:10px 10px 0 0'>"
        "<div style='font-size:11px;letter-spacing:1.6px;color:#94a3b8;"
        "font-weight:600'>LAW RADER KR</div>"
        "<div style='margin:6px 0 0;font-size:20px;font-weight:700;color:#ffffff'>"
        f"{_esc(head_title)}</div>"
        "<div style='margin:4px 0 0;font-size:13px;color:#cbd5e1'>"
        f"{_esc(head_sub)}</div>"
        "</td></tr>",
        "<tr><td style='padding:18px 16px 6px;background:#f8fafc'>",
    ]

    parts.extend(_source_sections(posts_by_source))

    if updates_total > 0:
        # ── 상세 업데이트 섹션. 신규 섹션과 시각적으로 분리하고, 무엇인지 한 줄로
        #    설명한다(신규 발송이 아니라는 것이 카드만 봐서는 드러나지 않는다).
        parts.append(
            "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' "
            "style='border-collapse:collapse;margin:18px 0 8px'><tr>"
            "<td style='padding:10px 12px;background:#fffbeb;border:1px solid #fde68a;"
            "border-radius:6px'>"
            "<div style='font-size:13px;font-weight:700;color:#b45309'>"
            f"{_esc(_UPDATE_HEADING)} {updates_total}건</div>"
            "<div style='margin:4px 0 0;font-size:12px;line-height:1.6;color:#92400e'>"
            f"{_esc(_UPDATE_NOTICE)}</div>"
            "</td></tr></table>"
        )
        parts.extend(_source_sections(updates))

    parts.append("</td></tr>")
    # ── 푸터. AI 요약이 실린 메일에만 요약 관련 유의사항을 덧붙인다.
    ai_note = f"{_esc(_AI_NOTICE)}<br>" if _has_summary(posts_by_source, updates) else ""
    parts.append(
        "<tr><td style='padding:16px 24px 22px;background:#f8fafc;"
        "border-radius:0 0 10px 10px;border-top:1px solid #e2e8f0'>"
        "<div style='font-size:11px;line-height:1.7;color:#94a3b8'>"
        "이 메일은 금융위원회·금융감독원·금융규제포털·의안정보시스템의 신규 게시물을 "
        f"자동 수집해 발송합니다.<br>{ai_note}"
        "첨부파일은 원문 그대로이며, 용량이 큰 파일은 링크로만 "
        "제공됩니다.<br>RADER stands for Regulatory Alert Detection & Email Reporter"
        "</div></td></tr>"
    )
    parts.append("</table></div>")
    return "".join(parts)


def _text_sections(posts_by_source: dict[str, list[Post]]) -> list[str]:
    """소스별 text/plain 블록. 신규와 상세 업데이트가 같은 렌더링을 공유한다."""
    lines: list[str] = []
    for source_name, posts in posts_by_source.items():
        if not posts:
            continue
        lines.append(f"\n[{source_name}] ({len(posts)}건)")
        for p in posts:
            lines.append(f"  - {p.title}  {p.date}".rstrip())
            if p.details:
                # 원문 그대로의 항목 — AI 요약과 구분되도록 다른 표제를 쓴다.
                lines.append(f"    [{_DETAILS_LABEL}]")
                for label, value in p.details:
                    lines.append(f"      {label}: {value}")
            elif p.summary:
                # text/plain 파트만 보는 수신자도 이 문장이 AI 생성물임을 알 수 있어야
                # 한다(원문 발췌와 혼동 금지). 하단 유의사항도 함께 붙는다.
                lines.append(f"    [{_summary_label(p)}]")
                for s in p.summary:
                    lines.append(f"      · {s}")
            elif p.body:
                lines.append(f"    [{_body_label(p)}]")
                if p.source_key == ASSEMBLY_SOURCE_KEY:
                    lines.extend(f"      {line}" for line in _assembly_excerpt(p))
                else:
                    lines.append(f"      {build_fallback_snippet(p.body, p.title)}")
            elif _is_pending(p):
                lines.append(f"    [{_PENDING_LABEL}]")
                lines.append(f"      {_PENDING_TEXT}")
            lines.append(f"    {p.url}")
            for a in p.attachments:
                lines.append(f"      첨부: {a.filename} ({a.url})")
    return lines


def build_text(
    posts_by_source: dict[str, list[Post]],
    detail_updates_by_source: dict[str, list[Post]] | None = None,
) -> str:
    """다이제스트 text/plain 파트. HTML 파트와 같은 의미를 전달해야 한다."""
    updates = detail_updates_by_source or {}
    lines = []
    total = sum(len(v) for v in posts_by_source.values())
    updates_total = sum(len(v) for v in updates.values())

    if updates_total <= 0:
        # 기존 호출(신규만)의 출력은 한 글자도 달라지지 않는다.
        lines.append(f"신규 게시물 {total}건\n")
    elif total > 0:
        lines.append(f"신규 게시물 {total}건")
        lines.append(f"{_UPDATE_HEADING} {updates_total}건\n")
    else:
        # 업데이트만 있는 메일을 '신규 게시물 0건'으로 시작하지 않는다.
        lines.append(f"{_UPDATE_HEADING} {updates_total}건")
        lines.append(f"{_UPDATE_NOTICE}\n")

    lines.extend(_text_sections(posts_by_source))

    if updates_total > 0 and total > 0:
        lines.append(f"\n=== {_UPDATE_HEADING} ({updates_total}건) ===")
        lines.append(_UPDATE_NOTICE)
    lines.extend(_text_sections(updates))

    if _has_summary(posts_by_source, updates):
        lines.append(f"\n※ {_AI_NOTICE}")
    return "\n".join(lines)


def missing_email_settings(cfg: EmailConfig) -> list[str]:
    """발송에 반드시 필요한데 비어 있는 설정 이름들. 비었으면 발송 가능."""
    missing = []
    if not cfg.smtp_user:
        missing.append("SMTP_USER")
    if not cfg.smtp_password:
        missing.append("SMTP_PASSWORD")
    if not cfg.recipients:
        missing.append("MAIL_TO(또는 config.yaml 의 email.recipients)")
    return missing


def verify_smtp_login(cfg: EmailConfig) -> None:
    """실제 발송 전에 SMTP 연결·인증만 확인한다(실패 시 예외).

    설정값이 '채워져 있는지'만 보는 missing_email_settings 로는 앱 비밀번호 폐기,
    호스트 도달 불가, 포트/TLS 오설정을 잡을 수 없다. 그대로 두면 요약을 다 돌린
    뒤 발송에서 실패하고, 실패는 신규를 seen 으로 확정하지 않으므로 매 실행이 같은
    글을 다시 요약해 무료 할당량을 계속 태운다. 그래서 요약 전에 한 번 로그인해 본다.

    연결을 열어둔 채 요약(최대 budget_sec)을 돌리면 서버가 유휴 연결을 끊을 수
    있으므로, 확인 후 바로 닫고 발송 때 새로 연결한다(핸드셰이크 1회 추가는 무시할
    수준이고, 유휴 끊김으로 발송이 실패하는 것보다 안전하다).
    """
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as server:
        server.starttls()
        server.login(cfg.smtp_user, cfg.smtp_password)


def build_subject(
    cfg: EmailConfig,
    posts_by_source: dict[str, list[Post]],
    detail_updates_by_source: dict[str, list[Post]] | None = None,
) -> str:
    """메일 제목. 신규만 있을 때는 기존 형식을 그대로 유지한다.

    상세 업데이트가 섞이면 제목만 보고도 두 종류가 들어 있음을 알 수 있어야 하고,
    업데이트만 있을 때는 '신규'라는 단어가 나오면 안 된다.
    """
    updates = detail_updates_by_source or {}
    total = sum(len(v) for v in posts_by_source.values())
    updates_total = sum(len(v) for v in updates.values())

    if total == 0:
        return f"{cfg.subject_prefix} {_UPDATE_HEADING} {updates_total}건"

    # 제목: 가장 많은 소스명 + 총 건수
    top_source = max(posts_by_source.items(), key=lambda kv: len(kv[1]))[0]
    others = sum(1 for v in posts_by_source.values() if v) - 1
    subject = f"{cfg.subject_prefix} 신규 {total}건 · {top_source}"
    if others > 0:
        subject += f" 외 {others}개 소스"
    if updates_total > 0:
        subject = (
            f"{cfg.subject_prefix} 신규 {total}건 · "
            f"{_UPDATE_HEADING} {updates_total}건"
        )
    return subject


def send_digest(
    cfg: EmailConfig,
    posts_by_source: dict[str, list[Post]],
    detail_updates_by_source: dict[str, list[Post]] | None = None,
) -> None:
    """한 실행의 결과를 다이제스트 **한 통**으로 보낸다.

    detail_updates_by_source 는 이미 알린 의안의 제안이유를 나중에 확보한 '상세
    업데이트'다. 신규와 함께 있으면 한 메일 안에 두 섹션으로 싣는다 — 실행당 메일
    한 통이라는 기존 원칙을 지키기 위해서다.
    """
    updates = detail_updates_by_source or {}
    total = sum(len(v) for v in posts_by_source.values())
    updates_total = sum(len(v) for v in updates.values())
    if total == 0 and updates_total == 0:
        log.info("신규 없음 — 메일 발송 생략")
        return

    missing = missing_email_settings(cfg)
    if missing:
        raise RuntimeError(
            f"SMTP 설정이 비어 있습니다: {', '.join(missing)}. "
            "환경변수/Secrets 와 config.yaml 을 확인하세요."
        )

    subject = build_subject(cfg, posts_by_source, updates)

    msg = EmailMessage()
    msg["Subject"] = subject
    # 발신자: 목록에는 표시이름(from_name)만, 클릭 시 "LAW RADER <발신주소>" 로 노출
    msg["From"] = formataddr((cfg.from_name, cfg.mail_from))
    # 수신자 숨김: To 헤더에는 발신주소만 표시하고, 실제 수신자는 봉투(envelope)로만
    # 전달한다. 이렇게 하면 여러 명에게 보내도 서로의 주소가 보이지 않는다(BCC 효과).
    msg["To"] = formataddr((cfg.from_name, cfg.mail_from))
    msg.set_content(build_text(posts_by_source, updates))
    msg.add_alternative(build_html(posts_by_source, updates), subtype="html")

    # 첨부 (용량 상한 내에서만)
    budget = cfg.max_attach_bytes
    for group in (posts_by_source, updates):
        for posts in group.values():
            for p in posts:
                for a in p.attachments:
                    if a.data and len(a.data) <= budget:
                        maintype, _, subtype = _guess_mime(a.filename)
                        msg.add_attachment(
                            a.data,
                            maintype=maintype,
                            subtype=subtype,
                            filename=a.filename,
                        )
                        budget -= len(a.data)

    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=60) as server:
        server.starttls()
        server.login(cfg.smtp_user, cfg.smtp_password)
        # 실제 수신자는 to_addrs(봉투)로만 전달 → To 헤더에 노출되지 않아 서로 안 보임.
        # send_message 는 '일부' 수신자만 거부되면 예외 대신 거부목록 dict 를 반환한다
        # (전원 거부 시에는 SMTPRecipientsRefused 예외). 반환값을 무시하면 거부된
        # 수신자가 알림을 못 받는데도 state 가 저장돼 재시도되지 않으므로 예외로 승격한다.
        refused = server.send_message(msg, to_addrs=cfg.recipients)

    if refused:
        # 일부 거부 → 실패로 취급해 state 저장을 막고 다음 실행에 재시도한다.
        # (성공한 수신자에게는 재발송되어 중복될 수 있으나, 알림 누락보다 낫다)
        raise RuntimeError(f"일부 수신자에게 발송 실패(재시도 대상): {refused}")

    log.info(
        "메일 발송 완료 → %s (신규 %d건 / %s %d건)",
        ", ".join(cfg.recipients),
        total,
        _UPDATE_HEADING,
        updates_total,
    )


def _guess_mime(filename: str) -> tuple[str, str, str]:
    import mimetypes

    mtype, _ = mimetypes.guess_type(filename)
    if not mtype:
        return "application", "/", "octet-stream"
    maintype, subtype = mtype.split("/", 1)
    return maintype, "/", subtype
