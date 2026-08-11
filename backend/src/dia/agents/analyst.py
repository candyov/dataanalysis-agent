"""Analyst -- ReAct 模式: 对比 + 下钻 + 归因 + 预测 + 统计检验"""
import json
import re
import logging
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode

from dia.core.base import get_llm, _safe_parse_content, BaseAgent
from dia.tools import ANALYST_TOOLS
from dia.core.state import AnalysisAgentInternalState

logger = logging.getLogger(__name__)

TOOLS = ANALYST_TOOLS

SYSTEM_PROMPT = """你是资深数据分析师. 所有工具直连数据库.

## 重要: source_id

用户消息里会提供 source_id (数据源标识, 如 "file_sample" 或 "verify_ds").
**必须使用提供的 source_id, 禁止编造 (如 "my_db").**
如果 source_id 为空, 工具调用会失败——此时应告知用户选择一个数据源.

## 工作方式: 循环探索 (ReAct)

你不是一次性规划所有步骤, 而是**每轮基于上一轮的工具结果决定下一步**:

1. **第一轮** — 看 [数据探查报告] 和 [分析路线图] (上下文已给出), 用 explore 探索核心维度:
   - 每个维度 → explore(aggregate) 分组汇总 + explore(share) 构成占比
   - 有 2+ 维度 → explore(cross_tab) 交叉分析
   - 有日期列 → explore(trend) 时间趋势
2. **看结果, 决定下一步** — 每轮只做基于已有发现最该做的事:
   - 发现"区域A高于区域B / 品类差异 / 渠道差异" → **立即用 test_difference 验证** (p<0.05)
   - 想解释差异 → attribution (控混淆变量)
   - 有日期列 → forecast 预测; 波动异常 → seasonal_analysis
   - 大额交易特征 → explore(top_n)
3. **不要重复** — 已做过的分析不要重做, 结果就在你面前的消息里
4. **最后一轮: 批量出图** — 分析完成后, **在一条消息里同时发全部 build_chart 调用** (8-12 张),
   不要一张一张发 (每轮有成本, 图表占轮次会耗尽预算)

## 轮次预算 (最多 5 轮工具调用, 超了会被代码截断)

建议分配: 探索 2 轮 → 验证 1 轮 → 预测 1 轮 → 出图 1 轮 (批量).
**每轮可以同时发多个工具调用** (如 aggregate + share 一起发), 省轮次.

## 硬性要求 (不满足 = 分析不完整, 代码会补做)

1. **对比结论必须先检验**: 任何"区域A高于区域B / 品类X优于品类Y / 渠道差异"的结论,
   **必须先调用 test_difference 得到 p 值**, 才能写进结论。explore 跑出的数字 = 探索不算证据。
2. **有日期列必须做趋势**: 数据含日期列时, 必须调用 forecast(预测),
   再决定是否 seasonal_analysis。
3. **必须出图**: 分析完成后调用 build_chart 生成 **8-12 张**图 (最后一批量发), 覆盖全部分析角度:
   - 趋势图 (line): 1-2 张 (月度趋势 + 预测)
   - 分组对比 (bar): 每个维度 1-2 张
   - 构成占比 (pie): 每个维度 1 张
   - 交叉分析 (heatmap): 做过 cross_tab 的必须补一张热力图 (区域×品类等)
   - 分布 (describe → 分位数图): 做过 describe 的必须补一张
   - **一张图都没有 = 分析失败**。
   **图表标题必须用中文业务语言** (如 "各区域营收对比", "月度营收趋势"),
   禁止英文标题 ("sales by region") 或纯列名; 类型按场景选:
   趋势→line, 占比→pie, 分组对比→bar, 相关性→scatter, 交叉→heatmap,
   分布→bar(分位数), 避免全是柱状图。
4. **不要重复探查**: 结构/质量/列角色在 [数据探查报告] 里已有, 不要调 explore 之外的工具查结构。

## 工具

| 类别 | 工具 | 何时用 |
|---|---|---|
| 探索 | explore | **主工具**. 6 种操作: aggregate(分组汇总)/cross_tab(交叉表)/trend(趋势)/top_n(大单)/share(占比)/describe(分布)。传 operation 参数; 聚合口径传 agg_func (auto 按指标名推断, 客单价/利润率等均值型指标自动用 avg) |
| 统计验证 | test_difference | **对比结论必须用**. 自动选检验方法(t/ANOVA/非参) + BH 校正, 输出 p 值/效应量/均值差 CI |
| 归因 | attribution | 什么驱动了目标指标: 相关扫描→多元回归(VIF)→显著特征; features 可传分类列(区域/品类, 自动 one-hot) |
| 预测 | forecast | 季节感知线性回归预测 + 置信区间 (自动检测周期, 率/价类指标自动 avg 口径) |
| 波动 | seasonal_analysis | 判断波动是季节驱动还是趋势/噪声驱动 (周期自动检测) |
| 环比 | compare | 环比/同比: 当前周期 vs 上期 (dod/wow/mom/qoq/yoy), 附两段显著性检验 |
| 异常 | detect | 异常检测: Z-score 尖峰 + 趋势漂移 (有 date_col 按日聚合) |
| 图表 | build_chart | 生成 ECharts 图表 (bar/line/pie/scatter), 最后一批量发 |

## 口径 (agg_func) 使用要点

- explore/forecast/compare 都有 agg_func 参数: **sum/avg/count/median**, 默认 auto
- auto 按指标名推断: 含"率/价/单价/avg/mean"的指标(客单价/利润率/均价)→ **avg**, 其余 → sum
- **均值型指标用 sum 会算错账** (客单价按天求和毫无意义), 不确定时让 auto 推断即可
- 需要行数时显式传 count, 需要稳健中心时传 median

## explore 使用要点

- 6 种操作对应 6 类任务, 一次调一个:
  - 区域/品类/渠道对比 → `explore(operation="aggregate", metric=..., group_by=...)`
  - 区域×品类交叉 → `explore(operation="cross_tab", metric=..., group_by=..., group_by2=...)`
  - 时间趋势 → `explore(operation="trend", metric=..., date_col=...)` (粒度自动选: 月/周/日)
  - 大额交易特征 → `explore(operation="top_n", metric=..., top_n=10)` (返回整行)
  - 构成占比 → `explore(operation="share", metric=..., group_by=..., top_n=5)` (尾部自动归并"其他")
  - 分布形态 → `explore(operation="describe", metric=...)`
- 列名必须从 [数据探查报告] 的列角色里取, 不要猜测
- 返回 JSON 结构化数据; 报错会说明原因

## 图表规则

- **时机**: 分析完成后统一出图, 不要边分析边画. 先完成全部数据探查/检验, 最后一条消息批量发 build_chart
- **数量**: 8-12 张, 覆盖全部分析角度
- **质量门槛**: 每张图必须对应一个**已确认的发现**(有具体数字或统计检验支撑)
- **语义绑定**: title 直接写结论, 如 "各区域销售额对比"、"品类营收占比"

## 输出格式

关键发现:
- [强] 华东vs华北营收差异显著 (p=0.003, Welch t=3.21): 华东312万 vs 华北287万
- [弱] 按品类看, 女装在华东占比最高 (描述性观察)
- [推测] 若按当前趋势外推, 下月华东预计下降8%±3% (线性回归, R²=0.87)

根因分析:
- attribution: 控制品类后, 价格对营收影响显著 (系数=-0.42, p=0.01)
- seasonal_analysis: 华东营收波动 78% 由季节性解释, 非趋势问题

建议:
- [具体可执行的建议1]
- [具体可执行的建议2]

## 规则

- 不写思考过程, 不编造数字
- **差异必须用 test_difference 验证, 禁止仅凭均值大小下结论**
- **explore 输出的数字只是探索结果, 不是统计证据** — 写进结论前必须用 test_difference/attribution 验证
- **回归/相关结论标注: 相关≠因果**
- 无异常时说"数据平稳"
- 结论用 [强]/[弱]/[推测] 分级
- 中文回复"""


