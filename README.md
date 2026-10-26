# LAW_RADER_KR

**RADER** stands for **R**egulatory **A**lert **D**etection & **E**mail **R**eporter.

Note: RADER is an intentional acronym, not a misspelling of RADAR.

한국 금융 규제·입법 관련 게시판을 주기적으로 확인해, **새로 올라온 글의 URL·내용·첨부파일을 이메일로 자동 발송**하는 모니터링 프로그램입니다.

## 모니터링 대상 (11곳)

| key | 사이트 | 게시판 | 수집 방식 |
|---|---|---|---|
| `fsc_press` | 금융위원회 | 보도자료 | HTML |
| `fsc_legislation` | 금융위원회 | 입법예고/규정변경예고 | HTML |
| `fss_press` | 금융감독원 | 보도자료 | HTML |
| `fss_admin_guidance` | 금융감독원 | 행정지도 예고 | HTML |
| `fss_rule_amendment` | 금융감독원 | 세칙 제·개정 예고 | HTML |
| `fss_sanction` | 금융감독원 | 검사결과 제재 | HTML |
| `fss_mgmt_notice` | 금융감독원 | 경영유의사항 등 공시 | HTML |
| `better_reply` | 금융규제·법령해석포털 | 법령해석·비조치의견서 회신사례 | JSON (POST, 목록) + 상세페이지(질의요지·회답·이유·첨부) |
| `pipc_notice` | 개인정보보호위원회 | 공지사항 | HTML |
| `pipc_press` | 개인정보보호위원회 | 보도자료 | HTML |
| `assembly_bill` | 의안정보시스템 | 계류의안 | 열린국회 Open API (목록) + 상세페이지(제안이유 및 주요내용) |

목록 파서는 실제 사이트 HTML/응답으로 검증되어 제목·URL·날짜·본문·첨부를 수집합니다.
(`fss_mgmt_notice` 는 상세가 PDF 직접 다운로드. `better_reply` 는 상세 주소가 확인된 **법령해석·비조치의견서**만 본문·첨부를 수집하고, 상세 주소가 확인되지 않은 구분(현장건의 과제 등)은 종전대로 목록 링크로 안내합니다)

