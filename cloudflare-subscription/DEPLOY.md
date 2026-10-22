# 수신 기관 설정 서비스 배포 가이드 (운영자용)

LAW RADER KR 기존 구독자가 이메일 인증 후 받을 기관을 고르는 서비스를 **월 0원**으로 배포하는 절차입니다.
개발 지식 없이 따라 할 수 있도록 순서대로 적었습니다. 각 단계의 **확인** 항목이 맞지 않으면 다음 단계로 넘어가지 마세요.

> **비밀값 원칙** — 아래에서 만드는 토큰·키는 **채팅·메일·이슈·코드·문서에 붙여 넣지 마세요.**
> 생성한 값은 비밀번호 관리자에 보관하고, Cloudflare 는 `wrangler secret put` 프롬프트에, GitHub 는
> 저장소 설정 화면에, Google 은 스크립트 속성 화면에 **직접** 입력합니다.

## 구성 요약

| 구성요소 | 역할 | 비용 |
|---|---|---|
| Cloudflare Worker `law-rader-subscription` (workers.dev) | 설정 페이지, 인증번호 발급·검증, 설정 저장, Python 용 내부 API | Workers Free |
| Cloudflare D1 `law-rader-subscription` | 구독자 식별자(HMAC)·설정·인증 상태 저장(이메일 원문 없음) | D1 Free |
| Google Apps Script 웹앱 | 인증번호 메일 발송만(서명된 요청만 처리) | 무료(MailApp) |
| 기존 GitHub Actions + Python | 정기 수집·발송(변경 없음), 발송 전 구독 설정 조회 | 기존과 동일 |

기존 `cloudflare-scheduler`(예약 실행)는 건드리지 않습니다. 구독 Worker 는 별도 Worker 입니다.

### 준비물
- Cloudflare 무료 계정, Google 계정(**인증번호 발송 전용 무료 Gmail 계정을 새로 만드는 것을 권장** — 정기 메일 발송 계정과 평판·한도를 분리)
- PC 터미널, Node.js 22 이상, 이 저장소 사본
- 비밀값 생성: 터미널에서 `openssl rand -hex 32` (실행할 때마다 64자 값이 나옵니다)

### 만들 비밀값 (5개)

| 이름 | 만드는 법 | 등록 위치 |
|---|---|---|
| `INTERNAL_API_TOKEN` | `openssl rand -hex 32` | Cloudflare Secret **와** GitHub Secret `SUBSCRIPTION_API_TOKEN` (같은 값) |
| `SUBSCRIBER_HASH_KEY` | `openssl rand -hex 32` | Cloudflare Secret **와** GitHub Secret `SUBSCRIBER_HASH_KEY` (같은 값) — **한 번 정하면 바꾸지 않음** |
| `SERVER_SECRET` | `openssl rand -hex 32` | Cloudflare Secret 만 |
| `APPS_SCRIPT_SHARED_SECRET` | `openssl rand -hex 32` | Cloudflare Secret **와** Apps Script 스크립트 속성 `OTP_SHARED_SECRET` (같은 값) |
| `APPS_SCRIPT_URL` | 8단계에서 생기는 웹앱 주소 | Cloudflare Secret 만 |

---

