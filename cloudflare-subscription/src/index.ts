// LAW RADER KR 구독 설정 Worker — 라우터와 정기 정리.
//
//   GET  /                          설정 페이지
//   GET  /app.js, /app.css          페이지 자산
//   POST /api/otp/request           인증번호 요청(등록 여부와 무관하게 202)
//   POST /api/otp/verify            인증번호 확인 → 세션 쿠키
//   GET  /api/preferences           본인 설정 + 기관 카탈로그
//   PUT  /api/preferences           본인 설정 저장(version 충돌 시 409)
//   POST /api/logout                세션 삭제
//   POST /internal/sync-and-resolve Python 전용(Bearer). API 계약 v1

import { handleLogout, handleOtpRequest, handleOtpVerify } from "./auth";
import type { Env } from "./env";
import { HttpError, clientIp, error, nowSec, withSecurityHeaders } from "./http";
import { handleSyncAndResolve } from "./internal";
import { APP_CSS, APP_JS, INDEX_HTML } from "./page";
import { handleGetPreferences, handlePutPreferences } from "./preferences";
import { allowInMemory } from "./ratelimit";

type Handler = (request: Request, env: Env, ctx: ExecutionContext) => Promise<Response> | Response;

const asset = (body: string, type: string): Handler => () =>
  new Response(body, { headers: { "Content-Type": `${type}; charset=utf-8` } });

const ROUTES: Record<string, Partial<Record<string, Handler>>> = {
  "/": { GET: asset(INDEX_HTML, "text/html") },
  "/app.js": { GET: asset(APP_JS, "text/javascript") },
  "/app.css": { GET: asset(APP_CSS, "text/css") },
  "/api/otp/request": { POST: handleOtpRequest },
  "/api/otp/verify": { POST: handleOtpVerify },
  "/api/preferences": { GET: handleGetPreferences, PUT: handlePutPreferences },
  "/api/logout": { POST: handleLogout },
  "/internal/sync-and-resolve": { POST: handleSyncAndResolve },
};

async function route(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
  const { pathname } = new URL(request.url);
  const methods = ROUTES[pathname];
  if (!methods) return error(404, "not_found");
  const handler = methods[request.method];
  if (!handler) return error(405, "method_not_allowed", { Allow: Object.keys(methods).join(", ") });
  // 공개 API 는 D1 을 건드리기 전에 isolate 메모리 제한부터 건다(무료 한도 보호).
  if (pathname.startsWith("/api/") && !allowInMemory(clientIp(request))) {
    return error(429, "rate_limited");
  }
  try {
    return await handler(request, env, ctx);
  } catch (e) {
    if (e instanceof HttpError) return error(e.status, e.code);
    // 예상하지 못한 오류는 성공으로 가장하지 않는다. 내용(입력값 포함 가능)은 싣지 않는다.
    console.error("unhandled", e instanceof Error ? e.name : "unknown");
    return error(500, "internal_error");
  }
}

export async function cleanup(env: Env, now = nowSec()): Promise<void> {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM otp_challenges WHERE expires_at <= ?1").bind(now),
    env.DB.prepare("DELETE FROM sessions WHERE expires_at <= ?1").bind(now),
    env.DB.prepare("DELETE FROM rate_limits WHERE expires_at <= ?1").bind(now),
  ]);
}

export default {
  async fetch(request, env, ctx): Promise<Response> {
    return withSecurityHeaders(await route(request, env, ctx));
  },
  async scheduled(_controller, env, ctx): Promise<void> {
    ctx.waitUntil(cleanup(env));
  },
} satisfies ExportedHandler<Env>;
