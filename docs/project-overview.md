# HTX Agent Passport — 项目全景图与流程图

## 一、项目全景概览

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        HTX Agent Passport 系统全景                          │
│            "权限 · 风险 · 审计" 的 AI 代理控制平面                          │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                    前端 (Next.js 14 App Router)                      │   │
│  │                                                                     │   │
│  │  /              登录页（演示入口）                                   │   │
│  │  /dashboard     仪表盘（Passport 列表 + 环境徽章）                  │   │
│  │  /credentials   凭证管理（添加/验证/删除 HTX API Key）              │   │
│  │  /passports     护照向导（创建/编辑 Policy）                        │   │
│  │  /actions/[id]  任务详情（实时状态 + 审批流 + 执行结果）            │   │
│  │  /audit         审计重放（哈希链验证 + STH 锚点）                   │   │
│  │  /demo          预设场景（一键演示）                                 │   │
│  │                                                                     │   │
│  │  组件: NavBar | EnvironmentBadge | PassportCard | PolicyEditor      │   │
│  │        TaskComposer | ApprovalModal | AuditTimeline | FeedbackLayer │   │
│  │        CredentialForm | STHViewer                                    │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                              │ HTTP API                                     │
│                              ▼                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                    后端 (FastAPI + Python 3.11)                      │   │
│  │                                                                     │   │
│  │  ┌──────────────────────────────────────────────────────────────┐  │   │
│  │  │ 感知层 (Perception)                                          │  │   │
│  │  │   input_normalizer → rule_router → context_builder           │  │   │
│  │  └──────────────────────────────────────────────────────────────┘  │   │
│  │  ┌──────────────────────────────────────────────────────────────┐  │   │
│  │  │ 决策层 (Decision)                                            │  │   │
│  │  │   capability_envelope → planner (B.AI) → schema_validator    │  │   │
│  │  └──────────────────────────────────────────────────────────────┘  │   │
│  │  ┌──────────────────────────────────────────────────────────────┐  │   │
│  │  │ 执行层 (Execution)                                           │  │   │
│  │  │   policy_engine → approval_service → execution_gateway       │  │   │
│  │  │   htx_adapter / simulation_engine / recovery_manager         │  │   │
│  │  └──────────────────────────────────────────────────────────────┘  │   │
│  │  ┌──────────────────────────────────────────────────────────────┐  │   │
│  │  │ 反馈层 (Feedback)                                            │  │   │
│  │  │   audit_writer → audit_merkle → observability → reputation   │  │   │
│  │  └──────────────────────────────────────────────────────────────┘  │   │
│  │                                                                     │   │
│  │  跨切面: auth | credentials_vault | passport_registry | daily_hist │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                              │                                              │
│              ┌───────────────┼───────────────┐                             │
│              ▼               ▼               ▼                             │
│  ┌────────────────┐  ┌────────────┐  ┌───────────────┐                    │
│  │  PostgreSQL 15 │  │  B.AI LLM  │  │  HTX Exchange │                    │
│  │  (8 tables)    │  │  API       │  │  (Pub + Priv) │                    │
│  └────────────────┘  └────────────┘  └───────────────┘                    │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 二、核心业务流程图（端到端 Happy Path）

