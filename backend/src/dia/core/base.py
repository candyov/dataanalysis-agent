"""共享模块 -- LLM 实例、安全解析工具、BaseAgent"""

import ast
import asyncio
import json
import logging
from typing import Any

from langchain_openai import ChatOpenAI
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatResult, ChatGeneration
from langgraph.config import get_stream_writer
from dia.core.config import settings
from dia.infrastructure.observability.callbacks import get_current_tracker

logger = logging.getLogger(__name__)


class DeepSeekChatOpenAI(ChatOpenAI):
    """DeepSeek 专用 ChatOpenAI 子类，捕获 reasoning_content 字段。

    DeepSeek V4 Pro 在 thinking mode 下会在响应中返回 reasoning_content，
    且要求后续请求中必须携带该字段。LangChain 默认不捕获此非标准字段，
    导致消息链断裂报 400 错误。
    """

    def _create_chat_result(self, response: Any, generation_info: dict | None = None) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)
        # 从原始响应中提取 reasoning_content (DeepSeek V4 Pro thinking mode)
        try:
            choices = response.choices if hasattr(response, "choices") else response.get("choices", [])
            for i, choice in enumerate(choices):
                msg = choice.message if hasattr(choice, "message") else choice.get("message", {})
                rc = msg.reasoning_content if hasattr(msg, "reasoning_content") else msg.get("reasoning_content")
                if rc and i < len(result.generations):
                    gen = result.generations[i]
                    if isinstance(gen, ChatGeneration):
                        gen.message.additional_kwargs["reasoning_content"] = rc
                        logger.debug(f"[DeepSeek] captured reasoning_content ({len(rc)} chars) for generation {i}")
                elif i < len(result.generations):
                    logger.debug(f"[DeepSeek] no reasoning_content in response choice {i}")
        except Exception as e:
            logger.debug(f"[DeepSeek] _create_chat_result error: {e}")
        return result

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        """重写以在消息中携带 reasoning_content。"""
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        # 在 payload["messages"] 中注入 reasoning_content
        messages = self._convert_input(input_).to_messages()
        if "messages" in payload:
            for i, m in enumerate(messages):
                if i < len(payload["messages"]):
                    rc = m.additional_kwargs.get("reasoning_content") if isinstance(m, AIMessage) else None
                    if rc:
                        payload["messages"][i]["reasoning_content"] = rc
        # DEBUG: 打印消息角色
        roles = [m.get("role", "?") for m in payload.get("messages", [])]
        has_rc = any("reasoning_content" in m for m in payload.get("messages", []))
        logger.debug(f"[DeepSeek] payload messages: {roles}, has_rc: {has_rc}")
        return payload


def _safe_parse_content(content: Any) -> Any:
    """安全解析 ToolMessage.content:支持 JSON 字符串、Python repr 字符串、已解析的 dict/list.

    问题背景:LangChain ToolNode 对 dict 返回值调用 str(),产生 Python repr
    (单引号),json.loads() 会失败.此函数用 ast.literal_eval 兜底.
    """
    if isinstance(content, (dict, list)):
        return content
    if not isinstance(content, str):
        return content
    try:
        return json.loads(content)
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        return ast.literal_eval(content)
    except (ValueError, SyntaxError):
        pass
    return content


# ── 温度分档 LLM 实例缓存 ──
_llm_instances: dict[float, ChatOpenAI] = {}
_llm_lock = asyncio.Lock()
_llm_config_sig: tuple = ()  # 配置签名: 变化 → 缓存失效 (热更新)


def build_llm(profile: dict | None = None, temperature: float | None = None) -> ChatOpenAI:
    """按模型档案构建 LLM 实例 (deepseek → 专用类捕获 reasoning_content, 其他 → 标准 ChatOpenAI).

    profile: 模型档案 dict {provider, base_url, model, api_key, temperature}
    """
    if profile:
        provider = profile.get("provider", "custom")
        model = profile.get("model", "")
        api_key = profile.get("api_key", "")
        base_url = profile.get("base_url", "")
        temp = float(profile.get("temperature", temperature if temperature is not None else settings.LLM_DEFAULT_TEMPERATURE))
        if not api_key:
            logger.warning(f"[LLM] 档案 {profile.get('name','?')} 未配置 API Key")
        cls = DeepSeekChatOpenAI if provider == "deepseek" else ChatOpenAI
        return cls(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=temp,
            streaming=False,  # 禁用 streaming, 确保 reasoning_content 被正确捕获
        )
    # 无档案 → 回退旧配置链: 动态 LLM_* > env
    from dia.infrastructure.config_store import get as _dyn_get
    model = _dyn_get("LLM_MODEL", settings.LLM_MODEL)
    api_key = _dyn_get("LLM_API_KEY", settings.LLM_API_KEY)
    base_url = _dyn_get("LLM_API_BASE", settings.LLM_API_BASE)
    if not api_key:
        logger.warning("LLM_API_KEY 未配置,LLM 调用将失败")
    return DeepSeekChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature if temperature is not None else settings.LLM_DEFAULT_TEMPERATURE,
        streaming=False,
    )


