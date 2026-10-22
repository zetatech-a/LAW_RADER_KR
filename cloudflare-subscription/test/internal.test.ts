// POST /internal/sync-and-resolve — Python API 계약 v1 호환성.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Env } from "../src/env";
import { hmacHex } from "../src/crypto";
import { KEY_CHECK_MESSAGE } from "../src/internal";
import { CATALOG, call, env, login, resetDb, sid, sync, syncRequest } from "./helpers";

beforeEach(resetDb);
afterEach(() => vi.restoreAllMocks());

const auth = { Authorization: `Bearer ${env.INTERNAL_API_TOKEN}` };
const post = (body: unknown, opts: Parameters<typeof call>[2] = {}) =>
  call("POST", "/internal/sync-and-resolve", { body, headers: auth, origin: null, ...opts });

async function count(sql: string): Promise<number> {
  return (await env.DB.prepare(sql).first<{ n: number }>())!.n;
}

async function setPref(email: string, mode: string, agencies: string[]) {
  await env.DB.prepare(
    "INSERT INTO preferences (subscriber, mode, agencies, version, updated_at) VALUES (?1, ?2, ?3, 1, 0)",
  )
    .bind(await sid(email), mode, JSON.stringify(agencies))
    .run();
}

describe("인증", () => {
  it("토큰이 없거나 틀리면 401, DB 는 바뀌지 않는다", async () => {
    const body = await syncRequest(["a@example.com"]);
    const variants: Record<string, string>[] = [{}, { Authorization: "Bearer wrong-token" }, { Authorization: env.INTERNAL_API_TOKEN }];
    for (const headers of variants) {
      const res = await call("POST", "/internal/sync-and-resolve", { body, headers, origin: null });
      expect(res.status).toBe(401);
    }
    expect(await count("SELECT count(*) AS n FROM subscribers")).toBe(0);
  });

  it("서버 토큰이 설정되지 않았으면(짧으면) 아무도 통과시키지 않는다", async () => {
    const weak = { ...env, INTERNAL_API_TOKEN: "" } as Env;
    const res = await post(await syncRequest(["a@example.com"]), {
      env: weak,
      headers: { Authorization: "Bearer " },
    });
    expect(res.status).toBe(500);
    expect(await count("SELECT count(*) AS n FROM subscribers")).toBe(0);
  });
});

describe("구독자 식별 키 일치 확인(key_check)", () => {
  it("GitHub 와 Worker 의 SUBSCRIBER_HASH_KEY 가 다르면 409 — 성공으로 응답하지 않고 DB 도 그대로", async () => {
    await sync(["paused@example.com", "asm@example.com"]);
    await setPref("paused@example.com", "selected", []);
    await setPref("asm@example.com", "selected", ["assembly"]);
    const before = (await env.DB.prepare("SELECT * FROM subscribers ORDER BY subscriber").all()).results;
    // Python 쪽에 다른 키가 등록된 상황: 식별자와 key_check 가 모두 그 키로 계산된다.
    const wrongKey = "a-different-key-pasted-in-github-0123456789";
    const res = await post({
      api_version: 1,
      key_check: await hmacHex(wrongKey, KEY_CHECK_MESSAGE),
      subscribers: [await hmacHex(wrongKey, "paused@example.com"), await hmacHex(wrongKey, "asm@example.com")].sort(),
      agencies: CATALOG,
    });
    expect(res.status).toBe(409);
    expect(await res.json()).toEqual({ error: "hash_key_mismatch" });
    expect((await env.DB.prepare("SELECT * FROM subscribers ORDER BY subscriber").all()).results).toEqual(before);
    expect(await count("SELECT count(*) AS n FROM preferences")).toBe(2);
  });

  it("key_check 누락·형식 오류는 400, Worker 키가 없으면 500", async () => {
    const base = await syncRequest(["a@example.com"]);
    const { key_check: _omit, ...missing } = base;
    expect(await (await post(missing)).json()).toEqual({ error: "bad_key_check" });
    expect(await (await post({ ...base, key_check: "ABC" })).json()).toEqual({ error: "bad_key_check" });
    const noKey = await post(base, { env: { ...env, SUBSCRIBER_HASH_KEY: "" } as Env });
    expect(noKey.status).toBe(500);
    expect(await count("SELECT count(*) AS n FROM subscribers")).toBe(0);
  });
});

