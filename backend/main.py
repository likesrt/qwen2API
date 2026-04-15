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

from backend.core.config import settings
from backend.core.database import AsyncJsonDB
from backend.core.browser_engine import BrowserEngine
from backend.core.httpx_engine import HttpxEngine
from backend.core.hybrid_engine import HybridEngine
from backend.core.account_pool import AccountPool
from backend.services.qwen_client import QwenClient
from backend.api import admin, v1_chat, probes, anthropic, gemini, embeddings, images
from backend.services.garbage_collector import garbage_collect_chats

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("qwen2api")

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting qwen2API v2.0 Enterprise Gateway...")

    app.state.accounts_db = AsyncJsonDB(settings.ACCOUNTS_FILE, default_data=[])
    app.state.users_db = AsyncJsonDB(settings.USERS_FILE, default_data=[])
    app.state.captures_db = AsyncJsonDB(settings.CAPTURES_FILE, default_data=[])

    browser_engine = BrowserEngine(pool_size=settings.BROWSER_POOL_SIZE)
    httpx_engine = HttpxEngine(base_url="https://chat.qwen.ai")

    if settings.ENGINE_MODE == "httpx":
        engine = httpx_engine
        log.info("引擎模式: httpx 直连")
    elif settings.ENGINE_MODE == "hybrid":
        engine = HybridEngine(browser_engine, httpx_engine)
        log.info("引擎模式: Hybrid (api_call=httpx优先, fetch_chat=browser)")
    else:
        engine = browser_engine
        log.info("引擎模式: Camoufox 浏览器")

    app.state.browser_engine = browser_engine
    app.state.httpx_engine = httpx_engine
    app.state.gateway_engine = engine
    app.state.account_pool = AccountPool(app.state.accounts_db, max_inflight=settings.MAX_INFLIGHT_PER_ACCOUNT)
    app.state.qwen_client = QwenClient(engine, app.state.account_pool)

    await app.state.account_pool.load()
    await engine.start()

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
app.include_router(images.router, tags=["Image Generation"])
app.include_router(anthropic.router, tags=["Claude Compatible"])
app.include_router(gemini.router, tags=["Gemini Compatible"])
app.include_router(embeddings.router, tags=["Embeddings"])
app.include_router(probes.router, tags=["Probes"])
app.include_router(admin.router, prefix="/api/admin", tags=["Dashboard Admin"])

@app.get("/api", tags=["System"])
async def root():
    return {
        "status": "qwen2API Enterprise Gateway is running",
        "docs": "/docs",
        "version": "2.0.0"
    }

# SPA 前端路由兜底：对非 API、非静态资源的请求返回 index.html
# 解决 BrowserRouter 刷新子路径时后端返回 404 的问题
FRONTEND_DIST = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend", "dist")
if os.path.exists(FRONTEND_DIST):
    _INDEX_HTML = os.path.join(FRONTEND_DIST, "index.html")

    # 需要由 FastAPI/Starlette 路由处理的 API 前缀
    _API_PREFIXES = ("/api/", "/v1/", "/anthropic/", "/v1beta/", "/docs", "/openapi.json", "/redoc")

    # 静态资源的常见文件扩展名
    _STATIC_EXTENSIONS = (
        ".js", ".mjs", ".css", ".map", ".png", ".jpg", ".jpeg", ".gif",
        ".svg", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".webmanifest",
        ".webp", ".avif", ".json", ".xml", ".txt", ".webm", ".mp4",
    )

    # 挂载 /assets 为纯静态目录（Vite 构建产物全部在此目录下）
    _assets_dir = os.path.join(FRONTEND_DIST, "assets")
    if os.path.isdir(_assets_dir):
        app.mount("/assets", StaticFiles(directory=_assets_dir), name="static_assets")

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def _spa_fallback(request: Request, path: str):
        """
        SPA 前端兜底路由：静态资源返回文件，其余路径返回 index.html。

        - 带 API 前缀的路径不应走到这里（FastAPI 显式路由优先），
          若意外到达则返回 404
        - 静态资源文件（JS/CSS/字体/图片等）直接从 dist 目录返回
        - 其余所有路径返回 index.html，供前端 BrowserRouter 处理
        """
        # API 路径不应由 fallback 处理
        if any(request.url.path.startswith(p) for p in _API_PREFIXES):
            return JSONResponse({"detail": "Not Found"}, status_code=404)

        # 尝试在 dist 目录中查找对应的静态文件
        if any(path.endswith(ext) for ext in _STATIC_EXTENSIONS):
            file_path = os.path.join(FRONTEND_DIST, path)
            if os.path.isfile(file_path):
                return FileResponse(file_path)

        # 其他所有路径（SPA 路由）返回 index.html
        return FileResponse(_INDEX_HTML)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=settings.PORT, workers=1)