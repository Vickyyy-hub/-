import assert from "node:assert/strict";
import test from "node:test";

import worker, {
  GitHubApiError,
  ensureOldestMissingWorkflowRun,
  ensureWorkflowRun,
  jobsForCron,
  previousShanghaiDate,
  recentPreviousShanghaiDates,
  requestWithRetry,
  selectMatchingRun,
  shanghaiDate,
  tokenExpiryStatus,
  validateEventId,
  validateRecoveryRequest,
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

const recoveryEnv = {
  ...env,
  WEWE_RECOVERY_SECRET: "recovery-secret",
};

test("按上海时区计算前一自然日", () => {
  assert.equal(previousShanghaiDate(new Date("2026-08-13T00:07:00Z")), "2026-08-12");
  assert.equal(previousShanghaiDate(new Date("2026-01-01T16:01:00Z")), "2026-01-01");
});

test("按上海时区生成最近7个已结束自然日", () => {
  assert.deepEqual(recentPreviousShanghaiDates(new Date("2026-08-15T04:07:00Z")), [
    "2026-08-08", "2026-08-09", "2026-08-10", "2026-08-11",
    "2026-08-12", "2026-08-13", "2026-08-14",
  ]);
});

test("按上海时区计算当前自然日", () => {
  assert.equal(shanghaiDate(new Date("2026-08-13T12:30:00Z")), "2026-08-13");
  assert.equal(shanghaiDate(new Date("2026-08-13T16:01:00Z")), "2026-08-14");
});

test("严格校验目标日期", () => {
  assert.equal(validateTargetDate("2026-08-12"), "2026-08-12");
  assert.throws(() => validateTargetDate("2026-02-30"), /无效/);
  assert.throws(() => validateTargetDate("08-12-2026"), /YYYY-MM-DD/);
});

test("恢复请求只接受最近7天且事件ID安全", () => {
  const now = new Date("2026-08-14T03:00:00Z");
  assert.equal(validateEventId("login-20260814-abcd1234"), "login-20260814-abcd1234");
  assert.deepEqual(validateRecoveryRequest({
    event_id: "login-20260814-abcd1234",
    target_dates: ["2026-08-13", "2026-08-14"],
  }, now), {
    eventId: "login-20260814-abcd1234",
    targetDates: ["2026-08-13", "2026-08-14"],
  });
  assert.throws(() => validateEventId("bad event"), /无效/);
  assert.throws(() => validateRecoveryRequest({
    event_id: "login-20260814-abcd1234",
    target_dates: ["2026-08-07"],
  }, now), /只能位于/);
  assert.throws(() => validateRecoveryRequest({
    event_id: "login-20260814-abcd1234",
    target_dates: ["2026-08-14", "2026-08-14"],
  }, now), /不能重复/);
});

test("只匹配Cloudflare同日期运行", () => {
  const match = selectMatchingRun([
    { id: 1, display_title: "个人成长网站版 · 2026-08-12 · manual", created_at: "2026-08-13T00:00:00Z" },
    { id: 2, display_title: "个人成长网站版 · 2026-08-12 · cloudflare", created_at: "2026-08-13T00:01:00Z" },
  ], "2026-08-12");
  assert.equal(match.id, 2);
});

test("公众号和网站任务不会误匹配", () => {
  const runs = [
    { id: 1, display_title: "个人成长网站版 · 2026-08-13 · cloudflare", created_at: "2026-08-14T00:07:00Z" },
    { id: 2, display_title: "公众号每日资讯 · 2026-08-13 · cloudflare", created_at: "2026-08-14T00:07:00Z" },
  ];
  assert.equal(selectMatchingRun(runs, "2026-08-13", { pipeline: "wechat", phase: "daily" }).id, 2);
  assert.equal(selectMatchingRun(runs, "2026-08-13").id, 1);
});

test("三个Cron都按上海前一日同时运行两条流水线", () => {
  const morning = jobsForCron("37 0 * * *", new Date("2026-08-14T00:37:00Z"));
  assert.deepEqual(morning, [
    { task: { pipeline: "website", phase: "daily" }, targetDate: "2026-08-13" },
    { task: { pipeline: "wechat", phase: "daily" }, targetDate: "2026-08-13" },
  ]);
  assert.deepEqual(jobsForCron("7 0 * * *", new Date("2026-08-14T00:07:00Z")), morning);
  assert.deepEqual(jobsForCron("7 1 * * *", new Date("2026-08-14T01:07:00Z")), morning);
  assert.deepEqual(jobsForCron("7 4 * * *", new Date("2026-08-14T04:07:00Z")), [{
    task: { pipeline: "website", phase: "daily" },
    recoveryLookbackDays: 7,
    scheduledAt: new Date("2026-08-14T04:07:00Z"),
  }]);
});

test("跨境任务按上海当天分流且不与个人成长任务混淆", () => {
  const now = new Date("2026-08-16T00:17:00Z");
  assert.deepEqual(jobsForCron("17 0 * * *", now), [{
    task: { pipeline: "cross_border", phase: "daily", job: "policy" },
    targetDate: "2026-08-16",
  }]);
  assert.deepEqual(jobsForCron("47 1 * * *", now), [{
    task: { pipeline: "cross_border", phase: "daily", job: "demand" },
    targetDate: "2026-08-16",
  }]);
  const runs = [{
    id: 9,
    display_title: "跨境市场情报 · policy · 2026-08-16 · cloudflare",
    created_at: "2026-08-16T00:17:01Z",
  }];
  assert.equal(selectMatchingRun(
    runs,
    "2026-08-16",
    { pipeline: "cross_border", phase: "daily", job: "policy" },
  ).id, 9);
  assert.equal(selectMatchingRun(runs, "2026-08-16"), undefined);
});

test("合并Cron在免费额度内按时间分流跨境任务", () => {
  const cron = "17 0,1,2,3,4,8,12 * * *";
  assert.deepEqual(jobsForCron(cron, new Date("2026-08-16T01:17:00Z")), [{
    task: { pipeline: "cross_border", phase: "daily", job: "demand" },
    targetDate: "2026-08-16",
  }]);
  assert.deepEqual(jobsForCron(cron, new Date("2026-08-16T04:17:00Z")), [
    {
      task: { pipeline: "cross_border", phase: "daily", job: "policy" },
      targetDate: "2026-08-16",
    },
    {
      task: { pipeline: "cross_border", phase: "daily", job: "demand" },
      recoveryLookbackDays: 7,
      scheduledAt: new Date("2026-08-16T04:17:00Z"),
    },
  ]);
  assert.deepEqual(jobsForCron(cron, new Date("2026-08-17T02:17:00Z")), [{
    task: { pipeline: "cross_border", phase: "daily", job: "weekly" },
    targetDate: "2026-08-17",
  }]);
  assert.deepEqual(jobsForCron(cron, new Date("2026-08-18T02:17:00Z")), []);
  assert.deepEqual(jobsForCron(cron, new Date("2026-09-01T03:17:00Z")), [{
    task: { pipeline: "cross_border", phase: "daily", job: "profiles" },
    targetDate: "2026-09-01",
  }]);
});

test("午间追补在网站任务运行时不创建新任务", async () => {
  let postCount = 0;
  const fetchImpl = async (_url, init) => {
    if (init.method === "POST") postCount += 1;
    return jsonResponse({ workflow_runs: [{
      id: 40,
      display_title: "个人成长网站版 · 2026-08-14 · manual",
      status: "in_progress",
      conclusion: null,
      created_at: "2026-08-15T03:00:00Z",
    }] });
  };
  const result = await ensureOldestMissingWorkflowRun(
    env,
    new Date("2026-08-15T04:07:00Z"),
    noWaitOptions(fetchImpl),
  );
  assert.equal(result.action, "skip_active_global");
  assert.equal(postCount, 0);
});

test("午间追补选择最近7天中最早未成功日期", async () => {
  let postCount = 0;
  const dates = recentPreviousShanghaiDates(new Date("2026-08-15T04:07:00Z"));
  const initialRuns = dates
    .filter((date) => date !== "2026-08-10")
    .map((date, index) => ({
      id: 50 + index,
      display_title: `个人成长网站版 · ${date} · cloudflare`,
      status: "completed",
      conclusion: "success",
      created_at: `2026-08-${String(index + 8).padStart(2, "0")}T04:00:00Z`,
    }));
  const fetchImpl = async (url, init) => {
    if (init.method === "POST") {
      postCount += 1;
      return new Response(null, { status: 204 });
    }
    if (postCount && url.includes("event=workflow_dispatch")) {
      return jsonResponse({ workflow_runs: [{
        id: 80,
        display_title: "个人成长网站版 · 2026-08-10 · cloudflare",
        status: "queued",
        conclusion: null,
        created_at: new Date().toISOString(),
      }] });
    }
    return jsonResponse({ workflow_runs: initialRuns });
  };
  const result = await ensureOldestMissingWorkflowRun(
    env,
    new Date("2026-08-15T04:07:00Z"),
    noWaitOptions(fetchImpl),
  );
  assert.equal(result.action, "dispatch");
  assert.equal(result.targetDate, "2026-08-10");
  assert.equal(postCount, 1);
});

test("午间追补在最近7天全部成功时跳过", async () => {
  const runs = recentPreviousShanghaiDates(new Date("2026-08-15T04:07:00Z")).map((date) => ({
    display_title: `个人成长网站版 · ${date} · cloudflare`,
    status: "completed",
    conclusion: "success",
    created_at: "2026-08-15T03:00:00Z",
  }));
  const result = await ensureOldestMissingWorkflowRun(
    env,
    new Date("2026-08-15T04:07:00Z"),
    noWaitOptions(async () => jsonResponse({ workflow_runs: runs })),
  );
  assert.equal(result.action, "skip_all_recent_success");
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

test("公众号任务使用独立工作流", async () => {
  const requests = [];
  const fetchImpl = async (url, init) => {
    requests.push({ url, init });
    if (init.method === "POST") {
      return new Response(null, { status: 204 });
    }
    const created = requests.some((request) => request.init.method === "POST");
    return jsonResponse({ workflow_runs: created ? [{
      id: 30,
      display_title: "公众号每日资讯 · 2026-08-13 · cloudflare",
      status: "queued",
      conclusion: null,
      created_at: new Date().toISOString(),
    }] : [] });
  };
  const options = {
    ...noWaitOptions(fetchImpl),
    task: { pipeline: "wechat", phase: "daily" },
  };
  const result = await ensureWorkflowRun(env, "2026-08-13", options);
  assert.equal(result.pipeline, "wechat");
  assert.equal(result.phase, "daily");
  const dispatch = requests.find((request) => request.init.method === "POST");
  assert.match(dispatch.url, /wechat_growth\.yml\/dispatches$/);
  assert.deepEqual(JSON.parse(dispatch.init.body).inputs, {
    target_date: "2026-08-13",
    sources: "all",
    trigger_source: "cloudflare",
  });
});

test("扫码恢复使用事件ID强制触发且与晨间运行隔离", async () => {
  const requests = [];
  const task = { pipeline: "wechat", phase: "login_recovery", eventId: "login-20260814-abcd1234" };
  const fetchImpl = async (_url, init) => {
    requests.push(init);
    if (init.method === "POST") return new Response(null, { status: 204 });
    const created = requests.some((item) => item.method === "POST");
    return jsonResponse({ workflow_runs: created ? [{
      id: 31,
      display_title: "公众号每日资讯 · 2026-08-14 · wewe-login-login-20260814-abcd1234",
      status: "queued",
      conclusion: null,
      created_at: new Date().toISOString(),
    }] : [{
      id: 30,
      display_title: "公众号每日资讯 · 2026-08-14 · cloudflare",
      status: "completed",
      conclusion: "success",
      created_at: new Date().toISOString(),
    }] });
  };
  const result = await ensureWorkflowRun(env, "2026-08-14", {
    ...noWaitOptions(fetchImpl),
    task,
  });
  assert.equal(result.action, "dispatch");
  const body = JSON.parse(requests.find((item) => item.method === "POST").body);
  assert.deepEqual(body.inputs, {
    target_date: "2026-08-14",
    sources: "all",
    trigger_source: "wewe-login-login-20260814-abcd1234",
    force: "true",
  });
});

test("同一扫码事件和日期不会重复触发", async () => {
  const task = { pipeline: "wechat", phase: "login_recovery", eventId: "login-20260814-abcd1234" };
  const fetchImpl = async () => jsonResponse({ workflow_runs: [{
    id: 32,
    display_title: "公众号每日资讯 · 2026-08-14 · wewe-login-login-20260814-abcd1234",
    status: "completed",
    conclusion: "success",
    created_at: new Date().toISOString(),
  }] });
  const result = await ensureWorkflowRun(env, "2026-08-14", {
    ...noWaitOptions(fetchImpl),
    task,
  });
  assert.equal(result.action, "skip_success");
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

test("扫码恢复入口独立鉴权并限制任务类型", async () => {
  const unauthorized = await worker.fetch(new Request("https://scheduler.test/wewe-recovered", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ event_id: "login-20260814-abcd1234", target_dates: ["2026-08-14"] }),
  }), recoveryEnv);
  assert.equal(unauthorized.status, 401);

  const malformed = await worker.fetch(new Request("https://scheduler.test/wewe-recovered", {
    method: "POST",
    headers: {
      Authorization: "Bearer recovery-secret",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ pipeline: "website", event_id: "bad", target_dates: [] }),
  }), recoveryEnv);
  assert.equal(malformed.status, 400);
});