**`pipc_notice`·`pipc_press` 는 라이브 HTML 로 검증되지 않았습니다.** 두 소스를 추가한 개발 환경에서 `pipc.go.kr` 접속이 egress 정책으로 차단(CONNECT 403)되어 실제 응답을 한 번도 받지 못했습니다. 자세한 범위와 배포 후 확인 절차는 아래 **[개인정보보호위원회(PIPC) 두 소스 — 라이브 미검증 범위](#개인정보보호위원회pipc-두-소스--라이브-미검증-범위)** 를 반드시 먼저 읽으세요.

> **확정된 수집 계약** (2026-08 Playwright 라이브 캡처):
>
> ```
> 1) GET  https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId={BILL_ID}
>          → 초기 HTML 에는 제안이유가 없다. form#form 과 meta[name="_csrf"] 만 있다.
> 2) POST https://likms.assembly.go.kr/bill/bi/bill/detail/billInfo.do
>          payload : form#form 의 named hidden input 전체 (URL-encoded)
>          header  : X-CSRF-TOKEN = meta[name="_csrf"] 의 content
>                    Referer     = 위 상세 URL
>          쿠키    : 상세 GET 과 같은 requests.Session
>          응답    : HTML — 제안이유가 등록돼 있으면 #prntsummary-sect 안의
>                    pre#prntSummary 에 본문이 있다. **등록 전이면 그 섹션이 아예
>                    생성되지 않는다**(응답 자체는 HTTP 200 정상 심사정보 HTML 이고
>                    의안번호·제안일자·제안자는 정상).
> ```
>
> `form#form` 은 **payload 원천일 뿐**입니다. 그 폼의 빈 `action` 과 기본 GET 을 그대로 replay 하면 안 됩니다 — JavaScript 가 폼을 serialize 한 뒤 별도 endpoint 로 POST 하기 때문입니다(replay 했다가 `billId` 중복으로 HTTP 400 을 받았습니다).
>
> Open API 의 `LINK_URL` 은 아직 구 경로 `/bill/billDetail.do` 를 주는데 최신 의안에서 그 경로는 "해당 의안 정보가 존재하지 않습니다"를 응답합니다(redirect 없음). 그래서 `BILL_ID` 로 현재 경로를 다시 만듭니다.
>
> 요청을 만들 수 없으면(`form#form` 없음 / CSRF 없음 / 폼의 `billId` 가 목록의 `BILL_ID` 와 다름) **요청을 보내지 않고** `ERROR` 로 둡니다 — 근거가 어긋난 요청은 남의 의안 본문을 받거나 400 을 반복할 뿐입니다.

### 제안이유 상태 (`ProposalContentStatus`)

**본문이 없는 것과 수집이 실패한 것은 다른 사건입니다.** 갓 접수된 의안은 원문이 아직 공개되지 않아 본문이 비는 것이 정상인데, 이를 실패로 세면 매 실행마다 거짓 ERROR 가 쌓여 진짜 고장을 가립니다. 그래서 의안마다 네 가지 상태를 남깁니다.

| 상태 | 뜻 | 메일 표시 | 집계 |
|---|---|---|---|
| `AVAILABLE` | 제안이유를 확보함 | AI 3줄 요약 (실패 시 발췌) | available |
| `PENDING` | **정상 billInfo 응답인데 제안이유 섹션이 아직 없거나 비어 있음** = 등록 대기 | `제안이유 및 주요내용 · 등록 대기` + "의안정보시스템에 제안이유 및 주요내용이 아직 공개되지 않았습니다." | pending (**실패 아님**) |
| `ERROR` | 네트워크·HTTP 오류, 마크업 변경, 정상 응답 구조 자체가 아님 | 제목·링크만 | failed |
| `UNKNOWN` | 아직 판정 전(기본값) | — | failed |

`PENDING` 은 **정상 심사정보 뼈대(`#tab_billInfo_sect`·`form#billInfoForm`·`#stage_list`·`#rcp_list`·`#insc-rcp-row`)가 온전한데 제안이유 섹션만 없거나 비어 있을 때** 확정합니다. 라이브 확인 결과 등록 전에는 `#prntsummary-sect` 와 `pre#prntSummary` 가 아예 생성되지 않기 때문입니다.

다음은 모두 `ERROR` 입니다 — 등록 대기로 넘기면 고장이 '정상'으로 위장됩니다.

- `#prntsummary-sect` 는 있는데 `pre#prntSummary` 만 없음 (마크업 변경)
- 응답에 `제안이유 및 주요내용` 표식은 있는데 예상 selector 가 없음 (마크업 변경)
- 정상 심사정보 뼈대 자체가 없음 (기대한 응답이 아님)
- `pre#prntSummary` 에 20자 미만의 잔여 텍스트만 있음
- `pre#prntSummary` 가 비어 있는데 **정상 심사정보 뼈대가 없음** — 빈 `pre` 하나만 남은 오류·중간 페이지를 등록 대기로 인정하면 덤프도 남지 않고 실패 집계에도 잡히지 않은 채 그 의안이 `seen` 으로 확정되어, 제안이유를 영영 받지 못합니다

반대로 `AVAILABLE`(본문 20자 이상 확보)은 뼈대를 요구하지 않습니다. 본문이 이미 손에 있는데 주변 마크업이 바뀌었다는 이유로 정상 수집을 실패로 뒤집을 이유가 없습니다.

'소관위 미확정', '문서 없음', '제안일이 오늘' 같은 정황은 등록 여부와 직접 관계가 없으므로 근거로 쓰지 않습니다.

**전면 장애 때 상세 단계가 폭주하지 않도록** 연속 실패 상한(기본 5건)과 시간예산(코드 기본 120초, 운영 `config.yaml` 은 360초)을 둡니다. 사이트가 죽으면 신규 의안 하나하나가 타임아웃×재시도를 각자 겪어 상세 단계만 수십 분을 먹고 다음 15분 주기 실행까지 밀리기 때문입니다. 둘 중 하나라도 걸리면 남은 의안은 **요청 없이** `ERROR` 로 떨어지고 메일은 그대로 나갑니다. 등록 대기는 실패로 세지 않으므로 갓 접수된 의안이 몰린 날 브레이커가 헛돌지 않습니다(`config.yaml` 의 `detail_max_consecutive_failures` / `detail_budget_sec`, 0 이면 해제).

**받아온 페이지가 그 의안의 것인지 먼저 확인합니다.** Open API 의 `LINK_URL` 이 다른 의안을 가리키거나 redirect 가 걸리면 A 의안 알림에 B 의안의 제안이유가 실릴 수 있기 때문입니다. 근거는 `form#form` 의 `billId`, 요청 URL 의 `billId`, 최종 응답 URL 의 `billId` 셋이며, 하나라도 어긋나면 `ERROR` 입니다. 구형 페이지의 inline 제안이유는 이 확인을 **통과했을 때만** 채택합니다(근거가 하나도 없으면 채택하지 않습니다 — `billInfo.do` 경로는 요청을 만들 때 `billId` 를 대조하지만 inline 경로는 그 검사를 거치지 않고 바로 발행되기 때문입니다).

진단 덤프(`debug/`)는 verify 워크플로가 아티팩트로 업로드하므로, 저장 전에 이름이 비밀인 `meta`/`input`(`_csrf`, `*token*`, `*session*` 등) 값과 인라인 스크립트를 지우고, **값이 URL 인 속성(`form@action`·`a@href`·`script@src` 등)** 은 URL 규칙으로 정화합니다 — 쿼리뿐 아니라 `;jsessionid=…` 경로 파라미터까지 봅니다. 태그·필드 이름과 공개 값(`billId` 등)은 진단에 필요하므로 남깁니다(덤프가 어느 의안의 것인지 알 수 없으면 쓸모가 없습니다).

`PENDING` 의안은 Gemini 배치 요약 대상에서 제외되고(요약할 원문이 없음), 의안 판정에서는 `available=0` 이어도 `pending>0` 이면 전면 실패 ERROR 를 남기지 않습니다(대신 실패가 섞여 있으면 경고).

전체 상세 수집의 `성공률 0%` 판정은 **등록 대기를 분모에서 뺀 뒤** 봅니다. '등록 대기가 하나라도 있으면 판정하지 않는다'로 두면, 갓 접수된 의안 한 건 때문에 등록 대기와 아무 관계 없는 다른 소스의 파서 전멸이 통째로 묻힙니다.

### 상세 재조회 큐와 '의안 상세 업데이트' 알림

**"알렸다"와 "상세를 확보했다"는 다른 상태입니다.** 최초 알림이 나가면 그 `BILL_ID` 는 `seen` 으로 확정되고, 다음 실행의 목록 수집은 그것을 신규로 돌려주지 않습니다. 예전에는 그 순간 상세 수집 기회도 함께 사라져, 최초 알림 때 `PENDING` 이던 의안의 제안이유가 나중에 등록돼도 **영영 가져오지 못했습니다**. 그렇다고 `PENDING` 을 `seen` 처리하지 않으면 같은 의안이 15분마다 '신규'로 다시 발송됩니다.

그래서 두 상태를 분리합니다.

| state 키 | 뜻 |
|---|---|
| `seen` | 이 의안의 **존재를 사용자에게 이미 알렸다** |
| `pending_detail` | 그런데 **제안이유 및 주요내용은 아직 확보하지 못했다** |

```jsonc
"assembly_bill": {
  "seen": ["PRC_…"],
  "baselined": true,
  "pending_detail": {                     // 선택 키 — 없으면 빈 큐(기존 파일 그대로 읽힘)
    "PRC_…": {
      "title": "…법률안 (홍길동의원)",     // 복구 알림에 쓸 원래 제목
      "url":   "https://likms.assembly.go.kr/bill/bi/billDetailPage.do?billId=PRC_…",
      "date":  "2026-08-24",
      "status": "pending",                // 마지막 판정
      "attempts": 3,
      "first_seen_at": "2026-08-24T09:00:00Z",
      "last_attempt_at": "2026-08-24T12:00:00Z",
      "note": "제안이유 섹션 없음"         // 진단용(메일에는 싣지 않음)
    }
  }
}
```

수명주기:

1. 신규 의안 → 최초 다이제스트 → **SMTP 성공** → `seen` 추가 **와 동시에** `pending_detail` 등록 (`AVAILABLE` 이 아닌 모든 상태 = `PENDING`/`ERROR`/`UNKNOWN`)
2. 다음 실행부터, 저장된 스냅샷으로 `Post` 를 최소 재구성해 **기존 `enrich()` 경로 그대로** 상세만 다시 조회합니다(목록에 다시 나타나기를 기다리지 않습니다 — 이미 `seen` 이라 목록에는 신규로 잡히지 않기 때문입니다). canonical URL·CSRF·같은 세션·`billInfo.do` POST·의안 동일성 검증·fail-closed 판정은 전부 그대로 재사용하며, 신규 상세 수집과 **같은 스크래퍼 인스턴스**를 써서 `detail_budget_sec`·연속 실패 브레이커가 신규+재조회를 합쳐 적용됩니다.
3. 여전히 `PENDING`/`ERROR`/`UNKNOWN` → 큐 유지, `attempts`·`last_attempt_at`·`status`·`note` 만 갱신(**메일 없음**)
4. `AVAILABLE` 로 복구 → '신규'가 아니라 **`의안 상세 업데이트`** 라는 별도 카테고리로 알립니다. 같은 실행의 신규가 있으면 **한 메일 안 두 섹션**으로 나갑니다.
5. 그 메일이 **성공한 뒤에만** 큐에서 지웁니다 → 다음 실행에서 재발송되지 않습니다. 발송이 실패하면 큐에 남아 다음 실행에서 다시 시도합니다.

완료 기준은 **"상세 원문을 사용자에게 전달했는가"** 이지 "AI 요약에 성공했는가"가 아닙니다. `--no-llm` 이거나 Gemini 가 실패해도 제안이유 발췌로 업데이트 메일이 나가고 큐는 정상적으로 비워집니다.

재조회는 **무제한이 아닙니다.** `detail_retry_interval_sec`(기본 1800초) 안에 이미 시도한 의안은 이번 실행에서 건너뛰고, 한 실행에서 처리할 기존 큐 항목은 `detail_retry_max_per_run`(기본 10건)까지입니다. 처리 순서는 결정적입니다(마지막 시도가 오래된 것 먼저 → `BILL_ID` 사전순). 이번 실행에서 새로 큐에 들어온 의안은 **같은 실행에서 다시 조회하지 않습니다**(큐 스냅샷을 실행 시작 시점에 잡습니다).

큐는 **자동으로 만료·삭제되지 않습니다.** 시도 횟수나 경과 일수를 이유로 조용히 지우면 그것이 곧 알림 누락이기 때문입니다. 7일 넘게 남은 항목은 경고 로그로 드러내되 삭제하지 않습니다(만료 정책은 별도 작업).

**영구 제외(`excluded_detail_ids`).** 운영자가 상세 재조회를 끝내기로 한 의안은 `assembly_bill.excluded_detail_ids`(선택 키, 없으면 빈 목록)에 둡니다. 등록은 `State.exclude_detail(source_key, ids)` 로만 하며, 제외 등록과 `pending_detail` 제거가 한 번에 일어납니다. 여기 있는 ID 는 재조회 대상에서 빠지고, `queue_detail()` 로 다시 큐에 들어가지 않으며, 목록 수집의 '이미 아는 ID' 에 포함되어 `seen` 에 없더라도 신규로 재알림되지 않습니다. 자동 만료·건수 상한은 없습니다. `seen` 은 건드리지 않습니다.

**`seen` 보관 상한.** 의안(`assembly_bill`)의 `seen` 은 상한 없이 누적합니다 — 계류 기간이 긴 의안이 상한에서 밀려나면 신규로 오인되어 최초 알림이 중복되기 때문입니다. 다른 소스는 기존대로 5,000건(`MAX_PER_SOURCE`)입니다.

의안이 `enabled: false` 이거나 `--only` 에 `assembly_bill` 이 없으면 **큐도 처리하지 않습니다** — 사용자가 실행 대상에서 제외한 사이트를 배경에서 몰래 두드리면 안 됩니다. 마찬가지로 SMTP 설정 누락·로그인 실패로 발송이 불가능하면 재조회 자체를 건너뜁니다(복구한 본문을 전달할 방법이 없는데 사이트만 두드릴 이유가 없고, 본문을 state 에 쌓아 두는 우회도 하지 않습니다).

> **과거 항목은 자동 backfill 하지 않습니다.** 이 기능이 배포되기 전에 `PENDING`/`ERROR` 상태로 `seen` 처리된 의안은 기존 state 에 실패 정보가 없습니다. 모든 과거 `seen` 의안을 다시 긁어 큐를 추측 생성하면 대량 중복 후속 알림이 발생하므로 하지 않습니다. **persistent 재조회는 배포 이후 새로 발생하는 `PENDING`/`ERROR`/`UNKNOWN` 부터 보호합니다.** 과거 누락의 backfill 은 별도의 명시적 작업입니다.

## 동작 방식

1. **Cloudflare Cron Trigger**가 월~금 09:07–18:07 (KST), 15분 간격으로 GitHub `workflow_dispatch` API에 예약 실행을 요청합니다.
   GitHub-hosted runner가 요청을 받아 Python 모니터(`python -m src.main`)를 실행합니다. Cloudflare는 예약 요청만 담당하며 Python을 실행하지 않습니다.
2. 각 게시판 목록을 수집해 **이전에 못 본 새 글**만 골라냅니다.
   - 어떤 소스를 **처음** 볼 때는 현재 목록을 "기준선"으로만 저장하고 메일을 보내지 않습니다(과거 글 폭탄 방지). 그 다음 실행부터 진짜 신규만 발송합니다.
   - 주말·공휴일에 올라온 글은 **다음 실행(월요일 첫 회차)에 모아서** 발송됩니다. "이미 본 글 ID" 기준이라 시간과 무관합니다.
3. 새 글의 **상세 본문과 첨부파일**을 내려받습니다.
4. 본문이 있는 글은 **Gemini API로 3줄 요약**해 메일에 싣습니다. 원문을 앞에서 자른 발췌 대신 핵심만 보이게 하기 위함입니다.
   - **일반 게시물**(금융위·금감원)은 1건당 1회 호출합니다.
   - 두 Gemini 단계(일반 → 의안)는 연달아 실행되므로, 각자의 예산(240 / 300) 위에 **하나의 공유 상한**(`llm.total_budget_sec`, 운영 360초)을 씌웁니다. 실행 시작에 한 번 만든 절대 마감을 두 단계가 함께 쓰므로 단순 합(540초)이 나오지 않습니다. 일반 단계가 공유 예산을 거의 다 썼다면 의안 호출을 시작하지 않고 발췌로 넘깁니다 — 시간예산 소진은 실행 실패가 아닙니다. 이렇게 해서 **의안 상세수집 ≤ 360초**와 **일반+의안 Gemini 합계 ≤ 360초**, 즉 주요 장시간 작업 상한이 약 12분에 머물러 15분 실행 주기에 목록 수집·SMTP·state 커밋 여유가 남습니다.
   - **계류의안**은 상세페이지에서 '제안이유 및 주요내용'을 수집한 뒤, 최대 **25건씩 한 번의 요청으로 묶어** 요약합니다(`llm.assembly_batch`). 의안은 하루 신규가 수십 건이라 1건당 1회로는 무료 티어 한도를 곧바로 소진하기 때문입니다. 한 실행의 상한은 75건이므로 정상 실행이라면 **최대 3회** top-level 호출입니다(1~25건 → 1회, 26~50건 → 2회, 51~75건 → 3회). 각 배치는 `request_timeout_sec`(90초)와 '남은 예산 ÷ 남은 배치 수' 중 작은 쪽을 자기 window 로 받습니다 — 첫 배치가 느려도 뒤 배치가 몇 초짜리 타임아웃으로 시작하지 않습니다. 반대로 공정하게 나눠도 한 번의 호출을 담지 못할 만큼(2초 미만) 예산이 남지 않았다면 **남은 배치 전체**를 호출 없이 발췌로 넘깁니다(앞 배치를 건너뛰고 뒤 배치만 부르면 앞의 몫을 뒤가 가져가게 됩니다). 배치 안의 분할·누락 재요청도 **그 배치의 window 안에서만** 일어나고, 전체 예산(`budget_sec`, 300초)이 최종 상한입니다. 결과는 배열 순서가 아니라 **BILL_ID로 매핑**하고, 검증(요청한 ID·중복·`llm.lines`줄·문장 길이)을 통과하지 못한 의안은 요약 없이 발췌로 나갑니다. 프롬프트의 출력 예시는 `llm.lines` 로부터 만듭니다 — 예시를 3줄로 못박아 두면 `lines` 를 바꿨을 때 규칙과 예시가 어긋나고, 모델이 예시를 따르면 검증이 **모든 의안을 버려** 배치 요약이 조용히 꺼집니다.
   - API 키(`GEMINI_API_KEY`)가 없거나 호출이 실패하면 **원문 발췌로 자동 대체**되고 메일은 정상 발송됩니다. 일반 게시물은 제목 중복과 담당부서·등록일·첨부파일 안내 같은 반복 안내문을 걷어낸 뒤, 짧은 본문은 전문을, 긴 본문은 원문 문장 최대 3개를 발췌합니다(외부 호출 없음, [AI 요약 실패 시 원문 발췌](#ai-요약-실패-시-원문-발췌) 참고).
   - **의안 발췌는 220자 한 줄이 아닙니다.** 의안 본문은 '제안이유 및 주요내용' 한 덩어리라 앞 220자에는 거의 항상 현행 제도 설명만 실리고 정작 *무엇을 바꾸는지*가 잘립니다. 그래서 의안만 원문에서 **최대 3개의 서로 다른 의미 구간**(배경 → 입법행위 → 결론)을 골라 총 900자 이내로 싣습니다. 문장 분리가 되지 않는 한 덩어리 본문은 `머리 … [중략] 꼬리` 형태로 보여줍니다. **AI 요약이 아니라 원문 그대로의 결정적(deterministic) 발췌**이므로 라벨도 '제안이유 및 주요내용 발췌'를 유지합니다. Phase 2 의 '의안 상세 업데이트' 알림도 같은 렌더러를 쓰므로 자동으로 같은 발췌를 받습니다.
   - 의안 배치 호출이 실패하면 **실패의 종류에 따라** 다르게 다룹니다. 인증(401·403)·한도(429)·잘못된 요청(400)·모델 부재처럼 다시 불러도 결과가 같은 실패는 남은 배치를 **즉시** 포기합니다. 반대로 일시 장애(408·5xx·네트워크·타임아웃)는 재시도한 뒤 **다음 배치를 한 번은 더 두드려 보고**, 연속 실패가 `llm.assembly_batch.max_consecutive_transient_failures` 에 도달할 때만 남은 배치를 포기합니다. 호출은 성공했고 **응답 '내용'이 문제인 경우**(깨진 JSON·스키마 위반·안전필터 차단 등)는 그 배치에 담긴 의안 내용에 달린 판정이라 다음 배치는 통과할 수 있으므로 멈추지 않습니다. 자세한 분류는 아래 [알려진 한계](#알려진-한계)의 실패 처리 표를 보세요.
   - **금감원 세칙 제·개정 예고**(`fss_rule_amendment`)는 **AI 요약을 쓰지 않습니다.** 상세 페이지의 `2. 규정의 제·개정 취지` · `3. 규정의 제·개정 내용` · `5. 의견제출시한(도착기준)` · `6. 접수부서, 담당자, 전화번호, 팩스번호` 4개 항목만 **원문 그대로** 뽑아(`src/scrapers/fss_rule.py`, 기존 `post.details` 재사용) 메일 카드에 금감원 강조색 • 제목(원문 번호 없이 '규정의 제개정 취지' 등) + 연한 박스로 싣습니다. 1번 명칭·4번 기타 필요사항은 싣지 않고 경계로만 씁니다. 특정 class 를 추측하지 않고 항목 제목 문구와 그 공통 조상 요소로 구조를 찾으며(th/dt 라벨이면 짝지어진 td/dd 를 그대로 사용), 제목은 원문 번호·순서가 맞을 때만 인정해 3번 안의 `1.`·`2.` 하위 목록이 잘리지 않습니다. 일부 항목이 없으면 그 자리에 누락 안내를, 전부 없으면 '상세 내용 추출 실패 — 원문 페이지에서 확인해 주세요.'를 싣고(제목·첨부·원문 링크는 유지) 원인·URL 을 warning 으로, 원본 HTML 을 `debug/` 로 남깁니다. **어떤 경우에도 일반 본문 추출이나 AI 요약으로 대체하지 않습니다**(`summarizer._prepare_body` 에서 소스 단위로 제외 — 요약 호출과 AI 집계 모두). 상세 수집 성공은 4개 항목을 모두 확보했을 때만 셉니다(첨부만으로 성공 처리하지 않음). 재시도 정책은 바꾸지 않았습니다 — 다른 일반 게시물과 같이 발송 성공 후 `seen` 으로 확정됩니다.
   - **금감원 행정지도 예고**(`fss_admin_guidance`)도 같은 방식입니다. 상세의 `2. 행정지도의 신규·변경·연장의 취지` · `3. 행정지도의 내용` · `5. 의견제출시한(도착기준)` · `6. 접수부서, 담당자, 전화번호, 팩스번호` 4개 항목만 원문 그대로 뽑아(`src/scrapers/fss_admin_guidance.py`) 세칙 예고와 같은 카드 디자인(금감원 강조색 • 제목, 원문 번호 없음 + 연한 박스)으로 싣습니다. 상세는 dt/dd 짝 없이 `div.n-dbdata` 한 요소 안에 1~6번이 글자와 `<br>` 로 이어지는 구조라, 세칙 예고의 추출 엔진(`fss_rule.extract_sections`)에 이 게시판의 항목 제목표만 넘겨 씁니다(번호+제목·순서 검증, 3번 하위 목록 보존, 1·4번은 경계로만). 값이 모두 빈 페이지에서 컨테이너를 바깥으로 넓히면 6번에 '목록'·'행정지도에 대한 의견제출' 버튼과 '정보관리 담당부서 안내'가 섞이므로 이 소스는 넓히지 않고 전체 실패로 드러냅니다. 제목 문구가 바뀌어 인식하지 못한 최상위 항목(예: `6. 담당부서 및 연락처`)의 내용이 앞 항목(5번 제출시한)에 붙지 않도록, 인식한 제목 사이에 빠진 번호가 있으면 그 번호로 시작하는 줄(빈 줄 뒤, 하위 목록의 다음 번호가 아닐 때)에서 앞 항목을 끊고, 경계를 확신할 수 없으면 그 항목을 누락으로 돌립니다(모든 제목이 인식되면 결과는 그대로). 누락·실패 안내, warning·`debug/` 덤프, AI 요약 제외(요약 호출·집계 모두), 4개 항목을 모두 확보해야 상세 수집 성공으로 세는 기준, `seen` 처리는 세칙 예고와 같습니다.
   - 금융규제포털 회신사례는 **법령해석·비조치의견서**의 질의요지·회답·이유를 `post.body` 에 담아 기존 일반 요약 경로(`min_body_chars` 이상이면 1건당 1회 호출)를 그대로 탑니다. 상세 응답이 목록의 그 글이 맞는지(제목·회신일·구분↔endpoint) 먼저 확인하고, 확인되지 않으면 본문·첨부를 수집하지 않을 뿐 아니라 **메일 링크도 통합조회 목록 URL 로 되돌립니다**(검증되지 않은 후보 링크를 그대로 두면 제목만 보고 누른 사용자가 다른 사건의 상세로 갑니다). 목록 `dataIdx`를 `lawreqIdx`/`opinionIdx`로 쓰는 매핑이 아직 라이브 확인 전이라, 가정이 틀렸을 때 다른 사건의 회답·첨부·링크가 나가는 것을 막는 안전장치입니다. 반대로 확인에 성공한 뒤의 본문 파싱·첨부 실패에서는 링크를 되돌리지 않습니다(원문을 직접 볼 수 있어야 합니다). 본문은 **세 항목이 모두 있을 때만** 만듭니다 — 회답이 빠진 질의만으로 3줄 요약을 만들면 결론을 지어낸 요약이 실립니다(하나라도 없으면 본문을 비우고 어떤 항목이 없는지 warning 을 남깁니다. 첨부 수집은 계속합니다). `details` 는 채우지 않습니다 — 실제 요약 호출 경로(`_summarize_general`)는 `details` 를 보지 않고 본문 길이만 보지만, notifier 가 `details` 를 요약보다 먼저 렌더해 그 요약이 메일에 보이지 않고 집계 로그(`ai_target_count`)만 대상에서 빼 로그와 실제 호출이 어긋나기 때문입니다. 상세 주소가 확인되지 않은 구분은 본문 없이 제목·링크만 나가며, 그 글은 per-post 훅(`BaseScraper.supports_enrich`)으로 상세 수집 통계에서 제외됩니다.
   - 모델이 수명 종료돼 404가 나면 `llm.fallback_models` 순서로 자동 전환하고, 성공한 모델을 그 실행 동안 재사용합니다. 전부 실패해도 원문 발췌로 발송됩니다. 일시 장애 중 **HTTP 503(과부하)만** 예외로, 그 모델의 기존 재시도를 모두 소진한 뒤 다음 설정 모델을 시도합니다(성공하면 그 모델을 재사용). **타임아웃·네트워크 실패·그 밖의 5xx(500·502·504)·408 에서는 모델을 바꾸지 않습니다.**
   - 의안 배치는 호출 1회마다 운영 로그에 입력 규모와 사용량을 남깁니다(추가 Gemini 요청 0회 — `countTokens` 를 따로 부르지 않습니다). 호출 전 `Assembly AI call — batch=i/n items=… body_chars=… prompt_chars=… min/avg/max_bill_chars=… allocated_window_sec=… overall_remaining_sec=…`, 성공 후 `Assembly AI call ok — elapsed_sec=… model_version=… prompt_tokens=… candidate_tokens=… thought_tokens=… total_tokens=…`(응답에 `usageMetadata` 가 없으면 `None`), 실패 시 `Assembly AI call failed — elapsed_sec=… error_kind=… error_type=… response_retry=…`. **API 키·프롬프트 전문·제안이유 본문은 로그에 남기지 않습니다**(길이만 남깁니다).
5. 발송할 것이 있으면 **다이제스트 메일 1통**으로 묶어 발송합니다(첨부 포함). 한 통 안에 두 종류가 들어갈 수 있습니다.
   - **신규 게시물** — 이번에 처음 발견한 글
   - **의안 상세 업데이트** — 이미 알린 의안의 '제안이유 및 주요내용'을 나중에 확보한 것(→ [상세 재조회 큐](#상세-재조회-큐와-의안-상세-업데이트-알림)). 업데이트만 있는 회차의 메일은 '신규 게시물'이라고 표시하지 않습니다.
6. "이미 본 글" 목록과 상세 재조회 큐(`state/seen.json`)를 저장소에 커밋해 다음 실행에 이어씁니다. **메일 발송이 성공한 뒤에만** 확정합니다 — 실패하면 신규는 `seen` 으로 확정되지 않고 복구분도 큐에 남아 다음 실행에 다시 시도합니다.

## 설정 방법 (GitHub Actions)

### 1) Gmail 앱 비밀번호 발급
1. Gmail 계정에 **2단계 인증** 활성화
2. https://myaccount.google.com/apppasswords 에서 앱 비밀번호(16자리) 발급

### 2) 저장소 Secrets 등록
`Settings → Secrets and variables → Actions → New repository secret` 에서:

| Secret 이름 | 값 |
|---|---|
| `SMTP_USER` | 보내는 Gmail 주소 **(필수)** |
| `SMTP_PASSWORD` | 발급받은 앱 비밀번호 16자리 **(필수)** |
| `MAIL_TO` | **수신자**. 여러 명은 `,` 또는 `;` 로 구분 **(필수)** |
| `ASSEMBLY_API_KEY` | 계류의안 수집용 열린국회정보(open.assembly.go.kr) API 인증키. 미설정 시 계류의안만 건너뜀 |
| `GEMINI_API_KEY` | 본문 **AI 3줄 요약**용 Google Gemini API 키(무료 티어 가능). [aistudio.google.com/apikey](https://aistudio.google.com/apikey) 에서 발급. 미설정 시 요약을 건너뛰고 기존처럼 원문 발췌를 표시 |
| `SMTP_HOST` | `smtp.gmail.com` (기본값과 같으면 생략 가능) |
| `SMTP_PORT` | `587` (기본값과 같으면 생략 가능) |
| `MAIL_FROM` | 발신 표시 주소(보통 `SMTP_USER`와 동일) |

`Settings → Secrets and variables → Actions → Variables` 탭에는 비밀이 아닌 설정을 둡니다:

| Variable 이름 | 값 |
|---|---|
| `MODEL` | 요약에 쓸 **Gemini 모델명**. 워크플로가 이 값을 `GEMINI_MODEL` 환경변수로 넘겨 그대로 primary 모델이 됩니다. 비워 두면 `config.yaml` 의 `llm.model` 로 되돌아갑니다 |

> **운영 환경(GitHub Actions)에서는 이 Variable 이 primary 모델을 덮어씁니다.** 다만
> 유일한 출처는 아닙니다 — Variable 이 없으면 아래 순서로 내려갑니다:
>
> ```
> GEMINI_MODEL  >  MODEL  >  config.yaml 의 llm.model  >  코드 기본값(_DEFAULT_MODEL)
> ```
>
> 빈 문자열은 '지정 안 함'으로 보고 다음 순위로 넘어갑니다(`MODEL` 환경변수 폴백은
> 로컬 실행·기존 설정 호환용). 즉 Variable 을 지워도 요약이 멈추지 않고 `config.yaml`
> 값으로 계속 동작하며, primary 를 쓸 수 없을 때(404) 넘어갈 `llm.fallback_models`
> 목록은 이와 별개로 `config.yaml` 에 남아 있습니다. 실제로 어떤 모델이 어느 출처에서
> 왔는지는 실행 시작 로그의 `LLM 설정 — primary=… , fallback=… (primary 출처: …)`
> 한 줄로 확인할 수 있습니다.

> 신규 글이 있으면 **AI 요약을 호출하기 전에** ① 필수 Secret(`SMTP_USER`·`SMTP_PASSWORD`·`MAIL_TO`)이 채워졌는지, ② 그 값으로 실제 SMTP 로그인이 되는지를 먼저 확인합니다. 둘 중 하나라도 실패하면 요약 없이 즉시 실패로 끝냅니다 — 어차피 보낼 수 없는 메일에 Gemini 무료 할당량을 쓰지 않기 위함입니다. 앱 비밀번호를 폐기했거나 SMTP 호스트에 닿지 못하는 경우가 여기 걸립니다. 신규 글은 미확정으로 남아 복구한 다음 실행에 그대로 발송됩니다.

### 3) 수신자 설정
수신자 주소는 **저장소에 커밋하지 않습니다.** `MAIL_TO` Secret(여러 명은 `a@x.com, b@y.com`)으로만 지정하세요. 로컬 실행 시에는 `.env` 의 `MAIL_TO` 를 사용합니다(`.env` 는 커밋 제외).
여러 명에게 보내도 **수신자끼리는 서로의 주소를 볼 수 없습니다**(BCC 방식). 메일함에는 발신자가 `LAW RADER` 로 표시됩니다.
구독자별로 받을 기관을 고르게 하려면 아래 [구독 기관 선택](#구독-기관-선택-기본-비활성) 절을 참고하세요(기본 비활성).

### 4) 실행
- 자동: Cloudflare Cron → GitHub `workflow_dispatch` → GitHub-hosted runner → Python 모니터 순서로 실행
- 수동: 저장소 **Actions 탭 → LAW RADER KR monitor → Run workflow**

> 처음 가동할 때는 수동 실행 1회로 기준선을 잡습니다. 이때는 **메일이 오지 않는 것이 정상**이며(로그에 `기준선 N건 기록(메일 생략)`), 그 다음 실행부터 신규 글만 발송됩니다.

## Cloudflare 예약 실행 설정

Worker 설정은 `cloudflare-scheduler/`의 독립 npm 프로젝트에 있습니다. 두 Cron Trigger는 UTC 기준이며 다음 KST 시각에 대응합니다.

| Cloudflare Cron (UTC) | KST 실행 예정 시각 |
|---|---|
| `7,22,37,52 0-8 * * MON-FRI` | 월~금 09:07~17:52, 15분 간격 |
| `7 9 * * MON-FRI` | 월~금 18:07 |

Dispatch 입력은 `trigger_source`(요청 출처), `scheduled_for`(Cloudflare가 기록한 UTC 예정 시각), `cron_expression`(실행된 Cron 표현식)입니다. 이 값과 job 단계가 실제 시작된 UTC 시각은 Actions 실행 이름과 로그에서 확인할 수 있습니다. GitHub-hosted runner의 실제 시작은 예약 시각보다 늦을 수 있습니다.

Fine-grained PAT는 대상 저장소 `zetatech-a/LAW_RADER_KR`만 선택하고 **Actions: Read and write** 저장소 권한을 부여합니다. PAT 값은 코드나 설정 파일이 아닌 Cloudflare Secret `GITHUB_TOKEN`으로 저장합니다. `GITHUB_OWNER`, `GITHUB_REPO`, `GITHUB_WORKFLOW`, `GITHUB_REF`는 `wrangler.jsonc`의 일반 변수입니다.

로컬 준비와 Scheduled Trigger 테스트:

Cloudflare Scheduler 개발 및 배포에는 Node.js 22 이상이 필요합니다.

```bash
cd cloudflare-scheduler
npm install
npm run check

# .dev.vars에는 GITHUB_TOKEN만 설정
npm run dev

# 별도 터미널에서 Scheduled Handler 호출
curl "http://localhost:8787/__scheduled?cron=7%2C22%2C37%2C52+0-8+*+*+MON-FRI"
```

`.dev.vars`는 `.gitignore`에 포함된 로컬 Secret 파일이며 저장소에 커밋하지 않습니다. 실제 PAT 값도 README나 다른 추적 파일에 기록하지 않습니다.

최초 운영 배포 시 `--secrets-file`을 사용해 Worker 코드와 `.dev.vars`의 Secret을 함께 배포합니다. 로그인과 배포는 계정을 확인한 뒤 운영자가 수행해야 합니다.

```bash
cd cloudflare-scheduler
npx wrangler login
npx wrangler whoami
npm run deploy -- --secrets-file .dev.vars
```

이미 배포된 Worker의 토큰만 교체하려면 `npx wrangler secret put GITHUB_TOKEN`을 사용할 수 있습니다. 이 명령을 실행하면 Secret을 반영한 새 Worker 버전이 즉시 배포됩니다.

현재 `.github/workflows/monitor.yml`의 기존 GitHub `schedule` 두 개는 Cloudflare 실운영 검증 전 임시 fallback으로 유지합니다. 검증 완료 후 별도 변경에서 제거합니다.

## 구독 기관 선택 (기본 비활성)

`MAIL_TO` 의 기존 구독자가 본인 인증 후 받고 싶은 기관만 고르는 기능입니다.

- Python(`src/subscription.py`): 발송 전 구독 설정 조회·필터·그룹 발송(Phase 1)
- Cloudflare Worker + D1(`cloudflare-subscription/`): 설정 페이지, 인증번호 발급·검증, 설정 저장, 내부 API(Phase 2)
- Google Apps Script(`google-apps-script/`): 서명된 요청에 한해 인증번호 메일 발송(Phase 2)

배포 절차는 **[cloudflare-subscription/DEPLOY.md](cloudflare-subscription/DEPLOY.md)**(운영자용 한국어 가이드)를 따릅니다.
**`config.yaml` 의 `subscription.enabled` 가 `false`(기본)이면 구독 API 를 부르지 않고 지금과 똑같이 발송합니다.**

### 동작 (enabled: true)

1. 보낼 것(신규 또는 의안 재조회 대상)이 있으면 SMTP 선점검 직후, **AI 요약 전에** 구독 API 를
   한 번 호출합니다(`POST /internal/sync-and-resolve`). `MAIL_TO` 주소는 HMAC 식별자로만 보냅니다.
2. 같은 실효 설정을 가진 수신자를 한 그룹(BCC 한 통)으로 묶습니다. 전체 수신 그룹이 먼저입니다.
3. 그룹마다 `Post.source_key` 로 신규 게시물과 의안 상세 업데이트를 거릅니다(표시명은 쓰지 않음).
   보낼 것이 없는 그룹(일시중지, 이번에 글이 없는 기관만 고른 경우)에는 메일을 보내지 않습니다.
4. **모든 그룹 발송이 성공한 뒤에만** 기존과 같이 `seen`·재조회 큐를 확정합니다. 받을 사람이
   없었던 글도 seen 으로 확정됩니다 — seen 은 "이 실행의 발송 처리가 끝났다"는 전역 상태입니다.
5. 메일 하단에 `수신 기관 설정` 링크(`SUBSCRIPTION_PAGE_URL`)가 붙습니다. 링크에는 주소·토큰이
   없습니다(전달된 메일로 설정이 바뀌지 않도록 인증은 페이지에서 OTP 로 합니다).

| 기관 ID | 표시명 | source_key |
|---|---|---|
| `fsc` | 금융위원회 | `fsc_press`, `fsc_legislation` |
| `fss` | 금융감독원 | `fss_press`, `fss_admin_guidance`, `fss_rule_amendment`, `fss_sanction`, `fss_mgmt_notice` |
| `better` | 금융규제·법령해석포털 | `better_reply` |
| `pipc` | 개인정보보호위원회 | `pipc_notice`, `pipc_press` |
| `assembly` | 의안정보시스템 | `assembly_bill` (의안 상세 업데이트 포함) |

매핑의 단일 원천은 `config.yaml` 의 `subscription.agencies` 입니다. 켜져 있으면 활성 소스가 모두
정확히 한 기관에 속해야 하며, 아니면 설정 오류로 실행이 멈춥니다. 구독자는 **기관 ID** 를
저장하므로 기관에 게시판을 추가하면 그 기관을 고른 사람도 자동으로 받고, `mode=all` 은 새
기관까지 모두 받습니다. 기관 ID 는 저장값이라 바꾸거나 지우지 마세요.

| 구독 상태 | 의미 |
|---|---|
| `mode=all` | 모든 기관 수신 — D1 에 행이 없는 기존 구독자의 초기값 |
| `mode=selected`, 기관 1개 이상 | 선택한 기관만 수신 |
| `mode=selected`, 빈 목록 | 전체 알림 일시중지 |

설정(값은 저장소에 두지 않음):

| 이름 | 종류 | 설명 |
|---|---|---|
| `SUBSCRIPTION_API_URL` | Secret | 구독 API 주소(`https://…workers.dev/internal/sync-and-resolve`) |
| `SUBSCRIPTION_API_TOKEN` | Secret | 구독 API Bearer 토큰(Worker Secret 과 같은 값) |
| `SUBSCRIBER_HASH_KEY` | Secret | 구독자 식별자 HMAC 키, 32자 이상(Worker Secret 과 같은 값) |
| `SUBSCRIPTION_PAGE_URL` | Variable | 메일 하단 설정 링크(`https://…workers.dev/`) |
| `SUBSCRIPTION_REQUIRED` | Variable | 운영 보호장치. 정식 전환 **직전** `true`. `true` 인데 `enabled: false` 면 실행 중단(아래). `true`/`false`/빈값 외에는 설정 오류 |
| `SUBSCRIPTION_DB_INSTANCE` | Variable | 운영 D1 식별자(32자 hex, 점검 스크립트 출력값). D1 재생성·교체 시 발송 중단. `SUBSCRIPTION_REQUIRED=true` 이면 필수 |

### 수신자 처리 규칙

| 상황 | 처리 |
|---|---|
| `MAIL_TO` 비어 있음 | 기존 `MAIL_TO` 누락 오류로 종료. 구독 API 를 부르지 않으며 빈 목록은 절대 보내지 않음 |
| 형식이 잘못된 주소(공백·비ASCII·`이름 <주소>` 등) | 설정 오류로 발송 중단(주소는 로그에 남기지 않고 개수만) |
| 중복 주소(대소문자만 다른 경우 포함) | 정규화(앞뒤 공백 제거 + ASCII 소문자) 기준 1명으로 보고 한 통만 발송 |
| `MAIL_TO` 에 새로 추가 | D1 에 행이 없으므로 `mode=all` — 첫 동기화 때 구독자로 등록 |
| `MAIL_TO` 에서 삭제 | 메일 대상에서 빠짐. Worker 는 그 구독자를 비활성(soft)으로 표시하고 로그인 세션·인증번호를 즉시 삭제, 설정은 보존 |
| 삭제 후 재등록 | 비활성 → 활성 복귀, 보존된 설정 재적용 |
| 최초 동기화 전 | D1 에 구독자가 없으므로 아무도 인증번호를 받을 수 없음(신규 가입 불가) |

비활성화는 **로그인 가능 여부에만** 영향을 줍니다. 발송 대상은 언제나 그 실행의 `MAIL_TO` 이므로,
`MAIL_TO` 를 실수로 줄였다가 되돌려도 설정은 사라지지 않습니다.

동기화 시점: 보낼 것이 있는 실행은 발송 직전에(fail-closed), **보낼 것이 없는 정기 실행도 매번**
같은 API 로 동기화합니다(Phase 2). 그래서 `MAIL_TO` 에서 뺀 사람의 설정 페이지 접근은 다음 정기
실행(최대 약 15분)에 막힙니다. 발송 없는 실행의 동기화 실패는 경고만 남기고 메일·state·실행 결과를
바꾸지 않습니다(다음 발송 실행이 fail-closed 로 드러냅니다). `--dry-run` 은 동기화하지 않습니다.

### 장애 정책

- **구독 API 장애·계약 위반**(연결 실패, 타임아웃, HTTP 200 외 응답, 리다이렉트, 잘못된 JSON,
  버전 불일치, 구독자 누락·중복, 알 수 없는 mode·기관 ID): 메일을 **한 통도** 보내지 않고 실패(exit 1)로
  끝냅니다. `MAIL_TO` 전체 발송으로 되돌아가지 않으며 Gemini 도 부르지 않습니다. 신규 글의 seen·상세 재조회
  큐·재조회 메타데이터는 확정하지 않고, 다음 실행(15분)이 처음부터 다시 시도합니다. API 호출 자체는 재시도하지 않습니다.
  예외: 이번 실행에서 **처음 기준선을 잡은 소스**의 기준선 기록은 구독 API 조회보다 먼저 저장됩니다(기존 동작 — 기준선은
  메일로 보내지 않는 글이므로 발송 대상과 무관하며, 워크플로가 state 를 커밋합니다). `SUBSCRIPTION_REQUIRED` 가드로 멈출 때는 수집 전이라 state 를 전혀 바꾸지 않습니다.
- **구독 설정 누락**(기능은 켰는데 Secret 이 비었거나 https 가 아님): 위와 같이 발송 중단.
- **그룹 일부 발송 실패**: 기존 '누락보다 중복' 정책을 유지합니다. 한 그룹이라도 실패하면 seen·큐를
  확정하지 않으므로, 먼저 성공한 그룹은 다음 실행에서 **같은 알림을 중복 수신할 수 있습니다.**
  로그에 `구독 그룹 i/n(라벨) 발송 실패 … 먼저 발송된 그룹 k개(수신자 m명) … 중복 수신` 이 남습니다
  (주소·식별자 없이 인원과 기관 ID 라벨만). 같은 그룹에서 반복 실패하면 매 실행마다 앞 그룹에 중복이
  쌓이므로 Actions 실패가 이어지면 바로 확인하세요.
- **장기 장애 주의**: 발송이 계속 막혀 있는 동안 한 소스의 신규가 `max_new_per_source`(50, 의안 75)를
  넘으면, 복구 후 최신 cap 건만 발송되고 나머지는 기존 안전장치에 따라 seen 처리됩니다(SMTP 장애와 같음).
- `--dry-run` 은 구독 API 를 호출하지 않으므로 운영 D1 을 바꾸지 않습니다.

### 운영 전환·롤백

- **켜기**(상세: [DEPLOY.md 14단계](cloudflare-subscription/DEPLOY.md)): 스테이징 검증 → 운영 D1 식별자를
  `SUBSCRIPTION_DB_INSTANCE` 에 등록 → 이 기능을 `enabled: false` 그대로 main 반영 → **전환 직전 `SUBSCRIPTION_REQUIRED=true`**
  (이 순간부터 `enabled: false` 인 main 은 잠시 실행을 멈춤) → `enabled: true` 반영 → 첫 실행 확인 → 링크 안내.
  보호장치를 사용자 설정 기능(메일 하단 링크)보다 먼저 켭니다. 첫 실행에서 기존 구독자는 모두 `mode=all` 이라 받는 내용은 전과 같습니다.
- **보호장치의 전제**: ① main 의 Python 코드(이 기능 포함 버전) ② `monitor.yml` env 줄 ③ GitHub Variable 이 모두 있어야 작동합니다.
  이 기능 이전의 운영 코드는 `SUBSCRIPTION_REQUIRED` 를 읽지 않으므로 Variable 만 먼저 바꿔도 효과가 없습니다.
- **운영 보호장치**: GitHub Variable `SUBSCRIPTION_REQUIRED=true` 상태에서
  `enabled` 가 `false` 가 되면 실행 시작 직후(수집 전) 멈추고 메일·state 를 바꾸지 않습니다(`--dry-run` 은 경고만).
  오타(`ture`, `yes`, `1` 등)는 false 로 읽지 않고 설정 오류로 실행을 실패시킵니다. 코드를 이 장치 이전
  버전으로 되돌리면 보호장치도 사라지므로, 되돌리기 전에 아래 폐지 절차를 먼저 마칩니다.
- **롤백은 단계에 따라 다릅니다**(상세: [DEPLOY.md '롤백·비상 정지'](cloudflare-subscription/DEPLOY.md)).
  - 기관 선택(일시중지 포함)을 저장한 구독자가 **없는** 도입 전에만 `enabled: false` + `SUBSCRIPTION_REQUIRED` 비움으로
    기존 전원 발송 방식에 되돌릴 수 있습니다(받는 메일이 같으므로).
  - 저장한 구독자가 **있으면** 그 조합은 금지입니다 — 일시중지·선택 수신 구독자에게 고르지 않은 기관의 메일이 나갑니다.
    기본 조치는 `SUBSCRIPTION_REQUIRED=true` 를 유지한 **안전 정지**(monitor 워크플로 비활성화, 또는 `true` 상태에서 `enabled: false`)이고,
    재개는 보호장치·D1 식별자 검사(Python)와 카탈로그 불일치 거절(Worker)이 있는 검증된 버전으로만 합니다.
  - Worker 장애에는 끄지 말고 복구를 기다리세요(메일은 보류됐다가 복구 후 발송).
  - 이 기능 이전 코드는 `SUBSCRIPTION_REQUIRED` 를 아예 읽지 않으므로, 그런 코드로 되돌리는 것은 롤백이 아니라 기능 폐지입니다.
    폐지는 ① 안전 정지 ② 구독자 사전 공지 ③ D1 에서 일시중지·선택 구독자를 확인해 필요한 주소를 `MAIL_TO` 에서 정리
    ④ 코드 되돌리기 순서로만 합니다. 검증된 코드가 없으면 워크플로 비활성화·SMTP 자격증명 비활성화(운영자 승인)로 멈춥니다.
- **HMAC 키 정책**: `SUBSCRIBER_HASH_KEY` 는 최초 설정 후 고정합니다. 실제 값은 Cloudflare·GitHub Secret 에만
  두고 git 추적 파일에 기록하지 않습니다. 키를 바꾸면 모든 식별자가 바뀝니다. 한쪽만 바뀌면 `hash_key_mismatch`,
  두 곳이 함께 바뀌면 `key_changed` 로 발송이 멈추며(저장된 선택이 `mode=all` 로 조용히 초기화되지는 않음),
  조용한 교체는 금지합니다. 유출 시에는 설정을 보존하는 별도 마이그레이션을 먼저 준비합니다
  (현재 버전에 자동 키 회전 기능은 없습니다).

### Python ↔ Worker API 계약 (v1)

`POST /internal/sync-and-resolve` · `Authorization: Bearer <SUBSCRIPTION_API_TOKEN>` ·
`Content-Type: application/json`

요청(Python → Worker):

```json
{
  "api_version": 1,
  "key_check": "9b2e…(HMAC-SHA256(SUBSCRIBER_HASH_KEY, \"lrk-subscriber-key-check-v1\") hex 64자)",
  "subscribers": [
    "1f0c…(HMAC-SHA256 hex 64자, 정렬·중복 제거)",
    "9a7e…"
  ],
  "agencies": [
    {"id": "fsc", "name": "금융위원회"},
    {"id": "fss", "name": "금융감독원"},
    {"id": "better", "name": "금융규제·법령해석포털"},
    {"id": "pipc", "name": "개인정보보호위원회"},
    {"id": "assembly", "name": "의안정보시스템"}
  ]
}
```

`instance`(선택): 운영자가 `SUBSCRIPTION_DB_INSTANCE` 에 등록한 D1 식별자(32자 hex). 있으면 Worker 의 D1 과 같은지 확인합니다.

응답(Worker → Python, HTTP 200):

```json
{
  "api_version": 1,
  "instance": "3f9c…(이 D1 의 식별자 32자 hex)",
  "subscribers": [
    {"subscriber": "1f0c…", "mode": "all", "agencies": []},
    {"subscriber": "9a7e…", "mode": "selected", "agencies": ["fsc", "assembly"]}
  ]
}
```

- 식별자 = `HMAC-SHA256(SUBSCRIBER_HASH_KEY, 정규화 주소)` 소문자 hex. 정규화 = 앞뒤 공백 제거 +
  ASCII 소문자(비ASCII 주소는 거부 — Python 과 JavaScript 의 대소문자 변환 차이를 없애기 위함).
- 응답 `subscribers` 는 **목록**입니다(JSON 객체는 중복 키를 감지할 수 없음). 요청한 식별자마다
  정확히 1개 — 누락·중복·요청하지 않은 식별자는 Python 이 오류로 처리합니다.
- `mode` 는 `all`/`selected` 만. `mode=all` 이면 `agencies` 는 빈 목록이어야 합니다.
  `selected` + 빈 목록은 정상(일시중지)입니다. 기관 ID 는 요청 카탈로그에 있는 것만, 중복 없이.
- `api_version` 은 정수 1. 그 밖의 최상위 키는 무시합니다(하위 호환 확장용). Python 은 `SUBSCRIPTION_DB_INSTANCE` 가
  설정돼 있으면 응답 `instance` 가 같은지도 확인합니다.
- **무결성 거절(Phase 2.6, 모두 409·쓰기 없음 — Python 은 발송 중단):** `instance_missing`(Python 이 식별자를 기대하는데 D1 에
  없음 = D1 재생성·교체 의심) · `instance_mismatch`(다른 D1) · `key_changed`(최초 동기화 때와 다른 키 — GitHub·Worker 키가 함께 바뀜) ·
  `catalog_mismatch`(구독자가 고른 기관이 카탈로그에서 빠짐) · `preference_missing`(설정을 저장한 적 있는 구독자의 설정 행이 없음).
  어느 경우든 기본값 `mode=all` 로 조용히 돌아가지 않습니다. Python 오류 메시지에 사유 코드가 함께 표시됩니다(예: `HTTP 409 (catalog_mismatch)`).
- `key_check` 는 Python 과 Worker 에 등록한 `SUBSCRIBER_HASH_KEY` 가 같은지 확인하는 값입니다(키 자체가 아님).
  Worker 가 자기 키로 계산한 값과 다르면 DB 를 건드리지 않고 `409 hash_key_mismatch` 로 거절합니다 —
  키가 다르면 모든 식별자가 '새 구독자'로 보여 전원 `mode=all` 로 응답되는(일시중지·선택이 조용히 무시되는)
  사고를 막기 위한 장치입니다(Phase 2.5 검수에서 추가).
- 응답 본문 1 MB 초과, 200 이외 상태(리다이렉트 포함)는 오류입니다. 타임아웃은 `subscription.timeout_sec`.

Worker 구현(`cloudflare-subscription/src/internal.ts`)이 지키는 것:

1. 토큰을 상수 시간으로 비교하고, 실패하면 401 을 반환하며 아무것도 쓰지 않는다. `key_check` 가
   다르면 409, 없거나 형식이 틀리면 400(모두 쓰기 없음).
2. 요청 전체를 먼저 검증한다(버전, 빈 목록 금지 → 400, 64자 hex, 중복 금지, 기관 ID 형식).
   하나라도 틀리면 **쓰기 없이** 4xx.
3. 동기화는 **차분만** 쓴다: 새 식별자 INSERT, 활성 상태가 바뀐 것만 UPDATE, 카탈로그는 바뀐 경우만
   upsert. 변화가 없는 실행은 쓰기 0건이어야 한다(15분마다 호출되므로).
4. 모든 쓰기를 하나의 `D1 batch` 로 실행한다(트랜잭션 — 중간 실패 시 전체 롤백). 실패하면 5xx 를
   반환해 Python 이 발송을 중단하게 한다. 부분 동기화로 기존 구독자가 비활성화되는 일이 없어야 한다.
5. 구독자가 고른 기관 중 하나라도 이번 요청 카탈로그에 없으면 **전체 수신으로 넓히지도, 조용히 좁히지도 않고**
   `409 catalog_mismatch` 로 거절한다(쓰기 없음, 저장된 선택 보존 — 기관을 되돌리면 그대로 복원). 일시중지(`selected` + 빈 목록)는
   정상이다. 기관을 없애야 하면 DEPLOY.md '기관 제거' 절차로 해당 선택을 먼저 정리한다. (Phase 2 의 '선택이 비면 mode=all' 규칙은
   원하지 않은 기관의 메일을 보낼 수 있어 Phase 2.6 에서 폐기했다.)
6. D1 에는 평문 이메일을 저장하지 않는다(식별자만). 로그에도 토큰·식별자를 남기지 않는다.

### 설정 서비스 구현 (Phase 2)

Worker API(`cloudflare-subscription/src/index.ts`):

| 메서드 · 경로 | 인증 | 설명 |
|---|---|---|
| `GET /` · `/app.js` · `/app.css` | — | 설정 페이지(프레임워크·외부 스크립트 없음, 기관 목록은 D1 카탈로그로 그림) |
| `POST /api/otp/request` `{email}` | Origin | 항상 `202 {"ok":true}`(등록 여부 비노출). 활성 구독자일 때만 응답 뒤 인증번호 발급·메일 요청 |
| `POST /api/otp/verify` `{email, code}` | Origin | 성공 시 `200` + `__Host-lrk_session` 쿠키. 실패는 사유와 무관하게 `400 invalid_code` |
| `GET /api/preferences` | 세션 | `{mode, agencies, version, catalog, session_expires_at}` (실효 설정 = 다음 발송에 적용될 상태) |
| `PUT /api/preferences` `{mode, agencies, version}` | 세션 + Origin | 본인 설정만 저장. 읽은 `version` 이 아니면 `409 version_conflict` + 최신 상태 |
| `POST /api/logout` | Origin | 세션 삭제·쿠키 제거 |
| `POST /internal/sync-and-resolve` | Bearer | Python 전용, 위 계약 v1 |

오류 응답은 `{"error":"코드"}` 와 알맞은 상태코드이며 실패를 200 으로 가장하지 않습니다.

**인증번호**: 6자리(`crypto.getRandomValues` + 거부 표본추출), 10분, 5회, 일회용, 재발급 시 이전 번호 무효
(구독자당 1행). D1 에는 `HMAC(SERVER_SECRET, "otp:{식별자}:{salt}:{번호}")` 만 저장합니다. 시도 횟수는 비교
**전에** 원자적으로 올리고, 성공 시 행 삭제로 소비해 병렬 요청에도 세션은 1개만 생깁니다.

**요청 제한**: isolate 메모리 IP 제한(분당 30회, D1 이전 1차 차단 — 최선 노력) → D1 고정 창 카운터(이메일 15분 3회·
하루 5회, IP 시간당 10회, 검증 IP 시간당 20회, 전체 발송 하루 30통, 전체 요청 예산 하루 인증번호 요청 1,000건·
검증 2,000건). 카운터 키는 HMAC 이라 원문이 없습니다. **이미 한도에 걸린 요청은 읽기만 하고 D1 에 쓰지 않습니다** —
거절될 요청까지 카운터를 올리면 공격 IP 하나로 D1 무료 쓰기 한도를 소진시켜 정기 메일(fail-closed)까지 멈출 수
있기 때문입니다(Phase 2.5 검수에서 수정). 전체 요청 예산을 넘으면 그날은 설정 페이지만 잠기고 정기 메일 동기화는
D1 쓰기 여유를 유지합니다.
Cloudflare Rate Limiting 바인딩은 무료 플랜 제공 여부를 공식 문서로 확인하지 못해 쓰지 않습니다.

**세션·웹 보안**: 256비트 토큰(D1 에는 SHA-256), 절대 만료 30분, 구독자당 1개.
`__Host-lrk_session; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=1800`. 상태 변경 API 는 `Origin` 이
`PUBLIC_ORIGIN` 과 정확히 같고 `Content-Type: application/json` 일 때만 받습니다(CORS 헤더 없음).
모든 응답에 CSP(`default-src 'none'; script-src 'self'; …; frame-ancestors 'none'`), `Cache-Control: no-store`,
`nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`. 화면은 서버 데이터를 `textContent` 로만 넣습니다.
세션 확인 때마다 구독자 활성 여부를 함께 확인합니다.

**Apps Script 호출 보호** — 공식 문서로 확인한 제약:
[Web Apps](https://developers.google.com/apps-script/guides/web) /
[Event Objects](https://developers.google.com/apps-script/guides/triggers/events) 의 `doPost(e)` 이벤트 객체에는
수신 요청 헤더 필드가 없으므로 인증 정보는 본문에 넣습니다.

- 본문: `{"v":1,"ts":<unix초>,"nonce":"<32자 hex>","to":"<정규화 주소>","code":"<6자리>","sig":"<hex>"}`,
  `sig = HMAC-SHA256(공유 Secret, "LRK-OTP-v1\n{ts}\n{nonce}\n{to}\n{code}")` — 수신자와 메일 내용(번호)에 대한 서명.
  공유 Secret 은 Worker Secret 과 Apps Script **스크립트 속성** `OTP_SHARED_SECRET` 에만 둡니다.
- Apps Script 순서: 본문 형식 → 서명(상수 시간) → `ts` ±300초 → `LockService` 직렬화 → **`PropertiesService`(영속)**
  nonce 재사용 검사 → 자체 상한(하루 30통·수신자별 5통) → `MailApp.getRemainingDailyQuota()` 예비분 확인 →
  nonce·카운터를 **발송 전에** 기록 → `MailApp.sendEmail()`. 서명이 틀린 요청은 nonce 저장소도 건드리지 않습니다.
- `CacheService` 는 쓰지 않습니다([공식 문서](https://developers.google.com/apps-script/reference/cache/cache)상 만료 전 삭제 가능).
  스크립트 속성은 값 9KB·저장소 500KB 한도라 15분 지난 nonce 와 지난 날짜 카운터를 매 요청 정리합니다.
- 웹앱 응답은 [공식 문서](https://developers.google.com/apps-script/guides/content)상 `script.googleusercontent.com` 의
  일회용 주소로 리다이렉트되므로 Worker 는 `redirect: "follow"` 로 따라가 JSON 본문의 `ok` 로만 판정합니다.
  **이 리다이렉트·응답 동작은 실제 Google 서버로 검증하지 못했으므로 배포 시 DEPLOY.md 8단계로 확인합니다.**

**발송량** — [Apps Script 할당량](https://developers.google.com/apps-script/guides/services/quotas):
일반 Gmail 계정의 `MailApp` 수신자 한도는 하루 100명입니다. 자체 상한(하루 30통)으로 여유를 둡니다. 정기
모니터링 메일(Gmail SMTP)은 별도 경로이지만 같은 계정이면 계정 평판을 공유할 수 있어(공식 수치 미확인)
**인증번호 전용 무료 Gmail 계정**으로 Apps Script 를 배포하는 것을 권장합니다.

### 무료 운영 제약 (월 추가 비용 0원)

- [Workers Free](https://developers.cloudflare.com/workers/platform/limits/): 하루 요청 100,000건(UTC 자정
  초기화, 초과 시 Error 1027), 요청당 CPU 10ms, 외부 subrequest 50개. 해시·HMAC 수준 연산만 하고 PBKDF2 같은
  무거운 연산은 쓰지 않습니다. 무료 `workers.dev` 주소를 씁니다(별도 도메인 없음).
- [D1 Free](https://developers.cloudflare.com/d1/platform/pricing/): 하루 읽기 500만 행·쓰기 10만 행(UTC 자정
  초기화). 2026-09-01 부터 초과 시 쿼리가 오류로 실패합니다(자동 과금·자동 전환 없음).
  **D1 이 한도로 실패하면 구독 API 도 실패하고, fail-closed 정책상 정기 메일 전체가 UTC 자정까지 보류됩니다.**
  그래서 OTP 엔드포인트는 D1 쓰기 전에 요청 제한을 걸고, 동기화는 차분만 씁니다.
- Apps Script: 위 `MailApp` 하루 100명. 유료 서비스·Resend·도메인 구매는 사용하지 않습니다.
- Workers·D1 무료 한도는 **계정 단위**입니다. 공개 페이지를 대량으로 두드려 한도를 소진시키면 구독 API 도
  실패해 정기 메일이 보류될 수 있습니다(fail-closed). D1 쓰기는 '막힌 요청은 쓰지 않음 + 전체 요청 예산'으로 보호하지만,
  **Workers 하루 요청 한도(100,000건)는 Worker 코드가 실행되기 전에 집계되므로 코드로 막을 수 없습니다.** 구독 Worker 를 별도 무료
  계정에 두면 예약 실행 Worker(scheduler)는 보호되지만, 구독 Worker 자신의 한도 소진 → 내부 동기화 실패 → 그날 KST 09시까지 정기 메일
  보류라는 구조적 위험은 남습니다(유료 기능 없이 완전히 없애는 방법은 없음). DEPLOY.md 15단계로 사용량을 확인하세요.
- D1 은 무료 플랜에서도 Time Travel(최근 7일 시점 복원)이 됩니다. 삭제된 DB 의 undelete 는 삭제 후 7일 이내 요청만
  받습니다(2026-10-23 changelog). 복원·백업 절차와 주의사항은 DEPLOY.md 'D1 백업·복구'.
- **월 0원은 무료 한도 안에서라는 조건이 붙은 운영 목표이지 가용성 보장이 아닙니다.** 한도를 넘으면 과금 대신 요청·쿼리가
  실패하고, 그동안 정기 메일은 보류됩니다(전원 발송으로 대체하지 않음).
- 수치는 2026-10 기준 공식 문서 확인 값이며 바뀔 수 있습니다. 배포 전에 다시 확인하세요.

## 주요 설정값 (`config.yaml`)

| 항목 | 기본값 | 설명 |
|---|---|---|
| `fetch.list_limit` | 30 | 목록 한 페이지에서 확인할 건수 |
| `fetch.max_pages` | 10 | 한 실행에서 "이미 본 글" 경계까지 훑을 최대 페이지 수(평상시엔 1~2페이지에서 멈춤) |
| `fetch.baseline_pages` | 3 | 최초 기준선 수립 시 기록할 페이지 수 |
| `fetch.max_new_per_source` | 50 | 한 소스에서 한 번에 발송할 신규 상한(폭주 안전장치). 소스가 `max_new_per_run` 을 선언하면 그 소스만 그 값을 씁니다 |
| `sources[assembly_bill].max_new_per_run` | 75 | 의안 전용 신규 상한. 계류의안은 대량 등록일에 하루 60건대가 **정상**이라 전역 50 으로는 매번 잘려 나갑니다. 다른 소스의 상한은 그대로 50 입니다 |
| `sources[assembly_bill].detail_budget_sec` | 360 (코드 기본값 120) | 의안 상세 수집 단계 전체 시간예산(초). 신규 상한이 75 로 늘어난 만큼 운영 설정에서 360 을 택합니다. **상한**이지 목표가 아닙니다 — 정상 실행은 훨씬 빨리 끝납니다. 신규 상세 수집과 상세 재조회 큐가 **같은 예산을 공유**하고, 의안과 무관한 작업·SMTP·유휴 시간은 이 예산에서 제외됩니다 |
| `email.max_attach_mb` | 15 | 메일에 첨부할 총 용량 상한. 초과분은 링크로만 안내 |
| `llm.enabled` | true | 본문 AI 요약 사용 여부. `false` 면 기존 원문 발췌로 발송 |
| `llm.model` | `gemini-3.5-flash-lite` | 요약에 쓸 기본 Gemini 모델. 반복되는 요약 실패로 무료 Flash-Lite 계열로 긴급 전환했습니다(이전 값: `gemini-flash-latest` alias, 키를 생략하면 쓰이는 코드 기본값). 버전을 고정한 모델이므로 수명 종료 시 404가 되면 `llm.fallback_models` 로 넘어갑니다. Flash-Lite 의 실제 API 호환성·요약 품질은 실측 검증 전입니다. **환경변수 `GEMINI_MODEL`(= GitHub Variable `MODEL`) 이 있으면 그 값이 우선**합니다 |
| `llm.fallback_models` | `gemini-3.1-flash-lite` | `model` 을 쓸 수 없을 때(404 / `NOT_FOUND` / 모델 부재 메시지) 이 **순서대로** 넘어갑니다. HTTP 503 은 그 모델의 재시도를 소진한 뒤에만 다음 모델을 시도합니다(사용 불가로 기록하지 않음). `[]` 로 두면 대체 없음. 기존 `gemini-3.6-flash`·`gemini-3.8-flash` 는 기본 체인에서만 뺐고(품질 비교 대상으로는 유지), 키를 생략하면 쓰이는 코드 기본값으로 남아 있습니다 |
| `llm.lines` | 3 | 요약 문장 수 |
| `llm.max_posts` | 40 | 한 실행에서 요약할 최대 글 수(무료 티어 일일 한도 보호). 초과분은 원문 발췌 |
| `llm.rpm` | 10 | 분당 요청 상한. 이 간격에 맞춰 호출을 벌립니다 |
| `llm.max_consecutive_failures` | 3 | 연속 실패가 이만큼 쌓이면 요약을 중단하고 남은 글은 원문 발췌로 발송(LLM 전면 장애 시 메일 지연 방지) |
| `llm.budget_sec` | 240 | **일반** 요약 단계의 시간예산(초). 개별 요청은 단조시계 마감으로 강제 중단되고 재시도 대기도 남은 예산 안으로 잘립니다 |
| `llm.total_budget_sec` | 360 (코드 기본값 0=비활성) | 일반 + 의안 Gemini 단계가 한 실행에서 **합쳐** 쓸 수 있는 상위 상한(초). 두 단계는 실행 시작에 한 번 정해진 **같은 절대 마감**을 공유하므로, 실제 단계 마감은 `min(단계 예산, 공유 예산)` 입니다. 한쪽만 느리면 그 단계는 기존 ceiling(240 / 300)을 그대로 쓰고, 둘 다 느릴 때만 합계가 360초에서 잘립니다 |
| `sources[assembly_bill].detail_retry_interval_sec` | 1800 | 상세 재조회 큐에서 **같은 의안**을 다시 시도하기 전 최소 간격(초). `0` 이면 매 실행 재시도 허용 |
| `sources[assembly_bill].detail_retry_max_per_run` | 10 | 한 실행에서 처리할 **기존 큐 항목** 최대 수. `0` 이면 건수 상한 없음(그래도 `detail_budget_sec`·연속 실패 브레이커는 적용) |
| `llm.assembly_batch.batch_size` | 25 | 한 요청에 담을 최대 의안 수 |
| `llm.assembly_batch.max_bills` | 75 | 한 실행에서 요약할 최대 의안 수. `batch_size` 25 기준으로 정상 실행이면 **최대 3회** top-level 호출입니다(본문이 아주 길면 `max_batch_chars` 때문에 25건 전에 나뉠 수 있습니다 — 기존 계약 그대로) |
| `llm.assembly_batch.request_timeout_sec` | 90 | 의안 배치 **요청 1회**에 허용할 시간(초). 생략하면 `llm.timeout_sec`(45)를 씁니다. 25건을 한 번에 보내므로 단건 요약보다 오래 걸리는데, 일반 경로의 45초는 그대로 둡니다 |
| `llm.assembly_batch.budget_sec` | 300 | 의안 요약 단계 **전체** 시간예산(초). "이만큼 계속 재시도한다"가 아니라, 최대 3개 배치가 각각 `request_timeout_sec` 만큼의 정상 요청 기회를 갖도록 하는 envelope 입니다 |
| `llm.assembly_batch.retry_response_timeout` | false | 응답 생성 타임아웃(`ReadTimeout`·자체 마감 초과)에서 **같은 요청을 다시 보낼지**. 생략하면 `true`(Phase 2 까지의 재시도 계약). `false` 여도 실패 종류는 `TRANSIENT` 그대로라 아래 배치 브레이커의 의미는 달라지지 않습니다 |
| `llm.assembly_batch.thinking_level` | `minimal` | 의안 배치 요청에만 붙일 thinking 수준. 생략하면 기존 generation payload 그대로입니다. 지원이 **확인된** 모델(현재 `gemini-3.6-flash` 계열)에만 실제로 전송되고, alias·미확인 모델에는 보내지 않습니다(현재 기본 체인인 3.5/3.1 Flash-Lite 에는 전송되지 않음). Gemini 2.5 계열의 `thinkingBudget` 계약과 일반 요약 경로는 영향받지 않습니다 |
| `llm.assembly_batch.max_consecutive_transient_failures` | 2 | 의안 배치가 **일시 장애(5xx·타임아웃)로 연속** 이만큼 실패하면 남은 배치는 호출 없이 발췌로 발송. `1` 로 두면 첫 배치의 503 하나로 그날 의안 전부가 발췌가 됩니다. 인증(401/403)·한도(429)·잘못된 요청(400)·모델 부재·내부 오류는 이 카운터와 무관하게 즉시 중단하고, 깨진 JSON·ID 누락·안전필터 차단 같은 **배치 내용** 문제는 세지 않습니다 |

## 개인정보보호위원회(PIPC) 두 소스 — 라이브 미검증 범위

`pipc_notice`(공지사항, `bbsId=BS061`)·`pipc_press`(보도자료, `bbsId=BS074`)는 **실제 사이트 응답을 한 번도 받아 보지 못한 채** 추가되었습니다. 두 소스를 구현한 개발 환경의 egress 정책이 `pipc.go.kr` 로의 CONNECT 를 403 으로 거부해, `curl`·`scripts/verify_sources.py`·`python -m src.main --dry-run` 이 모두 `Tunnel connection failed: 403 Forbidden` 으로 끝났습니다. `likms.assembly.go.kr` 때와 같은 상황입니다(`tests/fixtures/README.md` 참고).

2026-09-16 사전점검에서 `curl`·저장소 `Fetcher` 로 **다시 시도했으나 결과는 동일**했습니다(`gateway answered 403 to CONNECT`). 따라서 이 저장소에서 PIPC 의 실제 DOM 을 확인한 적은 아직 없습니다.

그래서 이 파서는 **추정에 기대는 부분을 최소화**하도록 설계되어 있습니다. 무엇이 사실에 근거하고 무엇이 추정인지 구분해 둡니다.

| 영역 | 근거 | 배포 후 확인 필요 |
|---|---|---|
| 목록에서 글을 찾는 방법 | 상세 URL 계약 `selectBoardArticle.do?bbsId=…&mCode=…&nttId=…` — 사실 | 불필요(마크업이 표든 리스트든 동일하게 동작) |
| `post_id` (`nttId:12503`) · 정규 상세 URL | 같은 URL 계약 | 불필요 |
| 제목 정리(`N`/`NEW`/`새글` 배지, 첨부 아이콘, 스크린리더 텍스트 제거) | 일반적 게시판 마크업 | 가벼운 확인 |
| 게시일 파싱 | 행 안의 '값 전체가 날짜인 셀' 우선 | 가벼운 확인 |
| 페이지네이션 `pageIndex` | eGovFrame 표준 게시판 관례(`fss.or.kr` 과 동일) | **확인 필요** |
| **상세 본문 컨테이너** | `td.tbl_cnts` — 2026-09-16 GitHub Actions verify-results 아티팩트의 실제 PIPC HTML 에서 확인(공지·보도자료 공통). 뒤의 후보 목록은 개편 대비 미검증 | 확인됨 |
| **첨부 다운로드 endpoint** | eGov 파일 식별자(`atchFileId`/`fileSn` 등) 또는 알려진 다운로드 핸들러 — 경로 부분문자열(`/download`)로는 받지 않음(추정) | **확인 필요** |
| **첨부 원본 파일명** | 앵커 `alt` 속성 — 같은 아티팩트에서 확인(`title`·보이는 텍스트는 '첨부파일 다운로드'/'다운로드' 공통 안내문) | 확인됨 |

### 틀렸을 때 어떻게 되는가 (모두 fail-soft)

- **`pageIndex` 가 무시되면** 2페이지가 1페이지와 같아지고, `BaseScraper.collect` 가 '진전 없음 = 경계 도달'로 보고 멈춥니다. 중복 글은 생기지 않고 1페이지 수집으로만 동작합니다(15분 주기에서는 사실상 충분).
- **본문 셀렉터가 맞지 않으면** `post.body` 가 비고, `[pipc_*] 상세 본문 selector 를 찾지 못함 — post_id=… url=…` 경고와 `debug/pipc_*_detail_*.txt` 덤프가 남습니다. `enrich_succeeded()` 가 `False` 를 돌려주므로 운영 로그의 `상세 수집 집계`에 실패로 잡히고, `verify_sources.py` 는 그 소스를 **🟠 부분 실패(PARTIAL)** 로 보고하며 종료코드 1 을 냅니다 (→ `verify.yml` job 이 빨간불). 첨부만 잡힌 상태를 성공으로 세지 않습니다. 메일은 제목·링크로 계속 나갑니다.
- **첨부 endpoint 가 맞지 않으면** 첨부가 비거나 다운로드가 실패하고, 기존 `AttachmentTooLarge`/다운로드 실패 처리대로 링크만 남습니다. 글 자체는 사라지지 않습니다.
- **목록 파싱이 0건이면** 최초 실행의 기준선을 잡지 않습니다(잘못된 빈 기준선 방지). 다음 실행에 다시 시도합니다.

### 배포 후 확인 절차

```bash
# 국내 IP(또는 접속 가능한 환경)에서
python scripts/verify_sources.py --only pipc_notice,pipc_press
python -m src.main --dry-run --debug --no-llm --only pipc_notice,pipc_press
```

`상태: ✅` 와 `페이지네이션: ✅ 2페이지가 1페이지와 다름`, `상세/첨부: 본문 N자 / 첨부 N개` 를 확인하면 됩니다. `🟠 부분 실패`(목록 성공·본문 0자)가 나오면 `debug/pipc_*_detail_*.txt` 에서 실제 본문 컨테이너 class 를 확인한 뒤, **코드를 고치지 말고** `config.yaml` 의 해당 소스에 한 줄만 추가하세요.

```yaml
  - key: pipc_press
    ...
    body_selectors: [".bbs-view-cont", ".view-cont"]   # 실제 확인한 셀렉터 순서대로
```

### 라이브 검증 판정 규칙 (2026-09-16 수정)

`scripts/verify_sources.py` 의 generic 경로에는 **`enrich_succeeded()` 가 `False` 여도 `status` 를 `OK` 로 덮어쓰는 버그**가 있었습니다. `enrich_ok=False` 를 계산해 두고도 아래쪽에서 `status = OK` 로 넘어가, 상세 파서가 깨진 소스가 ✅ 로 보고되고 종료코드도 0 이었습니다(요약 줄에도 드러나지 않았습니다). 상세 계약을 엄격히 선언한 소스일수록 그 선언이 라이브 검증에서 아무 효과가 없었던 셈입니다.

이제 판정은 다음과 같습니다(PIPC 특례가 아니라 **모든 소스에 적용되는 generic 계약**입니다).

| 상황 | status | 종료코드 |
|---|---|---|
| 목록 성공 + `enrich_succeeded()` True | ✅ OK | 0 |
| 목록 성공 + `enrich_succeeded()` False | 🟠 PARTIAL | 1 |
| 목록 성공 + `enrich()` 가 예외 | 🟠 PARTIAL | 1 |
| 목록 실패 | ❌ FAIL | 1 |

PARTIAL 의 `detail` 에는 소스 key·표본 URL·본문 길이·첨부 개수·원인(계약 미충족인지 예외인지)이 함께 찍힙니다. 기존 동작은 그대로입니다 — 상세가 PDF 직접 다운로드인 소스(`fss_mgmt_notice`)나 구조화 항목만 채우는 소스(`fss_sanction`)는 기본 판정을 쓰므로 종전과 같이 ✅ 입니다.

### 검증된 것

`tests/test_pipc.py`·`tests/test_pipc_summary.py`·`tests/test_verify_sources.py` 는 위 계약을 **선언한 대로** 지키는지 확인합니다(특히 `enrich_succeeded=False` 가 ✅ 를 만들 수 없음). 상세 fixture 는 `tests/fixtures/synthetic/pipc_*_detail.html` 의 **손으로 만든 구조 fixture**이며 캡처한 실제 응답이 아닙니다 — 통과한다고 라이브 계약이 검증된 것은 아닙니다.

AI 3줄 요약 연동은 라이브와 무관하게 검증됩니다. 두 소스는 PIPC 전용 LLM 코드 없이 기존 `summarizer.Summarizer` 경로를 그대로 타며(`assembly_bill` 만 배치 경로로 갈리고 PIPC 는 일반 경로), 요약은 기존과 같은 `Post.summary` 에 담겨 기존 `notifier` 렌더로 나갑니다. `--no-llm` 동작도 기존 소스와 동일합니다.

## 알려진 한계

- **예약 실행 지연:** Cloudflare가 예약 시각에 Dispatch를 요청해도 GitHub-hosted runner의 실제 시작 시각은 부하에 따라 늦을 수 있으며 정확한 시작 시각은 보장되지 않습니다.
- **AI 요약의 정확도:** 3줄 요약은 생성형 AI가 만든 것으로 부정확하거나 누락이 있을 수 있습니다. 메일 하단에도 같은 안내가 표시되며, 판단 전에는 반드시 원문 링크를 확인해야 합니다.
- **Gemini 모델 수명 종료:** Google이 특정 모델을 종료하면 그 모델 호출은 404가 됩니다. 기본 체인은 `gemini-3.5-flash-lite` → `gemini-3.1-flash-lite`(운영 primary 는 GitHub Variable `MODEL` 이 우선)이고 404 시 `llm.fallback_models` 로 자동 전환하지만, primary 와 대체 모델이 모두 막히면 해당 회차는 원문 발췌로만 발송됩니다(메일 자체는 정상 발송). 로그의 `모델 … 사용 불가` / `요약 모델 확정: …` 로 확인하고 `config.yaml` 의 모델 목록을 갱신하세요.
- **Gemini 실패 처리:** 실패를 의미별로 나눠 다르게 다룹니다. 어느 경우든 요약이 비면 메일은 기존 원문 발췌로 정상 발송됩니다.

  | 분류 | 예 | 처리 |
  |---|---|---|
  | `TRANSIENT` (연결·5xx) | 408·5xx·연결 실패·`ConnectTimeout`·DNS/reset | `llm.max_retries` 만큼 지수 백오프(+작은 지터)로 재시도 → 실패하면 그 글/배치만 발췌. 의안은 **다음 배치를 한 번 더 시도**하고, 연속 실패가 `llm.assembly_batch.max_consecutive_transient_failures` 에 도달할 때만 남은 배치를 포기 |
  | `TRANSIENT` (응답 타임아웃) | `ReadTimeout`·요청 자체의 wall-clock 마감 초과 | 일반 게시물은 위와 같은 bounded retry. **의안은 `retry_response_timeout: false` 이면 같은 요청을 다시 보내지 않고** 그 배치만 즉시 발췌로 넘깁니다(25건이 90초를 넘겼다면 같은 25건을 다시 보내도 또 넘길 뿐이고, 그 사이 뒤 배치의 시간을 먹습니다). 실패 종류는 `TRANSIENT` 그대로라 브레이커 의미는 동일하고, **타임아웃에서 다른 모델로 넘어가지 않습니다** |
  | `AUTH` · `RATE_LIMIT` · `BAD_REQUEST` · `MODEL_UNAVAILABLE` · `INTERNAL` | 401/403, 429(재시도 소진 후), 400, 설정된 모델 전멸, 코드 내부 예외 | 재시도하지 않고 **이번 실행의 남은 LLM 호출을 전부 중단**. 일반 게시물 단계에서 확정되면 의안 배치 단계도 호출하지 않음(같은 키·같은 설정이라 결과가 같음) |
  | `CONTENT` | 깨진 JSON, 안전필터 차단, 스키마 위반, BILL_ID 누락 | 그 글/배치만의 문제로 격리. 전역 중단·연속 실패 카운터를 올리지 않음(다음 배치는 그대로 시도) |

  일시 장애에서는 원칙적으로 **다른 모델로 자동 전환하지 않습니다**(운영자가 `MODEL` 로 고정한 모델을 존중). 모델을 바꾸는 것은 404 계열(`MODEL_UNAVAILABLE`)에서 `llm.fallback_models` 로 넘어갈 때와, **HTTP 503(과부하)으로 그 모델의 재시도를 모두 소진했을 때**뿐입니다. 503 전환은 모델을 사용 불가로 기록하지 않고, 설정된 모델이 모두 503 이면 마지막 503 을 `TRANSIENT` 그대로 올려 기존 연속 실패 브레이커가 판단합니다. 500·502·504·408·타임아웃·네트워크 실패는 전환하지 않습니다. 로그의 `Gemini transient 실패 — model=… status=… attempt=…` / `Gemini HTTP 503 재시도 소진 — model=…, 다음 모델 … 로 전환합니다` 로 확인하세요.
- **Phase 2 이전 누락분:** 상세 재조회 큐가 배포되기 전에 `PENDING`/`ERROR` 로 `seen` 처리된 의안은 자동으로 복구되지 않습니다. state 에 그 실패 정보가 없고, 과거 `seen` 전체를 추측으로 큐에 넣으면 대량 중복 후속 알림이 발생하기 때문입니다. 필요하면 별도의 명시적 backfill 작업으로 처리해야 합니다.
- **신규 상한 초과분:** 한 소스의 신규가 상한(의안 75건, 그 외 50건)을 넘으면 최신 그만큼만 발송·상세수집되고 나머지는 상세 수집 없이 `seen` 처리됩니다. 그 초과분은 애초에 사용자에게 알려지지 않았으므로 상세 재조회 큐에도 넣지 않습니다(큐는 '이미 알린 의안의 후속 정보'를 위한 것입니다). 이 backlog 는 여전히 별개의 미해결 문제입니다.
- **PIPC 두 소스 라이브 미검증:** `pipc_notice`·`pipc_press` 는 개발 환경의 egress 차단으로 실제 응답을 받아 보지 못한 채 배포됩니다. 상세 본문 셀렉터·첨부 endpoint·`pageIndex` 가 확인 대상이며, 어긋나도 fail-soft 로 degrade 합니다(목록만 수집되거나 본문이 빔 — 메일·state 는 정상). 범위와 확인 절차는 위 **[개인정보보호위원회(PIPC) 두 소스 — 라이브 미검증 범위](#개인정보보호위원회pipc-두-소스--라이브-미검증-범위)** 참고.
- **금감원 세칙 제·개정 예고 상세 — 실제 구조 확인 범위:** 원문 4개 항목 추출은 사용자가 제공한 **실제 #4405 상세 HTML**(`tests/fixtures/fss_rule/live/lrgSlno_4405.html`, 담당자 연락처만 가짜 값으로 치환)로 검증했습니다 — 실제 구조는 `dl.form-group > dt.form-tit / dd.form-conts` 입니다. 다만 개발 환경에서는 `www.fss.or.kr` 이 egress 정책으로 차단되어 **GitHub 러너가 `requests` 로 받는 응답**은 직접 확인하지 못했습니다. Actions 의 *Verify sources* 를 `only=fss_rule_amendment` 로 돌리면 목록 첫 글의 표본 URL·추출된 4개 항목 값이 로그에 찍히고, 미달이면 🟠 와 함께 원본 HTML 이 `debug/` 아티팩트로 남습니다. 다른 게시물은 `python scripts/preview_fss_rule.py --url <상세 URL>` 로 단건 확인할 수 있습니다(메일·state·Gemini 사용 없음).
- **금감원 행정지도 예고 상세 — 실제 구조 확인 범위:** 원문 4개 항목 추출은 사용자가 제공한 **실제 seqno=280 상세 HTML**(`tests/fixtures/fss_admin_guidance/live/seqno_280.html`, 6번 담당자 성명·직통·팩스·이메일만 가짜 값으로 치환)로 검증했습니다 — 실제 구조는 `div.krds-bd-view > div.n-dbdata`(글자 + `<br>`) 입니다. 세칙 예고와 마찬가지로 **GitHub 러너가 `requests` 로 받는 응답**은 개발 환경의 egress 차단으로 직접 확인하지 못했습니다. *Verify sources* 를 `only=fss_admin_guidance` 로 돌리면 표본 URL·4개 항목 값이 로그에 찍히고, 단건은 `python scripts/preview_fss_rule.py --source fss_admin_guidance --url <상세 URL>` 로 확인할 수 있습니다(`--url` 이 `admnPrvntc` 경로면 `--source` 생략 가능).
- **해외 IP 접속 제한:** `fsc.go.kr`·`better.fsc.go.kr`·`open.assembly.go.kr` 는 시간대에 따라 GitHub 러너(해외 IP)에서 연결이 되지 않아 해당 회차에 건너뛸 수 있습니다(로그에 `ConnectTimeout`). 금융감독원(`fss.or.kr`) 5개 소스는 안정적으로 수집됩니다.
  - 연결 실패는 소스별로 격리되어 나머지 소스 수집에는 영향이 없고, 건너뛴 글은 **접속이 되는 다음 회차에 신규로 잡혀 발송**됩니다.
  - 완전히 해결하려면 국내 IP에서 실행해야 합니다(self-hosted runner 또는 국내 서버 cron). 코드는 환경 독립적이라 그대로 사용 가능합니다.

## 로컬에서 실행/테스트

```bash
pip install -r requirements.txt

# 환경변수 설정
cp .env.example .env      # 값 채우기
set -a; source .env; set +a

# 수집만 해보기(메일 미발송, state 미변경)
python -m src.main --dry-run

# 특정 소스만, 상세 로그
python -m src.main --dry-run --debug --only fss_press

# LLM 요약 없이 실행(API 키 없이 확인하거나 무료 한도를 아낄 때)
python -m src.main --dry-run --no-llm

# SMTP 설정 확인용 테스트 메일 1통
python scripts/send_test_email.py

# 테스트
python -m pytest -q

# 구독 설정 Worker 테스트(workerd + 로컬 D1, 외부 호출 없음)
cd cloudflare-subscription && npm ci && npm run check && npm test && cd ..

# 인증번호 메일 Apps Script 테스트(Node 대역, 실제 Google 서비스 호출 없음)
node --test google-apps-script/test/*.test.mjs
```

로컬 Worker 와 Python 의 실제 HTTP 연동 테스트(`tests/test_subscription_contract.py` 의 E2E)는
`wrangler dev` 로 로컬 Worker 를 띄우고 `LRK_WORKER_E2E_URL`(127.0.0.1 만 허용)·`LRK_WORKER_E2E_TOKEN`·
`LRK_WORKER_E2E_HASH_KEY` 를 지정했을 때만 실행됩니다(지정하지 않으면 skip).

## 소스 점검 (파서가 깨졌을 때)

사이트 개편 등으로 특정 소스가 `파싱 0건` 으로 나오면, 메일·상태 변경 없이 점검만 하는 검증 도구를 사용합니다.

- **GitHub Actions:** Actions 탭 → **"Verify sources (live)"** → **Run workflow** → 완료 후 `verify-results` 아티팩트에서 리포트(`verify_report.txt`)와 원본 HTML(`debug/`) 확인
- **로컬:**
  ```bash
  python scripts/verify_sources.py                  # 전체
  python scripts/verify_sources.py --only fss_press # 특정 소스
  ```

리포트는 소스별로 **① 목록 건수 ② 제목 샘플 ③ 페이지네이션 동작 ④ 본문/첨부 추출 여부** 를 보여줍니다. 0건인 소스는 `debug/<key>_list.txt` 원본을 열어 실제 셀렉터를 확인한 뒤 해당 스크래퍼의 `_parse_list` 를 수정하면 됩니다.

판정은 세 가지입니다 — ✅ 정상 / 🟠 **부분 실패(목록 성공·상세 실패)** / ❌ 실패. 상세가 PDF 직접 다운로드인 소스(`fss_mgmt_notice`)는 본문이 비어도 정상으로 봅니다.

`better_reply` 는 구분마다 상세 endpoint 가 달라(`법령해석`→`LawreqDetail.do`, `비조치의견서`→`OpinionDetail.do`) **두 유형을 각각 한 건씩 검증**합니다. 한쪽만 성공하거나 한쪽 표본을 찾지 못하면 초록불이 아니라 🟠 부분 실패이며, 리포트에 유형별로 `OK / 실패 / 표본 없음` 이 표시됩니다. 표본은 페이지네이션 검증에서 이미 받아 둔 2페이지까지 재사용하므로 추가 요청이 생기지 않습니다. 아직 라이브 확인 전인 `OpinionDetail.do` + `muNo=117` 조합을 실제로 확인하는 수단이 이 검증입니다.

계류의안은 **available 검증과 pending 검증을 따로** 합니다. 목록 맨 위는 갓 접수된 의안이라 원문이 아직 없을 수 있어(등록 대기), 첫 건만 보면 '등록 대기'와 '수집 고장'을 구분할 수 없기 때문입니다. 표본 `--assembly-sample`(기본 3)건을 훑어 상태별로 세고 다음과 같이 판정합니다.

| 표본 결과 | 판정 | 뜻 |
|---|---|---|
| `failed > 0` | ❌ | 구조·endpoint 가 깨졌다 |
| `failed = 0`, `available > 0` | ✅ | 추출이 실제로 동작한다(등록 대기가 섞여도 무방) |
| `failed = 0`, `available = 0` | 🟠 | 전부 등록 대기 — 고장은 아니지만 추출을 **확인하지 못했다**. 표본을 늘려 재확인 |

GitHub Actions 에서는 `pending_bill_id` 입력에 **방금 접수돼 제안이유가 아직 없는 의안**을 넣으면, 그 의안이 `ERROR` 가 아니라 `PENDING` 으로 판정되는지 별도 게이트로 확인합니다.

의안 상세 endpoint 를 조사할 때는 `capture_bill_id`(또는 `capture_bill_url`) 입력을 함께 넣으세요. Playwright 로 브라우저를 띄워 심사정보 탭이 실제로 보내는 XHR 을 캡처하고, HAR·XHR 본문·`billDetail.js` 분석 보고서를 아티팩트로 올립니다(세션·토큰 값은 제거되고 헤더·필드 **이름은 유지**됩니다).

fixture 캡처(`capture_assembly_fixture.py`)도 같은 규칙을 씁니다. `meta.json` 은 **저장소에 커밋되므로** 기록하는 URL·폼 `action`·예외 메시지를 모두 정화하고, 캡처 대상 URL 도 로그에 찍기 전에 정화합니다 — `--url` 은 `billId` 외의 쿼리를 막지 않아 서명 URL 을 그대로 붙여넣을 수 있기 때문입니다(요청 자체는 원본으로 보냅니다).

정화는 HTML 은 `meta`/`input`, JSON 은 객체 키를 **이름 기준으로 재귀 탐색해** 값을 지웁니다 — 값 패턴만으로는 UUID·Base64 형 토큰을 잡지 못하고, 비밀은 `{"payload": {"csrfToken": …}}` 처럼 중첩되어 오기 때문입니다. 아티팩트에 기록되는 URL — 요청 URL·최종 URL·redirect 경로, **`Referer`·`Location` 처럼 값이 URL 인 헤더**, **`form@action`·`a@href`·`script@src` 같은 URL 속성**, 그리고 **예외 메시지·콘솔 로그에 박힌 URL**(Playwright 타임아웃 예외는 call log 에 전체 URL 을 담습니다) — 도 모두 정화합니다 — 쿼리뿐 아니라 `;jsessionid=…` 경로 파라미터까지 봅니다(쿠키가 막힌 클라이언트에 서블릿 컨테이너가 붙이는 자리입니다). `Referer` 는 보통 그 페이지의 전체 URL 을 담는데, 헤더 이름 자체는 비밀이 아니라 마스킹 대상이 아니고 값 패턴은 쿼리 파라미터 이름을 모르므로 URL 규칙을 따로 태워야 합니다. 값 패턴은 조립이 끝난 URL 이 아니라 **조각마다** 적용합니다. 통째로 돌리면 `JSESSIONID=[^;"'\s]+` 가 `?`·`&` 까지 삼켜 뒤따르는 쿼리가 사라집니다. 원본 HAR 은 `record_har_content="embed"` 라 쿠키·인증 헤더가 응답 본문째 들어 있으므로 **업로드 경로 밖(임시 디렉터리)** 에 만들고, 캡처가 중간에 죽어도 `finally` 로 디렉터리째 지웁니다. 아티팩트에는 정화본만 들어갑니다.

## 프로젝트 구조

```
config.yaml                 # 수신자·발송옵션·모니터링 대상 목록
.github/workflows/
  monitor.yml               # Dispatch 실행(수집→메일→state 커밋)
  verify.yml                # 수동 소스 점검
cloudflare-scheduler/       # Cron Trigger 및 GitHub Dispatch Worker
cloudflare-subscription/    # 수신 기관 설정 Worker + D1 마이그레이션 + 테스트(DEPLOY.md 배포 가이드)
google-apps-script/         # 인증번호 메일 발송 전용 Apps Script 웹앱 + Node 테스트
src/
  main.py                   # 오케스트레이션(수집→신규판별→enrich→메일→저장)
  config.py  fetcher.py     # 설정 로딩 / HTTP 세션(재시도·한글 인코딩·첨부 용량 제한)
  state.py   notifier.py    # 신규 판별 상태(원자적 저장) / 이메일 렌더·발송
  subscription.py           # 구독 API 조회·검증, 수신자 그룹화, 기관별 필터(기본 비활성)
  summarizer.py             # 일반 게시물 3줄 요약(1건당 1회, 실패 시 원문 발췌 폴백)
  assembly_summary.py       # 계류의안 배치 3줄 요약(최대 25건/요청, BILL_ID 로 매핑,
                            #   배치별 공정 시간배분 + 호출 텔레메트리)
  snippet.py                # 요약 실패 시 쓰는 원문 발췌 정제(제목 중복·상용구 제거)
                            #   + 의안 전용 다구간 발췌(build_assembly_fallback_lines)
  models.py                 # Post, Attachment
  scrapers/
    base.py                 # 스크래퍼 베이스(페이지네이션·디버그 덤프)
    fsc.py  fss.py          # 금융위 / 금감원 게시판
    fss_rule.py             # 금감원 세칙 제·개정 예고 상세의 원문 4개 항목 추출(AI 요약 없음)
    fss_admin_guidance.py   # 금감원 행정지도 예고 상세의 원문 4개 항목(fss_rule 엔진 + 항목 제목표)
    better_fsc.py assembly.py  # 회신사례(JSON 목록 + 상세 수집) / 계류의안(Open API + 제안이유 수집)
    pipc.py                 # 개인정보위 공지사항·보도자료(상세 URL 계약 기반 목록 파싱)
state/seen.json             # 이미 본 글 ID (자동 커밋)
scripts/
  send_test_email.py        # SMTP 설정 확인
  check_subscription_api.py # 구독 API 연동 점검(동기화 + 그룹 요약, 메일·state 변경 없음)
  preview_fss_rule.py       # 세칙·행정지도 예고 1건 단건 점검·메일 미리보기(발송·state·Gemini 없음)
  verify_sources.py         # 소스 점검(목록/상세를 구분해 판정)
  capture_assembly_network.py  # [진단] 의안 상세 XHR 캡처(Playwright, HAR·JS 분석)
  capture_assembly_fixture.py  # [진단] 의안 상세 HTML fixture 캡처(requests)
tests/fixtures/             # 캡처한 실제 응답(회귀 테스트용, README 참고)
  synthetic/                # 손으로 만든 구조 fixture(상태 판정기 검증용 — 캡처 아님)
  fss_rule/                 # 세칙 예고 상세 fixture(합성) + live/ 캡처 자리(README 참고)
  fss_admin_guidance/       # 행정지도 예고 상세 실제 응답 live/seqno_280.html(README 참고)
```

새 게시판을 추가하려면 `config.yaml` 의 `sources` 에 항목을 추가하고, 알맞은 `type`(파서)을 지정하면 됩니다.

### AI 요약 실패 시 원문 발췌

Gemini의 호출·재시도·모델 전환 정책은 그대로입니다. AI 요약이 없는 일반 게시물 카드에는 외부 API·모델·추가 라이브러리 없이 원문을 아래 순서로 싣습니다. 고른 문장은 재작성하지 않고 원문 순서대로 표시하며, 규칙으로 추정한 역할 라벨(변경 내용·대상·조건 등)은 붙이지 않습니다.

1. **금융규제포털 회답:** 수집기가 만드는 **질의요지 → 회답 → 이유** 구조가 완전하면 세 항목을 그대로 싣습니다. 회답은 600자, 질의·이유는 각 300자로 제한하고, 긴 섹션은 앞쪽의 완전한 문장과 결론이 있는 원문 끝을 남긴 채 중간 생략을 표시합니다. 설정의 소스 key와 무관하게 시작 표식·세 라벨 순서·각 내용으로 판별하며, 불완전한 데이터는 아래 일반 경로로 처리합니다. 수집기가 세 섹션이 모두 있어야 본문을 채우는 기존 계약은 그대로입니다.
2. **짧은 본문(정제 후 600자 이하):** 문장을 고르지 않고 전문을 싣습니다. 제목 중복, 머리말·꼬리말 메타데이터, 첨부 목록, 라벨·값·내비게이션 같은 구조적 안내 줄만 걷어내고, 길이는 후보 필터나 문장 선별 전 단계에서 잽니다. 기호 불릿(□ ○ 등)만 떼고 번호·글자 표식과 연도 없는 날짜(예: `10. 15.`)는 원문 그대로 둡니다. 부정·예외·숫자·날짜 문장과 소제목 텍스트도 남습니다. 표제는 **원문 발췌**입니다.
3. **장문(600자 초과):** 기존 규칙 기반 순위(정책 변화·대상·일정·수치·제목 관련성·중복도)로 원문 문장을 최대 3개 고르고 원문 순서로 싣습니다. 표제는 **원문 발췌 · N문장 선별**이고 "원문 문장을 자동으로 선별했습니다." 안내를 붙입니다. 300자를 넘는 문장은 어절 경계에서 앞·뒤를 남기고 생략 표시를 붙입니다.
4. **안전 발췌:** 위 경로가 결과를 내지 못하거나 오류가 나면 기존 220자 발췌를 싣습니다. 그마저 실패하면 원문 앞부분의 공백만 정리해 220자로 자릅니다. 메일 렌더링(HTML·텍스트) 중 한 게시물의 판정·발췌 오류는 게시물 식별자·예외 종류·발생 위치만 로그에 남기고(본문은 기록하지 않음) 다른 게시물과 다이제스트 생성을 막지 않습니다.

공백 없이 숫자로 시작해 숫자·쉼표·마침표가 100자를 넘게 이어지거나 공백 없는 토큰이 1,000자를 넘으면, 선별 정규식을 실행하기 전에 안전 발췌로 우회합니다. 셀마다 줄이 나뉜 큰 표도 선형 시간에 처리합니다.

HTML과 텍스트 메일은 같은 표제·문장·안내를 싣고, HTML은 AI 요약과 같은 카드 디자인을 씁니다. 구조화 항목(`details`)·AI 요약·첨부 안내(‘본문 안내’)·등록 대기의 표시 우선순위와 의안의 배경→개정→결론 발췌(‘제안이유 및 주요내용 발췌’)는 그대로입니다. Gemini에 전달하는 `Post.body`는 바꾸지 않습니다.

공용 정제 함수 `strip_edge_noise`는 본문 앞쪽에서 첨부 목록 라벨(`첨부파일 목록`, `첨부 파일 (2)` 등) 바로 뒤에 파일명이 오는 블록도 걷어냅니다. 같은 정제를 쓰는 `classify_body`를 거치므로, 그 블록을 걷어낸 뒤 첨부 참조 안내만 남는 글은 Gemini 요약 대상에서 빠지고 메일에서 ‘본문 안내’로 표시됩니다.

규칙 기반 선택이므로 장문에서는 중요한 문장이 빠지거나 덜 중요한 문장이 선택될 수 있습니다. 발췌는 원문 확인을 돕는 용도이며 법률적 결론을 새로 만들지 않습니다. 금융위·금감원·개보위의 실제 자료와 포털·의안의 기존 실제 fixture를 회귀 테스트에 사용합니다(`tests/fixtures/excerpts/README.md`). `Verify fallback excerpts`는 Secret이나 실제 API 호출 없이 전체 회귀 테스트를 실행합니다.
