"""공통 데이터 모델."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# 의안(계류의안) 소스의 key. 요약 경로(배치)와 메일 라벨이 이 값으로 갈리므로
# 여러 모듈이 같은 문자열을 각자 적어 두지 않도록 한 곳에 둔다(config.yaml 의 key 와 동일).
ASSEMBLY_SOURCE_KEY = "assembly_bill"


class ProposalContentStatus(str, Enum):
    """의안 '제안이유 및 주요내용'의 상태.

    **본문 없음과 수집 실패를 같은 것으로 취급하지 않는다.** 갓 접수된 의안은 원문이
    아직 공개되지 않아 본문이 비는 것이 정상이고, 그것을 장애로 세면 매 실행마다
    거짓 ERROR 가 쌓여 진짜 고장을 가린다. 반대로 셀렉터가 바뀌어 못 읽는 것을
    '아직 등록 안 됨'으로 보면 고장이 조용히 묻힌다.

    판정 규칙:
      UNKNOWN    아직 판정하지 않음(기본값). enrich 를 돌리지 않았거나 판정 전.
      AVAILABLE  제안이유 본문을 확보함(post.body 에 담김).
      PENDING    **정상 billInfo 응답인데 제안이유 섹션이 아직 없거나 비어 있음.**
                 = 아직 등록되지 않음. 응답 자체가 정상이라는 확인이 있어야만 쓴다.
      ERROR      네트워크·HTTP 오류, 마크업 변경, 정상 응답 구조 자체가 아님.

    라이브 확인(2026-08 Action #13): 제안이유가 등록되기 전에는 #prntsummary-sect 와
    pre#prntSummary 가 **아예 생성되지 않는다**. 그 응답도 HTTP 200 정상 심사정보
    HTML 이고 의안번호·제안일자·제안자는 정상적으로 들어 있다. 그래서 PENDING 은
    '정상 심사정보 뼈대가 온전한데 제안이유 섹션만 없거나 비어 있다'로 판정한다.

    반대로 섹션은 있는데 pre 만 없거나, 제안이유 표식은 있는데 예상 selector 가 없으면
    마크업이 바뀐 것이므로 ERROR 다 — 등록 대기로 넘기면 고장이 묻힌다.

    PENDING 은 '소관위 미확정', '문서 없음', '제안일이 오늘' 같은 정황으로 추정하지
    않는다 — 그런 조건은 등록 여부와 직접 관계가 없다.
    """

    UNKNOWN = "unknown"
    AVAILABLE = "available"
    PENDING = "pending"
    ERROR = "error"


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
    # 의안 전용: '제안이유 및 주요내용'의 상태. 다른 소스에서는 UNKNOWN 으로 남는다.
    proposal_status: ProposalContentStatus = ProposalContentStatus.UNKNOWN
    # 판정 근거(로그·디버그용). 메일에는 싣지 않는다.
    proposal_note: str = ""

    @property
    def uid(self) -> str:
        """소스 간 충돌 없는 전역 고유 키."""
        return f"{self.source_key}:{self.post_id}"
