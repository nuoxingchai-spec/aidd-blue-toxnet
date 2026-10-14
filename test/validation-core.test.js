"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const Core = require("../validation-core.js");

const catalog = () => ({ schema: Core.SCHEMA, formalAvailability: false, datasets: [{ id: "software-fixture", qualification: "not-qualified" }], models: [{ id: "B16", available: false }], testFixture: true });
const run = (status = "completed") => ({ schema: Core.SCHEMA, runId: "fixture-run", status, report: null, testFixture: true });

test("validation accepts only explicit research schema and registered dataset/model identifiers", () => {
  assert.equal(Core.validateCatalog(catalog()).formalAvailability, false);
  for (const changed of [{ schema: "other" }, { formalAvailability: true }, { datasets: [{ id: "../../private" }] }, { models: [{ id: "invented" }] }]) assert.throws(() => Core.validateCatalog({ ...catalog(), ...changed }), /不兼容/);
  assert.equal(Core.validId("audit-123:version_2"), true);
  for (const value of ["", "..", ".", "https://host/path", "C:\\private", "raw/file.csv", null]) assert.equal(Core.validId(value), false);
});

test("validation requests exclude paths, labels, URLs and temporary model options", () => {
  const fields = { datasetId: "registered-id", protocolId: "frozen-v1", modelIds: ["C_Morgan", "B16", "A4"], seed: 999, rawRows: [], modelPath: "private", dataUrl: "https://host" };
  assert.deepEqual(Core.request("audit", fields), { datasetId: "registered-id" });
  assert.deepEqual(Core.request("evaluate", fields), { datasetId: "registered-id", protocolId: "frozen-v1", modelIds: ["A4", "B16", "C_Morgan"], seed: 42 });
  assert.throws(() => Core.request("evaluate", { ...fields, modelIds: [] }), /请选择/);
  assert.throws(() => Core.request("evaluate", { ...fields, modelIds: ["B16", "B16"] }), /请选择/);
  assert.throws(() => Core.request("evaluate", { ...fields, modelIds: ["v2"] }), /请选择/);
  assert.throws(() => Core.request("evaluate", { ...fields, modelIds: ["A4", "B16"] }), /固定/);
  assert.throws(() => Core.request("evaluate", { ...fields, protocolId: "" }), /协议/);
});

test("validation missing or nonfinite scientific metrics stay uncomputed instead of zero", () => {
  for (const value of [null, undefined, NaN, Infinity, -0.1, 1.1, "0.9"]) assert.equal(Core.metric(value), "未计算");
  assert.equal(Core.metric(0), "0.0000"); assert.equal(Core.metric(1), "1.0000");
  assert.equal(Core.count(null), "未计算"); assert.equal(Core.count(0), "0");
  assert.equal(Core.interval({ lower: .2, upper: .8 }), "[0.2000, 0.8000]");
  assert.equal(Core.interval([.8, .2]), "未计算"); assert.equal(Core.interval({ lower: null, upper: .8 }), "未计算");
});

test("validation preserves explicit endpoint/model rows without fabricating absent endpoints", () => {
  const row = { modelId: "B16", count: 12, positives: 3, metrics: { auroc: .5 } };
  assert.equal(Core.endpointRows({ fairComparison: { endpoints: { herg: [row] } } })[0].endpoint, "herg");
  assert.equal(Core.endpointRows({ fairComparison: { endpoints: [{ endpoint: "herg", models: { B16: row } }] } })[0].modelId, "B16");
  assert.deepEqual(Core.endpointRows({ fairComparison: { status: "not-qualified" } }), []);
});

test("production paired comparison shape binds endpoint rows to actual model versions", () => {
  const rows = Core.endpointRows({ models: { B16: { modelBinding: { modelVersion: "actual-bound-fixture-version" }, endpoints: { herg: { count: 12, coveredCount: 12, positives: 3, auroc: null, ece10Bins: .2 } } } } });
  assert.equal(rows.length, 1); assert.equal(rows[0].modelId, "B16");
  assert.equal(rows[0].modelVersion, "actual-bound-fixture-version"); assert.equal(rows[0].endpoint, "herg");
  assert.equal(rows[0].auroc, null);
});

test("reliability curves require measured source-linked real bins with observations", () => {
  const curve = { measured: true, sourceId: "software-fixture-only", points: [{ predicted: .2, observed: .3, count: 10 }] };
  const report = (value) => ({ calibration: { reliabilityCurves: [value] } });
  assert.equal(Core.reliabilityCurves(report(curve)).length, 1);
  for (const value of [{ ...curve, measured: false }, { ...curve, sourceId: "" }, { ...curve, points: [] }, { ...curve, points: [{ predicted: .2, observed: null, count: 10 }] }, { ...curve, points: [{ predicted: .2, observed: .3, count: 0 }] }]) assert.equal(Core.reliabilityCurves(report(value)).length, 0);
  assert.deepEqual(Core.reliabilityCurves(null), []);
});

test("validation generation rejects late acknowledgments, polls and failures after edits", () => {
  let state = Core.reduce(Core.initial(), { type: "START" }); const generation = state.generation;
  state = Core.reduce(state, { type: "EDIT" });
  assert.equal(state.stale, true);
  for (const event of [{ type: "ACK", runId: "late" }, { type: "RUN", run: run() }, { type: "ERROR", message: "late" }]) assert.equal(Core.reduce(state, { ...event, generation }), state);
});

