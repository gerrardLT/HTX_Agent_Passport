# 04 · 差距总表与改进路线图

> 汇总前三册的 19 项差距，按严重性分级，给出分阶段改进路线图。

## 4.1 差距总表（按严重性排序）

### 🔴 严重（生产阻断级 —— 上真实交易前必须修）

| # | 差距 | 影响 | 修复代价 |
|---|------|------|---------|
| G13 | **TOCTOU 竞态**：日限额/订单数"读-判断-写"非原子，并发可击穿限额 | 资金风险：实际成交超出用户授权上限 | 中（DB 事务 + 行锁） |
| G14 | **执行网关重裁决用空 daily_history**（代码自承"简化"） | 二次裁决形同虚设，日限额在执行层未真正强制 | 低（接入真实聚合） |
| G7 | **主密钥存环境变量**，非 KMS/HSM；业界判定为 custodial 反模式 | 密钥泄露风险 + 合规/牌照风险 | 中（接 KMS） |
| G8 | **无 envelope encryption**（单层加密） | 无法轮换 KEK、无 key 使用审计 | 中（随 G7 一起改） |

### 🟠 高（架构债 —— 影响可信度与可维护性）

| # | 差距 | 影响 | 修复代价 |
|---|------|------|---------|
| G15 | place_order 无幂等键 | 并发/重试重复下单 | 中 |
| G16 | ✅ 审批延迟后未重校验 market snapshot 时效（已修复 2026-05-31） | stale price 导致成交偏离 | 低 |
| G17 | 缺并发竞态测试 | 竞态 bug 无法被 CI 捕获 | 低 |
| G9 | 无密钥轮换机制 | 长期静态密钥，泄露后影响面大 | 中 |
| G10 | ✅ 审计单链无包含/一致性证明（已完成 2026-05-31） | 无法支撑"不信任服务器"的第三方审计 | 中（Trillian） |
| G11 | ✅ 审计链可被"删除+全链重写"绕过（已完成 2026-05-31） | "防篡改"宣称被夸大 | 低（外部 STH 锚点） |
| G1 | Policy Engine 硬编码，非"策略即代码"，无形式化可分析性 ✅（Cedar shadow PoC 完成 2026-05-31） | 策略变更无法形式化验证等价性，不可热更新 | 高（Cedar/OPA） |
| G2 | 缺跨步攻击链 / 信息流追踪 ✅（已完成 2026-05-31，default-off opt-in） | 防不住多步组合攻击 | 高 |

### 🟡 中（增强项）

| # | 差距 | 修复代价 |
|---|------|---------|
| G3 | 裁决顺序敏感，reason_codes 不累积 ✅（已完成 2026-05-31，diagnose_policy 累积函数） | 低 |
| G4 | 缺专用 prompt 注入分类器 | 中 |
| G5 | 工具返回值（真实行情）未做不可信输入处理 ✅（已完成 2026-05-31） | 中 |
| G19 | 策略执行在 app 层而非密码学强制层 | 高（CEX 场景非强求） |

### 🟢 低（锦上添花）

| # | 差距 | 修复代价 |
|---|------|---------|
| G6 | 缺 `LLM_CHECK` 式模糊操作二次审查 | 中 |
| G12 | 无动态/短时凭证、无 secret-level 审计 ✅（凭证使用限额已完成 2026-05-31） | 中 |
| G18 | 全操作审批，未缓解审批疲劳 ✅（已完成 2026-05-31） | 低 |

## 4.2 我们做得好的地方（不要误改）

调研同时确认了我们的多项设计是**业界最佳实践的正确实现**，应当保留并在文档中强调：

1. ✅ **零信任执行**：授权决策不交给 LLM，确定性 PEP 在 LLM 循环之外（对齐 Anthropic / CSA / Cequence）。
2. ✅ **审批重裁决 + 执行二次裁决**：正确解决了 LangGraph 社区点名的"stale state on late resume"头号失败模式（policy 维度）。
3. ✅ **双重审批防护 + 主动过期扫描**：HITL 生命周期管理比 LangGraph 默认更完善。
4. ✅ **Policy Engine 直接裁决工具参数**：避开了 NeMo"rails 只看 content、看不到 tool_calls 参数"的已知限制。
5. ✅ **prompt 注入第 4 道防线**：Policy Engine 只读结构化字段、忽略 rationale 自然语言——L5 测试已验证注入无法改变裁决。
6. ✅ **密钥不进 prompt/LLM**：对齐 Coinbase"私钥永不暴露给 LLM"。
7. ✅ **kill switch / withdraw 硬禁用 / 反幻觉校验**：防御纵深到位。

