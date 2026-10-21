import { applyD1Migrations } from "cloudflare:test";
import { env } from "cloudflare:workers";

const e = env as unknown as { DB: D1Database; TEST_MIGRATIONS: Parameters<typeof applyD1Migrations>[1] };
await applyD1Migrations(e.DB, e.TEST_MIGRATIONS);
