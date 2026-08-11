"""report/blueprint merge_analyst_results 测试: 图表分配 + 去重 + 标记剥离

覆盖本次修复的关键行为:
- 图表按标题去重 (LLM 重复画同题图)
- findings 剥离 [强]/[弱]/[推测] 标记
- 全局分配: 每张图只归属一个章节 (cross > time_series > group_compare > yoy > top)
- 中英文维度名映射 (图标题可能用中文或英文)
"""

from dia.report.blueprint import merge_analyst_results


def _blueprint():
    return {
        "chapters": [
            {"id": "quality", "title": "数据质量评估", "type": "quality"},
            {"id": "trend", "title": "时间趋势分析", "type": "time_series", "dimension": "date"},
            {"id": "by_region", "title": "各region对比分析", "type": "group_compare", "dimension": "region"},
            {"id": "by_category", "title": "各category对比分析", "type": "group_compare", "dimension": "category"},
            {"id": "by_channel", "title": "各channel对比分析", "type": "group_compare", "dimension": "channel"},
            {"id": "cross_rc", "title": "region × category 交叉分析", "type": "cross_analysis",
             "dimensions": ["region", "category"]},
            {"id": "cross_cc", "title": "category × channel 交叉分析", "type": "cross_analysis",
             "dimensions": ["category", "channel"]},
            {"id": "yoy", "title": "年度对比分析", "type": "year_over_year", "dimension": "date"},
        ]
    }


def _chart(title, ctype="bar"):
    return {"title": title, "chart_type": ctype, "echarts_option": {"series": [1, 2]}}


def _chart_titles(chapter):
    return [c["title"] for c in chapter.get("charts", [])]


class TestDedupe:
    def test_duplicate_titles_kept_once(self):
        bp = _blueprint()
        merged = merge_analyst_results(bp, {
            "charts": [_chart("sales by region"), _chart("sales by region"),
                       _chart("sales by region")],
            "findings": [], "summary": "",
        })
        region = next(ch for ch in merged["chapters"] if ch["id"] == "by_region")
        assert len(region["charts"]) == 1


class TestStripLevel:
    def test_strip_level_from_findings(self):
        bp = _blueprint()
        merged = merge_analyst_results(bp, {
            "charts": [],
            "findings": [{"claim": "[强] 华东领先", "evidence": "x"},
                         {"claim": "[弱] 月度下滑不显著", "evidence": "y"},
                         "普通发现"],
            "summary": "",
        })
        all_claims = []
        for ch in merged["chapters"]:
            all_claims += [f.get("claim", "") if isinstance(f, dict) else str(f)
                           for f in ch.get("findings", [])]
        joined = " ".join(all_claims)
        assert "[强]" not in joined and "[弱]" not in joined
        assert "华东领先" in joined and "月度下滑不显著" in joined


class TestGlobalAssignment:
    def test_cross_chart_only_in_matching_cross_chapter(self):
        bp = _blueprint()
        merged = merge_analyst_results(bp, {
            "charts": [_chart("区域×品类交叉：华东+电子产品是绝对主力组合")],
            "findings": [], "summary": "",
        })
        cross_rc = next(ch for ch in merged["chapters"] if ch["id"] == "cross_rc")
        cross_cc = next(ch for ch in merged["chapters"] if ch["id"] == "cross_cc")
        region = next(ch for ch in merged["chapters"] if ch["id"] == "by_region")
        assert len(cross_rc["charts"]) == 1
        assert len(cross_cc["charts"]) == 0
        assert len(region["charts"]) == 0  # 交叉图不落单维章节

    def test_english_title_chart_in_group_chapter(self):
        bp = _blueprint()
        merged = merge_analyst_results(bp, {
            "charts": [_chart("sales by region")],
            "findings": [], "summary": "",
        })
        region = next(ch for ch in merged["chapters"] if ch["id"] == "by_region")
        assert _chart_titles(region) == ["sales by region"]

    def test_chinese_title_chart_in_group_chapter(self):
        bp = _blueprint()
        merged = merge_analyst_results(bp, {
            "charts": [_chart("各区域营收对比：华东领跑")],
            "findings": [], "summary": "",
        })
        region = next(ch for ch in merged["chapters"] if ch["id"] == "by_region")
        assert len(region["charts"]) == 1

    def test_trend_chart_in_time_series(self):
        bp = _blueprint()
        merged = merge_analyst_results(bp, {
            "charts": [_chart("月度营收趋势 (2025.07-2026.06)")],
            "findings": [], "summary": "",
        })
        trend = next(ch for ch in merged["chapters"] if ch["id"] == "trend")
        assert len(trend["charts"]) == 1
        # 趋势图不落 group/yoy 章节
        assert all(len(ch.get("charts", [])) == 0 for ch in merged["chapters"]
                   if ch["id"] != "trend")

    def test_each_chart_assigned_once(self):
        """全局分配: 全部图表总分配数 = 去重后图表数 (不重复展示)"""
        bp = _blueprint()
        charts = [
            _chart("月度营收趋势"),
            _chart("各区域营收对比"),
            _chart("各品类营收对比"),
            _chart("渠道营收对比"),
            _chart("区域×品类交叉"),
        ]
        merged = merge_analyst_results(bp, {"charts": charts, "findings": [], "summary": ""})
        total = sum(len(ch.get("charts", [])) for ch in merged["chapters"])
        assert total == len(charts)

    def test_yoy_chart_not_hijacked_by_group(self):
        """含'对比'的年度图: 若无真正年度图, yoy 章节可为空 (不硬塞单维图)"""
        bp = _blueprint()
        merged = merge_analyst_results(bp, {
            "charts": [_chart("各区域营收对比：华东领跑")],
            "findings": [], "summary": "",
        })
        yoy = next(ch for ch in merged["chapters"] if ch["id"] == "yoy")
        # 单维图优先归 group_compare (优先级更高), yoy 不抢
        assert len(yoy["charts"]) == 0
