"""SSE 事件类型标准化 -- 所有前后端通信事件的 Pydantic Model

前端消费:
    const event = JSON.parse(line.replace('data: ', ''))
    switch (event.type) { case 'thinking': ... }

后端生成:
    from dia.core.events import StageEvent
    yield StageEvent(agent="curator", label="数据准备").to_sse()
"""

from __future__ import annotations
import json
from typing import Any, Literal
from pydantic import BaseModel


# ═══════════════════════════════════════════════
# Base
# ═══════════════════════════════════════════════

class SSEEvent(BaseModel):
    """SSE 事件基类"""
    type: str

    def model_dump_event(self) -> dict:
        """序列化为前端接收格式,type 平铺在内"""
        d = self.model_dump(exclude_none=True)
        return d

    def to_sse(self) -> str:
        """生成 SSE 数据行"""
        return f"data: {json.dumps(self.model_dump_event(), ensure_ascii=False)}\n\n"


# ═══════════════════════════════════════════════
# 会话生命周期
# ═══════════════════════════════════════════════

class StartEvent(SSEEvent):
    type: Literal["start"] = "start"
    message: str = "Multi-Agent starting..."


class DoneEvent(SSEEvent):
    type: Literal["done"] = "done"
    status: Literal["completed", "failed"] = "completed"
    message: str = ""


class CompleteEvent(SSEEvent):
    type: Literal["complete"] = "complete"


# ═══════════════════════════════════════════════
# 流式内容
# ═══════════════════════════════════════════════

class ThinkingEvent(SSEEvent):
    type: Literal["thinking"] = "thinking"
    text: str


class StreamEvent(SSEEvent):
    type: Literal["stream"] = "stream"
    text: str


class BotEvent(SSEEvent):
    type: Literal["bot"] = "bot"
    text: str
    # 报告分段 (文本段/图表段交替, 图表数据内嵌) — 后端确定性生成, 前端只渲染
    segments: list[dict] | None = None


class SummaryEvent(SSEEvent):
    type: Literal["summary"] = "summary"
    text: str


# ═══════════════════════════════════════════════
# 阶段与状态
# ═══════════════════════════════════════════════

class StageEvent(SSEEvent):
    type: Literal["stage"] = "stage"
    agent: str       # curator / analyst / reporter / finish
    label: str       # 中文标签


class PlanEvent(SSEEvent):
    """分析计划 (任务列表初始化) — supervisor 首次规划时推送: [{"agent","goal"}...]"""
    type: Literal["plan"] = "plan"
    steps: list[dict] = []


class StatusEvent(SSEEvent):
    type: Literal["status"] = "status"
    status_type: str = "info"  # info / warning / error
    text: str


# ═══════════════════════════════════════════════
# 工具调用
# ═══════════════════════════════════════════════

class ToolCallEvent(SSEEvent):
    type: Literal["tool_call"] = "tool_call"
    tool: str         # 工具名称
    agent: str        # 调用方 agent


class AnalysisResultEvent(SSEEvent):
    type: Literal["analysis_result"] = "analysis_result"
    tool: str
    data: dict | None = None


# ═══════════════════════════════════════════════
# 图表
# ═══════════════════════════════════════════════

class ChartEvent(SSEEvent):
    type: Literal["chart"] = "chart"
    title: str
    chart_type: str
    echarts_option: dict


# ═══════════════════════════════════════════════
# Token 统计
# ═══════════════════════════════════════════════

class TokenSummaryEvent(SSEEvent):
    type: Literal["token_summary"] = "token_summary"
    trace_id: str = ""
    by_agent: dict = {}
    totals: dict = {}


# ═══════════════════════════════════════════════
# 错误
# ═══════════════════════════════════════════════

class ErrorEvent(SSEEvent):
    type: Literal["error"] = "error"
    message: str


# ═══════════════════════════════════════════════
# 新增: 人机协同 (P2-4)
# ═══════════════════════════════════════════════

class ConfirmRequiredEvent(SSEEvent):
    type: Literal["confirm_required"] = "confirm_required"
    reason: str                                    # 为什么需要确认
    confidence: float = 0.0                        # 当前置信度
    options: list[str] = ["继续", "重新分析", "换个角度"]   # 用户可选操作


class DegradedEvent(SSEEvent):
    type: Literal["degraded"] = "degraded"
    agent: str                                     # 降级的 agent
    reason: str                                    # 降级原因
    partial_result: dict | None = None             # 部分结果