# ═══════════════════════════════════════════════
# ReAct agent_node
# ═══════════════════════════════════════════════

MAX_TOOL_ROUNDS = 5  # ReAct 工具轮次上限, 超了由 gap_fill 补缺后收尾


async def analysis_agent_node(state: dict, config=None) -> dict[str, Any]:
    """自由 ReAct 节点 — LLM 每轮基于已有工具结果决定下一步调用.

    轮次上限由 should_continue 硬截 (MAX_TOOL_ROUNDS), 防止 DeepSeek 无限循环.
    """
    llm = await get_llm(temperature=0.2)
    llm_with_tools = llm.bind_tools(TOOLS)

    messages = list(state.get("messages", []))

    if not messages:
        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=f"用户需求: {state.get('user_request', '')}\n\n请开始分析: 先探索, 看结果再决定下一步."),
        ]

    logger.info(f"[Analysis] ReAct round: {len(messages)} 条消息")

    response = await llm_with_tools.ainvoke(messages, config=config)

    # 收集 thinking
    thinking = ""
    for loc in ("reasoning_content", "reasoning"):
        val = response.additional_kwargs.get(loc, "")
        if val:
            thinking = val
    response.additional_kwargs["_thinking"] = thinking

    if response.tool_calls:
        logger.info(f"[Analysis] round: {len(response.tool_calls)} 个工具调用")
    else:
        logger.info("[Analysis] LLM 直接回复 (无工具调用)")
    return {"messages": [response]}


tool_node = ToolNode(TOOLS)


async def serial_tool_node(state: dict) -> dict:
    """串行工具执行节点 — 替代 ToolNode 的并行 gather.

    原因: MySQL 连接不支持并发 (单连接共享), ToolNode 用 asyncio.gather 并行
    执行多个 tool_calls 会导致连接争用。这里逐条串行执行, 保连接安全。
    """
    import json as _json

    messages = list(state.get("messages", []))
    last = messages[-1] if messages else None
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return {"messages": messages}

    tool_map = {t.name: t for t in TOOLS}
    # 注意: 只返回新增的 ToolMessage (add_messages reducer 会追加, 返回全量会重复)
    new_msgs = []
    for tc in last.tool_calls:
        name, args, tc_id = tc.get("name", ""), tc.get("args", {}), tc.get("id", "")
        tool = tool_map.get(name)
        if tool is None:
            new_msgs.append(ToolMessage(content=f"未知工具: {name}", tool_call_id=tc_id, name=name))
            continue
        try:
            result = await tool.ainvoke(args) if hasattr(tool, "ainvoke") else tool.invoke(args)
            content = result if isinstance(result, str) else _json.dumps(result, ensure_ascii=False, default=str)
        except Exception as e:
            content = _json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
        new_msgs.append(ToolMessage(content=content, tool_call_id=tc_id, name=name))
    return {"messages": new_msgs}

# ── 代码判定辅助 (gap_fill 用) ──


def _has_comparison_candidates(state: dict) -> bool:
    """代码判定: 探索结果里是否有对比候选 (组间差异 > 20%) → 需要验证阶段"""
    for m in state.get("messages", []):
        if not isinstance(m, ToolMessage) or m.name != "explore":
            continue
        try:
            data = _safe_parse_content(m.content)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        # aggregate: 组间主口径 (value, 按 agg_func) 差异
        groups = data.get("groups") or []
        if groups and ("value" in groups[0] or "sum" in groups[0]):
            vals = [float(g.get("value", g.get("sum", 0))) for g in groups
                    if g.get("value") is not None or g.get("sum") is not None]
            if len(vals) >= 2 and min(vals) > 0 and max(vals) / min(vals) > 1.2:
                return True
        # cross_tab: 组合值差异
        table = data.get("table")
        if isinstance(table, dict):
            vals = [float(v) for row in table.values() if isinstance(row, dict) for v in row.values() if v is not None]
            if len(vals) >= 2 and min(vals) > 0 and max(vals) / min(vals) > 1.2:
                return True
    return False


def _has_date_col(state: dict) -> bool:
    """代码判定: 是否有日期列 (决定 forecast 是否必需).

    优先用 extract_input 注入的 date_cols (子图隔离, 读不到 shared_context.glossary),
    回退到 user_request 关键词.
    """
    if state.get("date_cols"):
        return True
    return any(kw in str(state.get("user_request", "")) for kw in ("趋势", "预测", "环比", "时间", "月度", "走势"))


def should_continue(state: dict) -> str:
    """ReAct 路由: 有工具调用且未超轮次上限 → tool_node; 否则 → gap_fill (补缺后 synthesize)."""
    messages = state.get("messages", [])
    if not messages:
        return "agent"
    last_msg = messages[-1]
    if isinstance(last_msg, AIMessage) and last_msg.tool_calls:
        rounds = sum(1 for m in messages if isinstance(m, AIMessage) and m.tool_calls)
        if rounds > MAX_TOOL_ROUNDS:
            logger.warning(f"[Analysis] 达到轮次上限 {MAX_TOOL_ROUNDS} → 截断, gap_fill 补缺")
            return "gap_fill"
        return "tool_node"
    return "gap_fill"


