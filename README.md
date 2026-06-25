# HTX Agent Passport

权限、风险、审计的 AI 代理控制平面。

HTX Agent Passport 是一个夹在 **LLM 规划器** 与 **加密货币交易所** 之间的安全控制层。
用户为 AI 代理签发"护照"（Passport，即能力包），用自然语言下达交易任务；系统通过
确定性策略引擎裁决、人机审批、哈希链审计，确保 AI 的每一步操作都在用户授权边界内，
可审批、可撤销、可追溯。

核心原则——**零信任执行**：LLM 的输出只是提案，所有可执行动作必须经过确定性策略引擎
裁决，AI 永远不能绕过策略边界直接执行金融操作。

---

## 架构

四层架构（感知 → 决策 → 执行 → 反馈）：

```
感知层   自然语言任务 → 规则路由（高危关键字拦截）→ 上下文构建（< 8K tokens）
决策层   B.AI Planner（LLM 生成 ActionPlan）→ Schema 校验 → 不可用时降级 mock
执行层   Policy Engine（7 步确定性裁决）→ 审批服务 → 执行网关 → HTX 适配器
反馈层   审计哈希链 + 可观测性（4 类数据）+ 声誉分
```

三道安全防线：
1. **规则路由**——高危意图（提现/借贷/杠杆/转出）在调用 LLM 前直接拦截。
2. **Policy Engine**——确定性裁决，不受 LLM 幻觉或 prompt 注入影响。
3. **执行网关二次裁决**——审批后、执行前再次校验当前策略。

技术栈：Next.js 14 (App Router) + FastAPI (Python 3.11+) + PostgreSQL + B.AI LLM API + HTX REST API。

---

## 快速开始

### 前置要求

- Python 3.11+（推荐用 conda 或 venv 虚拟环境）
- Node.js 18.18+
- 一个 PostgreSQL 数据库（本地、Docker 或云端 Neon/Supabase 均可）

### 1. 后端

```bash
# 创建虚拟环境（conda 示例）
conda create -n htx-passport python=3.11 -y
conda activate htx-passport

# 安装依赖
cd backend
pip install -r requirements.txt
```

创建 `backend/.env`（参考仓库根目录 `.env.example`），至少配置以下变量：

| 变量 | 必填 | 说明 |
|------|------|------|
| `DATABASE_URL` | ✅ | PostgreSQL 连接串，如 `postgresql+psycopg://user:pass@host/db?sslmode=require` |
| `VAULT_MASTER_KEY` | ✅ | AES-256-GCM 主密钥，64 hex 字符。生成：`python -c "import secrets; print(secrets.token_hex(32))"` |
| `JWT_SECRET` | ✅ | JWT 签名密钥（任意强随机字符串） |
| `GENESIS_HASH` | ✅ | 审计链首事件常量，如 `HTX_AGENT_PASSPORT_GENESIS_V1` |
| `BAI_API_KEY` | 可选 | 留空走 mock planner；填入后调用真实 LLM（在 https://b.ai 创建，格式 `sk-xxx`） |
| `BAI_MODEL` | 可选 | B.AI 模型 ID，默认 `deepseek-v4-flash`（也支持 `gpt-5.2` / `claude-sonnet-4-6`） |
| `VOLCENGINE_TTS_API_KEY` | 可选 | 火山引擎 API Key，留空时演示页面降级到 Web Speech API |
| `VOLCENGINE_TTS_VOICE_ID` | 可选 | TTS 音色 ID，默认 `zh_female_vv_uranus_bigtts` |

运行数据库迁移并启动后端：

```bash
cd backend
alembic upgrade head
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

后端启动后访问 http://localhost:8000/health 验证（应返回 `{"status":"ok"}`）。

### 2. 前端

```bash
cd frontend
npm install
npm run dev
```

可选 `frontend/.env.local`：

```
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
NEXT_PUBLIC_EXECUTION_MODE=SIMULATION
```

前端默认运行在 http://localhost:3000。

---

## 产品演示页面

启动后端后访问 http://localhost:8000/static/product-demo-video.html，
即可观看带 AI 语音旁白的全流程产品演示（8幕动画 + 画中画真人头像）。

演示前置：
1. 在 `.env` 中配置 `VOLCENGINE_TTS_API_KEY`（否则降级到 Web Speech API）
2. 以 `uvicorn main:app --host 127.0.0.1 --port 8000` 启动后端
3. 浏览器打开 http://localhost:8000/static/product-demo-video.html
4. 页面加载时自动预合成全部旁白（约 2 分钟），完成后自动播放

---

## 操作流程

```
进入系统 → 仪表盘
            ├─ 凭证管理（添加 HTX API Key → 验证 → TRADE_ENABLED）
            ├─ 创建护照（选模板 → 绑定凭证 → 编辑策略 → 激活）
            ├─ 护照详情 → 发起任务（自然语言 + 执行模式）
            │      → 规划 → 策略裁决 → 审批 → 执行 → 结果反馈
            └─ 审计重放（完整事件链 + 哈希验证）
