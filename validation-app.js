(function () {
  "use strict";
  const Core = window.ValidationCore;
  const $ = (id) => document.getElementById(id);
  const STATES = { pending: "等待执行", running: "正在执行", completed: "已完成工程运行", partial: "部分完成", failed: "失败", cancelled: "已取消", interrupted: "服务中断（不会自动续跑）", "not-qualified": "尚不具备独立评价资格", "not-independently-qualified": "尚不具备独立评价资格", "pending-current-audit": "待本次资格审计", "independently-qualified-for-sealing": "资格审计通过，待封存评价", qualified: "按当前协议审计合格", "not-evaluated": "尚未评价", unavailable: "不可用", "not-fitted": "未拟合", "not-validated": "未验证", "insufficient-evidence": "证据不足", computed: "按协议计算", "single-class": "单类别，AUROC/AP不可计算", "no-data": "无观测数据", "coverage-incomplete": "预测覆盖不完整，不发布完整成员指标", "artifact-present-not-execution-verified": "工件存在，执行与SHA尚未核验", "missing-artifact": "模型工件缺失" };
  const MODELS = { A4: "validationModelA4", B16: "validationModelB16", C_Morgan: "validationModelMorgan" };
  let catalog = null, state = Core.initial(), pollTimer = null, controller = null, catalogGeneration = 0, historyGeneration = 0, runSelection = null;
  const regionSignatures = new WeakMap();
  const STORAGE = "aidd.validation.lastRunId";
  function node(tag, text, className) { const result = document.createElement(tag); if (text !== undefined && text !== null) result.textContent = String(text); if (className) result.className = className; return result; }
  function text(element, value) { const result = String(value === undefined || value === null ? "" : value); if (element.textContent !== result) element.textContent = result; }
  function status(value) { const id = value && typeof value === "object" ? value.status : value; return STATES[id] || id || "未计算"; }
  function object(value) { return value === null || value === undefined ? "未计算 / 尚无可核验记录" : typeof value === "object" ? JSON.stringify(Core.sanitize(value), null, 2) : String(value); }
  function region(element, value, build) { const key = Core.signature(value); if (regionSignatures.has(element) && regionSignatures.get(element) === key) return; build(); regionSignatures.set(element, key); }
  function fields() { const selected = dataset(); return { datasetId: $("validationDataset").value, protocolId: selected && selected.protocolId || catalog && catalog.protocol && catalog.protocol.id, modelIds: Core.MODEL_IDS.filter((id) => $(MODELS[id]).checked) }; }
  function remember(runId) { try { if (Core.validId(runId)) localStorage.setItem(STORAGE, runId); } catch (_) {} }
  function remembered() { try { const value = localStorage.getItem(STORAGE); return Core.validId(value) ? value : null; } catch (_) { return null; } }
  async function api(path, options = {}) {
    const timeout = new AbortController(), timer = setTimeout(() => timeout.abort(), 30000);
    try {
      const result = await fetch(path, { ...options, credentials: "same-origin", cache: "no-store", signal: options.signal ? AbortSignal.any([options.signal, timeout.signal]) : timeout.signal,
        headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}) } });
      let value; try { value = await result.json(); } catch (_) { throw new Error("验证服务没有返回合法 JSON。"); }
      if (!result.ok) {
        const detail = value.error || value.detail;
        throw new Error(`${detail && typeof detail === "object" && (detail.message || detail.reason || detail.code) || typeof detail === "string" && detail || "验证请求被拒绝"}（HTTP ${result.status}）`);
      }
      return value;
    } catch (error) { if (timeout.signal.aborted && !(options.signal && options.signal.aborted)) throw new Error("验证状态请求超时；后台任务可能仍在执行，请刷新记录或取消。"); throw error; }
    finally { clearTimeout(timer); }
  }
  function stopReads() { if (pollTimer !== null) clearTimeout(pollTimer); pollTimer = null; if (controller) controller.abort(); controller = null; }
  function dispatch(event) { state = Core.reduce(state, event); render(); }
  function quietCancel(runId) { if (Core.validId(runId)) api(`/api/research/validation/runs/${encodeURIComponent(runId)}`, { method: "DELETE" }).catch(() => {}); }
  function edited() { const runId = state.run && state.run.runId, active = state.pending; stopReads(); dispatch({ type: "EDIT" }); if (active && runId) quietCancel(runId); renderDataset(); }
  function dataset() { return catalog && catalog.datasets.find((item) => item.id === $("validationDataset").value); }
  function definition(element, pairs) {
    const list = node("dl", null, "validation-definition");
    for (const [name, value] of pairs) list.append(node("dt", name), node("dd", object(value)));
    element.replaceChildren(list);
  }
  function renderDataset() {
    const item = dataset();
    text($("validationDatasetHint"), item ? `${status(item.qualification)}；数据版本 ${item.version || "未提供"}。` : "没有可选择的已登记数据集；不能构造数据或指标。");
    text($("validationQualification"), status(item && item.qualification));
    region($("validationDatasetSummary"), item, () => definition($("validationDatasetSummary"), [["数据集", item && (item.name || item.id)], ["版本", item && item.version], ["资格", item && item.qualification], ["许可状态", item && item.rightsStatus]]));
    text($("validationDatasetSource"), object(item && { dataset: item, protocol: catalog.protocol, licenseReview: catalog.licenseReview }));
    $("validationProtocol").value = item && item.protocolId || catalog && catalog.protocol && catalog.protocol.id || "";
    const dependencies = item && item.missingDependencies;
    text($("validationDependencies"), Array.isArray(dependencies) ? dependencies.length ? `缺少依赖：${dependencies.map(object).join("；")}` : "清单未列出缺失依赖；是否合格仍以审计与协议为准。" : "依赖清单未提供，不能推断已具备评价条件。");
  }
  async function refreshCatalog() {
    const generation = ++catalogGeneration;
    $("validationRefresh").disabled = true;
    try {
      const next = Core.validateCatalog(await api("/api/research/validation/datasets"));
      if (generation !== catalogGeneration) return;
      const changed = catalog && Core.signature(catalog) !== Core.signature(next);
      const selected = $("validationDataset").value; catalog = next;
      const options = next.datasets.map((item) => { const option = node("option", `${item.name || item.id} · ${status(item.qualification)}`); option.value = item.id; return option; });
      if (!options.length) { const option = node("option", "没有已登记数据集"); option.value = ""; options.push(option); }
      $("validationDataset").replaceChildren(...options);
      if (next.datasets.some((item) => item.id === selected)) $("validationDataset").value = selected;
      text($("validationProtocol"), ""); $("validationProtocol").value = next.protocol && next.protocol.id || "";
      text($("validationModelStatus"), next.models.map((model) => `${model.id}：${model.status ? status(model.status) : model.available === true ? "登记工件存在，科学资格另行审计" : "工件不可用"}${model.reason ? ` · ${model.reason}` : ""}`).join("；") || "尚无可核验模型清单。");
      if (changed && (state.run || state.pending)) edited();
      renderDataset(); render();
      if (next.registryError) { text($("validationError"), object(next.registryError)); $("validationError").hidden = false; }
    } catch (error) { if (generation === catalogGeneration) { text($("validationError"), error.message); $("validationError").hidden = false; } }
    finally { if (generation === catalogGeneration) $("validationRefresh").disabled = false; }
  }
  async function start(kind) {
    if (!catalog || state.pending || state.cancelling) return;
    const selected = dataset();
    if (selected && (kind === "audit" && selected.auditAvailable === false || kind === "evaluate" && selected.evaluationAvailable === false)) {
      text($("validationError"), `当前数据未获准执行${kind === "audit" ? "审计" : "独立评价"}：${(selected.missingDependencies || []).map(object).join("；") || "需要服务端登记、资格审计与封存协议。"}`); $("validationError").hidden = false; return;
    }
    let payload; try { payload = Core.request(kind, fields()); } catch (error) { text($("validationError"), error.message); $("validationError").hidden = false; return; }
    stopReads(); runSelection = Core.signature(fields()); dispatch({ type: "START" }); const generation = state.generation;
    try {
      const accepted = await api(`/api/research/validation/${kind}`, { method: "POST", body: JSON.stringify(payload) });
      if (!Core.validId(accepted.runId)) throw new Error("验证创建接口没有返回合法运行 ID。");
      if (accepted.schema !== Core.SCHEMA || !["pending", "running"].includes(accepted.status)) { quietCancel(accepted.runId); throw new Error("验证创建响应的合同或等待状态不兼容。"); }
      if (generation !== state.generation) { quietCancel(accepted.runId); return; }
      remember(accepted.runId); dispatch({ type: "ACK", generation, runId: accepted.runId }); poll(generation, accepted.runId);
    } catch (error) { if (generation === state.generation) dispatch({ type: "ERROR", generation, message: error.message }); }
  }
  async function poll(generation, runId) {
    if (generation !== state.generation || !state.pending || state.cancelling) return;
    controller = new AbortController();
    try {
      const run = Core.validateRun(await api(`/api/research/validation/runs/${encodeURIComponent(runId)}`, { signal: controller.signal }));
      if (run.runId !== runId) throw new Error("验证服务返回了其他运行的结果。");
      dispatch({ type: "RUN", generation, run });
      if (generation === state.generation && state.pending) pollTimer = setTimeout(() => poll(generation, runId), 1000);
      else if (generation === state.generation) refreshHistory();
    } catch (error) { if (generation === state.generation && error.name !== "AbortError") dispatch({ type: "ERROR", generation, message: error.message }); }
  }
  async function cancel() {
    if (!state.run || Core.TERMINAL.has(state.run.status) || state.stale || state.cancelling) return;
    const runId = state.run.runId; stopReads(); dispatch({ type: "CANCEL" }); const generation = state.generation;
    try { const run = Core.validateRun(await api(`/api/research/validation/runs/${encodeURIComponent(runId)}`, { method: "DELETE" })); if (run.runId !== runId) throw new Error("取消响应的运行 ID 不一致。"); dispatch({ type: "RUN", generation, run }); if (state.pending) poll(generation, runId); }
    catch (error) { dispatch({ type: "ERROR", generation, message: `取消未确认：${error.message}` }); }
  }
  async function restore(runId) {
    if (!Core.validId(runId) || state.pending) return;
    stopReads(); dispatch({ type: "RESTORE" }); const generation = state.generation; controller = new AbortController();
    try {
      const run = Core.validateRun(await api(`/api/research/validation/runs/${encodeURIComponent(runId)}`, { signal: controller.signal }));
      if (generation !== state.generation) return;
      if (run.runId !== runId) throw new Error("历史记录运行 ID 不一致。");
      if (catalog && catalog.datasets.some((item) => item.id === run.datasetId)) $("validationDataset").value = run.datasetId;
      runSelection = Core.signature(fields()); remember(runId); dispatch({ type: "RUN", generation, run }); renderDataset(); if (state.pending) poll(generation, runId);
    } catch (error) { if (error.name !== "AbortError") dispatch({ type: "ERROR", generation, message: error.message }); }
  }
  function renderMetricRows(report) {
    const rows = Core.endpointRows(report);
    region($("validationMetricRows"), rows, () => {
      const rendered = rows.map((item) => {
        const m = item.metrics || item, rawCi = item.confidenceIntervals || m.confidenceIntervals || {}, ci = rawCi.intervals || rawCi;
        const row = node("tr");
        for (const value of [`${item.endpoint || "未提供"} / ${item.modelId || "未提供"}`, `${Core.count(item.count ?? item.samples)} / ${Core.count(item.positives)}`, Core.coverage(item), `${Core.metric(m.auroc)} / ${Core.interval(ci.auroc)}`, `${Core.metric(m.averagePrecision ?? m.ap)} / ${Core.interval(ci.averagePrecision ?? ci.ap)}`, `${Core.metric(m.brier ?? m.brierScore)} / ${Core.metric(m.ece ?? m.ece10Bins)}`, `${status(item.status)} / ${item.modelVersion || "未提供版本"}`]) row.append(node("td", value));
        return row;
      });
      if (!rendered.length) { const row = node("tr"), cell = node("td", "未计算：尚无通过数据、标签合同和模型来源核验的端点评价。"); cell.colSpan = 7; row.append(cell); rendered.push(row); }
      $("validationMetricRows").replaceChildren(...rendered);
    });
  }
  function renderReliability(report) {
    const curves = Core.reliabilityCurves(report);
    region($("validationReliability"), curves, () => {
      $("validationReliability").replaceChildren();
      if (!curves.length) { $("validationReliability").append(node("p", "未计算：没有真实、具备来源且可核验的可靠性分箱。")); return; }
      for (const curve of curves) {
        const make = (tag, attrs) => { const item = document.createElementNS("http://www.w3.org/2000/svg", tag); for (const [key, value] of Object.entries(attrs)) item.setAttribute(key, String(value)); return item; };
        const svg = make("svg", { viewBox: "0 0 360 270", role: "img", "aria-label": `${curve.endpoint || "端点"} ${curve.modelId || "模型"} 实测可靠性分箱曲线`, class: "validation-chart" });
        svg.append(make("line", { x1: 45, y1: 225, x2: 320, y2: 225, stroke: "#aabed9" }), make("line", { x1: 45, y1: 225, x2: 45, y2: 25, stroke: "#aabed9" }), make("line", { x1: 45, y1: 225, x2: 320, y2: 25, stroke: "#c1cfdf", "stroke-dasharray": "4 4" }));
        const points = [...curve.points].sort((a, b) => a.predicted - b.predicted);
        svg.append(make("polyline", { points: points.map((point) => `${45 + point.predicted * 275},${225 - point.observed * 200}`).join(" "), fill: "none", stroke: "#3d76d6", "stroke-width": 2 }));
        for (const point of points) svg.append(make("circle", { cx: 45 + point.predicted * 275, cy: 225 - point.observed * 200, r: 3, fill: "#3d76d6" }));
        const label = make("text", { x: 70, y: 250, fill: "#7e91aa", "font-size": 10 }); label.textContent = "横轴：平均预测；纵轴：观察阳性比例"; svg.append(label);
        $("validationReliability").append(node("p", `${curve.endpoint || "端点未提供"} / ${curve.modelId || "模型未提供"}${curve.kind ? ` · ${curve.kind}` : ""} · 来源 ${curve.sourceId}${curve.evidenceStatus ? ` · ${curve.evidenceStatus}` : ""}`), svg);
      }
    });
  }
  function render() {
    const run = state.run, report = run && run.report || {}, item = dataset();
    $("validationAudit").disabled = !catalog || !item || item.auditAvailable === false || state.pending;
    $("validationEvaluate").disabled = !catalog || !item || item.evaluationAvailable === false || state.pending;
    text($("validationEvaluate"), item && item.evaluationAvailable === false ? "尚无合格封存数据 · 不可评价" : "执行独立评价");
    $("validationCancel").disabled = !run || Core.TERMINAL.has(run.status) || state.stale || state.cancelling;
    $("validationExport").disabled = !Core.exportable(state);
    $("validationStale").hidden = !state.stale;
    $("validationError").hidden = !state.error && !(run && run.error);
    if (state.error || run && run.error) text($("validationError"), state.error || object(run.error));
    text($("validationStatus"), state.cancelling ? "正在取消，已完成的聚合证据会保留。" : run ? `${status(run.status)} · ${run.runId}${state.stale ? " · 旧配置报告" : ""}` : catalog ? "数据清单已读取。先审计资格，再按冻结协议评价。" : "正在读取真实数据与协议状态…");
    text($("validationRunMeta"), run ? `执行设备：${run.device || "未提供"}；资源清理：${run.cleanupConfirmed === true ? "已确认" : run.cleanupConfirmed === false ? "未确认" : "未提供"}；结果 SHA256：${run.resultSha256 || "未生成"}。` : "尚无本次运行。设备与资源清理不推算为科学验证证据。");
    $("validationFixture").hidden = !(catalog && catalog.testFixture === true || run && (run.testFixture === true || report.testFixture === true));
    const audit = report.datasetAudit || item && item.audit || null;
    if (report.datasetAudit || report.independenceStatus) text($("validationQualification"), status(report.independenceStatus || audit.qualification || audit.status));
    text($("validationAuditSummary"), audit ? status(audit.qualification || audit.status) : "未计算：没有可核验的结构隔离与来源审计。");
    text($("validationAuditDetails"), object(audit)); text($("validationFairDetails"), object(report.fairComparison || (report.models ? report : null)));
    const evidence = Core.evidence(run && run.report);
    text($("validationEvidenceSummary"), evidence ? `数据独立性：${status(evidence.independenceStatus)}；标签与成员合同：${evidence.labelsAndMembersMatched === true ? "服务端已核验" : "尚无核验记录"}；预测后读标签：${evidence.labelReadAfterPrediction === true ? "服务端已记录" : "尚无记录"}。` : "尚无本次数据、协议、模型或评价源码的可追溯绑定；不能推定科学验收完成。");
    text($("validationEvidenceDetails"), object(evidence));
    renderMetricRows(report); renderReliability(report);
    region($("validationCalibration"), report.calibration, () => definition($("validationCalibration"), [["校准状态", report.calibration && report.calibration.status], ["校准证据", report.calibration || null]]));
    region($("validationApplicability"), report.applicability, () => definition($("validationApplicability"), [["适用域状态", report.applicability && report.applicability.status], ["来源与限制", report.applicability || null]]));
    const negative = { currentRun: report.negativeTransfer || null, developmentReference: catalog && catalog.negativeTransfer || null };
    region($("validationNegativeTransfer"), negative, () => { const pre = node("pre", object(negative)); $("validationNegativeTransfer").replaceChildren(node("p", "本次独立运行与历史开发观察分别记录；不能把开发指标转为新盲测证据。"), pre); });
    const limitations = [...new Set([...(catalog && catalog.limitations || []), ...(report.limitations || []), "研究原型，不构成临床诊断或人体风险概率。", "独立数据、校准或适用域证据缺失时，不能声称筛选可靠性已验证。", "旧 test 与历史 hERG_Karim 已曝光；不用于新模型选择或调参。", "少样本任务和开发集负迁移观察不能替代独立评价。", "软件测试、模型部署和自然语言报告通过，不代表科学准确率改善。"] )];
    region($("validationLimitations"), limitations, () => $("validationLimitations").replaceChildren(...limitations.map((value) => node("li", object(value)))));
  }
  async function refreshHistory() {
    const generation = ++historyGeneration; $("validationHistoryRefresh").disabled = true;
    try {
      const value = await api("/api/research/validation/runs");
      if (generation !== historyGeneration) return;
      if (value.schema !== Core.SCHEMA || !Array.isArray(value.runs)) throw new Error("验证运行历史格式不兼容。");
      const rows = value.runs.filter((item) => Core.validId(item.runId)).map((item) => {
        const row = node("div", null, "validation-history-row"), open = node("button", "只读查看", "secondary-button"); open.type = "button"; open.disabled = state.pending;
        open.addEventListener("click", () => restore(item.runId)); row.append(node("span", `${item.runId} · ${status(item.status)} · ${item.datasetId || "数据集未提供"}`), open); return row;
      });
      $("validationHistory").replaceChildren(...(rows.length ? rows : [node("p", "暂无已保存的独立验证运行。") ]));
    } catch (error) { if (generation === historyGeneration) text($("validationHistory"), `历史读取失败：${error.message}`); }
    finally { if (generation === historyGeneration) $("validationHistoryRefresh").disabled = false; }
  }
  async function exportReport() {
    if (!Core.exportable(state)) return;
    if (runSelection !== null && Core.signature(fields()) !== runSelection) {
      edited(); text($("validationError"), "配置已修改，原报告不能按新配置导出。"); $("validationError").hidden = false; return;
    }
    const generation = state.generation, runId = state.run.runId, selected = Core.signature(fields());
    try {
      const value = await api(`/api/research/validation/reports/${encodeURIComponent(runId)}`);
      if (generation !== state.generation || !Core.exportable(state) || Core.signature(fields()) !== selected) throw new Error("配置已修改，原报告不能按新配置导出。");
      if (!value || value.schema !== Core.SCHEMA || value.runId && value.runId !== runId) throw new Error("聚合报告合同或运行 ID 不一致。");
      const blob = new Blob([JSON.stringify(Core.sanitize(value), null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob), link = node("a"); link.href = url; link.download = `aidd-validation-${runId}.json`; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { text($("validationError"), error.message); $("validationError").hidden = false; }
  }
  $("validationForm").addEventListener("submit", (event) => { event.preventDefault(); start("evaluate"); });
  $("validationAudit").addEventListener("click", () => start("audit")); $("validationCancel").addEventListener("click", cancel);
  $("validationRefresh").addEventListener("click", refreshCatalog); $("validationHistoryRefresh").addEventListener("click", refreshHistory); $("validationExport").addEventListener("click", exportReport);
  for (const id of ["validationDataset", ...Object.values(MODELS)]) $(id).addEventListener("change", edited);
  window.addEventListener("pagehide", stopReads);
  window.addEventListener("pageshow", (event) => { if (event.persisted && state.run && state.pending && !state.stale) poll(state.generation, state.run.runId); });
  render(); refreshCatalog().then(() => { const runId = remembered(); if (runId) restore(runId); }); refreshHistory();
})();
