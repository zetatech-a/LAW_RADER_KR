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


def build_html(posts_by_source: dict[str, list[Post]]) -> str:
    parts = [
        "<div style='font-family:Apple SD Gothic Neo,Malgun Gothic,sans-serif;"
        "font-size:14px;color:#222;line-height:1.6'>"
    ]
    total = sum(len(v) for v in posts_by_source.values())
    parts.append(f"<h2 style='margin:0 0 4px'>신규 게시물 {total}건</h2>")
    parts.append(
        "<p style='color:#666;margin:0 0 16px'>모니터링 대상 사이트에 새로 등록된 항목입니다.</p>"
    )

    for source_name, posts in posts_by_source.items():
        if not posts:
            continue
        parts.append(
            f"<h3 style='margin:20px 0 8px;padding-bottom:4px;"
            f"border-bottom:2px solid #1a4b8c;color:#1a4b8c'>"
            f"{_esc(source_name)} <span style='color:#888;font-weight:normal'>"
            f"({len(posts)}건)</span></h3>"
        )
        for p in posts:
            parts.append("<div style='margin:0 0 14px;padding:0 0 0 4px'>")
            parts.append(
                f"<a href='{_esc(p.url)}' style='font-weight:600;color:#1155cc;"
                f"text-decoration:none;font-size:15px'>{_esc(p.title)}</a>"
            )
            if p.date:
                parts.append(
                    f"<span style='color:#999;margin-left:8px;font-size:12px'>"
                    f"{_esc(p.date)}</span>"
                )
            if p.body:
                snippet = p.body.strip().replace("\n", " ")
                if len(snippet) > 400:
                    snippet = snippet[:400] + " …"
                parts.append(
                    f"<div style='color:#444;margin:4px 0'>{_esc(snippet)}</div>"
                )
            if p.attachments:
                links = []
                for a in p.attachments:
                    tag = f"📎 <a href='{_esc(a.url)}'>{_esc(a.filename)}</a>"
                    if a.data is None:
                        tag += " <span style='color:#999'>(첨부 실패·링크만)</span>"
                    links.append(tag)
                parts.append(
                    "<div style='color:#555;font-size:12px;margin:2px 0'>"
                    + " &nbsp; ".join(links)
                    + "</div>"
                )
            parts.append(
                f"<div style='font-size:12px;color:#999'>{_esc(p.url)}</div>"
            )
            parts.append("</div>")

    parts.append(
        "<hr style='border:none;border-top:1px solid #eee;margin:20px 0'>"
        "<p style='color:#aaa;font-size:11px'>LAW RADAR KR · 자동 발송 메일</p>"
    )
    parts.append("</div>")
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
        # 실제 수신자는 to_addrs(봉투)로만 전달 → To 헤더에 노출되지 않아 서로 안 보임
        server.send_message(msg, to_addrs=cfg.recipients)

    log.info("메일 발송 완료 → %s (%d건)", ", ".join(cfg.recipients), total)


def _guess_mime(filename: str) -> tuple[str, str, str]:
    import mimetypes

    mtype, _ = mimetypes.guess_type(filename)
    if not mtype:
        return "application", "/", "octet-stream"
    maintype, subtype = mtype.split("/", 1)
    return maintype, "/", subtype
