# 07 · Phase 3 调研：审批疲劳、LLM_CHECK、动态凭证

> **范围**：G18（审批疲劳缓解）、G6（LLM_CHECK 模糊操作二次审查）、G12（动态/短时凭证）。

---

## 7.1 G18：审批疲劳缓解（Risk-tiered HITL）

### 7.1.1 业界主流模型（多源交叉验证）

**Facio 决策框架**（2026-05）—— 最实用的 4 层模型：

| Tier | 标签 | 触发条件 | 路由规则 | 典型工作量 |
|------|------|----------|----------|-----------|
| L0 | Auto | 只读 / 内部摘要 / 分类去重 | 立即执行 + 审计 | 0 人时 |
| L1 | Soft approval | 草稿 / 错字修复 / 低风险跟进 | confidence ≥ 0.85 自动批准；< 自动审批阈值则人工审批 | 10-20 项/日 |
| L2 | Hard approval | 花钱 / 权限变更 / 公开消息 | 总是要人 + 一键 UI | 20-40 项/日 |
| L3 | Dual approval | 政策回应 / 合规承诺 / 银行变更 | 双独立审批 + 强制冷却期 | 1-5 项/周 |

**4 个判别条件**（同源）：
1. 30 秒内可恢复吗？
2. 影响是否传播到系统外（公开消息 / 计费 / 公网端点）？
3. 是否触及政策、合同、合规义务？
4. 回滚是否给用户带来成本？

任一为 yes → 不允许 auto-execute（不论 confidence 多高）。

**实证数据**（Facio 案例）：
- 引入基于 confidence 的 L1 层后，日审批数从 ~300 降到 ~80，3 个月稳定到 ~40。审批不是更细致——而是过滤掉了不需要人判断的项。

**风险评分函数**（Cordum / Facio / Unimon 三方一致）：
```
Risk Score = f(reversibility, dollar_exposure, customer_impact, access_scope)
```
任一维度高分都需要人审，独立于 confidence。

### 7.1.2 与本项目的契合度

我们当前是"全审批"模式（`policy.approval.required_for_trade=true` 时每笔写操作都要人审）——这是教科书级 L2，但少了 L0/L1 加速通道。

**真正契合 Phase 3 的改进**：
1. **read_market / read_account 已经是 L0**（auto-approved，policy_engine 已实现）。
2. **L1 缺失**：低 notional + 在 allowed_symbols + 历史声誉高的 place_order 应自动放行。我们已经有 `passport.reputation_score`（0-100）但没用到自动审批阈值。
3. **L3 dual approval 缺失**：没有"两个独立审批人"机制。

### 7.1.3 推荐最小实施

新增 `policy.approval.auto_approval_thresholds`（可选）字段：

```json
"approval": {
  "required_for_trade": true,
  "required_for_policy_change": true,
  "expires_after_seconds": 300,
  "auto_approval_thresholds": {
    "max_notional_usdt": 5,        // 单笔 ≤ $5 自动放行
    "min_reputation_score": 80,    // 声誉 ≥ 80 才适用
    "allowed_action_types": ["place_order"],
    "max_per_day": 20              // 每日最多 20 次自动审批
  }
}
```

Policy engine 在判定 `REQUIRE_APPROVAL` 之前先看是否符合 `auto_approval_thresholds`，符合则降级为 `ALLOW`（AUTO_APPROVED），写 `APPROVAL_AUTO_APPROVED` 审计事件。

**默认关闭**：`auto_approval_thresholds=null` 时走原有"全审批"路径。这是**严格向后兼容**的字段添加。

**ccmanager 启示**（更高级方案，长期）：用一个独立 LLM 实例（上下文未污染）做"是否需要人审"的二次判断——但对金融交易场景，建议保守先做规则版。

---

## 7.2 G6：LLM_CHECK 模糊操作二次审查

### 7.2.1 业界做法（FinHarness / FormalJudge / SpartanGuard）

**FinHarness**（arXiv 2605.27333）—— 当前 SOTA 金融 agent 安全 harness：
- 3 组件：Query Monitor + Tool Monitor + Cascade（cheap/advanced LLM judge 路由）。
- 滑动窗口风险评分 + 自适应路由：低风险走廉价 judge，高风险走高级 judge。
- 关键数据：ASR 从 38.3% 降到 15.0%，advanced judge 调用量减少 4.7×。

