# CI/CD 配置说明

本文档说明 HTX Agent Passport 的 GitHub Actions CI/CD 流水线所需的配置，包括 GitHub Secrets、Variables、`production` 受保护环境，以及生产服务器侧的预置约定。在首次启用流水线（`.github/workflows/ci.yml` 与 `.github/workflows/deploy.yml`）前，请按本文档完成全部配置。

> 仓库镜像命名空间：`ghcr.io/gerrardLT/HTX_Agent_Passport-backend` 与 `ghcr.io/gerrardLT/HTX_Agent_Passport-frontend`。

---

## 1. Secrets 与 Variables 清单

下表列出流水线运行所需的全部密钥（Secrets）与变量（Variables），含类型、作用域与用途。**Secrets 经 `secrets.*` 引用后由 GitHub Actions 自动遮蔽日志输出；工作流中严禁以明文 `echo` 暴露其值。**

| 名称 | 类型 | 作用域 | 用途 |
| --- | --- | --- | --- |
| `GITHUB_TOKEN` | 内置 token | 作业级（自动提供） | CI 侧向 GHCR 推送镜像的认证凭据，配合作业 `permissions: packages: write`。无需手动创建。 |
| `SSH_HOST` | Secret | `production` 环境 | 生产服务器地址（主机名或 IP）。 |
| `SSH_USER` | Secret | `production` 环境 | SSH 登录用户名。 |
| `SSH_KEY` | Secret | `production` 环境 | 用于连接生产服务器的 SSH 私钥（PEM 格式）。 |
| `GHCR_USER` | Secret | `production` 环境 | 生产服务器侧登录 GHCR 拉取镜像的用户名（GitHub 用户名）。 |
| `GHCR_TOKEN` | Secret | `production` 环境 | 生产服务器侧拉取镜像所用的 token，仅需 **只读 packages（`read:packages`）** 权限。 |
| `VAULT_MASTER_KEY` | Secret | `production` 环境 | 凭证保险库主密钥，注入 `.env.prod` / 后端容器。 |
| `JWT_SECRET` | Secret | `production` 环境 | JWT 签发密钥。 |
| `DATABASE_URL` | Secret | `production` 环境 | Neon 托管 PostgreSQL 完整连接串（`postgresql+psycopg://用户:密码@主机/库?sslmode=require`），注入后端容器。数据库为外部托管，compose 内不再运行本地 PostgreSQL 容器。 |
| `NEXT_PUBLIC_EXECUTION_MODE` | Variable | 仓库 / 环境 | 前端构建参数（非敏感），构建期注入前端镜像。 |

### 配置位置

- **Secrets / Variables 入口**：仓库 → `Settings` → `Secrets and variables` → `Actions`。
  - Secrets 在 `Secrets` 标签页添加；Variables 在 `Variables` 标签页添加。
- **作用域说明**：
  - 标注「`production` 环境」的 Secret 应配置为**环境级 Secret**（见第 2 节在 `production` 环境内添加），而非仓库级，以确保仅 `deploy` 作业在 `production` 环境上下文中可读取，符合权限最小化原则。
  - `NEXT_PUBLIC_EXECUTION_MODE` 为非敏感构建参数，可配置为**仓库级 Variable**；若不同环境需不同取值，也可配置为环境级 Variable。
  - `GITHUB_TOKEN` 由 GitHub Actions 在每次运行时自动注入，**无需手动创建**；`build-push` 作业通过声明 `permissions: { contents: read, packages: write }` 获得 GHCR 推送权限。

### `GHCR_TOKEN` 创建要点（只读拉取）

生产服务器只需**拉取**镜像，因此 `GHCR_TOKEN` 应使用最小权限：

1. 在 GitHub → `Settings` → `Developer settings` → `Personal access tokens` 创建 token。
2. 仅授予 `read:packages` 权限（不要授予 `write:packages` 或 `delete:packages`）。
3. 将生成的 token 填入 `production` 环境的 `GHCR_TOKEN` Secret，`GHCR_USER` 填对应 GitHub 用户名。
4. 若镜像所属仓库为私有，请确保该用户对 `gerrardLT/HTX_Agent_Passport` 的 packages 具有读取访问权。