async def gap_fill_node(state: dict) -> dict:
    """ReAct 兜底补缺节点 — 代码检查关键产出缺口, 缺什么直接调什么 (不靠 LLM).

    1. 有对比候选 (explore 组间差异>20%) 但无 test_difference → 补调
    2. 有日期列但无 forecast → 补调
    3. build_chart < 8 张 → 用规则从工具结果补图 (chart_data)
    """
    messages = list(state.get("messages", []))
    executed_tools = {m.name for m in messages if isinstance(m, ToolMessage)}
    new_msgs: list = []
    # extract_input 注入的 source_id (子图隔离, explore 输出不携带 source_id)
    source_id = state.get("source_id", "")
    date_cols = state.get("date_cols") or []

    # 1. 对比候选 → 必须验证
    if _has_comparison_candidates(state) and "test_difference" not in executed_tools:
        # 找第一个有对比的 explore 结果, 自动构造 test_difference 调用
        for m in messages:
            if isinstance(m, ToolMessage) and m.name == "explore":
                try:
                    data = _safe_parse_content(m.content)
                except Exception:
                    continue
                if isinstance(data, dict) and data.get("groups"):
                    groups = data["groups"]
                    if len(groups) >= 2 and all(g.get("group") for g in groups[:2]):
                        metric = data.get("metric", "")
                        group_by = data.get("group_by", "")
                        args = {"metric": metric, "group_by": group_by,
                                "source_id": source_id, "group_a": groups[0]["group"],
                                "group_b": groups[1]["group"]}
                        logger.info(f"[Analysis] gap_fill: 补调 test_difference({metric} by {group_by})")
                        new_msgs.append(await _invoke_tool("test_difference", args))
                        executed_tools.add("test_difference")
                        break

    # 2. 有日期列但无预测 → 补调 forecast
    if _has_date_col(state) and "forecast" not in executed_tools:
        date_col = date_cols[0] if date_cols else ""
        for m in messages:
            if isinstance(m, ToolMessage) and m.name == "explore":
                try:
                    data = _safe_parse_content(m.content)
                except Exception:
                    continue
                if isinstance(data, dict) and data.get("periods") and data.get("metric"):
                    args = {"metric": data["metric"], "source_id": source_id,
                            "date_col": date_col or data.get("date_col", "")}
                    logger.info(f"[Analysis] gap_fill: 补调 forecast({data['metric']})")
                    new_msgs.append(await _invoke_tool("forecast", args))
                    executed_tools.add("forecast")
                    break

    # 3. 图表不足 → 规则补图 (复用 _extract_chart_data)
    chart_count = sum(1 for m in messages if isinstance(m, ToolMessage) and m.name == "build_chart")
    if chart_count < 8:
        chart_suggestions = _extract_chart_data(messages)
        for suggestion in chart_suggestions[: 8 - chart_count]:
            title = suggestion.get("title", "")
            chart_type = suggestion.get("chart_type", "bar")
            payload = {"data": suggestion.get("data", [])} if chart_type == "pie" else {
                "categories": suggestion.get("categories", []),
                "series": [{"name": title, "data": suggestion.get("data", [])}],
            }
            logger.info(f"[Analysis] gap_fill: 补图 {title}")
            new_msgs.append(await _invoke_tool("build_chart", {
                "chart_type": chart_type, "data": payload, "title": title,
            }))
            chart_count += 1
            if chart_count >= 8:
                break

    if new_msgs:
        logger.info(f"[Analysis] gap_fill: 补齐 {len(new_msgs)} 项缺口")
        return {"messages": new_msgs}
    return {"messages": []}


async def _invoke_tool(name: str, args: dict) -> ToolMessage:
    """直接调用工具, 返回 ToolMessage (gap_fill 用)."""
    import json as _json
    tool_map = {t.name: t for t in TOOLS}
    tool = tool_map.get(name)
    try:
        result = await tool.ainvoke(args) if hasattr(tool, "ainvoke") else tool.invoke(args)
        content = result if isinstance(result, str) else _json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        content = _json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
    return ToolMessage(content=content, tool_call_id=f"gap_{name}", name=name)


async def force_done(state: dict) -> dict:
    """工具轮次已满, 用 LLM 根据已有结果生成总结"""
    return await _synthesize(state)


def build_analysis_graph():
    graph = StateGraph(AnalysisAgentInternalState)
    graph.add_node("agent", analysis_agent_node)
    graph.add_node("tool_node", serial_tool_node)
    graph.add_node("gap_fill", gap_fill_node)
    graph.add_node("synthesize", force_done)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges(
        "agent", should_continue,
        {"tool_node": "tool_node", "gap_fill": "gap_fill"},
    )
    graph.add_edge("tool_node", "agent")
    # gap_fill 补缺后直接 synthesize (补的工具结果已在 messages 里, synthesize 的 LLM 能看到)
    graph.add_edge("gap_fill", "synthesize")
    graph.add_edge("synthesize", END)
    return graph.compile()