## 4.3 分阶段改进路线图

### Phase 0 — 上真实交易前的"安全闸门"（🔴 必做）
目标：消除资金风险与密钥反模式。预计 1-2 周。

1. **修 TOCTOU（G13/G14）** — ✅ **已完成（2026-05-31）**
   - 新增 `app/services/daily_history.py`：`aggregate_daily_history` 从 DB 真实聚合当日
     「在途 + 已执行」写操作的 notional/order_count（UTC 日边界、排除自身、排除被拒/过期/取消）。
   - `aggregate_daily_history_for_update` 带 passport 行锁（`SELECT ... FOR UPDATE`），
     把"聚合-裁决-写入"串行化，消除并发击穿限额的竞态（PostgreSQL 生效；SQLite 测试串行）。
   - 执行网关 `execute()` 改为执行前用真实聚合做重裁决；审批服务重裁决同样接入真实聚合。
   - 删除了"简化：用空 DailyActionHistory"的临时代码（日限额此前**从未生效**，现已修复）。
   - 测试：`tests/unit/test_daily_limit_enforcement.py`（12 例，含"串行累加至限额后续笔被拒"核心场景）。
2. **place_order 幂等键（G15）** — ✅ **已完成**：执行网关执行前检查该 action 是否已有 SUCCESS
   执行结果，是则拒绝（`ALREADY_EXECUTED`）；action_id 作天然幂等键。
3. **并发竞态测试（G17）** — ✅ **部分完成**：已补功能级强制测试；纯并发压测（HTTP/2 多路复用）
   留待生产环境 PostgreSQL 下补充。
4. **KMS envelope encryption（G7/G8）** — ✅ **架构已完成（2026-05-31），KMS 接入待云环境**
   - 新增 `app/core/key_provider.py`：可插拔 KEK provider 抽象（`LocalKeyProvider`
     默认从 VAULT_MASTER_KEY 派生；`AwsKmsKeyProvider` 为生产接入点占位，启用需 boto3 + VAULT_KMS_KEY_ID）。
   - 新增 `app/core/envelope_vault.py`：envelope encryption（per-record DEK + KEK 包裹），
     versioned 信封格式 "EV02"，**向后兼容**旧单层 CredentialVault 密文（迁移期已有 DB 行仍可解）。
   - 凭证服务 `_get_vault()` 切换为 `EnvelopeVault`；新写入用信封格式，读取双格式透明兼容。
   - 配置开关 `VAULT_KEY_PROVIDER`（local/aws-kms），切换无需改业务代码。
   - 测试：`tests/unit/test_envelope_vault.py`（20 例，含向后兼容、轮换、篡改检测、provider 不匹配）。
   - ⏳ **剩余**：生产前把 `VAULT_KEY_PROVIDER=aws-kms` 并实现 `AwsKmsKeyProvider`（boto3 调 KMS
     GenerateDataKey/Decrypt），需你的 AWS/云环境。
5. **密钥轮换（G9）** — ✅ **机制已完成**：`EnvelopeVault.rewrap` + `needs_rewrap`（Vault Transit 模式：
   KEK 版本号写入信封头，旧版本 KEK 保留用于解旧密文，后台 job 可渐进 re-wrap）。`LocalKeyProvider`
   支持多版本 KEK。生产轮换流程：轮换 KEK → 后台遍历 `api_credentials` 密文列调 rewrap 回写。
6. **诚实文档** — ⏳ 待办：README 安全说明标注当前密钥管理威胁模型边界（local provider 是开发级，
   生产需 aws-kms）。

### Phase 1 — 可信度增强（🟠 高）
目标：让"可审计、防篡改"的宣称名副其实。预计 2-4 周。

