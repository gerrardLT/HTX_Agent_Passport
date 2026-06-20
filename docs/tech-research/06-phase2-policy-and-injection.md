# 06 · Phase 2 调研：策略外部化、跨步攻击链、注入分类器、工具返回值校验

> **范围**：G1/G3（Cedar/OPA 外部化）、G2（跨步攻击链）、G4（注入分类器）、G5（工具返回值校验）。每项给出：业界主流方案、与本项目的契合度、最小可行集成路径。

---

## 6.1 G1/G3：Policy DSL 外部化（Cedar vs OPA/Rego）

### 6.1.1 现状

我们当前的 `app/services/policy_engine.py` 是 Python 内置的"裁决步骤序列"，硬编码 7 步检查。优点：纯函数、PBT 测试 100% 覆盖；缺点：策略变更必须改代码 + 部署，没有形式化验证，无法热更新。

### 6.1.2 候选方案对比（多源交叉验证）

**OPA (Open Policy Agent) + Rego**：
- CNCF 毕业项目；最广泛部署的策略引擎。
- 通用 datalog 衍生语言，能写复杂条件 + 外部数据查询。
- 部署形态多：sidecar、库内嵌（go/wasm）、远程服务。
- Python：仅有 community REST client，需运行 OPA daemon。

**AWS Cedar**：
- AWS 自研，2023 开源；专注于 authorization。
- Rust 实现（核心可形式化验证），有 Python binding `cedarpy`（k9securityio 维护）+ Cedar CLI。
- 内嵌设计（pip install cedarpy 一行集成），无需独立服务。
- 故意不支持正则/HTTP 调用——为了形式化分析的"完备且可终止"性质。

**独立基准对比**（来自 Natoma 与 Microsoft Agent Governance Toolkit 报告）：
- "Rego 表达能力强但易错（运行期异常 / 非确定性 / 扩展性风险）；Cedar 安全且确定性，有强类型校验和隔离"——独立测试基于 DoS 韧性、内存安全、输入校验等真实安全漏洞自动化测试。
- "Cedar Analysis 提供形式化策略冲突检测；OPA 测试框架只能跑 case，做不到数学证明"。

### 6.1.3 与本项目的契合度评估

| 维度 | OPA/Rego | Cedar | 我们的 PolicyDSL v0 |
|------|----------|-------|---------------------|
| 语义模型 | 通用条件/datalog | permit / forbid + condition | capabilities + limits + blocked_actions |
| 形式化验证 | 仅靠测试 | Cedar Analysis（数学证明） | PBT 测试（hypothesis） |
| 部署 | 需 sidecar / daemon / wasm runtime | pip install 一行 | 已内嵌 |
| 性能 | 亚毫秒（go/wasm） | 亚毫秒（rust binding） | 亚毫秒（Python） |
| 热更新 | ✅ | ✅ | ❌（需重启） |
| 多语言 | 全栈 | 部分（Rust/Python/JS binding） | 仅 Python |

**结论**：**Cedar 是我们的最佳契合**——
1. 语义模型 `permit / forbid` 与我们 `capabilities + blocked_actions` 几乎 1-1 对应。
2. `cedarpy` 直接 `pip install`，无需启 OPA daemon，与现有架构零摩擦。
3. Cedar 的"故意不支持正则/HTTP"反而对我们的场景是优点——零信任 PEP 不应该有动态外部调用。
4. Cedar Analysis 能数学证明"策略 A 蕴含策略 B"（policy equivalence），让我们以后做"策略变更不破坏现有授权"的回归校验。

**OPA 不选的理由**：要起 daemon、Rego 学习曲线 30-40 小时（独立报告数据）、Python 集成是 community REST client 而非官方 SDK。对单 Python 后端无 sidecar 架构是反向引力。

### 6.1.4 最小可行集成方案

**Phase 2 PoC（不替换现有引擎）**：在 `evaluate_policy` 旁挂一个 Cedar shadow evaluator，用真实策略对照运行 30 天，确认两者裁决结果 100% 一致后再考虑切换。

