# 设计说明：粒度、可加性、关联与并发

## 1. 问题

一张订单文档长这样：

```json
{
  "order_id": "ORD-1001", "order_amount": 10000,
  "items":       [ {item_id, sku, qty, unit_price_cents}, ... ],
  "discounts":   [ {discount_id, code, kind, amount_cents}, ... ],
  "fulfillments":[ {fulfillment_id, carrier, shipping_fee_cents, qty}, ... ]
}
```

业务要"把商品、优惠、履约关联起来看"。SQL 直觉会写三表 JOIN 或
`CROSS JOIN`/`UNNEST` 三层数组，然后 `SUM(item_amount)`。对 ORD-1001
（3 商品 × 3 优惠 × 2 履约）会得到 18 行，商品金额被数了 6 次：
10000 → 60000。若再按 `code` 去重，`WELCOME` 的两条会被合并，真实重复被抹掉；
若任一数组为空，`INNER JOIN` 还会整单消失。

所以这不是"写对 JOIN"的问题，而是**先声明粒度与可加性，再决定执行形状**。

## 2. 粒度模型

| 粒度 | 一行代表 | 数组 |
|---|---|---|
| `order` | 一张订单 | —（锚） |
| `item` | 一个商品元素位置 | `$.items[i]` |
| `discount` | 一个优惠元素位置 | `$.discounts[i]` |
| `fulfillment` | 一个履约元素位置 | `$.fulfillments[i]` |

元素身份 **永远** 是 `(订单, 版本, 粒度, 元素序号)`，不是业务 id。桥表
`bridge_rows` 的主键就是这四元组；`source_id` 只是元数据。这样：

- 同一 `code=WELCOME` 的两条优惠是两行、两条金额，`pos_count=2`；
- 同一 `tracking_no` 的两条履约也是两行；
- 缺失业务 id 时用 `__pos_<i>` 占位，不丢弃元素。

`bridge_edges` 记录同一版本里哪些位置共现（空数组侧用 `-1` 哨兵），
它用于一致性审计（边数 = 各数组规模之积，含哨兵），**不**用于一次性展开。

## 3. 指标目录（app/metrics.py）

每个指标声明：`kind`（`additive` / `derived`）、`native` 粒度、
`additive_at` 粒度集合、口径说明。执行器只信这张表。

| 指标 | 类型 | 原生 | 可加粒度 | 说明 |
|---|---|---|---|---|
| `order_amount` | 可加 | order | order | 订单头金额，不是三条数组之和 |
| `item_qty` | 可加 | item | item, order | 商品件数 |
| `item_gross_amount` | 可加 | item | item, order | `unit_price*qty` |
| `item_discount_share` | **重算** | item | item, order | 订单内优惠按商品原价最大余额分摊 |
| `item_net_amount` | **重算** | item | item, order | 原价 − 分摊优惠 |
| `item_shipping_share` | **重算** | item | item, order | 运费按商品件数分摊；无履约时 0 |
| `item_landed_amount` | **重算** | item | item, order | 净额 + 分摊运费 |
| `discount_amount` | 可加 | discount | discount, order | 逐优惠元素 |
| `fulfillment_shipping_fee` | 可加 | fulfillment | fulfillment, order | 逐履约元素 |
| `fulfillment_qty` | 可加 | fulfillment | fulfillment, order | 逐履约元素 |
| `pos_count` | 可加 | — | 所有粒度 | 原始位置数，每个位置计一次 |
| `order_count` | **重算** | order | —（按输出重算） | 唯一允许的跨粒度去重：数订单 |

判定规则：

1. `native == grain` → 原生可读。
2. `grain ∈ additive_at` → 该粒度下每条源事实至多贡献一次，可 SUM。
3. 否则拒绝，并说明应回到哪个粒度。
4. `derived` 指标在输出粒度按其 `depends_on` **重新计算**，绝不把别的粒度的
   结果按比例放大/缩小；跨数组的分摊在订单内用整数算法完成，保证
   `Σshare == 原始总额`（分币不丢、不造）。
5. 维度只能用于它所在粒度的分组/过滤；例如 `carrier` 不能作为商品粒度的
   过滤条件——那会改变口径。

### 为什么 `discount_amount` 不在商品粒度可加

优惠不是商品行的属性。把它"摊"到商品上是一种**分摊规则**（我们确实提供
`item_discount_share`），但那是 `derived` 指标，性质和 `SUM(discount_amount)`
不同。如果把 `SUM(discount_amount)` 声明为商品粒度可加，任何一次多优惠 × 多商品的
展开都会把它按商品数重复计数；也就是题目里"直接展开成倍放大金额"的同一错误。
`fulfillment_shipping_fee`、`fulfillment_qty` 同理。

## 4. 执行形状：先聚合再关联

```
订单行 (1)
  ├─ items       ──(只在 item 粒度展开一次)
  ├─ discounts   ──(聚合为订单总额 → 按商品原价分摊)
  └─ fulfillments──(聚合为订单总额 → 按商品件数分摊)
```

