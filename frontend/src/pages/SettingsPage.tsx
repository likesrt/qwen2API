import { type Dispatch, type SetStateAction, useEffect, useState } from "react"
import { Code, Globe, KeyRound, RefreshCw, ServerCrash, Settings2 } from "lucide-react"
import { toast } from "sonner"

import { Button } from "../components/ui/button"
import { getAuthHeader } from "../lib/auth"
import { API_BASE } from "../lib/api"

type ProxyConfig = {
  proxy_url: string
  enabled: boolean
}

type RuntimeSettings = {
  version?: string
  max_inflight_per_account?: number
  engine_mode?: string
  model_aliases?: Record<string, string>
  proxy?: ProxyConfig
}

type SessionKeyCardProps = {
  sessionKey: string
  setSessionKey: Dispatch<SetStateAction<string>>
  onSave: () => void
  onClear: () => void
}

type RuntimeConfigCardProps = {
  version: string
  maxInflight: number
  setMaxInflight: Dispatch<SetStateAction<number>>
  engineMode: string
  setEngineMode: Dispatch<SetStateAction<string>>
  onSave: () => void
}

type ProxyCardProps = {
  proxy: ProxyConfig
  setProxy: Dispatch<SetStateAction<ProxyConfig>>
  testingProxy: boolean
  onSave: () => void
  onTest: () => void
}

type AliasesCardProps = {
  modelAliases: string
  setModelAliases: Dispatch<SetStateAction<string>>
  onSave: () => void
}

/**
 * 构造管理接口请求参数。
 *
 * @param method 请求方法。
 * @param body 可选 JSON 请求体。
 * @returns 可直接传给 fetch 的 RequestInit。
 */
function adminRequest(method: string, body?: unknown): RequestInit {
  const headers = body === undefined
    ? getAuthHeader()
    : { "Content-Type": "application/json", ...getAuthHeader() }
  return body === undefined ? { method, headers } : { method, headers, body: JSON.stringify(body) }
}

/**
 * 读取本地保存的控制台会话 Key。
 *
 * @param setSessionKey 会话 Key 状态写入函数。
 */
function loadSessionKey(setSessionKey: Dispatch<SetStateAction<string>>): void {
  setSessionKey(localStorage.getItem("qwen2api_key") || "")
}

/**
 * 拉取后台运行时配置并同步到页面状态。
 *
 * @param setSettings 原始配置写入函数。
 * @param setMaxInflight 并发配置写入函数。
 * @param setEngineMode 引擎模式写入函数。
 * @param setModelAliases 模型映射文本写入函数。
 * @param setProxy 代理配置写入函数。
 */
async function loadSettings(
  setSettings: Dispatch<SetStateAction<RuntimeSettings | null>>,
  setMaxInflight: Dispatch<SetStateAction<number>>,
  setEngineMode: Dispatch<SetStateAction<string>>,
  setModelAliases: Dispatch<SetStateAction<string>>,
  setProxy: Dispatch<SetStateAction<ProxyConfig>>,
): Promise<void> {
  try {
    const response = await fetch(`${API_BASE}/api/admin/settings`, adminRequest("GET"))
    if (!response.ok) throw new Error("unauthorized")
    const data = await response.json()
    setSettings(data)
    setMaxInflight(data.max_inflight_per_account || 4)
    setEngineMode(data.engine_mode || "hybrid")
    setModelAliases(JSON.stringify(data.model_aliases || {}, null, 2))
    setProxy(data.proxy || { proxy_url: "", enabled: false })
  } catch {
    toast.error("配置获取失败，请检查会话 Key")
  }
}

/**
 * 保存运行时配置并在成功后刷新页面状态。
 *
 * @param payload 待保存的配置片段。
 * @param successText 成功提示文案。
 * @param onRefresh 保存成功后的刷新回调。
 */
async function saveSettings(
  payload: Record<string, unknown>,
  successText: string,
  onRefresh: () => void,
): Promise<void> {
  try {
    const response = await fetch(`${API_BASE}/api/admin/settings`, adminRequest("PUT", payload))
    const data = await response.json()
    if (!response.ok || !data.ok) {
      toast.error(data.detail || "保存失败")
      return
    }
    toast.success(successText)
    onRefresh()
  } catch {
    toast.error("保存请求失败")
  }
}

/**
 * 保存会话 Key 到本地存储。
 *
 * @param sessionKey 待保存的会话 Key。
 * @param onRefresh 保存成功后的刷新回调。
 */
