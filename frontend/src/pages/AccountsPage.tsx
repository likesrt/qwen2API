import { type Dispatch, type SetStateAction, useEffect, useMemo, useState } from "react"
import { Bot, MailWarning, Plus, RefreshCw, ShieldCheck, Trash2 } from "lucide-react"
import { toast } from "sonner"

import { Button } from "../components/ui/button"
import { getAuthHeader } from "../lib/auth"
import { API_BASE } from "../lib/api"

const DEFAULT_LOG_PAGE_SIZE = 20

type AccountItem = {
  email: string
  password?: string
  token?: string
  username?: string
  valid?: boolean
  inflight?: number
  rate_limited_until?: number
  activation_pending?: boolean
  status_code?: string
  status_text?: string
  last_error?: string
}

type RegisterLog = {
  id: string
  batch_id: string
  sequence: number
  created_at: string
  started_at: string
  finished_at: string
  status: string
  account: { email: string; username: string }
  error: string
}

type AccountStats = {
  valid: number
  pending: number
  rateLimited: number
  banned: number
  invalid: number
}

type RegisterLogFilters = {
  batchId: string
  account: string
  status: string
}

type RegisterLogPage = {
  logs: RegisterLog[]
  total: number
  page: number
  pageSize: number
  totalPages: number
  runningBatches: string[]
}

type AccountHeaderProps = {
  registering: boolean
  verifyingAll: boolean
  onVerifyAll: () => void
  onRefresh: () => void
  onAutoRegister: () => void
}

type BatchRegisterCardProps = {
  batchCount: number
  setBatchCount: Dispatch<SetStateAction<number>>
  batchRegistering: boolean
  runningBatches: string[]
  onSubmit: () => void
}

type ManualInjectCardProps = {
  email: string
  setEmail: Dispatch<SetStateAction<string>>
  password: string
  setPassword: Dispatch<SetStateAction<string>>
  token: string
  setToken: Dispatch<SetStateAction<string>>
  onSubmit: () => void
}

type AccountRowProps = {
  account: AccountItem
  verifying: string | null
  onActivate: (email: string) => void
  onVerify: (email: string) => void
  onDelete: (email: string) => void
}

type AccountsTableProps = {
  accounts: AccountItem[]
  verifying: string | null
  onActivate: (email: string) => void
  onVerify: (email: string) => void
  onDelete: (email: string) => void
}

type RegisterLogsFilterRowProps = {
  draftFilters: RegisterLogFilters
  setDraftFilters: Dispatch<SetStateAction<RegisterLogFilters>>
  onApply: () => void
  onReset: () => void
  runningBatches: string[]
}

type RegisterLogsTableProps = {
  logPage: RegisterLogPage
  draftFilters: RegisterLogFilters
  setDraftFilters: Dispatch<SetStateAction<RegisterLogFilters>>
  onApplyFilters: () => void
  onResetFilters: () => void
  onPageChange: (page: number) => void
}

/**
 * 构造管理接口请求参数。
 *
 * @param method 请求方法。
 * @param body 可选的 JSON 请求体。
 * @returns 可直接传给 fetch 的 RequestInit。
 */
function adminRequest(method: string, body?: unknown): RequestInit {
  const headers = body === undefined ? getAuthHeader() : { "Content-Type": "application/json", ...getAuthHeader() }
  return body === undefined ? { method, headers } : { method, headers, body: JSON.stringify(body) }
}

/**
 * 根据账号状态码返回状态标签样式。
 *
 * @param code 后端返回的状态码。
 * @returns Tailwind 样式字符串。
 */
function statusStyle(code?: string): string {
  switch (code) {
    case "valid":
      return "bg-green-500/10 text-green-700 dark:text-green-400 ring-green-500/20"
    case "pending_activation":
      return "bg-orange-500/10 text-orange-700 dark:text-orange-400 ring-orange-500/20"
    case "rate_limited":
      return "bg-yellow-500/10 text-yellow-700 dark:text-yellow-300 ring-yellow-500/20"
    case "banned":
      return "bg-red-500/10 text-red-700 dark:text-red-400 ring-red-500/20"
    default:
      return "bg-slate-500/10 text-slate-700 dark:text-slate-300 ring-slate-500/20"
  }
}

/**
 * 将账号状态码转换为页面展示文案。
 *
 * @param account 账号状态对象。
 * @returns 中文状态名称。
 */
function statusText(account: Pick<AccountItem, "status_code" | "valid">): string {
  switch (account.status_code) {
    case "valid":
      return "可用"
    case "pending_activation":
      return "未激活"
    case "rate_limited":
      return "限流"
    case "banned":
      return "封禁"
    case "auth_error":
      return "认证失效"
    default:
      return account.valid ? "可用" : "失效"
  }
}

