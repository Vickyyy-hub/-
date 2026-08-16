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
  const localDate = new Date(Date.UTC(Number(values.year), Number(values.month) - 1, Number(values.day)));
  localDate.setUTCDate(localDate.getUTCDate() - 1);
  return localDate.toISOString().slice(0, 10);
}

export function currentShanghaiDate(now = new Date()) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: SHANGHAI_TIME_ZONE, year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(now);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

function utcDateOffset(value, days) {
  const parsed = new Date(`${value}T00:00:00Z`);
  parsed.setUTCDate(parsed.getUTCDate() + days);
  return parsed.toISOString().slice(0, 10);
}

export function recentPreviousShanghaiDates(now = new Date(), days = 7) {
  if (!Number.isInteger(days) || days < 1 || days > 31) throw new Error("追补天数必须为1至31天");
  const previous = previousShanghaiDate(now);
  return Array.from({ length: days }, (_, index) => utcDateOffset(previous, index - days + 1));
}

export function validateTargetDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) throw new Error("目标日期必须为 YYYY-MM-DD");
  const parsed = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value) {
    throw new Error("目标日期无效");
  }
  return value;
}

const sleepDefault = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

function githubConfig(env) {
  return {
    owner: env.GITHUB_OWNER,
    repo: env.GITHUB_REPO,
    workflow: env.GITHUB_WORKFLOW || "personal_growth.yml",
    ref: env.GITHUB_REF || "main",
    runPrefix: env.GITHUB_RUN_PREFIX || "新闻情报日报",
  };
}

function headers(token) {
  return {
    Accept: "application/vnd.github+json",
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
    "User-Agent": "news-intelligence-cloudflare-scheduler",
    "X-GitHub-Api-Version": "2022-11-28",
  };
}

export async function requestWithRetry(url, init, options = {}) {
  const fetchImpl = options.fetchImpl || fetch;
  const sleep = options.sleep || sleepDefault;
  const delays = options.retryDelaysMs || DEFAULT_RETRY_DELAYS_MS;
  let lastError;
  for (let attempt = 0; attempt <= delays.length; attempt += 1) {
    try {
      const response = await fetchImpl(url, init);
      if (response.ok) return response;
      const detail = (await response.text()).slice(0, 1_000);
      const error = new GitHubApiError(`GitHub API返回 ${response.status}`, response.status, detail);
      if (!(response.status === 429 || response.status >= 500) || attempt === delays.length) throw error;
      lastError = error;
    } catch (error) {
      if (error instanceof GitHubApiError && !(error.status === 429 || error.status >= 500)) throw error;
      lastError = error;
      if (attempt === delays.length) break;
    }
    await sleep(delays[attempt]);
  }
  if (lastError instanceof GitHubApiError) throw lastError;
  throw new GitHubApiError(`GitHub API网络失败：${String(lastError)}`);
}

function apiUrls(env) {
  const config = githubConfig(env);
  const base = `https://api.github.com/repos/${encodeURIComponent(config.owner)}/${encodeURIComponent(config.repo)}`;
  const workflow = encodeURIComponent(config.workflow);
  return {
    runs: `${base}/actions/workflows/${workflow}/runs?event=workflow_dispatch&branch=${encodeURIComponent(config.ref)}&per_page=30`,
    allRuns: `${base}/actions/workflows/${workflow}/runs?branch=${encodeURIComponent(config.ref)}&per_page=100`,
    dispatch: `${base}/actions/workflows/${workflow}/dispatches`,
  };
}

function expectedTitle(env, targetDate, job = "all") {
  return `${githubConfig(env).runPrefix} · ${job} · ${targetDate} · cloudflare`;
}

export function selectMatchingRun(runs, targetDate, env, job = "all") {
  const title = expectedTitle(env, targetDate, job);
  return (runs || [])
    .filter((run) => run.display_title === title)
    .sort((left, right) => String(right.created_at).localeCompare(String(left.created_at)))[0];
}

async function listRuns(env, options) {
  const response = await requestWithRetry(apiUrls(env).runs, { method: "GET", headers: headers(env.GITHUB_TOKEN) }, options);
  return (await response.json()).workflow_runs || [];
}

async function listAllRuns(env, options) {
  const response = await requestWithRetry(apiUrls(env).allRuns, { method: "GET", headers: headers(env.GITHUB_TOKEN) }, options);
  return (await response.json()).workflow_runs || [];
}

