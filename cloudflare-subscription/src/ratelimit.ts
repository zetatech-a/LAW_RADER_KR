// 요청 횟수 제한.
//
// 1단계(D1 이전): isolate 메모리의 IP 별 슬라이딩 창. D1 행 읽기·쓰기를 쓰기 전에 폭주를 거른다.
//   isolate 마다 따로 세므로 전역 보장은 아니다(최선 노력). Cloudflare Rate Limiting 바인딩은
//   무료 플랜 제공 여부를 공식 문서로 확인하지 못해 쓰지 않는다.
// 2단계(권위): D1 고정 창 카운터. bucket 의 식별 부분은 SERVER_SECRET 으로 만든 HMAC 이라
//   이메일·IP 원문이 남지 않는다. 만료 행은 매일 cron 으로 지운다.

import { hmacHex } from "./crypto";
import type { Env } from "./env";

const MEMORY_WINDOW_MS = 60_000;
const MEMORY_LIMIT = 30;
const MEMORY_MAX_KEYS = 10_000;
const memory = new Map<string, number[]>();

/** isolate 메모리 제한. 허용이면 true. */
export function allowInMemory(ip: string, now = Date.now()): boolean {
  if (memory.size > MEMORY_MAX_KEYS) memory.clear();
  const recent = (memory.get(ip) ?? []).filter((t) => now - t < MEMORY_WINDOW_MS);
  if (recent.length >= MEMORY_LIMIT) {
    memory.set(ip, recent);
    return false;
  }
  recent.push(now);
  memory.set(ip, recent);
  return true;
}

/** 테스트용: isolate 메모리 카운터 초기화. */
export function resetMemoryLimiter(): void {
  memory.clear();
}

export interface Window {
  /** 버킷 종류(로그·키 구분용, 개인정보 아님) */
  kind: string;
  /** 식별자 원문(이메일·IP 등). 저장 전에 HMAC 으로 바뀐다. 전역 버킷은 "" */
  subject: string;
  seconds: number;
}

async function bucketKey(env: Env, w: Window): Promise<string> {
  if (!w.subject) return w.kind;
  return `${w.kind}:${(await hmacHex(env.SERVER_SECRET, `rl:${w.kind}:${w.subject}`)).slice(0, 32)}`;
}

function windowStart(now: number, seconds: number): number {
  return now - (now % seconds);
}

const SQL_BUMP = `
INSERT INTO rate_limits (bucket, window_start, count, expires_at) VALUES (?1, ?2, 1, ?3)
ON CONFLICT (bucket, window_start) DO UPDATE SET count = count + 1
RETURNING count`;

const SQL_PEEK = `SELECT count FROM rate_limits WHERE bucket = ?1 AND window_start = ?2`;

/** 여러 창을 한 번의 batch 로 1씩 올리고, 올린 뒤의 값을 돌려준다. */
export async function bump(env: Env, windows: Window[], now: number): Promise<number[]> {
  const stmts = await Promise.all(
    windows.map(async (w) => {
      const start = windowStart(now, w.seconds);
      return env.DB.prepare(SQL_BUMP).bind(await bucketKey(env, w), start, start + w.seconds);
    }),
  );
  const results = await env.DB.batch<{ count: number }>(stmts);
  return results.map((r) => r.results?.[0]?.count ?? Number.POSITIVE_INFINITY);
}

/**
 * 여러 창의 현재 값을 읽기만 한다(batch 1회). 이미 한도에 걸린 요청을 D1 에 '쓰기 전에' 거르는
 * 데 쓴다 — 거절될 요청까지 카운터를 올리면 공격 한 IP 로도 D1 무료 쓰기 한도(하루 10만 행)를
 * 소진시켜, fail-closed 인 정기 메일까지 멈출 수 있다.
 */
export async function peekMany(env: Env, windows: Window[], now: number): Promise<number[]> {
  const stmts = await Promise.all(
    windows.map(async (w) => env.DB.prepare(SQL_PEEK).bind(await bucketKey(env, w), windowStart(now, w.seconds))),
  );
  const results = await env.DB.batch<{ count: number }>(stmts);
  return results.map((r) => r.results?.[0]?.count ?? 0);
}
