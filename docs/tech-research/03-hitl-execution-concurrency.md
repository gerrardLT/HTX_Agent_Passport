# 03 · 人机审批、执行并发安全与 Crypto Agent 对标

> 对标：LangGraph（interrupt/checkpointer）、OWASP Business Logic / Transaction Authorization、Anthropic 审批疲劳研究、Coinbase/Thirdweb session keys

## 3.1 人机审批（HITL）：vs LangGraph

### 我们的方案
`create_approval_request`（设 expires_at）+ `submit_approval`（双重审批防护、过期检查、撤销检查、policy 版本变化重裁决）+ `scan_expired_approvals`（每 30s 主动过期）。前端 ApprovalModal + typed_confirmation="APPROVE"。

### LangGraph 的 HITL 模式（业界事实标准）

LangGraph 用 `interrupt()` + `Command(resume=...)` + checkpointer 实现 HITL。对比我们：

| 维度 | LangGraph | 我们 | 评价 |
|------|-----------|------|------|
| 暂停机制 | `interrupt()` 抛异常，checkpointer 持久化全状态 | action 状态机转 APPROVAL_REQUIRED，状态存 DB | ✅ 等价（我们用状态机+DB，更显式） |
| 恢复 | `Command(resume=value)`，同 thread_id | submit_approval(action_id) | ✅ 等价 |
| **超时处理** | 文档建议"后台 job 扫描 stale threads，N 小时后过期" | ✅ `scan_expired_approvals` 每 30s + 惰性过期 | ✅ **我们做得好** |
| **双重审批/重复 resume** | 需自己处理 interrupt 队列 | ✅ Property 6 双重审批防护（approved IS NULL 检查） | ✅ **我们做得好** |
| **stale state 重校验** | ⚠️ 文档明确警告"HITL 不会自动用新世界状态重新校验提案，必须自己在执行节点加检查" | ✅ policy 版本变化时重裁决 + 执行网关二次裁决 | ✅ **我们做得好** |

**关键验证**：LangGraph 社区文档把"stale state on late resume"列为 HITL 头号失败模式：
> "LLM 在 09:00 基于代码库快照提议变更，人类在 17:00 批准，期间合并了 3 个 PR，提议现在冲突了。HITL 不会自动用新世界状态重新校验，你必须把这个检查显式构建进 apply_changes 节点。"
> 内容已改写以符合授权许可要求。

**我们的审批重裁决（Req 8 AC9）+ 执行网关二次裁决（Req 9 AC2）正是这个最佳实践的正确实现。** 这是我们设计的一大亮点。

### 但有一个未覆盖的隐患：审批与执行之间的 stale market data

我们重裁决时用的是**审批时刻**的 policy 版本。但 `max_notional_usdt` 是基于**规划时刻**的 limit_price 算的。如果审批延迟很久，市场价格已大变：
- 用户规划时 BTC=68000，下单 10 USDT
- 30 分钟后审批，BTC 暴涨到 90000
- 我们的重裁决检查 policy 版本（没变）→ 放行
- 但实际成交的 notional 已偏离

这与 LangGraph 的"stale state"是同一类问题。我们重裁决了 **policy** 但没重校验 **market snapshot 时效性**。

**改进建议**：审批/执行时检查 market_snapshot 的 `as_of` 时间戳，超过阈值（如 60s）则强制刷新行情并重算 notional，偏差超 `max_slippage_bps` 则要求重新审批（Req 16 AC2 已提到 max_slippage_bps 但当前未在审批延迟场景启用）。

## 3.2 审批疲劳：Anthropic 的关键数据

我们当前是"每个写操作都要审批"。Anthropic 的生产遥测给出了警示：

> "我们的遥测显示用户批准了大约 93% 的权限提示。用户看到的批准越多，对每个的注意力越少，监督随时间变得不那么尽职……一个原本为提供监督而设计的功能，可能反而起相反作用。"
> —— Anthropic《How we contain Claude》。内容已改写以符合授权许可要求。