## 1. Cloudflare 무료 계정 준비
1. https://dash.cloudflare.com 에서 가입합니다. **결제 수단을 등록하지 않습니다**(무료 한도를 넘으면 오류가 날 뿐 자동 과금되지 않습니다).
   **기존 `law-rader-kr-scheduler` 가 있는 계정과 다른, 새 무료 계정을 권장합니다.** Workers·D1 무료 한도는 계정 단위라,
   공개 설정 페이지에 요청이 몰려 한도가 소진되면 같은 계정의 예약 실행 Worker 까지 영향을 받을 수 있습니다
   (예약 실행이 요청 한도에 어떻게 집계되는지는 공식 문서로 확인하지 못했습니다).
   > **별도 계정으로도 막지 못하는 위험(구조적 한계):** 계정을 나누면 예약 실행 Worker 는 보호되지만, 구독 Worker 자신의
   > 하루 요청 한도(100,000건) 소진은 막지 못합니다. 한도가 소진되면 Python 의 내부 동기화 요청도 실패하고, fail-closed 정책에
   > 따라 **그날 KST 09시까지 정기 메일이 보류**됩니다(전체 발송으로 되돌아가지 않음). 요청은 Worker 코드가 실행되기 전에
   > 집계되므로 코드로는 막을 수 없고, 무료 플랜에서는 workers.dev 앞단 WAF 요청 제한도 쓸 수 없습니다. 사용량을 주기적으로 확인하세요.
2. 왼쪽 메뉴 **Workers & Pages** 를 한 번 열어 workers.dev 하위 도메인을 정합니다(예: `myname`). 주소는 `https://law-rader-subscription.myname.workers.dev` 형태가 됩니다.
3. 터미널:
   ```bash
   cd cloudflare-subscription
   npm ci
   npx wrangler login        # 브라우저가 열리면 Cloudflare 로그인·허용
   npx wrangler whoami       # 계정 이름이 보이면 성공
   ```

## 2. D1 데이터베이스 생성
```bash
npx wrangler d1 create law-rader-subscription
```
출력된 `database_id` 를 `wrangler.jsonc` 의 `"database_id": "00000000-..."` 자리에 넣습니다(비밀값 아님).

## 3. D1 마이그레이션 적용
```bash
npx wrangler d1 migrations apply law-rader-subscription --remote
```
**확인:** `0001_init.sql ✅`. 대시보드 **Storage & Databases → D1 → law-rader-subscription → Tables** 에
`subscribers, agencies, preferences, otp_challenges, sessions, rate_limits` 가 보입니다.

## 4. Worker 환경변수·Secret 등록
1. `wrangler.jsonc` 의 `PUBLIC_ORIGIN` 을 1단계에서 정한 실제 주소로 바꿉니다.
   예: `"PUBLIC_ORIGIN": "https://law-rader-subscription.myname.workers.dev"` (끝에 `/` 없음)
   — 이 값이 틀리면 모든 로그인·저장 요청이 거절됩니다(안전한 실패).
2. 먼저 한 번 배포합니다(5단계). Secret 이 없는 동안 Worker 는 모든 기능을 `server_misconfigured` 로 거절합니다.
3. Secret 을 하나씩 등록합니다. 명령을 실행하면 값을 묻는 프롬프트가 나오며, 입력값은 화면·기록에 남지 않습니다.
   ```bash
   npx wrangler secret put INTERNAL_API_TOKEN
   npx wrangler secret put SUBSCRIBER_HASH_KEY
   npx wrangler secret put SERVER_SECRET
   npx wrangler secret put APPS_SCRIPT_SHARED_SECRET
   ```
   (`APPS_SCRIPT_URL` 은 8단계 후에 등록합니다.) 모든 Secret 은 32자 이상이어야 하며, 짧으면 해당 기능이 거절됩니다.
   Secret 을 등록하면 새 버전이 즉시 배포됩니다.

## 5. workers.dev 설정 페이지 준비
```bash
npx wrangler deploy
```
**확인:** 브라우저로 `https://law-rader-subscription.myname.workers.dev/` 를 열면 '수신 기관 설정' 화면이 나옵니다.
아직 구독자가 동기화되지 않았으므로 어떤 주소로도 인증번호가 오지 않는 것이 정상입니다.