function saveSessionKey(sessionKey: string, onRefresh: () => void): void {
  if (!sessionKey.trim()) {
    toast.error("请输入 Key")
    return
  }
  localStorage.setItem("qwen2api_key", sessionKey.trim())
  toast.success("Key 已保存到本地，刷新数据...")
  onRefresh()
}

/**
 * 清除本地保存的会话 Key。
 *
 * @param setSessionKey 会话 Key 状态写入函数。
 */
function clearSessionKey(setSessionKey: Dispatch<SetStateAction<string>>): void {
  localStorage.removeItem("qwen2api_key")
  setSessionKey("")
  toast.success("Key 已清除")
}

/**
 * 保存单账号并发与引擎模式配置。
 *
 * @param maxInflight 单账号最大并发。
 * @param engineMode 网关引擎模式。
 * @param onRefresh 保存成功后的刷新回调。
 */
function saveRuntimeConfig(maxInflight: number, engineMode: string, onRefresh: () => void): void {
  void saveSettings({ max_inflight_per_account: Number(maxInflight), engine_mode: engineMode }, "运行时配置已保存", onRefresh)
}

/**
 * 保存模型映射规则。
 *
 * @param modelAliases 文本形式的 JSON 映射。
 * @param onRefresh 保存成功后的刷新回调。
 */
function saveAliases(modelAliases: string, onRefresh: () => void): void {
  try {
    const parsed = JSON.parse(modelAliases)
    void saveSettings({ model_aliases: parsed }, "模型映射规则已更新", onRefresh)
  } catch {
    toast.error("JSON 格式错误，请检查语法")
  }
}

/**
 * 保存全局代理配置。
 *
 * @param proxy 代理配置对象。
 * @param onRefresh 保存成功后的刷新回调。
 */
function saveProxy(proxy: ProxyConfig, onRefresh: () => void): void {
  void saveSettings({ proxy }, "代理配置已保存", onRefresh)
}

/**
 * 测试临时代理配置的连通性。
 *
 * @param proxy 待测试的代理配置。
 * @param setTestingProxy 控制按钮加载态。
 */
async function testProxyConnection(
  proxy: ProxyConfig,
  setTestingProxy: Dispatch<SetStateAction<boolean>>,
): Promise<void> {
  setTestingProxy(true)
  try {
    const response = await fetch(`${API_BASE}/api/admin/proxy/test`, adminRequest("POST", { proxy_url: proxy.proxy_url, enabled: true }))
    const data = await response.json()
    if (!response.ok || data.success === false) {
      toast.error(data.error || "代理连通性测试失败")
      return
    }
    toast.success(`代理可用，耗时 ${data.time_ms || 0}ms`)
  } catch {
    toast.error("代理测试请求失败")
  } finally {
    setTestingProxy(false)
  }
}

/**
 * 计算展示给用户的 Base URL。
 *
 * @returns 当前页面应展示的 API 基础地址。
 */
function buildBaseUrl(): string {
  return API_BASE || `http://${window.location.hostname}`
}

/**
 * 生成 OpenAI 兼容接口的 curl 示例。
 *
 * @param baseUrl 当前展示的 API 基础地址。
 * @returns 多行 curl 示例文本。
 */
function buildCurlExample(baseUrl: string): string {
  return `# OpenAI 流式对话
curl ${baseUrl}/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -d '{
    "model": "qwen3.6-plus",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": true
  }'

# 查看模型列表
curl ${baseUrl}/v1/models \\
  -H "Authorization: Bearer YOUR_API_KEY"`
}

/**
 * 渲染页面顶部标题与刷新按钮。
 *
 * @param onRefresh 点击刷新后的回调。
 * @returns 设置页头部区域。
 */
function SettingsHeader({ onRefresh }: { onRefresh: () => void }) {
  return (
    <div className="flex justify-between items-center">
      <div>
        <h2 className="text-2xl font-bold tracking-tight">系统设置</h2>
        <p className="text-muted-foreground">管理控制台认证、代理和网关运行时配置。</p>
      </div>
      <Button variant="outline" onClick={onRefresh}><RefreshCw className="mr-2 h-4 w-4" /> 刷新配置</Button>
    </div>
  )
}

/**
 * 渲染会话 Key 配置卡片。
 *
 * @param props 当前会话 Key 与操作回调。
 * @returns 会话 Key 配置区域。
 */
