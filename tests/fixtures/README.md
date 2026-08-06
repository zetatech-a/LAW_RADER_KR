# tests/fixtures

실제 사이트 응답을 캡처해 두는 곳. 네트워크 없이 회귀 테스트를 돌리기 위한 것이다.

## assembly_detail_*.html — 아직 캡처되지 않음

의안 상세('제안이유 및 주요내용') 수집은 **아직 실제 응답으로 검증되지 않았다.**
개발 환경에서 `likms.assembly.go.kr` 접속이 차단되어 캡처하지 못했다.

한국 네트워크 또는 GitHub Actions 러너에서 아래를 한 번 실행하고, 생성된 파일을
눈으로 확인한 뒤 커밋하면 `tests/test_assembly_fixture.py` 가 자동으로 활성화된다.

```bash
python scripts/capture_assembly_fixture.py --bill-id PRC_XXXXXXXXXXXX
```

생성물:

| 파일 | 내용 |
| --- | --- |
| `assembly_detail_get.html` | 상세 GET 응답(민감값 치환됨) |
| `assembly_detail_post.html` | 후속 요청 응답(별도 요청이 필요한 구조일 때만) |
| `assembly_capture_meta.json` | 최종 URL·redirect·form 목록·CSRF 전달 방식·셀렉터 적중 여부 |

## 커밋 전 확인

캡처 스크립트가 세션값·토큰·개인정보(주민번호/휴대전화/이메일 형태)를 치환하지만
**알려진 패턴만** 덮는다. 커밋 전에 반드시 파일을 열어 확인할 것.
