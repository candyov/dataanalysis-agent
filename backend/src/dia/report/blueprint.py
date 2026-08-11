"""Report Blueprint — 数据驱动的报告骨架生成器

从 Curator 探查结果中提取维度/指标, 自动推导报告章节结构。
数据里有什么维度, 报告就长成什么形状。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 维度名 → 中英文别名 (图标题可能用中文 "区域×品类交叉" 或英文 "sales by region")
_DIM_ALIASES = {
    "region": ["region", "区域", "地区"],
    "category": ["category", "品类", "类别", "分类"],
    "channel": ["channel", "渠道"],
    "date": ["date", "日期", "时间", "日", "月"],
    "product": ["product", "产品", "商品"],
    "supplier": ["supplier", "供应商"],
    "customer": ["customer", "客户"],
    "sales": ["sales", "销售", "营收", "销售额"],
    "cost": ["cost", "成本"],
    "orders": ["order", "订单"],
}


def _cat_dimensions(glossary: dict) -> list[dict]:
    """从 glossary 中提取分类维度列 (role=dimension)."""
    dims = []
    for name, entry in glossary.items():
        if isinstance(entry, dict) and entry.get("role") == "dimension":
            dims.append({
                "name": name,
                "label": entry.get("label", name),
                "type": "categorical",
                "value_count": entry.get("unique_count", entry.get("value_count", "?")),
                "sample_values": entry.get("sample_values", [])[:10],
            })
    return dims


def _temporal_dimensions(inspect_results: list[dict]) -> list[dict]:
    """从 inspect/date_range 工具结果中提取时间维度."""
    dims = []
    for r in inspect_results:
        entries = r.get("date_columns", [])
        if isinstance(entries, list):
            for e in entries:
                dims.append({
                    "name": e.get("column", ""),
                    "label": e.get("column", ""),
                    "type": "temporal",
                    "grain": e.get("inferred_grain", "day"),
                    "span": f"{e.get('min', '?')} 至 {e.get('max', '?')}",
                })
        elif isinstance(entries, dict):
            for col, info in entries.items():
                dims.append({
                    "name": col,
                    "label": col,
                    "type": "temporal",
                    "grain": info.get("inferred_grain", "day") if isinstance(info, dict) else "day",
                    "span": f"{info.get('min','?')} 至 {info.get('max','?')}" if isinstance(info, dict) else str(info),
                })
    return dims


def _metric_columns(glossary: dict) -> list[dict]:
    """从 glossary 中提取数值指标列 (role=metric), 过滤掉衍生 KPI.

    只保留基础指标 (原始列), 跳过: profit_margin, avg_order_value, share, ratio, growth, arpu 等衍生指标.
    """
    skip_patterns = ('margin', 'avg', 'share', 'ratio', 'growth', 'arpu', 'per_', '_per_')
    results = []
    for name, entry in glossary.items():
        if isinstance(entry, dict) and entry.get("role") == "metric":
            name_lower = name.lower()
            if any(p in name_lower for p in skip_patterns):
                continue
            results.append({
                "name": name, "label": entry.get("label", name),
                "agg": entry.get("agg", "sum"),
            })
    # 最多保留 6 个基础指标
    return results[:6]


def _derive_chapters(
    dims: list[dict],
    metrics: list[dict],
    quality: dict,
    time_span: str,
) -> list[dict]:
    """根据维度类型和数量自动推导报告章节.

    规则:
      - 有 temporal 维度 → 趋势章节 (time_series)
      - 每个 categorical 维度 → 分组对比章节 (group_compare)
      - 2+ categorical 维度 → 交叉分析章节 (cross_analysis, 两两组合)
      - temporal + 跨年 → 年度对比章节 (year_over_year)
      - 有 metric → Top N 章节 (top_n)
    """
    chapters = []
    cat_dims = [d for d in dims if d.get("type") == "categorical"]
    time_dims = [d for d in dims if d.get("type") == "temporal"]

    # 1. 时间趋势 (有 temporal 维度时)
    if time_dims:
        td = time_dims[0]
        chapters.append({
            "id": "trend",
            "title": "时间趋势分析",
            "type": "time_series",
            "dimension": td["name"],
            "grain": td.get("grain", "month"),
            "description": f"按{td.get('grain', 'month')}粒度展示 {td.get('span', '')} 的变化趋势。",
        })

    # 2. 分组对比 (每个 categorical 维度)
    for d in cat_dims:
        chapters.append({
            "id": f"by_{d['name']}",
            "title": f"各{d['label']}对比分析",
            "type": "group_compare",
            "dimension": d["name"],
            "description": f"按{d['label']}维度拆解核心指标, 识别头部与尾部差异。",
        })

    # 3. 交叉分析 (2 个 categorical dims 两两组合, 只取前2对)
    if len(cat_dims) >= 2:
        for i in range(min(len(cat_dims) - 1, 2)):
            d1, d2 = cat_dims[i], cat_dims[i + 1]
            chapters.append({
                "id": f"cross_{d1['name']}_{d2['name']}",
                "title": f"{d1['label']} × {d2['label']} 交叉分析",
                "type": "cross_analysis",
                "dimensions": [d1["name"], d2["name"]],
                "description": f"识别不同{d1['label']}在不同{d2['label']}上的表现差异, 发现优势组合。",
            })

    # 4. 年度对比 (temporal + 跨度跨年)
    if time_dims:
        span = time_dims[0].get("span", "")
        years = set()
        for part in span.replace("至", " ").split():
            part = part.strip("- ")
            if len(part) >= 4 and part[:4].isdigit():
                years.add(part[:4])
        if len(years) >= 2:
            chapters.append({
                "id": "yoy",
                "title": "年度对比分析",
                "type": "year_over_year",
                "dimension": time_dims[0]["name"],
                "description": f"跨年度核心指标对比 ({', '.join(sorted(years))})。",
            })

    # 5. Top N (有 metric 时)
    if metrics:
        top_metric = metrics[0]
        chapters.append({
            "id": "top_n",
            "title": f"Top 10 {top_metric['label']}最高记录",
            "type": "top_n",
            "dimension": top_metric["name"],
            "description": f"识别{top_metric['label']}最高的10条记录, 分析大单特征。",
        })

    # 6. 数据质量 (总是放最前)
    chapters.insert(0, {
        "id": "quality",
        "title": "数据质量评估",
        "type": "quality",
        "description": "原始数据完整性、一致性和准确性校验结果。",
    })

    return chapters


def build_blueprint(
    glossary: dict,
    inspect_results: list[dict],
    quality: dict,
    overview: dict,
) -> dict:
    """从 Curator 探查结果构建报告蓝图.

    Args:
        glossary: 列 → {name, label, role, ...} 映射 (从 Curator 的 inspect full 或 KPI tree 推断)
        inspect_results: 工具执行结果的原始数据列表 (含 date_columns 等)
        quality: 质量评估结果 {grade, blockers, degraded, ...}
        overview: 数据概览 {tables, time_span, row_count, ...}

    Returns:
        report_blueprint dict: {dimensions, metrics, chapters, overview, quality}
    """
    dims = _cat_dimensions(glossary) + _temporal_dimensions(inspect_results)
    metrics = _metric_columns(glossary)
    time_span = overview.get("time_span", "") or overview.get("tables", "")

    chapters = _derive_chapters(dims, metrics, quality, time_span)

    blueprint = {
        "dimensions": dims,
        "metrics": metrics,
        "chapters": chapters,
        "overview": {
            "row_count": overview.get("row_count", "?"),
            "table_count": overview.get("table_count", 1),
            "time_span": time_span,
        },
        "quality": {
            "grade": quality.get("grade", "B"),
            "blockers": quality.get("blockers", [])[:5],
            "degraded": quality.get("degraded", [])[:5],
        },
    }

    logger.info(f"[Blueprint] 生成章节: {len(chapters)} 个, 维度: {len(dims)}, 指标: {len(metrics)}")
    return blueprint


def merge_analyst_results(blueprint: dict, analysis_bag: dict) -> dict:
    """将 Analyst 的分析结果注入蓝图 chapters.

    analysis_bag: {"charts": [...], "findings": [...], "summary": "..."}

    图表全局分配 (每张图只归属一个章节, 按优先级 cross > time_series > group_compare
    > year_over_year > top_n), 避免一图多属/重复展示:
      - cross_analysis: 标题同时含两个维度名 (中英文 _DIM_ALIASES) 才进
      - time_series:    含"趋势"/"trend"/"时间"/"时序" 或 维度名
      - group_compare:  含该维度名 (中文"区域"/英文"region"均可)
      - year_over_year: 含"年"/"yoy"/"同期"/"对比"
      - top_n:          含"top"/"排行"/"大单"/"前十"
    其他处理:
      - 图表按标题去重 (LLM 可能重复画同题图)
      - findings 剥离 [强]/[弱]/[推测] 标记 (报告面向决策者)
    """
    chapters = blueprint.get("chapters", [])
    charts = analysis_bag.get("charts", [])

    # 图表去重: 同标题只保留第一张 (LLM 重复 build_chart 画同题图)
    seen_titles: set[str] = set()
    unique_charts = []
    for c in charts:
        t = (c.get("title") or "").strip()
        if not t:
            continue
        if t in seen_titles:
            continue
        seen_titles.add(t)
        unique_charts.append(c)
    if len(unique_charts) < len(charts):
        logger.info(f"[Blueprint] 图表去重: {len(charts)} → {len(unique_charts)}")

    # 给每个 chapter 匹配图表 — 全局分配: 每张图只归属一个章节 (按优先级
    # cross > time_series > group_compare > yoy > top_n), 避免一图多属/重复展示
    _TYPE_PRIORITY = {"cross_analysis": 0, "time_series": 1, "group_compare": 2,
                      "year_over_year": 3, "top_n": 4}
    ordered_chapters = sorted(chapters, key=lambda ch: _TYPE_PRIORITY.get(ch.get("type", ""), 9))
    for ch in chapters:
        ch["charts"] = []
    assigned_titles: set[str] = set()

    for c in unique_charts:
        title = (c.get("title") or "").lower()

        def _dim_in_title(dim: str) -> bool:
            aliases = _DIM_ALIASES.get(dim, [dim])
            return any(a in title for a in aliases)

        # 按优先级找第一个匹配章节
        target = None
        for ch in ordered_chapters:
            ch_type = ch.get("type", "")
            ch_dim = ch.get("dimension", "")
            ch_dims = ch.get("dimensions", [])
            hit = False
            if ch_type == "cross_analysis":
                # 交叉图: 标题同时含两个维度名才进 (避免 "区域×品类" 进 region 单维章节)
                hit = bool(ch_dims) and all(_dim_in_title(d) for d in ch_dims)
            elif ch_type == "time_series":
                hit = any(kw in title for kw in ("趋势", "trend", "时间", "时序")) or (
                    ch_dim and _dim_in_title(ch_dim))
            elif ch_type == "group_compare":
                # 单维图: 含该维度名即命中 (交叉图已由更高优先级的 cross 章节先捕获,
                # 落到这里的只可能是无对应 cross 章节的组合, 归入首个匹配维度即可)
                hit = bool(ch_dim) and _dim_in_title(ch_dim)
            elif ch_type == "year_over_year":
                hit = any(kw in title for kw in ("年", "yoy", "同期", "对比"))
            elif ch_type == "top_n":
                hit = any(kw in title for kw in ("top", "排行", "大单", "前十"))
            if hit:
                target = ch
                break
        if target is None:
            continue
        target["charts"].append(c)
        assigned_titles.add(title)

    # findings → 分配章节前剥离 [强]/[弱]/[推测] 标记 (报告面向决策者)
    findings = []
    for f in analysis_bag.get("findings", []):
        if isinstance(f, dict):
            f = {**f, "claim": _strip_level(f.get("claim", ""))}
        else:
            f = _strip_level(str(f))
        findings.append(f)

    # findings → 分配到章节 (保守匹配, 没匹配到的放 summary)
    for ch in chapters:
        ch["findings"] = []

    unassigned = []
    for f in findings:
        claim = (f.get("claim", "") if isinstance(f, dict) else str(f)).lower()
        assigned = False
        for ch in chapters:
            ch_dim = ch.get("dimension", "")
            if ch_dim and ch_dim.lower() in claim:
                ch["findings"].append(f)
                assigned = True
                break
        if not assigned:
            unassigned.append(f)

    # 未匹配的归入第一个非 quality chapter 的 findings
    if unassigned and chapters:
        for ch in chapters:
            if ch.get("type") != "quality":
                ch["findings"].extend(unassigned)
                break

    return blueprint


def _strip_level(text: str) -> str:
    """剥离 [强]/[弱]/[推测] 分级标记 — 报告面向决策者, 置信度用业务语言表达."""
    import re
    return re.sub(r"\[(强|弱|推测)\]\s*", "", text)