6. **审计 Merkle 化（G10/G11）** — ✅ **已完成（2026-05-31，含周期签发 + 外部锚定 + 前端展示）**
   - 新增 `app/core/merkle.py`：纯函数 RFC 6962 Merkle 树（leaf/node/root hash、
     inclusion proof、consistency proof、verify_inclusion_proof）。
   - 新增 `app/models/audit_tree_head.py` + 迁移 `audit_merkle_v1`：`audit_tree_heads` 表
     存周期签发的 Signed Tree Head（root_hash + tree_size + signature + signed_at）。
   - 新增 `app/services/audit_merkle_service.py`：从 audit_events 派生叶子（用现有
     event_hash 作 leaf data，与线性链共享密码学承诺）、签发/验证 STH、生成 inclusion 与
     consistency proof。HMAC-SHA256 签名（密钥 `AUDIT_STH_SIGNING_KEY`，回退 JWT_SECRET），
     生产可升级 Ed25519。
   - **价值**：单链无法防御的"删除+全链重写"攻击，现在 STH 一经发布（建议外部锚定到
     append-only 媒介）即可检测。评委可拿 inclusion proof 在不下载全链的前提下证明某事件
     确实在日志中（O(log N)）。
   - **周期 STH 签发后台任务**（新）：`app/services/audit_sth_scheduler.py` 提供
     `STHScheduler` 类（asyncio.create_task + 可中断 sleep）+ `issue_sth_for_all_chains`
     扫描所有 (user_id, passport_id) 链按需签发；FastAPI lifespan 已挂入
     （`AUDIT_STH_ENABLED=true` 默认开启，5 分钟一轮，CI 默认关）。
     单链失败不影响其他链；同 root 不签冗余 STH（按 tree_size 单调判断）。
   - **外部锚定**（新）：`app/services/audit_sth_anchor.py` 把 STH 追加到本地 JSONL 文件
     （`AUDIT_STH_ANCHOR_PATH`），unix 工具友好（`tail -f`/`grep`/`jq -c` 可消费）；
     幂等（同 STH 跳过）+ 失败静默（不让锚定故障拖垮主路径）。生产可切换到
     S3 Object Lock / 公开 git / 区块链锚定，不影响调用方。
   - **审计/STH 路由**（新）：`/api/audit/events`（事件列表，强制 user_id 鉴权隔离）
     + `/api/audit/sth/latest`（最新 STH）+ `/api/audit/sth/issue`（手动签发，POST
     后立即锚定）+ `/api/audit/events/{event_id}/inclusion`（inclusion proof，前端可
     用 `verify_inclusion_proof` 独立验证 root）+ `/api/audit/sth/consistency`
     （consistency proof）。新增 `/api/actions/{id}` + `/api/actions/{id}/audit`
     补全前端轮询 + 审计重放页面所需的 action-centric 端点。
   - **前端 STH 展示**（新）：`frontend/src/components/STHViewer.tsx` 显示
     当前最新 STH（root_hash 可复制 + signature 缩写 + tree_size + signed_at），
     带"立即签发"按钮；用户级审计中心 `/audit` 页面 + 单 action 审计重放
     `/actions/[id]/audit` 页面均集成 STHViewer。NavBar 已加"审计"入口。
   - 测试：
     - `tests/unit/test_audit_merkle.py`（26 例：merkle 核心 + service 层）
     - `tests/unit/test_audit_sth_anchor.py`（12 例：JSONL 写入、幂等、失败静默）
     - `tests/unit/test_audit_sth_scheduler.py`（15 例：生命周期幂等、单链容错、_tick_sync 集成）
     - `tests/integration/test_audit_router.py`（21 例：5 端点鉴权 + inclusion proof
       端到端密码学正确性）
     - `tests/integration/test_actions_router.py`（9 例：action 详情 + audit 列表
       鉴权隔离）
   - ✅ 已完成全部"剩余"项（周期签发 / 外部锚定 / 前端展示）。生产可继续：
     接入 S3 Object Lock / 公开 git / 区块链锚定（替换 `audit_sth_anchor` 实现）；
     升级 HMAC-SHA256 → Ed25519（替换 `_sign_payload` / `_verify_signature`）。
