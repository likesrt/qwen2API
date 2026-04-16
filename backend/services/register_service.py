"""批量注册服务：负责创建注册任务、后台执行注册流程并持久化日志。"""

import asyncio
import gzip
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from backend.core.account_pool import Account
from backend.core.config import resolve_model, settings
from backend.core.database import AsyncJsonDB
from backend.services.auth_resolver import activate_account, register_qwen_account

log = logging.getLogger("qwen2api.register")


def _now_text() -> str:
    """返回当前本地时间的可读字符串，用于日志显示。"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _new_log(batch_id: str, sequence: int) -> dict[str, Any]:
    """构造单条批量注册日志的初始数据。"""
    return {
        "id": uuid.uuid4().hex,
        "batch_id": batch_id,
        "sequence": sequence,
        "created_at": _now_text(),
        "started_at": "",
        "finished_at": "",
        "status": "pending",
        "account": {"email": "", "username": ""},
        "error": "",
    }


def _finish_patch(status: str, error: str = "") -> dict[str, str]:
    """生成任务结束时写回日志的状态补丁。"""
    return {"status": status, "error": error, "finished_at": _now_text()}


def _error_text(exc: Exception) -> str:
    """提取适合展示到注册日志中的异常文本。"""
    text = str(exc).strip()
    return text or exc.__class__.__name__


def _account_info(account: Account) -> dict[str, str]:
    """提取需要展示在日志中的账号信息。"""
    return {"email": account.email, "username": account.username}


def _readiness_state(error: str) -> str:
    """将 readiness check 的错误文本映射为内部状态码。"""
    lower = error.lower()
    if not error:
        return "success"
    if any(key in lower for key in ("pending activation", "please check your email", "not activated")):
        return "pending_activation"
    if any(key in lower for key in ("unauthorized", "forbidden", "401", "403", "token", "login")):
        return "auth_error"
    return "success"


def _archive_dir() -> Path:
    """返回注册日志归档目录，并在缺失时自动创建。"""
    path = Path(settings.REGISTER_LOG_ARCHIVE_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _archive_paths() -> list[Path]:
    """返回按文件名排序的归档切片列表。"""
    return sorted(_archive_dir().glob("register_logs-*.json.gz"))


def _archive_name() -> str:
    """生成新的归档切片文件名，保证时间顺序与唯一性。"""
    stamp = time.strftime("%Y%m%d%H%M%S", time.localtime())
    return f"register_logs-{stamp}-{uuid.uuid4().hex[:8]}.json.gz"


def _json_size(data: Any) -> int:
    """估算 JSON 数据写入磁盘后的字节数，用于触发切割。"""
    return len(json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"))


def _clamp_page(page: int, page_size: int) -> tuple[int, int]:
    """约束分页参数，避免接口返回超大页。"""
    safe_page = max(1, page)
    safe_page_size = max(1, min(page_size, 100))
    return safe_page, safe_page_size


def _archivable(item: dict[str, Any]) -> bool:
    """判断日志是否已经结束，可安全写入压缩归档。"""
    return str(item.get("status", "") or "") not in {"pending", "running"}


def _trim_archives() -> None:
    """按保留上限清理最旧的归档切片，避免目录无限增长。"""
    keep = max(1, settings.REGISTER_LOG_ARCHIVE_KEEP)
    paths = _archive_paths()
    for path in paths[:-keep]:
        path.unlink(missing_ok=True)


def _read_archive(path: Path) -> list[dict[str, Any]]:
    """读取单个 gzip 归档切片；异常时返回空列表。"""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        log.warning("[Register] 读取归档失败: %s (%s)", path, exc)
        return []
    return data if isinstance(data, list) else []


def _write_archive(items: list[dict[str, Any]]) -> None:
    """将切割出的旧日志写入 gzip 归档切片。"""
    path = _archive_dir() / _archive_name()
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(items, handle, indent=2, ensure_ascii=False)
    _trim_archives()


def _match_log(item: dict[str, Any], batch_id: str, account: str, status: str) -> bool:
    """判断日志是否命中批次、账号和状态筛选条件。"""
    batch_value = str(item.get("batch_id", "") or "").lower()
    account_value = str(item.get("account", {}).get("email", "") or "").lower()
    status_value = str(item.get("status", "") or "").lower()
    if batch_id and batch_id.lower() not in batch_value:
        return False
    if account and account.lower() not in account_value:
        return False
    if status and status.lower() != status_value:
        return False
    return True


def _paginate_logs(items: list[dict[str, Any]], page: int, page_size: int) -> dict[str, Any]:
    """按页切片日志列表，并返回总量与页码信息。"""
    safe_page, safe_page_size = _clamp_page(page, page_size)
    total = len(items)
    total_pages = max(1, (total + safe_page_size - 1) // safe_page_size)
    current_page = min(safe_page, total_pages)
    start = (current_page - 1) * safe_page_size
    end = start + safe_page_size
    return {
        "logs": items[start:end],
        "total": total,
        "page": current_page,
        "page_size": safe_page_size,
        "total_pages": total_pages,
    }


async def _load_logs(db: AsyncJsonDB) -> list[dict[str, Any]]:
    """读取活动注册日志列表，并对异常数据进行兜底。"""
    data = await db.get()
    return data if isinstance(data, list) else []


async def _save_logs(db: AsyncJsonDB, logs: list[dict[str, Any]]) -> None:
    """保存活动日志，并在超过阈值时自动压缩切割旧数据。"""
    active_logs = list(logs)
    slice_size = max(1, settings.REGISTER_LOG_SLICE_SIZE)
    while _json_size(active_logs) > settings.REGISTER_LOG_MAX_BYTES and len(active_logs) > slice_size:
        archived = active_logs[:slice_size]
        # 只归档已结束的旧日志，避免运行中的批次被提前切出活动文件后无法继续 patch。
        if any(not _archivable(item) for item in archived):
            break
        _write_archive(archived)
        active_logs = active_logs[slice_size:]
    await db.save(active_logs)


async def _all_logs(db: AsyncJsonDB) -> list[dict[str, Any]]:
    """汇总活动日志与压缩归档，供分页和筛选查询复用。"""
    logs = await _load_logs(db)
    for path in _archive_paths():
        logs.extend(_read_archive(path))
    return logs


async def append_logs(db: AsyncJsonDB, items: list[dict[str, Any]]) -> None:
    """向注册日志存储中追加多条日志，并在必要时触发切割。"""
    logs = await _load_logs(db)
    logs.extend(items)
    await _save_logs(db, logs)


async def patch_log(db: AsyncJsonDB, log_id: str, patch: dict[str, Any]) -> None:
    """按日志 ID 更新指定字段，保持其余内容不变。"""
    logs = await _load_logs(db)
    for item in logs:
        if item.get("id") == log_id:
            item.update(patch)
            break
    await _save_logs(db, logs)


async def get_log(db: AsyncJsonDB, log_id: str) -> dict[str, Any] | None:
    """按日志 ID 查询单条注册日志；不存在时返回 None。"""
    for item in await _all_logs(db):
        if item.get("id") == log_id:
            return item
    return None


async def list_logs(
    db: AsyncJsonDB,
    batch_id: str = "",
    account: str = "",
    status: str = "",
    page: int = 1,
    page_size: int = settings.REGISTER_LOG_PAGE_SIZE,
) -> dict[str, Any]:
    """返回注册日志分页结果，并支持批次、账号和状态筛选。"""
    logs = await _all_logs(db)
    logs = [item for item in logs if _match_log(item, batch_id, account, status)]
    logs.sort(key=lambda item: (item.get("created_at", ""), item.get("sequence", 0)), reverse=True)
    return _paginate_logs(logs, page, page_size)


async def _check_account_ready(client, account: Account) -> str:
    """执行新账号就绪检查，并返回内部状态码。"""
    chat_id: Optional[str] = None
    error = ""
    try:
        chat_id = await client.create_chat(account.token, resolve_model("qwen"))
    except Exception as exc:
        error = str(exc)
    finally:
        if chat_id:
            try:
                await client.delete_chat(account.token, chat_id)
            except Exception:
                pass
    return _readiness_state(error)


def _mark_valid(account: Account) -> None:
    """将账号标记为可用状态，供入池和日志同步复用。"""
    account.valid = True
    account.activation_pending = False
    account.status_code = "valid"
    account.last_error = ""


def _mark_pending(account: Account) -> None:
    """将账号标记为待激活状态，供入池和日志同步复用。"""
    account.valid = False
    account.activation_pending = True
    account.status_code = "pending_activation"
    account.last_error = "账号已注册，但仍需激活"


async def _save_success(pool, db: AsyncJsonDB, log_id: str, account: Account) -> None:
    """持久化成功注册的账号，并将对应日志更新为成功。"""
    _mark_valid(account)
    await pool.add(account)
    await patch_log(db, log_id, {"account": _account_info(account), **_finish_patch("success")})


async def _save_pending(pool, db: AsyncJsonDB, log_id: str, account: Account) -> None:
    """持久化待激活账号，并将对应日志更新为待激活。"""
    _mark_pending(account)
    await pool.add(account)
    patch = {"account": _account_info(account), **_finish_patch("pending_activation", account.last_error)}
    await patch_log(db, log_id, patch)


async def _finalize_account(app, log_id: str, account: Account) -> None:
    """根据就绪检查结果决定账号最终状态，并同步入池与日志。"""
    pool = app.state.account_pool
    db = app.state.register_logs_db
    state = await _check_account_ready(app.state.qwen_client, account)
    if state == "success":
        await _save_success(pool, db, log_id, account)
        return
    if state == "pending_activation":
        # 先尝试自动激活，失败后再以待激活状态入池，避免丢失已注册账号。
        if await activate_account(account):
            await _save_success(pool, db, log_id, account)
            return
        await _save_pending(pool, db, log_id, account)
        return
    patch = {"account": _account_info(account), **_finish_patch("failed", "上游鉴权或注册校验失败")}
    await patch_log(db, log_id, patch)


async def _run_single(app, log_id: str) -> None:
    """执行单个注册任务，并在异常时确保日志不会停留在运行中。"""
    db = app.state.register_logs_db
    await patch_log(db, log_id, {"status": "running", "started_at": _now_text()})
    try:
        account = await register_qwen_account()
        if not account:
            await patch_log(db, log_id, _finish_patch("failed", "自动注册流程未返回有效账号"))
            return
        await patch_log(db, log_id, {"account": _account_info(account)})
        await _finalize_account(app, log_id, account)
    except Exception as exc:
        # 后台任务一旦冒泡，前端就只能看到永久“注册中”，这里必须落失败态。
        await patch_log(db, log_id, _finish_patch("failed", _error_text(exc)))
        log.exception("[Register] 单个注册任务失败: log_id=%s", log_id)


async def _run_batch(app, batch_id: str, log_ids: list[str]) -> None:
    """顺序执行同一批次的注册任务，并在批次级异常时补全失败状态。"""
    try:
        for log_id in log_ids:
            await _run_single(app, log_id)
    except Exception as exc:
        db = app.state.register_logs_db
        error = _error_text(exc)
        for log_id in log_ids:
            item = await get_log(db, log_id)
            if item and item.get("status") in {"pending", "running"}:
                # 批次异常时补写未结束任务，避免日志和实际后台状态不一致。
                await patch_log(db, log_id, _finish_patch("failed", error))
        log.exception("[Register] 批量注册任务失败: batch_id=%s", batch_id)
    finally:
        app.state.register_tasks.pop(batch_id, None)


async def register_once(app) -> dict[str, Any]:
    """同步执行一次注册流程，并返回兼容旧接口的结果结构。"""
    batch_id = f"single-{uuid.uuid4().hex}"
    log_item = _new_log(batch_id, 1)
    await append_logs(app.state.register_logs_db, [log_item])
    await _run_single(app, log_item["id"])
    result = await get_log(app.state.register_logs_db, log_item["id"])
    if not result:
        return {"ok": False, "error": "注册日志写入失败"}
    status = result.get("status")
    email = result.get("account", {}).get("email", "")
    if status == "success":
        return {"ok": True, "email": email, "message": "注册成功"}
    if status == "pending_activation":
        return {
            "ok": True,
            "email": email,
            "activation_pending": True,
            "message": "账号已注册，但仍需激活",
            "error": result.get("error", ""),
        }
    return {"ok": False, "email": email, "error": result.get("error", "注册失败")}


async def create_batch(app, quantity: int) -> dict[str, Any]:
    """创建批量注册任务并启动后台执行，返回批次元信息。"""
    if quantity < 1 or quantity > settings.BATCH_REGISTER_MAX:
        raise ValueError(f"批量注册数量必须在 1 到 {settings.BATCH_REGISTER_MAX} 之间")
    if len(app.state.account_pool.accounts) + quantity > 100:
        raise ValueError("账号池容量不足，无法创建该批次任务")
    batch_id = uuid.uuid4().hex
    logs = [_new_log(batch_id, index + 1) for index in range(quantity)]
    await append_logs(app.state.register_logs_db, logs)
    log_ids = [item["id"] for item in logs]
    app.state.register_tasks[batch_id] = asyncio.create_task(_run_batch(app, batch_id, log_ids))
    return {"batch_id": batch_id, "quantity": quantity, "log_ids": log_ids}
