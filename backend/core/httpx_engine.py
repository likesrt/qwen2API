"""httpx/curl_cffi 引擎：使用浏览器指纹直连 Qwen API。"""

import asyncio
import codecs
import json
import logging

from backend.core.proxy import proxy_manager

log = logging.getLogger("qwen2api.httpx_engine")

BASE_URL = "https://chat.qwen.ai"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://chat.qwen.ai/",
    "Origin": "https://chat.qwen.ai",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

_IMPERSONATE = "chrome124"


def _split_sse_messages(buffer: str) -> tuple[list[str], str]:
    """提取完整 SSE 消息，保留末尾未闭合片段以避免分块截断。

    参数:
        buffer: 当前累计的 SSE 文本缓冲区。
    返回:
        tuple[list[str], str]: 已闭合消息列表与剩余未闭合文本。
    边界条件:
        当上游事件恰好被拆在 chunk 边界时，不会提前产出半条消息。
    """
    messages: list[str] = []
    while True:
        sep_index = buffer.find("\n\n")
        if sep_index == -1:
            return messages, buffer
        messages.append(buffer[:sep_index])
        buffer = buffer[sep_index + 2:]


class HttpxEngine:
    """基于 curl_cffi 的直连引擎，保持与 BrowserEngine 相同接口。"""

    def __init__(self, pool_size: int = 3, base_url: str = BASE_URL):
        """初始化基础地址和启动状态。"""
        self.base_url = base_url
        self._started = False
        self._ready = asyncio.Event()

    async def start(self):
        """启动直连引擎。"""
        self._started = True
        self._ready.set()
        log.info("[HttpxEngine] 已启动（curl_cffi Chrome指纹直连模式）")

    async def stop(self):
        """停止直连引擎。"""
        self._started = False
        log.info("[HttpxEngine] 已停止")

    def _auth_headers(self, token: str) -> dict:
        """构造包含 Bearer Token 的请求头。"""
        return {**_HEADERS, "Authorization": f"Bearer {token}"}

    async def api_call(self, method: str, path: str, token: str, body: dict = None) -> dict:
        """通过 curl_cffi 发送一次普通 API 请求。"""
        from curl_cffi.requests import AsyncSession

        url = self.base_url + path
        headers = {**self._auth_headers(token), "Content-Type": "application/json"}
        data = json.dumps(body, ensure_ascii=False).encode() if body else None
        proxy = proxy_manager.get_curl_cffi_proxy()
        try:
            async with AsyncSession(impersonate=_IMPERSONATE, timeout=30, proxy=proxy, trust_env=False) as client:
                resp = await client.request(method, url, headers=headers, data=data)
            return {"status": resp.status_code, "body": resp.text}
        except Exception as exc:
            log.error(f"[HttpxEngine] api_call error: {exc}")
            return {"status": 0, "body": str(exc)}

    async def fetch_chat(self, token: str, chat_id: str, payload: dict, buffered: bool = False):
        """通过 curl_cffi 发送 SSE 聊天请求并按完整事件逐步产出响应。

        参数:
            token: 上游账号 Bearer Token。
            chat_id: 已创建的上游会话 ID。
            payload: 发送给上游的聊天请求体。
            buffered: 兼容旧接口的保留参数，当前不启用整包缓冲。
        返回:
            async generator: 逐条产出完整 SSE 消息或错误结果。
        边界条件:
            UTF-8 多字节字符和 SSE 事件都可能横跨网络 chunk，必须等消息闭合后再下发。
        """
        from curl_cffi.requests import AsyncSession

        url = self.base_url + f"/api/v2/chat/completions?chat_id={chat_id}"
        headers = {**self._auth_headers(token), "Content-Type": "application/json", "Accept": "text/event-stream"}
        body_bytes = json.dumps(payload, ensure_ascii=False).encode()
        proxy = proxy_manager.get_curl_cffi_proxy()
        try:
            async with AsyncSession(impersonate=_IMPERSONATE, timeout=1800, proxy=proxy, trust_env=False) as client:
                async with client.stream("POST", url, headers=headers, data=body_bytes) as resp:
                    if resp.status_code != 200:
                        body_chunks = []
                        async for chunk in resp.aiter_content():
                            body_chunks.append(chunk)
                        body_text = b"".join(body_chunks).decode(errors="replace")[:2000]
                        yield {"status": resp.status_code, "body": body_text}
                        return
                    decoder = codecs.getincrementaldecoder("utf-8")("replace")
                    pending = ""
                    async for chunk in resp.aiter_content():
                        pending += decoder.decode(chunk)
                        pending = pending.replace("\r\n", "\n").replace("\r", "\n")
                        messages, pending = _split_sse_messages(pending)
                        for message in messages:
                            yield {"status": "streamed", "chunk": f"{message}\n\n"}
                    pending += decoder.decode(b"", final=True)
                    pending = pending.replace("\r\n", "\n").replace("\r", "\n")
                    if pending:
                        yield {"status": "streamed", "chunk": pending}
        except Exception as exc:
            log.error(f"[HttpxEngine] fetch_chat error: {exc}")
            yield {"status": 0, "body": str(exc)}
