# SMS Hub (`wa-sms-bridge`) v1.1.0

## v1.1.0 更新亮点

- **常驻可折叠侧边栏与自适应容器布局**：侧边栏全视口常驻左侧，新增一键折叠/展开导轨模式（记忆偏好 + 收起态悬浮提示与实时徽标），主内容区采用 CSS Container Queries 自适应不同宽度，彻底消除中等视口遮罩弹窗与双栏挤压。
- **全自定义高层次交互控件**：以带语义徽标、状态圆点、统计计数及自动向上防裁切定位的 `CustomSelect` 全面替换原生系统 `<select>`，统一升级开关、密码显隐、预设快捷标签与分层卡片视觉体系。
- **全链路细节加固**：完善上游 `complete` 永久拒绝状态分类、强制刷新报价代数并发保护、多线路国家检索、跨前缀黑名单匹配（`+E.164` / 纯数字）及 API 权限与长度边界校验。

## v1.0.0 核心架构亮点

- **独立接码核心引擎 (`OrderService`)**：彻底从 `wa-app` 解耦，新增完整 `sms_orders` / `sms_messages` 接码订单状态机，支持任意服务取号、实时显示与一键复制验证码、重发短信 (`status=3`)、确认完成 (`status=6`)、取消退款 (`status=8`)、一键拉黑与超时自动退款。
- **SMSBower 单活优化 & 旧平台保留**：默认仅激活 `smsbower`（新增 30 分钟国家缓存与 10 秒 Singleflight 价格缓存），保留 `5sim` / `HeroSMS` 代码且默认不初始化（零连接与报错噪音），日后按 `ACTIVE_PROVIDERS` 即可恢复测试。
- **标准对外 API v1 (`/api/v1/*`)**：统一结构化错误码、OpenAPI 3.1 文档、幂等取号 (`Idempotency-Key`)、项目级 API Key 鉴权与分项目统计、`/wait` 长轮询、SSE 实时推码与 HMAC-SHA256 Webhook 回调。
- **验证码留存与隐私策略**：活跃期明文展示，落盘 Fernet 加密，历史订单支持默认掩码、按时长自动脱敏清理与一键手动清理，日志文件自动脱敏。
- **可选业务插件机制**：原有 `wa-app` 自动注册编排迁入 `app/plugins/whatsapp_wa/` 并默认关闭 (`ENABLE_WA_PLUGIN=false`)，独立部署不再依赖 `wa-app`。
- **全新轻量现代前端**：React 19 + TypeScript + Vite + Tailwind CSS v4 组件化重构，Docker 多阶段构建产出的纯静态资源由 FastAPI 单容器托管，零运行时 Node 进程、以单连接 SSE 替代高频轮询。
