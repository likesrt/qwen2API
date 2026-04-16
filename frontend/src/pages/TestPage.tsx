import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react"
import { Button } from "../components/ui/button"
import { Send, RefreshCw, Bot } from "lucide-react"
import { getAuthHeader } from "../lib/auth"
import { API_BASE } from "../lib/api"
import { toast } from "sonner"

// 渲染消息内容：自动把 Markdown 图片和图片 URL 渲染成 <img>
function MessageContent({ content }: { content: string }) {
  type Seg = { start: number; end: number; url: string }
  const segs: Seg[] = []
  const fullRe = /!\[[^\]]*\]\((https?:\/\/[^)\s]+)\)|(https?:\/\/[^\s"<>]+\.(?:jpg|jpeg|png|webp|gif)[^\s"<>]*)/gi
  let m: RegExpExecArray | null
  while ((m = fullRe.exec(content)) !== null) {
    segs.push({ start: m.index, end: m.index + m[0].length, url: (m[1] || m[2]) as string })
  }

  if (segs.length === 0) {
    return <div className="whitespace-pre-wrap leading-relaxed">{content}</div>
  }

  const nodes: JSX.Element[] = []
  let cursor = 0
  segs.forEach((seg, i) => {
    if (seg.start > cursor) {
      nodes.push(<span key={"t" + i}>{content.slice(cursor, seg.start)}</span>)
    }
    nodes.push(
      <div key={"i" + i} className="my-2">
        <img
          src={seg.url}
          alt="generated"
          className="max-w-full rounded-lg shadow-md border"
          loading="lazy"
          onError={e => { (e.currentTarget as HTMLImageElement).style.display = "none" }}
        />
        <div className="text-xs text-muted-foreground mt-1 break-all font-mono">{seg.url}</div>
      </div>
    )
    cursor = seg.end
  })
  if (cursor < content.length) {
    nodes.push(<span key="tail">{content.slice(cursor)}</span>)
  }
  return <div className="whitespace-pre-wrap leading-relaxed">{nodes}</div>
}

type ChatMessage = {
  role: string
  content: string
  reasoning?: string
  error?: boolean
}

/**
 * 提取兼容 OpenAI 风格错误对象的可读错误文本。
 *
 * @param error 原始错误对象或字符串。
 * @returns 适合直接展示给用户的错误文本。
 * @remarks 后端现在返回标准 error 对象，这里优先读取 message，避免页面只显示 [object Object]。
 */
function extractErrorMessage(error: unknown): string {
  if (typeof error === "string") return error
  if (error && typeof error === "object" && "message" in error && typeof error.message === "string") return error.message
  return JSON.stringify(error)
}


/**
 * 构造兼容 OpenAI 扩展字段的助手消息更新对象。
 *
 * @param content 当前累计的正文内容。
 * @param reasoning 当前累计的思考内容。
 * @returns 可直接写入消息列表的 assistant 消息对象。
 * @remarks 当 thinking 先于正文到达时，仍要保留 reasoning，避免测试页看起来长时间无输出。
 */
function buildAssistantMessage(content: string, reasoning: string): ChatMessage {
  return { role: "assistant", content, reasoning }
}

type TestPageState = {
  input: string
  loading: boolean
  model: string
  stream: boolean
  enableThinking: boolean
}

/**
 * 构造测试页请求体，并按前端开关透传 thinking 配置。
 *
 * @param state 当前页面状态。
 * @param messages 已有消息列表。
 * @param userMsg 当前待发送的用户消息。
 * @returns 发送到 OpenAI 兼容接口的请求体。
 * @remarks 只在开启思考时透传 enable_thinking，和后端默认关闭策略保持一致。
 */
function buildRequestBody(state: TestPageState, messages: ChatMessage[], userMsg: ChatMessage) {
  return {
    model: state.model,
    messages: [...messages, userMsg],
    stream: state.stream,
    ...(state.enableThinking ? { enable_thinking: true } : {})
  }
}

/**
 * 用新消息替换列表中的最后一条助手消息。
 *
 * @param setMessages 消息状态更新函数。
 * @param message 新的助手消息。
 * @returns 无返回值。
 * @remarks 流式过程中始终只覆盖最后一条助手消息，避免重复插入气泡。
 */
