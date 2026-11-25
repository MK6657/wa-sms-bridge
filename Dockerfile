# ── Stage 1: 构建轻量前端静态资源（不进入最终运行镜像） ──────────
FROM node:22-alpine AS frontend-builder

WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ── Stage 2: 极简 Python 运行时（零 Node.js 进程与残留） ──────────
FROM python:3.11-slim

LABEL version="1.1.0"
LABEL description="SMS Hub (wa-sms-bridge) — 独立 SMS 接码核心与对外集成 API 服务"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY --from=frontend-builder /build/app/web_dist ./app/web_dist

RUN mkdir -p /data && adduser --disabled-password --gecos "" appuser && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 9090

ENV BRIDGE_DATA_DIR=/data
ENV LOG_LEVEL=INFO
ENV ACTIVE_PROVIDERS=smsbower
ENV DEFAULT_PROVIDER=smsbower
ENV ENABLE_WA_PLUGIN=false

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9090"]