## 6. Google Apps Script 프로젝트 생성
1. 인증번호 발송용 Google 계정으로 https://script.google.com → **새 프로젝트**. 이름: `LAW RADER OTP`.
2. 기본 `Code.gs` 내용을 지우고 저장소의 `google-apps-script/Code.gs` 전체를 붙여 넣습니다.
3. **프로젝트 설정(톱니바퀴)** → **"appsscript.json" 매니페스트 파일을 편집기에 표시** 체크 → 편집기의 `appsscript.json` 을
   저장소의 `google-apps-script/appsscript.json` 내용으로 바꿉니다(시간대 Asia/Seoul, 메일 발송 권한만 요청).
4. **프로젝트 설정 → 스크립트 속성 → 속성 추가**: 이름 `OTP_SHARED_SECRET`, 값 = `APPS_SCRIPT_SHARED_SECRET` 와 같은 값.
5. 저장(Ctrl+S).

## 7. MailApp 최초 권한 승인
1. 편집기 상단 함수 목록에서 `doGet` 을 고르고 **실행** 합니다(메일을 보내지 않는 함수입니다).
2. 권한 검토 창 → 계정 선택 → "Google에서 확인하지 않은 앱" 경고가 나오면 **고급 → (안전하지 않음)으로 이동** — 본인이 만든 스크립트이므로 정상입니다.
3. 요청 권한이 **"내 계정으로 이메일 전송"** 하나인지 확인하고 허용합니다. 다른 권한을 요구하면 중단하고 코드를 다시 확인하세요.

## 8. Apps Script 웹앱 배포
1. **배포 → 새 배포 → 유형: 웹 앱**
2. **다음 사용자 인증 정보로 실행: 나**, **액세스 권한이 있는 사용자: 모든 사용자**(로그인하지 않은 사용자 포함 — Worker 가 Google 로그인 없이 호출하기 때문입니다).
   웹앱 주소는 공개된 것으로 간주하며, **서명이 맞지 않는 요청은 메일을 보내지 않습니다.**
3. **배포** → 나오는 **웹 앱 URL**(`https://script.google.com/macros/s/.../exec`)을 복사합니다.
4. 코드를 고칠 때는 **배포 관리 → 수정(연필) → 버전: 새 버전 → 배포** 로 같은 주소를 유지합니다(새 배포를 만들면 주소가 바뀝니다).

**검증(메일 발송 없음) — 리다이렉트·JSON 응답 확인.** 공식 문서상 웹앱 응답은 `script.googleusercontent.com` 의
일회용 주소로 리다이렉트됩니다. 일부러 잘못된 요청을 보내 응답 형식만 확인합니다:
```bash
read -r APPS_URL   # 웹 앱 URL 붙여 넣기(이 주소도 공개하지 않습니다)
curl -sSL \
  -H 'Content-Type: application/json' \
  --data '{}' \
  "$APPS_URL"
```
(`-X POST` 를 쓰지 않습니다 — `-X POST` 는 리다이렉트 뒤에도 POST 를 강제할 수 있어, 리다이렉트 후 GET 으로 결과를 받는
웹앱 응답 방식과 다르게 동작할 수 있습니다. `--data` 만으로 첫 요청은 POST 가 됩니다.)
**확인:** `{"ok":false,"error":"bad_request"}` 가 나와야 합니다. HTML(로그인 페이지 등)이 나오면 8-2 의 액세스 설정을 다시 확인하세요.
> 이 동작(POST → 리다이렉트 → JSON 본문)은 개발 단계에서 실제 Google 서버로 검증하지 못했습니다. 이 확인이 통과해야 다음 단계로 갑니다.

## 9. Worker 와 Apps Script 연결
```bash
npx wrangler secret put APPS_SCRIPT_URL     # 8단계 웹 앱 URL 입력
```
Worker 는 `https://script.google.com/macros/s/<ID>/exec` 형식이 아닌 주소로는 요청하지 않습니다.

## 10. GitHub Secrets/Variables 등록
저장소 **Settings → Secrets and variables → Actions**

