"""신규 게시글을 요약한 다이제스트 이메일 발송 (SMTP)."""
from __future__ import annotations

import html
import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr

from .config import EmailConfig
from .models import Post

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


def _snippet(text: str, limit: int = 220) -> str:
    s = " ".join((text or "").split())
    return s[:limit] + " …" if len(s) > limit else s


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

    if p.body:
        rows.append(
            f"<div style='margin:8px 0 0;font-size:13px;line-height:1.6;color:#475569'>"
            f"{_esc(_snippet(p.body))}</div>"
        )

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


def build_html(posts_by_source: dict[str, list[Post]]) -> str:
    total = sum(len(v) for v in posts_by_source.values())
    src_count = sum(1 for v in posts_by_source.values() if v)

    parts = [
        f"<div style=\"margin:0;padding:24px 12px;background:#f1f5f9;font-family:{_FONT}\">",
        "<table role='presentation' align='center' width='100%' cellpadding='0' "
        "cellspacing='0' style='max-width:640px;margin:0 auto;border-collapse:collapse'>",
        # ── 헤더
        "<tr><td style='padding:22px 24px;background:#0f172a;border-radius:10px 10px 0 0'>"
        "<div style='font-size:11px;letter-spacing:1.6px;color:#94a3b8;"
        "font-weight:600'>LAW RADAR KR</div>"
        "<div style='margin:6px 0 0;font-size:20px;font-weight:700;color:#ffffff'>"
        f"신규 게시물 {total}건</div>"
        "<div style='margin:4px 0 0;font-size:13px;color:#cbd5e1'>"
        f"{src_count}개 기관에서 새로 등록되었습니다</div>"
        "</td></tr>",
        "<tr><td style='padding:18px 16px 6px;background:#f8fafc'>",
    ]

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

    parts.append("</td></tr>")
    # ── 푸터
    parts.append(
        "<tr><td style='padding:16px 24px 22px;background:#f8fafc;"
        "border-radius:0 0 10px 10px;border-top:1px solid #e2e8f0'>"
        "<div style='font-size:11px;line-height:1.7;color:#94a3b8'>"
        "이 메일은 금융위원회·금융감독원·금융규제포털·의안정보시스템의 신규 게시물을 "
        "자동 수집해 발송합니다.<br>첨부파일은 원문 그대로이며, 용량이 큰 파일은 링크로만 "
        "제공됩니다."
        "</div></td></tr>"
    )
    parts.append("</table></div>")
    return "".join(parts)


def build_text(posts_by_source: dict[str, list[Post]]) -> str:
    lines = []
    total = sum(len(v) for v in posts_by_source.values())
    lines.append(f"신규 게시물 {total}건\n")
    for source_name, posts in posts_by_source.items():
        if not posts:
            continue
        lines.append(f"\n[{source_name}] ({len(posts)}건)")
        for p in posts:
            lines.append(f"  - {p.title}  {p.date}".rstrip())
            lines.append(f"    {p.url}")
            for a in p.attachments:
                lines.append(f"      첨부: {a.filename} ({a.url})")
    return "\n".join(lines)


def send_digest(cfg: EmailConfig, posts_by_source: dict[str, list[Post]]) -> None:
    total = sum(len(v) for v in posts_by_source.values())
    if total == 0:
        log.info("신규 없음 — 메일 발송 생략")
        return

    if not (cfg.smtp_user and cfg.smtp_password and cfg.recipients):
        raise RuntimeError(
            "SMTP 설정이 비어 있습니다. SMTP_USER / SMTP_PASSWORD 환경변수와 "
            "config.yaml 의 recipients 를 확인하세요."
        )

    # 제목: 가장 많은 소스명 + 총 건수
    top_source = max(posts_by_source.items(), key=lambda kv: len(kv[1]))[0]
    others = sum(1 for v in posts_by_source.values() if v) - 1
    subject = f"{cfg.subject_prefix} 신규 {total}건 · {top_source}"
    if others > 0:
        subject += f" 외 {others}개 소스"

    msg = EmailMessage()
    msg["Subject"] = subject
    # 발신자: 목록에는 표시이름(from_name)만, 클릭 시 "LAW RADER <발신주소>" 로 노출
    msg["From"] = formataddr((cfg.from_name, cfg.mail_from))
    # 수신자 숨김: To 헤더에는 발신주소만 표시하고, 실제 수신자는 봉투(envelope)로만
    # 전달한다. 이렇게 하면 여러 명에게 보내도 서로의 주소가 보이지 않는다(BCC 효과).
    msg["To"] = formataddr((cfg.from_name, cfg.mail_from))
    msg.set_content(build_text(posts_by_source))
    msg.add_alternative(build_html(posts_by_source), subtype="html")

    # 첨부 (용량 상한 내에서만)
    budget = cfg.max_attach_bytes
    for posts in posts_by_source.values():
        for p in posts:
            for a in p.attachments:
                if a.data and len(a.data) <= budget:
                    maintype, _, subtype = _guess_mime(a.filename)
                    msg.add_attachment(
                        a.data, maintype=maintype, subtype=subtype, filename=a.filename
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

    log.info("메일 발송 완료 → %s (%d건)", ", ".join(cfg.recipients), total)


def _guess_mime(filename: str) -> tuple[str, str, str]:
    import mimetypes

    mtype, _ = mimetypes.guess_type(filename)
    if not mtype:
        return "application", "/", "octet-stream"
    maintype, subtype = mtype.split("/", 1)
    return maintype, "/", subtype
