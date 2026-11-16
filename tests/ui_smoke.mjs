/**
 * UI smoke test: runs web/app.js in a tiny DOM shim against the real HTTP
 * server, drives the grain picker / metric checkboxes / run button / dialogs,
 * and checks rendered output against the hand-checked baseline.
 *
 * Usage: APP_BASE=http://127.0.0.1:PORT node tests/ui_smoke.mjs
 *
 * This exists because the environment has no browser: it is not a substitute
 * for visual review, but it does catch broken bindings, missing ids, wrong
 * payload fields and rendering exceptions.
 */
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const BASE = process.env.APP_BASE || "http://127.0.0.1:8765";
const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");

class ClassList {
  constructor(node) { this.node = node; }
  _items() { return (this.node.className || "").split(/\s+/).filter(Boolean); }
  add(...c) { const s = new Set(this._items()); c.forEach((x) => s.add(x)); this.node.className = [...s].join(" "); }
  remove(...c) { const s = new Set(this._items()); c.forEach((x) => s.delete(x)); this.node.className = [...s].join(" "); }
  contains(c) { return this._items().includes(c); }
  toggle(c) { this.contains(c) ? this.remove(c) : this.add(c); }
}

class Node {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.className = "";
    this.style = {};
    this.dataset = {};
    this._text = "";
    this._attrs = {};
    this._listeners = {};
    this.classList = new ClassList(this);
    this.checked = false;
    this.value = "";
    this.disabled = false;
    this.type = "";
    this.title = "";
    this.open = false;
  }
  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    if (this._text) this._text = "";
    return child;
  }
  removeChild(child) {
    this.children = this.children.filter((c) => c !== child);
    return child;
  }
  setAttribute(k, v) {
    this._attrs[k] = String(v);
    if (k === "id") this.id = String(v);
    if (k.startsWith("data-")) {
      this.dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(v);
    }
  }
  getAttribute(k) { return this._attrs[k] ?? null; }
  addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }
  removeEventListener() {}
  dispatch(type, ev) { (this._listeners[type] || []).forEach((fn) => fn(ev)); }
  closest(sel) {
    let cur = this;
    while (cur) {
      if (sel === "dialog" && cur.tagName === "DIALOG") return cur;
      if (sel.startsWith(".") && cur.classList.contains(sel.slice(1))) return cur;
      cur = cur.parentNode;
    }
    return null;
  }
  showModal() { this.open = true; }
  close() { this.open = false; }
  set innerHTML(v) {
    this.children = [];
    this._text = String(v || "").replace(/<[^>]*>/g, "");
  }
  get innerHTML() { return this.textContent; }
  set textContent(v) {
    this.children = [];
    this._text = v === undefined || v === null ? "" : String(v);
  }
  get textContent() {
    return this._text + this.children.map((c) => c.textContent).join("");
  }
}

const allNodes = [];
const byId = new Map();

function create(tag) {
  const node = new Node(tag);
  allNodes.push(node);
  return node;
}

// Build a flat document from index.html. Layout is irrelevant to app.js; only
// ids, [data-load] and [data-close] hooks are used. The close-button binding
// calls closest("dialog"), which this shim also supports up to the flat root.
const html = fs.readFileSync(path.join(ROOT, "web", "index.html"), "utf8");
for (const m of html.matchAll(/<([a-zA-Z][\w-]*)([^>]*)\/?>/g)) {
  const tag = m[1];
  if (tag === "meta" || tag === "link" || tag === "br") continue;
  const attrs = m[2];
  const id = /id="([^"]+)"/.exec(attrs)?.[1];
  const dataLoad = /data-load="([^"]+)"/.exec(attrs)?.[1];
  const dataClose = /\bdata-close\b/.test(attrs);
  if (!id && !dataLoad && !dataClose) continue;
  const node = create(tag);
  if (id) { node.id = id; byId.set(id, node); }
  if (dataLoad) node.setAttribute("data-load", dataLoad);
  if (dataClose) node.setAttribute("data-close", "");
}

const document = {
  createElement: (tag) => create(tag),
  createTextNode: (text) => {
    const node = new Node("#text");
    node.textContent = text;
    return node;
  },
  querySelector(sel) {
    if (sel.startsWith("#")) return byId.get(sel.slice(1)) || null;
    throw new Error("unsupported selector " + sel);
  },
  querySelectorAll(sel) {
    if (sel === "[data-load]") return allNodes.filter((n) => n.dataset.load !== undefined);
    if (sel === "[data-close]") return allNodes.filter((n) => n.getAttribute("data-close") !== null);
    return [];
  },
};

const sandbox = {
  document,
  navigator: { clipboard: { writeText: async () => {} } },
  console,
  setTimeout,
  clearTimeout,
  performance,
  fetch: (url, opts) => fetch(new URL(url, BASE), opts),
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(ROOT, "web", "app.js"), "utf8"), sandbox, {
  filename: "app.js",
});

