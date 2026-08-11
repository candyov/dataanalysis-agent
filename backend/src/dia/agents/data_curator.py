"""DataCurator — 专业数据分析师风格的数据探查 Agent

设计理念:
- LLM 是数据探查分析师，工具是它的眼和手
- 产出完整的语义层文档，让下游 Analyst 直接可用
- 六个模块: 问题确认 → 数据概览 → 质量分层 → KPI树 → 分析路径 → 风险标注
"""

from __future__ import annotations

import json
import logging
import re
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages

from dia.core.base import BaseAgent, get_llm
from dia.core.config import settings

logger = logging.getLogger(__name__)


class CuratorInternalState(TypedDict, total=False):
    """Curator 子图 ReAct 状态。

    messages 必须用 add_messages reducer (Annotated) — 不能用裸 dict:
    裸 dict 下每个节点返回 {"messages": [...]} 是覆盖而非追加,
    ToolNode 的 ToolMessage 会冲掉 agent 的 AI(tool_calls) 消息,
    LLM 收到孤悬的 tool 消息链报 400 (Messages with role 'tool' must be...).

    user_request / source_id 由 extract_input 注入, fill 节点补调探查时使用
    (子图隔离, 读不到外层 state).
    """
    messages: Annotated[list[BaseMessage], add_messages]
    _done: bool
    _skip: bool
    user_request: str
    source_id: str

# ══════════════════════════════════════════════════════════════════
#  System Prompt
# ══════════════════════════════════════════════════════════════════

CURATOR_PROMPT = """你是资深数据探查分析师。拿到一个数据源和用户问题后，像一个有经验的分析师一样去探索数据。

## 核心原则

1. **先理解问题，再看数据** — 不要上来就 inspect。先想: 用户到底想知道什么？什么口径？数据能回答吗？
2. **看真数据，不只结构** — inspect 给列名和采样值。列名叫"br_qty" → 看采样实际值才能推断含义
3. **质量评估要分影响** — 不是报"缺失15%"，而是判断: revenue 缺15%→致命, email 缺50%→无所谓
4. **KPI 要分层设计** — 不只列数值列，而是设计基础→效率→趋势的多层指标树，含衍生公式
5. **分析路径要递进** — 不是一句话"建议下钻"，而是多轮递进计划，每轮说清用什么工具、预期产出什么

## 可用工具

| 工具 | 用途 | 何时用 |
|---|---|---|
| inspect | 结构+采样+列语义推断 (depth: structure/sample/full) | 探查起点。列名模糊→depth=sample 看真实值; 全面分析→depth=full 拿角色推断 |
| assess_quality | 数据质量检查(缺失/重复/异常/零值) | 需要评估数据可信度时 |
| date_range | 检测日期列的时间跨度和粒度 | 发现日期列时, 判断能否做趋势/环比 |

## 探查策略 (自主决策)

**不要按固定顺序跑完所有工具。** 每一步都基于上一步的发现决定下一步:

- 先 inspect(depth=structure) 了解结构 → 根据结果判断:
  - 列名模糊/有编码? → inspect(depth=sample) 看真实值
  - 有日期列? → date_range 看时间跨度和粒度 (决定下游能否做趋势/环比)
  - 数据够不够? 质量行不行? → assess_quality
  - 需要列角色/数值分布? → inspect(depth=full)
- **探查深度由用户请求决定**:
  - 用户只是"看看数据/有什么字段" → 浅探查, inspect(structure) 即可
  - 一般分析请求 → 标准深度, inspect(full) + assess_quality
  - 归因/诊断/对比类 → 深探查, 加上 date_range, 确保下游有足够信息
- 发现数据有问题(如严重缺失)时,及时停止并报告,不要继续无效探查
- 工具调用不超过 8 次 (可一轮批量发多个), 够用即可, 不要为调而调

## 产出格式

最终回复中输出结构化报告，每个 section 用 `[SECTION_NAME]` 标记:

```report
[CONFIRM]
用户问题: (复述用户问题)
我的理解: (你对问题的解读，包括分析目的、预期结论类型)
口径定义: (关键指标的口径，如 营收=SUM(revenue)、环比=(本月-上月)/上月)
数据能回答: (列出数据能够回答的具体问题)
数据无法回答: (列出数据无法回答的问题，并说明缺失什么)

[DATA_OVERVIEW]
表清单: (N个表，各多少行，主表是哪个)
时间跨度: (有日期列时: 从X到Y，粒度是月/日)
采样发现:
- (看了真实数据后的发现: 列的值域、格式、编码含义)
- (例如 region 列有 East/West/South/North，还有3%的 Unknown)
- (例如 br_qty 取值100/200/500，推断是批量数量)

[QUALITY]
综合等级: A/B/C/D
阻塞性问题 (影响分析结论准确性的):
- (问题描述) → (影响) → (建议处理方式)
降级问题 (可接受，但不完美):
- (问题描述) → (为何可接受)
非问题 (可忽略):
- (问题描述) → (为何无关)

[KPI_TREE]
基础指标:
  revenue|总营收|sum|原始列|✓
  cost|成本|sum|原始列|✓
效率指标:
  unit_price|客单价|avg|衍生:revenue/quantity|✓ 具备各列
  profit_margin|利润率|avg|衍生:(revenue-cost)/revenue|✓
趋势指标:
  mom_growth|环比增长率|pct_change|衍生:(本月-上月)/上月|✓ 有日期列
结构指标:
  revenue_share|营收占比|ratio|衍生:区域营收/总营收|✓
不可得:
  customer_ltv|客户LTV|avg|需:客户ID+多次购买|✗ 无客户维度

[ROADMAP]
第一轮 (定位问题):
  - 做什么: (简述) → 工具: (工具名) → 预期: (预期产出)
第二轮 (拆解维度):
  - 做什么: (简述) → 工具: (工具名) → 预期: (预期产出)
第三轮 (归因分析):
  - 做什么: (简述) → 工具: (工具名) → 预期: (预期产出)
可做但非必需:
  - (可以做，但不是优先级的分析)
  不可做:
  - (无法做的分析) → 原因: (缺少什么)
```

## 规则

- 每个 section 必须输出，无内容时写"无"
- KPI 名称用英文列名(方便 Analyst 写 SQL)，label 用中文
- 不确定列的含义时标注"(推测)"
- 中文回复
"""

