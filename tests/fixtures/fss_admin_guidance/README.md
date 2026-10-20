# tests/fixtures/fss_admin_guidance — 금감원 행정지도 예고 상세

## live/seqno_280.html — 실제 응답 (seqno=280)

2026-10-20 사용자가 제공한 실제 금감원 행정지도 예고 상세 페이지 원본(서버 소스 저장본, 세션·CSRF
토큰 없음)입니다. 게시물은 「온투업권 스탁론 리스크 관리 방안」 연장 예고입니다. **6번 항목의 담당자
성명·직통번호·팩스번호·이메일 4개만** 같은 형태의 가짜 값(홍길동 / 02-0000-0001 / 02-0000-0009 /
contact1@example.com)으로 바꿨고, 나머지는 바이트 그대로입니다(달라진 줄은 그 값 한 줄).
페이지 하단 '정보관리 담당부서 안내'의 부서 번호와 대표전화는 기관 번호라 두었습니다.

확인된 실제 구조(세칙 예고의 `dl.form-group > dt / dd` 와 다름):

```html
<div class="contents">
  <div class="info-cmt">금융감독원은 시행·변경·연장 예정인 행정지도 사항을 … (안내문)</div>
  <div class="krds-bd-view">
    <div class="sub-info"><h3 class="subject">「…」 연장 예고</h3></div>
    <dl class="attach-file-box">첨부파일 …</dl>
    <div class="n-dbdata">
      1. 행정지도의 명칭 <br> - … <br><br>
      2. 행정지도의 신규·변경·연장의 취지 <br> - … <br><br>
      …
      6. 접수부서, 담당자, 전화번호, 팩스번호 <br> - … <br>
    </div>
  </div>
  <div class="page-btn-wrap">목록 / 행정지도에 대한 의견제출</div>
  <div class="adm-info">정보관리 담당부서 안내 …</div>
</div>
```

`#content` 는 없고(주석 `<!-- //#content -->` 만 남음) 기존 공통 selector(`.view-cont`·`.board-view`·
`.bbs-view`·`.cont`·`#content`)가 하나도 맞지 않아, 이전에는 본문이 비어 메일에 제목·날짜·첨부·링크만
실렸습니다.

다른 게시물을 다시 캡처하려면(메일·state·Gemini 사용 없음):

```bash
python scripts/preview_fss_rule.py \
  --url "https://www.fss.or.kr/fss/job/admnPrvntc/view.do?seqno=<번호>&menuNo=200491&pageIndex=1" \
  --save-html tests/fixtures/fss_admin_guidance/live/seqno_<번호>.html
```

커밋 전에 담당자 연락처를 위와 같이 가짜 값으로 바꾸세요.
