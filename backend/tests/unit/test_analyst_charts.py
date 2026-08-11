"""Analyst 图表提取测试: 同题图去重 / 标题归一化"""

from langchain_core.messages import ToolMessage
from dia.agents.analyst import _extract_charts_from_msgs


def _chart_msg(title: str, chart_type: str = "bar") -> ToolMessage:
    import json
    content = json.dumps({
        "chart_type": chart_type,
        "echarts_option": {"title": {"text": title}, "xAxis": {"data": ["A"]}},
    }, ensure_ascii=False)
    return ToolMessage(content=content, name="build_chart", tool_call_id=f"tc_{title[:6]}")


class TestChartDedup:
    def test_duplicate_titles_dropped(self):
        """同标题图表只保留第一张"""
        msgs = [_chart_msg("各区域营收对比"), _chart_msg("各区域营收对比")]
        charts = _extract_charts_from_msgs(msgs)
        assert len(charts) == 1
        assert charts[0]["title"] == "各区域营收对比"

    def test_near_duplicate_titles_dropped(self):
        """标题仅空白/标点差异 → 视为重复"""
        msgs = [_chart_msg("月度营收趋势"), _chart_msg(" 月度营收趋势（line） ")]
        charts = _extract_charts_from_msgs(msgs)
        assert len(charts) == 1

    def test_distinct_titles_kept(self):
        """不同标题全部保留"""
        msgs = [_chart_msg("各区域营收对比"), _chart_msg("月度营收趋势")]
        charts = _extract_charts_from_msgs(msgs)
        assert len(charts) == 2

    def test_chart_type_variety_preserved(self):
        """去重不影响图表类型字段"""
        msgs = [_chart_msg("区域占比", "pie"), _chart_msg("成本vs营收", "scatter")]
        charts = _extract_charts_from_msgs(msgs)
        assert {c["chart_type"] for c in charts} == {"pie", "scatter"}


class TestChartDataChineseTitles:
    """gap_fill 补图标题汉化: 英文列名 → 中文业务语言"""

    def _explore_msg(self, metric="sales", group_by="region", op="aggregate"):
        import json
        content = json.dumps({
            "metric": metric, "group_by": group_by, "operation": op,
            "groups": [{"group": "华东", "value": 100.0}],
        }, ensure_ascii=False)
        return ToolMessage(content=content, name="explore", tool_call_id="tc_e1")

    def test_aggregate_title_chinese(self):
        """aggregate 补图: 'sales by region' → '各区域销售额对比'"""
        from dia.agents.analyst import _extract_chart_data
        suggs = _extract_chart_data([self._explore_msg()])
        assert suggs[0]["title"] == "各区域销售额对比"
        assert suggs[0]["chart_type"] == "bar"

    def test_pct_title_chinese(self):
        """share 占比补图: metric 汉化"""
        import json
        content = json.dumps({
            "metric": "sales", "operation": "share",
            "groups": [{"group": "华东", "pct": 23.6}],
        }, ensure_ascii=False)
        msg = ToolMessage(content=content, name="explore", tool_call_id="tc_e2")
        from dia.agents.analyst import _extract_chart_data
        suggs = _extract_chart_data([msg])
        assert suggs[0]["title"] == "销售额占比结构"
        assert suggs[0]["chart_type"] == "pie"

    def test_trend_title_chinese(self):
        """trend 补图: 'sales 趋势' → '销售额趋势'"""
        import json
        content = json.dumps({
            "metric": "sales", "operation": "trend", "grain": "month",
            "periods": ["2025-07"], "values": [100.0],
        }, ensure_ascii=False)
        msg = ToolMessage(content=content, name="explore", tool_call_id="tc_e3")
        from dia.agents.analyst import _extract_chart_data
        suggs = _extract_chart_data([msg])
        assert suggs[0]["title"] == "销售额趋势 (month)"
        assert suggs[0]["chart_type"] == "line"

    def test_unknown_metric_kept_as_is(self):
        """未在映射表的列名 → 原样保留 (不崩)"""
        import json
        content = json.dumps({
            "metric": "gmv_xyz", "group_by": "store", "operation": "aggregate",
            "groups": [{"group": "A", "value": 1.0}],
        }, ensure_ascii=False)
        msg = ToolMessage(content=content, name="explore", tool_call_id="tc_e4")
        from dia.agents.analyst import _extract_chart_data
        suggs = _extract_chart_data([msg])
        assert "gmv_xyz" in suggs[0]["title"]

    def test_cross_tab_heatmap_extraction(self):
        """cross_tab 结果 → heatmap 建议 (中文标题 + 矩阵)"""
        import json
        content = json.dumps({
            "metric": "sales", "row_dim": "region", "col_dim": "category",
            "agg_func": "sum", "table": {"华东": {"电子产品": 100.0, "食品": 50.0},
                                         "华北": {"电子产品": 80.0, "食品": 20.0}},
        }, ensure_ascii=False)
        msg = ToolMessage(content=content, name="explore", tool_call_id="tc_e5")
        from dia.agents.analyst import _extract_chart_data
        suggs = _extract_chart_data([msg])
        heatmaps = [s for s in suggs if s["chart_type"] == "heatmap"]
        assert heatmaps, "cross_tab 应产出 heatmap 建议"
        hm = heatmaps[0]
        assert hm["title"] == "区域×品类交叉"
        assert hm["x"] == ["华东", "华北"]
        assert set(hm["y"]) == {"电子产品", "食品"}  # 列名排序不定, 只断言集合

    def test_describe_distribution_extraction(self):
        """describe 结果 → 分位数分布图建议"""
        import json
        content = json.dumps({
            "metric": "sales", "n": 6570, "p5": 1000.0, "p25": 4000.0, "median": 8314.83,
            "p75": 12000.0, "p95": 17648.77, "skewness": 1.04,
            "distribution_shape": "右偏(大量小值+少量极大值)", "top20_concentration": 34.6,
        }, ensure_ascii=False)
        msg = ToolMessage(content=content, name="explore", tool_call_id="tc_e6")
        from dia.agents.analyst import _extract_chart_data
        suggs = _extract_chart_data([msg])
        dist = suggs[0]
        assert dist["title"] == "销售额分布 (偏度1.04)"
        assert dist["categories"] == ["P5", "P25", "中位数", "P75", "P95"]


class TestBuildChartHeatmap:
    """build_chart heatmap 类型: 校验 + option 生成"""

    def test_heatmap_valid(self):
        from dia.tools.output import build_chart
        import json
        out = json.loads(build_chart.invoke({
            "chart_type": "heatmap",
            "title": "区域×品类交叉",
            "data": {"x": ["华东", "华北"], "y": ["电子", "食品"],
                     "values": [[100, 50], [80, 20]]},
        }))
        assert out["chart_type"] == "heatmap"
        opt = out["echarts_option"]
        assert opt["series"][0]["type"] == "heatmap"
        assert opt["visualMap"]["max"] == 100
        assert len(opt["series"][0]["data"]) == 4  # 2x2 全部单元格

    def test_heatmap_missing_values_rejected(self):
        from dia.tools.output import build_chart
        import json
        out = json.loads(build_chart.invoke({
            "chart_type": "heatmap",
            "title": "x",
            "data": {"x": ["A", "B"], "y": ["C", "D"], "values": [[1, 2]]},  # 行数不对
        }))
        assert "error" in out

    def test_heatmap_unsupported_absent(self):
        """旧类型列表不含 heatmap → 现在应支持"""
        from dia.tools.output import build_chart
        desc = build_chart.description
        assert "heatmap" in desc