# ══════════════════════════════════════════════════════════════════
#  Output type
# ══════════════════════════════════════════════════════════════════

class CuratorReport:
    __slots__ = ("confirm", "data_overview", "quality", "kpi_tree", "roadmap")

    def __init__(self):
        self.confirm: dict[str, Any] = {}
        self.data_overview: dict[str, Any] = {}
        self.quality: dict[str, Any] = {"grade": "B", "blockers": [], "degraded": [], "irrelevant": []}
        self.kpi_tree: dict[str, Any] = {}
        self.roadmap: dict[str, Any] = {"rounds": [], "optional": [], "impossible": []}

    def to_dict(self) -> dict:
        return {
            "confirm": self.confirm,
            "data_overview": self.data_overview,
            "quality": self.quality,
            "kpi_tree": self.kpi_tree,
            "roadmap": self.roadmap,
        }


# ══════════════════════════════════════════════════════════════════
#  Report parser
# ══════════════════════════════════════════════════════════════════

def _parse_report(text: str) -> CuratorReport:
    """从 LLM 回复中解析 [CONFIRM]/[DATA_OVERVIEW]/[QUALITY]/[KPI_TREE]/[ROADMAP] 六段报告。"""
    report = CuratorReport()

    # 提取 ```report ... ``` 代码块
    m = re.search(r"```report\s*\n(.*?)```", text, re.DOTALL)
    body = m.group(1) if m else text

    sections = _split_sections(body)

    if "CONFIRM" in sections:
        report.confirm = _parse_confirm(sections["CONFIRM"])
    if "DATA_OVERVIEW" in sections:
        report.data_overview = _parse_overview(sections["DATA_OVERVIEW"])
    if "QUALITY" in sections:
        report.quality = _parse_quality(sections["QUALITY"])
    if "KPI_TREE" in sections:
        report.kpi_tree = _parse_kpi_tree(sections["KPI_TREE"])
    if "ROADMAP" in sections:
        report.roadmap = _parse_roadmap(sections["ROADMAP"])

    return report


def _split_sections(body: str) -> dict[str, str]:
    """将报告体按 [SECTION_NAME] 拆分为 dict。"""
    sections = {}
    pattern = re.compile(r"\[(CONFIRM|DATA_OVERVIEW|QUALITY|KPI_TREE|ROADMAP)\]\s*\n")
    matches = list(pattern.finditer(body))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections[m.group(1)] = body[start:end].strip()
    return sections


