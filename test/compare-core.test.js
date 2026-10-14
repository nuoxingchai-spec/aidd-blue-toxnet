"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const Core = require("../compare-core.js");

const request = (changes = {}) => Core.buildRequest({ smiles: "CCO", question: "讨论来源与限制", includeTox21: true, ...changes });
const run = (changes = {}) => ({ contractVersion: Core.CONTRACT, runId: "run-1", status: "completed", models: { "toxnet-v3-b16": { status: "completed", data: { prediction: { endpoints: [{ id: "herg", score: .61 }, { id: "dili", score: .42 }, { id: "ames", score: .27 }, { id: "clintox", score: .18 }, ...Object.keys(Core.TASK_NAMES).map((id, i) => ({ id, score: i / 12 }))] } } } }, ...changes });
function started() { return Core.reduce(Core.initialState(), { type: "START", request: request() }); }
function accepted() { const state = started(); return Core.reduce(state, { type: "ACK", generation: state.generation, runId: "run-1" }); }
function complete() { const state = accepted(); return Core.reduce(state, { type: "POLL", generation: state.generation, runId: "run-1", run: run() }); }

test("compare request fixes roles, preserves identity and defaults to no cloud transfer", () => {
  const result = request({ smiles: " C[C@H](O)Cl ", unexpectedApiKey: "secret", allowCloudTransfer: true });
  assert.equal(result.smiles, "C[C@H](O)Cl");
  assert.equal(result.mode, "compare");
  assert.equal(result.primaryPredictor, "toxnet-v3-b16");
  assert.equal(result.specialistPredictor, "txgemma-2b-predict");
  assert.equal(result.primaryReporter, "ollama-qwen-local");
  assert.equal(result.comparisonReporter, null);
  assert.equal(result.allowCloudTransfer, false);
  assert.equal(result.ollamaModel, null);
  assert.equal("unexpectedApiKey" in result, false);
  assert.equal(result.reportContract, "v2");
  assert.equal(result.reportMode, "fast");
});

test("report modes and contracts are bounded, preserved in presets and change the run fingerprint", () => {
  const fast = request(); const deep = request({ reportMode: "deep" });
  assert.notEqual(Core.fingerprint(fast), Core.fingerprint(deep));
  assert.equal(Core.presetSettings(deep).reportMode, "deep");
  assert.equal(Core.presetSettings(deep).reportContract, "v2");
  assert.equal(request({ reportContract: "v1", reportMode: "deep" }).reportContract, "v1");
  assert.throws(() => request({ reportMode: "unchecked" }), /报告模式/);
  assert.throws(() => request({ reportContract: "v3" }), /合同版本/);
});

test("historical report metadata stays legacy while reruns use explicit current defaults", () => {
  assert.deepEqual(Core.reportMetadata({}), { contract: "v1", mode: "deep", legacy: true, label: "v1 · 深度报告 Deep（历史合同）" });
  const saved = { request: { reportContract: "v2", reportMode: "fast" } };
  assert.equal(Core.reportMetadata(saved).label, "v2 · 快速报告 Fast");
  assert.equal(Core.reportMetadata(saved, { reportSchema: "aidd.compare.report.v1", reportMode: "deep" }).contract, "v1", "The executed report schema is the display authority");
  assert.equal(Core.restoredFields({}).reportContract, "v2");
  assert.equal(Core.restoredFields({}).reportMode, "fast");
  assert.equal(Core.restoredFields({ reportMode: "deep" }).reportMode, "deep");
});

test("explicit DeepSeek role and per-run consent are both required for transfer", () => {
  assert.equal(request({ comparisonEnabled: true }).comparisonReporter, "deepseek-api");
  assert.equal(request({ comparisonEnabled: true }).allowCloudTransfer, false);
  assert.equal(request({ comparisonEnabled: true, allowCloudTransfer: true }).allowCloudTransfer, true);
  assert.equal(request({ comparisonEnabled: "true", allowCloudTransfer: "true" }).allowCloudTransfer, false);
});

test("invalid form size and endpoint fail before sending a model request", () => {
  assert.throws(() => request({ smiles: "  " }), /请输入/);
  assert.throws(() => request({ smiles: "C".repeat(4097) }), /4096/);
  assert.equal(request({ question: "x".repeat(1000) }).question.length, 1000);
  assert.throws(() => request({ question: "x".repeat(1001) }), /1000/);
  assert.throws(() => request({ explainEndpoint: "tox21_sr_p53" }), /归因端点/);
  assert.equal(request({ smiles: "C CO" }).smiles, "C CO", "RDKit, not frontend guessing, validates internal whitespace");
});

