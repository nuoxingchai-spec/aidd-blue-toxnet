"use strict";

// Software interaction fixtures only. Every fetch, model result, and performance
// field below is stubbed; these tests do not demonstrate real model deployment.
const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const Core = require("../compare-core.js");
const source = fs.readFileSync(path.join(__dirname, "..", "compare-app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "..", "compare.html"), "utf8");
const contractVersion = Core.CONTRACT;

function deferred() {
  let resolve, reject;
  const promise = new Promise((accept, fail) => { resolve = accept; reject = fail; });
  return { promise, resolve, reject };
}
const flush = () => new Promise((resolve) => setImmediate(resolve));

class Element {
  constructor(tag = "div", ownerDocument = null) {
    this.tagName = tag.toUpperCase(); this.ownerDocument = ownerDocument;
    this.children = []; this.parentElement = null; this.dataset = {}; this.events = {};
    this.attributesMap = {}; this.style = {}; this.id = ""; this.hidden = false;
    this.disabled = false; this.checked = false; this.open = false; this.className = "";
    this._value = ""; this._text = ""; this.replaceWrites = 0;
  }
  get value() { return this._value; }
  set value(value) {
    const text = String(value);
    this._value = this.tagName === "SELECT" && !this.children.some((child) => child.value === text) ? "" : text;
  }
  get firstChild() { return this.children[0] || null; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  set textContent(value) { this._text = String(value); this.children.forEach((child) => { child.parentElement = null; }); this.children = []; }
  get isConnected() { return Boolean(this.ownerDocument && this.ownerDocument.body.contains(this)); }
  append(...children) {
    for (const child of children) {
      if (child.parentElement) child.remove();
      const firstOption = this.tagName === "SELECT" && !this.children.length;
      this.children.push(child); child.parentElement = this;
      if (firstOption) this._value = child.value;
    }
  }
  replaceChildren(...children) {
    this.replaceWrites += 1; this._text = "";
    this.children.forEach((child) => { child.parentElement = null; }); this.children = [];
    if (this.tagName === "SELECT") this._value = "";
    this.append(...children);
  }
  remove() {
    if (this.parentElement) this.parentElement.children = this.parentElement.children.filter((child) => child !== this);
    this.parentElement = null;
  }
  contains(child) { return this === child || this.children.some((node) => node.contains(child)); }
  setAttribute(name, value) { this.attributesMap[name] = String(value); }
  addEventListener(name, callback) { (this.events[name] ||= []).push(callback); }
  fire(name, event = {}) {
    const results = (this.events[name] || []).map((callback) => callback({ preventDefault() {}, target: this, ...event }));
    return Promise.all(results);
  }
  click() { return this.fire("click"); }
  focus() { if (this.ownerDocument) this.ownerDocument.activeElement = this; }
  reset() { if (this.ownerDocument && this.ownerDocument.resetFields) this.ownerDocument.resetFields(); }
  querySelectorAll(selector) {
    const matches = (element) => selector.split(",").some((piece) => {
      const part = piece.trim();
      if (part === "details[data-view-key]") return element.tagName === "DETAILS" && Object.hasOwn(element.dataset, "viewKey");
      if (part === "[data-focus-key]") return Object.hasOwn(element.dataset, "focusKey");
      if (part === "[data-reading-key]") return Object.hasOwn(element.dataset, "readingKey");
      if (part === "[id]") return Boolean(element.id);
      return element.tagName === part.toUpperCase();
    });
    const results = [];
    const visit = (node) => { for (const child of node.children) { if (matches(child)) results.push(child); visit(child); } };
    visit(this); return results;
  }
}

function registry() {
  return { contractVersion, testFixture: false,
    models: Core.MODEL_IDS.map((id) => ({ id, configured: id !== "txgemma-2b-predict", available: id === "toxnet-v3-b16",
      runtime: id === "ollama-qwen-local" ? "ollama" : "software-fixture", actualModelId: `${id}-fixture`, revision: "software-fixture-only",
      ...(id === "ollama-qwen-local" ? { installedModels: [{ name: "qwen-test-only:4b", digest: "software-fixture-digest" }], selectedDefaultModel: "qwen-test-only:4b" } : {}) })),
    specialistTasks: [{ id: "tdc-fixture-task", name: "Software fixture task", supported: true, directComparable: false }] };
}
function report() {
  return { snapshotSha256: "fixture-snapshot", sections: [{ id: "fixture-section", title: "软件交互测试报告", text: "此处仅为软件验证文字。", claimIds: ["fixture-score"] }],
    sourceVerification: { status: "software-fixture-only" }, verifiedFactIds: ["fixture-score"] };
}
function run(overrides = {}) {
  const request = Core.buildRequest({ smiles: "CCO", question: "软件夹具问题", includeEvidence: true, evidenceEndpoint: "ames",
    includeAttribution: false, ollamaModel: "qwen-test-only:4b" });
  return { contractVersion, runId: "fixture-run-1", status: "completed", request, contentSaved: true,
    structure: { canonicalSmiles: "CCO", structureId: "fixture-structure-only" },
    snapshot: { snapshotSha256: "fixture-snapshot", facts: [{ id: "fixture-score", sourceKind: "software-fixture", text: "软件验证分数，不是真实预测。", sourceIds: ["fixture-source"] }] },
    models: {
      "toxnet-v3-b16": { status: "completed", provenance: { testFixture: true }, data: {
        prediction: { endpoints: [{ id: "ames", score: 0.25, positiveClass: "软件夹具标签" }] },
        evidence: { status: "insufficient-evidence", results: [], limitations: ["软件夹具，没有实验数据"] } } },
      "txgemma-2b-predict": { status: "skipped" },
      "ollama-qwen-local": { status: "completed", provenance: { testFixture: true }, data: { report: report() } },
      "deepseek-api": { status: "skipped" },
    }, ...overrides };
}
function response(value, status = 200) { return { ok: status >= 200 && status < 300, status, async json() { return value; } }; }

async function setup(options = {}) {
  const elements = {}, requests = [], blobs = [], confirms = [], timers = new Map(), storageWrites = [];
  const storage = new Map(Object.entries(options.storage || {}));
  const document = { activeElement: null,
    getElementById(id) { assert.ok(elements[id], `Missing Compare HTML element: ${id}`); return elements[id]; },
    createElement(tag) { return new Element(tag, document); },
    querySelectorAll(selector) { return document.body.querySelectorAll(selector); },
  };
  document.body = new Element("body", document);
  for (const match of html.matchAll(/<([\w-]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const [, tag, attrs, id] = match;
    const element = new Element(tag, document); element.id = id;
    element.hidden = /\bhidden(?:\s|$)/.test(attrs); element.disabled = /\bdisabled(?:\s|$)/.test(attrs); element.checked = /\bchecked(?:\s|$)/.test(attrs);
    if (tag === "select") {
      const start = match.index + match[0].length;
      const markup = html.slice(start, html.indexOf("</select>", start));
      for (const item of markup.matchAll(/<option\b[^>]*value="([^"]*)"[^>]*>([^<]*)<\/option>/g)) {
        const option = new Element("option", document); option.value = item[1]; option.textContent = item[2]; element.append(option);
      }
    }
    elements[id] = element; document.body.append(element);
  }
  let timerId = 0;
  const windowEvents = {};
  const window = { CompareCore: Core, scrollY: 0, scrollX: 0, innerWidth: 1200, innerHeight: 900,
    addEventListener(name, callback) { (windowEvents[name] ||= []).push(callback); },
    fire(name, event = {}) { return Promise.all((windowEvents[name] || []).map((callback) => callback(event))); },
    scrollTo() {}, confirm(message) { confirms.push(message); return options.confirm ? options.confirm(message) : true; } };
  const context = { window, document, Node: Element, AbortController, AbortSignal, Blob, Intl, Date,
    location: { protocol: "http:" },
    localStorage: { getItem(key) { return storage.get(key) ?? null; }, setItem(key, value) { storageWrites.push([key, value]); storage.set(key, value); }, removeItem(key) { storage.delete(key); } },
    setTimeout(callback, delay) { const id = ++timerId; timers.set(id, { callback, delay }); return id; }, clearTimeout(id) { timers.delete(id); },
    URL: { createObjectURL(blob) { blobs.push(blob); return "blob:software-fixture"; }, revokeObjectURL() {} },
    async fetch(url, options_ = {}) {
      const request = { url, method: options_.method || "GET", body: options_.body ? JSON.parse(options_.body) : null, signal: options_.signal };
      requests.push(request);
      if (options.fetch) {
        const custom = await options.fetch(request);
        if (custom !== undefined) return custom;
      }
      if (url === "/api/research/models") return response(registry());
      if (url === "/api/research/compare/presets" && request.method === "GET") return response({ contractVersion, presets: options.presets || [] });
      if (url === "/api/research/compare/runs" && request.method === "POST") return response({ contractVersion, runId: "fixture-run-1" }, 202);
      if (url === "/api/research/compare/runs/fixture-run-1") return response(run({ status: request.method === "DELETE" ? "cancelled" : "completed" }));
      throw new Error(`Unstubbed software fixture request ${request.method} ${url}`);
    },
  };
  vm.runInNewContext(source, context, { filename: "compare-app.js" });
  await flush();
  return { elements, requests, document, window, timers, storage, storageWrites, blobs, confirms,
    async pollOnce() { const timer = [...timers.entries()].find(([, item]) => item.delay === 1000); assert.ok(timer, "Expected scheduled status poll"); timers.delete(timer[0]); await timer[1].callback(); await flush(); } };
}

test("software fixture: remembered run is read-only, resets both permissions, and keeps old Mock provenance visible", async () => {
  const delayedRegistry = deferred();
  const historical = run(); historical.request.allowCloudTransfer = true; historical.request.persistSensitiveContent = true; historical.request.comparisonReporter = "deepseek-api";
  const ui = await setup({ storage: { "aidd.compare.lastRunId": "fixture-run-1" }, fetch(request) {
    if (request.url === "/api/research/models") return delayedRegistry.promise;
    if (request.url === "/api/research/compare/runs/fixture-run-1") return response(historical);
  } });
  assert.equal(ui.elements.compareRunButton.disabled, true);
  delayedRegistry.resolve(response(registry())); await flush();
  assert.ok(ui.requests.some((request) => request.url.endsWith("/fixture-run-1")));
  assert.ok(ui.requests.every((request) => request.method === "GET"), "Recovery must never create or cancel a model run");
  assert.equal(ui.elements.compareAllowCloud.checked, false);
  assert.equal(ui.elements.comparePersistSensitive.checked, false);
  assert.equal(ui.elements.compareEnableDeepSeek.checked, false);
  assert.equal(ui.elements.compareSmiles.value, "CCO");
  assert.equal(ui.elements.compareOllamaModel.value, "qwen-test-only:4b", "Late registry population must restore the selected installed model without replaying permissions");
  assert.equal(ui.elements.compareStaleBanner.hidden, true);
  assert.equal(ui.elements.compareFixtureBanner.hidden, false);
  assert.match(ui.elements.compareFixtureBanner.textContent, /历史运行.*Mock/);
  assert.match(ui.elements.compareModelCards.textContent, /已完成 Mock 软件验证/);
  assert.doesNotMatch(ui.elements.compareModelCards.textContent, /已完成真实报告生成/);
  assert.match(ui.elements.compareRecoveryNotice.textContent, /只读恢复.*授权均保持关闭/);
});

for (const keepSourceUrl of [true, false]) {
  test(`software fixture: historical GGUF source stays independent of current Transformers registry (URL=${keepSourceUrl})`, async () => {
    const current = registry();
    const txEntry = current.models.find((item) => item.id === "txgemma-2b-predict");
    Object.assign(txEntry, { runtime: "local-transformers", source: "Google official current safetensors source", licenseStatus: "current-license" });
    const provenance = { testFixture: true, runtime: "ollama-gguf", modelId: "txgemma-fixture:latest", revision: "historical-manifest",
      sourceKind: "third-party-quantized", sourceRepository: "matrixportalx/txgemma-2b-predict-GGUF", sourceRevision: "historical-source-revision",
      upstreamRevisionStatus: "unknown-not-published-by-quantizer", ggufSha256: "fixture-gguf-sha", quantization: "Q4_K_M", licenseStatus: "operator-accepted" };
    if (keepSourceUrl) provenance.sourceUrl = "https://huggingface.co/matrixportalx/txgemma-2b-predict-GGUF/blob/historical-source-revision/test.gguf";
    const historical = run(); historical.models["txgemma-2b-predict"] = { status: "completed", provenance };
    const ui = await setup({ storage: { "aidd.compare.lastRunId": "fixture-run-1" }, fetch(request) {
      if (request.url === "/api/research/models") return response(current);
      if (request.url === "/api/research/compare/runs/fixture-run-1") return response(historical);
    } });
    await flush();
    const card = ui.elements.compareModelCards.children.find((item) => item.dataset.modelId === "txgemma-2b-predict");
    const items = card.querySelectorAll("dl")[0].children;
    const fields = {}; for (let i = 0; i < items.length; i += 2) fields[items[i].textContent] = items[i + 1].textContent;
    assert.equal(fields["运行方式"], "ollama-gguf");
    assert.equal(fields["来源"], keepSourceUrl ? provenance.sourceUrl : "matrixportalx/txgemma-2b-predict-GGUF@historical-source-revision");
    assert.match(fields["来源审核类型"], /第三方量化.*不是已验证的官方 safetensors revision/);
    assert.match(fields["上游权重修订审核"], /unknown-not-published-by-quantizer/);
    assert.match(fields["许可状态"], /操作者声明/);
    assert.doesNotMatch(card.textContent, /Google official current safetensors source|local-transformers|current-license/);
    assert.ok(ui.requests.every((request) => request.method === "GET"));
  });
}

test("software fixture: Qwen shows per-run generation options separately from configured budget and status reason", async () => {
  const current = registry();
  const qwen = current.models.find((item) => item.id === "ollama-qwen-local");
  qwen.metadata = { generationPolicy: { contextTokens: 8192, outputTokenLimit: 4096, configuredNotCapacityMeasured: true }, statusReason: "Fixture budget checked; GPU capacity unmeasured." };
  const historical = run(); historical.models["ollama-qwen-local"].provenance.generationOptions = { num_ctx: 4096, num_predict: 2048 };
  const ui = await setup({ storage: { "aidd.compare.lastRunId": "fixture-run-1" }, fetch(request) {
    if (request.url === "/api/research/models") return response(current);
    if (request.url === "/api/research/compare/runs/fixture-run-1") return response(historical);
  } });
  await flush();
  const card = ui.elements.compareModelCards.children.find((item) => item.dataset.modelId === "ollama-qwen-local");
  const items = card.querySelectorAll("dl")[0].children;
  const fields = {}; for (let i = 0; i < items.length; i += 2) fields[items[i].textContent] = items[i + 1].textContent;
  assert.match(fields["本次生成配置"], /"num_ctx": 4096/);
  assert.match(fields["当前生成预算（配置值，非显存实测）"], /"contextTokens": 8192/);
  assert.equal(fields["当前模型状态说明"], qwen.metadata.statusReason);
});

test("software fixture: Run and keyboard shortcut cannot POST before model registry completes", async () => {
  const pending = deferred();
  const ui = await setup({ fetch(request) { if (request.url === "/api/research/models") return pending.promise; } });
  ui.elements.compareSmiles.value = "CCO";
  assert.equal(ui.elements.compareRunButton.disabled, true);
  await ui.elements.compareForm.fire("submit");
  await ui.elements.compareForm.fire("keydown", { key: "Enter", ctrlKey: true });
  await flush();
  assert.equal(ui.requests.filter((request) => request.method === "POST").length, 0);
  assert.match(ui.elements.compareError.textContent, /先完成真实模型状态检查/);
  pending.resolve(response(registry())); await flush();
  assert.equal(ui.elements.compareRunButton.disabled, false);
  assert.equal(ui.requests.filter((request) => request.method === "POST").length, 0, "Registry arrival must not silently retry submission");
});

test("software fixture: per-run permissions are consumed before ACK and late ACK preserves newly granted permissions", async () => {
  const accepted = deferred();
  const ui = await setup({ fetch(request) { if (request.method === "POST" && request.url.endsWith("/runs")) return accepted.promise; } });
  ui.elements.compareSmiles.value = "CCO";
  ui.elements.compareEnableDeepSeek.checked = true; ui.elements.compareAllowCloud.checked = true; ui.elements.comparePersistSensitive.checked = true;
  const submitted = ui.elements.compareForm.fire("submit");
  const create = ui.requests.find((request) => request.method === "POST");
  assert.equal(create.body.allowCloudTransfer, true); assert.equal(create.body.persistSensitiveContent, true);
  assert.equal(ui.elements.compareAllowCloud.checked, false); assert.equal(ui.elements.comparePersistSensitive.checked, false);
  ui.elements.compareSmiles.value = "CCN"; await ui.elements.compareSmiles.fire("input");
  ui.elements.compareAllowCloud.checked = true; await ui.elements.compareAllowCloud.fire("change");
  ui.elements.comparePersistSensitive.checked = true; await ui.elements.comparePersistSensitive.fire("change");
  accepted.resolve(response({ contractVersion, runId: "fixture-run-1" }, 202)); await submitted; await flush();
  assert.equal(ui.elements.compareAllowCloud.checked, true); assert.equal(ui.elements.comparePersistSensitive.checked, true);
  assert.ok(ui.requests.some((request) => request.method === "DELETE" && request.url.endsWith("/fixture-run-1")), "Accepted stale run must be cancelled");
  assert.equal(ui.storage.get("aidd.compare.lastRunId"), undefined, "A stale acceptance cannot become the remembered current run");
  assert.equal(ui.elements.compareExportButton.disabled, true);
});

test("software fixture: evidence filtering and attribution use independent request switches and endpoints", async () => {
  for (const includeAttribution of [false, true]) {
    const ui = await setup();
    ui.elements.compareSmiles.value = "CCO";
    ui.elements.compareIncludeEvidence.checked = true; ui.elements.compareEvidenceEndpoint.value = "ames";
    ui.elements.compareIncludeAttribution.checked = includeAttribution; ui.elements.compareExplainEndpoint.value = "dili";
    await ui.elements.compareIncludeAttribution.fire("change"); await ui.elements.compareIncludeEvidence.fire("change");
    assert.equal(ui.elements.compareExplainEndpoint.disabled, !includeAttribution);
    assert.equal(ui.elements.compareEvidenceEndpoint.disabled, false);
    await ui.elements.compareForm.fire("submit"); await flush();
    const payload = ui.requests.find((request) => request.method === "POST").body;
    assert.equal(payload.includeEvidence, true); assert.equal(payload.evidenceEndpoint, "ames");
    assert.equal(payload.includeAttribution, includeAttribution); assert.equal(payload.explainEndpoint, "dili");
    assert.equal(payload.allowCloudTransfer, false); assert.equal(payload.persistSensitiveContent, false);
  }
});

test("software fixture: failed history pagination restores buttons and retains the last successful page", async () => {
  let count = 0;
  const ui = await setup({ fetch(request) {
    if (request.url.startsWith("/api/research/compare/runs?")) {
      if (++count === 1) return response({ contractVersion, runs: [{ runId: "history-only", status: "partial", structureId: "fixture-structure" }], offset: 0, limit: 10, total: 12, hasMore: true });
      throw new Error("software fixture network failure");
    }
  } });
  await ui.elements.compareHistoryButton.fire("click"); await flush();
  assert.equal(ui.elements.compareHistoryNext.disabled, false); assert.equal(ui.elements.compareHistoryPrevious.disabled, true);
  const rows = ui.elements.compareHistoryRows.children.slice();
  await ui.elements.compareHistoryNext.fire("click"); await flush();
  assert.equal(ui.elements.compareHistoryRefresh.disabled, false);
  assert.equal(ui.elements.compareHistoryNext.disabled, false); assert.equal(ui.elements.compareHistoryPrevious.disabled, true);
  assert.equal(ui.elements.compareHistoryRows.children[0], rows[0]);
  assert.match(ui.elements.compareHistoryPage.textContent, /第 1 页/);
  assert.match(ui.elements.compareHistoryStatus.textContent, /读取历史失败.*network failure/);
  assert.equal(ui.requests.filter((request) => request.method !== "GET").length, 0);
});

test("software fixture: preset update/delete require confirmation, surface 409, and never save sensitive authorizations", async () => {
  let allow = false;
  const ui = await setup({ presets: [{ id: "preset-fixture", name: "Fixture preset", settings: {}, updatedAt: "fixture-date" }],
    confirm() { return allow; }, fetch(request) {
      if (request.url === "/api/research/compare/presets/preset-fixture") return response({ error: { code: "PRESET_NAME_CONFLICT", message: "配置名称已存在（409 软件夹具）" } }, 409);
    } });
  ui.elements.comparePresetSelect.value = "preset-fixture"; await ui.elements.comparePresetSelect.fire("change");
  ui.elements.comparePresetName.value = "New fixture name";
  await ui.elements.compareUpdatePreset.fire("click"); await ui.elements.compareDeletePreset.fire("click");
  assert.equal(ui.confirms.length, 2);
  assert.equal(ui.requests.filter((request) => ["PATCH", "DELETE"].includes(request.method)).length, 0);
  allow = true; ui.elements.compareAllowCloud.checked = true; ui.elements.comparePersistSensitive.checked = true;
  await ui.elements.compareUpdatePreset.fire("click");
  const update = ui.requests.find((request) => request.method === "PATCH");
  assert.ok(update); assert.equal(update.body.name, "New fixture name");
  for (const key of ["allowCloudTransfer", "persistSensitiveContent", "smiles", "question", "apiKey"]) assert.equal(Object.hasOwn(update.body.settings, key), false, `Preset must exclude ${key}`);
  assert.match(ui.elements.comparePresetStatus.textContent, /名称已存在.*409/);
  assert.equal(ui.elements.compareUpdatePreset.disabled, false);
  await ui.elements.compareDeletePreset.fire("click");
  assert.ok(ui.requests.some((request) => request.method === "DELETE"));
  assert.match(ui.elements.comparePresetStatus.textContent, /名称已存在.*409/);
  assert.equal(ui.elements.compareDeletePreset.disabled, false);
});

test("software fixture: unchanged polling preserves report/evidence details objects, expansion, and focus", async () => {
  function reverseObjectKeys(value) {
    if (Array.isArray(value)) return value.map(reverseObjectKeys);
    if (value && typeof value === "object") return Object.fromEntries(Object.keys(value).reverse().map((key) => [key, reverseObjectKeys(value[key])]));
    return value;
  }
  let reads = 0;
  const base = run({ status: "running" }); base.models["deepseek-api"] = { status: "running" };
  const ui = await setup({ fetch(request) {
    if (request.method === "GET" && request.url === "/api/research/compare/runs/fixture-run-1") {
      reads += 1; const updated = JSON.parse(JSON.stringify(base));
      updated.models["deepseek-api"].metrics = { durationMs: reads * 100 };
      // JSON object key order is not a report/evidence change. A later status
      // response may serialize equivalent facts in a different order.
      return response(reads > 1 ? reverseObjectKeys(updated) : updated);
    }
  } });
  ui.elements.compareSmiles.value = "CCO"; await ui.elements.compareForm.fire("submit"); await flush();
  const target = ui.elements.compareQwenContent, evidence = ui.elements.compareEvidenceContent;
  const reportDetails = target.querySelectorAll("details[data-view-key]")[0];
  const evidenceDetails = evidence.querySelectorAll("details[data-view-key]")[0];
  assert.ok(reportDetails); assert.ok(evidenceDetails);
  reportDetails.open = true; evidenceDetails.open = true;
  const summary = reportDetails.querySelectorAll("summary")[0]; summary.focus();
  const reportWrites = target.replaceWrites, evidenceWrites = evidence.replaceWrites;
  await ui.pollOnce();
  assert.equal(reads, 2);
  assert.equal(target.querySelectorAll("details[data-view-key]")[0], reportDetails);
  assert.equal(evidence.querySelectorAll("details[data-view-key]")[0], evidenceDetails);
  assert.equal(reportDetails.open, true); assert.equal(evidenceDetails.open, true);
  assert.equal(ui.document.activeElement, summary); assert.equal(target.contains(summary), true);
  assert.equal(target.replaceWrites, reportWrites); assert.equal(evidence.replaceWrites, evidenceWrites);
});

test("software fixture: summary-only history exports a summary and input edits invalidate that export", async () => {
  const historical = run({ request: undefined, contentSaved: false, recovery: { contentAvailable: false } });
  delete historical.snapshot;
  historical.structure = { structureId: "fixture-structure-only" };
  delete historical.models["toxnet-v3-b16"].data.evidence;
  delete historical.models["ollama-qwen-local"].data;
  const ui = await setup({ storage: { "aidd.compare.lastRunId": "fixture-run-1" }, fetch(request) {
    if (request.url.endsWith("/fixture-run-1")) return response(historical);
  } });
  assert.equal(ui.elements.compareSmiles.value, "");
  assert.match(ui.elements.compareRecoveryNotice.textContent, /内容未保存.*报告正文无法恢复/);
  assert.match(ui.elements.compareQwenContent.textContent, /报告正文未保存/);
  assert.equal(ui.elements.compareExportButton.textContent, "导出摘要 JSON");
  await ui.elements.compareExportButton.fire("click");
  assert.equal(ui.blobs.length, 1); const exported = JSON.parse(await ui.blobs[0].text());
  assert.equal(exported.recovery.contentAvailable, false);
  assert.equal(Object.hasOwn(exported, "request"), false);
  assert.equal(Object.hasOwn(exported.structure, "canonicalSmiles"), false);
  ui.elements.compareSmiles.value = "CCN"; await ui.elements.compareSmiles.fire("input");
  assert.equal(ui.elements.compareExportButton.disabled, true);
  await ui.elements.compareExportButton.fire("click"); assert.equal(ui.blobs.length, 1);
  assert.equal(ui.requests.filter((request) => request.method !== "GET").length, 0);
});

test("software fixture: back-forward cache resumes GET polling without resubmission or inherited permissions", async () => {
  const pendingRun = run({ status: "running" });
  const ui = await setup({ fetch(request) {
    if (request.url === "/api/research/compare/runs/fixture-run-1") return response(pendingRun);
  } });
  ui.elements.compareSmiles.value = "CCO";
  await ui.elements.compareForm.fire("submit"); await flush();
  const oldPoll = ui.requests.find((request) => request.url.endsWith("/fixture-run-1"));
  const oldGets = ui.requests.filter((request) => request.method === "GET" && request.url.endsWith("/fixture-run-1")).length;
  await ui.window.fire("pagehide", { persisted: true });
  assert.equal(oldPoll.signal.aborted, true);
  assert.equal(ui.requests.filter((request) => request.method === "DELETE").length, 0, "Leaving a retained page must not cancel the accepted server run");
  ui.elements.compareAllowCloud.checked = true; ui.elements.comparePersistSensitive.checked = true;
  await ui.window.fire("pageshow", { persisted: true }); await flush();
  assert.equal(ui.elements.compareAllowCloud.checked, false); assert.equal(ui.elements.comparePersistSensitive.checked, false);
  assert.equal(ui.requests.filter((request) => request.method === "POST").length, 1, "Returning to a retained page must not create a second run");
  assert.equal(ui.requests.filter((request) => request.method === "DELETE").length, 0);
  assert.ok(ui.requests.filter((request) => request.method === "GET" && request.url.endsWith("/fixture-run-1")).length > oldGets);
});

test("software fixture: fast/deep mode submits v2 explicitly, invalidates old results and blocks silent export", async () => {
  const ui = await setup();
  ui.elements.compareSmiles.value = "CCO";
  assert.equal(ui.elements.compareReportMode.value, "fast");
  await ui.elements.compareForm.fire("submit"); await flush();
  const payload = ui.requests.find((item) => item.method === "POST").body;
  assert.equal(payload.reportContract, "v2"); assert.equal(payload.reportMode, "fast");
  assert.equal(ui.elements.compareExportButton.disabled, false);
  ui.elements.compareReportMode.value = "deep";
  await ui.elements.compareReportMode.fire("change");
  assert.equal(ui.elements.compareStaleBanner.hidden, false);
  assert.equal(ui.elements.compareExportButton.disabled, true);
  assert.match(ui.elements.compareRunStatus.textContent, /报告模式.*旧结果已失效/);
  await ui.elements.compareExportButton.fire("click");
  assert.equal(ui.blobs.length, 0);
});

test("software fixture: v2 facts are read from snapshot and generated commentary remains visually unverified", async () => {
  const historical = run();
  const report_ = historical.models["ollama-qwen-local"].data.report;
  Object.assign(report_, { reportSchema: "aidd.compare.report.v2", reportMode: "deep" });
  Object.assign(report_.sections[0], { text: "This report field must not replace the snapshot score", commentary: ["软件夹具生成解释，未验证。"] });
  const ui = await setup({ storage: { "aidd.compare.lastRunId": "fixture-run-1" }, fetch(request) {
    if (request.url.endsWith("/fixture-run-1")) return response(historical);
  } });
  const target = ui.elements.compareQwenContent;
  assert.match(target.textContent, /v2 · 深度报告 Deep/);
  assert.match(target.textContent, /已核验快照事实.*软件验证分数，不是真实预测/);
  assert.match(target.textContent, /模型生成解释 · 未验证.*软件夹具生成解释/);
  assert.doesNotMatch(target.textContent, /must not replace/);
  await ui.elements.compareExportButton.fire("click");
  const packet = JSON.parse(await ui.blobs[0].text());
  assert.equal(packet.models["ollama-qwen-local"].data.report.reportSchema, "aidd.compare.report.v2");
  assert.equal(packet.models["ollama-qwen-local"].data.report.sections[0].commentary[0], "软件夹具生成解释，未验证。");
});

test("software fixture: legacy history stays v1, old presets choose fast v2 and neither action executes models", async () => {
  const historical = run(); delete historical.request.reportMode; delete historical.request.reportContract;
  const ui = await setup({ storage: { "aidd.compare.lastRunId": "fixture-run-1" }, presets: [{ id: "old-preset", name: "Legacy preset", settings: { includeTox21: true } }], fetch(request) {
    if (request.url.endsWith("/fixture-run-1")) return response(historical);
  } });
  assert.match(ui.elements.compareReportMetadata.textContent, /v1.*历史合同/);
  assert.match(ui.elements.compareQwenContent.textContent, /v1.*历史合同/);
  ui.elements.compareReportMode.value = "deep";
  ui.elements.comparePresetSelect.value = "old-preset"; await ui.elements.comparePresetSelect.fire("change");
  assert.equal(ui.elements.compareReportMode.value, "fast");
  assert.equal(ui.requests.filter((request) => request.method !== "GET").length, 0);
});

test("software fixture: history search encodes local query, resets page and keeps pagination", async () => {
  const queries = [];
  const ui = await setup({ fetch(request) {
    if (request.url.startsWith("/api/research/compare/runs?")) {
      queries.push(request.url);
      const offset = request.url.includes("offset=10") ? 10 : 0;
      return response({ contractVersion, runs: [{ runId: "search-result", status: "completed", request: { reportContract: "v2", reportMode: "deep" } }], offset, limit: 10, total: 11, hasMore: !offset });
    }
  } });
  await ui.elements.compareHistoryButton.fire("click"); await flush();
  await ui.elements.compareHistoryNext.fire("click"); await flush();
  assert.match(queries.at(-1), /offset=10/);
  ui.elements.compareHistoryQuery.value = "  完成 & model  ";
  await ui.elements.compareHistoryQuery.fire("keydown", { key: "Enter" }); await flush();
  assert.match(queries.at(-1), /offset=0&q=%E5%AE%8C%E6%88%90%20%26%20model$/);
  assert.match(ui.elements.compareHistoryRows.textContent, /v2 · 深度报告 Deep/);
  await ui.elements.compareHistoryNext.fire("click"); await flush();
  assert.match(queries.at(-1), /offset=10&q=/);
  assert.ok(ui.requests.every((request) => request.method === "GET"));
});

test("software fixture: runtime ownership, queue, tokens and cleanup retain measured/unknown distinction", async () => {
  const historical = run({ resourceState: { activeModel: "ollama-qwen-local", blocked: false }, metrics: { durationMs: 1200, queueWaitMs: null } });
  historical.models["ollama-qwen-local"].metrics = { queueWaitMs: 0, durationMs: 1000, generationSeconds: null,
    promptTokens: 123, completionTokens: 45, gpu: { measured: false, peakAllocatedMiB: 8000 }, cleanupState: { confirmed: false } };
  const ui = await setup({ storage: { "aidd.compare.lastRunId": "fixture-run-1" }, fetch(request) {
    if (request.url.endsWith("/fixture-run-1")) return response(historical);
  } });
  assert.match(ui.elements.compareResourceStatus.textContent, /GPU 调度当前阶段：Qwen · Ollama/);
  assert.match(ui.elements.compareResourceStatus.textContent, /入队等待：未测量/);
  assert.match(ui.elements.compareMetricsRows.textContent, /排队 0.00 ms.*生成 未测量.*123 \/ 45.*释放未确认/);
  assert.doesNotMatch(ui.elements.compareMetricsRows.textContent, /8000/);
});

test("software fixture: production run top-level queue/duration override legacy metrics even when unknown", async () => {
  const historical = run({ queueWaitMs: 12.5, durationMs: 1500.25, metrics: { durationMs: 9999, queueWaitMs: 8888 } });
  const ui = await setup({ storage: { "aidd.compare.lastRunId": "fixture-run-1" }, fetch(request) {
    if (request.url.endsWith("/fixture-run-1")) return response(historical);
  } });
  assert.match(ui.elements.compareResourceStatus.textContent, /入队等待：12.50 ms.*总耗时：1500.25 ms/);
  assert.doesNotMatch(ui.elements.compareResourceStatus.textContent, /9999|8888/);
  // Null means unmeasured in the production contract; do not replace it with
  // an obsolete nested measurement merely because null is falsy.
  historical.queueWaitMs = null; historical.durationMs = null;
  // A fresh fixture boot exercises the null shape without changing execution.
  const second = await setup({ storage: { "aidd.compare.lastRunId": "fixture-run-1" }, fetch(request) {
    if (request.url.endsWith("/fixture-run-1")) return response(historical);
  } });
  assert.match(second.elements.compareResourceStatus.textContent, /入队等待：未测量.*总耗时：未测量/);
  assert.doesNotMatch(second.elements.compareResourceStatus.textContent, /9999|8888/);
});

test("software fixture: search bounds match API maximum and model stage status updates without metric changes", async () => {
  let polls = 0;
  const progress = run({ status: "running" }); progress.models["ollama-qwen-local"] = { status: "pending", metrics: {} };
  const ui = await setup({ fetch(request) {
    if (request.url.startsWith("/api/research/compare/runs?")) return response({ contractVersion, runs: [], offset: 0, limit: 10, total: 0, hasMore: false });
    if (request.method === "GET" && request.url.endsWith("/fixture-run-1")) {
      const result = JSON.parse(JSON.stringify(progress));
      result.models["ollama-qwen-local"].status = ++polls === 1 ? "pending" : "running";
      return response(result);
    }
  } });
  assert.match(html, /id="compareHistoryQuery"[^>]*maxlength="120"/);
  ui.elements.compareHistoryQuery.value = "q".repeat(130);
  await ui.elements.compareHistorySearch.fire("click"); await flush();
  const history = ui.requests.find((request) => request.url.startsWith("/api/research/compare/runs?"));
  assert.equal(decodeURIComponent(history.url.split("&q=")[1]).length, 120);
  ui.elements.compareSmiles.value = "CCO"; await ui.elements.compareForm.fire("submit"); await flush();
  const row = () => ui.elements.compareMetricsRows.children[2].textContent;
  assert.match(row(), /排队中 \/ 排队 未测量/);
  await ui.pollOnce();
  assert.match(row(), /正在运行 \/ 排队 未测量/);
});

test("software fixture: queue hint excludes unselected or unauthorized stages without rewriting server statuses", async () => {
  for (const { enabled, authorized } of [{ enabled: false, authorized: false }, { enabled: true, authorized: false }, { enabled: true, authorized: true }]) {
    const progress = run({ status: "running", resourceState: { activeModel: "toxnet-v3-b16", blocked: false } });
    progress.request = Core.buildRequest({ smiles: "CCO", specialistTask: authorized ? "tdc-fixture-task" : null,
      comparisonEnabled: enabled, allowCloudTransfer: authorized });
    progress.models["toxnet-v3-b16"].status = "running";
    progress.models["txgemma-2b-predict"] = { status: "pending" };
    progress.models["ollama-qwen-local"] = { status: "pending" };
    progress.models["deepseek-api"] = { status: "pending" };
    const ui = await setup({ storage: { "aidd.compare.lastRunId": "fixture-run-1" }, fetch(request) {
      if (request.url.endsWith("/fixture-run-1")) return response(progress);
    } });
    const hint = ui.elements.compareResourceStatus.textContent;
    assert.match(hint, /等待阶段：.*Qwen · Ollama/);
    if (authorized) {
      assert.match(hint, /等待阶段：TxGemma 2B → Qwen · Ollama → DeepSeek API/);
    } else {
      assert.doesNotMatch(hint, /TxGemma|DeepSeek/);
    }
    const cards = ui.elements.compareModelCards.children;
    assert.equal(cards.find((card) => card.dataset.modelId === "txgemma-2b-predict").dataset.state, "pending");
    assert.equal(cards.find((card) => card.dataset.modelId === "deepseek-api").dataset.state, "pending");
    assert.ok(ui.requests.every((request) => request.method === "GET"), "Reading queue status cannot invoke a model or cloud transfer");
  }
});
