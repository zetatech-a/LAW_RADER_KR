// 매일 cron: 만료된 인증번호·세션·요청 제한 기록 정리.
import { beforeEach, expect, it } from "vitest";
import { cleanup } from "../src/index";
import { env, resetDb, sid, sync } from "./helpers";

beforeEach(resetDb);

it("만료된 행만 지우고 유효한 행·구독 설정은 남긴다", async () => {
  await sync(["a@example.com", "b@example.com"]);
  const a = await sid("a@example.com");
  const b = await sid("b@example.com");
  const now = 1_800_000_000;
  await env.DB.batch([
    env.DB.prepare("INSERT INTO otp_challenges VALUES (?1, ?2, 's', 0, ?3, 0)").bind(a, "1".repeat(64), now - 1),
    env.DB.prepare("INSERT INTO otp_challenges VALUES (?1, ?2, 's', 0, ?3, 0)").bind(b, "2".repeat(64), now + 60),
    env.DB.prepare("INSERT INTO sessions VALUES (?1, ?2, ?3, 0)").bind("3".repeat(64), a, now - 1),
    env.DB.prepare("INSERT INTO sessions VALUES (?1, ?2, ?3, 0)").bind("4".repeat(64), b, now + 60),
    env.DB.prepare("INSERT INTO rate_limits VALUES ('old', 0, 1, ?1)").bind(now - 1),
    env.DB.prepare("INSERT INTO rate_limits VALUES ('live', 0, 1, ?1)").bind(now + 60),
    env.DB.prepare("INSERT INTO preferences VALUES (?1, 'selected', '[]', 1, 0)").bind(a),
  ]);
  await cleanup(env, now);
  const n = async (t: string) => (await env.DB.prepare(`SELECT count(*) AS n FROM ${t}`).first<{ n: number }>())!.n;
  expect(await n("otp_challenges")).toBe(1);
  expect(await n("sessions")).toBe(1);
  expect(await n("rate_limits")).toBe(1);
  expect(await n("preferences")).toBe(1);
  expect(await n("subscribers")).toBe(2);
});
