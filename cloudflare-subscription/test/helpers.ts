// 테스트 공통 도우미. Worker 모듈을 직접 호출하고(ctx.waitUntil 까지 기다림),
// Apps Script 호출은 globalThis.fetch 대역으로 가로채 서명·본문을 검사한다.

import { createExecutionContext, waitOnExecutionContext } from "cloudflare:test";
import { env as rawEnv } from "cloudflare:workers";
import { vi } from "vitest";
import { hmacHex, subscriberId } from "../src/crypto";
import type { Env } from "../src/env";
import worker from "../src/index";
import { KEY_CHECK_MESSAGE } from "../src/internal";
import { canonicalOtpMessage } from "../src/mailer";
import { resetMemoryLimiter } from "../src/ratelimit";

export const env = rawEnv as unknown as Env;
export const ORIGIN = env.PUBLIC_ORIGIN;
export const CATALOG = [
  { id: "fsc", name: "금융위원회" },
  { id: "fss", name: "금융감독원" },
  { id: "better", name: "금융규제·법령해석포털" },
  { id: "pipc", name: "개인정보보호위원회" },
  { id: "assembly", name: "의안정보시스템" },
];

export interface CallOptions {
  body?: unknown;
  rawBody?: string;
  headers?: Record<string, string>;
  cookie?: string;
  ip?: string;
  origin?: string | null;
  contentType?: string | null;
  env?: Env;
}

export async function call(method: string, path: string, opts: CallOptions = {}): Promise<Response> {
  const headers: Record<string, string> = { "CF-Connecting-IP": opts.ip ?? "203.0.113.10", ...opts.headers };
  const origin = opts.origin === undefined ? ORIGIN : opts.origin;
  if (origin !== null && method !== "GET") headers.Origin = origin;
  let body: string | undefined;
  if (opts.rawBody !== undefined || opts.body !== undefined) {
    body = opts.rawBody ?? JSON.stringify(opts.body);
    const ct = opts.contentType === undefined ? "application/json" : opts.contentType;
    if (ct !== null) headers["Content-Type"] = ct;
  }
  if (opts.cookie) headers.Cookie = opts.cookie;
  const ctx = createExecutionContext();
  const res = await worker.fetch(
    new Request(`https://lrk-test.example.workers.dev${path}`, { method, headers, body }),
    opts.env ?? env,
    ctx,
  );
  await waitOnExecutionContext(ctx);
  return res;
}

export const sid = (email: string) => subscriberId(email.trim().toLowerCase(), env.SUBSCRIBER_HASH_KEY);

export async function syncRequest(emails: string[], catalog = CATALOG, overrides: Record<string, unknown> = {}) {
  const subscribers = (await Promise.all(emails.map(sid))).sort();
  const key_check = await hmacHex(env.SUBSCRIBER_HASH_KEY, KEY_CHECK_MESSAGE);
  return { api_version: 1, key_check, subscribers, agencies: catalog, ...overrides };
}

export async function sync(emails: string[], catalog = CATALOG, opts: CallOptions = {}): Promise<Response> {
  return call("POST", "/internal/sync-and-resolve", {
    body: await syncRequest(emails, catalog),
    headers: { Authorization: `Bearer ${env.INTERNAL_API_TOKEN}` },
    origin: null,
    ...opts,
  });
}

export async function resetDb(): Promise<void> {
  resetMemoryLimiter();
  await env.DB.batch(
    ["sessions", "otp_challenges", "preferences", "rate_limits", "subscribers", "agencies"].map((t) =>
      env.DB.prepare(`DELETE FROM ${t}`),
    ),
  );
}

export interface SentMail {
  body: { v: number; ts: number; nonce: string; to: string; code: string; sig: string };
  url: string;
}

/** Apps Script 대역. reply 로 응답을 바꿀 수 있다. 실제 외부 호출은 일어나지 않는다. */
export function stubAppsScript(reply: () => Response = () => Response.json({ ok: true })) {
  const sent: SentMail[] = [];
  const spy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    sent.push({ url, body: JSON.parse(String(init?.body)) });
    return reply();
  });
  return { sent, spy };
}

export async function expectedSignature(m: SentMail["body"]): Promise<string> {
  return hmacHex(env.APPS_SCRIPT_SHARED_SECRET, canonicalOtpMessage(m.ts, m.nonce, m.to, m.code));
}

export function cookieFrom(res: Response): string {
  const set = res.headers.get("Set-Cookie") ?? "";
  return set.split(";")[0];
}

/** 동기화 → 인증번호 요청 → 확인까지 마치고 세션 쿠키를 돌려준다. */
export async function login(email: string, others: string[] = []): Promise<string> {
  await sync([email, ...others]);
  const mail = stubAppsScript();
  const req = await call("POST", "/api/otp/request", { body: { email } });
  if (req.status !== 202) throw new Error(`otp request ${req.status}`);
  const code = mail.sent.at(-1)!.body.code;
  mail.spy.mockRestore();
  const res = await call("POST", "/api/otp/verify", { body: { email, code } });
  if (res.status !== 200) throw new Error(`verify ${res.status}`);
  return cookieFrom(res);
}

/** 시계를 '오늘(UTC) 0시 + secondsIntoDay' 로 고정한다 — 창 경계에 걸려 흔들리지 않게. */
export function freezeInDay(secondsIntoDay: number) {
  const now = Math.floor(Date.now() / 1000);
  const dayStart = now - (now % 86_400);
  return vi.spyOn(Date, "now").mockReturnValue((dayStart + secondsIntoDay) * 1000);
}

/** Date.now 를 고정·이동시킨다(초 단위 offset). */
export function travel(offsetSec: number) {
  const base = Date.now();
  return vi.spyOn(Date, "now").mockReturnValue(base + offsetSec * 1000);
}
