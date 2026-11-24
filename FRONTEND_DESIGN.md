# wa-sms-bridge 前端架构与设计说明

## 项目概述

wa-sms-bridge 是一个 WhatsApp 账号自动注册的编排工具。前端是一个单页应用（SPA），通过 FastAPI 后端代理转发到三个 SMS 平台（SMSBower、5sim、HeroSMS）和 wa-app（WhatsApp 注册服务）。

**定位**：内部运维工具，不面向公网，不要求登录鉴权。

---

## 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| HTML | 单页 `index.html`（264行, 15.8KB） | 所有结构、样式、内容都在一个文件 |
| CSS | 内联在 `<style>` 中（116行） | 无 CSS 预处理器，无框架 |
| JS | 独立 `app.js`（192行, 12.9KB） | 纯 Vanilla JS，无框架无依赖 |
| 后端 | FastAPI (`/api/*`) | 所有数据通过 AJAX fetch 获取 |
| 部署 | Python uvicorn 挂载静态文件 | `/api/*` 代理到后端 |

---

## 页面布局结构

```
┌──────────────────────────────────────────────────────────┐
│ Header: 📱 wa-sms-bridge  | 🟢 wa-app 已连接 | 💡 快速开始│
├──────────────────────────────────────────────────────────┤
│ Stats Bar: 余额 $4.92 | 运行中 0 | 已注册 5 | 今日 0 | 黑名单 0 │
├──────────┬───────────────────────────────────────────────┤
│ Sidebar  │ Content Area                                  │
│          │                                               │
│ 📋 任务  │ ┌─── Register Form (左 360px, always visible)┐│
│ 👤 账号  │ │ 平台 [▼SMSBower] [🔄] 国家 [🔍搜索...]  ││
│ 📜 历史  │ │ 价格$ 超时 次数 备用国家 代理            ││
│ 🔨 黑名单 │ │ [▶ 开始注册] [■停止] [📞测试] [🔄刷新] ││
│ ⚙️ 配置  │ └───────────────────────────────────────────┘│
│          │ ┌─── Current Status (right column) ─────────┐│
│          │ │ ✅ 就绪 / ⏳ 运行中 / ❌ 失败              ││
│          │ │ Log Box (terminal-style)                  ││
│          │ └───────────────────────────────────────────┘│
│          │ ┌─── Switchable Section (below) ────────────┐│
│          │ │ Tasks / Accounts / History / Blacklist / Config│
│          │ └───────────────────────────────────────────┘│
└──────────┴───────────────────────────────────────────────┘
```

---

## 各区域说明

### 1. Header（顶部栏）
- **左侧**：应用名 `📱 wa-sms-bridge`（"wa"字绿色高亮）
- **中间**：连接状态徽章（`status-badge` 绿色圆点表示 wa-app 在线）
- **右侧**：💡 快速开始按钮（toggle 引导面板）

### 2. Stats Bar（统计栏）
5 个等宽卡片，每个显示数字 + 标签：

| ID | 内容 | 数据来源 |
|----|------|---------|
| `s-balance` | 总余额（所有平台汇总） | `GET /api/provider/balance`（三个平台依次请求） |
| `s-running` | 运行中的任务数 | `GET /api/tasks` 过滤 status=running |
| `s-accounts` | 已注册账号数 | `GET /api/accounts` 返回的数组长度 |
| `s-today` | 今日成功注册数 | `GET /api/tasks` 过滤 today + completed |
| `s-blacklist` | 黑名单数 | `GET /api/blacklist` entries 长度 |

### 3. Sidebar（左侧边栏）
5 个按钮，点击切换下方 content section：
- 桌面端（>768px）：图标 + 文字标签
- 移动端（≤768px）：仅图标（48px 宽窄版）

**数据属性**：每个按钮有 `data-section="tasks|accounts|history|blacklist|config"`

### 4. Register Form（注册表单，始终可见）
左侧固定 360px 面板，包含：

| 字段 | ID | 类型 | 默认值 |
|------|----|------|--------|
| 短信平台 | `reg-provider` | `<select>` | SMSBower / 5sim / HeroSMS |
| 国家 | `reg-country-search` | 搜索式 input + 下拉 | 从 `GET /api/provider/countries` 动态加载 |
| 价格上限 $ | `reg-price` | `<input>` | 0.16 |
| 超时(秒) | `reg-timeout` | `<input>` | 180 |
| 尝试次数 | `reg-attempts` | `<input>` | 3 |
| 备用国家 | `reg-fallback` | `<input>` | 逗号分隔 ID |
| 代理 | `reg-proxy` | `<input>` | socks5://... |

**操作按钮**：开始注册(primary) / 停止 / 测试取号 / 刷新余额

### 5. Current Status（状态面板，始终可见）
右侧弹性区域，包含：
- **`#last-result`**：大号状态条（idle/running/ok/fail 四种样式）
- **Log Box**：终端风格日志，自动滚动到底部

### 6. Switchable Sections（可切换内容区）
侧边栏控制显示哪个 section，所有 section 共享相同位置：

| Section ID | 内容 | 数据来源 | 特殊功能 |
|------------|------|----------|----------|
| sec-tasks | 任务表格（7列） | `GET /api/tasks` | 搜索栏 + 状态筛选 |
| sec-accounts | 账号表格（4列） | `GET /api/accounts` | 只读列表 |
| sec-history | 历史表格（6列） | `GET /api/history` | 最近 100 条 |
| sec-blacklist | 黑名单表格（5列） | `GET /api/blacklist` | 搜索 + 平台筛选 + 移除按钮 |
| sec-config | 配置面板 | `GET /api/config` | wa-app 地址 + 3 平台 accordion |

---

## API 端点索引

