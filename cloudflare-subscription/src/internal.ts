// POST /internal/sync-and-resolve — Python(src/subscription.py) ↔ Worker API 계약 v1.
//
// 1) Bearer 토큰 → 2) 요청 전체 검증 → 3) 단일 D1 batch(트랜잭션)로 구독자·카탈로그 차분
// 동기화 + 설정 조회 → 4) 요청한 구독자마다 정확히 1개 응답.
// 실패는 절대 200 으로 응답하지 않는다(Python 은 200 이외를 모두 발송 중단으로 처리한다).
// 로그에는 건수만 남기고 식별자·토큰은 남기지 않는다.

import { hmacHex, sha256Hex, timingSafeEqual } from "./crypto";
import { strongSecret, type Env } from "./env";
import { HttpError, json, nowSec, readJson } from "./http";
import { CorruptPreference, effectivePreference } from "./preferences-model";

export const API_VERSION = 1;
const MAX_BODY_BYTES = 512 * 1024;
const MAX_SUBSCRIBERS = 5000;
const MAX_AGENCIES = 50;
const SUBSCRIBER_RE = /^[0-9a-f]{64}$/;
export const AGENCY_ID_RE = /^[a-z][a-z0-9_]{0,31}$/;
/** Python src/subscription.py KEY_CHECK_MESSAGE 와 같은 고정 문구. */
export const KEY_CHECK_MESSAGE = "lrk-subscriber-key-check-v1";

interface Agency {
  id: string;
  name: string;
}

interface SyncRequest {
  keyCheck: string;
  subscribers: string[];
  agencies: Agency[];
}

async function assertBearer(request: Request, env: Env): Promise<void> {
  if (!strongSecret(env.INTERNAL_API_TOKEN)) throw new HttpError(500, "server_misconfigured");
  const header = request.headers.get("Authorization") ?? "";
  const match = /^Bearer (\S+)$/.exec(header);
  // 길이·내용을 숨기기 위해 양쪽을 해시한 뒤 상수 시간으로 비교한다.
  const given = await sha256Hex(match ? match[1] : "");
  const expected = await sha256Hex(env.INTERNAL_API_TOKEN);
  if (!match || !timingSafeEqual(given, expected)) throw new HttpError(401, "unauthorized");
}

export function validateSyncRequest(body: unknown): SyncRequest {
  const bad = (code: string): never => {
    throw new HttpError(400, code);
  };
  if (typeof body !== "object" || body === null || Array.isArray(body)) bad("bad_request");
  const obj = body as Record<string, unknown>;
  const version = obj.api_version;
  if (typeof version !== "number" || !Number.isInteger(version) || version !== API_VERSION) {
    bad("unsupported_api_version");
  }

  const keyCheck = obj.key_check;
  if (typeof keyCheck !== "string" || !SUBSCRIBER_RE.test(keyCheck)) bad("bad_key_check");

  const subscribers = obj.subscribers;
  if (!Array.isArray(subscribers) || subscribers.length === 0) bad("empty_subscribers");
  const subs = subscribers as unknown[];
  if (subs.length > MAX_SUBSCRIBERS) bad("too_many_subscribers");
  const seenSubs = new Set<string>();
  for (const s of subs) {
    if (typeof s !== "string" || !SUBSCRIBER_RE.test(s)) bad("bad_subscriber");
    if (seenSubs.has(s as string)) bad("duplicate_subscriber");
    seenSubs.add(s as string);
  }

  const agencies = obj.agencies;
  if (!Array.isArray(agencies) || agencies.length === 0) bad("empty_agencies");
  const ags = agencies as unknown[];
  if (ags.length > MAX_AGENCIES) bad("too_many_agencies");
  const seenIds = new Set<string>();
  const parsed: Agency[] = [];
  for (const a of ags) {
    if (typeof a !== "object" || a === null || Array.isArray(a)) bad("bad_agency");
    const { id, name } = a as Record<string, unknown>;
    if (typeof id !== "string" || !AGENCY_ID_RE.test(id)) bad("bad_agency");
    if (typeof name !== "string") bad("bad_agency");
    const trimmed = (name as string).trim();
    if (trimmed.length === 0 || trimmed.length > 100) bad("bad_agency");
    if (seenIds.has(id as string)) bad("duplicate_agency");
    seenIds.add(id as string);
    parsed.push({ id: id as string, name: trimmed });
  }
  return { keyCheck: keyCheck as string, subscribers: subs as string[], agencies: parsed };
}

// 고정 개수의 집합 단위 SQL. 구독자 수와 무관하게 batch 1회(= D1 호출 1회)이며, 목록은 JSON
// 파라미터 1개로 넘겨 D1 의 '쿼리당 바인딩 100개' 제한에 걸리지 않는다. 각 문장은 실제로
// 바뀌는 행만 쓴다(WHERE 조건) — 변화 없는 정기 동기화는 쓰기 0행이다.
const SQL_UPSERT_SUBSCRIBERS = `
INSERT INTO subscribers (subscriber, active, created_at, updated_at)
SELECT value, 1, ?1, ?1 FROM json_each(?2) WHERE 1
ON CONFLICT (subscriber) DO UPDATE SET active = 1, updated_at = excluded.updated_at
WHERE subscribers.active = 0`;

