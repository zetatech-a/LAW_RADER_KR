// 인증번호 요청·검증, 세션, 로그아웃, CSRF, 요청 제한, 개인정보 비노출.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LIMITS } from "../src/auth";
import type { Env } from "../src/env";
import {
  call,
  cookieFrom,
  env,
  expectedSignature,
  login,
  resetDb,
  sid,
  stubAppsScript,
  freezeInDay,
  sync,
  travel,
} from "./helpers";

beforeEach(resetDb);
afterEach(() => vi.restoreAllMocks());

const requestOtp = (email: string, ip?: string) => call("POST", "/api/otp/request", { body: { email }, ip });
const verify = (email: string, code: string, ip?: string) =>
  call("POST", "/api/otp/verify", { body: { email, code }, ip });
const count = async (sql: string) => (await env.DB.prepare(sql).first<{ n: number }>())!.n;

describe("인증번호 요청", () => {
  it("활성 구독자: 202, 서명된 요청 1건, 수신자는 정규화 주소, DB 에는 해시만", async () => {
    await sync(["user@example.com"]);
    const mail = stubAppsScript();
    const res = await requestOtp("  User@Example.COM ");
    expect(res.status).toBe(202);
    expect(await res.json()).toEqual({ ok: true });
    expect(mail.sent).toHaveLength(1);
    const m = mail.sent[0];
    expect(m.url).toBe(env.APPS_SCRIPT_URL);
    expect(m.body.to).toBe("user@example.com");
    expect(m.body.code).toMatch(/^\d{6}$/);
    expect(m.body.sig).toBe(await expectedSignature(m.body));
    expect(Math.abs(m.body.ts - Date.now() / 1000)).toBeLessThan(5);
    const row = await env.DB.prepare("SELECT * FROM otp_challenges").first<Record<string, unknown>>();
    expect(JSON.stringify(row)).not.toContain(m.body.code);
    expect(row?.code_hash).toMatch(/^[0-9a-f]{64}$/);
    expect(row?.expires_at).toBe(m.body.ts + 600);
  });

  it("미등록·비활성·최초 동기화 전 주소는 같은 202 응답이지만 메일은 0통", async () => {
    // 최초 동기화 전(구독자 테이블 비어 있음)
    let mail = stubAppsScript();
    const before = await requestOtp("user@example.com");
    expect(before.status).toBe(202);
    expect(mail.sent).toHaveLength(0);
    mail.spy.mockRestore();

    await sync(["member@example.com", "gone@example.com"]);
    await sync(["member@example.com"]); // gone 은 비활성
    mail = stubAppsScript();
    const member = await requestOtp("member@example.com", "198.51.100.1");
    const stranger = await requestOtp("stranger@example.com", "198.51.100.2");
    const gone = await requestOtp("gone@example.com", "198.51.100.3");
    for (const r of [member, stranger, gone]) {
      expect(r.status).toBe(202);
      expect(await r.json()).toEqual({ ok: true });
    }
    expect(mail.sent.map((m) => m.body.to)).toEqual(["member@example.com"]);
    expect(await count("SELECT count(*) AS n FROM otp_challenges")).toBe(1);
  });

  it("이메일 형식 오류는 400(등록 여부와 무관)", async () => {
    for (const email of ["", "no-at", "홍길동@example.com", 42]) {
      const res = await call("POST", "/api/otp/request", { body: { email } });
      expect(res.status).toBe(400);
    }
  });

  it("이메일 기준 15분 3회 — 4번째는 등록 여부와 무관하게 429", async () => {
    freezeInDay(3_600 + 60);
    await sync(["member@example.com"]);
    const mail = stubAppsScript();
    for (const email of ["member@example.com", "stranger@example.com"]) {
      for (let i = 0; i < LIMITS.emailPer15m; i++) {
        expect((await requestOtp(email, `192.0.2.${i}`)).status).toBe(202);
      }
      const blocked = await requestOtp(email, "192.0.2.99");
      expect(blocked.status).toBe(429);
      expect(await blocked.json()).toEqual({ error: "rate_limited" });
    }
    expect(mail.sent).toHaveLength(LIMITS.emailPer15m);
  });

  it("이메일 기준 하루 5회", async () => {
    await sync(["member@example.com"]);
    stubAppsScript();
    let sent = 0;
    for (let window = 0; window < 3; window++) {
      const t = freezeInDay(3_600 + window * 900 + 1);
      for (let i = 0; i < 3; i++) {
        const r = await requestOtp("member@example.com", `192.0.2.${window * 10 + i}`);
        if (r.status === 202) sent++;
      }
      t.mockRestore();
    }
    expect(sent).toBe(LIMITS.emailPerDay);
  });

  it("IP 기준 시간당 10회", async () => {
    freezeInDay(7_200 + 60);
    stubAppsScript();
    const statuses = [];
    for (let i = 0; i < LIMITS.ipPerHour + 1; i++) {
      statuses.push((await requestOtp(`u${i}@example.com`, "198.51.100.77")).status);
    }
    expect(statuses.slice(0, LIMITS.ipPerHour).every((s) => s === 202)).toBe(true);
    expect(statuses.at(-1)).toBe(429);
  });

  it("전체 하루 30통 상한에 도달하면 모두 429(구독자·비구독자 동일), 메일 0통", async () => {
    freezeInDay(3_600);
    await sync(["member@example.com"]);
    const day = Math.floor(Date.now() / 1000);
    const start = day - (day % 86_400);
    await env.DB.prepare("INSERT INTO rate_limits VALUES ('otp_sent_global', ?1, ?2, ?3)")
      .bind(start, LIMITS.globalSendsPerDay, start + 86_400)
      .run();
    const mail = stubAppsScript();
    expect((await requestOtp("member@example.com", "192.0.2.1")).status).toBe(429);
    expect((await requestOtp("stranger@example.com", "192.0.2.2")).status).toBe(429);
    expect(mail.sent).toHaveLength(0);
  });

  it("Apps Script 실패(ok:false·HTTP 오류·네트워크)여도 사용자 응답은 같고 로그에 개인정보 없음", async () => {
    await sync(["member@example.com"]);
    const errors = vi.spyOn(console, "error");
    for (const [i, reply] of [
      () => Response.json({ ok: false, error: "quota_exhausted" }),
      () => new Response("oops", { status: 500 }),
      () => {
        throw new TypeError("network down");
      },
    ].entries()) {
      const mail = stubAppsScript(reply as () => Response);
      expect((await requestOtp("member@example.com", `192.0.2.${i}`)).status).toBe(202);
      mail.spy.mockRestore();
    }
    const logged = JSON.stringify(errors.mock.calls);
    expect(logged).toContain("quota_exhausted");
    expect(logged).toContain("http_500");
    expect(logged).not.toContain("member@example.com");
    expect(logged).not.toContain(await sid("member@example.com"));
  });

  it("Apps Script 주소·비밀이 잘못 설정되면 외부 요청 자체를 하지 않는다", async () => {
    await sync(["member@example.com"]);
    const mail = stubAppsScript();
    for (const bad of [
      { APPS_SCRIPT_URL: "https://evil.example.com/exec" },
      { APPS_SCRIPT_SHARED_SECRET: "short" },
    ]) {
      const res = await call("POST", "/api/otp/request", {
        body: { email: "member@example.com" },
        env: { ...env, ...bad } as Env,
        ip: `192.0.2.${Object.keys(bad)[0].length}`,
      });
      expect(res.status).toBe(202);
    }
    expect(mail.sent).toHaveLength(0);
  });
});

