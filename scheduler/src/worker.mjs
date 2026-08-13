const SHANGHAI_TIME_ZONE = "Asia/Shanghai";
const DEFAULT_RETRY_DELAYS_MS = [5_000, 15_000, 45_000];
const DEFAULT_VERIFY_DELAYS_MS = [2_000, 5_000, 10_000];

export class GitHubApiError extends Error {
  constructor(message, status = 0, detail = "") {
    super(message);
    this.name = "GitHubApiError";
    this.status = status;
    this.detail = detail;
  }
}

export function previousShanghaiDate(now = new Date()) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: SHANGHAI_TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  const localMidnightUtc = new Date(
    Date.UTC(Number(values.year), Number(values.month) - 1, Number(values.day)),
  );
  localMidnightUtc.setUTCDate(localMidnightUtc.getUTCDate() - 1);
  return localMidnightUtc.toISOString().slice(0, 10);
}

export function validateTargetDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    throw new Error("目标日期必须为 YYYY-MM-DD");
  }
  const parsed = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value) {
    throw new Error("目标日期无效");
  }
  return value;
}

function sleepDefault(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function githubConfig(env) {
  return {
    owner: env.GITHUB_OWNER || "Vickyyy-hub",
    repo: env.GITHUB_REPO || "-",
    workflow: env.GITHUB_WORKFLOW || "personal_growth.yml",
    ref: env.GITHUB_REF || "main",
  };
}

function githubHeaders(token) {
  return {
    Accept: "application/vnd.github+json",
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
    "User-Agent": "personal-growth-cloudflare-scheduler",
    "X-GitHub-Api-Version": "2022-11-28",
  };
}

function shouldRetryStatus(status) {
  return status === 429 || status >= 500;
}

export async function requestWithRetry(url, init, options = {}) {
  const fetchImpl = options.fetchImpl || fetch;
  const sleep = options.sleep || sleepDefault;
  const delays = options.retryDelaysMs || DEFAULT_RETRY_DELAYS_MS;
  let lastError;

  for (let attempt = 0; attempt <= delays.length; attempt += 1) {
    try {
      const response = await fetchImpl(url, init);
      if (response.ok) {
        return response;
      }
      const detail = (await response.text()).slice(0, 1_000);
      const error = new GitHubApiError(
        `GitHub API返回 ${response.status}`,
        response.status,
        detail,
      );
      if (!shouldRetryStatus(response.status) || attempt === delays.length) {
        throw error;
      }
      lastError = error;
    } catch (error) {
      if (error instanceof GitHubApiError && !shouldRetryStatus(error.status)) {
        throw error;
      }
      lastError = error;
      if (attempt === delays.length) {
        break;
      }
    }
    await sleep(delays[attempt]);
  }

  if (lastError instanceof GitHubApiError) {
    throw lastError;
  }
  throw new GitHubApiError(`GitHub API网络失败：${String(lastError)}`);
}

function workflowRunsUrl(env) {
  const config = githubConfig(env);
  return `https://api.github.com/repos/${encodeURIComponent(config.owner)}/${encodeURIComponent(config.repo)}/actions/workflows/${encodeURIComponent(config.workflow)}/runs?event=workflow_dispatch&branch=${encodeURIComponent(config.ref)}&per_page=30`;
}

function workflowDispatchUrl(env) {
  const config = githubConfig(env);
  return `https://api.github.com/repos/${encodeURIComponent(config.owner)}/${encodeURIComponent(config.repo)}/actions/workflows/${encodeURIComponent(config.workflow)}/dispatches`;
}

function runTitle(targetDate) {
  return `个人成长网站版 · ${targetDate} · cloudflare`;
}

export function selectMatchingRun(runs, targetDate) {
  const expectedTitle = runTitle(targetDate);
  return (runs || [])
    .filter((run) => run.display_title === expectedTitle)
    .sort((left, right) => String(right.created_at).localeCompare(String(left.created_at)))[0];
}

async function listWorkflowRuns(env, options) {
  const response = await requestWithRetry(
    workflowRunsUrl(env),
    { method: "GET", headers: githubHeaders(env.GITHUB_TOKEN) },
    options,
  );
  const payload = await response.json();
  return payload.workflow_runs || [];
}

async function dispatchWorkflow(env, targetDate, options) {
  const config = githubConfig(env);
  await requestWithRetry(
    workflowDispatchUrl(env),
    {
      method: "POST",
      headers: githubHeaders(env.GITHUB_TOKEN),
      body: JSON.stringify({
        ref: config.ref,
        inputs: {
          target_date: targetDate,
          sources: "all",
          trigger_source: "cloudflare",
        },
      }),
    },
    options,
  );
}

async function waitForCreatedRun(env, targetDate, notBefore, options) {
  const sleep = options.sleep || sleepDefault;
  const delays = options.verifyDelaysMs || DEFAULT_VERIFY_DELAYS_MS;
  for (const delay of delays) {
    await sleep(delay);
    const run = selectMatchingRun(await listWorkflowRuns(env, options), targetDate);
    if (run && new Date(run.created_at).getTime() >= notBefore - 2_000) {
      return run;
    }
  }
  return undefined;
}

export function tokenExpiryStatus(expiresAt, now = new Date()) {
  if (!expiresAt) {
    return { configured: false, warning: true, daysRemaining: null };
  }
  const expiry = new Date(`${expiresAt}T23:59:59Z`);
  if (Number.isNaN(expiry.getTime())) {
    return { configured: true, warning: true, daysRemaining: null };
  }
  const daysRemaining = Math.ceil((expiry.getTime() - now.getTime()) / 86_400_000);
  return { configured: true, warning: daysRemaining <= 30, daysRemaining };
}

export async function ensureWorkflowRun(env, targetDate, options = {}) {
  validateTargetDate(targetDate);
  if (!env.GITHUB_TOKEN) {
    throw new Error("缺少Cloudflare Secret: GITHUB_TOKEN");
  }

  const expiry = tokenExpiryStatus(env.GITHUB_TOKEN_EXPIRES_AT, options.now || new Date());
  if (expiry.warning) {
    console.warn(JSON.stringify({ event: "github_token_expiry_warning", ...expiry }));
  }

  const existing = selectMatchingRun(await listWorkflowRuns(env, options), targetDate);
  if (existing && existing.status !== "completed") {
    return { action: "skip_active", targetDate, run: existing, expiry };
  }
  if (existing?.conclusion === "success") {
    return { action: "skip_success", targetDate, run: existing, expiry };
  }

  for (let dispatchAttempt = 1; dispatchAttempt <= 2; dispatchAttempt += 1) {
    const dispatchedAt = Date.now();
    await dispatchWorkflow(env, targetDate, options);
    const created = await waitForCreatedRun(env, targetDate, dispatchedAt, options);
    if (created) {
      return {
        action: existing ? "resume" : "dispatch",
        targetDate,
        dispatchAttempt,
        run: created,
        expiry,
      };
    }
  }
  throw new Error(`GitHub已接收调度请求，但未查到 ${targetDate} 的新运行`);
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

function bearerToken(request) {
  const value = request.headers.get("Authorization") || "";
  return value.startsWith("Bearer ") ? value.slice(7) : "";
}

export default {
  async scheduled(controller, env, ctx) {
    const targetDate = previousShanghaiDate(new Date(controller.scheduledTime));
    ctx.waitUntil(
      ensureWorkflowRun(env, targetDate)
        .then((result) => console.log(JSON.stringify({ event: "scheduled_dispatch", ...result })))
        .catch((error) => {
          console.error(JSON.stringify({
            event: "scheduled_dispatch_failed",
            targetDate,
            message: error.message,
            status: error.status || null,
            detail: error.detail || null,
          }));
          throw error;
        }),
    );
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/health") {
      return jsonResponse({ ok: true, service: "personal-growth-scheduler" });
    }
    if (request.method !== "POST" || url.pathname !== "/trigger") {
      return jsonResponse({ error: "not_found" }, 404);
    }
    if (!env.SCHEDULER_TEST_SECRET || bearerToken(request) !== env.SCHEDULER_TEST_SECRET) {
      return jsonResponse({ error: "unauthorized" }, 401);
    }
    try {
      const body = await request.json().catch(() => ({}));
      const targetDate = validateTargetDate(
        body.target_date || previousShanghaiDate(new Date()),
      );
      const result = await ensureWorkflowRun(env, targetDate);
      return jsonResponse({ ok: true, ...result });
    } catch (error) {
      console.error(JSON.stringify({
        event: "manual_dispatch_failed",
        message: error.message,
        status: error.status || null,
        detail: error.detail || null,
      }));
      return jsonResponse({
        ok: false,
        error: error.message,
        status: error.status || null,
      }, 502);
    }
  },
};
