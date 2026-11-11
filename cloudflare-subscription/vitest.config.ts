import path from "node:path";
import { cloudflareTest, readD1Migrations } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";

// 테스트는 workerd + 로컬 D1(SQLite) 위에서 실제 마이그레이션을 적용해 돌린다.
// 아래 비밀값은 테스트 전용 더미다(운영 값 아님).
export default defineConfig(async () => {
  const migrations = await readD1Migrations(path.join(import.meta.dirname, "migrations"));
  return {
    plugins: [
      cloudflareTest({
        wrangler: { configPath: "./wrangler.jsonc" },
        miniflare: {
          bindings: {
            TEST_MIGRATIONS: migrations,
            PUBLIC_ORIGIN: "https://lrk-test.example.workers.dev",
            INTERNAL_API_TOKEN: "test-internal-token-0123456789abcdef0123",
            SUBSCRIBER_HASH_KEY: "test-subscriber-hash-key-0123456789abcdef",
            SERVER_SECRET: "test-server-secret-0123456789abcdef012345",
            APPS_SCRIPT_URL: "https://script.google.com/macros/s/TEST_DEPLOYMENT_ID/exec",
            APPS_SCRIPT_SHARED_SECRET: "test-apps-script-secret-0123456789abcdef",
          },
        },
      }),
    ],
    test: { setupFiles: ["./test/apply-migrations.ts"] },
  };
});
