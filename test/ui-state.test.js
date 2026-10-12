"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "..", "app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");

class Element {
  constructor(id = "") {
    this.id = id;
    this.value = "";
    this.textContent = "";
    this.hidden = !["emptyState"].includes(id);
    this.disabled = id === "exportButton";
    this.events = {};
    this.attributesMap = {};
    this.children = [];
    this.dataset = {};
    this.offsetTop = 0;
    this.htmlWrites = 0;
    this.classes = new Set();
    this.classList = { toggle: (name, on) => on ? this.classes.add(name) : this.classes.delete(name) };
    this.style = { setProperty: (name, value) => { this.attributesMap[name] = value; } };
  }
  set innerHTML(value) {
    this.markup = value;
    this.htmlWrites += 1;
    if (this.id === "sampleList") {
      this.buttons = [...value.matchAll(/data-sample-id="([^"]+)"/g)].map((match) => {
        const button = new Element();
        button.dataset.sampleId = match[1];
        return button;
      });
    }
  }
  get innerHTML() { return this.markup || ""; }
  addEventListener(name, callback) { this.events[name] = callback; }
  fire(name, event = {}) { return this.events[name]?.({ preventDefault() {}, ...event }); }
  setAttribute(name, value) { this.attributesMap[name] = value; }
  removeAttribute(name) { delete this.attributesMap[name]; }
  get attributes() { return Object.entries(this.attributesMap).map(([name, value]) => ({ name, value })); }
  querySelectorAll() { return this.id === "sampleList" ? this.buttons : []; }
  closest() { return this; }
  replaceChildren(...children) { this.children = children; }
  appendChild(child) { this.children.push(child); child.parent = this; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((child) => child !== this); }
  click() { this.clicked = true; }
  focus() { this.focused = true; }
  showModal() { this.open = true; }
  close() { this.open = false; this.fire("close"); }
  reportValidity() { return true; }
}

class SvgParser {
  parseFromString(markup) {
    const svg = new Element();
    svg.localName = markup.includes("<svg") ? "svg" : "invalid";
    svg.sourceMarkup = markup;
    return { documentElement: svg, querySelector() { return null; } };
  }
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((accept, fail) => { resolve = accept; reject = fail; });
  return { promise, resolve, reject };
}
const flush = () => new Promise((resolve) => setImmediate(resolve));
function health(overrides = {}) {
  return {
    status: "ok", contractVersion: "toxilens.api.v2",
    chemistry: { rdkitVersion: "2026.03", rulesVersion: "rules.v2" },
    model: { configured: true, modelName: "test-model", version: "model.v1" },
    modelProviders: [
      { id: "chatgpt", label: "ChatGPT", configured: true, modelName: "test-model", version: "model.v1" },
      { id: "deepseek", label: "DeepSeek", configured: true, modelName: "test-deepseek", version: "model.v2" },
      { id: "qwen", label: "千问", configured: false },
    ],
    defaultProvider: "chatgpt", capabilities: { rules: true, model: true }, ...overrides,
  };
}
function report(input = "CCO", mode = "model") {
  return {
    input, canonicalSmiles: input, structureId: `structure:${input}`, known: null,
    generatedAt: "2026-10-11T06:00:00Z", overall: mode === "model" ? 0.75 : 0.21,
    tier: { id: "high", label: "高风险", action: "实验复核" }, evidenceLabel: mode === "model" ? "真实模型输出" : "规则基线",
    scoreKind: "uncalibrated_score", moleculeSvg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 560 260"><path d="M0 0L20 20" /></svg>',
    endpoints: [{ id: "dili", short: "DILI", label: "药物性肝损伤", source: "TDC DILI", description: "肝毒性倾向",
      score: mode === "model" ? 0.8 : 0.21, modelScore: mode === "model" ? 0.8 : null,
      ruleScore: 0.21, referenceScore: 0.78, scoreSource: mode, explanationSource: mode === "model" ? "none" : "structural_rules",
      evidence: mode === "model" ? "模型输出" : "结构规则", drivers: [],
      ruleDrivers: [{ label: "规则碱性胺", detail: "只解释规则分数", contribution: 0.02 }],
    }],
    descriptors: { heavyAtoms: 3, molecularWeight: 46.069, logP: -0.0014, tpsa: 20.23, hbd: 1, hba: 1, rings: 0, rotatableBonds: 0, basicAmines: 0 }, alerts: [],
    calibration: { status: "unknown" }, applicability: { status: "unknown" },
    provenance: { contractVersion: "toxilens.api.v2", apiVersion: "v2", mode,
      chemistry: { rdkitVersion: "2026.03", version: "chemistry.v2" }, rules: { version: "rules.v2" },
      model: mode === "model" ? { provider: "chatgpt", name: "test-model", version: "model.v1", requestId: "request-123", scoreKind: "uncalibrated_score", promptVersion: "prompt.v1" } : null,
    },
  };
}