describe("인증번호 검증과 세션", () => {
  async function issue(email = "user@example.com") {
    const mail = stubAppsScript();
    expect((await requestOtp(email)).status).toBe(202);
    mail.spy.mockRestore();
    return mail.sent.at(-1)!.body.code;
  }

  it("성공 시 __Host- 세션 쿠키(HttpOnly·Secure·SameSite=Strict·30분), DB 에는 토큰 해시만", async () => {
    await sync(["user@example.com"]);
    const code = await issue();
    const res = await verify("user@example.com", code);
    expect(res.status).toBe(200);
    const set = res.headers.get("Set-Cookie")!;
    expect(set).toMatch(/^__Host-lrk_session=[A-Za-z0-9_-]{43}; Path=\/; Secure; HttpOnly; SameSite=Strict; Max-Age=1800$/);
    const token = cookieFrom(res).split("=")[1];
    const rows = JSON.stringify((await env.DB.prepare("SELECT * FROM sessions").all()).results);
    expect(rows).not.toContain(token);
    expect(await count("SELECT count(*) AS n FROM otp_challenges")).toBe(0); // 일회용
  });

  it("같은 번호 재사용 불가", async () => {
    await sync(["user@example.com"]);
    const code = await issue();
    expect((await verify("user@example.com", code)).status).toBe(200);
    expect((await verify("user@example.com", code)).status).toBe(400);
  });

  it("틀린 번호 5회 후에는 맞는 번호도 거절", async () => {
    await sync(["user@example.com"]);
    const code = await issue();
    const wrong = code === "000000" ? "111111" : "000000";
    for (let i = 0; i < 5; i++) {
      const r = await verify("user@example.com", wrong, `192.0.2.${i}`);
      expect(await r.json()).toEqual({ error: "invalid_code" });
    }
    expect((await verify("user@example.com", code, "192.0.2.50")).status).toBe(400);
    expect(await count("SELECT count(*) AS n FROM otp_challenges")).toBe(0);
  });

  it("시도 횟수가 이미 5회에 도달한 번호는 맞아도 비교하지 않고 거절(병렬 오답 직후 상태)", async () => {
    await sync(["user@example.com"]);
    const code = await issue();
    // 병렬 오답 5건이 횟수만 올리고 아직 행을 지우기 전의 상태를 그대로 만든다.
    await env.DB.prepare("UPDATE otp_challenges SET attempts = 5").run();
    expect(await (await verify("user@example.com", code)).json()).toEqual({ error: "invalid_code" });
    expect(await count("SELECT count(*) AS n FROM sessions")).toBe(0);
  });

  it("재발급하면 이전 번호는 무효", async () => {
    await sync(["user@example.com"]);
    const first = await issue();
    let second = await issue();
    while (second === first) second = await issue();
    expect((await verify("user@example.com", first)).status).toBe(400);
    expect((await verify("user@example.com", second)).status).toBe(200);
  });

  it("10분이 지나면 만료", async () => {
    await sync(["user@example.com"]);
    const code = await issue();
    travel(601);
    expect((await verify("user@example.com", code)).status).toBe(400);
  });

  it("병렬 검증: 맞는 번호로 동시에 들어와도 세션은 1개만", async () => {
    await sync(["user@example.com"]);
    const code = await issue();
    const results = await Promise.all([1, 2, 3, 4].map((i) => verify("user@example.com", code, `192.0.2.${i}`)));
    expect(results.filter((r) => r.status === 200)).toHaveLength(1);
    expect(await count("SELECT count(*) AS n FROM sessions")).toBe(1);
  });

  it("병렬 오답: 동시에 10번 시도해도 5회를 넘겨 비교하지 않는다", async () => {
    await sync(["user@example.com"]);
    const code = await issue();
    const wrong = code === "123456" ? "654321" : "123456";
    await Promise.all(Array.from({ length: 10 }, (_, i) => verify("user@example.com", wrong, `192.0.2.${i}`)));
    expect(await count("SELECT count(*) AS n FROM otp_challenges")).toBe(0);
  });

  it("미등록 주소·형식 오류 검증은 같은 오류", async () => {
    await sync(["user@example.com"]);
    expect(await (await verify("stranger@example.com", "123456")).json()).toEqual({ error: "invalid_code" });
    expect((await verify("user@example.com", "12345")).status).toBe(400);
  });

  it("IP 기준 검증 시도 제한", async () => {
    freezeInDay(7_200 + 60);
    await sync(["user@example.com"]);
    const statuses = [];
    for (let i = 0; i < LIMITS.verifyPerIpHour + 1; i++) {
      statuses.push((await verify("stranger@example.com", "123456", "198.51.100.200")).status);
    }
    expect(statuses.at(-1)).toBe(429);
  });

  it("세션은 30분 후 만료", async () => {
    const cookie = await login("user@example.com");
    expect((await call("GET", "/api/preferences", { cookie })).status).toBe(200);
    travel(1801);
    expect((await call("GET", "/api/preferences", { cookie })).status).toBe(401);
  });

  it("로그아웃하면 세션이 사라지고 쿠키가 지워진다", async () => {
    const cookie = await login("user@example.com");
    const res = await call("POST", "/api/logout", { cookie, body: {} });
    expect(res.status).toBe(200);
    expect(res.headers.get("Set-Cookie")).toContain("Max-Age=0");
    expect(await count("SELECT count(*) AS n FROM sessions")).toBe(0);
    expect((await call("GET", "/api/preferences", { cookie })).status).toBe(401);
  });

  it("위조·형식 오류 쿠키는 401", async () => {
    await sync(["user@example.com"]);
    for (const cookie of ["__Host-lrk_session=forged", `__Host-lrk_session=${"A".repeat(43)}`, "other=1"]) {
      expect((await call("GET", "/api/preferences", { cookie })).status).toBe(401);
    }
  });
});

