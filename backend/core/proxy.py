"""全局代理管理模块：统一维护代理配置、环境变量和连接检测。"""

import os
import time
from typing import Optional
from urllib.parse import unquote, urlparse

from backend.core.config import settings


def _normalize_curl_cffi_proxy(proxy: str) -> str:
    """将 SOCKS5 代理转换为 curl_cffi 更稳定的 SOCKS 写法。"""
    lower = proxy.lower()
    if lower.startswith("socks5://"):
        return f"socks://{proxy[9:]}"
    if lower.startswith("socks5h://"):
        return f"socks://{proxy[10:]}"
    return proxy


def _proxy_mount(proxy: str) -> dict[str, str]:
    """为 curl_cffi 生成 http/https 代理映射，并兼容 SOCKS5 配置。"""
    normalized = _normalize_curl_cffi_proxy(proxy)
    return {"http": normalized, "https": normalized}


class ProxyManager:
    """全局代理管理器，负责代理配置、环境同步和连通性检测。"""

    def __init__(self, proxy_url: str = "", enabled: bool = False):
        """初始化代理配置，并立即同步环境变量。"""
        self._proxy_url = proxy_url.strip()
        self._enabled = enabled
        self._sync_env()

    @property
    def proxy_url(self) -> str:
        """返回当前代理地址。"""
        return self._proxy_url

    @proxy_url.setter
    def proxy_url(self, value: str) -> None:
        """更新代理地址并同步环境变量。"""
        self._proxy_url = value.strip()
        self._sync_env()

    @property
    def enabled(self) -> bool:
        """返回代理当前是否启用。"""
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        """更新代理启用状态并同步环境变量。"""
        self._enabled = value
        self._sync_env()

    def get_proxy(self) -> Optional[str]:
        """返回当前生效的代理地址；未启用时返回 None。"""
        if self._enabled and self._proxy_url:
            return self._proxy_url
        return None

    def get_httpx_proxy(self) -> Optional[str]:
        """返回 httpx 可直接使用的代理地址。"""
        return self.get_proxy()

    def get_curl_cffi_proxy(self) -> Optional[str]:
        """返回 curl_cffi 单值代理地址，并兼容 SOCKS5 配置。"""
        proxy = self.get_proxy()
        return _normalize_curl_cffi_proxy(proxy) if proxy else None

    def get_curl_cffi_proxies(self) -> Optional[dict[str, str]]:
        """返回 curl_cffi 可直接使用的代理映射。"""
        proxy = self.get_proxy()
        return _proxy_mount(proxy) if proxy else None

    def get_browser_proxy(self) -> Optional[dict[str, str]]:
        """将代理地址转换为 Camoufox 可接受的浏览器代理配置。"""
        proxy = self.get_proxy()
        if not proxy:
            return None
        parsed = urlparse(proxy)
        if not parsed.scheme or not parsed.hostname:
            return None
        server = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port:
            server = f"{server}:{parsed.port}"
        data = {"server": server}
        if parsed.username:
            data["username"] = unquote(parsed.username)
        if parsed.password:
            data["password"] = unquote(parsed.password)
        return data

    def to_dict(self) -> dict[str, object]:
        """序列化当前代理配置，供接口返回和持久化。"""
        return {"proxy_url": self._proxy_url, "enabled": self._enabled}

    def update_from_dict(self, data: dict) -> None:
        """从字典更新代理配置，仅覆盖传入字段。"""
        if "proxy_url" in data:
            self._proxy_url = str(data["proxy_url"] or "").strip()
        if "enabled" in data:
            self._enabled = bool(data["enabled"])
        self._sync_env()

    def _sync_env(self) -> None:
        """同步 HTTP(S)/ALL_PROXY 环境变量，覆盖默认出网客户端。"""
        proxy = self.get_proxy()
        keys = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]
        for key in keys:
            if proxy:
                os.environ[key] = proxy
            elif key in os.environ:
                del os.environ[key]

    async def check_connection(self) -> dict[str, object]:
        """检测代理连通性，返回是否成功、耗时和出口 IP。"""
        proxy = self.get_proxy()
        if not proxy:
            return {"success": False, "error": "代理未启用或未配置", "time_ms": 0}
        import httpx

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(proxy=proxy, timeout=10) as client:
                resp = await client.get("https://httpbin.org/ip")
            elapsed = int((time.monotonic() - start) * 1000)
            if resp.status_code != 200:
                return {"success": False, "error": f"HTTP {resp.status_code}", "time_ms": elapsed}
            return {"success": True, "ip": resp.json().get("origin", "unknown"), "time_ms": elapsed}
        except Exception as exc:
            elapsed = int((time.monotonic() - start) * 1000)
            return {"success": False, "error": str(exc), "time_ms": elapsed}


proxy_manager = ProxyManager(proxy_url=settings.PROXY_URL, enabled=settings.PROXY_ENABLED)
