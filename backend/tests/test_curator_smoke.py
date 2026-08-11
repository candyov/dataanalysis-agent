"""Smoke tests for Curator refactor v2 — 6-module report parser + new tools."""
import ast
import json
import sys
sys.path.insert(0, 'src')


def test_imports():
    from dia.tools import CURATOR_TOOLS, ANALYST_TOOLS
    curator_names = {t.name for t in CURATOR_TOOLS}
    assert "inspect" in curator_names
    assert "assess_quality" in curator_names
    assert "date_range" in curator_names
    # 能力导向: curator 只保留探查工具
    assert "query" not in curator_names
    assert "sample_rows" not in curator_names
    analyst_names = {t.name for t in ANALYST_TOOLS}
    assert "explore" in analyst_names
    assert "test_difference" in analyst_names
    assert "attribution" in analyst_names
    assert "forecast" in analyst_names
    assert "build_chart" in analyst_names


# ══ Report parser ══

def test_parse_report_full():
    from dia.agents.data_curator import _parse_report
    sample = """```report
[CONFIRM]
用户问题: 为什么华东区营收下滑
我的理解: 分析华东区域营收变化趋势，定位下滑时间点和原因
口径定义: 营收=SUM(revenue)，环比=(本月-上月)/上月
数据能回答:
- 各区域/品类/时间维度的营收对比
数据无法回答:
- 客户流失分析: 缺少客户维度

[DATA_OVERVIEW]
表清单: orders 12450行, products 200行, 主表 orders
时间跨度: 2025-01 ~ 2026-06, 按月粒度
采样发现:
- region列含 East/West/South/North/Unknown(3%)
- br_qty取值100/200/500,推断为批量数量

[QUALITY]
综合等级: B
阻塞性问题 (影响分析结论准确性的):
- revenue列15%缺失 → 可能导致总营收偏低 → 建议排除或均值填充
降级问题 (可接受，但不完美):
- email列30%缺失 → 不影响营收分析
非问题 (可忽略):
- notes列为空 → 备注字段

[KPI_TREE]
基础指标:
  revenue|总营收|sum|原始列|✓
  cost|成本|sum|原始列|✓
效率指标:
  unit_price|客单价|avg|衍生:revenue/quantity|✓ 具备各列
  profit_margin|利润率|avg|衍生:(revenue-cost)/revenue|✓
趋势指标:
  mom_growth|环比增长率|pct_change|衍生:(本月-上月)/上月|✓ 有日期列
不可得:
  customer_ltv|客户LTV|avg|需:客户ID|✗ 无客户维度

[ROADMAP]
第一轮 (定位问题):
  - 按月汇总营收，确定下滑时间窗口
第二轮 (拆解维度):
  - 按品类下钻华东各月营收 → drill_down
第三轮 (归因分析):
  - find_drivers找营收下降相关因素
可做但非必需:
  - forecast预测下月趋势
不可做:
  - 竞品对比: 无外部数据
```"""
    r = _parse_report(sample)

    # CONFIRM
    assert "华东区" in r.confirm.get("user_question", "")
    assert len(r.confirm.get("can_answer", [])) >= 1
    assert len(r.confirm.get("cannot_answer", [])) >= 1
    assert "SUM" in r.confirm.get("caliber", "").upper() or "sum" in r.confirm.get("caliber", "")

    # DATA_OVERVIEW
    assert "orders" in r.data_overview.get("tables", "")
    assert len(r.data_overview.get("findings", [])) >= 2

    # QUALITY — three tiers
    assert r.quality["grade"] == "B"
    assert len(r.quality["blockers"]) >= 1
    assert "revenue" in r.quality["blockers"][0]
    assert len(r.quality["degraded"]) >= 1
    assert "email" in r.quality["degraded"][0]
    assert len(r.quality["irrelevant"]) >= 1

    # KPI_TREE — hierarchical
    assert len(r.kpi_tree.get("基础指标", [])) >= 2
    assert len(r.kpi_tree.get("效率指标", [])) >= 2
    assert len(r.kpi_tree.get("趋势指标", [])) >= 1
    assert len(r.kpi_tree.get("不可得", [])) >= 1
    # Derived KPI has formula
    profit = [k for k in r.kpi_tree.get("效率指标", []) if k["name"] == "profit_margin"]
    assert profit and "revenue" in profit[0].get("source", "")
    # Unavailable KPI
    ltv = r.kpi_tree.get("不可得", [])
    assert any("LTV" in k.get("name", "").upper() or "ltv" in k.get("name", "").lower() for k in ltv)

    # ROADMAP — multi-round
    assert len(r.roadmap["rounds"]) >= 3
    assert any("定位" in rd["title"] for rd in r.roadmap["rounds"])
    assert len(r.roadmap["optional"]) >= 1
    assert len(r.roadmap["impossible"]) >= 1


