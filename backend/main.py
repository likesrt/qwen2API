"""应用主入口：负责初始化存储、引擎和 API 路由。"""

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Windows UTF-8 输出修复
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 将项目根目录加入到 sys.path，解决直接运行 main.py 时找不到 backend 模块的问题
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.api import admin, anthropic, embeddings, gemini, images, probes, v1_chat, v1_models
from backend.core.account_pool import AccountPool
from backend.core.browser_engine import BrowserEngine
from backend.core.config import settings
from backend.core.database import AsyncJsonDB
from backend.core.httpx_engine import HttpxEngine
from backend.core.runtime_config import apply_runtime_config, current_runtime_config
from backend.core.runtime_stack import build_gateway_engine
from backend.services.garbage_collector import garbage_collect_chats
from backend.services.qwen_client import QwenClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("qwen2api")


async def _init_storage(app: FastAPI) -> None:
    """初始化 JSON 存储和批量注册任务容器。"""
    app.state.accounts_db = AsyncJsonDB(settings.ACCOUNTS_FILE, default_data=[])
    app.state.users_db = AsyncJsonDB(settings.USERS_FILE, default_data=[])
    app.state.captures_db = AsyncJsonDB(settings.CAPTURES_FILE, default_data=[])
    app.state.config_db = AsyncJsonDB(settings.CONFIG_FILE, default_data=current_runtime_config())
    app.state.register_logs_db = AsyncJsonDB(settings.REGISTER_LOG_FILE, default_data=[])
    app.state.register_tasks = {}


async def _load_runtime_state(app: FastAPI) -> None:
    """从持久化配置中恢复可热更新的运行时配置。"""
    config = await app.state.config_db.get()
    if isinstance(config, dict):
        apply_runtime_config(config, pool=app.state.account_pool)


async def _start_gateway(app: FastAPI) -> None:
    """创建并启动当前引擎模式对应的网关实例。"""
    browser_engine = BrowserEngine(pool_size=settings.BROWSER_POOL_SIZE)
    httpx_engine = HttpxEngine(base_url="https://chat.qwen.ai")
    engine = build_gateway_engine(browser_engine, httpx_engine)
    app.state.browser_engine = browser_engine
    app.state.httpx_engine = httpx_engine
    app.state.gateway_engine = engine
    app.state.qwen_client = QwenClient(engine, app.state.account_pool)
    await engine.start()
    if settings.ENGINE_MODE == "httpx":
        log.info("引擎模式: httpx 直连")
    elif settings.ENGINE_MODE == "hybrid":
        log.info("引擎模式: Hybrid (api_call=httpx优先, fetch_chat=browser)")
    else:
        log.info("引擎模式: Camoufox 浏览器")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """在应用启动和关闭时初始化并释放核心资源。"""
    log.info("Starting qwen2API v2.0 Enterprise Gateway...")
    await _init_storage(app)
    app.state.account_pool = AccountPool(app.state.accounts_db, max_inflight=settings.MAX_INFLIGHT_PER_ACCOUNT)
    await app.state.account_pool.load()
    await _load_runtime_state(app)
    await _start_gateway(app)
    asyncio.create_task(garbage_collect_chats(app.state.qwen_client))
    yield
    log.info("Shutting down gateway...")
    await app.state.gateway_engine.stop()


app = FastAPI(title="qwen2API Enterprise Gateway", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载路由
app.include_router(v1_chat.router, tags=["OpenAI Compatible"])
app.include_router(v1_models.router, tags=["OpenAI Models"])
app.include_router(images.router, tags=["Image Generation"])
app.include_router(anthropic.router, tags=["Claude Compatible"])
app.include_router(gemini.router, tags=["Gemini Compatible"])
app.include_router(embeddings.router, tags=["Embeddings"])
app.include_router(probes.router, tags=["Probes"])
app.include_router(admin.router, prefix="/api/admin", tags=["Dashboard Admin"])


@app.get("/api", tags=["System"])
async def root():
    """返回系统运行状态和文档入口。"""
    return {
        "status": "qwen2API Enterprise Gateway is running",
        "docs": "/docs",
        "version": "2.0.0",
    }


# SPA 前端路由兜底：对非 API、非静态资源的请求返回 index.html
FRONTEND_DIST = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend", "dist")
if os.path.exists(FRONTEND_DIST):
    _INDEX_HTML = os.path.join(FRONTEND_DIST, "index.html")
    _API_PREFIXES = ("/api/", "/v1/", "/anthropic/", "/v1beta/", "/docs", "/openapi.json", "/redoc")
    _STATIC_EXTENSIONS = (
        ".js", ".mjs", ".css", ".map", ".png", ".jpg", ".jpeg", ".gif",
        ".svg", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".webmanifest",
        ".webp", ".avif", ".json", ".xml", ".txt", ".webm", ".mp4",
    )
    _assets_dir = os.path.join(FRONTEND_DIST, "assets")
    if os.path.isdir(_assets_dir):
        app.mount("/assets", StaticFiles(directory=_assets_dir), name="static_assets")

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def _spa_fallback(request: Request, path: str):
        """为 SPA 返回静态资源或 index.html。"""
        if any(request.url.path.startswith(prefix) for prefix in _API_PREFIXES):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if any(path.endswith(ext) for ext in _STATIC_EXTENSIONS):
            file_path = os.path.join(FRONTEND_DIST, path)
            if os.path.isfile(file_path):
                return FileResponse(file_path)
        return FileResponse(_INDEX_HTML)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host="0.0.0.0", port=settings.PORT, workers=1)
