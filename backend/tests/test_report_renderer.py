"""HTML 报告渲染器单测."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dia.report.renderer import build_report_data, render_report_html


def _mock_state():
    return {
        "user_request": "全面分析该数据",
        # source_id 置空: 避免连上 datasources.json 残留的真实数据源, 强制走文本兜底 KPI
        "source_id": "",
        "shared_context": {
            "tables": ["daily_sales"],
            "curator_report": (
                "数据源 test_analysis_mysql: 2 个表\n"
                "daily_sales: 6570 行, 9 列, 质量等级 A级\n"
                "总营收 6030.49万元 (60,304,868.65元)\n"
                "总成本 3618.53万元, 利润率 40.0%\n"
                "订单数 196708, 客户数 146340\n"
            ),
            "charts": [
                {"title": "sales by region", "chart_type": "bar", "echarts_option": {
                    "xAxis": {"type": "category", "data": ["华东", "华南"]},
                    "yAxis": {"type": "value"},
                    "series": [{"type": "bar", "data": [1420.2, 1080.5]}],
                }},
            ],
        },
        "analysis": {
            "summary": "全年总营收6030万，环比降4.63%。",
            "structured_data": {
                "findings": [
                    {"claim": "[强] 华东vs华北差异显著 (p=0.0025)", "evidence": "test_difference", "confidence": 0.85},
                    {"claim": "[弱] 电子产品占29.9%", "evidence": "explore:aggregate", "confidence": 0.7},
                ]
            },
        },
    }


def test_build_report_data():
    data = build_report_data(_mock_state())
    # KPI: 去重后应有 总营收/利润率/订单数/客户数/成本
    labels = [k["label"] for k in data["kpis"]]
    assert labels.count("总营收") == 1, "KPI 应去重"
    assert "利润率" in labels and "订单数" in labels
    # 订单数不应带尾逗号
    order_kpi = next(k for k in data["kpis"] if k["label"] == "订单数")
    assert not order_kpi["value"].endswith(",")
    # findings 分级
    assert data["findings"][0]["level"] == "强"
    assert data["findings"][1]["level"] == "弱"
    assert len(data["charts"]) == 1


def test_render_html_structure():
    html_out = render_report_html(build_report_data(_mock_state()))
    for marker in ["<!DOCTYPE html>", "kpi-card", "chart-box", "level-强",
                   "level-弱", "echarts.min.js", "质量A", "华东vs华北差异显著"]:
        assert marker in html_out, f"HTML 缺 {marker}"
    # KPI 值已渲染
    assert "6030.49万" in html_out


def test_render_empty_findings():
    state = _mock_state()
    state["analysis"]["structured_data"]["findings"] = []
    html_out = render_report_html(build_report_data(state))
    assert "无结构化发现" in html_out


def test_chart_data_fallback():
    """无 echarts_option 但有 chart_data (categories/series) → 自动转换"""
    state = _mock_state()
    state["shared_context"]["charts"] = [
        {"title": "趋势", "chart_type": "line", "categories": ["1月", "2月"], "series": [{"name": "s", "data": [1, 2]}]},
    ]
    data = build_report_data(state)
    assert len(data["charts"]) == 1
    eo = data["charts"][0]["echarts_option"]
    assert eo["series"][0]["type"] == "line"


def test_pie_chart_data_fallback():
    state = _mock_state()
    state["shared_context"]["charts"] = [
        {"title": "占比", "chart_type": "pie", "data": [{"name": "A", "value": 1}, {"name": "B", "value": 2}]},
    ]
    data = build_report_data(state)
    eo = data["charts"][0]["echarts_option"]
    assert eo["series"][0]["type"] == "pie"
    assert eo["series"][0]["data"][0]["name"] == "A"