```

执行模式：
- `simulation`——模拟引擎，不触碰真实交易所（默认）。
- `real_read`——仅调用 HTX 公共行情。
- `real_trade`——真实下单，需 `DEMO_REAL_TRADE=true` 才允许。

### 预设场景

系统内置 3 个预设场景，可通过 `/demo` 页面或 API 触发，全程无外部依赖：

| 场景 | 任务 | 预期结果 |
|------|------|---------|
| happy | 合法 10 USDT 限价买入 | `EXECUTED` |
| reject | 提现全部 USDT | `AUTO_REJECTED`（BLOCKED_ACTION_WITHDRAW） |
| over_limit | 买入 500 USDT 的 BTC | `AUTO_REJECTED`（LIMIT_MAX_NOTIONAL_EXCEEDED） |

API 调用（需先登录拿 token）：

```bash
curl -X POST http://localhost:8000/api/scenarios/happy -H "Authorization: Bearer <token>"
```

---

## 测试

测试分五层（L1 单元 → L2 集成 → L3 Eval → L4 E2E → L5 对抗与混沌）：

```bash
cd backend

# 全部测试
python -m pytest

# 分层运行
python -m pytest tests/unit                          # L1 单元（含 PBT）
python -m pytest tests/integration/test_l2_integration.py   # L2 集成
python -m pytest tests/eval                           # L3 Eval（路由/工具/安全/效率/质量）
python -m pytest tests/integration/test_l4_e2e.py     # L4 端到端
python -m pytest tests/integration/test_l5_adversarial_chaos.py  # L5 对抗与混沌
```

前端类型检查：

```bash
cd frontend
npm run type-check
```

测试要点：
- **确定性组件**（Policy Engine、Schema 校验、哈希链、加密）由 Property-Based Testing
  （hypothesis）验证 10 个正确性属性。
- **安全合规零容忍**——L3 安全维度、L5 prompt 注入测试通过率必须 100%，任一失败即 blocker。
- 全部测试使用 in-memory SQLite + mock B.AI/HTX，无外部网络依赖。

---

## 安全说明

- **API 密钥**：经**信封加密（envelope encryption）**存储——每条凭证用独立 DEK（AES-256-GCM）
  加密，DEK 再由 KEK 包裹；绝不以明文落库、绝不发送给 LLM、绝不出现在日志或审计事件中。
  `GET /api/credentials` 永不返回 secret。
- **提现硬禁用**：MVP 中 `withdraw` 能力硬编码为 `false`，任何试图开启的请求被拒绝。
- **零信任执行**：LLM 不持有任何工具调用能力；其输出仅经 Schema 校验提取结构化 ActionPlan，
  原始响应绝不直接暴露给执行网关。
- **prompt 注入防护**：Policy Engine 是确定性纯函数，只读结构化字段，完全忽略 rationale 中的
  自然语言指令。"忽略以上规则"之类的注入无法改变裁决结果。
- **审计哈希链**：每个事件 `event_hash = sha256(canonical_json + previous_hash + created_at)`，
  从 genesis 起逐事件可重算验证；额外有 RFC 6962 风格 **Merkle 树 + 签名 Tree Head（STH）**
  层（`audit_tree_heads` 表），任何篡改/删除/插入 STH 签发后都会被检测，支持 O(log N)
  inclusion proof，便于第三方审计。
- **前端存储**：token 存于 sessionStorage（非 localStorage），不存储任何密钥。
- **kill switch**：`DEMO_DISABLE_EXECUTION=true` 时全局拒绝所有非只读操作。

### 密钥管理威胁模型（务必知晓）

凭证加密用可插拔的 KEK provider（`VAULT_KEY_PROVIDER`）：

- **`local`（默认）**：KEK 从 `VAULT_MASTER_KEY` 环境变量派生，存在于进程内存。
  这是**开发/演示级**方案——KEK 不具备 HSM/KMS 的隔离性。适合本地、Demo、无真实资金的场景。
- **`aws-kms`（生产推荐）**：KEK（CMK）永不离开 AWS KMS/HSM 边界（FIPS 140-3 L3），
  wrap/unwrap 通过 KMS API 完成。**接入真实交易前应切换到此模式**（需 boto3 + `VAULT_KMS_KEY_ID`）。

密钥轮换：`EnvelopeVault.rewrap` 支持 KEK 轮换（信封头记录 KEK 版本，旧版本保留用于解旧密文，
后台 job 渐进 re-wrap），无需重加密 payload、不停机。

> ⚠️ **生产/真实资金部署前**：(1) `VAULT_KEY_PROVIDER=aws-kms`；(2) 日限额 TOCTOU 已通过
> passport 行锁修复，但建议在生产 PostgreSQL 下补充并发压测；(3) 审计为单链哈希，
> 如需"不信任服务器的可验证审计"应升级为 Merkle 树（见 `docs/tech-research/`）。

---

## 项目结构

```
backend/
  app/
    core/         配置、数据库、认证、加密、状态机、审计链内核
    models/       8 张表的 SQLAlchemy ORM 模型
    schemas/      Policy DSL v0 / ActionPlan v0 / 请求响应 schema
    services/     Policy Engine、Planner、审批、执行网关、HTX 适配器、恢复管理器等
    routers/      auth / credentials / passports / approvals / actions / audit / demo / tts / ws
  alembic/        数据库迁移
  tests/          unit (L1) / integration (L2,L4,L5) / eval (L3)
frontend/
  src/
    app/          App Router 页面（登录/仪表盘/凭证/护照/任务/审计）
    components/    NavBar / PolicyEditor / ApprovalModal / AuditTimeline 等
    hooks/         useAuth / useActionPolling
    lib/           API 客户端 / 类型
design-demos/
  product-demo-video.html  全流程产品演示页面（8幕 + TTS 旁白 + PIP 头像）
  avatar.png               画中画真人头像
  index.html               三版 UI 设计变体预览
```