class AnalystAgent(BaseAgent):
    """Analyst -- 数据查询 + 分析 + 总结"""

    def build_graph(self):
        return build_analysis_graph()

    async def run(self, state: dict, config=None) -> dict:
        return await super().run(state, config)

    async def extract_input(self, state: dict) -> dict:
        from langchain_core.messages import HumanMessage, SystemMessage

        shared = state.get("shared_context", {}) or {}
        quality_score = shared.get("data_quality_score", "?")
        kpis = shared.get("registered_kpis", [])
        glossary = shared.get("glossary", {})
        curator_report = shared.get("curator_report", {})

        # 从 glossary 提取维度/指标分类
        dim_cols = [k for k, v in glossary.items() if v.get("role") == "dimension"]
        num_cols = [k for k, v in glossary.items() if v.get("role") == "metric"]
        dt_cols = [
            k for k, v in glossary.items()
            if v.get("role") == "datetime"
            or str(v.get("sql_type", "")).upper() in ("DATE", "TIMESTAMP", "DATETIME", "TIME")
        ]

        # 工具可用性提示
        tool_hints = []
        if not dt_cols:
            tool_hints.append("无日期列, forecast / seasonal_analysis 不可用")
        if len(dim_cols) < 1:
            tool_hints.append("无分类列, explore(aggregate) / test_difference 不可用")
        if not num_cols:
            tool_hints.append("无数值列, explore / attribution / forecast 不可用")

        context = (
            f"[预加载上下文]\n"
            f"数据质量: {quality_score}/100\n"
            f"已注册KPI: {', '.join(kpis[:10]) if kpis else '暂无'}\n"
            f"维度列: {', '.join(dim_cols[:10]) or '暂无'}\n"
            f"数值列: {', '.join(num_cols[:10]) or '暂无'}\n"
            f"日期列: {', '.join(dt_cols[:5]) or '暂无'}\n"
        )
        if glossary:
            # 给 LLM 关键映射 (列名→中文)
            context += "关键映射: " + ", ".join(
                f"{k}={v.get('name','')}" for k, v in list(glossary.items())[:10]
            ) + "\n"

        # ── Curator 探查报告注入 ──
        if curator_report:
            context += "\n[数据探查报告]\n"
            confirm = curator_report.get("confirm", {})
            if confirm:
                context += f"口径: {confirm.get('caliber', '未定义')}\n"
                if confirm.get("cannot_answer"):
                    context += f"数据无法回答: {'; '.join(confirm['cannot_answer'][:5])}\n"
            roadmap = curator_report.get("roadmap", {})
            if roadmap.get("rounds"):
                roadmap_lines = []
                for i, rd in enumerate(roadmap["rounds"], 1):
                    steps = "; ".join(rd.get("steps", [])) or rd.get("title", "")
                    roadmap_lines.append(f"  第{i}轮: {rd.get('title','')}: {steps}")
                context += "[分析路线图 (Curator 建议, 优先执行)]\n" + "\n".join(roadmap_lines) + "\n"
            if roadmap.get("impossible"):
                context += f"不可做: {'; '.join(roadmap['impossible'][:5])}\n"
            kpi_tree = curator_report.get("kpi_tree", {})
            if kpi_tree:
                kpi_lines = []
                for tier in ("基础指标", "效率指标", "趋势指标", "结构指标"):
                    for k in kpi_tree.get(tier, [])[:6]:
                        kpi_lines.append(f"{k.get('name')}({k.get('label','')},{k.get('source','')})")
                if kpi_lines:
                    context += "[Curator KPI 建议]\n" + ", ".join(kpi_lines) + "\n"
            # 数据质量分层: 阻塞问题必须告知, 避免对脏数据做统计
            quality = curator_report.get("quality", {}) or shared.get("quality_report", {})
            blockers = quality.get("blockers", [])
            if blockers:
                context += "[数据质量警告]\n" + "\n".join(f"- {b}" for b in blockers[:5]) + "\n"
            # 数据概览: 时间跨度决定能否做趋势/环比
            overview = curator_report.get("data_overview", {})
            if overview:
                ov_lines = []
                if overview.get("tables"):
                    ov_lines.append(f"表: {overview['tables']}")
                if overview.get("time_span"):
                    ov_lines.append(f"时间跨度: {overview['time_span']}")
                if overview.get("findings"):
                    ov_lines.append("采样发现: " + "; ".join(overview["findings"][:3]))
                if ov_lines:
                    context += "[数据概览]\n" + "\n".join(ov_lines) + "\n"

        # ── 历史结论记忆 (跨会话): 同数据源之前分析的结论, 仅背景参考 ──
        # supervisor 首次规划时注入 shared_context.analysis_history (带时间戳),
        # 让 LLM 知道"上次分析过什么", 可延续/对比; 明确标注时间防止当新数据结论
        history = shared.get("analysis_history", [])
        if history:
            hist_lines = []
            for h in history[:3]:
                ts = h.get("created_at", 0)
                try:
                    from datetime import datetime
                    ts_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                except Exception:
                    ts_str = "?"
                q = h.get("question", "") or ""
                c = (h.get("conclusion", "") or "")[:150]
                hist_lines.append(f"- [{ts_str}] 问题「{q}」→ {c}")
            if hist_lines:
                context += ("\n[历史分析结论 (仅背景参考, 时间早于本次分析, 勿当本次数据结论)]\n"
                            + "\n".join(hist_lines) + "\n")

        # ── 多轮对话: 注入上一轮分析结论 ──
        # 从 state.analysis.summary 取 (结构化字段), 不从消息链尾部找:
        # 消息链尾部是 reporter 的完整报告, 取到报告全文而非 analyst 结论
        prev_analysis = (state.get("analysis", {}) or {}).get("summary", "") or ""
        if not prev_analysis:
            for m in reversed(state.get("messages", [])):
                if isinstance(m, AIMessage) and m.content and not getattr(m, "tool_calls", None):
                    prev_analysis = str(m.content)[:800]
                    break
        if prev_analysis:
            context += "\n[上一轮分析结论]\n" + str(prev_analysis)[:800] + "\n"

        # ── 报告蓝图注入: 明确告知必须覆盖的分析维度 ──
        blueprint = shared.get("report_blueprint")
        if blueprint:
            bp_chapters = blueprint.get("chapters", [])
            dim_list = blueprint.get("dimensions", [])
            metric_list = blueprint.get("metrics", [])

            must_cover = []
            for ch in bp_chapters:
                if ch.get("type") == "group_compare":
                    must_cover.append(f"  对比维度: {ch.get('dimension','?')} ({ch['title']})")
                elif ch.get("type") == "time_series":
                    must_cover.append(f"  趋势: 按{ch.get('grain','月')}维度时间序列")
                elif ch.get("type") == "cross_analysis":
                    dims = ch.get("dimensions", [])
                    must_cover.append(f"  交叉: {' × '.join(dims)}")
                elif ch.get("type") == "year_over_year":
                    must_cover.append(f"  年度对比: {ch['title']}")
                elif ch.get("type") == "top_n":
                    must_cover.append(f"  大单识别: Top 10")

            if must_cover:
                context += "\n[报告蓝图 — 必须全面覆盖]\n"
                context += "分析要求: 以下所有维度都必须分析并出图:\n"
                context += "\n".join(must_cover) + "\n"
                if dim_list:
                    cat_dims = [d for d in dim_list if d.get("type") == "categorical"]
                    if cat_dims:
                        context += f"分类维度 ({len(cat_dims)} 个): " + ", ".join(d["name"] for d in cat_dims) + "\n"
                if metric_list:
                    context += f"核心指标: " + ", ".join(m["name"] for m in metric_list[:4]) + "\n"
                context += "\n"

        inner: dict = {
            "messages": [],
            "user_request": state.get("user_request", ""),
            "analysis_done": False,
            # 子图隔离: shared_context 不进入内层 state, 这里显式注入 gap_fill 补缺需要的字段
            "source_id": state.get("source_id", ""),
            "date_cols": dt_cols,
        }

        source_id = state.get("source_id", "")
        goal_line = f"执行目标: {state.get('current_goal', '')}\n" if state.get("current_goal") else ""

        inner["messages"] = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=(
                f"source_id: {source_id}\n"
                f"(表结构/列名已在 [数据探查报告] 中给出, 无需再查结构。若需了解列的具体取值, 可用 query 采样)\n"
                f"用户请求: {state.get('user_request', '')}\n"
                f"{goal_line}"
                f"{context}"
                + (f"\n[工具约束]\n" + "\n".join(tool_hints) if tool_hints else "")
            )),
        ]
        return inner

    def build_output(self, state: dict, result: dict) -> dict:
        msgs: list = result.get("messages", [])

        # 找最后一条 AIMessage
        analysis_summary = ""
        tool_msgs = []
        for msg in reversed(msgs):
            if isinstance(msg, AIMessage) and msg.content and not msg.tool_calls:
                analysis_summary = str(msg.content)[:2000]
                break
        for msg in msgs:
            if isinstance(msg, ToolMessage):
                tool_msgs.append(msg)

        # 置信度:基于工具质量
        confidence = 0.5
        if analysis_summary and tool_msgs:
            tool_scores = []
            for m in tool_msgs:
                try:
                    data = _safe_parse_content(m.content)
                    if not isinstance(data, (dict, list)):
                        continue  # 字符串数据无法提取分数, 跳过
                    if isinstance(data, dict):
                        if m.name == "forecast" and "slope" in data:
                            tool_scores.append(0.75)
                        elif m.name == "explore" and data.get("groups"):
                            tool_scores.append(0.7)
                        elif m.name == "explore" and data.get("table"):  # cross_tab
                            tool_scores.append(0.75)
                        elif m.name == "explore" and data.get("periods"):  # trend
                            tool_scores.append(0.75)
                        elif m.name == "test_difference" and data.get("conclusion") == "差异显著":
                            tool_scores.append(0.85)  # 有统计检验支撑
                        elif m.name == "attribution" and data.get("significant_features"):
                            tool_scores.append(0.85)  # 控混淆变量的回归
                        elif m.name == "seasonal_analysis" and "seasonality_strength" in data:
                            tool_scores.append(0.75)
                        elif m.name == "explore" and "median" in data:  # describe
                            tool_scores.append(0.6)
                        else:
                            tool_scores.append(0.6)
                except Exception as e:
                    logger.debug(f"Tool quality scoring failed: {e}")
                    tool_scores.append(0.5)
            if tool_scores:
                confidence = sum(tool_scores) / len(tool_scores)
                confidence = min(confidence + 0.1, 0.95)
        elif analysis_summary:
            confidence = 0.4

        # 输入质量影响置信度
        quality_score = state.get("shared_context", {}).get("data_quality_score", 80)
        if isinstance(quality_score, str):
            quality_score = 80
        confidence = round(confidence * max(quality_score / 100.0, 0.3), 2)

        # 自反思降权
        reflect_boost = _check_self_reflection(analysis_summary)
        confidence = max(min(confidence + reflect_boost, 0.95), 0.1)

        # 从工具结果提取结构化图表数据
        chart_data = _extract_chart_data(msgs)

        # 实际生成的图表 (build_chart ToolMessage, 含 echarts_option + 实际标题) —
        # Reporter 图表清单必须基于这份 (标题/编号与前端渲染一致); 规则建议
        # chart_data 无 echarts_option, 只用于 blueprint 章节填充
        charts = _extract_charts_from_msgs(msgs)
        if charts:
            logger.info(f"[Analysis] 实际图表 {len(charts)} 张: {[c['title'][:20] for c in charts]}")

        # 实际生成图表数 (LLM build_chart 成功 + gap_fill 规则补图) — Reporter 图表章节实况
        chart_count = 0
        for m in msgs:
            if isinstance(m, ToolMessage) and m.name == "build_chart":
                try:
                    d = _safe_parse_content(m.content)
                    if isinstance(d, dict) and d.get("echarts_option"):
                        chart_count += 1
                except Exception:
                    continue

        # 结构化发现: 优先用 _synthesize 提取的 findings, 否则回退到 summary 截断
        findings = result.get("findings") or [{"claim": analysis_summary[:500], "evidence": "synthesize", "confidence": confidence}]

        # 结构化工具结果 (仅 Analyst 子图消息, 供 Reporter 使用 —
        # 不扫外层全链, 避免混入 Curator 探查噪音)
        tool_results = []
        for m in msgs:
            if isinstance(m, ToolMessage) and m.name != "build_chart":
                try:
                    d = _safe_parse_content(m.content)
                    if isinstance(d, (dict, list)):
                        tool_results.append({"tool": m.name, "data": d})
                except Exception:
                    continue

        # ── 动态路由建议 ──
        shared = dict(state.get("shared_context", {}))
        # charts 由 chat.py SSE 收集器独占 (echarts_option), 图内不写 shared_context.charts —
        # 规则提取的 chart_data 只作为 analysis 输出字段, 避免污染会话图表 (含无效 echarts_option=None 条目)
        suggest = _suggest_next(state, msgs, tool_msgs)
        if suggest:
            shared["suggest_next"] = suggest
            logger.info(f"[Analysis] 建议下一步: {suggest}")

        # ── 将分析结果注入 report_blueprint ──
        # 用实际渲染的 charts (带 echarts_option, 与聊天内联/报告页一致), 而非规则建议
        # chart_data (无 echarts_option 且含重复) — 报告页图表须与聊天所见完全一致
        blueprint = shared.get("report_blueprint")
        if blueprint and charts:
            try:
                from dia.report.blueprint import merge_analyst_results
                merged = merge_analyst_results(blueprint, {
                    "charts": charts,
                    "findings": findings,
                    "summary": analysis_summary,
                })
                shared["report_blueprint"] = merged
                logger.info(f"[Analysis] blueprint merged: {len(merged.get('chapters',[]))} chapters")
            except Exception as e:
                logger.warning(f"[Analysis] blueprint merge failed: {e}")

        # messages 瘦身: 只回传最终总结 (AI 文本) — 工具/图表消息不回流外层,
        # 避免跨轮无限膨胀 (工具过程实时由 SSE ToolCallEvent 展示)
        display_msgs = [m for m in msgs
                        if isinstance(m, AIMessage) and m.content and not getattr(m, "tool_calls", None)]

        return {
            "messages": display_msgs,
            "analysis": {
                "done": result.get("analysis_done", False),
                "summary": analysis_summary,
                "confidence": confidence,
                "chart_data": chart_data,
                "charts": charts,
                "charts_generated": chart_count,
                "structured_data": {"findings": findings, "tool_results": tool_results},
            },
            # 合并而非覆盖: 保留 Curator 的 glossary/quality/roadmap, 追加 charts + blueprint
            "shared_context": shared,
        }


