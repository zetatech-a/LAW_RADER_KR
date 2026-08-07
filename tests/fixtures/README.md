# tests/fixtures

실제 사이트 응답을 캡처해 두는 곳. 네트워크 없이 회귀 테스트를 돌리기 위한 것이다.

## assembly/ — 아직 캡처되지 않음

의안 상세('제안이유 및 주요내용') 수집은 **아직 실제 응답으로 검증되지 않았다.**
개발 환경에서 `likms.assembly.go.kr` 접속이 차단되어 캡처하지 못했다.

AVAILABLE 과 PENDING 을 **각각** 캡처한다(서로 덮어쓰지 않는다).

```bash
# 제안이유가 이미 있는 의안
python scripts/capture_assembly_fixture.py --expect available --bill-id PRC_XXXX
# 방금 접수돼 제안이유가 아직 없는 의안
python scripts/capture_assembly_fixture.py --expect pending   --bill-id PRC_YYYY
# URL 로도 지정할 수 있다(billId 는 URL 에서 추출·검증된다)
python scripts/capture_assembly_fixture.py --expect available \
  --url "https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId=PRC_XXXX"
```

생성물:

```
tests/fixtures/assembly/available/{detail.html, billinfo.html, meta.json}
tests/fixtures/assembly/pending/  {detail.html, billinfo.html, meta.json}
```

| 파일 | 내용 |
| --- | --- |
| `detail.html` | 상세 GET 응답(민감값 치환됨) |
| `billinfo.html` | `billInfo.do` POST 응답(요청을 실제로 보냈을 때만) |
| `meta.json` | 최종 URL·redirect·HTTP 상태·form 목록·CSRF 유무·기대/실제 상태·후속 요청 형태 |

`meta.json` 에는 **값이 아니라 이름만** 담긴다
(`follow_up_request.data_keys` / `header_keys`, `csrf_meta.has_token`).
토큰·세션값은 저장소에 들어가지 않는다.

캡처 후 `tests/test_assembly_fixture.py` 의 skip 8건이 0이 된다.
`ASSEMBLY_FIXTURE_DIR` 환경변수로 루트를 바꿔 임시 디렉터리로 검증할 수도 있다.

### 종료 코드

| 코드 | 뜻 |
| --- | --- |
| `0` | 기대 상태(`--expect`)와 실제 판정이 일치 |
| `2` | 기대와 다름. **fixture 와 meta 는 저장되며** 진단이 출력된다 |
| `3` | 캡처 대상이 잘못됨(billId 없음/불일치, 비HTTPS, 다른 호스트) — 요청 전에 중단 |

## 커밋 전 확인

캡처 스크립트가 세션값·토큰·개인정보(주민번호/휴대전화/이메일 형태)를 치환하지만
**알려진 패턴만** 덮는다. 커밋 전에 반드시 파일을 열어 확인할 것.
`tests/test_assembly_fixture.py::test_committed_fixture_files_contain_no_session_values`
가 한 번 더 검사하지만, 그것도 알려진 표식만 본다.

## synthetic/

손으로 만든 구조 fixture(캡처 아님). 상태 판정기 검증용 — 그 디렉터리 README 참고.
