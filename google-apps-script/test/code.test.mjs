// Code.gs 를 Node vm 샌드박스에서 Apps Script 서비스 대역과 함께 실행한다.
// 실행: node --test google-apps-script/test/
// 실제 Google 서비스(MailApp 등)는 호출하지 않는다. 대역은 공식 문서의 메서드 이름·반환 형식을 따른다
// (computeHmacSha256Signature 는 부호 있는 바이트 배열을 돌려준다).
import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";

const here = path.dirname(new URL(import.meta.url).pathname);
const CODE = fs.readFileSync(path.join(here, "..", "Code.gs"), "utf8");
const FIXTURE = JSON.parse(fs.readFileSync(path.join(here, "fixtures", "signed_request.json"), "utf8"));
const SECRET = FIXTURE.secret;

function makeEnv({ quota = 100, lockAvailable = true, sendError = null, secret = SECRET } = {}) {
  const store = secret === null ? {} : { OTP_SHARED_SECRET: secret };
  const sent = [];
  const logs = [];
  let lockHeld = false;
  let propertyOps = 0;
  const props = {
    getProperty: (k) => (propertyOps++, Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
    getProperties: () => (propertyOps++, { ...store }),
    setProperty: (k, v) => (propertyOps++, (store[k] = String(v)), props),
    setProperties: (obj, deleteAllOthers) => {
      propertyOps++;
      if (deleteAllOthers) throw new Error("must not delete other properties");
      for (const [k, v] of Object.entries(obj)) store[k] = String(v);
      return props;
    },
    deleteProperty: (k) => (propertyOps++, delete store[k], props),
  };
  const sandbox = {
    PropertiesService: { getScriptProperties: () => props },
    LockService: {
      getScriptLock: () => ({
        tryLock: () => {
          if (!lockAvailable || lockHeld) return false;
          lockHeld = true;
          return true;
        },
        releaseLock: () => {
          lockHeld = false;
        },
      }),
    },
    MailApp: {
      getRemainingDailyQuota: () => quota,
      sendEmail: (msg) => {
        if (sendError) throw sendError;
        sent.push(msg);
        if (env.onSend) env.onSend(msg);
      },
    },
    Utilities: {
      Charset: { UTF_8: "UTF-8" },
      computeHmacSha256Signature: (value, key, charset) => {
        assert.equal(charset, "UTF-8");
        const digest = crypto.createHmac("sha256", Buffer.from(key, "utf8")).update(value, "utf8").digest();
        return Array.from(digest, (b) => (b > 127 ? b - 256 : b));
      },
      formatDate: (date, tz, fmt) => {
        assert.equal(tz, "UTC");
        assert.equal(fmt, "yyyyMMdd");
        return date.toISOString().slice(0, 10).replace(/-/g, "");
      },
    },
    ContentService: {
      MimeType: { JSON: "JSON" },
      createTextOutput: (content) => ({
        content,
        mime: null,
        setMimeType(m) {
          this.mime = m;
          return this;
        },
      }),
    },
    console: { warn: (...a) => logs.push(a.join(" ")), log: (...a) => logs.push(a.join(" ")), error: (...a) => logs.push(a.join(" ")) },
    JSON,
    Math,
    Date,
    Number,
    String,
    Object,
    Array,
  };
  const env = { sandbox, store, sent, logs, onSend: null, get propertyOps() { return propertyOps; } };
  vm.createContext(sandbox);
  vm.runInContext(CODE, sandbox, { filename: "Code.gs" });
  return env;
}

function sign(req, secret = SECRET) {
  const canonical = ["LRK-OTP-v1", String(req.ts), req.nonce, req.to, req.code].join("\n");
  return crypto.createHmac("sha256", secret).update(canonical, "utf8").digest("hex");
}

function request(overrides = {}, { secret = SECRET, resign = true } = {}) {
  const base = {
    v: 1,
    ts: 1_800_000_000,
    nonce: crypto.randomBytes(16).toString("hex"),
    to: "user@example.com",
    code: "123456",
    ...overrides,
  };
  if (resign && !("sig" in overrides)) base.sig = sign(base, secret);
  return base;
}

const event = (body) => {
  const contents = typeof body === "string" ? body : JSON.stringify(body);
  return { postData: { contents, type: "application/json", length: contents.length, name: "postData" }, parameter: {}, parameters: {} };
};
// vm 컨텍스트에서 만든 객체는 프로토타입이 달라 deepStrictEqual 이 거부하므로 JSON 으로 옮겨 비교한다.
const plain = (v) => JSON.parse(JSON.stringify(v));
const call = (env, body, now = 1_800_000_000) => plain(env.sandbox.handleRequest_(event(body), now));

test("공통 고정 요청(Worker 와 같은 서명 규칙)을 받아들여 정확히 1통 발송", () => {
  const env = makeEnv();
  const res = call(env, FIXTURE.request, FIXTURE.request.ts);
  assert.deepEqual(res, { ok: true });
  assert.equal(env.sent.length, 1);
  assert.equal(env.sent[0].to, "user@example.com");
  assert.match(env.sent[0].body, /인증번호: 123456/);
  assert.equal(env.sent[0].name, "LAW RADER");
  assert.doesNotMatch(env.sent[0].body, /https?:\/\//); // 메일에 링크 없음(피싱 방지)
});

test("doPost 는 공식 필드(e.postData.contents)만 읽고 JSON 텍스트 출력으로 응답", () => {
  const env = makeEnv();
  const now = Math.floor(Date.now() / 1000);
  const out = env.sandbox.doPost(event(request({ ts: now })));
  assert.equal(out.mime, "JSON");
  assert.deepEqual(JSON.parse(out.content), { ok: true });
  const get = env.sandbox.doGet();
  assert.deepEqual(JSON.parse(get.content), { ok: false, error: "method_not_allowed" });
});

test("위조된 서명·변조된 수신자·변조된 번호·다른 키 → 메일 0통", () => {
  const env = makeEnv();
  const good = request();
  const cases = [
    { ...good, sig: "0".repeat(64) },
    { ...good, to: "attacker@example.com" },
    { ...good, code: "654321" },
    { ...good, ts: good.ts + 1 },
    request({}, { secret: "another-secret-another-secret-12345678" }),
  ];
  for (const body of cases) {
    assert.deepEqual(call(env, body), { ok: false, error: "bad_signature" });
  }
  assert.equal(env.sent.length, 0);
});

test("서명 검증 전에는 nonce 저장소를 건드리지 않는다(위조 요청은 비밀 1회 읽기만)", () => {
  const env = makeEnv();
  const before = env.propertyOps;
  call(env, { ...request(), sig: "f".repeat(64) });
  assert.equal(env.propertyOps - before, 1);
});

test("만료·미래 타임스탬프 거절(±300초)", () => {
  const env = makeEnv();
  assert.deepEqual(call(env, request({ ts: 1_800_000_000 - 301 })), { ok: false, error: "expired" });
  assert.deepEqual(call(env, request({ ts: 1_800_000_000 + 301 })), { ok: false, error: "expired" });
  assert.deepEqual(call(env, request({ ts: 1_800_000_000 - 300 })), { ok: true });
  assert.equal(env.sent.length, 1);
});

test("형식 오류(빈 본문·깨진 JSON·필드 누락·대문자 주소·긴 본문·postData 없음) → bad_request, 메일 0통", () => {
  const env = makeEnv();
  const good = request();
  const bodies = [
    "",
    "{not json",
    "[]",
    { ...good, v: 2 },
    { ...good, ts: "1800000000" },
    { ...good, ts: 1_800_000_000.5 },
    { ...good, nonce: "short" },
    { ...good, to: "User@Example.com" },
    { ...good, to: "no-at-sign" },
    { ...good, to: "a@b@c" },
    { ...good, code: "12345" },
    { ...good, sig: "XYZ" },
    "x".repeat(3000),
  ];
  for (const b of bodies) assert.deepEqual(call(env, b), { ok: false, error: "bad_request" });
  assert.deepEqual(plain(env.sandbox.handleRequest_({}, 1_800_000_000)), { ok: false, error: "bad_request" });
  assert.deepEqual(plain(env.sandbox.handleRequest_(undefined, 1_800_000_000)), { ok: false, error: "bad_request" });
  assert.equal(env.sent.length, 0);
});

test("공유 비밀이 없거나 짧으면 발송하지 않는다", () => {
  for (const secret of [null, "short"]) {
    const env = makeEnv({ secret });
    assert.deepEqual(call(env, request()), { ok: false, error: "misconfigured" });
    assert.equal(env.sent.length, 0);
  }
});

test("nonce 재사용 거절 — 영속 저장소(PropertiesService)에 기록", () => {
  const env = makeEnv();
  const body = request();
  assert.deepEqual(call(env, body), { ok: true });
  assert.deepEqual(call(env, body, 1_800_000_010), { ok: false, error: "replay" });
  assert.equal(env.sent.length, 1);
  assert.equal(env.store[`nonce:${body.nonce}`], String(body.ts));
});

test("병렬 중복 요청: 잠금을 못 얻으면 busy(메일 0통), 발송 중 같은 요청이 끼어들어도 1통", () => {
  const busy = makeEnv({ lockAvailable: false });
  assert.deepEqual(call(busy, request()), { ok: false, error: "busy" });
  assert.equal(busy.sent.length, 0);

  const env = makeEnv();
  const body = request();
  const nested = [];
  env.onSend = () => {
    if (nested.length === 0) nested.push(call(env, body, 1_800_000_001));
  };
  assert.deepEqual(call(env, body), { ok: true });
  assert.deepEqual(nested[0], { ok: false, error: "replay" });
  assert.equal(env.sent.length, 1);
});

test("자체 일일 상한 30통·수신자별 5통", () => {
  const env = makeEnv();
  let ok = 0;
  for (let i = 0; i < 40; i++) {
    const r = call(env, request({ to: `user${i % 8}@example.com` }), 1_800_000_000);
    if (r.ok) ok++;
  }
  assert.equal(ok, 30);
  assert.equal(env.sent.length, 30);
  assert.deepEqual(call(env, request({ to: "fresh@example.com" })), { ok: false, error: "daily_cap" });

  const one = makeEnv();
  const results = Array.from({ length: 6 }, () => call(one, request({ to: "same@example.com" })));
  assert.equal(results.filter((r) => r.ok).length, 5);
  assert.deepEqual(results[5], { ok: false, error: "recipient_cap" });
});

test("MailApp 남은 할당량이 예비분 이하이면 발송하지 않는다", () => {
  const env = makeEnv({ quota: 5 });
  assert.deepEqual(call(env, request()), { ok: false, error: "quota_exhausted" });
  assert.equal(env.sent.length, 0);
});

test("외부 메일 발송 실패는 send_failed — nonce 는 소모되어 같은 요청으로 재발송되지 않는다", () => {
  const env = makeEnv({ sendError: new Error("Service invoked too many times") });
  const body = request();
  assert.deepEqual(call(env, body), { ok: false, error: "send_failed" });
  assert.deepEqual(call(env, body, 1_800_000_001), { ok: false, error: "replay" });
});

test("오래된 nonce·지난 날짜 카운터는 정리된다(저장 용량 상한 유지)", () => {
  const env = makeEnv();
  env.store["nonce:old"] = String(1_800_000_000 - 1000);
  env.store["day:20000101"] = "30";
  env.store["rcpt:20000101:abc"] = "5";
  env.store["unrelated"] = "keep";
  call(env, request());
  assert.equal(env.store["nonce:old"], undefined);
  assert.equal(env.store["day:20000101"], undefined);
  assert.equal(env.store["rcpt:20000101:abc"], undefined);
  assert.equal(env.store.unrelated, "keep");
  assert.equal(env.store.OTP_SHARED_SECRET, SECRET);
});

test("로그에 수신 주소·인증번호·서명이 남지 않는다", () => {
  const env = makeEnv();
  const good = request({ to: "private@example.com", code: "987654" });
  call(env, good);
  call(env, good);
  call(env, { ...good, sig: "0".repeat(64) });
  call(env, "{bad");
  const text = env.logs.join("\n");
  for (const secret of ["private@example.com", "987654", good.sig, SECRET]) assert.ok(!text.includes(secret));
  assert.match(text, /replay/);
});
