# =============================================================================
# HTX Agent Passport — 后端 Dockerfile（生产部署）
# Python 3.11-slim + uvicorn（单 worker 即可，搭配 nginx 做反向代理）
# =============================================================================

FROM python:3.11-slim AS base

# 防止 Python 缓冲 stdout/stderr（确保日志实时可见）
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 系统依赖（psycopg 需要 libpq）
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq5 curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先复制依赖文件，利用 Docker 缓存层
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 非 root 用户运行
RUN addgroup --system appgroup && \
    adduser --system --ingroup appgroup appuser && \
    chown -R appuser:appgroup /app
USER appuser

EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# 入口脚本：自动执行 alembic 迁移，然后启动服务
ENTRYPOINT ["/entrypoint.sh"]

# 启动：4 workers + 绑定所有接口
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--log-level", "info"]