async function setup(options = {}) {
  const ids = [...html.matchAll(/id="([^"]+)"/g)].map((match) => match[1]);
  const elements = Object.fromEntries(ids.map((id) => [id, new Element(id)]));
  elements.modeSelect.value = "model";
  elements.providerSelect.value = "custom";
  const requests = [], blobs = [], timers = [];
  const document = {
    body: new Element("body"), activeElement: null,
    getElementById(id) { assert.ok(elements[id], `Missing HTML element ${id}`); return elements[id]; },
    createElement() { return new Element(); }, importNode(node) { return node; },
  };
  const core = {
    getHealth: options.getHealth || (() => Promise.resolve(options.health || health())),
    getProviderConfig: options.getProviderConfig || (() => Promise.resolve(providerConfig())),
    saveProviderConfig: options.saveProviderConfig || ((provider, config) => Promise.resolve({ ...config, providerId: provider, hasApiKey: true })),
    getLocalModelInfo: options.getLocalModelInfo || (() => Promise.resolve(localModelInfo())),
    analyzeSmiles(smiles, analysisOptions) {
      const pending = deferred();
      requests.push({ smiles, options: analysisOptions, ...pending });
      return pending.promise;
    }, riskTier() { return { id: "high", label: "高风险", action: "实验复核" }; },
  };
  vm.runInNewContext(source, {
    window: { ToxicityCore: core, location: { protocol: options.protocol || "http:" }, innerWidth: 1200,
      setTimeout(callback) { timers.push(callback); }, scrollTo() {} },
    document, AbortController, Blob, Intl, DOMParser: SvgParser,
    URL: { createObjectURL(blob) { blobs.push(blob); return "blob:test"; }, revokeObjectURL() {} },
  }, { filename: "app.js" });
  await flush();
  return { elements, requests, blobs, timers, document };
}
function providerConfig(overrides = {}) {
  return { apiUrl: "https://example.com/v1/chat/completions", modelName: "test-model", modelVersion: "model.v1", apiFormat: "openai", scoreKind: "uncalibrated_score", timeoutSeconds: 30, temperature: null, sendImage: false, jsonMode: true, hasApiKey: true, ...overrides };
}
function localHealth(configured = true) {
  return health({ defaultProvider: "local", modelProviders: [
    { id: "local", label: "蓝研 ToxNet · 自训练", configured, localModel: true, modelName: "蓝研 ToxNet", version: "trained.v1", supportsImage: false },
    ...health().modelProviders,
  ] });
}
function localModelInfo(overrides = {}) {
  return { status: "trained", name: "蓝研 ToxNet", version: "trained.v1",
    architecture: { name: "Molecular neural network", parameterCount: 123456, input: "Morgan fingerprint", description: "独立训练的分子分类器" },
    data: { version: "dataset.v1", sources: [{ name: "verified-dataset", url: "https://example.com/data", endpoint: "Ames" }], splits: { train: 800, validation: 100, test: 100 }, splitMethod: "scaffold" },
    endpoints: [{ id: "ames", label: "Ames", counts: { train: 800, validation: 100, test: 100 }, metrics: { auroc: 0.7123, averagePrecision: 0.6891, brier: 0.2045 }, calibration: { status: "validated" } }],
    calibration: { status: "validated", method: "Platt", evaluationSet: "test" }, applicability: { method: "Morgan/Tanimoto", threshold: 0.3, detail: "结构相似度启发式，尚无置信度验证。" }, ...overrides };
}
function start(ui, smiles = "CCO") {
  ui.elements.smilesInput.value = smiles;
  return ui.elements.analyzeButton.fire("click");
}
async function complete(ui, smiles = "CCO", mode = "model") {
  const pending = start(ui, smiles);
  ui.requests.at(-1).resolve(report(smiles, mode));
  await pending;
}