| 종류 | 이름 | 값 |
|---|---|---|
| Secret | `SUBSCRIPTION_API_URL` | `https://law-rader-subscription.myname.workers.dev/internal/sync-and-resolve` |
| Secret | `SUBSCRIPTION_API_TOKEN` | `INTERNAL_API_TOKEN` 과 같은 값 |
| Secret | `SUBSCRIBER_HASH_KEY` | Worker 와 같은 값 |
| Variable | `SUBSCRIPTION_PAGE_URL` | `https://law-rader-subscription.myname.workers.dev/` |
| Variable | `SUBSCRIPTION_DB_INSTANCE` | **지금은 비워 둡니다**(14단계에서 운영 D1 식별자 등록) |
| Variable | `SUBSCRIPTION_REQUIRED` | **지금은 비워 둡니다**(14단계 전환 직전에 `true`) |

`config.yaml` 의 `subscription.enabled` 가 `false` 인 동안에는 이 값들이 있어도 정기 발송은 지금과 똑같습니다.

## 11. 테스트 이메일로 인증번호 확인
인증번호는 **동기화된 활성 구독자에게만** 발송됩니다. 운영 전환 전이므로 **본인 시험 주소 하나만** 동기화합니다
(D1 이 비어 있는 지금만 안전합니다 — 동기화는 넘긴 목록에 없는 구독자를 비활성화합니다).
```bash
cd ..   # 저장소 루트
read -rs SUBSCRIPTION_API_TOKEN; export SUBSCRIPTION_API_TOKEN
read -rs SUBSCRIBER_HASH_KEY;    export SUBSCRIBER_HASH_KEY
export SUBSCRIPTION_API_URL=https://law-rader-subscription.myname.workers.dev/internal/sync-and-resolve
export SUBSCRIPTION_PAGE_URL=https://law-rader-subscription.myname.workers.dev/
export MAIL_TO=본인시험주소@gmail.com
python scripts/check_subscription_api.py
```
**확인:** `성공: 그룹 1개 / all: 1명`. 함께 출력되는 `구독 DB 식별자` 는 이 D1 의 고유값입니다(비밀값 아님, 14단계에서 사용).
그다음 설정 페이지에서 시험 주소를 입력 → 메일(스팸함 포함)의 6자리 번호 입력 → 설정 화면이 열리면 성공입니다.
등록되지 않은 다른 주소를 넣으면 화면 안내는 같지만 메일은 오지 않아야 합니다.

## 12. 기관별 선택 저장 검증
1. 설정 화면에서 **선택한 기관만 수신 → 의안정보시스템** 만 체크 → **설정 저장** → "저장되었습니다" 확인.
2. 페이지를 새로 고쳐 같은 설정이 보이는지 확인.
3. 11단계 명령을 다시 실행 → `selected:assembly: 1명` 이 나오면 Python 이 같은 설정을 읽고 있는 것입니다.
4. **모든 기관 수신** 으로 되돌려 저장해 둡니다.