- 输出 `order`：三条数组都先归约，行数 = 订单数。不会因数组规模放大。
- 输出 `item`：只展开 `items`；优惠/运费先在订单内归约，再分摊到商品行。
- 输出 `fulfillment` / `discount`：只展开对应数组；不碰其他数组的行数。
- 任一数组为空：不产生该粒度的行，但订单在 `order` 粒度仍可见；分摊额为 0。
  结果里以 `skipped_orders` + `warnings.empty_arrays` 明确说明，而不是静默丢单。

结果行带 `provenance.positions`（本行自己的原始位置）与
`provenance.derived_from`（分摊/汇总来自哪些位置），前端点击即可复制
`$.discounts[2]` 这类路径。`detail=true` 时额外返回逐位置明细。

## 5. 关联桥的保留与使用

保留 `bridge_edges` 的理由不是性能，而是**可核查**：

1. 记录空数组（`-1` 哨兵），使"订单有商品但无优惠/履约"在关联层可见；
2. 审计：任何时刻边数都应等于 `max(1,|items|)*max(1,|discounts|)*max(1,|fulfillments|)`，
   与桥表行数、计划里的 shape 交叉校验；
3. 对照页用真实规模展示"直接展开"的行数与金额放大倍数——这是对照，不是结果。

执行器本身不做三数组笛卡尔积，因此该表不随查询增长。

## 6. 计划、版本与摘要

每个订单有 `order_plan`：

```
{order_key, order_version, projection, shape{items,discounts,fulfillments},
 grain="order", filter={lifecycle:latest}}
```

`plan_hash = sha256(plan)`。**version、projection、shape 都进入哈希**，所以：

- 增量更正追加 version N+1 → 新哈希；
- 结构升级 projection+1 → 新哈希；
- 数组规模变化 → 新哈希。

查询时对参与订单的 `(order_key, version, projection, plan_hash)` 计算
**数据集摘要** `dataset_digest`。它是发布和缓存一致性的唯一依据。

## 7. 并发：升级、更正、缓存

所有写操作走 `BEGIN IMMEDIATE`（SQLite 串行化写），读走独立连接上的
一致性快照。三件写操作：

1. **载入/更正**：读 projection（在写事务内）→ 写不可变版本 → 重投影 → 写计划。
2. **结构升级**：单个写事务内 projection+1，重投影所有最新版本，重写计划。
   与之并发的载入要么先提交（随后被升级重投影），要么后提交（自己读到新
   projection）。
3. **结果发布**：写事务内重算当前数据集摘要，与计算时观察到的摘要比较；
   不相等则 `StaleResult`，不写缓存、不动 head。缓存按
   `(query_key, dataset_digest)` 追加，`result_head` 指向权威条目。

因此"旧展开结果不能覆盖新粒度"是强制的：旧结果对应的摘要已经不再是当前摘要。
`store.audit()` 检查每个订单的计划 version/projection/shape 与桥表行数、边数是否
一致，并区分"过时 head"（正常，等待重发）与"孤儿 head"（不一致）。

## 8. 中断恢复

- 每篇文档 + 检查点一个事务；检查点含 `(doc_index, order_key, version, fingerprint)`。
- 中断（`KeyboardInterrupt` / 进程崩溃）只留下已提交前缀；运行标为 `aborted`。
- 续跑必须提供同一文档流；逐篇比对 order_key 与内容指纹，不一致直接
  `VersionConflict`，防止"用改过的数据续跑"。
- 已提交前缀在新运行里重录检查点（使新运行自身可再续跑），然后继续。
- 测试 `test_interrupted_load_resumes_exactly_once` 比较逐订单
  `(version, plan_hash)` 与桥表行数，证明与不中断一致。

## 9. 空数组与真实重复

| 样例 | 现象 |
|---|---|
| ORD-1002 | 有商品，无优惠、无履约 → 商品粒度运费分摊 0，订单粒度完整 |
| ORD-1004 | 无商品，有优惠、有履约 → 商品粒度无行，优惠/履约粒度各 1 行 |
| ORD-1008 | 三条全空 → 只在订单粒度出现，边数 1（哨兵） |
| ORD-1001 | `WELCOME` 两条（1000+500）→ 优惠粒度 `pos_count=2` |
| ORD-1005 | `SAVE10` 两条（各 1000）→ 同上；无履约 → 运费分摊 0 |
| ORD-1007 | 同一商品拆两履约（500+300）→ 履约粒度 2 行，运费合计 800 |

## 10. 边界与取舍

- 金额一律整数分；v1/v2 的字符串金额、v2 的百分比优惠在规范化时换算，
  换算规则集中在 `normalize.py`，可单测。
- 分摊平局按"商品原始位置序号"稳定决胜，保证同输入同输出；`largest_remainder`
  有单测（含 `Σ == 总额`）。
- 过滤只筛选输出行；分摊始终按整单计算，所以过滤后小计可能不等于订单总额，
  这是口径正确的表现，前端与结果里都明确提示。
- 订单头金额与商品原价之和不强制相等（真实数据可能含运费/手工改价）；
  工具只保证**同一口径内**的加总与分摊自洽，并在对照里同时展示两者。
