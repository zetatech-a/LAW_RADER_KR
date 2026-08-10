# LAW_RADER_KR

**RADER** stands for **R**egulatory **A**lert **D**etection & **E**mail **R**eporter.

Note: RADER is an intentional acronym, not a misspelling of RADAR.

한국 금융 규제·입법 관련 게시판을 주기적으로 확인해, **새로 올라온 글의 URL·내용·첨부파일을 이메일로 자동 발송**하는 모니터링 프로그램입니다.

## 모니터링 대상 (9곳)

| key | 사이트 | 게시판 | 수집 방식 |
|---|---|---|---|
| `fsc_press` | 금융위원회 | 보도자료 | HTML |
| `fsc_legislation` | 금융위원회 | 입법예고/규정변경예고 | HTML |
| `fss_press` | 금융감독원 | 보도자료 | HTML |
| `fss_admin_guidance` | 금융감독원 | 행정지도 예고 | HTML |
| `fss_rule_amendment` | 금융감독원 | 세칙 제·개정 예고 | HTML |
| `fss_sanction` | 금융감독원 | 검사결과 제재 | HTML |
| `fss_mgmt_notice` | 금융감독원 | 경영유의사항 등 공시 | HTML |
| `better_reply` | 금융규제·법령해석포털 | 법령해석·비조치의견서 회신사례 | JSON (POST) |
| `assembly_bill` | 의안정보시스템 | 계류의안 | 열린국회 Open API (목록) + 상세페이지(제안이유 및 주요내용) |

목록 파서는 실제 사이트 HTML/응답으로 검증되어 제목·URL·날짜·본문·첨부를 수집합니다.
(`fss_mgmt_notice` 는 상세가 PDF 직접 다운로드, `better_reply` 는 상세가 JS 함수라 목록 링크로 안내)

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

**받아온 페이지가 그 의안의 것인지 먼저 확인합니다.** Open API 의 `LINK_URL` 이 다른 의안을 가리키거나 redirect 가 걸리면 A 의안 알림에 B 의안의 제안이유가 실릴 수 있기 때문입니다. 근거는 `form#form` 의 `billId`, 요청 URL 의 `billId`, 최종 응답 URL 의 `billId` 셋이며, 하나라도 어긋나면 `ERROR` 입니다. 구형 페이지의 inline 제안이유는 이 확인을 **통과했을 때만** 채택합니다(근거가 하나도 없으면 채택하지 않습니다 — `billInfo.do` 경로는 요청을 만들 때 `billId` 를 대조하지만 inline 경로는 그 검사를 거치지 않고 바로 발행되기 때문입니다).

진단 덤프(`debug/`)는 verify 워크플로가 아티팩트로 업로드하므로, 저장 전에 이름이 비밀인 `meta`/`input`(`_csrf`, `*token*`, `*session*` 등) 값과 인라인 스크립트를 지웁니다. 태그·필드 이름과 공개 값은 진단에 필요하므로 남깁니다.

`PENDING` 의안은 Gemini 배치 요약 대상에서 제외되고(요약할 원문이 없음), 의안 판정에서는 `available=0` 이어도 `pending>0` 이면 전면 실패 ERROR 를 남기지 않습니다(대신 실패가 섞여 있으면 경고).

전체 상세 수집의 `성공률 0%` 판정은 **등록 대기를 분모에서 뺀 뒤** 봅니다. '등록 대기가 하나라도 있으면 판정하지 않는다'로 두면, 갓 접수된 의안 한 건 때문에 등록 대기와 아무 관계 없는 다른 소스의 파서 전멸이 통째로 묻힙니다.

> pending 의안의 **재조회와 후속 메일 발송은 아직 구현되지 않았습니다.** 원문이 나중에 등록돼도 그 의안은 이미 `seen` 처리되어 다시 발송되지 않습니다.

## 동작 방식