_analyst = AnalystAgent(name="analysis_agent")


async def analysis_wrapper_node(state: dict, config=None) -> dict:
    return await _analyst.run(state, config)



# ═══════════════════════════════════════════════
# 动态路由建议
# ═══════════════════════════════════════════════

# 用户明确表示不需要完整报告时 → 跳过 reporter
SKIP_REPORT_KEYWORDS = ["只要图表", "只要图", "图表就行", "不用报告", "不需要报告", "不要报告", "简单说", "简要说明"]


def _suggest_next(state: dict, msgs: list, tool_msgs: list) -> dict | None:
    """生成动态路由建议: 跳过报告 / 重探查。无建议返回 None。

    由 Supervisor 消费 (shared_context.suggest_next):
      {"action": "skip",   "target": "reporter"}  → 跳过报告
      {"action": "revisit", "target": "curator"}  → 重置下游并重探查
    """
    user_request = state.get("user_request", "")

    # 1. 用户只要图表/简要结论 → 跳过 reporter
    if any(kw in user_request for kw in SKIP_REPORT_KEYWORDS):
        return {"action": "skip", "target": "reporter", "reason": "用户只要图表/简要结论, 无需完整报告"}

    # 2. 工具大面积失败 → 数据可能有问题, 重新探查
    if tool_msgs:
        error_count = 0
        for m in tool_msgs:
            try:
                d = _safe_parse_content(m.content)
                if isinstance(d, dict) and "error" in d:
                    error_count += 1
            except Exception:
                pass
        if error_count / len(tool_msgs) > 0.6:
            return {"action": "revisit", "target": "curator",
                    "reason": f"{error_count}/{len(tool_msgs)} 工具调用失败, 数据可能无法分析, 需重新探查"}

    return None


