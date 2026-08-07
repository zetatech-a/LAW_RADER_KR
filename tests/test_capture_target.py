"""캡처 대상(billId/URL) 확정과 검증 — 요청을 보내기 **전에** 실패해야 한다."""
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scripts.capture_assembly_fixture as cap
from scripts.capture_assembly_fixture import (
    CaptureTargetError,
    resolve_capture_target,
)

_TEMPLATE = "https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId={bill_id}"
_BID = "PRC_O2O6N0J7J3I0I1G5F0G5N0O0N1N3L2"
_URL = _TEMPLATE.format(bill_id=_BID)

SYNTH = Path(__file__).parent / "fixtures" / "synthetic"


def _fx(name):
    return (SYNTH / name).read_text(encoding="utf-8")


# --- billId 추출 ---


def test_url_only_extracts_bill_id():
    """--url 만 줘도 billId 를 뽑아야 한다.

    예전에는 "UNKNOWN" 이 되어 form#form 의 billId 와 불일치했고, 생산 코드가 요청을
    거부해 캡처가 항상 실패했다.
    """
    bill_id, url = resolve_capture_target("", _URL, _TEMPLATE)
    assert bill_id == _BID
    assert url == _URL


def test_url_with_extra_query_params():
    url = _URL + "&ageFrom=22&ageTo=22"
    bill_id, got = resolve_capture_target("", url, _TEMPLATE)
    assert bill_id == _BID
    assert got == url            # URL 은 그대로 쓴다


def test_bill_id_only_builds_url_from_template():
    bill_id, url = resolve_capture_target(_BID, "", _TEMPLATE)
    assert bill_id == _BID
    assert url == _URL


def test_matching_bill_id_and_url_is_accepted():
    bill_id, url = resolve_capture_target(_BID, _URL, _TEMPLATE)
    assert bill_id == _BID and url == _URL


# --- 거부해야 하는 입력 ---


def test_mismatched_bill_id_and_url_is_rejected():
    with pytest.raises(CaptureTargetError, match="다릅니다"):
        resolve_capture_target("PRC_OTHER", _URL, _TEMPLATE)


def test_url_without_bill_id_is_rejected():
    with pytest.raises(CaptureTargetError, match="billId 쿼리가 없음"):
        resolve_capture_target(
            "", "https://likms.assembly.go.kr/bill/bi/billDetailPage.do", _TEMPLATE
        )


def test_url_with_empty_bill_id_is_rejected():
    with pytest.raises(CaptureTargetError, match="비어 있음"):
        resolve_capture_target(
            "", "https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId=",
            _TEMPLATE,
        )


def test_url_with_duplicate_bill_id_is_rejected():
    with pytest.raises(CaptureTargetError, match="여러 개"):
        resolve_capture_target("", _URL + f"&billId={_BID}", _TEMPLATE)


def test_non_https_url_is_rejected():
    with pytest.raises(CaptureTargetError, match="HTTPS"):
        resolve_capture_target("", _URL.replace("https://", "http://"), _TEMPLATE)


@pytest.mark.parametrize(
    "host",
    ["evil.example.com", "likms.assembly.go.kr.evil.com", "assembly.go.kr"],
)
def test_foreign_host_is_rejected(host):
    url = _URL.replace("likms.assembly.go.kr", host)
    with pytest.raises(CaptureTargetError, match="호스트"):
        resolve_capture_target("", url, _TEMPLATE)


def test_neither_bill_id_nor_url_is_rejected():
    with pytest.raises(CaptureTargetError):
        resolve_capture_target("", "", _TEMPLATE)


# --- main() 이 요청 전에 멈추는지 ---


class _RecordingFetcher:
    """호출되면 기록만 하고 실패시킨다 — 요청이 0회여야 한다."""

    def __init__(self):
        self.calls = 0

    def get(self, *a, **k):
        self.calls += 1
        raise AssertionError("검증 실패인데 요청을 보냈다")

    def post(self, *a, **k):
        self.calls += 1
        raise AssertionError("검증 실패인데 요청을 보냈다")

    def text(self, resp):  # pragma: no cover
        raise AssertionError


def test_bad_target_makes_no_network_call(monkeypatch, tmp_path):
    rec = _RecordingFetcher()
    monkeypatch.setattr(cap, "Fetcher", lambda **k: rec)
    rc = cap.main(
        ["--bill-id", "PRC_OTHER", "--url", _URL,
         "--out", str(tmp_path), "--expect", "available"]
    )
    assert rc == cap.EXIT_BAD_TARGET
    assert rec.calls == 0            # 네트워크 요청 0회
    assert not list(tmp_path.iterdir())   # fixture 도 만들지 않는다


