# HTX Agent Passport — 技术方案深度调研与差距分析

> 调研日期：2026-05-31
> 方法：基于 Exa 实时 Web 检索 + 官方文档交叉验证，对标 GitHub 顶级项目与行业标准。
> 目的：把我们当前的技术方案与业界领先实现做横向对比，找出**真实存在的不足**，给出可执行的改进建议。

## 文档结构

本调研因篇幅较大，拆分为多个文档：

| 文档 | 内容 |
|------|------|
| `00-index.md`（本文） | 总览、方法论、对标对象清单、核心结论 |
| `01-guardrails-policy-engine.md` | 护栏框架 + 策略引擎对比（NeMo / AgentGuard / OPA / Cedar / Anthropic Zero Trust） |
| `02-audit-and-secrets.md` | 审计哈希链（vs Merkle/Certificate Transparency）+ 密钥管理（vs Vault/KMS/TEE） |
| `03-hitl-execution-concurrency.md` | 人机审批（vs LangGraph）+ 执行并发安全（TOCTOU 竞态）+ crypto agent 钱包对标 |
| `04-gap-summary-and-roadmap.md` | 差距总表（按严重性分级）+ 分阶段改进路线图 |
| `05-production-upgrades.md`（**新**） | STH 签名升级（HMAC-SHA256 → Ed25519）+ 外部锚定（本地 JSONL → S3 Object Lock / Git / 区块链） |
| `06-phase2-policy-and-injection.md`（**新**） | Phase 2 调研：G1/G3 Cedar 外部化 + G2 跨步攻击链 + G4 注入分类器 + G5 工具返回值校验 |
| `07-phase3-approval-llmcheck-credentials.md`（**新**） | Phase 3 调研：G18 审批疲劳 + G6 LLM_CHECK + G12 动态凭证 |

## 我们的技术方案速览（被对标对象）

HTX Agent Passport 是一个**夹在 LLM 与加密交易所之间的安全控制层**，四层架构：

- **感知层**：规则路由（关键字拦截）+ 上下文构建（< 8K tokens）
- **决策层**：B.AI Planner（LLM 生成 ActionPlan）+ Pydantic Schema 校验 + mock 降级
- **执行层**：确定性 Policy Engine（硬编码 7 步裁决）+ 审批服务 + 执行网关 + HTX 适配器
- **反馈层**：审计哈希链（单链 SHA-256）+ 可观测性 + 声誉分

技术栈：Next.js 14 + FastAPI (Python 3.11) + PostgreSQL + AES-256-GCM（凭证）+ JWT。

## 对标对象清单（均为 2025-2026 年活跃项目/标准）

### 护栏 / 策略引擎
- **NVIDIA NeMo Guardrails**（Apache 2.0）— 五类 rails（input/output/dialog/retrieval/execution），Colang DSL
- **AgentGuard**（WhitzardAgent，GitHub）— 基于属性的工具调用访问控制（ABAC），与我们高度同构
- **Guardrails AI**（Apache 2.0）— 70+ 验证器 Hub，RAIL 输出 schema
- **OPA / Rego**（CNCF）+ **AWS Cedar**（Rust，开源）— 通用策略即代码引擎
- **Anthropic「Trustworthy Agents」框架 + Zero Trust for AI Agents**（2026-05）
- **Cloud Security Alliance Agentic Trust Framework (ATF)**（2026-02）

### 审计 / 可验证日志
- **Certificate Transparency (RFC 6962 / RFC 9162)** + **Google Trillian** — Merkle 树可验证日志
- **Crosby & Wallach「Efficient Data Structures for Tamper-Evident Logging」(2009)**
- **AAD（Append-Only Authenticated Dictionaries, CCS'19）**

### 密钥管理
- **HashiCorp Vault**（Transit / envelope encryption / dynamic secrets）
- **AWS KMS + Secrets Manager**（envelope encryption, FIPS 140-3 L3）
- **Coinbase Agentic Wallets / AgentKit**、**Openfort**、**Turnkey**、**Thirdweb Engine**（TEE/MPC/session keys）

### 人机协作 / 执行
- **LangGraph**（interrupt / Command / checkpointer / durable execution）
- **OWASP Business Logic / Transaction Authorization Cheat Sheets**（TOCTOU 竞态）

## 核心结论（TL;DR）

我们的方案在**架构理念上与业界最佳实践高度一致**——尤其是"零信任执行：授权决策绝不交给 LLM"这一核心原则，与 Anthropic、CSA、Cequence 的 Agentic Zero Trust 完全吻合，确定性 Policy Engine + PEP（策略执行点）位于 LLM 推理循环之外，是教科书级的正确设计。

但在**工程成熟度与生产级安全**上，存在以下分级差距：

**🔴 严重（生产阻断级）**
1. **密钥管理**：app 层 AES-256-GCM + 主密钥存环境变量，业界明确视为"custodial by default"反模式；缺 KMS/Vault envelope encryption、密钥轮换、HSM/TEE 隔离。
2. **TOCTOU 竞态**：daily_notional / orders_per_day 限额检查是"读-判断-写"非原子操作，并发请求可击穿日限额（OWASP 业务逻辑 Top 10）；审批重裁决同样存在 stale-state 窗口。

**🟠 高（架构债）**
3. **Policy Engine 硬编码**：策略逻辑用 Python if-else 写死，非"策略即代码"；缺 Cedar/OPA 式的策略外部化、形式化可分析性、热更新、SMT 验证。
4. **审计单链 vs Merkle 树**：单链 SHA-256 验证是 O(N)、无法生成包含/一致性证明、无法支撑"不信任服务器"的第三方审计，弱于 CT/Trillian。
5. **执行层缺工具参数级 rails**：NeMo 的 execution rails / AgentGuard 的 TRACE 跨步攻击链检测我们没有。

**🟡 中（增强项）**
6. **prompt 注入防御单一**：仅靠规则路由关键字 + system prompt + schema，缺专用注入分类器（Lakera/Rebuff/Llama Guard）。
7. **审批疲劳**：未借鉴 Anthropic Plan Mode（批量审批）/ 工具风险分级自动审批。
8. **LLM 可用性**：未利用 LangGraph 式 durable checkpointer，恢复管理器是自研轻量版。

详见后续各分册。每条差距都标注了**证据来源**、**我们的现状**、**业界做法**、**改进建议**与**优先级**。