# ── CONFIRM ──

def _parse_confirm(text: str) -> dict:
    result: dict[str, Any] = {"user_question": "", "understanding": "", "caliber": "", "can_answer": [], "cannot_answer": []}
    current = None
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("用户问题"):
            current = "user_question"
            result[current] = line.split(":", 1)[-1].strip() if ":" in line else ""
        elif line.startswith("我的理解"):
            current = "understanding"
            result[current] = line.split(":", 1)[-1].strip() if ":" in line else ""
        elif line.startswith("口径"):
            current = "caliber"
            result[current] = line.split(":", 1)[-1].strip() if ":" in line else ""
        elif line.startswith("数据能回答"):
            current = "can_answer"
        elif line.startswith("数据无法回答"):
            current = "cannot_answer"
        elif line.startswith(("-", "*")) and current in ("can_answer", "cannot_answer"):
            item = line.lstrip("-* ").strip()
            if item:
                result[current].append(item)
        elif current in ("understanding", "caliber"):
            result[current] += " " + line
    return result


# ── DATA_OVERVIEW ──

def _parse_overview(text: str) -> dict:
    result: dict[str, Any] = {"tables": "", "time_span": "", "findings": []}
    current = None
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("表清单"):
            current = "tables"
            result["tables"] = line.split(":", 1)[-1].strip() if ":" in line else ""
        elif line.startswith("时间跨度"):
            current = "time_span"
            result["time_span"] = line.split(":", 1)[-1].strip() if ":" in line else ""
        elif line.startswith("采样发现") or line.startswith("发现"):
            current = "findings"
        elif line.startswith(("-", "*")) and current == "findings":
            item = line.lstrip("-* ").strip()
            if item:
                result["findings"].append(item)
    return result


# ── QUALITY ──

def _parse_quality(text: str) -> dict:
    grade_map = {"A": "A", "B": "B", "C": "C", "D": "D"}
    result: dict[str, Any] = {"grade": "B", "blockers": [], "degraded": [], "irrelevant": []}

    grade_match = re.search(r"等级[：:]\s*([A-D])", text)
    if grade_match:
        result["grade"] = grade_match.group(1)

    current = None
    for line in text.split("\n"):
        line = line.strip()
        if "阻塞" in line:
            current = "blockers"
        elif "降级" in line:
            current = "degraded"
        elif "非问题" in line or "忽略" in line:
            current = "irrelevant"
        elif line.startswith(("-", "*")) and current:
            item = line.lstrip("-* ").strip()
            if item:
                if len(result[current]) < 10:
                    result[current].append(item)
    return result


# ── KPI_TREE ──

def _parse_kpi_tree(text: str) -> dict:
    """解析分层 KPI 树: 基础/效率/趋势/结构/不可得。"""
    result: dict[str, Any] = {
        "基础指标": [], "效率指标": [], "趋势指标": [], "结构指标": [], "不可得": [],
    }
    current = None
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("基础"):
            current = "基础指标"
        elif line.startswith("效率"):
            current = "效率指标"
        elif line.startswith("趋势"):
            current = "趋势指标"
        elif line.startswith("结构"):
            current = "结构指标"
        elif line.startswith("不可得"):
            current = "不可得"
        elif current and "|" in line:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 4:
                result[current].append({
                    "name": parts[0],
                    "label": parts[1] if len(parts) > 1 else parts[0],
                    "agg": parts[2] if len(parts) > 2 else "sum",
                    "source": parts[3] if len(parts) > 3 else "",
                    "computable": parts[4] if len(parts) > 4 else "?",
                })
    return result


# ── ROADMAP ──

def _parse_roadmap(text: str) -> dict:
    result: dict[str, Any] = {"rounds": [], "optional": [], "impossible": []}
    current_round = None
    current_section = None  # "rounds", "optional", or "impossible"

    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue

        # 检测轮次标题
        round_match = re.match(r"第[一二三四五六七八]轮", line)
        if round_match:
            current_section = "rounds"
            current_round = {"title": line.strip("(): "), "steps": []}
            result["rounds"].append(current_round)
            continue

        # 检测 section 切换
        if "可做但非必需" in line or "可选" in line:
            current_section = "optional"
            current_round = None
            continue
        if "不可做" in line:
            current_section = "impossible"
            current_round = None
            continue

        # 收集列表项
        if line.startswith(("-", "*")):
            item = line.lstrip("-* ").strip()
            if not item:
                continue
            if current_section == "rounds" and current_round is not None:
                current_round["steps"].append(item)
            elif current_section == "optional":
                result["optional"].append(item)
            elif current_section == "impossible":
                result["impossible"].append(item)

    return result


