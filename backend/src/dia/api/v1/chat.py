"""
chat API -- HTTP 层 (薄 router)

职责: 参数校验 + 调用 application/chat_service 编排 + 会话/报告/调试查询。
业务编排 (SSE 流式执行 Multi-Agent) 见 dia.application.chat_service.stream_chat。
依赖方向: api → application → graph/agents/infrastructure
"""
import logging
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from dia.api.schemas.chat import ChatRequest
from dia.application.chat_service import stream_chat
from dia.infrastructure.persistence.sessions import get_session, save_session, delete_session, list_sessions
from dia.core.base import _safe_parse_content

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/chat")
async def chat(req: ChatRequest, request: Request):
    if not req.message.strip():
        raise HTTPException(400, "消息不能为空")

    # SSE 生成器在独立 task 运行, contextvar 不传播 → 显式传 trace_id
    trace_id = getattr(request.state, "trace_id", "")

    return StreamingResponse(
        stream_chat(
            user_request=req.message,
            source_id=req.source_id,
            session_id=req.session_id,
            is_disconnected=lambda: request.is_disconnected(),
            trace_id=trace_id,
            confirmation=req.confirmation,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/sessions")
async def list_sessions_api():
    return {"sessions": list_sessions()}


@router.get("/sessions/{session_id}")
async def get_session_api(session_id: str):
    """获取单个会话的完整状态,供前端切换会话时恢复."""
    state = get_session(session_id)
    if state is None:
        raise HTTPException(404, "会话不存在或已过期")

    # 转换 LangChain 消息为前端可渲染格式
    msgs = state.get("messages", [])
    frontend_msgs = []
    pending_tool_calls = {}  # tool_call_id → index in frontend_msgs

    for m in msgs:
        type_name = type(m).__name__ if hasattr(m, "__class__") else ""

        # Fallback: 消息可能是 dict(未反序列化)
        if isinstance(m, dict):
            msg_type = m.get("type", "")
            if msg_type == "human":
                content = str(m.get("data", {}).get("content", "") if isinstance(m.get("data"), dict) else m.get("content", ""))
                frontend_msgs.append({"role": "user", "text": content})
            elif msg_type == "ai":
                content = str(m.get("data", {}).get("content", "") if isinstance(m.get("data"), dict) else m.get("content", ""))
                if content.strip():
                    frontend_msgs.append({"role": "bot", "text": content})
            continue

        # HumanMessage → user
        if type_name == "HumanMessage":
            content = str(m.content) if hasattr(m, "content") else str(m)
            frontend_msgs.append({"role": "user", "text": content})

        # AIMessage with tool_calls → tool entries
        elif type_name == "AIMessage" and hasattr(m, "tool_calls") and m.tool_calls:
            for tc in m.tool_calls:
                idx = len(frontend_msgs)
                pending_tool_calls[tc.get("id", "")] = idx
                frontend_msgs.append({
                    "role": "tool",
                    "tool": tc.get("name", "unknown"),
                    "args": tc.get("args", {}),
                    "result": None,
                })

        # AIMessage without tool_calls → bot
        elif type_name == "AIMessage":
            content = str(m.content) if hasattr(m, "content") else ""
            if content.strip():
                frontend_msgs.append({"role": "bot", "text": content})

        # ToolMessage → update corresponding tool entry
        elif type_name == "ToolMessage":
            tc_id = getattr(m, "tool_call_id", "")
            if tc_id and tc_id in pending_tool_calls:
                idx = pending_tool_calls[tc_id]
                try:
                    result = _safe_parse_content(m.content)
                except Exception as e:
                    logger.debug(f"Save session parse failed: {e}")
                    result = str(m.content)
                frontend_msgs[idx]["result"] = result
            pending_tool_calls.pop(tc_id, None)

    # 提取图表(含完整 echarts_option,供前端恢复渲染)
    charts = []
    shared_ctx = state.get("shared_context", {}) or {}
    for c in shared_ctx.get("charts", []):
        charts.append({
            "title": c.get("title", ""),
            "chart_type": c.get("chart_type", ""),
            "echarts_option": c.get("echarts_option", None),
        })

    # 提取文件信息
    file_info = {}
    data_output = state.get("data", {})
    if data_output.get("done"):
        file_info = {
            "file_name": state.get("file_path", "").replace("\\", "/").split("/")[-1] if state.get("file_path") else "",
            "file_path": state.get("file_path", ""),
        }

    # 报告分段: 挂到报告文本对应的 bot 消息上 (前端恢复会话时直接渲染 segments,
    # 图表数据内嵌其中, 无需重建图表池)
    saved_segments = (state.get("reporter") or {}).get("segments")
    if saved_segments:
        report_text = (state.get("reporter") or {}).get("report") or (state.get("shared_context") or {}).get("final_report", "")
        for fm in reversed(frontend_msgs):
            if fm.get("role") == "bot" and report_text and fm.get("text") == report_text:
                fm["segments"] = saved_segments
                break

    return {
        "session_id": session_id,
        "file_path": state.get("file_path", ""),
        "file_info": file_info,
        "intent": (state.get("plan") or {}).get("intent", "") if isinstance(state.get("plan"), dict) else "",
        "iteration_count": state.get("iteration_count", 0),
        "data": {
            "done": data_output.get("done", False),
            "summary": data_output.get("summary", ""),
            "clean_path": data_output.get("clean_path", ""),
            "metadata_path": data_output.get("metadata_path", ""),
            # 唯一真源是 shared_context.data_quality_score (curator 写);
            # data.quality_score 是死字段 (无人写), 不再读它
            "quality_score": (state.get("shared_context") or {}).get("data_quality_score", 100),
            "confidence": data_output.get("confidence", 1.0),
            "structured_data": data_output.get("structured_data", {}),
        },
        "analysis": {
            "done": state.get("analysis", {}).get("done", False),
            "summary": state.get("analysis", {}).get("summary", ""),
            "confidence": state.get("analysis", {}).get("confidence", 1.0),
            "chart_data": state.get("analysis", {}).get("chart_data", []),
        },
        "reporter": {
            "done": state.get("reporter", {}).get("done", False),
            "charts": charts,
            "summary": state.get("reporter", {}).get("summary", ""),
        },
        "messages": frontend_msgs,
    }


@router.delete("/sessions/{session_id}")
async def delete_session_api(session_id: str):
    delete_session(session_id)
    return {"status": "deleted"}


@router.get("/sessions/{session_id}/report")
async def download_report(session_id: str, format: str = "md"):
    """生成分析报告 (md 或 html).

    format=md  → Markdown 文本 (旧行为)
    format=html → 独立 HTML 报告文件 (KPI 卡片/图表/发现/质量, 数据来自本项目产出)
    """
    from datetime import datetime
    from fastapi.responses import PlainTextResponse, HTMLResponse

    state = get_session(session_id)
    if state is None:
        raise HTTPException(404, "会话不存在或已过期")

    if format == "html":
        from dia.report.renderer import build_report_data, render_report_html
        data = build_report_data(state)
        html_report = render_report_html(data)
        return HTMLResponse(
            html_report,
            headers={
                "Content-Disposition": f"attachment; filename=report_{session_id[:8]}.html"
            },
        )

    user_request = state.get("user_request", "")
    data_summary = state.get("data", {}).get("summary", "")  # 兼容旧会话
    analysis_summary = state.get("analysis", {}).get("summary", "")
    shared_ctx = state.get("shared_context", {}) or {}
    final_report = shared_ctx.get("final_report", "")  # Reporter 完整报告
    charts = shared_ctx.get("charts", [])
    file_path = state.get("file_path", "")
    file_name = file_path.replace("\\", "/").split("/")[-1] if file_path else "未知"

    lines = []
    lines.append("# 数据分析报告\n")
    lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    lines.append(f"**数据文件**: {file_name}\n")
    lines.append(f"**分析问题**: {user_request}\n")
    lines.append("\n---\n")

    # 优先 Reporter 完整报告; 旧会话回退到 analysis summary
    if final_report:
        lines.append("\n## 分析报告\n")
        lines.append(final_report)
        lines.append("")
    else:
        if data_summary:
            lines.append("\n## 数据概况\n")
            lines.append(data_summary)
            lines.append("")
        if analysis_summary:
            lines.append("\n## 分析结论\n")
            lines.append(analysis_summary)
            lines.append("")

    if charts:
        lines.append("\n## 生成图表\n")
        for c in charts:
            lines.append(f"- [{c.get('chart_type', '图表')}] {c.get('title', '')}")
        lines.append("")

    lines.append("\n---\n")
    lines.append("*本报告由 AI 数据分析助手自动生成*\n")

    report = "\n".join(lines)

    return PlainTextResponse(
        report,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename=report_{session_id[:8]}.md"
        },
    )


@router.get("/sessions/{session_id}/data")
async def get_session_data(session_id: str):
    """获取会话的 report_blueprint 数据 (供前端原生渲染报告).

    返回 shared_context.report_blueprint, 包含:
      - dimensions / metrics / chapters (含图表和发现)
      - quality / overview
    若无蓝图则返回 404.
    """
    state = get_session(session_id)
    if state is None:
        raise HTTPException(404, "会话不存在或已过期")

    shared = state.get("shared_context", {}) or {}
    blueprint = shared.get("report_blueprint")
    if not blueprint:
        raise HTTPException(404, "该会话暂无报告蓝图数据 (分析未完成或 Curator 未生成)")

    # row_count 兜底: blueprint.overview.row_count 为 "?" 时, 从 state.data 取真实行数
    # (旧会话/探查未回填 row_count 的会话, KPI 卡片会显示 "?" — 这里统一修正)
    bp_overview = blueprint.setdefault("overview", {})
    if not bp_overview.get("row_count") or bp_overview.get("row_count") == "?":
        data_out = state.get("data", {}) or {}
        structured = data_out.get("structured_data", {}) or {}
        rc = structured.get("row_count") or data_out.get("row_count")
        if isinstance(rc, (int, float)) and rc > 0:
            bp_overview["row_count"] = int(rc)
        elif isinstance(rc, str) and rc.isdigit():
            bp_overview["row_count"] = int(rc)

    return {
        "session_id": session_id,
        "user_request": state.get("user_request", ""),
        "report_blueprint": blueprint,
    }


# ═══════════════════════════════════════════════
# Debug API -- Trace 快照与回放
# ═══════════════════════════════════════════════

from dia.infrastructure.persistence.sessions import list_traces as _list_traces, get_trace_snapshot as _get_trace


@router.get("/debug/traces")
async def list_traces_api(limit: int = 50):
    """列出最近的 trace 快照"""
    return {"traces": _list_traces(limit)}


@router.get("/debug/traces/{trace_id}")
async def get_trace_api(trace_id: str):
    """获取单个 trace 的完整快照"""
    trace = _get_trace(trace_id)
    if trace is None:
        raise HTTPException(404, "Trace 不存在或已过期")
    return trace


@router.get("/admin/audit")
async def list_audit_api(trace_id: str = "", agent: str = "", limit: int = 100):
    """查询审计日志"""
    from dia.infrastructure.security.audit import query_audit
    return {"audit": query_audit(trace_id=trace_id, agent=agent, limit=limit)}
