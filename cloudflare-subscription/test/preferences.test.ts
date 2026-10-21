// GET/PUT /api/preferences — 본인 권한, 입력 검증, 버전 충돌, 내부 API 와의 일관성.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CATALOG, call, env, login, resetDb, sid, sync } from "./helpers";

beforeEach(resetDb);
afterEach(() => vi.restoreAllMocks());

const get = (cookie?: string) => call("GET", "/api/preferences", { cookie });
const put = (cookie: string, body: unknown) => call("PUT", "/api/preferences", { cookie, body });

async function resolve(email: string, all: string[]) {
  const body = await (await sync(all)).json<any>();
  const id = await sid(email);
  return body.subscribers.find((s: any) => s.subscriber === id);
}

describe("미인증", () => {
  it("세션 없이 조회·저장 불가, 설정 상세를 주지 않는다", async () => {
    await sync(["a@example.com"]);
    const g = await get();
    expect(g.status).toBe(401);
    expect(await g.json()).toEqual({ error: "unauthorized" });
    const p = await call("PUT", "/api/preferences", { body: { mode: "all", agencies: [], version: 0 } });
    expect(p.status).toBe(401);
    expect(await env.DB.prepare("SELECT count(*) AS n FROM preferences").first("n")).toBe(0);
  });
});

describe("조회·저장", () => {
  it("초기 상태는 mode=all, version 0, 카탈로그는 D1(동기화된 순서)", async () => {
    const cookie = await login("a@example.com");
    const res = await get(cookie);
    expect(res.status).toBe(200);
    const view = await res.json<any>();
    expect(view).toMatchObject({ mode: "all", agencies: [], version: 0 });
    expect(view.catalog).toEqual(CATALOG);
    expect(JSON.stringify(view)).not.toContain("a@example.com");
    expect(JSON.stringify(view)).not.toContain(await sid("a@example.com"));
  });

  it("의안정보시스템만 선택 → 저장·재조회·내부 API 응답 모두 일치", async () => {
    const cookie = await login("a@example.com");
    const res = await put(cookie, { mode: "selected", agencies: ["assembly"], version: 0 });
    expect(res.status).toBe(200);
    expect(await res.json()).toMatchObject({ mode: "selected", agencies: ["assembly"], version: 1 });
    expect(await (await get(cookie)).json()).toMatchObject({ mode: "selected", agencies: ["assembly"], version: 1 });
    expect(await resolve("a@example.com", ["a@example.com"])).toEqual({
      subscriber: await sid("a@example.com"),
      mode: "selected",
      agencies: ["assembly"],
    });
  });

  it("선택 순서와 무관하게 카탈로그 순서로 저장", async () => {
    const cookie = await login("a@example.com");
    const res = await put(cookie, { mode: "selected", agencies: ["assembly", "fsc"], version: 0 });
    expect((await res.json<any>()).agencies).toEqual(["fsc", "assembly"]);
  });

  it("0개 선택은 일시중지로 저장되고 내부 API 도 selected + 빈 목록", async () => {
    const cookie = await login("a@example.com");
    expect((await put(cookie, { mode: "selected", agencies: [], version: 0 })).status).toBe(200);
    expect(await resolve("a@example.com", ["a@example.com"])).toMatchObject({ mode: "selected", agencies: [] });
  });

  it("전체 수신으로 되돌리기", async () => {
    const cookie = await login("a@example.com");
    await put(cookie, { mode: "selected", agencies: ["fss"], version: 0 });
    const res = await put(cookie, { mode: "all", agencies: [], version: 1 });
    expect(await res.json()).toMatchObject({ mode: "all", agencies: [], version: 2 });
    expect(await resolve("a@example.com", ["a@example.com"])).toMatchObject({ mode: "all", agencies: [] });
  });
});