test("API configuration reads the selected provider without displaying a saved secret", async () => {
  let selected;
  const ui = await setup({ getProviderConfig(provider) { selected = provider; return Promise.resolve(providerConfig()); } });
  ui.elements.providerSelect.value = "deepseek";
  ui.elements.providerSelect.fire("change");
  await ui.elements.configApiButton.fire("click");
  assert.equal(selected, "deepseek");
  assert.equal(ui.elements.apiConfigDialog.open, true);
  assert.match(ui.elements.apiConfigTitle.textContent, /DeepSeek/);
  assert.equal(ui.elements.configModelName.value, "test-model");
  assert.equal(ui.elements.configApiKey.value, "");
  assert.equal(ui.elements.configApiKey.required, false);
  assert.match(ui.elements.configKeyHint.textContent, /已保存/);
  assert.equal(ui.elements.configScoreKind.disabled, true);
  assert.equal(ui.elements.configApiFormat.disabled, false);
});

test("local model uses a separate details dialog, real training metadata and no API key configuration", async () => {
  let externalConfigCalls = 0;
  const ui = await setup({ health: localHealth(), getProviderConfig() { externalConfigCalls += 1; return Promise.resolve(providerConfig()); } });
  assert.equal(ui.elements.providerSelect.value, "local");
  assert.equal(ui.elements.configApiButton.textContent, "模型详情");
  assert.equal(ui.elements.configApiButton.attributesMap["aria-controls"], "localModelDialog");
  assert.match(ui.elements.modeHint.textContent, /本机独立训练/);
  await ui.elements.configApiButton.fire("click");
  assert.equal(externalConfigCalls, 0);
  assert.equal(ui.elements.localModelDialog.open, true);
  assert.notEqual(ui.elements.apiConfigDialog.open, true);
  const detail = ui.elements.localModelContent.innerHTML;
  assert.match(detail, /123,456/);
  assert.match(detail, /verified-dataset/);
  assert.match(detail, /0\.712/);
  assert.match(detail, /0\.689/);
  assert.match(detail, /0\.204/);
  assert.match(detail, /结构相似度启发式/);
  const pending = start(ui);
  assert.equal(ui.requests[0].options.provider, "local");
  ui.requests[0].resolve(report());
  await pending;
});

test("untrained local model reports missing training honestly and cannot call analysis", async () => {
  const ui = await setup({ health: localHealth(false), getLocalModelInfo() { return Promise.resolve(localModelInfo({ status: "untrained", architecture: {}, data: {}, endpoints: [], calibration: {} })); } });
  assert.match(ui.elements.providerSelect.innerHTML, /未训练/);
  await start(ui);
  assert.equal(ui.requests.length, 0);
  assert.match(ui.elements.errorMessage.textContent, /尚未完成训练/);
  await ui.elements.configApiButton.fire("click");
  assert.match(ui.elements.localModelStatus.textContent, /尚无可用的训练模型/);
  assert.match(ui.elements.localModelContent.innerHTML, /尚无可用的留出测试集评估/);
  assert.doesNotMatch(ui.elements.localModelContent.innerHTML, /0\.712/);
});

test("external source evaluation stays distinct from internal metrics and escapes its limitations", async () => {
  const ui = await setup({ health: localHealth(), getLocalModelInfo() { return Promise.resolve(localModelInfo({
    externalEvaluation: { datasetName: "TDC hERG_Karim", neuralModel: { count: 9978, positives: 4886, auroc: 0.643476, averagePrecision: 0.625562, brier: 0.334936, ece10Bins: 0.311605 },
      fingerprintBaseline: { auroc: 0.647018, averagePrecision: 0.642957, brier: 0.346826, ece10Bins: 0.314246 },
      meaning: "外部来源回顾性评估，来源人群独立性未验证。", limitation: "<script>unsafe</script>当前仍为研究原型", evidenceSha256: "external-test-hash" },
  })); } });
  await ui.elements.configApiButton.fire("click");
  const markup = ui.elements.localModelContent.innerHTML;
  assert.match(markup, /四端点评估/);
  assert.match(markup, /外部来源回顾性评估/);
  assert.match(markup, /9,978/);
  assert.match(markup, /0\.643/);
  assert.match(markup, /0\.647/);
  assert.match(markup, /来源人群独立性未验证/);
  assert.match(markup, /external-test-hash/);
  assert.doesNotMatch(markup, /<script>/);
});

test("closing model details aborts loading and ignores late metadata", async () => {
  const pending = deferred();
  let signal;
  const ui = await setup({ health: localHealth(), getLocalModelInfo(options) { signal = options.signal; return pending.promise; } });
  const loading = ui.elements.configApiButton.fire("click");
  ui.elements.closeLocalModelButton.fire("click");
  assert.equal(signal.aborted, true);
  pending.resolve(localModelInfo());
  await loading;
  assert.equal(ui.elements.localModelDialog.open, false);
  assert.equal(ui.elements.localModelContent.hidden, true);
  assert.doesNotMatch(ui.elements.localModelContent.innerHTML, /verified-dataset/);
});