/**
 * 将注册日志状态转换为中文文案。
 *
 * @param status 日志状态码。
 * @returns 页面展示的短文本。
 */
function logText(status: string): string {
  switch (status) {
    case "success":
      return "成功"
    case "running":
      return "注册中"
    case "pending_activation":
      return "待激活"
    case "failed":
      return "失败"
    default:
      return "排队中"
  }
}

/**
 * 生成账号状态补充说明。
 *
 * @param account 当前账号对象。
 * @returns 限流恢复时间或最近错误信息。
 */
function statusNote(account: AccountItem): string {
  if ((account.rate_limited_until || 0) > Date.now() / 1000) return `预计 ${Math.max(0, Math.ceil(account.rate_limited_until! - Date.now() / 1000))} 秒后恢复`
  return account.last_error || ""
}

/**
 * 将常见英文错误转换为更易读的中文提示。
 *
 * @param error 原始错误文本。
 * @returns 适合 toast 展示的错误信息。
 */
function localizeError(error?: string): string {
  if (!error) return "未知错误"
  const lower = error.toLowerCase()
  if (lower.includes("activation already in progress")) return "账号正在激活中，请稍后刷新"
  if (lower.includes("activation link") || lower.includes("token not found")) return "激活链接或 Token 获取失败"
  if (lower.includes("token invalid") || lower.includes("auth")) return "Token 无效或认证失败"
  return error
}

/**
 * 统计不同账号状态的数量。
 *
 * @param accounts 当前账号列表。
 * @returns 可直接渲染到统计卡片的数据。
 */
function buildAccountStats(accounts: AccountItem[]): AccountStats {
  return accounts.reduce<AccountStats>((result, account) => {
    if (account.status_code === "valid") result.valid += 1
    else if (account.status_code === "pending_activation") result.pending += 1
    else if (account.status_code === "rate_limited") result.rateLimited += 1
    else if (account.status_code === "banned") result.banned += 1
    else result.invalid += 1
    return result
  }, { valid: 0, pending: 0, rateLimited: 0, banned: 0, invalid: 0 })
}

/**
 * 返回日志筛选条件的默认值。
 *
 * @returns 空筛选条件对象；用于初始化和重置表头过滤器。
 */
function defaultLogFilters(): RegisterLogFilters {
  return { batchId: "", account: "", status: "" }
}

/**
 * 返回日志分页状态的默认值。
 *
 * @returns 空日志页结构；用于首屏渲染和请求失败后的兜底状态。
 */
function defaultLogPage(): RegisterLogPage {
  return { logs: [], total: 0, page: 1, pageSize: DEFAULT_LOG_PAGE_SIZE, totalPages: 1, runningBatches: [] }
}

/**
 * 规范化后端返回的日志分页结构。
 *
 * @param data 注册日志接口返回值。
 * @returns 前端统一使用的分页数据结构。
 */
function normalizeLogPage(data: any): RegisterLogPage {
  return {
    logs: data.logs || [],
    total: data.total || 0,
    page: data.page || 1,
    pageSize: data.page_size || DEFAULT_LOG_PAGE_SIZE,
    totalPages: data.total_pages || 1,
    runningBatches: data.running_batches || [],
  }
}

/**
 * 组装注册日志查询参数。
 *
 * @param filters 批次、账号和状态筛选条件。
 * @param page 目标页码；超出范围时由后端再次约束。
 * @returns 可直接拼接到 URL 的查询字符串。
 */
function buildLogQuery(filters: RegisterLogFilters, page: number): string {
  const params = new URLSearchParams({ page: String(page), page_size: String(DEFAULT_LOG_PAGE_SIZE) })
  if (filters.batchId.trim()) params.set("batch_id", filters.batchId.trim())
  if (filters.account.trim()) params.set("account", filters.account.trim())
  if (filters.status) params.set("status", filters.status)
  return params.toString()
}

/**
 * 从后端拉取账号列表。
 *
 * @param setAccounts React 状态写入函数。
 * @returns 请求完成后的 Promise；失败时弹出提示。
 */
async function loadAccounts(setAccounts: Dispatch<SetStateAction<AccountItem[]>>): Promise<void> {
  try {
    const response = await fetch(`${API_BASE}/api/admin/accounts`, { headers: getAuthHeader() })
    if (!response.ok) throw new Error("unauthorized")
    const data = await response.json()
    setAccounts(data.accounts || [])
  } catch {
    toast.error("刷新账号列表失败，请检查会话密钥")
  }
}