@pytest.mark.parametrize(
    "argv",
    [
        ["--url", "http://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId=PRC_A"],
        ["--url", "https://evil.example.com/bill?billId=PRC_A"],
        ["--url", "https://likms.assembly.go.kr/bill/bi/billDetailPage.do"],
    ],
)
def test_invalid_urls_make_no_network_call(monkeypatch, tmp_path, argv):
    rec = _RecordingFetcher()
    monkeypatch.setattr(cap, "Fetcher", lambda **k: rec)
    rc = cap.main(argv + ["--out", str(tmp_path), "--expect", "available"])
    assert rc == cap.EXIT_BAD_TARGET
    assert rec.calls == 0


# --- --expect 게이트 / 디렉터리 분리 ---


class _FakeResp:
    def __init__(self, url):
        self.url = url
        self.status_code = 200
        self.history = []


class _FakeFetcher:
    def __init__(self, detail, reply):
        self._detail, self._reply = detail, reply
        self.n = 0

    def get(self, url, referer=None, params=None, headers=None):
        self.n += 1
        return _FakeResp(_URL)

    def post(self, url, referer=None, data=None, headers=None):
        self.n += 1
        return _FakeResp(url)

    def text(self, resp):
        return self._detail if resp.url == _URL and self.n <= 1 else self._reply


def _capture(monkeypatch, out, expect, reply):
    detail = _fx("bill_detail_page.html").replace("PRC_SYNTH_0001", _BID)
    monkeypatch.setattr(cap, "Fetcher", lambda **k: _FakeFetcher(detail, reply))
    return cap.main(["--url", _URL, "--out", str(out), "--expect", expect])


def test_available_capture_succeeds_and_writes_available_dir(monkeypatch, tmp_path):
    rc = _capture(monkeypatch, tmp_path, "available", _fx("billinfo_available.html"))
    assert rc == cap.EXIT_OK
    d = tmp_path / "available"
    assert sorted(p.name for p in d.iterdir()) == [
        "billinfo.html", "detail.html", "meta.json"
    ]
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["expect"] == "available"
    assert meta["status"] == "available"
    assert meta["body_length"] >= 20


def test_pending_capture_succeeds_and_writes_pending_dir(monkeypatch, tmp_path):
    rc = _capture(monkeypatch, tmp_path, "pending", _fx("billinfo_pending.html"))
    assert rc == cap.EXIT_OK
    meta = json.loads((tmp_path / "pending" / "meta.json").read_text(encoding="utf-8"))
    assert meta["expect"] == "pending"
    assert meta["status"] == "pending"
    assert meta["body_length"] == 0


def test_pending_capture_does_not_overwrite_available_fixture(monkeypatch, tmp_path):
    """두 상태 fixture 는 서로 다른 디렉터리에 저장되어 덮어쓰지 않는다."""
    assert _capture(monkeypatch, tmp_path, "available", _fx("billinfo_available.html")) == 0
    before = (tmp_path / "available" / "billinfo.html").read_text(encoding="utf-8")

    assert _capture(monkeypatch, tmp_path, "pending", _fx("billinfo_pending.html")) == 0
    after = (tmp_path / "available" / "billinfo.html").read_text(encoding="utf-8")

    assert after == before                       # available 은 그대로
    assert (tmp_path / "pending" / "billinfo.html").exists()
    assert after != (tmp_path / "pending" / "billinfo.html").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "expect,reply_fixture",
    [
        ("available", "billinfo_pending.html"),          # 기대 available, 실제 pending
        ("pending", "billinfo_available.html"),          # 기대 pending, 실제 available
        ("available", "billinfo_malformed_shell.html"),  # 기대 available, 실제 error
        ("pending", "billinfo_malformed_shell.html"),    # 기대 pending, 실제 error
    ],
)
def test_status_mismatch_saves_artifacts_but_fails(
    monkeypatch, tmp_path, expect, reply_fixture
):
    rc = _capture(monkeypatch, tmp_path, expect, _fx(reply_fixture))
    assert rc == cap.EXIT_NO_SUMMARY          # 종료코드는 실패
    d = tmp_path / expect
    assert (d / "detail.html").exists()        # 그래도 아티팩트는 저장한다
    assert (d / "meta.json").exists()
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] != expect


def test_expect_is_required():
    with pytest.raises(SystemExit):
        cap.main(["--bill-id", _BID])


# --- 캡처된 fixture 로 fixture 테스트를 돌리면 skip 이 0 이어야 한다 ---


def test_captured_fixtures_leave_no_skips(monkeypatch, tmp_path):
    """capture mode 에서 두 상태를 다 캡처하면 fixture 테스트 skip 이 0 이 된다."""
    import subprocess

    assert _capture(monkeypatch, tmp_path, "available", _fx("billinfo_available.html")) == 0
    assert _capture(monkeypatch, tmp_path, "pending", _fx("billinfo_pending.html")) == 0

    env = dict(os.environ, ASSEMBLY_FIXTURE_DIR=str(tmp_path))
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_assembly_fixture.py", "-q"],
        cwd=str(Path(__file__).parent.parent),
        env=env,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "skipped" not in out.stdout, out.stdout
