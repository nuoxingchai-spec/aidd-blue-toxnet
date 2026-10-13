(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.CompareCore = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const CONTRACT = "aidd.multimodel.compare.v1";
  const MODEL_IDS = ["toxnet-v3-b16", "txgemma-2b-predict", "ollama-qwen-local", "deepseek-api"];
  const ENDPOINT_IDS = ["herg", "dili", "ames", "clintox"];
  const TERMINAL = new Set(["completed", "partial", "failed", "cancelled"]);
  const RUN_STATES = new Set(["pending", "running", ...TERMINAL]);
  const MODEL_STATES = new Set(["pending", "running", "completed", "partial", "failed", "unavailable", "unsupported", "skipped", "cancelled"]);
  const ENDPOINT_NAMES = { herg: "hERG", dili: "DILI", ames: "Ames", clintox: "ClinTox" };
  const TASK_NAMES = { tox21_nr_ar: "NR-AR", tox21_nr_ar_lbd: "NR-AR-LBD", tox21_nr_ahr: "NR-AhR", tox21_nr_aromatase: "NR-Aromatase", tox21_nr_er: "NR-ER", tox21_nr_er_lbd: "NR-ER-LBD", tox21_nr_ppar_gamma: "NR-PPAR-γ", tox21_sr_are: "SR-ARE", tox21_sr_atad5: "SR-ATAD5", tox21_sr_hse: "SR-HSE", tox21_sr_mmp: "SR-MMP", tox21_sr_p53: "SR-p53" };

  function buildRequest(fields) {
    const smiles = String(fields.smiles || "").trim();
    const question = String(fields.question || "").trim();
    if (!smiles || smiles.length > 4096) throw new Error("请输入 1–4096 字符的 SMILES；化学合法性由 RDKit 验证。");
    if (question.length > 1000) throw new Error("研究问题请限制在 1000 字符以内。");
    const explainEndpoint = fields.explainEndpoint || null;
    if (explainEndpoint && !ENDPOINT_IDS.includes(explainEndpoint)) throw new Error("归因端点无效。");
    const evidenceEndpoint = fields.evidenceEndpoint || null;
    if (evidenceEndpoint && !ENDPOINT_IDS.includes(evidenceEndpoint)) throw new Error("实验检索端点无效。");
    const includeAttribution = Object.hasOwn(fields, "includeAttribution") ? fields.includeAttribution === true : Boolean(explainEndpoint);
    const cloud = fields.comparisonEnabled === true && fields.allowCloudTransfer === true;
    return {
      smiles, question, mode: "compare", primaryPredictor: "toxnet-v3-b16",
      specialistPredictor: "txgemma-2b-predict", specialistTask: fields.specialistTask || null,
      primaryReporter: "ollama-qwen-local", comparisonReporter: fields.comparisonEnabled === true ? "deepseek-api" : null,
      includeTox21: fields.includeTox21 !== false, includeEvidence: fields.includeEvidence === true,
      evidenceEndpoint, includeAttribution, explainEndpoint: includeAttribution ? explainEndpoint || "herg" : explainEndpoint,
      persistSensitiveContent: fields.persistSensitiveContent === true,
      allowCloudTransfer: cloud, ollamaModel: fields.ollamaModel || null,
    };
  }

  function fingerprint(request) { return JSON.stringify(request); }
  function initialState() { return { generation: 0, run: null, stale: false, inFlight: false, cancelling: false, fingerprint: null, error: null, viewingHistory: false, restoring: false }; }
  function current(state, event) {
    return event.generation === state.generation && (!event.runId || !state.run || event.runId === state.run.runId);
  }
  function reduce(state, event) {
    switch (event.type) {
      case "START": return { ...initialState(), generation: state.generation + 1, fingerprint: fingerprint(event.request), inFlight: true };
      case "RESTORE_REQUEST": return { ...initialState(), generation: state.generation + 1, viewingHistory: true, restoring: true,
        run: { contractVersion: CONTRACT, runId: event.runId, status: "pending", models: {} } };
      case "RESTORED":
        if (!current(state, event) || !state.restoring) return state;
        return { ...state, run: event.run, restoring: false, inFlight: !TERMINAL.has(event.run.status),
          fingerprint: event.request ? fingerprint(event.request) : null, error: null };
      case "ACK":
        if (!current(state, event) || !state.inFlight) return state;
        return { ...state, run: { contractVersion: CONTRACT, runId: event.runId, status: "pending", models: {} } };
      case "POLL":
        if (!current(state, event) || state.stale || state.cancelling || !state.inFlight) return state;
        return { ...state, run: event.run, inFlight: !TERMINAL.has(event.run.status), error: null };
      case "EDIT":
        if (!state.fingerprint && !state.run) return state;
        return { ...state, generation: state.generation + 1, stale: true, inFlight: false, cancelling: false, restoring: false, error: null };
      case "CANCEL_REQUEST":
        if (!state.run || TERMINAL.has(state.run.status) || state.stale || state.cancelling) return state;
        return { ...state, generation: state.generation + 1, inFlight: true, cancelling: true };
      case "CANCELLED":
        if (!current(state, event)) return state;
        return { ...state, run: event.run, inFlight: !TERMINAL.has(event.run.status), cancelling: false };
      case "ERROR":
        if (!current(state, event)) return state;
        return { ...state, error: event.message, inFlight: false, cancelling: false, restoring: false };
      case "RESET": return { ...initialState(), generation: state.generation + 1 };
      default: return state;
    }
  }

  function validateRegistry(value) {
    if (!value || value.contractVersion !== CONTRACT || !Array.isArray(value.models)) throw new Error("模型注册接口版本或格式不兼容。");
    const ids = new Set(value.models.map((item) => item.id));
    if (MODEL_IDS.some((id) => !ids.has(id))) throw new Error("注册接口未提供完整四模型信息。");
    return value;
  }
  function validateRun(value) {
    if (!value || value.contractVersion !== CONTRACT || typeof value.runId !== "string" || !RUN_STATES.has(value.status) || !value.models || typeof value.models !== "object" || Array.isArray(value.models)) throw new Error("运行状态接口格式不兼容。");
    for (const [id, item] of Object.entries(value.models)) {
      if (!MODEL_IDS.includes(id) || !item || !MODEL_STATES.has(item.status)) throw new Error("模型状态接口包含未知模型或状态。");
    }
    return value;
  }
  function completedModel(run, id) {
    const item = run && run.models && run.models[id];
    return item && ["completed", "partial"].includes(item.status) ? item : null;
  }
  function predictionData(run) {
    const item = completedModel(run, "toxnet-v3-b16");
    const data = item && item.data;
    return data && (data.prediction || data);
  }
  function scoreMap(run) {
    const data = predictionData(run);
    const endpoints = data && data.endpoints;
    return new Map((Array.isArray(endpoints) ? endpoints : []).map((item) => [item.id, item]));
  }
  function finiteScore(value) { return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1 ? value : null; }
  function endpointRows(run) {
    const scores = scoreMap(run);
    return ENDPOINT_IDS.map((id) => {
      const item = scores.get(id);
      return { id, name: ENDPOINT_NAMES[id], score: finiteScore(item && item.score), positiveClass: item && item.positiveClass || null };
    });
  }
  function pathwayRows(run) {
    const data = predictionData(run);
    const pathways = data && (data.pathways || data.tox21 && data.tox21.pathways);
    const scores = Array.isArray(pathways) ? new Map(pathways.map((item) => [item.id, item])) : scoreMap(run);
    return Object.keys(TASK_NAMES).map((id) => {
      const item = scores.get(id);
      return { id, name: TASK_NAMES[id], score: finiteScore(item && item.score), positiveClass: item && item.positiveClass || "assay active" };
    });
  }
  function installedOllamaModels(registry) {
    const model = registry && registry.models.find((item) => item.id === "ollama-qwen-local");
    const list = model && (model.installedModels || model.models || model.metadata && model.metadata.installedModels);
    return (Array.isArray(list) ? list : []).map((item) => typeof item === "string" ? { name: item, digest: null } : item)
      .filter((item) => item && typeof item.name === "string" && item.name && item.supported !== false);
  }
  function presetSettings(request) {
    const result = {};
    for (const key of ["mode", "primaryPredictor", "specialistPredictor", "specialistTask", "primaryReporter", "comparisonReporter", "includeTox21", "includeEvidence", "evidenceEndpoint", "includeAttribution", "explainEndpoint", "ollamaModel"]) {
      if (Object.hasOwn(request, key)) result[key] = request[key];
    }
    return result;
  }
  function sanitizeExport(value) {
    if (Array.isArray(value)) return value.map(sanitizeExport);
    if (!value || typeof value !== "object") return value;
    const result = {};
    for (const [key, item] of Object.entries(value)) {
      if (/^(api[-_]?key|access[-_]?token|authorization|password|secret|hf[-_]?token|model[-_]?dir|encoder[-_]?dir|file[-_]?path|cache[-_]?path|raw[-_]?rows)$/i.test(key)) continue;
      result[key] = sanitizeExport(item);
    }
    return result;
  }
  function exportRun(state) {
    if (!state.run || state.stale || !TERMINAL.has(state.run.status)) throw new Error("当前结果尚未完成或输入配置已修改；请重新运行后导出。");
    return sanitizeExport(state.run);
  }
  function formatScore(value) { const score = finiteScore(value); return score === null ? "未提供" : score.toFixed(6); }
  function formatMetric(value, unit, digits = 2) {
    return typeof value === "number" && Number.isFinite(value) && value >= 0 ? `${value.toFixed(digits)}${unit ? ` ${unit}` : ""}` : "未测量";
  }
  function gpuMemoryMiB(gpu, kind) {
    if (!gpu || gpu.measured === false) return null;
    const base = kind === "reserved" ? "peakReserved" : "peakAllocated";
    const mib = gpu[`${base}MiB`];
    if (typeof mib === "number" && Number.isFinite(mib) && mib >= 0) return mib;
    const bytes = gpu[`${base}Bytes`];
    return typeof bytes === "number" && Number.isFinite(bytes) && bytes >= 0 ? bytes / (1024 * 1024) : null;
  }
  function specialistTasks(run) {
    const model = completedModel(run, "txgemma-2b-predict");
    if (!model || !model.data) return [];
    return Array.isArray(model.data.tasks) ? model.data.tasks : [model.data];
  }
  function summaryOnly(run) { return Boolean(run && run.recovery && run.recovery.contentAvailable === false); }
  function fixtureResult(result) {
    return Boolean(result && (result.testFixture === true || result.provenance && result.provenance.testFixture === true
      || result.data && result.data.prediction && result.data.prediction.provenance && result.data.prediction.provenance.testFixture === true));
  }
  function fixtureRun(run) { return Boolean(run && (run.testFixture === true || Object.values(run.models || {}).some(fixtureResult))); }
  function validRunId(value) { return typeof value === "string" && /^[A-Za-z0-9_-]{1,80}$/.test(value); }
  function validateHistory(value) {
    if (!value || value.contractVersion !== CONTRACT || !Array.isArray(value.runs) || !Number.isInteger(value.total) || value.total < 0
      || !Number.isInteger(value.limit) || value.limit < 1 || !Number.isInteger(value.offset) || value.offset < 0
      || value.runs.some((run) => !run || !validRunId(run.runId) || !RUN_STATES.has(run.status))) throw new Error("实验历史接口格式不兼容。");
    return value;
  }
  function restoredFields(request) {
    const saved = request || {};
    return { smiles: typeof saved.smiles === "string" ? saved.smiles : "", question: typeof saved.question === "string" ? saved.question : "",
      specialistTask: saved.specialistTask || null, ollamaModel: saved.ollamaModel || null,
      includeTox21: saved.includeTox21 !== false, includeEvidence: saved.includeEvidence === true,
      evidenceEndpoint: ENDPOINT_IDS.includes(saved.evidenceEndpoint) ? saved.evidenceEndpoint : null,
      includeAttribution: Object.hasOwn(saved, "includeAttribution") ? saved.includeAttribution === true : Boolean(saved.explainEndpoint),
      explainEndpoint: ENDPOINT_IDS.includes(saved.explainEndpoint) ? saved.explainEndpoint : "herg",
      comparisonEnabled: false, allowCloudTransfer: false, persistSensitiveContent: false };
  }
  function selectedModelReadiness(registry, id, selectedName) {
    const entry = registry && registry.models.find((item) => item.id === id);
    if (!entry) return { status: "unknown", modelId: null, revision: null, reason: "尚未检查实际模型。" };
    if (registry.testFixture === true) {
      const selected = id === "ollama-qwen-local" ? installedOllamaModels(registry).find((item) => item.name === (selectedName || entry.selectedDefaultModel || entry.requestedModelId || entry.actualModelId)) : null;
      return { status: "mock-ready", modelId: selected && selected.name || entry.actualModelId || null,
        revision: selected && selected.digest || entry.revision || null,
        reason: "Mock 软件测试夹具；模型清单、ID 与 digest 均为测试数据，未验证真实安装、账户权限或运行能力。" };
    }
    const result = { status: entry.available === true ? "available" : entry.configured === true ? "configured" : "unavailable",
      modelId: entry.actualModelId || null, revision: entry.revision || null, reason: entry.reason || null };
    if (id === "deepseek-api" && entry.configured === true && entry.accessVerified !== true) return { ...result, status: "configured-unverified" };
    if (id !== "ollama-qwen-local") return result;
    const desired = selectedName || entry.selectedDefaultModel || entry.requestedModelId || entry.actualModelId;
    const installed = installedOllamaModels(registry).find((item) => item.name === desired);
    if (installed) return { ...result, status: "installed", modelId: installed.name, revision: installed.digest || null,
      reason: "已从真实本机清单核对当前选择的 ID 与 digest；尚需本次报告生成验收。" };
    return { ...result, modelId: desired || null, status: entry.errorCode === "OLLAMA_UNAVAILABLE" || entry.errorCode === "OLLAMA_LOOPBACK_REQUIRED" ? "unreachable" : "not-installed" };
  }
  function regionSignature(value) {
    function canonical(item) {
      if (Array.isArray(item)) return item.map(canonical);
      if (item && typeof item === "object") return Object.fromEntries(Object.keys(item).sort().map((key) => [key, canonical(item[key])]));
      return item;
    }
    return JSON.stringify(canonical(value));
  }
  function createRegionRenderer() {
    const signatures = new WeakMap();
    return function renderRegion(element, value, build) {
      const signature = regionSignature(value);
      if (signatures.has(element) && signatures.get(element) === signature) return false;
      const oldDetails = new Map(Array.from(element.querySelectorAll("details[data-view-key]")).map((item) => [item.dataset.viewKey, item.open]));
      const document = element.ownerDocument;
      const focused = document && document.activeElement && element.contains(document.activeElement) ? document.activeElement : null;
      const focusKey = focused && (focused.dataset.focusKey || focused.id);
      const selection = focused && typeof focused.selectionStart === "number" ? [focused.selectionStart, focused.selectionEnd] : null;
      build();
      for (const item of element.querySelectorAll("details[data-view-key]")) if (oldDetails.has(item.dataset.viewKey)) item.open = oldDetails.get(item.dataset.viewKey);
      if (focused && !element.contains(focused) && focusKey) {
        const replacement = Array.from(element.querySelectorAll("[data-focus-key], [id]")).find((item) => item.dataset.focusKey === focusKey || item.id === focusKey);
        if (replacement) {
          replacement.focus({ preventScroll: true });
          if (selection && typeof replacement.setSelectionRange === "function") replacement.setSelectionRange(...selection);
        }
      }
      signatures.set(element, signature);
      return true;
    };
  }

  return { CONTRACT, MODEL_IDS, ENDPOINT_IDS, ENDPOINT_NAMES, TASK_NAMES, TERMINAL, buildRequest, fingerprint, initialState, reduce, validateRegistry, validateRun, endpointRows, pathwayRows, predictionData, installedOllamaModels, presetSettings, sanitizeExport, exportRun, formatScore, formatMetric, gpuMemoryMiB, specialistTasks, summaryOnly, fixtureResult, fixtureRun, validRunId, validateHistory, restoredFields, selectedModelReadiness, createRegionRenderer, regionSignature };
});
