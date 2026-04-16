"""运行时配置模块：负责归一化、应用和导出可热更新的系统配置。"""

from typing import Any

from backend.core.config import MODEL_MAP, settings
from backend.core.proxy import proxy_manager


def current_runtime_config() -> dict[str, Any]:
    """导出当前可热更新配置，供接口返回和持久化使用。"""
    return {
        "max_inflight_per_account": settings.MAX_INFLIGHT_PER_ACCOUNT,
        "engine_mode": settings.ENGINE_MODE,
        "model_aliases": dict(MODEL_MAP),
        "proxy": proxy_manager.to_dict(),
    }


def normalize_runtime_config(data: dict[str, Any] | None) -> dict[str, Any]:
    """合并外部配置与当前运行配置，补齐缺失字段并修正类型。"""
    base = current_runtime_config()
    if not isinstance(data, dict):
        return base
    if "max_inflight_per_account" in data:
        base["max_inflight_per_account"] = max(1, int(data["max_inflight_per_account"]))
    if data.get("engine_mode") in {"httpx", "browser", "hybrid"}:
        base["engine_mode"] = data["engine_mode"]
    aliases = data.get("model_aliases")
    if isinstance(aliases, dict):
        base["model_aliases"] = {str(k): str(v) for k, v in aliases.items()}
    proxy = data.get("proxy")
    if isinstance(proxy, dict):
        base["proxy"] = {
            "proxy_url": str(proxy.get("proxy_url", "") or "").strip(),
            "enabled": bool(proxy.get("enabled", False)),
        }
    return base


def apply_runtime_config(data: dict[str, Any], pool=None) -> dict[str, Any]:
    """将运行时配置同步到 settings、账号池和代理管理器。"""
    config = normalize_runtime_config(data)
    settings.MAX_INFLIGHT_PER_ACCOUNT = config["max_inflight_per_account"]
    settings.ENGINE_MODE = config["engine_mode"]
    MODEL_MAP.clear()
    MODEL_MAP.update(config["model_aliases"])
    proxy_manager.update_from_dict(config["proxy"])
    if pool is not None:
        pool.set_max_inflight(config["max_inflight_per_account"])
    return config