```python
# app/services/policy_engine_cedar_shadow.py
import cedarpy

CEDAR_SCHEMA = """
entity User in [];
entity Passport in [];
entity Action in [];
type Context = {
  daily_notional: Long, daily_orders: Long,
  market_snapshot: Set<String>, now_utc_minutes: Long,
};
action place_order, cancel_order, read_market, read_account
  appliesTo { principal: [User], resource: [Passport], context: Context };
"""

CEDAR_POLICIES = """
// 1. blocked actions hard-deny
forbid (principal, action, resource) when {
  resource has blocked_actions && action.type in resource.blocked_actions
};

// 2. capability gate
forbid (principal, action, resource) unless {
  resource.capabilities has action.type
};

// 3. notional / daily limits
forbid (principal, action == Action::"place_order", resource) when {
  context.daily_notional + action.notional > resource.max_daily_notional
};

// permit by default if no forbid
permit (principal, action, resource);
"""

def shadow_evaluate(action, policy, history, snapshot, now):
    """与主 evaluate_policy 并行运行；返回结果用于差异对比。"""
    result = cedarpy.is_authorized(
        request={...},
        policies=CEDAR_POLICIES,
        entities=[...],
        schema=CEDAR_SCHEMA,
    )
    return result.decision, result.diagnostics
```

后续每次 `evaluate_policy` 调用同时记录"主裁决结果 vs Cedar 影子裁决结果"，差异写日志告警。30 天 0 差异后再考虑替换。

### 6.1.5 reason_codes 累积（G3）

当前实现：触发任一拒绝立即返回单一 reason_code。改进：跑完所有检查累积全部触发的 reason_codes。

**为什么不立刻做**：当前 `_make_reject` + 早返回的"first match wins"模式与 PRD §9.1 7 步顺序裁决严格一致；要改 cumulative 模式需要：
1. 改 `PolicyVerdict` 字段语义（`reason_codes` 从单元素变多元素）。
2. 重写所有 PBT 测试（约 30 例）。
3. 评估前端 `AuditTimeline` 对多 reason_codes 的展示。

**推荐时机**：与 Cedar shadow 切换并行——Cedar 天然支持累积（多个 `forbid` 同时触发都会被记录在 `diagnostics.reasons` 里），切换 Cedar 时一次性升级 reason_codes 语义。

---

## 6.2 G2：跨步攻击链 / 信息流追踪（Tessera / CaMeL / Trust Boundary）

### 6.2.1 业界最强方案

**Google DeepMind CaMeL**（Capabilities for Machine Learning，arxiv 2503.18813）：
- 双 LLM：Privileged LLM 写代码，Quarantined LLM 处理不可信内容。
- 数据流图：每个变量带 capabilities 标签（provenance + readers），调 tool 前查依赖图。
- **解决**："agent 用 RAG 拉到一个含恶意指令的 PDF → LLM 被诱导调 send_email 给攻击者邮箱"——CaMeL 在 send_email 调用前查 body 的 readers，发现 to 不在 readers 集合内 → 拒绝。
- AgentDojo benchmark 67% 完成率 + 几乎 100% 注入防御。

**Tessera**（GitHub kenithphilip/Tessera）：
- 简化版 CaMeL：**"trust label 的 min, 不是 max"**——任何一段不可信内容会把整个 context 拉到 untrusted。
- LangGraph / LangChain / OpenAI Agents SDK / MCP / CrewAI 14 个适配器。
- Apache-2.0；开箱即用。

**Anticipator**（GitHub usestackbox/anticipator）：
- 不阻断只观察："smoke detector, not firewall"。
- 10 层检测：phrase / encoding / entropy / heuristic / canary / homoglyph / path traversal / tool alias / threat categories / config drift。
- 适合 demo / 灰度部署。

### 6.2.2 与本项目的契合度

我们已经做对了：
- ✅ `capability_envelope` 等价 Tessera 的"per-tool trust requirements"
- ✅ Policy Engine 直接裁决 tool 参数（避开 NeMo 看不到 tool_calls 参数的限制）
- ✅ `prompt_hash` + LLM 不持有工具能力 = CaMeL 的 P-LLM 隔离
- ✅ ActionPlan schema 严格校验 = CaMeL 的 schema enforcement