test("validation cancel increments generation and keeps successfully completed aggregate records", () => {
  let state = Core.reduce(Core.initial(), { type: "START" });
  state = Core.reduce(state, { type: "RUN", generation: state.generation, run: run("running") });
  const old = state.generation; state = Core.reduce(state, { type: "CANCEL" });
  assert.equal(state.cancelling, true);
  assert.equal(Core.reduce(state, { type: "RUN", generation: old, run: run() }), state);
  state = Core.reduce(state, { type: "RUN", generation: state.generation, run: run("cancelled") });
  assert.equal(Core.exportable(state), false, "Cancelled or interrupted records do not have a completed public report");
  assert.equal(Core.exportable(Core.reduce(state, { type: "EDIT" })), false);
});

test("validation export redacts sensitive rows, structures, credentials and private path fields", () => {
  const value = { schema: Core.SCHEMA, report: { count: 4, rawRows: ["private"], smiles: "private", provenance: { modelDir: "private", apiKey: "private", sourceSha256: "kept" } } };
  const result = Core.sanitize(value);
  assert.doesNotMatch(JSON.stringify(result), /private|rawRows|smiles|apiKey|modelDir/);
  assert.equal(result.report.provenance.sourceSha256, "kept");
  assert.equal(value.report.provenance.apiKey, "private");
});

test("validation run contracts reject malformed reports and unknown execution states", () => {
  assert.equal(Core.validateRun(run("not-qualified")).status, "not-qualified");
  assert.equal(Core.validateRun(run("interrupted")).status, "interrupted");
  for (const value of [{ ...run(), schema: "other" }, { ...run(), runId: "../escape" }, { ...run(), status: "qualified-and-clinical" }, { ...run(), report: [] }]) assert.throws(() => Core.validateRun(value), /格式|聚合/);
});

test("production fairComparison model maps retain observed coverage and cluster confidence intervals", () => {
  const report = { fairComparison: { models: { A4: { modelBinding: { modelVersion: "software-fixture-only" }, endpoints: { herg: { count: 30, positives: 10, coveredCount: 27, coverageDenominator: 30, coverageFraction: .9, confidenceIntervals: { intervals: { auroc: { lower: .2, upper: .8, validReplicates: 10 } } } } } } } } };
  const row = Core.endpointRows(report)[0];
  assert.equal(row.endpoint, "herg"); assert.equal(row.modelVersion, "software-fixture-only");
  assert.equal(Core.coverage(row), "0.9000 (27/30)");
  assert.equal(Core.interval(row.confidenceIntervals.intervals.auroc), "[0.2000, 0.8000]");
  assert.equal(Core.coverage({ count: 0, coveredCount: 0, coverageFraction: null }), "未计算");
  assert.equal(Core.coverage({ coveredCount: 100, coverageDenominator: 20 }), "未计算");
});

test("calibration-resubstitution diagnostic bins keep their source binding and non-independent warning", () => {
  const diagnostic = { schema: "aidd.calibration-diagnostics.v1", role: "calibration-resubstitution", independentTest: false, methodSelectionPermitted: false,
    ...Object.fromEntries(["diagnosticSha256", "datasetManifestSha256", "protocolSha256", "membershipSha256", "modelBindingSha256"].map((name) => [name, "a".repeat(64)])),
    endpoints: { herg: { count: 10, positives: 3, reliabilityBefore: [{ count: 10, meanPredicted: .2, observedPositiveRate: .3 }, { count: 0, meanPredicted: null, observedPositiveRate: null }], reliabilityAfter: null } } };
  const curves = Core.reliabilityCurves({ calibration: diagnostic });
  assert.equal(curves.length, 1); assert.equal(curves[0].points.length, 1);
  assert.equal(curves[0].sourceId, "a".repeat(64)); assert.match(curves[0].evidenceStatus, /非独立评价/);
  assert.deepEqual(Core.reliabilityCurves({ calibration: { ...diagnostic, independentTest: true } }), []);
  assert.deepEqual(Core.reliabilityCurves({ calibration: { ...diagnostic, diagnosticSha256: null } }), []);
  assert.deepEqual(Core.reliabilityCurves({ calibration: { ...diagnostic, endpoints: { herg: { ...diagnostic.endpoints.herg, positives: 0 } } } }), []);
  assert.deepEqual(Core.reliabilityCurves({ calibration: { ...diagnostic, endpoints: { herg: { ...diagnostic.endpoints.herg, count: 11 } } } }), []);
});

test("evidence binding never upgrades completed engineering runs to independent scientific qualification", () => {
  assert.equal(Core.evidence(null), null);
  const item = Core.evidence({ status: "completed", formalAvailability: false, fairComparison: { models: { B16: {} } } });
  assert.equal(item.independenceStatus, null); assert.equal(item.labelsAndMembersMatched, null);
  const sourced = Core.evidence({ independenceStatus: "not-independently-qualified", exposure: { freshBlindTestClaimed: false }, protocolSha256: "fixture", labelReadAfterPrediction: true, fairComparison: { labelsAndMembersMatched: true } });
  assert.equal(sourced.independenceStatus, "not-independently-qualified"); assert.equal(sourced.labelReadAfterPrediction, true);
  assert.equal(sourced.exposure.freshBlindTestClaimed, false);
});

test("aggregate export omits observed raw labels and local source members", () => {
  const exported = Core.sanitize({ labels: { herg: 1 }, eligibleRows: [], quarantined_rows: [], rows: [], modelFile: "private", datasetManifestSha256: "kept" });
  assert.deepEqual(exported, { datasetManifestSha256: "kept" });
});
