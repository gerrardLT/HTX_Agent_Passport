# 02 · 审计哈希链与密钥管理对比

> 对标：Certificate Transparency (RFC 6962/9162)、Google Trillian、Crosby-Wallach、HashiCorp Vault、AWS KMS、Coinbase/Openfort/Turnkey

## 2.1 审计哈希链：单链 vs Merkle 树

### 我们的方案
`event_hash = sha256(canonical_json + previous_event_hash + created_at_iso)`，按 `(user_id, passport_id)` 分链，首事件用 GENESIS_HASH。`verify_chain_integrity` 从 genesis 逐事件重算比对。

这是一个**单向链表式哈希链**（hash chain），类似区块链的"prev_hash"链接，但**不是 Merkle 树**。

### 业界标准：Certificate Transparency / Trillian 的 Merkle 树

CT（RFC 6962/9162）和 Google Trillian 用 **Merkle Hash Tree**，提供我们单链缺失的三大能力。来源：Russ Cox《Transparent Logs for Skeptical Clients》、RFC 6962、Trillian 文档。内容已改写以符合授权许可要求。

| 能力 | 单链哈希（我们） | Merkle 树（CT/Trillian） |
|------|-----------------|------------------------|
| **完整性验证成本** | O(N)——必须重算整条链 | O(log N)——只需 log N 个哈希 |
| **包含证明**（inclusion proof） | ❌ 无法证明"某事件在链中"而不下载全链 | ✅ O(log N) 证明 |
| **一致性证明**（consistency proof） | ❌ 无法高效证明"新链是旧链的追加扩展" | ✅ O(log N) 证明追加性 |
| **不信任服务器的验证** | ❌ 必须信任服务器返回了完整链 | ✅ "skeptical client"可独立验证，服务器无法在不被发现的情况下删除/篡改条目 |
| **第三方审计** | 🟡 评委必须拿到全部事件才能验 | ✅ auditor 可异步遍历，monitor 可 gossip 比对 STH 检测分叉 |

### Crosby-Wallach 的关键洞察（2009）
> "在基于日志的 PKI 中，一个毁灭性攻击仍然可能：恶意 CA 在日志中发布伪造证书，但之后与日志服务器串通把它移除，使受害者永远无法检测到攻击。透明日志因此应当证明它保持 append-only。"
> 内容已改写以符合授权许可要求。

**对我们的直接威胁**：我们的单链如果由**同一个数据库服务器**存储和验证，一个能写数据库的攻击者（或恶意 DBA）可以：
1. 删除中间某段事件
2. 用新的 prev_hash 重新计算后续所有事件的 hash
3. 重新串成一条"看起来完整"的链

我们的 `verify_chain_integrity` **会通过**这条被重写的链——因为它只检查"链内部自洽"，无法检测"整条链被替换"。CT 通过**已签名的 tree head（STH）+ gossip 协议**让多方持有的 root hash 互相比对来防御这点，我们没有。

### 严重性评估
- **对黑客松/Demo**：单链足够——评委只需看到"事件链自洽 + 篡改单个事件会被检测"，我们的 PBT 已验证这点。
- **对生产**：🟠 单链不满足"不可信服务器下的可验证审计"。如果要对外宣称"可审计、防篡改"，单链是**夸大**——它只能防"篡改后不重算"的低级攻击，防不住"删除+全链重写"。

### 改进建议
1. **低成本**：定期把链尾的 `event_hash`（相当于一个 checkpoint）**对外发布/签名**（如写入一个 append-only 的外部存储、或用服务私钥签名后存证），让"全链重写"留下不一致痕迹。
2. **中成本**：升级为 Merkle 树。可直接用 **Google Trillian**（gRPC 服务 + MySQL 后端，生产级），把每个审计事件作为 leaf，获得 inclusion/consistency proof + STH 签名。审计重放界面可展示 inclusion proof，真正做到"评委不信任服务器也能验证"。
3. **高成本（P2）**：若要去中心化存证，把周期性 STH 上链（与 PRD 中 sHTX 链上声誉徽章 P1 项可合并）。

## 2.2 密钥管理：app 层加密 vs Vault/KMS/TEE —— 🔴 最严重的差距

### 我们的方案
`CredentialVault`：AES-256-GCM，主密钥（`VAULT_MASTER_KEY`）从**环境变量**读取，32 字节 hex。加密 HTX API key/secret 后以 BYTEA 存 PostgreSQL。

### 业界明确判定：这是反模式

**Openfort《Agent Wallet Solutions for Developers》（2026-04）直接点名：**
> "把密钥存在环境变量或 Postgres 列里，默认就是 custodial（托管）——这意味着货币转移牌照（money transmitter licensing）风险，意味着法律工作量远超任何 agent wallet 厂商的成本……保护平台免于托管风险的安全模型——TEE 隔离密钥、远程证明、签名边界的策略执行——确实很难自建，也确实容易做错。"
> 内容已改写以符合授权许可要求。

