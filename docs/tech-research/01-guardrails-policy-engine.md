# 01 · 护栏框架与策略引擎对比

> 对标：NeMo Guardrails、AgentGuard、Guardrails AI、OPA/Rego、AWS Cedar、Anthropic Zero Trust、CSA ATF

## 1.1 架构理念对比：我们做对了什么

业界 2025-2026 年已形成强共识，而我们的核心设计与之**高度一致**：

> **「授权决策必须不交给 LLM 本身。策略引擎（OPA、Cedar、Rego）提供确定性、可测试、可审计的授权规则……每个工具调用都由策略引擎评估……如果不通过，操作被阻止——无论 LLM 决定了什么。这就是 Agent 的策略执行点（PEP），它必须位于 LLM 推理循环之外。」**
> —— Cequence《Agentic Zero Trust》研究论文（2026-05）
> 内容已改写以符合授权许可要求。

我们的确定性 `Policy Engine`（`evaluate_policy` 纯函数，无副作用，PBT 验证确定性）正是这个"LLM 循环之外的 PEP"。Anthropic 的框架同样强调五大原则之一是"keeping humans in control"+ 在高风险动作前要人类批准——这与我们的 `REQUIRE_APPROVAL` 裁决一致。

**结论：架构方向正确，无需推倒重来。** 差距在工程实现层面。

## 1.2 AgentGuard —— 与我们最同构的对标对象