## 13. Python 구독 API 연동 테스트
- 11·12단계의 `scripts/check_subscription_api.py` 는 정기 실행과 **같은 코드**로 동기화·검증·그룹화를 수행합니다(메일·state 변경 없음).
- 실패 시 원인(메시지 괄호 안 코드):

  | 메시지 | 뜻 | 조치 |
  |---|---|---|
  | `HTTP 401 (unauthorized)` | 내부 API 토큰 불일치 | GitHub `SUBSCRIPTION_API_TOKEN` 과 Worker `INTERNAL_API_TOKEN` 을 같은 값으로 |
  | `HTTP 409 (hash_key_mismatch)` | GitHub 와 Worker 의 `SUBSCRIBER_HASH_KEY` 가 다름 | 두 곳에 같은 값을 다시 등록 |
  | `HTTP 409 (key_changed)` | 두 키가 **함께** 바뀜(최초 동기화 때 키와 다름) | 원래 키로 되돌리기. 의도한 교체는 아래 'HMAC 키 정책' |
  | `HTTP 409 (instance_missing)` | D1 이 재생성·교체되어 기록된 식별자가 없음 | 아래 'D1 백업·복구' 로 원래 데이터 복원. 의도한 재생성이면 '의도적 변경 절차' |
  | `HTTP 409 (instance_mismatch)` / `구독 DB 식별자가 … 일치하지 않습니다` | 다른 D1 에 연결됨 | `wrangler.jsonc` 의 database_id·`SUBSCRIPTION_DB_INSTANCE` 확인 |
  | `HTTP 409 (catalog_mismatch)` | 구독자가 고른 기관이 `config.yaml` 카탈로그에서 빠짐 | 기관을 되돌리거나 '기관 제거 절차' 수행 |
  | `HTTP 409 (preference_missing)` | 설정을 저장한 적 있는 구독자의 설정 행이 사라짐 | 'D1 백업·복구' 로 복원(운영자 승인 후) |
  | `HTTP 500 (server_misconfigured)` | Worker Secret 누락·짧음 | 4단계 Secret 확인 |
  | `응답에서 구독자 N명 누락`·`시간 초과` | 계약 위반·네트워크 | Worker 로그·상태 확인 |
- Secret 은 `wrangler secret put` 의 **대화형 프롬프트로 입력**하세요. `echo 값 | wrangler secret put` 처럼 파이프로 넣으면
  줄바꿈이 함께 저장될 수 있어(미검증) 401·409 의 원인이 됩니다.

## 14. 실제 운영 기능 활성화

순서가 중요합니다. **사용자가 설정을 바꿀 수 있게 되기 전에(=메일에 설정 링크가 붙기 전에) 보호장치를 먼저 켭니다.**
설정 링크는 `enabled: true` 일 때만 메일에 붙습니다.

**보호장치의 전제와 한계(먼저 이해할 것)**
- `SUBSCRIPTION_REQUIRED` 검사는 ① main 의 Python 코드(이 기능이 들어간 버전) ② `monitor.yml` 의 `SUBSCRIPTION_REQUIRED`·
  `SUBSCRIPTION_DB_INSTANCE` env 줄 ③ GitHub Variable — **세 가지가 모두 있어야** 작동합니다. 하나라도 없으면 조용히 꺼집니다.
- **현재 운영 main(이 기능 이전 코드)은 `SUBSCRIPTION_REQUIRED` 를 읽지 않습니다.** 코드를 반영하기 전에 Variable 만 바꿔도 아무 효과가 없습니다.
- main 을 이 기능 이전 코드로 되돌리면 보호장치도 함께 사라지고, MAIL_TO 전원(일시중지한 사람 포함)에게 전체 메일이 재개됩니다(아래 '롤백').

### 14-A. 스테이징 검증(전환 전)
1. 11~13단계를 모두 통과합니다(시험 주소만 동기화된 상태, 운영 메일 발송 코드는 아직 기존 그대로).
2. 다음도 시험 주소로 확인합니다: 의안만 선택 → 점검 스크립트 `selected:assembly`, 일시중지 → `selected:일시중지`, 전체 수신 복귀.

### 14-B. 준비(운영 메일에는 아직 영향 없음)
3. 운영 `MAIL_TO` 의 주소가 모두 `이름 <주소>` 형식이 아닌 **주소만**으로 되어 있는지 확인합니다(형식이 다르면 전환 후 발송이 중단됩니다).
4. 운영 `MAIL_TO` 값으로 11단계 점검 스크립트를 실행합니다(시험 주소가 MAIL_TO 에 없으면 이때 비활성화됩니다).
   출력된 `구독 DB 식별자` 를 GitHub Variable **`SUBSCRIPTION_DB_INSTANCE`** 에 등록합니다.
5. 이 기능 브랜치를 평소 절차(별도 승인)로 main 에 반영합니다. **`config.yaml` 의 `enabled: false` 그대로** 반영하므로
   `SUBSCRIPTION_REQUIRED` 가 비어 있는 동안 운영 발송은 기존과 같습니다. 반영 후 정기 실행 1회가 정상인지 확인합니다.

