# 05 · 生产升级：STH 签名与外部锚定

> **范围**：Phase 1 G10/G11 已上线"HMAC-SHA256 + 本地 JSONL 锚定"，本册回答两个生产前必须做的升级问题：(1) 把 HMAC 升级到非对称签名（Ed25519），(2) 把本地 JSONL 锚定升级到强不可抵赖介质（S3 Object Lock / git / blockchain）。
>
> 调研方法：交叉验证 RFC 6962 / C2SP transparency-log 标准 + 业界产品（TrustWarden / Tracehold / Kosli / VeritasChain）+ 官方 SDK（pyca/cryptography、HashiCorp Vault）。所有结论至少有 2 个独立来源支持，单一来源连续引用不超过 30 词。

---

## 5.1 STH 签名升级：HMAC-SHA256 → Ed25519

### 5.1.1 当前状态与差距

我们当前签名实现在 `app/services/audit_merkle_service.py`：

```python
def _sign_payload(tree_size, root_hash, signed_at_iso) -> str:
    payload = f"v1|{tree_size}|{root_hash}|{signed_at_iso}".encode()
    return hmac.new(_signing_key(), payload, hashlib.sha256).hexdigest()
```

**HMAC-SHA256 的局限**（多源确认）：
- **对称密钥**：验证方与签发方共享密钥；任何能验证的人都能伪造。
- **不可对外发布公钥**：第三方审计员要么持有签名密钥（信任全失），要么无法验证（信任陈述变空话）。
- **不符合 transparency log 标准**：RFC 6962 + C2SP `signed-note` v1.0 标准明确要求 Ed25519 / ECDSA P-256 等非对称签名（见 [C2SP signed-note](https://github.com/C2SP/C2SP/blob/main/signed-note.md)，签名类型 `0x01` = Ed25519）。

### 5.1.2 候选方案对比

| 方案 | 密钥长度 | 签名长度 | 速度 | 标准 | 推荐 |
|------|---------|---------|------|------|------|
| **Ed25519**（RFC 8032） | 32 字节 | 64 字节 | ~25k ops/sec（CPU） | C2SP `signed-note` 默认；CT v2/Sigsum 默认 | ✅ |
| ECDSA P-256 | 32 字节 | ~70 字节（DER）/ 64（IEEE） | ~10k ops/sec | C2SP 备选（`0x02`） | 🟡 仅 HSM/KMS 强制时 |
| RSA-PSS 2048 | 256 字节 | 256 字节 | ~1k ops/sec | TLS 兼容 | ❌ 不推荐 |
| HMAC-SHA256（现状） | 共享 | 32 字节 | 极快 | 仅适合内部完整性 | ❌ 升级 |

**Ed25519 优势**（RFC 8032 + pyca/cryptography 官方文档交叉验证）：
1. **确定性签名**：同密钥+同消息→恒定签名（HMAC 性质保留，PBT 测试可重放）。
2. **极小公钥**（32 字节）：可嵌入 `signed-note` 的 4 字节 key ID + verifier key（base64）公开发布。
3. **无 padding 选择**：相比 RSA-PSS 不需要协商 mgf/salt 长度，攻击面更小。
4. **官方 Python 支持**：`cryptography.hazmat.primitives.asymmetric.ed25519` 自 v2.6 起稳定（2018），后端使用 OpenSSL 实现。
5. **C2SP / Trillian / Sigstore / Sigsum 全栈默认**：未来要做"第三方 witness 共签"（split-view 攻击防护）必走 Ed25519 路径。

### 5.1.3 实施方案（向后兼容）

**核心设计**：保留 HMAC 路径作为开发/CI 默认，新增 Ed25519 路径作为生产推荐。`audit_tree_heads.signature` 字段加版本前缀（`hmac:` / `ed25519:`），数据库行支持混合存在。

```
signature 字段格式：
  hmac:<64-hex-char>           （现状，向后兼容）
  ed25519:<128-hex-char>       （新增，生产推荐）
```

**API 接入**：
- 配置新增 `AUDIT_STH_SIGNING_ALGO`（`hmac-sha256` / `ed25519`，默认 `hmac-sha256`）。
- 配置新增 `AUDIT_STH_ED25519_PRIVATE_KEY_PATH`（指向 PEM/Raw 32 字节文件）+ `AUDIT_STH_ED25519_PUBLIC_KEY_PATH`（公开发布给审计员）。
- 提供 `python -m app.scripts.generate_ed25519_key` 一次性生成密钥对脚本。
- 验证 API（`verify_sth_against_chain`）按签名前缀自动 dispatch。

**密钥生命周期**（多源最佳实践）：
- 私钥用 `cryptography.hazmat.primitives.serialization.PrivateFormat.PKCS8` + `BestAvailableEncryption(passphrase)` 落盘。
- 生产建议接入 KMS / HSM：AWS KMS 支持 Ed25519（2023 起）；本地 fallback 走文件方案，与 KEK provider 抽象同款。
- 公钥发布：暴露 `GET /api/audit/sth/verifier-key` 端点返回 C2SP `vkey` 格式 + 原始 hex，方便外部审计员独立验证。

### 5.1.4 后续路径（非阻塞，未来 Phase）

- **C2SP signed-note 格式**：把 STH 文本编码改为 `signed-note` v1.0（带 4 字节 key ID 的 base64 签名行），与公开 transparency log 网络互操作。
- **Witness cosignature**：参考 C2SP `tlog-cosignature` v1.0.1，让独立 witness 给 STH 加共签，防御 split-view 攻击（同 root_hash 给不同审计员看到不同 tree_size）。
- **后量子迁移**：Ed25519 不抗量子；2030 后逐步迁移到 CRYSTALS-Dilithium（NIST FIPS 204），现在的字段格式版本前缀已为这次迁移留好接口。

---

## 5.2 STH 外部锚定升级：本地 JSONL → S3 Object Lock / Git / 区块链

### 5.2.1 当前状态与差距

我们当前实现 `app/services/audit_sth_anchor.py` 把 STH 追加到本地 JSONL 文件（`AUDIT_STH_ANCHOR_PATH`）。问题：
- **本地文件可被同主机攻击者改写**：除非操作系统层加 immutable bit（`chattr +i`）或 append-only 挂载，否则与 DB 一样不安全。
- **不可分发**：审计员要拿到锚定证据必须有服务器访问权，与"零信任审计"理念相悖。

### 5.2.2 三个候选方案交叉对比

#### 方案 A：S3 Object Lock（Compliance 模式）✅ **首选生产方案**

**机制**：每条 STH 作为一个 S3 对象写入；桶启用 Object Lock + Versioning，retention=Compliance 模式后**任何用户（含 root）都无法在保留期内删除或覆盖**。

**多源确认**：
- AWS 官方文档明确 Compliance 模式下"对象不能被任何用户删除或修改，包括 root 账号"。
- TrustWarden Engineering 案例（QLDB 替代）选择 S3 Object Lock + DynamoDB 索引方案，原因是"hash chain 能检测篡改但不能阻止删除；S3 Object Lock 阻止删除本身"。
- OpenTelemetry 官方推荐 audit log 不可变管道默认走 S3 Object Lock + Compliance 7-year retention。
- Velt audit log 文章明确"hash chains + WORM 是 SaaS 等价 blockchain 不可抵赖性的成本可控方案"。

**优势**：
- 生产可用、企业合规友好（SOC 2 / HIPAA / SEC 17a-4 接受）。
- AWS 服务商保证：连 root 也删不了。
- 现成 Python SDK（boto3）一行 `put_object` 完成；retention 头通过 `ObjectLockMode='COMPLIANCE'` + `ObjectLockRetainUntilDate` 配置。

**劣势**：
- 锁死在 AWS（vendor lock-in）；多云方案需额外抽象。
- 成本：每 PUT 收费 + 存储 + 复制；中小规模忽略不计（< $1/月），大规模需评估。
- 仍需信任 AWS 服务（与"完全不信任服务方"的密码学愿景有差距）。

#### 方案 B：Git 仓库 + GitHub/Gitea append-only 分支 🟡 **开发友好备选**

**机制**：每条 STH 作为一行追加到 git 仓库（公开 GitHub repo / 私有 Gitea），用 `--no-ff` + 分支保护规则禁用 force-push / delete。

**多源确认**：
- Kosli 公开 blog 把整个 audit trail 实现为 git 仓库：理由是"git 用 Merkle 树天然 append-only + 跨语言开放标准"。
- C2SP transparency log 网络中已有项目用 GitHub release 作为 STH 锚点（如 sigstore root)。

