"""LAW_RADER_KR 실행 진입점.

흐름:
  1) config + state 로드
  2) 소스별로 목록 수집 → 신규 판별
     - 해당 소스가 처음이면(baseline 미수립) 신규를 메일로 보내지 않고 기준선만 기록
  3) 신규 게시글의 상세/첨부 수집(enrich)
  4) 신규가 있으면 다이제스트 메일 1통 발송
  5) state 저장

사용:
  python -m src.main            # 정상 실행(메일 발송)
  python -m src.main --dry-run  # 수집만 하고 메일은 보내지 않음(출력만)
  python -m src.main --debug    # 상세 로그
  python -m src.main --only fss_press,fsc_press   # 특정 소스만
"""
from __future__ import annotations

import argparse
import logging
import sys

from .config import load_config
from .fetcher import Fetcher
from .models import Post
from .notifier import send_digest
from .scrapers import build_scraper
from .state import State

log = logging.getLogger("law_rader")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="한국 금융 규제·입법 모니터링")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--state", default="state/seen.json")
    p.add_argument("--dry-run", action="store_true", help="메일 발송 없이 수집 결과만 출력")
    p.add_argument("--debug", action="store_true", help="디버그 로그")
    p.add_argument("--only", default="", help="쉼표로 구분한 소스 key 만 실행")
    return p.parse_args(argv)


def run(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config(args.config)
    state = State(args.state)
    fetcher = Fetcher(timeout=cfg.fetch.timeout_sec, delay=cfg.fetch.delay_sec)

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    posts_by_source: dict[str, list[Post]] = {}
    errors: list[str] = []

    for src in cfg.sources:
        if not src.enabled:
            continue
        if only and src.key not in only:
            continue

        try:
            scraper = build_scraper(src, fetcher)
            listing = scraper.fetch_list(cfg.fetch.list_limit)
        except Exception as e:  # noqa: BLE001  — 한 소스 실패가 전체를 막지 않도록
            log.error("[%s] 목록 수집 실패: %s", src.key, e)
            errors.append(f"{src.key}: {e}")
            continue

        current_ids = [p.post_id for p in listing]

        # 최초 실행: 기준선만 기록하고 메일 생략
        if not state.is_baselined(src.key):
            log.info("[%s] 최초 실행 — 기준선 %d건 기록(메일 생략)", src.key, len(current_ids))
            state.mark_seen(src.key, current_ids, baselined=True)
            continue

        new_posts = [p for p in listing if state.is_new(src.key, p.post_id)]
        if not new_posts:
            log.info("[%s] 신규 없음 (목록 %d건 확인)", src.key, len(listing))
            continue

        log.info("[%s] 신규 %d건 발견 — 상세 수집", src.key, len(new_posts))
        for p in new_posts:
            try:
                scraper.enrich(p)
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] enrich 실패 %s: %s", src.key, p.url, e)

        posts_by_source[src.name] = new_posts
        # 신규 id 를 seen 에 추가 (아직 저장 전; 메일 성공 후 save)
        state.mark_seen(src.key, [p.post_id for p in new_posts])

    total = sum(len(v) for v in posts_by_source.values())
    log.info("총 신규 %d건", total)

    if args.dry_run:
        _print_dry_run(posts_by_source)
        log.info("--dry-run: 메일 미발송, state 미저장")
        return 0

    if total > 0:
        try:
            send_digest(cfg.email, posts_by_source)
        except Exception as e:  # noqa: BLE001
            log.error("메일 발송 실패 — state 저장하지 않고 종료(다음 실행에 재시도): %s", e)
            return 1

    # 메일 성공(또는 신규 없음) 시에만 state 확정 저장
    state.save()
    log.info("state 저장 완료")

    if errors:
        log.warning("일부 소스 오류: %s", "; ".join(errors))
    return 0


def _print_dry_run(posts_by_source: dict[str, list[Post]]) -> None:
    for source_name, posts in posts_by_source.items():
        print(f"\n=== {source_name} ({len(posts)}건) ===")
        for p in posts:
            print(f"  [{p.date or '날짜미상'}] {p.title}")
            print(f"    {p.url}")
            if p.attachments:
                print(f"    첨부 {len(p.attachments)}개: " + ", ".join(a.filename for a in p.attachments))


if __name__ == "__main__":
    sys.exit(run())