### 14-C. 전환(짧은 시간 안에 이어서)
6. GitHub Variable **`SUBSCRIPTION_REQUIRED` = `true`**. 이 순간부터 `enabled: false` 인 main 은 **실행을 멈춥니다**
   (의도된 일시 중단 — 메일·state 변경 없음, 신규 글은 미확정으로 남아 전환 후 발송).
7. `config.yaml` 의 `subscription.enabled: true` 변경을 main 에 반영합니다. 다음 실행부터 구독 설정에 따라 발송합니다.
   6과 7 사이가 길어지면 한 게시판의 신규가 50건(의안 75건)을 넘어 일부가 발송 없이 확정될 수 있으니 바로 이어서 진행합니다.
8. 첫 정기 실행 로그에서 `구독 설정 조회 완료 — 수신자 N명 / 그룹 1개 (전체 수신 N명 …)` 를 확인합니다(409·실패 없음).
   이 시점에는 모두 `mode=all` 이므로 받는 메일 내용은 전과 같고, 하단에 '수신 기관 설정' 링크가 붙습니다.

### 14-D. 공개
9. 정상 확인 후 구독자에게 링크로 설정할 수 있음을 안내합니다.

## 15. 장애 복구 및 비용 사용량 확인

### 사용량 확인(월 1회 권장)
| 항목 | 위치 | 무료 한도(2026-10 공식 문서 기준) |
|---|---|---|
| Worker 요청 | Workers & Pages → law-rader-subscription → Metrics | 하루 100,000건(UTC 0시 = KST 09시 초기화) |
| D1 읽기·쓰기 | D1 → law-rader-subscription → Metrics | 하루 읽기 500만 행·쓰기 10만 행 |
| 인증번호 메일 | Apps Script → 실행 | 일반 Gmail MailApp 하루 100명(자체 상한 30통) |

정상 운영 예상치(구독자 50명 기준): Worker 요청 하루 수십~수백 건, D1 읽기 하루 수천 행, 쓰기는 거의 0행입니다.

### 장애별 대응
| 증상 | 영향 | 조치 |
|---|---|---|
| Actions 로그 `구독 설정 조회 실패(...)` | **해당 실행의 메일 보류**(전체 발송으로 복귀하지 않음). 신규 글은 미확정으로 남아 복구 후 발송 | Worker 상태·Secret 확인. 복구되면 다음 실행이 자동 발송 |
| `HTTP 503`·D1 오류 | 위와 같음 | D1 무료 한도 초과면 KST 09시에 자동 해제. 대시보드에서 이상 요청 확인 |
| `HTTP 409`(hash_key_mismatch) | 해당 실행의 메일 보류(전원 전체 발송 방지) | GitHub Secret 과 Worker Secret 의 `SUBSCRIBER_HASH_KEY` 를 같은 값으로 다시 등록 |
| 설정 페이지만 하루 종일 `잠시 후 다시 시도` | 설정 변경 불가(정기 메일 영향 없음) | 전체 요청 예산(인증번호 요청 1,000·검증 2,000/일) 소진 — 공격 가능성. KST 09시 해제 |
| 인증번호 메일이 안 옴 | 설정 변경만 불가(정기 메일 영향 없음) | Apps Script 실행 기록의 거절 코드(`bad_signature`·`quota_exhausted`·`daily_cap`) 확인 |
| `SUBSCRIPTION_REQUIRED=true 인데 ... false` | 실행 중단(메일·state 변경 없음) | `enabled: true` 로 되돌리기 |
| `구독 설정 누락: SUBSCRIPTION_DB_INSTANCE(...)` | 실행의 메일 보류 | 14-B-4 의 식별자를 Variable 에 등록 |
| `HTTP 409 (instance_missing·key_changed·catalog_mismatch·preference_missing)` | 메일 보류(전원 전체 발송 방지) | 13단계 표의 조치 |