---

## 2. `production` 受保护环境配置

`deploy` 作业声明 `environment: production`，因此所有部署相关 Secret 应配置在该环境内，并启用必需审批者以防未授权部署。

### 2.1 创建 `production` 环境

1. 进入仓库 → `Settings` → `Environments`。
2. 点击 `New environment`，名称输入 `production`，保存。

### 2.2 配置必需审批者（Required reviewers）

1. 在 `production` 环境详情页，勾选 `Required reviewers`。
2. 添加 1 名或多名审批者（用户或团队）。当 `deploy` 作业触发时，作业会进入 `pending`（待审批）状态，**只有在所需审批完成后才会执行任何部署步骤**（满足 Requirement 5.3）。
3. （可选）配置 `Wait timer`、`Deployment branches and tags`（建议限制为仅 `main` 分支可部署），进一步收紧部署来源。

### 2.3 在 `production` 环境内添加环境级 Secrets

在 `production` 环境详情页的 `Environment secrets` 区域，逐一添加以下 Secret：

- `SSH_HOST`、`SSH_USER`、`SSH_KEY`
- `GHCR_USER`、`GHCR_TOKEN`
- `VAULT_MASTER_KEY`、`JWT_SECRET`、`DATABASE_URL`

> 这些 Secret 仅在运行于 `production` 环境上下文的 `deploy` 作业中可见，`ci.yml` 与 `build-push` 作业无法读取，符合权限最小化原则。

---

## 3. 生成并配置 SSH 部署密钥（`SSH_KEY` / `authorized_keys`）

部署作业需要自动 SSH 登录生产服务器执行命令。出于安全，使用**密钥对**而非密码登录。

### 概念

`ssh-keygen` 生成一对配对文件，类似「锁」与「钥匙」：

| 文件 | 角色 | 放置位置 |
| --- | --- | --- |
| `htx_deploy.pub`（公钥） | 公开的「锁」，可公开 | 生产服务器的 `~/.ssh/authorized_keys` |
| `htx_deploy`（私钥） | 绝密的「钥匙」，绝不外泄 | GitHub `production` 环境的 `SSH_KEY` Secret |

原理：服务器装上公钥后，持有配对私钥的一方（GitHub Actions）即可免密登录。`authorized_keys` 是服务器上「允许哪些密钥登录」的清单。

> ⚠️ 私钥（`htx_deploy`）绝不能提交到代码仓库或发送给他人，仅放入 GitHub Secret（加密保管）。

### 3.1 在本地生成密钥对（Windows PowerShell）

```powershell
ssh-keygen -t ed25519 -C "github-deploy" -f $HOME\.ssh\htx_deploy -N '""'
```

生成两个文件：
- `htx_deploy`（私钥）
- `htx_deploy.pub`（公钥）

> `-N '""'` 表示不设密码短语（GitHub Actions 无法交互输入密码短语，故部署密钥不设 passphrase）。

### 3.2 把公钥追加到生产服务器

先用现有方式登录生产服务器，然后执行（将 `<公钥内容>` 替换为 `htx_deploy.pub` 文件里的那一整行）：

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
echo "<公钥内容>" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

查看公钥内容（在本地 PowerShell 执行后复制输出）：

```powershell
Get-Content $HOME\.ssh\htx_deploy.pub
```

### 3.3 把私钥粘贴到 `SSH_KEY` Secret

查看私钥的**完整内容**（含 `-----BEGIN ...` 到 `... END-----` 全部行）：

```powershell
Get-Content $HOME\.ssh\htx_deploy
```

将输出全文粘贴到 GitHub `production` 环境的 `SSH_KEY` Secret。

### 3.4 验证免密登录

```powershell
ssh -i $HOME\.ssh\htx_deploy <SSH_USER>@<SSH_HOST>
```

