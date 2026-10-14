(function () {
  "use strict";
  const Core = window.CompareCore;
  const $ = (id) => document.getElementById(id);
  const NAMES = { "toxnet-v3-b16": "ToxNet v3 B16", "txgemma-2b-predict": "TxGemma 2B", "ollama-qwen-local": "Qwen · Ollama", "deepseek-api": "DeepSeek API" };
  const ROLES = { "toxnet-v3-b16": "主要预测 · 16 任务", "txgemma-2b-predict": "专项预测 · 官方 TDC 合同", "ollama-qwen-local": "主报告 · 本机独立生成", "deepseek-api": "对照报告 · 授权外传" };
  const MARKS = { "toxnet-v3-b16": "B16", "txgemma-2b-predict": "Tx", "ollama-qwen-local": "Q", "deepseek-api": "D" };
  const STATUS = { "mock-ready": "Mock 软件测试入口", available: "实际可用", installed: "当前模型已安装 · 待生成验收", "configured-unverified": "账户已配置 · 访问待核验", "not-installed": "所选模型未安装", unreachable: "本机服务不可达", unavailable: "不可用", configured: "已配置 · 待核验", unknown: "尚未检查", pending: "排队中", running: "正在运行", completed: "已完成", partial: "部分完成", failed: "失败", skipped: "已跳过", unsupported: "任务未支持", cancelled: "已取消" };
  const POSITIVE = { herg: "来源标签：hERG 通道抑制", dili: "来源标签：药物性肝损伤", ames: "Ames 致突变试验阳性", clintox: "临床毒性标签；区别于批准状态" };
  const SOURCE_KINDS = { "model-computation": "模型计算结果", "model-metadata": "模型版本与校准状态", "human-rule": "人工规则警示", "neural-attribution": "神经网络研究归因", "evidence-audit-summary": "实验来源审计摘要", "insufficient-evidence": "证据不足", limitation: "科研限制" };
  let state = Core.initialState();
  let registry = null;
  let presets = [];
  let registryGeneration = 0;
  let controller = null;
  let pollTimer = null;
  let historyGeneration = 0;
  let historyOffset = 0;
  let historyQuery = "";
  let historyPaging = { previousDisabled: true, nextDisabled: true };
  const historyLimit = 10;
  const runIdStorageKey = "aidd.compare.lastRunId";
  const renderRegion = Core.createRegionRenderer();

  function setText(element, value) { const text = String(value === undefined || value === null ? "" : value); if (element.textContent !== text) element.textContent = text; }
  function rememberedRunId() { try { const value = localStorage.getItem(runIdStorageKey); return Core.validRunId(value) ? value : null; } catch (_) { return null; } }
  function rememberRunId(value) { try { if (Core.validRunId(value)) localStorage.setItem(runIdStorageKey, value); else localStorage.removeItem(runIdStorageKey); } catch (_) {} }

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
    details.dataset.viewKey = title;
    details.dataset.readingKey = `detail:${container.id || "section"}:${title}`;
    const summary = node("summary", title); summary.dataset.focusKey = `detail:${title}`;
    details.append(summary, node("pre", objectText(value)));
    container.append(details);
  }
  function fields() {
    return {
      smiles: $("compareSmiles").value, question: $("compareQuestion").value,
      reportMode: $("compareReportMode").value, reportContract: "v2",
      specialistTask: $("compareSpecialistTask").value, ollamaModel: $("compareOllamaModel").value,
      includeTox21: $("compareIncludeTox21").checked, includeEvidence: $("compareIncludeEvidence").checked,
      evidenceEndpoint: $("compareEvidenceEndpoint").value, includeAttribution: $("compareIncludeAttribution").checked,
      explainEndpoint: $("compareExplainEndpoint").value, persistSensitiveContent: $("comparePersistSensitive").checked,
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
        error.status = response.status;
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
    updateTaskHint(); updateOptionalControls();
  }
  function metadataFingerprint(value) {
    return JSON.stringify({ testFixture: value.testFixture === true, models: value.models.map((model) => ({ id: model.id, actualModelId: model.actualModelId, revision: model.revision, configured: model.configured, available: model.available, device: model.device, runtime: model.runtime, source: metadata(model, "source"), sourceKind: metadata(model, "sourceKind"), sourceRevision: metadata(model, "sourceRevision"), upstreamRevisionStatus: metadata(model, "upstreamRevisionStatus"), generationPolicy: metadata(model, "generationPolicy"), installedModels: model.installedModels || model.metadata && model.metadata.installedModels, selectedDefaultModel: model.selectedDefaultModel || model.metadata && model.metadata.selectedDefaultModel, weightsSha256: model.weightsSha256 || model.metadata && model.metadata.weightsSha256 })) });
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
    const readiness = Core.selectedModelReadiness(registry, "ollama-qwen-local", $("compareOllamaModel").value);
    $("compareOllamaHint").textContent = readiness.status === "installed" ? `当前选择 ${readiness.modelId}，已核验 digest；尚需本次真实报告生成验收。` : readiness.reason || "Ollama 不可用；先启动本机服务并安装有许可的 Qwen 模型，再刷新状态。";
    const deepseek = registry.models.find((item) => item.id === "deepseek-api");
    const cloudOption = node("option", deepseek.actualModelId || "未配置 DeepSeek 账户模型"); cloudOption.value = deepseek.actualModelId || "";
    replace($("compareDeepSeekModel"), cloudOption);
    updateTaskHint(); updateOptionalControls();
  }
  function updateOptionalControls() {
    $("compareEvidenceEndpoint").disabled = !$("compareIncludeEvidence").checked;
    $("compareExplainEndpoint").disabled = !$("compareIncludeAttribution").checked;
    if (registry) {
      const readiness = Core.selectedModelReadiness(registry, "ollama-qwen-local", $("compareOllamaModel").value);
      setText($("compareOllamaHint"), readiness.reason || "当前本机模型尚未通过真实生成验收。");
    }
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
      else if (state.viewingHistory && !state.stale && !state.restoring && state.run && state.run.request) {
        // History can load before the slower GPU/model registry check. Once
        // real choices arrive, restore settings without treating that initial
        // population as a user edit or restoring either per-run permission.
        applyFormSettings(state.run.request, true);
        state = { ...state, fingerprint: $("compareSmiles").value ? Core.fingerprint(Core.buildRequest(fields())) : null };
      }
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
    if (state.inFlight || state.cancelling || state.restoring) return;
    if (!registry) { setText($("compareError"), "请先完成真实模型状态检查；检查失败时可使用“刷新模型”重试。"); $("compareError").hidden = false; return; }
    let request;
    try {
      request = Core.buildRequest(fields());
      if (request.comparisonReporter && !request.allowCloudTransfer) throw new Error("已选择 DeepSeek 对照；请明确勾选本次云端外传授权，或关闭 DeepSeek 对照。");
      if (location.protocol === "file:") throw new Error("请通过已启动的本机 FastAPI /compare 页面打开工作台。");
    } catch (error) { $("compareError").textContent = error.message; $("compareError").hidden = false; return; }
    stopPolling();
    dispatch({ type: "START", request });
    // Consume both explicit permissions for this submission immediately.
    // Waiting for the ACK would allow an edit/retry to inherit permission while
    // an earlier POST is still pending, or let its late ACK clear newer consent.
    $("compareAllowCloud").checked = false;
    $("comparePersistSensitive").checked = false;
    const generation = state.generation;
    controller = new AbortController();
    try {
      // Do not abort this brief create call on edits: its ID is needed to cancel
      // a server run that was accepted just before the UI changed.
      const accepted = await api("/api/research/compare/runs", { method: "POST", body: JSON.stringify(request) });
      if (!accepted || accepted.contractVersion !== Core.CONTRACT || typeof accepted.runId !== "string") throw new Error("创建运行响应格式不兼容。");
      if (generation !== state.generation) { quietlyCancel(accepted.runId); return; }
      rememberRunId(accepted.runId);
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
      else if (generation === state.generation && !$("compareHistoryPanel").hidden) refreshHistory();
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
    $("comparePersistSensitive").checked = false;
    rememberRunId(null);
    dispatch({ type: "RESET" });
    if (wasRunning) quietlyCancel(runId);
    updateRegistryControlsIfReady();
  }
  function updateRegistryControlsIfReady() { if (registry) updateRegistryControls(); }
  function modelResult(id) { return state.run && state.run.models && state.run.models[id]; }
  function renderCards() {
    for (const id of Core.MODEL_IDS) {
      const entry = registry && registry.models.find((item) => item.id === id);
      const result = modelResult(id);
      const readiness = Core.selectedModelReadiness(registry, id, $("compareOllamaModel").value);
      const status = result ? result.status : readiness.status;
      let card = Array.from($("compareModelCards").children).find((item) => item.dataset.modelId === id);
      if (!card) { card = node("article", null, "compare-model-card"); card.dataset.modelId = id; $("compareModelCards").append(card); }
      const provenance = result && result.provenance || result && result.data && result.data.provenance || {};
      const fixture = registry && registry.testFixture === true || Core.fixtureResult(result);
      renderRegion(card, { readiness, entry, status, error: result && result.error, provenance, stale: state.stale, summaryOnly: Core.summaryOnly(state.run), fixture }, () => {
      replace(card); card.dataset.state = status;
      const title = node("div", null, "compare-model-card-title");
      const copy = node("div"); copy.append(node("h3", NAMES[id]), node("p", ROLES[id], "compare-model-role"));
      title.append(node("span", MARKS[id], "compare-model-monogram"), copy);
      const generated = result && result.status === "completed" && ["ollama-qwen-local", "deepseek-api"].includes(id);
      const badgeText = generated ? fixture ? "已完成 Mock 软件验证" : "已完成真实报告生成" : fixture && status === "completed" ? "已完成 Mock 软件验证" : STATUS[status] || status;
      const badge = node("span", `${badgeText}${state.stale ? " · 旧记录" : ""}`, "compare-state"); badge.dataset.state = status;
      const reason = fixture && result ? "Mock 测试夹具输出，仅验证软件流程，不是真实模型推理。" : Core.summaryOnly(state.run) && result ? "已恢复历史执行摘要，正文没有保存；没有重新调用模型。" : result && result.error ? result.error.message : result && result.status === "completed" ? "输出已完成，来源和性能记录见下方。" : result && result.status === "skipped" ? "本次未选择或未授权该调用。" : readiness.reason || "尚未核验，注册项不代表模型已安装。";
      const details = node("details", null, "compare-model-details"); details.dataset.viewKey = `model:${id}`;
      const list = node("dl");
      // A historical execution keeps its own runtime/source, even when today's
      // registry uses another package. Missing historical metadata stays unknown.
      const hasRunProvenance = Boolean(result && Object.keys(provenance).length);
      const execution = hasRunProvenance ? provenance : entry;
      const sourceRepository = metadata(execution, "sourceRepository");
      const sourceRevision = metadata(execution, "sourceRevision");
      const source = metadata(execution, "sourceUrl") || (sourceRepository ? `${sourceRepository}${sourceRevision ? `@${sourceRevision}` : ""}` : metadata(execution, "source"));
      const sourceKind = metadata(execution, "sourceKind");
      const upstreamStatus = metadata(execution, "upstreamRevisionStatus");
      const licenseStatus = metadata(execution, "licenseStatus");
      const records = [
        [fixture ? "Mock 模型 ID" : "实际执行 / 当前模型 ID", provenance.returnedModel || provenance.actualModelId || provenance.modelId || provenance.modelVersion || readiness.modelId],
        [fixture ? "夹具 revision / digest" : "revision / digest", provenance.revision || provenance.manifestSha256 || provenance.artifactSha256 || provenance.modelVersion || readiness.revision],
        ["当前配置选择", readiness.modelId],
        ["运行方式", metadata(execution, "runtime")], ["执行设备", metadata(execution, "device")],
        ["配置状态", entry ? entry.configured === true ? "已配置" : "未配置" : "尚未检查"],
        ["任务语义", entry && entry.semantics],
        ["许可状态", licenseStatus === "operator-accepted" ? "operator-accepted（操作者声明，非独立法律认证）" : licenseStatus], ["任务能力", entry && entry.capabilities],
        ["来源", source],
        ["研究限制", entry && entry.researchOnly ? "researchOnly=true · formalAvailability=false" : "语言整理不构成实验验证"],
      ];
      if (sourceKind) records.push(["来源审核类型", sourceKind === "third-party-quantized" ? "第三方量化来源（third-party-quantized）；不是已验证的官方 safetensors revision" : sourceKind]);
      if (sourceRevision) records.push(["来源仓库固定修订", sourceRevision]);
      if (upstreamStatus) records.push(["上游权重修订审核", upstreamStatus === "unknown-not-published-by-quantizer" ? "unknown-not-published-by-quantizer（量化发布者未公布上游修订）" : upstreamStatus]);
      if (metadata(execution, "ggufSha256")) records.push(["GGUF 文件 SHA256", metadata(execution, "ggufSha256")]);
      if (metadata(execution, "quantization")) records.push(["量化格式", metadata(execution, "quantization")]);
      if (id === "ollama-qwen-local") {
        if (provenance.generationOptions) records.push(["本次生成配置", provenance.generationOptions]);
        if (metadata(entry, "generationPolicy")) records.push(["当前生成预算（配置值，非显存实测）", metadata(entry, "generationPolicy")]);
      }
      if (metadata(entry, "statusReason") || metadata(entry, "reason")) records.push(["当前模型状态说明", metadata(entry, "statusReason") || metadata(entry, "reason")]);
      for (const [label, value] of records) list.append(node("dt", label), node("dd", objectText(value)));
      const summary = node("summary", "ID、许可与执行信息"); summary.dataset.focusKey = `model:${id}:summary`;
      details.append(summary, list);
      card.append(title, badge, node("p", reason, "compare-model-reason"), details);
      });
    }
  }
  function tableRow(values) { const row = node("tr"); for (const value of values) row.append(typeof value === "object" && value instanceof Node ? value : node("td", value)); return row; }
  function scoreCell(score) {
    const cell = node("td", null, "compare-score-cell");
    cell.append(node("span", Core.formatScore(score), "compare-score-value"));
    if (typeof score === "number") { const track = node("div", null, "compare-score-track"); const fill = node("span"); fill.style.width = `${score * 100}%`; track.append(fill); cell.append(track); }
    return cell;
  }
  function renderPrediction(run) {
    const rows = Core.endpointRows(run);
    renderRegion($("compareEndpointRows"), rows, () => replace($("compareEndpointRows"), ...rows.map((item) => tableRow([item.name, scoreCell(item.score), item.positiveClass || POSITIVE[item.id], "未建立同标签直接对照"])))) ;
    $("comparePathwayPanel").hidden = run.request && run.request.includeTox21 === false;
    const pathways = Core.pathwayRows(run);
    renderRegion($("comparePathwayRows"), pathways, () => replace($("comparePathwayRows"), ...pathways.map((item) => tableRow([item.name, scoreCell(item.score), item.positiveClass]))));
    renderRegion($("compareSpecialistResult"), { result: run.models && run.models["txgemma-2b-predict"], contracts: registry && registry.specialistTasks }, () => renderSpecialist(run));
  }
  function renderSpecialist(run) {
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
    if (Core.summaryOnly(run)) { target.append(node("p", "原运行报告正文未保存。本次只恢复执行摘要，不重新调用语言模型；没有可重新展开的原报告。", "compare-hint")); return; }
    if (!report || !Array.isArray(report.sections)) {
      target.append(node("p", result && result.error && result.error.message || (result && result.status === "skipped" ? "本次未选择或未授权。" : "尚未生成可核验的结构化研究报告。"), "compare-hint"));
      return;
    }
    const snapshotFacts = new Map(((run.snapshot && run.snapshot.facts) || []).map((fact) => [fact.id, fact]));
    const reportInfo = Core.reportMetadata(run, report);
    target.append(node("p", `报告合同 / 模式：${reportInfo.label}`, "compare-source-meta"));
    target.append(node("p", `事实快照：${report.snapshotSha256 || "未提供"}`, "compare-source-meta"));
    for (const section of report.sections) {
      const block = node("section", null, "compare-report-block");
      block.dataset.readingKey = `report:${id}:${section.id}`;
      block.append(node("h4", section.title || section.id || "研究说明"));
      const known = (section.claimIds || []).every((claim) => snapshotFacts.has(claim));
      // Render authoritative facts from the immutable snapshot rather than
      // treating a generator's narration as verified scientific evidence.
      const citedFacts = (section.claimIds || []).map((claim) => snapshotFacts.get(claim)).filter(Boolean);
      for (const fact of citedFacts) {
        const factBlock = node("div", null, "compare-verified-fact");
        factBlock.append(node("span", "已核验快照事实", "compare-fact-label"), node("p", fact.text || objectText(fact.value)), node("div", `事实 ${fact.id} · 来源 ${(fact.sourceIds || []).join("、") || "无外部实验来源"} · ${SOURCE_KINDS[fact.sourceKind] || fact.sourceKind || "来源类型未提供"}`, "compare-source-meta"));
        block.append(factBlock);
      }
      // Legacy report text was deterministically rebuilt by the server. The
      // snapshot remains the display authority; missing facts stay unverified.
      if (!citedFacts.length && section.text) block.append(node("p", `未核验的报告文字：${section.text}`, "compare-unverified"));
      block.append(node("div", (section.claimIds || []).length ? `${known ? "事实 ID 对应当前快照" : "含无法对应快照的 ID · 未核验"}：${(section.claimIds || []).join("、")}` : "没有可追溯事实引用；该段不能视为已确认实验事实。", "compare-source-meta"));
      const commentary = Array.isArray(section.commentary) ? section.commentary : section.commentary ? [section.commentary] : [];
      for (const text of commentary) if (text) {
        const generated = node("div", null, "compare-generated-explanation");
        generated.append(node("span", "模型生成解释 · 未验证", "compare-generated-label"), node("p", `未验证的语言推理：${objectText(text)}`, "compare-unverified"));
        block.append(generated);
      }
      target.append(block);
    }
    if (report.unverifiedAdditions && report.unverifiedAdditions.length) target.append(node("p", `${report.unverifiedAdditions.length} 条新增语言推理均为 unverified，不属于已确认的实验事实。`, "compare-source-meta"));
    appendDetails(target, "报告来源核验记录", report.sourceVerification || { status: report.status, verifiedFactIds: report.verifiedFactIds });
  }
  function renderFactAudit(run) {
    const facts = run.snapshot && run.snapshot.facts || [];
    const list = node("ul", null, "compare-audit-list");
    for (const fact of facts) {
      const item = node("li"); item.append(node("strong", `${fact.id} · ${SOURCE_KINDS[fact.sourceKind] || fact.sourceKind || "来源类型未提供"}`), node("div", fact.text || objectText(fact.value)), node("div", `来源 ID：${(fact.sourceIds || []).join("、") || "未提供"}`, "compare-source-meta")); list.append(item);
    }
    replace($("compareFactAudit"));
    $("compareFactAudit").append(facts.length ? list : node("p", "尚无已核验事实快照。", "compare-hint"));
    if (Core.summaryOnly(run)) $("compareFactAudit").append(node("p", "原事实快照全文没有保存；保留的预测摘要属于原运行记录，不是本次新推理。", "compare-hint"));
    appendDetails($("compareFactAudit"), "快照来源及核验状态", run.snapshot && { schema: run.snapshot.schema, snapshotSha256: run.snapshot.snapshotSha256, sources: run.snapshot.sources });
    const interpretation = run.snapshot && run.snapshot.questionInterpretation;
    if (interpretation) appendDetails($("compareFactAudit"), "中文研究问题确定性识别", interpretation);
  }
  function renderDifferences(run) {
    replace($("compareDifferences"));
    appendDetails($("compareDifferences"), "独立输出差异与报告核验汇总", run.comparison);
    $("compareDifferences").append(node("p", "输出差异和延迟不等同于科学准确率；没有新的合法独立标签时不作准确率排名。", "compare-hint"));
  }
  function renderOptional(run) {
    const prediction = run.models && run.models["toxnet-v3-b16"];
    const data = prediction && prediction.data || {};
    const container = $("compareEvidenceContent"); replace(container);
    const includeAttribution = run.request && (Object.hasOwn(run.request, "includeAttribution") ? run.request.includeAttribution === true : Boolean(run.request.explainEndpoint));
    $("compareEvidencePanel").hidden = !(run.request && (run.request.includeEvidence || includeAttribution));
    if (Core.summaryOnly(run)) { container.append(node("p", "检索到的实验记录和归因全文未保存，本次只恢复运行摘要。", "compare-hint")); return; }
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
      const stateText = `${STATUS[item && item.status] || "未记录"} / 排队 ${Core.formatMetric(m.queueWaitMs, "ms")}`;
      const cleanup = m.cleanupState;
      const cleanupText = cleanup && cleanup.confirmed === true ? "已确认释放" : cleanup && cleanup.confirmed === false ? "释放未确认" : m.resourceCleanupVerified === true ? "已确认释放" : "未测量";
      return tableRow([NAMES[id], stateText, `${Core.formatMetric(m.durationMs, "ms")} / 生成 ${Core.formatMetric(m.generationSeconds, "s")}`, `${Core.formatMetric(m.promptTokens, "", 0)} / ${Core.formatMetric(m.completionTokens, "", 0)}`, Core.formatMetric(m.tokensPerSecond, "token/s"), cost, memory, cleanupText]);
    }); replace($("compareMetricsRows"), ...rows);
  }
  function renderResourceStatus(run) {
    // Only the live run endpoint supplies current ownership. Registry checks
    // and restored historical resources must not masquerade as current GPU use.
    const resource = run && run.resourceState;
    let text = "GPU 当前模型：未测量；服务端尚未提供实时资源状态。";
    if (resource) {
      const active = resource.activeModel;
      const currentLabel = state.stale ? "旧记录的 GPU 调度快照" : "GPU 调度当前阶段";
      text = `${currentLabel}：${active ? NAMES[active] || active : Object.hasOwn(resource, "activeModel") ? "当前无 Compare 占用" : "未测量"}；此状态不代表整卡显存测量。`;
      if (resource.blocked === true) text += ` 资源已隔离：${resource.blockedReason || "清理未确认"}。`;
      const waiting = Core.MODEL_IDS.filter((id) => run.models && run.models[id] && run.models[id].status === "pending");
      if (waiting.length && state.inFlight) text += ` 等待阶段：${waiting.map((id) => NAMES[id]).join(" → ")}。`;
    }
    if (run) {
      const legacy = run.metrics || {};
      const queueWaitMs = Object.hasOwn(run, "queueWaitMs") ? run.queueWaitMs : legacy.queueWaitMs;
      const durationMs = Object.hasOwn(run, "durationMs") ? run.durationMs : legacy.durationMs;
      text += ` 本次入队等待：${Core.formatMetric(queueWaitMs, "ms")}；已记录总耗时：${Core.formatMetric(durationMs, "ms")}。`;
    }
    setText($("compareResourceStatus"), text);
  }
  function captureReadingPosition() {
    if (!window.scrollY || typeof document.elementFromPoint !== "function") return null;
    const visible = document.elementFromPoint(Math.round(window.innerWidth * .7), Math.min(180, Math.round(window.innerHeight / 3)));
    const anchor = visible && visible.closest("[data-reading-key], [id]");
    return anchor ? { anchor, id: anchor.id, key: anchor.dataset.readingKey, top: anchor.getBoundingClientRect().top, x: window.scrollX, y: window.scrollY } : null;
  }
  function restoreReadingPosition(position) {
    if (!position) return;
    const anchor = position.anchor.isConnected ? position.anchor : position.id ? document.getElementById(position.id) : Array.from(document.querySelectorAll("[data-reading-key]")).find((item) => item.dataset.readingKey === position.key);
    if (!anchor) return;
    if (typeof anchor.getClientRects === "function" && !anchor.getClientRects().length) return;
    const delta = anchor.getBoundingClientRect().top - position.top;
    if (Number.isFinite(delta) && Math.abs(delta) >= 1) window.scrollTo({ left: position.x, top: Math.max(0, position.y + delta), behavior: "instant" });
  }
  function render() {
    const position = captureReadingPosition();
    try { renderContents(); } finally { restoreReadingPosition(position); }
  }
  function renderContents() {
    const fixture = registry && registry.testFixture === true || Core.fixtureRun(state.run);
    $("compareFixtureBanner").hidden = !fixture;
    setText($("compareFixtureBanner"), registry && registry.testFixture === true ? "Mock 测试夹具：所有输出仅验证软件交互，不是真实模型推理。" : fixture ? "当前历史运行包含 Mock 测试夹具输出：只用于软件交互验证，不可视为真实模型推理或科研结果。" : "");
    renderCards();
    $("compareRunButton").disabled = !registry || state.inFlight || state.cancelling || state.restoring;
    $("compareCancelButton").disabled = !state.run || Core.TERMINAL.has(state.run.status) || state.stale || state.cancelling || state.restoring;
    setText($("compareRunButton"), state.restoring ? "正在读取历史记录…" : state.inFlight ? "正在执行独立对照…" : !registry ? "等待模型状态检查…" : "↗ 运行 Compare");
    $("compareStaleBanner").hidden = !state.stale;
    $("compareError").hidden = !state.error;
    if (state.error) $("compareError").textContent = state.error;
    $("compareExportButton").disabled = !state.run || state.stale || !Core.TERMINAL.has(state.run.status);
    const run = state.run;
    renderResourceStatus(run);
    const summary = Core.summaryOnly(run);
    setText($("compareExportButton"), summary ? "导出摘要 JSON" : "导出 JSON");
    $("compareEmptyState").hidden = Boolean(run || state.inFlight);
    $("compareResults").hidden = !run;
    if (!run) {
      setText($("compareRunStatus"), state.inFlight ? "正在提交研究运行；模型在服务端依次执行。" : "准备输入一个分子，开始可追溯的研究对照。");
      $("compareRecoveryNotice").hidden = true;
      return;
    }
    setText($("compareRunStatus"), state.restoring ? `正在只读恢复运行 ${run.runId}…` : state.cancelling ? "正在取消后续执行；已经成功的输出会保留。" : `${STATUS[run.status] || run.status} · ${run.runId}${state.viewingHistory ? " · 历史查询（未重新调用模型）" : ""}${state.stale ? " · 输入或配置已修改" : ""}`);
    const recoveryText = summary ? "完整敏感内容未保存：仅恢复结构标识、真实执行状态及已记录的预测摘要；SMILES、研究问题、实验记录和报告正文无法恢复。本次没有重新调用模型，也没有复用云端授权。"
      : state.viewingHistory ? "已只读恢复本机运行记录。查看历史不会再次运行模型，云端及敏感内容保存授权均保持关闭。"
      : run.contentSaved === false ? "当前全文仍可在当次运行内存中查看，但默认只持久化摘要。后台重启后全文无法恢复，请及时导出需要保留的 JSON。" : "";
    $("compareRecoveryNotice").hidden = !recoveryText; setText($("compareRecoveryNotice"), recoveryText);
    setText($("compareCanonical"), run.structure && run.structure.canonicalSmiles || (summary ? "查询分子全文未保存" : "结构验证中或未完成"));
    setText($("compareStructureId"), run.structure && run.structure.structureId || "未提供");
    setText($("compareRunId"), run.runId);
    setText($("compareSnapshotHash"), run.snapshot && run.snapshot.snapshotSha256 || run.snapshotSha256 || (summary ? "事实快照全文未保存" : "事实快照尚未完成"));
    setText($("compareReportMetadata"), Core.reportMetadata(run).label);
    renderPrediction(run);
    for (const [id, targetId] of [["ollama-qwen-local", "compareQwenContent"], ["deepseek-api", "compareDeepSeekContent"]]) {
      const result = run.models && run.models[id];
      const report = run.reports && run.reports[id] || result && result.data && (result.data.report || result.data);
      renderRegion($(targetId), { report, status: result && result.status, error: result && result.error, facts: run.snapshot && run.snapshot.facts, reportConfiguration: run.request || run.reportConfiguration, summary }, () => renderReport(run, id, $(targetId)));
    }
    renderRegion($("compareFactAudit"), { snapshot: run.snapshot, summary }, () => renderFactAudit(run));
    renderRegion($("compareDifferences"), run.comparison, () => renderDifferences(run));
    const primary = run.models && run.models["toxnet-v3-b16"];
    renderRegion($("compareEvidenceContent"), { evidence: primary && primary.data && primary.data.evidence, attribution: primary && primary.data && primary.data.attribution, request: run.request && { includeEvidence: run.request.includeEvidence, includeAttribution: run.request.includeAttribution, explainEndpoint: run.request.explainEndpoint }, summary }, () => renderOptional(run));
    renderRegion($("compareMetricsRows"), Core.MODEL_IDS.map((id) => {
      const item = run.models && run.models[id];
      return { status: item && item.status, metrics: item && item.metrics };
    }), () => renderMetrics(run));
    const limitations = [...new Set([...(run.limitations || []), "研究原型，不构成临床诊断或人体风险概率。", "B16 未独立校准，适用域未完成验证。ClinTox 少样本，可能发生负迁移。", "旧 test 和历史 hERG_Karim 属于已曝光参考，不用于本轮模型选择。", "归因不等于因果机制；自然语言推理不等于科学机制验证。"])] ;
    renderRegion($("compareLimitations"), limitations, () => replace($("compareLimitations"), ...limitations.map((text) => node("li", objectText(text)))));
  }
  function exportJson() {
    try {
      if (state.fingerprint) {
        const submitted = JSON.parse(state.fingerprint);
        const current = Core.buildRequest(fields());
        // Consent is consumed after acceptance. All other current form values
        // must still match, including programmatic edits with no input event.
        current.allowCloudTransfer = submitted.allowCloudTransfer;
        current.persistSensitiveContent = submitted.persistSensitiveContent;
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
  function applyFormSettings(config, restore = false) {
    const values = restore ? Core.restoredFields(config) : config || {};
    if (restore) { $("compareSmiles").value = values.smiles; $("compareQuestion").value = values.question; }
    const taskAllowed = registry && (registry.specialistTasks || []).some((task) => task.id === values.specialistTask && task.supported === true);
    $("compareSpecialistTask").value = taskAllowed ? values.specialistTask : "";
    $("compareOllamaModel").value = Core.installedOllamaModels(registry).some((model) => model.name === values.ollamaModel) ? values.ollamaModel : "";
    $("compareReportMode").value = Core.REPORT_MODES.includes(values.reportMode) ? values.reportMode : "fast";
    $("compareIncludeTox21").checked = values.includeTox21 !== false;
    $("compareIncludeEvidence").checked = values.includeEvidence === true;
    $("compareEvidenceEndpoint").value = Core.ENDPOINT_IDS.includes(values.evidenceEndpoint) ? values.evidenceEndpoint : "";
    $("compareIncludeAttribution").checked = Object.hasOwn(values, "includeAttribution") ? values.includeAttribution === true : Boolean(values.explainEndpoint);
    $("compareExplainEndpoint").value = Core.ENDPOINT_IDS.includes(values.explainEndpoint) ? values.explainEndpoint : "herg";
    $("compareEnableDeepSeek").checked = restore ? false : values.comparisonReporter === "deepseek-api";
    $("compareAllowCloud").checked = false;
    $("comparePersistSensitive").checked = false;
    updateOptionalControls(); updateTaskHint();
  }
  async function restoreRun(runId) {
    if (!Core.validRunId(runId)) return;
    stopPolling();
    dispatch({ type: "RESTORE_REQUEST", runId });
    const generation = state.generation;
    controller = new AbortController();
    try {
      const run = Core.validateRun(await api(`/api/research/compare/runs/${encodeURIComponent(runId)}`, { signal: controller.signal }));
      if (generation !== state.generation) return;
      applyFormSettings(run.request, true);
      let request = null;
      if ($("compareSmiles").value) request = Core.buildRequest(fields());
      rememberRunId(runId);
      dispatch({ type: "RESTORED", generation, runId, run, request });
      if (state.inFlight) poll(generation, runId);
    } catch (error) {
      if (generation !== state.generation || error.name === "AbortError") return;
      if (error.status === 404) rememberRunId(null);
      dispatch({ type: "ERROR", generation, message: `无法恢复历史记录：${error.message}。只执行了查询，没有重新运行模型。` });
    }
  }
  async function refreshHistory(offset = historyOffset) {
    const generation = ++historyGeneration;
    const requestedOffset = Math.max(0, offset);
    $("compareHistoryRefresh").disabled = true;
    $("compareHistoryPrevious").disabled = true;
    $("compareHistoryNext").disabled = true;
    setText($("compareHistoryStatus"), "正在读取本机运行摘要…");
    try {
      const history = Core.validateHistory(await api(`/api/research/compare/runs?limit=${historyLimit}&offset=${requestedOffset}${historyQuery ? `&q=${encodeURIComponent(historyQuery)}` : ""}`));
      if (generation !== historyGeneration) return;
      historyOffset = history.offset;
      renderRegion($("compareHistoryRows"), history.runs, () => {
        const rows = history.runs.map((item) => {
          const open = node("button", "只读打开", "secondary-button"); open.type = "button"; open.dataset.focusKey = `history:${item.runId}:open`;
          open.addEventListener("click", () => restoreRun(item.runId));
          const action = node("td"); action.append(open);
          const identity = node("td"); identity.append(node("div", STATUS[item.status] || item.status), node("code", item.structureId || item.structure && item.structure.structureId || "结构标识未记录", "compare-source-meta"), node("div", Core.reportMetadata(item).label, "compare-source-meta"));
          const models = Core.MODEL_IDS.map((id) => { const model = item.models && item.models[id]; const status = typeof model === "string" ? model : model && model.status; return `${NAMES[id]}：${STATUS[status] || status || "未记录"}`; }).join("\n");
          const modelCell = node("td", models, "compare-history-models");
          const timestamp = item.createdAt && !Number.isNaN(Date.parse(item.createdAt)) ? new Date(item.createdAt).toLocaleString("zh-CN") : "未提供";
          return tableRow([timestamp, identity, modelCell, item.contentSaved === true ? "已显式保存完整内容" : "仅持久化摘要 · 全文未保存", action]);
        });
        if (!rows.length) { const row = node("tr"); const cell = node("td", historyQuery ? "没有匹配的已保存运行摘要。" : "暂无可恢复的本机运行记录。"); cell.colSpan = 5; row.append(cell); rows.push(row); }
        replace($("compareHistoryRows"), ...rows);
      });
      $("compareHistoryPrevious").disabled = history.offset === 0;
      $("compareHistoryNext").disabled = !history.hasMore;
      historyPaging = { previousDisabled: history.offset === 0, nextDisabled: !history.hasMore };
      setText($("compareHistoryPage"), `第 ${Math.floor(history.offset / history.limit) + 1} 页 · 共 ${history.total} 条`);
      setText($("compareHistoryStatus"), "历史查询完成；不会重新调用四模型。");
    } catch (error) {
      if (generation === historyGeneration) {
        $("compareHistoryPrevious").disabled = historyPaging.previousDisabled;
        $("compareHistoryNext").disabled = historyPaging.nextDisabled;
        setText($("compareHistoryStatus"), `读取历史失败：${error.message}`);
      }
    }
    finally { if (generation === historyGeneration) $("compareHistoryRefresh").disabled = false; }
  }
  function toggleHistory() {
    const open = $("compareHistoryPanel").hidden;
    $("compareHistoryPanel").hidden = !open;
    $("compareHistoryButton").setAttribute("aria-expanded", String(open));
    if (open) refreshHistory();
  }
  function updatePresetActions() {
    const selected = presets.find((item) => item.id === $("comparePresetSelect").value);
    $("compareUpdatePreset").disabled = !selected;
    $("compareDeletePreset").disabled = !selected;
    setText($("comparePresetUpdated"), selected ? `更新时间：${selected.updatedAt || selected.createdAt || "原配置未记录时间"}` : "");
  }
  async function refreshPresets() {
    try {
      const value = await api("/api/research/compare/presets");
      presets = Array.isArray(value.presets) ? value.presets : [];
      const selectedId = $("comparePresetSelect").value;
      const blank = node("option", "选择预设…"); blank.value = ""; replace($("comparePresetSelect"), blank);
      for (const preset of presets) { const option = node("option", preset.name); option.value = preset.id; $("comparePresetSelect").append(option); }
      if (presets.some((item) => item.id === selectedId)) $("comparePresetSelect").value = selectedId;
      updatePresetActions();
    } catch (error) { $("comparePresetStatus").textContent = `无法读取配置：${error.message}`; }
  }
  async function savePreset() {
    let request;
    try {
      const name = $("comparePresetName").value.trim();
      if (!name) throw new Error("请先填写配置名称。");
      request = Core.buildRequest({ ...fields(), smiles: "C", question: "", allowCloudTransfer: false });
      $("compareSavePreset").disabled = true;
      const created = await api("/api/research/compare/presets", { method: "POST", body: JSON.stringify({ name, settings: Core.presetSettings(request) }) });
      $("comparePresetStatus").textContent = "配置已保存；不包含分子、问题、密钥、云端授权或敏感内容保存授权。";
      await refreshPresets();
      if (created && presets.some((item) => item.id === created.id)) { $("comparePresetSelect").value = created.id; updatePresetActions(); }
    } catch (error) { $("comparePresetStatus").textContent = error.message; }
    finally { $("compareSavePreset").disabled = false; }
  }
  async function updatePreset() {
    const preset = presets.find((item) => item.id === $("comparePresetSelect").value); if (!preset) return;
    const name = $("comparePresetName").value.trim();
    if (!name) { setText($("comparePresetStatus"), "请先填写新配置名称。"); return; }
    if (!window.confirm(`确认将“${preset.name}”更新为“${name}”，并用当前设置覆盖原配置？云端及敏感内容授权不会保存。`)) return;
    $("compareUpdatePreset").disabled = true;
    try {
      const request = Core.buildRequest({ ...fields(), smiles: "C", question: "", allowCloudTransfer: false, persistSensitiveContent: false });
      await api(`/api/research/compare/presets/${encodeURIComponent(preset.id)}`, { method: "PATCH", body: JSON.stringify({ name, settings: Core.presetSettings(request) }) });
      await refreshPresets();
      setText($("comparePresetStatus"), "配置已更新；同名冲突由服务端明确拒绝，不覆盖其他预设。");
    } catch (error) { setText($("comparePresetStatus"), error.message); }
    finally { updatePresetActions(); }
  }
  async function deletePreset() {
    const preset = presets.find((item) => item.id === $("comparePresetSelect").value); if (!preset) return;
    if (!window.confirm(`确认删除本机配置“${preset.name}”？这不会删除模型、实验记录或当前结果。`)) return;
    $("compareDeletePreset").disabled = true;
    try {
      await api(`/api/research/compare/presets/${encodeURIComponent(preset.id)}`, { method: "DELETE" });
      await refreshPresets();
      setText($("comparePresetStatus"), "已删除选中配置。");
    } catch (error) { setText($("comparePresetStatus"), error.message); }
    finally { updatePresetActions(); }
  }
  function applyPreset() {
    updatePresetActions();
    const preset = presets.find((item) => item.id === $("comparePresetSelect").value); if (!preset) return;
    $("comparePresetName").value = preset.name;
    applyFormSettings(preset.settings, false);
    edited("研究配置已切换");
    $("comparePresetStatus").textContent = "已加载配置；本次云端外传及敏感内容保存均需单独授权。";
  }
  $("compareForm").addEventListener("submit", submit);
  for (const id of ["compareSmiles", "compareQuestion"]) $(id).addEventListener("input", () => edited("分子或研究问题已修改"));
  for (const id of ["compareSpecialistTask", "compareOllamaModel", "compareReportMode", "compareIncludeTox21", "compareIncludeEvidence", "compareEvidenceEndpoint", "compareIncludeAttribution", "compareExplainEndpoint", "compareEnableDeepSeek", "compareAllowCloud", "comparePersistSensitive"]) $(id).addEventListener("change", () => edited("任务、报告模式或模型设置已修改"));
  $("compareForm").addEventListener("keydown", (event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); submit(); } });
  $("compareCancelButton").addEventListener("click", cancel);
  $("compareResetButton").addEventListener("click", reset);
  $("compareExportButton").addEventListener("click", exportJson);
  $("compareRefreshModels").addEventListener("click", async () => {
    await refreshRegistry();
    if (state.run && state.error && !state.stale && !Core.TERMINAL.has(state.run.status)) { state = { ...state, inFlight: true, error: null }; render(); poll(state.generation, state.run.runId); }
  });
  $("compareSavePreset").addEventListener("click", savePreset);
  $("compareUpdatePreset").addEventListener("click", updatePreset);
  $("compareDeletePreset").addEventListener("click", deletePreset);
  $("comparePresetSelect").addEventListener("change", applyPreset);
  $("compareHistoryButton").addEventListener("click", toggleHistory);
  $("compareHistoryRefresh").addEventListener("click", () => refreshHistory());
  function searchHistory() { historyQuery = $("compareHistoryQuery").value.trim().slice(0, 120); refreshHistory(0); }
  $("compareHistorySearch").addEventListener("click", searchHistory);
  $("compareHistoryQuery").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); searchHistory(); } });
  $("compareHistoryPrevious").addEventListener("click", () => refreshHistory(historyOffset - historyLimit));
  $("compareHistoryNext").addEventListener("click", () => refreshHistory(historyOffset + historyLimit));
  window.addEventListener("pagehide", stopPolling);
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    // A browser back/forward cache restores the old JS context without running
    // bootstrap again. Resume reads of an active run; never repeat its POST.
    $("compareAllowCloud").checked = false;
    $("comparePersistSensitive").checked = false;
    refreshRegistry();
    if (state.run && !state.stale && !Core.TERMINAL.has(state.run.status)) {
      if (state.restoring) restoreRun(state.run.runId);
      else { state = { ...state, inFlight: true, cancelling: false, error: null }; render(); poll(state.generation, state.run.runId); }
    }
  });
  updateOptionalControls(); render(); refreshRegistry(); refreshPresets();
  const lastRunId = rememberedRunId();
  if (lastRunId) restoreRun(lastRunId);
})();
