// 인증번호(OTP) 요청·검증, 세션, 로그아웃.
//
// 이메일 등록 여부를 드러내지 않는다:
//   - 요청 응답은 등록 여부와 무관하게 같은 상태·본문(202). 요청 제한도 모두에게 똑같이 센다.
//   - 구독자 조회·번호 발급·메일 발송은 응답 뒤 ctx.waitUntil 에서 한다(응답 시간 차이 제거).
//   - 검증 실패는 사유와 무관하게 한 가지 코드(invalid_code).
// 인증번호·세션 토큰·이메일·식별자는 로그에 남기지 않는다.

import { hmacHex, normalizeEmail, randomHex, randomOtp, randomToken, sha256Hex, subscriberId, timingSafeEqual } from "./crypto";
import { strongSecret, type Env } from "./env";
import {
  HttpError,
  SESSION_COOKIE,
  SESSION_TTL_SEC,
  assertSameOrigin,
  clearSessionCookie,
  clientIp,
  json,
  nowSec,
  readCookie,
  readJson,
  sessionCookie,
} from "./http";
import { sendOtpMail } from "./mailer";
import { bump, peek, type Window } from "./ratelimit";

export const OTP_TTL_SEC = 10 * 60;
export const OTP_MAX_ATTEMPTS = 5;
export const LIMITS = {
  emailPer15m: 3,
  emailPerDay: 5,
  ipPerHour: 10,
  globalSendsPerDay: 30,
  verifyPerIpHour: 20,
} as const;
const MAX_BODY_BYTES = 2048;
const GLOBAL_SENDS: Window = { kind: "otp_sent_global", subject: "", seconds: 86_400 };

function assertSecrets(env: Env): void {
  if (!strongSecret(env.SERVER_SECRET) || !strongSecret(env.SUBSCRIBER_HASH_KEY)) {
    throw new HttpError(500, "server_misconfigured");
  }
}

export async function handleOtpRequest(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
  assertSameOrigin(request, env);
  assertSecrets(env);
  const body = (await readJson(request, MAX_BODY_BYTES)) as { email?: unknown } | null;
  const email = normalizeEmail(body?.email);
  if (!email) throw new HttpError(400, "invalid_email");

  const now = nowSec();
  const ip = clientIp(request);
  const [per15m, perDay, perIp] = await bump(
    env,
    [
      { kind: "otp_email_15m", subject: email, seconds: 900 },
      { kind: "otp_email_day", subject: email, seconds: 86_400 },
      { kind: "otp_ip_hour", subject: ip, seconds: 3_600 },
    ],
    now,
  );
  const sentToday = await peek(env, GLOBAL_SENDS, now);
  if (
    per15m > LIMITS.emailPer15m ||
    perDay > LIMITS.emailPerDay ||
    perIp > LIMITS.ipPerHour ||
    sentToday >= LIMITS.globalSendsPerDay
  ) {
    throw new HttpError(429, "rate_limited");
  }

  ctx.waitUntil(issueOtp(env, email, now));
  return json({ ok: true }, 202);
}

/** 응답 이후 실행. 활성 구독자일 때만 번호를 발급하고 메일을 요청한다. */
export async function issueOtp(env: Env, email: string, now: number): Promise<void> {
  try {
    const sid = await subscriberId(email, env.SUBSCRIBER_HASH_KEY);
    const row = await env.DB.prepare("SELECT active FROM subscribers WHERE subscriber = ?1")
      .bind(sid)
      .first<{ active: number }>();
    if (row?.active !== 1) return; // 미등록·비활성: 메일 없음(응답은 이미 동일하게 나갔다)

    // 전역 일일 상한은 실제 발송 직전에 원자적으로 센다(검사 시점과 경합해도 넘지 않도록).
    const [sent] = await bump(env, [GLOBAL_SENDS], now);
    if (sent > LIMITS.globalSendsPerDay) {
      console.warn("otp: global daily cap reached");
      return;
    }
    const code = randomOtp();
    const salt = randomHex(16);
    const codeHash = await hmacHex(env.SERVER_SECRET, `otp:${sid}:${salt}:${code}`);
    // 구독자당 1행 — 새 번호가 이전 번호를 대체(무효화)한다. 시도 횟수도 초기화된다.
    await env.DB.prepare(
      `INSERT INTO otp_challenges (subscriber, code_hash, salt, attempts, expires_at, created_at)
       VALUES (?1, ?2, ?3, 0, ?4, ?5)
       ON CONFLICT (subscriber) DO UPDATE SET code_hash = excluded.code_hash, salt = excluded.salt,
         attempts = 0, expires_at = excluded.expires_at, created_at = excluded.created_at`,
    )
      .bind(sid, codeHash, salt, now + OTP_TTL_SEC, now)
      .run();
    const result = await sendOtpMail(env, email, code, now);
    if (!result.ok) console.error("otp: mail request failed", { reason: result.reason });
  } catch (e) {
    console.error("otp: issue failed", e instanceof Error ? e.name : "unknown");
  }
}

