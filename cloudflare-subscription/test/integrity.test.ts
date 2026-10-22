// 설정 유실·D1 교체·키 동시 변경 감지(Phase 2.6). 어느 경우든 기본값(mode=all)으로 조용히 돌아가지 않고
// 내부 API 가 409 로 거절해야 한다(Python 은 발송 중단). 거절 시 DB 는 바뀌지 않는다.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { hmacHex } from "../src/crypto";
import type { Env } from "../src/env";
import { KEY_CHECK_MESSAGE } from "../src/internal";
import { CATALOG, call, env, login, resetDb, sid, sync, syncRequest } from "./helpers";

beforeEach(resetDb);
afterEach(() => vi.restoreAllMocks());

const auth = { Authorization: `Bearer ${env.INTERNAL_API_TOKEN}` };
const post = (body: unknown, e: Env = env) =>
  call("POST", "/internal/sync-and-resolve", { body, headers: auth, origin: null, env: e });
const count = async (sql: string) => (await env.DB.prepare(sql).first<{ n: number }>())!.n;
const snapshot = async () =>
  JSON.stringify(
    await Promise.all(
      ["subscribers", "agencies", "preferences", "service_meta"].map(
        async (t) => (await env.DB.prepare(`SELECT * FROM ${t} ORDER BY 1`).all()).results,
      ),
    ),
  );

async function bootstrap(emails: string[]): Promise<string> {
  const res = await sync(emails);
  expect(res.status).toBe(200);
  return (await res.json<{ instance: string }>()).instance;
}

describe("D1 식별자(instance)", () => {
  it("최초 동기화가 식별자를 만들고, 같은 식별자로 다시 오면 정상", async () => {
    const instance = await bootstrap(["a@example.com"]);
    expect(instance).toMatch(/^[0-9a-f]{32}$/);
    const meta = (await env.DB.prepare("SELECT k, v FROM service_meta ORDER BY k").all()).results;
    expect(meta).toEqual([
      { k: "instance", v: instance },
      { k: "key_check", v: await hmacHex(env.SUBSCRIBER_HASH_KEY, KEY_CHECK_MESSAGE) },
    ]);
    const again = await post(await syncRequest(["a@example.com"], CATALOG, { instance }));
    expect(again.status).toBe(200);
    expect((await again.json<{ instance: string }>()).instance).toBe(instance);
  });

  it("D1 이 재생성(빈 DB)됐는데 Python 이 기존 식별자를 보내면 409 instance_missing, 아무것도 쓰지 않음", async () => {
    const instance = await bootstrap(["paused@example.com"]);
    await resetDb(); // 운영 D1 이 통째로 새로 만들어진 상황
    const res = await post(await syncRequest(["paused@example.com"], CATALOG, { instance }));
    expect(res.status).toBe(409);
    expect(await res.json()).toEqual({ error: "instance_missing" });
    expect(await count("SELECT count(*) AS n FROM subscribers")).toBe(0);
    expect(await count("SELECT count(*) AS n FROM service_meta")).toBe(0);
  });

  it("다른 D1 의 식별자면 409 instance_mismatch", async () => {
    await bootstrap(["a@example.com"]);
    const before = await snapshot();
    const res = await post(await syncRequest(["a@example.com"], CATALOG, { instance: "f".repeat(32) }));
    expect(await res.json()).toEqual({ error: "instance_mismatch" });
    expect(await snapshot()).toBe(before);
  });

  it("식별자 형식 오류는 400", async () => {
    const res = await post(await syncRequest(["a@example.com"], CATALOG, { instance: "XYZ" }));
    expect(await res.json()).toEqual({ error: "bad_instance" });
  });

  it("식별자를 보내지 않으면(운영 Variable 미설정) 검사를 생략한다 — 기존 동작 호환", async () => {
    await bootstrap(["a@example.com"]);
    expect((await sync(["a@example.com"])).status).toBe(200);
  });
});

