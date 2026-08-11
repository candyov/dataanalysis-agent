"""report/segments 纯函数测试: 图表编号 + 清单 + 确定性分段

覆盖:
- 编号分配/清单生成
- 编号引用内联 (图N)
- 标题引用模糊匹配 (兼容旧格式)
- 全角括号/无括号变体
- 重复引用去重
- 非法编号保留原文
- 末尾兜底 (未引用图不丢)
- 无图表池回退纯文本
"""

import pytest

from dia.report.segments import (
    assign_chart_ids,
    build_chart_catalog,
    extract_referenced_ids,
    split_report_segments,
)

CHARTS = [
    {"title": "各区域营收对比", "chart_type": "bar", "echarts_option": {"series": [1, 2]}},
    {"title": "月度营收趋势", "chart_type": "line", "echarts_option": {"series": [3, 4]}},
    {"title": "品类营收占比", "chart_type": "pie", "echarts_option": {"series": [5, 6]}},
]


def chart_titles(segs):
    return [s["title"] for s in segs if s["type"] == "chart"]


def text_of(segs):
    return "".join(s.get("text", "") for s in segs if s["type"] == "text")


class TestChartIds:
    def test_assign_ids_sequential(self):
        out = assign_chart_ids(CHARTS)
        assert [c["chart_id"] for c in out] == ["图1", "图2", "图3"]

    def test_assign_does_not_mutate_input(self):
        src = [dict(c) for c in CHARTS]
        assign_chart_ids(src)
        assert all("chart_id" not in c for c in src)

    def test_catalog_lists_id_and_title(self):
        cat = build_chart_catalog(CHARTS)
        assert cat == "图1: 各区域营收对比\n图2: 月度营收趋势\n图3: 品类营收占比"


class TestNumberRefs:
    def test_number_ref_inlines(self):
        segs = split_report_segments("华东领先 (见图: 图1)。", CHARTS)
        assert chart_titles(segs) == ["各区域营收对比", "月度营收趋势", "品类营收占比"]
        # 图1 内联位置: 文本段被拆成两半, 图夹中间
        assert segs[0]["type"] == "text" and "华东领先" in segs[0]["text"]
        assert segs[1]["type"] == "chart" and segs[1]["title"] == "各区域营收对比"
        assert segs[2]["type"] == "text" and segs[2]["text"].startswith("。")

    def test_invalid_number_kept_in_text_and_rest_tailed(self):
        segs = split_report_segments("无此图 (见图: 图99)", CHARTS)
        # 图99 匹配不到 → 原文保留在文本段
        assert "图99" in text_of(segs)
        # 未引用图全部兜底到末尾
        assert chart_titles(segs) == ["各区域营收对比", "月度营收趋势", "品类营收占比"]

    def test_duplicate_ref_inlines_once(self):
        segs = split_report_segments("(见图: 图1) 再 (见图: 图1)", CHARTS)
        titles = chart_titles(segs)
        assert titles.count("各区域营收对比") == 1
        # 未引用的图2/图3 兜底到末尾
        assert titles.count("月度营收趋势") == 1
        assert titles.count("品类营收占比") == 1

    def test_wide_parentheses_and_no_parens(self):
        segs = split_report_segments("（见图：图2）走势 见图: 图1 领先", CHARTS)
        titles = chart_titles(segs)
        assert titles[0] == "月度营收趋势"  # 图2 先出现
        assert titles[1] == "各区域营收对比"  # 图1 后出现

    def test_multi_ref_comma_inlines_all(self):
        segs = split_report_segments("多图 (见图: 图1, 图2) 并列", CHARTS)
        titles = chart_titles(segs)
        # 图1+图2 都内联在同一位置 (紧跟文本段)
        assert titles[:2] == ["各区域营收对比", "月度营收趋势"]
        assert segs[0]["type"] == "text" and "多图" in segs[0]["text"]
        assert segs[1]["type"] == "chart"
        assert segs[2]["type"] == "chart"
        # 未引用的图3 兜底
        assert titles[-1] == "品类营收占比"

    def test_multi_ref_and_inlines_all(self):
        segs = split_report_segments("(见图: 图1和图2)", CHARTS)
        assert chart_titles(segs)[:2] == ["各区域营收对比", "月度营收趋势"]

    def test_multi_ref_wide_comma(self):
        segs = split_report_segments("（见图：图1、图2）", CHARTS)
        assert chart_titles(segs)[:2] == ["各区域营收对比", "月度营收趋势"]


class TestTitleRefs:
    def test_title_fuzzy_match(self):
        segs = split_report_segments("区域差异 (见图: 各区域营收对比)", CHARTS)
        assert chart_titles(segs)[0] == "各区域营收对比"

    def test_title_whitespace_insensitive(self):
        segs = split_report_segments("(见图: 各 区域 营收对比)", CHARTS)
        assert chart_titles(segs)[0] == "各区域营收对比"


class TestFallbacks:
    def test_empty_pool_returns_pure_text(self):
        segs = split_report_segments("纯文本报告", [])
        assert segs == [{"type": "text", "text": "纯文本报告"}]

    def test_no_refs_all_tailed(self):
        segs = split_report_segments("没有任何引用", CHARTS)
        assert chart_titles(segs) == ["各区域营收对比", "月度营收趋势", "品类营收占比"]
        # 兜底区有标题标记
        assert "图表" in text_of(segs)

    def test_tail_section_marker_added_once(self):
        segs = split_report_segments("正文无图引用", CHARTS)
        assert text_of(segs).count("**图表**") == 1


class TestExtractRefs:
    def test_extracts_number_ids(self):
        assert extract_referenced_ids("(见图: 图1) (见图: 图3)") == [1, 3]

    def test_extracts_multi_number_ids(self):
        assert extract_referenced_ids("(见图: 图9, 图10) (见图: 图2和图5)") == [9, 10, 2, 5]

    def test_title_refs_not_extracted(self):
        assert extract_referenced_ids("(见图: 各区域营收对比)") == []

    def test_empty_when_no_refs(self):
        assert extract_referenced_ids("纯文字报告") == []
