"""Python ↔ Cloudflare Worker 계약 교차 검증.

1) 정규화·HMAC 공통 벡터: cloudflare-subscription/test/fixtures/hmac_vectors.json 을 Python 과
   Worker(test/crypto.test.ts) 테스트가 함께 검사한다 — 어느 한쪽 구현이 바뀌면 둘 중 하나가 깨진다.
2) (선택) 로컬 Worker 실연동: LRK_WORKER_E2E_URL 이 있을 때만 실행한다. `wrangler dev` 로 띄운
   로컬 Worker + 로컬 D1 에 실제 HTTP 로 붙어 Python 의 요청·응답 검증을 그대로 통과하는지 본다.
   운영 Worker·운영 D1 에는 절대 붙이지 않는다(127.0.0.1 만 허용).
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import AgencyConfig, EmailConfig, SubscriptionConfig
from src.models import ASSEMBLY_SOURCE_KEY, Post
from src.subscription import (
    fetch_preferences,
    group_recipients,
    normalize_email,
    send_grouped,
    subscriber_hash,
)

ROOT = Path(__file__).resolve().parent.parent
VECTORS = json.loads(
    (ROOT / "cloudflare-subscription" / "test" / "fixtures" / "hmac_vectors.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: repr(c["input"]))
def test_python_matches_shared_vectors(case):
    if case["normalized"] is None:
        with pytest.raises(ValueError):
            normalize_email(case["input"])
    else:
        assert normalize_email(case["input"]) == case["normalized"]
        assert subscriber_hash(case["input"], VECTORS["key"]) == case["hash"]


# --------------------------------------------------------------------------
# 로컬 Worker 실연동(선택)
# --------------------------------------------------------------------------
E2E_URL = os.environ.get("LRK_WORKER_E2E_URL", "")
needs_worker = pytest.mark.skipif(not E2E_URL, reason="LRK_WORKER_E2E_URL 미설정 — 로컬 Worker 실연동 생략")

AGENCIES = (
    AgencyConfig("fsc", "금융위원회", ("fsc_press", "fsc_legislation")),
    AgencyConfig("fss", "금융감독원", ("fss_press",)),
    AgencyConfig("better", "금융규제·법령해석포털", ("better_reply",)),
    AgencyConfig("pipc", "개인정보보호위원회", ("pipc_notice", "pipc_press")),
    AgencyConfig("assembly", "의안정보시스템", (ASSEMBLY_SOURCE_KEY,)),
)


def _e2e_sub():
    host = urlparse(E2E_URL).hostname
    assert host in ("127.0.0.1", "localhost"), "실연동 테스트는 로컬 Worker 에만 붙는다"
    return SubscriptionConfig(
        enabled=True,
        api_url=E2E_URL,
        api_token=os.environ["LRK_WORKER_E2E_TOKEN"],
        subscriber_hash_key=os.environ["LRK_WORKER_E2E_HASH_KEY"],
        timeout_sec=10.0,
        agencies=AGENCIES,
    )


def _d1_local(sql: str) -> None:
    subprocess.run(
        ["npx", "wrangler", "d1", "execute", "law-rader-subscription", "--local", "--command", sql],
        cwd=ROOT / "cloudflare-subscription",
        check=True,
        capture_output=True,
        timeout=120,
    )


@needs_worker
def test_e2e_python_and_worker_agree_end_to_end():
    sub = _e2e_sub()
    emails = ["all@example.com", "asm@example.com", "fscasm@example.com", "paused@example.com"]
    ids = {e: subscriber_hash(e, sub.subscriber_hash_key) for e in emails}

    # 1) 최초 동기화: Python 검증기를 그대로 통과하고 전원 mode=all
    first = fetch_preferences(sub, list(ids.values()))
    assert {p.mode for p in first.values()} == {"all"}

    # 2) 설정 페이지가 저장했을 행(로컬 D1 에 직접) → 다시 동기화
    for email, mode, agencies in [
        ("asm@example.com", "selected", '["assembly"]'),
        ("fscasm@example.com", "selected", '["fsc","assembly"]'),
        ("paused@example.com", "selected", "[]"),
    ]:
        _d1_local(
            "INSERT INTO preferences (subscriber, mode, agencies, version, updated_at) "
            f"VALUES ('{ids[email]}', '{mode}', '{agencies}', 1, 0) "
            "ON CONFLICT (subscriber) DO UPDATE SET mode = excluded.mode, agencies = excluded.agencies"
        )
    prefs = fetch_preferences(sub, list(ids.values()))
    assert prefs[ids["all@example.com"]].mode == "all"
    assert prefs[ids["asm@example.com"]].agencies == ("assembly",)
    assert prefs[ids["fscasm@example.com"]].agencies == ("fsc", "assembly")
    assert prefs[ids["paused@example.com"]].agencies == ()

    # 3) Python 그룹화·필터·그룹 발송까지 실제 응답으로
    pairs = [(e, ids[e]) for e in emails]
    groups = group_recipients(pairs, prefs, AGENCIES)
    news = {
        "의안": [Post(ASSEMBLY_SOURCE_KEY, "의안", "B1", "법률안", "https://e/b1")],
        "금융위": [Post("fsc_press", "금융위", "F1", "보도", "https://e/f1")],
    }
    sent = []
    email_cfg = EmailConfig(
        recipients=emails, from_name="x", subject_prefix="[x]", max_attach_mb=1,
        smtp_host="h", smtp_port=1, smtp_user="u", smtp_password="p", mail_from="m",
    )
    send_grouped(lambda cfg, posts, **kw: sent.append((cfg.recipients, posts)), email_cfg, groups, news, {})
    by = {tuple(r): {p.post_id for ps in posts.values() for p in ps} for r, posts in sent}
    assert by[("all@example.com",)] == {"B1", "F1"}
    assert by[("asm@example.com",)] == {"B1"}
    assert by[("fscasm@example.com",)] == {"B1", "F1"}
    assert all("paused@example.com" not in r for r in by)

    # 4) MAIL_TO 에서 빠진 구독자는 비활성(설정 보존) — 다시 넣으면 복원
    fetch_preferences(sub, [ids[e] for e in emails if e != "asm@example.com"])
    back = fetch_preferences(sub, list(ids.values()))
    assert back[ids["asm@example.com"]].agencies == ("assembly",)