# ═══════════════════════════════════════════════
# 置信度辅助函数
# ═══════════════════════════════════════════════

def _check_self_reflection(summary: str) -> float:
    """规则检查结论可靠性,返回置信度调整值"""
    if not summary:
        return -0.3
    low_confidence_words = ["可能", "也许", "似乎", "大概", "不确定", "需要进一步"]
    count = sum(1 for w in low_confidence_words if w in summary)
    return max(-0.05 * count, -0.2)


def _extract_charts_from_msgs(msgs: list) -> list[dict]:
    """从 build_chart ToolMessage 提取实际图表 (含 echarts_option + 标题), 同题图去重.

    去重规则: 标题去空白/标点/括号后小写比较, 相同只保留第一张 —
    LLM 可能对同一角度重复画图 (如 "sales by region" 画两次), 会导致
    报告页/聊天内联出现重复图。
    """
    charts: list[dict] = []
    seen_titles: set[str] = set()
    for m in msgs:
        if not (isinstance(m, ToolMessage) and m.name == "build_chart"):
            continue
        try:
            d = _safe_parse_content(m.content)
        except Exception:
            continue
        if not (isinstance(d, dict) and d.get("echarts_option")):
            continue
        _opt = d.get("echarts_option") or {}
        _opt_title = _opt.get("title")
        title = (d.get("title") or
                 (_opt_title.get("text") if isinstance(_opt_title, dict) else _opt_title) or
                 f"图表{len(charts) + 1}")
        norm = re.sub(r"[\s:：,，、]", "", title).lower()
        # 去掉括号及其内容 (通常为类型标注/副标题, 如 "月度趋势（line）" 与 "月度趋势" 视为同图)
        norm = re.sub(r"\([^)]*\)|（[^）]*）|\[[^\]]*\]|【[^】]*】", "", norm)
        if norm in seen_titles:
            logger.info(f"[Analysis] 图表去重: 跳过重复标题 '{title}'")
            continue
        seen_titles.add(norm)
        charts.append({
            "title": title,
            "chart_type": d.get("chart_type", ""),
            "echarts_option": _opt,
        })
    return charts


def _extract_chart_data(msgs: list) -> list[dict]:
    """从 ToolMessage 提取结构化图表数据, 供 build_chart 直接使用.

    标题用中文业务语言 (metric/group_by 英文列名 → 中文映射), 避免
    gap_fill 补出的图出现 "sales by region" 这类用户看不懂的标题。
    """
    import json

    # 常见列名 → 中文 (gap_fill 补图标题用, 未命中则原样)
    _metric_zh = {
        "sales": "销售额", "revenue": "营收", "cost": "成本", "profit": "利润",
        "orders": "订单量", "customers": "客户数", "price": "价格",
    }
    _dim_zh = {
        "region": "区域", "category": "品类", "channel": "渠道",
        "product": "产品", "date": "日期", "month": "月份",
    }

    def _zh(s: str) -> str:
        return _metric_zh.get(s, _dim_zh.get(s, s))

    chart_suggestions = []
    for m in msgs:
        if not isinstance(m, ToolMessage):
            continue
        try:
            data = json.loads(m.content) if isinstance(m.content, str) else m.content
        except Exception as e:
            logger.debug(f"ToolMessage content parse failed: {e}")
            continue
        if not isinstance(data, dict):
            continue

        # explore aggregate: 主口径是 value (按 agg_func 聚合, 均值型指标=avg),
        # sum 只是附赠字段 — 只读 sum 会把客单价/利润率这类均值指标按求和入图
        if m.name == "explore" and data.get("groups") and "value" in (data["groups"][0] if data["groups"] else {}):
            groups = data["groups"]
            if groups:
                chart_suggestions.append({
                    "chart_type": "bar",
                    "title": f"各{_zh(data.get('group_by',''))}{_zh(data.get('metric',''))}对比",
                    "categories": [g.get("group", "?") for g in groups],
                    "data": [float(g.get("value", g.get("sum", 0))) for g in groups],
                })
        elif m.name == "explore" and data.get("groups") and "pct" in (data["groups"][0] if data["groups"] else {}):
            groups = data["groups"]
            if groups:
                chart_suggestions.append({
                    "chart_type": "pie",
                    "title": f"{_zh(data.get('metric',''))}占比结构",
                    "data": [{"name": g.get("group", "?"), "value": g.get("pct", 0)} for g in groups],
                })
        elif m.name == "explore" and data.get("periods"):  # trend
            chart_suggestions.append({
                "chart_type": "line",
                "title": f"{_zh(data.get('metric',''))}趋势 ({data.get('grain','')})",
                "categories": data["periods"],
                "data": data["values"],
            })
        elif m.name == "explore" and data.get("skewness") is not None:  # describe
            # 分布形态: 分位数 → 箱线图 (用 bar 展示 p5/median/p95 区间概览)
            chart_suggestions.append({
                "chart_type": "bar",
                "title": f"{_zh(data.get('metric',''))}分布 (偏度{data.get('skewness','?')})",
                "categories": ["P5", "P25", "中位数", "P75", "P95"],
                "data": [data.get("p5", 0), data.get("p25", 0), data.get("median", 0),
                         data.get("p75", 0), data.get("p95", 0)],
            })
        elif m.name == "explore" and data.get("table") and isinstance(data.get("table"), dict):  # cross_tab 交叉表
            # 双维度交叉: table = {行维度: {列维度: 值}} → heatmap
            matrix = data.get("table") or {}
            row_names = list(matrix.keys())[:10]
            col_names = sorted({str(c) for r in matrix.values() for c in (r or {}).keys()})[:10]
            values = [[float((matrix.get(r) or {}).get(c, 0)) for c in col_names] for r in row_names]
            if row_names and col_names:
                chart_suggestions.append({
                    "chart_type": "heatmap",
                    "title": f"{_zh(data.get('row_dim',''))}×{_zh(data.get('col_dim',''))}交叉",
                    "x": row_names, "y": col_names, "values": values,
                })
        elif m.name == "forecast" and "predictions" in data:
            chart_suggestions.append({
                "chart_type": "line",
                "title": f"{_zh(data.get('metric',''))}预测",
                "categories": [f"T+{i+1}" for i in range(len(data["predictions"]))],
                "data": data["predictions"],
            })
        elif m.name == "test_difference" and data.get("pairs"):
            pairs = data["pairs"]
            if pairs:
                p = pairs[0]  # 最大 vs 次大
                chart_suggestions.append({
                    "chart_type": "bar",
                    "title": f"{_zh(data.get('metric',''))}对比 (p={p.get('p_value_adjusted', p.get('p_value', '?'))})",
                    "categories": [p.get("group_a", "?"), p.get("group_b", "?")],
                    "data": [p.get("mean_a", 0), p.get("mean_b", 0)],
                })
        elif m.name == "compare" and data.get("current_period_value") is not None:
            # 环比/同比: 上期 vs 本期
            chart_suggestions.append({
                "chart_type": "bar",
                "title": f"{_zh(data.get('metric',''))}{data.get('period','')}对比",
                "categories": ["上期", "本期"],
                "data": [data.get("prev_period_value", 0), data.get("current_period_value", 0)],
            })
    return chart_suggestions[:12]


