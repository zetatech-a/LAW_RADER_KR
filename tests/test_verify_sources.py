"""verify_sources 의 성공/부분실패 판정 테스트(네트워크 없음).

핵심: 의안(assembly_bill)은 목록만 되고 상세 본문이 0자면 '전체 성공'이 아니다.
그 상태를 초록불로 넘기면 메일에 제목·링크만 실리는 채로 운영이 계속된다.
다른 소스는 상세가 PDF·JS 팝업이라 body 가 비는 것이 정상이므로 기존 기준을 유지한다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.verify_sources import FAIL, OK, PARTIAL, verify_source
from src.models import Attachment, Post, ProposalContentStatus as S


class _Scraper:
    """fetch_list/enrich 만 흉내내는 최소 스크래퍼."""

    PAGE_PARAM = None
    SUPPORTS_PAGINATION = False

    def __init__(self, key, posts, enrich=None, list_error=None):
        self.key = key
        self.name = key
        self._posts = posts
        self._enrich = enrich
        self._list_error = list_error

    @property
    def paginates(self):
        return False

    def fetch_list(self, limit, page=1):
        if self._list_error:
            raise self._list_error
        return self._posts if page == 1 else []

    def enrich(self, post):
        if self._enrich:
            self._enrich(post)


def _post(key="assembly_bill"):
    return Post(
        source_key=key,
        source_name=key,
        post_id="1",
        title="제목",
        url="https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId=PRC_A1",
    )


def _run(key, enrich=None, posts=None, do_enrich=True):
    sc = _Scraper(key, posts if posts is not None else [_post(key)], enrich)
    return verify_source(sc, 30, do_enrich)[0]


# --- 의안: available 검증과 pending 검증을 따로 한다 ---


def _bills(n=3):
    return [_post("assembly_bill") for _ in range(n)]


def _statuses(*states):
    """표본 각 건에 지정한 상태를 부여하는 enrich 를 만든다."""
    it = iter(states)

    def _enrich(p):
        st = next(it)
        p.proposal_status = st
        if st is S.AVAILABLE:
            p.body = "제안이유 및 주요내용 " * 20

    return _enrich


def test_assembly_all_available_is_ok():
    r = _run("assembly_bill", enrich=_statuses(S.AVAILABLE, S.AVAILABLE, S.AVAILABLE),
             posts=_bills())
    assert r["status"] == OK
    assert r["detail_ok"] is True
    assert r["proposal_counts"] == {
        "sampled": 3, "available": 3, "pending": 0, "failed": 0
    }


def test_assembly_mixed_available_and_pending_is_ok():
    """등록 대기가 섞여 있어도 available 이 하나라도 있으면 추출은 확인된 것이다."""
    r = _run("assembly_bill", enrich=_statuses(S.PENDING, S.AVAILABLE, S.PENDING),
             posts=_bills())
    assert r["status"] == OK
    assert r["proposal_counts"]["available"] == 1
    assert r["proposal_counts"]["pending"] == 2
    assert "available 1 / pending 2" in r["enrich"]


def test_assembly_all_pending_is_partial_not_fail():
    """전부 등록 대기 = 고장은 아니지만 available 을 확인하지 못했다."""
    r = _run("assembly_bill", enrich=_statuses(S.PENDING, S.PENDING, S.PENDING),
             posts=_bills())
    assert r["status"] == PARTIAL          # ❌ 가 아니다
    assert r["detail_ok"] is False
    assert "모두 등록 대기" in r["detail"]
    assert r["proposal_counts"]["failed"] == 0


def test_assembly_any_error_is_fail_even_with_available():
    """수집 실패가 섞이면 등록 대기와 달리 구조가 깨진 것이므로 실패다."""
    r = _run("assembly_bill", enrich=_statuses(S.AVAILABLE, S.ERROR, S.PENDING),
             posts=_bills())
    assert r["status"] == FAIL
    assert "제안이유 수집 실패 1건" in r["detail"]


def test_assembly_all_error_is_fail():
    r = _run("assembly_bill", enrich=_statuses(S.ERROR, S.ERROR, S.ERROR),
             posts=_bills())
    assert r["status"] == FAIL
    assert r["proposal_counts"]["failed"] == 3


def test_assembly_enrich_exception_counts_as_failed():
    def _boom(p):
        raise RuntimeError("HTTP 400")

    r = _run("assembly_bill", enrich=_boom, posts=_bills())
    assert r["status"] == FAIL
    assert r["proposal_counts"]["failed"] == 3


def test_assembly_unknown_status_counts_as_failed():
    # enrich 가 상태를 남기지 않으면(구현 누락) 성공으로 세면 안 된다.
    r = _run("assembly_bill", enrich=lambda p: None, posts=_bills())
    assert r["status"] == FAIL
    assert r["proposal_counts"]["failed"] == 3


def test_assembly_attachments_alone_do_not_count_as_available():
    # 의안은 첨부가 아니라 '제안이유 본문'이 요약 입력이다.
    def _att(p):
        p.proposal_status = S.ERROR
        p.attachments.append(Attachment(filename="x.pdf", url="https://x/x.pdf"))

    r = _run("assembly_bill", enrich=_att, posts=_bills())
    assert r["status"] == FAIL


def test_assembly_list_failure_is_still_fail():
    sc = _Scraper("assembly_bill", [], list_error=RuntimeError("Open API 오류"))
    r = verify_source(sc, 30, True)[0]
    assert r["status"] == FAIL


def test_assembly_not_penalised_when_enrich_skipped():
    # --no-enrich 로 돌리면 상세를 보지 않았으므로 부분실패로 낙인찍지 않는다.
    r = _run("assembly_bill", do_enrich=False)
    assert r["status"] == OK


# --- 다른 소스: 기존 판정 기준 유지 ---


def test_pdf_only_source_stays_ok_without_body():
    # fss_mgmt_notice 는 상세가 PDF 직접 다운로드라 body 가 비는 것이 정상.
    def _att(p):
        p.attachments.append(Attachment(filename="x.pdf", url="https://x/x.pdf"))

    r = _run("fss_mgmt_notice", enrich=_att)
    assert r["status"] == OK


def test_structured_detail_source_stays_ok_without_body():
    # fss_sanction 은 details(구조화 항목)만 채우는 것이 정상.
    def _details(p):
        p.details = [("금융기관명", "A은행")]

    r = _run("fss_sanction", enrich=_details)
    assert r["status"] == OK


def test_other_source_with_no_detail_still_ok_but_flagged():
    # 기존 기준: 표식을 선언하지 않은 소스는 상세가 비어도 status 는 OK 로 두고
    # enrich_ok 로만 알린다(훅이 없는 스크래퍼는 종전대로 첫 글을 표본으로 쓴다).
    r = _run("fss_mgmt_notice", enrich=lambda p: None)
    assert r["status"] == OK
    assert r["enrich_ok"] is False


# --- 회신사례: 상세 표본 선정과 본문 표식 검증 ---


class _MixedScraper(_Scraper):
    """상세 수집 대상과 비대상이 섞인 소스(회신사례)를 흉내낸다."""

    def supports_enrich(self, post):
        return "Detail.do" in post.url


def _reply_post(url):
    return Post(source_key="better_reply", source_name="회신사례", post_id=url,
                title="제목", url=url)


_LIST = "https://better.fsc.go.kr/fsc_new/replyCase/TotalReplyList.do?stNo=11"
_DETAIL = "https://better.fsc.go.kr/fsc_new/replyCase/LawreqDetail.do?lawreqIdx=1"


def _run_mixed(posts, enrich=None):
    sc = _MixedScraper("better_reply", posts, enrich)
    return verify_source(sc, 30, True)[0]


def test_reply_picks_supported_post_as_detail_sample():
    """첫 글이 미지원 유형이어도 지원 유형을 찾아 상세 파서를 검증한다."""
    enriched = []

    def _enrich(p):
        enriched.append(p.url)
        p.body = "[질의요지]\n질의\n\n[회답]\n회답\n\n[이유]\n이유"

    r = _run_mixed([_reply_post(_LIST), _reply_post(_DETAIL)], _enrich)
    assert enriched == [_DETAIL]          # 목록 URL 글이 아니라 상세 글을 표본으로
    assert r["status"] == OK
    assert r["detail_ok"] is True


def test_reply_without_supported_sample_is_partial():
    """지원 유형이 하나도 없으면 정상 성공으로 가장하지 않는다."""
    r = _run_mixed([_reply_post(_LIST), _reply_post(_LIST + "&x=2")])
    assert r["status"] == PARTIAL
    assert r["enrich_ok"] is False
    assert "상세 검증 표본 없음" in r["enrich"]


def test_reply_with_partial_body_is_partial():
    """세 표식이 다 없으면(부분 본문이라 body 가 비워진 경우 포함) 부분 실패다."""

    def _enrich(p):
        p.body = ""      # 회답·이유 누락 → 스크래퍼가 본문을 비운 상태

    r = _run_mixed([_reply_post(_DETAIL)], _enrich)
    assert r["status"] == PARTIAL
    assert r["detail_ok"] is False
    assert "[질의요지]" in r["detail"] and "[회답]" in r["detail"]
