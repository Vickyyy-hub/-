import assert from "node:assert/strict";
import test from "node:test";

import worker, {
  GitHubApiError,
  ensureWorkflowRun,
  previousShanghaiDate,
  requestWithRetry,
  selectMatchingRun,
  tokenExpiryStatus,
  validateTargetDate,
} from "../src/worker.mjs";

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function noWaitOptions(fetchImpl) {
  return {
    fetchImpl,
    sleep: async () => {},
    retryDelaysMs: [],
    verifyDelaysMs: [0],
    now: new Date("2026-08-13T00:00:00Z"),
  };
}

const env = {
  GITHUB_TOKEN: "test-token",
  GITHUB_TOKEN_EXPIRES_AT: "2027-08-13",
  GITHUB_OWNER: "Vickyyy-hub",
  GITHUB_REPO: "-",
  GITHUB_WORKFLOW: "personal_growth.yml",
  GITHUB_REF: "main",
};

test("按上海时区计算前一自然日", () => {
  assert.equal(previousShanghaiDate(new Date("2026-08-13T00:07:00Z")), "2026-08-12");
  assert.equal(previousShanghaiDate(new Date("2026-01-01T16:01:00Z")), "2026-01-01");
});

test("严格校验目标日期", () => {
  assert.equal(validateTargetDate("2026-08-12"), "2026-08-12");
  assert.throws(() => validateTargetDate("2026-02-30"), /无效/);
  assert.throws(() => validateTargetDate("08-12-2026"), /YYYY-MM-DD/);
});

test("只匹配Cloudflare同日期运行", () => {
  const match = selectMatchingRun([
    { id: 1, display_title: "个人成长网站版 · 2026-08-12 · manual", created_at: "2026-08-13T00:00:00Z" },
    { id: 2, display_title: "个人成长网站版 · 2026-08-12 · cloudflare", created_at: "2026-08-13T00:01:00Z" },
  ], "2026-08-12");
  assert.equal(match.id, 2);
});

test("已成功日期不重复触发", async () => {
  let calls = 0;
  const fetchImpl = async () => {
    calls += 1;
    return jsonResponse({ workflow_runs: [{
      id: 10,
      display_title: "个人成长网站版 · 2026-08-12 · cloudflare",
      status: "completed",
      conclusion: "success",
      created_at: "2026-08-13T00:07:01Z",
    }] });
  };
  const result = await ensureWorkflowRun(env, "2026-08-12", noWaitOptions(fetchImpl));
  assert.equal(result.action, "skip_success");
  assert.equal(calls, 1);
});

test("排队或运行中日期不重复触发", async () => {
  const fetchImpl = async () => jsonResponse({ workflow_runs: [{
    id: 11,
    display_title: "个人成长网站版 · 2026-08-12 · cloudflare",
    status: "in_progress",
    conclusion: null,
    created_at: "2026-08-13T00:07:01Z",
  }] });
  const result = await ensureWorkflowRun(env, "2026-08-12", noWaitOptions(fetchImpl));
  assert.equal(result.action, "skip_active");
});

test("不存在运行时发起调度并确认新运行", async () => {
  const requests = [];
  const fetchImpl = async (url, init) => {
    requests.push({ url, init });
    if (init.method === "POST") {
      return new Response(null, { status: 204 });
    }
    const created = requests.some((request) => request.init.method === "POST");
    return jsonResponse({ workflow_runs: created ? [{
      id: 12,
      display_title: "个人成长网站版 · 2026-08-12 · cloudflare",
      status: "queued",
      conclusion: null,
      created_at: new Date().toISOString(),
    }] : [] });
  };
  const result = await ensureWorkflowRun(env, "2026-08-12", noWaitOptions(fetchImpl));
  assert.equal(result.action, "dispatch");
  const dispatch = requests.find((request) => request.init.method === "POST");
  const body = JSON.parse(dispatch.init.body);
  assert.deepEqual(body.inputs, {
    target_date: "2026-08-12",
    sources: "all",
    trigger_source: "cloudflare",
  });
});

test("失败运行会触发断点恢复", async () => {
  let postCount = 0;
  let listCount = 0;
  const fetchImpl = async (_url, init) => {
    if (init.method === "POST") {
      postCount += 1;
      return new Response(null, { status: 204 });
    }
    listCount += 1;
    return jsonResponse({ workflow_runs: [{
      id: listCount === 1 ? 20 : 21,
      display_title: "个人成长网站版 · 2026-08-12 · cloudflare",
      status: listCount === 1 ? "completed" : "queued",
      conclusion: listCount === 1 ? "failure" : null,
      created_at: listCount === 1 ? "2026-08-13T00:07:00Z" : new Date().toISOString(),
    }] });
  };
  const result = await ensureWorkflowRun(env, "2026-08-12", noWaitOptions(fetchImpl));
  assert.equal(result.action, "resume");
  assert.equal(postCount, 1);
});

test("429和5xx会重试，401不重试", async () => {
  let attempts = 0;
  const eventualSuccess = await requestWithRetry("https://example.test", {}, {
    fetchImpl: async () => {
      attempts += 1;
      return attempts < 3 ? new Response("busy", { status: attempts === 1 ? 429 : 503 }) : new Response("ok");
    },
    sleep: async () => {},
    retryDelaysMs: [0, 0, 0],
  });
  assert.equal(eventualSuccess.status, 200);
  assert.equal(attempts, 3);

  attempts = 0;
  await assert.rejects(
    requestWithRetry("https://example.test", {}, {
      fetchImpl: async () => {
        attempts += 1;
        return new Response("bad token", { status: 401 });
      },
      sleep: async () => {},
      retryDelaysMs: [0, 0, 0],
    }),
    (error) => error instanceof GitHubApiError && error.status === 401,
  );
  assert.equal(attempts, 1);
});

test("令牌到期前30天开始告警", () => {
  assert.equal(tokenExpiryStatus("2026-09-01", new Date("2026-08-13T00:00:00Z")).warning, true);
  assert.equal(tokenExpiryStatus("2027-08-13", new Date("2026-08-13T00:00:00Z")).warning, false);
});

test("手动入口必须使用保护密钥", async () => {
  const protectedEnv = { ...env, SCHEDULER_TEST_SECRET: "manual-secret" };
  const unauthorized = await worker.fetch(
    new Request("https://scheduler.test/trigger", { method: "POST" }),
    protectedEnv,
  );
  assert.equal(unauthorized.status, 401);

  const health = await worker.fetch(new Request("https://scheduler.test/health"), protectedEnv);
  assert.equal(health.status, 200);
  assert.deepEqual(await health.json(), { ok: true, service: "personal-growth-scheduler" });
});