```mermaid
flowchart TD
    %% ===== 用户入口 =====
    START([用户打开应用]) --> LOGIN{已登录?}
    LOGIN -->|否| DEMO_LOGIN[点击"进入演示"<br/>POST /api/auth/demo-login]
    DEMO_LOGIN --> JWT[签发 JWT + 写入 USER_LOGIN 审计事件]
    JWT --> DASH
    LOGIN -->|是| DASH[仪表盘<br/>显示环境徽章 + Passport 列表]

    %% ===== 凭证管理 =====
    DASH --> CRED{有 HTX 凭证?}
    CRED -->|否| ADD_CRED[添加 API 凭证<br/>AES-256-GCM 加密存储]
    ADD_CRED --> VALIDATE[验证凭证能力<br/>READ_ONLY / TRADE_ENABLED / INVALID]
    VALIDATE --> WITHDRAW_CHECK{有 withdraw 权限?}
    WITHDRAW_CHECK -->|是| FORCE_DISABLE[强制 permission_withdraw=false<br/>写入审计覆盖记录]
    WITHDRAW_CHECK -->|否| CRED_READY
    FORCE_DISABLE --> CRED_READY[凭证就绪]
    CRED -->|是| CRED_READY

    %% ===== 创建护照 =====
    CRED_READY --> PASSPORT{有 Passport?}
    PASSPORT -->|否| CREATE_PP[创建代理护照<br/>选择模板 + 自定义 Policy DSL v0]
    CREATE_PP --> PP_READY[Passport ACTIVE<br/>绑定 Policy + 凭证 + 声誉分]
    PASSPORT -->|是| PP_READY

    %% ===== 任务提交 =====
    PP_READY --> SUBMIT_TASK[用户提交自然语言任务<br/>POST /api/passports/:id/actions]

    %% ===== 感知层 =====
    SUBMIT_TASK --> NORMALIZE[输入归一化器<br/>提取结构化意图]
    NORMALIZE --> RULE_ROUTE{规则路由<br/>高置信危险关键字?}
    RULE_ROUTE -->|是: 提现/借贷| BLOCK[直接拒绝<br/>REJECT + BLOCKED_ACTION]
    RULE_ROUTE -->|否| CTX_BUILD[上下文构建器<br/>注入 policy + market_snapshot + 时间]

    %% ===== 决策层 =====
    CTX_BUILD --> PLANNER[B.AI Planner 调用<br/>返回 ActionPlan v0 JSON]
    PLANNER --> PLANNER_FAIL{B.AI 可用?}
    PLANNER_FAIL -->|否| MOCK_PLAN[降级 Mock Planner<br/>返回 no_op ActionPlan]
    PLANNER_FAIL -->|是| SCHEMA_VAL[ActionPlan Schema 校验<br/>v0 格式 + 条件必填]
    MOCK_PLAN --> SCHEMA_VAL
    SCHEMA_VAL --> SCHEMA_PASS{校验通过?}
    SCHEMA_PASS -->|否| HALLUCINATION[标记 PLAN_HALLUCINATION<br/>写入审计 + 拒绝]
    SCHEMA_PASS -->|是| POLICY_ENGINE

    %% ===== 执行层 - 策略裁决 =====
    POLICY_ENGINE[Policy Engine 裁决<br/>7 步确定性评估] --> VERDICT{verdict?}
    VERDICT -->|REJECT| REJECT_ACTION[拒绝 + reason_codes<br/>写入审计事件]
    VERDICT -->|ALLOW| AUTO_EXEC[自动执行<br/>仅限 read 操作]
    VERDICT -->|REQUIRE_APPROVAL| APPROVAL

    %% ===== 审批流 =====
    APPROVAL[创建审批请求<br/>展示 risk_score + 详情] --> USER_DECIDE{用户决定}
    USER_DECIDE -->|REJECT| USER_REJECT[用户拒绝<br/>action → REJECTED]
    USER_DECIDE -->|过期 5min| TIMEOUT[审批超时<br/>action → EXPIRED]
    USER_DECIDE -->|APPROVE<br/>typed_confirmation| RE_EVALUATE

    %% ===== 执行前重裁决 =====
    RE_EVALUATE[执行网关重裁决<br/>真实 daily_history + 行锁] --> RE_PASS{仍然 ALLOW?}
    RE_PASS -->|否| STALE_REJECT[拒绝: 策略/限额变化]
    RE_PASS -->|是| EXEC_MODE

    %% ===== 执行模式分发 =====
    EXEC_MODE{执行模式?}
    EXEC_MODE -->|simulation| SIM[模拟引擎<br/>确定性 fake 结果]
    EXEC_MODE -->|real_read| REAL_READ[HTX 公共 API<br/>真实行情数据]
    EXEC_MODE -->|real_trade| REAL_TRADE[HTX 私有 API<br/>小额真实下单]

    %% ===== 结果处理 =====
    SIM --> RESULT
    REAL_READ --> RESULT
    REAL_TRADE --> RESULT
    AUTO_EXEC --> RESULT

    RESULT[写入执行结果<br/>更新 action 状态] --> AUDIT[写入审计哈希链事件<br/>sha256(json + prev_hash + ts)]
    AUDIT --> REPUTATION[更新声誉分<br/>成功+1 / 被拒-3 / 失败-5]
    REPUTATION --> FEEDBACK[前端分层反馈<br/>进度 → 批次 → 风险 → 最终]

    %% ===== 失败恢复 =====
    PLANNER --> RECOVERY{执行异常?}
    EXEC_MODE --> RECOVERY
    RECOVERY -->|是| RECOVERY_MGR[恢复管理器<br/>5类失败处理策略]
    RECOVERY_MGR --> CHECKPOINT[检查点回滚/重试/降级]

    %% ===== 审计重放 =====
    FEEDBACK --> REPLAY[审计重放界面<br/>完整决策链路可视化]

    %% 样式
    style BLOCK fill:#f44,color:#fff
    style REJECT_ACTION fill:#f44,color:#fff
    style HALLUCINATION fill:#f44,color:#fff
    style STALE_REJECT fill:#f44,color:#fff
    style USER_REJECT fill:#f44,color:#fff
    style TIMEOUT fill:#f80,color:#fff
    style POLICY_ENGINE fill:#4a9,color:#fff
    style AUDIT fill:#38f,color:#fff
    style REPLAY fill:#38f,color:#fff
```

