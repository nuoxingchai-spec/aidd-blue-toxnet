const test = require("node:test");
const assert = require("node:assert/strict");

const core = require("../toxicity-core.js");
const originalFetch = global.fetch;
test.afterEach(() => { global.fetch = originalFetch; });

function report() {
  return { input: "CCO", canonicalSmiles: "CCO", structureId: "sha256:test", descriptors: {}, alerts: [], moleculeSvg: "<svg></svg>", endpoints: ["herg", "dili", "ames", "clintox"].map((id) => ({ id, score: 0.2 })), overall: 0.2, tier: { id: "low" }, provenance: { contractVersion: "toxilens.api.v2" } };
}
function response(payload, ok = true) { return { ok, json: async () => payload }; }

test("model request sends selected provider to server and validates contract", async () => {
  let received;
  global.fetch = async (url, options) => { received = { url, ...options }; return response(report()); };
  const result = await core.analyzeSmiles(" CCO ", { provider: "qwen" });
  assert.equal(received.url, "/api/analyze");
  assert.deepEqual(JSON.parse(received.body), { smiles: "CCO", mode: "model", provider: "qwen" });
  assert.equal(result.canonicalSmiles, "CCO");
});

test("rules baseline requires explicit selection", async () => {
  let body;
  global.fetch = async (_, options) => { body = JSON.parse(options.body); return response(report()); };
  await core.analyzeSmiles("CCO", { mode: "rules" });
  assert.equal(body.mode, "rules");
});

test("unconfigured model is surfaced without fallback", async () => {
  let calls = 0;
  global.fetch = async () => { calls++; return response({ error: { code: "MODEL_NOT_CONFIGURED", message: "模型尚未配置", requestId: "test-id" } }, false); };
  await assert.rejects(core.analyzeSmiles("CCO"), (error) => error.code === "MODEL_NOT_CONFIGURED" && error.requestId === "test-id");
  assert.equal(calls, 1);
});

test("duplicate or missing endpoints and invalid scores are rejected", async () => {
  for (const mutate of [
    (r) => { r.endpoints[1].id = "herg"; },
    (r) => { r.endpoints.pop(); },
    (r) => { r.endpoints[0].score = true; },
    (r) => { r.endpoints[0].score = NaN; },
    (r) => { r.endpoints[0].score = 1.5; },
    (r) => { r.provenance.contractVersion = "unknown"; },
  ]) {
    const invalid = report(); mutate(invalid);
    global.fetch = async () => response(invalid);
    await assert.rejects(core.analyzeSmiles("CCO"), { code: "INVALID_REPORT" });
  }
});

test("reset abort signal reaches network request", async () => {
  const controller = new AbortController();
  global.fetch = (_, { signal }) => new Promise((_, reject) => signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true }));
  const pending = core.analyzeSmiles("CCO", { signal: controller.signal });
  controller.abort();
  await assert.rejects(pending, { name: "AbortError" });
});

test("empty input does not issue a network request", async () => {
  global.fetch = async () => { throw new Error("must not call fetch"); };
  await assert.rejects(core.analyzeSmiles(""), { code: "INVALID_SMILES" });
});

test("health checks use same-origin API", async () => {
  global.fetch = async (url) => { assert.equal(url, "/api/health"); return response({ status: "ok", model: { configured: false } }); };
  assert.equal((await core.getHealth()).status, "ok");
});

test("provider configuration uses same-origin GET and JSON POST", async () => {
  const received = [];
  global.fetch = async (url, options) => { received.push({ url, ...options }); return response({ hasApiKey: true }); };
  await core.getProviderConfig("qwen");
  await core.saveProviderConfig("qwen", { modelName: "test-model", apiKey: "test-secret" });
  assert.equal(received[0].url, "/api/providers/qwen/config");
  assert.equal(received[0].method, "GET");
  assert.equal(received[1].method, "POST");
  assert.equal(received[1].credentials, "same-origin");
  assert.equal(received[1].headers["Content-Type"], "application/json");
  assert.equal(JSON.parse(received[1].body).apiKey, "test-secret");
});

test("configuration rejects unsafe provider paths before any request", async () => {
  global.fetch = async () => { throw new Error("must not call fetch"); };
  await assert.rejects(core.getProviderConfig("../health"), { code: "INVALID_PROVIDER" });
});