/**
 * 从后端拉取注册日志分页数据。
 *
 * @param filters 当前表头筛选条件。
 * @param page 目标页码。
 * @param setLogPage 分页状态写入函数。
 * @returns 请求完成后的 Promise；失败时弹出提示。
 */
async function loadRegisterLogs(filters: RegisterLogFilters, page: number, setLogPage: Dispatch<SetStateAction<RegisterLogPage>>): Promise<void> {
  try {
    const response = await fetch(`${API_BASE}/api/admin/accounts/register/logs?${buildLogQuery(filters, page)}`, { headers: getAuthHeader() })
    if (!response.ok) throw new Error("unauthorized")
    const data = await response.json()
    setLogPage(normalizeLogPage(data))
  } catch {
    toast.error("刷新注册日志失败")
  }
}

/**
 * 同步刷新账号列表和注册日志。
 *
 * @param setAccounts 账号列表写入函数。
 * @param setLogPage 注册日志分页写入函数。
 * @param filters 当前日志筛选条件。
 * @param page 当前日志页码。
 */
function refreshAccountDashboard(setAccounts: Dispatch<SetStateAction<AccountItem[]>>, setLogPage: Dispatch<SetStateAction<RegisterLogPage>>, filters: RegisterLogFilters, page: number): void {
  void loadAccounts(setAccounts)
  void loadRegisterLogs(filters, page, setLogPage)
}

/**
 * 在存在运行中批次时定时刷新页面数据。
 *
 * @param runningBatches 当前运行中的批次列表。
 * @param onRefresh 刷新函数；批次结束后自动停止轮询。
 */
function useBatchPolling(runningBatches: string[], onRefresh: () => void): void {
  useEffect(() => {
    if (!runningBatches.length) return
    const timer = window.setInterval(onRefresh, 3000)
    return () => window.clearInterval(timer)
  }, [onRefresh, runningBatches])
}

/**
 * 手动注入账号到账号池。
 *
 * @param email 可选邮箱；为空时使用临时邮箱占位。
 * @param password 可选密码；用于后续自动激活或刷新。
 * @param token 必填的账号 Token。
 * @param setEmail 表单邮箱写入函数。
 * @param setPassword 表单密码写入函数。
 * @param setToken 表单 Token 写入函数。
 * @param onRefresh 成功后刷新页面数据。
 */
async function addManualAccount(email: string, password: string, token: string, setEmail: Dispatch<SetStateAction<string>>, setPassword: Dispatch<SetStateAction<string>>, setToken: Dispatch<SetStateAction<string>>, onRefresh: () => void): Promise<void> {
  if (!token.trim()) {
    toast.error("请先填写 Token")
    return
  }
  const id = toast.loading("正在注入账号...")
  try {
    const body = { email: email || `manual_${Date.now()}@qwen`, password, token }
    const response = await fetch(`${API_BASE}/api/admin/accounts`, adminRequest("POST", body))
    const data = await response.json()
    if (!data.ok) return void toast.error(localizeError(data.error) || "账号注入失败", { id, duration: 8000 })
    toast.success("账号已加入账号池", { id })
    setEmail("")
    setPassword("")
    setToken("")
    onRefresh()
  } catch {
    toast.error("账号注入请求失败", { id })
  }
}

/**
 * 调用单账号自动注册接口。
 *
 * @param setRegistering 控制按钮加载态。
 * @param onRefresh 注册完成后刷新页面数据。
 */
async function registerSingleAccount(setRegistering: Dispatch<SetStateAction<boolean>>, onRefresh: () => void): Promise<void> {
  setRegistering(true)
  const id = toast.loading("正在自动注册新账号，请稍候...")
  try {
    const response = await fetch(`${API_BASE}/api/admin/accounts/register`, adminRequest("POST"))
    const data = await response.json()
    if (data.activation_pending) toast.warning(`账号已注册，但仍需激活：${data.email}`, { id, duration: 8000 })
    else if (data.ok) toast.success(data.message || `注册成功：${data.email}`, { id, duration: 8000 })
    else toast.error(localizeError(data.error) || "自动注册失败", { id, duration: 8000 })
    onRefresh()
  } catch {
    toast.error("自动注册请求失败", { id })
  } finally {
    setRegistering(false)
  }
}

/**
 * 创建批量注册任务。
 *
 * @param batchCount 需要注册的账号数量。
 * @param setBatchRegistering 控制按钮加载态。
 * @param onRefresh 任务创建成功后刷新日志。
 */