1. **Cloudflare Cron Trigger**가 월~금 09:07–18:07 (KST), 15분 간격으로 GitHub `workflow_dispatch` API에 예약 실행을 요청합니다.
   GitHub-hosted runner가 요청을 받아 Python 모니터(`python -m src.main`)를 실행합니다. Cloudflare는 예약 요청만 담당하며 Python을 실행하지 않습니다.
2. 각 게시판 목록을 수집해 **이전에 못 본 새 글**만 골라냅니다.
   - 어떤 소스를 **처음** 볼 때는 현재 목록을 "기준선"으로만 저장하고 메일을 보내지 않습니다(과거 글 폭탄 방지). 그 다음 실행부터 진짜 신규만 발송합니다.
   - 주말·공휴일에 올라온 글은 **다음 실행(월요일 첫 회차)에 모아서** 발송됩니다. "이미 본 글 ID" 기준이라 시간과 무관합니다.
3. 새 글의 **상세 본문과 첨부파일**을 내려받습니다.
4. 본문이 있는 글은 **Gemini API로 3줄 요약**해 메일에 싣습니다. 원문을 앞에서 자른 발췌 대신 핵심만 보이게 하기 위함입니다.
   - **일반 게시물**(금융위·금감원)은 1건당 1회 호출합니다.
   - **계류의안**은 상세페이지에서 '제안이유 및 주요내용'을 수집한 뒤, 최대 **25건씩 한 번의 요청으로 묶어** 요약합니다(`llm.assembly_batch`). 의안은 하루 신규가 수십 건이라 1건당 1회로는 무료 티어 한도를 곧바로 소진하기 때문입니다. 결과는 배열 순서가 아니라 **BILL_ID로 매핑**하고, 검증(요청한 ID·중복·`llm.lines`줄·문장 길이)을 통과하지 못한 의안은 요약 없이 발췌로 나갑니다. 프롬프트의 출력 예시는 `llm.lines` 로부터 만듭니다 — 예시를 3줄로 못박아 두면 `lines` 를 바꿨을 때 규칙과 예시가 어긋나고, 모델이 예시를 따르면 검증이 **모든 의안을 버려** 배치 요약이 조용히 꺼집니다.
   - API 키(`GEMINI_API_KEY`)가 없거나 호출이 실패하면 **원문 발췌로 자동 대체**되고 메일은 정상 발송됩니다. 이때는 제목 중복과 담당부서·등록일·첨부파일 안내 같은 반복 안내문을 제외한 본문 첫 부분을 규칙 기반으로 발췌합니다(외부 호출 없음). 의안은 '제안이유 및 주요내용 발췌'로 표시됩니다.
   - 의안 배치 호출이 **호출 계층에서 실패**하면(401·403·5xx·네트워크) 남은 배치는 호출하지 않습니다. 같은 자격증명으로 같은 서비스를 다시 부를 뿐이라 실패만 쌓이며 시간예산을 태우고 메일이 그만큼 늦어집니다. 반대로 호출은 성공했고 **응답 '내용'이 문제인 경우**(깨진 JSON·스키마 위반·안전필터 차단 등)는 그 배치에 담긴 의안 내용에 달린 판정이라 다음 배치는 통과할 수 있으므로 멈추지 않습니다.
   - 상세 본문이 없는 소스(금융규제포털 회신사례)는 요약 대상이 아닙니다. 상세가 JS 팝업이라 수집할 대상 자체가 없으므로(`SUPPORTS_ENRICH = False`) **상세 수집 통계에서도 제외**합니다 — 실패로 세면 그 소스만 신규인 실행이 '성공률 0%' 로 잡혀 거짓 장애 경보가 울립니다.
   - 모델이 수명 종료돼 404가 나면 `llm.fallback_models` 순서로 자동 전환하고, 성공한 모델을 그 실행 동안 재사용합니다. 전부 실패해도 원문 발췌로 발송됩니다.
5. 신규가 있으면 **다이제스트 메일 1통**으로 묶어 발송합니다(첨부 포함).
6. "이미 본 글" 목록(`state/seen.json`)을 저장소에 커밋해 다음 실행에 이어씁니다.

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