[AgentGuard](https://github.com/WhitzardAgent/AgentGuard)（基于属性的工具调用访问控制框架）几乎是我们 Policy Engine 的"通用化版本"。对比：

| 维度 | AgentGuard | 我们的 Policy Engine | 差距 |
|------|-----------|---------------------|------|
| 决策位置 | LLM planner 与工具之间 | LLM 与执行网关之间 | ✅ 一致 |
| 裁决动作 | `ALLOW` / `DENY` / `HUMAN_CHECK` / `LLM_CHECK` | `ALLOW` / `REJECT` / `REQUIRE_APPROVAL` | 🟡 我们缺 `LLM_CHECK`（用 LLM 二次审查模糊操作） |
| 策略表达 | 独立 DSL（可引用身份/工具元数据/参数/会话历史/调用链） | 硬编码 Python（policy_json 是数据，逻辑是代码） | 🟠 我们策略逻辑不可外部化 |
| 评估阶段 | `requested`（执行前）/ `completed`（执行后）/ `failed` | 仅执行前 + 执行网关二次裁决 | 🟠 缺执行后规则（如基于 tool.result 的后续审计） |
| **跨步攻击链** | `TRACE` 函数：可表达"读数据库→发邮件"、"读敏感文件→上传外部"等多步攻击链 | ❌ 仅单 action 裁决，无跨步链分析 | 🔴 缺失能力 |
| 属性模型 | agent（trust_level/scope）+ tool（boundary/sensitivity/integrity） | passport policy（capabilities/limits） | 🟡 我们无 trust_level 分级、无信息流标签 |
| 部署形态 | 中心化控制平面 + 分布式 agent 客户端 | 单体后端内嵌 | 🟡 多 agent 场景扩展性弱 |

**关键差距：跨步攻击链检测。** AgentGuard 能识别"外部输入最终流入 shell 命令"这类组合攻击。我们的 Policy Engine 只看当前单个 action，无法防御"先 read_market 探测→再用结果构造越权 place_order"这类多步组合（虽然我们每步都裁决，但缺少**会话级信息流追踪**）。

## 1.3 NeMo Guardrails —— 五类 rails 框架

NeMo 提出了被广泛采用的"五类 rails"分类。对照我们的覆盖情况：

| Rail 类型 | 触发点 | NeMo 能力 | 我们的覆盖 |
|-----------|--------|-----------|-----------|
| Input rails | 收到用户输入 | 校验/过滤/改写输入 | 🟡 仅规则路由关键字拦截（无注入分类器） |
| Output rails | LLM 生成输出 | 校验/过滤/改写响应 | ✅ ActionPlan schema 校验（extra='forbid'）|
| Dialog rails | 计算 canonical form 后 | Colang 控制对话流 | ➖ 不适用（我们非多轮对话） |
| Retrieval rails | RAG 检索后 | 过滤检索 chunk | ➖ 不适用（无 RAG） |
| **Execution rails** | 工具调用前/后 | **校验工具参数 + 工具输出** | 🟠 部分：我们校验 schema 但 NeMo 的 `tool_input`/`tool_output` flows 更细 |

**NeMo 官方文档承认的一个关键限制**（对我们是警示）：
> "Rails 只评估消息的 content 字段。工具调用参数在 tool_calls 字段，不在 content——input/output rails 看不到也不校验它们。工具结果以 ToolMessage 返回时不经过 input rail 校验。"
> 内容已改写以符合授权许可要求。

我们比 NeMo 在这点上**反而更好**：我们的 Policy Engine 直接对 `normalized_action`（即工具参数）做裁决，不依赖 content 字段。这是我们设计的一个隐藏优势。

但 NeMo 的 **Agentic Security 安全准则**值得我们逐条对照：
1. 隔离所有认证信息，不让 LLM 接触 — ✅ 我们做到了（凭证加密 + 不进 prompt）
2. 校验和清洗所有工具输入 — 🟡 部分（schema 校验，但未对工具**返回值**做注入扫描）
3. 对工具调用应用 execution rails — ✅ Policy Engine
4. 监控 agent 异常行为 — 🟡 有可观测性，但无行为基线告警

## 1.4 OPA / Cedar —— "策略即代码"的差距

这是我们**最值得借鉴**的方向。我们的 `evaluate_policy` 把策略逻辑硬编码在 Python 里，policy_json 只是数据。业界领先做法是把**策略本身也变成可独立编写、测试、版本化、热更新的代码/数据**。

### Cedar 的四大设计目标（我们逐项对照）

来源：AWS《Cedar: A New Language for Expressive, Fast, Safe, and Analyzable Authorization》论文。内容已改写以符合授权许可要求。

| Cedar 目标 | 含义 | 我们的现状 |
|-----------|------|-----------|
| **Expressive** | RBAC/ABAC/ReBAC 统一表达 | 🟡 仅能表达固定的 capabilities/limits，加新维度要改 Python |
| **Performant** | 策略切片，亚毫秒级 | ✅ 我们纯函数也快，但无策略切片概念 |
| **Safe** | deny by default、无副作用、求值顺序无关、类型校验 | ✅ 我们也是 deny→ask→allow，但**顺序敏感**（7 步严格顺序），且无类型 schema 校验策略本身 |
| **Analyzable** | 可归约为 SMT，形式化证明"重构策略后授权不变" | 🔴 **完全缺失**——我们无法形式化证明策略变更的等价性 |

### Cedar 的核心优势（对我们的启示）

> "Cedar 的授权器是确定性的：对给定请求、层级和策略集，保证终止并总是产生相同的授权决策。因为 Cedar 策略无副作用、无通用循环，策略求值顺序无关紧要。"
> 内容已改写以符合授权许可要求。

注意：**我们的 Policy Engine 是"求值顺序敏感"的**（Req 7 AC1 规定严格 7 步顺序，先 blocked_actions 再 capabilities...）。这意味着：
- 同一个 REJECT 可能因为顺序不同返回不同的 `reason_code`（例如一个既越权又超限的 action，我们只报第一个命中的原因）。
- 重构这 7 步顺序时，我们**没有形式化工具**能证明"授权结果不变"，只能靠 PBT 大量采样。

Cedar 用 Lean 证明助手证明了关键属性，并用 SMT 做策略分析——这是我们 PBT（采样验证）达不到的"完备性"。

### 性能数据（来自 AWS 迁移博客与 Styra 对比）
- Cedar 比 Rego 快 **42-80×**（in-process Rust 实现）。内容已改写以符合授权许可要求。
- OPA 官方文档列出"custom policy engine"（即我们这种）相比 OPA 的劣势：策略求值逻辑要自己重造、策略与应用代码耦合、无法跨语言/团队共享策略。

### 改进建议（按代价排序）

1. **低成本（推荐先做）**：把 7 步裁决重构为"规则列表 + 显式优先级"的数据驱动结构，让 reason_codes 可累积（返回**所有**命中原因而非第一个），并显式声明"求值顺序无关"或"优先级明确"。
2. **中成本**：引入 Cedar（Rust core，有 Python binding）或 OPA（REST sidecar）做 PoC，把 capabilities/limits 用 Cedar policy 表达。收益：策略热更新、形式化校验、policy 与代码解耦、CI 中 `cedar validate`。
3. **高成本（P2）**：对核心安全不变量（如"withdraw 永远 REJECT"、"超限永远 REJECT"）做 SMT 形式化证明。

## 1.5 prompt 注入防御深度对比

业界共识：**output-only 过滤不够，需要多层防御**。我们当前防御层：
1. 规则路由关键字拦截（提现/withdraw/借贷...）
2. PLANNER_SYSTEM_PROMPT 安全约束
3. ActionPlan schema `extra='forbid'`
4. Policy Engine 只读结构化字段（注入文本在 rationale 里被完全忽略）— **这是我们最强的一道防线**

我们 L5 对抗测试已验证第 4 层有效。但相比业界，我们缺：

| 防御手段 | 业界工具 | 我们 |
|---------|---------|------|
| 专用注入分类器 | Lakera Guard（<50ms）、Rebuff（自学习向量）、Llama Guard 4、GA Guard（HarmBench 0.983 F1） | ❌ 无 |
| 工具返回值注入扫描 | Anthropic 代理层 classifier 检查 tool output | ❌ 无（我们 mock 工具，但真实接入 HTX 后行情数据也可能被污染） |
| 间接注入（poisoned 数据） | NeMo retrieval rails | ➖ 当前无外部数据源 |

**关键洞察**（Anthropic《How we contain Claude》）：
> "工具输出是攻击面，即使工具本身可信。一旦被污染的工具返回值把 agent 引导去 exfiltrate 数据，日志只显示一次成功的、已授权的 API 调用。事后没有信号可查。"
> 内容已改写以符合授权许可要求。

对我们的启示：当从 mock 切到真实 HTX 行情时，**市场数据返回值也应被视为不可信输入**。我们的反幻觉校验（symbol 必须在 snapshot 内）部分缓解了这点，但价格字段本身若被污染（中间人/API 被黑），我们的 Policy Engine 会用污染价格做 max_notional 计算——这是一个未覆盖的攻击面。

## 1.6 本章差距小结

| # | 差距 | 严重性 | 证据来源 |
|---|------|--------|---------|
| G1 | Policy Engine 硬编码，非"策略即代码"，无形式化可分析性 | 🟠 高 | Cedar 论文、OPA 文档、Styra 对比 |
| G2 | 缺跨步攻击链 / 信息流追踪 | 🟠 高 | AgentGuard TRACE |
| G3 | 裁决顺序敏感，reason_codes 不累积 | 🟡 中 | Cedar"顺序无关"设计 |
| G4 | 缺专用 prompt 注入分类器 | 🟡 中 | Lakera/Rebuff/Llama Guard/GA Guard |
| G5 | 工具返回值（真实行情）未做不可信输入处理 | 🟡 中 | Anthropic 工具输出攻击面 |
| G6 | 缺 `LLM_CHECK` 式模糊操作二次审查 | 🟢 低 | AgentGuard |
