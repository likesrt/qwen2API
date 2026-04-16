from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
import asyncio as aio
import json
import logging
import uuid
import time
import re
from typing import Optional
from backend.core.account_pool import Account
from backend.services.qwen_client import QwenClient
from backend.services.token_calc import calculate_usage
from backend.services.prompt_builder import messages_to_prompt
from backend.services.tool_parser import parse_tool_calls, inject_format_reminder, build_tool_blocks_from_native_chunks, should_block_tool_call
from backend.core.config import resolve_model, settings, IMAGE_MODEL_DEFAULT

log = logging.getLogger("qwen2api.chat")
router = APIRouter()

async def _stream_events_with_cleanup(client: QwenClient, model: str, prompt: str, has_custom_tools: bool, exclude_accounts=None):
    """包装上游事件流，在下游取消时主动回收账号和会话。

    参数:
        client: 当前 QwenClient 实例。
        model: 本次请求使用的模型名。
        prompt: 已构造完成的提示词。
        has_custom_tools: 是否启用了工具模式。
        exclude_accounts: 当前轮次需要排除的账号集合。
    返回:
        async generator: 原样转发 meta 与 event 项。
    边界条件:
        客户端断开会触发取消，此时会删除上游 chat 并释放 inflight 占用。
    """
    acc: Optional[Account] = None
    chat_id: Optional[str] = None
    try:
        async for item in client.chat_stream_events_with_retry(model, prompt, has_custom_tools=has_custom_tools, exclude_accounts=exclude_accounts):
            if item.get("type") == "meta":
                chat_id = item.get("chat_id")
                meta_acc = item.get("acc")
                if isinstance(meta_acc, Account):
                    acc = meta_acc
            yield item
    except aio.CancelledError:
        await client._abort_active_chat(acc, chat_id)
        raise


def _oai_error_payload(detail) -> dict:
    """把异常详情归一化为 OpenAI 兼容 error 对象。

    参数:
        detail: 原始异常详情，可能是字符串或已有字典。
    返回:
        dict: 形如 `{"error": {"message": ..., "type": ...}}` 的错误对象。
    边界条件:
        如果 detail 已经是合法 error 对象，会直接复用，避免重复包裹。
    """
    if isinstance(detail, dict) and isinstance(detail.get("error"), dict):
        return detail
    if isinstance(detail, dict) and {"message", "type"}.issubset(detail):
        return {"error": detail}
    message = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
    return {"error": {"message": message, "type": "api_error"}}


def _oai_error_chunk(detail) -> str:
    """把错误对象序列化成 OpenAI 兼容 SSE 数据块。

    参数:
        detail: 原始异常详情，可能是字符串或已有字典。
    返回:
        str: 可直接下发给流式客户端的 `data:` 错误块。
    边界条件:
        该函数只负责错误包格式，不附带 `[DONE]`，由调用方决定是否结束流。
    """
    return f"data: {json.dumps(_oai_error_payload(detail), ensure_ascii=False)}\n\n"


async def _stream_items_with_keepalive(client, model: str, prompt: str, has_custom_tools: bool, exclude_accounts=None):
    """为上游事件流增加 keepalive，并在消费端断开时停止生产者。

    参数:
        client: 当前 QwenClient 实例。
        model: 本次请求使用的模型名。
        prompt: 已构造完成的提示词。
        has_custom_tools: 是否启用了工具模式。
        exclude_accounts: 当前轮次需要排除的账号集合。
    返回:
        async generator: 轮询队列后连续产出 item、error 或 keepalive。
    边界条件:
        客户端断开会取消 producer task，避免后台继续占用上游流式连接。
    """
    queue: aio.Queue = aio.Queue()

    async def _producer():
        try:
            async for item in _stream_events_with_cleanup(client, model, prompt, has_custom_tools=has_custom_tools, exclude_accounts=exclude_accounts):
                await queue.put(("item", item))
        except Exception as e:
            await queue.put(("error", e))
        finally:
            await queue.put(("done", None))

    producer_task = aio.create_task(_producer())
    try:
        while True:
            try:
                kind, payload = await aio.wait_for(queue.get(), timeout=max(1, settings.STREAM_KEEPALIVE_INTERVAL))
            except aio.TimeoutError:
                yield {"type": "keepalive"}
                continue

            if kind == "item":
                yield payload
            elif kind == "error":
                raise payload
            elif kind == "done":
                break
    finally:
        if not producer_task.done():
            producer_task.cancel()
            try:
                await producer_task
            except aio.CancelledError:
                pass