7. **密钥轮换（G9）** — ✅ 已完成（envelope_vault.rewrap，见 Phase 0）。
8. **审批 stale price 重校验（G16）** — ✅ **已完成（2026-05-31）**
   - 新增 `app/services/stale_price_check.py`：纯函数
     `check_market_snapshot_freshness_and_slippage` 同时检查（a）snapshot 时效性
     （`now - as_of > 60s` 即过期）与（b）`limit_price` 偏离 snapshot.last 是否
     超过 `policy.limits.max_slippage_bps`。返回不可变 `StalePriceCheckResult`,
     `ok=True/False + reason_code + detail`，调用方据此写审计 + 阻断。
   - 新增审计事件 `MARKET_SLIPPAGE_DETECTED`（`AuditEventType` + `ALL` frozenset）
     与两个 reason_code（`MARKET_SLIPPAGE_EXCEEDED` 80 / `MARKET_SNAPSHOT_STALE` 70）
     登记到 `policy_engine.REASON_CODES`，让审计 / 前端展示有统一契约。
   - 在 `SEED_MARKET_DATA`（`htx_adapter` + `seed_data`）增加 `as_of` 静态时间戳,
     生产前必须接入实时数据使 `as_of` 始终新鲜——种子数据时间永久过期反而是好事:
     强制开发者上真实数据再成交，杜绝"按种子数据成交"的资金风险。
   - 执行网关 `execute()` 在 Step 3c（聚合后 / 重裁决前）做检查；审批服务
     `submit_approval()` 在 Step 7b（policy 版本重裁决后 / 状态更新前）做检查。
     失败时执行网关抛 `ConflictError(MARKET_SNAPSHOT_STALE/EXCEEDED)`,
     审批服务抛新增的 `MarketSlippageExceededError` 并把 action 推到
     `REJECTED_BY_USER`（要求用户重新发起新单，不自动按新价审批）。
   - **价值**：修复 LangGraph 社区点名的 HITL 头号失败模式——human approval 延迟后
     市场已变，仅校验 policy 版本不够。Req 16 AC2 的 `max_slippage_bps` 此前仅
     在规划阶段使用，现已覆盖"延迟提交"场景（与"policy 版本变化"并列形成双重重裁决）。
   - 测试：`tests/unit/test_stale_price_recheck.py`（27 例：22 个纯函数场景覆盖
     "新鲜通过/过期阻断/slippage 超限/未配置/非 place_order/market 单/无 as_of/
     symbol 缺失/naive datetime/边界值"等；5 个集成场景验证执行网关与审批服务
     端到端拦截）。

### Phase 2 — 架构升级（🟠/🟡 选做，看产品走向）
目标：策略即代码、形式化可验证。预计 1-2 月。

9. **Policy Engine 外部化（G1/G3）**：✅ **PoC 完成（2026-05-31，30 天观察期 + 主裁决器切换待启动）**
    - **G1 Cedar shadow evaluator**：新增 `app/services/policy_engine_cedar.py`，把 PolicyDSL Step 0-3（kill switch / blocked_actions / capabilities / allowed_symbols）翻译为 Cedar 策略 + schema；用 `cedarpy` 4.8 引擎并行评估。
    - 新增 `CedarShadowResult` dataclass 与 `cedar_decision_matches_main` 一致性比对函数；执行网关 `execute()` 重裁决路径在 `CEDAR_SHADOW_ENABLED=true` 时跑 shadow，差异写 `CEDAR_SHADOW_DIVERGENCE` 日志（不影响主路径）。
    - **范围**：覆盖 Step 0-3 原子裁决；Step 4-7（日限额累加 / 动态时间窗口）+ 反幻觉 + G2 provenance 由 Python 主裁决器处理（Cedar 不擅长动态计算）。
    - **G3 reason_codes 累积**：新增 `app/services/policy_diagnostics.py::diagnose_policy` 累积式诊断函数，把 7 步检查全部跑完返回所有触发的 reason_codes（与 first-match-wins 的 `evaluate_policy` 并列）。前端 audit-replay / 调试场景消费；不进入主路径以保持 PBT 确定性。
    - **设计权衡**：不替换主裁决器——`evaluate_policy` 仍是 Property 1 + 30+ PBT 守护的强语义入口；Cedar/diagnose 是"second-opinion"工具。30 天 0 差异后再考虑切换。
    - 测试：
      - `tests/unit/test_cedar_shadow_evaluator.py`（30 例：Step 0-3 全覆盖 + 决定一致性比对 + ExecutionGateway 集成 + 日志记录）
      - `tests/unit/test_policy_diagnostics.py`（16 例：单一违规 / 多重累积 / no_op 路径 / G2 互动 / dataclass 性质）
    - **价值**：让"策略变更不破坏现有授权"成为可机器验证的陈述（Cedar Analysis）；reason_codes 累积让前端能一次性展示所有违规,避免反复试错。
    - ⏳ **后续**：30 天观察期统计差异后切换主裁决器；前端 audit-replay 页面展示 cumulative reason_codes；Cedar Analysis CI 集成（policy diff 时检查"等价 / 严格更松 / 严格更严"）。
