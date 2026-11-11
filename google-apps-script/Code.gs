/**
 * LAW RADER KR — 인증번호(OTP) 메일 발송 전용 Google Apps Script 웹앱.
 *
 * 역할은 하나뿐이다: Cloudflare Worker 가 서명한 요청을 받아 그 주소로 인증번호 메일을 보낸다.
 * 인증번호 생성·저장·검증과 구독자 확인은 Worker(D1)가 한다. 정기 모니터링 메일(Python SMTP)과 무관하다.
 *
 * 보안 순서(서명 검증 전에는 MailApp 을 절대 부르지 않는다):
 *   1) 본문 형식 검사(크기·필드)          — doPost(e) 의 공식 필드 e.postData.contents 만 사용
 *   2) HMAC-SHA256 서명 검증(상수 시간)    — 요청 헤더는 읽지 않는다(이벤트 객체에 헤더 필드 없음)
 *   3) 타임스탬프 ±300초
 *   4) LockService 로 직렬화한 뒤 PropertiesService(영속)에서 nonce 재사용 검사·일일 상한 검사,
 *      발송 '전에' nonce·카운터를 기록 → 같은 요청이 다시·동시에 와도 한 번만 발송
 *   5) MailApp 남은 할당량 확인 → MailApp.sendEmail
 *
 * CacheService 는 쓰지 않는다(공식 문서상 만료 전에 항목이 사라질 수 있어 재전송 방지 저장소로 부적합).
 * 공유 비밀은 코드에 넣지 않고 스크립트 속성 OTP_SHARED_SECRET 에만 둔다.
 * 로그에는 수신 주소·인증번호·서명을 남기지 않는다(거절 사유 코드만).
 */

var LRK = {
  VERSION: 1,
  PREFIX: 'LRK-OTP-v1',
  SECRET_PROP: 'OTP_SHARED_SECRET',
  MAX_BODY_CHARS: 2048,
  MAX_SKEW_SEC: 300,
  NONCE_TTL_SEC: 900,
  DAILY_CAP: 30,
  PER_RECIPIENT_DAILY: 5,
  QUOTA_RESERVE: 5,
  LOCK_WAIT_MS: 10000,
  SENDER_NAME: 'LAW RADER',
  SUBJECT: '[LAW RADER] 수신 기관 설정 인증번호'
};

function doPost(e) {
  return respond_(handleRequest_(e, Math.floor(Date.now() / 1000)));
}

function doGet() {
  return respond_({ ok: false, error: 'method_not_allowed' });
}

function handleRequest_(e, now) {
  var req = parseRequest_(e);
  if (!req) return reject_('bad_request');

  var props = PropertiesService.getScriptProperties();
  var secret = props.getProperty(LRK.SECRET_PROP);
  if (!secret || secret.length < 32) return reject_('misconfigured');

  if (!constantTimeEqual_(hmacHex_(secret, canonical_(req)), req.sig)) return reject_('bad_signature');
  if (Math.abs(now - req.ts) > LRK.MAX_SKEW_SEC) return reject_('expired');

  var lock = LockService.getScriptLock();
  if (!lock.tryLock(LRK.LOCK_WAIT_MS)) return reject_('busy');
  try {
    var day = utcDay_(now);
    var all = props.getProperties();
    pruneExpired_(props, all, now, day);

    var nonceKey = 'nonce:' + req.nonce;
    if (all[nonceKey]) return reject_('replay');

    var dailyKey = 'day:' + day;
    var recipientKey = 'rcpt:' + day + ':' + hmacHex_(secret, 'rcpt:' + req.to).slice(0, 32);
    var daily = Number(all[dailyKey] || 0);
    var perRecipient = Number(all[recipientKey] || 0);

    var update = {};
    update[nonceKey] = String(req.ts);
    if (daily >= LRK.DAILY_CAP) {
      props.setProperties(update);
      return reject_('daily_cap');
    }
    if (perRecipient >= LRK.PER_RECIPIENT_DAILY) {
      props.setProperties(update);
      return reject_('recipient_cap');
    }
    if (MailApp.getRemainingDailyQuota() <= LRK.QUOTA_RESERVE) {
      props.setProperties(update);
      return reject_('quota_exhausted');
    }
    // 발송 전에 기록한다. 발송이 실패해도 같은 nonce 로 재시도하지 않는다(Worker 가 새 요청을 만든다).
    update[dailyKey] = String(daily + 1);
    update[recipientKey] = String(perRecipient + 1);
    props.setProperties(update);
  } finally {
    lock.releaseLock();
  }

  try {
    MailApp.sendEmail({
      to: req.to,
      subject: LRK.SUBJECT,
      body: mailBody_(req.code),
      name: LRK.SENDER_NAME
    });
  } catch (err) {
    return reject_('send_failed');
  }
  return { ok: true };
}