async function registerBatchAccounts(batchCount: number, setBatchRegistering: Dispatch<SetStateAction<boolean>>, onRefresh: () => void): Promise<void> {
  if (batchCount < 1) {
    toast.error("批量数量至少为 1")
    return
  }
  setBatchRegistering(true)
  const id = toast.loading(`正在创建 ${batchCount} 个账号注册任务...`)
  try {
    const response = await fetch(`${API_BASE}/api/admin/accounts/register/batch`, adminRequest("POST", { quantity: Number(batchCount) }))
    const data = await response.json()
    if (!response.ok || !data.ok) return void toast.error(data.detail || data.error || "批量注册任务创建失败", { id, duration: 8000 })
    toast.success(`批量注册任务已创建：${data.batch_id}`, { id, duration: 8000 })
    onRefresh()
  } catch {
    toast.error("批量注册请求失败", { id })
  } finally {
    setBatchRegistering(false)
  }
}

/**
 * 删除指定邮箱的账号。
 *
 * @param targetEmail 要删除的账号邮箱。
 * @param onRefresh 删除成功后刷新账号列表。
 */
async function deleteAccountByEmail(targetEmail: string, onRefresh: () => void): Promise<void> {
  const id = toast.loading(`正在删除 ${targetEmail}...`)
  try {
    const response = await fetch(`${API_BASE}/api/admin/accounts/${encodeURIComponent(targetEmail)}`, adminRequest("DELETE"))
    if (!response.ok) throw new Error("delete failed")
    toast.success(`已删除 ${targetEmail}`, { id })
    onRefresh()
  } catch {
    toast.error("删除账号失败", { id })
  }
}

/**
 * 验证指定账号状态。
 *
 * @param targetEmail 需要验证的账号邮箱。
 * @param setVerifying 当前正在验证的邮箱状态写入函数。
 * @param onRefresh 验证结束后刷新账号列表。
 */
async function verifyOneAccount(targetEmail: string, setVerifying: Dispatch<SetStateAction<string | null>>, onRefresh: () => void): Promise<void> {
  setVerifying(targetEmail)
  const id = toast.loading(`正在验证 ${targetEmail}...`)
  try {
    const response = await fetch(`${API_BASE}/api/admin/accounts/${encodeURIComponent(targetEmail)}/verify`, adminRequest("POST"))
    const data = await response.json()
    if (data.valid) toast.success(`验证通过：${targetEmail}`, { id })
    else toast.error(`验证失败：${statusText(data) || localizeError(data.error)}`, { id, duration: 8000 })
    onRefresh()
  } catch {
    toast.error("验证请求失败", { id })
  } finally {
    setVerifying(null)
  }
}

/**
 * 并发巡检账号池中的全部账号。
 *
 * @param setVerifyingAll 控制全量巡检按钮加载态。
 * @param onRefresh 巡检结束后刷新账号列表。
 */
async function verifyAllAccounts(setVerifyingAll: Dispatch<SetStateAction<boolean>>, onRefresh: () => void): Promise<void> {
  setVerifyingAll(true)
  const id = toast.loading("正在并发巡检所有账号...")
  try {
    const response = await fetch(`${API_BASE}/api/admin/verify`, adminRequest("POST"))
    const data = await response.json()
    if (data.ok) toast.success(`全量巡检完成，并发数：${data.concurrency || 1}`, { id })
    else toast.error("全量巡检失败", { id })
    onRefresh()
  } catch {
    toast.error("全量巡检请求失败", { id })
  } finally {
    setVerifyingAll(false)
  }
}

/**
 * 激活待激活账号。
 *
 * @param targetEmail 需要激活的账号邮箱。
 * @param onRefresh 激活结束后刷新账号列表。
 */
async function activateOneAccount(targetEmail: string, onRefresh: () => void): Promise<void> {
  const id = toast.loading(`正在激活 ${targetEmail}...`)
  try {
    const response = await fetch(`${API_BASE}/api/admin/accounts/${encodeURIComponent(targetEmail)}/activate`, adminRequest("POST"))
    const data = await response.json()
    if (data.pending) toast.success(`账号正在激活中，请稍后刷新：${targetEmail}`, { id, duration: 6000 })
    else if (data.ok) toast.success(data.message || `激活成功：${targetEmail}`, { id, duration: 6000 })
    else toast.error(`激活失败：${localizeError(data.error || data.message)}`, { id, duration: 8000 })
    onRefresh()
  } catch {
    toast.error("激活请求失败", { id })
  }
}

