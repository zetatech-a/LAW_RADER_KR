// 기관 카탈로그에서 사용자가 고른 기관이 빠졌을 때 — 선택 수신이 전체 수신으로 '확대'되면 안 된다.
// 불일치는 설정 오류로 보고 내부 API 가 정상(200) 응답을 하지 않는다(Python 은 발송 중단).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CATALOG, call, env, login, resetDb, sid, sync } from "./helpers";

beforeEach(resetDb);
afterEach(() => vi.restoreAllMocks());

const WITHOUT_ASSEMBLY = CATALOG.filter((c) => c.id !== "assembly");
const count = async (sql: string) => (await env.DB.prepare(sql).first<{ n: number }>())!.n;
const snapshot = async () =>
  JSON.stringify(
    await Promise.all(
      ["subscribers", "agencies", "preferences"].map(
        async (t) => (await env.DB.prepare(`SELECT * FROM ${t} ORDER BY 1`).all()).results,
      ),
    ),
  );

async function setPref(email: string, mode: string, agencies: string[]) {
  await env.DB.prepare(
    "INSERT INTO preferences (subscriber, mode, agencies, version, updated_at) VALUES (?1, ?2, ?3, 1, 0)",
  ).bind(await sid(email), mode, JSON.stringify(agencies)).run();
}

describe("카탈로그 불일치 — 선택 수신이 전체 수신으로 확대되지 않는다", () => {
  it("의안만 고른 사용자 + 카탈로그에서 의안 제거 → 200 mode=all 이 아니라 409, DB 변경 없음", async () => {
    await sync(["asm@example.com", "all@example.com"]);
    await setPref("asm@example.com", "selected", ["assembly"]);
    const before = await snapshot();
    const res = await sync(["asm@example.com", "all@example.com"], WITHOUT_ASSEMBLY);
    expect(res.status).toBe(409);
    expect(await res.json()).toEqual({ error: "catalog_mismatch" });
    expect(await snapshot()).toBe(before); // 의안 기관도 비활성화되지 않음
  });

  it("금융위+의안 사용자에게서 의안만 제거해도 409(조용히 좁히거나 넓히지 않음)", async () => {
    await sync(["fa@example.com"]);
    await setPref("fa@example.com", "selected", ["fsc", "assembly"]);
    const res = await sync(["fa@example.com"], WITHOUT_ASSEMBLY);
    expect(res.status).toBe(409);
    expect(await res.json()).toEqual({ error: "catalog_mismatch" });
  });

  it("일시중지(selected + 빈 목록)는 카탈로그 변경과 무관하게 정상 유지", async () => {
    await sync(["paused@example.com"]);
    await setPref("paused@example.com", "selected", []);
    const res = await sync(["paused@example.com"], WITHOUT_ASSEMBLY);
    expect(res.status).toBe(200);
    expect((await res.json<any>()).subscribers[0]).toMatchObject({ mode: "selected", agencies: [] });
  });

  it("기관을 되돌리면 기존 선택이 그대로 복원된다(저장값 보존)", async () => {
    await sync(["fa@example.com"]);
    await setPref("fa@example.com", "selected", ["fsc", "assembly"]);
    expect((await sync(["fa@example.com"], WITHOUT_ASSEMBLY)).status).toBe(409);
    const res = await sync(["fa@example.com"]);
    expect(res.status).toBe(200);
    expect((await res.json<any>()).subscribers[0]).toMatchObject({ mode: "selected", agencies: ["fsc", "assembly"] });
  });

  it("불일치 중에는 어떤 경로로도 mode=all 정상 응답이 나오지 않는다(설정 페이지 포함)", async () => {
    const cookie = await login("asm@example.com");
    await setPref("asm@example.com", "selected", ["assembly"]);
    const res = await sync(["asm@example.com"], WITHOUT_ASSEMBLY);
    expect(res.status).not.toBe(200);
    // 카탈로그를 직접 비활성화한 상태(운영자가 수동으로 바꾼 경우)에서도 설정 페이지가 '전체 수신'으로 보이지 않는다.
    await env.DB.prepare("UPDATE agencies SET active = 0 WHERE id = 'assembly'").run();
    const view = await call("GET", "/api/preferences", { cookie });
    expect(view.status).toBe(409);
    expect(await view.json()).toEqual({ error: "catalog_mismatch" });
  });

  it("아무도 고르지 않은 기관의 제거·이름 변경·추가는 정상 반영", async () => {
    await sync(["sel@example.com"]);
    await setPref("sel@example.com", "selected", ["fsc"]);
    const next = [{ id: "fsc", name: "금융위원회(변경)" }, { id: "fss", name: "금융감독원" }, { id: "newagency", name: "신규" }];
    const res = await sync(["sel@example.com"], next);
    expect(res.status).toBe(200);
    expect((await res.json<any>()).subscribers[0]).toMatchObject({ mode: "selected", agencies: ["fsc"] });
    expect(await count("SELECT count(*) AS n FROM agencies WHERE active = 0")).toBe(3);
  });
});