**优势**：
- 开发体验极佳：`git clone` + `git log` 即可审计。
- 公开发布：审计员 / 评委直接看 GitHub commits，无需 AWS 账号。
- 跨语言兼容：git 是所有平台都能消费的标准格式。
- 成本：GitHub 公开 repo 免费；私有 repo $5/月起。

**劣势**：
- **GitHub 服务商可删 commit**：分支保护规则只防自己人，平台运营者总有最高权。需依赖 GitHub 的 Audit Log 反查删除事件（Enterprise 套餐才有）。
- 大规模写入慢：每秒一条 commit 不现实，需要 batch（如每分钟一次合并多条 STH 一次提交）。
- 公开 repo 暴露元数据（user_id 哈希 / passport_id 哈希），需要客户端做 pre-hash 防去匿名化。

#### 方案 C：区块链锚定（Bitcoin / Ethereum / Solana） 🟢 **最强不可抵赖但成本/复杂度最高**

**机制**：把当前 root_hash 通过 OP_RETURN（Bitcoin）/ event log（Ethereum）/ memo（Solana）写到公链。

**多源确认**：
- isaacsight/kbot-finance（capital markets audit infra 参考实现）路线图：v3 audit primitive 是"zk-STARK-verified compute against Goldsky subgraph"——明确把链上锚定列为最高保证级。
- Stamp.it、Stratumn、OpenTimestamps 等服务专门做"hash → Bitcoin OP_RETURN"批量锚定。