/**
 * 维护账号页面的表单与按钮加载状态。
 *
 * @returns 手动注入、批量注册和巡检操作所需的状态集合；仅在当前页面生命周期内生效。
 */
function useAccountFormState() {
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [token, setToken] = useState("")
  const [batchCount, setBatchCount] = useState(1)
  const [registering, setRegistering] = useState(false)
  const [batchRegistering, setBatchRegistering] = useState(false)
  const [verifying, setVerifying] = useState<string | null>(null)
  const [verifyingAll, setVerifyingAll] = useState(false)
  return { email, setEmail, password, setPassword, token, setToken, batchCount, setBatchCount, registering, setRegistering, batchRegistering, setBatchRegistering, verifying, setVerifying, verifyingAll, setVerifyingAll }
}

/**
 * 维护注册日志的已生效筛选条件、草稿筛选器与分页状态。
 *
 * @returns 已提交筛选器、表头输入草稿和分页结果；只有点击筛选时才会把草稿提交到查询参数。
 */
function useRegisterLogsState() {
  const initial = defaultLogFilters()
  const [filters, setFilters] = useState<RegisterLogFilters>(initial)
  const [draftFilters, setDraftFilters] = useState<RegisterLogFilters>(initial)
  const [logPage, setLogPage] = useState<RegisterLogPage>(defaultLogPage())
  return { filters, setFilters, draftFilters, setDraftFilters, logPage, setLogPage }
}

/**
 * 渲染页面顶部操作区。
 *
 * @param props 页面当前加载态与操作回调。
 * @returns 账号管理页头部区域。
 */
function AccountHeader(props: AccountHeaderProps) {
  return (
    <div className="flex items-center justify-between"><div><h2 className="text-3xl font-extrabold tracking-tight">账号管理</h2><p className="mt-1 text-muted-foreground">统一管理上游账号池，并区分未激活、限流、封禁与失效状态。</p></div><div className="flex gap-2"><Button variant="secondary" onClick={props.onVerifyAll} disabled={props.verifyingAll}><ShieldCheck className={`mr-2 h-4 w-4 ${props.verifyingAll ? "animate-pulse" : ""}`} /> 全量巡检</Button><Button variant="outline" onClick={props.onRefresh}><RefreshCw className="mr-2 h-4 w-4" /> 刷新状态</Button><Button variant="default" onClick={props.onAutoRegister} disabled={props.registering}>{props.registering ? <RefreshCw className="mr-2 h-4 w-4 animate-spin" /> : <Bot className="mr-2 h-4 w-4" />}{props.registering ? "正在注册..." : "一键获取新号"}</Button></div></div>
  )
}

/**
 * 渲染账号状态统计卡片。
 *
 * @param stats 账号统计信息。
 * @returns 五列统计卡片网格。
 */
function StatsGrid({ stats }: { stats: AccountStats }) {
  const items = [["可用", stats.valid], ["未激活", stats.pending], ["限流", stats.rateLimited], ["封禁", stats.banned], ["其他失效", stats.invalid]]
  return (
    <div className="grid gap-3 md:grid-cols-5">{items.map(([label, value]) => <div key={label} className="rounded-xl border bg-card p-4"><div className="text-sm text-muted-foreground">{label}</div><div className="text-2xl font-bold">{value}</div></div>)}</div>
  )
}

/**
 * 渲染批量注册区域。
 *
 * @param props 批量注册表单状态与提交回调。
 * @returns 批量注册卡片。
 */
function BatchRegisterCard(props: BatchRegisterCardProps) {
  return (
    <div className="space-y-4 rounded-2xl border bg-card/40 p-6"><div><h3 className="text-base font-bold">批量注册</h3><p className="text-sm text-muted-foreground">输入数量 N，后台异步创建 N 个注册任务，并在下方查看日志。</p></div><div className="flex flex-col items-end gap-4 md:flex-row"><div className="w-full md:w-40"><label className="mb-1.5 block text-xs font-semibold">数量</label><input type="number" min="1" max="10" value={props.batchCount} onChange={event => props.setBatchCount(Number(event.target.value))} className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm" /></div><Button onClick={props.onSubmit} disabled={props.batchRegistering} className="h-10 w-full font-semibold md:w-auto">{props.batchRegistering ? <RefreshCw className="mr-2 h-4 w-4 animate-spin" /> : <Bot className="mr-2 h-4 w-4" />}一键获取 N 个账号</Button>{!!props.runningBatches.length && <div className="text-sm text-orange-600 dark:text-orange-400">运行中批次：{props.runningBatches.join(", ")}</div>}</div></div>
  )
}

