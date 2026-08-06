# tests/fixtures

실제 사이트 응답을 캡처해 두는 곳. 네트워크 없이 회귀 테스트를 돌리기 위한 것이다.

## assembly_detail_*.html — 아직 캡처되지 않음

의안 상세('제안이유 및 주요내용') 수집은 **아직 실제 응답으로 검증되지 않았다.**
개발 환경에서 `likms.assembly.go.kr` 접속이 차단되어 캡처하지 못했다.

한국 네트워크 또는 GitHub Actions 러너에서 아래를 한 번 실행하고, 생성된 파일을
눈으로 확인한 뒤 커밋하면 `tests/test_assembly_fixture.py` 가 자동으로 활성화된다
(skip 5건 → 0건).

```bash
python scripts/capture_assembly_fixture.py --bill-id PRC_XXXXXXXXXXXX
# 또는 상세 URL 을 직접 알고 있으면
python scripts/capture_assembly_fixture.py --url "https://likms.assembly.go.kr/..."
```

GitHub Actions 에서는 **Verify sources (live)** 워크플로의 `capture_bill_id` 또는
`capture_bill_url` 입력으로 같은 일을 할 수 있다. 캡처 직후 fixture 테스트까지
자동으로 돌고, 실패해도 결과는 `verify-results` 아티팩트로 올라온다.

### 종료 코드

| 코드 | 뜻 |
| --- | --- |
| `0` | 제안이유를 확보했다(GET 응답 또는 후속 요청 응답). 검증 성공 |
| `2` | 응답은 받았지만 제안이유를 찾지 못했다. **fixture 와 meta 는 저장되며** 진단이 출력된다 |

`2` 로 끝나도 저장은 되므로, 그 파일을 열어 실제 컨테이너·폼을 확인하고
`src/scrapers/assembly.py` 의 `_SUMMARY_SELECTORS` 나 `_summary_form` 판정을 고치면 된다.

### 생성물

| 파일 | 내용 |
| --- | --- |
| `assembly_detail_get.html` | 상세 GET 응답(민감값 치환됨) |
| `assembly_detail_post.html` | 후속 요청 응답(별도 요청을 실제로 보냈고 응답이 있을 때만) |
| `assembly_capture_meta.json` | 최종 URL·redirect·HTTP 상태·form 목록·CSRF 전달 방식·셀렉터 적중 여부·후속 요청 형태 |

`assembly_capture_meta.json` 에는 **값이 아니라 이름만** 담긴다
(`follow_up_request.data_keys` / `header_keys`, `csrf_meta.has_token`).
토큰·세션값은 저장소에 들어가지 않는다.

## 커밋 전 확인

캡처 스크립트가 세션값·토큰·개인정보(주민번호/휴대전화/이메일 형태)를 치환하지만
**알려진 패턴만** 덮는다. 커밋 전에 반드시 파일을 열어 확인할 것.
`tests/test_assembly_fixture.py::test_fixture_and_meta_contain_no_secret_values` 가
한 번 더 검사하지만, 그것도 알려진 표식만 본다.