describe("요청 검증 — 실패하면 200 이 아니고 DB 변경 없음", () => {
  const cases: [string, (b: any) => unknown, number, string][] = [
    ["api_version 2", (b) => ({ ...b, api_version: 2 }), 400, "unsupported_api_version"],
    ["api_version 문자열", (b) => ({ ...b, api_version: "1" }), 400, "unsupported_api_version"],
    ["api_version true", (b) => ({ ...b, api_version: true }), 400, "unsupported_api_version"],
    ["빈 구독자", (b) => ({ ...b, subscribers: [] }), 400, "empty_subscribers"],
    ["대문자 hex", (b) => ({ ...b, subscribers: [b.subscribers[0].toUpperCase()] }), 400, "bad_subscriber"],
    ["짧은 식별자", (b) => ({ ...b, subscribers: ["abc"] }), 400, "bad_subscriber"],
    ["중복 구독자", (b) => ({ ...b, subscribers: [b.subscribers[0], b.subscribers[0]] }), 400, "duplicate_subscriber"],
    ["빈 카탈로그", (b) => ({ ...b, agencies: [] }), 400, "empty_agencies"],
    ["기관 ID 형식", (b) => ({ ...b, agencies: [{ id: "FSC!", name: "x" }] }), 400, "bad_agency"],
    ["기관 이름 없음", (b) => ({ ...b, agencies: [{ id: "fsc" }] }), 400, "bad_agency"],
    ["기관 ID 중복", (b) => ({ ...b, agencies: [CATALOG[0], CATALOG[0]] }), 400, "duplicate_agency"],
    ["배열 본문", () => [], 400, "bad_request"],
  ];
  for (const [name, mutate, status, code] of cases) {
    it(name, async () => {
      const res = await post(mutate(await syncRequest(["a@example.com", "b@example.com"])));
      expect(res.status).toBe(status);
      expect(await res.json()).toEqual({ error: code });
      expect(await count("SELECT count(*) AS n FROM subscribers")).toBe(0);
      expect(await count("SELECT count(*) AS n FROM agencies")).toBe(0);
    });
  }

  it("깨진 JSON / 잘못된 Content-Type / 너무 큰 본문", async () => {
    expect((await post(undefined, { rawBody: "{not json" })).status).toBe(400);
    expect((await post(await syncRequest(["a@example.com"]), { contentType: "text/plain" })).status).toBe(415);
    expect((await post(undefined, { rawBody: "x".repeat(600 * 1024) })).status).toBe(413);
    expect(await count("SELECT count(*) AS n FROM subscribers")).toBe(0);
  });
});

