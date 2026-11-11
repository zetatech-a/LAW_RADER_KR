// 정적 페이지·라우팅·보안 헤더.
import { beforeEach, describe, expect, it } from "vitest";
import { call, resetDb } from "./helpers";

beforeEach(resetDb);

const EXPECTED_CSP =
  "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; " +
  "form-action 'none'; base-uri 'none'; frame-ancestors 'none'";

function expectSecurityHeaders(res: Response) {
  expect(res.headers.get("Content-Security-Policy")).toBe(EXPECTED_CSP);
  expect(res.headers.get("Cache-Control")).toBe("no-store");
  expect(res.headers.get("X-Content-Type-Options")).toBe("nosniff");
  expect(res.headers.get("X-Frame-Options")).toBe("DENY");
  expect(res.headers.get("Referrer-Policy")).toBe("no-referrer");
  expect(res.headers.get("Access-Control-Allow-Origin")).toBeNull();
}

describe("설정 페이지", () => {
  it("HTML·JS·CSS 를 올바른 형식과 보안 헤더로 제공", async () => {
    for (const [path, type] of [["/", "text/html"], ["/app.js", "text/javascript"], ["/app.css", "text/css"]]) {
      const res = await call("GET", path);
      expect(res.status).toBe(200);
      expect(res.headers.get("Content-Type")).toContain(type);
      expectSecurityHeaders(res);
    }
  });

  it("인라인 스크립트·인라인 이벤트·기관 하드코딩이 없다", async () => {
    const html = await (await call("GET", "/")).text();
    const js = await (await call("GET", "/app.js")).text();
    expect(html).not.toMatch(/<script(?![^>]*\bsrc=)[^>]*>/);
    expect(html).not.toMatch(/\son[a-z]+=/i);
    expect(js).not.toContain("innerHTML");
    for (const name of ["금융위원회", "금융감독원", "개인정보보호위원회", "의안정보시스템", '"fsc"', '"assembly"']) {
      expect(html + js).not.toContain(name);
    }
  });

  it("접근성 기본 요소(label·fieldset·legend·aria-live)", async () => {
    const html = await (await call("GET", "/")).text();
    expect(html).toContain('<label for="email">');
    expect(html).toContain('<label for="code">');
    expect(html).toContain("<legend>");
    expect(html).toContain('aria-live="polite"');
    expect(html).toContain('lang="ko"');
  });
});

describe("라우팅", () => {
  it("없는 경로 404, 허용되지 않은 메서드 405 — 오류에도 보안 헤더", async () => {
    const nf = await call("GET", "/nope");
    expect(nf.status).toBe(404);
    expectSecurityHeaders(nf);
    const na = await call("DELETE", "/api/preferences");
    expect(na.status).toBe(405);
    expectSecurityHeaders(na);
    expect((await call("GET", "/internal/sync-and-resolve")).status).toBe(405);
  });

  it("isolate 메모리 제한: 같은 IP 가 분당 30회를 넘기면 D1 이전에 429", async () => {
    const statuses = [];
    for (let i = 0; i < 31; i++) statuses.push((await call("GET", "/api/preferences", { ip: "192.0.2.250" })).status);
    expect(statuses.slice(0, 30).every((s) => s === 401)).toBe(true);
    expect(statuses[30]).toBe(429);
  });
});