所有 API 路径以 `/api/` 开头，由 `app/router_api.py` 处理：

| 端点 | 方法 | 用途 |
|------|------|------|
| `/api/config` | GET | 获取配置（Key 已掩码） |
| `/api/config` | PUT | 保存配置 |
| `/api/provider/balance` | GET | 查询指定平台余额（`?provider=xxx`） |
| `/api/provider/countries` | GET | 查询平台支持的国家列表 |
| `/api/provider/test-number` | POST | 测试取号 |
| `/api/countries/refresh` | POST | 同步国家列表 |
| `/api/register` | POST | 发起注册任务 |
| `/api/tasks` | GET | 获取所有任务列表 |
| `/api/task/{id}` | GET | 获取单个任务状态 |
| `/api/task/{id}/stop` | POST | 停止运行中的任务 |
| `/api/accounts` | GET | 获取已注册的 wa-app 账号 |
| `/api/history` | GET | 获取注册历史（含费用） |
| `/api/blacklist` | GET | 获取黑名单（支持 `?provider=` 过滤） |
| `/api/blacklist` | POST | 添加号码到黑名单 |
| `/api/blacklist?phone=&provider=` | DELETE | 从黑名单移除 |

---

## 颜色方案（当前）

| Token | 色值 | 用途 |
|-------|------|------|
| `--bg` | `#f5f0e1` | 页面背景（暖纸色） |
| `--card` | `#ffffff` | 卡片/面板背景 |
| `--border` | `#d4c9b4` | 边框（高辨识度） |
| `--green` | `#5b8c5a` | 强调色/成功状态 |
| `--green-light` | `#e8f0e6` | 悬停/高亮背景 |
| `--red` | `#c0392b` | 错误状态 |
| `--text` | `#333333` | 主文字色 |
| `--text-light` | `#888888` | 次要文字色 |

---

## JavaScript 模块架构

```
Helpers
├── $()        — document.getElementById 简写
├── esc()      — HTML 转义（XSS 防护）
├── log()      — 向日志框添加条目
├── fetchJSON() — fetch + JSON 解析（自动 catch null）
├── toggleGuide() — 切换引导面板

Sidebar
└── switchSection() — 切换侧边栏 + 加载对应数据

Init
└── init()     — 启动时并行加载 config/accounts/tasks

Data Loading
├── refreshBalance() — 轮询三个平台余额
├── updateStats()    — 更新统计卡片
├── loadTasks()      — 加载并渲染任务列表
├── loadAccounts()   — 加载并渲染账号列表
├── loadHistory()    — 加载注册历史
├── loadBlacklist()  — 加载黑名单（支持搜索/筛选）

Country Search
├── onProviderChange() — 平台切换时加载国家 + 余额
├── onCountrySearch()  — 国家搜索过滤
├── openCountryList()  — 打开下拉
├── closeCountryList() — 关闭下拉
├── renderDropdown()   — 渲染国家下拉列表
├── syncCountries()    — 一键同步国家数据

Registration
├── startRegister() — 提交注册请求
├── pollTask()      — 轮询任务状态（3s间隔，最长180s）
├── stopRegister()  — 停止运行中的任务
├── testNumber()    — 测试取号

Config
├── buildConfigPanels() — 动态生成配置面板（3个provider accordion）
├── saveConfig() — 保存 provider 配置（加密存储）
├── testProv()   — 测试 provider 连接
├── saveWaUrl()  — 保存 wa-app 地址
```

---

## 当前问题与设计局限性（供设计师参考）

### 布局局限性
1. **左侧 360px 固定** — 在 768-960px 区间挤压右侧状态面板
2. **日志框 max-height 200px** — 内容多时被截断，无展开按钮
3. **侧边栏只能切换 5 个 section** — 缺少仪表盘/概览首页
4. **移动端断点单一** — 仅 768px 一个断点，缺少 480px 手机专用优化

### 交互局限性
1. **无 Toast 通知系统** — 所有反馈只进日志框，容易遗漏
2. **无实时 WebSocket** — 任务状态靠前端 3 秒轮询
3. **表格无分页** — 任务/历史列表仅展示最新 100 条
4. **国家下拉无键盘导航** — 只能用鼠标选择
5. **停止任务无确认对话框**

### 功能缺失（已知）
1. **登录页/鉴权** — 没有用户认证（内部工具可接受）
2. **账号操作** — 无法手动注销/删除 wa-app 账号
3. **数据导出** — 无 CSV/JSON 导出功能
4. **批量注册** — 一次只能注册一个号码

### 前端技术债
1. **全局 inline onclick** — 不利于维护，应改为 addEventListener
2. **模板字面量生成的 HTML** — config panels 和表格都是 JS 拼接 HTML
3. **无前端构建工具** — 无打包、无 tree-shaking、无压缩

---

## 设计建议方向

1. **框架选择**：推荐 Vue 3 或 React（HMR 开发体验好）或保留 Vanilla + 组件化重构
2. **UI 组件库**：建议使用轻量级库避免臃肿（如 Naive UI / Ant Design 或 Tailwind CSS）
3. **实时性**：建议 WebSocket 推送任务状态替代轮询
4. **布局**：可以做三栏布局（侧边栏 → 主操作区 → 详情/日志侧栏）
5. **主题**：当前暖纸风可保留，建议出一套明/暗双主题
6. **国际化**：当前全中文，可考虑中英双语

---

## 文件清单

```
app/web/
├── index.html       # 主页面 (15.8KB, 264行)
├── app.js           # JavaScript 逻辑 (12.9KB, 192行)
└── style.css        # （可选，当前在 index.html 内联）
```

> 注：CSS 当前全部内联在 `index.html` 的 `<style>` 标签内（116行），可考虑提取到独立的 `style.css`。
