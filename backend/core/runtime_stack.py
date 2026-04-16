"""运行时网络栈管理：负责根据当前配置构建或刷新网关引擎。"""

from backend.core.browser_engine import BrowserEngine
from backend.core.config import settings
from backend.core.httpx_engine import HttpxEngine
from backend.core.hybrid_engine import HybridEngine


def build_gateway_engine(browser_engine: BrowserEngine, httpx_engine: HttpxEngine):
    """根据当前引擎模式返回实际对外使用的网关引擎实例。"""
    if settings.ENGINE_MODE == "httpx":
        return httpx_engine
    if settings.ENGINE_MODE == "hybrid":
        return HybridEngine(browser_engine, httpx_engine)
    return browser_engine


async def refresh_gateway_stack(app) -> None:
    """重建浏览器引擎，并按当前模式重新绑定和启动网关引擎。"""
    old_browser = getattr(app.state, "browser_engine", None)
    if old_browser is not None:
        await old_browser.stop()
    browser_engine = BrowserEngine(pool_size=settings.BROWSER_POOL_SIZE)
    app.state.browser_engine = browser_engine
    app.state.gateway_engine = build_gateway_engine(browser_engine, app.state.httpx_engine)
    app.state.qwen_client.engine = app.state.gateway_engine
    mode = settings.ENGINE_MODE
    if mode == "hybrid":
        await app.state.gateway_engine.start()
        return
    if mode == "browser":
        await app.state.browser_engine.start()
        return
    await app.state.httpx_engine.start()
