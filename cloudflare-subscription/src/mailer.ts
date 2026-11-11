// 인증번호 메일 발송 요청 — Google Apps Script 웹앱(google-apps-script/Code.gs)에 서명된 요청을 보낸다.
//
// Apps Script doPost(e) 는 요청 헤더를 읽는 필드가 공식 문서에 없으므로 인증 정보는 전부 본문에 싣는다.
//   sig = HMAC-SHA256(APPS_SCRIPT_SHARED_SECRET, "LRK-OTP-v1\n{ts}\n{nonce}\n{to}\n{code}")
// 웹앱 응답은 공식 문서상 script.googleusercontent.com 의 일회용 URL 로 리다이렉트되므로
// redirect: "follow" 로 따라간다. 응답의 HTTP 상태가 아니라 JSON 본문의 ok 로만 성공을 판정한다.
// (리다이렉트 후 본문 수신 동작은 실제 배포 전에 검증해야 한다 — DEPLOY.md 참고)

import { hmacHex, randomHex } from "./crypto";
import { strongSecret, type Env } from "./env";

export const OTP_SIGNING_PREFIX = "LRK-OTP-v1";
const APPS_SCRIPT_URL_RE = /^https:\/\/script\.google\.com\/macros\/s\/[A-Za-z0-9_-]+\/exec$/;
const TIMEOUT_MS = 15_000;

export function canonicalOtpMessage(ts: number, nonce: string, to: string, code: string): string {
  return [OTP_SIGNING_PREFIX, String(ts), nonce, to, code].join("\n");
}

export type SendResult = { ok: true } | { ok: false; reason: string };

export async function sendOtpMail(env: Env, to: string, code: string, nowSec: number): Promise<SendResult> {
  if (!strongSecret(env.APPS_SCRIPT_SHARED_SECRET) || !APPS_SCRIPT_URL_RE.test(env.APPS_SCRIPT_URL ?? "")) {
    return { ok: false, reason: "mailer_misconfigured" };
  }
  const nonce = randomHex(16);
  const sig = await hmacHex(env.APPS_SCRIPT_SHARED_SECRET, canonicalOtpMessage(nowSec, nonce, to, code));
  let res: Response;
  try {
    res = await fetch(env.APPS_SCRIPT_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ v: 1, ts: nowSec, nonce, to, code, sig }),
      redirect: "follow",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch (e) {
    return { ok: false, reason: e instanceof Error && e.name === "TimeoutError" ? "timeout" : "network" };
  }
  if (!res.ok) return { ok: false, reason: `http_${res.status}` };
  let body: unknown;
  try {
    body = await res.json();
  } catch {
    return { ok: false, reason: "bad_response" };
  }
  if (typeof body === "object" && body !== null && (body as { ok?: unknown }).ok === true) {
    return { ok: true };
  }
  const err = (body as { error?: unknown } | null)?.error;
  // Apps Script 가 돌려주는 오류 코드는 고정 목록이다. 그 밖의 값은 그대로 싣지 않는다.
  return { ok: false, reason: typeof err === "string" && /^[a-z_]{1,32}$/.test(err) ? err : "rejected" };
}
