# 订单粒度分析台（商品 / 优惠 / 履约）

一个从零实现的、可核查的嵌套订单分析执行页。订单文档里 `items`、`discounts`、
`fulfillments` 是三条并列数组；直接同时展开会让行数和金额按乘积放大。本项目把
"粒度"变成一等概念：查询先声明输出粒度，再由计划器判定每个指标在该粒度上是否可加，
可加则聚合，不可加则拒绝或回到原生粒度重算，结果每一行都带原始嵌套位置。

## 运行

```bash
python3 -m app.server --port 8765     # 打开 http://127.0.0.1:8765
python3 -m tests.run                  # 37 个测试，无第三方依赖（含页面冒烟）
python3 bench/run.py                  # 放大倍数与耗时对照
PORT=8811 ./tests/run_ui_smoke.sh     # 单独跑页面冒烟（需要 node；可无头环境）
```

页面冒烟测试用极小的 DOM 垫片在真实 HTTP 服务上运行 `web/app.js`，驱动粒度切换、
指标勾选、执行、追溯弹窗、订单弹窗和结构升级，断言渲染出的基准数字。它不能替代
浏览器视觉检查，但能抓住绑定失效、id 拼写错误和响应字段误用。

数据：`data/orders.json`（9 张订单，真实的多优惠/多履约/空数组/三种 schema 版本），
`data/corrections.json`（ORD-1001 的增量更正）。首次启动自动建库并载入。

## 目录

```
app/schema.py      粒度、数组、原始位置的定义
app/metrics.py     指标目录：类型（可加/重算）、原生粒度、可加粒度、口径说明
app/normalize.py   v1/v2/v3 三种源 schema → 规范文档，元素位置 = 身份
app/store.py       SQLite：不可变订单版本、按位置键的桥表、计划/计划历史、结果缓存
app/plan.py        计划与三个摘要（数据集摘要 / 查询摘要 / 计划哈希）
app/executor.py    粒度校验、执行（先聚合再单数组展开）、分摊、追溯、放大对照
app/service.py     服务门面（Web/CLI/测试共用）
app/server.py      stdlib HTTP + JSON API
web/               分析执行页（无框架）
data/              基准与更正样例
bench/             放大倍数基准
tests/             37 个测试 + 页面冒烟（ui_smoke.mjs，python 测试自动调用）
docs/DESIGN.md     设计决策与口径判定
docs/BASELINE.md   手工可核对的基准数字
```

## 关键结论（详见 docs/DESIGN.md）

- **先聚合再关联**：以订单为锚，先把三条数组分别归约到订单粒度，再只展开一个数组。
  需要跨数组的分摊（优惠→商品、运费→商品）用整数最大余额法在订单内完成，
  分摊总额恒等于原始总额。保留"关联桥"（`bridge_edges`）是为了记录哪些元素位置
  在同一版本里共现（空数组用哨兵记录），而不是为了一次性笛卡尔展开。
- **跨粒度可加**：`item_qty`、`item_gross_amount`、`discount_amount`、
  `fulfillment_shipping_fee`、`fulfillment_qty`、`pos_count`（逐位置）在各自
  源事实"每行至多计一次"的粒度上可加。
- **必须重算**：`item_discount_share`、`item_net_amount`、`item_shipping_share`、
  `item_landed_amount` 只能在其原生（商品）粒度按分量重算；`order_count` 是唯一
  允许跨粒度"去重"的指标（它数的就是订单）。
- **不去重掩盖重复**：优惠码 `WELCOME` 出现两次就是两个位置、两条金额；
  结果按 `code` 分组只是展示，`pos_count` 仍为 2，并标注
  `duplicate_discount_code`。桥表主键是 `(订单, 版本, 粒度, 元素序号)`，
  业务 id 只是元数据。
- **并发一致**：结构升级（projection+1）、增量更正（version+1）、缓存发布
  三者用同一把写事务串行化；查询固定每个订单的
  `(version, projection, plan_hash)` 并计算数据集摘要；发布时按摘要做
  compare-and-swap，旧展开结果无法覆盖新粒度。缓存按
  `(查询, 数据集摘要)` 追加，`result_head` 指向权威条目。
- **中断恢复**：每篇文档 + 检查点一个事务提交；中断后从检查点续跑，
  逐篇校验指纹，最终状态与不中断完全一致（测试逐订单比较 version 与 plan_hash）。