10. **跨步攻击链（G2）**：✅ **已完成（2026-05-31，default-off opt-in 模式）**
    - 新增 `policy_engine.TRUSTED_MARKET_PROVENANCES = {"seed", "htx_real", "htx_cached"}` 信任白名单。
    - `evaluate_policy` 对 `place_order` 检查 `market_snapshot[symbol].provenance` 字段；不在白名单内（含缺失字段）→ 返回 `MARKET_DATA_UNTRUSTED` REJECT。
    - 新增 `GlobalConfig.enforce_market_provenance` + `ENFORCE_MARKET_PROVENANCE` 环境变量；**默认关闭**保持向后兼容；生产部署应启用。
    - `SEED_MARKET_DATA`（htx_adapter + seed_data）已带 `provenance="seed"`,启用后兼容。
    - 测试：`tests/unit/test_market_provenance.py`（19 例：默认关闭 / 启用后白名单通过 / 非白名单拒绝 / 非 place_order 跳过 / SEED_MARKET_DATA 兼容性）。
    - 设计依据：CaMeL（capabilities + provenance + readers）+ Tessera（trust label min, not max）+ Anthropic Zero Trust。
    - **价值**：防御"用户上传文档 / RAG 文档诱导按伪造价格下单"——这是 LangGraph / OpenAI Agents SDK 静态分析器（agentic-guard / Tessera）共同识别的 confused-deputy 攻击模式。
    - ⏳ **后续**：接入用户上传文档 / RAG 时，给那些来源标 `provenance="user_provided"`,自动被本检查拦截。中期可集成 Tessera 适配器跨 tool call 累积污染。
11. **prompt 注入分类器（G4）**：⏳ **调研完成**（同上）。推荐 Llama Guard 3 1B 自托管；接真实交易后做。
12. **工具返回值不可信处理（G5）**：✅ **已完成（2026-05-31）**
    - 新增 `app/services/htx_adapter.py::validate_ticker_sanity` 纯函数：4 项检查（正数 / bid≤last≤ask / 历史价格范围 / spread<5%）。
    - 新增 `TICKER_PRICE_RANGES` 常量（btcusdt: $1k-$1M，ethusdt: $10-$100k 等）。
    - `HTXAdapter.get_ticker` mock 路径已接入 sanity 校验，失败抛 `HTX_NETWORK_ERROR` → Policy Engine 反幻觉路径接管（symbol 不进 market_snapshot → PLAN_HALLUCINATION）。
    - 测试：`tests/unit/test_ticker_sanity.py`（15 例：纯函数 12 + 集成 3）。
    - **价值**：防御纵深加一层——HTX 公共 API 被中间人攻击 / 网络异常返回离谱价格时，sanity 校验在数据进入 Policy Engine 之前拦下；现有 stale_price_check（G16）继续守审批延迟场景。

### Phase 3 — 可选高级（🟢）
13. 审批疲劳缓解（G18）：✅ **已完成（2026-05-31）**
    - 新增 `app/schemas/policy.py::AutoApprovalThresholds` Pydantic 模型 + 同步 JSON Schema 字段。
    - `policy_engine.evaluate_policy` 在 REQUIRE_APPROVAL 出口前先查 `_passes_auto_approval_thresholds`，全部满足 → ALLOW + reason_codes=`AUTO_APPROVED_LOW_RISK`。
    - 新增 `GlobalConfig.passport_reputation_score` 字段；`execution_gateway.execute()` 与 `approval_service.submit_approval()` 重裁决时传入 `passport.reputation_score`。
    - 新增 `DailyActionHistory.auto_approved_count_today_utc` 字段（max_per_day 上限校验）。
    - **保守默认**：4 个阈值（max_notional_usdt / min_reputation_score / allowed_action_types / max_per_day）任一缺失即不放行；空 dict 等价"未启用"——**严格向后兼容**未配置 G18 的 passport。
    - 测试：`tests/unit/test_auto_approval_thresholds.py`（19 例：默认走人工 / 部分配置不放行 / 全满足放行 / 边界值 / 任一不满足拒 / 与 REJECT 路径交互）。
    - 设计依据：Facio 4 层 L0/L1/L2/L3 模型（详见 `docs/tech-research/07-...md` §7.1）。
    - **价值**：缓解 Anthropic / Facio / DeepMind 共同识别的"审批疲劳"问题——低 notional + 高声誉 + 未达每日上限的 place_order 直接放行,把人工审批配额留给真正需要判断的高风险操作。