test("completed and partial runs are exportable while pending runs are not", () => {
  assert.throws(() => Core.exportRun(accepted()), /尚未完成/);
  const completed = complete();
  assert.equal(completed.inFlight, false);
  assert.equal(Core.exportRun(completed).runId, "run-1");
  const state = accepted();
  const partial = Core.reduce(state, { type: "POLL", generation: state.generation, runId: "run-1", run: run({ status: "partial", models: { ...run().models, "deepseek-api": { status: "unavailable", error: { code: "UNCONFIGURED", message: "not configured" } } } }) });
  assert.equal(Core.endpointRows(Core.exportRun(partial))[0].score, .61);
});

test("input or any configuration edit invalidates completed results and prevents export", () => {
  const state = Core.reduce(complete(), { type: "EDIT" });
  assert.equal(state.stale, true);
  assert.equal(state.inFlight, false);
  assert.equal(state.run.runId, "run-1", "old scientific outputs remain visible as stale");
  assert.throws(() => Core.exportRun(state), /配置已修改/);
});

test("a late create acknowledgment cannot restore an edited or reset run", () => {
  const old = started();
  const edited = Core.reduce(old, { type: "EDIT" });
  assert.equal(Core.reduce(edited, { type: "ACK", generation: old.generation, runId: "late" }), edited);
  const reset = Core.reduce(old, { type: "RESET" });
  assert.equal(Core.reduce(reset, { type: "ACK", generation: old.generation, runId: "late" }), reset);
  assert.equal(reset.run, null);
});

test("late previous-run responses and failures never replace the current molecule", () => {
  const old = accepted();
  const latest = Core.reduce(old, { type: "START", request: request({ smiles: "CC#N" }) });
  assert.equal(Core.reduce(latest, { type: "POLL", generation: old.generation, runId: "run-1", run: run() }), latest);
  assert.equal(Core.reduce(latest, { type: "ERROR", generation: old.generation, message: "late failure" }), latest);
  const current = Core.reduce(latest, { type: "ACK", generation: latest.generation, runId: "run-2" });
  assert.equal(Core.reduce(current, { type: "POLL", generation: current.generation, runId: "run-1", run: run() }), current);
});

test("cancel ignores in-flight polls and retains successful predictor output", () => {
  let state = accepted();
  state = Core.reduce(state, { type: "POLL", generation: state.generation, runId: "run-1", run: run({ status: "running" }) });
  const previousGeneration = state.generation;
  state = Core.reduce(state, { type: "CANCEL_REQUEST" });
  assert.equal(state.cancelling, true);
  assert.equal(Core.reduce(state, { type: "POLL", generation: previousGeneration, runId: "run-1", run: run() }), state);
  const cancelled = Core.reduce(state, { type: "CANCELLED", generation: state.generation, runId: "run-1", run: run({ status: "cancelled" }) });
  assert.equal(cancelled.inFlight, false);
  assert.equal(cancelled.cancelling, false);
  assert.equal(Core.endpointRows(cancelled.run)[0].score, .61);
  assert.equal(Core.exportRun(cancelled).status, "cancelled");
});

test("poll failure preserves successful results and the run ID for recovery", () => {
  const acceptedState = accepted();
  const progress = Core.reduce(acceptedState, { type: "POLL", generation: acceptedState.generation, runId: "run-1", run: run({ status: "running" }) });
  const failedPoll = Core.reduce(progress, { type: "ERROR", generation: progress.generation, message: "timeout" });
  assert.equal(failedPoll.run.runId, "run-1");
  assert.equal(failedPoll.error, "timeout");
  assert.equal(Core.endpointRows(failedPoll.run)[2].score, .27);
  const cancelRequest = Core.reduce(failedPoll, { type: "CANCEL_REQUEST" });
  assert.equal(cancelRequest.cancelling, true, "a timed-out polling request does not remove the ability to cancel the real server run");
  assert.equal(cancelRequest.inFlight, true);
});

test("sixteen actual B16 scores become separate four-toxicity and twelve-pathway rows", () => {
  const actual = run();
  const four = Core.endpointRows(actual);
  const pathways = Core.pathwayRows(actual);
  assert.deepEqual(four.map((item) => item.id), ["herg", "dili", "ames", "clintox"]);
  assert.equal(four[0].score, .61);
  assert.equal(pathways.length, 12);
  assert.equal(pathways[0].id, "tox21_nr_ar");
  assert.equal(pathways[11].id, "tox21_sr_p53");
  assert.equal(pathways[11].score, 11 / 12);
  assert.equal("combinedScore" in actual, false);
});

