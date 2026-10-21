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
curl -sL -X POST -H 'Content-Type: application/json' -d '{}' "$APPS_URL"
```
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
| Variable | `SUBSCRIPTION_REQUIRED` | **지금은 비워 둡니다**(14단계 마지막에 `true`) |

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
**확인:** `성공: 그룹 1개 / all: 1명`.
그다음 설정 페이지에서 시험 주소를 입력 → 메일(스팸함 포함)의 6자리 번호 입력 → 설정 화면이 열리면 성공입니다.
등록되지 않은 다른 주소를 넣으면 화면 안내는 같지만 메일은 오지 않아야 합니다.

## 12. 기관별 선택 저장 검증
1. 설정 화면에서 **선택한 기관만 수신 → 의안정보시스템** 만 체크 → **설정 저장** → "저장되었습니다" 확인.
2. 페이지를 새로 고쳐 같은 설정이 보이는지 확인.
3. 11단계 명령을 다시 실행 → `selected:assembly: 1명` 이 나오면 Python 이 같은 설정을 읽고 있는 것입니다.
4. **모든 기관 수신** 으로 되돌려 저장해 둡니다.

## 13. Python 구독 API 연동 테스트
- 11·12단계의 `scripts/check_subscription_api.py` 는 정기 실행과 **같은 코드**로 동기화·검증·그룹화를 수행합니다(메일·state 변경 없음).
- 실패 시 원인 코드: `HTTP 401`(토큰 불일치) · `HTTP 500`(Worker Secret 누락·짧음) · `응답에서 구독자 N명 누락`(계약 위반) · `시간 초과`.

## 14. 실제 운영 기능 활성화
1. 운영 `MAIL_TO` 의 주소가 모두 `이름 <주소>` 형식이 아닌 **주소만**으로 되어 있는지 확인합니다(형식이 다르면 활성화 후 발송이 중단됩니다).
2. `config.yaml` 의 `subscription.enabled: false` → `true` 로 바꾸는 변경을 평소 절차로 main 에 반영합니다.
3. 다음 정기 실행 로그에서 `구독 설정 조회 완료 — 수신자 N명 / 그룹 1개 (전체 수신 N명 …)` 을 확인합니다.
   처음에는 모든 기존 구독자가 `mode=all` 이므로 받는 메일은 전과 같습니다. 시험 주소가 MAIL_TO 에 없으면 이때 비활성화됩니다.
4. 정상 확인 후 GitHub Variable **`SUBSCRIPTION_REQUIRED` = `true`** 로 설정합니다. 이후 실수로 `enabled` 가 `false` 가 되면 실행이 멈춥니다(전체 발송 재개 방지).
5. 구독자에게 메일 하단 **'수신 기관 설정'** 링크로 설정할 수 있음을 안내합니다.

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
| 인증번호 메일이 안 옴 | 설정 변경만 불가(정기 메일 영향 없음) | Apps Script 실행 기록의 거절 코드(`bad_signature`·`quota_exhausted`·`daily_cap`) 확인 |
| `SUBSCRIPTION_REQUIRED=true 인데 ... false` | 실행 중단(메일·state 변경 없음) | `enabled: true` 로 되돌리기 |

장기 장애로 한 게시판의 신규가 50건(의안 75건)을 넘으면, 복구 후 최신 분만 발송되고 나머지는 기존 안전장치에 따라 확정됩니다.

### 롤백
- **기능을 끄는 것은 장애 대응이 아닙니다.** `enabled: false` 는 사용자 선택(일시중지 포함)을 무시하고 전원에게 전체 메일을 보냅니다.
  `SUBSCRIPTION_REQUIRED=true` 상태에서는 실행 자체가 멈춥니다.
- **모든 메일을 잠시 멈춰야 할 때:** Cloudflare 대시보드에서 `law-rader-kr-scheduler` 의 Cron 트리거를 끄거나 GitHub Actions 의 monitor 워크플로를 비활성화합니다.
- **구독 기능을 폐지할 때:** ① 구독자에게 사전 공지 ② 설정 페이지에서 일시중지·선택 수신 중인 사람을 확인해 필요하면 `MAIL_TO` 정리
  ③ `SUBSCRIPTION_REQUIRED` 를 비우기 ④ `enabled: false`. 코드를 이전 버전으로 되돌리면 보호장치(`SUBSCRIPTION_REQUIRED` 검사)도 함께 사라지므로, **코드 되돌리기는 ①~③을 마친 뒤에만** 합니다.

## HMAC 키(`SUBSCRIBER_HASH_KEY`) 정책
- 최초 설정 후 **고정**합니다. 실제 값은 Cloudflare Secret 과 GitHub Secret 에만 두며 git 추적 파일·문서·채팅에 기록하지 않습니다.
- 키를 바꾸면 모든 구독자의 식별자가 바뀌어 **저장된 기관 선택이 전부 '모든 기관 수신'으로 초기화**됩니다. 조용한 키 교체는 금지합니다.
- 유출이 의심되면 즉시 교체하지 말고, 설정을 보존하는 별도 마이그레이션(이전 키·새 키 식별자 대응표 생성 → D1 일괄 변환)을 먼저 준비합니다.
  현재 버전에는 자동 키 회전 기능이 없습니다.

## 보안 동작 요약
- D1 에는 이메일 원문·인증번호·세션 토큰이 없습니다(식별자 HMAC, 인증번호 키 해시, 토큰 SHA-256).
- 인증번호: 6자리·10분·5회·일회용·재발급 시 이전 번호 무효. 요청 제한: 이메일 15분 3회·하루 5회, IP 시간당 10회, 전체 하루 30통.
- 등록되지 않은 주소도 같은 안내를 받습니다(등록 여부 비노출). 세션: 30분, `__Host-` 쿠키(HttpOnly·Secure·SameSite=Strict), Origin 검사.
- Apps Script 는 서명(HMAC)·시각(±5분)·nonce(영속 저장) 검증 후에만 메일을 보냅니다.
