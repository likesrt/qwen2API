"""OpenAI 兼容模型列表接口：提供 GET /v1/models。"""

import time
from collections.abc import Iterator, MutableMapping
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from backend.core.config import API_KEYS, settings

router = APIRouter()


class _SlidingWindowCounter(MutableMapping):
    """滑动窗口限流容器，按请求方标识统计时间戳。"""

    def __init__(self, window: int = 60):
        """初始化底层存储和窗口大小。"""
        self._store: dict[str, list[float]] = {}
        self._window = window

    def _evict(self, key: str) -> None:
        """移除指定 key 下过期的请求记录。"""
        cutoff = time.time() - self._window
        self._store[key] = [value for value in self._store.get(key, []) if value > cutoff]

    def __getitem__(self, key: str) -> list[float]:
        """返回指定 key 的有效请求时间戳列表。"""
        self._evict(key)
        return self._store.setdefault(key, [])

    def __setitem__(self, key: str, value: list[float]) -> None:
        """写入指定 key 的请求时间戳列表。"""
        self._store[key] = value

    def __delitem__(self, key: str) -> None:
        """删除指定 key 的限流记录。"""
        del self._store[key]

    def __len__(self) -> int:
        """返回当前已记录的请求方数量。"""
        return len(self._store)

    def __iter__(self) -> Iterator[str]:
        """返回底层存储的迭代器。"""
        return iter(self._store)


_rate_limiter = _SlidingWindowCounter(window=settings.MODELS_RATE_LIMIT_WINDOW)


def _check_rate_limit(key: str) -> bool:
    """检查指定 key 是否在滑动窗口内超限。"""
    timestamps = _rate_limiter[key]
    if len(timestamps) >= settings.MODELS_RATE_LIMIT_COUNT:
        return False
    timestamps.append(time.time())
    _rate_limiter[key] = timestamps
    return True


def _verify_api_key(request: Request) -> str:
    """校验请求中的 API Key，失败时抛出 401 或 403。"""
    auth = request.headers.get("Authorization", "")
    key = auth.split("Bearer ", 1)[1].strip() if auth.startswith("Bearer ") else request.query_params.get("key", "")
    if not key:
        raise HTTPException(status_code=401, detail="Unauthorized: API Key required")
    if key != settings.ADMIN_KEY and key not in API_KEYS:
        raise HTTPException(status_code=403, detail="Forbidden: Invalid API Key")
    return key


def _model_tokens(request: Request) -> list[str]:
    """收集可用于查询上游模型列表的账号 Token，优先选择有效账号。"""
    pool = request.app.state.account_pool
    preferred = [account.token for account in pool.accounts if account.token and account.valid and not account.activation_pending]
    if preferred:
        return preferred
    return [account.token for account in pool.accounts if account.token and not account.activation_pending]


async def _upstream_models(request: Request) -> list[dict[str, Any]]:
    """按账号顺序查询上游模型列表，直到拿到可用结果。"""
    tokens = _model_tokens(request)
    if not tokens:
        raise HTTPException(status_code=503, detail="No upstream account available")
    client = request.app.state.qwen_client
    for token in tokens:
        models = await client.list_models(token)
        if models:
            return [item for item in models if isinstance(item, dict) and item.get("id")]
    raise HTTPException(status_code=502, detail="Failed to fetch upstream models")


def _model_description(model: dict[str, Any]) -> str:
    """提取上游模型描述，缺失时返回空字符串。"""
    info = model.get("info")
    meta = info.get("meta") if isinstance(info, dict) else None
    return str(meta.get("description", "") or "") if isinstance(meta, dict) else ""


def _model_owner(model: dict[str, Any]) -> str:
    """提取上游模型拥有方，缺失时回退到 qwen。"""
    return str(model.get("owned_by") or "qwen")


def _model_item(model: dict[str, Any]) -> dict[str, Any]:
    """将上游模型对象转换为 OpenAI 兼容格式。"""
    model_id = str(model.get("id", "")).strip()
    return {
        "id": model_id,
        "object": "model",
        "created": 1700000000,
        "owned_by": _model_owner(model),
        "permission": [],
        "root": model_id,
        "parent": None,
        "description": _model_description(model),
    }


@router.get("/v1/models")
async def list_models(request: Request) -> dict[str, Any]:
    """返回上游实际支持的模型列表，不暴露本地模型映射别名。"""
    key = _verify_api_key(request)
    client_id = f"{request.client.host if request.client else 'unknown'}:{key}"
    if not _check_rate_limit(client_id):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    data = []
    seen: set[str] = set()
    for model in await _upstream_models(request):
        model_id = str(model.get("id", "")).strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        data.append(_model_item(model))
    return {"object": "list", "data": data}
