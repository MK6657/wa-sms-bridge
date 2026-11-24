# wa-sms-bridge

v0.2.0 短信平台桥接与任务控制台，基于 Python 和 FastAPI。支持 SMSBower、5sim 和 HeroSMS，通过兼容的 `wa-app` HTTP API 处理任务。

## Docker 部署

先启动兼容的 `wa-app`，并在它的配置中设置 `WA_APP_AUTH_PASSWORD`。下面的桥接配置通过 `selfhosted-wa` 网络访问 `http://wa-app:8080`。

- 配套源码可参考包含验证码提交与失败清理接口的 [`wa-app` 提交 `62bff6e`](https://github.com/pood1e/wa-app/tree/62bff6e545c6b6ac36bc73aad59d4062cf73bf86)。两套服务的 API 要匹配。
- 如果 `wa-app` 容器尚未加入共享网络，先用 `docker network create selfhosted-wa` 创建网络，再用 `docker network connect --alias wa-app selfhosted-wa wa-app` 接入。末尾的 `wa-app` 是容器名，按实际名称填写。若要连接已有的远端服务，在 `.env` 中改填容器可访问的 `WA_APP_URL`，同时保留 Compose 所需的共享网络。

```powershell
git clone https://github.com/MK6657/wa-sms-bridge.git
cd wa-sms-bridge
Copy-Item .env.example .env
```

编辑 `.env`，为 `BRIDGE_SECRET_KEY` 设置长期随机值，并把 `WA_APP_API_PASSWORD` 设为配套服务的 `WA_APP_AUTH_PASSWORD`。

```powershell
docker compose up -d --build
```

浏览器访问 [http://127.0.0.1:9099](http://127.0.0.1:9099)，在控制台填写短信平台配置。健康检查路径为 `/api/health`。

## 数据与测试

数据库、平台配置和任务记录保存在 `wa-sms-bridge-data` 数据卷。迁移数据库时保留原有 `BRIDGE_SECRET_KEY`，用于解密平台凭据。

```powershell
python -m pip install -r requirements.txt pytest pytest-asyncio
python -m pytest -q
```
