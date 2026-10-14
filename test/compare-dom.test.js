"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const Core = require("../compare-core.js");

// A minimal DOM contract fixture exercises node retention and focus behavior;
// actual layout/scroll acceptance is additionally performed in the browser.
function region() {
  const document = { activeElement: null };
  const root = { ownerDocument: document, items: [], querySelectorAll(selector) {
    if (selector === "details[data-view-key]") return this.items.filter((item) => item.kind === "details");
    return this.items.filter((item) => item.dataset.focusKey || item.id);
  }, contains(item) { return this.items.includes(item); } };
  function detail(key) {
    const item = { kind: "details", dataset: { viewKey: key }, open: false };
    const summary = { kind: "summary", dataset: { focusKey: `${key}:summary` }, focus(options) { document.activeElement = this; this.focusOptions = options; } };
    return [item, summary];
  }
  return { root, document, detail };
}

test("identical polling content retains original nodes, open details and keyboard focus", () => {
  const render = Core.createRegionRenderer(); const dom = region(); let builds = 0;
  const content = { status: "verified-structured-report", sections: [{ id: "evidence", text: "事实保持不变" }] };
  const build = () => { builds += 1; dom.root.items = dom.detail("evidence"); };
  assert.equal(render(dom.root, content, build), true);
  const [detail, summary] = dom.root.items; detail.open = true; dom.document.activeElement = summary;
  assert.equal(render(dom.root, JSON.parse(JSON.stringify(content)), build), false);
  assert.equal(render(dom.root, { sections: [{ text: "事实保持不变", id: "evidence" }], status: "verified-structured-report" }, build), false, "JSON object key order is not a content change");
  assert.equal(builds, 1);
  assert.equal(dom.root.items[0], detail);
  assert.equal(dom.document.activeElement, summary);
  assert.equal(detail.open, true);
});

test("updates to another model do not recreate a completed report or alter its focus", () => {
  const render = Core.createRegionRenderer(); const report = region(); const status = region();
  render(report.root, { text: "已核验的 Qwen 报告" }, () => { report.root.items = report.detail("report-qwen"); });
  report.root.items[0].open = true; const summary = report.root.items[1]; report.document.activeElement = summary;
  render(status.root, { deepseek: "running" }, () => { status.root.items = []; });
  assert.equal(render(report.root, { text: "已核验的 Qwen 报告" }, () => { throw new Error("unchanged report must not be rebuilt"); }), false);
  render(status.root, { deepseek: "completed" }, () => { status.root.items = []; });
  assert.equal(report.root.items[0].open, true);
  assert.equal(report.document.activeElement, summary);
});

test("changed region restores previously opened details and focuses the matching new summary", () => {
  const render = Core.createRegionRenderer(); const dom = region();
  render(dom.root, { revision: 1 }, () => { dom.root.items = dom.detail("source-audit"); });
  const oldSummary = dom.root.items[1]; dom.root.items[0].open = true; dom.document.activeElement = oldSummary;
  render(dom.root, { revision: 2 }, () => { dom.root.items = dom.detail("source-audit"); });
  assert.notEqual(dom.root.items[1], oldSummary);
  assert.equal(dom.root.items[0].open, true);
  assert.equal(dom.document.activeElement, dom.root.items[1]);
  assert.deepEqual(dom.root.items[1].focusOptions, { preventScroll: true });
});

test("a changed history row retains its stable button focus and input selection where present", () => {
  const render = Core.createRegionRenderer(); const dom = region();
  function input() { return { dataset: { focusKey: "history:run-1:open" }, selectionStart: 2, selectionEnd: 4,
    focus() { dom.document.activeElement = this; }, setSelectionRange(start, end) { this.restoredSelection = [start, end]; } }; }
  render(dom.root, { status: "running" }, () => { dom.root.items = [input()]; });
  dom.document.activeElement = dom.root.items[0];
  render(dom.root, { status: "completed" }, () => { dom.root.items = [input()]; });
  assert.equal(dom.document.activeElement, dom.root.items[0]);
  assert.deepEqual(dom.root.items[0].restoredSelection, [2, 4]);
});

test("undefined initial region and changed empty content are rendered once, not mistaken for cached content", () => {
  const render = Core.createRegionRenderer(); const dom = region(); let builds = 0;
  assert.equal(render(dom.root, undefined, () => { builds += 1; }), true);
  assert.equal(render(dom.root, undefined, () => { builds += 1; }), false);
  assert.equal(render(dom.root, null, () => { builds += 1; }), true);
  assert.equal(builds, 2);
});