14. LLM_CHECK 模糊操作二次审查（G6）：⏳ **调研完成**（同上）。FinHarness 风格 Cascade routing；接真实交易后做。
15. 动态凭证 / secret-level 审计（G12）：✅ **凭证使用限额已完成（2026-05-31，capability token 后续）**
    - 新增 `app/models/credential.py` 4 字段：`max_uses_per_day` / `current_uses_today` / `last_use_at` / `expires_at`。
    - 新增 Alembic 迁移 `credential_usage_v1`（rev: 0003，全字段 nullable，向后兼容）。
    - 新增 `app/services/credential_usage.py::check_and_record_credential_use`：状态检查 + 过期检查（自动转 INVALID + 写审计） + UTC 跨日自动重置 + 限额检查 + 计数。
    - 测试：`tests/unit/test_credential_usage.py`（17 例：默认无限制 / 过期触发 INVALID / 每日上限 / 跨 UTC 日重置 / 状态非法 / 软删除拒绝）。
    - **保守默认**：全字段 NULL = 无限制（向后兼容）；显式启用后强制每用一次过一关。
    - **设计权衡**：HTX API key 不支持动态短时凭证（交易所平台限制），所以做法是"使用维度短时化"——把"每用一次都过一关"作为零信任使用层。详见 `docs/tech-research/07-...md` §7.3。
    - ⏳ **后续**：HTX adapter sign_request 路径接入 `check_and_record_credential_use`（与 envelope_vault.decrypt 同位置）；Action-level capability token 签发（OAuth 2.0 token exchange 模式）；生产 Vault dynamic DB secrets。

---

### Phase 2.5（生产升级，2026-05-31 上线）

**STH 签名升级 HMAC-SHA256 → Ed25519** — ✅ **已完成**
- 新增 `app/core/sth_signing.py`：抽象层支持 HMAC + Ed25519 双 backend，`signature` 字段加算法前缀（`hmac:` / `ed25519:`），同表混合存储；验证器自动按前缀路由。
- 新增 `AUDIT_STH_SIGNING_ALGO` / `AUDIT_STH_ED25519_PRIVATE_KEY_PATH` / `AUDIT_STH_ED25519_PUBLIC_KEY_PATH` 三项配置。
- 新增 `scripts/generate_ed25519_key.py` 工厂脚本：一行命令生成 PEM PKCS8 私钥 + 公钥 + hex（公钥可贴 README/git 公开发布）。
- 新增 `GET /api/audit/sth/verifier-key` 端点：暴露公钥 + 当前签名算法标识，外部审计员可凭此独立验证任意 STH。
- **零切换日**：旧 HMAC 行（无前缀 / `hmac:` 前缀）和新 Ed25519 行可永久共存，`verify_sth_signature` 两路径都尝试。
- 测试：`tests/unit/test_sth_signing.py`（20 例：HMAC backend / Ed25519 backend / 跨算法路由 / 无前缀兼容 / Raw 32 字节格式 / 配置错误异常）。
- **价值**：符合 RFC 6962 / C2SP signed-note 标准；公钥可对外发布让"零信任审计"成立——评委拿公钥 + STH + inclusion proof 即可在浏览器/独立程序里验证某事件确实在日志中，不需要信任服务方。
- 详见 `docs/tech-research/05-production-upgrades.md` §5.1。
- ⏳ **生产前剩余**：在 .env 配置 `AUDIT_STH_SIGNING_ALGO=ed25519` + 部署私钥文件；可选升级到 AWS KMS Ed25519（不落盘只调 Sign API）；可选接入 C2SP signed-note 文本格式与公开 transparency log 网络。

**STH 外部锚定升级（本地 JSONL → S3 / Git / 区块链）** — ✅ **AnchorBackend ABC + S3 backend 已上线（2026-05-31，剩余生产配置）**
- 详见 `docs/tech-research/05-production-upgrades.md` §5.2。
- 新增 `AnchorBackend` ABC（`app/services/audit_sth_anchor.py`）+ 3 个具体实现：
  - `JsonLineFileAnchorBackend`（默认 / dev / CI；包装原有 JSONL 行为）
  - `S3ObjectLockAnchorBackend`（生产推荐，Compliance 模式 + 自定义 retention）
  - `NullAnchorBackend`（显式禁用）