function SessionKeyCard(props: SessionKeyCardProps) {
  return (
    <div className="rounded-xl border bg-card text-card-foreground shadow-sm">
      <div className="flex flex-col space-y-1.5 p-6 border-b bg-muted/30"><div className="flex items-center gap-2"><KeyRound className="h-5 w-5 text-primary" /><h3 className="font-semibold leading-none tracking-tight">当前会话 Key</h3></div><p className="text-sm text-muted-foreground">将已有的 API Key 粘贴到此处，控制台将使用它进行所有管理操作。</p></div>
      <div className="p-6"><div className="flex gap-2 items-center"><input type="password" value={props.sessionKey} onChange={event => props.setSessionKey(event.target.value)} placeholder="sk-qwen-... 或默认管理员密钥 admin" className="flex h-10 w-full flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm" /><Button onClick={props.onSave}>保存</Button><Button variant="ghost" onClick={props.onClear}>清除</Button></div></div>
    </div>
  )
}

/**
 * 渲染连接信息卡片。
 *
 * @param baseUrl 当前展示的 API 基础地址。
 * @returns 只读连接信息区域。
 */
function ConnectionInfoCard({ baseUrl }: { baseUrl: string }) {
  return (
    <div className="rounded-xl border bg-card text-card-foreground shadow-sm">
      <div className="flex flex-col space-y-1.5 p-6 border-b bg-muted/30"><div className="flex items-center gap-2"><ServerCrash className="h-5 w-5 text-primary" /><h3 className="font-semibold leading-none tracking-tight">连接信息</h3></div></div>
      <div className="p-6"><div className="space-y-1"><label className="text-sm font-medium">API 基础地址 (Base URL)</label><input type="text" readOnly value={baseUrl} className="flex h-10 w-full rounded-md border border-input bg-muted px-3 py-2 text-sm font-mono text-muted-foreground" /></div></div>
    </div>
  )
}

/**
 * 渲染核心运行参数卡片。
 *
 * @param props 当前运行参数状态与保存回调。
 * @returns 运行参数区域。
 */
function RuntimeConfigCard(props: RuntimeConfigCardProps) {
  return (
    <div className="rounded-xl border bg-card text-card-foreground shadow-sm">
      <div className="flex flex-col space-y-1.5 p-6 border-b bg-muted/30"><div className="flex items-center gap-2"><Settings2 className="h-5 w-5 text-primary" /><h3 className="font-semibold leading-none tracking-tight">核心运行参数</h3></div></div>
      <div className="p-6 space-y-4"><div className="flex justify-between items-center py-2 border-b"><span className="text-sm font-medium">当前系统版本</span><span className="font-mono text-sm">{props.version}</span></div><div className="flex justify-between items-center py-2 border-b gap-4"><div className="space-y-1"><span className="text-sm font-medium">单账号最大并发</span><p className="text-xs text-muted-foreground">控制每个上游账号同时处理的请求数量。</p></div><div className="flex gap-2 items-center"><input type="number" min="1" max="10" value={props.maxInflight} onChange={event => props.setMaxInflight(Number(event.target.value))} className="flex h-8 w-20 rounded-md border border-input bg-background px-3 py-1 text-sm text-center" /><select value={props.engineMode} onChange={event => props.setEngineMode(event.target.value)} className="flex h-8 rounded-md border border-input bg-background px-3 py-1 text-sm"><option value="browser">browser</option><option value="httpx">httpx</option><option value="hybrid">hybrid</option></select><Button size="sm" onClick={props.onSave}>保存</Button></div></div></div>
    </div>
  )
}

/**
 * 渲染全局代理配置卡片。
 *
 * @param props 代理配置状态、保存和测试回调。
 * @returns 代理配置区域。
 */
function ProxyCard(props: ProxyCardProps) {
  return (
    <div className="rounded-xl border bg-card text-card-foreground shadow-sm">
      <div className="flex flex-col space-y-1.5 p-6 border-b bg-muted/30"><div className="flex items-center gap-2"><Globe className="h-5 w-5 text-primary" /><h3 className="font-semibold leading-none tracking-tight">全局代理</h3></div><p className="text-sm text-muted-foreground">账号注册、邮箱获取和上游接口请求都会使用这里配置的代理。</p></div>
      <div className="p-6 space-y-4"><label className="flex items-center gap-3 text-sm font-medium"><input type="checkbox" checked={props.proxy.enabled} onChange={event => props.setProxy(current => ({ ...current, enabled: event.target.checked }))} />启用全局代理</label><div className="space-y-1"><label className="text-sm font-medium">代理地址</label><input type="text" value={props.proxy.proxy_url} onChange={event => props.setProxy(current => ({ ...current, proxy_url: event.target.value }))} placeholder="http://127.0.0.1:7890 或 socks5://127.0.0.1:1080" className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm" /></div><div className="flex gap-2"><Button onClick={props.onSave}>保存代理</Button><Button variant="outline" onClick={props.onTest} disabled={props.testingProxy}>{props.testingProxy ? <RefreshCw className="mr-2 h-4 w-4 animate-spin" /> : null}测试连通性</Button></div></div>
    </div>
  )
}

