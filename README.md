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
| `assembly_bill` | 의안정보시스템 | 계류의안 | 열린국회 Open API |

파서는 모두 실제 사이트 HTML/응답으로 검증되어 제목·URL·날짜·본문·첨부를 수집합니다.
(`fss_mgmt_notice` 는 상세가 PDF 직접 다운로드, `better_reply` 는 상세가 JS 함수라 목록 링크로 안내)

## 동작 방식

1. **Cloudflare Cron Trigger**가 월~금 09:07–18:07 (KST), 15분 간격으로 GitHub `workflow_dispatch` API에 예약 실행을 요청합니다.
   GitHub-hosted runner가 요청을 받아 Python 모니터(`python -m src.main`)를 실행합니다. Cloudflare는 예약 요청만 담당하며 Python을 실행하지 않습니다.
2. 각 게시판 목록을 수집해 **이전에 못 본 새 글**만 골라냅니다.
   - 어떤 소스를 **처음** 볼 때는 현재 목록을 "기준선"으로만 저장하고 메일을 보내지 않습니다(과거 글 폭탄 방지). 그 다음 실행부터 진짜 신규만 발송합니다.
   - 주말·공휴일에 올라온 글은 **다음 실행(월요일 첫 회차)에 모아서** 발송됩니다. "이미 본 글 ID" 기준이라 시간과 무관합니다.
3. 새 글의 **상세 본문과 첨부파일**을 내려받습니다.
4. 본문이 있는 글은 **Gemini API로 3줄 요약**해 메일에 싣습니다. 원문을 앞에서 자른 발췌 대신 핵심만 보이게 하기 위함입니다.
   - API 키(`GEMINI_API_KEY`)가 없거나 호출이 실패하면 **기존 원문 발췌로 자동 대체**되고 메일은 정상 발송됩니다.
   - 상세 본문이 없는 소스(금융규제포털 회신사례·계류의안)는 요약 대상이 아닙니다.
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

> 필수 Secret(`SMTP_USER`·`SMTP_PASSWORD`·`MAIL_TO`)이 하나라도 비어 있으면 실행이 **AI 요약을 호출하기 전에** 실패로 끝납니다. 어차피 보낼 수 없는 메일에 Gemini 무료 할당량을 쓰지 않기 위함이며, 신규 글은 미확정으로 남아 설정을 채운 다음 실행에 그대로 발송됩니다.

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
| `llm.model` | `gemini-2.5-flash` | 요약에 쓸 Gemini 모델. 무료 한도가 빠듯하면 `gemini-2.5-flash-lite` |
| `llm.lines` | 3 | 요약 문장 수 |
| `llm.max_posts` | 40 | 한 실행에서 요약할 최대 글 수(무료 티어 일일 한도 보호). 초과분은 원문 발췌 |
| `llm.rpm` | 10 | 분당 요청 상한. 이 간격에 맞춰 호출을 벌립니다 |
| `llm.max_consecutive_failures` | 3 | 연속 실패가 이만큼 쌓이면 요약을 중단하고 남은 글은 원문 발췌로 발송(LLM 전면 장애 시 메일 지연 방지) |
| `llm.budget_sec` | 240 | 요약 단계 전체 시간예산(초). 개별 요청의 타임아웃·재시도 대기도 남은 예산 안으로 잘리므로 요약 단계 소요시간의 실질 상한 |

## 알려진 한계

- **예약 실행 지연:** Cloudflare가 예약 시각에 Dispatch를 요청해도 GitHub-hosted runner의 실제 시작 시각은 부하에 따라 늦을 수 있으며 정확한 시작 시각은 보장되지 않습니다.
- **AI 요약의 정확도:** 3줄 요약은 생성형 AI가 만든 것으로 부정확하거나 누락이 있을 수 있습니다. 메일 하단에도 같은 안내가 표시되며, 판단 전에는 반드시 원문 링크를 확인해야 합니다.
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
  summarizer.py             # 본문 3줄 요약(Gemini REST API, 실패 시 원문 발췌 폴백)
  models.py                 # Post, Attachment
  scrapers/
    base.py                 # 스크래퍼 베이스(페이지네이션·디버그 덤프)
    fsc.py  fss.py          # 금융위 / 금감원 게시판
    better_fsc.py assembly.py  # 회신사례(JSON) / 계류의안(Open API)
state/seen.json             # 이미 본 글 ID (자동 커밋)
scripts/
  send_test_email.py        # SMTP 설정 확인
  verify_sources.py         # 소스 점검
```

새 게시판을 추가하려면 `config.yaml` 의 `sources` 에 항목을 추가하고, 알맞은 `type`(파서)을 지정하면 됩니다.
