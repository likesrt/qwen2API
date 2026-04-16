"""浏览器引擎：使用 Camoufox 代理真实页面请求和聊天流。"""

import asyncio
import logging
import os
import random
import uuid
from contextlib import asynccontextmanager

from backend.core.config import settings
from backend.core.proxy import proxy_manager

log = logging.getLogger("qwen2api.browser")


def _request_jitter_seconds() -> float:
    """按配置生成一次请求抖动时间。"""
    low = max(0, settings.REQUEST_JITTER_MIN_MS)
    high = max(low, settings.REQUEST_JITTER_MAX_MS)
    return random.uniform(low, high) / 1000.0


JS_FETCH = (
    "async (args) => {"
    "const opts={method:args.method,headers:{'Content-Type':'application/json','Authorization':'Bearer '+args.token}};"
    "if(args.body)opts.body=JSON.stringify(args.body);"
    "const res=await fetch(args.url,opts);"
    "const text=await res.text();"
    "return{status:res.status,body:text};"
    "}"
)

JS_STREAM_POLL = (
    "async (args) => {"
    "const ctrl=new AbortController();"
    "const tmr=setTimeout(()=>ctrl.abort(),1800000);"
    "const store=(window.__qwenStreamState=window.__qwenStreamState||{});"
    "const state=store[args.scriptName]||{status:0,queue:[],done:false,error:'',started:false};"
    "store[args.scriptName]=state;"
    "try{"
    "const res=await fetch(args.url,{method:'POST',"
    "headers:{'Content-Type':'application/json','Authorization':'Bearer '+args.token,'Accept':'text/event-stream'},"
    "body:JSON.stringify(args.payload),signal:ctrl.signal});"
    "state.status=res.status;state.started=true;"
    "if(!res.ok){state.error=(await res.text()).substring(0,2000);state.done=true;clearTimeout(tmr);return state;}"
    "const rdr=res.body.getReader();"
    "const dec=new TextDecoder();"
    "let pending='';"
    "while(true){"
    "const{done,value}=await rdr.read();"
    "if(done)break;"
    "pending+=dec.decode(value,{stream:true}).replace(/\r\n/g,'\n').replace(/\r/g,'\n');"
    "let idx=pending.indexOf('\n\n');"
    "while(idx!==-1){state.queue.push(pending.slice(0,idx)+'\n\n');pending=pending.slice(idx+2);idx=pending.indexOf('\n\n');}"
    "}"
    "pending+=dec.decode();"
    "pending=pending.replace(/\r\n/g,'\n').replace(/\r/g,'\n');"
    "if(pending)state.queue.push(pending);"
    "state.done=true;clearTimeout(tmr);return state;"
    "}catch(e){"
    "state.error='JS error: '+e.message;state.done=true;clearTimeout(tmr);return state;"
    "}"
    "}"
)

_CAMOUFOX_OPTS = {
    "headless": True,
    "humanize": True,
    "i_know_what_im_doing": True,
    "os": "windows",
    "locale": "zh-CN",
    "firefox_user_prefs": {
        "gfx.webrender.software": True,
        "media.hardware-video-decoding.enabled": False,
        "browser.cache.disk.enable": True,
        "browser.cache.memory.enable": True,
        "app.update.auto": False,
        "browser.shell.checkDefaultBrowser": False,
    },
}


def _browser_options() -> dict:
    """根据当前代理配置构造 Camoufox 启动参数。"""
    options = dict(_CAMOUFOX_OPTS)
    proxy = proxy_manager.get_browser_proxy()
    if proxy:
        options["proxy"] = proxy
    return options


def _should_retry_browser_api(result: dict) -> bool:
    """判断浏览器 API 调用结果是否适合立即换页重试。"""
    body = str(result.get("body") or "")
    return result.get("status") == 0 and ("NetworkError" in body or body.startswith("JS error:"))


def _stream_script_name() -> str:
    """生成一次性页面脚本名，避免并发流请求之间互相覆盖。

    返回:
        str: 当前流请求专属的脚本标识。
    副作用:
        名称只在单次 fetch_chat 生命周期内使用，不写入持久状态。
    """
    return f"qwen_stream_{uuid.uuid4().hex}"


@asynccontextmanager
async def _new_browser():
    """创建一个临时 Camoufox 浏览器实例，用于注册和激活流程。"""
    from camoufox.async_api import AsyncCamoufox

    async with AsyncCamoufox(**_browser_options()) as browser:
        yield browser


