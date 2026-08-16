import assert from "node:assert/strict";
import test from "node:test";
import worker, {
  ensureOldestMissingWorkflowRun,
  ensureWorkflowRun,
  currentShanghaiDate,
  previousShanghaiDate,
  recentPreviousShanghaiDates,
  requestWithRetry,
} from "../src/worker.mjs";

const response = (payload, status = 200) => new Response(JSON.stringify(payload), { status });
const env = {
  GITHUB_TOKEN: "test-token", GITHUB_OWNER: "owner", GITHUB_REPO: "repo",
  GITHUB_WORKFLOW: "personal_growth.yml", GITHUB_REF: "main", GITHUB_RUN_PREFIX: "测试日报",
  CROSS_BORDER_START_DATE: "2026-08-08",
};
const options = (fetchImpl) => ({ fetchImpl, sleep: async () => {}, retryDelaysMs: [], verifyDelaysMs: [0] });

test("uses the previous Shanghai natural date", () => {
  assert.equal(previousShanghaiDate(new Date("2026-08-14T00:07:00Z")), "2026-08-13");
  assert.deepEqual(recentPreviousShanghaiDates(new Date("2026-08-15T04:07:00Z")), [
    "2026-08-08", "2026-08-09", "2026-08-10", "2026-08-11",
    "2026-08-12", "2026-08-13", "2026-08-14",
  ]);
  assert.equal(currentShanghaiDate(new Date("2026-08-14T00:07:00Z")), "2026-08-14");
});

test("skips a successful or active run", async () => {
  for (const [status, conclusion, expected] of [["completed", "success", "skip_success"], ["in_progress", null, "skip_active"]]) {
    const result = await ensureWorkflowRun(env, "2026-08-13", options(async () => response({ workflow_runs: [{
      display_title: "测试日报 · all · 2026-08-13 · cloudflare", status, conclusion, created_at: "2026-08-14T00:07:01Z",
    }] })));
    assert.equal(result.action, expected);
  }
});

test("dispatches missing run and confirms creation", async () => {
  let dispatched = false;
  const fetchImpl = async (_url, init) => {
    if (init.method === "POST") { dispatched = true; return new Response(null, { status: 204 }); }
    return response({ workflow_runs: dispatched ? [{
      display_title: "测试日报 · all · 2026-08-13 · cloudflare", status: "queued", conclusion: null, created_at: new Date().toISOString(),
    }] : [] });
  };
  assert.equal((await ensureWorkflowRun(env, "2026-08-13", options(fetchImpl))).action, "dispatch");
});

test("catch-up skips while a workflow is active", async () => {
  let posts = 0;
  const fetchImpl = async (_url, init) => {
    if (init.method === "POST") posts += 1;
    return response({ workflow_runs: [{
      display_title: "测试日报 · all · 2026-08-14 · manual",
      status: "in_progress",
      conclusion: null,
      created_at: "2026-08-15T03:00:00Z",
    }] });
  };
  const result = await ensureOldestMissingWorkflowRun(
    env,
    new Date("2026-08-15T04:07:00Z"),
    options(fetchImpl),
  );
  assert.equal(result.action, "skip_active_global");
  assert.equal(posts, 0);
});

test("catch-up dispatches the oldest missing date", async () => {
  let posts = 0;
  const dates = recentPreviousShanghaiDates(new Date("2026-08-15T04:07:00Z"));
  const completeRuns = dates.filter((date) => date !== "2026-08-10").map((date) => ({
    display_title: `测试日报 · demand · ${date} · cloudflare`,
    status: "completed",
    conclusion: "success",
    created_at: "2026-08-15T03:00:00Z",
  }));
  const fetchImpl = async (url, init) => {
    if (init.method === "POST") {
      posts += 1;
      return new Response(null, { status: 204 });
    }
    if (posts && url.includes("event=workflow_dispatch")) {
      return response({ workflow_runs: [{
        display_title: "测试日报 · demand · 2026-08-10 · cloudflare",
        status: "queued",
        conclusion: null,
        created_at: new Date().toISOString(),
      }] });
    }
    return response({ workflow_runs: completeRuns });
  };
  const result = await ensureOldestMissingWorkflowRun(
    env,
    new Date("2026-08-15T04:07:00Z"),
    options(fetchImpl),
  );
  assert.equal(result.action, "dispatch");
  assert.equal(result.targetDate, "2026-08-10");
  assert.equal(posts, 1);
});

test("catch-up skips when all recent dates succeeded", async () => {
  const runs = recentPreviousShanghaiDates(new Date("2026-08-15T04:07:00Z")).map((date) => ({
    display_title: `测试日报 · demand · ${date} · cloudflare`,
    status: "completed",
    conclusion: "success",
    created_at: "2026-08-15T03:00:00Z",
  }));
  const result = await ensureOldestMissingWorkflowRun(
    env,
    new Date("2026-08-15T04:07:00Z"),
    options(async () => response({ workflow_runs: runs })),
  );
  assert.equal(result.action, "skip_all_recent_success");
});

test("retries 429 and 5xx but not 401", async () => {
  let calls = 0;
  await requestWithRetry("https://example.test", {}, { fetchImpl: async () => ++calls < 3 ? new Response("busy", { status: calls === 1 ? 429 : 503 }) : new Response("ok"), sleep: async () => {}, retryDelaysMs: [0, 0] });
  assert.equal(calls, 3);
  calls = 0;
  await assert.rejects(requestWithRetry("https://example.test", {}, { fetchImpl: async () => { calls += 1; return new Response("no", { status: 401 }); }, sleep: async () => {}, retryDelaysMs: [0, 0] }));
  assert.equal(calls, 1);
});

test("protects manual trigger and exposes health", async () => {
  const protectedEnv = { ...env, SCHEDULER_TEST_SECRET: "secret" };
  assert.equal((await worker.fetch(new Request("https://x.test/trigger", { method: "POST" }), protectedEnv)).status, 401);
  assert.equal((await worker.fetch(new Request("https://x.test/health"), protectedEnv)).status, 200);
});