describe("DEPLOY.md 의 운영자 SQL 이 실제로 동작한다", () => {
  it("기관 제거 절차: 선택에서 그 기관만 빼면 이후 동기화 정상(그 기관만 골랐던 사람은 일시중지)", async () => {
    await sync(["only@example.com", "fa@example.com"]);
    await setPref("only@example.com", "selected", ["assembly"]);
    await setPref("fa@example.com", "selected", ["fsc", "assembly"]);
    // DEPLOY.md '의도적 변경 절차 — 기관 제거' ①·③ 그대로
    const affected = await env.DB.prepare(
      "SELECT count(*) AS n FROM preferences p, json_each(p.agencies) a WHERE a.value = 'assembly'",
    ).first<{ n: number }>();
    expect(affected?.n).toBe(2);
    await env.DB.prepare(
      "UPDATE preferences SET agencies = (SELECT json_group_array(value) FROM json_each(preferences.agencies) WHERE value <> 'assembly'), version = version + 1 WHERE EXISTS (SELECT 1 FROM json_each(preferences.agencies) WHERE value = 'assembly')",
    ).run();
    const res = await sync(["only@example.com", "fa@example.com"], WITHOUT_ASSEMBLY);
    expect(res.status).toBe(200);
    const by = Object.fromEntries((await res.json<any>()).subscribers.map((x: any) => [x.subscriber, x]));
    expect(by[await sid("only@example.com")]).toMatchObject({ mode: "selected", agencies: [] });
    expect(by[await sid("fa@example.com")]).toMatchObject({ mode: "selected", agencies: ["fsc"] });
  });

  it("설정 유실 해소 SQL(Phase 2.7): 유실된 구독자는 일시중지로 복구되고 전체 수신으로 넓혀지지 않는다", async () => {
    const emails = ["lost@example.com", "kept@example.com", "fresh@example.com"];
    await sync(emails);
    await setPref("kept@example.com", "selected", ["fsc"]);
    await env.DB.prepare("UPDATE subscribers SET pref_saved = 1 WHERE subscriber IN (?1, ?2)")
      .bind(await sid("lost@example.com"), await sid("kept@example.com")).run();
    expect((await sync(emails)).status).toBe(409); // lost@ 의 설정 행이 없음 → preference_missing
    // DEPLOY.md '의도적 변경 절차 — 설정 유실' ①·② 그대로
    const affected = await env.DB.prepare(
      "SELECT count(*) AS n FROM subscribers WHERE pref_saved = 1 AND subscriber NOT IN (SELECT subscriber FROM preferences)",
    ).first<{ n: number }>();
    expect(affected?.n).toBe(1);
    await env.DB.prepare(
      "INSERT INTO preferences (subscriber, mode, agencies, version, updated_at) SELECT subscriber, 'selected', '[]', 1, CAST(strftime('%s', 'now') AS INTEGER) FROM subscribers WHERE pref_saved = 1 AND subscriber NOT IN (SELECT subscriber FROM preferences)",
    ).run();
    const res = await sync(emails);
    expect(res.status).toBe(200);
    const by = Object.fromEntries((await res.json<any>()).subscribers.map((x: any) => [x.subscriber, x]));
    expect(by[await sid("lost@example.com")]).toMatchObject({ mode: "selected", agencies: [] }); // 일시중지
    expect(by[await sid("kept@example.com")]).toMatchObject({ mode: "selected", agencies: ["fsc"] }); // 손대지 않음
    expect(by[await sid("fresh@example.com")]).toMatchObject({ mode: "all", agencies: [] }); // 진짜 신규는 그대로
    expect(await count("SELECT count(*) AS n FROM subscribers WHERE pref_saved = 1")).toBe(2); // 저장 이력 보존
    // 본인은 설정 화면에서 다시 고를 수 있다.
    const cookie = await login("lost@example.com", emails.filter((e) => e !== "lost@example.com"));
    const put = await call("PUT", "/api/preferences", { cookie, body: { mode: "all", agencies: [], version: 1 } });
    expect(put.status).toBe(200);
  });
});