/** 본문 검증. 형식이 하나라도 어긋나면 null(사유를 구분해 돌려주지 않는다). */
function parseRequest_(e) {
  if (!e || !e.postData || typeof e.postData.contents !== 'string') return null;
  var raw = e.postData.contents;
  if (raw.length === 0 || raw.length > LRK.MAX_BODY_CHARS) return null;
  var obj;
  try {
    obj = JSON.parse(raw);
  } catch (err) {
    return null;
  }
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return null;
  if (obj.v !== LRK.VERSION) return null;
  if (typeof obj.ts !== 'number' || Math.floor(obj.ts) !== obj.ts) return null;
  if (typeof obj.nonce !== 'string' || !/^[0-9a-f]{32}$/.test(obj.nonce)) return null;
  if (typeof obj.to !== 'string' || obj.to.length > 254 || obj.to !== obj.to.toLowerCase()) return null;
  if (!/^[\x21-\x3f\x41-\x7e]+@[\x21-\x3f\x41-\x7e]+$/.test(obj.to)) return null;
  if (typeof obj.code !== 'string' || !/^[0-9]{6}$/.test(obj.code)) return null;
  if (typeof obj.sig !== 'string' || !/^[0-9a-f]{64}$/.test(obj.sig)) return null;
  return { v: obj.v, ts: obj.ts, nonce: obj.nonce, to: obj.to, code: obj.code, sig: obj.sig };
}

/** Worker(src/mailer.ts canonicalOtpMessage)와 같은 서명 대상 문자열. */
function canonical_(req) {
  return [LRK.PREFIX, String(req.ts), req.nonce, req.to, req.code].join('\n');
}

function hmacHex_(key, message) {
  var bytes = Utilities.computeHmacSha256Signature(message, key, Utilities.Charset.UTF_8);
  var out = '';
  for (var i = 0; i < bytes.length; i++) {
    var b = (bytes[i] + 256) % 256; // Apps Script 는 부호 있는 바이트(-128~127)를 돌려준다
    out += (b < 16 ? '0' : '') + b.toString(16);
  }
  return out;
}

function constantTimeEqual_(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  var diff = 0;
  for (var i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function utcDay_(now) {
  return Utilities.formatDate(new Date(now * 1000), 'UTC', 'yyyyMMdd');
}

/** 오래된 nonce·지난 날짜 카운터를 지운다. 스크립트 속성 총량(500KB) 안에 머물게 한다. */
function pruneExpired_(props, all, now, day) {
  for (var key in all) {
    if (!Object.prototype.hasOwnProperty.call(all, key)) continue;
    var stale = false;
    if (key.indexOf('nonce:') === 0) {
      stale = now - Number(all[key]) > LRK.NONCE_TTL_SEC;
    } else if (key.indexOf('day:') === 0) {
      stale = key !== 'day:' + day;
    } else if (key.indexOf('rcpt:') === 0) {
      stale = key.indexOf('rcpt:' + day + ':') !== 0;
    }
    if (stale) {
      props.deleteProperty(key);
      delete all[key];
    }
  }
}

function mailBody_(code) {
  return [
    'LAW RADER KR 수신 기관 설정 인증번호입니다.',
    '',
    '인증번호: ' + code,
    '',
    '이 번호는 10분 동안 유효하며 한 번만 사용할 수 있습니다.',
    '본인이 요청하지 않았다면 이 메일을 무시하세요. 번호를 다른 사람에게 알려 주지 마세요.'
  ].join('\n');
}

function reject_(code) {
  console.warn('otp request rejected: ' + code);
  return { ok: false, error: code };
}

function respond_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