---

## 三、系统架构图（分层详解）

```mermaid
flowchart TB
    subgraph Frontend["🖥️ 前端 Next.js 14"]
        direction TB
        FE_LOGIN[/ 登录页 /]
        FE_DASH[/dashboard 仪表盘/]
        FE_CRED[/credentials 凭证管理/]
        FE_PP[/passports 护照管理/]
        FE_ACTION[/actions/:id 任务详情/]
        FE_AUDIT[/audit 审计重放/]
        FE_DEMO[/demo 预设场景/]
    end

    subgraph Backend["⚙️ 后端 FastAPI"]
        direction TB
        subgraph L1["感知层 Perception"]
            INPUT[input_normalizer<br/>文本→结构化意图]
            ROUTER[rule_router<br/>危险关键字拦截]
            CONTEXT[context_builder<br/>组装 Planner Prompt]
        end

        subgraph L2["决策层 Decision"]
            CAP[capability_envelope<br/>能力包构建]
            PLAN[planner / bai_client<br/>B.AI → ActionPlan]
            SCHEMA[schema_validator<br/>ActionPlan v0 校验]
        end

        subgraph L3["执行层 Execution"]
            PE[policy_engine<br/>7步确定性裁决]
            PE_CEDAR[policy_engine_cedar<br/>Cedar Shadow PoC]
            APPROVE[approval_service<br/>审批生命周期]
            EXEC[execution_gateway<br/>重裁决+调度]
            HTX[htx_adapter<br/>HTX API 封装]
            SIM[simulation_engine<br/>确定性模拟]
            RECOVER[recovery_manager<br/>5类失败恢复]
            STALE[stale_price_check<br/>行情时效校验]
            DAILY[daily_history<br/>日限额原子聚合]
        end

        subgraph L4["反馈层 Feedback"]
            AUDIT_W[audit_writer<br/>事件写入]
            MERKLE[audit_merkle_service<br/>Merkle Tree + STH]
            STH[audit_sth_anchor<br/>外部锚点签名]
            OBS[observability<br/>4类数据聚合]
            REP[reputation_service<br/>声誉评分]
        end

        subgraph Cross["跨切面服务"]
            AUTH[auth<br/>JWT 签发/验证]
            VAULT[credentials<br/>AES-256-GCM 保险库]
            PASSPORT[passports<br/>注册中心+状态机]
            SEED[seed_data / demo_scenarios<br/>种子数据]
        end
    end

    subgraph External["🌐 外部依赖"]
        BAI[("B.AI LLM API<br/>自然语言→ActionPlan")]
        HTX_PUB[("HTX 公共 API<br/>行情/深度")]
        HTX_PRIV[("HTX 私有 API<br/>账户/下单")]
    end

    subgraph Storage["💾 存储"]
        PG[("PostgreSQL 15<br/>8 张核心表")]
    end

    Frontend -->|REST API| Backend
    PLAN --> BAI
    HTX --> HTX_PUB
    HTX --> HTX_PRIV
    Backend --> PG
```

