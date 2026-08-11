"""Smoke tests for Analyst refactor — statistical tools on real SQLite data."""
import json
import os
import sqlite3
import sys
import tempfile
sys.path.insert(0, 'src')


# ══ Fixture: 真实 SQLite 数据源 ══

DB_PATH = None
SOURCE_ID = "test_analyst_src"


def setup_module():
    """创建带显著差异的真实数据: A组营收均值≈100, B组≈110, 各200行。"""
    global DB_PATH
    fd, DB_PATH = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE sales (id INTEGER, region TEXT, revenue REAL, price REAL, quantity INTEGER, order_date TEXT)")
    import random
    random.seed(42)
    rows = []
    for i in range(200):
        base = 100 if i % 2 == 0 else 110  # region A vs B
        rows.append((i, "A" if i % 2 == 0 else "B",
                     base + random.uniform(-5, 5),
                     10 + random.uniform(-1, 1),
                     random.randint(5, 15),
                     f"2026-01-{i % 28 + 1:02d}"))
    conn.executemany("INSERT INTO sales VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    # 注册数据源
    from dia.infrastructure.database.manager import get_datasource_manager
    from dia.infrastructure.database.base import DataSourceConfig
    mgr = get_datasource_manager()
    mgr.add_source(DataSourceConfig(id=SOURCE_ID, name="test", db_type="sqlite", database=DB_PATH))


def teardown_module():
    from dia.infrastructure.database.manager import get_datasource_manager
    try:
        get_datasource_manager().disconnect_all()
    except Exception:
        pass
    if DB_PATH and os.path.exists(DB_PATH):
        os.unlink(DB_PATH)


# ══ 统计工具 ══

def test_hypothesis_test_detects_significant_diff():
    from dia.tools.analysis import hypothesis_test
    raw = hypothesis_test.func(metric="revenue", group_by="region", source_id=SOURCE_ID)
    data = json.loads(raw)
    assert "error" not in data, data
    assert "Welch t-test" in data["method"]  # 可能带 BH FDR 校正后缀
    assert len(data["pairs"]) >= 1
    # A均值100 vs B均值110 → 差异应显著
    assert data["conclusion"] == "差异显著"
    for p in data["pairs"]:
        assert p["significant"] is True
        assert p["p_value"] < 0.05


def test_hypothesis_test_missing_col():
    from dia.tools.analysis import hypothesis_test
    raw = hypothesis_test.func(metric="nope", group_by="region", source_id=SOURCE_ID)
    data = json.loads(raw)
    assert "error" in data


def test_regression_analysis():
    from dia.tools.analysis import regression_analysis
    # revenue 与 quantity 弱相关, 与 price 无关
    raw = regression_analysis.func(target="revenue", features=["price", "quantity"], source_id=SOURCE_ID)
    data = json.loads(raw)
    assert "error" not in data, data
    assert data["method"] == "OLS 多元线性回归"
    assert 0 <= data["r_squared"] <= 1
    assert len(data["coefficients"]) == 2
    for c in data["coefficients"]:
        assert "coefficient" in c and "p_value" in c and "significant" in c


def test_percentile_analysis():
    from dia.tools.analysis import percentile_analysis
    raw = percentile_analysis.func(metric="revenue", source_id=SOURCE_ID)
    data = json.loads(raw)
    assert "error" not in data, data
    assert data["p25"] <= data["median"] <= data["p75"]
    assert "skewness" in data and "top20_concentration" in data


def test_forecast_intervals():
    from dia.tools.analysis import forecast
    raw = forecast.func(metric="revenue", source_id=SOURCE_ID, date_col="order_date", periods=3)
    data = json.loads(raw)
    assert "error" not in data, data
    assert len(data["predictions"]) == 3
    assert len(data["intervals"]) == 3
    for iv in data["intervals"]:
        assert iv["lower"] <= iv["upper"]
        assert "period" in iv


def test_seasonal_analysis():
    from dia.tools.analysis import seasonal_analysis
    # 周期=7 但数据28天 → 可分解
    raw = seasonal_analysis.func(metric="revenue", date_col="order_date", source_id=SOURCE_ID, period=7)
    data = json.loads(raw)
    assert "error" not in data, data
    assert 0 <= data["seasonality_strength"] <= 1
    assert data["seasonality_level"] in ("强", "中", "弱")
    assert data["trend_direction"] in ("上升", "下降", "平稳")


def test_explain_anomaly():
    from dia.tools.analysis import explain_anomaly
    # 数据标准差≈5, 无超3σ的异常 → 应返回无异常(或少量)
    raw = explain_anomaly.func(metric="revenue", date_col="order_date", source_id=SOURCE_ID)
    data = json.loads(raw)
    assert "error" not in data, data
    assert "anomaly_count" in data


# ══ extract_input 注入 ROADMAP ══

def test_extract_input_injects_roadmap():
    import asyncio
    from dia.agents.analyst import AnalystAgent
    from langchain_core.messages import SystemMessage, HumanMessage

    agent = AnalystAgent(name="analyst")
    state = {
        "source_id": SOURCE_ID,
        "user_request": "为什么华东区营收下滑",
        "shared_context": {
            "data_quality_score": 80,
            "curator_report": {
                "confirm": {"caliber": "营收=SUM(revenue)", "cannot_answer": ["客户流失分析"]},
                "roadmap": {
                    "rounds": [
                        {"title": "定位问题", "steps": ["按月汇总营收"]},
                        {"title": "拆解维度", "steps": ["按品类下钻"]},
                    ],
                    "impossible": ["竞品对比"],
                },
                "kpi_tree": {
                    "基础指标": [{"name": "revenue", "label": "营收", "source": "原始列"}],
                    "效率指标": [{"name": "unit_price", "label": "客单价", "source": "衍生:revenue/quantity"}],
                },
                "quality": {"grade": "B", "blockers": ["revenue列15%缺失 → 总营收可能低报"]},
                "data_overview": {"tables": "orders 12450行", "time_span": "2025-01~2026-06", "findings": ["region含Unknown 3%"]},
            },
        },
        "messages": [],
    }
    inner = asyncio.run(agent.extract_input(state))
    assert len(inner["messages"]) == 2
    assert isinstance(inner["messages"][0], SystemMessage)
    assert isinstance(inner["messages"][1], HumanMessage)
    text = inner["messages"][1].content
    assert "[分析路线图" in text
    assert "定位问题" in text
    assert "拆解维度" in text
    assert "unit_price" in text  # KPI 建议注入
    assert "客户流失分析" in text  # 不可答注入
    assert "竞品对比" in text  # 不可做注入
    assert "revenue列15%缺失" in text  # 质量警告注入
    assert "时间跨度: 2025-01~2026-06" in text  # 数据概览注入
    assert "Unknown" in text  # 采样发现注入
    assert "[上一轮分析结论]" not in text  # 无历史 → 不注入


def test_extract_input_injects_prev_analysis():
    """多轮对话: 最近一条非工具 AIMessage 作为上一轮结论注入"""
    import asyncio
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, SystemMessage
    from dia.agents.analyst import AnalystAgent

    agent = AnalystAgent(name="analyst")
    state = {
        "source_id": SOURCE_ID,
        "user_request": "那华北呢?",
        "shared_context": {"data_quality_score": 80, "glossary": {}},
        "messages": [
            SystemMessage(content="旧系统 prompt"),
            HumanMessage(content="第一轮请求"),
            ToolMessage(content="{}", name="drill_down", tool_call_id="t1"),
            AIMessage(content="[强] 华东营收下滑 8.3%, 主因女装品类", tool_calls=[]),  # 上一轮结论
            HumanMessage(content="那华北呢?"),  # 当前轮
        ],
    }
    inner = asyncio.run(agent.extract_input(state))
    text = inner["messages"][1].content
    assert "[上一轮分析结论]" in text
    assert "华东营收下滑 8.3%" in text


# ══ build_output 合并 shared_context (不覆盖 Curator 探查结果) ══

def test_build_output_merges_shared_context():
    from langchain_core.messages import AIMessage
    from dia.agents.analyst import AnalystAgent

    agent = AnalystAgent(name="analyst")
    state = {
        "shared_context": {
            "data_quality_score": 80,
            "glossary": {"revenue": {"role": "metric"}},
            "curator_report": {"confirm": {"caliber": "营收=SUM"}},
        },
    }
    result = {
        "analysis_done": True,
        "messages": [AIMessage(content="[强] 华东vs华北差异显著 (p=0.003)")],
    }
    out = agent.build_output(state, result)

    shared = out["shared_context"]
    # Curator 数据保留
    assert shared["data_quality_score"] == 80
    assert "revenue" in shared["glossary"]
    assert shared["curator_report"]["confirm"]["caliber"] == "营收=SUM"
    # Analyst 数据追加
    # 无工具结果 → 不写 charts (fallback-only: charts 由 chat.py SSE 收集器独占)
    assert "charts" not in shared
    # analysis 输出完整
    assert out["analysis"]["done"] is True
    assert "华东" in out["analysis"]["summary"]


# ══ should_continue: ReAct 轮次上限 (防死循环) ══

def test_should_continue_round_limit():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from dia.agents.analyst import should_continue, MAX_TOOL_ROUNDS

    def mk_round(i):
        return [
            AIMessage(content="", tool_calls=[{"name": "explore", "args": {"operation": "aggregate"}, "id": f"c{i}", "type": "tool_call"}]),
            ToolMessage(content='{"groups": []}', name="explore", tool_call_id=f"c{i}"),
        ]

    # MAX_TOOL_ROUNDS-1 轮已完成 + 新一轮请求 → 允许执行 (第 MAX 轮)
    msgs = [HumanMessage(content="分析数据")]
    for i in range(MAX_TOOL_ROUNDS - 1):
        msgs += mk_round(i)
    msgs.append(AIMessage(content="", tool_calls=[{"name": "explore", "args": {}, "id": "last", "type": "tool_call"}]))
    assert should_continue({"messages": msgs}) == "tool_node"

    # 已到上限后再请求 → 截断到 gap_fill (LLM 停不下来时由代码收尾)
    msgs2 = [HumanMessage(content="分析数据")]
    for i in range(MAX_TOOL_ROUNDS):
        msgs2 += mk_round(i)
    msgs2.append(AIMessage(content="", tool_calls=[{"name": "explore", "args": {}, "id": "over", "type": "tool_call"}]))
    assert should_continue({"messages": msgs2}) == "gap_fill"

    # 无工具调用 → gap_fill (补缺后 synthesize)
    msgs3 = [HumanMessage(content="分析数据"), AIMessage(content="结论")]
    assert should_continue({"messages": msgs3}) == "gap_fill"


# ══ gap_fill: source_id/date_col 从 state 取 (explore 输出不携带) ══

def test_gap_fill_uses_state_source_id():
    """gap_fill 补调 test_difference/forecast 必须用 state.source_id, 不能依赖 explore 输出."""
    import asyncio
    from unittest.mock import patch
    from langchain_core.messages import ToolMessage, AIMessage
    from dia.agents.analyst import gap_fill_node

    async def run():
        state = {
            "source_id": "file_sales",
            "date_cols": ["order_date"],
            "user_request": "全面分析",
            "messages": [
                AIMessage(content="", tool_calls=[]),
                ToolMessage(content=json.dumps({
                    "metric": "revenue", "group_by": "region",
                    "groups": [{"group": "A", "sum": 100}, {"group": "B", "sum": 80}],
                }), name="explore", tool_call_id="t1"),
                ToolMessage(content=json.dumps({
                    "metric": "revenue", "date_col": "order_date",
                    "periods": ["2026-01", "2026-02"], "values": [1, 2],
                }), name="explore", tool_call_id="t2"),
            ],
        }
        captured = {}

        async def fake_invoke(name, args):
            captured[name] = args
            return ToolMessage(content='{"ok": true}', tool_call_id=f"gap_{name}", name=name)

        with patch("dia.agents.analyst._invoke_tool", side_effect=fake_invoke):
            out = await gap_fill_node(state)
        return captured, out

    captured, out = asyncio.run(run())
    assert captured["test_difference"]["source_id"] == "file_sales"
    assert captured["forecast"]["source_id"] == "file_sales"
    assert captured["forecast"]["date_col"] == "order_date"


# ══ dt_cols 识别: role=datetime 的列不再被误判为无日期 ══

def test_extract_input_detects_date_cols():
    import asyncio
    from dia.agents.analyst import AnalystAgent

    agent = AnalystAgent(name="analyst")
    state = {
        "source_id": "s1",
        "user_request": "分析趋势",
        "shared_context": {
            "data_quality_score": 80,
            "glossary": {
                "order_date": {"name": "order_date", "label": "日期", "role": "datetime", "sql_type": "DATE"},
                "revenue": {"name": "revenue", "label": "营收", "role": "metric", "sql_type": "REAL"},
                "region": {"name": "region", "label": "区域", "role": "dimension", "sql_type": "TEXT"},
            },
        },
    }
    inner = asyncio.run(agent.extract_input(state))
    text = inner["messages"][1].content
    # 有日期列 → 不应出现"无日期列"约束
    assert "无日期列" not in text
    assert "日期列: order_date" in text
    # 有维度列 → 不应出现"无分类列"约束
    assert "无分类列" not in text


# ══ _extract_findings: 统计工具结果进入结构化发现 ══

def test_extract_findings_new_tools():
    from dia.agents.analyst import _extract_findings

    parsed = [
        {"name": "test_difference", "data": {
            "group_by": "region", "pairs": [
                {"group_a": "华东", "group_b": "华北", "mean_a": 312, "mean_b": 287,
                 "p_value": 0.003, "p_value_adjusted": 0.004, "significant": True, "effect_size": 0.8},
            ]}},
        {"name": "attribution", "data": {
            "target": "revenue", "r_squared": 0.87,
            "coefficients": [{"feature": "price", "coefficient": -0.42, "p_value": 0.01, "significant": True}]}},
        {"name": "seasonal_analysis", "data": {
            "metric": "revenue", "seasonality_strength": 0.78, "seasonality_level": "强", "trend_direction": "上升"}},
        {"name": "explore", "data": {
            "metric": "revenue", "distribution_shape": "右偏", "top20_concentration": 70, "median": 100}},
    ]
    findings = _extract_findings(parsed)

    claims = [f["claim"] for f in findings]
    assert any("差异显著" in c for c in claims), "test_difference 未进入 findings"
    assert any("控制其他变量后" in c for c in claims), "attribution 未进入 findings"
    assert any("季节性" in c for c in claims), "seasonal_analysis 未进入 findings"
    assert any("右偏" in c for c in claims), "explore describe 未进入 findings"
    # 置信度反映统计严谨性
    ht = [f for f in findings if f["evidence"] == "test_difference"]
    assert ht and ht[0]["confidence"] == 0.85


# ══ build_chart 新签名: dict 参数 ══

def test_build_chart_accepts_dict():
    from dia.tools.output import build_chart

    raw = build_chart.func("bar", {"categories": ["A", "B"], "series": [{"name": "x", "data": [1, 2]}]}, "测试图")
    data = json.loads(raw)
    assert data["chart_type"] == "bar"
    assert "echarts_option" in data
    assert data["echarts_option"]["xAxis"]["data"] == ["A", "B"]

    raw = build_chart.func("pie", {"data": [{"name": "华东", "value": 100}]}, "占比")
    assert json.loads(raw)["echarts_option"]["series"][0]["type"] == "pie"


# ══ 工具 schema: features/metrics 为 array 类型 ══

def test_list_params_schema():
    from dia.tools.analysis import detect, segment, regression_analysis

    for t in [detect, segment, regression_analysis]:
        schema = t.args_schema.model_json_schema()
        props = schema.get("properties", {})
        for pname in ("features", "metrics"):
            if pname in props:
                assert props[pname]["type"] == "array", f"{t.name}.{pname} 不是 array"
                assert props[pname]["items"]["type"] == "string"


# ══ compare: 直连日期列算环比 ══

def test_compare_direct_from_table():
    from dia.tools.analysis import compare

    # 测试数据 order_date 是 2026-01-01 ~ 2026-01-28, 按月聚合只有1个月 → 报错提示跨度不够
    raw = compare.func(metric="revenue", date_col="order_date", source_id=SOURCE_ID, period="mom")
    data = json.loads(raw)
    assert "error" in data  # 28天只有1个月 → 不足2期
    assert "跨度" in data["error"]

    # 按日环比: 28天有28期 → 正常计算
    raw = compare.func(metric="revenue", date_col="order_date", source_id=SOURCE_ID, period="dod")
    data = json.loads(raw)
    assert "error" not in data, data
    assert data["period"] == "dod"
    assert "current_period_value" in data
    assert "change_pct" in data
    assert data["trend"] in ("上升", "下降", "平稳")
    assert data["periods_covered"] >= 2


# ══ build_output: charts 不覆盖流式收集的 echarts_option ══

def test_build_output_preserves_streamed_charts():
    """shared.charts 已有流式收集的 echarts_option → 不覆盖"""
    from langchain_core.messages import AIMessage
    from dia.agents.analyst import AnalystAgent

    agent = AnalystAgent(name="analyst")
    streamed_charts = [{"title": "LLM画的图", "chart_type": "bar", "echarts_option": {"xAxis": {}}}]
    state = {"shared_context": {"charts": streamed_charts}, "user_request": "分析"}
    result = {"analysis_done": True, "messages": [AIMessage(content="结论")]}
    out = agent.build_output(state, result)
    assert out["shared_context"]["charts"] == streamed_charts  # 保留流式数据


def test_build_output_charts_owned_by_sse():
    """charts 由 SSE 收集器独占: build_output 不写 shared_context.charts, chart_data 只在 analysis 字段"""
    from langchain_core.messages import AIMessage, ToolMessage
    from dia.agents.analyst import AnalystAgent

    agent = AnalystAgent(name="analyst")
    state = {"shared_context": {}, "user_request": "分析"}
    drill_msg = ToolMessage(
        # 真实 explore aggregate 输出恒含 value (按 agg_func 的主口径) + sum/avg/median/count
        content='{"metric":"revenue","group_by":"region","groups":[{"group":"A","value":100,"sum":100,"avg":100,"median":100,"count":1},{"group":"B","value":80,"sum":80,"avg":80,"median":80,"count":1}]}',
        name="explore", tool_call_id="t1",
    )
    result = {"analysis_done": True, "messages": [AIMessage(content="结论"), drill_msg]}
    out = agent.build_output(state, result)
    assert "charts" not in out["shared_context"]  # 不污染会话图表 (SSE 收集器独占)
    chart_data = out["analysis"]["chart_data"]
    assert chart_data and chart_data[0]["chart_type"] == "bar"  # 规则提取数据仍在 analysis 字段


def test_build_output_messages_slim():
    """messages 瘦身: 只回传最终 AI 文本, 工具/图表消息不回流外层 (防跨轮膨胀)"""
    from langchain_core.messages import AIMessage, ToolMessage, HumanMessage
    from dia.agents.analyst import AnalystAgent

    agent = AnalystAgent(name="analyst")
    state = {"shared_context": {}}
    msgs = [
        HumanMessage(content="x"),
        AIMessage(content="", tool_calls=[{"name": "explore", "args": {}, "id": "c1", "type": "tool_call"}]),
        ToolMessage(content='{}', name="explore", tool_call_id="c1"),
        AIMessage(content="[强] 结论"),
    ]
    result = {"analysis_done": True, "messages": msgs}
    out = agent.build_output(state, result)
    returned = out["messages"]
    assert len(returned) == 1, f"期望只回传最终总结, 实际 {len(returned)} 条"
    assert returned[0].content == "[强] 结论"


# ══ build_output: 优先用 _synthesize 的结构化 findings ══

def test_build_output_uses_structured_findings():
    from langchain_core.messages import AIMessage
    from dia.agents.analyst import AnalystAgent

    agent = AnalystAgent(name="analyst")
    state = {"shared_context": {}}
    result = {
        "analysis_done": True,
        "findings": [{"claim": "华东vs华北差异显著 p=0.003", "evidence": "hypothesis_test", "confidence": 0.85}],
        "messages": [AIMessage(content="分析完成")],
    }
    out = agent.build_output(state, result)
    findings = out["analysis"]["structured_data"]["findings"]
    assert isinstance(findings, list)
    assert findings[0]["claim"] == "华东vs华北差异显著 p=0.003"
    assert findings[0]["evidence"] == "hypothesis_test"


# ══════════════════════════════════════════════════════════════════
#  工具重构 (v2) — 口径参数化 / 统计严谨性 / 新工具
# ══════════════════════════════════════════════════════════════════

# ══ explore: agg_func 口径 ══

def test_explore_agg_func_auto_avg():
    """均值型指标 (price) auto → avg, 不再被求和"""
    from dia.tools.explore import explore
    d = json.loads(explore.func(operation="aggregate", metric="price", source_id=SOURCE_ID, group_by="region"))
    assert "error" not in d, d
    assert d["agg_func"] == "avg"
    assert 9 <= d["groups"][0]["value"] <= 11  # price ≈ 10 ± 1
    assert "median" in d["groups"][0]


def test_explore_agg_func_explicit_median():
    from dia.tools.explore import explore
    d = json.loads(explore.func(operation="aggregate", metric="revenue", source_id=SOURCE_ID,
                                group_by="region", agg_func="median"))
    assert d["agg_func"] == "median"


# ══ test_difference: 最小样本 + CI ══

def test_test_difference_ci_and_significant():
    from dia.tools.explore import test_difference
    d = json.loads(test_difference.func(metric="revenue", group_by="region", source_id=SOURCE_ID))
    assert "error" not in d, d
    p = d["pairs"][0]
    assert "mean_diff_ci" in p
    assert p["mean_diff_ci"][0] <= p["mean_diff_ci"][1]
    assert p["mean_diff"] == round(p["mean_a"] - p["mean_b"], 2)
    assert p["significant"] is True  # A≈100 vs B≈110 → 显著


def test_test_difference_small_group_rejected():
    """每组 <10 样本 → 拒绝检验 (原 ≥3 无功效)"""
    import sqlite3, tempfile, os as _os
    fd, db = tempfile.mkstemp(suffix=".db"); _os.close(fd)
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t (g TEXT, v REAL)")
    c.executemany("INSERT INTO t VALUES (?,?)", [("A", 1.0), ("A", 2.0), ("A", 3.0), ("B", 4.0), ("B", 5.0)])
    c.commit(); c.close()
    from dia.infrastructure.database.manager import get_datasource_manager
    from dia.infrastructure.database.base import DataSourceConfig
    mgr = get_datasource_manager()
    mgr.add_source(DataSourceConfig(id="tiny", name="t", db_type="sqlite", database=db))
    from dia.tools.explore import test_difference
    d = json.loads(test_difference.func(metric="v", group_by="g", source_id="tiny"))
    assert "error" in d and "10" in d["error"]
    mgr.disconnect_all()
    try: _os.unlink(db)
    except PermissionError: pass


# ══ attribution: 分类列 one-hot ══

def test_attribution_categorical_feature():
    from dia.tools.explore import attribution
    d = json.loads(attribution.func(target="revenue", source_id=SOURCE_ID, features=["region", "price"]))
    assert "error" not in d, d
    feats = [c["feature"] for c in d["coefficients"]]
    assert any(f.startswith("region=") for f in feats), f"分类列未 one-hot: {feats}"
    # 数据: B 组 revenue 比 A 高 10 → region=B 系数 ≈ +10 (A 为基准)
    b_coef = next(c["coefficient"] for c in d["coefficients"] if c["feature"] == "region=B")
    assert 5 <= b_coef <= 15, b_coef


# ══ forecast: 季节感知 ══

def test_forecast_seasonal_detection():
    """周季节数据 → 自动检测周期 7 + 季节调整 + 趋势 p 值"""
    import sqlite3, tempfile, os as _os
    from datetime import date, timedelta
    fd, db = tempfile.mkstemp(suffix=".db"); _os.close(fd)
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t (d TEXT, v REAL)")
    start = date(2026, 1, 1)
    rows = [(str(start + timedelta(days=i)), round(100 + i * 0.8 + (i % 7) * 15, 2)) for i in range(90)]
    c.executemany("INSERT INTO t VALUES (?,?)", rows)
    c.commit(); c.close()
    from dia.infrastructure.database.manager import get_datasource_manager
    from dia.infrastructure.database.base import DataSourceConfig
    mgr = get_datasource_manager()
    mgr.add_source(DataSourceConfig(id="seas", name="t", db_type="sqlite", database=db))
    from dia.tools.analysis import forecast
    d = json.loads(forecast.func(metric="v", source_id="seas", date_col="d", periods=3))
    assert "error" not in d, d
    assert d["period"] == 7, d
    assert d["season_adjusted"] is True
    assert "trend_p_value" in d
    mgr.disconnect_all()
    try: _os.unlink(db)
    except PermissionError: pass


# ══ compare: 两段显著性检验 ══

def test_compare_significance():
    """最近 7 天跳升 → 两段 Welch t 显著"""
    import sqlite3, tempfile, os as _os
    from datetime import date, timedelta
    fd, db = tempfile.mkstemp(suffix=".db"); _os.close(fd)
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t (d TEXT, v REAL)")
    start = date(2026, 1, 1)
    rows = [(str(start + timedelta(days=i)), round(100 + i * 0.5 + (15 if i >= 53 else 0), 2)) for i in range(60)]
    c.executemany("INSERT INTO t VALUES (?,?)", rows)
    c.commit(); c.close()
    from dia.infrastructure.database.manager import get_datasource_manager
    from dia.infrastructure.database.base import DataSourceConfig
    mgr = get_datasource_manager()
    mgr.add_source(DataSourceConfig(id="cmp", name="t", db_type="sqlite", database=db))
    from dia.tools.analysis import compare
    d = json.loads(compare.func(metric="v", date_col="d", source_id="cmp", period="wow"))
    assert "error" not in d, d
    assert d["significant"] is True, d
    assert d["p_value"] < 0.05
    mgr.disconnect_all()
    try: _os.unlink(db)
    except PermissionError: pass


# ══ detect: 按日期聚合 ══

def test_detect_with_date_col():
    """按日聚合检测尖峰 (时间序列模式)"""
    import sqlite3, tempfile, os as _os
    fd, db = tempfile.mkstemp(suffix=".db"); _os.close(fd)
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t (d TEXT, v REAL)")
    rows = [(f"2026-01-{i % 28 + 1:02d}", 100.0) for i in range(100)]
    rows += [(f"2026-02-{i % 28 + 1:02d}", 500.0) for i in range(3)]
    c.executemany("INSERT INTO t VALUES (?,?)", rows)
    c.commit(); c.close()
    from dia.infrastructure.database.manager import get_datasource_manager
    from dia.infrastructure.database.base import DataSourceConfig
    mgr = get_datasource_manager()
    mgr.add_source(DataSourceConfig(id="det", name="t", db_type="sqlite", database=db))
    from dia.tools.analysis import detect
    d = json.loads(detect.func(metrics=["v"], source_id="det", date_col="d", threshold=2.0))
    assert "error" not in d, d
    assert d["count"] >= 1
    assert all("date" in a for a in d["anomalies"])
    mgr.disconnect_all()
    try: _os.unlink(db)
    except PermissionError: pass


# ══ build_chart: 严格校验 ══

def test_build_chart_validation():
    from dia.tools.output import build_chart
    assert "error" in json.loads(build_chart.func("bar", {"series": [{"name": "x", "data": [1]}]}))
    assert "error" in json.loads(build_chart.func("pie", {"data": [{"name": "A"}]}))
    assert "error" in json.loads(build_chart.func("scatter", {"x": [1, 2], "y": [1]}))
    ok = json.loads(build_chart.func("bar", {"categories": ["A"], "series": [{"name": "x", "data": [1]}]}))
    assert "echarts_option" in ok
