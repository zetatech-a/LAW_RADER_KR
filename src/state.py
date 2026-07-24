"""이미 발송한 게시글 ID 저장소.

state/seen.json 구조:
{
  "<source_key>": {
    "seen": ["id1", "id2", ...],   # 최근 것부터, MAX_PER_SOURCE 개까지 유지
    "baselined": true               # 최초 1회 기준선 수립 완료 여부
  },
  ...
}

- 특정 소스가 처음 등장(baselined 없음)하면: 현재 목록을 모두 seen 으로 기록하되
  메일은 보내지 않는다(= 백로그 전체를 첫 실행에 쏟아내지 않기 위한 기준선 수립).
- 이후 실행부터 seen 에 없는 항목만 '신규'로 판정한다.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# 소스별 seen ID 보관 상한(짧은 문자열이라 용량 부담은 작다).
MAX_PER_SOURCE = 5000


class State:
    def __init__(self, path: str | Path = "state/seen.json"):
        self.path = Path(path)
        self._data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def is_baselined(self, source_key: str) -> bool:
        return self._data.get(source_key, {}).get("baselined", False)

    def seen_ids(self, source_key: str) -> set[str]:
        return set(self._data.get(source_key, {}).get("seen", []))

    def is_new(self, source_key: str, post_id: str) -> bool:
        return post_id not in self.seen_ids(source_key)

    def mark_seen(self, source_key: str, post_ids: list[str], *, baselined: bool = True) -> None:
        entry = self._data.setdefault(source_key, {"seen": [], "baselined": False})
        existing = entry["seen"]
        # 새 id 를 앞쪽에 추가하고 중복 제거, 상한 유지
        merged = list(dict.fromkeys(list(post_ids) + existing))
        entry["seen"] = merged[:MAX_PER_SOURCE]
        if baselined:
            entry["baselined"] = True

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
