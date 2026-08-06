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

from dataclasses import dataclass

from .config import load_config
from .fetcher import Fetcher
from .models import ASSEMBLY_SOURCE_KEY, Post, ProposalContentStatus
from .notifier import missing_email_settings, send_digest, verify_smtp_login
from .scrapers import build_scraper
from .state import State
from .summarizer import ai_target_count, summarize_posts

log = logging.getLogger("law_rader")


@dataclass
class _DetailStats:
    """상세(enrich) 수집 집계.

    '등록 대기(pending)'는 실패가 아니다. 갓 접수된 의안은 원문이 아직 공개되지
    않아 본문이 비는 것이 정상이고, 그것을 실패로 세면 매 실행마다 거짓 실패가
    쌓여 진짜 고장을 가린다. 그래서 succeeded/pending/failed 를 따로 센다.
    """

    attempted: int = 0
    succeeded: int = 0
    pending: int = 0

    @property
    def failed(self) -> int:
        return self.attempted - self.succeeded - self.pending

    def count(self, ok: bool, pending: bool = False) -> None:
        self.attempted += 1
        if pending:
            self.pending += 1
        elif ok:
            self.succeeded += 1


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="한국 금융 규제·입법 모니터링")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--state", default="state/seen.json")
    p.add_argument("--dry-run", action="store_true", help="메일 발송 없이 수집 결과만 출력")
    p.add_argument("--debug", action="store_true", help="디버그 로그")
    p.add_argument("--only", default="", help="쉼표로 구분한 소스 key 만 실행")
    p.add_argument(
        "--no-llm", action="store_true", help="LLM 본문 요약을 건너뛴다(원문 발췌로 발송)"
    )
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
    # 발송 대상 신규 ID (소스별). 메일 성공 후에만 seen 처리한다. 발송 전에 미리
    # 기록하면, 뒤 소스의 기준선 즉시저장이 이 ID들까지 디스크에 써버려 메일 실패 시
    # 재발송 없이 영구 누락된다.
    pending_seen: list[tuple[str, list[str]]] = []
    errors: list[str] = []
    selected = 0   # 실행 대상(활성+선택) 소스 수
    succeeded = 0  # 목록 수집에 성공한 소스 수
    # 상세(enrich) 집계 — 실행 종료 시 한 번에 보고한다. 상세 수집이 전멸하면
    # 메일은 제목·링크만 남은 채로 계속 나가므로, 로그를 보지 않으면 알아채기 어렵다.
    #
    # 의안은 따로 센다. 의안 상세(제안이유)는 폼을 발견해 되쏘는 방식이라 다른 소스와
    # 독립적으로 깨질 수 있는데, 전체 합계만 보면 금융위·금감원이 성공하는 한 의안
    # 전면 실패가 묻힌다.
    detail = _DetailStats()
    assembly_detail = _DetailStats()

    for src in cfg.sources:
        if not src.enabled:
            continue
        if only and src.key not in only:
            continue
        selected += 1

        log.info("[%s] 수집 시작 (%s)", src.key, src.name)
        try:
            scraper = build_scraper(src, fetcher)
            baselined = state.is_baselined(src.key)
            if baselined:
                # 운영: seen 과 대조해 신규만, 경계까지 최대 max_pages 페이지 훑음.
                result = scraper.collect(
                    cfg.fetch.list_limit, state.seen_ids(src.key), cfg.fetch.max_pages
                )
            else:
                # 최초 기준선: 얕게(baseline_pages) 기록. 이미 있는 글을 신규로 오인하지
                # 않을 버퍼면 충분하고, 전체 아카이브를 훑어 실행이 폭주하지 않도록 한다.
                result = scraper.collect(
                    cfg.fetch.list_limit, set(), cfg.fetch.baseline_pages
                )
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
                # 기준선은 메일과 무관하므로 소스마다 즉시 저장한다(뒤 소스가 실패·취소돼도
                # 앞 기준선 보존 → 최초 실행 반복 방지). 단 dry-run 에서는 저장하지 않는다.
                if not args.dry_run:
                    state.save()
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
            # 스크래퍼 대부분은 실패를 내부에서 삼키므로(한 건 실패가 나머지를 막지
            # 않도록) 예외 유무가 아니라 '무언가 채워졌는지'로 성공을 센다.
            # verify_sources.py 의 enrich_ok 와 같은 기준이다.
            ok_detail = bool(p.body or p.details or p.attachments)
            # 등록 대기는 실패가 아니다(원문이 아직 공개되지 않았을 뿐).
            is_pending = (
                p.source_key == ASSEMBLY_SOURCE_KEY
                and p.proposal_status is ProposalContentStatus.PENDING
            )
            detail.count(ok_detail, pending=is_pending)
            # 의안 통계는 의안 게시물로만 만든다. 다른 소스(상세가 원래 비는 게시판
            # 등)의 실패가 섞이면 있지도 않은 의안 장애를 보고하게 된다.
            if p.source_key == ASSEMBLY_SOURCE_KEY:
                assembly_detail.count(ok_detail, pending=is_pending)

        # 신규(초과분 포함)는 '메일 성공 후'에만 seen 처리하도록 보류한다.
        pending_seen.append((src.key, all_new_ids))
        posts_by_source[src.name] = new_posts

    total = sum(len(v) for v in posts_by_source.values())
    log.info("총 신규 %d건", total)

    # 선택된 소스가 있는데 하나도 수집에 성공하지 못하면 실패로 종료한다.
    # (전면 장애를 초록불로 숨기지 않기 위함. 부분 실패는 그대로 격리·진행)
    if selected > 0 and succeeded == 0:
        log.error("선택된 소스 %d개가 모두 실패 — 실패 종료. 오류: %s", selected, "; ".join(errors))
        return 1

    # 어차피 못 보낼 메일이면 LLM 을 호출하기 전에 멈춘다. 발송 실패는 신규를 seen 으로
    # 확정하지 않으므로, 발송이 막힌 채 방치되면 매 실행이 같은 글을 다시 요약하며
    # 무료 할당량과 시간예산만 반복 소모한다. (--dry-run 은 원래 발송하지 않으므로 제외)
    if total > 0 and not args.dry_run:
        missing = missing_email_settings(cfg.email)
        if missing:
            log.error(
                "메일 설정 누락(%s) — 발송이 불가능하므로 요약·발송을 건너뜁니다. "
                "신규 %d건은 미확정으로 남아 설정 후 다음 실행에 발송됩니다.",
                ", ".join(missing),
                total,
            )
            return 1
        # 값이 채워져 있어도 앱 비밀번호 폐기·호스트 도달 불가면 발송은 실패한다.
        # 요약에 할당량을 쓰기 전에 실제로 로그인해 본다.
        try:
            verify_smtp_login(cfg.email)
        except Exception as e:  # noqa: BLE001
            log.error(
                "SMTP 연결/인증 실패(%s: %s) — 발송이 불가능하므로 요약·발송을 "
                "건너뜁니다. 신규 %d건은 미확정으로 남아 복구 후 다음 실행에 발송됩니다.",
                type(e).__name__,
                e,
                total,
            )
            return 1

    # 본문이 있는 글은 LLM 으로 3줄 요약해 메일에 싣는다. 요약이 실패하면 summary 가
    # 빈 채로 남고 notifier 가 기존 원문 발췌로 되돌아가므로, 발송 자체는 막지 않는다.
    if total > 0 and not args.no_llm:
        try:
            summarize_posts(cfg.llm, posts_by_source)
        except Exception as e:  # noqa: BLE001
            log.warning("LLM 요약 단계 실패 — 원문 발췌로 발송합니다: %s", e)
    elif args.no_llm:
        log.info("--no-llm: LLM 요약 생략")

    # 실행 집계. 발송 직전에 남겨 실패 여부와 무관하게 항상 기록되게 한다.
    _log_run_summary(cfg, posts_by_source, detail, assembly_detail)

    if args.dry_run:
        _print_dry_run(posts_by_source)
        log.info("--dry-run: 메일 미발송, state 미저장")
        return 0

    if total > 0:
        try:
            send_digest(cfg.email, posts_by_source)
        except Exception as e:  # noqa: BLE001
            log.error("메일 발송 실패 — 신규 미확정, 다음 실행에 재시도: %s", e)
            return 1

    # 메일 성공(또는 신규 없음) 후에 비로소 발송분을 seen 으로 확정하고 저장한다.
    for key, ids in pending_seen:
        state.mark_seen(key, ids)
    state.save()
    log.info("state 저장 완료")

    if errors:
        log.warning("일부 소스 오류: %s", "; ".join(errors))
    return 0