async def get_llm(temperature: float | None = None) -> ChatOpenAI:
    """获取 LLM 实例,按温度分档缓存.

    模型切换: 激活档案 (设置面板切换) 优先, 配置变更自动重建实例, 无需重启.

    Args:
        temperature: 0.1=意图分类, 0.2=工具调用, 0.3=可视化生成
    """
    global _llm_instances, _llm_config_sig
    if temperature is None:
        temperature = settings.LLM_DEFAULT_TEMPERATURE
    # 激活档案 (多模型切换) > 动态 LLM_* > env
    from dia.api.v1.models import get_active_profile
    profile = get_active_profile()
    if profile:
        model = profile.get("model", "")
        api_key = profile.get("api_key", "")
        base_url = profile.get("base_url", "")
    else:
        from dia.infrastructure.config_store import get as _dyn_get
        model = _dyn_get("LLM_MODEL", settings.LLM_MODEL)
        api_key = _dyn_get("LLM_API_KEY", settings.LLM_API_KEY)
        base_url = _dyn_get("LLM_API_BASE", settings.LLM_API_BASE)
    sig = (model, api_key, base_url)
    if sig != _llm_config_sig:
        _llm_instances.clear()
        _llm_config_sig = sig
    temp_key = round(temperature, 1)
    if temp_key not in _llm_instances:
        async with _llm_lock:
            if temp_key not in _llm_instances:
                _llm_instances[temp_key] = build_llm(profile=profile, temperature=temperature)
    return _llm_instances[temp_key]


# ═══════════════════════════════════════════════
# BaseAgent -- 统一 Agent 框架
# ═══════════════════════════════════════════════

