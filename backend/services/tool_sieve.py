import json
import re

class ToolSieve:
    """
    工具调用防泄漏与拦截解析器。
    用于解析 Qwen 流式返回中的 tool_call 事件，或识别模型由于 Prompt 劫持吐出的文本格式工具调用。
    """
    def __init__(self):
        self.buffer = ""
        self.in_tool_call = False

    def process_delta(self, text: str) -> tuple[str, list[dict]]:
        """
        接收增量文本，返回 (安全文本, 工具调用列表)
        """
        if not text:
            return "", []

        self.buffer += text
        emitted_text = ""
        tool_calls = []

        # 简单匹配 ##TOOL_CALL## 块
        # 如果还在 buffer 里匹配到，提取出来作为工具调用
        while "##TOOL_CALL##" in self.buffer:
            start_idx = self.buffer.find("##TOOL_CALL##")
            end_idx = self.buffer.find("##END_CALL##", start_idx)
            
            if end_idx != -1:
                # 提取完整的 tool call
                tc_block = self.buffer[start_idx + len("##TOOL_CALL##"):end_idx].strip()
                try:
                    tc_json = json.loads(tc_block)
                    tool_calls.append(tc_json)
                except Exception:
                    pass # 解析失败忽略
                
                # 发送之前的内容
                emitted_text += self.buffer[:start_idx]
                self.buffer = self.buffer[end_idx + len("##END_CALL##"):]
            else:
                # 不完整，等下一个 chunk
                break
                
        # 如果 buffer 里没有 tool call 的前缀，说明安全，直接刷出
        if "##TOOL_CALL" not in self.buffer:
            emitted_text += self.buffer
            self.buffer = ""

        return emitted_text, tool_calls

    def flush(self) -> tuple[str, list[dict]]:
        """
        流结束时，将缓冲区的剩余文本强制刷出。
        返回 (剩余安全文本, 解析出的最后工具调用)
        """
        emitted_text = self.buffer
        self.buffer = ""
        return emitted_text, []
