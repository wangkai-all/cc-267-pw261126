"use strict";

const state = {
  boot: null,
  grain: "order",
  metrics: new Set(["order_amount", "pos_count", "order_count"]),
  groupBy: new Set([]),
  filters: {},
  detail: false,
  result: null,
  busy: false,
};

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

function fmtMoney(cents) {
  if (cents === null || cents === undefined) return "—";
  const sign = cents < 0 ? "-" : "";
  const abs = Math.abs(cents);
  return `${sign}¥${(abs / 100).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtValue(metric, value) {
  const spec = state.boot.catalogue.metrics[metric];
  if (!spec) return String(value);
  if (spec.unit === "cent") return fmtMoney(value);
  return Number(value).toLocaleString("zh-CN");
}

function toast(msg, bad = false) {
  const node = $("#toast");
  node.textContent = msg;
  node.className = "toast show" + (bad ? " bad" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (node.className = "toast"), 3600);
}

async function api(path, options) {
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({ ok: false, error: "响应不是 JSON" }));
  if (!res.ok || data.ok === false) {
    const err = new Error(data.error || `HTTP ${res.status}`);
    err.data = data;
    throw err;
  }
  return data;
}

// ---------------------------------------------------------------- bootstrap
async function boot() {
  const data = await api("/api/bootstrap");
  state.boot = data;
  if (!state.metrics.size || !validMetrics(state.grain, [...state.metrics]).length) {
    state.metrics = new Set(defaultMetrics(state.grain));
  } else {
    state.metrics = new Set(validMetrics(state.grain, [...state.metrics]));
    if (!state.metrics.size) state.metrics = new Set(defaultMetrics(state.grain));
  }
  renderGrainPicker();
  renderGrainHint();
  renderMetrics();
  renderGroupBy();
  renderOrders();
  renderOps();
  renderDigest();
}

function defaultMetrics(grain) {
  const native = state.boot.catalogue.grains[grain]?.metrics || [];
  const pref = native.slice(0, 3);
  if (grain === "order") return ["order_amount", "item_net_amount", "discount_amount", "pos_count"];
  return pref.concat(["pos_count"]);
}

function validMetrics(grain, metrics) {
  const ref = state.boot.metric_reference[grain];
  return metrics.filter((m) => ref.metrics.find((x) => x.metric === m && x.accepted));
}

function renderDigest() {
  const d = state.boot.stats.dataset_digest || "";
  $("#digest").textContent = `dataset ${d.slice(0, 12)} · projection ${state.boot.stats.projection}`;
}

// --------------------------------------------------------------------- grain
function renderGrainPicker() {
  const box = $("#grains");
  box.innerHTML = "";
  for (const grain of state.boot.grains) {
    const btn = el("button", "grain-btn" + (grain.name === state.grain ? " active" : ""));
    btn.appendChild(el("b", "", grain.label));
    const native = state.boot.catalogue.grains[grain.name].metrics.filter((m) => m !== "pos_count").length;
    btn.appendChild(el("span", "", `${native} 个原生指标`));
    btn.onclick = () => {
      if (state.grain === grain.name) return;
      state.grain = grain.name;
      state.groupBy = new Set();
      state.metrics = new Set(validMetrics(grain.name, [...state.metrics]));
      if (!state.metrics.size) state.metrics = new Set(defaultMetrics(grain.name));
      renderGrainPicker();
      renderGrainHint();
      renderMetrics();
      renderGroupBy();
      $("#grainTag").textContent = grain.name;
    };
    box.appendChild(btn);
  }
  $("#grainTag").textContent = state.grain;
}

function renderGrainHint() {
  const g = state.boot.grains.find((x) => x.name === state.grain);
  $("#grainHint").textContent =
    state.grain === "order"
      ? "订单粒度：一行一张订单。所有指标在此原生可读或可去重汇总。"
      : `${g.label}粒度：只展开 ${g.label} 数组；其他数组先按订单聚合，再按定义分摊。`;
}

// ------------------------------------------------------------------ metrics
function renderMetrics() {
  const box = $("#metrics");
  box.innerHTML = "";
  const ref = state.boot.metric_reference[state.grain];
  for (const entry of ref.metrics) {
    if (entry.metric === "pos_count" || entry.metric === "order_count") continue;
    const wrap = el("label", "metric" + (entry.accepted ? "" : " disabled"));
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = state.metrics.has(entry.metric);
    cb.disabled = !entry.accepted;
    cb.onchange = () => {
      if (cb.checked) state.metrics.add(entry.metric);
      else state.metrics.delete(entry.metric);
      updateMetricTag();
    };
    const body = el("div");
    const name = el("div", "name");
    name.appendChild(document.createTextNode(entry.label));
    const kind = el(
      "span",
      "kind " + entry.kind,
      entry.kind === "additive" ? "可加" : entry.kind === "derived" ? "重算" : entry.kind
    );
    name.appendChild(kind);
    body.appendChild(name);
    body.appendChild(el("div", "why", entry.accepted ? entry.reason : "不可用：" + entry.reason));
    wrap.appendChild(cb);
    wrap.appendChild(body);
    box.appendChild(wrap);
  }
  // Always-available structural metrics.
  for (const [metric, label, why] of [
    ["pos_count", "原始位置数", "统计真实重复，逐位置计数"],
    ["order_count", "订单数（去重）", "按输出粒度重算 distinct order_id"],
  ]) {
    const wrap = el("label", "metric");
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = state.metrics.has(metric);
    cb.onchange = () => {
      if (cb.checked) state.metrics.add(metric);
      else state.metrics.delete(metric);
      updateMetricTag();
    };
    const body = el("div");
    body.appendChild(el("div", "name", label));
    body.appendChild(el("div", "why", why));
    wrap.appendChild(cb);
    wrap.appendChild(body);
    box.appendChild(wrap);
  }
  updateMetricTag();
}

function updateMetricTag() {
  $("#metricTag").textContent = `${state.metrics.size} 个`;
}

// ----------------------------------------------------------------- group by
function renderGroupBy() {
  const box = $("#groupBy");
  box.innerHTML = "";
  const g = state.boot.catalogue.grains[state.grain];
  const dims = g.dimensions.filter((d) => !["order"].includes(d));
  if (!dims.length) {
    box.appendChild(el("span", "small muted", "该粒度没有可用分组维度"));
    return;
  }
  for (const dim of dims) {
    const chip = el("span", "chip" + (state.groupBy.has(dim) ? " on" : ""), state.boot.catalogue.dimensions[dim].label);
    chip.onclick = () => {
      if (state.groupBy.has(dim)) state.groupBy.delete(dim);
      else state.groupBy.add(dim);
      renderGroupBy();
    };
    box.appendChild(chip);
  }
}

// ------------------------------------------------------------------- orders
function renderOrders() {
  const box = $("#orders");
  box.innerHTML = "";
  for (const order of state.boot.orders) {
    const chip = el("span", "chip", order.order_id);
    chip.title = `${order.order_key} · v${order.version} · ${order.schema_version}`;
    chip.onclick = () => showOrder(order.order_key);
    box.appendChild(chip);
  }
}

// ---------------------------------------------------------------------- run
function parseFilters(text) {
  const filters = {};
  const trimmed = text.trim();
  if (!trimmed) return filters;
  for (const part of trimmed.split(/\s+/)) {
    const eq = part.indexOf("=");
    if (eq < 0) continue;
    const key = part.slice(0, eq).trim();
    const value = part.slice(eq + 1).trim();
    if (key && value) filters[key] = value.split(",").map((v) => v.trim()).filter(Boolean);
  }
  return filters;
}

async function runQuery(fresh = false) {
  if (state.busy) return;
  if (!state.metrics.size) {
    toast("至少选择一个指标", true);
    return;
  }
  state.busy = true;
  $("#runBtn").disabled = true;
  $("#freshBtn").disabled = true;
  const t0 = performance.now();
  try {
    const body = {
      grain: state.grain,
      metrics: [...state.metrics],
      group_by: [...state.groupBy],
      filters: parseFilters($("#filterText").value),
      use_cache: !fresh,
      publish: true,
      detail: state.detail,
    };
    const data = await api("/api/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    data._ms = Math.round(performance.now() - t0);
    state.result = data;
    renderResult(data);
    $("#resultTag").textContent = `${data.grain_label} · ${data.rows.length} 行`;
    $("#runMeta").textContent =
      `${data._ms} ms · ${data.orders_considered} 张订单 · 缓存${data.cache.hit ? "命中" : "未命中"} · ` +
      `数据集 ${data.plan.dataset_digest.slice(0, 12)}`;
    await refreshOps();
  } catch (err) {
    toast(err.message, true);
    renderError(err);
  } finally {
    state.busy = false;
    $("#runBtn").disabled = false;
    $("#freshBtn").disabled = false;
  }
}

function renderError(err) {
  const body = $("#resultBody");
  body.innerHTML = "";
  const box = el("div", "panel");
  box.appendChild(el("h2", "", "请求被拒绝"));
  box.appendChild(el("div", "", err.message));
  if (err.data?.details?.length) {
    const ul = el("ul", "warn-list");
    for (const d of err.data.details) {
      ul.appendChild(el("li", "", `${d.label || d.metric}: ${d.reason || d.message || ""}`));
    }
    box.appendChild(ul);
  }
  body.appendChild(box);
}

// ------------------------------------------------------------------- render
function renderResult(data) {
  const body = $("#resultBody");
  body.innerHTML = "";

  const badges = el("div", "badges");
  const cacheBadge = el("span", "badge " + (data.cache.hit ? "hit" : "miss"), data.cache.hit ? "缓存命中" : "重新计算");
  badges.appendChild(cacheBadge);
  if (data.published) {
    const status = data.published.status;
    const cls = status === "refused" ? "bad" : status === "existing" ? "" : "good";
    badges.appendChild(el("span", "badge " + cls, `发布:${status}`));
  }
  badges.appendChild(el("span", "badge", `粒度 ${data.grain}`));
  badges.appendChild(el("span", "badge", `订单 ${data.orders_considered}`));
  if (data.fanout) {
    badges.appendChild(
      el("span", "badge bad", `直接展开将产生 ${data.fanout.naive_rows} 行`)
    );
  }
  badges.appendChild(el("span", "badge", `计划 ${data.plan.dataset_digest.slice(0, 10)}`));
  body.appendChild(badges);

  const totals = el("div", "totals");
  for (const metric of data.metrics) {
    const card = el("div", "stat");
    card.appendChild(el("div", "k", state.boot.catalogue.metrics[metric].label));
    const v = el("div", "v", fmtValue(metric, data.totals[metric] ?? 0));
    card.appendChild(v);
    totals.appendChild(card);
  }
  body.appendChild(totals);

  if (data.rows.length) {
    body.appendChild(renderTable(data));
  } else {
    body.appendChild(el("div", "empty", "当前粒度/过滤条件下没有输出行。空数组不产生行，但订单仍在订单粒度可见。"));
  }

  const warnBlock = renderWarnings(data);
  if (warnBlock) body.appendChild(warnBlock);

  if (data.fanout) body.appendChild(renderFanout(data.fanout));
  if (data.detail && data.details) body.appendChild(renderDetails(data));
  body.appendChild(renderPlan(data));
}

function renderTable(data) {
  const wrap = el("div", "table-wrap");
  const table = el("table");
  const thead = el("thead");
  const hr = el("tr");
  const groupKeys = data.group_by.length ? data.group_by : [];
  for (const dim of groupKeys.concat(data.group_by.length ? [] : ["范围"])) {
    hr.appendChild(el("th", "", dim === "范围" ? "订单" : state.boot.catalogue.dimensions[dim].label));
  }
  for (const metric of data.metrics) {
    hr.appendChild(el("th", "num", state.boot.catalogue.metrics[metric].label));
  }
  hr.appendChild(el("th", "", "原始位置 / 追溯"));
  hr.appendChild(el("th", "", "标记"));
  thead.appendChild(hr);
  table.appendChild(thead);

  const tbody = el("tbody");
  for (const row of data.rows) {
    const tr = el("tr");
    if (data.group_by.length) {
      for (const dim of data.group_by) tr.appendChild(el("td", "", String(row.group[dim] ?? "")));
    } else {
      const orders = row.provenance.orders;
      tr.appendChild(el("td", "wrap", orders.length === 1 ? orders[0] : `${orders.length} 张订单`));
    }
    for (const metric of data.metrics) {
      tr.appendChild(el("td", "num", fmtValue(metric, row.metrics[metric] ?? 0)));
    }
    const posCell = el("td", "wrap");
    const positions = row.provenance.positions || [];
    const shown = positions.slice(0, 8);
    for (const p of shown) {
      const chip = el("span", `pos grain-${p.grain}`, `${p.position}${p.grain !== "order" ? " · " + p.source_id : ""}`);
      chip.title = `${p.grain} · ${p.position} · source_id=${p.source_id}`;
      chip.onclick = () => copyText(p.position);
      posCell.appendChild(chip);
    }
    if (positions.length > shown.length) {
      posCell.appendChild(el("span", "small muted", `+${positions.length - shown.length} 个位置`));
    }
    const derivedKeys = Object.keys(row.provenance.derived_from || {});
    if (derivedKeys.length) {
      const det = el("details", "trace");
      det.appendChild(el("summary", "", `重算来源（${derivedKeys.length}）`));
      const inner = el("div", "inner");
      for (const key of derivedKeys) {
        const trace = row.provenance.derived_from[key];
        if (!trace || !trace.length) {
          inner.appendChild(el("div", "small muted", `${state.boot.catalogue.metrics[key]?.label || key}: 空数组，按 0 参与`));
          continue;
        }
        const line = el("div", "small");
        line.appendChild(el("b", "", `${state.boot.catalogue.metrics[key]?.label || key}: `));
        for (const p of trace.slice(0, 10)) {
          const chip = el("span", `pos grain-${p.grain}`, `${p.position} · ${p.source_id}`);
          chip.onclick = () => copyText(p.position);
          line.appendChild(chip);
        }
        if (trace.length > 10) line.appendChild(el("span", "small muted", `+${trace.length - 10}`));
        inner.appendChild(line);
      }
      det.appendChild(inner);
      posCell.appendChild(det);
    }
    tr.appendChild(posCell);
    const flagCell = el("td", "wrap");
    for (const flag of row.provenance.flags || []) {
      flagCell.appendChild(el("span", "flag" + (flag.startsWith("negative") ? " bad" : ""), flagLabel(flag)));
    }
    tr.appendChild(flagCell);
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  return wrap;
}

function flagLabel(flag) {
  const map = {
    no_discounts: "无优惠数组",
    no_fulfillments: "无履约数组",
    no_items: "无商品数组",
    duplicate_discount_code: "优惠码重复(真实)",
    negative_net_amount: "净额为负",
    zero_fulfillment_qty: "履约件数 0",
  };
  return map[flag] || flag;
}

function renderWarnings(data) {
  if (!data.warnings || !data.warnings.length) return null;
  const panel = el("div", "panel");
  panel.appendChild(el("h2", "", "口径与数据提示"));
  const ul = el("ul", "warn-list");
  for (const w of data.warnings) {
    const li = el("li");
    li.appendChild(el("b", "", w.kind + " · "));
    li.appendChild(document.createTextNode(w.message));
    if (w.orders && w.orders.length) {
      const detail = w.orders
        .slice(0, 6)
        .map((o) => `${o.order_key}${o.empty ? ":" + o.empty.join("/") : ""}`)
        .join("，");
      li.appendChild(el("span", "small muted", `（${detail}${w.orders.length > 6 ? " …" : ""}）`));
    }
    if (w.items && w.items.length) {
      const detail = w.items.slice(0, 6).map((i) => `${i.order_key} ${i.code}×${i.positions}`).join("，");
      li.appendChild(el("span", "small muted", `（${detail}）`));
    }
    ul.appendChild(li);
  }
  panel.appendChild(ul);
  return panel;
}

function renderFanout(fanout) {
  const panel = el("details", "panel");
  panel.appendChild(el("summary", "", `对照：直接三数组展开（仅用于核对放大倍数，不是查询结果）`));
  panel.appendChild(el("div", "small muted", fanout.note));
  const table = el("table");
  table.className = "runs";
  const thead = el("thead");
  const hr = el("tr");
  for (const h of ["订单", "商品", "优惠", "履约", "展开行数", "展开后商品金额", "真实商品金额"]) {
    hr.appendChild(el("th", "", h));
  }
  thead.appendChild(hr);
  table.appendChild(thead);
  const tbody = el("tbody");
  for (const o of fanout.inflated_orders || []) {
    const tr = el("tr");
    tr.appendChild(el("td", "mono", o.order_key));
    for (const v of [o.items, o.discounts, o.fulfillments, o.rows]) tr.appendChild(el("td", "num", String(v)));
    tr.appendChild(el("td", "num", fmtMoney(o.inflated_item_gross)));
    tr.appendChild(el("td", "num", fmtMoney(o.true_item_gross)));
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  const wrap = el("div", "table-wrap");
  wrap.appendChild(table);
  panel.appendChild(wrap);
  panel.appendChild(
    el(
      "div",
      "small muted",
      `合计：直接展开 ${fanout.naive_rows} 行；内连接会丢掉 ${fanout.orders_dropped_by_inner_join} 张含空数组的订单；` +
        `商品金额被放大到 ${fmtMoney(fanout.naive_item_gross)}。`
    )
  );
  return panel;
}

function renderDetails(data) {
  const panel = el("div", "panel");
  panel.appendChild(el("h2", "", "逐位置明细（每一行都能回到原始嵌套位置）"));
  const wrap = el("div", "table-wrap");
  const table = el("table");
  const thead = el("thead");
  const hr = el("tr");
  hr.appendChild(el("th", "", "订单"));
  hr.appendChild(el("th", "", "位置"));
  for (const metric of data.metrics) hr.appendChild(el("th", "num", state.boot.catalogue.metrics[metric].label));
  hr.appendChild(el("th", "", "标记"));
  thead.appendChild(hr);
  table.appendChild(thead);
  const tbody = el("tbody");
  for (const d of data.details) {
    const tr = el("tr");
    tr.appendChild(el("td", "mono", d.order_id));
    const posCell = el("td");
    for (const p of d.positions) {
      const chip = el("span", `pos grain-${p.grain}`, `${p.position}${p.grain !== "order" ? " · " + p.source_id : ""}`);
      chip.onclick = () => copyText(p.position);
      posCell.appendChild(chip);
    }
    tr.appendChild(posCell);
    for (const metric of data.metrics) tr.appendChild(el("td", "num", fmtValue(metric, d.metrics[metric] ?? 0)));
    const flags = el("td");
    for (const f of d.flags) flags.appendChild(el("span", "flag", flagLabel(f)));
    tr.appendChild(flags);
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  panel.appendChild(wrap);
  return panel;
}

function renderPlan(data) {
  const panel = el("details", "panel");
  panel.appendChild(el("summary", "", "执行计划与可加性判定"));
  panel.appendChild(el("div", "small muted", data.plan.strategy));
  const wrap = el("div", "table-wrap");
  const table = el("table");
  const thead = el("thead");
  const hr = el("tr");
  for (const h of ["指标", "类型", "原生粒度", "可加粒度", "判定"]) hr.appendChild(el("th", "", h));
  thead.appendChild(hr);
  table.appendChild(thead);
  const tbody = el("tbody");
  for (const p of data.metric_policy) {
    const tr = el("tr");
    tr.appendChild(el("td", "", p.label));
    tr.appendChild(el("td", "", p.kind));
    tr.appendChild(el("td", "", p.native));
    tr.appendChild(el("td", "mono", (p.additive_at || []).join(", ")));
    tr.appendChild(el("td", "wrap small", p.reason));
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  panel.appendChild(wrap);
  const orders = el("div", "small muted", `已固定订单状态：${data.plan.orders.length} 张，数据集 ${data.plan.dataset_digest.slice(0, 16)}…`);
  panel.appendChild(orders);
  return panel;
}

function copyText(text) {
  navigator.clipboard?.writeText(text).then(
    () => toast(`已复制 ${text}`),
    () => toast(text)
  );
}

// --------------------------------------------------------------------- ops
function renderOps() {
  const s = state.boot.stats;
  $("#opsStats").textContent =
    `订单 ${s.orders} · 版本 ${s.versions} · 桥表行 ${s.bridge_rows} · 缓存 ${s.cache_entries} · projection ${s.projection}`;
  const runs = $("#runs");
  runs.innerHTML = "";
  const table = el("table", "runs");
  const thead = el("thead");
  const hr = el("tr");
  for (const h of ["run", "状态", "已处理", "恢复自", "说明"]) hr.appendChild(el("th", "", h));
  thead.appendChild(hr);
  table.appendChild(thead);
  const tbody = el("tbody");
  for (const r of state.boot.load_runs.slice().reverse()) {
    const tr = el("tr");
    tr.appendChild(el("td", "mono", r.run_id.replace(/^run-/, "")));
    tr.appendChild(el("td", "", r.status));
    tr.appendChild(el("td", "num", String(r.processed)));
    tr.appendChild(el("td", "num", String(r.resumed_from || 0)));
    tr.appendChild(el("td", "wrap small", r.note));
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  runs.appendChild(table);

  const hist = $("#cacheHist");
  hist.innerHTML = "";
  if (!state.boot.cache.length) {
    hist.appendChild(el("div", "small muted", "尚无缓存条目"));
    return;
  }
  const t2 = el("table", "runs");
  const h2 = el("thead");
  const r2 = el("tr");
  for (const h of ["查询", "数据集", "粒度", "当前头", "时间"]) r2.appendChild(el("th", "", h));
  h2.appendChild(r2);
  t2.appendChild(h2);
  const b2 = el("tbody");
  for (const c of state.boot.cache.slice().reverse()) {
    const tr = el("tr");
    tr.appendChild(el("td", "mono", c.query_key.slice(0, 10)));
    tr.appendChild(el("td", "mono", c.dataset_digest.slice(0, 10)));
    tr.appendChild(el("td", "", c.grain));
    tr.appendChild(el("td", "", c.is_head ? "是" : "旧"));
    tr.appendChild(el("td", "small", c.computed_at.slice(11, 19)));
    b2.appendChild(tr);
  }
  t2.appendChild(b2);
  hist.appendChild(t2);
}

async function refreshOps() {
  const data = await api("/api/ops");
  state.boot.stats = data.stats;
  state.boot.load_runs = data.load_runs;
  state.boot.cache = data.cache;
  renderOps();
  renderDigest();
}

// ------------------------------------------------------------- order dialog
async function showOrder(orderKey) {
  const dlg = $("#orderDialog");
  $("#orderTitle").textContent = orderKey;
  $("#orderBody").innerHTML = '<div class="small muted">加载中…</div>';
  dlg.showModal();
  try {
    const data = await api("/api/orders/" + encodeURIComponent(orderKey));
    const body = $("#orderBody");
    body.innerHTML = "";
    const s = data.state;
    const head = el("div", "badges");
    head.appendChild(el("span", "badge", `${s.schema_version} · v${s.version} · projection ${s.projection}`));
    head.appendChild(el("span", "badge", `商品 ${s.shape.items} / 优惠 ${s.shape.discounts} / 履约 ${s.shape.fulfillments}`));
    head.appendChild(el("span", "badge", `桥边 ${data.edges}`));
    head.appendChild(el("span", "badge", `plan ${s.plan_hash.slice(0, 12)}`));
    body.appendChild(head);

    body.appendChild(el("h2", "", "原始文档"));
    const pre = el("pre", "doc", JSON.stringify(data.document, null, 2));
    body.appendChild(pre);

    body.appendChild(el("h2", "", "桥表行（按元素位置，重复业务 id 保留）"));
    const wrap = el("div", "table-wrap");
    const table = el("table");
    const thead = el("thead");
    const hr = el("tr");
    for (const h of ["粒度", "位置", "source_id", "数组规模", "载荷"]) hr.appendChild(el("th", "", h));
    thead.appendChild(hr);
    table.appendChild(thead);
    const tbody = el("tbody");
    for (const r of data.bridge_rows) {
      const tr = el("tr");
      tr.appendChild(el("td", "", r.grain));
      tr.appendChild(el("td", "mono", r.payload.position || "$"));
      tr.appendChild(el("td", "mono", r.source_id));
      tr.appendChild(el("td", "num", String(r.array_size)));
      tr.appendChild(el("td", "wrap mono small", JSON.stringify(r.payload)));
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    body.appendChild(wrap);

    body.appendChild(el("h2", "", "计划历史（版本 × projection）"));
    const hist = el("div", "table-wrap");
    const t2 = el("table");
    const h2 = el("thead");
    const r2 = el("tr");
    for (const h of ["订单版本", "projection", "plan hash", "原因", "时间"]) r2.appendChild(el("th", "", h));
    h2.appendChild(r2);
    t2.appendChild(h2);
    const b2 = el("tbody");
    for (const h of data.plan_history) {
      const tr = el("tr");
      tr.appendChild(el("td", "num", String(h.order_version)));
      tr.appendChild(el("td", "num", String(h.projection)));
      tr.appendChild(el("td", "mono", h.plan_hash.slice(0, 16)));
      tr.appendChild(el("td", "", h.reason));
      tr.appendChild(el("td", "small", h.created_at.slice(0, 19)));
      b2.appendChild(tr);
    }
    t2.appendChild(b2);
    hist.appendChild(t2);
    body.appendChild(hist);
  } catch (err) {
    $("#orderBody").textContent = err.message;
  }
}

// ------------------------------------------------------------- trace dialog
function showTrace() {
  const dlg = $("#traceDialog");
  const body = $("#traceBody");
  body.innerHTML = "";
  for (const grain of state.boot.grains) {
    const ref = state.boot.metric_reference[grain.name];
    body.appendChild(el("h2", "", `${grain.label}粒度`));
    const wrap = el("div", "table-wrap");
    const table = el("table");
    const thead = el("thead");
    const hr = el("tr");
    for (const h of ["指标", "类型", "判定", "说明"]) hr.appendChild(el("th", "", h));
    thead.appendChild(hr);
    table.appendChild(thead);
    const tbody = el("tbody");
    for (const m of ref.metrics) {
      const tr = el("tr");
      tr.appendChild(el("td", "", m.label));
      tr.appendChild(el("td", "", m.kind));
      const ok = el("td");
      ok.appendChild(el("span", "badge " + (m.accepted ? "good" : "bad"), m.accepted ? "可用" : "拒绝"));
      tr.appendChild(ok);
      tr.appendChild(el("td", "wrap small", m.reason));
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    body.appendChild(wrap);
  }
  dlg.showModal();
}

// ------------------------------------------------------------------- wiring
function bind() {
  $("#runBtn").onclick = () => runQuery(false);
  $("#freshBtn").onclick = () => runQuery(true);
  $("#detailToggle").onclick = () => {
    state.detail = !state.detail;
    $("#detailToggle").textContent = `明细追溯：${state.detail ? "开" : "关"}`;
  };
  $("#traceBtn").onclick = showTrace;
  $("#upgradeBtn").onclick = async () => {
    try {
      const data = await api("/api/upgrade", { method: "POST", body: "{}" });
      toast(`结构升级：projection ${data.upgrade.old_projection} → ${data.upgrade.projection}，${data.upgrade.orders} 张订单已重投影`);
      await reloadAll();
    } catch (err) {
      toast(err.message, true);
    }
  };
  $("#resumeBtn").onclick = () => resumeDemo();
  document.querySelectorAll("[data-load]").forEach((btn) => {
    btn.onclick = async () => {
      const fixture = btn.dataset.load;
      try {
        await api("/api/load", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ fixture }),
        });
        toast(`已载入 ${fixture}`);
        await reloadAll();
      } catch (err) {
        toast(err.message, true);
      }
    };
  });
  document.querySelectorAll("[data-close]").forEach((btn) => {
    btn.onclick = () => btn.closest("dialog").close();
  });
  $("#filterText").addEventListener("keydown", (e) => {
    if (e.key === "Enter") runQuery(false);
  });
}

async function reloadAll() {
  await boot();
  if (state.result) await runQuery(true);
}

async function resumeDemo() {
  try {
    const data = await api("/api/resume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ interrupt_after: 4 }),
    });
    const r = data.resume;
    toast(
      `中断于第 ${r.interrupted.processed} 篇（已提交检查点 ${r.committed_before_interrupt} 个），` +
        `恢复后处理 ${r.resumed.processed} 篇，恢复前缀 ${r.resumed.resumed_from} 篇`
    );
    await reloadAll();
  } catch (err) {
    toast(err.message || "恢复失败", true);
  }
}

boot().then(() => {
  bind();
  $("#grainTag").textContent = state.grain;
});
