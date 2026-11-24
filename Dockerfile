FROM python:3.11-slim

LABEL version="0.2.0"
LABEL description="wa-sms-bridge — WhatsApp 注册桥接服务"

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY . .

# 使用非 root 用户运行
RUN mkdir -p /data && adduser --disabled-password --gecos "" appuser && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 9090

ENV BRIDGE_DATA_DIR=/data
ENV LOG_LEVEL=INFO

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9090"]