Anthropic 的应对：
1. **Plan Mode**：不逐步批准，而是把**整个计划**一次性展示给用户审查/编辑/批准，把监督粒度从"单步"提升到"整体策略"。
2. **OS-level sandbox**：用容器边界减少 84% 的权限提示。
3. **工具风险分级**：read-only 默认放行，只对修改操作要批准。

### 对我们的启示

我们的 ActionPlan 支持 1-3 个 actions，但审批是**逐 action**的吗？我们其实是逐 action 裁决 + 整 plan 审批，方向对。但可以进一步：

| Anthropic 做法 | 我们可借鉴 |
|---------------|-----------|
| Plan Mode（整体计划审批） | ✅ 已部分实现（ApprovalModal 展示整个 ActionPlan）；可强化"一次审批整个多步计划" |
| 工具风险分级自动审批 | 🟡 我们 read_market/read_account 已 AUTO_APPROVED，但所有 place_order 都要审批——可对"远低于限额的小额单"（如 < 10% max_notional）配置自动审批阈值 |
| 监督粒度匹配用户能力 | ➖ 未做用户分级 |

**注意权衡**：加自动审批阈值会降低安全性。对金融操作，"审批疲劳"和"零容忍"是矛盾的。建议：**保持默认全审批**，但提供 passport 级可配置的"信任阈值"（用户自己决定，默认关闭）。

## 3.3 🔴 TOCTOU 竞态条件：我们最隐蔽的生产级 bug

这是调研中发现的**最具体、最可被利用**的技术缺陷。

### 我们的代码现状

Policy Engine 的日限额检查（design.md / policy_engine.py）：
```
# Step 5: max_daily_notional_usdt
if daily_history.total_notional_today_utc + action.max_notional_usdt > limit:
    return REJECT(DAILY_LIMIT_EXCEEDED)
# Step 6: max_orders_per_day
if daily_history.order_count_today_utc >= limit:
    return REJECT(DAILY_ORDER_COUNT_EXCEEDED)
```

`daily_history` 是调用方**先查询聚合**得到的快照，然后判断，然后（执行后）才写入新 action。这是典型的 **读-判断-写（check-then-act）非原子操作**。

### 攻击/故障场景（OWASP Business Logic Top 10 / CWE-367）

来源：OWASP Business Logic Security Cheat Sheet、vibe-eval《Race conditions in money paths》。内容已改写以符合授权许可要求。

```
passport 日限额 = 100 USDT，已用 0
并发提交 20 个 "买入 10 USDT" 的 action：
- Request A: 查 daily_total=0, 0+10<100 OK
- Request B: 查 daily_total=0, 0+10<100 OK   (并发，读到同样的旧值)
- ... 20 个全部通过检查
- 20 个全部执行 → 实际成交 200 USDT，击穿 100 限额
```

> "一个常见的合理化借口是'check 和 update 之间的窗口只有微秒，没有攻击者能利用'。并发请求工具可以轻易让几十个请求在 1 毫秒内到达。现代漏洞赏金工具用 HTTP/2 单次多路复用让它们几乎同时落地。窗口只要存在，就假设它可被利用。"
> 内容已改写以符合授权许可要求。

### 我们当前为什么"看起来没问题"
- Demo/单用户串行操作时不会触发。
- 我们的 PBT 测试 Policy Engine 是**纯函数**（给定 daily_history 确定输出），**没有测并发**——因为竞态不在纯函数内部，而在"聚合 daily_history → 裁决 → 写入"这个**编排层**。
- 审批二次裁决也用 `DailyActionHistory()`（注释写"简化：重裁决时不重新聚合日限额"）——意味着**执行网关根本没在执行时聚合真实日累计**，竞态窗口更大。

### OWASP 给出的标准修复

