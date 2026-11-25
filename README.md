# SMS Hub (`wa-sms-bridge` v1.0.0)

独立、轻量、低耦合的 SMS 接码中心与标准对外 API 服务（基于 Python FastAPI + SQLite WAL + React/TypeScript 静态工作台）。

- **当前主活平台**：`SMSBower`（支持实时价档与库存查询、价格策略取号、接收并展示验证码、重发短信 `status=3`、完成 `status=6`、取消退款 `status=8`）。
- **旧平台保留**：`5sim`、`HeroSMS` 代码完整保留并默认休眠（零连接与报错噪音），日后在 `ACTIVE_PROVIDERS` 中加入即可启用测试。
- **面向后续项目接入**：提供标准 `/api/v1/*` REST API、OpenAPI 3.1 文档 (`/docs`)、分项目原子幂等取号 (`Idempotency-Key`)、项目级 API Key 鉴权与租户数据隔离、长轮询 `/wait`、支持 `Last-Event-ID` 回放的 SSE 实时流以及持久化 Outbox Webhook 回调（带 SSRF 防护与 HMAC 签名）。
- **原有 `wa-app` 解耦**：降级为默认关闭的可选插件（`ENABLE_WA_PLUGIN=false`），主体接码服务无需 `wa-app` 即可独立运行。

---

## 快速启动（独立模式）

### 方式一：Docker Compose（推荐，多阶段构建单容器）

```powershell
Copy-Item .env.example .env
# 1. 设置 BRIDGE_SECRET_KEY（长期固定随机串，用于加密 API Key 与验证码）
# 2. 设置 CONSOLE_ADMIN_PASSWORD（容器经端口映射发布时，服务端看到的是网桥地址而非回环地址，
#    宿主机浏览器登录管理台必须配置管理员口令）
docker compose up -d --build
```

浏览器打开 [http://127.0.0.1:9099](http://127.0.0.1:9099)，使用 `.env` 中配置的 `CONSOLE_ADMIN_PASSWORD` 登录接码工作台；Swagger API 文档位于 [http://127.0.0.1:9099/docs](http://127.0.0.1:9099/docs)。

### 方式二：本机直接运行（开发/调试）

```powershell
# 1. 构建前端静态包（已预构建于 app/web_dist/，修改前端后执行）
cd frontend
npm install
npm run build
cd ..

# 2. 启动后端（FastAPI 直接托管前端静态包）
$env:BRIDGE_SECRET_KEY="dev-local-secret-key"
$env:BRIDGE_DATA_DIR="./run_data"
python -m uvicorn app.main:app --host 127.0.0.1 --port 9099
```

---

## v0.2.0 升级与 WA 插件启用指南

1. **保留数据与密钥**：升级时保持原有 `wa-sms-bridge-data` 数据卷与原 `BRIDGE_SECRET_KEY` 不变，启动时自动无损执行 Schema v2 前向迁移（保留已加密的 SMSBower API Key、黑名单与旧历史记录）。
2. **启用原有 `wa-app` 自动注册插件**：
   - 使用 `compose.wa.yaml` 前，先确保外部 Docker 网络 `selfhosted-wa` 已存在并接入 `wa-app` 容器：
     ```powershell
     docker network create selfhosted-wa
     docker network connect --alias wa-app selfhosted-wa wa-app
     ```
   - 在 `.env` 中填写 `WA_APP_API_PASSWORD`，如需兼容 v0.2.0 的 `/api/tasks`、`/api/register` 等旧路径，设置 `ENABLE_LEGACY_API=true`（所有旧路径同样受管理员会话/令牌保护）：
     ```powershell
     docker compose -f compose.yaml -f compose.wa.yaml up -d --build
     ```
3. **旧接口 → v1 接口迁移对照**：

| v0.2.0 旧接口 | v1.0.0 对应接口 | 说明 |
|--------------|----------------|------|
| `GET /api/health` | `GET /api/v1/health`（保留 `/api/health`） | DB 异常返回 HTTP 503；存活探针 `/api/v1/health/live` |
| `POST /api/provider/test-number` | `POST /api/v1/orders` + `/cancel` | 完整订单状态机与激活对账 |
| `GET /api/provider/balance` | `GET /api/v1/providers/{provider}/balance` | 仅限已激活平台 |
| `GET /api/provider/countries` | `GET /api/v1/providers/{provider}/countries` | 内置 30 分钟缓存 |
| `GET /api/provider/prices` | `GET /api/v1/providers/{provider}/prices` | 内置 10 秒 Singleflight 缓存 |
| `POST /api/register` / `GET /api/tasks` | `/api/v1/plugins/whatsapp_wa/*` | 需启用 `ENABLE_WA_PLUGIN=true` 且具备管理员或 `wa:r/rw` 权限 |
| `GET/POST/DELETE /api/blacklist` | `GET/POST/DELETE /api/v1/blacklist` | 按项目隔离（管理员可管理全局名单） |

---

## 安全配置说明

- **管理台认证**：未设置 `CONSOLE_ADMIN_PASSWORD` 时，仅允许本机直连回环（无任何 `X-Forwarded-*` 代理头）初始化免密会话；**任何非回环或反代部署均必须配置 `CONSOLE_ADMIN_PASSWORD`（或 `CONSOLE_ADMIN_TOKEN`）**。
- **Webhook 出站防护**：默认仅允许公网 `https://` 目标并阻断回环/私有网段/云元数据 IP 及 3xx 重定向；内部回调接收方需在 `WEBHOOK_ALLOWED_HOSTS` 中显式授权。
- **强制加密**：验证码与 Webhook 签名密钥均依赖 `BRIDGE_SECRET_KEY` 加密落盘，绝不静默降级为明文。

---

## 测试

```powershell
python -m pip install -r requirements.txt pytest pytest-asyncio
python -m pytest -v
```