---

## 四、数据模型关系图

```mermaid
erDiagram
    users ||--o{ api_credentials : "拥有"
    users ||--o{ agent_passports : "创建"
    api_credentials ||--o{ agent_passports : "绑定"
    agent_passports ||--o{ agent_actions : "执行"
    agent_actions ||--o| approvals : "需要审批"
    agent_actions ||--o| execution_results : "产生结果"
    agent_actions ||--o{ audit_events : "生成审计"
    agent_actions ||--o{ model_calls : "LLM调用"

    users {
        uuid id PK
        text primary_wallet UK
        text email
        text role
        timestamptz created_at
    }

    api_credentials {
        uuid id PK
        uuid user_id FK
        text access_key_hash
        bytea encrypted_secret_key
        text state "CREATED|READ_ONLY|TRADE_ENABLED|INVALID|DELETED"
        boolean permission_withdraw "永远 false"
        timestamptz deleted_at "软删除"
    }

    agent_passports {
        uuid id PK
        uuid user_id FK
        uuid api_credential_id FK
        text name
        jsonb policy_json "Policy DSL v0"
        text state "DRAFT|ACTIVE|SUSPENDED|REVOKED"
        float reputation_score "0-100"
        int version
    }

    agent_actions {
        uuid id PK
        uuid passport_id FK
        text user_task "原始自然语言"
        jsonb action_plan "ActionPlan v0"
        text status "PENDING→PLANNING→POLICY_CHECK→APPROVED→EXECUTING→SUCCESS/FAILED/REJECTED"
        jsonb verdict "Policy Engine 裁决"
        text trace_id "全链路追踪"
    }

    approvals {
        uuid id PK
        uuid action_id FK
        text status "PENDING|APPROVED|REJECTED|EXPIRED"
        text confirmation_text
        timestamptz expires_at "5分钟超时"
    }

    execution_results {
        uuid id PK
        uuid action_id FK
        text execution_mode "simulation|real_read|real_trade"
        jsonb result_data
        text status "SUCCESS|FAILED|SIMULATED"
    }

    audit_events {
        uuid id PK
        uuid action_id FK
        text event_type
        jsonb event_json
        text event_hash "sha256 哈希链"
        text previous_event_hash
        int sequence_number
    }

    model_calls {
        uuid id PK
        uuid action_id FK
        text model_role "planner|policy|summary|fallback"
        int input_tokens
        int output_tokens
        int latency_ms
    }
```

---

## 五、执行模式与安全分层

```mermaid
flowchart LR
    subgraph Modes["三种执行模式"]
        SIM["🟢 Simulation (默认)<br/>确定性模拟 · 无真实调用"]
        READ["🟡 Real Read<br/>真实行情 · 写操作仍模拟"]
        TRADE["🔴 Real Trade<br/>小额真实下单<br/>需 DEMO_REAL_TRADE=true"]
    end

    subgraph Security["安全纵深（6层防御）"]
        S1["① 规则路由 — 危险关键字直接拦截"]
        S2["② Schema 校验 — 格式/幻觉检测"]
        S3["③ Policy Engine — 确定性 7 步裁决"]
        S4["④ 审批服务 — 人类审核 + 重裁决"]
        S5["⑤ 执行网关 — 二次裁决 + 幂等"]
        S6["⑥ withdraw 硬禁用 — 代码级不可覆盖"]
    end

    SIM -.->|最安全| Security
    TRADE -.->|全防御生效| Security
```

---

## 六、Policy Engine 7 步裁决流程

