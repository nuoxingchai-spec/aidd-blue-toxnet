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
    const cloud = fields.comparisonEnabled === true && fields.allowCloudTransfer === true;
    return {
      smiles, question, mode: "compare", primaryPredictor: "toxnet-v3-b16",
      specialistPredictor: "txgemma-2b-predict", specialistTask: fields.specialistTask || null,
      primaryReporter: "ollama-qwen-local", comparisonReporter: fields.comparisonEnabled === true ? "deepseek-api" : null,
      includeTox21: fields.includeTox21 !== false, includeEvidence: fields.includeEvidence === true,
      explainEndpoint, allowCloudTransfer: cloud, ollamaModel: fields.ollamaModel || null,
    };
  }

  function fingerprint(request) { return JSON.stringify(request); }
  function initialState() { return { generation: 0, run: null, stale: false, inFlight: false, cancelling: false, fingerprint: null, error: null }; }
  function current(state, event) {
    return event.generation === state.generation && (!event.runId || !state.run || event.runId === state.run.runId);
  }
  function reduce(state, event) {
    switch (event.type) {
      case "START": return { ...initialState(), generation: state.generation + 1, fingerprint: fingerprint(event.request), inFlight: true };
      case "ACK":
        if (!current(state, event) || !state.inFlight) return state;
        return { ...state, run: { contractVersion: CONTRACT, runId: event.runId, status: "pending", models: {} } };
      case "POLL":
        if (!current(state, event) || state.stale || state.cancelling || !state.inFlight) return state;
        return { ...state, run: event.run, inFlight: !TERMINAL.has(event.run.status), error: null };
      case "EDIT":
        if (!state.fingerprint && !state.run) return state;
        return { ...state, generation: state.generation + 1, stale: true, inFlight: false, cancelling: false, error: null };
      case "CANCEL_REQUEST":
        if (!state.run || TERMINAL.has(state.run.status) || state.stale || state.cancelling) return state;
        return { ...state, generation: state.generation + 1, inFlight: true, cancelling: true };
      case "CANCELLED":
        if (!current(state, event)) return state;
        return { ...state, run: event.run, inFlight: !TERMINAL.has(event.run.status), cancelling: false };
      case "ERROR":
        if (!current(state, event)) return state;
        return { ...state, error: event.message, inFlight: false, cancelling: false };
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
    for (const key of ["mode", "primaryPredictor", "specialistPredictor", "specialistTask", "primaryReporter", "comparisonReporter", "includeTox21", "includeEvidence", "explainEndpoint", "ollamaModel"]) {
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

  return { CONTRACT, MODEL_IDS, ENDPOINT_IDS, ENDPOINT_NAMES, TASK_NAMES, TERMINAL, buildRequest, fingerprint, initialState, reduce, validateRegistry, validateRun, endpointRows, pathwayRows, predictionData, installedOllamaModels, presetSettings, sanitizeExport, exportRun, formatScore, formatMetric, gpuMemoryMiB, specialistTasks };
});