def test_parse_report_empty():
    from dia.agents.data_curator import _parse_report
    r = _parse_report("")
    assert isinstance(r.confirm, dict)
    assert isinstance(r.kpi_tree, dict)
    assert r.quality.get("grade") == "B"


# ══ Fallbacks ══

def test_fallback_glossary():
    from langchain_core.messages import ToolMessage
    from dia.agents.data_curator import _fallback_glossary
    fake = ToolMessage(
        content='数据库 test: 1 个表\n\n  orders: 30 行, 3 列\n    列: id(integer), revenue(real), region(varchar)',
        name='inspect', tool_call_id='t1',
    )
    g = _fallback_glossary([fake])
    assert g['id']['role'] == 'identifier'
    assert g['revenue']['role'] == 'metric'
    assert g['region']['role'] == 'dimension'


def test_fallback_quality():
    from langchain_core.messages import ToolMessage
    from dia.agents.data_curator import _fallback_quality
    fake = ToolMessage(
        content='{"total_rows": 100, "quality_grade": "B", "issues": ["email: 15% missing"]}',
        name='assess_quality', tool_call_id='t2',
    )
    q = _fallback_quality([fake])
    assert q['grade'] == 'B'
    assert 'email' in q['blockers'][0]


def test_fallback_kpis():
    from dia.agents.data_curator import _fallback_kpis
    glossary = {
        'revenue': {'name': 'revenue', 'label': '营收', 'role': 'metric'},
        'region': {'name': 'region', 'label': '区域', 'role': 'dimension'},
    }
    kpis = _fallback_kpis(glossary)
    assert len(kpis['基础指标']) == 1
    assert kpis['基础指标'][0]['name'] == 'revenue'


# ══ New tools ══

def test_assess_quality_works():
    from dia.tools.data import assess_quality
    # 不存在的数据源 → 抛 ValueError (连接层行为)
    try:
        assess_quality.func("nonexistent")
    except ValueError as e:
        assert "数据源不存在" in str(e)


# ══════════════════════════════════════════════════════════════════
#  Curator 修复 (v3) — 批量执行 / 复用校验 / 探查强制 / 质量分层
# ══════════════════════════════════════════════════════════════════

def test_apply_reuse_source_id_check():
    """多轮复用必须校验 source_id: 换数据源不复用旧探查报告"""
    from dia.graph.supervisor import _apply_reuse
    plan = {"steps": [{"agent": "curator", "goal": "x"}, {"agent": "analyst", "goal": "y"}]}

    # 同 source_id → 复用 (跳过 curator)
    out, _ = _apply_reuse(dict(plan), {"curator_report": {"source_id": "src1"}}, "src1")
    assert [s["agent"] for s in out["steps"]] == ["analyst"]

    # 不同 source_id → 不复用 (重新探查)
    out2, _ = _apply_reuse(dict(plan), {"curator_report": {"source_id": "src1"}}, "src2")
    assert [s["agent"] for s in out2["steps"]] == ["curator", "analyst"]

    # 旧 session 无 source_id 字段 → 不复用 (安全方向: 多探查一次而非用错报告)
    out3, _ = _apply_reuse(dict(plan), {"curator_report": {"confirm": {}}}, "src1")
    assert [s["agent"] for s in out3["steps"]] == ["curator", "analyst"]