> 신규 글이 있으면 **AI 요약을 호출하기 전에** ① 필수 Secret(`SMTP_USER`·`SMTP_PASSWORD`·`MAIL_TO`)이 채워졌는지, ② 그 값으로 실제 SMTP 로그인이 되는지를 먼저 확인합니다. 둘 중 하나라도 실패하면 요약 없이 즉시 실패로 끝냅니다 — 어차피 보낼 수 없는 메일에 Gemini 무료 할당량을 쓰지 않기 위함입니다. 앱 비밀번호를 폐기했거나 SMTP 호스트에 닿지 못하는 경우가 여기 걸립니다. 신규 글은 미확정으로 남아 복구한 다음 실행에 그대로 발송됩니다.

### 3) 수신자 설정
수신자 주소는 **저장소에 커밋하지 않습니다.** `MAIL_TO` Secret(여러 명은 `a@x.com, b@y.com`)으로만 지정하세요. 로컬 실행 시에는 `.env` 의 `MAIL_TO` 를 사용합니다(`.env` 는 커밋 제외).
여러 명에게 보내도 **수신자끼리는 서로의 주소를 볼 수 없습니다**(BCC 방식). 메일함에는 발신자가 `LAW RADER` 로 표시됩니다.

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

## 주요 설정값 (`config.yaml`)

| 항목 | 기본값 | 설명 |
|---|---|---|
| `fetch.list_limit` | 30 | 목록 한 페이지에서 확인할 건수 |
| `fetch.max_pages` | 10 | 한 실행에서 "이미 본 글" 경계까지 훑을 최대 페이지 수(평상시엔 1~2페이지에서 멈춤) |
| `fetch.baseline_pages` | 3 | 최초 기준선 수립 시 기록할 페이지 수 |
| `fetch.max_new_per_source` | 50 | 한 소스에서 한 번에 발송할 신규 상한(폭주 안전장치) |
| `email.max_attach_mb` | 15 | 메일에 첨부할 총 용량 상한. 초과분은 링크로만 안내 |
| `llm.enabled` | true | 본문 AI 요약 사용 여부. `false` 면 기존 원문 발췌로 발송 |
| `llm.model` | `gemini-flash-latest` | 요약에 쓸 기본 Gemini 모델. 특정 버전을 고정하면 그 버전 수명 종료일에 전 요청이 404가 되므로 공식 latest alias 사용 |
| `llm.fallback_models` | `gemini-3.6-flash`, `gemini-3.5-flash-lite` | `model` 을 쓸 수 없을 때(404 / `NOT_FOUND` / 모델 부재 메시지) 이 **순서대로** 넘어갑니다. `[]` 로 두면 대체 없음 |
| `llm.lines` | 3 | 요약 문장 수 |
| `llm.max_posts` | 40 | 한 실행에서 요약할 최대 글 수(무료 티어 일일 한도 보호). 초과분은 원문 발췌 |
| `llm.rpm` | 10 | 분당 요청 상한. 이 간격에 맞춰 호출을 벌립니다 |
| `llm.max_consecutive_failures` | 3 | 연속 실패가 이만큼 쌓이면 요약을 중단하고 남은 글은 원문 발췌로 발송(LLM 전면 장애 시 메일 지연 방지) |
| `llm.budget_sec` | 240 | 요약 단계 전체 시간예산(초). 개별 요청은 단조시계 마감으로 강제 중단되고 재시도 대기도 남은 예산 안으로 잘리므로, 요약 단계 소요시간의 실질 상한 |

## 알려진 한계