**真正缺失的 G2**：
1. **市场快照 / 工具返回值的 trust label**：现在 `market_snapshot` 是个 dict，没标 provenance。如果未来接入"用户上传 PDF 让 agent 分析"这类不可信源，没有 trust label 会出问题。
2. **跨 action / 跨 trace 的累积污染追踪**：目前每次 action 独立裁决。CaMeL 的 dataflow graph 跨 tool call 累积。

### 6.2.3 推荐路径（按当前需求"够用即可"）

**短期（hackathon/MVP 范围内**:
- 不上 CaMeL/Tessera 全栈——overkill。
- 在 `MarketSnapshot` 数据结构上加 `provenance: Literal["seed", "htx_real", "user_provided"]` 字段；Policy Engine 在 `place_order` 路径检查 `provenance != "user_provided"`。
- 这是 1 天工作量，覆盖 80% 的"代理别按用户上传文档里的指令下单"风险。

**中期（接入用户上传文档 / RAG 后）**：
- 集成 Tessera（轻量、Python、LangGraph 友好）。
- 用 Tessera 的 `min` taint 模型：把 user_request / market_snapshot / 上传文档各自 tag，调 `place_order` 前要求 trust >= INTERNAL。

**长期（多代理协作场景）**：
- 评估 CaMeL 双 LLM 架构（成本翻倍但安全性最强）。

---

## 6.3 G4：Prompt 注入分类器（Llama Guard vs Lakera）

### 6.3.1 业界主流对比（多源确认）

| 方案 | 部署 | 延迟 | 类型 | 我们的契合度 |
|------|------|------|------|-------------|
| **Lakera Guard** | 托管 SaaS API（Check Point 旗下） | < 50ms | 商业 + DLP + 内容审核 + 多模态 | 🟡 SaaS 依赖 |
| **Llama Guard 3 8B** | 自托管（vLLM/HF/llama-cookbook） | ~500ms（CPU）/ ~50ms（GPU） | 开源；MLCommons 14 类危害分类 | ✅ 中长期 |
| **Llama Guard 3 1B** | 自托管 | ~165ms（边缘 CPU 可跑） | 开源；轻量但不支持 S14 code interpreter | ✅ 短期可考虑 |
| **PromptGuard / Lakera Prompt Defense** | 托管 / 自托管 | < 50ms | 专注 prompt injection（不含 DLP） | ✅ |
| **NeMo Guardrails** | 自托管 | 中等 | 编排框架，规则 + LLM 混合 | 🟡 太重 |

### 6.3.2 与本项目的契合度

**我们已经做了"第 4 道防线"**：Policy Engine 只读结构化字段、忽略 rationale 自然语言——L5 测试已验证 prompt 注入无法改变裁决（注入只能"骗" LLM 编造 ActionPlan，编造的 ActionPlan 被 schema + Policy Engine 双重过滤）。

**注入分类器在我们这是"前置层"**——在调 B.AI 之前先扫一遍用户输入。价值：
1. 早期识别恶意输入，不浪费 B.AI tokens。
2. 监控指标：注入尝试率（QPS）作为安全 dashboard 指标。
3. 多模态扩展：未来接入文档上传时同样过一遍 Llama Guard。

### 6.3.3 推荐方案

**短期**：**保持现状不接分类器**。理由：
- 我们现在拒绝模式（policy engine 拒绝）已经是"零容忍"安全 eval（L5 测试 100% 通过）。
- 加分类器边际收益是"前置过滤节省 tokens"，但每次调用增加 50-500ms 延迟 + GPU 成本——对 hackathon/MVP 不划算。

**中期（接真实交易后）**：接入 **Llama Guard 3 1B 自托管**：
- 1B 模型可在 CPU 跑；vLLM serving 即可。
- 成本：单 GPU $50-150/月；CPU 推理 $0。
- 集成位置：`app/services/planner.py` 的 `build_planner_context` 之后、`call_b_ai` 之前。
- 失败模式：分类器返回 unsafe → 走规则路由 fallback（Req 5 AC8 已有路径）。