**优势**：
- 数学上最强的不可抵赖：连 AWS / GitHub 都打不过区块链共识。
- 公开可验证：任何人用 block explorer 都能查。

**劣势**：
- **成本**：Bitcoin 一笔 OP_RETURN ~$2-20（视手续费）；Ethereum L1 一笔 ~$5-50。每 5 分钟签一次 STH = $14k-360k/年，对 hackathon/MVP 不现实。
- **延迟**：BTC 6 个确认 ~60 分钟才"最终"上链。
- **聚合服务**：实践中用 OpenTimestamps 这类聚合服务，单 OP_RETURN 锚定一棵 Merkle 树承诺成千上万个 STH，分摊到几乎免费——但引入第三方信任。
- **政策风险**：部分司法辖区对区块链有限制；金融合规场景可能需要避免。

### 5.2.3 推荐分阶段路径

| 阶段 | 锚定介质 | 触发时机 | 配置开关 | 成本 |
|------|----------|----------|----------|------|
| **当前**（dev/CI） | 本地 JSONL | 每次签发 | `AUDIT_STH_ANCHOR_PATH=./.../sth.jsonl` | 0 |
| **Phase 2a**（staging/初期生产） | S3 Object Lock Compliance | 每次签发，立即 | `AUDIT_STH_ANCHOR_BACKEND=s3` + S3 凭证 | 几乎为零 |
| **Phase 2b**（透明度增强） | + 公开 git repo（每日 batch） | 后台任务，每日一次合并写入 | `AUDIT_STH_ANCHOR_BACKEND=s3+git` | $0-5/月 |
| **Phase 3**（高合规/链上承诺） | + OpenTimestamps Bitcoin 聚合 | 后台任务，每周一次 | `AUDIT_STH_ANCHOR_BACKEND=s3+git+ots` | $0-50/月 |

