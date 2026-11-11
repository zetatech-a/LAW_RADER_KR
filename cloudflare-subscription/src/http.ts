// HTTP 공통: 보안 헤더, JSON 응답, 쿠키, 출처(Origin) 검증, 본문 읽기.

import type { Env } from "./env";

export const SESSION_COOKIE = "__Host-lrk_session";
export const SESSION_TTL_SEC = 30 * 60;

const SECURITY_HEADERS: Record<string, string> = {
  "Content-Security-Policy":
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; " +
    "connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'",
  "Cache-Control": "no-store",
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
  "X-Frame-Options": "DENY",
  "Strict-Transport-Security": "max-age=31536000",
  "Cross-Origin-Opener-Policy": "same-origin",
  "Cross-Origin-Resource-Policy": "same-origin",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
};

export function withSecurityHeaders(res: Response): Response {
  const out = new Response(res.body, res);
  for (const [k, v] of Object.entries(SECURITY_HEADERS)) out.headers.set(k, v);
  return out;
}

export function json(body: unknown, status = 200, extra: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...extra },
  });
}

export function error(status: number, code: string, extra: Record<string, string> = {}): Response {
  return json({ error: code }, status, extra);
}

export class HttpError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super(code);
  }
}

export function sessionCookie(token: string): string {
  return `${SESSION_COOKIE}=${token}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=${SESSION_TTL_SEC}`;
}

export function clearSessionCookie(): string {
  return `${SESSION_COOKIE}=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0`;
}

export function readCookie(request: Request, name: string): string | null {
  const header = request.headers.get("Cookie");
  if (!header) return null;
  for (const part of header.split(";")) {
    const idx = part.indexOf("=");
    if (idx === -1) continue;
    if (part.slice(0, idx).trim() === name) return part.slice(idx + 1).trim();
  }
  return null;
}

/**
 * 상태를 바꾸는 공개 API 의 CSRF 방어: 정확한 Origin 일치 + JSON 본문만 허용.
 * (쿠키는 SameSite=Strict 이고 CORS 헤더를 내보내지 않으므로 이중 방어다.)
 * PUBLIC_ORIGIN 이 설정되지 않았으면 검사를 건너뛰지 않고 서버 설정 오류로 거절한다.
 */
export function assertSameOrigin(request: Request, env: Env): void {
  const expected = env.PUBLIC_ORIGIN;
  if (!expected || !/^https:\/\/[^/]+$/.test(expected)) {
    throw new HttpError(500, "server_misconfigured");
  }
  if (request.headers.get("Origin") !== expected) throw new HttpError(403, "bad_origin");
  const type = request.headers.get("Content-Type") ?? "";
  if (!/^application\/json(\s*;|$)/i.test(type)) throw new HttpError(415, "unsupported_media_type");
}

/** 크기 상한을 지키며 JSON 본문을 읽는다. 실패 사유는 코드로만 돌려준다(입력을 되풀이하지 않음). */
export async function readJson(request: Request, maxBytes: number): Promise<unknown> {
  const declared = Number(request.headers.get("Content-Length") ?? "0");
  if (declared > maxBytes) throw new HttpError(413, "payload_too_large");
  const text = await request.text();
  if (new TextEncoder().encode(text).length > maxBytes) throw new HttpError(413, "payload_too_large");
  try {
    return JSON.parse(text);
  } catch {
    throw new HttpError(400, "bad_json");
  }
}

export function clientIp(request: Request): string {
  return request.headers.get("CF-Connecting-IP") ?? "unknown";
}

export function nowSec(): number {
  return Math.floor(Date.now() / 1000);
}