async function waitForRun(env, targetDate, notBefore, options) {
  const job = options.job || "all";
  const sleep = options.sleep || sleepDefault;
  for (const delay of options.verifyDelaysMs || DEFAULT_VERIFY_DELAYS_MS) {
    await sleep(delay);
    const run = selectMatchingRun(await listRuns(env, options), targetDate, env, job);
    if (run && new Date(run.created_at).getTime() >= notBefore - 2_000) return run;
  }
  return undefined;
}

export async function ensureWorkflowRun(env, targetDate, options = {}) {
  validateTargetDate(targetDate);
  if (!env.GITHUB_TOKEN) throw new Error("缺少Cloudflare Secret: GITHUB_TOKEN");
  if (!env.GITHUB_OWNER || !env.GITHUB_REPO) throw new Error("缺少GITHUB_OWNER或GITHUB_REPO");
  const job = options.job || "all";
  const existing = selectMatchingRun(await listRuns(env, options), targetDate, env, job);
  if (existing && existing.status !== "completed") return { action: "skip_active", targetDate, run: existing };
  if (existing?.conclusion === "success") return { action: "skip_success", targetDate, run: existing };

  const config = githubConfig(env);
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    const dispatchedAt = Date.now();
    await requestWithRetry(apiUrls(env).dispatch, {
      method: "POST",
      headers: headers(env.GITHUB_TOKEN),
      body: JSON.stringify({
        ref: config.ref,
        inputs: { target_date: targetDate, job, trigger_source: "cloudflare" },
      }),
    }, options);
    const run = await waitForRun(env, targetDate, dispatchedAt, options);
    if (run) return { action: existing ? "resume" : "dispatch", targetDate, attempt, run };
  }
  throw new Error(`GitHub已接收调度请求，但未查到 ${targetDate} 的新运行`);
}

export async function ensureOldestMissingWorkflowRun(env, now = new Date(), options = {}) {
  if (!env.GITHUB_TOKEN) throw new Error("缺少Cloudflare Secret: GITHUB_TOKEN");
  if (!env.GITHUB_OWNER || !env.GITHUB_REPO) throw new Error("缺少GITHUB_OWNER或GITHUB_REPO");
  const prefix = `${githubConfig(env).runPrefix} · `;
  const runs = (await listAllRuns(env, options))
    .filter((run) => String(run.display_title || "").startsWith(prefix));
  const active = runs.find((run) => run.status !== "completed");
  if (active) return { action: "skip_active_global", run: active, lookbackDays: 7 };

  const startDate = env.CROSS_BORDER_START_DATE || "2026-08-16";
  const dates = recentPreviousShanghaiDates(now, 7).filter((date) => date >= startDate);
  const targetDate = dates.find((date) => {
    const title = expectedTitle(env, date, "demand");
    return !runs.some((run) => run.display_title === title && run.conclusion === "success");
  });
  if (!targetDate) return { action: "skip_all_recent_success", lookbackDays: 7, dates };
  return ensureWorkflowRun(env, targetDate, { ...options, job: "demand" });
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json; charset=utf-8" } });
}

export default {
  async scheduled(controller, env, ctx) {
    const scheduledAt = new Date(controller.scheduledTime);
    if (controller.cron === "47 4 * * *") {
      ctx.waitUntil(ensureOldestMissingWorkflowRun(env, scheduledAt));
      return;
    }
    const jobs = {
      "17 0 * * *": "policy",
      "47 1 * * *": "demand",
      "17 4 * * *": "policy",
      "17 8 * * *": "policy",
      "17 12 * * *": "policy",
    };
    const job = jobs[controller.cron];
    if (!job) throw new Error(`未配置的Cron：${controller.cron}`);
    ctx.waitUntil(ensureWorkflowRun(env, currentShanghaiDate(scheduledAt), { job }));
  },
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/health") return jsonResponse({ ok: true, service: "news-intelligence-scheduler" });
    if (request.method !== "POST" || url.pathname !== "/trigger") return jsonResponse({ error: "not_found" }, 404);
    const bearer = (request.headers.get("Authorization") || "").replace(/^Bearer\s+/, "");
    if (!env.SCHEDULER_TEST_SECRET || bearer !== env.SCHEDULER_TEST_SECRET) return jsonResponse({ error: "unauthorized" }, 401);
    try {
      const body = await request.json().catch(() => ({}));
      const targetDate = validateTargetDate(body.target_date || currentShanghaiDate());
      return jsonResponse({ ok: true, ...(await ensureWorkflowRun(env, targetDate, { job: body.job || "all" })) });
    } catch (error) {
      return jsonResponse({ ok: false, error: error.message, status: error.status || null }, 502);
    }
  },
};