- 新增 `get_default_anchor_backend()` 工厂：按 `AUDIT_STH_ANCHOR_BACKEND` 配置自动选择；S3 模式失败回退到 Null（不阻断 scheduler）。
- 新增 6 项配置：`AUDIT_STH_ANCHOR_BACKEND` / `_S3_BUCKET` / `_S3_RETENTION_YEARS` / `_S3_KEY_PREFIX` / `_S3_REGION` / `_S3_ENDPOINT`。
- `STHScheduler` 升级支持 `anchor_backend` 参数注入；`build_default_scheduler` 通过工厂注入。
- 测试：`tests/unit/test_audit_sth_s3_anchor.py`（20 例：构造校验 / S3 写入 + Object Lock 验证 / 幂等 / 失败静默 / 工厂路由 / 不同 retention）。**用 moto 库 mock S3，无需真实 AWS。**
- **价值**：S3 Object Lock Compliance 模式下连 root 账号都不能删除/覆盖对象——这是比 DB 行锁强得多的物理不可变性保证；多源调研（TrustWarden / Tracehold / OpenTelemetry / Velt）一致推荐为 SaaS audit log 的"等价 blockchain 不可抵赖性 + 成本可控"方案。
- 推荐分阶段：staging/初期生产 → S3（已实施）；透明度增强 → + 公开 git；高合规 → + OpenTimestamps Bitcoin。
- ⏳ **生产前剩余**：在 .env 配置 `AUDIT_STH_ANCHOR_BACKEND=s3` + S3 桶预先启用 ObjectLockEnabled + Versioning；可选升级到 KMS 加密对象 / 跨 region 复制。

## 4.4 一句话总结

**我们的架构理念是对的，而且对得很彻底**——零信任、确定性 PEP、HITL 重裁决这些"难而正确"的事我们都做了。真正的差距集中在**工程成熟度的两个点**上：

1. **并发安全**（TOCTOU）——这是一个能被实际利用的资金风险 bug，且我们的纯函数 PBT 测试覆盖不到它，必须在上真实交易前修。
2. **密钥管理**——app 层加密 + 环境变量主密钥是业界明确反对的反模式，KMS envelope encryption 是底线。

其余差距（Merkle 审计、策略即代码、注入分类器）是"锦上添花"或"长期架构债"，可按产品节奏推进。对黑客松/Demo 而言，当前方案已经相当完整；对生产/真实资金而言，Phase 0 是不可跳过的安全闸门。

---

## 附录：本次调研的主要信息源

- NVIDIA NeMo Guardrails 官方文档（docs.nvidia.com/nemo/guardrails）
- AgentGuard（github.com/WhitzardAgent/AgentGuard）
- AWS Cedar 论文《Cedar: A New Language for...Authorization》+ AWS 迁移博客
- Styra / permit.io / cybersrely OPA vs Cedar 对比
- Anthropic《Our framework for...trustworthy agents》《Trustworthy agents in practice》《How we contain Claude》《Zero Trust for AI agents》
- Cequence《Agentic Zero Trust》研究论文（Chase Cunningham）+ CSA Agentic Trust Framework
- Russ Cox《Transparent Logs for Skeptical Clients》、RFC 6962、RFC 9162、Google Trillian 文档、Crosby-Wallach (2009)、AAD (CCS'19)
- HashiCorp Vault 文档（Transit/envelope/rewrap）、AWS KMS Prescriptive Guidance、The HLD Handbook《Secrets Management》、metaeye《Env Vars vs KMS vs Vault》
- Coinbase Agentic Wallets / AgentKit、Openfort《Agent Wallet Solutions》、Thirdweb/Turnkey
- LangGraph 官方文档（interrupts/persistence）+ LangChain HITL 博客 + 社区教程
- OWASP Business Logic Security Cheat Sheet、Action Limit Overrun、vibe-eval/ZeriFlow/FlowVerify（TOCTOU/idempotency）

> 所有引用内容均已改写或摘要以符合授权许可要求；单一来源连续引用不超过 30 词。详细原文见各 URL。