test("unavailable models, labels, strings and nonfinite values cannot become probabilities", () => {
  assert.equal(Core.endpointRows(run({ models: { "toxnet-v3-b16": { status: "unavailable", data: { endpoints: [{ id: "herg", score: .9 }] } } } }))[0].score, null);
  const unsafe = run({ models: { "toxnet-v3-b16": { status: "completed", data: { endpoints: [{ id: "herg", score: "0.8" }, { id: "dili", score: Infinity }, { id: "ames", score: 1.1 }, { id: "clintox", score: -1 }] } }, "txgemma-2b-predict": { status: "completed", data: { classLabel: "A", directComparable: false } } } });
  assert.deepEqual(Core.endpointRows(unsafe).map((item) => item.score), [null, null, null, null]);
  assert.equal(Core.formatScore("positive"), "未提供");
  assert.equal(Core.formatScore(0), "0.000000");
});

test("Ollama selector uses actual installed models and retains digest/order", () => {
  const values = [{ name: "qwen3.5:4b", digest: "one" }, { name: "qwen3:8b", digest: "two" }, { name: "llama3:8b", digest: "three", supported: false }];
  const models = Core.installedOllamaModels({ models: [{ id: "ollama-qwen-local", metadata: { installedModels: values } }] });
  assert.deepEqual(models.map((item) => item.name), ["qwen3.5:4b", "qwen3:8b"]);
  assert.equal(models[0].digest, "one");
  assert.deepEqual(Core.installedOllamaModels({ models: [{ id: "ollama-qwen-local", configured: true }] }), []);
  assert.deepEqual(Core.installedOllamaModels(null), []);
});

test("presets remove molecule, free question, credentials and cloud permission", () => {
  const settings = Core.presetSettings({ ...request({ comparisonEnabled: true, allowCloudTransfer: true, specialistTask: "tdc-bbb-martins", ollamaModel: "qwen3.5:4b" }), apiKey: "secret" });
  assert.equal(settings.specialistTask, "tdc-bbb-martins");
  assert.equal(settings.ollamaModel, "qwen3.5:4b");
  for (const key of ["smiles", "question", "apiKey", "allowCloudTransfer", "persistSensitiveContent"]) assert.equal(key in settings, false);
});

test("JSON export keeps measured tokens, facts and scores while redacting credential/cache fields", () => {
  const state = complete();
  state.run.models["toxnet-v3-b16"].metrics = { promptTokens: 21, completionTokens: 3, costAmount: null };
  state.run.models["toxnet-v3-b16"].provenance = { apiKey: "secret-1", authorization: "secret-2", nested: { hfToken: "secret-3", encoderDir: "C:/private", weightsSha256: "verified-sha" } };
  const packet = Core.exportRun(state);
  const serialized = JSON.stringify(packet);
  assert.doesNotMatch(serialized, /secret-|C:\/private|apiKey|authorization|encoderDir/);
  assert.equal(packet.models["toxnet-v3-b16"].metrics.promptTokens, 21);
  assert.equal(packet.models["toxnet-v3-b16"].provenance.nested.weightsSha256, "verified-sha");
  assert.equal(state.run.models["toxnet-v3-b16"].provenance.apiKey, "secret-1", "redaction does not mutate the original run");
});

test("versioned status contracts reject unknown state, model IDs and array-shaped model objects", () => {
  assert.equal(Core.validateRun(run()).status, "completed");
  assert.throws(() => Core.validateRun(run({ contractVersion: "other" })), /格式/);
  assert.throws(() => Core.validateRun(run({ status: "ready" })), /格式/);
  assert.throws(() => Core.validateRun(run({ models: [] })), /格式/);
  assert.throws(() => Core.validateRun(run({ models: { random: { status: "completed" } } })), /未知模型/);
  assert.throws(() => Core.validateRun(run({ models: { "deepseek-api": { status: "ready" } } })), /未知模型/);
  assert.throws(() => Core.validateRegistry({ contractVersion: Core.CONTRACT, models: [] }), /完整/);
});

test("unknown performance is not displayed as a zero-cost or zero-memory measurement", () => {
  assert.equal(Core.formatMetric(null, "MiB"), "未测量");
  assert.equal(Core.formatMetric(undefined, "token/s"), "未测量");
  assert.equal(Core.formatMetric(NaN, "ms"), "未测量");
  assert.equal(Core.formatMetric(-2, "ms"), "未测量");
  assert.equal(Core.formatMetric(0, "USD", 6), "0.000000 USD");
  assert.equal(Core.formatMetric(125.25, "ms"), "125.25 ms");
});

