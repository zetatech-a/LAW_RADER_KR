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
# 목록은 됐지만 상세가 안 된 상태. '전체 성공'과 구분해야 한다 — 의안은 상세 본문이
# 곧 요약 입력이라, 목록만 되고 본문이 비면 메일에 제목·링크만 나간다.
PARTIAL = "🟠"

# 상세 본문(body)이 반드시 있어야 하는 소스.
# 다른 소스는 상세가 PDF 직접 다운로드(fss_mgmt_notice)거나, 상세 주소가 확인된 구분만
# 수집하는 소스(better_reply — 현장건의 과제 등은 목록 링크만)라 body 가 비는 것이
# 정상이므로 기존 판정 기준(본문·첨부·구조화항목 중 하나)을 유지한다.
_BODY_REQUIRED = {"assembly_bill"}

# 상세 본문에 반드시 들어 있어야 하는 표식(선언한 소스만 검사한다).
# 회신사례는 질의요지·회답·이유가 모두 있을 때만 본문을 만들므로, 하나라도 빠지면
# 본문이 통째로 비어 나간다. 그 상태를 초록불로 넘기면 상세 파서가 깨진 채 운영된다.
_REQUIRED_BODY_MARKERS = {"better_reply": ("[질의요지]", "[회답]", "[이유]")}

# 의안은 여러 건을 표본으로 본다. 목록 맨 위는 갓 접수된 의안이라 원문이 아직 없을 수
# 있어(PENDING), 첫 건만 보면 '등록 대기'와 '수집 고장'을 구분할 수 없다.
_ASSEMBLY_SAMPLE = 3


def _sample(posts, n=5):
    lines = []
    for p in posts[:n]:
        lines.append(f"      - [{p.date or '날짜?'}] {p.title[:60]}")
        lines.append(f"        {p.url}")
    return "\n".join(lines)


def verify_source(scraper, list_limit, do_enrich, assembly_sample=_ASSEMBLY_SAMPLE):
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
    if do_enrich and key in _BODY_REQUIRED:
        return _verify_assembly_detail(scraper, page1, report, assembly_sample), page1

    if do_enrich:
        # 한 소스 안에 상세 수집 대상과 비대상이 섞일 수 있다(회신사례는 구분별로
        # 상세 endpoint 가 확인된 글만 상세 페이지가 있다). 비대상 글을 표본으로 잡으면
        # 상세 파서를 한 번도 검증하지 못한 채 초록불이 나가므로 대상인 글을 고른다.
        first = _enrichable_sample(scraper, page1)
        if first is None:
            report["status"] = PARTIAL
            report["list_ok"] = True
            report["body_len"] = 0
            report["enrich_ok"] = False
            report["detail_ok"] = False
            report["enrich"] = f"{WARN} 상세 검증 표본 없음(1페이지에 상세 수집 대상 글이 없음)"
            report["detail"] = (
                f"목록 수집 성공({len(page1)}건) / 1페이지에 상세 수집 대상 글이 없어 "
                "상세 파서를 검증하지 못했습니다. 지원 유형이 나올 때까지 "
                "fetch.list_limit 을 늘려 재확인하세요."
            )
            return report, page1
        try:
            scraper.enrich(first)
            body_len = len(first.body or "")
            n_att = len(first.attachments)
            n_dl = sum(1 for a in first.attachments if a.data)
            # 구조화 항목만 채우는 소스(검사결과 제재)는 body 가 비어 있는 것이 정상이므로
            # details 도 '상세 수집 성공'으로 센다.
            n_details = len(first.details)
            detail_note = f" / 구조화 항목 {n_details}개" if n_details else ""
            report["body_len"] = body_len
            report["enrich"] = (
                f"본문 {body_len}자 / 첨부 {n_att}개(다운로드 {n_dl}개){detail_note}"
            )
            report["enrich_ok"] = body_len > 0 or n_att > 0 or n_details > 0
        except Exception as e:  # noqa: BLE001
            report["body_len"] = 0
            report["enrich"] = f"{WARN} enrich 실패: {e}"
            report["enrich_ok"] = False

        # 본문 표식을 선언한 소스는 '본문이 비어도 OK' 예외를 적용하지 않는다.
        missing = [m for m in _REQUIRED_BODY_MARKERS.get(key, ()) if m not in (first.body or "")]
        if missing:
            report["status"] = PARTIAL
            report["list_ok"] = True
            report["detail_ok"] = False
            report["detail"] = (
                f"목록 수집 성공({len(page1)}건) / 상세 본문에 {' '.join(missing)} 가 "
                f"없습니다({first.url}) — 부분 본문은 요약 입력으로 쓰지 않아 본문이 통째로 "
                "비워집니다. 상세 마크업을 확인하세요."
            )
            return report, page1

    report["status"] = OK
    report["list_ok"] = True
    report["detail_ok"] = report.get("enrich_ok", True)
    return report, page1


def _enrichable_sample(scraper, page1):
    """상세 수집 대상인 첫 글. 대상이 하나도 없으면 None.

    per-post 훅(BaseScraper.supports_enrich)이 없는 스크래퍼는 기존대로 첫 글을 쓴다.
    """
    hook = getattr(scraper, "supports_enrich", None)
    if hook is None:
        return page1[0]
    for p in page1:
        if hook(p):
            return p
    return None


