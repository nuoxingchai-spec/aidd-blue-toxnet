(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ToxicityCore = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
  "use strict";
  const CLIENT_VERSION = "2.0.0";
  const CONTRACT_VERSION = "toxilens.api.v2";
  const ENDPOINT_IDS = ["herg", "dili", "ames", "clintox"];
  class APIError extends Error {
    constructor(message, code, requestId) {
      super(message);
      this.name = "APIError";
      this.code = code || "API_ERROR";
      this.requestId = requestId || null;
    }
  }
  function riskTier(score) {
    if (score < 0.33) return { id: "low", label: "低分区间", action: "结合实验与适用域证据判断", color: "blue" };
    if (score < 0.58) return { id: "moderate", label: "中分区间", action: "补充实验与模型验证证据", color: "amber" };
    if (score < 0.78) return { id: "high", label: "高分区间", action: "优先实验复核", color: "orange" };
    return { id: "critical", label: "重点关注", action: "优先复核模型与实验结果", color: "red" };
  }
  function validateReport(report) {
    if (!report || typeof report !== "object" || !Array.isArray(report.endpoints)) throw new APIError("模型服务返回了不完整的报告。", "INVALID_REPORT");
    const ids = report.endpoints.map((item) => item.id);
    if (ids.length !== 4 || new Set(ids).size !== 4 || ENDPOINT_IDS.some((id) => !ids.includes(id))) throw new APIError("报告必须包含四个完整且不重复的毒性端点。", "INVALID_REPORT");
    for (const endpoint of report.endpoints) {
      if (typeof endpoint.score !== "number" || !Number.isFinite(endpoint.score) || endpoint.score < 0 || endpoint.score > 1) throw new APIError("报告端点分数必须是 0 到 1 的有限数字。", "INVALID_REPORT");
    }
    if (report.provenance?.contractVersion !== CONTRACT_VERSION || typeof report.canonicalSmiles !== "string" || typeof report.structureId !== "string" || typeof report.overall !== "number" || !Number.isFinite(report.overall) || report.overall < 0 || report.overall > 1 || !report.descriptors || !report.tier || !Array.isArray(report.alerts) || typeof report.moleculeSvg !== "string") throw new APIError("报告字段或接口版本不匹配，请检查 API 服务版本。", "INVALID_REPORT");
    return report;
  }
  async function request(path, options) {
    if (root.location?.protocol === "file:") throw new APIError("请启动 API 服务，并通过 http://127.0.0.1:4173 打开本项目。", "API_SERVER_REQUIRED");
    const settings = options || {};
    const controller = new AbortController();
    const externalSignal = settings.signal;
    const cancel = () => controller.abort();
    if (externalSignal?.aborted) controller.abort();
    else externalSignal?.addEventListener("abort", cancel, { once: true });
    let timedOut = false;
    const timer = root.setTimeout(() => { timedOut = true; controller.abort(); }, settings.timeout || 90000);
    try {
      const response = await root.fetch(path, { method: settings.body ? "POST" : "GET", headers: { Accept: "application/json", ...(settings.body ? { "Content-Type": "application/json" } : {}) }, body: settings.body ? JSON.stringify(settings.body) : undefined, signal: controller.signal, credentials: "same-origin" });
      let payload;
      try { payload = await response.json(); } catch { throw new APIError("服务未返回有效 JSON，请检查 API 地址。", "INVALID_RESPONSE"); }
      if (!response.ok) throw new APIError(payload?.error?.message || "服务请求失败。", payload?.error?.code, payload?.error?.requestId);
      return payload;
    } catch (error) {
      if (timedOut) throw new APIError("服务响应超时，请稍后重试。", "API_TIMEOUT");
      if (error?.name === "AbortError" || error instanceof APIError) throw error;
      throw new APIError("无法连接 API 服务，请检查服务是否已启动。", "NETWORK_ERROR");
    } finally { root.clearTimeout(timer); externalSignal?.removeEventListener("abort", cancel); }
  }
  async function analyzeSmiles(smiles, options) {
    const input = typeof smiles === "string" ? smiles.trim() : "";
    if (!input) throw new APIError("请输入 SMILES。", "INVALID_SMILES");
    if (input.length > 4096) throw new APIError("SMILES 超过支持长度。", "INVALID_SMILES");
    const mode = options?.mode || "model";
    if (!["model", "rules"].includes(mode)) throw new APIError("无效的分析模式。", "INVALID_MODE");
    return validateReport(await request("/api/analyze", { body: { smiles: input, mode, ...(options?.provider ? { provider: options.provider } : {}) }, signal: options?.signal }));
  }
  async function getHealth(options) { return request("/api/health", { signal: options?.signal, timeout: 5000 }); }
  async function getLocalModelInfo(options) { return request("/api/local-model/info", { signal: options?.signal, timeout: 15000 }); }
  async function explainSmiles(smiles, options) {
    const result = await request("/api/research/explain", { body: { smiles, model: "v2", endpoint: options?.endpoint || "herg",
      includeLanguage: Boolean(options?.includeLanguage), languageProvider: options?.languageProvider || null,
      ...(options?.expectedWeightsSha256 ? { expectedWeightsSha256: options.expectedWeightsSha256 } : {}),
      ...(options?.expectedStructureId ? { expectedStructureId: options.expectedStructureId } : {}) }, signal: options?.signal, timeout: 180000 });
    if (result?.contractVersion !== "aidd.research.explanation.api.v1" || !["available", "partial"].includes(result.status)
      || typeof result.structureId !== "string" || !Array.isArray(result.modelPrediction?.endpoints)
      || !result.attribution || !Array.isArray(result.evidence?.results) || !Array.isArray(result.report?.sections)
      || !Array.isArray(result.report?.claims) || !Array.isArray(result.report?.sources)) throw new APIError("研究解释契约不完整。", "INVALID_EXPLANATION");
    if (!/^[0-9a-f]{64}$/.test(result.structureId)
        || typeof result.modelPrediction.provenance?.modelVersion !== "string"
        || !/^[0-9a-f]{64}$/.test(result.modelPrediction.provenance?.weightsSha256 || "")
        || !["not-requested", "unavailable", "organized"].includes(result.report.language?.status)
        || !["available", "computed-with-warning", "unavailable"].includes(result.attribution.status)
        || !["available", "insufficient-evidence"].includes(result.evidence.status)
        || result.report.sections.some(s => !s || typeof s.title !== "string" || typeof s.text !== "string"
          || !Array.isArray(s.sourceIds) || !Array.isArray(s.limitations))) throw new APIError("研究解释来源或报告字段无效。", "INVALID_EXPLANATION");
    const ids = result.modelPrediction.endpoints.map(e => e.id);
    if (ids.length !== 4 || new Set(ids).size !== 4 || ENDPOINT_IDS.some(id => !ids.includes(id))
        || result.modelPrediction.endpoints.some(e => typeof e.score !== "number" || !Number.isFinite(e.score) || e.score < 0 || e.score > 1)
        || result.provenance?.researchOnly !== true || result.provenance?.humanRiskProbability !== false
        || !Array.isArray(result.attribution.atoms) || result.attribution.atoms.some(a => !Number.isFinite(a.contribution))) throw new APIError("研究解释含无效模型数据。", "INVALID_EXPLANATION");
    return result;
  }
  function providerConfigPath(provider) {
    if (typeof provider !== "string" || !/^[a-z][a-z0-9_-]{0,39}$/.test(provider)) throw new APIError("请选择有效的模型供应商。", "INVALID_PROVIDER");
    return `/api/providers/${encodeURIComponent(provider)}/config`;
  }
  async function getProviderConfig(provider, options) { return request(providerConfigPath(provider), { signal: options?.signal, timeout: 5000 }); }
  async function saveProviderConfig(provider, config) { return request(providerConfigPath(provider), { body: config, timeout: 10000 }); }
  return { CLIENT_VERSION, CONTRACT_VERSION, APIError, analyzeSmiles, explainSmiles, getHealth, getProviderConfig, saveProviderConfig, getLocalModelInfo, riskTier };
});
