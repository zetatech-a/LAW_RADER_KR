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
            if baselined:
                # 운영: seen 과 대조해 신규만, 경계까지 최대 max_pages 페이지 훑음.
                result = scraper.collect(
                    cfg.fetch.list_limit, state.seen_ids(src.key), cfg.fetch.max_pages
                )
            else:
                # 최초 기준선. append-only 게시판은 얕게(baseline_pages) 잡으면 충분하지만,
                # 가변 멤버십 소스(FULL_BASELINE=True, 예: 계류의안)는 항목이 빠질 때
                # 오래된 항목이 신규로 오인되지 않도록 현재 전체를 깊게 기록한다.
                base_pages = (
                    cfg.fetch.full_baseline_pages
                    if scraper.FULL_BASELINE
                    else cfg.fetch.baseline_pages
                )
                result = scraper.collect(cfg.fetch.list_limit, set(), base_pages)
        except Exception as e:  # noqa: BLE001  — 한 소스 실패가 전체를 막지 않도록
            log.error("[%s] 목록 수집 실패: %s", src.key, e)
            errors.append(f"{src.key}: {e}")
            continue

        # HTTP 는 성공했지만 파서가 0건을 반환하면(마크업/AJAX 변경 등) '성공'으로 세지
        # 않는다. 그래야 전 소스가 빈 결과인 파싱 전면장애를 all-failed 가드가 잡아낸다.
        if result.scanned > 0:
            succeeded += 1
        else:
            log.warning("[%s] HTTP 성공했으나 파싱 0건 — 파서/마크업 확인 필요", src.key)

        # 최초 실행: 기준선만 기록하고 메일 생략. 단, 0건이면(일시 오류·셀렉터 불일치·
        # AJAX 미로딩 가능) 기준선을 잡지 않는다(잘못된 빈 기준선은 이후 폭탄이 된다).
        if not baselined:
            if result.scanned == 0:
                log.warning(
                    "[%s] 최초 실행인데 0건 — 기준선 보류(다음 실행에 재시도). "
                    "지속되면 debug 덤프로 셀렉터/AJAX 확인 필요.",
                    src.key,
                )
            else:
                log.info("[%s] 최초 실행 — 기준선 %d건 기록(메일 생략)", src.key, result.scanned)
                state.mark_seen(
                    src.key, [p.post_id for p in result.posts], baselined=True
                )
            continue

        new_posts = result.posts  # collect 가 이미 seen 과 대조해 신규만 반환

        if not new_posts:
            log.info("[%s] 신규 없음 (조회 %d건)", src.key, result.scanned)
            continue

        # 폭주 안전장치: 한 번에 비정상적으로 많은 신규가 잡히면(상태 불일치 등) 수백 건의
        # 상세/첨부를 내려받아 실행이 폭주하는 것을 막는다. 최신 cap 건만 상세수집·발송하고,
        # 나머지는 seen 처리한다(전부 seen 처리하므로 다음 실행에 재발생하지 않음).
        cap = cfg.fetch.max_new_per_source
        all_new_ids = [p.post_id for p in new_posts]
        if len(new_posts) > cap:
            log.warning(
                "[%s] 신규 %d건 — 비정상적 대량(상태 불일치 의심). 최신 %d건만 발송하고 "
                "나머지는 seen 처리합니다.",
                src.key,
                len(new_posts),
                cap,
            )
            new_posts = new_posts[:cap]

        log.info("[%s] 신규 %d건 발견 — 상세 수집", src.key, len(new_posts))
        for p in new_posts:
            try:
                scraper.enrich(p)
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] enrich 실패 %s: %s", src.key, p.url, e)

        # 신규(초과분 포함)를 모두 seen 에 기록. 메일 성공 후 state.save() 로 확정.
        state.mark_seen(src.key, all_new_ids)
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