장기 장애로 한 게시판의 신규가 50건(의안 75건)을 넘으면, 복구 후 최신 분만 발송되고 나머지는 기존 안전장치에 따라 확정됩니다.

### 롤백
- **`enabled: false` 는 장애 대응이 아닙니다.** `SUBSCRIPTION_REQUIRED=true` 상태에서는 실행이 멈추고(아래 긴급 정지),
  `SUBSCRIPTION_REQUIRED` 가 비어 있으면 사용자 선택(일시중지 포함)을 무시하고 전원에게 전체 메일을 보냅니다.
- **긴급 정지(모든 메일을 잠시 멈춤):** 가장 단순한 방법은 `SUBSCRIPTION_REQUIRED=true` 인 상태에서 `enabled: false` 를 main 에
  반영하는 것입니다(실행이 시작 직후 멈춤, state 변경 없음). 또는 `law-rader-kr-scheduler` 의 Cron 트리거와 GitHub Actions monitor 워크플로를 끕니다.
- **main 을 이 기능 이전 코드로 되돌릴 때(위험):** 되돌린 코드에는 보호장치가 없어 **MAIL_TO 전원에게 전체 메일이 재개**됩니다. 반드시
  ① 긴급 정지로 먼저 발송을 멈추고 ② 구독자에게 공지하고 ③ D1 에서 일시중지·선택 수신 현황을 확인해 필요하면 MAIL_TO 를 정리한 뒤
  ④ 코드를 되돌리고 ⑤ 발송을 재개합니다. 가능하면 되돌리기 대신 문제 부분만 고치는 반영(fix-forward)을 우선합니다.

