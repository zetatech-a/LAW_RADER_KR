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
    fetcher = Fetcher(
        timeout=cfg.fetch.timeout_sec,
        delay=cfg.fetch.delay_sec,
        max_download_bytes=cfg.email.max_attach_bytes,
    )

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    posts_by_source: dict[str, list[Post]] = {}
    errors: list[str] = []
    selected = 0   # 실행 대상(활성+선택) 소스 수
    succeeded = 0  # 목록 수집에 성공한 소스 수

    for src in cfg.sources:
        if not src.enabled:
            continue
        if only and src.key not in only:
            continue
        selected += 1

        try:
            scraper = build_scraper(src, fetcher)
            baselined = state.is_baselined(src.key)
            reached_boundary = True
            if baselined:
                # 이미 기준선이 있는 소스: 이미 본 글 경계에 닿을 때까지 페이지를 넘겨 수집
                # (평상시엔 첫 페이지에서 곧바로 멈춤 → 요청 1회).
                # 직전 실행이 cap 에 걸렸으면(backfill) 이어서 남은 백로그를 계속 수집.
                listing, reached_boundary = scraper.collect(
                    cfg.fetch.list_limit,
                    state.seen_ids(src.key),
                    cfg.fetch.max_pages,
                    backfill=state.backfill_pending(src.key),
                )
            else:
                # 최초 실행: 기준선 수립용으로 1페이지만
                listing = scraper.fetch_list(cfg.fetch.list_limit)
        except Exception as e:  # noqa: BLE001  — 한 소스 실패가 전체를 막지 않도록
            log.error("[%s] 목록 수집 실패: %s", src.key, e)
            errors.append(f"{src.key}: {e}")
            continue

        current_ids = [p.post_id for p in listing]

        # HTTP 는 성공했지만 파서가 0건을 반환하면(마크업/AJAX 변경 등) '성공'으로 세지
        # 않는다. 그래야 전 소스가 빈 결과인 파싱 전면장애를 all-failed 가드가 잡아낸다.
        # (이 게시판들은 정상 시 항상 목록이 있으므로 0건 = 이상 신호)
        if current_ids:
            succeeded += 1
        else:
            log.warning("[%s] HTTP 성공했으나 파싱 0건 — 파서/마크업 확인 필요", src.key)

        # 최초 실행: 기준선만 기록하고 메일 생략.
        # 단, 0건이면(일시 오류·셀렉터 불일치·AJAX 미로딩 가능) 기준선을 잡지 않는다.
        # 잘못된 빈 기준선은 이후 수집이 되기 시작할 때 전량을 '신규'로 오인해 폭탄이 된다.
        if not baselined:
            if not current_ids:
                log.warning(
                    "[%s] 최초 실행인데 0건 — 기준선 보류(다음 실행에 재시도). "
                    "지속되면 debug 덤프로 셀렉터/AJAX 확인 필요.",
                    src.key,
                )
            else:
                log.info("[%s] 최초 실행 — 기준선 %d건 기록(메일 생략)", src.key, len(current_ids))
                state.mark_seen(src.key, current_ids, baselined=True)
            continue

        new_posts = [p for p in listing if state.is_new(src.key, p.post_id)]

        # 현재 목록에 있는 글은 모두 seen 앞쪽으로 갱신한다. 이렇게 하면 상단 고정
        # 공지처럼 계속 노출되는 글이 500개 상한에 밀려 제거→재발송되는 일을 막는다.
        if current_ids:
            state.mark_seen(src.key, current_ids)

        # cap 에 걸려 경계 미도달이면 백로그가 남은 것 → 다음 실행에 backfill 로 이어받는다.
        state.set_backfill_pending(src.key, not reached_boundary)
        if not reached_boundary:
            log.info("[%s] 백로그 잔여 — 다음 실행에 이어서 수집(backfill)", src.key)

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

    total = sum(len(v) for v in posts_by_source.values())
    log.info("총 신규 %d건", total)

    # 선택된 소스가 있는데 하나도 수집에 성공하지 못하면 실패로 종료한다.
    # (전면 장애를 초록불로 숨기지 않기 위함. 부분 실패는 그대로 격리·진행)
    if selected > 0 and succeeded == 0:
        log.error("선택된 소스 %d개가 모두 실패 — 실패 종료. 오류: %s", selected, "; ".join(errors))
        return 1

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