async def _synthesize(state: dict) -> dict:
    """结构化总结:从工具结果中提取关键发现 + 结构化 findings.

    兜底补图: 分析工具已产出可画数据但一张图都没出 (LLM 忘记/轮次耗尽) 时,
    用规则从工具结果提取图表数据补上, 保证"分析必须出图"的硬性要求.
    """
    msgs = state.get("messages", [])
    user_request = state.get("user_request", "")
    base_count = len(msgs)  # 兜底补图会 append, 返回时只带新增部分 (reducer 会追加)

    # ── 兜底补图 ──
    chart_count = sum(1 for m in msgs if isinstance(m, ToolMessage) and m.name == "build_chart")
    fallback_charts: list[dict] = []
    if chart_count == 0:
        fallback_charts = _extract_chart_data(msgs)
        if fallback_charts:
            logger.info(f"[Analysis] 兜底补图: 分析无图表, 规则提取 {len(fallback_charts)} 张")
            # 把兜底图表作为 build_chart 消息注入, 供 chat.py 收集到 shared_context.charts
            from dia.tools.output import build_chart as _bc
            import uuid as _uuid
            for c in fallback_charts:
                try:
                    ctype = c.get("chart_type", "bar")
                    if ctype == "pie":
                        data_arg = {"data": c.get("data", [])}
                    else:
                        data_arg = {"categories": c.get("categories", []),
                                    "series": [{"name": c.get("title", ""), "data": c.get("data", [])}]}
                    raw = _bc.invoke({
                        "chart_type": ctype,
                        "data": data_arg,
                        "title": c.get("title", ""),
                    })
                    msgs.append(ToolMessage(content=raw, tool_call_id=str(_uuid.uuid4()), name="build_chart"))
                except Exception as e:
                    logger.debug(f"[Analysis] 兜底补图失败: {e}")

    tool_results = []
    parsed_tools: list[dict] = []
    for m in msgs:
        if isinstance(m, ToolMessage) and m.name != "build_chart":  # 图表消息不进 findings
            try:
                data = _safe_parse_content(m.content)
                if not isinstance(data, (dict, list)):
                    data = {"raw": str(m.content)[:500]}  # 转为安全 dict
                tool_results.append(f"### {m.name}\n{json.dumps(data, ensure_ascii=False, default=str)[:600]}")
                parsed_tools.append({"name": m.name, "data": data})
            except Exception as e:
                logger.debug(f"Tool result parse failed: {e}")

    if not tool_results and not fallback_charts:
        return {"messages": [AIMessage(content="分析完成.")], "analysis_done": True, "findings": []}

    # 从工具结果中提取结构化 findings
    findings = _extract_findings(parsed_tools)

    llm = await get_llm(temperature=0.2)
    prompt = f"""根据以下工具执行结果, 输出关键发现 (每条一行).

用户需求: {user_request}

工具结果:
{chr(10).join(tool_results)}

要求:
- 只列事实, 每条带具体数字
- 每条结论用 [强]/[弱]/[推测] 分级: [强]=有统计检验支撑(p<0.05), [弱]=仅描述性观察, [推测]=外推/估计
- 不超过 10 行. 不要写完整报告或建议."""
    try:
        resp = await llm.ainvoke([SystemMessage(content=prompt)])
        summary = resp.content.strip()[:800]
    except Exception as e:
        logger.debug(f"Reflection parse failed: {e}")
        parts = []
        for m in msgs:
            if isinstance(m, AIMessage) and m.content and not m.tool_calls:
                parts.append(m.content)
        summary = "\n\n".join(parts)[:2000] if parts else "分析完成."

    # ── 兜底 findings 提取: 从 summary 文本解析 [强]/[弱]/[推测] 行
    if not findings and summary:
        import re as _re
        for line in summary.split("\n"):
            line = line.strip()
            m = _re.match(r"^\[?(强|弱|推测)\]?\s*(.+)", line)
            if m:
                findings.append({
                    "claim": m.group(2)[:200],
                    "level": m.group(1),
                    "evidence": "synthesize",
                    "confidence": 0.75 if m.group(1) == "强" else 0.5,
                })

    return {
        # 只返回新增消息: 兜底补图的 build_chart + 总结 (历史消息由 reducer 保留, 不重复)
        "messages": msgs[base_count:] + [AIMessage(content=summary)],
        "analysis_done": True,
        "findings": findings,  # v4: 结构化发现
    }