function replaceLastMessage(setMessages: Dispatch<SetStateAction<ChatMessage[]>>, message: ChatMessage) {
  setMessages(prev => {
    const msgs = [...prev]
    msgs[msgs.length - 1] = message
    return msgs
  })
}

/**
 * 追加最新的正文或 thinking 分片到最后一条助手消息。
 *
 * @param setMessages 消息状态更新函数。
 * @param content 最新正文分片。
 * @param reasoning 最新思考分片。
 * @returns 无返回值。
 * @remarks thinking 和正文可能交错到达，因此这里要分别累计。
 */
function appendAssistantDelta(setMessages: Dispatch<SetStateAction<ChatMessage[]>>, content: string, reasoning: string) {
  setMessages(prev => {
    const msgs = [...prev]
    const last = msgs[msgs.length - 1]
    msgs[msgs.length - 1] = buildAssistantMessage((last.content ?? "") + content, (last.reasoning ?? "") + reasoning)
    return msgs
  })
}

/**
 * 解析单条 SSE 数据行，并更新最后一条助手消息。
 *
 * @param rawLine 原始 SSE 行文本。
 * @param setMessages 消息状态更新函数。
 * @returns 当前行是否产出了有效载荷。
 * @remarks 后端会用 reasoning_content 作为网关扩展字段，因此这里同时读取 reasoning 和正文。
 */
function processStreamLine(rawLine: string, setMessages: Dispatch<SetStateAction<ChatMessage[]>>): boolean {
  const line = rawLine.trim()
  if (!line || line.startsWith(":") || line === "data: [DONE]" || !line.startsWith("data: ")) return false
  try {
    const data = JSON.parse(line.slice(6))
    if (data.error) {
      replaceLastMessage(setMessages, { role: "assistant", content: `❌ ${extractErrorMessage(data.error)}`, error: true })
      return true
    }
    const delta = data.choices?.[0]?.delta ?? {}
    const content: string = delta.content ?? ""
    const reasoning: string = delta.reasoning_content ?? ""
    if (!content && !reasoning) return false
    appendAssistantDelta(setMessages, content, reasoning)
    return true
  } catch {
    return false
  }
}

/**
 * 处理非流式响应，并兼容展示 reasoning_content。
 *
 * @param res 非流式 HTTP 响应。
 * @param setMessages 消息状态更新函数。
 * @returns 无返回值。
 * @remarks 后端错误对象已经标准化，这里优先读取 error.message。
 */
async function handleNonStreamResponse(res: Response, setMessages: Dispatch<SetStateAction<ChatMessage[]>>) {
  const data = await res.json()
  if (data.error) return setMessages(prev => [...prev, { role: "assistant", content: `❌ ${extractErrorMessage(data.error)}`, error: true }])
  if (data.choices?.[0]) {
    const message = data.choices[0].message
    return setMessages(prev => [...prev, buildAssistantMessage(message.content ?? "", message.reasoning_content ?? "")])
  }
  setMessages(prev => [...prev, { role: "assistant", content: `❌ 未知响应: ${JSON.stringify(data)}`, error: true }])
}

/**
 * 处理流式响应，并把正文与 thinking 分片实时写回页面。
 *
 * @param res 流式 HTTP 响应。
 * @param setMessages 消息状态更新函数。
 * @returns 无返回值。
 * @remarks 这里要缓存半行文本，避免浏览器把单条 SSE 切在 JSON 中间导致解析失败。
 */
async function handleStreamResponse(res: Response, setMessages: Dispatch<SetStateAction<ChatMessage[]>>) {
  if (!res.ok) {
    const errText = await res.text()
    return setMessages(prev => [...prev, { role: "assistant", content: `❌ HTTP ${res.status}: ${errText}`, error: true }])
  }
  if (!res.body) throw new Error("No response body")
  setMessages(prev => [...prev, buildAssistantMessage("", "")])
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let hasPayload = false
  let pending = ""
  while (true) {
    const { done, value } = await reader.read()
    pending += done ? decoder.decode() : decoder.decode(value, { stream: true })
    const lines = pending.split("\n")
    pending = done ? "" : lines.pop() ?? ""
    hasPayload = lines.some(line => processStreamLine(line, setMessages)) || hasPayload
    if (done) break
  }
  if (!hasPayload) replaceLastMessage(setMessages, { role: "assistant", content: "❌ 响应为空（账号可能未激活或无可用账号）", error: true })
}