def _extract_blocked_tool_names(text: str) -> list[str]:
    if not text:
        return []
    return re.findall(r"Tool\s+([A-Za-z0-9_.:-]+)\s+does not exists?\.?", text)

def _has_recent_unchanged_read_result(messages) -> bool:
    checked = 0
    for msg in reversed(messages or []):
        checked += 1
        content = msg.get("content", "")
        texts = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    t = part.get("type")
                    if t == "text":
                        texts.append(part.get("text", ""))
                    elif t == "tool_result":
                        inner = part.get("content", "")
                        if isinstance(inner, str):
                            texts.append(inner)
                        elif isinstance(inner, list):
                            for p in inner:
                                if isinstance(p, dict) and p.get("type") == "text":
                                    texts.append(p.get("text", ""))
                elif isinstance(part, str):
                    texts.append(part)
        merged = "\n".join(t for t in texts if t)
        if "Unchanged since last read" in merged:
            return True
        if checked >= 10:
            break
    return False

_T2I_PATTERN = re.compile(
    r'(生成图片|画(一|个|张)?图|draw|generate\s+image|create\s+image|make\s+image|图片生成|文生图|生成一张|画一张)',
    re.IGNORECASE
)
_T2V_PATTERN = re.compile(
    r'(生成视频|make\s+video|generate\s+video|create\s+video|视频生成|文生视频)',
    re.IGNORECASE
)

