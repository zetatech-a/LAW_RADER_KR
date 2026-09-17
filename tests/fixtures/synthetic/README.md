# tests/fixtures/synthetic

**손으로 만든 구조 fixture입니다. 캡처한 실제 응답이 아닙니다.**

여기 파일은 확정된 billInfo.do 계약과 `ProposalContentStatus` 판정기
(AVAILABLE / PENDING / ERROR)를 검증하기 위한 최소 HTML입니다.
실제 사이트의 마크업을 그대로 옮긴 것이 아니므로,
**이 파일들이 통과한다고 해서 라이브 계약이 검증된 것은 아닙니다.**

라이브 검증은 `tests/fixtures/`(상위 디렉터리)의 캡처 fixture로 합니다 —
`scripts/capture_assembly_network.py` / `capture_assembly_fixture.py` 참고.

| 파일 | 재현하는 상황 | 기대 판정 |
| --- | --- | --- |
| `bill_detail_page.html` | 확정 계약의 상세페이지(form#form + meta csrf, 제안이유 없음) | 요청 조립용 |
| `billinfo_available.html` | 정상 shell + `#prntsummary-sect` + `pre#prntSummary` 본문 | `AVAILABLE` |
| `billinfo_pending.html` | **정상 shell 만 있고 제안이유 섹션이 아예 없음**(라이브 확인된 등록 대기 구조) | `PENDING` |
| `billinfo_section_without_pre.html` | `#prntsummary-sect` 는 있는데 `pre` 만 없음 | `ERROR` |
| `billinfo_marker_without_selector.html` | 제안이유 표식은 있는데 예상 selector 없음 | `ERROR` |
| `billinfo_malformed_shell.html` | 정상 심사정보 shell 자체가 없음 | `ERROR` |
| `bill_available.html` | 구형 페이지 — 제안이유가 상세 HTML에 실려 있음 | `AVAILABLE` |
| `bill_not_found.html` | '해당 의안 정보가 존재하지 않습니다' | `ERROR` |
| `pipc_notice_detail.html` | PIPC 공지사항 상세 — 본문 `.bbs-view-cont` + 메뉴·메타·첨부·이전다음글·만족도·푸터 | 본문만 추출 |
| `pipc_press_detail.html` | PIPC 보도자료 상세 — 본문 `.view-cont` + `javascript:fn_egov_downFile` 첨부(HWPX/PDF) | 본문만 추출 + 첨부 2건 |

토큰 값은 `SYNTHETIC-TOKEN-NOT-REAL` 자리표시자이며 실제 세션값이 아닙니다.

PIPC 두 파일은 **캡처 시도조차 하지 못했습니다** — 개발 환경에서 `pipc.go.kr` 접속이
egress 정책으로 차단(CONNECT 403)되었습니다. 배포 후 실제 응답을 확인해
`config.yaml` 의 `body_selectors` 로 바로잡을 수 있습니다(README 참고).