async function main() {
  const $ = (id) => {
    const node = byId.get(id);
    if (!node) throw new Error("missing element #" + id);
    return node;
  };
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));

  async function waitFor(fn, label, timeout = 15000) {
    const start = Date.now();
    while (Date.now() - start < timeout) {
      try {
        if (fn()) return;
      } catch (e) {
        /* keep polling */
      }
      await wait(50);
    }
    throw new Error("timeout waiting for " + label);
  }

  const metricLabel = (text) =>
    $("metrics").children.find((n) => n.textContent.includes(text)) || null;

  async function selectMetric(text) {
    const label = metricLabel(text);
    assert.ok(label, `metric containing ${text} should exist`);
    const cb = label.children[0];
    if (!cb.checked) {
      cb.checked = true;
      cb.onchange();
    }
  }

  // A real browser does not dispatch click on a disabled button; this shim
  // does, so wait for runQuery's finally block before firing the next run.
  async function run() {
    await waitFor(() => $("runBtn").disabled === false, "run button enabled");
    $("runBtn").onclick();
  }

  // 1. boot renders the grain picker, metric matrix, orders and dataset digest.
  await waitFor(() => $("orders").children.length === 9, "9 order chips");
  assert.equal($("grains").children.length, 4, "four grains");
  assert.ok($("digest").textContent.includes("dataset"));
  assert.ok($("grains").children.some((n) => n.textContent.includes("商品")));
  assert.ok($("grains").children.every((n) => n.textContent.includes("原生指标")));

  // boot() must have finished wiring by now; bind runs after the first await.
  await waitFor(() => typeof $("runBtn").onclick === "function", "run button binding");

  // 2. default order query: hand-checked order_amount total is ¥443.00.
  await run();
  await waitFor(() => $("resultTag").textContent.includes("订单 · 9 行"), "order result rows");
  let text = $("resultBody").textContent;
  assert.ok(text.includes("443.00"), "order amount ¥443.00; got: " + text.slice(0, 500));
  assert.ok(text.includes("原始位置"), "position column rendered");

  // 3. add discount_amount at order grain and rerun: 5150 cents = ¥51.50.
  await waitFor(() => metricLabel("优惠金额") !== null, "discount metric listed");
  await selectMetric("优惠金额");
  await run();
  await waitFor(() => $("resultBody").textContent.includes("51.50"), "discount total ¥51.50");
  text = $("resultBody").textContent;
  assert.ok(text.includes("¥51.50"), "discount total ¥51.50");
  assert.ok(text.includes("$.discounts[0]"), "discount positions are traceable");

  // 4. item grain: per-position rows, original nested paths, allocation totals.
  const itemBtn = $("grains").children.find((n) => n.textContent.includes("商品"));
  itemBtn.onclick();
  assert.equal($("grainTag").textContent, "item");
  assert.ok($("grainHint").textContent.includes("只展开 商品 数组"));
  await waitFor(() => metricLabel("商品分摊运费") !== null, "item metric list");
  await selectMetric("商品分摊运费");
  await run();
  await waitFor(() => $("resultTag").textContent.includes("商品 · 11 行"), "item rows: 3+2+1+2+1+2=11");
  text = $("resultBody").textContent;
  assert.ok(text.includes("ORD-1001"));
  assert.ok(text.includes("$.items[0]"), "original item positions shown");
  assert.ok(text.includes("32.00"), "allocated shipping total 3200 cents");
  // 5. naive fan-out comparison is rendered from the real server payload.
  assert.ok(text.includes("直接三数组展开"), "fanout comparison rendered");
  assert.ok(text.includes("22"), "22 naive rows in comparison");

  // 6. illegal metrics at item grain are visibly disabled with a reason.
  const rejected = $("metrics").children.filter((n) => n.classList.contains("disabled"));
  assert.ok(rejected.length >= 3, "discount/order/fulfillment metrics disabled at item grain");
  assert.ok(rejected.some((n) => n.textContent.includes("优惠金额")));
  assert.ok(rejected.every((n) => n.textContent.includes("不可用")));

  // 7. trace dialog shows accept/reject for every grain.
  $("traceBtn").onclick();
  assert.equal($("traceDialog").open, true);
  const trace = $("traceBody").textContent;
  for (const label of ["订单粒度", "商品粒度", "履约粒度", "优惠粒度"]) {
    assert.ok(trace.includes(label), "trace covers " + label);
  }
  assert.ok(trace.includes("拒绝") && trace.includes("可用"));

  // 8. order dialog: raw document, bridge rows by position, plan history.
  $("orders").children[0].onclick();
  await waitFor(() => $("orderBody").textContent.includes("计划历史"), "order dialog loaded");
  assert.equal($("orderDialog").open, true);
  const orderText = $("orderBody").textContent;
  assert.ok(orderText.includes("原始文档"));
  assert.ok(orderText.includes("$.discounts[2]"), "bridge rows keep element positions");
  assert.ok(orderText.includes("initial load"), "plan history reason");
  assert.ok(orderText.includes("桥边"), "edge count badge");

  // 9. structural upgrade from the UI reloads state and bumps projection.
  $("upgradeBtn").onclick();
  await waitFor(() => $("digest").textContent.includes("projection 1"), "projection bumped");
  await waitFor(() => $("toast").textContent.includes("结构升级"), "upgrade toast");

  // 10. upgrade keeps the hand-checked values: recompute happened automatically.
  await waitFor(() => $("resultTag").textContent.includes("商品 · 11 行"), "result after reload");

  console.log("ui smoke ok: boot / order / item / disabled metrics / trace / order dialog / upgrade");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
