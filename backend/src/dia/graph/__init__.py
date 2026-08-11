"""StateGraph -- Data Intelligence Agent

3 Agent: Curator + Analyst + Reporter (文件上传统一走 /datasources/upload API)
"""
import logging
from langgraph.graph import StateGraph, START, END
from dia.core.state import MultiAgentState

logger = logging.getLogger(__name__)

_graph = None


async def build_graph():
    """Build StateGraph with AsyncSqliteSaver for multi-turn state."""
    from dia.graph.supervisor import supervisor_node, supervisor_router
    from dia.agents.data_curator import curator_node
    from dia.agents.analyst import analysis_wrapper_node
    from dia.agents.reporter import reporter_node

    g = StateGraph(MultiAgentState)

    g.add_node("supervisor", supervisor_node)
    g.add_node("curator", curator_node)
    g.add_node("analyst", analysis_wrapper_node)
    g.add_node("reporter", reporter_node)

    g.add_edge(START, "supervisor")
    g.add_conditional_edges("supervisor", supervisor_router, {
        "curator": "curator",
        "analyst": "analyst",
        "reporter": "reporter",
        "__end__": END,  # 必须声明: router 返回 END 但 map 缺它 → 运行时 KeyError('__end__')
    })

    for node in ["curator", "analyst"]:
        g.add_edge(node, "supervisor")
    g.add_edge("reporter", END)

    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        import aiosqlite
        from dia.core.config import settings as _settings
        import os
        os.makedirs(_settings.STORAGE_DIR, exist_ok=True)
        checkpoints_path = os.path.join(_settings.STORAGE_DIR, "checkpoints.db")
        conn = await aiosqlite.connect(checkpoints_path)
        compiled = g.compile(checkpointer=AsyncSqliteSaver(conn))
        logger.info(f"StateGraph compiled with AsyncSqliteSaver ({checkpoints_path})")
    except ImportError:
        compiled = g.compile()
        logger.warning("AsyncSqliteSaver/aiosqlite not available, no checkpointer")

    return compiled


async def get_graph():
    global _graph
    if _graph is None:
        _graph = await build_graph()
    return _graph