**FormalJudge**（arXiv 2602.11136）—— 形式化验证升级：
- LLM 不直接出"安全/不安全"判定，而是输出 atomic facts → Dafny/Z3 形式化验证。
- 解决"LLM judge 的 probabilistic echo chamber"问题。

**SpartanGuard / 多 agent debate**：
- Verifier vs Skeptic vs Judge 三方辩论，对每个 atomic claim 投票。
- 信息不对称：Verifier 看正向证据、Skeptic 看反例、Judge 不看任何 confidence。

### 7.2.2 与本项目的契合度

**我们已经有部分等价机制**：
- ActionPlan schema + Policy Engine 是"deterministic verifier"——不需要 LLM_CHECK 也能拦下绝大多数风险。
- Stale price recheck（G16 已实施）是一种 ex-ante 二次审查。

**真正缺失**：模糊场景下的"二次意见"——比如 LLM 规划了一个"看起来合理但 amount 临近上限 + 时机异常"的 action。

### 7.2.3 推荐最小实施（仅在 G16 / G18 验证后做）

**单 LLM 二次审查（最便宜方案）**：

```python
# app/services/planner_audit.py
async def llm_secondary_review(action_plan, policy, market) -> tuple[bool, str]:
    """让 cheap-tier LLM (haiku/4o-mini) 做"理智回顾"。

    仅在以下情形触发：
    - max_notional_usdt > 50% of policy.limits.max_notional_usdt_per_order
    - 当日订单数 ≥ 80% 限额
    - market_snapshot 价格相对种子数据偏离 > 30%
    - amount 突变（与该 passport 历史 90% 分位差 > 3 倍）

    返回 (approved, reason)；False 时把 action 推到 APPROVAL_REQUIRED 强制人审。
    """
    ...
```

集成位置：`policy_engine.evaluate_policy` 决定 ALLOW 之后但写审计前。

**为什么不立刻做**：当前 L1-L5 测试 100% 通过；LLM_CHECK 边际价值在"模糊但合规"的场景，对 hackathon 演示路径帮助不大；接真实交易时再做。

**避免双 LLM 辩论的理由**：成本翻倍 + 延迟翻倍；我们的场景"裁决依据"已经在 policy_json 里规则化，多 agent 辩论的优势主要在缺规则的"开放语义"场景。

---

## 7.3 G12：动态/短时凭证（HashiCorp Vault Dynamic Secrets）

### 7.3.1 业界主流做法

**HashiCorp Vault Dynamic Secrets Engine**（首选生产方案）：
- 应用请求时即时生成 DB 凭证（每请求或每会话独立 username/password）。
- TTL 短（5 分钟-1 小时）；过期自动 revoke。
- 每凭证带 lease_id，可主动 revoke。
- 全部操作进 Vault audit log（凭证请求次数 / 持有人 / 用途完整可追溯）。
- HashiCorp 自家 LangChain PoC：每聊天会话 5 分钟独立凭证，零硬编码。

**HCP Vault + OAuth2 OBO 模式**（HashiCorp 推荐 AI agent 模式 2025-09）：
- 用户 JWT → AI agent OBO token → Vault JWT auth → 短时 DB credential。
- 端到端 traceability：每个数据库查询能反查到具体用户 + 会话。

### 7.3.2 与 HTX Agent Passport 场景的契合度评估

**关键差异**：HTX API key 不是数据库凭证。HTX 不支持"动态生成短时 access_key"——这是交易所平台限制，与 Vault 能动态生成 PostgreSQL 用户的本质区别。

**我们能做的"短时化"**：
1. **凭证使用次数限额**：在 `api_credentials` 表加 `max_uses_per_day` / `current_uses_today`；超限自动转 INVALID 状态。
2. **凭证最大有效期**：加 `expires_at` 字段；过期自动转 INVALID。这迫使用户主动重新授权（间接达到"短时"语义）。
3. **per-action 凭证审计**：每次 HTX 调用记录 credential_id + action_id + caller，等价 Vault 的 audit log。

**真正动态的部分**——passport-action 间的能力授权：
- **Action-level capability token**（短时 JWT）：approve_action 时签发一枚仅含本 action_id + capability + 5min TTL 的 token，execution_gateway 凭 token 调用，过期 / mismatch 即拒。
- 这是 OAuth 2.0 token exchange 的应用，与 HashiCorp OBO 模式同源思路。

