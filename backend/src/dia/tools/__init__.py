"""Tools registry -- 按 Agent 能力域分组 (能力导向, 不按算法)

DataCurator (探查域):   inspect, assess_quality, probe_date
Analyst (分析域):       explore, test_difference, attribution, forecast, detect_anomalies, visualize
Reporter (叙事域):      (纯 LLM, 无工具)

设计原则:
- 工具按"分析任务"划分, 算法选择由确定性代码决定, LLM 只识别任务+填参数
- 三个域能力零重叠: curator 看结构/质量, analyst 算指标/验证, reporter 只叙事
- 旧探索工具 (drill_down/rank/decompose/detect/segment/compare 等) 已退役,
  保留在 analysis.py 中仅作 fallback 参考, 不再暴露给 LLM
- 代码执行器 (run_pandas/run_sklearn) 已移除: 分析任务可枚举, 确定性工具全覆盖
"""

from dia.tools.data import inspect, query, profile, assess_quality, sample_rows, date_range
from dia.tools.analysis import forecast, seasonal_analysis, compare, detect
from dia.tools.output import build_chart
from dia.tools.explore import explore, test_difference, attribution
# ── DataCurator: 探查域 ──
CURATOR_TOOLS = [inspect, assess_quality, date_range]

# ── Analyst: 分析域 ──
ANALYST_TOOLS = [
    explore, test_difference, attribution,
    forecast, seasonal_analysis, compare, detect,
    build_chart,
]

REPORTER_TOOLS: list = []

ALL_TOOLS = CURATOR_TOOLS + ANALYST_TOOLS + REPORTER_TOOLS

# 兼容导出: 保留旧工具引用 (供 analysis.py 内部/测试引用, 不暴露给 LLM)
from dia.tools.analysis import (  # noqa: E402
    drill_down, find_drivers, decompose, rank, detect, segment,
    hypothesis_test, regression_analysis, explain_anomaly, percentile_analysis, compare,
)

__all__ = [
    "inspect", "assess_quality", "date_range",
    "explore", "test_difference", "attribution", "forecast", "seasonal_analysis", "build_chart",
    "ALL_TOOLS", "CURATOR_TOOLS", "ANALYST_TOOLS", "REPORTER_TOOLS",
    # 兼容
    "query", "profile", "sample_rows", "drill_down", "find_drivers", "decompose", "rank",
    "detect", "segment", "hypothesis_test", "regression_analysis", "explain_anomaly",
    "percentile_analysis", "compare",
]