const SQL_DEACTIVATE_SUBSCRIBERS = `
UPDATE subscribers SET active = 0, updated_at = ?1
WHERE active = 1 AND subscriber NOT IN (SELECT value FROM json_each(?2))`;

// 비활성 구독자의 로그인 세션·인증번호는 즉시 없앤다(MAIL_TO 에서 빠진 사람의 접근 차단).
const SQL_PURGE_INACTIVE_SESSIONS = `
DELETE FROM sessions WHERE subscriber IN (SELECT subscriber FROM subscribers WHERE active = 0)`;
const SQL_PURGE_INACTIVE_OTPS = `
DELETE FROM otp_challenges WHERE subscriber IN (SELECT subscriber FROM subscribers WHERE active = 0)`;

const SQL_UPSERT_AGENCIES = `
INSERT INTO agencies (id, name, sort, active, updated_at)
SELECT json_extract(value, '$.id'), json_extract(value, '$.name'), CAST(key AS INTEGER), 1, ?1
FROM json_each(?2) WHERE 1
ON CONFLICT (id) DO UPDATE SET name = excluded.name, sort = excluded.sort, active = 1,
  updated_at = excluded.updated_at
WHERE agencies.name IS NOT excluded.name OR agencies.sort IS NOT excluded.sort
   OR agencies.active = 0`;

const SQL_DEACTIVATE_AGENCIES = `
UPDATE agencies SET active = 0, updated_at = ?1
WHERE active = 1 AND id NOT IN (SELECT json_extract(value, '$.id') FROM json_each(?2))`;

const SQL_RESOLVE = `
SELECT j.value AS subscriber, p.mode AS mode, p.agencies AS agencies
FROM json_each(?1) AS j LEFT JOIN preferences AS p ON p.subscriber = j.value
ORDER BY j.key`;

interface ResolvedRow {
  subscriber: string;
  mode: string | null;
  agencies: string | null;
}

export async function handleSyncAndResolve(request: Request, env: Env): Promise<Response> {
  await assertBearer(request, env);
  const type = request.headers.get("Content-Type") ?? "";
  if (!/^application\/json(\s*;|$)/i.test(type)) throw new HttpError(415, "unsupported_media_type");
  const req = validateSyncRequest(await readJson(request, MAX_BODY_BYTES));
  // 두 곳(GitHub·Worker)에 등록한 SUBSCRIBER_HASH_KEY 가 다르면 모든 식별자가 새 구독자로 보여
  // 기존 구독자는 비활성화되고 전원이 mode=all 로 응답된다(사용자 선택이 조용히 무시됨).
  // DB 를 건드리기 전에 키 일치를 확인하고, 다르면 성공으로 응답하지 않는다.
  if (!strongSecret(env.SUBSCRIBER_HASH_KEY)) throw new HttpError(500, "server_misconfigured");
  const expectedCheck = await hmacHex(env.SUBSCRIBER_HASH_KEY, KEY_CHECK_MESSAGE);
  if (!timingSafeEqual(req.keyCheck, expectedCheck)) {
    console.error("sync: subscriber hash key mismatch");
    throw new HttpError(409, "hash_key_mismatch");
  }

  const now = nowSec();
  const subsJson = JSON.stringify(req.subscribers);
  const agenciesJson = JSON.stringify(req.agencies);
  const db = env.DB;
  let results: D1Result[];
  try {
    results = await db.batch([
      db.prepare(SQL_UPSERT_SUBSCRIBERS).bind(now, subsJson),
      db.prepare(SQL_DEACTIVATE_SUBSCRIBERS).bind(now, subsJson),
      db.prepare(SQL_PURGE_INACTIVE_SESSIONS),
      db.prepare(SQL_PURGE_INACTIVE_OTPS),
      db.prepare(SQL_UPSERT_AGENCIES).bind(now, agenciesJson),
      db.prepare(SQL_DEACTIVATE_AGENCIES).bind(now, agenciesJson),
      db.prepare(SQL_RESOLVE).bind(subsJson),
    ]);
  } catch (e) {
    // batch 는 트랜잭션이라 중간 실패 시 전체가 롤백된다 — 부분 동기화는 남지 않는다.
    console.error("sync: storage error", e instanceof Error ? e.name : "unknown");
    throw new HttpError(503, "storage_error");
  }

  const rows = (results[6].results ?? []) as unknown as ResolvedRow[];
  const catalogOrder = req.agencies.map((a) => a.id);
  if (rows.length !== req.subscribers.length) throw new HttpError(500, "resolve_mismatch");
  let subscribers;
  try {
    subscribers = rows.map((row, i) => {
      if (row.subscriber !== req.subscribers[i]) throw new CorruptPreference("order");
      const eff = effectivePreference(row.mode, row.agencies, catalogOrder);
      return { subscriber: row.subscriber, mode: eff.mode, agencies: eff.agencies };
    });
  } catch (e) {
    if (e instanceof CorruptPreference) {
      console.error("sync: corrupt preference row");
      throw new HttpError(500, "corrupt_preference");
    }
    throw e;
  }

  console.log("sync ok", {
    requested: req.subscribers.length,
    activated: results[0].meta.changes,
    deactivated: results[1].meta.changes,
    agencies_changed: results[4].meta.changes + results[5].meta.changes,
  });
  return json({ api_version: API_VERSION, subscribers });
}