describe("GitHub·Worker HMAC 키가 함께 바뀜", () => {
  it("최초 동기화 때의 키 확인값과 다르면 409 key_changed — 전원 새 구독자(mode=all)로 처리하지 않음", async () => {
    await bootstrap(["paused@example.com"]);
    await env.DB.prepare("INSERT INTO preferences VALUES (?1, 'selected', '[]', 1, 0)").bind(await sid("paused@example.com")).run();
    await env.DB.prepare("UPDATE subscribers SET pref_saved = 1").run();
    const before = await snapshot();
    const newKey = "rotated-key-in-both-github-and-worker-0123";
    const rotated = { ...env, SUBSCRIBER_HASH_KEY: newKey } as Env;
    const res = await post(
      {
        api_version: 1,
        key_check: await hmacHex(newKey, KEY_CHECK_MESSAGE),
        subscribers: [await hmacHex(newKey, "paused@example.com")],
        agencies: CATALOG,
      },
      rotated,
    );
    expect(res.status).toBe(409);
    expect(await res.json()).toEqual({ error: "key_changed" });
    expect(await snapshot()).toBe(before);
  });
});

describe("설정 행 유실(pref_saved)", () => {
  it("설정을 저장하면 저장 이력이 남는다", async () => {
    const cookie = await login("a@example.com");
    expect(await env.DB.prepare("SELECT pref_saved FROM subscribers").first("pref_saved")).toBe(0);
    const res = await call("PUT", "/api/preferences", { cookie, body: { mode: "selected", agencies: ["fsc"], version: 0 } });
    expect(res.status).toBe(200);
    expect(await env.DB.prepare("SELECT pref_saved FROM subscribers").first("pref_saved")).toBe(1);
  });

  it("저장 이력이 있는데 설정 행이 사라지면 409 preference_missing — mode=all 로 돌아가지 않음, 쓰기 없음", async () => {
    const cookie = await login("saved@example.com", ["new@example.com"]);
    await call("PUT", "/api/preferences", { cookie, body: { mode: "selected", agencies: ["assembly"], version: 0 } });
    await env.DB.prepare("DELETE FROM preferences").run(); // 예기치 않은 행 삭제
    const before = await snapshot();
    const res = await sync(["saved@example.com", "new@example.com"]);
    expect(res.status).toBe(409);
    expect(await res.json()).toEqual({ error: "preference_missing" });
    expect(await snapshot()).toBe(before);
    // 설정 페이지도 '모든 기관 수신'으로 보여 주지 않는다.
    const view = await call("GET", "/api/preferences", { cookie });
    expect(view.status).toBe(409);
    expect(await view.json()).toEqual({ error: "preference_missing" });
  });

  it("신규 구독자(저장 이력 없음)는 지금처럼 mode=all", async () => {
    const res = await sync(["fresh@example.com"]);
    expect(res.status).toBe(200);
    expect((await res.json<any>()).subscribers[0]).toMatchObject({ mode: "all", agencies: [] });
  });

  it("MAIL_TO 에서 빠졌다 돌아온 구독자의 저장 이력·설정은 유지", async () => {
    const cookie = await login("back@example.com", ["stay@example.com"]);
    await call("PUT", "/api/preferences", { cookie, body: { mode: "selected", agencies: ["pipc"], version: 0 } });
    await sync(["stay@example.com"]);
    const res = await sync(["stay@example.com", "back@example.com"]);
    expect(res.status).toBe(200);
    const id = await sid("back@example.com");
    const back = (await res.json<any>()).subscribers.find((x: any) => x.subscriber === id);
    expect(back).toEqual({ subscriber: id, mode: "selected", agencies: ["pipc"] });
    expect(await env.DB.prepare("SELECT pref_saved FROM subscribers WHERE subscriber = ?1").bind(await sid("back@example.com")).first("pref_saved")).toBe(1);
  });

  it("버전 충돌로 저장하지 못한 경우에도 거짓 이력이 생기지 않는다", async () => {
    const cookie = await login("a@example.com");
    const res = await call("PUT", "/api/preferences", { cookie, body: { mode: "all", agencies: [], version: 3 } });
    expect(res.status).toBe(409);
    expect(await env.DB.prepare("SELECT pref_saved FROM subscribers").first("pref_saved")).toBe(0);
  });
});
