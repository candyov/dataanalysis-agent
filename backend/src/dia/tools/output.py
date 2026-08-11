"""Output tools -- 可视化输出 (Reporter / Analyst)

build_chart: 生成 ECharts 图表
"""

from langchain_core.tools import tool
import json
import logging

logger = logging.getLogger(__name__)


@tool
def build_chart(chart_type: str, data: dict, title: str = "") -> str:
    """生成 ECharts option JSON, 前端自动渲染。

    Args:
        chart_type: bar, line, pie, scatter, heatmap
        data: 图表数据对象。格式取决于图表类型:
            bar/line: {"categories":["A","B"], "series":[{"name":"销量","data":[100,200]}]}
            pie: {"data":[{"name":"华东","value":100},...]}
            scatter: {"x":[1,2,3], "y":[10,20,30]}
            heatmap: {"x":["华东","华北"], "y":["电子产品","食品"], "values":[[100,50],[80,20]]}
                    (values[i][j] = 第 i 个 x × 第 j 个 y 的值)
        title: 图表标题, **必须用中文业务语言写结论**, 如 "华东vs华北营收对比 (p=0.003)",
              禁止英文标题 (如 "sales by region") 或纯字段名.

    图表类型选择规则 (必须遵守, 避免全是柱状图):
      - line: 时间序列/趋势 (按月/日的变化, 如月度营收趋势)
      - pie:  占比结构 (如各区域营收占比, 类别数 ≤ 8)
      - bar:  分组对比 (如各区域营收高低排名, 类别数 ≤ 12)
      - scatter: 两指标相关性 (如销售额 vs 成本散点)
      - heatmap: 双维度交叉 (如区域×品类营收热力图, 用颜色深浅表示强弱)
    """
    if chart_type not in ("bar", "line", "pie", "scatter", "heatmap"):
        return json.dumps({
            "error": f"不支持的图表类型: {chart_type}. 可选: bar / line / pie / scatter / heatmap"
        }, ensure_ascii=False)
    if not isinstance(data, dict):
        return json.dumps({"error": "data 必须是对象 (dict), 不是数组/字符串"}, ensure_ascii=False)

    # 数据结构严格校验 — 格式错误直接报错, 不画空图
    if chart_type in ("bar", "line"):
        categories = data.get("categories") or []
        series = data.get("series") or []
        if not isinstance(categories, list) or not categories:
            return json.dumps({"error": f"{chart_type} 图缺少 categories (非空数组), 如 [\"华东\",\"华北\"]"}, ensure_ascii=False)
        if not isinstance(series, list) or not series:
            return json.dumps({"error": f"{chart_type} 图缺少 series (非空数组), 如 [{{\"name\":\"营收\",\"data\":[100,200]}}]"}, ensure_ascii=False)
        for s in series:
            if not isinstance(s.get("data"), list):
                return json.dumps({"error": f"series 每项必须含 data 数组, 当前: {str(s)[:80]}"}, ensure_ascii=False)
    elif chart_type == "pie":
        pie_data = data.get("data") or []
        if not isinstance(pie_data, list) or not pie_data:
            return json.dumps({"error": "pie 图缺少 data (非空数组), 如 [{\"name\":\"华东\",\"value\":100}]"}, ensure_ascii=False)
        for item in pie_data:
            if not isinstance(item, dict) or "value" not in item:
                return json.dumps({"error": f"pie data 每项必须是 {{name, value}}, 当前: {str(item)[:80]}"}, ensure_ascii=False)
    elif chart_type == "scatter":
        xs, ys = data.get("x") or [], data.get("y") or []
        if not isinstance(xs, list) or not isinstance(ys, list) or not xs or len(xs) != len(ys):
            return json.dumps({"error": "scatter 图需要等长非空的 x 和 y 数组"}, ensure_ascii=False)
    elif chart_type == "heatmap":
        hx, hy = data.get("x") or [], data.get("y") or []
        values = data.get("values") or []
        if not isinstance(hx, list) or not hx or not isinstance(hy, list) or not hy:
            return json.dumps({"error": "heatmap 图需要非空的 x 和 y 数组 (行列标签)"}, ensure_ascii=False)
        if not isinstance(values, list) or len(values) != len(hx):
            return json.dumps({"error": f"heatmap values 行数必须等于 x 数 ({len(hx)}), 每行是长度为 y 数的数组"}, ensure_ascii=False)
        for row in values:
            if not isinstance(row, list) or len(row) != len(hy):
                return json.dumps({"error": f"heatmap values 每行长度必须等于 y 数 ({len(hy)})"}, ensure_ascii=False)

    option = {
        "title": {"text": title or "", "left": "center"},
        "tooltip": {},
        "grid": {"left": "3%", "right": "4%", "bottom": "3%", "containLabel": True},
        "animation": True,
    }

    if chart_type in ("bar", "line"):
        option.update({
            "xAxis": {"type": "category", "data": data.get("categories", [])},
            "yAxis": {"type": "value"},
            "series": [{"name": s.get("name", ""), "type": chart_type, "data": s.get("data", [])}
                      for s in data.get("series", [])],
        })
    elif chart_type == "pie":
        option["series"] = [{"type": "pie", "radius": "60%", "data": data.get("data", [])}]
    elif chart_type == "scatter":
        option.update({
            "xAxis": {"type": "value"},
            "yAxis": {"type": "value"},
            "series": [{"type": "scatter", "data": [[x, y] for x, y in zip(data.get("x", []), data.get("y", []))]}],
        })
    elif chart_type == "heatmap":
        # 交叉热力图: x 轴类别 × y 轴类别, values 矩阵 → [x_idx, y_idx, value] 三元组
        hx, hy = data.get("x", []), data.get("y", [])
        cell_data = []
        for xi in range(len(hx)):
            for yi in range(len(hy)):
                cell_data.append([xi, yi, float(data["values"][xi][yi])])
        option.update({
            "xAxis": {"type": "category", "data": hx, "splitArea": {"show": True}},
            "yAxis": {"type": "category", "data": hy, "splitArea": {"show": True}},
            "visualMap": {"min": 0, "max": None, "calculable": True, "orient": "horizontal",
                          "left": "center", "bottom": 0, "inRange": {"color": ["#f7fbff", "#08519c"]}},
            "series": [{"type": "heatmap", "data": cell_data,
                        "label": {"show": True, "fontSize": 10}}],
        })
        # visualMap max: 用数据最大值 (避免全图一个颜色)
        try:
            flat = [float(v) for row in data["values"] for v in row]
            option["visualMap"]["max"] = max(flat) or 1
        except Exception:
            option["visualMap"]["max"] = 1

    return json.dumps({"chart_type": chart_type, "echarts_option": option}, ensure_ascii=False)
