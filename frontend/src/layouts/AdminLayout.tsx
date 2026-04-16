import { Outlet, Link, useLocation } from "react-router-dom"
import { Activity, Key, Settings, LayoutDashboard, MessageSquare, Menu, X, Image } from "lucide-react"
import { useState } from "react"

type SidebarBackdropProps = {
  open: boolean
  onClose: () => void
}

type SidebarNavProps = {
  open: boolean
  onClose: () => void
}

const NAV_ITEMS = [
  { name: "运行状态", path: "/", icon: LayoutDashboard },
  { name: "账号管理", path: "/accounts", icon: Activity },
  { name: "API Key", path: "/tokens", icon: Key },
  { name: "接口测试", path: "/test", icon: MessageSquare },
  { name: "图片生成", path: "/images", icon: Image },
  { name: "系统设置", path: "/settings", icon: Settings },
]

/**
 * 渲染移动端侧栏遮罩层。
 *
 * @param props 遮罩层显示状态与关闭回调。
 * @returns 仅在移动端侧栏展开时显示的全屏遮罩。
 */
function SidebarBackdrop(props: SidebarBackdropProps) {
  if (!props.open) return null
  return (
    <div
      className="fixed inset-0 z-40 bg-black/20 backdrop-blur-sm transition-opacity dark:bg-black/50 md:hidden"
      onClick={props.onClose}
    />
  )
}

/**
 * 渲染桌面端固定、移动端抽屉式的侧边导航。
 *
 * @param props 侧栏开关状态与导航关闭回调。
 * @returns 带菜单项的导航侧栏；桌面端保持固定不跟随内容区滚动。
 */
function SidebarNav(props: SidebarNavProps) {
  const loc = useLocation()
  const stateClass = props.open ? "translate-x-0" : "-translate-x-full md:translate-x-0"
  return (
    <aside className={`fixed inset-y-0 left-0 z-50 flex w-64 shrink-0 flex-col border-r border-border/40 bg-card/90 shadow-2xl shadow-black/5 transition-transform duration-300 backdrop-blur-xl dark:shadow-black/50 md:sticky md:top-0 md:h-screen md:bg-card/50 ${stateClass}`}>
      <div className="flex h-16 items-center justify-between border-b border-border/40 px-6"><div className="bg-gradient-to-br from-indigo-500 to-purple-500 bg-clip-text text-xl font-extrabold tracking-tight text-transparent">qwen2API</div><button className="text-muted-foreground transition-colors hover:text-foreground md:hidden" onClick={props.onClose}><X className="h-5 w-5" /></button></div>
      <nav className="flex-1 space-y-2 overflow-y-auto p-4">
        {NAV_ITEMS.map(item => {
          const active = loc.pathname === item.path
          const tone = active ? "bg-primary/10 text-primary ring-1 ring-primary/20" : "text-muted-foreground hover:bg-black/5 hover:text-foreground dark:hover:bg-white/5"
          return <Link key={item.path} to={item.path} onClick={props.onClose} className={`flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-all duration-300 ${tone}`}><item.icon className={`h-4 w-4 ${active ? "drop-shadow-[0_0_8px_rgba(168,85,247,0.5)]" : ""}`} />{item.name}</Link>
        })}
      </nav>
    </aside>
  )
}

/**
 * 渲染移动端顶部栏。
 *
 * @param props 打开侧栏的点击回调。
 * @returns 仅在移动端显示的页面头部。
 */
function MobileHeader(props: { onOpen: () => void }) {
  return (
    <header className="z-10 flex h-16 items-center justify-between border-b border-border/40 bg-card/80 px-6 shadow-sm backdrop-blur-xl md:hidden">
      <div className="bg-gradient-to-br from-indigo-500 to-purple-500 bg-clip-text text-lg font-extrabold text-transparent">qwen2API</div>
      <button className="text-muted-foreground transition-colors hover:text-foreground" onClick={props.onOpen}><Menu className="h-6 w-6" /></button>
    </header>
  )
}

/**
 * 管理后台整体布局。
 *
 * @returns 包含固定侧边导航与独立内容滚动区的后台框架；桌面端侧栏不会跟随页面内容滚动。
 */
export default function AdminLayout() {
  const [mobileOpen, setMobileOpen] = useState(false)
  return (
    <div className="flex h-screen w-full overflow-hidden bg-background text-foreground transition-colors duration-300">
      <SidebarBackdrop open={mobileOpen} onClose={() => setMobileOpen(false)} />
      <SidebarNav open={mobileOpen} onClose={() => setMobileOpen(false)} />
      <main className="relative flex min-w-0 flex-1 flex-col">
        <MobileHeader onOpen={() => setMobileOpen(true)} />
        <div className="z-0 flex-1 overflow-y-auto p-6 md:p-8"><div className="mx-auto max-w-6xl animate-fade-in-up"><Outlet /></div></div>
      </main>
    </div>
  )
}
