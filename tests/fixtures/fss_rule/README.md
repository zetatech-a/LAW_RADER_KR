# tests/fixtures/fss_rule — 금감원 세칙 제·개정 예고 상세

## synthetic_*.html — 손으로 만든 구조 fixture (캡처 아님)

**실제 응답을 캡처한 것이 아닙니다.** 개발 환경에서 `www.fss.or.kr` 접속이 egress
정책으로 차단(CONNECT 403)되어 세칙 예고 상세의 실제 마크업을 한 번도 받지 못했습니다.
그래서 추출기(`src/scrapers/fss_rule.py`)는 특정 class 를 추측하지 않고 **항목 제목 문구**
(`2. 규정의 제·개정 취지` 등)로 구조를 찾으며, 아래 fixture 는 그 방식이 여러 마크업
형태에서 성립하는지 확인하는 용도입니다. **이 파일들이 통과한다고 해서 라이브 마크업이
검증된 것은 아닙니다.**

| 파일 | 재현하는 형태 |
| --- | --- |
| `synthetic_inline.html` | 개편 페이지 껍데기(메뉴·메타·첨부 상자·이전/다음글·꼬리말) + `<br>` 로 줄을 나눈 원문 한 덩어리. 2·3·5번 문구는 작업 지시서의 #4405 레이아웃 예시 |
| `synthetic_table.html` | th(제목)/td(값) 표. th 안 `<br>` 로 갈라진 제목, 가운뎃점 변형, `<ol>` 하위 목록, 빈 4번, 마지막 '첨부파일' 행 |

연락처·이메일·전화번호는 모두 가짜(`example.com`, `02-0000-xxxx`)입니다.

## live/ — 실제 응답 캡처 (아직 없음)

인터넷 접속이 되는 환경(개인 PC 또는 GitHub Actions 러너)에서 아래 한 줄로 캡처합니다.
메일을 보내지 않고 `state/seen.json` 도 건드리지 않으며 Gemini 도 호출하지 않습니다.

```bash
python scripts/preview_fss_rule.py \
  --url "https://www.fss.or.kr/fss/job/lrgRegItnPrvntc/view.do?lrgSlno=4405&menuNo=200489&pageIndex=1" \
  --save-html tests/fixtures/fss_rule/live/lrgSlno_4405.html
```

파일이 생기면 `tests/test_fss_rule_amendment.py` 의 라이브 fixture 테스트(현재 skip)가
자동으로 돌아 4개 항목 추출·1/4번 제외·3번 하위 번호 보존·원문 일치(추출값의 모든 줄이
페이지 원문에 그대로 있는지)를 검사합니다. 커밋 전에 파일을 열어 개인정보(담당자 연락처)가
공개 게시물 범위인지 확인하세요.