/**
 * 渲染手动注入账号区域。
 *
 * @param props 表单字段状态与提交回调。
 * @returns 手动注入账号卡片。
 */
function ManualInjectCard(props: ManualInjectCardProps) {
  return (
    <div className="space-y-4 rounded-2xl border bg-card/40 p-6"><div><h3 className="text-base font-bold">手动注入账号</h3><p className="text-sm text-muted-foreground">如果你已经在 chat.qwen.ai 登录过，可以把 token 手动注入到账号池。</p></div><div className="flex flex-col items-end gap-4 md:flex-row"><div className="w-full flex-1"><label className="mb-1.5 block text-xs font-semibold">Token（必填）</label><input type="text" value={props.token} onChange={event => props.setToken(event.target.value)} className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm" placeholder="粘贴 token" /></div><div className="w-full md:w-64"><label className="mb-1.5 block text-xs font-semibold">邮箱（选填）</label><input type="text" value={props.email} onChange={event => props.setEmail(event.target.value)} className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm" placeholder="邮箱地址" /></div><div className="w-full md:w-64"><label className="mb-1.5 block text-xs font-semibold">密码（选填）</label><input type="text" value={props.password} onChange={event => props.setPassword(event.target.value)} className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm" placeholder="用于自动刷新或激活" /></div><Button onClick={props.onSubmit} variant="secondary" className="h-10 w-full font-semibold md:w-auto"><Plus className="mr-2 h-4 w-4" /> 注入账号</Button></div></div>
  )
}

/**
 * 渲染单行账号记录。
 *
 * @param props 当前账号与行级操作。
 * @returns 账号表格中的一行。
 */
function AccountRow(props: AccountRowProps) {
  return (
    <tr className="transition-colors hover:bg-black/5 dark:hover:bg-white/5"><td className="px-6 py-4 align-middle font-mono font-medium text-foreground/90">{props.account.email}</td><td className="px-6 py-4 align-middle"><span className={`inline-flex items-center rounded-full px-2.5 py-1 text-xs font-bold ring-1 ${statusStyle(props.account.status_code)}`}>{statusText(props.account)}</span></td><td className="px-6 py-4 align-middle font-mono"><span className="inline-flex items-center justify-center rounded border bg-muted/50 px-2 py-1 text-xs">{props.account.inflight || 0} 线程</span></td><td className="max-w-[420px] truncate px-6 py-4 align-middle text-muted-foreground" title={statusNote(props.account)}>{statusNote(props.account) || "-"}</td><td className="px-6 py-4 align-middle text-right"><div className="flex items-center justify-end gap-2">{props.account.status_code !== "valid" && props.account.status_code !== "rate_limited" && props.account.status_code !== "banned" && <Button variant="outline" size="sm" onClick={() => props.onActivate(props.account.email)} className="border-orange-500/30 font-medium text-orange-600 hover:bg-orange-500/10 dark:text-orange-400"><MailWarning className="mr-1 h-4 w-4" /> 激活</Button>}<Button variant="outline" size="sm" onClick={() => props.onVerify(props.account.email)} disabled={props.verifying === props.account.email}>{props.verifying === props.account.email ? <RefreshCw className="h-4 w-4 animate-spin text-blue-500" /> : <ShieldCheck className="h-4 w-4" />}</Button><Button variant="ghost" size="sm" onClick={() => props.onDelete(props.account.email)} className="text-destructive hover:bg-destructive/10 hover:text-destructive"><Trash2 className="h-4 w-4" /></Button></div></td></tr>
  )
}

/**
 * 渲染账号列表表格。
 *
 * @param props 列表数据与行级事件处理函数。
 * @returns 账号管理表格。
 */
function AccountsTable(props: AccountsTableProps) {
  return (
    <div className="overflow-hidden rounded-2xl border bg-card/30"><div className="flex items-center justify-between border-b bg-muted/10 p-6"><h3 className="text-xl font-bold">账号列表</h3><span className="inline-flex items-center justify-center rounded-full bg-primary/10 px-3 py-1 text-xs font-bold text-primary">{props.accounts.length}</span></div><table className="w-full text-left text-sm"><thead className="border-b bg-muted/30 text-xs font-semibold uppercase tracking-wider text-muted-foreground"><tr><th className="h-12 px-6 align-middle">账号</th><th className="h-12 px-6 align-middle">状态</th><th className="h-12 px-6 align-middle">并发负载</th><th className="h-12 px-6 align-middle">说明</th><th className="h-12 px-6 align-middle text-right">操作</th></tr></thead><tbody className="divide-y divide-border/50">{!props.accounts.length && <tr><td colSpan={5} className="px-6 py-12 text-center text-muted-foreground">暂无账号，请手动注入或一键获取新号。</td></tr>}{props.accounts.map(account => <AccountRow key={account.email} account={account} verifying={props.verifying} onActivate={props.onActivate} onVerify={props.onVerify} onDelete={props.onDelete} />)}</tbody></table></div>
  )
}

