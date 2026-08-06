"""verify_sources 의 성공/부분실패 판정 테스트(네트워크 없음).

핵심: 의안(assembly_bill)은 목록만 되고 상세 본문이 0자면 '전체 성공'이 아니다.
그 상태를 초록불로 넘기면 메일에 제목·링크만 실리는 채로 운영이 계속된다.
다른 소스는 상세가 PDF·JS 팝업이라 body 가 비는 것이 정상이므로 기존 기준을 유지한다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.verify_sources import FAIL, OK, PARTIAL, verify_source
from src.models import Attachment, Post


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


# --- 의안: 상세 본문이 필수 ---


def test_assembly_list_ok_but_empty_body_is_partial():
    r = _run("assembly_bill", enrich=lambda p: None)   # 아무것도 채우지 못함
    assert r["status"] == PARTIAL
    assert r["list_ok"] is True
    assert r["detail_ok"] is False
    assert "목록 수집 성공" in r["detail"] and "상세 본문 0자" in r["detail"]


def test_assembly_with_body_is_ok():
    r = _run("assembly_bill", enrich=lambda p: setattr(p, "body", "제안이유 " * 20))
    assert r["status"] == OK
    assert r["detail_ok"] is True


def test_assembly_enrich_exception_is_partial():
    def _boom(p):
        raise RuntimeError("HTTP 400")

    r = _run("assembly_bill", enrich=_boom)
    assert r["status"] == PARTIAL
    assert r["body_len"] == 0


def test_assembly_attachments_alone_do_not_count_as_detail_success():
    # 의안은 첨부가 아니라 '제안이유 본문'이 요약 입력이다.
    def _att(p):
        p.attachments.append(Attachment(filename="x.pdf", url="https://x/x.pdf"))

    r = _run("assembly_bill", enrich=_att)
    assert r["status"] == PARTIAL


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
    # 기존 기준: 다른 소스는 상세가 비어도 status 자체는 OK 로 두고 enrich_ok 로만 알린다.
    r = _run("better_reply", enrich=lambda p: None)
    assert r["status"] == OK
    assert r["enrich_ok"] is False
