"""
hybrid_engine.py — mix browser stability with httpx speed.
Phase 1 policy:
- api_call: httpx first, browser fallback on failures
- fetch_chat: browser first, httpx fallback on failures
"""

import logging

from backend.core.proxy import proxy_manager

log = logging.getLogger("qwen2api.hybrid_engine")


def _should_fallback(status: int, body_text: str) -> bool:
    """判断当前响应是否需要切换到另一条引擎路径。"""
    return (
        status == 0
        or status in (401, 403, 429)
        or "waf" in body_text
        or "<!doctype" in body_text
        or "forbidden" in body_text
        or "unauthorized" in body_text
    )


def _body_preview(result: dict) -> str:
    """提取短响应片段，便于记录回退日志。"""
    return (result.get("body") or "")[:160].replace("\n", "\\n")


class HybridEngine:
    """组合浏览器与直连引擎，根据场景在两者之间切换。"""

    def __init__(self, browser_engine, httpx_engine):
        """保存底层引擎实例，并暴露统一状态字段。"""
        self.browser_engine = browser_engine
        self.httpx_engine = httpx_engine
        self._started = False
        self.base_url = getattr(browser_engine, "base_url", getattr(httpx_engine, "base_url", "https://chat.qwen.ai"))
        self.pool_size = getattr(browser_engine, "pool_size", 0)
        self._pages = getattr(browser_engine, "_pages", None)

    async def start(self):
        """启动两套底层引擎，并记录当前 API 路由策略。"""
        log.info("[HybridEngine] 启动开始：先启动 httpx 引擎")
        await self.httpx_engine.start()
        log.info("[HybridEngine] 第一步完成：httpx 已启动，继续启动浏览器引擎")
        await self.browser_engine.start()
        self._started = bool(getattr(self.httpx_engine, "_started", False) and getattr(self.browser_engine, "_started", False))
        log.info(f"[HybridEngine] 已启动：api_call=httpx优先，fetch_chat=httpx优先，started={self._started} browser_started={getattr(self.browser_engine, '_started', False)} httpx_started={getattr(self.httpx_engine, '_started', False)}")

    async def stop(self):
        try:
            await self.httpx_engine.stop()
        finally:
            await self.browser_engine.stop()
        self._started = False
        log.info("[HybridEngine] 已停止")

    async def api_call(self, method: str, path: str, token: str, body: dict = None) -> dict:
        """优先使用直连引擎，失败后再回退浏览器。"""
        log.info(f"[HybridEngine] api_call 路由：优先走 httpx，method={method} path={path}")
        result = await self.httpx_engine.api_call(method, path, token, body)
        status = result.get("status")
        body_text = (result.get("body") or "").lower()
        if _should_fallback(status, body_text):
            log.warning(f"[HybridEngine] api_call 回退到 browser，method={method} path={path} status={status} body_preview={_body_preview(result)!r}")
            return await self.browser_engine.api_call(method, path, token, body)
        log.info(f"[HybridEngine] api_call 实际由 httpx 完成，method={method} path={path} status={status}")
        return result

    async def fetch_chat(self, token: str, chat_id: str, payload: dict, buffered: bool = False):
        """优先使用直连流式通道，失败后再回退浏览器通道。

        参数:
            token: 上游账号 Bearer Token。
            chat_id: 已创建的上游会话 ID。
            payload: 发送给上游的聊天请求体。
            buffered: 兼容旧接口的保留参数，透传到底层引擎。
        返回:
            async generator: 连续产出底层引擎返回的流式分片或错误对象。
        边界条件:
            只要首个成功分片已经发出，就不再切换引擎，避免客户端收到重复内容。
        """
        log.info(f"[HybridEngine] fetch_chat 路由：优先走 httpx，chat_id={chat_id} buffered={buffered}")
        saw_success = False
        upstream_error = None
        try:
            async for item in self.httpx_engine.fetch_chat(token, chat_id, payload, buffered=buffered):
                status = item.get("status")
                if status in ("streamed", 200):
                    saw_success = True
                    yield item
                    continue
                body_text = (item.get("body") or "").lower()
                if _should_fallback(status, body_text) and not saw_success:
                    upstream_error = item
                    break
                if status == 0 and not saw_success:
                    upstream_error = item
                    break
                yield item
            if upstream_error is None:
                return
        except Exception as exc:
            if saw_success:
                return
            upstream_error = {"status": 0, "body": str(exc)}

        preview = ((upstream_error.get("body") or "")[:160]).replace("\n", "\\n") if isinstance(upstream_error, dict) else str(upstream_error)[:160]
        log.warning(
            f"[HybridEngine] fetch_chat httpx 失败，回退到 browser：chat_id={chat_id} "
            f"status={upstream_error.get('status') if isinstance(upstream_error, dict) else 'unknown'} "
            f"body_preview={preview!r}"
        )
        async for item in self.browser_engine.fetch_chat(token, chat_id, payload, buffered=buffered):
            yield item

    def status(self) -> dict:
        """返回混合引擎的当前运行状态和实际 API 路由策略。"""
        free_pages = 0
        queue = 0
        if self._pages is not None:
            try:
                free_pages = self._pages.qsize()
                queue = max(0, self.pool_size - free_pages)
            except Exception:
                free_pages = 0
                queue = 0
        return {
            "started": self._started,
            "mode": "hybrid",
            "stream_via": "httpx_first",
            "api_via": "httpx_first",
            "browser_started": getattr(self.browser_engine, "_started", False),
            "httpx_started": getattr(self.httpx_engine, "_started", False),
            "pool_size": self.pool_size,
            "free_pages": free_pages,
            "queue": queue,
        }