def _verify_assembly_detail(scraper, page1, report, sample_size=_ASSEMBLY_SAMPLE):
    """의안 상세를 표본으로 검증한다 — available 과 pending 을 **따로** 판정한다.

    '원문이 아직 등록되지 않음(PENDING)'은 고장이 아니다. 그래서 두 질문을 나눠 묻는다:
      ① available 검증 — 원문이 있는 의안에서 실제로 본문을 뽑아내는가?
      ② pending 검증  — 원문이 없는 의안을 '실패'가 아니라 '등록 대기'로 판정하는가?

    표본 안에 ERROR 가 하나라도 있으면 구조·전송이 깨진 것이므로 실패다.
    ERROR 가 없는데 available 이 0이면 ①을 확인하지 못한 것이므로 부분 실패로 둔다
    (초록불로 넘기면 '전부 등록 대기'라는 말에 고장이 숨을 수 있다).
    """
    from src.models import ProposalContentStatus as S

    sample = page1[: max(1, sample_size)]
    counts = {S.AVAILABLE: 0, S.PENDING: 0, S.ERROR: 0, S.UNKNOWN: 0}
    lines = []
    for p in sample:
        try:
            scraper.enrich(p)
        except Exception as e:  # noqa: BLE001 - enrich 는 원래 삼키지만 방어
            p.proposal_status = S.ERROR
            p.proposal_note = f"{type(e).__name__}: {e}"
        counts[p.proposal_status] = counts.get(p.proposal_status, 0) + 1
        lines.append(
            f"      - {p.proposal_status.value:<9} 본문 {len(p.body or '')}자  "
            f"{p.post_id}  {p.proposal_note[:70]}"
        )

    available = counts[S.AVAILABLE]
    pending = counts[S.PENDING]
    failed = counts[S.ERROR] + counts[S.UNKNOWN]
    report["body_len"] = max((len(p.body or "") for p in sample), default=0)
    report["proposal_counts"] = {
        "sampled": len(sample),
        "available": available,
        "pending": pending,
        "failed": failed,
    }
    report["enrich"] = (
        f"표본 {len(sample)}건 — available {available} / pending {pending} / "
        f"failed {failed}\n" + "\n".join(lines)
    )
    report["enrich_ok"] = available > 0
    report["list_ok"] = True

    if failed:
        report["status"] = FAIL
        report["detail"] = (
            f"목록 수집 성공({len(page1)}건) / 제안이유 수집 실패 {failed}건 — "
            "구조·endpoint 확인 필요. scripts/capture_assembly_network.py 로 실제 "
            "XHR 계약을 확인하세요."
        )
        report["detail_ok"] = False
    elif available == 0:
        # 전부 등록 대기. 고장은 아니지만 available 을 확인하지 못했다.
        report["status"] = PARTIAL
        report["detail"] = (
            f"목록 수집 성공({len(page1)}건) / 표본 {len(sample)}건이 모두 등록 대기 — "
            "제안이유 추출 자체는 확인하지 못했습니다. 원문이 있는 오래된 의안으로 "
            "재확인하세요(--assembly-sample 로 표본을 늘릴 수 있습니다)."
        )
        report["detail_ok"] = False
    else:
        report["status"] = OK
        report["detail_ok"] = True
    return report


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--only", default="")
    ap.add_argument("--no-enrich", action="store_true")
    ap.add_argument(
        "--assembly-sample",
        type=int,
        default=_ASSEMBLY_SAMPLE,
        help="의안 상세를 몇 건까지 표본으로 볼지(등록 대기와 고장을 구분하기 위함)",
    )
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
    print("LAW RADER KR — 소스 라이브 검증")
    print("=" * 70)

    reports = []
    for src in cfg.sources:
        if not src.enabled or (only and src.key not in only):
            continue
        scraper = build_scraper(src, fetcher)
        print(f"\n[{src.key}] {src.name}")
        report, page1 = verify_source(
            scraper, cfg.fetch.list_limit, not args.no_enrich, args.assembly_sample
        )
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
        line = f"  {r['status']} {r['key']:<20} 목록 {pg}건  {r.get('pagination','')}"
        pc = r.get("proposal_counts")
        if pc:
            line += (
                f"  [제안이유 available {pc['available']} / pending {pc['pending']}"
                f" / failed {pc['failed']}]"
            )
        elif r["status"] == PARTIAL:
            line += f"  [상세 본문 {r.get('body_len', 0)}자]"
        print(line)

    n_fail = sum(1 for r in reports if r["status"] == FAIL)
    n_partial = sum(1 for r in reports if r["status"] == PARTIAL)
    print(f"\n실패(수정 필요) 소스: {n_fail}/{len(reports)}")
    if n_partial:
        # 목록만 되는 상태를 초록불로 넘기면, 메일에 제목·링크만 실리는 채로 운영이 계속된다.
        print(f"부분 실패(목록 성공 / 상세 실패) 소스: {n_partial}/{len(reports)}")
        for r in reports:
            if r["status"] == PARTIAL:
                print(f"  {PARTIAL} {r['key']}: {r.get('detail', '')}")
    print("원본 HTML 은 debug/ 에 저장되었습니다. 0건 소스는 이 파일로 셀렉터를 확인하세요.")
    return 1 if (n_fail or n_partial) else 0


if __name__ == "__main__":
    sys.exit(main())
