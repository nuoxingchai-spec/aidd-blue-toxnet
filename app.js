(function () {
  "use strict";

  const core = window.ToxicityCore;
  if (!core) throw new Error("ToxicityCore failed to load.");
  const SAMPLES = [
    { id: "acetaminophen", name: "对乙酰氨基酚", note: "DILI 示例", smiles: "CC(=O)NC1=CC=C(O)C=C1" },
    { id: "terfenadine", name: "特非那定", note: "hERG 示例", smiles: "CC(C)(C)C1=CC=C(C=C1)C(O)CCCN1CCC(CC1)C(C2=CC=CC=C2)(C3=CC=CC=C3)O" },
    { id: "nitrofurantoin", name: "呋喃妥因", note: "Ames 示例", smiles: "C1C(=O)NC(=O)N1N=CC2=CC=C(O2)[N+](=O)[O-]" },
    { id: "aspirin", name: "阿司匹林", note: "对照示例", smiles: "CC(=O)OC1=CC=CC=C1C(=O)O" },
    { id: "anthraquinone", name: "蒽醌", note: "氧化还原示例", smiles: "C1=CC=C2C(=C1)C(=O)C3=CC=CC=C3C2=O" },
    { id: "ethanol", name: "乙醇", note: "小分子示例", smiles: "CCO" },
  ];
  const ids = ["emptyState", "loadingState", "errorState", "errorMessage", "report", "smilesInput", "analyzeButton", "resetButton", "exportButton", "sampleList", "sampleCount", "reportTitle", "reportSubtitle", "evidenceBadge", "timestamp", "overallScore", "riskTier", "riskAction", "riskGauge", "gaugeValue", "highestEndpoint", "moleculeView", "atomCount", "endpointGrid", "alertCount", "alertList", "propertyGrid", "evidenceFlow", "riskSummary", "modeSelect", "providerSelect", "modeHint", "inputStatus", "apiStatus", "apiDetails", "modelChip", "healthButton", "scoreContext", "validationStatus", "configApiButton", "apiConfigDialog", "apiConfigTitle", "apiConfigForm", "closeConfigButton", "cancelConfigButton", "saveConfigButton", "configApiUrl", "configModelName", "configModelVersion", "configApiKey", "configKeyHint", "configApiFormat", "configScoreKind", "configTimeout", "configTemperature", "configSendImage", "configJsonMode", "configStatus", "configAdvanced", "localModelDialog", "localModelTitle", "localModelStatus", "localModelContent", "closeLocalModelButton", "doneLocalModelButton", "refreshLocalModelButton"];
  const elements = Object.fromEntries(ids.map((id) => [id, document.getElementById(id)]));
  const state = {
    selectedSampleId: null, result: null, resultMode: null, resultProvider: null,
    analyzedInput: null, dirty: false, requestId: 0, controller: null, loading: false,
    health: null, healthError: null, mode: "model", provider: "custom", providerInitialized: false, surface: "empty",
    modelRevision: 0, resultModelRevision: null, configProvider: null, configRequestId: 0, configController: null, configSaving: false,
    localInfoRequestId: 0, localInfoController: null,
  };
  const TIER_STYLES = {
    low: { color: "#2563eb", background: "#e7efff" },
    moderate: { color: "#b66d05", background: "#fff0d6" },
    high: { color: "#c85624", background: "#ffe5d6" },
    critical: { color: "#be3b37", background: "#fce1df" },
  };

  function escapeHtml(value) {
    return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
  }
  function normalizedInput(value) { return String(value || "").trim(); }
  function scoreValue(value) { return Number.isFinite(value) ? String(Math.round(value * 100)) : "—"; }
  function scoreText(value) { return Number.isFinite(value) ? `${scoreValue(value)} / 100` : "未运行"; }
  function scoreColor(score) {
    if (score < 0.33) return "#2563eb";
    if (score < 0.58) return "#c47b0a";
    if (score < 0.78) return "#d35f27";
    return "#be3b37";
  }
  function driverLabel(driver) { return driver?.label || driver?.name || driver?.feature || "未提供说明"; }
  function driverDetail(driver) { return driver?.detail || driver?.description || driverLabel(driver); }
  function listDrivers(value) { return Array.isArray(value) ? value : []; }
  function explanationSourceLabel(source, modelMode) {
    if (["none", "unknown", "not_provided"].includes(source)) return "未提供";
    if (["model", "model_explanation", "provider", "model-generated", "model-provided"].includes(source)) return "模型服务提供";
    if (["rule", "rules", "structural_rules", "rule_surrogate", "structure-rules"].includes(source)) return "结构规则";
    return source || (modelMode ? "未提供" : "结构规则");
  }
  function isFilePage() { return window.location?.protocol === "file:"; }
  function providers() {
    if (Array.isArray(state.health?.modelProviders)) return state.health.modelProviders;
    return [{ id: "custom", label: "配置的模型服务", ...(state.health?.model || {}) }];
  }
  function currentProvider() { return providers().find((item) => item.id === state.provider); }
  function isLocalProvider() { return state.provider === "local" || Boolean(currentProvider()?.localModel); }

  function renderSamples() {
    elements.sampleCount.textContent = `${SAMPLES.length} compounds`;
    elements.sampleList.innerHTML = SAMPLES.map((sample) => `
      <div role="listitem"><button class="sample-button" type="button" data-sample-id="${sample.id}" aria-pressed="false">
        <strong>${escapeHtml(sample.name)}</strong><span>${escapeHtml(sample.note)} · ${escapeHtml(sample.smiles)}</span>
      </button></div>`).join("");
  }
  function updateSelectedSample() {
    elements.sampleList.querySelectorAll("button[data-sample-id]").forEach((button) => {
      const selected = button.dataset.sampleId === state.selectedSampleId;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
  }
  function showSurface(surface) {
    state.surface = surface;
    elements.emptyState.hidden = surface !== "empty";
    elements.loadingState.hidden = surface !== "loading";
    elements.errorState.hidden = surface !== "error";
    elements.report.hidden = surface !== "report";
    elements.report.setAttribute("aria-busy", String(surface === "loading"));
  }
  function updateInputStatus() {
    elements.exportButton.disabled = !state.result || state.dirty || state.loading || state.surface !== "report";
    elements.inputStatus.classList.toggle("is-dirty", state.dirty);
    elements.inputStatus.textContent = state.dirty
      ? state.resultMode === "model" && state.modelRevision !== state.resultModelRevision
        ? "模型 API 配置已修改，请重新筛选。当前报告为上次结果，导出已暂停。"
        : "输入、模式或模型供应商已修改，请重新筛选。当前报告为上次结果，导出已暂停。"
      : state.loading ? "正在筛选当前输入…"
      : state.result ? "当前输入与报告一致，可以导出审计包。" : "选择示例或输入 SMILES 后开始筛选。";
  }
  function invalidateRequest() {
    state.requestId += 1;
    state.controller?.abort();
    state.controller = null;
    state.loading = false;
    elements.analyzeButton.disabled = false;
  }
  function markInputChanged() {
    const wasLoading = state.loading;
    invalidateRequest();
    state.dirty = Boolean(state.result) && (
      normalizedInput(elements.smilesInput.value) !== state.analyzedInput ||
      state.mode !== state.resultMode || (state.mode === "model" && (state.provider !== state.resultProvider || state.modelRevision !== state.resultModelRevision))
    );
    if (wasLoading) showSurface(state.result ? "report" : "empty");
    updateInputStatus();
  }
  function setError(message) {
    elements.errorMessage.textContent = message;
    showSurface("error");
    elements.exportButton.disabled = true;
  }
  async function analyzeCurrent() {
    const smiles = elements.smilesInput.value.trim();
    invalidateRequest();
    if (!smiles) {
      state.dirty = Boolean(state.result);
      updateInputStatus();
      setError("请输入 SMILES，或从示例库选择一个分子。");
      return;
    }
    if (isFilePage()) {
      setError("请先启动 API 后台，并通过 http://localhost:4173 打开工作台。直接打开 index.html 无法连接分子分析服务。");
      return;
    }
    if (state.mode === "model" && currentProvider()?.configured === false) {
      setError(isLocalProvider()
        ? "蓝研 ToxNet 尚未完成训练或模型文件不可用。请打开“模型详情”查看状态；也可主动选择规则基线。"
        : "所选模型供应商尚未配置。请配置该模型服务后重试，或主动选择“规则基线”运行独立的规则分析。");
      return;
    }
    const mode = state.mode;
    const provider = state.provider;
    const modelRevision = state.modelRevision;
    const requestId = state.requestId;
    const controller = new AbortController();
    state.controller = controller;
    state.loading = true;
    elements.analyzeButton.disabled = true;
    updateInputStatus();
    showSurface("loading");
    try {
      const result = await core.analyzeSmiles(smiles, { mode, provider, signal: controller.signal });
      if (requestId !== state.requestId || controller.signal.aborted) return;
      state.result = result;
      state.resultMode = mode;
      state.resultProvider = provider;
      state.resultModelRevision = modelRevision;
      state.analyzedInput = normalizedInput(smiles);
      state.dirty = false;
      renderReport(result);
      showSurface("report");
      if (window.innerWidth <= 820) window.scrollTo({
        top: Math.max(0, elements.report.offsetTop - 14),
        behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches ? "auto" : "smooth",
      });
    } catch (error) {
      if (requestId !== state.requestId || controller.signal.aborted) return;
      state.result = null;
      state.dirty = false;
      setError(typeof error?.message === "string" ? error.message : "分析服务返回未知错误，请检查 API 后台状态。");
    } finally {
      if (requestId === state.requestId) {
        state.loading = false;
        state.controller = null;
        elements.analyzeButton.disabled = false;
        updateInputStatus();
      }
    }
  }

  function renderProviders() {
    const available = providers();
    const previous = state.provider;
    if (!state.providerInitialized || !available.some((item) => item.id === state.provider)) {
      state.provider = available.find((item) => item.id === state.health?.defaultProvider)?.id ||
        available.find((item) => item.configured)?.id || available[0]?.id || "custom";
      state.providerInitialized = Boolean(state.health);
    }
    elements.providerSelect.innerHTML = available.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.label || item.id)}${item.configured === false ? item.localModel || item.id === "local" ? "（未训练）" : "（未配置）" : ""}</option>`).join("");
    elements.providerSelect.value = state.provider;
    if (state.provider !== previous && state.mode === "model") markInputChanged();
  }
  function renderServiceStatus() {
    const health = state.health;
    const model = currentProvider() || health?.model || {};
    const local = isLocalProvider();
    elements.providerSelect.disabled = state.mode === "rules";
    elements.configApiButton.disabled = !health || isFilePage();
    elements.configApiButton.textContent = local ? "模型详情" : "配置 API";
    elements.configApiButton.setAttribute("aria-controls", local ? "localModelDialog" : "apiConfigDialog");
    elements.modelChip.textContent = state.mode === "rules" ? "规则基线" : model.label || "真实模型";
    if (isFilePage()) {
      elements.apiStatus.textContent = "需要启动 API 后台";
      elements.apiDetails.textContent = "启动后访问 http://localhost:4173；file:// 页面无法请求分析服务。";
    } else if (health) {
      elements.apiStatus.textContent = "API 已连接 · RDKit 分子引擎";
      elements.apiDetails.textContent = model.configured
        ? `${local ? "本机自训练模型" : "模型"}：${model.modelName || "已配置"}${model.version ? ` · ${model.version}` : ""}；RDKit ${health.chemistry?.rdkitVersion || "版本未提供"}${model.supportsImage ? "；支持分子结构图输入" : ""}`
        : `${local ? "本机模型尚未训练或文件不可用" : "所选模型供应商未配置"}；可主动使用规则基线。RDKit ${health.chemistry?.rdkitVersion || "版本未提供"}`;
    } else if (state.healthError) {
      elements.apiStatus.textContent = "API 未连接";
      elements.apiDetails.textContent = "请先启动 API 后台，再刷新连接状态。默认地址：http://localhost:4173。";
    } else {
      elements.apiStatus.textContent = "正在检查 API 连接…";
      elements.apiDetails.textContent = "分子解析与分析均由 API 后台执行。";
    }
    elements.modeHint.textContent = state.mode === "rules"
      ? "主动使用结构规则基线；不会调用真实模型，也不使用示例分数替代结果。"
      : local
        ? "使用本机独立训练的分子毒性模型，无需 API 密钥。模型详情可查看真实数据来源、参数规模与测试集指标。"
        : "调用所选供应商的真实模型；模型不可用时明确报错。分数与校准状态分别报告。";
  }
  async function refreshHealth() {
    if (isFilePage()) { renderServiceStatus(); return; }
    elements.healthButton.disabled = true;
    try {
      state.health = await core.getHealth();
      state.healthError = null;
      renderProviders();
    } catch (error) {
      state.health = null;
      state.healthError = error;
    } finally {
      elements.healthButton.disabled = false;
      renderServiceStatus();
    }
  }

  const CONFIG_FIELDS = ["configApiUrl", "configModelName", "configModelVersion", "configApiKey", "configApiFormat", "configScoreKind", "configTimeout", "configTemperature", "configSendImage", "configJsonMode"];
  function configStatus(message, error = false) {
    elements.configStatus.textContent = message;
    elements.configStatus.hidden = !message;
    elements.configStatus.classList.toggle("is-error", error);
  }
  function updateConfigFormat() {
    const chat = elements.configApiFormat.value === "openai";
    if (chat) elements.configScoreKind.value = "uncalibrated_score";
    elements.configScoreKind.disabled = chat;
    elements.configJsonMode.disabled = !chat;
    elements.configTemperature.disabled = !chat;
  }
  function setConfigBusy(busy, saving = false) {
    state.configSaving = saving;
    CONFIG_FIELDS.forEach((id) => { elements[id].disabled = busy; });
    elements.saveConfigButton.disabled = busy;
    elements.closeConfigButton.disabled = saving;
    elements.cancelConfigButton.disabled = saving;
    elements.saveConfigButton.textContent = saving ? "正在保存…" : "保存配置";
    elements.apiConfigForm.setAttribute("aria-busy", String(busy));
    if (!busy) updateConfigFormat();
  }
  function fillProviderConfig(config) {
    elements.configApiUrl.value = config.apiUrl || "";
    elements.configModelName.value = config.modelName || "";
    elements.configModelVersion.value = config.modelVersion || "";
    elements.configApiKey.value = "";
    elements.configApiKey.required = !config.hasApiKey && state.configProvider !== "custom";
    elements.configApiKey.placeholder = config.hasApiKey ? "已保存密钥，留空保持当前密钥" : "粘贴服务商提供的 API Key";
    elements.configKeyHint.textContent = config.hasApiKey
      ? "密钥已保存到本机后台。留空保持当前密钥，填写新值即可替换。"
      : state.configProvider === "custom" ? "仅由本机后台使用。本机无认证的预测服务可以留空。" : "仅由本机后台使用，重新打开时不会回显。";
    elements.configApiFormat.value = config.apiFormat || "openai";
    elements.configScoreKind.value = config.scoreKind || "uncalibrated_score";
    elements.configTimeout.value = String(config.timeoutSeconds ?? 30);
    elements.configTemperature.value = config.temperature == null ? "" : String(config.temperature);
    elements.configSendImage.checked = Boolean(config.sendImage);
    elements.configJsonMode.checked = config.jsonMode !== false;
  }
  async function openApiConfig() {
    state.configController?.abort();
    const requestId = ++state.configRequestId;
    state.configProvider = state.provider;
    const controller = new AbortController();
    state.configController = controller;
    elements.apiConfigTitle.textContent = `配置 API · ${currentProvider()?.label || state.provider}`;
    CONFIG_FIELDS.forEach((id) => { elements[id].value = ""; });
    elements.configAdvanced.open = false;
    configStatus("正在读取当前供应商配置…");
    setConfigBusy(true);
    elements.apiConfigDialog.showModal();
    try {
      const config = await core.getProviderConfig(state.configProvider, { signal: controller.signal });
      if (requestId !== state.configRequestId) return;
      fillProviderConfig(config);
      setConfigBusy(false);
      configStatus("");
      elements.configModelName.focus();
    } catch (error) {
      if (requestId !== state.configRequestId || controller.signal.aborted) return;
      configStatus(error.message || "读取配置失败，请检查 API 后台连接。", true);
    }
  }
  function closeApiConfig() {
    if (state.configSaving) return;
    state.configRequestId += 1;
    state.configController?.abort();
    state.configController = null;
    elements.configApiKey.value = "";
    elements.apiConfigDialog.close();
  }
  async function saveApiConfig(event) {
    event.preventDefault();
    if (state.configSaving || !elements.apiConfigForm.reportValidity()) return;
    const key = elements.configApiKey.value.trim();
    const config = {
      apiUrl: elements.configApiUrl.value.trim(), modelName: elements.configModelName.value.trim(),
      modelVersion: elements.configModelVersion.value.trim(), apiFormat: elements.configApiFormat.value,
      scoreKind: elements.configScoreKind.value, timeoutSeconds: Number(elements.configTimeout.value),
      temperature: elements.configTemperature.value === "" ? null : Number(elements.configTemperature.value),
      sendImage: elements.configSendImage.checked, jsonMode: elements.configJsonMode.checked,
      ...(key ? { apiKey: key } : {}),
    };
    setConfigBusy(true, true);
    configStatus("正在保存到本机后台…");
    try {
      const saved = await core.saveProviderConfig(state.configProvider, config);
      elements.configApiKey.value = "";
      state.modelRevision += 1;
      if (state.mode === "model") markInputChanged();
      fillProviderConfig(saved);
      await refreshHealth();
      configStatus(saved.restartOverrideFields?.length
        ? "配置已保存并立即生效。系统环境变量会在重启时覆盖部分设置，请保持两处一致。"
        : "配置已保存并立即生效。关闭弹窗后即可开始筛选；模型可用性以实际调用结果为准。");
    } catch (error) {
      configStatus(error.message || "配置保存失败，请检查填写内容。", true);
    } finally {
      setConfigBusy(false);
    }
  }
  function countText(value) { return Number.isInteger(value) && value >= 0 ? value.toLocaleString("zh-CN") : "未提供"; }
  function metricText(value) { return Number.isFinite(value) ? value.toFixed(3) : "—"; }
  function calibrationLabel(value) {
    const status = typeof value === "string" ? value : value?.status;
    return { calibrated: "已拟合校准器", validated: "已在留出集评估", verified: "已在留出集评估", "measured-not-certified": "已评估，未认证", uncalibrated: "未校准", unknown: "未验证", unavailable: "未提供", not_calibrated: "未校准" }[status] || "未验证";
  }
  function measuredCalibrationText(calibration) {
    const statuses = calibration?.fitStatus && typeof calibration.fitStatus === "object" ? Object.values(calibration.fitStatus) : [];
    if (!statuses.length) return "已报告校准评估，拟合状态未提供，尚未认证";
    const fitted = statuses.filter((status) => status === "fitted").length;
    if (!fitted) return "校准器未拟合；原始模型概率已在内部测试集评估，尚未认证";
    if (fitted < statuses.length) return `${statuses.length} 个端点中 ${fitted} 个已拟合校准器；内部测试集已评估，尚未认证`;
    return "所有端点已拟合校准器并在内部测试集评估，尚未认证";
  }
  function endpointCalibrationText(calibration, endpointId) {
    const status = calibration?.fitStatus?.[endpointId];
    if (status === "not-fitted-insufficient-classes") return "验证集类别不足，未拟合";
    if (status === "not-fitted-optimizer-failure") return "优化失败，未拟合";
    if (status === "fitted") return "已拟合，内部评估";
    if (status) return "拟合状态未知";
    return calibrationLabel(calibration);
  }
  function sourceLink(source) {
    const name = escapeHtml(source.name || source.dataset || "数据来源未命名");
    const candidateUrl = source.documentationUrl || source.url || source.downloadUrl;
    const url = typeof candidateUrl === "string" && /^https?:\/\//i.test(candidateUrl) ? candidateUrl : null;
    const checksum = source.verifiedSha256 || source.sha256;
    return `${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${name}</a>` : name}${source.version ? ` · ${escapeHtml(source.version)}` : ""}${source.endpoint ? ` · ${escapeHtml(source.endpoint)}` : ""}${checksum ? `<span class="local-source-checksum">SHA256：${escapeHtml(checksum)}</span>` : ""}`;
  }
  function renderLocalModelInfo(info) {
    const trained = ["trained", "ready"].includes(info.status) || info.trained === true;
    const architecture = typeof info.architecture === "string" ? { name: info.architecture } : info.architecture || {};
    const data = info.data || info.dataset || {};
    const uniqueCounts = info.training?.uniqueMoleculeCounts;
    const splits = uniqueCounts || data.splits || info.splits || info.training?.splits || info.training?.splitCounts || {};
    const sources = Array.isArray(data.sources) ? data.sources : Array.isArray(info.sources) ? info.sources : Array.isArray(info.datasetProvenance) ? info.datasetProvenance : [];
    const evaluationEndpoints = info.evaluation?.endpoints || {};
    const endpoints = Array.isArray(info.endpoints) ? info.endpoints.map((endpoint) => typeof endpoint === "string" ? { id: endpoint, metrics: evaluationEndpoints[endpoint], calibration: info.calibration } : endpoint) : Object.entries(evaluationEndpoints).map(([id, metrics]) => ({ id, metrics, calibration: info.calibration }));
    const calibration = info.calibration || {};
    const applicability = info.applicability || {};
    const limitations = Array.isArray(info.limitations) ? info.limitations : [];
    elements.localModelTitle.textContent = info.name || info.modelName || "蓝研 ToxNet · 自训练模型";
    elements.localModelStatus.textContent = trained
      ? "模型已训练，可在本机进行分子毒性筛选。以下数字来自保存的训练与评估记录。"
      : info.message || "尚无可用的训练模型。完成真实数据训练后，才会显示模型评估指标并启用预测。";
    elements.localModelStatus.classList.toggle("is-error", !trained);
    const section = (title, body) => `<section class="local-info-section"><h3>${title}</h3>${body}</section>`;
    const entry = (label, value) => `<div><dt>${label}</dt><dd>${escapeHtml(value || "未提供")}</dd></div>`;
    const rows = endpoints.map((endpoint) => {
      const metrics = endpoint.metrics || endpoint.testMetrics || {};
      const counts = endpoint.counts || endpoint.splits || {};
      const endpointCount = (split) => {
        const classes = info.training?.classCounts?.[split]?.[endpoint.id];
        return counts[split] ?? (split === "validation" ? counts.val : undefined) ?? (Number.isInteger(classes?.positive) && Number.isInteger(classes?.negative) ? classes.positive + classes.negative : split === "test" ? metrics.count : undefined);
      };
      const endpointCalibration = endpointCalibrationText(endpoint.calibration || calibration, endpoint.id);
      const label = endpoint.label || { herg: "hERG", dili: "DILI", ames: "Ames", clintox: "ClinTox" }[endpoint.id] || endpoint.id;
      return `<tr><th scope="row">${escapeHtml(label)}</th><td>${countText(endpointCount("train"))}</td><td>${countText(endpointCount("validation"))}</td><td>${countText(endpointCount("test"))}</td><td>${countText(metrics.positives)}</td><td>${metricText(metrics.auroc ?? metrics.rocAuc)}</td><td>${metricText(metrics.averagePrecision ?? metrics.ap)}</td><td>${metricText(metrics.brier)}</td><td>${metricText(metrics.ece10Bins ?? metrics.ece)}</td><td>${escapeHtml(endpointCalibration)}</td></tr>`;
    }).join("");
    const architectureBody = `<dl class="local-info-grid">${entry("模型架构", architecture.name || architecture.type)}${entry("可训练参数", countText(architecture.parameterCount ?? info.parameterCount))}${entry("模型版本", info.version || info.modelVersion)}${entry("数据版本", data.version || info.datasetVersion || info.dataVersion || info.training?.dataVersion)}${entry("输入特征", architecture.input || info.inputRepresentation)}${entry("划分方法", data.splitMethod || info.splitMethod || info.training?.splitMethod)}</dl>${architecture.description ? `<p>${escapeHtml(architecture.description)}</p>` : ""}`;
    const dataBody = `<div class="local-split-counts"><div><span>训练集</span><strong>${countText(splits.train)}</strong></div><div><span>验证集</span><strong>${countText(splits.validation ?? splits.val)}</strong></div><div><span>测试集</span><strong>${countText(splits.test)}</strong></div></div><p>${uniqueCounts ? "以上为各划分的独立分子数；同一个分子可以具有多个端点标签。" : "计数以训练记录为准。"}端点标签覆盖数见下表。</p>${sources.length ? `<ul class="local-source-list">${sources.map((source) => `<li>${typeof source === "string" ? escapeHtml(source) : sourceLink(source)}</li>`).join("")}</ul>` : "<p>尚未提供可追溯数据来源。</p>"}`;
    const metricsBody = rows ? `<div class="local-metrics-scroll"><table class="local-metrics-table"><caption>留出测试集指标 · AUROC / AP 越高越好，Brier / ECE 越低越好</caption><thead><tr><th scope="col">端点</th><th scope="col">训练</th><th scope="col">验证</th><th scope="col">测试</th><th scope="col">测试阳性</th><th scope="col">AUROC</th><th scope="col">AP</th><th scope="col">Brier</th><th scope="col">ECE</th><th scope="col">校准状态</th></tr></thead><tbody>${rows}</tbody></table></div><p>“—”表示当前训练记录未提供该指标；模型测试结果不能推断所有新分子的准确性。端点标签不平衡时需同时查看阳性样本数与 AP。</p>` : "<p>尚无可用的留出测试集评估，不显示示例指标。</p>";
    const validationBody = `<dl class="local-info-grid">${entry("概率校准", calibration.status === "measured-not-certified" ? measuredCalibrationText(calibration) : calibrationLabel(calibration))}${entry("校准方法", calibration.method)}${entry("校准拟合集", calibration.fitSplit)}${entry("校准评估集", calibration.evaluationSet || info.evaluation?.split)}${entry("适用域方法", applicability.method)}${entry("参考分子数", countText(applicability.referenceCount))}</dl>${calibration.detail ? `<p>${escapeHtml(calibration.detail)}</p>` : ""}<p>${escapeHtml(applicability.detail || "结构相似度属于适用域启发式，不代表已验证的预测置信度。")}</p>${Number.isFinite(applicability.threshold) ? `<p>相似度阈值：${applicability.threshold.toFixed(3)}，此阈值需要结合实际用途验证。</p>` : ""}${limitations.length ? `<h4 class="local-limitations-title">训练记录中的限制</h4><ul class="local-source-list">${limitations.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : ""}${info.training?.completedAt ? `<p>训练完成时间：${escapeHtml(info.training.completedAt)}</p>` : ""}${info.artifactHash || info.artifactSha256 ? `<p class="local-artifact-hash">模型清单 SHA256：${escapeHtml(info.artifactHash || info.artifactSha256)}</p>` : ""}${info.weightsSha256 ? `<p class="local-artifact-hash">模型权重 SHA256：${escapeHtml(info.weightsSha256)}</p>` : ""}`;
    const external = info.externalEvaluation;
    const externalBody = external ? `<p>${escapeHtml(external.datasetName)} · ${countText(external.neuralModel?.count)} 个分子，阳性 ${countText(external.neuralModel?.positives)}。</p><div class="local-metrics-scroll"><table class="local-metrics-table"><caption>外部来源 hERG · 冻结模型与原训练集指纹基线</caption><thead><tr><th>模型</th><th>AUROC</th><th>AP</th><th>Brier</th><th>ECE</th></tr></thead><tbody>${[["蓝研 ToxNet", external.neuralModel], ["指纹逻辑回归", external.fingerprintBaseline]].map(([label, values]) => `<tr><th>${escapeHtml(label)}</th>${["auroc", "averagePrecision", "brier", "ece10Bins"].map((key) => `<td>${metricText(values?.[key])}</td>`).join("")}</tr>`).join("")}</tbody></table></div><p>${escapeHtml(external.meaning)}</p><p>${escapeHtml(external.limitation)}</p><p class="local-artifact-hash">外部评估 SHA256：${escapeHtml(external.evidenceSha256)}</p>` : "";
    elements.localModelContent.innerHTML = section("模型与版本", architectureBody) + section("真实数据与划分", dataBody) + section("四端点评估", metricsBody) + (external ? section("外部来源回顾性评估", externalBody) : "") + section("校准、适用域与限制", validationBody);
    elements.localModelContent.hidden = false;
  }
  async function loadLocalModelInfo() {
    state.localInfoController?.abort();
    const requestId = ++state.localInfoRequestId;
    const controller = new AbortController();
    state.localInfoController = controller;
    elements.localModelStatus.textContent = "正在读取本机模型训练记录…";
    elements.localModelStatus.classList.toggle("is-error", false);
    elements.localModelContent.hidden = true;
    elements.refreshLocalModelButton.disabled = true;
    try {
      const info = await core.getLocalModelInfo({ signal: controller.signal });
      if (requestId !== state.localInfoRequestId || controller.signal.aborted) return;
      renderLocalModelInfo(info);
    } catch (error) {
      if (requestId !== state.localInfoRequestId || controller.signal.aborted) return;
      elements.localModelStatus.textContent = error.message || "读取本机模型详情失败，请检查分析后台。";
      elements.localModelStatus.classList.toggle("is-error", true);
    } finally {
      if (requestId === state.localInfoRequestId) {
        state.localInfoController = null;
        elements.refreshLocalModelButton.disabled = false;
      }
    }
  }
  async function openLocalModel() {
    elements.localModelDialog.showModal();
    await loadLocalModelInfo();
  }
  function closeLocalModel() {
    state.localInfoRequestId += 1;
    state.localInfoController?.abort();
    state.localInfoController = null;
    elements.refreshLocalModelButton.disabled = false;
    elements.localModelDialog.close();
  }
  function validationText(result) {
    const calibration = result.calibration || result.provenance?.calibration;
    const applicability = result.applicability || result.provenance?.applicability;
    const calibrationStatus = calibration?.status || calibration;
    const domainStatus = applicability?.status || applicability;
    const similarity = applicability?.maximumTanimoto ?? applicability?.maxSimilarity ?? applicability?.maxTanimoto;
    const calibrated = ["validated", "verified", "calibrated"].includes(calibrationStatus);
    const domainText = domainStatus === "similarity-heuristic"
      ? `结构相似度启发式：${applicability.insideThreshold === true ? "达到参考阈值" : applicability.insideThreshold === false ? "低于参考阈值" : "未判定"}${Number.isFinite(similarity) ? `（${similarity.toFixed(3)}）` : ""}，适用域未验证`
      : domainStatus === "in_domain" ? "适用域：域内（服务声明）"
      : domainStatus === "out_of_domain" ? "适用域：域外（服务声明）" : "适用域未知";
    return `${calibrationStatus === "measured-not-certified" ? measuredCalibrationText(calibration) : calibrated ? "校准已验证（服务声明）" : "校准未验证"} · ${domainText}`;
  }
  function renderReport(result) {
    const tier = result.tier || core.riskTier(result.overall);
    const tierStyle = TIER_STYLES[tier.id] || TIER_STYLES.low;
    elements.reportTitle.textContent = result.known ? `${result.known.name} · 示例化合物` : "自定义候选分子";
    elements.reportSubtitle.textContent = result.canonicalSmiles || result.input;
    elements.evidenceBadge.textContent = result.evidenceLabel || (state.resultMode === "model" ? "真实模型输出" : "结构规则基线");
    elements.timestamp.textContent = new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
    }).format(new Date(result.generatedAt));
    elements.overallScore.textContent = scoreValue(result.overall);
    elements.riskTier.textContent = tier.label;
    elements.riskAction.textContent = tier.action;
    elements.riskGauge.style.setProperty("--gauge-angle", String(result.overall * 360));
    elements.riskGauge.style.setProperty("--risk-color", tierStyle.color);
    elements.gaugeValue.textContent = `${scoreValue(result.overall)} / 100`;
    elements.riskSummary.style.setProperty("--risk-color", tierStyle.color);
    elements.riskSummary.style.setProperty("--risk-bg", tierStyle.background);
    const highest = [...result.endpoints].sort((a, b) => b.score - a.score)[0];
    elements.highestEndpoint.textContent = highest ? `${highest.label} · ${scoreText(highest.score)}` : "—";
    elements.atomCount.textContent = `${result.descriptors.heavyAtoms} heavy atoms`;
    const kind = { uncalibrated_score: "未校准模型分数", probability: "模型概率输出", binary_label: "模型二元标签" }[result.scoreKind] || "模型分数";
    elements.scoreContext.textContent = `${state.resultMode === "rules" ? "结构规则分数" : kind} · 综合筛选指数为聚合分数，不代表毒性发生概率。示例参考值仅供演示。`;
    elements.validationStatus.textContent = validationText(result);
    renderMoleculeGraph(result.moleculeSvg);
    renderEndpoints(result.endpoints);
    renderAlerts(result.alerts || []);
    renderProperties(result.descriptors);
    renderEvidence(result);
  }
  function renderEndpoints(endpoints) {
    elements.endpointGrid.innerHTML = endpoints.map((endpoint) => {
      const modelMode = state.resultMode === "model";
      const ruleDriver = listDrivers(endpoint.ruleDrivers)[0];
      const activeDriver = modelMode ? listDrivers(endpoint.drivers)[0] : ruleDriver;
      const activeLabel = modelMode ? "模型解释" : "规则解释";
      const noExplanation = modelMode ? "模型未提供解释" : "未命中强规则驱动";
      const reference = Number.isFinite(endpoint.referenceScore) ? scoreText(endpoint.referenceScore) : "无示例参考";
      const color = scoreColor(endpoint.score);
      return `<article class="endpoint-card" style="--endpoint-color:${color}">
        <div class="endpoint-top"><div><div class="endpoint-code">${escapeHtml(endpoint.short || endpoint.id)}</div><h3 class="endpoint-name">${escapeHtml(endpoint.label)}</h3></div>
          <strong class="endpoint-score">${scoreValue(endpoint.score)}<small>/100</small></strong></div>
        <div class="risk-track" style="--score:${scoreValue(endpoint.score)}%" aria-hidden="true"><span></span></div>
        <p class="endpoint-description">${escapeHtml(endpoint.description || "")}</p>
        <dl class="score-comparison">
          <div${modelMode ? ' class="active-source"' : ""}><dt>模型</dt><dd>${scoreText(endpoint.modelScore)}</dd></div>
          <div${!modelMode ? ' class="active-source"' : ""}><dt>规则基线</dt><dd>${scoreText(endpoint.ruleScore)}</dd></div>
          <div><dt>示例参考</dt><dd>${reference}</dd></div></dl>
        <div class="driver"><span>${activeLabel} · ${escapeHtml(explanationSourceLabel(endpoint.explanationSource, modelMode))}</span>
          <strong title="${escapeHtml(activeDriver ? driverDetail(activeDriver) : noExplanation)}">${escapeHtml(activeDriver ? driverLabel(activeDriver) : noExplanation)}</strong></div>
        ${modelMode ? `<div class="rule-explanation"><span>规则基线说明（仅解释规则分数）</span><strong title="${escapeHtml(ruleDriver ? driverDetail(ruleDriver) : "未命中强规则驱动")}">${escapeHtml(ruleDriver ? driverLabel(ruleDriver) : "未命中强规则驱动")}</strong></div>` : ""}
      </article>`;
    }).join("");
  }
  function renderAlerts(alerts) {
    elements.alertCount.textContent = alerts.length ? `${alerts.length} alerts` : "clear";
    if (!alerts.length) {
      elements.alertList.innerHTML = '<div class="clear-state"><p>当前规则库未命中结构警示；这不等于已证明安全。</p></div>';
      return;
    }
    elements.alertList.innerHTML = alerts.map((alert) => `<article class="alert-item" style="--alert-color:${alert.severity === "high" ? "#be3b37" : "#c47b0a"}">
      <div class="alert-dot" aria-hidden="true"></div><div><strong>${escapeHtml(alert.label)}</strong><p>${escapeHtml(alert.detail)}</p></div>
      <span class="alert-level">${alert.severity === "high" ? "high" : "medium"}</span></article>`).join("");
  }
  function renderProperties(descriptors) {
    const properties = [
      ["分子量", `${descriptors.molecularWeight} Da`], ["cLogP", descriptors.logP],
      ["TPSA", `${descriptors.tpsa} Å²`], ["HBD / HBA", `${descriptors.hbd} / ${descriptors.hba}`],
      ["环数", descriptors.rings], ["可旋转键", descriptors.rotatableBonds],
      ["重原子", descriptors.heavyAtoms], ["碱性胺位点", descriptors.basicAmines],
    ];
    elements.propertyGrid.innerHTML = properties.map(([label, value]) => `<div class="property-item"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join("");
  }
  function renderEvidence(result) {
    const provenance = result.provenance || {};
    const chemistry = provenance.chemistry || {};
    const model = provenance.model || {};
    const highest = [...result.endpoints].sort((a, b) => b.score - a.score)[0];
    const modelName = model.name || model.modelName || currentProvider()?.modelName || "已配置模型";
    const modelVersion = model.modelVersion || model.version || currentProvider()?.version || "版本未提供";
    const rdkitVersion = chemistry.rdkitVersion || provenance.rdkitVersion || state.health?.chemistry?.rdkitVersion || "版本未提供";
    const rulesVersion = provenance.rules?.version || provenance.rulesVersion || chemistry.rulesVersion || state.health?.chemistry?.rulesVersion || "未提供";
    elements.evidenceFlow.innerHTML = `<article class="evidence-step"><span>01 / Structure</span><strong>RDKit 分子标准化与性质计算</strong>
      <p>结构标识 ${escapeHtml(result.structureId || "未提供")}；RDKit ${escapeHtml(rdkitVersion)}。</p></article>
      <article class="evidence-step"><span>02 / Scores</span><strong>${state.resultMode === "model" ? "真实模型输出" : "独立规则基线"}</strong>
      <p>${state.resultMode === "model" ? `${escapeHtml(modelName)} · ${escapeHtml(modelVersion)}` : `规则版本 ${escapeHtml(rulesVersion)}`}；最高端点 ${escapeHtml(highest?.label || "—")} ${scoreText(highest?.score)}。</p></article>
      <article class="evidence-step"><span>03 / Validation</span><strong>${escapeHtml(validationText(result))}</strong>
      <p>审计包记录模式、模型、规则与化学引擎来源。筛选结果需结合适用域和实验验证。</p></article>`;
  }
  function renderMoleculeGraph(svgText) {
    if (typeof svgText !== "string" || !svgText.trim()) throw new Error("分子服务未返回 RDKit 结构图。");
    const parsed = new DOMParser().parseFromString(svgText, "image/svg+xml");
    const svg = parsed.documentElement;
    if (parsed.querySelector("parsererror") || svg.localName !== "svg" || /<!DOCTYPE|<!ENTITY/i.test(svgText)) throw new Error("分子服务返回的结构图格式无效。");
    const allowedTags = new Set(["svg", "g", "path", "line", "polygon", "polyline", "circle", "ellipse", "rect", "text", "tspan", "defs", "title", "desc"]);
    [svg, ...svg.querySelectorAll("*")].forEach((node) => {
      if (!allowedTags.has(node.localName)) { node.remove(); return; }
      [...node.attributes].forEach((attribute) => {
        if (/^on/i.test(attribute.name) || /(?:href|src)$/i.test(attribute.name) || /url\s*\(|javascript:/i.test(attribute.value)) node.removeAttribute(attribute.name);
      });
    });
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "RDKit 生成的候选分子二维结构图");
    svg.removeAttribute("width");
    svg.removeAttribute("height");
    elements.moleculeView.replaceChildren(document.importNode(svg, true));
  }
  function auditValue(value) {
    if (Array.isArray(value)) return value.map(auditValue);
    if (value && typeof value === "object") return Object.fromEntries(Object.entries(value)
      .filter(([key]) => !/(?:api[_-]?key|authorization|password|secret|credential|(?:access|refresh|bearer|auth)[_-]?token|^token$)/i.test(key))
      .map(([key, nested]) => [key, auditValue(nested)]));
    return value;
  }
  function exportAuditPacket() {
    if (!state.result || state.dirty || state.loading || elements.exportButton.disabled) return;
    const result = state.result;
    const packet = auditValue({
      schema: "toxilens.audit.v2", purpose: "Explainable toxicity screening; experimental validation required",
      generatedAt: result.generatedAt, mode: state.resultMode, provider: state.resultProvider, scoreKind: result.scoreKind,
      input: { smiles: result.input, canonicalSmiles: result.canonicalSmiles, structureId: result.structureId, referenceCompound: result.known },
      overall: { score: result.overall, kind: "aggregate_screening_index", tier: result.tier },
      endpoints: result.endpoints, structuralAlerts: result.alerts, descriptors: result.descriptors,
      provenance: result.provenance || {}, calibration: result.calibration || result.provenance?.calibration || { status: "unknown" }, applicability: result.applicability || result.provenance?.applicability || { status: "unknown" },
      limitation: "Model, rule baseline and demonstration reference scores have separate sources. An aggregate index is not a toxicity probability. Calibration and applicability require independent validation; not suitable for clinical or regulatory decisions.",
    });
    const blob = new Blob([JSON.stringify(packet, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `toxilens-audit-${Date.now()}.json`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function bindEvents() {
    elements.sampleList.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-sample-id]");
      const sample = button && SAMPLES.find((item) => item.id === button.dataset.sampleId);
      if (!sample) return;
      state.selectedSampleId = sample.id;
      elements.smilesInput.value = sample.smiles;
      updateSelectedSample();
      analyzeCurrent();
    });
    elements.analyzeButton.addEventListener("click", analyzeCurrent);
    elements.resetButton.addEventListener("click", () => {
      invalidateRequest();
      state.result = null;
      state.selectedSampleId = null;
      state.analyzedInput = null;
      state.resultMode = null;
      state.resultProvider = null;
      state.dirty = false;
      elements.smilesInput.value = "";
      updateSelectedSample();
      updateInputStatus();
      showSurface("empty");
    });
    elements.exportButton.addEventListener("click", exportAuditPacket);
    elements.smilesInput.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") { event.preventDefault(); analyzeCurrent(); }
    });
    elements.smilesInput.addEventListener("input", () => {
      const sample = SAMPLES.find((item) => item.id === state.selectedSampleId);
      if (sample && normalizedInput(elements.smilesInput.value) !== normalizedInput(sample.smiles)) {
        state.selectedSampleId = null;
        updateSelectedSample();
      }
      markInputChanged();
    });
    elements.modeSelect.addEventListener("change", () => {
      state.mode = elements.modeSelect.value === "rules" ? "rules" : "model";
      markInputChanged();
      renderServiceStatus();
    });
    elements.providerSelect.addEventListener("change", () => {
      state.provider = elements.providerSelect.value;
      if (state.mode === "model") markInputChanged();
      renderServiceStatus();
    });
    elements.healthButton.addEventListener("click", refreshHealth);
    elements.configApiButton.addEventListener("click", () => isLocalProvider() ? openLocalModel() : openApiConfig());
    elements.closeConfigButton.addEventListener("click", closeApiConfig);
    elements.cancelConfigButton.addEventListener("click", closeApiConfig);
    elements.apiConfigDialog.addEventListener("cancel", (event) => { event.preventDefault(); closeApiConfig(); });
    elements.apiConfigDialog.addEventListener("close", () => { elements.configApiKey.value = ""; });
    elements.apiConfigForm.addEventListener("submit", saveApiConfig);
    elements.configApiFormat.addEventListener("change", updateConfigFormat);
    elements.closeLocalModelButton.addEventListener("click", closeLocalModel);
    elements.doneLocalModelButton.addEventListener("click", closeLocalModel);
    elements.localModelDialog.addEventListener("cancel", (event) => { event.preventDefault(); closeLocalModel(); });
    elements.refreshLocalModelButton.addEventListener("click", loadLocalModelInfo);
  }

  renderSamples();
  bindEvents();
  updateInputStatus();
  renderProviders();
  renderServiceStatus();
  refreshHealth();
})();
