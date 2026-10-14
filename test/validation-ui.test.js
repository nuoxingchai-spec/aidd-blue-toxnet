"use strict";
// Every service response in this file is a labeled software fixture. No model,
// dataset, calibration or scientific measurement is executed by these tests.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const Core = require("../validation-core.js");
const source = fs.readFileSync(path.join(__dirname, "..", "validation-app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "..", "validation.html"), "utf8");
const flush = () => new Promise((resolve) => setImmediate(resolve));
function deferred() { let resolve; const promise = new Promise((accept) => { resolve = accept; }); return { promise, resolve }; }

class Element {
  constructor(tag, document) { this.tagName = tag.toUpperCase(); this.ownerDocument = document; this.children = []; this.events = {}; this.dataset = {}; this.attributes = {}; this.parentElement = null; this.id = ""; this.hidden = false; this.disabled = false; this.checked = false; this.open = false; this._value = ""; this._text = ""; this.className = ""; }
  get value() { return this._value; } set value(value) { this._value = String(value); }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  set textContent(value) { this._text = String(value); this.children = []; }
  append(...children) { for (const child of children) { const first = !this.children.length; this.children.push(child); child.parentElement = this; if (this.tagName === "SELECT" && first) this.value = child.value; } }
  replaceChildren(...children) { this._text = ""; this.children.forEach((child) => { child.parentElement = null; }); this.children = []; this.append(...children); }
  remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter((child) => child !== this); this.parentElement = null; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, callback) { (this.events[name] ||= []).push(callback); }
  fire(name, extra = {}) { return Promise.all((this.events[name] || []).map((callback) => callback({ preventDefault() {}, target: this, ...extra }))); }
  click() { return this.fire("click"); }
}
const catalog = () => ({ schema: Core.SCHEMA, formalAvailability: false, testFixture: true, datasets: [{ id: "fixture-data", name: "软件夹具数据清单", version: "software-only", qualification: "not-qualified", rightsStatus: "not-reviewed", missingDependencies: ["真实独立标签缺失"], audit: null }], models: Core.MODEL_IDS.map((id) => ({ id, available: false, reason: "软件夹具，未加载模型" })), protocol: { id: "fixture-protocol", status: "draft" }, negativeTransfer: { status: "not-evaluated" }, limitations: [] });
const run = (changes = {}) => ({ schema: Core.SCHEMA, runId: "fixture-run", kind: "audit", status: "completed", datasetId: "fixture-data", testFixture: true, report: { datasetAudit: { status: "not-qualified", reason: "真实独立标签缺失" }, fairComparison: null, calibration: null, applicability: null, negativeTransfer: null, limitations: [] }, error: null, ...changes });
const response = (value, status = 200) => ({ ok: status >= 200 && status < 300, status, async json() { return value; } });

async function setup(options = {}) {
  const elements = {}, requests = [], blobs = [], timers = new Map(), windowEvents = {};
  const store = new Map(Object.entries(options.storage || {}));
  const document = { getElementById(id) { assert.ok(elements[id], `Missing validation element ${id}`); return elements[id]; }, createElement(tag) { return new Element(tag, document); }, createElementNS(_, tag) { return new Element(tag, document); } };
  document.body = new Element("body", document);
  for (const match of html.matchAll(/<([\w-]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const [, tag, attrs, id] = match, element = new Element(tag, document); element.id = id;
    element.hidden = /\bhidden(?:\s|$)/.test(attrs); element.disabled = /\bdisabled(?:\s|$)/.test(attrs); element.checked = /\bchecked(?:\s|$)/.test(attrs);
    if (tag === "select") { const option = new Element("option", document); option.value = ""; element.append(option); }
    elements[id] = element; document.body.append(element);
  }
  let timerId = 0;
  const window = { ValidationCore: Core, addEventListener(name, callback) { (windowEvents[name] ||= []).push(callback); }, fire(name, extra = {}) { return Promise.all((windowEvents[name] || []).map((callback) => callback(extra))); } };
  const context = { window, document, AbortController, AbortSignal, Blob,
    setTimeout(callback, delay) { const id = ++timerId; timers.set(id, { callback, delay }); return id; }, clearTimeout(id) { timers.delete(id); },
    localStorage: { getItem(key) { return store.get(key) || null; }, setItem(key, value) { store.set(key, value); } },
    URL: { createObjectURL(blob) { blobs.push(blob); return "blob:software-test"; }, revokeObjectURL() {} },
    async fetch(url, opts = {}) {
      const request = { url, method: opts.method || "GET", body: opts.body ? JSON.parse(opts.body) : null, signal: opts.signal }; requests.push(request);
      if (options.fetch) { const result = await options.fetch(request); if (result !== undefined) return result; }
      if (url.endsWith("/datasets")) return response(catalog());
      if (url === "/api/research/validation/runs") return response({ schema: Core.SCHEMA, runs: [] });
      if (url.endsWith("/audit") || url.endsWith("/evaluate")) return response({ schema: Core.SCHEMA, runId: "fixture-run", status: "pending" }, 202);
      if (url.endsWith("/runs/fixture-run")) return response(run({ status: request.method === "DELETE" ? "cancelled" : "completed" }));
      if (url.endsWith("/reports/fixture-run")) return response(run());
      throw new Error(`Unstubbed validation fixture ${request.method} ${url}`);
    } };
  vm.runInNewContext(source, context, { filename: "validation-app.js" }); await flush(); await flush();
  return { elements, requests, blobs, timers, window, async pollOnce() { const timer = [...timers.entries()].find(([, item]) => item.delay === 1000); assert.ok(timer, "Expected validation poll"); timers.delete(timer[0]); await timer[1].callback(); await flush(); } };
}

test("validation software fixture: missing independent data is not-qualified with no invented metrics or curve", async () => {
  const ui = await setup();
  assert.equal(ui.elements.validationFixture.hidden, false);
  assert.match(ui.elements.validationQualification.textContent, /尚不具备独立评价资格/);
  assert.match(ui.elements.validationDependencies.textContent, /真实独立标签缺失/);
  assert.match(ui.elements.validationMetricRows.textContent, /未计算/);
  assert.match(ui.elements.validationReliability.textContent, /未计算/);
  assert.doesNotMatch(ui.elements.validationMetricRows.textContent, /0\.0000/);
  assert.ok(ui.requests.every((item) => item.method === "GET"));
});

test("validation software fixture: audit/evaluate post only registered IDs and fixed protocol settings", async () => {
  const ui = await setup();
  await ui.elements.validationAudit.fire("click"); await flush();
  assert.deepEqual(ui.requests.find((item) => item.url.endsWith("/audit")).body, { datasetId: "fixture-data" });
  await ui.elements.validationForm.fire("submit"); await flush();
  const payload = ui.requests.find((item) => item.url.endsWith("/evaluate")).body;
  assert.deepEqual(payload, { datasetId: "fixture-data", protocolId: "fixture-protocol", modelIds: ["A4", "B16", "C_Morgan"], seed: 42 });
});

test("validation software fixture: server 409 retains explicit qualification reason without fallback scores", async () => {
  const ui = await setup({ fetch(request) { if (request.url.endsWith("/evaluate")) return response({ error: { code: "NOT_QUALIFIED", message: "缺少未曝光的独立实验标签" } }, 409); } });
  await ui.elements.validationForm.fire("submit"); await flush();
  assert.match(ui.elements.validationError.textContent, /独立实验标签.*409/);
  assert.equal(ui.elements.validationExport.disabled, true);
  assert.match(ui.elements.validationMetricRows.textContent, /未计算/);
});

test("validation software fixture: late accepted run is cancelled after configuration changes", async () => {
  const pending = deferred();
  const ui = await setup({ fetch(request) { if (request.url.endsWith("/audit")) return pending.promise; } });
  const started = ui.elements.validationAudit.fire("click");
  ui.elements.validationModelB16.checked = false; await ui.elements.validationModelB16.fire("change");
  pending.resolve(response({ schema: Core.SCHEMA, runId: "fixture-run", status: "pending" }, 202)); await started; await flush();
  assert.equal(ui.elements.validationStale.hidden, false);
  assert.ok(ui.requests.some((item) => item.method === "DELETE" && item.url.endsWith("/fixture-run")));
  assert.equal(ui.elements.validationExport.disabled, true);
});

test("validation software fixture: history restore performs reads only and report export redacts private content", async () => {
  const historical = run(); historical.report.rawRows = ["private-data"]; historical.report.apiKey = "private-key";
  const ui = await setup({ storage: { "aidd.validation.lastRunId": "fixture-run" }, fetch(request) { if (request.url.endsWith("/reports/fixture-run")) return response(historical); } });
  assert.ok(ui.requests.every((item) => item.method === "GET"));
  assert.equal(ui.elements.validationExport.disabled, false);
  await ui.elements.validationExport.fire("click");
  assert.equal(ui.blobs.length, 1);
  const packet = JSON.parse(await ui.blobs[0].text());
  assert.equal(packet.schema, Core.SCHEMA); assert.equal(packet.runId, "fixture-run");
  assert.doesNotMatch(JSON.stringify(packet), /private-data|private-key/);
  ui.elements.validationModelA4.checked = false; await ui.elements.validationModelA4.fire("change");
  await ui.elements.validationExport.fire("click"); assert.equal(ui.blobs.length, 1);
});

test("validation software fixture: programmatic configuration edits cannot export an unchanged old run", async () => {
  const ui = await setup({ storage: { "aidd.validation.lastRunId": "fixture-run" } });
  ui.elements.validationModelA4.checked = false;
  await ui.elements.validationExport.fire("click");
  assert.equal(ui.blobs.length, 0); assert.equal(ui.elements.validationStale.hidden, false);
  assert.match(ui.elements.validationError.textContent, /配置已修改/);
});

test("validation software fixture: repeated cancellation is idempotent and preserves completed aggregate results", async () => {
  const cancellation = deferred();
  const ui = await setup({ fetch(request) { if (request.url.endsWith("/runs/fixture-run")) return request.method === "DELETE" ? cancellation.promise : response(run({ status: "running" })); } });
  await ui.elements.validationAudit.fire("click"); await flush();
  const first = ui.elements.validationCancel.fire("click"); await ui.elements.validationCancel.fire("click");
  assert.equal(ui.requests.filter((item) => item.method === "DELETE").length, 1);
  cancellation.resolve(response(run({ status: "cancelled" }))); await first; await flush();
  assert.match(ui.elements.validationStatus.textContent, /已取消/);
  assert.equal(ui.elements.validationExport.disabled, true);
});

test("validation software fixture: unchanged polling preserves metric nodes and pagehide aborts the current read", async () => {
  const progress = run({ status: "running" });
  const ui = await setup({ fetch(request) { if (request.url.endsWith("/runs/fixture-run")) return response(progress); } });
  await ui.elements.validationAudit.fire("click"); await flush();
  const row = ui.elements.validationMetricRows.children[0];
  await ui.pollOnce();
  assert.equal(ui.elements.validationMetricRows.children[0], row);
  const get = ui.requests.findLast((request) => request.url.endsWith("/runs/fixture-run"));
  await ui.window.fire("pagehide"); assert.equal(get.signal.aborted, true);
  assert.equal(ui.requests.filter((item) => item.method === "DELETE").length, 0);
});

test("validation routes have independent source scripts and preserve both original workbench entry points", () => {
  assert.match(html, /href="\/compare"/); assert.match(html, /href="\/"/);
  assert.match(html, /validation-core\.js/); assert.match(html, /validation-app\.js/);
  assert.doesNotMatch(source, /\/api\/analyze|\/api\/research\/compare\/runs|\/api\/chat/);
});

test("validation production catalog flags disable unregistered candidates and keep missing-data reasons", async () => {
  const current = catalog();
  Object.assign(current.datasets[0], { qualification: "not-independently-qualified", auditAvailable: false, evaluationAvailable: false });
  const ui = await setup({ fetch(request) { if (request.url.endsWith("/datasets")) return response(current); } });
  assert.equal(ui.elements.validationAudit.disabled, true); assert.equal(ui.elements.validationEvaluate.disabled, true);
  assert.match(ui.elements.validationQualification.textContent, /尚不具备独立评价资格/);
  await ui.elements.validationAudit.fire("click"); await ui.elements.validationForm.fire("submit");
  assert.ok(ui.requests.every((request) => request.method === "GET"));
  assert.match(ui.elements.validationError.textContent, /真实独立标签缺失/);
});

test("validation production aggregate shows paired-model metrics, nested cluster intervals and completed cleanup", async () => {
  const historical = run({ device: "cpu", cleanupConfirmed: true, resultSha256: "software-fixture-sha" });
  historical.report = { models: { B16: { modelBinding: { modelVersion: "fixture-bound-model" }, endpoints: { clintox: { count: 20, coveredCount: 19, positives: 2, auroc: null, averagePrecision: null, brier: .2, ece10Bins: .1, status: "coverage-incomplete", confidenceIntervals: { intervals: { auroc: { lower: null, upper: null } } } } } } }, limitations: [] };
  const ui = await setup({ storage: { "aidd.validation.lastRunId": "fixture-run" }, fetch(request) { if (request.url.endsWith("/runs/fixture-run")) return response(historical); } });
  assert.match(ui.elements.validationMetricRows.textContent, /clintox \/ B16.*20 \/ 2.*19\/20.*未计算.*0\.2000 \/ 0\.1000.*fixture-bound-model/);
  assert.match(ui.elements.validationRunMeta.textContent, /cpu.*已确认.*software-fixture-sha/);
  assert.equal(ui.elements.validationExport.disabled, false);
});

test("validation registered custom protocol takes precedence over the unqualified global placeholder", async () => {
  const current = catalog(); current.datasets[0].protocolId = "registered-frozen-protocol";
  const ui = await setup({ fetch(request) { if (request.url.endsWith("/datasets")) return response(current); } });
  assert.equal(ui.elements.validationProtocol.value, "registered-frozen-protocol");
  await ui.elements.validationForm.fire("submit"); await flush();
  assert.equal(ui.requests.find((request) => request.url.endsWith("/evaluate")).body.protocolId, "registered-frozen-protocol");
});

test("validation catalog changes invalidate a pending POST and cancel its late acknowledgment", async () => {
  const pending = deferred(); let revision = 0;
  const ui = await setup({ fetch(request) {
    if (request.url.endsWith("/audit")) return pending.promise;
    if (request.url.endsWith("/datasets")) { const current = catalog(); current.datasets[0].version = `software-only-${revision++}`; return response(current); }
  } });
  const started = ui.elements.validationAudit.fire("click");
  await ui.elements.validationRefresh.fire("click");
  pending.resolve(response({ schema: Core.SCHEMA, runId: "fixture-run", status: "pending" }, 202)); await started; await flush();
  assert.equal(ui.elements.validationStale.hidden, false);
  assert.ok(ui.requests.some((request) => request.method === "DELETE" && request.url.endsWith("/fixture-run")));
  assert.ok(!ui.requests.some((request) => request.method === "GET" && request.url.endsWith("/runs/fixture-run")));
});

test("validation incompatible accepted schema is cancelled without publishing metrics", async () => {
  const ui = await setup({ fetch(request) { if (request.url.endsWith("/audit")) return response({ schema: "unknown", runId: "fixture-run", status: "pending" }, 202); } });
  await ui.elements.validationAudit.fire("click"); await flush();
  assert.match(ui.elements.validationError.textContent, /合同/);
  assert.equal(ui.elements.validationExport.disabled, true);
  assert.ok(ui.requests.some((request) => request.method === "DELETE"));
  assert.ok(!ui.requests.some((request) => request.method === "GET" && request.url.endsWith("/runs/fixture-run")));
});

test("validation nested fairComparison uses source-derived coverage and preserves not-qualified evidence", async () => {
  const historical = run(); historical.report = { independenceStatus: "not-independently-qualified", formalAvailability: false,
    datasetManifestSha256: "software-only-source", exposure: { status: "previously-evaluated-same-frozen-protocol", freshBlindTestClaimed: false },
    fairComparison: { labelsAndMembersMatched: true, models: { B16: { modelBinding: { modelVersion: "software-only-version" }, endpoints: { herg: { count: 30, positives: 10, coveredCount: 27, coverageDenominator: 30, coverageFraction: .9, auroc: null, confidenceIntervals: null, status: "coverage-incomplete" } } } } } };
  const ui = await setup({ storage: { "aidd.validation.lastRunId": "fixture-run" }, fetch(request) { if (request.url.endsWith("/runs/fixture-run")) return response(historical); } });
  assert.match(ui.elements.validationMetricRows.textContent, /0\.9000 \(27\/30\)/);
  assert.match(ui.elements.validationMetricRows.textContent, /不发布完整成员指标/);
  assert.match(ui.elements.validationEvidenceSummary.textContent, /尚不具备独立评价资格/);
  assert.match(ui.elements.validationEvidenceDetails.textContent, /previously-evaluated-same-frozen-protocol/);
  assert.equal(ui.elements.validationQualification.textContent, "尚不具备独立评价资格");
});

test("validation calibration diagnostics render measured bins with a visible resubstitution limitation", async () => {
  const diagnostic = { schema: "aidd.calibration-diagnostics.v1", role: "calibration-resubstitution", independentTest: false, methodSelectionPermitted: false,
    ...Object.fromEntries(["diagnosticSha256", "datasetManifestSha256", "protocolSha256", "membershipSha256", "modelBindingSha256"].map((name) => [name, "a".repeat(64)])),
    endpoints: { herg: { count: 10, positives: 3, reliabilityBefore: [{ count: 10, meanPredicted: .2, observedPositiveRate: .3 }], reliabilityAfter: null } } };
  const historical = run(); historical.report.calibration = diagnostic;
  const ui = await setup({ storage: { "aidd.validation.lastRunId": "fixture-run" }, fetch(request) { if (request.url.endsWith("/runs/fixture-run")) return response(historical); } });
  assert.match(ui.elements.validationReliability.textContent, /校准前.*非独立评价/);
  assert.equal(ui.elements.validationReliability.children.filter((node) => node.tagName === "SVG").length, 1);
});