| 操作形态 | 安全模式 |
|---------|---------|
| 单行 read-modify-write | `SELECT ... FOR UPDATE` + UPDATE，事务内 |
| 计数器条件递减 | `UPDATE ... SET v=v-1 WHERE v>0`，检查 affected rows |
| 每用户限额 | 数据库唯一约束 / 原子计数 |
| 外部非幂等调用 | 幂等键表 + 事务写结果 |
| 跨行一致性 | SERIALIZABLE 事务 + 重试 |

### 改进建议（🔴 高优先级）

1. **执行网关执行前**：在一个 DB 事务内，用 `SELECT ... FOR UPDATE` 锁住该 passport 的当日累计行（或一个 passport 级的 lock row），重新聚合 `daily_notional` / `order_count`，重裁决，确认通过后才执行并写入。让"聚合-判断-写"原子化。
2. **修复执行网关重裁决的"简化"**：当前 `DailyActionHistory()` 传空值是个已知缺陷（代码注释自承），必须改为真实聚合。
3. **幂等键**：place_order 引入 idempotency key（action_id 即天然幂等键），防止 action 被重复执行（网络重试/并发 resume）。参考 FlowVerify 的 pending/succeeded/failed 三态 + `INSERT ON CONFLICT` + `SELECT FOR UPDATE`。
4. **并发测试**：补一个 L5 并发竞态测试——并发提交 N 个 action，断言"实际执行总额 ≤ 日限额"。OWASP 明确建议"如果两个请求能竞争，就写一个测试让它们竞争"。

## 3.4 Crypto Agent 钱包对标（spending limits / session keys）

虽然我们对接 CEX（HTX API key）而非链上钱包，但 agent wallet 领域的 spending control 设计值得借鉴：

| 能力 | Coinbase/Thirdweb/Openfort | 我们 |
|------|---------------------------|------|
| Session caps（每会话上限） | ✅ | 🟡 有 daily limit，无 session 概念 |
| Per-transaction limit | ✅ | ✅ max_notional_usdt_per_order |
| 时间窗限制 | ✅（session key 编码时间窗） | ✅ allowed_time_utc |
| 允许的合约/方法白名单 | ✅（session key 编码 method selector） | ✅ capabilities + allowed_symbols |
| KYT/合规筛查 | ✅ Coinbase 内置 | ❌ 无 |
| Kill switch | ✅ | ✅ DEMO_DISABLE_EXECUTION |
| 策略执行位置 | on-chain（合约强制）/ TEE | app 层（Policy Engine） |

**评价**：我们的 policy 维度覆盖度其实**不输**这些钱包（甚至 limits 维度更细）。主要差距是：
1. 缺合规筛查（KYT）——CEX 场景下 HTX 自己做了，可不强求。
2. 策略执行在 app 层而非密码学强制层——这与 2.2 的密钥管理差距同源。on-chain session key 的优势是"即使 app 被攻破，合约仍拒绝越权交易"；我们的 Policy Engine 若 app 层被绕过就失效。

## 3.5 本章差距小结

| # | 差距 | 严重性 | 证据来源 |
|---|------|--------|---------|
| G13 | 日限额/订单数检查存在 TOCTOU 竞态，并发可击穿限额 | 🔴 严重 | OWASP Business Logic、CWE-367、vibe-eval |
| G14 | 执行网关重裁决用空 daily_history（代码自承"简化"） | 🔴 严重 | 我们的 execution_gateway.py 注释 |
| G15 | place_order 无幂等键，并发/重试可能重复执行 | 🟠 高 | FlowVerify、Stripe idempotency |
| G16 | 审批延迟后未重校验 market snapshot 时效（stale price） | 🟠 高 | LangGraph stale-state、Req 16 AC2 未启用 |
| G17 | 缺并发竞态测试 | 🟠 高 | OWASP"races 要写测试去竞争" |
| G18 | 全操作审批，未借鉴 Plan Mode / 风险分级缓解审批疲劳 | 🟢 低 | Anthropic 93% 数据 |
| G19 | 策略执行在 app 层而非密码学强制层 | 🟡 中（CEX 场景） | Openfort on-chain session keys |