def _detect_media_intent(messages: list) -> str:
    """Return 't2i', 't2v', or 't2t' based on last user message."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                text = " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
            else:
                text = str(content)
            if _T2V_PATTERN.search(text):
                return "t2v"
            if _T2I_PATTERN.search(text):
                return "t2i"
            break
    return "t2t"

def _extract_last_user_text(messages: list) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                return " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
            return str(content)
    return ""

def _extract_image_urls(text: str) -> list[str]:
    urls: list[str] = []
    for u in re.findall(r'!\[.*?\]\((https?://[^\s\)]+)\)', text):
        urls.append(u.rstrip(").,;"))
    if not urls:
        for u in re.findall(r'"(?:url|image|src|imageUrl|image_url)"\s*:\s*"(https?://[^"]+)"', text):
            urls.append(u)
    if not urls:
        cdn_pattern = r'https?://(?:wanx\.alicdn\.com|img\.alicdn\.com|[^\s"<>]+\.(?:jpg|jpeg|png|webp|gif))[^\s"<>]*'
        for u in re.findall(cdn_pattern, text, re.IGNORECASE):
            urls.append(u.rstrip(".,;)\"'>"))
    seen: set[str] = set()
    result: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result


def _oai_chunk_payload(completion_id: str, created: int, model_name: str, delta: dict, finish: str | None = None) -> str:
    """构造一条 OpenAI 兼容的 SSE 数据块。

    参数:
        completion_id: 当前补全请求 ID。
        created: Unix 时间戳。
        model_name: 下游请求声明的模型名。
        delta: 当前这条 chunk 的增量载荷。
        finish: 可选的结束原因。
    返回:
        str: 已带 `data:` 前缀和空行结尾的 SSE 文本。
    边界条件:
        该函数只负责序列化，不校验 delta 语义是否符合客户端期望。
    """
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_name,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _merge_native_tool_delta(native_tc_chunks: dict, evt: dict) -> tuple[str, dict, str]:
    """合并一条原生 tool_call 分片，并返回本次新增的参数片段。

    参数:
        native_tc_chunks: 当前会话已收集的原生工具调用状态。
        evt: 单条统一 delta 事件。
    返回:
        tuple[str, dict, str]: tool_call_id、合并后的状态、当前新增 arguments 片段。
    边界条件:
        当 content 不是合法 JSON 时，会把整段内容按 arguments 追加，避免参数被静默丢弃。
    """
    tc_id = evt.get("extra", {}).get("tool_call_id", "tc_0")
    state = native_tc_chunks.setdefault(tc_id, {"name": "", "args": ""})
    args_delta = ""
    try:
        chunk = json.loads(evt.get("content", ""))
    except (json.JSONDecodeError, TypeError, ValueError):
        args_delta = evt.get("content", "")
    else:
        if chunk.get("name"):
            state["name"] = chunk["name"]
        if chunk.get("arguments"):
            args_delta = chunk["arguments"]
    if args_delta:
        state["args"] += args_delta
    return tc_id, state, args_delta


def _build_oai_native_tool_chunks(
    completion_id: str,
    created: int,
    model_name: str,
    tool_indexes: dict,
    started_tools: set,
    tc_id: str,
    state: dict,
    args_delta: str,
) -> list[str]:
    """把原生 tool_call 状态转成 OpenAI 流式 tool_calls 分片。

    参数:
        completion_id: 当前补全请求 ID。
        created: Unix 时间戳。
        model_name: 下游请求声明的模型名。
        tool_indexes: tool_call_id 到下标的映射。
        started_tools: 已经向客户端发过头部的 tool_call_id 集合。
        tc_id: 当前工具调用 ID。
        state: 当前工具调用的累计状态。
        args_delta: 当前新增的 arguments 片段。
    返回:
        list[str]: 可直接 yield 的 SSE 文本列表。
    边界条件:
        当名称晚于参数到达时，会在首次拿到名称后把已缓存参数一次性补发给客户端。
    """
    chunks: list[str] = []
    index = tool_indexes.setdefault(tc_id, len(tool_indexes))
    just_started = False
    if state.get("name") and tc_id not in started_tools:
        chunks.append(_oai_chunk_payload(completion_id, created, model_name, {
            "tool_calls": [{"index": index, "id": tc_id, "type": "function", "function": {"name": state["name"], "arguments": ""}}]
        }))
        started_tools.add(tc_id)
        just_started = True
    if tc_id in started_tools:
        arguments = state.get("args", "") if just_started else args_delta
        if arguments:
            chunks.append(_oai_chunk_payload(completion_id, created, model_name, {
                "tool_calls": [{"index": index, "function": {"arguments": arguments}}]
            }))
    return chunks


def _build_oai_tool_use_chunks(completion_id: str, created: int, model_name: str, tool_blocks: list[dict]) -> list[str]:
    """把完整工具调用块转换成 OpenAI 流式 tool_calls 输出。

    参数:
        completion_id: 当前补全请求 ID。
        created: Unix 时间戳。
        model_name: 下游请求声明的模型名。
        tool_blocks: 已解析完成的 tool_use 块列表。
    返回:
        list[str]: 可直接下发给客户端的 SSE 文本列表。
    边界条件:
        该函数按完整参数一次性输出，适合文本解析或已完成的原生工具调用。
    """
    chunks: list[str] = []
    tc_list = [b for b in tool_blocks if b.get("type") == "tool_use"]
    for idx, tc in enumerate(tc_list):
        chunks.append(_oai_chunk_payload(completion_id, created, model_name, {
            "tool_calls": [{"index": idx, "id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": ""}}]
        }))
        chunks.append(_oai_chunk_payload(completion_id, created, model_name, {
            "tool_calls": [{"index": idx, "function": {"arguments": json.dumps(tc.get("input", {}), ensure_ascii=False)}}]
        }))
    return chunks


async def _release_stream_account(client: QwenClient, acc: Optional[Account], chat_id: Optional[str]) -> None:
    """释放账号并异步删除会话，避免流式提前返回后残留占用。

    参数:
        client: 当前 QwenClient 实例。
        acc: 当前占用的账号对象。
        chat_id: 对应的上游会话 ID。
    返回:
        None: 仅做资源回收，不返回额外数据。
    边界条件:
        当账号或会话为空时会直接跳过，避免清理阶段再次抛错。
    """
    if acc is None:
        return
    client.account_pool.release(acc)
    if chat_id:
        aio.create_task(client.delete_chat(acc.token, chat_id))


@router.post("/completions")
@router.post("/chat/completions")
@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    app = request.app
    users_db = app.state.users_db
    client: QwenClient = app.state.qwen_client

    # 鉴权
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""

    if not token:
        token = request.headers.get("x-api-key", "").strip()
    if not token:
        token = request.query_params.get("key", "").strip() or request.query_params.get("api_key", "").strip()

    from backend.core.config import API_KEYS
    admin_k = settings.ADMIN_KEY

    if API_KEYS:
        if token != admin_k and token not in API_KEYS and not token:
            raise HTTPException(status_code=401, detail="Invalid API Key")

    # 获取下游用户并处理配额
    users = await users_db.get()
    user = next((u for u in users if u["id"] == token), None)
    if user and user.get("quota", 0) <= user.get("used_tokens", 0):
        raise HTTPException(status_code=402, detail="Quota Exceeded")
        
    try:
        req_data = await request.json()
    except Exception:
        raise HTTPException(400, {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}})
        
    model_name = req_data.get("model", "gpt-3.5-turbo")
    qwen_model = resolve_model(model_name)
    stream = req_data.get("stream", False)
    
    prompt, tools = messages_to_prompt(req_data)
    log.info(f"[OAI] model={qwen_model}, stream={stream}, tools={[t.get('name') for t in tools]}, prompt_len={len(prompt)}")
    history_messages = req_data.get("messages", [])

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    # Media intent routing: auto-detect image / video generation requests
    media_intent = _detect_media_intent(history_messages)
    if media_intent == "t2v":
        log.warning("[OAI] t2v intent detected but not yet validated; falling back to t2t")
        media_intent = "t2t"

    if media_intent == "t2i":
        image_prompt = _extract_last_user_text(history_messages)
        log.info(f"[OAI-T2I] Routing to image generation, model={IMAGE_MODEL_DEFAULT}, prompt={image_prompt[:80]!r}")

        if stream:
            async def generate_image_stream():
                mk = lambda delta, finish=None: json.dumps({
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created, "model": model_name,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]
                }, ensure_ascii=False)
                try:
                    answer_text, acc, chat_id = await client.image_generate_with_retry(IMAGE_MODEL_DEFAULT, image_prompt)
                    client.account_pool.release(acc)
                    aio.create_task(client.delete_chat(acc.token, chat_id))
                    image_urls = _extract_image_urls(answer_text)
                    content = "\n".join(f"![generated]({u})" for u in image_urls) if image_urls else answer_text
                    yield f"data: {mk({'role': 'assistant'})}\n\n"
                    yield f"data: {mk({'content': content})}\n\n"
                    yield f"data: {mk({}, 'stop')}\n\n"
                    yield "data: [DONE]\n\n"
                except Exception as e:
                    log.error(f"[OAI-T2I] 生成失败: {e}")
                    yield _oai_error_chunk(str(e))
            return StreamingResponse(generate_image_stream(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        else:
            try:
                answer_text, acc, chat_id = await client.image_generate_with_retry(IMAGE_MODEL_DEFAULT, image_prompt)
                client.account_pool.release(acc)
                aio.create_task(client.delete_chat(acc.token, chat_id))
                image_urls = _extract_image_urls(answer_text)
                content = "\n".join(f"![generated]({u})" for u in image_urls) if image_urls else answer_text
                from fastapi.responses import JSONResponse
                return JSONResponse({
                    "id": completion_id, "object": "chat.completion", "created": created, "model": model_name,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                    "images": image_urls,
                    "usage": {"prompt_tokens": len(image_prompt), "completion_tokens": len(content),
                              "total_tokens": len(image_prompt) + len(content)}
                })
            except Exception as e:
                log.error(f"[OAI-T2I] 生成失败: {e}")
                raise HTTPException(status_code=503, detail=_oai_error_payload(str(e))["error"])

    if stream:
        async def generate():
            current_prompt = prompt
            excluded_accounts = set()
            max_attempts = settings.TOOL_MAX_RETRIES if tools else settings.MAX_RETRIES
            for stream_attempt in range(max_attempts):
              try:
                events = []
                chat_id: Optional[str] = None
                acc: Optional[Account] = None

                # ── 无工具：事件到来立即转发给客户端（真流式）──────────────
                if not tools:
                    sent_role = False
                    streamed_len = 0
                    async for item in _stream_items_with_keepalive(client, qwen_model, current_prompt, has_custom_tools=False, exclude_accounts=excluded_accounts):
                        if item["type"] == "keepalive":
                            yield ": keepalive\n\n"
                            continue
                        if item["type"] == "meta":
                            chat_id = item["chat_id"]
                            meta_acc = item["acc"]
                            if isinstance(meta_acc, Account):
                                acc = meta_acc
                            yield ": upstream-connected\n\n"
                            continue
                        if item["type"] != "event":
                            continue
                        evt = item["event"]
                        if evt.get("type") != "delta":
                            continue
                        phase = evt.get("phase", "")
                        content = evt.get("content", "")
                        if phase == "answer" and content:
                            if not sent_role:
                                mk = lambda delta, finish=None: json.dumps({
                                    "id": completion_id, "object": "chat.completion.chunk",
                                    "created": created, "model": model_name,
                                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]
                                }, ensure_ascii=False)
                                yield f"data: {mk({'role': 'assistant'})}\n\n"
                                sent_role = True
                            streamed_len += len(content)
                            yield f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model_name, 'choices': [{'index': 0, 'delta': {'content': content}, 'finish_reason': None}]}, ensure_ascii=False)}\n\n"

                    # 空响应重试（还没发过内容才重试）
                    if streamed_len == 0 and stream_attempt < min(settings.EMPTY_RESPONSE_RETRIES, max_attempts - 1):
                        if acc is not None:
                            client.account_pool.release(acc)
                            if chat_id:
                                aio.create_task(client.delete_chat(acc.token, chat_id))
                            excluded_accounts.add(acc.email)
                        log.warning(f"[Stream] 空响应，重试 (attempt {stream_attempt+1}/{settings.MAX_RETRIES})")
                        await aio.sleep(0.3)
                        continue

                    if not sent_role:
                        yield f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model_name, 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]}, ensure_ascii=False)}\n\n"
                    yield f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model_name, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"

                    users = await users_db.get()
                    for u in users:
                        if u["id"] == token:
                            u["used_tokens"] += streamed_len + len(prompt)
                            break
                    await users_db.save(users)
                    if acc is not None:
                        client.account_pool.release(acc)
                        if chat_id:
                            aio.create_task(client.delete_chat(acc.token, chat_id))
                    return

                # ── 有工具：原生 tool_call / reasoning 先实时下发，文本回答保留到收尾判定──────────────
                sent_role = False
                streamed_reasoning = False
                answer_text = ""
                reasoning_text = ""
                native_tc_chunks: dict = {}
                tool_indexes: dict = {}
                started_tools: set[str] = set()
                emitted_payload = False

                async for item in _stream_items_with_keepalive(client, qwen_model, current_prompt, has_custom_tools=True, exclude_accounts=excluded_accounts):
                    if item["type"] == "keepalive":
                        yield ": keepalive\n\n"
                        continue
                    if item["type"] == "meta":
                        chat_id = item["chat_id"]
                        meta_acc = item["acc"]
                        if isinstance(meta_acc, Account):
                            acc = meta_acc
                        yield ": upstream-connected\n\n"
                        continue
                    if item["type"] != "event":
                        continue
                    evt = item["event"]
                    if evt.get("type") != "delta":
                        continue
                    phase = evt.get("phase", "")
                    content = evt.get("content", "")
                    if phase in ("think", "thinking_summary") and content:
                        reasoning_text += content
                        if not sent_role:
                            yield _oai_chunk_payload(completion_id, created, model_name, {"role": "assistant"})
                            sent_role = True
                        yield _oai_chunk_payload(completion_id, created, model_name, {"reasoning_content": content})
                        streamed_reasoning = True
                        emitted_payload = True
                    elif phase == "tool_call" and content:
                        tc_id, state, args_delta = _merge_native_tool_delta(native_tc_chunks, evt)
                        tool_chunks = _build_oai_native_tool_chunks(completion_id, created, model_name, tool_indexes, started_tools, tc_id, state, args_delta)
                        if tool_chunks and not sent_role:
                            yield _oai_chunk_payload(completion_id, created, model_name, {"role": "assistant"})
                            sent_role = True
                        for chunk in tool_chunks:
                            yield chunk
                            emitted_payload = True
                    elif phase == "answer" and content:
                        answer_text += content
                    if evt.get("status") == "finished" and phase == "answer":
                        break

                log.info(
                    f"[OAI-诊断] 流式轮次={stream_attempt+1}/{settings.MAX_RETRIES} answer_len={len(answer_text)} reasoning_len={len(reasoning_text)} "
                    f"native_tc_count={len(native_tc_chunks)} streamed_tool_count={len(started_tools)}"
                )
                tool_blocks, stop = build_tool_blocks_from_native_chunks(native_tc_chunks, tools)
                if not tool_blocks or stop != "tool_use":
                    tool_blocks, stop = parse_tool_calls(answer_text, tools)
                has_tool_call = stop == "tool_use"
                blocked_names = _extract_blocked_tool_names(answer_text.strip())

                if blocked_names and not has_tool_call and not emitted_payload and stream_attempt < max_attempts - 1:
                    blocked_name = blocked_names[0]
                    if acc is not None:
                        await _release_stream_account(client, acc, chat_id)
                        excluded_accounts.add(acc.email)
                        acc = None
                        chat_id = None
                    log.warning(f"[NativeBlock-Stream] Qwen拦截原生工具调用 '{blocked_name}'，注入格式纠正后重试 (attempt {stream_attempt+1}/{settings.MAX_RETRIES})")
                    current_prompt = inject_format_reminder(current_prompt, blocked_name)
                    await aio.sleep(0.15)
                    continue

                first_tool = next((b for b in tool_blocks if b.get("type") == "tool_use"), None) if has_tool_call else None
                if first_tool and not emitted_payload:
                    blocked_tool_call, blocked_reason = should_block_tool_call(history_messages, first_tool.get("name", ""), first_tool.get("input", {}))
                    if blocked_tool_call and stream_attempt < max_attempts - 1:
                        if acc is not None:
                            await _release_stream_account(client, acc, chat_id)
                            acc = None
                            chat_id = None
                        current_prompt = current_prompt.rstrip()
                        force_text = f"[MANDATORY NEXT STEP]: {blocked_reason}. Do NOT call the same tool with the same arguments again. Choose another tool or provide final answer."
                        current_prompt = current_prompt[:-len("Assistant:")] + force_text + "\nAssistant:" if current_prompt.endswith("Assistant:") else current_prompt + "\n\n" + force_text + "\nAssistant:"
                        log.warning(f"[ToolLoop-OAI] 阻止重复工具调用：tool={first_tool.get('name')} reason={blocked_reason} (attempt {stream_attempt+1}/{max_attempts})")
                        await aio.sleep(0.15)
                        continue
                    if first_tool.get("name") == "Read" and _has_recent_unchanged_read_result(history_messages) and stream_attempt < max_attempts - 1:
                        if acc is not None:
                            await _release_stream_account(client, acc, chat_id)
                            acc = None
                            chat_id = None
                        current_prompt = current_prompt.rstrip()
                        force_text = "[MANDATORY NEXT STEP]: You just received 'Unchanged since last read'. Do NOT call Read again on the same target. Choose another tool now."
                        current_prompt = current_prompt[:-len("Assistant:")] + force_text + "\nAssistant:" if current_prompt.endswith("Assistant:") else current_prompt + "\n\n" + force_text + "\nAssistant:"
                        log.warning(f"[ToolLoop-OAI] 检测到 Unchanged since last read，立即阻止重复 Read (attempt {stream_attempt+1}/{settings.MAX_RETRIES})")
                        await aio.sleep(0.15)
                        continue

                if not sent_role:
                    yield _oai_chunk_payload(completion_id, created, model_name, {"role": "assistant"})
                    sent_role = True
                if has_tool_call and not started_tools:
                    for chunk in _build_oai_tool_use_chunks(completion_id, created, model_name, tool_blocks):
                        yield chunk
                if not has_tool_call and answer_text:
                    yield _oai_chunk_payload(completion_id, created, model_name, {"content": answer_text})
                if not has_tool_call and reasoning_text and not streamed_reasoning:
                    yield _oai_chunk_payload(completion_id, created, model_name, {"reasoning_content": reasoning_text})
                yield _oai_chunk_payload(completion_id, created, model_name, {}, "tool_calls" if has_tool_call else "stop")
                yield "data: [DONE]\n\n"

                users = await users_db.get()
                for u in users:
                    if u["id"] == token:
                        u["used_tokens"] += len(answer_text) + len(prompt)
                        break
                await users_db.save(users)
                await _release_stream_account(client, acc, chat_id)
                return  # success — exit the retry loop
              except HTTPException as he:
                yield _oai_error_chunk(he.detail)
                return
              except Exception as e:
                if acc and acc.inflight > 0:
                    client.account_pool.release(acc)
                    if chat_id:
                        import asyncio
                        aio.create_task(client.delete_chat(acc.token, chat_id))
                yield _oai_error_chunk(str(e))
                return

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    else:
        current_prompt = prompt
        excluded_accounts = set()
        max_attempts = settings.TOOL_MAX_RETRIES if tools else settings.MAX_RETRIES
        acc: Optional[Account] = None
        chat_id: Optional[str] = None
        for stream_attempt in range(max_attempts):
            try:
                events = []
                chat_id = None
                acc = None
                
                async for item in client.chat_stream_events_with_retry(qwen_model, current_prompt, has_custom_tools=bool(tools), exclude_accounts=excluded_accounts):
                    if item["type"] == "meta":
                        chat_id = item["chat_id"]
                        acc = item["acc"]
                        continue
                    if item["type"] == "event":
                        events.append(item["event"])

                answer_text = ""
                reasoning_text = ""
                native_tc_chunks: dict = {}
                for evt in events:
                    if evt["type"] != "delta":
                        continue
                    phase = evt.get("phase", "")
                    content = evt.get("content", "")
                    if phase in ("think", "thinking_summary") and content:
                        reasoning_text += content
                    elif phase == "answer" and content:
                        answer_text += content
                    elif phase == "tool_call" and content:
                        tc_id = evt.get("extra", {}).get("tool_call_id", "tc_0")
                        if tc_id not in native_tc_chunks:
                            native_tc_chunks[tc_id] = {"name": "", "args": ""}
                        try:
                            chunk = json.loads(content)
                            if "name" in chunk:
                                native_tc_chunks[tc_id]["name"] = chunk["name"]
                            if "arguments" in chunk:
                                native_tc_chunks[tc_id]["args"] += chunk["arguments"]
                        except (json.JSONDecodeError, ValueError):
                            native_tc_chunks[tc_id]["args"] += content
                    if evt.get("status") == "finished" and phase == "answer":
                        break
                        
                if native_tc_chunks and not answer_text:
                    tc_parts = []
                    for tc_id, tc in native_tc_chunks.items():
                        name = tc["name"]
                        try:
                            inp = json.loads(tc["args"]) if tc["args"] else {}
                        except (json.JSONDecodeError, ValueError):
                            inp = {"raw": tc["args"]}
                        tc_parts.append(f'<tool_call>{{"name": {json.dumps(name)}, "input": {json.dumps(inp, ensure_ascii=False)}}}</tool_call>')
                    answer_text = "\n".join(tc_parts)

                blocked_names = _extract_blocked_tool_names(answer_text.strip())
                if blocked_names and tools and stream_attempt < max_attempts - 1:
                    blocked_name = blocked_names[0]
                    if acc:
                        client.account_pool.release(acc)
                        if chat_id:
                            aio.create_task(client.delete_chat(acc.token, chat_id))
                    current_prompt = inject_format_reminder(current_prompt, blocked_name)
                    await aio.sleep(0.15)
                    continue

                tool_blocks, stop = parse_tool_calls(answer_text, tools)
                has_tool_call = stop == "tool_use"
                if has_tool_call:
                    first_tool = next((b for b in tool_blocks if b.get("type") == "tool_use"), None)
                    if first_tool:
                        blocked_tool_call, blocked_reason = should_block_tool_call(history_messages, first_tool.get("name", ""), first_tool.get("input", {}))
                        if blocked_tool_call and stream_attempt < max_attempts - 1:
                            if acc:
                                client.account_pool.release(acc)
                                if chat_id:
                                    aio.create_task(client.delete_chat(acc.token, chat_id))
                            current_prompt = current_prompt.rstrip()
                            force_text = (
                                f"[MANDATORY NEXT STEP]: {blocked_reason}. "
                                f"Do NOT call the same tool with the same arguments again. "
                                f"Choose another tool or provide final answer."
                            )
                            if current_prompt.endswith("Assistant:"):
                                current_prompt = current_prompt[:-len("Assistant:")] + force_text + "\nAssistant:"
                            else:
                                current_prompt += "\n\n" + force_text + "\nAssistant:"
                            log.warning(f"[ToolLoop-OAI] 阻止重复工具调用：tool={first_tool.get('name')} reason={blocked_reason} (attempt {stream_attempt+1}/{max_attempts})")
                            await aio.sleep(0.15)
                            continue
                    if (first_tool and first_tool.get("name") == "Read"
                            and _has_recent_unchanged_read_result(history_messages)
                            and stream_attempt < max_attempts - 1):
                        if acc:
                            client.account_pool.release(acc)
                            if chat_id:
                                aio.create_task(client.delete_chat(acc.token, chat_id))
                        current_prompt = current_prompt.rstrip()
                        force_text = (
                            "[MANDATORY NEXT STEP]: You just received 'Unchanged since last read'. "
                            "Do NOT call Read again on the same target. "
                            "Choose another tool now."
                        )
                        if current_prompt.endswith("Assistant:"):
                            current_prompt = current_prompt[:-len("Assistant:")] + force_text + "\nAssistant:"
                        else:
                            current_prompt += "\n\n" + force_text + "\nAssistant:"
                        log.warning(f"[ToolLoop-OAI] 检测到 Unchanged since last read，立即阻止重复 Read (attempt {stream_attempt+1}/{settings.MAX_RETRIES})")
                        await aio.sleep(0.15)
                        continue

                if has_tool_call:
                    tc_list = [b for b in tool_blocks if b["type"] == "tool_use"]
                    oai_tool_calls = [{
                        "id": tc["id"], "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc.get("input", {}), ensure_ascii=False)
                        }
                    } for tc in tc_list]
                    msg = {"role": "assistant", "content": None, "tool_calls": oai_tool_calls}
                    finish_reason = "tool_calls"
                else:
                    msg = {"role": "assistant", "content": answer_text}
                    if reasoning_text:
                        msg["reasoning_content"] = reasoning_text
                    finish_reason = "stop"

                users = await users_db.get()
                for u in users:
                    if u["id"] == token:
                        u["used_tokens"] += len(answer_text) + len(prompt)
                        break
                await users_db.save(users)

                if acc:
                    client.account_pool.release(acc)
                    if chat_id:
                        import asyncio
                        aio.create_task(client.delete_chat(acc.token, chat_id))

                from fastapi.responses import JSONResponse
                return JSONResponse({
                    "id": completion_id, "object": "chat.completion", "created": created, "model": model_name,
                    "choices": [{"index": 0, "message": msg, "finish_reason": finish_reason}],
                    "usage": {"prompt_tokens": len(prompt), "completion_tokens": len(answer_text),
                              "total_tokens": len(prompt) + len(answer_text)}
                })
            except Exception as e:
                if acc and acc.inflight > 0:
                    client.account_pool.release(acc)
                    if chat_id:
                        import asyncio
                        aio.create_task(client.delete_chat(acc.token, chat_id))
                if stream_attempt == settings.MAX_RETRIES - 1:
                    raise HTTPException(status_code=503, detail=_oai_error_payload(str(e))["error"])
                await aio.sleep(1)
