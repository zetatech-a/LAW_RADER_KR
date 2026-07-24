# LAW_RADER_KR

한국 금융 규제·입법 관련 게시판을 주기적으로 확인해, **새로 올라온 글의 URL·내용·첨부파일을 이메일로 자동 발송**하는 모니터링 프로그램입니다.

## 모니터링 대상 (9곳)

| key | 사이트 | 게시판 |
|---|---|---|
| `fsc_press` | 금융위원회 | 보도자료 |
| `fsc_legislation` | 금융위원회 | 입법예고/규정변경예고 |
| `fss_press` | 금융감독원 | 보도자료 |
| `fss_admin_guidance` | 금융감독원 | 행정지도 예고 |
| `fss_rule_amendment` | 금융감독원 | 세칙 제·개정 예고 |
| `fss_sanction` | 금융감독원 | 검사결과 제재 |
| `fss_mgmt_notice` | 금융감독원 | 경영유의사항 등 공시 |
| `better_reply` | 금융규제·법령해석포털 | 법령해석·비조치의견서 회신사례 |
| `assembly_bill` | 의안정보시스템 | 계류의안 |

## 동작 방식

1. **월~금 09:00–18:00 (KST), 30분 간격**으로 GitHub Actions가 실행됩니다.
2. 각 게시판 목록을 수집해 **이전에 못 본 새 글**만 골라냅니다.
   - 어떤 소스를 **처음** 볼 때는 현재 목록을 "기준선"으로만 저장하고 메일을 보내지 않습니다(과거 글 폭탄 방지). 그 다음 실행부터 진짜 신규만 발송합니다.
3. 새 글의 **상세 본문과 첨부파일**을 내려받습니다.
4. 신규가 있으면 **다이제스트 메일 1통**으로 묶어 발송합니다(첨부 포함).
5. "이미 본 글" 목록(`state/seen.json`)을 저장소에 커밋해 다음 실행에 이어씁니다.

## 설정 방법 (GitHub Actions)

### 1) Gmail 앱 비밀번호 발급
1. Gmail 계정에 **2단계 인증** 활성화
2. https://myaccount.google.com/apppasswords 에서 앱 비밀번호(16자리) 발급

### 2) 저장소 Secrets 등록
`Settings → Secrets and variables → Actions → New repository secret` 에서:

| Secret 이름 | 값 |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | 보내는 Gmail 주소 |
| `SMTP_PASSWORD` | 발급받은 앱 비밀번호 16자리 |
| `MAIL_FROM` | 발신 표시 주소(보통 `SMTP_USER`와 동일) |
| `MAIL_TO` | **수신자**. 여러 명은 `,` 또는 `;` 로 구분 |
| `ASSEMBLY_API_KEY` | 계류의안 수집용 열린국회정보(open.assembly.go.kr) API 인증키. 미설정 시 계류의안만 건너뜀 |

### 3) 수신자 설정
수신자 주소는 **저장소에 커밋하지 않습니다.** `MAIL_TO` Secret(여러 명은 `a@x.com, b@y.com`)으로만 지정하세요. 로컬 실행 시에는 `.env` 의 `MAIL_TO` 를 사용합니다(`.env` 는 커밋 제외).
여러 명에게 보내도 **수신자끼리는 서로의 주소를 볼 수 없습니다**(BCC 방식). 메일함에는 발신자가 `LAW RADER` 로, 클릭하면 `LAW RADER <krome000234@gmail.com>` 로 표시됩니다.

### 4) 실행
- 자동: 위 스케줄에 따라 실행 (`.github/workflows/monitor.yml`)
- 수동: 저장소 **Actions 탭 → LAW RADAR KR monitor → Run workflow**

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

# SMTP 설정 확인용 테스트 메일 1통
python scripts/send_test_email.py

# 테스트
python -m pytest -q
```

## 프로젝트 구조

```
config.yaml                 # 수신자·발송옵션·모니터링 대상 목록
.github/workflows/monitor.yml
src/
  main.py                   # 오케스트레이션(수집→신규판별→enrich→메일→저장)
  config.py  fetcher.py     # 설정 로딩 / HTTP 세션(재시도·한글 인코딩)
  state.py   notifier.py    # 신규 판별 상태 / 이메일 렌더·발송
  models.py                 # Post, Attachment
  scrapers/
    base.py                 # 스크래퍼 베이스 + 디버그 덤프
    fsc.py  fss.py          # 금융위 / 금감원 게시판
    better_fsc.py assembly.py  # 회신사례 / 계류의안
state/seen.json             # 이미 본 글 ID (자동 커밋)
scripts/send_test_email.py
```

새 게시판을 추가하려면 `config.yaml` 의 `sources` 에 항목을 추가하고, 알맞은 `type`(파서)을 지정하면 됩니다.

## ⚠️ 라이브 검증 관련 (중요)

개발 환경에서 대상 사이트로의 외부 접속이 차단되어 **각 사이트의 실제 HTML 구조를 라이브로 검증하지 못했습니다.** 파서는 해당 정부 사이트들의 일반적인 구조(eGovFrame 게시판 등)를 기준으로 방어적으로 작성했으며, 다음 사항을 첫 라이브 실행 시 확인해야 합니다.

- **목록 파싱이 0건이면** 자동으로 `debug/<key>_list.txt` 에 원본 HTML을 덤프합니다. 이를 열어 실제 셀렉터를 확인한 뒤 해당 스크래퍼의 `_parse_list` 를 조정하세요.
- **`better_reply`(회신사례)·`assembly_bill`(계류의안)** 은 목록이 자바스크립트(AJAX)로 로드될 가능성이 높습니다. 이 경우:
  - 계류의안은 **국회 의안정보 Open API(open.assembly.go.kr)** 사용을 권장합니다(가장 안정적, API 키 필요).
  - 또는 브라우저 개발자도구로 실제 목록 POST 엔드포인트를 확인해 반영하거나, Playwright 렌더링으로 전환합니다.
- **접속 IP 차단:** 일부 한국 정부 사이트는 해외(GitHub Actions는 미국) IP를 차단할 수 있습니다. 그럴 경우 국내 IP의 **self-hosted runner** 또는 개인 서버 cron으로 실행을 옮기면 됩니다(코드는 환경 독립적이라 그대로 사용 가능).

### 라이브 검증 방법 (권장 첫 단계)

정식 가동 전, 실제 사이트에서 파서와 페이지 파라미터가 동작하는지 먼저 확인하세요. 메일·상태변경 없이 점검만 합니다.

- **GitHub Actions에서 (가장 쉬움):** Actions 탭 → **"Verify sources (live)"** → **Run workflow**. 실행이 끝나면 `verify-results` 아티팩트로 **검증 리포트(`verify_report.txt`)와 원본 HTML(`debug/`)** 을 내려받을 수 있습니다.
- **로컬에서:** 인터넷이 되는 환경에서
  ```bash
  python scripts/verify_sources.py                 # 전체
  python scripts/verify_sources.py --only fss_press # 특정 소스
  ```

검증 리포트는 소스별로 **① 목록 건수 ② 제목 샘플 ③ 페이지네이션 작동 여부 ④ 본문/첨부 추출 여부** 를 보여줍니다. 0건이거나 페이지네이션이 "무시된 듯"으로 나오는 소스는 `debug/<key>_list.txt` 원본을 열어 실제 셀렉터/파라미터명을 확인한 뒤 해당 스크래퍼를 조정하면 됩니다.