### 7.3.3 推荐最小实施

**Phase 3a：凭证使用限额**（1 天工作量）：
```python
# app/models/api_credential.py 新字段
max_uses_per_day: Mapped[int | None]      # null = 无限
current_uses_today: Mapped[int]           # 计数器，UTC 日重置
last_use_at: Mapped[datetime | None]
expires_at: Mapped[datetime | None]       # null = 永久
```

`htx_adapter._sign_request` 之前查 `current_uses_today < max_uses_per_day` + `expires_at > now`，不通过抛 `HTX_AUTH_FAILED`。

**Phase 3b：Action-level capability token**（2 天）：
```python
# 审批通过时签发短时 token（HMAC 或 Ed25519）
token = sign_capability({
  "action_id": action.id,
  "passport_id": passport.id,
  "exp": now + 5min,
  "scopes": ["place_order:btcusdt"],
})

# execution_gateway.execute() 验证 token：
verify_capability(token, action_id, scopes=["place_order"])
```

**Phase 3c（生产）：Vault dynamic secrets for DB**（可选）：
- 把 PostgreSQL `DATABASE_URL` 改为 Vault 动态生成。
- 每 backend instance 独立短时凭证。
- 主要价值：DB 层零信任，不是 HTX 层。

### 7.3.4 与 KMS envelope encryption 的关系

我们已实施 G7/G8 envelope encryption（KEK provider 抽象）。Phase 3 的动态凭证是"凭证生命周期"维度，与 envelope encryption 的"密钥管理"维度互补：
- envelope encryption = 静态加密的密钥怎么存（KEK + DEK）。
- dynamic credentials = 用密钥时怎么减少暴露（短时 token + 用量限额）。

两者结合形成完整 zero-trust credential surface。

---

## 7.4 推荐 Phase 3 实施顺序

按 ROI 排序：

1. ✅ **G18 auto_approval_thresholds**（1-2 天）：新增 policy 字段 + Engine 判定 + 审计事件。**默认关闭**严格向后兼容。
2. ✅ **G12 凭证使用限额**（1 天）：`api_credentials` 加 4 个字段 + adapter 校验。
3. ⏳ **G12 capability token**（2 天）：审批签发 + 执行验证。可选；与 G18 互补。
4. ⏳ **G6 LLM_CHECK**（1 周）：仅模糊场景触发；与 cheap-tier B.AI 同套部署。**接真实交易后再做**。
5. ⏳ **G12 Vault dynamic DB secrets**（生产化阶段）：要 Vault server + auth 集成。

**G18 + G12 凭证限额可在 hackathon 范围内立即落地**；其余建议生产化阶段。

---

## 7.5 信息源

- Facio: When AI Agents Should Ask for Human Approval: A Decision Framework
- Encyclopedia of Agentic Coding Patterns: Approval Fatigue
- Cordum: AI Agent Approval Gates: Step-by-Step Guide
- Unimon: AI Agent Permission Design (Least Privilege) - SP 800-53 AC-6 mapped
- omnithium: Human-in-the-Loop Patterns for High-Stakes AI Agent Decisions
- ccmanager Auto Approval（zenn.dev/kbwok/articles/d9d1b14a0dc55a）
- FinHarness: An Inline Lifecycle Safety Harness for Finance LLM Agents（arXiv 2605.27333）
- FormalJudge（arXiv 2602.11136）
- SpartanGuard（github.com/shashidharbabu/guardrails-enterprise）
- AgentGuard（github.com/hidearmoon/agentshield）
- HashiCorp Vault Dynamic Secrets - Database Credentials（developer.hashicorp.com/vault/tutorials/db-credentials/database-secrets）
- HashiCorp: Secure AI identity with Vault（hashicorp.com/blog/secure-ai-identity-with-hashicorp-vault）
- HashiCorp: Validated AI agent authentication using Vault dynamic secrets（developer.hashicorp.com/validated-patterns/vault/ai-agent-identity-with-hashicorp-vault）
- AI Agent Traps: approval fatigue（Franklin et al., Google DeepMind 2025）
- Goddard et al. 2012: automation bias in clinical decision support

> 所有引用内容均已改写或摘要以符合授权许可要求；单一来源连续引用不超过 30 词。