```mermaid
flowchart TD
    START[接收 ActionPlan] --> S1[Step 1: Passport 状态检查<br/>必须 ACTIVE]
    S1 -->|非 ACTIVE| R1[REJECT: PASSPORT_REVOKED/SUSPENDED]
    S1 -->|ACTIVE| S2[Step 2: 执行模式检查<br/>action 是否被当前模式允许]
    S2 -->|不允许| R2[REJECT: EXECUTION_DISABLED]
    S2 -->|允许| S3[Step 3: 阻断动作检查<br/>blocked_actions 列表]
    S3 -->|命中| R3[REJECT: BLOCKED_ACTION_xxx]
    S3 -->|未命中| S4[Step 4: 能力检查<br/>capabilities 白名单]
    S4 -->|无权限| R4[REJECT: CAPABILITY_NOT_GRANTED]
    S4 -->|有权限| S5[Step 5: 限额检查<br/>max_notional · max_orders_per_day]
    S5 -->|超限| R5[REJECT: LIMIT_xxx_EXCEEDED]
    S5 -->|未超| S6[Step 6: Symbol 白名单<br/>allowed_symbols 检查]
    S6 -->|不允许| R6[REJECT: SYMBOL_NOT_ALLOWED]
    S6 -->|允许| S7[Step 7: 审批要求<br/>approval.require_human?]
    S7 -->|是且为写操作| APPROVE[REQUIRE_APPROVAL]
    S7 -->|否或为读操作| ALLOW[ALLOW]

    style R1 fill:#f44,color:#fff
    style R2 fill:#f44,color:#fff
    style R3 fill:#f44,color:#fff
    style R4 fill:#f44,color:#fff
    style R5 fill:#f44,color:#fff
    style R6 fill:#f44,color:#fff
    style APPROVE fill:#f80,color:#fff
    style ALLOW fill:#4a9,color:#fff
```

---

## 七、审计哈希链与 Merkle 树

```mermaid
flowchart LR
    subgraph HashChain["哈希链 (append-only)"]
        E0["Event₀<br/>hash = sha256(json₀ + GENESIS + ts₀)"]
        E1["Event₁<br/>hash = sha256(json₁ + hash₀ + ts₁)"]
        E2["Event₂<br/>hash = sha256(json₂ + hash₁ + ts₂)"]
        EN["Event_n<br/>hash = sha256(json_n + hash_{n-1} + ts_n)"]
        E0 --> E1 --> E2 --> EN
    end

    subgraph MerkleTree["Merkle 树 + STH"]
        LEAF1[叶节点₁]
        LEAF2[叶节点₂]
        LEAF3[叶节点₃]
        LEAF4[叶节点₄]
        NODE1[内部节点]
        NODE2[内部节点]
        ROOT[Merkle Root]
        LEAF1 --> NODE1
        LEAF2 --> NODE1
        LEAF3 --> NODE2
        LEAF4 --> NODE2
        NODE1 --> ROOT
        NODE2 --> ROOT
        ROOT --> STH[Signed Tree Head<br/>定期签名锚点]
    end

    EN -.->|叶节点| LEAF4
```

---

## 八、项目模块全景

### 后端服务模块 (29个)

| 模块 | 层 | 职责 |
|------|------|------|
| `input_normalizer` | 感知 | NL → 结构化意图 |
| `context_builder` | 感知 | 组装 Planner Prompt |
| `capability_envelope` | 决策 | Passport → 能力包 |
| `planner` / `bai_client` | 决策 | B.AI 调用 + 降级 |
| `policy_engine` | 执行 | 确定性 7 步裁决 |
| `policy_engine_cedar` | 执行 | Cedar 形式化验证 (shadow) |
| `policy_validator` | 执行 | Policy DSL v0 结构校验 |
| `policy_diagnostics` | 执行 | 策略诊断 + reason_codes 累积 |
| `approval_service` | 执行 | 审批生命周期管理 |
| `execution_gateway` | 执行 | 重裁决 + 调度执行 |
| `htx_adapter` | 执行 | HTX API 封装 (mock/real) |
| `simulation_engine` | 执行 | 确定性模拟结果 |
| `stale_price_check` | 执行 | 行情时效校验 |
| `daily_history` | 执行 | 日限额原子聚合 (行锁) |
| `recovery_manager` | 执行 | 5 类失败恢复 |
| `tool_executor` | 执行 | 工具调用分发 |
| `audit_writer` | 反馈 | 审计事件写入 |
| `audit_merkle_service` | 反馈 | Merkle Tree 构建 |
| `audit_sth_anchor` | 反馈 | STH 外部锚点 |
| `audit_sth_scheduler` | 反馈 | 定时 STH 签名 |
| `observability` | 反馈 | 4 类可观测数据 |
| `reputation_service` | 反馈 | 声誉评分更新 |
| `credentials` | 跨切面 | 凭证 CRUD + 加密 |
| `credential_usage` | 跨切面 | 凭证使用限额 |
| `passports` | 跨切面 | Passport 注册中心 |
| `seed_data` | 跨切面 | 种子数据生成 |
| `demo_scenarios` | 跨切面 | 预设演示场景 |

