"""State 의 pending_detail(상세 재조회 큐) 계약.

지키는 것:
  - 기존 seen.json 을 migration 없이 그대로 읽는다(pending_detail 없으면 빈 큐).
  - 기존 seen/baselined/알 수 없는 필드를 깨뜨리거나 지우지 않는다.
  - 큐 항목 하나가 깨져 있어도 State 로드(=신규 판별 기준선) 전체를 잃지 않는다.
  - 원자적 저장(temp → flush → fsync → os.replace)은 그대로다.
  - 시각 계산은 주입된 now 로만 한다(실제 sleep 을 쓰지 않는다).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.state import PENDING_DETAIL_KEY, State, parse_ts, utcnow_iso

KEY = "assembly_bill"
T0 = "2026-08-24T09:00:00Z"
T1 = "2026-08-24T10:00:00Z"


def _write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# --- 1. 기존 seen.json 로드 → pending_detail 은 빈 큐 ---


def test_existing_seen_json_loads_without_migration(tmp_path):
    path = tmp_path / "seen.json"
    _write(path, {KEY: {"seen": ["a", "b"], "baselined": True}})

    st = State(path)
    assert st.is_baselined(KEY)
    assert st.seen_ids(KEY) == {"a", "b"}
    # pending_detail 키가 없어도 예외 없이 빈 큐로 취급되어야 한다.
    assert st.pending_detail(KEY) == {}


def test_unknown_source_has_empty_queue(tmp_path):
    assert State(tmp_path / "seen.json").pending_detail("nope") == {}


# --- 2. round trip ---


def test_pending_detail_round_trip(tmp_path):
    path = tmp_path / "seen.json"
    st = State(path)
    st.mark_seen(KEY, ["PRC_1"])
    st.queue_detail(
        KEY,
        "PRC_1",
        title="법률안 (홍길동의원)",
        url="https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId=PRC_1",
        date="2026-08-24",
        status="pending",
        note="제안이유 미등록",
        now=T0,
    )
    st.save()

    again = State(path)
    queue = again.pending_detail(KEY)
    assert set(queue) == {"PRC_1"}
    entry = queue["PRC_1"]
    assert entry["title"] == "법률안 (홍길동의원)"
    assert entry["url"].endswith("billId=PRC_1")
    assert entry["date"] == "2026-08-24"
    assert entry["status"] == "pending"
    assert entry["note"] == "제안이유 미등록"
    assert entry["attempts"] == 1          # 큐 등록 시점에 이미 1회 시도한 상태다
    assert entry["first_seen_at"] == T0
    assert entry["last_attempt_at"] == T0
    # seen/baselined 는 그대로여야 한다.
    assert again.seen_ids(KEY) == {"PRC_1"}


def test_pending_detail_returns_a_copy(tmp_path):
    st = State(tmp_path / "seen.json")
    st.queue_detail(KEY, "PRC_1", status="pending", now=T0)
    snapshot = st.pending_detail(KEY)
    snapshot["PRC_1"]["status"] = "tampered"
    snapshot["PRC_9"] = {}
    assert st.pending_detail(KEY)["PRC_1"]["status"] == "pending"
    assert "PRC_9" not in st.pending_detail(KEY)


# --- 3. 알 수 없는 기존 필드 보존 ---


def test_unknown_source_fields_are_preserved(tmp_path):
    path = tmp_path / "seen.json"
    _write(
        path,
        {
            KEY: {
                "seen": ["a"],
                "baselined": True,
                "future_field": {"x": 1},
                PENDING_DETAIL_KEY: {
                    "PRC_1": {"status": "pending", "custom_diag": "keep me"}
                },
            },
            "other_source": {"seen": ["z"], "baselined": True},
        },
    )
    st = State(path)
    st.mark_seen(KEY, ["b"])
    st.record_detail_attempt(KEY, "PRC_1", status="error", note="n", now=T1)
    st.save()

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw[KEY]["future_field"] == {"x": 1}
    assert raw["other_source"] == {"seen": ["z"], "baselined": True}
    # 항목 안의 모르는 필드도 지우지 않는다.
    assert raw[KEY][PENDING_DETAIL_KEY]["PRC_1"]["custom_diag"] == "keep me"
    assert raw[KEY][PENDING_DETAIL_KEY]["PRC_1"]["status"] == "error"


# --- 4. add / update / remove ---


def test_queue_update_keeps_first_seen_and_bumps_attempts(tmp_path):
    st = State(tmp_path / "seen.json")
    st.queue_detail(KEY, "PRC_1", title="t", status="pending", now=T0)
    st.record_detail_attempt(KEY, "PRC_1", status="error", note="네트워크", now=T1)

    entry = st.pending_detail(KEY)["PRC_1"]
    assert entry["first_seen_at"] == T0      # 언제부터 미확보인지는 덮어쓰지 않는다
    assert entry["last_attempt_at"] == T1
    assert entry["attempts"] == 2
    assert entry["status"] == "error"
    assert entry["note"] == "네트워크"
    assert entry["title"] == "t"             # 스냅샷은 유지


def test_record_attempt_ignores_unqueued_bill(tmp_path):
    """재시도 기록이 큐를 새로 만들면 안 된다(알린 적 없는 의안이 큐에 생긴다)."""
    st = State(tmp_path / "seen.json")
    st.record_detail_attempt(KEY, "PRC_UNKNOWN", status="pending", now=T0)
    assert st.pending_detail(KEY) == {}


def test_unqueue_removes_entry_and_key(tmp_path):
    path = tmp_path / "seen.json"
    st = State(path)
    st.mark_seen(KEY, ["PRC_1"])
    st.queue_detail(KEY, "PRC_1", status="pending", now=T0)
    st.queue_detail(KEY, "PRC_2", status="error", now=T0)
    st.unqueue_detail(KEY, "PRC_1")
    assert set(st.pending_detail(KEY)) == {"PRC_2"}

    st.unqueue_detail(KEY, "PRC_2")
    st.save()
    raw = json.loads(path.read_text(encoding="utf-8"))
    # 큐가 비면 키 자체를 지워 기존 파일 모양으로 돌아간다(seen 은 그대로).
    assert PENDING_DETAIL_KEY not in raw[KEY]
    assert raw[KEY]["seen"] == ["PRC_1"]


def test_unqueue_missing_is_noop(tmp_path):
    st = State(tmp_path / "seen.json")
    st.unqueue_detail(KEY, "PRC_X")            # 예외 없이 무시
    st.unqueue_detail("no_such_source", "PRC_X")
    assert st.pending_detail(KEY) == {}


# --- 5. atomic save 유지 ---


def test_save_is_atomic_and_leaves_no_temp(tmp_path):
    path = tmp_path / "nested" / "seen.json"
    st = State(path)
    st.mark_seen(KEY, ["a"])
    st.queue_detail(KEY, "PRC_1", status="pending", now=T0)
    st.save()

    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))[KEY]["seen"] == ["a"]
    assert not (path.parent / "seen.json.tmp").exists()
    assert sorted(p.name for p in path.parent.iterdir()) == ["seen.json"]


def test_save_uses_replace_not_truncate(tmp_path, monkeypatch):
    """os.replace 로 교체해야 중간에 죽어도 잘린 JSON 이 남지 않는다."""
    seen_calls = []
    real_replace = os.replace

    def _spy(src, dst):
        seen_calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _spy)
    path = tmp_path / "seen.json"
    st = State(path)
    st.mark_seen(KEY, ["a"])
    st.save()
    assert len(seen_calls) == 1 and seen_calls[0][1] == str(path)


# --- 6. 깨진 pending_detail 이 State 로드를 깨뜨리지 않는다 ---


def test_malformed_pending_detail_does_not_break_load(tmp_path):
    path = tmp_path / "seen.json"
    _write(
        path,
        {
            KEY: {
                "seen": ["a"],
                "baselined": True,
                PENDING_DETAIL_KEY: "이건 dict 가 아니다",
            }
        },
    )
    st = State(path)
    assert st.seen_ids(KEY) == {"a"}          # 기준선은 살아 있어야 한다
    assert st.is_baselined(KEY)
    assert st.pending_detail(KEY) == {}


def test_malformed_queue_entries_are_skipped_not_fatal(tmp_path):
    path = tmp_path / "seen.json"
    _write(
        path,
        {
            KEY: {
                "seen": ["a"],
                "baselined": True,
                PENDING_DETAIL_KEY: {
                    "PRC_OK": {"status": "pending", "attempts": "세 번"},
                    "PRC_BAD": ["리스트라니"],
                    "PRC_NULL": None,
                },
            }
        },
    )
    st = State(path)
    queue = st.pending_detail(KEY)
    assert set(queue) == {"PRC_OK"}
    # attempts 가 숫자가 아니어도 증가 기록이 예외 없이 된다.
    st.record_detail_attempt(KEY, "PRC_OK", status="pending", now=T1)
    assert st.pending_detail(KEY)["PRC_OK"]["attempts"] == 1


def test_corrupt_top_level_json_falls_back_to_empty(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    st = State(path)
    assert st.pending_detail(KEY) == {}
    assert st.seen_ids(KEY) == set()


def test_mark_seen_keeps_existing_queue(tmp_path):
    """seen 갱신이 큐를 날리지 않는다(둘은 독립된 상태다)."""
    path = tmp_path / "seen.json"
    _write(
        path,
        {KEY: {"seen": ["a"], "baselined": True,
               PENDING_DETAIL_KEY: {"PRC_1": {"status": "pending"}}}},
    )
    st = State(path)
    st.mark_seen(KEY, ["b", "c"])
    assert set(st.pending_detail(KEY)) == {"PRC_1"}
    assert st.seen_ids(KEY) == {"a", "b", "c"}


# --- 7. 타임스탬프 계산은 결정적 (실제 sleep 없음) ---


def test_parse_ts_is_deterministic():
    base = parse_ts("2026-08-24T09:00:00Z")
    later = parse_ts("2026-08-24T09:30:00Z")
    assert later - base == 1800.0
    # 오프셋 표기와 'Z' 표기는 같은 시각이다.
    assert parse_ts("2026-08-24T09:00:00+00:00") == base
    # 타임존이 없으면 UTC 로 본다.
    assert parse_ts("2026-08-24T09:00:00") == base


def test_parse_ts_returns_none_for_unreadable_values():
    for bad in (None, "", "   ", "어제", 12345, {"t": 1}, "2026-13-99T99:99:99Z"):
        assert parse_ts(bad) is None


def test_utcnow_iso_shape():
    now = utcnow_iso()
    assert now.endswith("Z") and "T" in now and "." not in now
    assert parse_ts(now) is not None
