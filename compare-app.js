(function () {
  "use strict";
  const Core = window.CompareCore;
  const $ = (id) => document.getElementById(id);
  const NAMES = { "toxnet-v3-b16": "ToxNet v3 B16", "txgemma-2b-predict": "TxGemma 2B", "ollama-qwen-local": "Qwen · Ollama", "deepseek-api": "DeepSeek API" };
  const ROLES = { "toxnet-v3-b16": "主要预测 · 16 任务", "txgemma-2b-predict": "专项预测 · 官方 TDC 合同", "ollama-qwen-local": "主报告 · 本机独立生成", "deepseek-api": "对照报告 · 授权外传" };
  const MARKS = { "toxnet-v3-b16": "B16", "txgemma-2b-predict": "Tx", "ollama-qwen-local": "Q", "deepseek-api": "D" };
  const STATUS = { available: "实际可用", unavailable: "不可用", configured: "已配置 · 待核验", unknown: "尚未检查", pending: "排队中", running: "正在运行", completed: "已完成", partial: "部分完成", failed: "失败", skipped: "已跳过", unsupported: "任务未支持", cancelled: "已取消" };
  const POSITIVE = { herg: "来源标签：hERG 通道抑制", dili: "来源标签：药物性肝损伤", ames: "Ames 致突变试验阳性", clintox: "临床毒性标签；区别于批准状态" };
  let state = Core.initialState();
  let registry = null;
  let presets = [];
  let registryGeneration = 0;
  let controller = null;
  let pollTimer = null;

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== null && text !== undefined) element.textContent = String(text);
    if (className) element.className = className;
    return element;
  }
  function replace(element, ...children) { element.replaceChildren(...children); }
  function objectText(value) {
    if (value === null || value === undefined) return "未提供";
    return typeof value === "object" ? JSON.stringify(Core.sanitizeExport(value), null, 2) : String(value);
  }
  function appendDetails(container, title, value) {
    if (value === null || value === undefined) return;
    const details = node("details", null, "compare-detail");
    details.append(node("summary", title), node("pre", objectText(value)));
    container.append(details);
  }
  function fields() {
    return {
      smiles: $("compareSmiles").value, question: $("compareQuestion").value,
      specialistTask: $("compareSpecialistTask").value, ollamaModel: $("compareOllamaModel").value,
      includeTox21: $("compareIncludeTox21").checked, includeEvidence: $("compareIncludeEvidence").checked,
      explainEndpoint: $("compareExplainEndpoint").value,
      comparisonEnabled: $("compareEnableDeepSeek").checked, allowCloudTransfer: $("compareAllowCloud").checked,
    };
  }
  async function api(path, options = {}) {
    const timeout = new AbortController();
    const timer = setTimeout(() => timeout.abort(), 45000);
    const signals = options.signal ? AbortSignal.any([options.signal, timeout.signal]) : timeout.signal;
    try {
      const response = await fetch(path, { ...options, signal: signals, cache: "no-store", credentials: "same-origin", headers: { "Accept": "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}), ...options.headers } });
      let value;
      try { value = await response.json(); } catch (_) { throw new Error("服务返回了无效 JSON；请检查 API 是否已启动。"); }
      if (!response.ok) {
        const error = new Error(value.error && value.error.message || value.detail && typeof value.detail === "string" && value.detail || `请求失败（HTTP ${response.status}）`);
        error.code = value.error && value.error.code;
        throw error;
      }
      return value;
    } catch (error) {
      if (timeout.signal.aborted && !(options.signal && options.signal.aborted)) throw new Error("API 请求超时；运行可能仍在服务端执行，可以重试状态查询或取消。");
      throw error;
    } finally { clearTimeout(timer); }
  }
  function stopPolling() {
    if (pollTimer !== null) clearTimeout(pollTimer);
    pollTimer = null;
    if (controller) controller.abort();
    controller = null;
  }
  function dispatch(event) { state = Core.reduce(state, event); render(); }
  function quietlyCancel(runId) {
    if (runId) api(`/api/research/compare/runs/${encodeURIComponent(runId)}`, { method: "DELETE" }).catch(() => {});
  }
  function edited(reason) {
    const runId = state.run && state.run.runId;
    const wasRunning = state.inFlight;
    stopPolling();
    dispatch({ type: "EDIT" });
    if (wasRunning && runId) quietlyCancel(runId);
    if (state.stale) $("compareRunStatus").textContent = `${reason || "输入或配置已修改"}，旧结果已失效。请重新运行。`;
    updateTaskHint();
  }
  function metadataFingerprint(value) {
    return JSON.stringify({ testFixture: value.testFixture === true, models: value.models.map((model) => ({ id: model.id, actualModelId: model.actualModelId, revision: model.revision, configured: model.configured, available: model.available, device: model.device, installedModels: model.installedModels || model.metadata && model.metadata.installedModels, selectedDefaultModel: model.selectedDefaultModel || model.metadata && model.metadata.selectedDefaultModel, weightsSha256: model.weightsSha256 || model.metadata && model.metadata.weightsSha256 })) });
  }
  function metadata(model, key) { return model && (model[key] !== undefined ? model[key] : model.metadata && model.metadata[key]); }
  function updateRegistryControls() {
    const oldTask = $("compareSpecialistTask").value;
    replace($("compareSpecialistTask"), node("option", "跳过专项预测"));
    $("compareSpecialistTask").firstChild.value = "";
    for (const task of registry.specialistTasks || []) {
      const option = node("option", `${task.name || task.id}${task.supported === true ? "" : " · 未审核/不支持"}`);
      option.value = task.id;
      option.disabled = task.supported !== true;
      $("compareSpecialistTask").append(option);
    }
    if ((registry.specialistTasks || []).some((task) => task.id === oldTask && task.supported === true)) $("compareSpecialistTask").value = oldTask;
    const qwen = registry.models.find((item) => item.id === "ollama-qwen-local");
    const installed = Core.installedOllamaModels(registry);
    const oldModel = $("compareOllamaModel").value;
    const defaultModel = metadata(qwen, "selectedDefaultModel") || qwen.actualModelId;
    replace($("compareOllamaModel"));
    if (!installed.length) {
      const option = node("option", "未发现可用的本机 Qwen 模型"); option.value = ""; $("compareOllamaModel").append(option);
    } else {
      const automatic = node("option", "使用服务端默认 Qwen（需真实匹配）"); automatic.value = ""; $("compareOllamaModel").append(automatic);
      for (const item of installed) {
        const option = node("option", item.name); option.value = item.name; $("compareOllamaModel").append(option);
      }
      $("compareOllamaModel").value = installed.some((item) => item.name === oldModel) ? oldModel : installed.some((item) => item.name === defaultModel) ? defaultModel : "";
    }
    $("compareOllamaHint").textContent = qwen.available ? `已发现 ${installed.length} 个可选模型，实际 ID 与 digest 记录在状态卡。` : qwen.reason || "Ollama 不可用；先启动本机服务并安装有许可的 Qwen 模型，再刷新状态。";
    const deepseek = registry.models.find((item) => item.id === "deepseek-api");
    const cloudOption = node("option", deepseek.actualModelId || "未配置 DeepSeek 账户模型"); cloudOption.value = deepseek.actualModelId || "";
    replace($("compareDeepSeekModel"), cloudOption);
    updateTaskHint();
  }
  function updateTaskHint() {
    const taskId = $("compareSpecialistTask").value;
    const task = registry && (registry.specialistTasks || []).find((item) => item.id === taskId);
    $("compareTaskHint").textContent = task ? `${task.positiveClass || task.outputMeaning || task.description || "官方任务分类语义"}。${task.directComparable === true ? "任务合同可比性须以服务端审核结果为准。" : "与原四毒性端点不直接可比；类别文本不转换为概率。"}` : "只提供已审核的官方任务合同；分类文本不转换成概率。";
  }
  async function refreshRegistry() {
    const generation = ++registryGeneration;
    $("compareRegistryStatus").textContent = "正在检查真实配置与本机模型…";
    $("compareRefreshModels").disabled = true;
    try {
      const next = Core.validateRegistry(await api("/api/research/models"));
      if (generation !== registryGeneration) return;
      const changed = registry && metadataFingerprint(next) !== metadataFingerprint(registry);
      registry = next;
      $("compareFixtureBanner").hidden = next.testFixture !== true;
      $("compareFixtureBanner").textContent = next.testFixture === true ? "Mock 测试夹具：所有输出仅验证软件交互，不是真实模型推理。" : "";
      updateRegistryControls();
      if (changed) edited("模型实际配置或修订已变化");
      $("compareRegistryStatus").textContent = "已检查 · 配置状态不等同于执行成功";
      const concurrency = registry.resourcePolicy && registry.resourcePolicy.localModelConcurrency;
      const ttl = registry.runPolicy && registry.runPolicy.terminalRecordTtlSeconds;
      $("compareResourcePolicy").textContent = `本机 GPU 模型按顺序排队${typeof concurrency === "number" ? `，最多 ${concurrency} 个本机模型同时执行` : ""}；不自动切换云端。${typeof ttl === "number" ? `完成记录在 ${Math.round(ttl / 60)} 分钟后过期，请及时导出。` : "运行记录仅在本机服务进程内保留，请及时导出。"}`;
      render();
    } catch (error) {
      if (generation !== registryGeneration) return;
      $("compareRegistryStatus").textContent = error.message;
      $("compareError").textContent = error.message; $("compareError").hidden = false;
    } finally { if (generation === registryGeneration) $("compareRefreshModels").disabled = false; }
  }
  async function submit(event) {
    if (event) event.preventDefault();
    if (state.inFlight || state.cancelling) return;
    let request;
    try {
      request = Core.buildRequest(fields());
      if (request.comparisonReporter && !request.allowCloudTransfer) throw new Error("已选择 DeepSeek 对照；请明确勾选本次云端外传授权，或关闭 DeepSeek 对照。");
      if (location.protocol === "file:") throw new Error("请通过已启动的本机 FastAPI /compare 页面打开工作台。");
    } catch (error) { $("compareError").textContent = error.message; $("compareError").hidden = false; return; }
    stopPolling();
    dispatch({ type: "START", request });
    const generation = state.generation;
    controller = new AbortController();
    try {
      // Do not abort this brief create call on edits: its ID is needed to cancel
      // a server run that was accepted just before the UI changed.
      const accepted = await api("/api/research/compare/runs", { method: "POST", body: JSON.stringify(request) });
      if (!accepted || accepted.contractVersion !== Core.CONTRACT || typeof accepted.runId !== "string") throw new Error("创建运行响应格式不兼容。");
      $("compareAllowCloud").checked = false;
      if (generation !== state.generation) { quietlyCancel(accepted.runId); return; }
      dispatch({ type: "ACK", generation, runId: accepted.runId });
      poll(generation, accepted.runId);
    } catch (error) {
      if (generation === state.generation && error.name !== "AbortError") dispatch({ type: "ERROR", generation, message: error.message });
    }
  }
  async function poll(generation, runId) {
    if (generation !== state.generation || !state.inFlight || state.cancelling) return;
    if (!controller || controller.signal.aborted) controller = new AbortController();
    try {
      const run = Core.validateRun(await api(`/api/research/compare/runs/${encodeURIComponent(runId)}`, { signal: controller.signal }));
      dispatch({ type: "POLL", generation, runId, run });
      if (generation === state.generation && state.inFlight) pollTimer = setTimeout(() => poll(generation, runId), 1000);
    } catch (error) {
      if (generation === state.generation && error.name !== "AbortError") {
        // Preserve the run ID and successful outputs if the status request fails.
        dispatch({ type: "ERROR", generation, message: `${error.message}（运行 ${runId}；可以刷新此运行状态。）` });
      }
    }
  }
  async function cancel() {
    if (!state.run || Core.TERMINAL.has(state.run.status) || state.stale || state.cancelling) return;
    const runId = state.run.runId;
    stopPolling();
    dispatch({ type: "CANCEL_REQUEST" });
    const generation = state.generation;
    try {
      const run = Core.validateRun(await api(`/api/research/compare/runs/${encodeURIComponent(runId)}`, { method: "DELETE" }));
      dispatch({ type: "CANCELLED", generation, runId, run });
      if (generation === state.generation && state.inFlight) poll(generation, runId);
    } catch (error) { dispatch({ type: "ERROR", generation, message: `取消请求未确认：${error.message}。可以刷新运行状态确认服务端是否已停止。` }); }
  }
  function reset() {
    const runId = state.run && state.run.runId;
    const wasRunning = state.inFlight;
    stopPolling();
    $("compareForm").reset();
    $("compareAllowCloud").checked = false;
    dispatch({ type: "RESET" });
    if (wasRunning) quietlyCancel(runId);
    updateRegistryControlsIfReady();
  }
  function updateRegistryControlsIfReady() { if (registry) updateRegistryControls(); }
  function modelResult(id) { return state.run && state.run.models && state.run.models[id]; }
  function renderCards() {
    const open = new Set(Array.from($("compareModelCards").querySelectorAll("details[open]")).map((item) => item.dataset.modelId));
    const cards = Core.MODEL_IDS.map((id) => {
      const entry = registry && registry.models.find((item) => item.id === id);
      const result = modelResult(id);
      const status = result ? result.status : entry ? entry.available ? "available" : "unavailable" : "unknown";
      const card = node("article", null, "compare-model-card"); card.dataset.state = status;
      const title = node("div", null, "compare-model-card-title");
      const copy = node("div"); copy.append(node("h3", NAMES[id]), node("p", ROLES[id], "compare-model-role"));
      title.append(node("span", MARKS[id], "compare-model-monogram"), copy);
      const badge = node("span", STATUS[status] || status, "compare-state"); badge.dataset.state = status;
      const reason = result && result.error ? result.error.message : result && result.status === "completed" ? "输出已完成，来源和性能记录见下方。" : result && result.status === "skipped" ? "本次未选择或未授权该调用。" : entry && entry.reason || (entry && entry.available ? "通过当前配置与本机检查，执行仍需单独验收。" : "尚未核验，注册项不代表模型已安装。");
      const details = node("details", null, "compare-model-details"); details.dataset.modelId = id; details.open = open.has(id);
      const list = node("dl");
      const provenance = result && result.provenance || result && result.data && result.data.provenance || {};
      const records = [
        ["实际模型 ID", provenance.returnedModel || provenance.actualModelId || provenance.modelId || entry && entry.actualModelId],
        ["revision / digest", provenance.revision || provenance.modelVersion || entry && entry.revision],
        ["运行方式", entry && entry.runtime], ["执行设备", provenance.device || entry && entry.device],
        ["配置状态", entry ? entry.configured === true ? "已配置" : "未配置" : "尚未检查"],
        ["任务语义", entry && entry.semantics],
        ["许可状态", entry && entry.licenseStatus], ["任务能力", entry && entry.capabilities],
        ["来源", provenance.source || entry && (entry.source || entry.sourceUrl)],
        ["研究限制", entry && entry.researchOnly ? "researchOnly=true · formalAvailability=false" : "语言整理不构成实验验证"],
      ];
      for (const [label, value] of records) list.append(node("dt", label), node("dd", objectText(value)));
      details.append(node("summary", "ID、许可与执行信息"), list);
      card.append(title, badge, node("p", reason, "compare-model-reason"), details);
      return card;
    });
    replace($("compareModelCards"), ...cards);
  }
  function tableRow(values) { const row = node("tr"); for (const value of values) row.append(typeof value === "object" && value instanceof Node ? value : node("td", value)); return row; }
  function scoreCell(score) {
    const cell = node("td", null, "compare-score-cell");
    cell.append(node("span", Core.formatScore(score), "compare-score-value"));
    if (typeof score === "number") { const track = node("div", null, "compare-score-track"); const fill = node("span"); fill.style.width = `${score * 100}%`; track.append(fill); cell.append(track); }
    return cell;
  }
  function renderPrediction(run) {
    replace($("compareEndpointRows"), ...Core.endpointRows(run).map((item) => tableRow([item.name, scoreCell(item.score), item.positiveClass || POSITIVE[item.id], "未建立同标签直接对照"]))) ;
    $("comparePathwayPanel").hidden = run.request && run.request.includeTox21 === false;
    replace($("comparePathwayRows"), ...Core.pathwayRows(run).map((item) => tableRow([item.name, scoreCell(item.score), item.positiveClass])));
    const tx = run.models && run.models["txgemma-2b-predict"];
    const container = $("compareSpecialistResult"); replace(container);
    const tasks = Core.specialistTasks(run);
    if (!tasks.length) { container.append(node("p", tx && tx.error && tx.error.message || "本次尚无可用的专项任务输出。", "compare-hint")); return; }
    for (const output of tasks) {
      const contract = registry && (registry.specialistTasks || []).find((task) => task.id === output.taskId);
      const table = node("table"); const body = node("tbody");
      const classText = `${output.class ? `${output.class}: ` : ""}${output.classLabel || output.output && output.output.classLabel || output.label || "未提供"}`;
      for (const [label, value] of [["TDC 原始任务", output.tdcTask || output.taskName || output.task && (output.task.tdcName || output.task.name) || output.taskId], ["输出类型", output.outputType || "类别文本"], ["类别输出", classText], ["类别含义 / 阳性定义", output.meaning || output.outputMeaning || output.positiveClass || contract && contract.positiveClass], ["与四毒性端点直接可比", output.directComparable === true ? "仅按审核后的对应任务合同" : "否；不得转换为四端点概率"], ["模板与权重来源", output.templateSource || output.templateSourceUrl || contract && contract.sourceUrl || tx.provenance]]) body.append(tableRow([label, objectText(value)]));
      table.append(body); container.append(table);
      appendDetails(container, "专项输出合同与来源", { output, provenance: tx.provenance });
    }
  }
  function renderReport(run, id, target) {
    replace(target);
    const result = run.models && run.models[id];
    const report = run.reports && run.reports[id] || result && result.data && (result.data.report || result.data);
    if (!report || !Array.isArray(report.sections)) {
      target.append(node("p", result && result.error && result.error.message || (result && result.status === "skipped" ? "本次未选择或未授权。" : "尚未生成可核验的结构化研究报告。"), "compare-hint"));
      return;
    }
    const snapshotFacts = new Map(((run.snapshot && run.snapshot.facts) || []).map((fact) => [fact.id, fact]));
    target.append(node("p", `事实快照：${report.snapshotSha256 || "未提供"}`, "compare-source-meta"));
    for (const section of report.sections) {
      const block = node("section", null, "compare-report-block");
      block.append(node("h4", section.title || section.id || "研究说明"));
      const known = (section.claimIds || []).every((claim) => snapshotFacts.has(claim));
      if (section.text) block.append(node("p", section.text));
      block.append(node("div", (section.claimIds || []).length ? `${known ? "事实 ID 对应当前快照" : "含无法对应快照的 ID · 未核验"}：${(section.claimIds || []).join("、")}` : "没有可追溯事实引用；该段不能视为已确认实验事实。", "compare-source-meta"));
      const commentary = Array.isArray(section.commentary) ? section.commentary : section.commentary ? [section.commentary] : [];
      for (const text of commentary) if (text) block.append(node("p", `未验证的语言推理：${objectText(text)}`, "compare-unverified"));
      target.append(block);
    }
    if (report.unverifiedAdditions && report.unverifiedAdditions.length) target.append(node("p", `${report.unverifiedAdditions.length} 条新增语言推理均为 unverified，不属于已确认的实验事实。`, "compare-source-meta"));
    appendDetails(target, "报告来源核验记录", report.sourceVerification || { status: report.status, verifiedFactIds: report.verifiedFactIds });
  }
  function renderAudit(run) {
    const facts = run.snapshot && run.snapshot.facts || [];
    const list = node("ul", null, "compare-audit-list");
    for (const fact of facts) {
      const item = node("li"); item.append(node("strong", `${fact.id} · ${fact.sourceKind || "来源类型未提供"}`), node("div", fact.text || objectText(fact.value)), node("div", `来源 ID：${(fact.sourceIds || []).join("、") || "未提供"}`, "compare-source-meta")); list.append(item);
    }
    replace($("compareFactAudit"));
    $("compareFactAudit").append(facts.length ? list : node("p", "尚无已核验事实快照。", "compare-hint"));
    appendDetails($("compareFactAudit"), "快照来源及核验状态", run.snapshot && { schema: run.snapshot.schema, snapshotSha256: run.snapshot.snapshotSha256, sources: run.snapshot.sources });
    replace($("compareDifferences"));
    appendDetails($("compareDifferences"), "独立输出差异与报告核验汇总", run.comparison);
    $("compareDifferences").append(node("p", "输出差异和延迟不等同于科学准确率；没有新的合法独立标签时不作准确率排名。", "compare-hint"));
  }
  function renderOptional(run) {
    const prediction = run.models && run.models["toxnet-v3-b16"];
    const data = prediction && prediction.data || {};
    const container = $("compareEvidenceContent"); replace(container);
    $("compareEvidencePanel").hidden = !(run.request && (run.request.includeEvidence || run.request.explainEndpoint));
    if (data.evidence) appendDetails(container, "本机实验检索与来源定义", data.evidence);
    if (data.attribution) {
      const image = data.attribution.visualization && data.attribution.visualization.imageDataUrl || data.attribution.imageDataUrl || data.attribution.image;
      if (typeof image === "string" && /^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(image)) { const img = node("img", null, "compare-evidence-image"); img.src = image; img.alt = "实际加载模型的研究性分子归因"; container.append(img); }
      appendDetails(container, "实际模型归因、遮蔽限制与权重关联", data.attribution);
    }
    if (!data.evidence && !data.attribution) container.append(node("p", "没有可用的实验或归因来源；证据不足。", "compare-hint"));
    container.append(node("p", "最大 Tanimoto 不代表预测置信度。不同实验阈值不能默认为一致，结构归因不代表毒性因果机制。", "compare-hint"));
  }
  function renderMetrics(run) {
    const rows = Core.MODEL_IDS.map((id) => {
      const item = run.models && run.models[id]; const m = item && item.metrics || {}; const gpu = m.gpu || {};
      const cost = typeof m.costAmount === "number" ? Core.formatMetric(m.costAmount, m.costCurrency || "（币种未提供）", 6) : "未测量";
      const peak = Core.gpuMemoryMiB(gpu, "allocated");
      const reserved = Core.gpuMemoryMiB(gpu, "reserved");
      const memory = typeof peak === "number" ? `${Core.formatMetric(peak, "MiB")} allocated${typeof reserved === "number" ? ` / ${Core.formatMetric(reserved, "MiB")} reserved` : ""}` : "未测量";
      return tableRow([NAMES[id], Core.formatMetric(m.durationMs, "ms"), `${Core.formatMetric(m.promptTokens, "", 0)} / ${Core.formatMetric(m.completionTokens, "", 0)}`, Core.formatMetric(m.tokensPerSecond, "token/s"), cost, memory]);
    }); replace($("compareMetricsRows"), ...rows);
  }
  function render() {
    renderCards();
    $("compareRunButton").disabled = state.inFlight || state.cancelling;
    $("compareCancelButton").disabled = !state.run || Core.TERMINAL.has(state.run.status) || state.stale || state.cancelling;
    $("compareRunButton").textContent = state.inFlight ? "正在执行独立对照…" : "↗ 运行 Compare";
    $("compareStaleBanner").hidden = !state.stale;
    $("compareError").hidden = !state.error;
    if (state.error) $("compareError").textContent = state.error;
    $("compareExportButton").disabled = !state.run || state.stale || !Core.TERMINAL.has(state.run.status);
    const run = state.run;
    $("compareEmptyState").hidden = Boolean(run || state.inFlight);
    $("compareResults").hidden = !run;
    if (!run) {
      $("compareRunStatus").textContent = state.inFlight ? "正在提交研究运行；模型在服务端依次执行。" : "准备输入一个分子，开始可追溯的研究对照。";
      return;
    }
    $("compareRunStatus").textContent = state.cancelling ? "正在取消后续执行；已经成功的输出会保留。" : `${STATUS[run.status] || run.status} · ${run.runId}${state.stale ? " · 输入或配置已修改" : ""}`;
    $("compareCanonical").textContent = run.structure && run.structure.canonicalSmiles || "结构验证中或未完成";
    $("compareStructureId").textContent = run.structure && run.structure.structureId || "未提供";
    $("compareRunId").textContent = run.runId;
    $("compareSnapshotHash").textContent = run.snapshot && run.snapshot.snapshotSha256 || "事实快照尚未完成";
    renderPrediction(run);
    renderReport(run, "ollama-qwen-local", $("compareQwenContent"));
    renderReport(run, "deepseek-api", $("compareDeepSeekContent"));
    renderAudit(run); renderOptional(run); renderMetrics(run);
    const limitations = [...new Set([...(run.limitations || []), "研究原型，不构成临床诊断或人体风险概率。", "B16 未独立校准，适用域未完成验证。ClinTox 少样本，可能发生负迁移。", "旧 test 和历史 hERG_Karim 属于已曝光参考，不用于本轮模型选择。", "归因不等于因果机制；自然语言推理不等于科学机制验证。"])] ;
    replace($("compareLimitations"), ...limitations.map((text) => node("li", objectText(text))));
  }
  function exportJson() {
    try {
      if (state.fingerprint) {
        const submitted = JSON.parse(state.fingerprint);
        const current = Core.buildRequest(fields());
        // Consent is consumed after acceptance. All other current form values
        // must still match, including programmatic edits with no input event.
        current.allowCloudTransfer = submitted.allowCloudTransfer;
        if (Core.fingerprint(current) !== state.fingerprint) {
          edited("当前输入或配置已变化");
          throw new Error("当前输入或配置已变化；请重新运行后导出。");
        }
      }
      const packet = Core.exportRun(state);
      const blob = new Blob([JSON.stringify(packet, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob); const link = node("a"); link.href = url; link.download = `aidd-compare-${state.run.runId}.json`; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { $("compareError").textContent = error.message; $("compareError").hidden = false; }
  }
  async function refreshPresets() {
    try {
      const value = await api("/api/research/compare/presets");
      presets = Array.isArray(value.presets) ? value.presets : [];
      const blank = node("option", "选择预设…"); blank.value = ""; replace($("comparePresetSelect"), blank);
      for (const preset of presets) { const option = node("option", preset.name); option.value = preset.id; $("comparePresetSelect").append(option); }
    } catch (error) { $("comparePresetStatus").textContent = `无法读取配置：${error.message}`; }
  }
  async function savePreset() {
    let request;
    try {
      const name = $("comparePresetName").value.trim();
      if (!name) throw new Error("请先填写配置名称。");
      request = Core.buildRequest({ ...fields(), smiles: "C", question: "", allowCloudTransfer: false });
      $("compareSavePreset").disabled = true;
      await api("/api/research/compare/presets", { method: "POST", body: JSON.stringify({ name, settings: Core.presetSettings(request) }) });
      $("comparePresetStatus").textContent = "配置已保存；不包含分子、问题、密钥或云端授权。";
      await refreshPresets();
    } catch (error) { $("comparePresetStatus").textContent = error.message; }
    finally { $("compareSavePreset").disabled = false; }
  }
  function applyPreset() {
    const preset = presets.find((item) => item.id === $("comparePresetSelect").value); if (!preset) return;
    const config = preset.settings || {};
    const taskAllowed = registry && (registry.specialistTasks || []).some((task) => task.id === config.specialistTask && task.supported === true);
    $("compareSpecialistTask").value = taskAllowed ? config.specialistTask : "";
    $("compareOllamaModel").value = Core.installedOllamaModels(registry).some((model) => model.name === config.ollamaModel) ? config.ollamaModel : "";
    $("compareIncludeTox21").checked = config.includeTox21 !== false;
    $("compareIncludeEvidence").checked = config.includeEvidence === true;
    $("compareExplainEndpoint").value = Core.ENDPOINT_IDS.includes(config.explainEndpoint) ? config.explainEndpoint : "";
    $("compareEnableDeepSeek").checked = config.comparisonReporter === "deepseek-api";
    $("compareAllowCloud").checked = false;
    edited("研究配置已切换");
    $("comparePresetStatus").textContent = "已加载配置；本次云端外传仍需单独授权。";
  }
  $("compareForm").addEventListener("submit", submit);
  for (const id of ["compareSmiles", "compareQuestion"]) $(id).addEventListener("input", () => edited("分子或研究问题已修改"));
  for (const id of ["compareSpecialistTask", "compareOllamaModel", "compareIncludeTox21", "compareIncludeEvidence", "compareExplainEndpoint", "compareEnableDeepSeek", "compareAllowCloud"]) $(id).addEventListener("change", () => edited("任务或模型设置已修改"));
  $("compareForm").addEventListener("keydown", (event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); submit(); } });
  $("compareCancelButton").addEventListener("click", cancel);
  $("compareResetButton").addEventListener("click", reset);
  $("compareExportButton").addEventListener("click", exportJson);
  $("compareRefreshModels").addEventListener("click", async () => {
    await refreshRegistry();
    if (state.run && state.error && !state.stale && !Core.TERMINAL.has(state.run.status)) { state = { ...state, inFlight: true, error: null }; render(); poll(state.generation, state.run.runId); }
  });
  $("compareSavePreset").addEventListener("click", savePreset);
  $("comparePresetSelect").addEventListener("change", applyPreset);
  window.addEventListener("pagehide", stopPolling);
  render(); refreshRegistry(); refreshPresets();
})();