/**
 * 渲染注册日志表头筛选行。
 *
 * @param props 当前筛选条件、运行中批次和筛选动作。
 * @returns 表头下方的筛选输入行；仅影响日志查询，不会改动账号列表。
 */
function RegisterLogsFilterRow(props: RegisterLogsFilterRowProps) {
  return (
    <tr className="border-b bg-muted/10"><th className="px-4 py-3 align-middle"><input value={props.draftFilters.batchId} onChange={event => props.setDraftFilters(current => ({ ...current, batchId: event.target.value }))} placeholder="筛选批次" className="h-9 w-full rounded-md border border-input bg-background px-3 text-xs font-normal" /></th><th className="px-4 py-3 align-middle text-center text-xs font-normal text-muted-foreground">筛选当前页</th><th className="px-4 py-3 align-middle"><input value={props.draftFilters.account} onChange={event => props.setDraftFilters(current => ({ ...current, account: event.target.value }))} placeholder="筛选邮箱" className="h-9 w-full rounded-md border border-input bg-background px-3 text-xs font-normal" /></th><th className="px-4 py-3 align-middle"><select value={props.draftFilters.status} onChange={event => props.setDraftFilters(current => ({ ...current, status: event.target.value }))} className="h-9 w-full rounded-md border border-input bg-background px-3 text-xs font-normal"><option value="">全部状态</option><option value="pending">排队中</option><option value="running">注册中</option><option value="success">成功</option><option value="pending_activation">待激活</option><option value="failed">失败</option></select></th><th className="px-4 py-3 align-middle text-xs font-normal text-muted-foreground">{props.runningBatches.length ? `运行中：${props.runningBatches.join(", ")}` : "当前无运行中批次"}</th><th className="px-4 py-3 align-middle"><div className="flex justify-end gap-2"><Button size="sm" variant="outline" onClick={props.onReset}>重置</Button><Button size="sm" onClick={props.onApply}>筛选</Button></div></th></tr>
  )
}

/**
 * 渲染注册日志单行记录。
 *
 * @param log 当前日志项。
 * @returns 注册日志表中的一行。
 */
function RegisterLogRow({ log }: { log: RegisterLog }) {
  return (
    <tr className="transition-colors hover:bg-black/5 dark:hover:bg-white/5"><td className="px-6 py-4 align-middle font-mono">{log.batch_id}</td><td className="px-6 py-4 align-middle">#{log.sequence}</td><td className="px-6 py-4 align-middle font-mono">{log.account?.email || "-"}</td><td className="px-6 py-4 align-middle">{logText(log.status)}</td><td className="px-6 py-4 align-middle text-muted-foreground">{log.finished_at || log.started_at || log.created_at}</td><td className="max-w-[420px] truncate px-6 py-4 align-middle text-muted-foreground" title={log.error || ""}>{log.error || "-"}</td></tr>
  )
}

/**
 * 渲染注册日志分页器。
 *
 * @param props 当前页、总页数和跳页回调。
 * @returns 上一页下一页操作区；到边界时自动禁用按钮。
 */
function RegisterLogsPager(props: { page: number; total: number; totalPages: number; onPageChange: (page: number) => void }) {
  return (
    <div className="flex items-center justify-between border-t bg-muted/10 px-6 py-4"><div className="text-sm text-muted-foreground">第 {props.page} / {props.totalPages} 页，共 {props.total} 条</div><div className="flex gap-2"><Button variant="outline" size="sm" disabled={props.page <= 1} onClick={() => props.onPageChange(props.page - 1)}>上一页</Button><Button variant="outline" size="sm" disabled={props.page >= props.totalPages} onClick={() => props.onPageChange(props.page + 1)}>下一页</Button></div></div>
  )
}

/**
 * 渲染注册日志表格、表头筛选和分页器。
 *
 * @param props 日志分页数据、筛选状态与翻页回调。
 * @returns 可分页、可筛选的注册日志区域；当日志被压缩切片后仍可查询归档数据。
 */
