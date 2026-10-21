// Python ↔ Worker 정규화·HMAC 교차 검증과 난수 유틸.
import { describe, expect, it } from "vitest";
import { normalizeEmail, randomOtp, randomToken, subscriberId, timingSafeEqual } from "../src/crypto";
import vectors from "./fixtures/hmac_vectors.json";

describe("Python 과 같은 정규화·식별자(공통 벡터)", () => {
  for (const c of vectors.cases) {
    it(JSON.stringify(c.input), async () => {
      expect(normalizeEmail(c.input)).toBe(c.normalized);
      if (c.normalized !== null) {
        expect(await subscriberId(c.normalized, vectors.key)).toBe(c.hash);
      }
    });
  }
  it("벡터에 유효·무효 사례가 모두 있다", () => {
    expect(vectors.cases.filter((c) => c.hash !== null).length).toBeGreaterThanOrEqual(5);
    expect(vectors.cases.filter((c) => c.hash === null).length).toBeGreaterThanOrEqual(5);
  });
});

describe("난수·비교", () => {
  it("인증번호는 항상 6자리 숫자이고 고르게 퍼진다", () => {
    const seen = new Set<string>();
    const firstDigits = new Array(10).fill(0);
    for (let i = 0; i < 2000; i++) {
      const code = randomOtp();
      expect(code).toMatch(/^\d{6}$/);
      seen.add(code);
      firstDigits[Number(code[0])]++;
    }
    expect(seen.size).toBeGreaterThan(1990);
    for (const n of firstDigits) expect(n).toBeGreaterThan(120);
  });
  it("세션 토큰은 256비트 base64url", () => {
    const t = randomToken();
    expect(t).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(randomToken()).not.toBe(t);
  });
  it("timingSafeEqual", () => {
    expect(timingSafeEqual("abc", "abc")).toBe(true);
    expect(timingSafeEqual("abc", "abd")).toBe(false);
    expect(timingSafeEqual("abc", "abcd")).toBe(false);
  });
});

describe("Worker ↔ Apps Script 서명 규칙(공통 고정 요청)", () => {
  it("Worker 가 만든 서명이 Apps Script 테스트가 검증하는 고정 요청과 같다", async () => {
    const { canonicalOtpMessage } = await import("../src/mailer");
    const { hmacHex } = await import("../src/crypto");
    const fixture = (await import("../../google-apps-script/test/fixtures/signed_request.json")).default;
    const r = fixture.request;
    expect(await hmacHex(fixture.secret, canonicalOtpMessage(r.ts, r.nonce, r.to, r.code))).toBe(r.sig);
  });
});
