"""Token 计量中间件 -- 基于 LangChain callback 统计 LLM 调用成本

用法:
    from dia.infrastructure.observability.callbacks import TokenTracker
    tracker = TokenTracker()
    config = {"callbacks": [tracker]}
    llm.ainvoke(messages, config=config)
"""

import contextvars
import logging
import time
from typing import Any, Optional
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

logger = logging.getLogger(__name__)

# ContextVar: asyncio 单事件循环线程内并发请求各自独立 (threading.local 会互相覆盖)
_current_tracker: contextvars.ContextVar = contextvars.ContextVar("token_tracker", default=None)


class TokenTracker(BaseCallbackHandler):
    """多 Agent Token 使用量追踪器

    统计维度:
    - 按 Agent (supervisor/curator/analyst/reporter) 分组
    - prompt_tokens / completion_tokens / total_tokens
    - 调��次数
    - 每次调用的耗时 (ms)
    """

    def __init__(self, trace_id: str = ""):
        self.trace_id = trace_id
        self._records: list[dict] = []
        self._current_start: float = 0.0
        self._current_agent: str = "unknown"
        self._current_tool_start: float = 0.0
        self._current_tool_name: str = ""

    def set_agent(self, agent_name: str) -> None:
        self._current_agent = agent_name

    def on_llm_start(self, serialized: dict, prompts: list, **kwargs) -> None:
        self._current_start = time.perf_counter()

    def on_llm_end(self, response: LLMResult, **kwargs) -> None:
        elapsed = (time.perf_counter() - self._current_start) * 1000
        usage = {}
        if response.llm_output and "token_usage" in response.llm_output:
            usage = response.llm_output["token_usage"]
        elif response.generations:
            gen = response.generations[0][0]
            if hasattr(gen, "generation_info") and gen.generation_info:
                usage = gen.generation_info.get("token_usage", {})
            usage_resp = getattr(gen, "usage_metadata", None) or {}
            if usage_resp:
                usage = {
                    "prompt_tokens": usage_resp.get("input_tokens", 0),
                    "completion_tokens": usage_resp.get("output_tokens", 0),
                    "total_tokens": usage_resp.get("total_tokens", 0),
                }

        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", 0)

        record = {
            "agent": self._current_agent,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "elapsed_ms": round(elapsed, 1),
        }
        self._records.append(record)
        logger.info(
            f"llm call agent={self._current_agent} "
            f"prompt={prompt_tokens} completion={completion_tokens} "
            f"total={total_tokens} elapsed_ms={round(elapsed, 1)}"
        )

    def summary(self) -> dict:
        """按 Agent 汇总 token 消耗"""
        agent_stats: dict[str, dict] = {}
        for r in self._records:
            agent = r["agent"]
            if agent not in agent_stats:
                agent_stats[agent] = {
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "total_elapsed_ms": 0,
                }
            s = agent_stats[agent]
            s["calls"] += 1
            s["prompt_tokens"] += r["prompt_tokens"]
            s["completion_tokens"] += r["completion_tokens"]
            s["total_tokens"] += r["total_tokens"]
            s["total_elapsed_ms"] += r["elapsed_ms"]

        grand_total = sum(s["total_tokens"] for s in agent_stats.values())
        grand_prompt = sum(s["prompt_tokens"] for s in agent_stats.values())
        grand_completion = sum(s["completion_tokens"] for s in agent_stats.values())
        grand_calls = sum(s["calls"] for s in agent_stats.values())
        grand_elapsed = sum(s["total_elapsed_ms"] for s in agent_stats.values())

        return {
            "trace_id": self.trace_id,
            "by_agent": agent_stats,
            "totals": {
                "calls": grand_calls,
                "prompt_tokens": grand_prompt,
                "completion_tokens": grand_completion,
                "total_tokens": grand_total,
                "total_elapsed_ms": round(grand_elapsed, 1),
            },
        }

    def on_llm_error(self, error: Exception, **kwargs) -> None:
        elapsed = (time.perf_counter() - self._current_start) * 1000
        logger.warning(f"llm error agent={self._current_agent} error={error} elapsed_ms={round(elapsed, 1)}")

    def on_tool_start(self, serialized: dict, input_str: str, **kwargs) -> None:
        """工具调用开始 -- 记录审计日志"""
        tool_name = serialized.get("name", "unknown")
        self._current_tool_start = time.perf_counter()
        self._current_tool_name = tool_name

    def on_tool_end(self, output: str, **kwargs) -> None:
        """工具调用结束 -- 写入审计日志"""
        elapsed = (time.perf_counter() - getattr(self, '_current_tool_start', time.perf_counter())) * 1000
        tool_name = getattr(self, '_current_tool_name', 'unknown')
        status = "error" if "error" in str(output).lower()[:50] else "success"

        try:
            from dia.infrastructure.security.audit import log_tool_call
            log_tool_call(
                trace_id=self.trace_id,
                agent=self._current_agent,
                tool=tool_name,
                args={},
                result=str(output)[:300],
                status=status,
                duration_ms=elapsed,
            )
        except Exception:
            pass  # 审计失败不影响主流程

    def on_tool_error(self, error: Exception, **kwargs) -> None:
        """工具调用异常 -- 记录审计日志"""
        tool_name = getattr(self, '_current_tool_name', 'unknown')
        try:
            from dia.infrastructure.security.audit import log_tool_call
            log_tool_call(
                trace_id=self.trace_id,
                agent=self._current_agent,
                tool=tool_name,
                args={},
                result=str(error)[:300],
                status="error",
                duration_ms=0,
            )
        except Exception:
            pass


def get_current_tracker() -> Optional[TokenTracker]:
    """获取当前请求的 TokenTracker 实例"""
    return _current_tracker.get()


def set_current_tracker(tracker: TokenTracker) -> None:
    """设置当前请求的 TokenTracker 实例"""
    _current_tracker.set(tracker)


def clear_current_tracker() -> None:
    """清理当前请求的 TokenTracker"""
    _current_tracker.set(None)
