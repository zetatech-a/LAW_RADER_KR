interface Env {
  GITHUB_TOKEN: string;
  GITHUB_OWNER: string;
  GITHUB_REPO: string;
  GITHUB_WORKFLOW: string;
  GITHUB_REF: string;
}

const ERROR_BODY_LIMIT = 2_000;

function validateEnv(env: Env): void {
  const requiredBindings = {
    GITHUB_TOKEN: env.GITHUB_TOKEN,
    GITHUB_OWNER: env.GITHUB_OWNER,
    GITHUB_REPO: env.GITHUB_REPO,
    GITHUB_WORKFLOW: env.GITHUB_WORKFLOW,
    GITHUB_REF: env.GITHUB_REF,
  };
  for (const [name, value] of Object.entries(requiredBindings)) {
    if (typeof value !== "string" || value.trim() === "") {
      throw new Error(`Required binding ${name} is empty`);
    }
  }
}

function workflowDispatchUrl(env: Env): string {
  const owner = encodeURIComponent(env.GITHUB_OWNER);
  const repo = encodeURIComponent(env.GITHUB_REPO);
  const workflow = encodeURIComponent(env.GITHUB_WORKFLOW);
  return `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}/dispatches`;
}

function parseResponseMetadata(body: string): Record<string, unknown> {
  if (body === "") return {};

  try {
    const parsed: unknown = JSON.parse(body);
    if (typeof parsed !== "object" || parsed === null) return {};
    const response = parsed as Record<string, unknown>;
    return Object.fromEntries(
      ["workflow_run_id", "run_url", "html_url"]
        .filter((key) => key in response)
        .map((key) => [key, response[key]]),
    );
  } catch {
    return {};
  }
}

export default {
  async scheduled(controller, env): Promise<void> {
    controller.noRetry();
    validateEnv(env);

    const scheduledFor = new Date(controller.scheduledTime).toISOString();
    const response = await fetch(workflowDispatchUrl(env), {
      method: "POST",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        "Content-Type": "application/json",
	"User-Agent": "law-rader-kr-scheduler",
        "X-GitHub-Api-Version": "2026-03-10",
      },
      body: JSON.stringify({
        ref: env.GITHUB_REF,
        inputs: {
          trigger_source: "cloudflare-cron",
          scheduled_for: scheduledFor,
          cron_expression: controller.cron,
        },
      }),
    });
    const responseBody = await response.text();

    if (!response.ok) {
      throw new Error(
        `GitHub workflow dispatch failed: ${response.status} ${response.statusText}; body=${responseBody.slice(0, ERROR_BODY_LIMIT)}`,
      );
    }

    console.log("GitHub workflow dispatch accepted", {
      status: response.status,
      scheduled_for: scheduledFor,
      cron_expression: controller.cron,
      ...parseResponseMetadata(responseBody),
    });
  },
} satisfies ExportedHandler<Env>;