test("actual worker byte measurements are converted exactly to MiB and unmeasured values stay unknown", () => {
  const gpu = { measured: true, peakAllocatedBytes: 235.5 * 1024 * 1024, peakReservedBytes: 268 * 1024 * 1024 };
  assert.equal(Core.gpuMemoryMiB(gpu, "allocated"), 235.5);
  assert.equal(Core.gpuMemoryMiB(gpu, "reserved"), 268);
  assert.equal(Core.gpuMemoryMiB({ ...gpu, measured: false }, "allocated"), null);
  assert.equal(Core.gpuMemoryMiB({ measured: true, peakAllocatedBytes: Infinity }, "allocated"), null);
  assert.equal(Core.gpuMemoryMiB({ measured: true, peakAllocatedBytes: -1 }, "allocated"), null);
  assert.equal(Core.gpuMemoryMiB({ measured: true, peakAllocatedBytes: 0 }, "allocated"), 0);
  assert.equal(Core.gpuMemoryMiB({ peakAllocatedMiB: 12.5 }, "allocated"), 12.5);
  assert.equal(Core.gpuMemoryMiB(null, "allocated"), null);
});

test("TxGemma actual tasks envelope retains categories and never becomes four-endpoint scores", () => {
  const result = run({ models: { "txgemma-2b-predict": { status: "completed", data: { structureId: "same-structure", tasks: [{ taskId: "tdc-bbb-martins", tdcTask: "BBB_Martins", outputType: "categorical-text", class: "B", classLabel: "crosses the BBB", directComparable: false, probability: null }] } } } });
  const tasks = Core.specialistTasks(result);
  assert.equal(tasks.length, 1);
  assert.equal(tasks[0].tdcTask, "BBB_Martins");
  assert.equal(tasks[0].classLabel, "crosses the BBB");
  assert.deepEqual(Core.endpointRows(result).map((item) => item.score), [null, null, null, null]);
  assert.deepEqual(Core.specialistTasks(run({ models: { "txgemma-2b-predict": { status: "failed", data: { tasks: tasks } } } })), []);
});

test("experiment evidence and neural attribution have independent controls and endpoints", () => {
  const evidence = request({ includeEvidence: true, evidenceEndpoint: "dili", includeAttribution: false, explainEndpoint: "herg" });
  assert.equal(evidence.includeEvidence, true);
  assert.equal(evidence.evidenceEndpoint, "dili");
  assert.equal(evidence.includeAttribution, false);
  assert.equal(evidence.explainEndpoint, "herg", "the saved attribution selection does not enable the disabled stage");
  const attribution = request({ includeAttribution: true, includeEvidence: false, evidenceEndpoint: "ames", explainEndpoint: "clintox" });
  assert.equal(attribution.includeAttribution, true);
  assert.equal(attribution.includeEvidence, false);
  assert.equal(attribution.explainEndpoint, "clintox");
  assert.equal(attribution.evidenceEndpoint, "ames");
  assert.throws(() => request({ evidenceEndpoint: "unregistered" }), /实验检索端点/);
});

test("legacy attribution inference remains explicit while sensitive persistence is off by default", () => {
  assert.equal(request({ explainEndpoint: "ames" }).includeAttribution, true);
  assert.equal(request({ includeAttribution: true }).explainEndpoint, "herg");
  assert.equal(request().persistSensitiveContent, false);
  assert.equal(request({ persistSensitiveContent: "true" }).persistSensitiveContent, false);
  assert.equal(request({ persistSensitiveContent: true }).persistSensitiveContent, true);
});

test("restoring old settings never reuses cloud or sensitive-content consent", () => {
  const fields = Core.restoredFields({ ...request({ comparisonEnabled: true, allowCloudTransfer: true, persistSensitiveContent: true, explainEndpoint: "dili" }), comparisonEnabled: true });
  assert.equal(fields.smiles, "CCO");
  assert.equal(fields.includeAttribution, true);
  assert.equal(fields.comparisonEnabled, false);
  assert.equal(fields.allowCloudTransfer, false);
  assert.equal(fields.persistSensitiveContent, false);
  const minimal = Core.restoredFields({ mode: "compare", includeEvidence: true, evidenceEndpoint: "ames" });
  assert.equal(minimal.smiles, "");
  assert.equal(minimal.question, "");
  assert.equal(minimal.includeEvidence, true);
  assert.equal(Core.summaryOnly({ contentSaved: false, recovery: { contentAvailable: true } }), false, "current-process complete content may exist although it was not persisted");
  assert.equal(Core.summaryOnly({ contentSaved: false, restored: true, recovery: { contentAvailable: false } }), true);
});

