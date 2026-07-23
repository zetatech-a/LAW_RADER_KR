"""SMTP 설정이 올바른지 확인하는 테스트 메일 발송 스크립트.

사용:
  set -a; source .env; set +a
  python scripts/send_test_email.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import load_config
from src.models import Post
from src.notifier import send_digest

cfg = load_config("config.yaml")
demo = Post(
    source_key="test",
    source_name="LAW RADAR 설정 테스트",
    post_id="0",
    title="테스트 메일 — 이 메일이 보이면 SMTP 설정이 정상입니다.",
    url="https://www.fsc.go.kr/no010101",
    date="테스트",
    body="이 메일은 scripts/send_test_email.py 로 발송한 확인용입니다.",
)
send_digest(cfg.email, {demo.source_name: [demo]})
print("테스트 메일 발송 완료:", ", ".join(cfg.email.recipients))