**核心架构原则**（与现有 `audit_sth_anchor.py` 完全对齐）：暴露统一 `AnchorBackend` ABC，子类实现 S3 / Git / OTS / JSONL，路由层 / scheduler 不感知具体后端——符合现有"接入点占位"设计哲学（与 `KeyProvider` ABC 同款）。

### 5.2.4 实施 PoC（可立即落地）

**1. 抽象出 `AnchorBackend` ABC**（小重构，不破坏现有 JSONL 路径）：
```python
class AnchorBackend(ABC):
    @abstractmethod
    def anchor(self, sth: AuditTreeHead) -> bool: ...

class JsonLineFileAnchorBackend(AnchorBackend):  # 现有实现包装
    ...

class S3ObjectLockAnchorBackend(AnchorBackend):  # 新增（生产可选）
    def __init__(self, bucket: str, retention_years: int = 7):
        ...
```

**2. S3 接入最小代码量**：约 40 行 + boto3 依赖；CI 用 `moto` mock S3 测试。

**3. 配置切换**：
```bash
# dev / CI（默认）
AUDIT_STH_ANCHOR_BACKEND=jsonl
AUDIT_STH_ANCHOR_PATH=/var/lib/htx-audit/sth.jsonl

# 生产
AUDIT_STH_ANCHOR_BACKEND=s3
AUDIT_STH_ANCHOR_S3_BUCKET=htx-passport-audit-anchor
AUDIT_STH_ANCHOR_S3_RETENTION_YEARS=7
```

**4. 风险与回滚**：S3 写入失败时（网络/凭证）由 `audit_sth_anchor` 的"失败静默"语义吞错，主路径不受影响——与现有设计完全一致。

---

## 5.3 立即可实施 vs 生产前实施 vs 选做

| 项 | 当前状态 | 推荐时机 |
|----|----------|----------|
| Ed25519 签名（保留 HMAC fallback） | 未实施 | ✅ **可立即实施**（hackathon/演示后即可） |
| `AnchorBackend` ABC 抽象 | 未实施 | ✅ **可立即实施**（重构,无新依赖） |
| S3 Object Lock backend | 未实施 | ⏳ **生产前**（需 AWS 账号） |
| Git append-only backend | 未实施 | ⏳ 选做（透明度增强） |
| OpenTimestamps backend | 未实施 | ⏳ 选做（高合规） |
| C2SP signed-note 互操作 | 未实施 | ⏳ 选做（接入公开 transparency log 时） |
| Witness 共签 | 未实施 | ⏳ 长期（多方信任场景） |

## 5.4 信息源

- C2SP signed-note v1.0（github.com/C2SP/C2SP/blob/main/signed-note.md）
- C2SP tlog-cosignature v1.0.1（github.com/C2SP/C2SP/blob/main/tlog-cosignature.md）
- C2SP tlog-tiles（github.com/C2SP/C2SP/blob/main/tlog-tiles.md）
- C2SP static-ct-api（github.com/C2SP/C2SP/blob/main/static-ct-api.md）
- RFC 6962 Certificate Transparency
- pyca/cryptography Ed25519 文档（cryptography.io/en/stable/hazmat/primitives/asymmetric/ed25519/）
- AWS S3 Object Lock 文档（docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html）
- TrustWarden Engineering: Why We Replaced QLDB with S3 Object Lock
- Tracehold: How to build immutable audit log with HMAC hash chaining
- Velt: Add Audit Trail to SaaS Product
- Kosli: Using Git for compliance audit trail
- OneUptime: Immutable audit log pipeline using OpenTelemetry
- VeritasChain: Ed25519 + Merkle Tree + UUIDv7 = Tamper-Proof Decision Logs
- HashiCorp Vault dynamic secrets（developer.hashicorp.com/vault/tutorials/db-credentials/database-secrets）

> 所有引用内容均已改写或摘要以符合授权许可要求，单一来源连续引用不超过 30 词。