describe("동기화와 응답", () => {
  it("최초 동기화: 구독자 생성, 모두 mode=all, 요청 순서대로 정확한 v1 스키마", async () => {
    const req = await syncRequest(["a@example.com", "b@example.com", "c@example.com"]);
    const res = await post(req);
    expect(res.status).toBe(200);
    const body = await res.json<any>();
    expect(Object.keys(body).sort()).toEqual(["api_version", "subscribers"]);
    expect(body.api_version).toBe(1);
    expect(body.subscribers.map((s: any) => s.subscriber)).toEqual(req.subscribers);
    for (const s of body.subscribers) {
      expect(Object.keys(s).sort()).toEqual(["agencies", "mode", "subscriber"]);
      expect(s).toMatchObject({ mode: "all", agencies: [] });
    }
    expect(await count("SELECT count(*) AS n FROM subscribers WHERE active = 1")).toBe(3);
    expect(await count("SELECT count(*) AS n FROM agencies WHERE active = 1")).toBe(5);
  });

  it("저장된 선택은 보존되고 카탈로그 순서로 응답한다(의안만 / 금융위+의안 / 일시중지)", async () => {
    await sync(["only-asm@example.com", "fsc-asm@example.com", "paused@example.com", "all@example.com"]);
    await setPref("only-asm@example.com", "selected", ["assembly"]);
    await setPref("fsc-asm@example.com", "selected", ["assembly", "fsc"]);
    await setPref("paused@example.com", "selected", []);
    const body = await (await sync(["only-asm@example.com", "fsc-asm@example.com", "paused@example.com", "all@example.com"])).json<any>();
    const by = Object.fromEntries(body.subscribers.map((s: any) => [s.subscriber, s]));
    expect(by[await sid("only-asm@example.com")]).toMatchObject({ mode: "selected", agencies: ["assembly"] });
    expect(by[await sid("fsc-asm@example.com")]).toMatchObject({ mode: "selected", agencies: ["fsc", "assembly"] });
    expect(by[await sid("paused@example.com")]).toMatchObject({ mode: "selected", agencies: [] });
    expect(by[await sid("all@example.com")]).toMatchObject({ mode: "all", agencies: [] });
  });

  it("MAIL_TO 에서 빠지면 비활성(설정 보존, 세션·인증번호 삭제), 다시 넣으면 설정 복원", async () => {
    const cookie = await login("gone@example.com", ["stay@example.com"]);
    await setPref("gone@example.com", "selected", ["pipc"]);
    await env.DB.prepare(
      "INSERT INTO otp_challenges (subscriber, code_hash, salt, attempts, expires_at, created_at) VALUES (?1, ?2, 's', 0, 9999999999, 0)",
    ).bind(await sid("gone@example.com"), "0".repeat(64)).run();

    expect((await sync(["stay@example.com"])).status).toBe(200);
    const row = await env.DB.prepare("SELECT active FROM subscribers WHERE subscriber = ?1").bind(await sid("gone@example.com")).first<{ active: number }>();
    expect(row?.active).toBe(0);
    expect(await count("SELECT count(*) AS n FROM sessions")).toBe(0);
    expect(await count("SELECT count(*) AS n FROM otp_challenges")).toBe(0);
    expect(await count("SELECT count(*) AS n FROM preferences")).toBe(1);
    expect((await call("GET", "/api/preferences", { cookie })).status).toBe(401);

    const back = await (await sync(["stay@example.com", "gone@example.com"])).json<any>();
    const gone = back.subscribers.filter((s: any) => s.mode === "selected");
    expect(gone).toEqual([{ subscriber: await sid("gone@example.com"), mode: "selected", agencies: ["pipc"] }]);
    expect(await count("SELECT count(*) AS n FROM subscribers WHERE active = 1")).toBe(2);
  });

  it("카탈로그 갱신: 이름 변경·추가는 반영, 사라진 기관은 비활성·응답에서 제외", async () => {
    await sync(["sel@example.com", "lost@example.com", "paused@example.com"]);
    await setPref("sel@example.com", "selected", ["fsc", "pipc"]);
    await setPref("lost@example.com", "selected", ["pipc"]);
    await setPref("paused@example.com", "selected", []);
    const next = [
      { id: "fsc", name: "금융위원회(변경)" },
      { id: "fss", name: "금융감독원" },
      { id: "assembly", name: "의안정보시스템" },
      { id: "newagency", name: "신규 기관" },
    ];
    const body = await (await sync(["sel@example.com", "lost@example.com", "paused@example.com"], next)).json<any>();
    const by = Object.fromEntries(body.subscribers.map((s: any) => [s.subscriber, s]));
    expect(by[await sid("sel@example.com")]).toMatchObject({ mode: "selected", agencies: ["fsc"] });
    // 선택한 기관이 전부 사라졌으면 조용히 끊지 않고 전체 수신으로 본다.
    expect(by[await sid("lost@example.com")]).toMatchObject({ mode: "all", agencies: [] });
    // 사용자가 고른 일시중지는 그대로.
    expect(by[await sid("paused@example.com")]).toMatchObject({ mode: "selected", agencies: [] });
    const agencies = (await env.DB.prepare("SELECT id, name, active FROM agencies ORDER BY id").all()).results;
    expect(agencies).toContainEqual({ id: "fsc", name: "금융위원회(변경)", active: 1 });
    expect(agencies).toContainEqual({ id: "pipc", name: "개인정보보호위원회", active: 0 });
    expect(agencies).toContainEqual({ id: "newagency", name: "신규 기관", active: 1 });
  });

  it("변화 없는 재동기화는 아무 행도 쓰지 않는다", async () => {
    await sync(["a@example.com", "b@example.com"]);
    const before = (await env.DB.prepare("SELECT * FROM subscribers ORDER BY subscriber").all()).results;
    const agBefore = (await env.DB.prepare("SELECT * FROM agencies ORDER BY id").all()).results;
    const log = vi.spyOn(console, "log");
    vi.spyOn(Date, "now").mockReturnValue(Date.now() + 3_600_000);
    await sync(["a@example.com", "b@example.com"]);
    expect((await env.DB.prepare("SELECT * FROM subscribers ORDER BY subscriber").all()).results).toEqual(before);
    expect((await env.DB.prepare("SELECT * FROM agencies ORDER BY id").all()).results).toEqual(agBefore);
    expect(log).toHaveBeenCalledWith("sync ok", { requested: 2, activated: 0, deactivated: 0, agencies_changed: 0 });
  });

  it("D1 트랜잭션 실패 시 503 이고 부분 반영이 없다", async () => {
    await sync(["keep@example.com"]);
    const failing = {
      ...env,
      DB: new Proxy(env.DB, {
        get(target, prop) {
          if (prop === "batch") {
            return (stmts: D1PreparedStatement[]) =>
              // 실제 문장들 사이에 제약 위반 문장을 끼워 넣어 batch 중간 실패를 만든다.
              target.batch([...stmts.slice(0, 2), target.prepare("INSERT INTO agencies (id) VALUES (NULL)"), ...stmts.slice(2)]);
          }
          const v = Reflect.get(target, prop);
          return typeof v === "function" ? v.bind(target) : v;
        },
      }),
    } as Env;
    const res = await post(await syncRequest(["new1@example.com", "new2@example.com"]), { env: failing });
    expect(res.status).toBe(503);
    expect(await res.json()).toEqual({ error: "storage_error" });
    // 신규 INSERT·keep 비활성화가 앞 문장에서 실행됐어도 전부 롤백되어야 한다.
    const rows = (await env.DB.prepare("SELECT subscriber, active FROM subscribers").all()).results;
    expect(rows).toEqual([{ subscriber: await sid("keep@example.com"), active: 1 }]);
  });

  it("DB 제약이 손상된 설정 행(알 수 없는 mode·깨진 JSON)의 저장 자체를 막는다", async () => {
    await sync(["a@example.com"]);
    const id = await sid("a@example.com");
    for (const [mode, agencies] of [["bogus", "[]"], ["selected", "{not json"], ["selected", '{"a":1}'], ["all", '["fsc"]']]) {
      const ok = await env.DB.prepare(
        "INSERT INTO preferences (subscriber, mode, agencies, version, updated_at) VALUES (?1, ?2, ?3, 1, 0)",
      ).bind(id, mode, agencies).run().then(() => true, () => false);
      expect(ok).toBe(false);
    }
  });

  it("운영 로그에 식별자·토큰을 남기지 않는다", async () => {
    const spies = ["log", "warn", "error", "info", "debug"].map((m) => vi.spyOn(console, m as "log"));
    await sync(["a@example.com", "b@example.com"]);
    await post({ api_version: 2 });
    const text = JSON.stringify(spies.flatMap((s) => s.mock.calls));
    expect(text).not.toContain(await sid("a@example.com"));
    expect(text).not.toContain(env.INTERNAL_API_TOKEN);
    expect(text).not.toContain("@example.com");
  });
});
