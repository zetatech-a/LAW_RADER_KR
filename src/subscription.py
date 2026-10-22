"""구독자별 수신 기관 선택 — 구독 API 조회와 수신자 그룹별 필터링.

config.yaml 의 subscription.enabled 가 true 일 때만 쓰인다(기본 false = 기존 발송 그대로).

흐름(main.run):
  1) 발송 선점검 단계에서 resolve_recipient_groups() 로 구독 API 를 **한 번** 부른다.
     MAIL_TO 의 각 주소는 HMAC-SHA256 식별자로만 보내고 평문 주소는 보내지 않는다.
  2) 같은 실효 설정(같은 소스 집합)을 가진 수신자를 한 그룹으로 묶는다(그룹 = BCC 한 통).
  3) 발송 단계에서 send_grouped() 가 그룹마다 Post.source_key 로 신규·상세 업데이트를
     거르고, 보낼 것이 있는 그룹에만 기존 send_digest() 로 보낸다.

장애 정책(fail-closed):
  구독 API 가 응답하지 않거나 계약에 어긋난 데이터를 주면 SubscriptionError 를 던진다.
  호출자는 메일을 한 통도 보내지 않고 실패로 끝내야 한다 — MAIL_TO 전체 발송으로
  되돌아가지 않는다. 'API 장애'와 '구독자가 mode=all' 은 절대 같은 값으로 표현하지
  않는다(전자는 예외, 후자는 정상 응답).

개인정보:
  이 모듈의 로그·예외 메시지에는 이메일 주소, 구독자 식별자(HMAC), 토큰을 싣지 않는다.
  수신자 수·그룹 수·기관 ID(공개 식별자)만 남긴다.

API 계약(Phase 2 Worker 가 지켜야 할 것)은 README '구독 기관 선택' 절 참고.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass, replace
from typing import Callable

import requests

from .config import AgencyConfig, EmailConfig, SubscriptionConfig
from .models import Post

log = logging.getLogger(__name__)

API_VERSION = 1
MODE_ALL = "all"
MODE_SELECTED = "selected"
_MODES = frozenset({MODE_ALL, MODE_SELECTED})

# HMAC-SHA256 hex(소문자 64자). Worker 도 같은 형식으로 저장·응답한다.
_SUBSCRIBER_RE = re.compile(r"^[0-9a-f]{64}$")
# HMAC 키 최소 길이(문자). 짧은 키는 주소 사전 대입으로 식별자를 되돌릴 여지를 키운다.
MIN_HASH_KEY_CHARS = 32
# 키 일치 확인용 고정 문구. key_check = HMAC(SUBSCRIBER_HASH_KEY, 이 문구) 를 함께 보내면 Worker 가
# 자기 키로 같은 값을 계산해 비교한다 — 두 곳에 등록한 키가 다르면 모든 식별자가 '새 구독자'로
# 보여 전원 mode=all 로 응답되는(사용자 선택이 조용히 무시되는) 사고를 409 로 막는다.
KEY_CHECK_MESSAGE = "lrk-subscriber-key-check-v1"
# 응답 본문 상한. 수신자 수 × 기관 수 규모면 충분하다 — 비정상 응답을 통째로 읽지 않는다.
_MAX_RESPONSE_BYTES = 1_000_000


class SubscriptionError(Exception):
    """구독 API 장애·계약 위반·설정 누락. 메시지에 개인정보·비밀을 넣지 않는다."""


@dataclass(frozen=True)
class Preference:
    """구독자 1명의 설정. selected + 빈 agencies = 전체 알림 일시중지."""

    mode: str
    agencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecipientGroup:
    """같은 실효 설정을 가진 수신자 묶음 — 메일 한 통(BCC)으로 보낸다."""

    sources: frozenset[str] | None  # None = 전체 소스(mode=all)
    agencies: tuple[str, ...]       # selected 일 때 기관 ID(카탈로그 순서). 진단 라벨용
    recipients: tuple[str, ...]

    @property
    def label(self) -> str:
        """로그용 라벨. 기관 ID 만 쓰고 수신자 정보는 싣지 않는다."""
        if self.sources is None:
            return "all"
        return "selected:" + ("+".join(self.agencies) if self.agencies else "일시중지")


# --------------------------------------------------------------------------
# 식별자
# --------------------------------------------------------------------------
def normalize_email(addr: str) -> str:
    """구독자 식별용 정규화: 앞뒤 공백 제거 + ASCII 소문자.

    Worker(JavaScript)와 결과가 바이트 단위로 같아야 하므로 ASCII 주소만 받는다
    (비ASCII 의 대소문자 변환은 언어마다 다르다). 실패 메시지에 주소를 넣지 않는다.
    """
    text = (addr or "").strip()
    local, sep, domain = text.partition("@")
    if (
        not sep
        or not local
        or not domain
        or "@" in domain
        or not text.isascii()
        or any(c.isspace() or ord(c) < 0x21 or ord(c) == 0x7F for c in text)
    ):
        raise ValueError("이메일 주소 형식이 올바르지 않습니다")
    return text.lower()


def subscriber_hash(addr: str, key: str) -> str:
    """HMAC-SHA256(key, normalize_email(addr)) 의 소문자 hex."""
    return hmac.new(
        key.encode("utf-8"), normalize_email(addr).encode("ascii"), hashlib.sha256
    ).hexdigest()


# --------------------------------------------------------------------------
# 설정 점검 · API 호출 · 응답 검증
# --------------------------------------------------------------------------
def missing_subscription_settings(sub: SubscriptionConfig, email: EmailConfig) -> list[str]:
    """구독 기능을 켰는데 비었거나 잘못된 설정 이름들(값은 싣지 않는다)."""
    missing = []
    if not sub.api_url.startswith("https://"):
        missing.append("SUBSCRIPTION_API_URL(https://)")
    if not sub.api_token:
        missing.append("SUBSCRIPTION_API_TOKEN")
    if len(sub.subscriber_hash_key) < MIN_HASH_KEY_CHARS:
        missing.append(f"SUBSCRIBER_HASH_KEY({MIN_HASH_KEY_CHARS}자 이상)")
    if not email.preferences_url.startswith("https://"):
        missing.append("SUBSCRIPTION_PAGE_URL(https://)")
    if not sub.agencies:
        missing.append("config.yaml subscription.agencies")
    return missing


def _unique_recipients(recipients: list[str], key: str) -> list[tuple[str, str]]:
    """(원래 주소, 식별자). 정규화가 같은 주소는 처음 것만 남긴다(같은 사람에게 두 통 금지)."""
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    invalid = 0
    for addr in recipients:
        try:
            sid = subscriber_hash(addr, key)
        except ValueError:
            invalid += 1
            continue
        if sid in seen:
            continue
        seen.add(sid)
        pairs.append((addr, sid))
    if invalid:
        raise SubscriptionError(f"MAIL_TO 에 형식이 올바르지 않은 주소가 {invalid}개 있습니다")
    dupes = len(recipients) - len(pairs)
    if dupes:
        log.warning("MAIL_TO 에 중복 주소 %d개 — 한 번만 발송합니다", dupes)
    if not pairs:
        # 빈 목록을 보내면 Worker 가 '전원 해지'로 오해할 수 있다 — 절대 보내지 않는다.
        raise SubscriptionError("구독 API 에 보낼 수신자가 없습니다")
    return pairs


def key_check(key: str) -> str:
    """HMAC-SHA256(key, KEY_CHECK_MESSAGE) 소문자 hex — 키 자체는 드러내지 않는다."""
    return hmac.new(key.encode("utf-8"), KEY_CHECK_MESSAGE.encode("ascii"), hashlib.sha256).hexdigest()


def build_request(sub: SubscriptionConfig, subscriber_ids: list[str]) -> dict:
    """POST /internal/sync-and-resolve 요청 본문.

    식별자는 정렬해서 보낸다(MAIL_TO 순서를 서버에 드러내지 않는다).
    """
    return {
        "api_version": API_VERSION,
        "key_check": key_check(sub.subscriber_hash_key),
        "subscribers": sorted(set(subscriber_ids)),
        "agencies": [{"id": a.id, "name": a.name} for a in sub.agencies],
    }


def parse_response(
    data, requested: set[str], agency_order: tuple[str, ...]
) -> dict[str, Preference]:
    """응답 검증. 계약에 조금이라도 어긋나면 SubscriptionError(부분 수용 없음).

    응답은 객체(dict)가 아니라 **목록**으로 받는다 — JSON 객체의 중복 키는 파서가 조용히
    덮어써 중복을 감지할 수 없기 때문이다.
    """
    if not isinstance(data, dict):
        raise SubscriptionError("구독 API 응답이 객체가 아닙니다")
    version = data.get("api_version")
    if type(version) is not int or version != API_VERSION:
        raise SubscriptionError("구독 API 버전이 일치하지 않습니다")
    items = data.get("subscribers")
    if not isinstance(items, list):
        raise SubscriptionError("구독 API 응답에 subscribers 목록이 없습니다")

    known = set(agency_order)
    prefs: dict[str, Preference] = {}
    for item in items:
        if not isinstance(item, dict):
            raise SubscriptionError("구독 API 응답 항목 형식이 올바르지 않습니다")
        sid = item.get("subscriber")
        if not isinstance(sid, str) or not _SUBSCRIBER_RE.match(sid):
            raise SubscriptionError("구독 API 응답의 구독자 식별자 형식이 올바르지 않습니다")
        if sid in prefs:
            raise SubscriptionError("구독 API 응답에 중복된 구독자가 있습니다")
        if sid not in requested:
            raise SubscriptionError("구독 API 응답에 요청하지 않은 구독자가 있습니다")
        mode = item.get("mode")
        if not isinstance(mode, str) or mode not in _MODES:
            raise SubscriptionError("구독 API 응답에 알 수 없는 mode 가 있습니다")
        agencies = item.get("agencies")
        if not isinstance(agencies, list) or not all(isinstance(a, str) for a in agencies):
            raise SubscriptionError("구독 API 응답의 agencies 형식이 올바르지 않습니다")
        if len(set(agencies)) != len(agencies):
            raise SubscriptionError("구독 API 응답의 agencies 에 중복이 있습니다")
        unknown = [a for a in agencies if a not in known]
        if unknown:
            raise SubscriptionError(
                f"구독 API 응답에 알 수 없는 기관 ID 가 {len(unknown)}개 있습니다"
            )
        if mode == MODE_ALL and agencies:
            raise SubscriptionError("구독 API 응답에 기관 목록이 붙은 mode=all 이 있습니다")
        chosen = set(agencies)
        prefs[sid] = Preference(mode, tuple(a for a in agency_order if a in chosen))

    missing = len(requested - prefs.keys())
    if missing:
        raise SubscriptionError(f"구독 API 응답에서 구독자 {missing}명이 누락되었습니다")
    return prefs


def fetch_preferences(sub: SubscriptionConfig, subscriber_ids: list[str]) -> dict[str, Preference]:
    """구독 API 1회 호출. 재시도하지 않는다 — 실패하면 실행 전체가 다음 주기에 재시도된다."""
    payload = build_request(sub, subscriber_ids)
    try:
        resp = requests.post(
            sub.api_url,
            json=payload,
            headers={
                "Authorization": f"Bearer {sub.api_token}",
                "Accept": "application/json",
                "User-Agent": "law-rader-kr-monitor",
            },
            timeout=sub.timeout_sec,
            # 리다이렉트를 따라가면 예상 밖 호스트로 요청이 갈 수 있다 — 계약 위반으로 본다.
            allow_redirects=False,
        )
    except requests.Timeout:
        raise SubscriptionError("구독 API 응답 시간 초과") from None
    except requests.RequestException as e:
        raise SubscriptionError(f"구독 API 연결 실패({type(e).__name__})") from None

    if resp.status_code != 200:
        raise SubscriptionError(f"구독 API HTTP {resp.status_code}")
    body = resp.content or b""
    if len(body) > _MAX_RESPONSE_BYTES:
        raise SubscriptionError("구독 API 응답이 너무 큽니다")
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):
        # 과도하게 중첩된 JSON 은 크기 상한 안에서도 RecursionError 를 낸다 — 같은 계약 위반으로 본다.
        raise SubscriptionError("구독 API 응답이 올바른 JSON 이 아닙니다") from None
    return parse_response(data, set(payload["subscribers"]), tuple(a.id for a in sub.agencies))


# --------------------------------------------------------------------------
# 그룹화 · 필터 · 그룹별 발송
# --------------------------------------------------------------------------
def group_recipients(
    pairs: list[tuple[str, str]],
    prefs: dict[str, Preference],
    agencies: tuple[AgencyConfig, ...],
) -> list[RecipientGroup]:
    """같은 실효 설정끼리 묶는다. 순서는 결정적: 전체 그룹 먼저, 나머지는 카탈로그 순."""
    index = {a.id: i for i, a in enumerate(agencies)}
    source_map = {a.id: a.sources for a in agencies}
    buckets: dict[tuple[str, ...] | None, list[str]] = {}
    for addr, sid in pairs:
        pref = prefs[sid]
        key = None if pref.mode == MODE_ALL else pref.agencies
        buckets.setdefault(key, []).append(addr)

    def _order(key):
        return (0, ()) if key is None else (1, tuple(index[a] for a in key))

    groups = []
    for key in sorted(buckets, key=_order):
        if key is None:
            groups.append(RecipientGroup(None, (), tuple(buckets[key])))
        else:
            sources = frozenset(s for a in key for s in source_map[a])
            groups.append(RecipientGroup(sources, key, tuple(buckets[key])))
    return groups


def resolve_recipient_groups(sub: SubscriptionConfig, email: EmailConfig) -> list[RecipientGroup]:
    """설정 점검 → 식별자 생성 → API 조회·검증 → 그룹화. 어느 단계든 실패하면 예외."""
    missing = missing_subscription_settings(sub, email)
    if missing:
        raise SubscriptionError("구독 설정 누락: " + ", ".join(missing))
    pairs = _unique_recipients(email.recipients, sub.subscriber_hash_key)
    prefs = fetch_preferences(sub, [sid for _, sid in pairs])
    groups = group_recipients(pairs, prefs, sub.agencies)
    paused = sum(len(g.recipients) for g in groups if g.sources is not None and not g.agencies)
    everyone = sum(len(g.recipients) for g in groups if g.sources is None)
    log.info(
        "구독 설정 조회 완료 — 수신자 %d명 / 그룹 %d개 (전체 수신 %d명 · 기관 선택 %d명 · 일시중지 %d명)",
        len(pairs),
        len(groups),
        everyone,
        len(pairs) - everyone - paused,
        paused,
    )
    return groups


def filter_posts(
    posts_by_source: dict[str, list[Post]], sources: frozenset[str] | None
) -> dict[str, list[Post]]:
    """Post.source_key 로 거른다(표시명 키는 쓰지 않는다). 빈 소스 묶음은 뺀다.

    sources 가 None(전체)이면 입력을 그대로 돌려준다 — 전체 수신 그룹은 기존과 같은 객체로 발송.
    """
    if sources is None:
        return posts_by_source
    out: dict[str, list[Post]] = {}
    for name, posts in posts_by_source.items():
        kept = [p for p in posts if p.source_key in sources]
        if kept:
            out[name] = kept
    return out


def send_grouped(
    send: Callable,
    email: EmailConfig,
    groups: list[RecipientGroup],
    posts_by_source: dict[str, list[Post]],
    detail_updates: dict[str, list[Post]],
) -> None:
    """그룹마다 걸러서 send(=send_digest)로 보낸다. 한 그룹이라도 실패하면 예외를 그대로 올린다.

    호출자는 예외가 나면 seen·큐를 확정하지 않아야 한다(기존 '누락보다 중복' 정책).
    그 경우 이미 발송된 그룹은 다음 실행의 전체 재시도에서 같은 알림을 다시 받을 수 있다.
    send 의 호출 형태는 기존 main 과 같다: 상세 업데이트가 없으면 (cfg, posts) 두 인자.
    """
    total = len(groups)
    sent_groups = sent_recipients = skipped = 0
    for idx, group in enumerate(groups, 1):
        posts = filter_posts(posts_by_source, group.sources)
        updates = filter_posts(detail_updates, group.sources)
        if not posts and not updates:
            skipped += 1
            log.info(
                "구독 그룹 %d/%d(%s) — 전송 대상 없음, 수신자 %d명 발송 생략",
                idx, total, group.label, len(group.recipients),
            )
            continue
        group_cfg = replace(email, recipients=list(group.recipients))
        try:
            if updates:
                send(group_cfg, posts, detail_updates_by_source=updates)
            else:
                send(group_cfg, posts)
        except Exception as e:
            log.error(
                "구독 그룹 %d/%d(%s) 발송 실패(%s) — 이번 실행에서 먼저 발송된 그룹 %d개"
                "(수신자 %d명)는 다음 실행의 전체 재시도 때 같은 알림을 중복 수신할 수 있습니다.",
                idx, total, group.label, type(e).__name__, sent_groups, sent_recipients,
            )
            raise
        sent_groups += 1
        sent_recipients += len(group.recipients)
        log.info(
            "구독 그룹 %d/%d(%s) 발송 완료 — 수신자 %d명 / 신규 %d건 / 상세 업데이트 %d건",
            idx, total, group.label, len(group.recipients),
            sum(len(v) for v in posts.values()), sum(len(v) for v in updates.values()),
        )
    log.info(
        "구독 그룹 발송 종료 — 발송 %d개 / 전송 대상 없음 %d개 / 전체 %d개",
        sent_groups, skipped, total,
    )
