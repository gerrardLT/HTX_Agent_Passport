# HTX Agent Passport — Backend

FastAPI + Python 3.11+ 实现的后端服务。负责凭证保险库、Passport 注册中心、
Policy Engine、审批流、执行网关、审计哈希链、TTS 语音合成代理等核心组件。

## 目录结构

```
backend/
├── main.py                  # FastAPI 入口（挂载路由 + /static 静态文件服务）
├── pyproject.toml           # 项目元数据 + 依赖声明
├── requirements.txt         # 运行时依赖（含 volcengine-audio websockets）
├── requirements-dev.txt     # 开发与测试依赖
├── Dockerfile               # 生产 Docker 镜像（python:3.11-slim）
├── entrypoint.sh            # 入口脚本（alembic upgrade head 再启动 uvicorn）
├── alembic.ini              # Alembic 迁移配置
├── alembic/                 # 迁移脚本目录
└── app/
    ├── core/                # 配置、加密、审计哈希链工具
    ├── models/              # SQLAlchemy ORM 模型（8 张表）
    ├── schemas/             # Pydantic / JSON Schema 校验
    ├── services/            # 业务服务（Policy Engine、Approval、Vault…）
    └── routers/             # FastAPI 路由
        ├── auth.py          #   登录鉴权
        ├── credentials.py   #   凭证管理
        ├── passports.py     #   Passport CRUD
        ├── actions.py       #   Action 详情与审计
        ├── approvals.py     #   审批流
        ├── audit.py         #   审计链 + STH + Inclusion Proof
        ├── demo.py          #   预设演示场景
        ├── tts.py           #   火山引擎 TTS 代理（/api/tts/synthesize + /batch）
        └── ws.py            #   WebSocket 实时推送
└── tests/
    ├── conftest.py          # pytest fixtures
    ├── unit/                # L1 单元测试 + PBT
    ├── integration/         # L2 集成测试
    └── eval/                # L3 Eval 套件
```

## 本地开发

```bash
# 1. 创建虚拟环境
python -m venv .venv
.\.venv\Scripts\activate   # Windows
# source .venv/bin/activate  # Linux / macOS

# 2. 安装依赖
pip install -r requirements-dev.txt
# 或基于 PEP 621
pip install -e ".[dev]"

# 3. 启动 PostgreSQL（仓库根目录的 docker-compose.yml）
docker-compose up -d postgres

# 4. 准备环境变量
cp ../.env.example ../.env
# 编辑 .env 填入真实值（VAULT_MASTER_KEY 必须为 32 字节 hex）

# 5. 运行数据库迁移并启动服务
alembic upgrade head
uvicorn main:app --reload --port 8000

# 6. 运行测试
pytest                       # 全量
pytest tests/unit            # L1 单元
pytest -m pbt                # 仅属性测试
pytest --cov=app             # 覆盖率
```

## TTS 语音合成代理

`/api/tts/` 路由为产品演示页面提供火山引擎豆包语音合成代理，避免浏览器直接请求时的 CORS 限制和 API Key 泄露。

| 端点 | 说明 |
|------|------|
| `POST /api/tts/synthesize` | 单段合成，返回 base64 MP3 |
| `POST /api/tts/batch` | 批量串行合成，确保全程人声一致 |
| `GET /static/product-demo-video.html` | 产品演示页面（静态文件服务） |

配置项（`backend/.env`）：

```
VOLCENGINE_TTS_API_KEY=<火山引擎控制台获取>
VOLCENGINE_TTS_VOICE_ID=zh_female_vv_uranus_bigtts
```

不配置 API Key 时，演示页面自动降级到浏览器内置 Web Speech API。

## 架构对应

各组件实现位置参考 design.md「组件与接口」章节。
方法论遵循《代理意图判断与连续执行方法论》四层架构：感知 → 决策 → 执行 → 反馈。
# HTX Agent Passport — Backend

FastAPI + Python 3.11+ 实现的后端服务。负责凭证保险库、Passport 注册中心、
Policy Engine、审批流、执行网关、审计哈希链等核心组件。

## 目录结构

```
backend/
├── main.py                  # FastAPI 入口
├── pyproject.toml           # 项目元数据 + 依赖声明
├── requirements.txt         # 运行时依赖
├── requirements-dev.txt     # 开发与测试依赖
├── alembic.ini              # Alembic 迁移配置
├── alembic/                 # 迁移脚本目录（任务 2 实现）
└── app/
    ├── core/                # 配置、加密、审计哈希链工具
    ├── models/              # SQLAlchemy ORM 模型
    ├── schemas/             # Pydantic / JSON Schema 校验
    ├── services/            # 业务服务（Policy Engine、Approval、Vault…）
    └── routers/             # FastAPI 路由
└── tests/
    ├── conftest.py          # pytest fixtures
    ├── unit/                # L1 单元测试 + PBT
    ├── integration/         # L2 集成测试
    └── eval/                # L3 Eval 套件
```

## 本地开发

> 任务 1 仅创建脚手架，**不执行依赖安装**。下列命令在后续任务中按需运行。

```bash
# 1. 创建虚拟环境
python -m venv .venv
.\.venv\Scripts\activate   # Windows
# source .venv/bin/activate  # Linux / macOS

# 2. 安装依赖
pip install -r requirements-dev.txt
# 或基于 PEP 621
pip install -e ".[dev]"

# 3. 启动 PostgreSQL（仓库根目录的 docker-compose.yml）
docker-compose up -d postgres

# 4. 准备环境变量
cp ../.env.example ../.env
# 编辑 .env 填入真实值（VAULT_MASTER_KEY 必须为 32 字节 hex）

# 5. 启动服务
uvicorn main:app --reload --port 8000

# 6. 运行测试
pytest                       # 全量
pytest tests/unit            # L1 单元
pytest -m pbt                # 仅属性测试
pytest --cov=app             # 覆盖率
```

## 架构对应

各组件实现位置参考 design.md「Components and Interfaces」章节。
方法论遵循《Agent 意图判断与连续执行方法论》四层架构：感知 → 决策 → 执行 → 反馈。