def _log_run_summary(
    cfg,
    posts_by_source: dict[str, list[Post]],
    detail: "_DetailStats",
    assembly_detail: "_DetailStats",
) -> None:
    """실행 종료 집계: 상세 수집(전체/의안)과 AI 요약의 시도/성공/실패 건수.

    상세 수집 성공률 0% 는 파서·마크업이 통째로 어긋났다는 뜻이라 ERROR 로 남긴다.
    전체와 의안을 따로 판정하는 이유는, 금융위·금감원이 정상이면 합계는 멀쩡해 보여
    의안 전면 실패가 묻히기 때문이다.

    어느 ERROR 도 발송을 막지 않는다 — 본문 없이라도 제목·링크가 담긴 알림이 나가는
    편이 알림 자체가 끊기는 것보다 낫다(전체 설계 원칙).
    """
    log.info(
        "상세 수집 집계(전체) — 시도 %d건 / 성공 %d건 / 등록대기 %d건 / 실패 %d건",
        detail.attempted,
        detail.succeeded,
        detail.pending,
        detail.failed,
    )
    # 의안은 요구된 이름 그대로 attempted / available / pending / failed 를 남긴다.
    log.info(
        "의안 제안이유 집계 — attempted %d / available %d / pending %d / failed %d",
        assembly_detail.attempted,
        assembly_detail.succeeded,
        assembly_detail.pending,
        assembly_detail.failed,
    )
    # 등록 대기가 하나라도 있으면 '전멸'이 아니다 — 원문이 아직 공개되지 않은 것뿐이다.
    if detail.attempted > 0 and detail.succeeded == 0 and detail.pending == 0:
        log.error(
            "상세 수집 성공률 0%% (시도 %d건 전부 실패) — 파서/마크업 확인 필요. "
            "메일은 제목·링크만으로 계속 발송합니다. debug/ 덤프를 확인하세요.",
            detail.attempted,
        )
    # 전체 ERROR 와 별개로 판정한다. 다른 소스가 성공해 위 조건이 거짓이어도
    # 의안만 전멸했다면 반드시 드러나야 한다.
    if (
        assembly_detail.attempted > 0
        and assembly_detail.succeeded == 0
        and assembly_detail.pending == 0
    ):
        log.error(
            "의안 제안이유 수집 available 0건 (시도 %d건 전부 실패, 등록대기 0건) — "
            "상세 페이지 구조/endpoint 변경 의심. 의안은 요약 없이 제목·링크만 "
            "발송됩니다. debug/assembly_bill_detail_*.txt 를 확인하고 "
            "scripts/capture_assembly_network.py 로 실제 XHR 계약을 확인하세요.",
            assembly_detail.attempted,
        )
    elif assembly_detail.failed > 0:
        # available=0 이어도 pending>0 이면 전면 실패로 보지 않는다(요구사항).
        # 다만 실패가 섞여 있다면 조용히 넘기지 않고 경고로 남긴다.
        log.warning(
            "의안 제안이유 수집 실패 %d건 (available %d / pending %d) — "
            "debug/assembly_bill_detail_*.txt 확인.",
            assembly_detail.failed,
            assembly_detail.succeeded,
            assembly_detail.pending,
        )

    targets = ai_target_count(cfg.llm, posts_by_source)
    summarized = sum(
        1 for posts in posts_by_source.values() for p in posts if p.summary
    )
    log.info(
        "AI 요약 집계 — 대상 %d건 / 요약 %d건 / 발췌 폴백 %d건",
        targets,
        summarized,
        max(targets - summarized, 0),
    )


def _print_dry_run(posts_by_source: dict[str, list[Post]]) -> None:
    for source_name, posts in posts_by_source.items():
        print(f"\n=== {source_name} ({len(posts)}건) ===")
        for p in posts:
            print(f"  [{p.date or '날짜미상'}] {p.title}")
            if p.details:
                for label, value in p.details:
                    print(f"      {label}: {value}")
            elif p.summary:
                for s in p.summary:
                    print(f"      · {s}")
            elif p.body:
                print(f"      (요약 없음) {p.body[:120]}")
            print(f"    {p.url}")
            if p.attachments:
                print(f"    첨부 {len(p.attachments)}개: " + ", ".join(a.filename for a in p.attachments))


if __name__ == "__main__":
    sys.exit(run())
