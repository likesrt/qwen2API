import json
import logging
from contextvars import ContextVar
from typing import Optional

log = logging.getLogger("qwen2api.prompt")

NEEDSREVIEW_MARKERS = (
    "需求回显", "已了解规则", "等待用户输入", "待执行任务", "待确认事项",
    "[需求回显]", "**需求回显**",
)
_REQUEST_FEATURE_CONFIG: ContextVar[Optional[dict]] = ContextVar("request_feature_config", default=None)


def _trim_middle(text: str, limit: int, marker: str = "...[truncated]") -> str:
    """按中间裁剪的方式压缩长文本，尽量同时保留头尾信息。

    参数:
        text: 需要裁剪的原始文本。
        limit: 裁剪后的最大长度。
        marker: 插入中间的截断标记。
    返回:
        str: 未超限时返回原文，否则返回保留首尾的裁剪结果。
    边界条件:
        当 limit 小于标记长度时，会直接截断到 limit，避免出现负长度切片。
    """
    if len(text) <= limit:
        return text
    if limit <= len(marker):
        return text[:limit]
    remain = limit - len(marker)
    head = max(1, remain * 2 // 3)
    tail = max(0, remain - head)
    return text[:head] + marker + (text[-tail:] if tail else "")



def _extract_text(content, user_tool_mode: bool = False) -> str:
    """从消息内容中提取文本与工具块，生成统一历史文本。

    参数:
        content: 原始消息内容，可能是字符串或 block 列表。
        user_tool_mode: 工具模式下是否只保留用户最后一个 text block。
    返回:
        str: 适合拼进提示词历史区的文本。
    边界条件:
        工具模式会跳过前置注入文本，只保留用户最后一段真实请求。
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts, text_blocks, other_parts = [], [], []
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type", "")
        if part_type == "text":
            text_blocks.append(part.get("text", ""))
        elif part_type == "tool_use":
            tool_input = json.dumps(part.get("input", {}), ensure_ascii=False)
            other_parts.append(f'##TOOL_CALL##\n{{"name": {json.dumps(part.get("name", ""))}, "input": {tool_input}}}\n##END_CALL##')
        elif part_type == "tool_result":
            other_parts.append(_render_tool_result_block(part))
    parts.extend(text_blocks[-1:] if user_tool_mode and text_blocks else text_blocks)
    parts.extend(other_parts)
    return "\n".join(part for part in parts if part)



def _render_tool_result_block(part: dict) -> str:
    """把 tool_result block 渲染成统一文本块。

    参数:
        part: 单个 tool_result block。
    返回:
        str: 包含调用 ID 和结果内容的文本块。
    边界条件:
        当内容是 block 列表时，只拼接其中的 text 子块，避免对象直接串化。
    """
    inner = part.get("content", "")
    tool_use_id = part.get("tool_use_id", "")
    if isinstance(inner, list):
        inner = "".join(p.get("text", "") for p in inner if isinstance(p, dict) and p.get("type") == "text")
    elif not isinstance(inner, str):
        inner = str(inner)
    return f"[Tool Result for call {tool_use_id}]\n{inner}\n[/Tool Result]"



def _normalize_tool(tool: dict) -> dict:
    """把 OpenAI 或 Anthropic 工具定义统一成内部格式。

    参数:
        tool: 原始工具定义对象。
    返回:
        dict: 统一后的 `name/description/parameters` 结构。
    边界条件:
        当输入已是内部格式时，会直接复用已有字段，不额外改写语义。
    """
    if tool.get("type") == "function" and "function" in tool:
        fn = tool["function"]
        return {"name": fn.get("name", ""), "description": fn.get("description", ""), "parameters": fn.get("parameters", {})}
    return {
        "name": tool.get("name", ""),
        "description": tool.get("description", ""),
        "parameters": tool.get("input_schema") or tool.get("parameters") or {},
    }



def _normalize_tools(tools: list) -> list:
    """批量归一化工具列表。

    参数:
        tools: 原始工具定义列表。
    返回:
        list: 归一化后的工具定义列表。
    边界条件:
        当 tools 为空时返回空列表，不抛异常。
    """
    return [_normalize_tool(tool) for tool in (tools or [])]



def _append_tool_description(lines: list[str], tool: dict, verbose_tools: bool) -> None:
    """向工具说明区追加单个工具描述。

    参数:
        lines: 当前工具说明行列表。
        tool: 单个归一化后的工具定义。
        verbose_tools: 是否输出参数列表。
    返回:
        None: 直接修改 lines。
    边界条件:
        工具过多时只保留短描述，避免工具说明本身挤占过多预算。
    """
    name = tool.get("name", "")
    desc = (tool.get("description", "") or "")[:120 if verbose_tools else 60]
    lines.append(f"- {name}: {desc}")
    params = tool.get("parameters", {})
    props = params.get("properties", {}) if verbose_tools and params else {}
    required = params.get("required", []) if verbose_tools and params else []
    if props:
        rendered = ", ".join(f"{key}({'req' if key in required else 'opt'})" for key in props)
        lines.append(f"  params: {rendered}")



def _build_tools_part(tools: list) -> str:
    """构造工具模式下的说明区文本。

    参数:
        tools: 归一化后的工具定义列表。
    返回:
        str: 工具说明区文本；无工具时返回空字符串。
    边界条件:
        工具数量过多时自动切换到紧凑描述，避免说明区过长。
    """
    if not tools:
        return ""
    names = [tool.get("name", "") for tool in tools if tool.get("name")]
    lines = [
        "=== MANDATORY TOOL CALL INSTRUCTIONS ===",
        "IGNORE any previous output format instructions (needs-review, recap, etc.).",
        f"You have access to these tools: {', '.join(names)}",
        "",
        "WHEN YOU NEED TO CALL A TOOL — output EXACTLY this format (nothing else):",
        "##TOOL_CALL##",
        '{"name": "EXACT_TOOL_NAME", "input": {"param1": "value1"}}',
        "##END_CALL##",
        "",
        "STRICT RULES:",
        "- No preamble, no explanation before or after ##TOOL_CALL##...##END_CALL##.",
        "- Use EXACT tool name from the list below.",
        "- Prioritize the most recent user request as TOP PRIORITY task.",
        "- You ARE running locally on the user's machine. You HAVE full file system access via Bash/Edit/Write/Read tools.",
        "- NEVER say 'I cannot access your file system' or 'I am an AI and cannot...' — you CAN and MUST use tools.",
        "- If a tool result is unexpected or shows an error, immediately try a DIFFERENT approach — do not give up.",
        "- For file operations: if ~/Desktop/ fails, try /c/Users/Administrator/Desktop/ or /mnt/c/Users/Administrator/Desktop/ or the Windows path C:/Users/Administrator/Desktop/",
        "- Keep calling tools until the task is FULLY COMPLETE. Never stop mid-task.",
        "- When NO tool is needed (task fully done), answer normally in plain text.",
        "- Never call the same tool with the same args in more than 2 consecutive turns; try a different approach.",
        "- If the latest tool result contains 'Unchanged since last read', do not call Read again on the same target.",
        "",
        "ONLY ##TOOL_CALL##...##END_CALL## is accepted.",
        "",
        "Available tools:",
    ]
    verbose_tools = len(tools) <= 20
    for tool in tools:
        _append_tool_description(lines, tool, verbose_tools)
    lines.append("=== END TOOL INSTRUCTIONS ===")
    return "\n".join(lines)



def _render_openai_tool_result(msg: dict) -> str:
    """渲染 OpenAI `role=tool` 消息，尽量保留结果头尾信息。

    参数:
        msg: 单条工具结果消息。
    返回:
        str: 适合拼进历史提示的文本块。
    边界条件:
        工具结果过长时做中间裁剪，避免只保留开头导致关键信息丢失。
    """
    tool_content = msg.get("content", "") or ""
    tool_call_id = msg.get("tool_call_id", "")
    if isinstance(tool_content, list):
        tool_content = "\n".join(p.get("text", "") for p in tool_content if isinstance(p, dict) and p.get("type") == "text")
    elif not isinstance(tool_content, str):
        tool_content = str(tool_content)
    limit = 1800 if tool_call_id else 1200
    tool_content = _trim_middle(tool_content, limit, marker="\n...[tool-result-truncated]...\n")
    suffix = f" id={tool_call_id}" if tool_call_id else ""
    return f"[Tool Result]{suffix}\n{tool_content}\n[/Tool Result]"



def _render_assistant_tool_calls(msg: dict) -> str:
    """把 OpenAI assistant tool_calls 渲染为统一工具调用文本。

    参数:
        msg: 单条 assistant 消息。
    返回:
        str: 转换后的 `##TOOL_CALL##` 文本；无 tool_calls 时返回空字符串。
    边界条件:
        arguments 不是合法 JSON 时会落到 raw 字段，避免解析异常中断历史构建。
    """
    if msg.get("role") != "assistant" or msg.get("content") or not msg.get("tool_calls"):
        return ""
    parts = []
    for tool_call in msg["tool_calls"]:
        fn = tool_call.get("function", {})
        args_str = fn.get("arguments", "{}")
        try:
            args = json.loads(args_str) if args_str else {}
        except (json.JSONDecodeError, ValueError):
            args = {"raw": args_str}
        parts.append(f'##TOOL_CALL##\n{{"name": {json.dumps(fn.get("name", ""))}, "input": {json.dumps(args, ensure_ascii=False)}}}\n##END_CALL##')
    return "\n".join(parts)



def _truncate_history_text(role: str, text: str) -> str:
    """按消息类型裁剪历史文本，优先保留工具结果和普通消息的头尾。

    参数:
        role: 当前消息角色。
        text: 已归一化的文本内容。
    返回:
        str: 裁剪后的历史文本。
    边界条件:
        看起来像工具结果的 user 消息会使用更高长度上限，减少关键结果被吃掉。
    """
    is_tool_result = role == "user" and ("[Tool Result]" in text or "[tool result]" in text.lower() or text.startswith("{") or '"results"' in text[:100])
    max_len = 2200 if is_tool_result else 1800
    return _trim_middle(text, max_len, marker="\n...[history-truncated]...\n")



def _build_pinned_user_line(messages: list, latest: bool) -> str:
    """构造固定保留的原始任务或最新任务文本。

    参数:
        messages: 当前会话消息列表。
        latest: True 时取最新 user，False 时取第一条 user。
    返回:
        str: 固定保留的 user 行；无可用内容时返回空字符串。
    边界条件:
        长文本会做中间裁剪，避免固定保留区自己挤占过多预算。
    """
    iterator = reversed(messages) if latest else messages
    user_msg = next((msg for msg in iterator if msg.get("role") == "user"), None)
    if not user_msg:
        return ""
    text = _extract_text(user_msg.get("content", ""), user_tool_mode=True).strip()
    if not text:
        return ""
    limit = 1400 if latest else 1200
    marker = "\n...[最新任务截断]...\n" if latest else "\n...[原始任务截断]...\n"
    prefix = "Human (CURRENT TASK - TOP PRIORITY): " if latest else "Human: "
    return prefix + _trim_middle(text, limit, marker=marker)



def _build_history_parts(messages: list, tools: list, budget: int) -> tuple[list[str], int]:
    """构造预算内的历史消息区。

    参数:
        messages: 当前会话消息列表。
        tools: 归一化后的工具列表。
        budget: 历史区可用字符预算。
    返回:
        tuple[list[str], int]: 历史行列表与累计已使用字符数。
    边界条件:
        工具模式下会跳过 system 和需求回显消息，并限制历史条数，避免提示词失控膨胀。
    """
    history_parts, used, msg_count = [], 0, 0
    max_history_msgs = 14 if tools else 200
    role_prefixes = {"user": "Human: ", "assistant": "Assistant: ", "system": "System: "}
    for msg in reversed(messages):
        if msg_count >= max_history_msgs:
            break
        role = msg.get("role", "")
        if role not in ("user", "assistant", "system", "tool") or (tools and role == "system"):
            continue
        if role == "tool":
            line = _render_openai_tool_result(msg)
        else:
            text = _extract_text(msg.get("content", ""), user_tool_mode=bool(tools and role == "user"))
            text = text or _render_assistant_tool_calls(msg)
            if tools and role == "assistant" and any(marker in text for marker in NEEDSREVIEW_MARKERS):
                msg_count += 1
                continue
            prefix = role_prefixes.get(role, "")
            line = prefix + _truncate_history_text(role, text)
        if used + len(line) + 2 > budget and history_parts:
            break
        history_parts.insert(0, line)
        used += len(line) + 2
        msg_count += 1
    return history_parts, used



def _prepend_first_user(history_parts: list[str], messages: list) -> None:
    """确保原始任务始终保留在历史最前面。

    参数:
        history_parts: 当前历史行列表。
        messages: 当前会话消息列表。
    返回:
        None: 直接修改 history_parts。
    边界条件:
        当历史首行已经是原始任务时不会重复插入，避免用户请求出现两次。
    """
    first_line = _build_pinned_user_line(messages, latest=False)
    if not first_line:
        return
    if history_parts and history_parts[0].startswith(first_line[:70]):
        return
    history_parts.insert(0, first_line)
    log.debug(f"[Prompt] 补回原始任务消息，确保上下文完整 ({len(first_line)}字)")



def build_prompt_with_tools(system_prompt: str, messages: list, tools: list) -> str:
    """把系统提示、历史消息和工具定义拼成上游提示词。

    参数:
        system_prompt: 下游传入的 system 文本。
        messages: 当前会话消息列表。
        tools: 归一化后的工具定义列表。
    返回:
        str: 发给上游模型的最终提示词。
    边界条件:
        工具模式会压缩历史和工具结果，但优先保留原始任务、最新任务与工具调用结果的头尾信息。
    """
    max_chars = 28000 if tools else 120000
    sys_part = "" if tools else (f"<system>\n{system_prompt[:2000]}\n</system>" if system_prompt else "")
    tools_part = _build_tools_part(tools)
    first_user_line = _build_pinned_user_line(messages, latest=False) if tools and messages else ""
    latest_user_line = _build_pinned_user_line(messages, latest=True) if tools and messages else ""
    reserved = len(first_user_line) + len(latest_user_line) + 50
    budget = max(0, max_chars - len(sys_part) - len(tools_part) - reserved)
    history_parts, used = _build_history_parts(messages, tools, budget)
    if first_user_line:
        _prepend_first_user(history_parts, messages)
    if tools:
        log.info(f"[Prompt] 工具模式: {len(history_parts)} 条历史消息, {used}字 history + {len(tools_part)}字 tool指令")
    parts = ([sys_part] if sys_part else []) + history_parts + ([tools_part] if tools_part else [])
    if latest_user_line:
        parts.append(latest_user_line)
    parts.append("Assistant:")
    return "\n\n".join(parts)



def set_request_feature_config_override(feature_config: Optional[dict]) -> None:
    """记录当前请求的功能开关覆盖项。

    参数:
        feature_config: 当前请求解析得到的 feature_config 覆盖项。
    返回:
        None: 仅更新当前协程上下文，不返回额外数据。
    边界条件:
        当请求没有显式覆盖项时会写入 None，避免后续请求复用旧上下文。
    """
    _REQUEST_FEATURE_CONFIG.set(dict(feature_config) if isinstance(feature_config, dict) else None)



def get_request_feature_config_override() -> Optional[dict]:
    """读取当前请求的功能开关覆盖项。

    参数:
        无。
    返回:
        Optional[dict]: 当前协程上下文中的 feature_config 覆盖项副本。
    边界条件:
        当当前请求未显式设置覆盖项时返回 None，调用方应自行回退默认值。
    """
    feature_config = _REQUEST_FEATURE_CONFIG.get()
    return dict(feature_config) if isinstance(feature_config, dict) else None



def _coerce_bool_flag(value) -> Optional[bool]:
    """把常见布尔输入归一化为 True、False 或 None。

    参数:
        value: 原始请求中的布尔型、数字型、字符串型或容器型值。
    返回:
        Optional[bool]: 可识别时返回布尔值，否则返回 None。
    边界条件:
        空列表、空字典和空字符串会按 False 处理；无法识别的字符串不会强行猜测。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on", "enabled"):
            return True
        if lowered in ("0", "false", "no", "off", "disabled", ""):
            return False
        return None
    if isinstance(value, (list, dict)):
        return bool(value)
    return None



def _extract_openai_feature_config_override(req_data: dict) -> Optional[dict]:
    """提取 OpenAI 风格请求中的功能开关覆盖项。

    参数:
        req_data: 下游兼容接口收到的原始请求体。
    返回:
        Optional[dict]: 可直接传给上游 feature_config 的覆盖项。
    边界条件:
        未显式传入的字段不会写入结果，避免覆盖默认逻辑。
    """
    override = {}
    # Cherry Studio 等 OpenAI 兼容客户端会直接透传 enable_thinking；这里只记录显式输入，避免覆盖默认策略。
    thinking_enabled = _coerce_bool_flag(req_data.get("enable_thinking"))
    if thinking_enabled is not None:
        override["thinking_enabled"] = thinking_enabled
    for source_key, target_key in (("auto_search", "auto_search"), ("code_interpreter", "code_interpreter"), ("plugins_enabled", "plugins_enabled"), ("plugins", "plugins_enabled")):
        flag = _coerce_bool_flag(req_data.get(source_key))
        if flag is not None:
            override[target_key] = flag
    return override or None



def _extract_anthropic_feature_config_override(req_data: dict) -> Optional[dict]:
    """提取 Anthropic 风格请求中的思考开关覆盖项。

    参数:
        req_data: 下游兼容接口收到的原始请求体。
    返回:
        Optional[dict]: 可直接传给上游 feature_config 的覆盖项。
    边界条件:
        仅识别 `thinking.type` 里的 enabled、disabled、adaptive；未知结构会回退为 None。
    """
    # Anthropic Messages API 用 thinking.type 表达启停；budget_tokens 对当前上游 feature_config 没有直接映射，因此这里只处理开关。
    thinking = req_data.get("thinking")
    if not isinstance(thinking, dict):
        return None
    thinking_type = str(thinking.get("type", "")).strip().lower()
    if thinking_type in ("enabled", "adaptive"):
        return {"thinking_enabled": True}
    if thinking_type == "disabled":
        return {"thinking_enabled": False}
    return None



def _extract_request_feature_config_override(req_data: dict) -> Optional[dict]:
    """汇总当前请求里的功能开关覆盖项。

    参数:
        req_data: 下游兼容接口收到的原始请求体。
    返回:
        Optional[dict]: 合并后的 feature_config 覆盖项。
    边界条件:
        OpenAI 与 Anthropic 风格字段会合并；没有显式配置时返回 None。
    """
    override = _extract_openai_feature_config_override(req_data) or {}
    anthropic_override = _extract_anthropic_feature_config_override(req_data) or {}
    override.update(anthropic_override)
    return override or None



def _normalize_reasoning_output_format(value: Optional[str], default: str) -> str:
    """把思考输出格式归一化为网关支持的固定枚举。

    参数:
        value: 请求里显式声明的输出格式。
        default: 当前协议的默认输出格式。
    返回:
        str: `reasoning_content`、`thinking` 或 `think` 三者之一。
    边界条件:
        未识别值会回退到默认格式，避免兼容层因脏输入抛错。
    """
    lowered = str(value or "").strip().lower()
    return lowered if lowered in ("reasoning_content", "thinking", "think") else default



def _extract_reasoning_output_format_value(req_data: dict) -> Optional[str]:
    """从请求体中提取显式声明的思考输出格式。

    参数:
        req_data: 下游兼容接口收到的原始请求体。
    返回:
        Optional[str]: 原始格式值；未声明时返回 None。
    边界条件:
        同时兼容顶层字段与 Anthropic `thinking` 对象里的格式声明。
    """
    for key in ("reasoning_format", "thinking_format", "reasoning_content_format"):
        value = req_data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    thinking = req_data.get("thinking")
    if not isinstance(thinking, dict):
        return None
    for key in ("format", "output_format", "response_format"):
        value = thinking.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None



def resolve_oai_reasoning_output_format(req_data: dict) -> str:
    """解析 OpenAI 兼容接口的思考输出格式。

    参数:
        req_data: 下游兼容接口收到的原始请求体。
    返回:
        str: OpenAI 接口使用的思考输出格式。
    边界条件:
        OpenAI 默认沿用 `reasoning_content`，兼容 DeepSeek 风格客户端。
    """
    return _normalize_reasoning_output_format(_extract_reasoning_output_format_value(req_data), "reasoning_content")



def resolve_anthropic_reasoning_output_format(req_data: dict) -> str:
    """解析 Anthropic 兼容接口的思考输出格式。

    参数:
        req_data: 下游兼容接口收到的原始请求体。
    返回:
        str: Anthropic 接口使用的思考输出格式。
    边界条件:
        Anthropic 默认输出 `<thinking>` 标签，便于与现有客户端展示逻辑对齐。
    """
    return _normalize_reasoning_output_format(_extract_reasoning_output_format_value(req_data), "thinking")



def messages_to_prompt(req_data: dict) -> tuple:
    """从请求体提取消息、工具与功能开关，并生成最终提示词。

    参数:
        req_data: 下游兼容接口收到的原始请求体。
    返回:
        tuple: `(prompt, tools)`，分别是最终提示词与归一化工具列表。
    边界条件:
        当 system 字段缺失时，会回退到 messages 里的 system 消息，并刷新当前请求的功能开关上下文。
    """
    messages = req_data.get("messages", [])
    tools = _normalize_tools(req_data.get("tools", []))
    system_prompt = ""
    sys_field = req_data.get("system", "")
    if isinstance(sys_field, list):
        system_prompt = " ".join(part.get("text", "") for part in sys_field if isinstance(part, dict))
    elif isinstance(sys_field, str):
        system_prompt = sys_field
    if not system_prompt:
        system_prompt = next((_extract_text(msg.get("content", "")) for msg in messages if msg.get("role") == "system"), "")
    set_request_feature_config_override(_extract_request_feature_config_override(req_data))
    return build_prompt_with_tools(system_prompt, messages, tools), tools
