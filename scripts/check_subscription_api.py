"""구독 API 연동 점검(운영자용) — 메일을 보내지 않고 state 도 바꾸지 않는다.

현재 환경변수의 MAIL_TO 를 구독 API(Cloudflare Worker)에 동기화하고, 정기 실행과 같은 코드
(src/subscription.py)로 받은 설정을 그룹 요약으로만 출력한다. 주소·식별자는 출력하지 않는다.

주의: 이 호출은 정기 실행과 같은 '동기화'다 — 여기 넘긴 MAIL_TO 에 없는 구독자는 비활성화된다
(설정은 보존되며 다음 동기화에 다시 포함되면 복원된다). 운영 전환 전 시험에는 시험용 MAIL_TO 를,
전환 후에는 운영 MAIL_TO 와 같은 값만 쓴다. config.yaml 의 subscription.enabled 값과 무관하게 점검한다.

사용(값은 셸에서 직접 입력 — 채팅·파일에 남기지 않는다):
  read -rs SUBSCRIPTION_API_TOKEN; export SUBSCRIPTION_API_TOKEN
  read -rs SUBSCRIBER_HASH_KEY;    export SUBSCRIBER_HASH_KEY
  export SUBSCRIPTION_API_URL=https://<worker>.workers.dev/internal/sync-and-resolve
  export SUBSCRIPTION_PAGE_URL=https://<worker>.workers.dev/
  export MAIL_TO=you@example.com
  python scripts/check_subscription_api.py
"""
import dataclasses
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import load_config
from src.subscription import SubscriptionError, resolve_recipient_groups


def main() -> int:
    cfg = load_config("config.yaml")
    sub = dataclasses.replace(cfg.subscription, enabled=True)
    email = dataclasses.replace(
        cfg.email, preferences_url=os.environ.get("SUBSCRIPTION_PAGE_URL", "").strip()
    )
    meta: dict = {}
    try:
        groups = resolve_recipient_groups(sub, email, meta)
    except SubscriptionError as e:
        print(f"실패: {e}")
        return 1
    print(f"성공: 그룹 {len(groups)}개")
    # 비밀값이 아닌 무작위 식별자다. 운영 전환 전에 GitHub Variable SUBSCRIPTION_DB_INSTANCE 로 등록한다.
    print(f"  구독 DB 식별자(SUBSCRIPTION_DB_INSTANCE): {meta.get('instance', '(응답에 없음)')}")
    for g in groups:
        print(f"  {g.label}: {len(g.recipients)}명")
    return 0


if __name__ == "__main__":
    sys.exit(main())