能直接登录（不提示输入密码）即配置成功。`SSH_HOST` / `SSH_USER` 即第 1 节中对应 Secret 的值。

---

## 4. 生产服务器 `.env.prod` 预置约定

部署脚本以 `docker compose --env-file .env.prod -f docker-compose.prod.yml ...` 运行，依赖生产服务器上预置的 `.env.prod` 文件。约定如下：

1. **由运维预置、不随仓库分发**：`.env.prod` 存放于生产服务器的部署目录中，包含敏感配置，**不提交到代码仓库**（已在 `.gitignore` 覆盖）。其密钥取值与 `production` 环境中的同名 Secret 保持一致（如 `VAULT_MASTER_KEY`、`JWT_SECRET`、`DATABASE_URL`）。
2. **镜像标签由 `IMAGE_TAG` 控制**：`docker-compose.prod.yml` 中 `backend` / `frontend` 服务引用 `ghcr.io/gerrardLT/HTX_Agent_Passport-{backend,frontend}:${IMAGE_TAG:-latest}`。部署时部署脚本会 `export IMAGE_TAG=<本次提交 SHA>` 以精确锁定本次构建的镜像；`.env.prod` 无需固定 `IMAGE_TAG`（缺省回退到 `latest`）。
3. **必备键**：`.env.prod` 至少应包含后端运行所需的环境变量（`DATABASE_URL`（Neon 连接串）、`VAULT_MASTER_KEY`、`JWT_SECRET` 等），具体键参照仓库根目录 `.env.example`。数据库为 Neon 托管，无需 `POSTGRES_USER/PASSWORD/DB` 等本地数据库变量。
4. **权限收紧**：建议将 `.env.prod` 权限设为 `600`（仅属主可读写），归属于执行部署的 SSH 用户。

### GHCR 只读拉取要求

生产服务器在拉取镜像前需登录 GHCR（部署脚本执行 `echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_USER}" --password-stdin`）：

- 使用第 1 节所述的**只读（`read:packages`）** token，避免在生产主机上保留可写凭据。
- 登录成功后由 `docker compose pull backend frontend` 拉取本次 `IMAGE_TAG` 对应镜像。
- 若 GHCR 登录或拉取失败，部署脚本在 `set -euo pipefail` 下终止，`deploy` 作业随之失败并保留日志供排查。

---

## 5. 日志遮蔽与安全约定（Requirement 5.4）

- 所有敏感值必须经 `secrets.*` 引用，GitHub Actions 会在作业日志中自动以 `***` 遮蔽。
- 工作流与远端部署脚本中**严禁**以明文 `echo`、`cat` 或调试输出打印任何 Secret 值。
- GHCR 登录统一使用 `--password-stdin` 方式传入 token，避免凭据出现在进程参数或命令历史中。

---

## 6. 配置检查清单

启用流水线前，确认以下项均已完成：

- [ ] 已在 `production` 环境内添加 `SSH_HOST`/`SSH_USER`/`SSH_KEY`/`GHCR_USER`/`GHCR_TOKEN`/`VAULT_MASTER_KEY`/`JWT_SECRET`/`DATABASE_URL`。
- [ ] 已生成 SSH 部署密钥对，公钥已写入服务器 `~/.ssh/authorized_keys`，私钥已粘入 `SSH_KEY` Secret，并验证可免密登录。
- [ ] 已添加仓库（或环境）Variable `NEXT_PUBLIC_EXECUTION_MODE`。
- [ ] `production` 环境已启用 `Required reviewers` 并指定审批者。
- [ ] （建议）`production` 环境的部署分支限制为 `main`。
- [ ] `GHCR_TOKEN` 仅具备 `read:packages` 权限。
- [ ] 生产服务器已预置 `.env.prod`（权限 `600`），键值与 Secrets 一致。
- [ ] 生产服务器可使用 `GHCR_USER`/`GHCR_TOKEN` 成功登录并拉取 GHCR 镜像。