test("local PyTorch artifact contract displays measured metrics without claiming certified calibration", async () => {
  const ui = await setup({ health: localHealth(), getLocalModelInfo() { return Promise.resolve({
    status: "ready", configured: true, modelName: "蓝研 ToxNet", version: "artifact.v1", parameterCount: 78910,
    architecture: { type: "Multitask MLP" }, endpoints: ["herg", "dili", "ames", "clintox"],
    datasetProvenance: [{ dataset: "TDC Ames", url: "https://example.com/ames", count: 1000 }],
    training: { splits: { train: 900, validation: 120, test: 115 }, uniqueMoleculeCounts: { train: 800, validation: 100, test: 100 }, classCounts: { train: { ames: { positive: 450, negative: 350 } }, validation: { ames: { positive: 55, negative: 45 } }, test: { ames: { positive: 45, negative: 55 } } } },
    evaluation: { split: "test", endpoints: { ames: { count: 100, positives: 45, auroc: 0.75, averagePrecision: 0.70, brier: 0.20 } } },
    calibration: { method: "temperature scaling", fitSplit: "validation", status: "measured-not-certified", certified: false },
    applicability: { method: "Morgan/Tanimoto", threshold: 0.3, validated: false }, artifactSha256: "artifact-hash",
  }); } });
  await ui.elements.configApiButton.fire("click");
  assert.match(ui.elements.localModelStatus.textContent, /模型已训练/);
  const markup = ui.elements.localModelContent.innerHTML;
  assert.match(markup, /TDC Ames/);
  assert.match(markup, /78,910/);
  assert.match(markup, /0\.750/);
  assert.match(markup, /已评估，未认证/);
  assert.match(markup, /artifact-hash/);
  assert.match(markup, /独立分子数/);
  assert.match(markup, /<th scope="row">Ames<\/th><td>800<\/td><td>100<\/td><td>100<\/td><td>45<\/td>/);
  assert.doesNotMatch(markup, /<strong>900<\/strong>/);
  const pending = start(ui);
  const result = report();
  result.calibration = { status: "measured-not-certified" };
  result.applicability = { status: "similarity-heuristic", maxTanimoto: 0.246, insideThreshold: false };
  ui.requests[0].resolve(result);
  await pending;
  assert.match(ui.elements.validationStatus.textContent, /已报告校准评估，拟合状态未提供，尚未认证/);
  assert.match(ui.elements.validationStatus.textContent, /低于参考阈值（0\.246）/);
  assert.match(ui.elements.validationStatus.textContent, /适用域未验证/);
  assert.doesNotMatch(ui.elements.validationStatus.textContent, /域内|校准已验证/);
});

test("partial calibration reports successful and failed fits separately and exposes training limitations", async () => {
  const calibration = { status: "measured-not-certified", fitSplit: "validation", fitStatus: { herg: "fitted", dili: "not-fitted-insufficient-classes", ames: "fitted", clintox: "not-fitted-optimizer-failure" } };
  const ui = await setup({ health: localHealth(), getLocalModelInfo() { return Promise.resolve({
    ...localModelInfo(), endpoints: ["herg", "dili", "ames", "clintox"], calibration,
    limitations: ["只有一次 scaffold 内部测试，未做外部验证", "ClinTox 阳性样本少", "<script>untrusted limitation</script>"],
    artifactSha256: "manifest-checksum", weightsSha256: "weights-checksum",
  }); } });
  await ui.elements.configApiButton.fire("click");
  const markup = ui.elements.localModelContent.innerHTML;
  assert.match(markup, /4 个端点中 2 个已拟合校准器/);
  assert.match(markup, /验证集类别不足，未拟合/);
  assert.match(markup, /优化失败，未拟合/);
  assert.match(markup, /已拟合，内部评估/);
  assert.match(markup, /只有一次 scaffold 内部测试，未做外部验证/);
  assert.match(markup, /ClinTox 阳性样本少/);
  assert.match(markup, /&lt;script&gt;untrusted limitation&lt;\/script&gt;/);
  assert.match(markup, /模型清单 SHA256：manifest-checksum/);
  assert.match(markup, /模型权重 SHA256：weights-checksum/);
  const pending = start(ui);
  const result = report();
  result.calibration = calibration;
  ui.requests[0].resolve(result);
  await pending;
  assert.match(ui.elements.validationStatus.textContent, /4 个端点中 2 个已拟合校准器/);
  assert.doesNotMatch(ui.elements.validationStatus.textContent, /所有端点已拟合/);
});

