-- LAW RADER KR 구독 설정 D1 스키마 (v1)
--
-- 원칙
--   * 이메일 주소 원문을 저장하지 않는다. 구독자는 HMAC-SHA256 식별자(64자 hex)로만 식별한다.
--   * 인증번호(OTP)·세션 토큰은 평문으로 저장하지 않는다(키 있는 해시 / SHA-256).
--   * 시각은 모두 UNIX 초(INTEGER).
--   * 무료 티어(행 읽기·쓰기 일일 한도)를 고려해 조회 경로마다 인덱스를 둔다.

-- 구독자: Python(MAIL_TO)이 동기화한다. MAIL_TO 에서 빠지면 active=0 (설정은 보존).
CREATE TABLE subscribers (
  subscriber  TEXT    PRIMARY KEY CHECK (length(subscriber) = 64),
  active      INTEGER NOT NULL CHECK (active IN (0, 1)),
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
);

-- 기관 카탈로그: Python(config.yaml)이 동기화한다. 사라진 기관은 active=0.
CREATE TABLE agencies (
  id          TEXT    PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 32),
  name        TEXT    NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
  sort        INTEGER NOT NULL,
  active      INTEGER NOT NULL CHECK (active IN (0, 1)),
  updated_at  INTEGER NOT NULL
);

-- 수신 설정: 행이 없으면 mode=all(기존 구독자의 초기값). agencies 는 기관 ID 의 JSON 배열.
-- version 은 여러 탭에서 동시에 저장할 때의 충돌 감지(낙관적 잠금)에 쓴다.
CREATE TABLE preferences (
  subscriber  TEXT    PRIMARY KEY REFERENCES subscribers (subscriber),
  mode        TEXT    NOT NULL CHECK (mode IN ('all', 'selected')),
  agencies    TEXT    NOT NULL CHECK (json_valid(agencies) AND json_type(agencies) = 'array'),
  version     INTEGER NOT NULL CHECK (version >= 1),
  updated_at  INTEGER NOT NULL,
  CHECK (mode = 'selected' OR agencies = '[]')
);

-- 인증번호: 구독자당 1개만 존재한다(재발급 = 행 교체 = 이전 번호 무효화).
-- code_hash = HMAC(SERVER_SECRET, "otp:{subscriber}:{salt}:{code}")
CREATE TABLE otp_challenges (
  subscriber  TEXT    PRIMARY KEY REFERENCES subscribers (subscriber),
  code_hash   TEXT    NOT NULL CHECK (length(code_hash) = 64),
  salt        TEXT    NOT NULL,
  attempts    INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  expires_at  INTEGER NOT NULL,
  created_at  INTEGER NOT NULL
);
CREATE INDEX otp_challenges_expires ON otp_challenges (expires_at);

-- 로그인 세션: token_hash = SHA-256(세션 토큰). 토큰 원문은 쿠키에만 있다.
CREATE TABLE sessions (
  token_hash  TEXT    PRIMARY KEY CHECK (length(token_hash) = 64),
  subscriber  TEXT    NOT NULL REFERENCES subscribers (subscriber),
  expires_at  INTEGER NOT NULL,
  created_at  INTEGER NOT NULL
);
CREATE INDEX sessions_subscriber ON sessions (subscriber);
CREATE INDEX sessions_expires ON sessions (expires_at);

-- 요청 횟수 제한(고정 창 카운터). bucket 의 식별 부분은 키 있는 해시라 원문(이메일·IP)이 없다.
CREATE TABLE rate_limits (
  bucket        TEXT    NOT NULL,
  window_start  INTEGER NOT NULL,
  count         INTEGER NOT NULL,
  expires_at    INTEGER NOT NULL,
  PRIMARY KEY (bucket, window_start)
);
CREATE INDEX rate_limits_expires ON rate_limits (expires_at);