- **예약 실행 지연:** Cloudflare가 예약 시각에 Dispatch를 요청해도 GitHub-hosted runner의 실제 시작 시각은 부하에 따라 늦을 수 있으며 정확한 시작 시각은 보장되지 않습니다.
- **AI 요약의 정확도:** 3줄 요약은 생성형 AI가 만든 것으로 부정확하거나 누락이 있을 수 있습니다. 메일 하단에도 같은 안내가 표시되며, 판단 전에는 반드시 원문 링크를 확인해야 합니다.
- **Gemini 모델 수명 종료:** Google이 특정 모델을 종료하면 그 모델 호출은 404가 됩니다. 기본값은 `gemini-flash-latest` alias이고 `llm.fallback_models` 로 자동 전환하지만, alias와 대체 모델이 모두 막히면 해당 회차는 원문 발췌로만 발송됩니다(메일 자체는 정상 발송). 로그의 `모델 … 사용 불가` / `요약 모델 확정: …` 로 확인하고 `config.yaml` 의 모델 목록을 갱신하세요.
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
```

## 소스 점검 (파서가 깨졌을 때)

사이트 개편 등으로 특정 소스가 `파싱 0건` 으로 나오면, 메일·상태 변경 없이 점검만 하는 검증 도구를 사용합니다.

- **GitHub Actions:** Actions 탭 → **"Verify sources (live)"** → **Run workflow** → 완료 후 `verify-results` 아티팩트에서 리포트(`verify_report.txt`)와 원본 HTML(`debug/`) 확인
- **로컬:**
  ```bash
  python scripts/verify_sources.py                  # 전체
  python scripts/verify_sources.py --only fss_press # 특정 소스
  ```

리포트는 소스별로 **① 목록 건수 ② 제목 샘플 ③ 페이지네이션 동작 ④ 본문/첨부 추출 여부** 를 보여줍니다. 0건인 소스는 `debug/<key>_list.txt` 원본을 열어 실제 셀렉터를 확인한 뒤 해당 스크래퍼의 `_parse_list` 를 수정하면 됩니다.

판정은 세 가지입니다 — ✅ 정상 / 🟠 **부분 실패(목록 성공·상세 실패)** / ❌ 실패. 상세가 PDF 직접 다운로드(`fss_mgmt_notice`)거나 JS 팝업(`better_reply`)인 소스는 본문이 비어도 정상으로 봅니다.

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
src/
  main.py                   # 오케스트레이션(수집→신규판별→enrich→메일→저장)
  config.py  fetcher.py     # 설정 로딩 / HTTP 세션(재시도·한글 인코딩·첨부 용량 제한)
  state.py   notifier.py    # 신규 판별 상태(원자적 저장) / 이메일 렌더·발송
  summarizer.py             # 일반 게시물 3줄 요약(1건당 1회, 실패 시 원문 발췌 폴백)
  assembly_summary.py       # 계류의안 배치 3줄 요약(최대 25건/요청, BILL_ID 로 매핑)
  snippet.py                # 요약 실패 시 쓰는 원문 발췌 정제(제목 중복·상용구 제거)
  models.py                 # Post, Attachment
  scrapers/
    base.py                 # 스크래퍼 베이스(페이지네이션·디버그 덤프)
    fsc.py  fss.py          # 금융위 / 금감원 게시판
    better_fsc.py assembly.py  # 회신사례(JSON) / 계류의안(Open API + 제안이유 수집)
state/seen.json             # 이미 본 글 ID (자동 커밋)
scripts/
  send_test_email.py        # SMTP 설정 확인
  verify_sources.py         # 소스 점검(목록/상세를 구분해 판정)
  capture_assembly_network.py  # [진단] 의안 상세 XHR 캡처(Playwright, HAR·JS 분석)
  capture_assembly_fixture.py  # [진단] 의안 상세 HTML fixture 캡처(requests)
tests/fixtures/             # 캡처한 실제 응답(회귀 테스트용, README 참고)
  synthetic/                # 손으로 만든 구조 fixture(상태 판정기 검증용 — 캡처 아님)
```

새 게시판을 추가하려면 `config.yaml` 의 `sources` 에 항목을 추가하고, 알맞은 `type`(파서)을 지정하면 됩니다.