test("calibration summary never claims a fitted calibrator when every fit failed", async () => {
  const ui = await setup({ health: localHealth() });
  const pending = start(ui);
  const result = report();
  result.calibration = { status: "measured-not-certified", fitStatus: { herg: "not-fitted-optimizer-failure", dili: "not-fitted-insufficient-classes", ames: "not-fitted-insufficient-classes", clintox: "not-fitted-optimizer-failure" } };
  ui.requests[0].resolve(result);
  await pending;
  assert.match(ui.elements.validationStatus.textContent, /校准器未拟合；原始模型概率/);
  assert.doesNotMatch(ui.elements.validationStatus.textContent, /已拟合/);
});

test("fully fitted calibration remains an internal evaluation rather than external validation", async () => {
  const ui = await setup({ health: localHealth() });
  const pending = start(ui);
  const result = report();
  result.calibration = { status: "measured-not-certified", fitStatus: { herg: "fitted", dili: "fitted", ames: "fitted", clintox: "fitted" } };
  ui.requests[0].resolve(result);
  await pending;
  assert.match(ui.elements.validationStatus.textContent, /所有端点已拟合校准器并在内部测试集评估，尚未认证/);
  assert.doesNotMatch(ui.elements.validationStatus.textContent, /校准已验证|外部验证/);
});