### D1 백업·복구(무료)
[공식 문서](https://developers.cloudflare.com/d1/reference/time-travel/) 기준으로 D1 은 Time Travel(시점 복원)이 항상 켜져 있으며,
[무료 플랜 보존 기간은 7일](https://developers.cloudflare.com/d1/platform/limits/)(유료 30일)입니다.

1. **저장 엔진 확인:** `npx wrangler d1 info law-rader-subscription` → `version` 이 `production` 이어야 Time Travel 대상입니다.
2. **사용 가능 여부 확인(읽기 전용):** `npx wrangler d1 time-travel info law-rader-subscription` → 현재 bookmark 가 출력되면 사용 가능합니다.
3. **복원:** `npx wrangler d1 time-travel restore law-rader-subscription --timestamp=<UNIX 초 또는 RFC3339>`
   - **운영자 승인 후에만** 실행합니다. 복원은 DB 를 **제자리에서 덮어쓰고** 진행 중인 쿼리를 취소합니다 — 그 순간의 동기화는
     실패해 해당 실행의 메일이 보류되고, 복원 시점 이후에 저장된 설정·세션은 사라집니다.
   - 복원 결과로 출력되는 이전 bookmark 로 되돌릴 수 있습니다. 10분에 10회까지 가능합니다.
   - 복원 직후 11단계 점검 스크립트로 `구독 DB 식별자` 가 Variable 과 같은지 확인합니다.
4. **DB 내부 데이터 오류 vs DB 삭제·교체:** Time Travel 은 **같은 DB 안에서** 과거 시점으로 되돌립니다(행 삭제·잘못된 수정 복구용).
   DB 자체를 삭제하거나 다른 DB 로 바꾼 경우 그 이전 데이터를 Time Travel 로 되살릴 수 있는지는 공식 문서로 확인하지 못했습니다.
   이에 대비해 월 1회 정도 `npx wrangler d1 export law-rader-subscription --remote --output=<경로>` 로 SQL 백업을 받아 둡니다
   (export 중에는 다른 요청이 막히므로 정기 실행 시간 밖에 — 예: 평일 KST 18:30 이후·주말 — 수행).
5. **백업 파일 취급:** 이메일 원문은 없지만 구독자 식별자(가명정보)·수신 설정·세션 해시가 들어 있어 **개인정보로 취급**합니다.
   저장소·채팅·메일에 올리지 않고, 암호화된 저장소에 보관하며, 보관 기간(예: 3개월)이 지나면 삭제합니다.

### 의도적 변경 절차
- **기관 제거:** 그 기관을 고른 구독자가 있으면 `catalog_mismatch` 로 발송이 멈춥니다(조용히 넓히거나 좁히지 않음). 제거 전에
  ① 영향 인원 확인 `npx wrangler d1 execute law-rader-subscription --remote --command "SELECT count(*) FROM preferences p, json_each(p.agencies) a WHERE a.value = '기관ID'"`
  ② 해당 구독자에게 공지 ③ 선택 목록에서 그 기관만 빼기(그 기관만 골랐던 사람은 '일시중지'가 됨 — 공지에 포함)
  `UPDATE preferences SET agencies = (SELECT json_group_array(value) FROM json_each(preferences.agencies) WHERE value <> '기관ID'), version = version + 1 WHERE EXISTS (SELECT 1 FROM json_each(preferences.agencies) WHERE value = '기관ID')`
  ④ 그다음 `config.yaml` 에서 기관을 제거합니다.
- **D1 을 의도적으로 새로 만들 때:** 기존 설정은 사라집니다(공지 필요, 또는 export 백업에서 복원). `SUBSCRIPTION_DB_INSTANCE` 를 비우면
  `SUBSCRIPTION_REQUIRED=true` 상태에서는 발송이 멈추므로, 새 D1 마이그레이션 → 점검 스크립트로 새 식별자 확인 → Variable 갱신을 바로 이어서 합니다.
- **설정 유실(`preference_missing`)을 복원하지 않고 해소해야 할 때:** 해당 구독자를 '전체 수신'으로 되돌리는 결정이므로 본인에게 알린 뒤
  `UPDATE subscribers SET pref_saved = 0 WHERE subscriber NOT IN (SELECT subscriber FROM preferences)` 를 실행합니다.

## HMAC 키(`SUBSCRIBER_HASH_KEY`) 정책
- 최초 설정 후 **고정**합니다. 실제 값은 Cloudflare Secret 과 GitHub Secret 에만 두며 git 추적 파일·문서·채팅에 기록하지 않습니다.
- 키를 바꾸면 모든 구독자의 식별자가 바뀝니다. 한쪽만 바뀌면 `hash_key_mismatch`, 두 곳이 함께 바뀌면 `key_changed`(D1 에 남은 최초 키 확인값과
  비교)로 **발송이 멈춥니다** — 저장된 선택이 '모든 기관 수신'으로 조용히 초기화되지는 않습니다. 조용한 키 교체는 금지합니다.
- 유출이 의심되면 즉시 교체하지 말고, 설정을 보존하는 별도 마이그레이션(이전 키·새 키 식별자 대응표 생성 → D1 일괄 변환)을 먼저 준비합니다.
  현재 버전에는 자동 키 회전 기능이 없습니다.

## 보안 동작 요약
- D1 에는 이메일 원문·인증번호·세션 토큰이 없습니다(식별자 HMAC, 인증번호 키 해시, 토큰 SHA-256).
- 인증번호: 6자리·10분·5회·일회용·재발급 시 이전 번호 무효. 요청 제한: 이메일 15분 3회·하루 5회, IP 시간당 10회, 전체 하루 30통.
- 등록되지 않은 주소도 같은 안내를 받습니다(등록 여부 비노출). 세션: 30분, `__Host-` 쿠키(HttpOnly·Secure·SameSite=Strict), Origin 검사.
- Apps Script 는 서명(HMAC)·시각(±5분)·nonce(영속 저장) 검증 후에만 메일을 보냅니다.