export async function handleOtpVerify(request: Request, env: Env): Promise<Response> {
  assertSameOrigin(request, env);
  assertSecrets(env);
  const body = (await readJson(request, MAX_BODY_BYTES)) as { email?: unknown; code?: unknown } | null;
  const email = normalizeEmail(body?.email);
  const code = body?.code;
  if (!email || typeof code !== "string" || !/^\d{6}$/.test(code)) throw new HttpError(400, "invalid_input");

  const now = nowSec();
  const [perIp] = await bump(env, [{ kind: "verify_ip_hour", subject: clientIp(request), seconds: 3_600 }], now);
  if (perIp > LIMITS.verifyPerIpHour) throw new HttpError(429, "rate_limited");

  const sid = await subscriberId(email, env.SUBSCRIBER_HASH_KEY);
  // 비교 전에 시도 횟수를 먼저 원자적으로 올린다 — 병렬 요청으로 5회 제한을 넘을 수 없다.
  const challenge = await env.DB.prepare(
    `UPDATE otp_challenges SET attempts = attempts + 1
     WHERE subscriber = ?1 AND expires_at > ?2 AND attempts < ?3
     RETURNING code_hash, salt, attempts`,
  )
    .bind(sid, now, OTP_MAX_ATTEMPTS)
    .first<{ code_hash: string; salt: string; attempts: number }>();
  if (!challenge) throw new HttpError(400, "invalid_code");

  const expected = await hmacHex(env.SERVER_SECRET, `otp:${sid}:${challenge.salt}:${code}`);
  if (!timingSafeEqual(expected, challenge.code_hash)) {
    if (challenge.attempts >= OTP_MAX_ATTEMPTS) {
      await env.DB.prepare("DELETE FROM otp_challenges WHERE subscriber = ?1").bind(sid).run();
    }
    throw new HttpError(400, "invalid_code");
  }

  // 일회용: 같은 번호로 동시에 들어온 요청 중 하나만 이 DELETE 에 성공한다.
  const consumed = await env.DB.prepare(
    "DELETE FROM otp_challenges WHERE subscriber = ?1 AND code_hash = ?2",
  )
    .bind(sid, challenge.code_hash)
    .run();
  if (consumed.meta.changes !== 1) throw new HttpError(400, "invalid_code");

  const token = randomToken();
  const tokenHash = await sha256Hex(token);
  const [, created] = await env.DB.batch([
    // 구독자당 세션 1개(다른 기기·탭의 기존 세션은 로그아웃된다).
    env.DB.prepare("DELETE FROM sessions WHERE subscriber = ?1").bind(sid),
    // 활성 구독자일 때만 세션을 만든다(검증 도중 MAIL_TO 에서 빠진 경우 차단).
    env.DB.prepare(
      `INSERT INTO sessions (token_hash, subscriber, expires_at, created_at)
       SELECT ?1, subscriber, ?2, ?3 FROM subscribers WHERE subscriber = ?4 AND active = 1`,
    ).bind(tokenHash, now + SESSION_TTL_SEC, now, sid),
  ]);
  if (created.meta.changes !== 1) throw new HttpError(400, "invalid_code");

  return json({ ok: true, expires_at: now + SESSION_TTL_SEC }, 200, { "Set-Cookie": sessionCookie(token) });
}

/** 유효한 세션의 구독자 식별자. 없거나 만료·비활성이면 401. */
export async function requireSession(request: Request, env: Env): Promise<{ subscriber: string; tokenHash: string; expiresAt: number }> {
  const token = readCookie(request, SESSION_COOKIE);
  if (!token || !/^[A-Za-z0-9_-]{43}$/.test(token)) throw new HttpError(401, "unauthorized");
  const tokenHash = await sha256Hex(token);
  const row = await env.DB.prepare(
    `SELECT s.subscriber AS subscriber, s.expires_at AS expires_at
     FROM sessions AS s JOIN subscribers AS u ON u.subscriber = s.subscriber AND u.active = 1
     WHERE s.token_hash = ?1 AND s.expires_at > ?2`,
  )
    .bind(tokenHash, nowSec())
    .first<{ subscriber: string; expires_at: number }>();
  if (!row) throw new HttpError(401, "unauthorized");
  return { subscriber: row.subscriber, tokenHash, expiresAt: row.expires_at };
}

export async function handleLogout(request: Request, env: Env): Promise<Response> {
  assertSameOrigin(request, env);
  const token = readCookie(request, SESSION_COOKIE);
  if (token && /^[A-Za-z0-9_-]{43}$/.test(token)) {
    await env.DB.prepare("DELETE FROM sessions WHERE token_hash = ?1").bind(await sha256Hex(token)).run();
  }
  return json({ ok: true }, 200, { "Set-Cookie": clearSessionCookie() });
}