test("model metadata escapes untrusted labels and does not activate unsafe source URLs", async () => {
  const ui = await setup({ health: localHealth(), getLocalModelInfo() { return Promise.resolve(localModelInfo({ architecture: { name: "<img onerror=alert(1)>" }, data: { sources: [{ name: "<script>unsafe</script>", url: "javascript:alert(1)" }] } })); } });
  await ui.elements.configApiButton.fire("click");
  assert.match(ui.elements.localModelContent.innerHTML, /&lt;img/);
  assert.match(ui.elements.localModelContent.innerHTML, /&lt;script&gt;/);
  assert.doesNotMatch(ui.elements.localModelContent.innerHTML, /href="javascript:/);
});

test("closing API configuration discards a typed key and ignores late config responses", async () => {
  const pending = deferred();
  const ui = await setup({ getProviderConfig() { return pending.promise; } });
  const loading = ui.elements.configApiButton.fire("click");
  ui.elements.configApiKey.value = "typed-secret";
  ui.elements.cancelConfigButton.fire("click");
  pending.resolve(providerConfig({ modelName: "late-model" }));
  await loading;
  assert.equal(ui.elements.apiConfigDialog.open, false);
  assert.equal(ui.elements.configApiKey.value, "");
  assert.notEqual(ui.elements.configModelName.value, "late-model");
});

test("saving API configuration preserves an omitted key and invalidates old model reports", async () => {
  let submitted;
  const ui = await setup({ saveProviderConfig(provider, config) { submitted = { provider, config }; return Promise.resolve(providerConfig()); } });
  await complete(ui);
  await ui.elements.configApiButton.fire("click");
  ui.elements.configModelName.value = "new-model";
  await ui.elements.apiConfigForm.fire("submit");
  assert.equal(submitted.config.modelName, "new-model");
  assert.equal("apiKey" in submitted.config, false);
  assert.equal(submitted.config.temperature, null);
  assert.equal(ui.elements.exportButton.disabled, true);
  assert.match(ui.elements.inputStatus.textContent, /API 配置已修改/);
  assert.match(ui.elements.configStatus.textContent, /已保存并立即生效/);
  ui.elements.smilesInput.fire("input");
  assert.equal(ui.elements.exportButton.disabled, true);
});

test("successful key replacement clears the field and cancels pending model analysis", async () => {
  let receivedKey;
  const ui = await setup({ saveProviderConfig(provider, config) { receivedKey = config.apiKey; return Promise.resolve(providerConfig()); } });
  const pending = start(ui);
  await ui.elements.configApiButton.fire("click");
  ui.elements.configApiKey.value = "new-secret";
  await ui.elements.apiConfigForm.fire("submit");
  assert.equal(receivedKey, "new-secret");
  assert.equal(ui.elements.configApiKey.value, "");
  assert.equal(ui.requests[0].options.signal.aborted, true);
  ui.requests[0].resolve(report());
  await pending;
  assert.equal(ui.elements.report.hidden, true);
});

test("API save failures remain editable and leave the current report valid", async () => {
  const ui = await setup({ saveProviderConfig() { return Promise.reject(new Error("API 地址无效")); } });
  await complete(ui);
  await ui.elements.configApiButton.fire("click");
  await ui.elements.apiConfigForm.fire("submit");
  assert.match(ui.elements.configStatus.textContent, /API 地址无效/);
  assert.equal(ui.elements.saveConfigButton.disabled, false);
  assert.equal(ui.elements.cancelConfigButton.disabled, false);
  assert.equal(ui.elements.exportButton.disabled, false);
});

test("reset aborts an analysis and ignores a late response", async () => {
  const ui = await setup();
  const pending = start(ui);
  ui.elements.resetButton.fire("click");
  assert.equal(ui.requests[0].options.signal.aborted, true);
  ui.requests[0].resolve(report());
  await pending;
  assert.equal(ui.elements.smilesInput.value, "");
  assert.equal(ui.elements.emptyState.hidden, false);
  assert.equal(ui.elements.report.hidden, true);
  assert.equal(ui.elements.exportButton.disabled, true);
});

test("a late response cannot overwrite a newer analysis", async () => {
  const ui = await setup();
  const old = start(ui, "CCO");
  const latest = start(ui, "CC#N");
  assert.equal(ui.requests[0].options.signal.aborted, true);
  ui.requests[1].resolve(report("CC#N"));
  await latest;
  ui.requests[0].resolve(report("CCO"));
  await old;
  assert.equal(ui.elements.reportSubtitle.textContent, "CC#N");
  assert.equal(ui.elements.exportButton.disabled, false);
});

test("an old request failure cannot end the latest loading state", async () => {
  const ui = await setup();
  const old = start(ui);
  const latest = start(ui, "CC#N");
  ui.requests[0].reject(new Error("old request failed"));
  await old;
  assert.equal(ui.elements.analyzeButton.disabled, true);
  assert.equal(ui.elements.loadingState.hidden, false);
  ui.requests[1].resolve(report("CC#N"));
  await latest;
  assert.equal(ui.elements.report.hidden, false);
});

test("input edits mark a report dirty and prevent even programmatic export", async () => {
  const ui = await setup();
  await complete(ui);
  ui.elements.smilesInput.value = "CC#N";
  ui.elements.smilesInput.fire("input");
  assert.equal(ui.elements.exportButton.disabled, true);
  assert.match(ui.elements.inputStatus.textContent, /重新筛选/);
  ui.elements.exportButton.fire("click");
  assert.equal(ui.blobs.length, 0);
  ui.elements.smilesInput.value = " CCO \n";
  ui.elements.smilesInput.fire("input");
  assert.equal(ui.elements.exportButton.disabled, false);
});

test("internal SMILES whitespace is a changed input and cannot export the old report", async () => {
  const ui = await setup();
  await complete(ui);
  ui.elements.smilesInput.value = "C CO";
  ui.elements.smilesInput.fire("input");
  assert.equal(ui.elements.exportButton.disabled, true);
  assert.match(ui.elements.inputStatus.textContent, /重新筛选/);
  ui.elements.exportButton.fire("click");
  assert.equal(ui.blobs.length, 0);
});

test("editing during analysis aborts the pending response", async () => {
  const ui = await setup();
  const pending = start(ui);
  ui.elements.smilesInput.value = "CC#N";
  ui.elements.smilesInput.fire("input");
  assert.equal(ui.requests[0].options.signal.aborted, true);
  ui.requests[0].resolve(report());
  await pending;
  assert.equal(ui.elements.report.hidden, true);
  assert.equal(ui.elements.exportButton.disabled, true);
  assert.equal(ui.elements.analyzeButton.disabled, false);
});

test("mode changes abort pending requests and use the explicit rule mode", async () => {
  const ui = await setup();
  const pending = start(ui);
  ui.elements.modeSelect.value = "rules";
  ui.elements.modeSelect.fire("change");
  assert.equal(ui.requests[0].options.signal.aborted, true);
  ui.requests[0].resolve(report());
  await pending;
  const rules = start(ui);
  assert.equal(ui.requests[1].options.mode, "rules");
  ui.requests[1].resolve(report("CCO", "rules"));
  await rules;
  assert.match(ui.elements.endpointGrid.innerHTML, /规则解释/);
  assert.doesNotMatch(ui.elements.endpointGrid.innerHTML, /模型解释/);
  assert.equal(ui.elements.providerSelect.disabled, true);
});

test("health selects the default provider and provider changes make a report dirty", async () => {
  const ui = await setup({ health: health({ defaultProvider: "deepseek" }) });
  assert.equal(ui.elements.providerSelect.value, "deepseek");
  const pending = start(ui);
  assert.equal(ui.requests[0].options.provider, "deepseek");
  ui.requests[0].resolve(report());
  await pending;
  ui.elements.providerSelect.value = "chatgpt";
  ui.elements.providerSelect.fire("change");
  assert.equal(ui.elements.exportButton.disabled, true);
  assert.match(ui.elements.inputStatus.textContent, /供应商已修改/);
});

test("provider changes abort pending model requests", async () => {
  const ui = await setup();
  const pending = start(ui);
  ui.elements.providerSelect.value = "deepseek";
  ui.elements.providerSelect.fire("change");
  assert.equal(ui.requests[0].options.signal.aborted, true);
  ui.requests[0].resolve(report());
  await pending;
  assert.equal(ui.elements.report.hidden, true);
  const latest = start(ui);
  assert.equal(ui.requests[1].options.provider, "deepseek");
  ui.requests[1].resolve(report());
  await latest;
});

test("health provider changes neither dirty a rule report nor abort pending rule analysis", async () => {
  let healthCalls = 0;
  const ui = await setup({ getHealth() {
    healthCalls += 1;
    const provider = healthCalls === 2 ? "deepseek" : "chatgpt";
    return Promise.resolve(health({ defaultProvider: provider,
      modelProviders: [{ id: provider, label: provider, configured: true, version: "model.v1" }],
    }));
  } });
  ui.elements.modeSelect.value = "rules";
  ui.elements.modeSelect.fire("change");
  await complete(ui, "CCO", "rules");
  await ui.elements.healthButton.fire("click");
  assert.equal(ui.elements.providerSelect.value, "deepseek");
  assert.equal(ui.elements.exportButton.disabled, false);
  assert.doesNotMatch(ui.elements.inputStatus.textContent, /重新筛选/);
  const pending = start(ui, "CCN");
  await ui.elements.healthButton.fire("click");
  assert.equal(ui.elements.providerSelect.value, "chatgpt");
  assert.equal(ui.requests.at(-1).options.signal.aborted, false);
  ui.requests.at(-1).resolve(report("CCN", "rules"));
  await pending;
  assert.equal(ui.elements.reportSubtitle.textContent, "CCN");
});

test("an unconfigured provider gives a clear error and never falls back to rules", async () => {
  const ui = await setup();
  ui.elements.providerSelect.value = "qwen";
  ui.elements.providerSelect.fire("change");
  await start(ui);
  assert.equal(ui.requests.length, 0);
  assert.match(ui.elements.errorMessage.textContent, /尚未配置/);
  ui.elements.modeSelect.value = "rules";
  ui.elements.modeSelect.fire("change");
  const pending = start(ui);
  assert.equal(ui.requests[0].options.mode, "rules");
  ui.requests[0].resolve(report("CCO", "rules"));
  await pending;
});

test("model failures remain errors instead of silently running rules", async () => {
  const ui = await setup();
  const pending = start(ui);
  ui.requests[0].reject(new Error("Model provider unavailable"));
  await pending;
  assert.equal(ui.requests.length, 1);
  assert.match(ui.elements.errorMessage.textContent, /Model provider unavailable/);
  assert.equal(ui.elements.errorState.hidden, false);
  assert.equal(ui.elements.exportButton.disabled, true);
});

test("report distinguishes model, rule and reference scores and explanation provenance", async () => {
  const ui = await setup();
  await complete(ui);
  const markup = ui.elements.endpointGrid.innerHTML;
  assert.match(markup, /<dt>模型<\/dt><dd>80 \/ 100<\/dd>/);
  assert.match(markup, /<dt>规则基线<\/dt><dd>21 \/ 100<\/dd>/);
  assert.match(markup, /<dt>示例参考<\/dt><dd>78 \/ 100<\/dd>/);
  assert.match(markup, /模型未提供解释/);
  assert.match(markup, /规则基线说明（仅解释规则分数）/);
  assert.match(ui.elements.scoreContext.textContent, /不代表毒性发生概率/);
  assert.equal(ui.elements.validationStatus.textContent, "校准未验证 · 适用域未知");
  assert.equal(ui.elements.moleculeView.children[0].sourceMarkup, report().moleculeSvg);
});

test("evidence uses the actual model version and supports flat chemistry provenance", async () => {
  const ui = await setup();
  const pending = start(ui);
  const result = report();
  result.provenance = {
    chemistryVersion: "chemistry.actual", rdkitVersion: "RDKit.actual", rulesVersion: "rules.actual",
    model: { name: "remote-model", modelVersion: "model.actual", version: "model.configured" },
  };
  ui.requests[0].resolve(result);
  await pending;
  assert.match(ui.elements.evidenceFlow.innerHTML, /remote-model · model.actual/);
  assert.match(ui.elements.evidenceFlow.innerHTML, /RDKit RDKit.actual/);
  assert.doesNotMatch(ui.elements.evidenceFlow.innerHTML, /model.configured/);
  ui.elements.modeSelect.value = "rules";
  ui.elements.modeSelect.fire("change");
  const rulesPending = start(ui);
  const rulesResult = report("CCO", "rules");
  rulesResult.provenance = { rulesVersion: "rules.actual", rdkitVersion: "RDKit.actual" };
  ui.requests.at(-1).resolve(rulesResult);
  await rulesPending;
  assert.match(ui.elements.evidenceFlow.innerHTML, /规则版本 rules.actual/);
});

test("audit exports the full provenance and separate scores without credential fields", async () => {
  const ui = await setup();
  const pending = start(ui);
  const result = report();
  result.provenance.model.apiKey = "sensitive-key";
  result.provenance.model.token = "sensitive-token";
  result.provenance.model.authorization = "Bearer sensitive-key";
  ui.requests[0].resolve(result);
  await pending;
  ui.elements.exportButton.fire("click");
  assert.equal(ui.blobs.length, 1);
  const raw = await ui.blobs[0].text();
  const packet = JSON.parse(raw);
  assert.equal(packet.schema, "toxilens.audit.v2");
  assert.equal(packet.provider, "chatgpt");
  assert.equal(packet.input.canonicalSmiles, "CCO");
  assert.equal(packet.input.structureId, "structure:CCO");
  assert.equal(packet.provenance.model.requestId, "request-123");
  assert.equal(packet.provenance.model.promptVersion, "prompt.v1");
  assert.equal(packet.provenance.chemistry.rdkitVersion, "2026.03");
  assert.equal(packet.endpoints[0].modelScore, 0.8);
  assert.equal(packet.endpoints[0].ruleScore, 0.21);
  assert.equal(packet.endpoints[0].referenceScore, 0.78);
  assert.equal(packet.overall.kind, "aggregate_screening_index");
  assert.doesNotMatch(raw, /sensitive-key|sensitive-token|apiKey|authorization/);
});

test("sample selection preserves the existing focused button and native button semantics", async () => {
  const ui = await setup();
  const button = ui.elements.sampleList.buttons.find((item) => item.dataset.sampleId === "ethanol");
  ui.document.activeElement = button;
  ui.elements.sampleList.fire("click", { target: button });
  assert.equal(ui.elements.sampleList.htmlWrites, 1);
  assert.equal(ui.elements.sampleList.buttons.find((item) => item.dataset.sampleId === "ethanol"), button);
  assert.equal(ui.document.activeElement, button);
  assert.equal(button.attributesMap["aria-pressed"], "true");
  assert.doesNotMatch(ui.elements.sampleList.innerHTML, /<button[^>]+role="listitem"/);
  ui.requests[0].resolve(report());
  await flush();
});

test("file pages explain how to connect to the API and do not request analysis", async () => {
  let healthCalls = 0;
  const ui = await setup({ protocol: "file:", getHealth() { healthCalls += 1; return Promise.resolve(health()); } });
  await start(ui);
  assert.equal(healthCalls, 0);
  assert.equal(ui.requests.length, 0);
  assert.match(ui.elements.errorMessage.textContent, /http:\/\/localhost:4173/);
  assert.match(ui.elements.apiStatus.textContent, /需要启动 API/);
});

test("invalid structure responses leave an error and cannot be exported", async () => {
  const ui = await setup();
  const pending = start(ui);
  const result = report();
  result.moleculeSvg = "not an SVG";
  ui.requests[0].resolve(result);
  await pending;
  assert.equal(ui.elements.errorState.hidden, false);
  assert.match(ui.elements.errorMessage.textContent, /结构图格式无效/);
  assert.equal(ui.elements.exportButton.disabled, true);
});
