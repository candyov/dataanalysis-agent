"""State definitions -- Strict TypedDict + runtime key validation.

MultiAgentState: all top-level keys defined explicitly.
Agent sub-states: nested TypedDict for curator/analyst/reporter/data.
(ingestor 字段保留仅为兼容旧 session 数据, 图内已无 ingestor agent)
"""

from __future__ import annotations
from typing import Annotated, Any, TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


# ══════════════════════════════════════════════════════════════════
#  Agent output sub-states
# ══════════════════════════════════════════════════════════════════

class CuratorOutput(TypedDict, total=False):
    """Curator data preparation output."""
    done: bool
    score: int
    summary: str
    kpis: list[str]


class IngestorOutput(TypedDict, total=False):
    """File ingestor output."""
    done: bool
    summary: str


class ChartSuggestion(TypedDict, total=False):
    title: str
    chart_type: str
    data: Any


class AnalysisOutput(TypedDict, total=False):
    """Analyst output."""
    done: bool
    summary: str
    confidence: float
    chart_data: list[dict[str, Any]]
    structured_data: dict[str, Any]


class ReporterOutput(TypedDict, total=False):
    """Reporter output."""
    done: bool
    report: str
    summary: str
    html: str


class DataOutput(TypedDict, total=False):
    """Data container (used in session persistence)."""
    done: bool
    summary: str
    clean_path: str
    metadata_path: str
    quality_score: int
    confidence: float
    structured_data: dict[str, Any]


class PlanStep(TypedDict):
    agent: str
    goal: str


class ExecutionPlan(TypedDict, total=False):
    intent: str
    summary: str
    steps: list[PlanStep]


class SharedContext(TypedDict, total=False):
    """Cross-agent whiteboard — 字段 ownership 与生命周期契约.

    生命周期语义 (写入时必须遵守):
    - 覆盖 (每轮重写): data_quality_score / quality_report / curator_report / report_blueprint / suggest_next
    - 累积 (append): charts (chat.py SSE 收集器独占, 图内 agent 禁止写入)
    - 一次性消费: suggest_next (supervisor 消费后置 None, 防跨轮残留)

    Ownership 矩阵 (写者 → 读者):
    - data_quality_score: curator → analyst / reporter / renderer / get_session_api
    - registered_kpis:     curator → analyst (提示用)
    - glossary:            curator → analyst / blueprint / analyst 子图 date_cols
    - quality_report:      curator → reporter / renderer
    - curator_report:      curator → analyst / reporter (含 source_id, 复用校验用)
    - report_blueprint:    curator 生成, analyst merge → renderer / reporter (章节注入)
    - charts:              chat.py SSE 收集器 → 前端 / session 恢复
    - suggest_next:        analyst → supervisor (skip/revisit 动态路由)
    - final_report:        reporter → renderer / 报告下载
    - degraded / degraded_agent / degraded_reason: supervisor → chat.py SSE (DegradedEvent)
    - confidence:          analyst (置信度) → 前端展示
    - analysis_history:    supervisor 从 glossary_cache 读入 → analyst (历史结论背景, 跨会话)
    """
    data_quality_score: int | str
    registered_kpis: list[str]
    glossary: dict[str, Any]
    final_report: str
    degraded: bool
    confidence: float
    degraded_agent: str
    degraded_reason: str
    charts: list[dict[str, Any]]
    # 动态路由建议: {"action": "skip"|"revisit", "target": "reporter"|"curator", "reason": "..."}
    suggest_next: dict[str, Any]
    # 报告蓝图: Curator 探查产出 → Analyst 填充 → Reporter 叙事 → Renderer 渲染
    report_blueprint: dict[str, Any]
    curator_report: dict[str, Any]
    quality_report: dict[str, Any]
    # 历史分析结论 (跨会话): supervisor 从 glossary_cache 读入 → analyst 注入 context
    analysis_history: list[dict[str, Any]]


# ══════════════════════════════════════════════════════════════════
#  Top-level state
# ══════════════════════════════════════════════════════════════════

def _merge_shared(a: dict | None, b: dict | None) -> dict:
    """shared_context 合并 reducer — 节点返回 partial 也自动合并, 不依赖手动 spread.

    注意: merge 语义无法通过"省略字段"删除 — 清除需显式赋 None/空值
    (如 supervisor 清 suggest_next: None, revisit 清 charts: []).
    """
    merged = dict(a or {})
    merged.update(b or {})
    return merged


class MultiAgentState(TypedDict, total=False):
    # -- Session --
    session_id: str
    user_request: str
    source_id: str
    file_path: str       # 文件路径 (兼容保留, 上传走 API 后无 agent 写入)

    # -- Supervisor --
    plan: ExecutionPlan
    plan_step: int
    current_goal: str
    next: str
    iteration_count: int

    # -- Agent outputs --
    curator: CuratorOutput
    ingestor: IngestorOutput
    analysis: AnalysisOutput
    reporter: ReporterOutput

    # -- Data container (session persistence) --
    data: DataOutput

    # -- Messages --
    messages: Annotated[list[BaseMessage], add_messages]

    # -- Cross-agent whiteboard -- (merge reducer: partial 返回自动合并)
    shared_context: Annotated[SharedContext, _merge_shared]


class AnalysisAgentInternalState(TypedDict, total=False):
    """Analyst 子图 ReAct 状态 (自由 ReAct + 代码兜底).

    图结构: agent → tool_node → agent (ReAct 循环, 轮次上限 MAX_TOOL_ROUNDS)
      → gap_fill (代码硬锁补缺) → synthesize (一次总结)

    source_id / date_cols 由 extract_input 注入, gap_fill 代码补缺时使用
    (不依赖内层 state 有 shared_context — 子图隔离).
    """
    messages: Annotated[list[BaseMessage], add_messages]
    user_request: str
    analysis_done: bool
    source_id: str
    date_cols: list[str]


# ══════════════════════════════════════════════════════════════════
#  Runtime validation
# ══════════════════════════════════════════════════════════════════

# 动态生成, 避免与 MultiAgentState 字段重复维护
_VALID_KEYS: frozenset[str] = frozenset(MultiAgentState.__annotations__.keys())


def validate_state(state: dict) -> None:
    """Strict validation — raises on unknown state keys.

    Call this in supervisor_node / graph entry points.
    Catches typos like 'plan_setp' instead of 'plan_step'.
    """
    extra = set(state.keys()) - _VALID_KEYS
    if extra:
        raise TypeError(
            f"MultiAgentState 非法 key: {sorted(extra)}. "
            f"有效 key: {sorted(_VALID_KEYS)}"
        )