/**
 * 发起一次聊天请求，并根据 stream 开关选择响应处理方式。
 *
 * @param state 当前页面状态。
 * @param messages 已有消息列表。
 * @param userMsg 当前待发送的用户消息。
 * @param setMessages 消息状态更新函数。
 * @returns 无返回值。
 * @remarks 请求体会透传 enable_thinking，方便验证后端的 thinking 开关适配。
 */
async function sendChatRequest(state: TestPageState, messages: ChatMessage[], userMsg: ChatMessage, setMessages: Dispatch<SetStateAction<ChatMessage[]>>) {
  const res = await fetch(`${API_BASE}/v1/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...getAuthHeader() },
    body: JSON.stringify(buildRequestBody(state, messages, userMsg))
  })
  if (state.stream) return handleStreamResponse(res, setMessages)
  return handleNonStreamResponse(res, setMessages)
}

/**
 * 渲染测试页顶部控制区。
 *
 * @param props 当前控制项与回调。
 * @returns 顶部标题与开关区域。
 * @remarks 把思考开关放在测试页里，便于直接验证前端显式开启时的后端透传行为。
 */
function TestPageHeader(props: { state: TestPageState; onModelChange: (model: string) => void; onStreamToggle: () => void; onThinkingToggle: () => void; onClear: () => void }) {
  return <div className="flex justify-between items-center">
    <div>
      <h2 className="text-2xl font-bold tracking-tight">接口测试</h2>
      <p className="text-muted-foreground">在此测试您的 API 分发是否正常工作。</p>
    </div>
    <div className="flex gap-4 items-center">
      <div className="flex items-center gap-2 text-sm bg-card border px-3 py-1.5 rounded-md"><span className="font-medium text-muted-foreground">模型:</span><select value={props.state.model} onChange={e => props.onModelChange(e.target.value)} className="bg-transparent font-mono outline-none"><option value="qwen3.6-plus">qwen3.6-plus</option><option value="qwen-max">qwen-max</option><option value="qwen-turbo">qwen-turbo</option></select></div>
      <label className="flex items-center gap-2 text-sm bg-card border px-3 py-1.5 rounded-md cursor-pointer"><input type="checkbox" checked={props.state.stream} onChange={props.onStreamToggle} className="cursor-pointer" /><span className="font-medium">流式传输</span></label>
      <label className="flex items-center gap-2 text-sm bg-card border px-3 py-1.5 rounded-md cursor-pointer"><input type="checkbox" checked={props.state.enableThinking} onChange={props.onThinkingToggle} className="cursor-pointer" /><span className="font-medium">开启思考</span></label>
      <Button variant="outline" onClick={props.onClear}><RefreshCw className="mr-2 h-4 w-4" /> 清空对话</Button>
    </div>
  </div>
}

/**
 * 渲染单条消息气泡，并在正文前展示 Thinking 区块。
 *
 * @param props 单条消息与加载状态。
 * @returns 单条消息气泡节点。
 * @remarks 当只有 thinking 尚无正文时，也要先展示思考内容，减少首字延时体感。
 */
function MessageBubble(props: { msg: ChatMessage; loading: boolean }) {
  const { msg, loading } = props
  const className = msg.role === "user" ? "bg-primary text-primary-foreground" : msg.error ? "bg-red-500/10 border border-red-500/30 text-red-400" : "bg-muted/30 border text-foreground"
  return <div className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
    <div className={`max-w-[80%] rounded-xl px-4 py-3 text-sm shadow-sm ${className}`}>
      {msg.role === "assistant" && !msg.content && !msg.reasoning && loading ? <span className="animate-pulse flex items-center gap-2 text-muted-foreground"><Bot className="h-4 w-4" /> 思考中...</span> : msg.role === "assistant" && !msg.error ? <div className="space-y-3">{msg.reasoning && <div className="rounded-lg border border-dashed bg-background/60 px-3 py-2"><div className="mb-1 text-xs font-medium text-muted-foreground">Thinking</div><div className="whitespace-pre-wrap leading-relaxed text-muted-foreground">{msg.reasoning}</div></div>}{msg.content && <MessageContent content={msg.content} />}</div> : <div className="whitespace-pre-wrap leading-relaxed">{msg.content}</div>}
    </div>
  </div>
}

/**
 * 渲染消息列表区域，并在列表变化时保持滚动到底部。
 *
 * @param props 消息列表、加载状态与底部锚点。
 * @returns 完整消息面板节点。
 * @remarks 空状态提示保留不变，只增加 thinking 内容的展示能力。
 */
function MessageList(props: { messages: ChatMessage[]; loading: boolean }) {
  return <>
    {props.messages.length === 0 && <div className="h-full flex flex-col items-center justify-center text-muted-foreground space-y-4"><Bot className="h-12 w-12 text-muted-foreground/30" /><p className="text-sm">发送一条消息以开始测试，系统将通过 /v1/chat/completions 进行调用。</p></div>}
    {props.messages.map((msg, i) => <MessageBubble key={i} msg={msg} loading={props.loading} />)}
  </>
}

/**
 * 渲染底部输入区，并处理回车发送。
 *
 * @param props 输入值、加载状态与交互回调。
 * @returns 输入框与发送按钮节点。
 * @remarks 发送中会禁用输入，避免用户重复提交同一条消息。
 */
function ChatComposer(props: { input: string; loading: boolean; onInputChange: (value: string) => void; onSend: () => void }) {
  return <div className="p-4 border-t bg-muted/30 flex gap-3 items-center">
    <input type="text" value={props.input} onChange={e => props.onInputChange(e.target.value)} onKeyDown={e => e.key === "Enter" && props.onSend()} className="flex h-12 w-full rounded-md border border-input bg-background px-4 py-2 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50" placeholder="输入测试消息..." disabled={props.loading} />
    <Button onClick={props.onSend} disabled={props.loading || !props.input.trim()} className="h-12 px-6">{props.loading ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}</Button>
  </div>
}

/**
 * 渲染接口测试页，并兼容展示普通回答与 thinking 内容。
 *
 * @returns 测试页面的 React 节点。
 * @remarks 当前页会把开启思考开关透传为 enable_thinking，便于直接验证兼容层行为。
 */
export default function TestPage() {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [state, setState] = useState<TestPageState>({ input: "", loading: false, model: "qwen3.6-plus", stream: true, enableThinking: false })
  const bottomRef = useRef<HTMLDivElement>(null)
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth" }) }, [messages])
  /**
   * 发送当前输入框内容，并在请求结束后恢复按钮状态。
   *
   * @returns 无返回值。
   * @remarks 这里使用提交前的 state 快照发请求，避免 setState 后读取到已清空的输入值。
   */
  const handleSend = async () => {
    const text = state.input.trim()
    if (!text || state.loading) return
    const userMsg = { role: "user", content: text }
    setMessages(prev => [...prev, userMsg])
    setState(prev => ({ ...prev, input: "", loading: true }))
    try { await sendChatRequest(state, messages, userMsg, setMessages) } catch (err: unknown) { const errorText = extractErrorMessage(err); toast.error(`网络错误: ${errorText}`); setMessages(prev => [...prev, { role: "assistant", content: `❌ 网络错误: ${errorText}`, error: true }]) } finally { setState(prev => ({ ...prev, loading: false })) }
  }
  return <div className="flex flex-col h-[calc(100vh-10rem)] space-y-4 max-w-5xl mx-auto"><TestPageHeader state={state} onModelChange={model => setState(prev => ({ ...prev, model }))} onStreamToggle={() => setState(prev => ({ ...prev, stream: !prev.stream }))} onThinkingToggle={() => setState(prev => ({ ...prev, enableThinking: !prev.enableThinking }))} onClear={() => setMessages([])} /><div className="flex-1 rounded-xl border bg-card overflow-hidden flex flex-col shadow-sm"><div className="flex-1 overflow-y-auto p-6 space-y-6 flex flex-col"><MessageList messages={messages} loading={state.loading} /><div ref={bottomRef} /></div><ChatComposer input={state.input} loading={state.loading} onInputChange={input => setState(prev => ({ ...prev, input }))} onSend={() => { void handleSend() }} /></div></div>
}
