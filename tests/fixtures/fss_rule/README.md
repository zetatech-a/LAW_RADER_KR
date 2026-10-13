# tests/fixtures/fss_rule — 금감원 세칙 제·개정 예고 상세

## synthetic_*.html — 손으로 만든 구조 fixture (캡처 아님)

**실제 응답을 캡처한 것이 아닙니다.** 추출기(`src/scrapers/fss_rule.py`)는 특정 class 를
추측하지 않고 **항목 제목 문구**(`2. 규정의 제·개정 취지` 등)로 구조를 찾으며, 아래 fixture 는
그 방식이 실제와 다른 마크업 형태(마크업이 다시 바뀌는 경우)에서도 성립하는지 확인하는
용도입니다. 실제 구조 검증은 아래 `live/` 로 합니다.

| 파일 | 재현하는 형태 |
| --- | --- |
| `synthetic_inline.html` | 개편 페이지 껍데기(메뉴·메타·첨부 상자·이전/다음글·꼬리말) + `<br>` 로 줄을 나눈 원문 한 덩어리. 2·3·5번 문구는 작업 지시서의 #4405 레이아웃 예시 |
| `synthetic_table.html` | th(제목)/td(값) 표. th 안 `<br>` 로 갈라진 제목, 가운뎃점 변형, `<ol>` 하위 목록, 빈 4번, 마지막 '첨부파일' 행 |

연락처·이메일·전화번호는 모두 가짜(`example.com`, `02-0000-xxxx`)입니다.

## live/lrgSlno_4405.html — 실제 응답 (#4405)

2026-10-13 사용자가 제공한 실제 금감원 상세 페이지 원본(서버 소스 저장본 — 브라우저가 고쳐 쓴
DOM 이 아님, 세션·CSRF 토큰 없음)입니다. **담당자 성명 3개·이메일 3개·직통/팩스 번호 4개만**
같은 형태의 가짜 값(홍길동 / contact1@example.com / 02-0000-0001 …)으로 바꿨고, 나머지는
바이트 그대로입니다(달라진 줄은 6번 항목 값 한 줄). 대표전화(02-3145-5114)는 기관 번호라 두었습니다.

확인된 실제 구조:

```html
<div class="krds-reg reg2">
  <dl class="form-group">
    <dt class="form-tit">2. 규정의 제∙개정 취지</dt>   <!-- 가운뎃점은 U+2219 -->
    <dd class="form-conts">…원문…</dd>                 <!-- 줄은 <br> 로 나뉨 -->
  </dl>
  … (1~6번이 각각 dl.form-group)
</div>
<div class="krds-bd-view …"><dl class="attach-file-box">첨부파일…</dl></div>
```

본문 영역은 `div.contents` 안에 있으며 `#content` 는 없습니다(주석 `<!-- //#content -->` 만 남음).
그래서 기존 공통 selector(`.view-cont`·`.board-view`·`.bbs-view`·`.cont`·`#content`)가 하나도
맞지 않아 운영 #4405 메일의 본문이 비었습니다.

다른 게시물을 다시 캡처하려면(메일·state·Gemini 사용 없음):

```bash
python scripts/preview_fss_rule.py \
  --url "https://www.fss.or.kr/fss/job/lrgRegItnPrvntc/view.do?lrgSlno=<번호>&menuNo=200489&pageIndex=1" \
  --save-html tests/fixtures/fss_rule/live/lrgSlno_<번호>.html
```

커밋 전에 담당자 연락처를 위와 같이 가짜 값으로 바꾸세요.
