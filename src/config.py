"""config.yaml + 환경변수 로딩."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class EmailConfig:
    recipients: list[str]
    from_name: str
    subject_prefix: str
    max_attach_mb: int
    # 아래는 환경변수에서 주입 (민감정보)
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    mail_from: str

    @property
    def max_attach_bytes(self) -> int:
        return self.max_attach_mb * 1024 * 1024


@dataclass
class FetchConfig:
    delay_sec: float
    timeout_sec: float
    list_limit: int


@dataclass
class SourceConfig:
    key: str
    name: str
    type: str
    list_url: str
    enabled: bool = True
    extra: dict = field(default_factory=dict)


@dataclass
class Config:
    email: EmailConfig
    fetch: FetchConfig
    sources: list[SourceConfig]


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def load_config(path: str | Path = "config.yaml") -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))

    em = raw.get("email", {})
    # 수신자: MAIL_TO 환경변수(쉼표/세미콜론 구분)가 있으면 그것을 우선 사용하고,
    # 없으면 config.yaml 의 recipients 를 쓴다.
    mail_to_env = _env("MAIL_TO")
    if mail_to_env:
        recipients = [x.strip() for x in re.split(r"[,;]", mail_to_env) if x.strip()]
    else:
        recipients = em.get("recipients", [])

    email = EmailConfig(
        recipients=recipients,
        from_name=em.get("from_name", "LAW RADAR KR"),
        subject_prefix=em.get("subject_prefix", "[LAW RADAR]"),
        max_attach_mb=int(em.get("max_attach_mb", 15)),
        smtp_host=_env("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(_env("SMTP_PORT", "587")),
        smtp_user=_env("SMTP_USER"),
        smtp_password=_env("SMTP_PASSWORD"),
        # MAIL_FROM 미지정 시 SMTP_USER 를 발신주소로 사용
        mail_from=_env("MAIL_FROM") or _env("SMTP_USER"),
    )

    fe = raw.get("fetch", {})
    fetch = FetchConfig(
        delay_sec=float(fe.get("delay_sec", 1.0)),
        timeout_sec=float(fe.get("timeout_sec", 30.0)),
        list_limit=int(fe.get("list_limit", 30)),
    )

    sources = []
    for s in raw.get("sources", []):
        known = {"key", "name", "type", "list_url", "enabled"}
        sources.append(
            SourceConfig(
                key=s["key"],
                name=s["name"],
                type=s["type"],
                list_url=s["list_url"],
                enabled=s.get("enabled", True),
                extra={k: v for k, v in s.items() if k not in known},
            )
        )

    return Config(email=email, fetch=fetch, sources=sources)