/**
 * 渲染模型映射规则卡片。
 *
 * @param props 模型映射文本与保存回调。
 * @returns 模型映射配置区域。
 */
function AliasesCard(props: AliasesCardProps) {
  return (
    <div className="rounded-xl border bg-card text-card-foreground shadow-sm">
      <div className="flex flex-col space-y-1.5 p-6 border-b bg-muted/30"><h3 className="font-semibold leading-none tracking-tight">自动模型映射规则 (Model Aliases)</h3><p className="text-sm text-muted-foreground">下游传入的模型名称会自动映射到千问实际模型。</p></div>
      <div className="p-6"><textarea rows={10} value={props.modelAliases} onChange={event => props.setModelAliases(event.target.value)} className="flex min-h-[200px] w-full rounded-md border border-input bg-slate-950 text-slate-300 px-3 py-2 text-sm font-mono" /><div className="mt-4 flex justify-end"><Button onClick={props.onSave}>保存映射</Button></div></div>
    </div>
  )
}

/**
 * 渲染 curl 使用示例卡片。
 *
 * @param curlExample 预生成的示例文本。
 * @returns 使用示例区域。
 */
function UsageExampleCard({ curlExample }: { curlExample: string }) {
  return (
    <div className="rounded-xl border bg-card text-card-foreground shadow-sm">
      <div className="flex flex-col space-y-1.5 p-6 border-b bg-muted/30"><div className="flex items-center gap-2"><Code className="h-5 w-5 text-primary" /><h3 className="font-semibold leading-none tracking-tight">使用示例</h3></div></div>
      <div className="p-6"><div className="bg-slate-950 rounded-lg p-4 text-sm font-mono text-slate-300 overflow-x-auto whitespace-pre">{curlExample}</div></div>
    </div>
  )
}

/**
 * 系统设置页：负责会话 Key、代理和运行时参数配置。
 *
 * @returns 后台系统设置页面。
 */
export default function SettingsPage() {
  const [settings, setSettings] = useState<RuntimeSettings | null>(null)
  const [sessionKey, setSessionKey] = useState("")
  const [maxInflight, setMaxInflight] = useState(4)
  const [engineMode, setEngineMode] = useState("hybrid")
  const [modelAliases, setModelAliases] = useState("")
  const [proxy, setProxy] = useState<ProxyConfig>({ proxy_url: "", enabled: false })
  const [testingProxy, setTestingProxy] = useState(false)
  const refreshSettings = () => void loadSettings(setSettings, setMaxInflight, setEngineMode, setModelAliases, setProxy)
  const baseUrl = buildBaseUrl()
  const curlExample = buildCurlExample(baseUrl)

  useEffect(() => {
    loadSessionKey(setSessionKey)
    refreshSettings()
  }, [])

  return (
    <div className="space-y-6 max-w-4xl">
      <SettingsHeader onRefresh={() => { refreshSettings(); toast.success("配置已刷新") }} />
      <div className="grid gap-6">
        <SessionKeyCard sessionKey={sessionKey} setSessionKey={setSessionKey} onSave={() => saveSessionKey(sessionKey, refreshSettings)} onClear={() => clearSessionKey(setSessionKey)} />
        <ConnectionInfoCard baseUrl={baseUrl} />
        <RuntimeConfigCard version={settings?.version || "..."} maxInflight={maxInflight} setMaxInflight={setMaxInflight} engineMode={engineMode} setEngineMode={setEngineMode} onSave={() => saveRuntimeConfig(maxInflight, engineMode, refreshSettings)} />
        <ProxyCard proxy={proxy} setProxy={setProxy} testingProxy={testingProxy} onSave={() => saveProxy(proxy, refreshSettings)} onTest={() => void testProxyConnection(proxy, setTestingProxy)} />
        <AliasesCard modelAliases={modelAliases} setModelAliases={setModelAliases} onSave={() => saveAliases(modelAliases, refreshSettings)} />
        <UsageExampleCard curlExample={curlExample} />
      </div>
    </div>
  )
}
