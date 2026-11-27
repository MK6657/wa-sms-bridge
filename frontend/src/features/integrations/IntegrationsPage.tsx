import { useCallback, useEffect, useState } from "react";
import {
  BookOpen,
  Check,
  Code2,
  Edit3,
  ExternalLink,
  Eye,
  EyeOff,
  KeyRound,
  Plus,
  RefreshCw,
  RotateCw,
  Send,
  Shield,
  Trash2,
  Webhook,
} from "lucide-react";
import { api } from "../../api/client";
import type { ProjectApiKey, WebhookDeliveryItem } from "../../api/types";
import { ConfirmModal } from "../../components/ConfirmModal";
import { CopyButton } from "../../components/CopyButton";
import { CustomSelect, type SelectOption } from "../../components/CustomSelect";
import { useToast } from "../../components/ToastProvider";
import { formatDateTime, formatFinancialSummary } from "../../utils/format";

type CodeTab = "curl" | "python" | "node" | "sse";

const SCOPE_META: Array<{ id: string; label: string }> = [
  { id: "orders:rw", label: "订单读写" },
  { id: "prices:r", label: "价格查询" },
  { id: "blacklist:rw", label: "私有黑名单" },
  { id: "events:r", label: "SSE 实时流" },
];

const DELIVERY_STATUS_OPTIONS: SelectOption[] = [
  { value: "", label: "全部投递状态" },
  { value: "delivered", label: "已送达 (delivered)", dotColor: "bg-emerald-500" },
  { value: "pending", label: "等待重试 (pending)", dotColor: "bg-amber-500" },
  { value: "delivering", label: "投递中 (delivering)", dotColor: "bg-sky-500" },
  { value: "failed", label: "投递失败 (failed)", dotColor: "bg-rose-500" },
  { value: "scrubbed", label: "已脱敏取消 (scrubbed)", dotColor: "bg-slate-400" },
];