class BaseAgent:
    """Agent 基类.子类只需实现 extract_input + build_output + build_graph.

    自动处理:
    - 子图编译缓存 (get_graph)
    - Stream writer 获取
    - 重试逻辑 (max_retries)
    - Token 追踪回调
    - 思维流推送
    - 降级输出
    """

    max_retries: int = settings.LLM_MAX_RETRIES

    def __init__(self, name: str):
        self.name = name
        self._graph = None

    # ── 子类必须实现 ──

    def build_graph(self):
        raise NotImplementedError

    def extract_input(self, state: dict) -> dict:
        """从 MultiAgentState 提取子图需要的字段"""
        raise NotImplementedError

    def build_output(self, state: dict, result: dict) -> dict:
        """将子图执行结果翻译为 MultiAgentState 可 merge 的 dict"""
        raise NotImplementedError

    # ── 框架方法(子类无需关心)──

    def get_graph(self):
        if self._graph is None:
            self._graph = self.build_graph()
        return self._graph

    async def run(self, state: dict, config=None) -> dict:
        """Agent 入口函数 -- 替代原来的 wrapper_node"""
        graph = self.get_graph()

        # 1. 获取 stream writer
        try:
            writer = get_stream_writer()
        except Exception as e:
            logger.warning(f"[{self.name}] get_stream_writer failed: {e}")
            writer = lambda _: None  # type: ignore[no-untyped-call]

        # 2. 提取输入
        inner = self.extract_input(state)
        # 支持异步 extract_input
        if asyncio.iscoroutine(inner):
            inner = await inner
        if "messages" not in inner and state.get("messages"):
            inner["messages"] = list(state["messages"])

        # 3. 回调链 — 子图自动继承父图 config 的 callbacks (langgraph 传播),
        #    不需要手动合并: 合并会把 AsyncCallbackManager 当 handler 嵌套,
        #    触发 'AsyncCallbackManager' object has no attribute 'run_inline'
        tracker = get_current_tracker()
        if tracker:
            tracker.set_agent(self.name)

        # 4. 执行 + 重试
        result = None
        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                result = await graph.ainvoke(inner, config=config)
                break
            except Exception as e:
                last_error = e
                if attempt < self.max_retries:
                    delay = 2 * (attempt + 1)
                    logger.warning(f"[{self.name}] attempt {attempt+1} failed: {e}, retry in {delay}s")
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"[{self.name}] failed after {self.max_retries} retries: {e}")

        if result is None:
            # 失败分类: LLM/图执行异常 → 区分超时类 vs 工具类 (supervisor 据此决定重试策略)
            err_str = str(last_error or "")
            err_lower = err_str.lower()
            if any(k in err_lower for k in ("timeout", "timed out", "deadline", "asyncio.timeout")):
                error_type = "llm_timeout"
            elif any(k in err_lower for k in ("apikey", "api key", "401", "403", "authentication", "unauthorized")):
                error_type = "llm_auth"
            elif any(k in err_lower for k in ("syntax error", "no such column", "no such table",
                                               "sqlite3.", "mysql", "operationalerror", "pymysql")):
                error_type = "tool_sql"
            else:
                error_type = "llm_generic"
            logger.error(f"[{self.name}] failed after {self.max_retries} retries: {last_error} (type={error_type})")
            return {
                "messages": [AIMessage(content=f"{self.name} 处理失败 ({last_error}),请重试.")],
                "error_type": error_type,
                "error_message": err_str[:500],
            }

        # 5. 推流思考过程
        self._stream(result, writer)

        # 6. 推流工具调用/图表/结果事件 (custom 模式 → chat.py 转发 SSE)
        # 父图 updates 模式只暴露 wrapper 返回值, 子图内部消息链不外泄 —
        # 必须在这里从子图全量消息提取, 否则 ToolCallEvent/ChartEvent 永不发
        self._stream_events(result, writer)

        # 7. 回写产出
        return self.build_output(state, result)

    def _stream(self, result: dict, writer) -> None:
        """推送子图的思考过程/流式文本（每轮最多一次 thinking + 真正流式最后一条）"""
        msgs = result.get("messages", [])
        for msg in msgs:
            if isinstance(msg, AIMessage):
                text = msg.additional_kwargs.get("_thinking", "")
                if text:
                    if msg.tool_calls:
                        # 工具调用 → 整段推送为一条 thinking (不逐字)
                        writer({"type": "thinking", "text": text})
                    else:
                        # 最终文本 → 流式推送最后一条
                        if msg is msgs[-1]:
                            for token in text:
                                writer({"type": "stream", "text": token})
                        else:
                            writer({"type": "thinking", "text": text})

    def _stream_events(self, result: dict, writer) -> None:
        """从子图全量消息推送工具调用/图表/结果事件 (custom 模式).

        父图 stream_mode=updates 只暴露 wrapper 返回值 (build_output 已瘦身
        messages), 子图内部带 tool_calls 的 AIMessage 与 ToolMessage 链不会
        冒泡 — 事件必须在这里提取, 由 chat.py 的 custom 分支转发 SSE.
        """
        import json as _json
        msgs = result.get("messages", [])
        for msg in msgs:
            if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
                for tc in msg.tool_calls:
                    writer({"type": "tool_call", "tool": tc.get("name", ""), "agent": self.name})
            elif isinstance(msg, ToolMessage):
                tool_name = getattr(msg, "name", "") or ""
                if tool_name == "build_chart":
                    # 图表数据: 解析 echarts_option, 复用 chat.py 的标题提取规则
                    try:
                        chart_data = _safe_parse_content(msg.content)
                    except Exception:
                        chart_data = None
                    if isinstance(chart_data, dict) and chart_data.get("echarts_option"):
                        _opt = chart_data.get("echarts_option") or {}
                        _opt_title = _opt.get("title")
                        title = (chart_data.get("title") or
                                 (_opt_title.get("text") if isinstance(_opt_title, dict) else _opt_title) or
                                 "图表")
                        writer({
                            "type": "chart",
                            "title": title,
                            "chart_type": chart_data.get("chart_type", ""),
                            "echarts_option": _opt,
                        })
                else:
                    # 其他工具结果 → analysis_result (前端更新 tool 完成状态)
                    try:
                        tool_result = _safe_parse_content(msg.content)
                    except Exception:
                        tool_result = None
                    writer({
                        "type": "analysis_result",
                        "tool": tool_name,
                        "agent": self.name,
                        "data": tool_result if isinstance(tool_result, (dict, list)) else None,
                    })

