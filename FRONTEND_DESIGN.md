# SMS Hub 前端架构设计说明 (v1.1.0)

## 技术选型与资源控制

| 层级 | 技术 | 设计考量 |
|------|------|---------|
| 框架 | React 19 + TypeScript (strict) | 组件化高可维护性、全链路类型安全，无重型状态管理框架 |
| 样式 | Tailwind CSS v4 (`@tailwindcss/vite`) | 原子化高一致性样式，生产构建 CSS 仅 ~7.5KB gzip，无 AntD/MUI 膨胀 |
| 图标 | `lucide-react` | 按需 tree-shaking 图标 |
| 构建与托管 | Vite 6 + Docker 两阶段构建 | 输出纯静态资源至 `app/web_dist/`，由 FastAPI 单容器托管，**运行时零 Node 进程** |
| 实时通信 | 原生 `EventSource` (SSE) | 单条长连接 `/api/v1/events/stream` 替代原有 3s/10s/30s 多路轮询 |

## 页面结构（5 个聚焦视图）

1. **接码工作台 (`features/workbench/`)**：SMSBower 余额与今日 KPI 条 + 左侧快捷取号表单（服务胶囊/自定义服务码、统一 `CountryCombobox`、实时 `PriceTierMatrix`、默认折叠的高级抢号策略）+ 右侧实时接码卡片墙（`ActiveOrderCard`：手机号双格式复制、倒计时条、大字验证码高亮与提示音、重发/完成/取消退款/拉黑）。
2. **订单与统计 (`features/orders/`)**：近 7 日分接入项目与分业务统计 + 分页订单检索 + 历史验证码掩码查看/单条脱敏 + CSV 导出。
3. **API 接入中心 (`features/integrations/`)**：鉴权模式切换（宽松/严格）+ 项目级 API Key 签发与生命周期管理 + Webhook 测试 + 动态生成 Python / cURL / Node.js / SSE 示例代码 + Swagger `/docs` 直达。
4. **号码黑名单 (`features/blacklist/`)**：单条/批量添加、检索与移除。
5. **系统配置 (`features/settings/`)**：SMSBower 密钥配置与测连、验证码留存与掩码策略、归档平台与可选插件状态总览、服务端脱敏日志诊断。