class BrowserEngine:
    """基于 Camoufox 的浏览器引擎，提供统一 API 调用和流式聊天能力。"""

    def __init__(self, pool_size: int = 3, base_url: str = "https://chat.qwen.ai"):
        """初始化页面池大小和基础地址。"""
        self.pool_size = pool_size
        self.base_url = base_url
        self._browser = None
        self._browser_cm = None
        self._pages: asyncio.Queue = asyncio.Queue()
        self._started = False
        self._ready = asyncio.Event()

    async def start(self):
        """启动浏览器引擎，失败时保留 ready 信号避免调用方永久等待。"""
        if self._started:
            return
        try:
            await self._start_camoufox()
        except Exception as exc:
            log.error(f"[Browser] camoufox failed: {exc}")
        finally:
            self._ready.set()

    async def _start_camoufox(self):
        """安装并启动 Camoufox 浏览器，然后预热页面池。"""
        await self._ensure_browser_installed()
        from camoufox.async_api import AsyncCamoufox

        log.info("Starting browser engine (camoufox)...")
        self._browser_cm = AsyncCamoufox(**_browser_options())
        self._browser = await self._browser_cm.__aenter__()
        await self._init_pages()
        self._started = True
        log.info("Browser engine started")

    async def _init_pages(self):
        """初始化固定数量的页面实例，减少首次请求延迟。"""
        log.info(f"[Browser] 正在初始化 {self.pool_size} 个并发渲染引擎页面...")
        for index in range(self.pool_size):
            page = await self._browser.new_page()
            try:
                await page.set_viewport_size({"width": 1920, "height": 1080})
            except Exception:
                pass
            try:
                await page.goto(self.base_url, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            await asyncio.sleep(0.5)
            self._pages.put_nowait(page)
            log.info(f"  [Browser] Page {index + 1}/{self.pool_size} ready")

    @staticmethod
    async def _ensure_browser_installed():
        """确认 Camoufox 已安装，缺失时自动下载。"""
        import subprocess
        import sys

        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run([sys.executable, "-m", "camoufox", "path"], capture_output=True, text=True, timeout=10),
            )
            cache_dir = result.stdout.strip()
            if cache_dir:
                exe_name = "camoufox.exe" if os.name == "nt" else "camoufox"
                exe_path = os.path.join(cache_dir, exe_name)
                if os.path.exists(exe_path):
                    return
        except Exception:
            pass
        log.info("[Browser] 未检测到 camoufox，正在自动下载...")
        try:
            loop = asyncio.get_event_loop()

            def _do_install():
                from camoufox.pkgman import CamoufoxFetcher

                CamoufoxFetcher().install()

            await loop.run_in_executor(None, _do_install)
        except Exception as exc:
            log.error(f"[Browser] 下载失败: {exc}")

    async def stop(self):
        """关闭浏览器和页面池，释放 Camoufox 资源。"""
        self._started = False
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._browser_cm:
            try:
                await self._browser_cm.__aexit__(None, None, None)
            except Exception:
                pass

    async def api_call(self, method: str, path: str, token: str, body: dict = None) -> dict:
        """用浏览器页面发起一次 API 调用，必要时换页重试一次。"""
        await asyncio.wait_for(self._ready.wait(), timeout=300)
        if not self._started:
            return {"status": 0, "body": "Browser engine failed to start"}
        try:
            page = await asyncio.wait_for(self._pages.get(), timeout=60)
        except asyncio.TimeoutError:
            return {"status": 429, "body": "Too Many Requests (Queue full)"}
        needs_refresh = False
        try:
            await asyncio.sleep(_request_jitter_seconds())
            result = await page.evaluate(JS_FETCH, {"method": method, "url": path, "token": token, "body": body or {}})
            if _should_retry_browser_api(result):
                needs_refresh = True
                return await self._retry_api_call(method, path, token, body or {})
            return result
        except Exception as exc:
            log.error(f"api_call error: {exc}")
            needs_refresh = True
            return await self._retry_api_call(method, path, token, body or {}, error=str(exc))
        finally:
            if needs_refresh:
                asyncio.create_task(self._refresh_page_and_return(page))
            else:
                self._pages.put_nowait(page)

    async def _retry_api_call(self, method: str, path: str, token: str, body: dict, error: str = "") -> dict:
        """为瞬时网络错误切换页面后补发一次 API 请求。"""
        try:
            retry_page = await asyncio.wait_for(self._pages.get(), timeout=10)
        except asyncio.TimeoutError:
            return {"status": 0, "body": error or "Browser retry page unavailable"}
        try:
            await self._refresh_page(retry_page)
            await asyncio.sleep(_request_jitter_seconds())
            return await retry_page.evaluate(JS_FETCH, {"method": method, "url": path, "token": token, "body": body})
        except Exception as exc:
            log.error(f"api_call retry error: {exc}")
            return {"status": 0, "body": error or str(exc)}
        finally:
            self._pages.put_nowait(retry_page)

    async def _start_page_stream(self, page, script_name: str, url: str, token: str, payload: dict) -> None:
        """在页面里启动后台流请求，并把结果写入共享状态。

        参数:
            page: 当前占用的浏览器页面实例。
            script_name: 本次流请求对应的唯一状态键。
            url: 上游聊天接口地址。
            token: 上游账号 Bearer Token。
            payload: 发送给上游的聊天请求体。
        返回:
            None: 页面端后台任务启动成功后立即返回。
        副作用:
            会在页面全局对象 `window.__qwenStreamState` 中创建本次流请求的状态槽位。
        """
        await page.evaluate(
            """async (args) => {
                window.__qwenStreamState = window.__qwenStreamState || {};
                const task = async () => { await (%s)(args); };
                task();
            }""" % JS_STREAM_POLL,
            {"url": url, "token": token, "payload": payload, "scriptName": script_name},
        )

    async def _read_page_stream_state(self, page, script_name: str) -> dict:
        """读取页面端当前流状态，并清空已经取走的待发送队列。

        参数:
            page: 当前占用的浏览器页面实例。
            script_name: 本次流请求对应的唯一状态键。
        返回:
            dict: 包含 status、queue、done、error 的当前快照。
        边界条件:
            如果页面里的状态槽位意外丢失，会返回 `stream state missing` 供上层判定失败。
        """
        return await asyncio.wait_for(
            page.evaluate(
                """(name) => {
                    const store = window.__qwenStreamState || {};
                    const state = store[name];
                    if (!state) return {status: 0, queue: [], done: true, error: 'stream state missing'};
                    const queue = Array.isArray(state.queue) ? state.queue.splice(0, state.queue.length) : [];
                    return {status: state.status || 0, queue, done: Boolean(state.done), error: state.error || ''};
                }""",
                script_name,
            ),
            timeout=30,
        )

    async def _poll_page_stream(self, page, script_name: str):
        """轮询页面流状态，并逐条产出已经闭合的 SSE 事件。

        参数:
            page: 当前占用的浏览器页面实例。
            script_name: 本次流请求对应的唯一状态键。
        返回:
            async generator: 连续产出 `streamed` 分片或错误对象。
        边界条件:
            当页面端已经发送过部分内容后又报错时，也会把错误继续向上抛，避免客户端只看到静默截断。
        """
        sent_any = False
        while True:
            state = await self._read_page_stream_state(page, script_name)
            if not isinstance(state, dict):
                yield {"status": 0, "body": str(state)}
                return
            for message in state.get("queue", []):
                sent_any = True
                yield {"status": "streamed", "chunk": message}
            if state.get("done"):
                if state.get("status") and state.get("status") != 200 and not sent_any:
                    yield {"status": state.get("status"), "body": state.get("error") or "Browser stream request failed"}
                    return
                if state.get("error"):
                    yield {"status": 0, "body": state.get("error")}
                return
            await asyncio.sleep(0.05)

    async def _clear_page_stream_state(self, page, script_name: str) -> None:
        """清理页面上的临时流状态，避免后续请求误读旧数据。

        参数:
            page: 当前占用的浏览器页面实例。
            script_name: 本次流请求对应的唯一状态键。
        返回:
            None: 清理完成后不返回额外数据。
        副作用:
            会删除页面全局对象 `window.__qwenStreamState` 下对应的临时槽位。
        """
        try:
            await page.evaluate("""(name) => { if (window.__qwenStreamState) delete window.__qwenStreamState[name]; }""", script_name)
        except Exception:
            pass

    async def fetch_chat(self, token: str, chat_id: str, payload: dict, buffered: bool = False):
        """通过浏览器页面执行聊天流式请求，并实时返回已经闭合的 SSE 事件。

        参数:
            token: 上游账号 Bearer Token。
            chat_id: 已创建的上游会话 ID。
            payload: 发送给上游的聊天请求体。
            buffered: 兼容旧接口的保留参数，当前始终按流式处理。
        返回:
            async generator: 连续产出 `streamed` 分片或错误对象。
        边界条件:
            页面流在输出过部分内容后若异常结束，会继续向上报告错误，避免客户端把半截内容当成正常完成。
        """
        await asyncio.wait_for(self._ready.wait(), timeout=300)
        if not self._started:
            yield {"status": 0, "body": "Browser engine failed to start"}
            return
        try:
            page = await asyncio.wait_for(self._pages.get(), timeout=60)
        except asyncio.TimeoutError:
            yield {"status": 429, "body": "Too Many Requests (Queue full)"}
            return
        needs_refresh = False
        script_name = _stream_script_name()
        url = f"/api/v2/chat/completions?chat_id={chat_id}"
        try:
            await asyncio.sleep(_request_jitter_seconds())
            await self._start_page_stream(page, script_name, url, token, payload)
            async for item in self._poll_page_stream(page, script_name):
                if item.get("status") == 0:
                    needs_refresh = True
                yield item
        except asyncio.TimeoutError:
            needs_refresh = True
            yield {"status": 0, "body": "Timeout"}
        except Exception as exc:
            needs_refresh = True
            yield {"status": 0, "body": str(exc)}
        finally:
            await self._clear_page_stream_state(page, script_name)
            if needs_refresh:
                asyncio.create_task(self._refresh_page_and_return(page))
            else:
                self._pages.put_nowait(page)

    async def _refresh_page(self, page):
        """刷新异常页面，尽量恢复到基础站点。"""
        try:
            await asyncio.wait_for(page.goto(self.base_url, wait_until="domcontentloaded"), timeout=20000)
        except Exception:
            pass

    async def _refresh_page_and_return(self, page):
        """刷新页面后重新放回页面池。"""
        await self._refresh_page(page)
        self._pages.put_nowait(page)