test("history restore changes read state without creating a new execution request", () => {
  const loading = Core.reduce(complete(), { type: "RESTORE_REQUEST", runId: "saved-1" });
  assert.equal(loading.viewingHistory, true);
  assert.equal(loading.restoring, true);
  assert.equal(loading.inFlight, false);
  assert.equal(loading.fingerprint, null);
  const summary = run({ runId: "saved-1", recovery: { contentAvailable: false }, contentSaved: false });
  const restored = Core.reduce(loading, { type: "RESTORED", generation: loading.generation, runId: "saved-1", run: summary });
  assert.equal(restored.restoring, false);
  assert.equal(restored.inFlight, false);
  assert.equal(Core.exportRun(restored).runId, "saved-1");
  const edited = Core.reduce(loading, { type: "EDIT" });
  assert.equal(edited.restoring, false, "editing while the read request is in flight cannot leave the page permanently loading");
  assert.equal(Core.reduce(edited, { type: "RESTORED", generation: loading.generation, runId: "saved-1", run: summary }), edited);
});

test("history pagination and stored run IDs reject malformed or unsafe responses", () => {
  const value = { contractVersion: Core.CONTRACT, runs: [run()], total: 21, limit: 10, offset: 10, hasMore: true };
  assert.equal(Core.validateHistory(value).offset, 10);
  assert.throws(() => Core.validateHistory({ ...value, offset: -1 }), /历史接口/);
  assert.throws(() => Core.validateHistory({ ...value, runs: [{ runId: "../../config", status: "completed" }] }), /历史接口/);
  assert.throws(() => Core.validateHistory({ ...value, runs: [{ runId: "safe", status: "invented" }] }), /历史接口/);
  assert.equal(Core.validRunId("a_123-456"), true);
  for (const id of [null, "", "http://example.com", "..", "a".repeat(81), "a\n"]) assert.equal(Core.validRunId(id), false);
});

test("actual selected Ollama IDs and digests determine readiness separately from default model", () => {
  const registry = { models: [{ id: "ollama-qwen-local", configured: true, available: false, requestedModelId: "qwen3.5:4b", errorCode: "OLLAMA_MODEL_NOT_INSTALLED", installedModels: [{ name: "qwen3:4b", digest: "actual-digest" }] }, { id: "deepseek-api", configured: true, available: false, actualModelId: "account-model", accessVerified: false }] };
  const selected = Core.selectedModelReadiness(registry, "ollama-qwen-local", "qwen3:4b");
  assert.equal(selected.status, "installed");
  assert.equal(selected.modelId, "qwen3:4b");
  assert.equal(selected.revision, "actual-digest");
  assert.equal(Core.selectedModelReadiness(registry, "ollama-qwen-local", "qwen3.5:4b").status, "not-installed");
  assert.equal(Core.selectedModelReadiness(registry, "deepseek-api").status, "configured-unverified");
  const offline = { models: [{ id: "ollama-qwen-local", configured: true, available: false, errorCode: "OLLAMA_UNAVAILABLE", installedModels: [] }] };
  assert.equal(Core.selectedModelReadiness(offline, "ollama-qwen-local").status, "unreachable");
});

test("persisted Mock provenance remains Mock even under a real current model registry", () => {
  const historical = run({ models: { "ollama-qwen-local": { status: "completed", provenance: { testFixture: true } } } });
  assert.equal(Core.fixtureRun(historical), true);
  assert.equal(Core.fixtureResult(historical.models["ollama-qwen-local"]), true);
  assert.equal(Core.fixtureRun(run()), false);
  assert.equal(Core.fixtureRun(run({ models: { "toxnet-v3-b16": { status: "completed", data: { prediction: { provenance: { testFixture: true } } } } } })), true);
});

test("fixture registry readiness does not claim real installation, access or digest verification", () => {
  const registry = { testFixture: true, models: [{ id: "ollama-qwen-local", available: true, configured: true, actualModelId: "qwen-unit:4b", installedModels: [{ name: "qwen-unit:4b", digest: "fixture-digest" }] }, { id: "toxnet-v3-b16", available: true, actualModelId: "fixture-b16" }] };
  const qwen = Core.selectedModelReadiness(registry, "ollama-qwen-local", "qwen-unit:4b");
  assert.equal(qwen.status, "mock-ready");
  assert.equal(qwen.modelId, "qwen-unit:4b");
  assert.match(qwen.reason, /未验证真实安装/);
  assert.equal(Core.selectedModelReadiness(registry, "toxnet-v3-b16").status, "mock-ready");
});
