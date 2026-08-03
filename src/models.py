"""공통 데이터 모델."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Attachment:
    """게시글 첨부파일."""

    filename: str
    url: str
    data: Optional[bytes] = None  # 다운로드 성공 시 채워짐

    @property
    def size(self) -> int:
        return len(self.data) if self.data else 0


@dataclass
class Post:
    """수집된 게시글 한 건."""

    source_key: str          # 소스 식별자 (config 의 key)
    source_name: str         # 사람이 읽는 소스 이름
    post_id: str             # 사이트 내 안정적인 고유 ID (신규 판별의 기준)
    title: str
    url: str                 # 상세 페이지 URL
    date: str = ""           # 게시일 문자열 (표시용)
    body: str = ""           # 상세 본문 텍스트 (enrich 단계에서 채움)
    # LLM 3줄 요약 (summarizer 단계에서 채움). 비어 있으면 메일에서 body 발췌로 대체.
    summary: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    # 상세 페이지에서 그대로 뽑은 (라벨, 값) 항목. 원문 그대로이며 AI 생성물이 아니다.
    # 리스트 순서가 곧 메일 표시 순서. 비어 있으면(기본) 기존 summary/body 경로를 탄다.
    details: list[tuple[str, str]] = field(default_factory=list)

    @property
    def uid(self) -> str:
        """소스 간 충돌 없는 전역 고유 키."""
        return f"{self.source_key}:{self.post_id}"