def _extract_findings(parsed_tools: list[dict]) -> list[dict]:
    """从工具结果中提取结构化的分析发现"""
    findings = []
    for pt in parsed_tools:
        name = pt["name"]
        data = pt["data"]
        if not isinstance(data, dict):
            continue

        if name == "query":
            # query 工具返回通用结果, 提取关键统计
            cols = data.get("columns", [])
            row_count = data.get("row_count", 0)
            stats = data.get("stats", {})
            if stats:
                claim_parts = []
                for col, s in list(stats.items())[:3]:
                    claim_parts.append(f"{col}: avg={s.get('mean','?')}, max={s.get('max','?')}")
                claim = f"查询返回{row_count}行, " + "; ".join(claim_parts)
                findings.append({"claim": claim, "evidence": "query", "confidence": 0.7, "actionable": True})

        elif name == "explore" and isinstance(data, dict):
            # aggregate: 分组对比 (主口径 value, 均值型指标=avg — 勿用 sum)
            if data.get("groups") and "value" in (data["groups"][0] if data["groups"] else {}):
                groups = data["groups"]
                if groups:
                    top, bot = groups[0], groups[-1] if len(groups) >= 2 else groups[0]
                    claim = (f"{data.get('group_by','?')}={top.get('group','?')}: {data.get('metric','?')}={top.get('value', top.get('sum','?'))}"
                             f", {data.get('group_by','?')}={bot.get('group','?')}: {data.get('metric','?')}={bot.get('value', bot.get('sum','?'))}")
                    findings.append({"claim": claim, "evidence": "explore:aggregate",
                                     "confidence": 0.7, "actionable": True})
            # trend: 时间趋势
            elif data.get("periods"):
                claim = (f"{data.get('metric','?')} 趋势{data.get('trend_direction','?')}: "
                         f"最近期 {data.get('current_period_value','?')} vs 上期 {data.get('prev_period_value','?')}"
                         f" (变动 {data.get('change_pct','?')}%)")
                findings.append({"claim": claim, "evidence": "explore:trend",
                                 "confidence": 0.7, "actionable": True})
            # share: 构成占比
            elif data.get("groups") and "pct" in (data["groups"][0] if data["groups"] else {}):
                groups = data["groups"]
                if groups:
                    top = groups[0]
                    claim = f"{top.get('group','?')} 占{top.get('pct','?')}% (共{len(groups)}组)"
                    findings.append({"claim": claim, "evidence": "explore:share",
                                     "confidence": 0.7, "actionable": True})
            # describe: 分布
            elif "median" in data:
                shape = data.get("distribution_shape", "?")
                top20 = data.get("top20_concentration", 0)
                claim = (f"{data.get('metric','?')} 分布{shape}, top20%占{top20}%")
                findings.append({"claim": claim, "evidence": "explore:describe",
                                 "confidence": 0.6, "actionable": top20 > 60})
            # cross_tab: 交叉表 (找最大组合)
            elif data.get("table"):
                table = data["table"]
                best = None
                for row_key, col_map in table.items():
                    for col_key, val in col_map.items():
                        if best is None or val > best[2]:
                            best = (row_key, col_key, val)
                if best:
                    claim = f"交叉最大组合: {data.get('row_dim','?')}={best[0]} × {data.get('col_dim','?')}={best[1]} = {best[2]}"
                    findings.append({"claim": claim, "evidence": "explore:cross_tab",
                                     "confidence": 0.7, "actionable": True})

        elif name == "forecast" and "predictions" in data:
            pred = data["predictions"]
            claim = f"{data.get('metric','')}: {data.get('trend','')}, 预测值为 {pred}"
            findings.append({"claim": claim, "evidence": "forecast", "confidence": 0.75, "actionable": True})

        elif name == "test_difference" and data.get("pairs"):
            # 统计显著性检验: 差异是否真实
            pairs = data["pairs"]
            if pairs:
                for p in pairs[:2]:
                    sig = "显著" if p.get("significant") else "不显著"
                    claim = (f"{data.get('group_by','?')} {p.get('group_a','?')} vs {p.get('group_b','?')}: "
                             f"均值 {p.get('mean_a','?')} vs {p.get('mean_b','?')}, 差异{sig} "
                             f"(p={p.get('p_value_adjusted', p.get('p_value','?'))}, 效应量={p.get('effect_size','?')})")
                    findings.append({"claim": claim, "evidence": "test_difference",
                                     "confidence": 0.85 if p.get("significant") else 0.5, "actionable": True})

        elif name == "attribution" and "coefficients" in data:
            # 归因: 控混淆变量后的真实驱动因素
            sig_feats = [c for c in data["coefficients"] if c.get("significant")]
            if sig_feats:
                for c in sig_feats[:3]:
                    claim = (f"控制其他变量后, {c.get('feature','?')} 对 {data.get('target','?')} 影响显著 "
                             f"(系数={c.get('coefficient','?')}, p={c.get('p_value','?')})")
                    findings.append({"claim": claim, "evidence": "attribution",
                                     "confidence": 0.85, "actionable": True})
            elif data.get("coefficients"):
                claim = f"回归 R²={data.get('r_squared','?')}, 无显著特征"
                findings.append({"claim": claim, "evidence": "attribution", "confidence": 0.6, "actionable": False})

        elif name == "seasonal_analysis" and "seasonality_strength" in data:
            strength = data.get("seasonality_strength", 0)
            level = data.get("seasonality_level", "弱")
            trend = data.get("trend_direction", "平稳")
            claim = (f"{data.get('metric','?')} 波动{level}由季节性驱动(强度={strength}), "
                     f"趋势{trend}")
            findings.append({"claim": claim, "evidence": "seasonal_analysis",
                             "confidence": 0.75 if strength > 0.25 else 0.6, "actionable": True})

        elif name == "compare" and data.get("change_pct") is not None:
            sig = data.get("significant")
            sig_txt = ("显著" if sig else "不显著") if sig is not None else "描述性"
            p_txt = f", p={data.get('p_value')}" if data.get("p_value") is not None else ""
            claim = (f"{data.get('metric','?')} {data.get('period','?')}环比 "
                     f"{data.get('change_pct','?')}% ({sig_txt}{p_txt}): "
                     f"{data.get('prev_period_value','?')} → {data.get('current_period_value','?')}")
            findings.append({"claim": claim, "evidence": "compare",
                             "confidence": 0.8 if sig else 0.5, "actionable": True})

        elif name == "detect" and isinstance(data, dict) and data.get("count"):
            cnt = data.get("count", 0)
            drift = [a for a in data.get("anomalies", []) if a.get("level") == "drift"]
            spike = cnt - len(drift)
            claim = f"检测到 {cnt} 个异常 ({spike} 个尖峰"
            if drift:
                d = drift[0]
                claim += f", 1 段{('上升' if d.get('direction')=='up' else '下降')}漂移 {d.get('total_change_pct','?')}%"
            claim += ")"
            findings.append({"claim": claim, "evidence": "detect",
                             "confidence": 0.7, "actionable": True})

    return findings[:8]