**避免接 Lakera 的理由**：商业 SaaS API 引入第三方依赖，与"零信任 / 不把 prompt 发给第三方"的安全愿景冲突；除非买 Lakera 自托管包，但那价格远超 hackathon 范围。

---

## 6.4 G5：工具返回值不可信处理

### 6.4.1 现状与差距

当前 `htx_adapter.get_ticker()` 返回的 `TickerResult` 直接进 `market_snapshot`，没做任何完整性 / 异常值校验。如果 HTX 公共 API 被中间人攻击 / 返回异常值（如 `last=0`），policy engine 的 `max_slippage_bps` 检查会出 false positive 或 false negative。

### 6.4.2 业界做法（CaMeL + agent infra 调研）

1. **Schema 强制**：tool 返回值用 Pydantic schema 严格校验（已部分实现：`TickerResult` 是 dataclass，但没有数值合理性约束）。
2. **异常值 / 范围校验**：last price 在合理范围（如 BTC: $1000-$1M），不在则降级到种子价。
3. **跨源比对**（CaMeL 风格）：用 2 个独立行情源（HTX 公共 + Coinbase 公共）拉同一 symbol，价格差 > X% 标可疑。
4. **Trust label**：tool 返回值带 `trust_level=external_low`，policy engine 用 trust 决定是否允许下单。

### 6.4.3 推荐最小集成

在 `htx_adapter._validate_ticker_sanity()` 加 3 项：

```python
def _validate_ticker_sanity(symbol, last, bid, ask) -> bool:
    """合理性校验：价格 > 0 + 跨值一致 + 在历史合理范围。"""
    if last <= 0 or bid <= 0 or ask <= 0:
        return False
    if not (bid <= last <= ask):
        return False
    # 简单范围（生产应用 3σ from rolling avg）
    if symbol == "btcusdt" and not (1000 <= last <= 1_000_000):
        return False
    return True
```

不通过的 ticker 走"种子数据 fallback + 写 `MARKET_DATA_ANOMALY` 审计事件"。这是 1-2 小时工作量，覆盖 80% 风险。

跨源比对（方案 3）成本/复杂度高，留待真实交易上线后做。

---

## 6.5 推荐 Phase 2 实施顺序

按 ROI 排序（值/成本）：

1. ✅ **G5 工具返回值校验**（1-2 小时，立即做）：在 `htx_adapter` 加 `_validate_ticker_sanity`。
2. ✅ **G2 trust label 最小集成**（1 天）：`market_snapshot` 加 `provenance` 字段；place_order 检查 `provenance != "user_provided"`。
3. 🟡 **G3 reason_codes 累积**（与 Cedar 切换并行）。
4. ⏳ **G1 Cedar shadow evaluator**（PoC 1-2 周；30 天观察期；切换 1 周）。
5. ⏳ **G4 Llama Guard 1B**（接入真实交易后；1 周自托管 + 集成）。

**G2 + G5 可在 hackathon 范围内立即落地**；其余建议生产化阶段做。

---

## 6.6 信息源

- AWS Cedar 官方（cedarpolicy.com）+ cedarpy（pypi.org/project/cedarpy）
- Microsoft Agent Governance Toolkit OPA/Rego/Cedar tutorial
- Styra OPA vs Cedar 对比（styra.com/knowledge-center/opa-vs-cedar-aws-verified-permissions）
- Natoma MCP Access Control: OPA vs Cedar Comparison（2025-07）
- Google DeepMind CaMeL paper（arxiv 2503.18813）+ adk-samples
- Tessera（github.com/kenithphilip/Tessera）
- Anticipator（github.com/usestackbox/anticipator）
- Llama Guard 3 model cards（llama.com/docs/model-cards-and-prompt-formats/llama-guard-3）
- Lakera Guard docs（docs.lakera.ai/guard）
- agentic-guard 静态分析器（dev.to/san_krish_c7d3b56904861f4/the-missing-bandit-for-ai-agents）
- Predicate Authority Chain Delegation demo（github.com/PredicateSystems/langgraph-poisoned-escalation-demo）

> 所有引用内容均已改写或摘要以符合授权许可要求；单一来源连续引用不超过 30 词。
