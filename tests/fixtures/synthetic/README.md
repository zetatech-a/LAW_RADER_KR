# tests/fixtures/synthetic

**손으로 만든 구조 fixture입니다. 캡처한 실제 응답이 아닙니다.**

여기 파일은 `ProposalContentStatus` 판정기(AVAILABLE / PENDING / ERROR)를 검증하기
위한 최소 HTML입니다. 실제 사이트의 마크업을 그대로 옮긴 것이 아니므로,
**이 파일들이 통과한다고 해서 라이브 계약이 검증된 것은 아닙니다.**

라이브 검증은 `tests/fixtures/`(상위 디렉터리)의 캡처 fixture로 합니다 —
`scripts/capture_assembly_network.py` / `capture_assembly_fixture.py` 참고.

| 파일 | 재현하는 상황 | 기대 판정 |
| --- | --- | --- |
| `bill_available.html` | 제안이유가 상세 HTML에 실려 있음 | `AVAILABLE` |
| `bill_pending.html` | 접수 직후 — 컨테이너는 있으나 내용이 없음 | `PENDING` |
| `bill_pending_notice.html` | 컨테이너에 '등록 대기' 안내만 있음 | `PENDING` |
| `bill_selector_changed.html` | 알려진 컨테이너가 아예 없음(구조 변경) | `ERROR` |
| `bill_not_found.html` | '해당 의안 정보가 존재하지 않습니다' | `ERROR` |