function RegisterLogsTable(props: RegisterLogsTableProps) {
  return (
    <div className="overflow-hidden rounded-2xl border bg-card/30"><div className="flex items-center justify-between border-b bg-muted/10 p-6"><div><h3 className="text-xl font-bold">注册日志</h3><p className="text-sm text-muted-foreground">支持按批次、账号和状态筛选，旧日志会自动压缩切割。</p></div><span className="inline-flex items-center justify-center rounded-full bg-primary/10 px-3 py-1 text-xs font-bold text-primary">{props.logPage.total}</span></div><table className="w-full text-left text-sm"><thead className="bg-muted/30 text-xs font-semibold uppercase tracking-wider text-muted-foreground"><tr><th className="h-12 px-6 align-middle">批次</th><th className="h-12 px-6 align-middle">序号</th><th className="h-12 px-6 align-middle">账号</th><th className="h-12 px-6 align-middle">状态</th><th className="h-12 px-6 align-middle">时间</th><th className="h-12 px-6 align-middle text-right">错误信息 / 操作</th></tr><RegisterLogsFilterRow draftFilters={props.draftFilters} setDraftFilters={props.setDraftFilters} onApply={props.onApplyFilters} onReset={props.onResetFilters} runningBatches={props.logPage.runningBatches} /></thead><tbody className="divide-y divide-border/50">{!props.logPage.logs.length && <tr><td colSpan={6} className="px-6 py-12 text-center text-muted-foreground">暂无匹配的注册日志。</td></tr>}{props.logPage.logs.map(log => <RegisterLogRow key={log.id} log={log} />)}</tbody></table><RegisterLogsPager page={props.logPage.page} total={props.logPage.total} totalPages={props.logPage.totalPages} onPageChange={props.onPageChange} /></div>
  )
}

/**
 * 账号管理页：负责账号列表、批量注册和注册日志展示。
 *
 * @returns 后台账号管理页面；日志区支持分页筛选，运行中批次会自动轮询刷新。
 */
export default function AccountsPage() {
  const [accounts, setAccounts] = useState<AccountItem[]>([])
  const form = useAccountFormState()
  const { filters, setFilters, draftFilters, setDraftFilters, logPage, setLogPage } = useRegisterLogsState()
  const stats = useMemo(() => buildAccountStats(accounts), [accounts])
  const refreshDashboard = () => refreshAccountDashboard(setAccounts, setLogPage, filters, logPage.page)
  const refreshLogs = (page = logPage.page) => void loadRegisterLogs(filters, page, setLogPage)
  const applyLogFilters = () => {
    setFilters(draftFilters)
    void loadRegisterLogs(draftFilters, 1, setLogPage)
  }
  const resetLogFilters = () => {
    const next = defaultLogFilters()
    setFilters(next)
    setDraftFilters(next)
    void loadRegisterLogs(next, 1, setLogPage)
  }
  useEffect(() => { refreshAccountDashboard(setAccounts, setLogPage, defaultLogFilters(), 1) }, [])
  useBatchPolling(logPage.runningBatches, refreshDashboard)
  return (
    <div className="relative space-y-6"><AccountHeader registering={form.registering} verifyingAll={form.verifyingAll} onVerifyAll={() => void verifyAllAccounts(form.setVerifyingAll, () => void loadAccounts(setAccounts))} onRefresh={() => { refreshDashboard(); toast.success("数据已刷新") }} onAutoRegister={() => void registerSingleAccount(form.setRegistering, refreshDashboard)} /><StatsGrid stats={stats} /><BatchRegisterCard batchCount={form.batchCount} setBatchCount={form.setBatchCount} batchRegistering={form.batchRegistering} runningBatches={logPage.runningBatches} onSubmit={() => void registerBatchAccounts(form.batchCount, form.setBatchRegistering, () => refreshLogs(1))} /><ManualInjectCard email={form.email} setEmail={form.setEmail} password={form.password} setPassword={form.setPassword} token={form.token} setToken={form.setToken} onSubmit={() => void addManualAccount(form.email, form.password, form.token, form.setEmail, form.setPassword, form.setToken, refreshDashboard)} /><AccountsTable accounts={accounts} verifying={form.verifying} onActivate={targetEmail => void activateOneAccount(targetEmail, () => void loadAccounts(setAccounts))} onVerify={targetEmail => void verifyOneAccount(targetEmail, form.setVerifying, () => void loadAccounts(setAccounts))} onDelete={targetEmail => void deleteAccountByEmail(targetEmail, () => void loadAccounts(setAccounts))} /><RegisterLogsTable logPage={logPage} draftFilters={draftFilters} setDraftFilters={setDraftFilters} onApplyFilters={applyLogFilters} onResetFilters={resetLogFilters} onPageChange={page => refreshLogs(page)} /></div>
  )
}