describe("입력 검증", () => {
  const bad: [string, unknown, string][] = [
    ["알 수 없는 기관", { mode: "selected", agencies: ["bank"], version: 0 }, "unknown_agency"],
    ["잘못된 mode", { mode: "some", agencies: [], version: 0 }, "bad_mode"],
    ["all 인데 기관 목록", { mode: "all", agencies: ["fsc"], version: 0 }, "bad_agencies"],
    ["중복 기관", { mode: "selected", agencies: ["fsc", "fsc"], version: 0 }, "bad_agencies"],
    ["문자열 아닌 기관", { mode: "selected", agencies: [1], version: 0 }, "bad_agencies"],
    ["음수 version", { mode: "all", agencies: [], version: -1 }, "bad_version"],
    ["소수 version", { mode: "all", agencies: [], version: 1.5 }, "bad_version"],
    ["배열 본문", [], "bad_request"],
  ];
  for (const [name, body, code] of bad) {
    it(name, async () => {
      const cookie = await login("a@example.com");
      const res = await put(cookie, body);
      expect(res.status).toBe(400);
      expect(await res.json()).toEqual({ error: code });
      expect(await env.DB.prepare("SELECT count(*) AS n FROM preferences").first("n")).toBe(0);
    });
  }

  it("비활성(카탈로그에서 빠진) 기관은 선택할 수 없다", async () => {
    const cookie = await login("a@example.com");
    await sync(["a@example.com"], CATALOG.filter((c) => c.id !== "pipc"));
    const res = await put(cookie, { mode: "selected", agencies: ["pipc"], version: 0 });
    expect(await res.json()).toEqual({ error: "unknown_agency" });
  });
});

describe("본인 권한과 동시성", () => {
  it("본문에 다른 구독자를 넣어도 본인 설정만 바뀐다", async () => {
    const cookie = await login("a@example.com", ["b@example.com"]);
    const res = await put(cookie, {
      mode: "selected",
      agencies: ["fsc"],
      version: 0,
      subscriber: await sid("b@example.com"),
    });
    expect(res.status).toBe(200);
    const rows = (await env.DB.prepare("SELECT subscriber FROM preferences").all()).results;
    expect(rows).toEqual([{ subscriber: await sid("a@example.com") }]);
  });

  it("두 탭이 같은 version 으로 저장하면 두 번째는 409 + 최신 상태, 덮어쓰지 않음", async () => {
    const cookie = await login("a@example.com");
    expect((await put(cookie, { mode: "selected", agencies: ["fsc"], version: 0 })).status).toBe(200);
    const stale = await put(cookie, { mode: "selected", agencies: ["pipc"], version: 0 });
    expect(stale.status).toBe(409);
    const body = await stale.json<any>();
    expect(body.error).toBe("version_conflict");
    expect(body.current).toMatchObject({ mode: "selected", agencies: ["fsc"], version: 1 });
    const stale2 = await put(cookie, { mode: "all", agencies: [], version: 7 });
    expect(stale2.status).toBe(409);
    expect(await (await get(cookie)).json()).toMatchObject({ agencies: ["fsc"], version: 1 });
  });

  it("병렬 저장: 같은 version 으로 동시에 보내면 정확히 하나만 반영", async () => {
    const cookie = await login("a@example.com");
    await put(cookie, { mode: "all", agencies: [], version: 0 });
    const results = await Promise.all(
      ["fsc", "fss", "pipc", "assembly"].map((a) => put(cookie, { mode: "selected", agencies: [a], version: 1 })),
    );
    expect(results.filter((r) => r.status === 200)).toHaveLength(1);
    expect(results.filter((r) => r.status === 409)).toHaveLength(3);
    expect(await (await get(cookie)).json()).toMatchObject({ version: 2 });
  });

  it("최초 저장(version 0)이 동시에 두 번 오면 하나만 생성", async () => {
    const cookie = await login("a@example.com");
    const results = await Promise.all([
      put(cookie, { mode: "selected", agencies: ["fsc"], version: 0 }),
      put(cookie, { mode: "selected", agencies: ["fss"], version: 0 }),
    ]);
    expect(results.map((r) => r.status).sort()).toEqual([200, 409]);
  });
});