def test_curator_route_fill_when_exploration_missing():
    """LLM 想提前结束但关键探查缺失 → fill 补调 (代码强制)"""
    from dia.agents.data_curator import DataCuratorAgent
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    agent = DataCuratorAgent()

    state = {"messages": [HumanMessage(content="x"), AIMessage(content="数据没问题")],
             "user_request": "分析数据"}
    assert agent._route(state) == "fill"

    # 探查完整 → synthesize
    state2 = {"messages": [HumanMessage(content="x"), AIMessage(content=""),
                           ToolMessage(content='{}', name="inspect", tool_call_id="t1"),
                           ToolMessage(content='{}', name="assess_quality", tool_call_id="t2"),
                           AIMessage(content="探查完毕")],
              "user_request": "分析数据"}
    assert agent._route(state2) == "synthesize"


def test_extract_input_must_do_injection():
    """探查深度由代码判定并注入必做清单"""
    from dia.agents.data_curator import DataCuratorAgent
    agent = DataCuratorAgent()

    inner = agent.extract_input({"source_id": "s1", "user_request": "全面分析销售趋势"})
    text = inner["messages"][1].content
    assert "date_range" in text and "assess_quality" in text
    assert "depth=full" in text

    inner2 = agent.extract_input({"source_id": "s1", "user_request": "看看有哪些字段"})
    text2 = inner2["messages"][1].content
    assert "date_range" not in text2
    assert "depth=structure" in text2


def test_fallback_quality_high_impact_tiered():
    """assess_quality 兜底: 高影响列问题进 blockers, 普通列进 degraded"""
    from langchain_core.messages import ToolMessage
    from dia.agents.data_curator import _fallback_quality
    fake = ToolMessage(
        content='{"total_rows": 100, "quality_grade": "C", '
                '"issues": ["高影响列 revenue: 15% 缺失", "email: 50% 缺失", "3 行重复"]}',
        name="assess_quality", tool_call_id="t2")
    q = _fallback_quality([fake])
    assert q["grade"] == "C"
    assert any("revenue" in b for b in q["blockers"])
    assert any("email" in d for d in q["degraded"])
    assert not any("重复" in b for b in q["blockers"])


def test_assess_quality_full_scan():
    """质量评估全量扫描: 500 行截断会漏掉尾部缺失"""
    import sqlite3, tempfile, os as _os
    fd, db = tempfile.mkstemp(suffix=".db"); _os.close(fd)
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t (revenue REAL)")
    c.executemany("INSERT INTO t VALUES (?)", [(100.0,)] * 500)
    c.executemany("INSERT INTO t VALUES (?)", [(None,)] * 100)  # 尾部缺失 16.7%
    c.commit(); c.close()
    from dia.infrastructure.database.manager import get_datasource_manager
    from dia.infrastructure.database.base import DataSourceConfig
    mgr = get_datasource_manager()
    mgr.add_source(DataSourceConfig(id="full", name="t", db_type="sqlite", database=db))
    from dia.tools.data import assess_quality
    d = json.loads(assess_quality.func("full"))
    assert "error" not in d, d
    assert d["total_rows"] == 600
    assert any("revenue" in i and "缺失" in i for i in d["issues"]), d["issues"]
    mgr.disconnect_all()
    try: _os.unlink(db)
    except PermissionError: pass


def test_fallback_glossary_special_col_names():
    """中文/特殊列名也能解析 (正则放宽)"""
    from langchain_core.messages import ToolMessage
    from dia.agents.data_curator import _fallback_glossary
    fake = ToolMessage(
        content="数据库 test: 1 个表\n\n  orders: 30 行, 3 列\n    列: 销售额(real), 区域(varchar), 订单号(integer)",
        name="inspect", tool_call_id="t1")
    g = _fallback_glossary([fake])
    assert "销售额" in g and g["销售额"]["role"] == "metric"
    assert "区域" in g and g["区域"]["role"] == "dimension"
    assert "订单号" in g
