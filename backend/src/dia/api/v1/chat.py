"""
chat API -- SSE 流式执行 Multi-Agent (Supervisor -> Curator / Analyst / Reporter)
"""
import json
import logging
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage
from dia.core.state import MultiAgentState
from dia.graph import get_graph
from dia.infrastructure.persistence.sessions import get_session, save_session, delete_session, list_sessions
from dia.core.base import _safe_parse_content
from dia.infrastructure.observability.callbacks import TokenTracker, set_current_tracker, clear_current_tracker
from dia.infrastructure.observability.logging import get_trace_id, set_trace_id
from dia.infrastructure.security.sanitizer import sanitize
from dia.core.events import (
    SSEEvent, StartEvent, DoneEvent, CompleteEvent, ErrorEvent,
    ThinkingEvent, StreamEvent, BotEvent, SummaryEvent,
    StageEvent, PlanEvent, StatusEvent, ToolCallEvent, AnalysisResultEvent,
    ChartEvent, TokenSummaryEvent, DegradedEvent, ConfirmRequiredEvent,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _sanitize_messages(msgs: list) -> list:
    """清除未配对的 tool / tool_calls 消息，避免 LLM API 400 错误."""
    cleaned = []
    pending_tool_calls = set()
    for m in msgs:
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                pending_tool_calls.add(tc.get("id", ""))
            cleaned.append(m)
        elif isinstance(m, ToolMessage):
            tc_id = getattr(m, "tool_call_id", "")
            if tc_id in pending_tool_calls:
                cleaned.append(m)
            # else: orphaned ToolMessage → skip
        else:
            cleaned.append(m)
    # 移除尾部未配对的 AIMessage (有 tool_calls 但无对应 ToolMessage)
    while cleaned and isinstance(cleaned[-1], AIMessage) and getattr(cleaned[-1], "tool_calls", None):
        tc_ids = {tc.get("id", "") for tc in cleaned[-1].tool_calls}
        has_response = any(
            isinstance(m, ToolMessage) and getattr(m, "tool_call_id", "") in tc_ids
            for m in cleaned
        )
        if not has_response:
            cleaned.pop()
        else:
            break
    return cleaned

SYSTEM_PROMPT = """你是 Data Intelligence Agent.用户连接了数据库,你需要分析数据、发现洞察.

工作流程:
1. Explorer: 自动发现数据源结构和业务语义
2. DataEngineer: 查询和分析数据
3. Analyst: 假设驱动的深度推理
4. Diagnostician: 根因分析和证据链
5. Reporter: 生成分析报告

用中文回复."""


class ChatRequest(BaseModel):
    message: str
    session_id: str = ""
    source_id: str = ""
    # 人机协同 (P2): 用户对 confirm_required 事件的回复
    # "continue" = 继续用已有分析结果 / "reanalyze" = 重新分析 / 空 = 正常新请求
    confirmation: str = ""


@router.post("/chat")
async def chat(req: ChatRequest, request: Request):
    if not req.message.strip():
        raise HTTPException(400, "消息不能为空")

    # SSE 生成器在独立 task 运行, contextvar 不传播 → 显式传 trace_id
    trace_id = getattr(request.state, "trace_id", "")

    return StreamingResponse(
        _stream(req.message, req.source_id, req.session_id, request, trace_id, req.confirmation),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _stream(user_request: str, source_id: str, session_id: str, request: Request = None,
                  trace_id: str = "", confirmation: str = ""):
    """Multi-Agent Supervisor 流式输出"""

    # 恢复 trace_id (SSE 生成器在独立 task, contextvar 已丢失)
    if trace_id:
        set_trace_id(trace_id)

    # 初始化 Token 追踪器
    tracker = TokenTracker(trace_id=get_trace_id())
    set_current_tracker(tracker)

    # 敏感信息脱敏
    user_request, sanitized_count = sanitize(user_request)
    if sanitized_count > 0:
        logger.info(f"[Sanitizer] 用户输入脱敏 {sanitized_count} 处")

    async def _disconnected() -> bool:
        """检查客户端是否已断开."""
        if request is None:
            return False
        return await request.is_disconnected()

    # ---- 问候 / 能力询问 ----
    # 快速路径:精确匹配常见问候
    msg_lower = user_request.strip().lower().rstrip("？?!！?")
    fast_greetings = {"你好", "hi", "hello", "嗨", "在吗", "在不在", "help", "帮助"}
    if msg_lower in fast_greetings:
        yield _evt(StatusEvent(status_type="info", text="你好！我是 AI 数据分析助手."))
        yield _evt(BotEvent(text=(
            "**我能做什么**\n\n"
            "**数据处理** -- 上传 CSV/Excel,自动探索、质检、清洗\n"
            "**数据分析** -- 趋势分析、分组对比、KPI 计算、异常检测、聚类分群、特征归因\n"
            "**数据可视化** -- 根据分析结果生成图表和看板\n"
            "**业务洞察** -- 用自然语言解释数据背后的业务含义\n\n"
            "**快速开始** -- 上传一份数据文件,然后跟我说「分析这份数据」"
        )))
        yield _evt(DoneEvent(status="completed"))
        return

    # 无数据源 + 短消息 → LLM 意图分类
    if not source_id and len(user_request) < 50:
        try:
            llm = await get_llm(temperature=0.1)
            intent_prompt = f"""判断用户意图,只回复一个词:
- greeting: 打招呼/闲聊 (你好、hi、在吗)
- capability: 询问功能/能力 (你能做什么、有什么功能、怎么用)
- analysis: 其他(默认数据分析请求)

用户消息: {user_request}

只回复一个词."""
            resp = await llm.ainvoke([SystemMessage(content=intent_prompt)])
            intent = resp.content.strip().lower()
            if intent in ('greeting', 'capability'):
                yield _evt(StatusEvent(status_type="info", text="你好！我是 AI 数据分析助手."))
                yield _evt(BotEvent(text=(
                    "**我能做什么**\n\n"
                    "**数据处理** -- 上传 CSV/Excel,自动探索、质检、清洗\n"
                    "**数据分析** -- 趋势分析、分组对比、KPI 计算、异常检测、聚类分群、特征归因\n"
                    "**数据可视化** -- 根据分析结果生成图表和看板\n"
                    "**业务洞察** -- 用自然语言解释数据背后的业务含义\n\n"
                    "**快速开始** -- 上传一份数据文件,然后跟我说「分析这份数据」"
                )))
                yield _evt(DoneEvent(status="completed"))
                return
        except Exception as e:
            logger.debug(f"意图分类失败(继续正常流程): {e}")

    # ---- 无数据源 → 引导上传 (分析必须有数据) ----
    if not source_id:
        yield _evt(StatusEvent(status_type="info", text="请先上传数据文件或选择数据源."))
        yield _evt(BotEvent(text=(
            "**开始之前**\n\n"
            "我需要一份数据才能分析。请先:\n"
            "1. **上传 CSV/Excel 文件**\n"
            "2. 或 **连接数据库**\n\n"
            "然后告诉我你想分析什么。"
        )))
        yield _evt(DoneEvent(status="completed"))
        return

    # ---- 正常流程 ----
    prev = get_session(session_id) if session_id else None
    if prev is not None:
        messages = _sanitize_messages(list(prev.get("messages", [])))
        messages.append(HumanMessage(content=user_request))
        # 新消息来了 → 清 plan 和状态,让 Supervisor 重新规划
        prev_data = prev.get("data", {})
        # 换数据源 → 旧 agent 输出/语义层全部作废:
        # 残留 curator.done=True 会让 supervisor 跳过新数据的数据准备,
        # 残留 glossary/curator_report 会把旧列名喂给 analyst (全链路错)
        prev_shared = prev.get("shared_context", {}) or {}
        if source_id and prev.get("source_id") != source_id:
            prev = {
                **prev,
                "curator": {},
                "analysis": {},
                "reporter": {},
                "shared_context": {
                    **prev_shared,
                    "glossary": {},
                    "curator_report": {},
                    "quality_report": {},
                    "report_blueprint": {},
                    "registered_kpis": [],
                    "charts": [],
                    "final_report": "",
                    "suggest_next": None,
                },
            }
        initial_state: MultiAgentState = {
            **{k: v for k, v in prev.items() if k not in ("messages", "plan", "plan_step", "next")},
            "user_request": user_request,
            "source_id": source_id or prev.get("source_id", ""),
            "messages": messages,
            "data": {**prev_data, "done": True},
            "analysis": {"done": False},
        }

        # 人机协同 (P2) 恢复: 用户对 confirm_required 的选择
        # - continue: 复用已有 analysis 结果 (confidence 低但用户接受), 直接进 reporter
        # - reanalyze: 清空 analysis 强制重跑
        if confirmation == "continue":
            prev_analysis = prev.get("analysis") or {}
            if prev_analysis.get("done"):
                initial_state["analysis"] = prev_analysis
                logger.info("[SSE] P2 用户选择 continue → 复用已有分析结果")
        elif confirmation == "reanalyze":
            initial_state["analysis"] = {"done": False}
            logger.info("[SSE] P2 用户选择 reanalyze → 重新分析")
    else:
        initial_state: MultiAgentState = {
            "user_request": user_request,
            "source_id": source_id,
            "session_id": session_id,
            "data": {},
            "analysis": {},
            "messages": [
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=f"source_id: {source_id}\nUser request: {user_request}\nPlease start processing."),
            ],
            "plan": {},
            "plan_step": 0,
            "current_goal": "",
            "next": "",
            "iteration_count": 0,
            "curator": {},
            "reporter": {},
            "shared_context": {},
        }

    yield _evt(StartEvent())

    final_state: dict = dict(initial_state)
    streamed_charts: list = []  # SSE 收集器独占 (echarts_option), 结束后合并进 final_state
    last_segments: list | None = None  # 报告分段 (BotEvent 已推), 保存时写入 state 供会话恢复
    prev_plan_step: int | None = None  # revisit 检测: plan_step 回退 → 旧图作废
    # tracker 放顶层 config → langgraph 自动传播给所有节点/子图的 LLM 调用
    config = {"configurable": {"thread_id": session_id or "default"}, "callbacks": [tracker]}

    try:
        async for event in (await get_graph()).astream(
            initial_state, config, stream_mode=["updates", "values", "custom"]
        ):
            # 客户端断开 → 立即停止
            if await _disconnected():
                logger.info("[SSE] 客户端已断开,停止流式输出")
                return

            mode, data = event
            if mode == "custom":
                if isinstance(data, dict):
                    t = data.get("type")
                    if t == "thinking":
                        # 推送思考过程 — 前端渲染为灰色小字, 自动折叠, 不抢眼
                        yield _evt(ThinkingEvent(text=data["text"]))
                    elif t == "stream":
                        yield _evt(StreamEvent(text=data["text"]))
                    elif t == "summary":
                        yield _evt(SummaryEvent(text=data["text"]))
                    elif t == "tool_call":
                        # 工具调用进度 — BaseAgent._stream_events 从子图全量消息提取
                        yield _evt(ToolCallEvent(
                            tool=data.get("tool", "unknown"),
                            agent=data.get("agent", "unknown"),
                        ))
                    elif t == "analysis_result":
                        # 工具结果摘要 — 前端更新 tool 完成状态
                        yield _evt(AnalysisResultEvent(
                            tool=data.get("tool", "unknown"),
                            data=data.get("data"),
                        ))
                    elif t == "chart":
                        # SSE 收集器独占 charts — 结束后合并进 final_state (session 恢复用)
                        chart_entry = {
                            "title": data.get("title", "图表"),
                            "chart_type": data.get("chart_type", ""),
                            "echarts_option": data.get("echarts_option", {}),
                        }
                        streamed_charts.append(chart_entry)
                        # 推送到前端
                        yield _evt(ChartEvent(
                            title=chart_entry["title"],
                            chart_type=chart_entry["chart_type"],
                            echarts_option=chart_entry["echarts_option"],
                        ))
                continue

            if mode == "values":
                # 完整图内 state — 最后一次赋值即最终 state (权威来源, 替代手工累加)
                final_state = data
                continue

            # mode == "updates": 节点增量, 只用于事件推送, 不再手工维护 final_state
            for node_name, node_output in data.items():
                if node_output is None:
                    continue

                if node_name == "supervisor":
                    decision = node_output.get("next", "?")
                    label_map = {
                        "curator": "数据准备",
                        "analyst": "分析引擎",
                        "reporter": "报告生成",
                        "finish": "完成",
                    }
                    # 人机协同 (P2): supervisor 请求确认 → 发事件 + 结束流 (用户选择后重发)
                    if decision == "confirm":
                        shared_now = node_output.get("shared_context", {}) or {}
                        yield _evt(ConfirmRequiredEvent(
                            reason=shared_now.get("confirm_reason", "分析置信度较低, 需要确认"),
                            confidence=float(shared_now.get("confirm_confidence", 0.0)),
                        ))
                        yield _evt(DoneEvent(status="failed", message="等待用户确认"))
                        return
                    yield _evt(StageEvent(agent=decision, label=label_map.get(decision, decision)))
                    # 任务列表: 首次规划 (plan_step=0) 推送完整步骤骨架
                    if node_output.get("plan_step", -1) == 0:
                        plan = node_output.get("plan") or {}
                        plan_steps = plan.get("steps") or []
                        if plan_steps:
                            yield _evt(PlanEvent(steps=[
                                {"agent": s.get("agent", ""), "goal": s.get("goal", "")}
                                for s in plan_steps if isinstance(s, dict)
                            ]))
                    # revisit (plan_step 回退) → 本轮已收集图表作废, 重跑后重新收集
                    new_step = node_output.get("plan_step")
                    if isinstance(new_step, int) and isinstance(prev_plan_step, int) and new_step < prev_plan_step:
                        streamed_charts.clear()
                        logger.info(f"[SSE] revisit: plan_step {prev_plan_step}→{new_step}, 清空已收集图表")
                    if isinstance(new_step, int):
                        prev_plan_step = new_step
                    continue

                if node_name == "curator":
                    curator_output = node_output.get("curator", {})
                    if curator_output.get("done"):
                        # 数据就绪摘要 (不推六模块探查全文 — 术语化/信息过载;
                        # 只给决策者 3 行: 质量 / 口径 / 能力边界)
                        shared = node_output.get("shared_context", {}) or {}
                        grade = (shared.get("quality_report") or {}).get("grade", "?")
                        confirm = (shared.get("curator_report") or {}).get("confirm", {}) or {}
                        lines = [f"数据就绪: 质量 {grade} 级"]
                        if confirm.get("caliber"):
                            lines.append(f"口径: {confirm['caliber']}")
                        cannot = confirm.get("cannot_answer") or []
                        if cannot:
                            lines.append(f"无法回答: {'; '.join(cannot[:3])}")
                        yield _evt(StatusEvent(text="\n".join(lines), status_type="info"))
                    # 工具调用/结果事件由 BaseAgent._stream_events 经 custom 分支推送,
                    # 这里不再从瘦身后的 messages 提取 (父图 updates 不含子图内部消息)
                    continue

                if node_name == "analyst":
                    # 工具调用/图表/结果事件由 BaseAgent._stream_events 经 custom 分支推送
                    continue

                if node_name == "reporter":
                    # 报告完成由 supervisor 的 StageEvent(finish) 驱动, 这里不重复发
                    # 完整报告文本 (图表由下方分段逻辑内嵌, 随 BotEvent 一起推送)
                    report_text = node_output.get("reporter", {}).get("report", "")
                    if report_text:
                        # 报告分段: 后端用 analysis.charts (与 Reporter 清单同源同序) 确定性拆分,
                        # 图表数据内嵌 segments — 前端实时/恢复都直接消费, 不再自行匹配
                        segments = None
                        try:
                            from dia.report.segments import split_report_segments
                            charts = (final_state.get("analysis") or {}).get("charts") or list(streamed_charts)
                            if charts:
                                segments = split_report_segments(report_text, charts)
                                logger.info(f"[SSE] 报告分段: {len(segments)} 段 ({sum(1 for s in segments if s.get('type')=='chart')} 图内联)")
                        except Exception as e:
                            logger.warning(f"[SSE] 报告分段失败(回退纯文本): {e}")
                        last_segments = segments
                        yield _evt(BotEvent(text=report_text, segments=segments))
                    continue

        # 流结束后再次检查断连,避免无效写库和推送
        if await _disconnected():
            logger.info("[SSE] 客户端已断开,跳过保存和完成事件")
            return

        if session_id:
            # charts 由 SSE 收集器独占: 图内 state 的旧 charts (多轮恢复) + 本轮流式图表
            inner_charts = list(((final_state.get("shared_context") or {}).get("charts") or []))
            final_state.setdefault("shared_context", {})["charts"] = inner_charts + streamed_charts
            # 报告分段持久化: 会话恢复时前端直接消费 segments, 图表数据内嵌无需重建池
            if last_segments:
                final_state.setdefault("reporter", {})["segments"] = last_segments
            save_session(session_id, final_state)

            # 历史结论记忆: 分析完成后把核心结论按 source_id 落盘 —
            # 新会话注入 Analyst context, 让 LLM 知道"上次分析过什么"
            try:
                from dia.infrastructure.persistence.glossary_cache import append_history
                shared_ctx = final_state.get("shared_context", {}) or {}
                final_report = shared_ctx.get("final_report", "")
                if final_report:
                    # 提取"核心结论"段 (报告第一段, 业务语言), 最长 400 字
                    conclusion = final_report
                    core_idx = conclusion.find("核心结论")
                    if core_idx >= 0:
                        conclusion = conclusion[core_idx:]
                    else:
                        conclusion = conclusion[:400]
                    append_history(
                        source_id=final_state.get("source_id", ""),
                        conclusion=conclusion[:400],
                        question=final_state.get("user_request", ""),
                    )
            except Exception as e:
                logger.warning(f"[SSE] 历史结论写入失败 (忽略): {e}")

        # 检查降级输出
        shared_ctx = final_state.get("shared_context", {}) or {}
        if shared_ctx.get("degraded"):
            yield _evt(DegradedEvent(
                agent=shared_ctx.get("degraded_agent", "unknown"),
                reason=shared_ctx.get("degraded_reason", "处理过程中遇到问题"),
            ))

        # 发送 Token 使用汇总
        token_summary = tracker.summary()
        yield _evt(TokenSummaryEvent(
            trace_id=token_summary.get("trace_id", ""),
            by_agent=token_summary.get("by_agent", {}),
            totals=token_summary.get("totals", {}),
        ))

        yield _evt(DoneEvent(status="completed", message="Multi-Agent complete"))

    except Exception as e:
        logger.error(f"[Multi-Agent] Error: {e}", exc_info=True)
        if not await _disconnected():
            yield _evt(ErrorEvent(message=str(e)))
            yield _evt(DoneEvent(status="failed"))

    if not await _disconnected():
        yield _evt(CompleteEvent())
    clear_current_tracker()


def _evt(event: SSEEvent) -> str:
    """将 SSEEvent 序列化为 SSE 数据行"""
    return event.to_sse()


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