# ══════════════════════════════════════════════════════════════════
#  Side effects — KPI registration + snapshot
# ══════════════════════════════════════════════════════════════════

def _register_and_snapshot_kpis(kpi_tree: dict, source_id: str):
    """将所有可计算的基础/效率/趋势/结构指标注册到 Metric Store 并写快照。"""
    all_kpis = []
    for tier in ("基础指标", "效率指标", "趋势指标", "结构指标"):
        for k in kpi_tree.get(tier, []):
            if k.get("computable", "") == "✓":
                all_kpis.append(k)
    if not all_kpis or not source_id:
        return

    # 注册到 Metric Store
    try:
        from dia.engine.metrics import init_metric_store
        init_metric_store()

        import sqlite3
        from datetime import datetime
        conn = sqlite3.connect(str(settings.STORAGE_DIR / "metric_store.db"))
        conn.row_factory = sqlite3.Row
        for kpi in all_kpis:
            conn.execute(
                """INSERT OR REPLACE INTO metrics (name, label, source_id, column_name, agg_func, formula, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (kpi.get("name", ""), kpi.get("label", ""), source_id,
                 kpi.get("name", ""), kpi.get("agg", "sum"), kpi.get("source", ""),
                 datetime.now().isoformat()),
            )
        conn.commit()
        conn.close()
        logger.info(f"[Curator] 注册 {len(all_kpis)} 个 KPI")
    except Exception as e:
        logger.warning(f"[Curator] KPI 注册失败: {e}")

    # 写初始快照 (聚合值, 不取第一行 — 第一行是任意行, 无业务意义)
    try:
        from dia.infrastructure.database.manager import get_datasource_manager
        from dia.engine.metrics import snapshot_metrics
        mgr = get_datasource_manager()
        conn = mgr.connect(source_id)
        tables = conn.list_tables()
        if tables:
            schema = conn.get_schema()
            main_table = max(tables, key=lambda t: schema.get(t, {}).get("row_count", 0))
            real_cols = {c["name"] for c in schema.get(main_table, {}).get("columns", [])}
            agg_sql = {"sum": "SUM", "avg": "AVG", "count": "COUNT"}
            agg_parts = [f"{agg_sql.get(kpi.get('agg', 'sum'), 'SUM')}({name}) AS {name}"
                         for kpi in all_kpis[:20]
                         if kpi.get("name") in real_cols]
            if agg_parts:
                result = conn.query(f"SELECT {', '.join(agg_parts)} FROM {main_table}", max_rows=None)
                if result.get("rows"):
                    row = result["rows"][0]
                    snap = {}
                    for k, v in row.items():
                        if v is None:
                            continue
                        try:
                            snap[k] = float(v)
                        except (ValueError, TypeError):
                            pass
                    if snap:
                        snapshot_metrics(snap, source_id)
    except Exception as e:
        logger.debug(f"[Curator] 快照写入跳过: {e}")


# ══════════════════════════════════════════════════════════════════
#  Fallback parsers (LLM 未按格式输出时)
# ══════════════════════════════════════════════════════════════════

def _infer_role(col_name: str, sql_type: str) -> str:
    name = col_name.lower()
    if name in ("id", "_id") or name.endswith(("_id", "_key")):
        return "identifier"
    if any(kw in name for kw in ("date", "time", "day", "month", "year", "dt", "timestamp")):
        return "datetime"
    sql_upper = sql_type.upper()
    if any(t in sql_upper for t in ("INT", "REAL", "FLOAT", "DOUBLE", "NUMERIC", "DECIMAL")):
        return "metric"
    if any(kw in name for kw in ("region", "area", "city", "category", "type",
                                 "channel", "brand", "status", "level", "grade",
                                 "segment", "store", "zone", "district",
                                 "区域", "地区", "城市", "品类", "类别", "类型", "渠道",
                                 "品牌", "状态", "等级", "门店", "分区", "部门")):
        return "dimension"
    return "description"


def _fallback_glossary(msgs: list) -> dict:
    col_entry = re.compile(r"([^\s()|]+)\((\w+(?:\([^)]*\))?)\)")
    glossary = {}
    for m in msgs:
        if not isinstance(m, ToolMessage) or m.name != "inspect":
            continue
        for line in str(m.content).split("\n"):
            if not line.strip().startswith("列:"):
                continue
            for match in col_entry.finditer(line):
                name = match.group(1)
                if name not in glossary:
                    glossary[name] = {"name": name, "label": name,
                                      "role": _infer_role(name, match.group(2)),
                                      "sql_type": match.group(2).upper(), "description": ""}
    return glossary


def _fallback_quality(msgs: list) -> dict:
    """assess_quality 结果兜底 → 质量分层.

    高影响列 (revenue/销售额等) 的问题进 blockers, 其余进 degraded;
    无任何高影响标记时全部视为阻塞 (保底).
    """
    for m in msgs:
        if not isinstance(m, ToolMessage) or m.name != "assess_quality":
            continue
        try:
            data = json.loads(m.content) if isinstance(m.content, str) else m.content
            if isinstance(data, dict):
                issues = data.get("issues", [])
                blockers = [i for i in issues if "高影响" in str(i)]
                degraded = [i for i in issues if "高影响" not in str(i) and "重复" not in str(i)]
                if not blockers and issues:
                    blockers = issues
                    degraded = []
                return {
                    "grade": data.get("quality_grade", "B"),
                    "blockers": blockers, "degraded": degraded, "irrelevant": [],
                }
        except (json.JSONDecodeError, Exception):
            gm = re.search(r"[A-D]", str(m.content))
            if gm:
                return {"grade": gm.group(0), "blockers": [], "degraded": [], "irrelevant": []}
    return {"grade": "B", "blockers": [], "degraded": [], "irrelevant": []}


def _fallback_kpis(glossary: dict) -> dict:
    metrics = []
    for name, entry in glossary.items():
        if entry.get("role") == "metric":
            metrics.append({"name": name, "label": entry.get("label", name),
                           "agg": "sum", "source": "原始列", "computable": "✓"})
    return {"基础指标": metrics, "效率指标": [], "趋势指标": [], "结构指标": [], "不可得": []}


# ══════════════════════════════════════════════════════════════════
#  Agent
# ══════════════════════════════════════════════════════════════════

class DataCuratorAgent(BaseAgent):
    """语义层构建 Agent — 专业数据分析师风格的探查。"""

    def __init__(self, name: str = "curator"):
        super().__init__(name)
        self.max_retries = 1

    def build_graph(self):
        from dia.tools import CURATOR_TOOLS as tools
        self._tools = tools

        g = StateGraph(CuratorInternalState)
        g.add_node("agent", self._agent_node)
        g.add_node("tools", self._serial_tool_node)
        g.add_node("fill", self._fill_node)      # 代码补调缺失的关键探查
        g.add_node("synthesize", self._synthesize_node)
        g.add_edge(START, "agent")
        g.add_conditional_edges("agent", self._route, {"tools": "tools", "fill": "fill", "synthesize": "synthesize"})
        g.add_edge("tools", "agent")
        g.add_edge("fill", "synthesize")
        g.add_edge("synthesize", END)
        return g.compile()

    async def _agent_node(self, state: dict, config=None) -> dict:
        llm = await get_llm(temperature=0.1)
        llm_with_tools = llm.bind_tools(self._tools)
        messages = list(state.get("messages", []))
        response = await llm_with_tools.ainvoke(messages)
        result: dict[str, Any] = {"messages": [response]}
        if not response.tool_calls:
            result["_done"] = True
        return result

    async def _serial_tool_node(self, state: dict) -> dict:
        """串行执行最后一条 AI 的全部 tool_calls.

        替代 stock ToolNode (asyncio.gather 并行): 多工具调用并发争用同一
        MySQL 连接会互相干扰, 逐条执行保连接安全; 顺带省掉逐轮等待.
        """
        msgs = list(state.get("messages", []))
        last = msgs[-1] if msgs else None
        if not isinstance(last, AIMessage) or not last.tool_calls:
            return {"messages": []}
        tool_map = {t.name: t for t in self._tools}
        new_msgs = []
        for tc in last.tool_calls:
            name, args, tc_id = tc.get("name", ""), tc.get("args", {}), tc.get("id", "")
            tool = tool_map.get(name)
            if tool is None:
                new_msgs.append(ToolMessage(content=f"未知工具: {name}", tool_call_id=tc_id, name=name))
                continue
            try:
                result = await tool.ainvoke(args) if hasattr(tool, "ainvoke") else tool.invoke(args)
                content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
            except Exception as e:
                content = json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
            new_msgs.append(ToolMessage(content=content, tool_call_id=tc_id, name=name))
        return {"messages": new_msgs}

    async def _fill_node(self, state: dict) -> dict:
        """代码补调缺失的关键探查 (LLM 想提前结束时兜底, 不靠 LLM 自觉).

        探查可枚举: inspect 结构 → assess_quality 质量 → (需要时间维度时) date_range.
        """
        msgs = list(state.get("messages", []))
        executed = {m.name for m in msgs if isinstance(m, ToolMessage)}
        source_id = state.get("source_id", "")
        req = str(state.get("user_request", ""))
        new_msgs = []
        if "inspect" not in executed:
            new_msgs.append(await self._invoke_tool("inspect", {"source_id": source_id, "depth": "structure"}))
        if "assess_quality" not in executed:
            new_msgs.append(await self._invoke_tool("assess_quality", {"source_id": source_id}))
        if "date_range" not in executed and any(k in req for k in ("趋势", "预测", "环比", "时间", "月度", "走势")):
            new_msgs.append(await self._invoke_tool("date_range", {"source_id": source_id}))
        if new_msgs:
            logger.info(f"[Curator] fill: 补调缺失探查 {[m.name for m in new_msgs]}")
        return {"messages": new_msgs}

    async def _invoke_tool(self, name: str, args: dict) -> ToolMessage:
        import json as _json
        tool_map = {t.name: t for t in self._tools}
        tool = tool_map.get(name)
        try:
            result = await tool.ainvoke(args) if hasattr(tool, "ainvoke") else tool.invoke(args)
            content = result if isinstance(result, str) else _json.dumps(result, ensure_ascii=False, default=str)
        except Exception as e:
            content = _json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
        return ToolMessage(content=content, tool_call_id=f"fill_{name}", name=name)

    def _route(self, state: dict) -> str:
        msgs = state.get("messages", [])
        tool_invocations = sum(1 for m in msgs if isinstance(m, AIMessage) and getattr(m, "tool_calls", None))
        tool_results = [m for m in msgs if isinstance(m, ToolMessage)]

        if tool_invocations >= 8:
            return "synthesize"
        if len(tool_results) >= 6:
            return "synthesize"

        last = msgs[-1] if msgs else None
        if isinstance(last, AIMessage) and not getattr(last, "tool_calls", None):
            # LLM 想结束 → 检查关键探查覆盖, 缺则代码补调 (fill)
            executed = {m.name for m in tool_results}
            missing = [t for t in ("inspect", "assess_quality") if t not in executed]
            req = str(state.get("user_request", ""))
            if "date_range" not in executed and any(k in req for k in ("趋势", "预测", "环比", "时间", "月度", "走势")):
                missing.append("date_range")
            if missing:
                logger.warning(f"[Curator] 探查不完整 ({missing}) → fill 补调")
                return "fill"
            return "synthesize"

        if tool_results:
            last_tool = tool_results[-1]
            try:
                data = json.loads(last_tool.content) if isinstance(last_tool.content, str) else last_tool.content
                if isinstance(data, dict) and "error" in data:
                    logger.warning(f"[Curator] 工具 {last_tool.name} error → synthesize")
                    return "synthesize"
            except Exception:
                pass
        return "tools"

    async def _synthesize_node(self, state: dict, config=None) -> dict:
        msgs = state.get("messages", [])
        for m in reversed(msgs):
            if isinstance(m, AIMessage) and m.content and not getattr(m, "tool_calls", None):
                return {"messages": []}  # 已有结论

        parts = ["根据以下探查结果, 按六个模块输出完整报告 (```report 代码块)。"]
        parts.append("必须包含: [CONFIRM] [DATA_OVERVIEW] [QUALITY] [KPI_TREE] [ROADMAP]")
        for m in msgs:
            if isinstance(m, ToolMessage):
                parts.append(f"\n[{m.name} 结果]\n{str(m.content)[:2000]}")
            elif isinstance(m, HumanMessage):
                parts.append(f"\n[用户意图]\n{str(m.content)[:800]}")
        llm = await get_llm(temperature=0.2)
        resp = await llm.ainvoke([SystemMessage(content="\n".join(parts))])
        return {"messages": [resp]}

    # ── BaseAgent 接口 ──

    def extract_input(self, state: dict) -> dict:
        source_id = state.get("source_id", "")
        user_request = state.get("user_request", "")

        if not source_id:
            return {"messages": [AIMessage(content="无数据源, 跳过数据准备")], "_done": True, "_skip": True}

        # 探查深度由代码按请求语义判定 (不依赖 LLM 自觉):
        # 时间类请求 → 必查 date_range; 分析/质量类 → 必查 assess_quality
        req = user_request or ""
        must_do = ["inspect 结构"]
        if any(k in req for k in ("趋势", "预测", "环比", "时间", "月度", "走势", "归因", "诊断", "对比", "全面")):
            must_do.append("date_range 时间跨度")
        if any(k in req for k in ("质量", "可信", "脏", "全面", "分析")):
            must_do.append("assess_quality 数据质量")
        depth = "full" if any(k in req for k in ("归因", "诊断", "对比", "全面", "分析")) else "structure"

        return {
            "messages": [
                SystemMessage(content=CURATOR_PROMPT),
                HumanMessage(content=(
                    f"source_id: {source_id}\n"
                    f"(用 inspect(source_id) 查结构。之后根据发现自主决定下一步探查)\n"
                    f"用户请求: {user_request}\n"
                    f"本次必做探查: {', '.join(must_do)} (inspect 建议 depth={depth})\n"
                    f"请开始探查: 先 inspect 了解结构, 然后根据发现自主决定下一步。\n"
                )),
            ],
            # 子图隔离: fill 节点补调探查需要, 显式注入
            "user_request": user_request,
            "source_id": source_id,
            "_done": False, "_skip": False,
        }

    def build_output(self, state: dict, result: dict) -> dict:
        if result.get("_skip"):
            return {"curator": {"done": True, "score": 0, "summary": "无数据源", "kpis": []}}

        msgs = result.get("messages", [])
        final_text = ""
        for m in reversed(msgs):
            if isinstance(m, AIMessage) and m.content and not getattr(m, "tool_calls", None):
                final_text = str(m.content)
                break

        parsed = _parse_report(final_text)

        # Fallbacks
        if not parsed.data_overview:
            parsed.data_overview = {"tables": "见 describe 结果", "time_span": "", "findings": []}
        if not parsed.quality["blockers"] and not parsed.quality["degraded"]:
            parsed.quality = _fallback_quality(msgs)
        if not parsed.kpi_tree.get("基础指标"):
            glossary = _fallback_glossary(msgs)
            parsed.kpi_tree = _fallback_kpis(glossary)

        # 质量分数
        grade_map = {"A": 95, "B": 80, "C": 60, "D": 30}
        score = grade_map.get(parsed.quality.get("grade", "B"), 80)

        # 副作用: 注册 KPI
        source_id = state.get("source_id", "")
        _register_and_snapshot_kpis(parsed.kpi_tree, source_id)

        # 构建 shared_context
        shared = dict(state.get("shared_context", {}))
        # 换数据源 → 清旧语义层 (glossary 混入旧列名会让 analyst 用错列;
        # merge reducer 无法删除字段, 显式置空覆盖)
        if (shared.get("curator_report") or {}).get("source_id") != source_id:
            shared["glossary"] = {}
        shared["data_quality_score"] = score
        shared["quality_report"] = parsed.quality
        report_dict = parsed.to_dict()
        report_dict["source_id"] = source_id  # 多轮复用校验: 换数据源必须重新探查
        shared["curator_report"] = report_dict

        # 提取 KPI 名列表
        kpi_names = []
        for tier in ("基础指标", "效率指标", "趋势指标", "结构指标"):
            for k in parsed.kpi_tree.get(tier, []):
                if k.get("computable", "") == "✓":
                    kpi_names.append(k["name"])
        shared["registered_kpis"] = kpi_names

        # Glossary (从 KPI tree + inspect 工具结果推断)
        glossary = shared.get("glossary", {})
        for kpi in kpi_names:
            if kpi not in glossary:
                glossary[kpi] = {"name": kpi, "label": kpi, "role": "metric"}
        # 从 inspect 结果补充 dimension 列 (采样值 + unique count)
        for m in msgs:
            if not isinstance(m, ToolMessage) or m.name != "inspect":
                continue
            try:
                data = json.loads(m.content) if isinstance(m.content, str) else m.content
            except Exception:
                data = None  # inspect 输出是文本，不是 JSON
            content = str(m.content)
            col_pattern = re.compile(r"(\w+)\((\w+(?:\([^)]*\))?)\)=([^|\n]*)")
            for match in col_pattern.finditer(content):
                col_name = match.group(1)
                if col_name not in glossary:
                    role = _infer_role(col_name, match.group(2))
                    glossary[col_name] = {
                        "name": col_name, "label": col_name,
                        "role": role, "sql_type": match.group(2),
                        "sample_value": match.group(3).strip(),
                    }
        shared["glossary"] = glossary

        # ── 构建 report_blueprint ──
        try:
            from dia.report.blueprint import build_blueprint
            # 整理 inspect 工具结果 (含 date_range 输出)
            inspect_results = []
            for m in msgs:
                if isinstance(m, ToolMessage):
                    try:
                        d = json.loads(m.content) if isinstance(m.content, str) else m.content
                        if isinstance(d, dict):
                            inspect_results.append(d)
                    except Exception:
                        inspect_results.append({"raw": str(m.content)[:500]})

            overview = {
                "tables": parsed.data_overview.get("tables", ""),
                "time_span": parsed.data_overview.get("time_span", ""),
                "row_count": parsed.data_overview.get("row_count", "?"),
                "table_count": 1,
            }
            blueprint = build_blueprint(
                glossary=glossary,
                inspect_results=inspect_results,
                quality=parsed.quality,
                overview=overview,
            )
            shared["report_blueprint"] = blueprint
            logger.info(f"[Curator] report_blueprint: {len(blueprint['chapters'])} 个章节")
        except Exception as e:
            logger.warning(f"[Curator] blueprint 生成失败: {e}")
            shared["report_blueprint_error"] = str(e)[:200]  # 诊断用, Analyst 侧已有自行规划降级

        # summary: 拼接 confirm + overview 首行
        confirm_summary = parsed.confirm.get("understanding", "")
        overview_tables = parsed.data_overview.get("tables", "")
        summary = f"{confirm_summary} | {overview_tables}"[:500]

        # messages 瘦身: 只回传**探查摘要** (3 行: 质量/口径/无法回答), 不推 CONFIRM 全文 —
        # 前端数据准备阶段只需摘要, 探查全文 (CONFIRM/DATA_OVERVIEW/KPI_TREE/ROADMAP)
        # 由 Analyst/Reporter 从 shared_context 消费, 展示给用户只会淹没关键信息.
        display_msgs = []
        try:
            from langchain_core.messages import AIMessage as _AI
            _q = parsed.quality
            _c = parsed.confirm
            summary_text = (
                f"**数据探查完成**（质量 {_q.get('grade', 'B')} 级）\n\n"
                f"- **口径**: {_c.get('caliber', '') or str(_c.get('understanding', ''))[:80]}\n"
                f"- **能回答**: {'；'.join(_c.get('can_answer') or [])[:100] or '见分析报告'}\n"
                f"- **无法回答**: {'；'.join(_c.get('cannot_answer') or [])[:100] or '无'}"
            )
            display_msgs.append(_AI(content=summary_text))
        except Exception as e:
            logger.warning(f"[Curator] 摘要消息构造失败: {e}")

        # 探查结果落盘 (按 source_id): 跨会话复用 — 同数据源再次分析跳过 Curator
        try:
            from dia.infrastructure.persistence.glossary_cache import save_glossary_cache
            save_glossary_cache(source_id, glossary, kpi_names, report_dict)
        except Exception as e:
            logger.warning(f"[Curator] glossary 缓存写入失败 (忽略): {e}")

        return {
            "messages": display_msgs,
            "curator": {"done": True, "score": score, "summary": summary, "kpis": kpi_names},
            "shared_context": shared,
        }


# ══════════════════════════════════════════════════════════════════
#  Module wrapper
# ══════════════════════════════════════════════════════════════════

_curator = DataCuratorAgent(name="curator")


async def curator_node(state: dict, config=None) -> dict:
    return await _curator.run(state, config)
