"""이미 발송한 게시글 ID + 상세 재조회 대기열 저장소.

state/seen.json 구조:
{
  "<source_key>": {
    "seen": ["id1", "id2", ...],   # 최근 것부터, MAX_PER_SOURCE 개까지 유지
    "baselined": true,              # 최초 1회 기준선 수립 완료 여부
    "pending_detail": {             # (선택) 상세를 아직 확보하지 못한 항목의 재조회 큐
      "<post_id>": {
        "title": "...", "url": "...", "date": "...",
        "status": "pending", "attempts": 1,
        "first_seen_at": "...", "last_attempt_at": "...", "note": "..."
      }
    }
  },
  ...
}

- 특정 소스가 처음 등장(baselined 없음)하면: 현재 목록을 모두 seen 으로 기록하되
  메일은 보내지 않는다(= 백로그 전체를 첫 실행에 쏟아내지 않기 위한 기준선 수립).
- 이후 실행부터 seen 에 없는 항목만 '신규'로 판정한다.

seen 과 pending_detail 은 **다른 질문에 답한다.**
  seen           = "이 의안의 존재를 사용자에게 이미 알렸다"
  pending_detail = "그런데 제안이유 및 주요내용은 아직 확보하지 못했다"
둘을 한 상태로 묶으면(= 예전 동작) 최초 알림이 나간 순간 그 의안은 영원히 신규가
아니게 되어, 나중에 제안이유가 등록돼도 다시 조회할 기회가 사라진다.

pending_detail 은 **선택 키**다. 이 키가 없는 기존 seen.json 도 migration 없이 그대로
읽히며 빈 큐로 취급된다. 항목에 우리가 모르는 필드가 있어도 지우지 않고 보존한다
(앞으로 늘어날 진단 필드를 이전 버전이 지워버리는 사고를 막는다).

큐에는 상세 재조회에 필요한 최소 스냅샷(title/url/date)만 담는다. 본문·첨부·요약은
넣지 않는다 — state 파일은 매 실행 커밋되는 저장소 파일이고, 알림에 필요한 것은
'다시 조회할 주소'와 '복구 알림에 쓸 원래 제목/날짜'뿐이다.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

# 소스별 seen ID 보관 상한(짧은 문자열이라 용량 부담은 작다).
MAX_PER_SOURCE = 5000

# 상세 재조회 큐가 들어가는 source entry 키.
PENDING_DETAIL_KEY = "pending_detail"


def utcnow_iso() -> str:
    """저장용 UTC 타임스탬프(초 단위, 'Z' 접미).

    테스트가 시계를 주입할 수 있도록 상태 변경 API 는 모두 now 인자를 받는다
    (실제 sleep 으로 시간을 흘려보내는 테스트를 쓰지 않기 위함).
    """
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def parse_ts(value) -> float | None:
    """저장된 ISO-8601 문자열 → epoch 초. 읽을 수 없으면 None.

    None 은 '시각을 알 수 없음'이며, 재조회 판정에서는 **즉시 due** 로 다룬다
    (판독 불가를 이유로 재조회를 영구히 미루면 누락 방지라는 목적에 반한다).
    """
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


class State:
    def __init__(self, path: str | Path = "state/seen.json"):
        self.path = Path(path)
        self._data: dict[str, dict] = {}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                loaded = {}
            # 최상위가 dict 가 아니면(손상) 빈 상태로 시작한다 — 아래 접근이
            # 전부 dict 를 전제하므로 여기서 한 번만 방어한다.
            self._data = loaded if isinstance(loaded, dict) else {}

    # --- seen / baseline (기존 계약 그대로) ---
    def is_baselined(self, source_key: str) -> bool:
        return self._entry_ro(source_key).get("baselined", False)

    def seen_ids(self, source_key: str) -> set[str]:
        seen = self._entry_ro(source_key).get("seen", [])
        return set(seen) if isinstance(seen, list) else set()

    def is_new(self, source_key: str, post_id: str) -> bool:
        return post_id not in self.seen_ids(source_key)

    def mark_seen(self, source_key: str, post_ids: list[str], *, baselined: bool = True) -> None:
        entry = self._entry(source_key)
        existing = entry.get("seen")
        if not isinstance(existing, list):
            existing = []
        # 새 id 를 앞쪽에 추가하고 중복 제거, 상한 유지
        merged = list(dict.fromkeys(list(post_ids) + existing))
        entry["seen"] = merged[:MAX_PER_SOURCE]
        if baselined:
            entry["baselined"] = True

    # --- 상세 재조회 큐 ---
    def pending_detail(self, source_key: str) -> dict[str, dict]:
        """소스의 재조회 큐 스냅샷(복사본).

        키가 없거나 형태가 깨졌으면 빈 dict 를 돌려준다 — 큐 하나가 망가졌다고
        State 로드 전체(=신규 판별 기준선)를 잃으면 그 편이 훨씬 큰 사고다.
        """
        raw = self._entry_ro(source_key).get(PENDING_DETAIL_KEY)
        if not isinstance(raw, dict):
            return {}
        return {
            k: dict(v)
            for k, v in raw.items()
            if isinstance(k, str) and k and isinstance(v, dict)
        }

    def queue_detail(
        self,
        source_key: str,
        post_id: str,
        *,
        title: str = "",
        url: str = "",
        date: str = "",
        status: str = "",
        note: str = "",
        now: str | None = None,
    ) -> None:
        """최초 알림을 보낸 뒤 상세가 미확보인 항목을 큐에 등록한다.

        이미 큐에 있으면 스냅샷과 상태만 갱신하고 first_seen_at 은 유지한다
        (얼마나 오래 미확보 상태인지가 운영 판단의 근거이므로 덮어쓰지 않는다).

        attempts 를 1 로 시작하는 이유: 이 항목은 이번 실행에서 이미 한 번 상세를
        시도했고(그 결과가 PENDING/ERROR/UNKNOWN 이라 큐에 들어온 것) last_attempt_at
        도 그 시도 시각이다. 0 으로 두면 방금 두드린 사이트를 다음 실행에서 곧바로
        다시 두드리게 된다.
        """
        ts = now or utcnow_iso()
        queue = self._queue(source_key)
        entry = queue.get(post_id)
        if not isinstance(entry, dict):
            entry = {"first_seen_at": ts, "attempts": 0}
            queue[post_id] = entry
        entry.setdefault("first_seen_at", ts)
        entry["title"] = title
        entry["url"] = url
        entry["date"] = date
        entry["status"] = status
        entry["note"] = note
        entry["attempts"] = _as_int(entry.get("attempts")) + 1
        entry["last_attempt_at"] = ts

    def record_detail_attempt(
        self,
        source_key: str,
        post_id: str,
        *,
        status: str,
        note: str = "",
        now: str | None = None,
    ) -> None:
        """기존 큐 항목의 재시도 결과(진단 메타데이터)를 기록한다.

        큐에 없는 항목은 만들지 않는다 — 재시도는 큐에 있는 것에만 일어나고,
        여기서 새로 만들면 '알림을 보낸 적 없는 의안'이 큐에 생길 수 있다.
        """
        queue = self._queue(source_key)
        entry = queue.get(post_id)
        if not isinstance(entry, dict):
            return
        entry["status"] = status
        entry["note"] = note
        entry["attempts"] = _as_int(entry.get("attempts")) + 1
        entry["last_attempt_at"] = now or utcnow_iso()

    def unqueue_detail(self, source_key: str, post_id: str) -> None:
        """상세를 사용자에게 전달 완료 → 큐에서 제거."""
        entry = self._data.get(source_key)
        if not isinstance(entry, dict):
            return
        queue = entry.get(PENDING_DETAIL_KEY)
        if isinstance(queue, dict):
            queue.pop(post_id, None)
            if not queue:
                # 빈 dict 는 남겨도 무해하지만, 기존 파일 모양(키 없음)으로 되돌려
                # 이 기능을 쓰지 않는 소스의 entry 가 불필요하게 커지지 않게 한다.
                entry.pop(PENDING_DETAIL_KEY, None)

    # --- 내부 ---
    def _entry_ro(self, source_key: str) -> dict:
        entry = self._data.get(source_key)
        return entry if isinstance(entry, dict) else {}

    def _entry(self, source_key: str) -> dict:
        entry = self._data.get(source_key)
        if not isinstance(entry, dict):
            entry = {"seen": [], "baselined": False}
            self._data[source_key] = entry
        entry.setdefault("seen", [])
        entry.setdefault("baselined", False)
        return entry

    def _queue(self, source_key: str) -> dict:
        entry = self._entry(source_key)
        queue = entry.get(PENDING_DETAIL_KEY)
        if not isinstance(queue, dict):
            queue = {}
            entry[PENDING_DETAIL_KEY] = queue
        return queue

    def save(self) -> None:
        # 원자적 저장: 임시파일에 먼저 쓰고 flush/fsync 후 os.replace 로 교체한다.
        # (도중 취소·오류로 대상 파일이 잘린 JSON 으로 남아 다음 실행이 전체 소스를
        #  재기준선 잡는 일을 막는다)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(self._data, ensure_ascii=False, indent=2)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)


def _as_int(value) -> int:
    """attempts 처럼 숫자여야 하는 필드를 안전하게 읽는다(깨져 있으면 0)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
