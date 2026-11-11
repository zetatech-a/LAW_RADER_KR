// GET/PUT /api/preferences — 인증된 본인의 설정만 읽고 쓴다.
//
// 대상 구독자는 항상 세션에서 꺼낸다. 요청 본문에는 구독자를 지정하는 필드가 없으며, 있더라도
// 읽지 않는다(본문 조작으로 타인의 설정을 바꿀 수 없다).
// 동시 저장(여러 탭)은 version 으로 감지한다: 읽었던 version 이 아니면 409 로 거절하고 덮어쓰지 않는다.

import { requireSession } from "./auth";
import type { Env } from "./env";
import { HttpError, assertSameOrigin, json, nowSec, readJson } from "./http";
import { CatalogMismatch, CorruptPreference, effectivePreference, type Effective } from "./preferences-model";

const MAX_BODY_BYTES = 4096;

interface CatalogEntry {
  id: string;
  name: string;
}

interface PreferenceView extends Effective {
  version: number;
  catalog: CatalogEntry[];
  session_expires_at: number;
}

async function loadView(env: Env, subscriber: string, sessionExpiresAt: number): Promise<PreferenceView> {
  const [catalogRes, prefRes] = await env.DB.batch([
    env.DB.prepare("SELECT id, name FROM agencies WHERE active = 1 ORDER BY sort, id"),
    env.DB.prepare(
      `SELECT p.mode AS mode, p.agencies AS agencies, p.version AS version, s.pref_saved AS pref_saved
       FROM subscribers AS s LEFT JOIN preferences AS p ON p.subscriber = s.subscriber
       WHERE s.subscriber = ?1`,
    ).bind(subscriber),
  ]);
  const catalog = (catalogRes.results ?? []) as unknown as CatalogEntry[];
  const row = (prefRes.results?.[0] ?? null) as
    | { mode: string | null; agencies: string | null; version: number | null; pref_saved: number }
    | null;
  const pref = row?.mode ? (row as { mode: string; agencies: string; version: number }) : null;
  // 저장한 적 있는 설정이 사라졌으면 '모든 기관 수신'으로 보여 주지 않는다(내부 API 와 같은 판정).
  if (!pref && row?.pref_saved === 1) throw new HttpError(409, "preference_missing");
  let eff: Effective;
  try {
    eff = effectivePreference(pref?.mode ?? null, pref?.agencies ?? null, catalog.map((c) => c.id));
  } catch (e) {
    if (e instanceof CorruptPreference) throw new HttpError(500, "corrupt_preference");
    if (e instanceof CatalogMismatch) throw new HttpError(409, "catalog_mismatch");
    throw e;
  }
  return { ...eff, version: pref?.version ?? 0, catalog, session_expires_at: sessionExpiresAt };
}

export async function handleGetPreferences(request: Request, env: Env): Promise<Response> {
  const session = await requireSession(request, env);
  return json(await loadView(env, session.subscriber, session.expiresAt));
}

interface PutBody {
  mode: "all" | "selected";
  agencies: string[];
  version: number;
}

function validatePut(body: unknown, activeIds: ReadonlySet<string>): PutBody {
  const bad = (code: string): never => {
    throw new HttpError(400, code);
  };
  if (typeof body !== "object" || body === null || Array.isArray(body)) bad("bad_request");
  const { mode, agencies, version } = body as Record<string, unknown>;
  if (mode !== "all" && mode !== "selected") bad("bad_mode");
  if (typeof version !== "number" || !Number.isInteger(version) || version < 0) bad("bad_version");
  if (!Array.isArray(agencies) || !agencies.every((a) => typeof a === "string")) bad("bad_agencies");
  const list = agencies as string[];
  if (new Set(list).size !== list.length) bad("bad_agencies");
  if (list.some((a) => !activeIds.has(a))) bad("unknown_agency");
  if (mode === "all" && list.length > 0) bad("bad_agencies");
  return { mode: mode as PutBody["mode"], agencies: list, version: version as number };
}

export async function handlePutPreferences(request: Request, env: Env): Promise<Response> {
  assertSameOrigin(request, env);
  const session = await requireSession(request, env);
  const body = await readJson(request, MAX_BODY_BYTES);
  const catalog = ((await env.DB.prepare("SELECT id FROM agencies WHERE active = 1 ORDER BY sort, id").all<{ id: string }>())
    .results ?? []).map((r) => r.id);
  const put = validatePut(body, new Set(catalog));
  const chosen = new Set(put.agencies);
  const ordered = catalog.filter((id) => chosen.has(id));
  const agenciesJson = JSON.stringify(ordered);
  const now = nowSec();

  const write =
    put.version === 0
      ? env.DB.prepare(
          `INSERT INTO preferences (subscriber, mode, agencies, version, updated_at)
           VALUES (?1, ?2, ?3, 1, ?4) ON CONFLICT (subscriber) DO NOTHING`,
        ).bind(session.subscriber, put.mode, agenciesJson, now)
      : env.DB.prepare(
          `UPDATE preferences SET mode = ?2, agencies = ?3, version = version + 1, updated_at = ?4
           WHERE subscriber = ?1 AND version = ?5`,
        ).bind(session.subscriber, put.mode, agenciesJson, now, put.version);
  // 저장 이력(pref_saved)을 같은 트랜잭션에서 남긴다 — 이후 설정 행이 사라지면 '유실'로 감지된다.
  // 설정 행이 실제로 있을 때만 표시하므로 버전 충돌로 쓰지 못한 경우에도 거짓 이력이 생기지 않는다.
  const [result] = await env.DB.batch([
    write,
    env.DB.prepare(
      `UPDATE subscribers SET pref_saved = 1
       WHERE subscriber = ?1 AND pref_saved = 0
         AND EXISTS (SELECT 1 FROM preferences WHERE subscriber = ?1)`,
    ).bind(session.subscriber),
  ]);

  const view = await loadView(env, session.subscriber, session.expiresAt);
  if (result.meta.changes !== 1) {
    // 다른 탭이 먼저 저장했다 — 덮어쓰지 않고 최신 상태를 돌려준다.
    return json({ error: "version_conflict", current: view }, 409);
  }
  return json(view);
}
