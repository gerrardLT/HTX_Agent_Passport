"""HTX Agent Passport 后端业务包。

子包划分（详见 design.md「Components and Interfaces」）：

- ``core``      —— 配置、日志、加密、审计哈希链等基础工具
- ``models``    —— SQLAlchemy ORM 模型（任务 2.1）
- ``schemas``   —— Pydantic / JSON Schema（Policy DSL v0、ActionPlan v0）
- ``services``  —— 业务服务（Policy Engine、Vault、Approval、Executor 等）
- ``routers``   —— FastAPI 路由层

任务 1 仅放置占位符；具体实现由后续任务填充。
"""