export function IntegrationsPage({ active = true }: { active?: boolean }) {
  const { notify } = useToast();
  const [keys, setKeys] = useState<ProjectApiKey[]>([]);
  const [authRequired, setAuthRequired] = useState(false);
  const [loading, setLoading] = useState(false);

  // 新建项目表单
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [newProjectId, setNewProjectId] = useState("");
  const [newWebhookUrl, setNewWebhookUrl] = useState("");
  const [newWebhookSecret, setNewWebhookSecret] = useState("");
  const [showNewSecret, setShowNewSecret] = useState(false);
  const [creating, setCreating] = useState(false);
  const [justCreatedKey, setJustCreatedKey] = useState<{ project_id: string; name: string; api_key: string } | null>(null);

  // 编辑项目回调与权限
  const [editingItem, setEditingItem] = useState<ProjectApiKey | null>(null);
  const [editName, setEditName] = useState("");
  const [editWebhookUrl, setEditWebhookUrl] = useState("");
  const [editWebhookSecret, setEditWebhookSecret] = useState("");
  const [clearEditSecret, setClearEditSecret] = useState(false);
  const [showEditSecret, setShowEditSecret] = useState(false);
  const [editScopes, setEditScopes] = useState<string[]>([]);
  const [initialCustomScopes, setInitialCustomScopes] = useState<string[]>([]);
  const [savingEdit, setSavingEdit] = useState(false);
  const [togglingId, setTogglingId] = useState("");

  // 轮转与删除确认
  const [rotateTarget, setRotateTarget] = useState<ProjectApiKey | null>(null);
  const [rotating, setRotating] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<ProjectApiKey | null>(null);
  const [deleting, setDeleting] = useState(false);

  // Webhook Outbox 投递日志
  const [deliveries, setDeliveries] = useState<WebhookDeliveryItem[]>([]);
  const [deliveryStatus, setDeliveryStatus] = useState("");
  const [loadingDeliveries, setLoadingDeliveries] = useState(false);

  const [codeTab, setCodeTab] = useState<CodeTab>("python");
  const [testingWebhookId, setTestingWebhookId] = useState("");

  const origin = typeof window !== "undefined" ? window.location.origin : "http://127.0.0.1:9099";

  const loadDeliveries = useCallback(async () => {
    setLoadingDeliveries(true);
    try {
      const res = await api.listWebhookDeliveries({ status: deliveryStatus || undefined, limit: 30 });
      setDeliveries(res.items);
    } catch {
      // ignore initial error
    } finally {
      setLoadingDeliveries(false);
    }
  }, [deliveryStatus]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.listProjectKeys();
      setKeys(res.items);
      setAuthRequired(res.api_auth_required);
    } catch (err) {
      notify(err instanceof Error ? err.message : "加载接入项目失败", "error");
    } finally {
      setLoading(false);
    }
  }, [notify]);

  useEffect(() => {
    if (active) {
      load();
    } else {
      setJustCreatedKey(null);
    }
  }, [active, load]);

  useEffect(() => {
    if (active) {
      loadDeliveries();
    }
  }, [active, loadDeliveries]);

  const toggleAuthMode = async () => {
    const next = !authRequired;
    try {
      await api.updateSettings({ api_auth_required: next });
      setAuthRequired(next);
      notify(next ? "已开启严格 API Key 鉴权模式" : "已切换为宽松模式（本机免密 + 带 Key 记账）", "success");
    } catch (err) {
      notify(err instanceof Error ? err.message : "切换鉴权模式失败", "error");
    }
  };

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newName.trim()) {
      notify("请输入接入项目名称", "warn");
      return;
    }
    setCreating(true);
    try {
      const created = await api.createProjectKey({
        name: newName.trim(),
        project_id: newProjectId.trim() || undefined,
        default_webhook_url: newWebhookUrl.trim() || undefined,
        webhook_secret: newWebhookSecret.trim() || undefined,
      });
      setJustCreatedKey({
        project_id: created.project_id,
        name: created.name,
        api_key: created.api_key,
      });
      setNewName("");
      setNewProjectId("");
      setNewWebhookUrl("");
      setNewWebhookSecret("");
      setShowCreate(false);
      notify(`已签发项目密钥：${created.name}`, "success");
      load();
    } catch (err) {
      notify(err instanceof Error ? err.message : "创建密钥失败", "error");
    } finally {
      setCreating(false);
    }
  };

  const handleToggleEnabled = async (item: ProjectApiKey) => {
    if (togglingId === item.project_id) return;
    setTogglingId(item.project_id);
    try {
      await api.updateProjectKey(item.project_id, { enabled: !item.enabled });
      notify(item.enabled ? `已停用 ${item.name}` : `已启用 ${item.name}`, "info");
      load();
    } catch (err) {
      notify(err instanceof Error ? err.message : "更新状态失败", "error");
    } finally {
      setTogglingId("");
    }
  };

  const startEdit = (item: ProjectApiKey) => {
    setShowCreate(false);
    setEditingItem(item);
    setEditName(item.name);
    setEditWebhookUrl(item.default_webhook_url || "");
    setEditWebhookSecret("");
    setClearEditSecret(false);
    setShowEditSecret(false);
    const currentScopes = Array.isArray(item.scopes) ? [...item.scopes] : SCOPE_META.map((s) => s.id);
    setEditScopes(currentScopes);
    setInitialCustomScopes(currentScopes.filter((sc) => !SCOPE_META.some((m) => m.id === sc)));
  };

  const handleSaveEdit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!editingItem) return;
    if (!editName.trim()) {
      notify("项目名称不能为空", "warn");
      return;
    }
    setSavingEdit(true);
    try {
      const payload: {
        name: string;
        default_webhook_url: string;
        scopes: string[];
        webhook_secret?: string;
      } = {
        name: editName.trim(),
        default_webhook_url: editWebhookUrl.trim(),
        scopes: editScopes,
      };
      if (clearEditSecret) {
        payload.webhook_secret = "";
      } else if (editWebhookSecret.trim()) {
        payload.webhook_secret = editWebhookSecret.trim();
      }
      await api.updateProjectKey(editingItem.project_id, payload);
      notify(`已更新项目「${editName.trim()}」配置`, "success");
      setEditingItem(null);
      load();
    } catch (err) {
      notify(err instanceof Error ? err.message : "保存项目配置失败", "error");
    } finally {
      setSavingEdit(false);
    }
  };

  const handleRotate = async () => {
    if (!rotateTarget) return;
    setRotating(true);
    try {
      const rotated = await api.rotateProjectKey(rotateTarget.project_id);
      setJustCreatedKey({
        project_id: rotated.project_id,
        name: rotated.name,
        api_key: rotated.api_key,
      });
      notify(`已轮转「${rotated.name}」密钥，旧密钥及旧 SSE 长连接已立即作废`, "success");
      setRotateTarget(null);
      load();
    } catch (err) {
      notify(err instanceof Error ? err.message : "轮转密钥失败", "error");
    } finally {
      setRotating(false);
    }
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      const deletedId = deleteTarget.project_id;
      await api.deleteProjectKey(deletedId);
      if (justCreatedKey?.project_id === deletedId) {
        setJustCreatedKey(null);
      }
      notify(`已删除项目密钥 ${deleteTarget.name}`, "success");
      setDeleteTarget(null);
      load();
    } catch (err) {
      notify(err instanceof Error ? err.message : "删除失败", "error");
    } finally {
      setDeleting(false);
    }
  };

  const handleTestWebhook = async (item: ProjectApiKey) => {
    if (!item.default_webhook_url) {
      notify("该项目尚未配置 Webhook URL", "warn");
      return;
    }
    setTestingWebhookId(item.project_id);
    try {
      const res = await api.testWebhook({ url: item.default_webhook_url, project_id: item.project_id });
      if (res.ok) {
        notify(`Webhook 测试成功 (HTTP ${res.status_code})`, "success");
      } else {
        notify(`Webhook 测试失败: ${res.error || `HTTP ${res.status_code}`}`, "error");
      }
    } catch (err) {
      notify(err instanceof Error ? err.message : "Webhook 测试异常", "error");
    } finally {
      setTestingWebhookId("");
    }
  };

  const sampleKey = justCreatedKey?.api_key || "sms_k_your_project_api_key";

  const snippets: Record<CodeTab, string> = {
    python: `import httpx, uuid

BASE = "${origin}"
HEADERS = {"X-API-Key": "${sampleKey}"}

with httpx.Client(base_url=BASE, headers=HEADERS, timeout=110) as client:
    # 1. 取号（带 Idempotency-Key 防止重试重复扣费）
    order_resp = client.post(
        "/api/v1/orders",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={
            "service": "apple",          # 支持 whatsapp / telegram / google / apple / openai / 原生服务码
            "country": "Thailand",       # 支持中文 / 英文 / ISO / SMSBower ID
            "wait_timeout": 180,
            "external_ref": "job-001",
            "strategy": {"target_price": 0.15, "price_float_enabled": True, "price_float_percent": 10},
        },
    )
    order_resp.raise_for_status()
    order = order_resp.json()
    print("拿到手机号:", order["phone_e164"])

    # 2. 长轮询等码（收到验证码瞬间返回，无须手写循环）
    wait_resp = client.get(f"/api/v1/orders/{order['order_id']}/wait", params={"timeout": 90})
    wait_resp.raise_for_status()
    res = wait_resp.json()
    if res["latest_code"]:
        print("收到验证码:", res["latest_code"])
        client.post(f"/api/v1/orders/{order['order_id']}/complete").raise_for_status()
    else:
        # 没收到或号码不可用：取消退款并可选拉黑
        client.post(f"/api/v1/orders/{order['order_id']}/cancel", json={"blacklist": False}).raise_for_status()`,

    curl: `# 1. 幂等取号
curl -X POST "${origin}/api/v1/orders" \\
  -H "Content-Type: application/json" \\
  -H "X-API-Key: ${sampleKey}" \\
  -H "Idempotency-Key: req-2026-0001" \\
  -d '{"service":"whatsapp","country":"Thailand","wait_timeout":180}'

# 2. 长轮询等待验证码（最长阻塞 60s）
curl "${origin}/api/v1/orders/<ORDER_ID>/wait?timeout=60" \\
  -H "X-API-Key: ${sampleKey}"

# 3. 确认完成（或 /resend 重发短信、/cancel 取消退款）
curl -X POST "${origin}/api/v1/orders/<ORDER_ID>/complete" \\
  -H "X-API-Key: ${sampleKey}"`,

    node: `const BASE = "${origin}";
const headers = {
  "Content-Type": "application/json",
  "X-API-Key": "${sampleKey}",
};
const parseJson = async (r) => {
  if (!r.ok) throw new Error(\`HTTP \${r.status}: \${await r.text()}\`);
  return r.json();
};

// 1. 取号
const order = await fetch(\`\${BASE}/api/v1/orders\`, {
  method: "POST",
  headers: { ...headers, "Idempotency-Key": crypto.randomUUID() },
  body: JSON.stringify({ service: "google", country: "52", wait_timeout: 180 }),
}).then(parseJson);

console.log("手机号:", order.phone_e164);

// 2. 等待验证码
const result = await fetch(\`\${BASE}/api/v1/orders/\${order.order_id}/wait?timeout=90\`, {
  headers,
}).then(parseJson);

if (result.latest_code) {
  console.log("验证码:", result.latest_code);
  await fetch(\`\${BASE}/api/v1/orders/\${order.order_id}/complete\`, { method: "POST", headers }).then(parseJson);
}`,

    sse: `// SSE 实时监听全部（或单个）订单的验证码事件
const es = new EventSource(
  "${origin}/api/v1/events/stream?api_key=${sampleKey}"
);

es.addEventListener("order.code_received", (ev) => {
  const payload = JSON.parse(ev.data);
  console.log("实时验证码到达:", payload.order_id, payload.data.latest_code);
});

es.addEventListener("order.expired", (ev) => {
  const payload = JSON.parse(ev.data);
  console.log("订单超时已自动退款:", payload.order_id);
});`,
  };

  return (
    <div className="space-y-5">
      {/* 顶部鉴权开关与 Swagger 入口 */}
      <div className="grid gap-4 @4xl:grid-cols-[minmax(0,1fr)_300px]">
        <section className="surface-card flex flex-wrap items-center justify-between gap-4 rounded-2xl p-5">
          <div className="flex min-w-0 flex-1 items-start gap-3">
            <div className="rounded-xl bg-indigo-50 p-2.5 text-indigo-600 ring-1 ring-inset ring-indigo-200/70">
              <Shield className="h-5 w-5" />
            </div>
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="text-sm font-bold text-slate-900">对外 API 鉴权策略</h2>
                <span
                  className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ring-1 ring-inset ${
                    authRequired
                      ? "bg-indigo-50 text-indigo-700 ring-indigo-200"
                      : "bg-emerald-50 text-emerald-700 ring-emerald-200"
                  }`}
                >
                  {authRequired ? "严格鉴权已开启" : "本机宽松调试"}
                </span>
              </div>
              <p className="mt-1 text-xs leading-relaxed text-slate-500">
                {authRequired
                  ? "外部调用 /api/v1/* 必须携带合法项目 X-API-Key 或 Bearer Token（Web 工作台不受影响）。"
                  : "本机回环可直接免密调试；携带项目 API Key 时自动按项目归属隔离订单并独立记账。"}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={toggleAuthMode}
            className={`shrink-0 cursor-pointer rounded-xl px-4 py-2 text-xs font-semibold transition ${
              authRequired
                ? "border border-slate-200 bg-white text-slate-700 shadow-2xs hover:bg-slate-50"
                : "bg-indigo-600 text-white shadow-xs hover:bg-indigo-700"
            }`}
          >
            {authRequired ? "切回宽松模式" : "开启严格鉴权"}
          </button>
        </section>

        <section className="surface-card flex flex-col justify-between rounded-2xl p-5">
          <div className="flex items-center gap-2 text-sm font-bold text-slate-900">
            <BookOpen className="h-4 w-4 text-emerald-600" />
            <span>OpenAPI 3.1 交互文档</span>
          </div>
          <p className="mt-1 text-xs text-slate-500">
            查看完整参数 Schema、错误码定义并直接在线试用接口。
          </p>
          <a
            href="/docs"
            target="_blank"
            rel="noreferrer"
            className="mt-3 inline-flex items-center justify-center gap-1.5 rounded-xl bg-slate-900 px-3.5 py-2 text-xs font-semibold text-white transition hover:bg-slate-800"
          >
            <span>打开 Swagger 文档 (/docs)</span>
            <ExternalLink className="h-3.5 w-3.5" />
          </a>
        </section>
      </div>

      {/* 新创建或轮转密钥的一次性明文提示框 */}
      {justCreatedKey ? (
        <div className="rounded-2xl border border-emerald-300 bg-emerald-50 p-4 shadow-xs">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="min-w-0 flex-1">
              <div className="text-xs font-semibold text-emerald-800">
                项目「{justCreatedKey.name}」({justCreatedKey.project_id}) 密钥已生成 — 明文仅此一次展示，请立即保存！
              </div>
              <div className="font-mono-code mt-1.5 break-all rounded-lg bg-white px-3 py-2 text-sm font-bold text-emerald-950 ring-1 ring-emerald-200">
                {justCreatedKey.api_key}
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <CopyButton value={justCreatedKey.api_key} label="复制密钥" variant="emerald" size="md" />
              <button
                type="button"
                onClick={() => setJustCreatedKey(null)}
                className="cursor-pointer rounded-lg border border-emerald-200 bg-white px-3 py-1.5 text-xs font-medium text-emerald-800 hover:bg-emerald-100"
              >
                我已保存
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {/* 项目 API Key 列表 */}
      <section className="surface-card rounded-2xl">
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-t-2xl border-b border-slate-200/75 bg-slate-50/70 p-4">
          <div className="flex items-center gap-2">
            <KeyRound className="h-4 w-4 text-indigo-600" />
            <h2 className="text-sm font-bold text-slate-900">已接入项目与密钥</h2>
            <span className="font-mono-code rounded-full bg-slate-200/80 px-2 py-0.5 text-xs font-semibold text-slate-700">
              {keys.length}
            </span>
          </div>
          <button
            type="button"
            onClick={() => {
              setEditingItem(null);
              setShowCreate((v) => !v);
            }}
            className="inline-flex cursor-pointer items-center gap-1.5 rounded-xl bg-indigo-600 px-3.5 py-2 text-xs font-semibold text-white shadow-xs transition hover:bg-indigo-700"
          >
            <Plus className="h-3.5 w-3.5" />
            <span>新建接入项目密钥</span>
          </button>
        </div>

        {showCreate ? (
          <form onSubmit={handleCreate} className="border-b border-slate-200/75 bg-slate-50/60 p-4">
            <div className="grid gap-3 @2xl:grid-cols-2">
              <div>
                <label className="mb-1 block text-xs font-medium text-slate-700">项目名称 *</label>
                <input
                  type="text"
                  required
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  placeholder="例如：Telegram 注册机 / Google 自动化"
                  className="input-surface w-full rounded-xl px-3 py-2 text-xs"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-slate-700">项目 ID（可选）</label>
                <input
                  type="text"
                  value={newProjectId}
                  onChange={(e) => setNewProjectId(e.target.value)}
                  placeholder="留空自动生成，如 tg_bot"
                  className="input-surface font-mono-code w-full rounded-xl px-3 py-2 text-xs"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-slate-700">默认 Webhook URL（可选 HTTPS）</label>
                <input
                  type="url"
                  value={newWebhookUrl}
                  onChange={(e) => setNewWebhookUrl(e.target.value)}
                  placeholder="https://hooks.example.com/sms"
                  className="input-surface font-mono-code w-full rounded-xl px-3 py-2 text-xs"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-slate-700">Webhook 签名密钥（可选 HMAC-SHA256）</label>
                <div className="relative">
                  <input
                    type={showNewSecret ? "text" : "password"}
                    value={newWebhookSecret}
                    onChange={(e) => setNewWebhookSecret(e.target.value)}
                    placeholder="用于校验 X-SMS-Signature 请求头"
                    className="input-surface font-mono-code w-full rounded-xl py-2 pl-3 pr-9 text-xs"
                  />
                  <button
                    type="button"
                    onClick={() => setShowNewSecret((v) => !v)}
                    className="absolute right-2.5 top-1/2 -translate-y-1/2 cursor-pointer rounded p-0.5 text-slate-400 hover:text-slate-700"
                  >
                    {showNewSecret ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                  </button>
                </div>
              </div>
            </div>
            <div className="mt-3 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setShowCreate(false)}
                className="cursor-pointer rounded-xl border border-slate-200 bg-white px-3.5 py-2 text-xs text-slate-600 hover:bg-slate-50"
              >
                取消
              </button>
              <button
                type="submit"
                disabled={creating}
                className="cursor-pointer rounded-xl bg-indigo-600 px-4 py-2 text-xs font-semibold text-white hover:bg-indigo-700 disabled:opacity-50"
              >
                {creating ? "生成中..." : "生成项目 API Key"}
              </button>
            </div>
          </form>
        ) : null}

        {editingItem ? (
          <form onSubmit={handleSaveEdit} className="border-b border-indigo-100 bg-indigo-50/45 p-4">
            <div className="mb-2.5 text-xs font-semibold text-indigo-950">
              编辑项目「{editingItem.name}」({editingItem.project_id})
            </div>
            <div className="grid gap-3 @3xl:grid-cols-3">
              <div>
                <label className="mb-1 block text-xs font-medium text-slate-700">项目名称</label>
                <input
                  type="text"
                  required
                  value={editName}
                  onChange={(e) => setEditName(e.target.value)}
                  className="input-surface w-full rounded-xl px-3 py-2 text-xs"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-slate-700">默认 Webhook URL（留空清空）</label>
                <input
                  type="url"
                  value={editWebhookUrl}
                  onChange={(e) => setEditWebhookUrl(e.target.value)}
                  placeholder="https://hooks.example.com/sms"
                  className="input-surface font-mono-code w-full rounded-xl px-3 py-2 text-xs"
                />
              </div>
              <div>
                <div className="mb-1 flex items-center justify-between gap-2">
                  <label className="block text-xs font-medium text-slate-700">更换签名密钥（留空保持不变）</label>
                  {editingItem.has_webhook_secret ? (
                    <button
                      type="button"
                      onClick={() => {
                        setClearEditSecret((prev) => {
                          const next = !prev;
                          if (next) setEditWebhookSecret("");
                          return next;
                        });
                      }}
                      className={`cursor-pointer rounded px-1.5 py-0.5 text-[11px] font-medium transition ${
                        clearEditSecret
                          ? "bg-rose-100 text-rose-700 ring-1 ring-inset ring-rose-300"
                          : "text-slate-500 hover:bg-slate-100 hover:text-rose-600"
                      }`}
                    >
                      {clearEditSecret ? "保存时将清除密钥" : "清除已设密钥"}
                    </button>
                  ) : null}
                </div>
                <div className="relative">
                  <input
                    type={showEditSecret ? "text" : "password"}
                    value={editWebhookSecret}
                    disabled={clearEditSecret}
                    onChange={(e) => {
                      setEditWebhookSecret(e.target.value);
                      if (e.target.value.trim()) setClearEditSecret(false);
                    }}
                    placeholder={
                      clearEditSecret
                        ? "保存后将清空已设签名密钥"
                        : editingItem.has_webhook_secret
                          ? "已设置签名密钥，留空保持不变"
                          : "可选 HMAC-SHA256 密钥"
                    }
                    className="input-surface font-mono-code w-full rounded-xl py-2 pl-3 pr-9 text-xs disabled:bg-slate-100 disabled:text-slate-400"
                  />
                  <button
                    type="button"
                    onClick={() => setShowEditSecret((v) => !v)}
                    className="absolute right-2.5 top-1/2 -translate-y-1/2 cursor-pointer rounded p-0.5 text-slate-400 hover:text-slate-700"
                  >
                    {showEditSecret ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                  </button>
                </div>
              </div>
            </div>
            <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap items-center gap-1.5 text-xs">
                <span className="font-medium text-slate-600">接口权限：</span>
                {[
                  ...SCOPE_META,
                  ...Array.from(new Set([...initialCustomScopes, ...editScopes]))
                    .filter((sc) => !SCOPE_META.some((m) => m.id === sc))
                    .map((sc) => ({ id: sc, label: "自定义权限" })),
                ].map((sc) => {
                  const checked = editScopes.includes(sc.id);
                  return (
                    <button
                      key={sc.id}
                      type="button"
                      onClick={() => {
                        if (checked) {
                          setEditScopes((prev) => prev.filter((x) => x !== sc.id));
                        } else {
                          setEditScopes((prev) => [...new Set([...prev, sc.id])]);
                        }
                      }}
                      className={`inline-flex cursor-pointer items-center gap-1 rounded-lg border px-2.5 py-1 text-[11px] font-medium transition ${
                        checked
                          ? "border-emerald-500 bg-emerald-50 text-emerald-800 shadow-2xs"
                          : "border-slate-200 bg-white text-slate-500 hover:bg-slate-50"
                      }`}
                    >
                      {checked ? <Check className="h-3 w-3 text-emerald-600" /> : null}
                      <span>{sc.label}</span>
                      <span className="font-mono-code text-[10px] opacity-70">({sc.id})</span>
                    </button>
                  );
                })}
              </div>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => setEditingItem(null)}
                  className="cursor-pointer rounded-xl border border-slate-200 bg-white px-3 py-1.5 text-xs text-slate-600 hover:bg-slate-50"
                >
                  取消
                </button>
                <button
                  type="submit"
                  disabled={savingEdit}
                  className="cursor-pointer rounded-xl bg-indigo-600 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-indigo-700 disabled:opacity-50"
                >
                  {savingEdit ? "保存中..." : "保存更改"}
                </button>
              </div>
            </div>
          </form>
        ) : null}

        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] border-collapse text-left text-xs">
            <thead>
              <tr className="border-b border-slate-100 bg-slate-50/50 text-slate-500">
                <th className="px-4 py-3 font-medium">项目 / 权限</th>
                <th className="px-4 py-3 font-medium">密钥前缀</th>
                <th className="px-4 py-3 font-medium">默认 Webhook</th>
                <th className="px-4 py-3 font-medium">累计调用 / 消费</th>
                <th className="px-4 py-3 font-medium">最近调用</th>
                <th className="px-4 py-3 font-medium">状态</th>
                <th className="px-4 py-3 font-medium text-right">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {keys.length === 0 ? (
                <tr>
                  <td colSpan={7} className="px-4 py-10 text-center text-slate-400">
                    {loading ? "加载中..." : "尚未创建外部项目密钥。点击右上角「新建接入项目密钥」即可给新项目签发独立凭据。"}
                  </td>
                </tr>
              ) : (
                keys.map((item) => (
                  <tr key={item.project_id} className="transition hover:bg-slate-50/70">
                    <td className="px-4 py-3">
                      <div className="font-semibold text-slate-900">{item.name}</div>
                      <div className="font-mono-code text-[11px] text-slate-400">{item.project_id}</div>
                      <div className="mt-1 flex flex-wrap gap-1">
                        {item.scopes && item.scopes.length > 0 ? (
                          item.scopes.map((sc) => (
                            <span
                              key={sc}
                              className="font-mono-code rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600"
                            >
                              {sc}
                            </span>
                          ))
                        ) : (
                          <span className="rounded bg-rose-50 px-1.5 py-0.5 text-[10px] text-rose-600">无权限</span>
                        )}
                      </div>
                    </td>
                    <td className="font-mono-code whitespace-nowrap px-4 py-3 text-slate-700">{item.key_prefix}••••</td>
                    <td className="px-4 py-3">
                      {item.default_webhook_url ? (
                        <div className="space-y-1">
                          <div className="flex items-center gap-1.5">
                            <Webhook className="h-3.5 w-3.5 shrink-0 text-slate-400" />
                            <span className="font-mono-code max-w-[190px] truncate text-slate-600" title={item.default_webhook_url}>
                              {item.default_webhook_url}
                            </span>
                            <button
                              type="button"
                              disabled={testingWebhookId === item.project_id}
                              onClick={() => handleTestWebhook(item)}
                              className="inline-flex shrink-0 cursor-pointer items-center gap-1 rounded-md border border-slate-200 bg-white px-1.5 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100"
                            >
                              <Send className="h-3 w-3" />
                              测试
                            </button>
                          </div>
                          {item.has_webhook_secret ? (
                            <span className="inline-block rounded bg-emerald-50 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700 ring-1 ring-inset ring-emerald-200/70">
                              HMAC 签名已配
                            </span>
                          ) : null}
                        </div>
                      ) : (
                        <span className="text-slate-400">未配置</span>
                      )}
                    </td>
                    <td className="font-mono-code px-4 py-3 text-slate-700">
                      {(() => {
                        const fin = formatFinancialSummary(item);
                        return (
                          <div>
                            <div className="whitespace-nowrap">
                              收码 {item.received_count ?? 0}/{item.order_count ?? 0} ·{" "}
                              <strong>{fin.primary}</strong>
                            </div>
                            {fin.lines.length > 0 ? (
                              <div className="mt-0.5 space-y-0.5 text-[11px] text-slate-400">
                                {fin.lines.map((line) => (
                                  <div key={line}>{line}</div>
                                ))}
                              </div>
                            ) : null}
                            {fin.fxNote ? (
                              <div className="mt-0.5 text-[11px] text-indigo-600">{fin.fxNote}</div>
                            ) : null}
                          </div>
                        );
                      })()}
                    </td>
                    <td className="font-mono-code whitespace-nowrap px-4 py-3 text-slate-500">
                      {item.last_used_at ? formatDateTime(item.last_used_at) : "尚未使用"}
                    </td>
                    <td className="whitespace-nowrap px-4 py-3">
                      <button
                        type="button"
                        disabled={togglingId === item.project_id}
                        onClick={() => handleToggleEnabled(item)}
                        className={`cursor-pointer rounded-full border px-2.5 py-0.5 text-[11px] font-medium disabled:opacity-50 ${
                          item.enabled
                            ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                            : "border-slate-200 bg-slate-100 text-slate-500"
                        }`}
                      >
                        {item.enabled ? "已启用" : "已停用"}
                      </button>
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 text-right">
                      <div className="inline-flex items-center gap-1">
                        <button
                          type="button"
                          onClick={() => startEdit(item)}
                          className="cursor-pointer rounded-lg p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
                          title="编辑回调地址与权限"
                        >
                          <Edit3 className="h-4 w-4" />
                        </button>
                        <button
                          type="button"
                          onClick={() => setRotateTarget(item)}
                          className="cursor-pointer rounded-lg p-1.5 text-slate-400 hover:bg-indigo-50 hover:text-indigo-600"
                          title="轮转密钥（保留历史订单归属，立即作废旧密钥）"
                        >
                          <RotateCw className="h-4 w-4" />
                        </button>
                        <button
                          type="button"
                          onClick={() => setDeleteTarget(item)}
                          className="cursor-pointer rounded-lg p-1.5 text-slate-400 hover:bg-rose-50 hover:text-rose-600"
                          title="删除密钥"
                        >
                          <Trash2 className="h-4 w-4" />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>

      {/* 即拷即用接入代码示例 */}
      <section className="surface-card rounded-2xl p-5">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <Code2 className="h-4 w-4 text-emerald-600" />
            <h2 className="text-sm font-bold text-slate-900">新项目接入代码模板</h2>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <div className="inline-flex rounded-xl border border-slate-200 bg-slate-50 p-0.5 text-xs">
              {(
                [
                  ["python", "Python"],
                  ["curl", "cURL"],
                  ["node", "Node.js"],
                  ["sse", "SSE 实时推码"],
                ] as const
              ).map(([tab, label]) => (
                <button
                  key={tab}
                  type="button"
                  onClick={() => setCodeTab(tab)}
                  className={`cursor-pointer rounded-lg px-2.5 py-1 font-medium transition ${
                    codeTab === tab ? "bg-white text-slate-900 shadow-2xs" : "text-slate-500 hover:text-slate-900"
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
            <CopyButton value={snippets[codeTab]} label="复制代码" size="md" />
          </div>
        </div>

        <pre className="font-mono-code overflow-x-auto rounded-xl bg-slate-900 p-4 text-xs leading-relaxed text-slate-100 shadow-inner">
          <code>{snippets[codeTab]}</code>
        </pre>
      </section>

      {/* Webhook Outbox 投递状态 */}
      <section className="surface-card rounded-2xl">
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-t-2xl border-b border-slate-200/75 bg-slate-50/70 p-4">
          <div className="flex items-center gap-2">
            <Webhook className="h-4 w-4 text-indigo-600" />
            <h2 className="text-sm font-bold text-slate-900">最近 Webhook 投递记录</h2>
            <span className="font-mono-code rounded-full bg-slate-200/80 px-2 py-0.5 text-xs font-semibold text-slate-700">
              {deliveries.length}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <CustomSelect
              value={deliveryStatus}
              onChange={setDeliveryStatus}
              options={DELIVERY_STATUS_OPTIONS}
              size="sm"
              align="right"
              activeHighlight={Boolean(deliveryStatus)}
              minMenuWidth={210}
              className="w-44"
            />
            <button
              type="button"
              onClick={loadDeliveries}
              disabled={loadingDeliveries}
              className="inline-flex cursor-pointer items-center gap-1 rounded-xl border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 shadow-2xs hover:bg-slate-50"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${loadingDeliveries ? "animate-spin text-emerald-600" : ""}`} />
              刷新
            </button>
          </div>
        </div>
        {deliveries.length === 0 ? (
          <div className="px-4 py-8 text-center text-xs text-slate-400">
            {loadingDeliveries ? "加载中..." : "暂无匹配的 Webhook 回调投递记录"}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[680px] border-collapse text-left text-xs">
              <thead>
                <tr className="border-b border-slate-100 bg-slate-50/50 text-slate-500">
                  <th className="px-4 py-2.5 font-medium">时间</th>
                  <th className="px-4 py-2.5 font-medium">项目 / 订单</th>
                  <th className="px-4 py-2.5 font-medium">事件</th>
                  <th className="px-4 py-2.5 font-medium">目标 URL</th>
                  <th className="px-4 py-2.5 font-medium">状态 / HTTP / 重试</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {deliveries.map((d) => (
                  <tr key={d.delivery_id} className="hover:bg-slate-50/60">
                    <td className="font-mono-code whitespace-nowrap px-4 py-2.5 text-slate-500">
                      {formatDateTime(d.updated_at || d.created_at)}
                    </td>
                    <td className="font-mono-code whitespace-nowrap px-4 py-2.5 text-slate-700">
                      <div>{d.project_id || "web-console"}</div>
                      <div className="text-[11px] text-slate-400">{d.order_id}</div>
                    </td>
                    <td className="font-mono-code whitespace-nowrap px-4 py-2.5 text-slate-800">{d.event_type}</td>
                    <td className="font-mono-code max-w-[220px] truncate px-4 py-2.5 text-slate-500" title={d.target_url}>
                      {d.target_url}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2.5">
                      <div className="flex items-center gap-1.5">
                        <span
                          className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${
                            d.status === "delivered"
                              ? "bg-emerald-50 text-emerald-700"
                              : d.status === "failed"
                                ? "bg-rose-50 text-rose-700"
                                : "bg-slate-100 text-slate-600"
                          }`}
                        >
                          {d.status} ({d.attempts}/{d.max_attempts})
                        </span>
                        {d.last_status_code > 0 ? (
                          <span className="font-mono-code rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600">
                            HTTP {d.last_status_code}
                          </span>
                        ) : null}
                      </div>
                      {d.last_error ? (
                        <div className="mt-0.5 max-w-[220px] truncate text-[11px] text-rose-600" title={d.last_error}>
                          {d.last_error}
                        </div>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <ConfirmModal
        open={Boolean(rotateTarget)}
        title="确认轮转该项目 API Key？"
        description={`轮转「${rotateTarget?.name || ""}」后，系统将生成全新密钥并保留全部历史订单归属；旧密钥与已建立的 SSE 连接将立即失效。`}
        confirmLabel="确认轮转"
        loading={rotating}
        onConfirm={handleRotate}
        onCancel={() => setRotateTarget(null)}
      />

      <ConfirmModal
        open={Boolean(deleteTarget)}
        title="确认删除项目密钥？"
        description={`删除「${deleteTarget?.name || ""}」后，使用该密钥的外部项目将立即失去访问权限（历史订单记录仍会保留）。`}
        confirmLabel="确认删除"
        danger
        loading={deleting}
        onConfirm={handleDelete}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}
