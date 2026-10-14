(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ValidationCore = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const SCHEMA = "aidd.scientific-validation.v1";
  const MODEL_IDS = ["A4", "B16", "C_Morgan"];
  const TERMINAL = new Set(["completed", "partial", "failed", "cancelled", "interrupted", "not-qualified"]);
  const RUN_STATES = new Set(["pending", "running", ...TERMINAL]);
  function validId(value) { return typeof value === "string" && /^[A-Za-z0-9_.:-]{1,128}$/.test(value) && ![".", ".."].includes(value); }
  function validateCatalog(value) {
    if (!value || value.schema !== SCHEMA || value.formalAvailability !== false || !Array.isArray(value.datasets) || !Array.isArray(value.models)
      || value.datasets.some((item) => !item || !validId(item.id)) || value.models.some((item) => !item || !MODEL_IDS.includes(item.id))) throw new Error("独立验证数据接口不兼容；不能将其作为科学验收证据。");
    return value;
  }
  function validateRun(value) {
    if (!value || value.schema !== SCHEMA || !validId(value.runId) || !RUN_STATES.has(value.status)) throw new Error("验证运行状态格式不兼容。");
    if (value.report !== null && value.report !== undefined && (typeof value.report !== "object" || Array.isArray(value.report))) throw new Error("验证报告必须为聚合对象。");
    return value;
  }
  function request(kind, fields) {
    if (!validId(fields.datasetId)) throw new Error("请先选择已登记的数据集。");
    if (kind === "audit") return { datasetId: fields.datasetId };
    if (kind !== "evaluate" || !validId(fields.protocolId)) throw new Error("尚无有效的冻结评价协议；请先检查数据与协议状态。");
    const ids = Array.isArray(fields.modelIds) ? fields.modelIds : [];
    if (ids.length !== MODEL_IDS.length || new Set(ids).size !== ids.length || ids.some((id) => !MODEL_IDS.includes(id))) throw new Error("请选择完整的固定 A4、B16 与 Morgan 三模型对照。");
    return { datasetId: fields.datasetId, protocolId: fields.protocolId, modelIds: MODEL_IDS.filter((id) => ids.includes(id)), seed: 42 };
  }
  function signature(value) {
    function canonical(item) {
      if (Array.isArray(item)) return item.map(canonical);
      if (item && typeof item === "object") return Object.fromEntries(Object.keys(item).sort().map((key) => [key, canonical(item[key])]));
      return item;
    }
    return JSON.stringify(canonical(value));
  }
  function initial() { return { generation: 0, run: null, pending: false, cancelling: false, stale: false, error: null }; }
  function reduce(state, event) {
    if (event.type === "START") return { ...initial(), generation: state.generation + 1, pending: true };
    if (event.type === "RESTORE") return { ...initial(), generation: state.generation + 1, pending: true };
    if (event.type === "EDIT") return state.run || state.pending ? { ...state, generation: state.generation + 1, pending: false, cancelling: false, stale: true, error: null } : state;
    if (event.type === "CANCEL") return { ...state, generation: state.generation + 1, cancelling: true, pending: true };
    if (event.generation !== state.generation || state.stale) return state;
    if (event.type === "ACK") return { ...state, run: { schema: SCHEMA, runId: event.runId, status: "pending" } };
    if (event.type === "RUN") return { ...state, run: event.run, pending: !TERMINAL.has(event.run.status), cancelling: false, error: null };
    if (event.type === "ERROR") return { ...state, pending: false, cancelling: false, error: event.message };
    return state;
  }
  function metric(value, digits = 4) { return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1 ? value.toFixed(digits) : "未计算"; }
  function count(value) { return Number.isInteger(value) && value >= 0 ? String(value) : "未计算"; }
  function interval(value) {
    const lower = Array.isArray(value) ? value[0] : value && (value.lower ?? value.low);
    const upper = Array.isArray(value) ? value[1] : value && (value.upper ?? value.high);
    return metric(lower) === "未计算" || metric(upper) === "未计算" || lower > upper ? "未计算" : `[${metric(lower)}, ${metric(upper)}]`;
  }
  function coverage(row) {
    const value = row.coverageFraction ?? (row.coverage && typeof row.coverage === "object" ? row.coverage.fraction : row.coverage);
    const denominator = row.coverageDenominator ?? row.count;
    const members = Number.isInteger(row.coveredCount) && Number.isInteger(denominator) && denominator > 0 && row.coveredCount >= 0 && row.coveredCount <= denominator ? `${row.coveredCount}/${denominator}` : null;
    return metric(value) !== "未计算" ? `${metric(value)}${members ? ` (${members})` : ""}` : members || "未计算";
  }
  function endpointRows(report) {
    const comparison = report && (report.fairComparison || report);
    const data = comparison && comparison.endpoints;
    const result = [];
    function add(endpoint, row, modelId) {
      if (!row || typeof row !== "object" || Array.isArray(row)) return;
      if (row.models && typeof row.models === "object") {
        for (const [model, value] of Object.entries(row.models)) add(endpoint, value, model);
      } else result.push({ ...row, endpoint: row.endpoint || row.endpointId || endpoint, modelId: row.modelId || row.model || modelId || null });
    }
    if (Array.isArray(data)) for (const row of data) add(row && (row.endpoint || row.endpointId), row);
    else if (data && typeof data === "object") for (const [endpoint, row] of Object.entries(data)) {
      if (Array.isArray(row)) for (const item of row) add(endpoint, item);
      else add(endpoint, row);
    }
    if (!result.length && comparison && comparison.models && typeof comparison.models === "object") {
      for (const [modelId, model] of Object.entries(comparison.models)) {
        for (const [endpoint, row] of Object.entries(model && model.endpoints || {})) {
          add(endpoint, { ...row, modelVersion: row.modelVersion || model.modelBinding && model.modelBinding.modelVersion }, modelId);
        }
      }
    }
    return result;
  }
  function reliabilityCurves(report) {
    const calibration = report && report.calibration;
    const curves = calibration && calibration.reliabilityCurves;
    const explicit = (Array.isArray(curves) ? curves : []).filter((curve) => curve && curve.measured === true && typeof curve.sourceId === "string" && curve.sourceId
      && Array.isArray(curve.points) && curve.points.length > 0 && curve.points.every((point) => point && metric(point.predicted) !== "未计算" && metric(point.observed) !== "未计算" && Number.isInteger(point.count) && point.count > 0));
    const diagnostic = report && (report.calibrationDiagnostics || calibration && calibration.diagnostics || calibration);
    const sha = (value) => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
    if (!diagnostic || diagnostic.schema !== "aidd.calibration-diagnostics.v1" || diagnostic.role !== "calibration-resubstitution" || diagnostic.independentTest !== false
      || diagnostic.methodSelectionPermitted !== false || !["diagnosticSha256", "datasetManifestSha256", "protocolSha256", "membershipSha256", "modelBindingSha256"].every((key) => sha(diagnostic[key]))) return explicit;
    const result = [...explicit];
    for (const [endpoint, row] of Object.entries(diagnostic.endpoints || {})) {
      if (!row || !Number.isInteger(row.count) || !Number.isInteger(row.positives) || row.positives <= 0 || row.positives >= row.count) continue;
      for (const [field, kind] of [["reliabilityBefore", "校准前"], ["reliabilityAfter", "校准后"]]) {
        if (!Array.isArray(row[field]) || row[field].some((point) => !point || !Number.isInteger(point.count) || point.count < 0)) continue;
        const bins = row[field].filter((point) => point.count > 0);
        if (!bins.length || bins.reduce((sum, point) => sum + point.count, 0) !== row.count || bins.some((point) => metric(point.meanPredicted) === "未计算" || metric(point.observedPositiveRate) === "未计算")) continue;
        result.push({ endpoint, kind, modelId: diagnostic.modelId || null, measured: true, sourceId: diagnostic.datasetManifestSha256,
          evidenceStatus: "校准集内重代入诊断，非独立评价；不能用于选择方法", diagnosticSha256: diagnostic.diagnosticSha256,
          points: bins.map((point) => ({ predicted: point.meanPredicted, observed: point.observedPositiveRate, count: point.count })) });
      }
    }
    return result;
  }
  function evidence(report) {
    if (!report || typeof report !== "object") return null;
    const comparison = report.fairComparison || report;
    return { independenceStatus: report.independenceStatus || report.datasetAudit && (report.datasetAudit.qualification || report.datasetAudit.status) || null,
      role: report.role || comparison.role || null, exposure: report.exposure || null,
      labelsAndMembersMatched: comparison.labelsAndMembersMatched === true ? true : null,
      labelReadAfterPrediction: report.labelReadAfterPrediction === true ? true : null,
      datasetManifestSha256: report.datasetManifestSha256 || null, protocolSha256: report.protocolSha256 || comparison.protocolSha256 || null,
      evaluationSource: report.evaluationSource || null, formalAvailability: report.formalAvailability === false ? false : null };
  }
  function sanitize(value) {
    if (Array.isArray(value)) return value.map(sanitize);
    if (!value || typeof value !== "object") return value;
    return Object.fromEntries(Object.entries(value).filter(([key]) => !/^(api[-_]?key|access[-_]?token|hf[-_]?token|authorization|password|secret|raw[-_]?rows|rows|labels|eligible[-_]?rows|quarantined[-_]?rows|smiles|canonical[-_]?smiles|file[-_]?path|model[-_]?dir|model[-_]?file|encoder[-_]?dir|cache[-_]?path)$/i.test(key)).map(([key, item]) => [key, sanitize(item)]));
  }
  function exportable(state) { return Boolean(state.run && state.run.status === "completed" && state.run.report && !state.stale && !state.pending && !state.cancelling); }
  return { SCHEMA, MODEL_IDS, TERMINAL, validId, validateCatalog, validateRun, request, signature, initial, reduce, metric, count, interval, coverage, endpointRows, reliabilityCurves, evidence, sanitize, exportable };
});