describe("CSRF — 상태 변경 API 는 정확한 Origin 과 JSON 만 받는다", () => {
  const targets: [string, string, unknown][] = [
    ["POST", "/api/otp/request", { email: "user@example.com" }],
    ["POST", "/api/otp/verify", { email: "user@example.com", code: "123456" }],
    ["PUT", "/api/preferences", { mode: "all", agencies: [], version: 0 }],
    ["POST", "/api/logout", {}],
  ];
  for (const [method, path, body] of targets) {
    it(`${method} ${path}`, async () => {
      const cookie = await login("user@example.com");
      const mail = stubAppsScript();
      expect((await call(method, path, { body, cookie, origin: null })).status).toBe(403);
      expect((await call(method, path, { body, cookie, origin: "https://evil.example.com" })).status).toBe(403);
      expect((await call(method, path, { body, cookie, contentType: "text/plain" })).status).toBe(415);
      expect((await call(method, path, { body, cookie, contentType: "application/x-www-form-urlencoded" })).status).toBe(415);
      expect(mail.sent).toHaveLength(0);
      expect(await count("SELECT count(*) AS n FROM sessions")).toBe(1); // 로그아웃도 일어나지 않음
    });
  }

  it("PUBLIC_ORIGIN 이 설정되지 않았으면 검사를 건너뛰지 않고 500", async () => {
    const res = await call("POST", "/api/otp/request", {
      body: { email: "user@example.com" },
      env: { ...env, PUBLIC_ORIGIN: "" } as Env,
    });
    expect(res.status).toBe(500);
  });
});

describe("개인정보 비노출", () => {
  it("요청·검증·로그인 전 과정의 로그에 이메일·번호·토큰·식별자가 없다", async () => {
    const spies = ["log", "warn", "error", "info", "debug"].map((m) => vi.spyOn(console, m as "log"));
    await sync(["user@example.com"]);
    const mail = stubAppsScript();
    await requestOtp("user@example.com");
    const code = mail.sent[0].body.code;
    await verify("user@example.com", code === "000000" ? "111111" : "000000");
    const ok = await verify("user@example.com", code);
    const token = cookieFrom(ok).split("=")[1];
    const text = JSON.stringify(spies.flatMap((s) => s.mock.calls));
    for (const secret of ["user@example.com", code, token, await sid("user@example.com"), mail.sent[0].body.sig]) {
      expect(text).not.toContain(secret);
    }
  });
});