**The HLD Handbook《Secrets Management》（2026-05）：**
> "硬编码在配置文件和环境变量里的密钥是灾难性泄露的头号原因：Uber 2016 年 5700 万用户记录泄露始于一个提交到私有 GitHub 仓库的 AWS 凭证……环境变量通过 /proc/<pid>/environ 泄露给任何同 UID 进程，出现在崩溃转储里，脚本 set -x 时被打进 CI 日志。"
> 内容已改写以符合授权许可要求。

### 我们方案的具体问题

| 问题 | 我们的现状 | 业界标准 |
|------|-----------|---------|
| **主密钥存储** | 环境变量明文（进程内存、可能进崩溃转储/日志） | KMS/HSM，KEK 永不离开 HSM 边界（AWS KMS FIPS 140-3 L3） |
| **密钥轮换** | ❌ 无机制——轮换 = 重新加密全部凭证 + 重部署 | Vault Transit `rewrap`：KEK 轮换只需 re-wrap DEK，不重加密数据；密文前缀带 key 版本 |
| **envelope encryption** | ❌ 单层（主密钥直接加密数据） | 两层：KEK（HSM 内）加密 per-record DEK，DEK 明文只在内存存活微秒 |
| **per-secret 审计** | 🟡 有 CREDENTIAL_* 审计事件 | Vault secret-level 审计日志（谁在何时读了哪个 secret） |
| **动态/短时凭证** | ❌ 长期静态凭证 | Vault dynamic secrets：按需生成、TTL 自动过期、可即时撤销 |
| **撤销** | 🟡 软删除（DB 标记） | KMS 禁用 key / Vault 即时 revoke lease |

### 加密货币 agent 场景的特殊性

Coinbase Agentic Wallets / Openfort / Turnkey 已为"AI agent 持有交易凭证"这一**精确场景**建立了标准：

- **Coinbase Agentic Wallets**：私钥在 Coinbase TEE（Trusted Execution Environment）内，**永不暴露给 agent 的 prompt 或 LLM**；programmable spending limits（session caps / transaction limits）；内置 KYT 合规筛查。内容已改写以符合授权许可要求。
- **Openfort / Turnkey**：TEE 隔离 operator key + 远程证明（remote attestation）让第三方可验证策略确实在执行；on-chain session keys 把"允许的合约/方法/限额/时间窗"编码进智能账户，执行时由合约强制。
- 三种策略执行模型：on-chain（session keys）、off-chain API、TEE-bound。

**对我们的启示**：我们已经做对了一半——"密钥不进 prompt/LLM"（Req 15 AC1-2）。但密钥**存储**层用 app 层加密 + 环境变量主密钥，远低于该场景的行业标准。HTX 是中心化交易所（CEX），用的是 API key（非链上私钥），所以 TEE/MPC 不是强制——但 **KMS envelope encryption + 密钥轮换**是底线。

### 改进建议（按优先级）

1. **🔴 必做（生产前）**：主密钥从环境变量迁移到 **KMS**（AWS KMS / GCP KMS / Vault Transit）。改造为 **envelope encryption**：用 KMS `GenerateDataKey` 产生 per-credential DEK，KMS KEK 加密 DEK，明文 DEK 只在内存。收益：KEK 永不落地、可轮换、有 key 使用审计、合规（FIPS 140-3）。
2. **🟠 高**：实现密钥轮换流程（参考 Vault Transit rewrap 模式：密文带 key 版本前缀，后台 job 渐进 re-wrap）。
3. **🟡 中**：API key 验证后考虑用 HTX 的 IP 白名单 + 只读/交易权限分离（我们已强制 `permission_withdraw=false`，方向正确）。
4. **文档**：在 README/安全说明中**诚实标注**当前密钥管理的威胁模型边界（目前是"开发/演示级"，生产需 KMS）。

## 2.3 本章差距小结

| # | 差距 | 严重性 | 证据来源 |
|---|------|--------|---------|
| G7 | 主密钥存环境变量，非 KMS/HSM；业界判定为 custodial 反模式 | 🔴 严重 | Openfort、HLD Handbook、AWS Prescriptive Guidance |
| G8 | 无 envelope encryption（单层加密） | 🔴 严重 | AWS Secrets Manager、Vault Transit |
| G9 | 无密钥轮换机制 | 🟠 高 | Vault rewrap、CircleCI 2023 事件 |
| G10 | 审计单链无法生成包含/一致性证明，无法支撑不信任服务器的审计 | 🟠 高 | RFC 6962、Trillian、Crosby-Wallach |
| G11 | 审计链可被"删除+全链重写"绕过（无外部 STH/签名锚点） | 🟠 高 | Crosby-Wallach append-only 攻击 |
| G12 | 无动态/短时凭证、无 secret-level 审计 | 🟢 低（CEX 场景非强制） | Vault dynamic secrets |
