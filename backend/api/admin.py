"""管理后台接口：账号、注册、设置和密钥管理。"""

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from backend.core.account_pool import Account, AccountPool
from backend.core.config import API_KEYS, MODEL_MAP, VERSION, save_api_keys, settings
from backend.core.database import AsyncJsonDB
from backend.core.proxy import proxy_manager
from backend.core.runtime_config import apply_runtime_config, current_runtime_config, normalize_runtime_config
from backend.core.runtime_stack import refresh_gateway_stack
from backend.services.auth_resolver import activate_account as activate_logic
from backend.services.qwen_client import QwenClient
from backend.services.register_service import create_batch, list_logs as list_register_logs, register_once

router = APIRouter()


class UserCreate(BaseModel):
    """用户创建请求体。"""

    name: str
    quota: int = 1000000


class BatchRegisterRequest(BaseModel):
    """批量注册请求体。"""

    quantity: int = 1


async def _read_json(request: Request) -> dict[str, Any]:
    """读取 JSON 请求体，失败时抛出 400。"""
    try:
        return await request.json()
    except Exception as exc:
        raise HTTPException(400, detail="Invalid JSON body") from exc


def verify_admin(authorization: str = Header(None)):
    """校验管理接口 Bearer Token。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = authorization.split("Bearer ", 1)[1]
    if token != settings.ADMIN_KEY and token not in API_KEYS:
        raise HTTPException(status_code=403, detail="Forbidden: Admin Key Mismatch")
    return token


def _engine_info(engine) -> dict[str, Any]:
    """提取当前网关引擎的运行状态。"""
    if hasattr(engine, "status"):
        return engine.status()
    if hasattr(engine, "_pages") and hasattr(engine, "pool_size"):
        free_pages = engine._pages.qsize()
        queue = max(0, engine.pool_size - free_pages)
        return {"started": engine._started, "mode": "browser", "pool_size": engine.pool_size, "free_pages": free_pages, "queue": queue}
    return {"started": getattr(engine, "_started", False), "mode": "httpx", "pool_size": 0, "free_pages": 0, "queue": 0}


def _browser_info(browser_engine) -> dict[str, Any]:
    """提取浏览器引擎状态。"""
    if not browser_engine:
        return {"started": False, "pool_size": 0, "free_pages": 0, "queue": 0}
    free_pages = browser_engine._pages.qsize() if getattr(browser_engine, "_pages", None) is not None else 0
    pool_size = getattr(browser_engine, "pool_size", 0)
    return {"started": getattr(browser_engine, "_started", False), "pool_size": pool_size, "free_pages": free_pages, "queue": max(0, pool_size - free_pages)}


def _httpx_info(httpx_engine) -> dict[str, Any]:
    """提取 httpx 引擎状态。"""
    if not httpx_engine:
        return {"started": False, "mode": "httpx"}
    return {"started": getattr(httpx_engine, "_started", False), "mode": "httpx"}


def _account_view(account: Account) -> dict[str, Any]:
    """将账号对象转换为前端可直接使用的结构。"""
    item = account.to_dict()
    item["valid"] = account.valid
    item["inflight"] = account.inflight
    item["rate_limited_until"] = account.rate_limited_until
    item["status_code"] = account.get_status_code()
    item["status_text"] = account.get_status_text()
    item["last_error"] = account.last_error
    return item


def _pending_activation_error(error: str) -> bool:
    """判断错误是否表示账号仍待激活。"""
    lower = error.lower()
    keys = ("pending activation", "please check your email", "not activated")
    return any(key in lower for key in keys)


def _auth_error(error: str) -> bool:
    """判断错误是否表示上游鉴权失败。"""
    lower = error.lower()
    keys = ("unauthorized", "forbidden", "401", "403", "token", "login")
    return any(key in lower for key in keys)


async def _safe_delete_chat(client: QwenClient, token: str, chat_id: str | None) -> None:
    """安全删除临时聊天，忽略清理阶段异常。"""
    if not chat_id:
        return
    try:
        await client.delete_chat(token, chat_id)
    except Exception:
        pass


async def _readiness_error(client: QwenClient, account: Account) -> str:
    """执行新账号就绪检查并返回错误文本。"""
    chat_id = None
    try:
        chat_id = await client.create_chat(account.token, "qwen3.6-plus")
        return ""
    except Exception as exc:
        return str(exc)
    finally:
        await _safe_delete_chat(client, account.token, chat_id)


def _mark_valid(account: Account) -> None:
    """将账号标记为可用。"""
    account.valid = True
    account.activation_pending = False
    account.status_code = "valid"
    account.last_error = ""


def _mark_pending(account: Account) -> None:
    """将账号标记为待激活。"""
    account.valid = False
    account.activation_pending = True
    account.status_code = "pending_activation"
    account.last_error = "账号已注册，但仍需激活"


async def _verify_account(client: QwenClient, account: Account) -> dict[str, Any]:
    """验证单个账号，必要时尝试自动刷新 Token。"""
    is_valid = await client.verify_token(account.token)
    refreshed = False
    if not is_valid and account.password:
        refreshed = await client.auth_resolver.refresh_token(account)
        is_valid = refreshed or is_valid
    account.valid = is_valid
    if is_valid:
        _mark_valid(account)
    elif account.activation_pending:
        account.status_code = "pending_activation"
        account.last_error = account.last_error or "账号仍待激活"
    elif account.get_status_code() != "rate_limited":
        account.status_code = account.status_code or "auth_error"
        account.last_error = account.last_error or "账号认证失败"
    return {"email": account.email, "valid": is_valid, "refreshed": refreshed, "status_code": account.get_status_code(), "status_text": account.get_status_text(), "error": account.last_error}


async def _verify_all(pool: AccountPool, client: QwenClient) -> dict[str, Any]:
    """并发验证账号池中的全部账号。"""
    concurrency = max(1, min(len(pool.accounts) or 1, max(2, settings.BROWSER_POOL_SIZE)))
    sem = asyncio.Semaphore(concurrency)

    async def verify_one(account: Account) -> dict[str, Any]:
        async with sem:
            return await _verify_account(client, account)

    results = await asyncio.gather(*(verify_one(account) for account in pool.accounts))
    await pool.save()
    return {"ok": True, "results": results, "concurrency": concurrency}


def _merge_runtime_config(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """合并当前运行时配置与外部更新数据。"""
    merged = dict(current)
    for key in ("max_inflight_per_account", "engine_mode", "model_aliases"):
        if key in patch:
            merged[key] = patch[key]
    if isinstance(patch.get("proxy"), dict):
        merged["proxy"] = {**current.get("proxy", {}), **patch["proxy"]}
    return normalize_runtime_config(merged)


async def _save_runtime_config(request: Request, data: dict[str, Any]) -> dict[str, Any]:
    """保存并应用运行时配置，必要时刷新网关栈。"""
    db: AsyncJsonDB = request.app.state.config_db
    current = normalize_runtime_config(await db.get())
    saved = _merge_runtime_config(current, data)
    await db.save(saved)
    apply_runtime_config(saved, pool=request.app.state.account_pool)
    if "engine_mode" in data or "proxy" in data:
        await refresh_gateway_stack(request.app)
    return saved


def _running_batches(tasks: dict[str, asyncio.Task]) -> list[str]:
    """返回当前仍在执行的批量注册批次 ID。"""
    return [batch_id for batch_id, task in tasks.items() if not task.done()]


@router.get("/status", dependencies=[Depends(verify_admin)])
async def get_system_status(request: Request):
    """返回账号池和多引擎的运行状态。"""
    engine = getattr(request.app.state, "gateway_engine", request.app.state.browser_engine)
    browser_engine = getattr(request.app.state, "browser_engine", None)
    httpx_engine = getattr(request.app.state, "httpx_engine", None)
    return {
        "accounts": request.app.state.account_pool.status(),
        "engine_mode": settings.ENGINE_MODE,
        "browser_engine": _browser_info(browser_engine),
        "httpx_engine": _httpx_info(httpx_engine),
        "hybrid_engine": _engine_info(engine) if settings.ENGINE_MODE == "hybrid" else None,
        "proxy": proxy_manager.to_dict(),
    }


@router.get("/users", dependencies=[Depends(verify_admin)])
async def list_users(request: Request):
    """列出所有 API 用户。"""
    db: AsyncJsonDB = request.app.state.users_db
    return {"users": await db.get()}


@router.post("/users", dependencies=[Depends(verify_admin)])
async def create_user(user: UserCreate, request: Request):
    """创建新的 API 用户记录。"""
    db: AsyncJsonDB = request.app.state.users_db
    data = await db.get()
    new_user = {"id": f"sk-{time.time_ns()}", "name": user.name, "quota": user.quota, "used_tokens": 0}
    data.append(new_user)
    await db.save(data)
    return new_user


@router.post("/accounts", dependencies=[Depends(verify_admin)])
async def add_account(request: Request):
    """手动注入一个账号到账号池。"""
    pool: AccountPool = request.app.state.account_pool
    client: QwenClient = request.app.state.qwen_client
    data = await _read_json(request)
    token = data.get("token", "")
    if not token:
        raise HTTPException(400, detail="token is required")
    account = Account(email=data.get("email", f"manual_{int(time.time())}@qwen"), password=data.get("password", ""), token=token, cookies=data.get("cookies", ""), username=data.get("username", ""))
    if not await client.verify_token(token):
        account.valid = False
        account.status_code = "auth_error"
        account.last_error = "Token 无效或已过期"
        return {"ok": False, "error": account.last_error, "message": account.last_error}
    _mark_valid(account)
    await pool.add(account)
    return {"ok": True, "email": account.email, "message": "账号已加入账号池"}


@router.get("/accounts", dependencies=[Depends(verify_admin)])
async def list_accounts(request: Request):
    """列出当前账号池中的所有账号。"""
    pool: AccountPool = request.app.state.account_pool
    return {"accounts": [_account_view(account) for account in pool.accounts]}


@router.post("/accounts/register-verify", dependencies=[Depends(verify_admin)])
async def verify_register_secret(request: Request):
    """验证注册解锁密码，正确后前端展示注册入口。"""
    body = await _read_json(request)
    expected = settings.REGISTER_SECRET
    if not expected:
        return {"ok": False, "error": "register secret not configured"}
    return {"ok": body.get("secret", "") == expected}


@router.post("/accounts/register", dependencies=[Depends(verify_admin)])
async def register_new_account(request: Request):
    """同步注册一个新账号，并返回兼容旧接口的结构。"""
    return await register_once(request.app)


@router.post("/accounts/register/batch", dependencies=[Depends(verify_admin)])
async def create_register_batch(data: BatchRegisterRequest, request: Request):
    """创建后台批量注册任务并返回批次信息。"""
    try:
        result = await create_batch(request.app, data.quantity)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return {"ok": True, **result}


@router.get("/accounts/register/logs", dependencies=[Depends(verify_admin)])
async def get_register_logs(
    request: Request,
    batch_id: str = "",
    account: str = "",
    status: str = "",
    page: int = 1,
    page_size: int = settings.REGISTER_LOG_PAGE_SIZE,
):
    """返回注册日志分页结果，并附带运行中批次与筛选条件。"""
    db: AsyncJsonDB = request.app.state.register_logs_db
    result = await list_register_logs(db, batch_id=batch_id, account=account, status=status, page=page, page_size=page_size)
    return {
        **result,
        "filters": {"batch_id": batch_id, "account": account, "status": status},
        "running_batches": _running_batches(request.app.state.register_tasks),
    }


@router.post("/verify", dependencies=[Depends(verify_admin)])
async def verify_all_accounts(request: Request):
    """并发验证账号池中的全部账号。"""
    pool: AccountPool = request.app.state.account_pool
    client: QwenClient = request.app.state.qwen_client
    return await _verify_all(pool, client)


@router.post("/accounts/{email}/activate", dependencies=[Depends(verify_admin)])
async def activate_account(email: str, request: Request):
    """主动触发指定账号的激活流程。"""
    pool: AccountPool = request.app.state.account_pool
    account = next((item for item in pool.accounts if item.email == email), None)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    started_at = float(getattr(account, "_activation_started_at", 0) or 0)
    if getattr(account, "_is_activating", False) and started_at and (time.time() - started_at) < 90:
        return {"ok": True, "pending": True, "message": "账号正在激活中，请稍后刷新"}
    success = await activate_logic(account)
    if success:
        _mark_valid(account)
        await pool.add(account)
        return {"ok": True, "message": "账号激活成功"}
    account.status_code = "pending_activation" if account.activation_pending else (account.status_code or "auth_error")
    account.last_error = account.last_error or "激活失败，请稍后重试"
    await pool.save()
    return {"ok": False, "error": account.last_error, "message": account.last_error}


@router.post("/accounts/{email}/verify", dependencies=[Depends(verify_admin)])
async def verify_account(email: str, request: Request):
    """单独验证指定账号的状态。"""
    pool: AccountPool = request.app.state.account_pool
    client: QwenClient = request.app.state.qwen_client
    account = next((item for item in pool.accounts if item.email == email), None)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    result = await _verify_account(client, account)
    await pool.save()
    return result


@router.delete("/accounts/{email}", dependencies=[Depends(verify_admin)])
async def delete_account(email: str, request: Request):
    """从账号池中删除指定邮箱的账号。"""
    pool: AccountPool = request.app.state.account_pool
    await pool.remove(email)
    return {"ok": True}


@router.get("/settings", dependencies=[Depends(verify_admin)])
async def get_settings():
    """返回当前生效的运行时配置。"""
    return {"version": VERSION, **current_runtime_config()}


@router.put("/settings", dependencies=[Depends(verify_admin)])
async def update_settings(data: dict[str, Any], request: Request):
    """更新运行时配置并立即应用到账号池和引擎。"""
    saved = await _save_runtime_config(request, data)
    return {"ok": True, **saved}


@router.get("/proxy/status", dependencies=[Depends(verify_admin)])
async def get_proxy_status():
    """返回当前代理配置和连接检测结果。"""
    status = await proxy_manager.check_connection()
    return {**proxy_manager.to_dict(), **status}


@router.post("/proxy/test", dependencies=[Depends(verify_admin)])
async def test_proxy(request: Request):
    """用临时代理配置执行一次连通性测试。"""
    body = await _read_json(request)
    old = proxy_manager.to_dict()
    proxy_manager.update_from_dict({"proxy_url": body.get("proxy_url", old["proxy_url"]), "enabled": body.get("enabled", True)})
    try:
        return await proxy_manager.check_connection()
    finally:
        proxy_manager.update_from_dict(old)


@router.get("/keys", dependencies=[Depends(verify_admin)])
async def get_keys():
    """列出当前所有管理密钥。"""
    return {"keys": list(API_KEYS)}


@router.post("/keys", dependencies=[Depends(verify_admin)])
async def generate_key():
    """生成一个新的管理密钥并持久化。"""
    new_key = f"sk-qwen-{time.time_ns()}"[:29]
    API_KEYS.add(new_key)
    save_api_keys(API_KEYS)
    return {"ok": True, "key": new_key}


@router.delete("/keys/{key}", dependencies=[Depends(verify_admin)])
async def delete_key(key: str):
    """删除指定管理密钥。"""
    if key in API_KEYS:
        API_KEYS.remove(key)
        save_api_keys(API_KEYS)
    return {"ok": True}