### 前端页面 & 组件 (7页 + 10组件)

| 页面 / 组件 | 功能 |
|-------------|------|
| `/` (page.tsx) | 登录入口，"进入系统"按钮 |
| `/dashboard` | Passport 卡片列表 + 环境模式 |
| `/credentials` | 凭证添加/验证/管理 |
| `/passports` | 创建/编辑护照 + Policy 编辑器 |
| `/actions/[id]` | 单次任务全流程可视化 |
| `/audit` | 审计链浏览 + 哈希验证 + STH |
| `/demo` | 一键预设场景触发 |
| `NavBar` | 全局导航 + 用户信息 |
| `EnvironmentBadge` | DEMO/SIMULATION/REAL 模式标识 |
| `PassportCard` | Passport 摘要卡片 |
| `PolicyEditor` | Policy DSL v0 可视化编辑 |
| `TaskComposer` | 自然语言任务输入框 |
| `ApprovalModal` | 审批确认弹窗 |
| `AuditTimeline` | 审计事件时间轴 |
| `FeedbackLayer` | 分层反馈（进度/风险/结果） |
| `CredentialForm` | 凭证表单 |
| `STHViewer` | Signed Tree Head 查看器 |

---

## 九、实施波次总览

```mermaid
gantt
    title 实施波次 (24 核心任务 · 全部已完成 ✅)
    dateFormat X
    axisFormat %s

    section Wave 1
    项目脚手架           :done, 1, 2

    section Wave 2
    数据库模型+迁移      :done, 2, 3
    演示认证             :done, 2, 3

    section Wave 3
    凭证保险库           :done, 3, 4
    Policy DSL           :done, 3, 4
    审计哈希链           :done, 3, 4
    HTX适配器+模拟引擎   :done, 3, 4

    section Wave 4
    ActionPlan Schema    :done, 4, 5
    可观测性+声誉        :done, 4, 5

    section Wave 5
    Policy Engine        :done, 5, 6
    输入归一化+路由      :done, 5, 6
    前端:认证+护照       :done, 5, 6

    section Wave 6
    Planner适配器        :done, 6, 7
    审批服务             :done, 6, 7
    恢复管理器           :done, 6, 7
    L2/L3/L5测试         :done, 6, 7

    section Wave 7
    执行网关             :done, 7, 8
    前端:任务+审批       :done, 7, 8
    前端:审计重放        :done, 7, 8

    section Wave 8
    Demo种子+场景        :done, 8, 9

    section Wave 9
    L4端到端测试         :done, 9, 10

    section Wave 10
    README+验收          :done, 10, 11
```

---

## 十、关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| LLM 输出处理 | 仅作为提案，不直接执行 | 零信任原则：确定性 Policy Engine 做最终裁决 |
| 策略引擎 | 确定性 Python（非规则引擎） | 可审计、可测试、无随机性 |
| 密钥存储 | AES-256-GCM + Envelope Encryption | 金融级安全标准 |
| 审计证据 | 哈希链 + Merkle Tree + STH | 防篡改 + 第三方可验证 |
| 执行模式 | 三模式分离 | 演示安全：默认不碰真实资金 |
| Withdraw | 代码级硬禁用 | 不可通过策略/配置开启提现 |
| B.AI 降级 | Mock Planner 返回 no_op | 外部依赖失败不阻断演示 |
| 审批超时 | 5 分钟自动过期 + 执行前重裁决 | 防止 stale state 被利用 |
