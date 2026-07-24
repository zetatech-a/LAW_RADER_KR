"""라이브 검증 도구 — 각 소스의 파서/페이지 파라미터가 실제로 동작하는지 점검.

이 스크립트는 메일을 보내지 않고 상태(state)도 바꾸지 않습니다.
인터넷 접속이 되는 환경(개인 PC, 또는 GitHub Actions 러너)에서 실행하세요.

각 소스에 대해 보고:
  - 1페이지 목록 건수 + 상위 제목/날짜/URL 샘플
  - 2페이지가 1페이지와 다른지(= PAGE_PARAM 페이지네이션 작동 여부)
  - 첫 글 상세(enrich) 시 본문 길이 / 첨부 개수(추출 여부)
  - 목록 원본 HTML 을 debug/<key>_list.txt 로 덤프(셀렉터 튜닝용)

사용:
  python scripts/verify_sources.py                 # 전체
  python scripts/verify_sources.py --only fss_press,fsc_press
  python scripts/verify_sources.py --no-enrich     # 상세/첨부 점검 생략(빠름)
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import load_config
from src.fetcher import Fetcher
from src.scrapers import build_scraper

OK = "✅"
WARN = "⚠️"
FAIL = "❌"


def _sample(posts, n=5):
    lines = []
    for p in posts[:n]:
        lines.append(f"      - [{p.date or '날짜?'}] {p.title[:60]}")
        lines.append(f"        {p.url}")
    return "\n".join(lines)


def verify_source(scraper, list_limit, do_enrich):
    key = scraper.key
    report = {"key": key, "name": scraper.name}

    # --- 1페이지 ---
    try:
        page1 = scraper.fetch_list(list_limit, page=1)
    except Exception as e:  # noqa: BLE001
        report["status"] = FAIL
        report["detail"] = f"1페이지 수집 실패: {e}"
        return report, None

    report["page1_count"] = len(page1)
    if not page1:
        report["status"] = FAIL
        report["detail"] = "1페이지 파싱 0건 — 셀렉터/AJAX 확인 필요(debug 덤프 참고)"
        return report, page1

    # --- 2페이지(페이지네이션 작동 확인) ---
    # HTML(PAGE_PARAM)뿐 아니라 POST/API 로 page 인자 페이지네이션하는 소스(SUPPORTS_
    # PAGINATION=True)도 실제로 2페이지를 요청해 오프셋 무시 여부까지 검증한다.
    if scraper.paginates:
        how = getattr(scraper, "PAGE_PARAM", None) or "page/offset"
        try:
            page2 = scraper.fetch_list(list_limit, page=2)
            ids1 = {p.post_id for p in page1}
            ids2 = {p.post_id for p in page2}
            if not page2:
                report["pagination"] = f"{WARN} 2페이지 0건(1페이지만 있거나 파라미터 무시)"
            elif ids2 - ids1:
                report["pagination"] = f"{OK} 2페이지가 1페이지와 다름(작동)"
            else:
                report["pagination"] = f"{WARN} 2페이지가 1페이지와 동일({how} 무시된 듯)"
        except Exception as e:  # noqa: BLE001
            report["pagination"] = f"{WARN} 2페이지 수집 실패: {e}"
    else:
        report["pagination"] = "— (페이지네이션 미지원 소스)"

    # --- 상세/첨부(enrich) ---
    if do_enrich:
        first = page1[0]
        try:
            scraper.enrich(first)
            body_len = len(first.body or "")
            n_att = len(first.attachments)
            n_dl = sum(1 for a in first.attachments if a.data)
            report["enrich"] = (
                f"본문 {body_len}자 / 첨부 {n_att}개(다운로드 {n_dl}개)"
            )
            report["enrich_ok"] = body_len > 0 or n_att > 0
        except Exception as e:  # noqa: BLE001
            report["enrich"] = f"{WARN} enrich 실패: {e}"
            report["enrich_ok"] = False

    report["status"] = OK
    return report, page1


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--only", default="")
    ap.add_argument("--no-enrich", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    # 검증 중 첨부는 크게 받지 않도록 소폭 상한(2MB)
    fetcher = Fetcher(
        timeout=cfg.fetch.timeout_sec,
        delay=cfg.fetch.delay_sec,
        max_download_bytes=2 * 1024 * 1024,
    )
    only = {s.strip() for s in args.only.split(",") if s.strip()}

    print("=" * 70)
    print("LAW RADAR KR — 소스 라이브 검증")
    print("=" * 70)

    reports = []
    for src in cfg.sources:
        if not src.enabled or (only and src.key not in only):
            continue
        scraper = build_scraper(src, fetcher)
        print(f"\n[{src.key}] {src.name}")
        report, page1 = verify_source(scraper, cfg.fetch.list_limit, not args.no_enrich)
        reports.append(report)
        # 0건 소스의 원본(HTML/JSON) 덤프는 각 스크래퍼가 내부에서 처리한다.

        print(f"  상태: {report['status']}")
        if "detail" in report:
            print(f"  {report['detail']}")
        if "page1_count" in report:
            print(f"  1페이지 건수: {report['page1_count']}")
        if page1:
            print(_sample(page1))
        if "pagination" in report:
            print(f"  페이지네이션: {report['pagination']}")
        if "enrich" in report:
            print(f"  상세/첨부: {report['enrich']}")

    # --- 요약 ---
    print("\n" + "=" * 70)
    print("요약")
    print("=" * 70)
    for r in reports:
        pg = r.get("page1_count", "-")
        print(f"  {r['status']} {r['key']:<20} 목록 {pg}건  {r.get('pagination','')}")

    n_fail = sum(1 for r in reports if r["status"] == FAIL)
    print(f"\n실패(수정 필요) 소스: {n_fail}/{len(reports)}")
    print("원본 HTML 은 debug/ 에 저장되었습니다. 0건 소스는 이 파일로 셀렉터를 확인하세요.")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
