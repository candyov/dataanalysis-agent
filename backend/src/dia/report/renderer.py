"""HTML 报告渲染器 — 把分析会话产物渲染成独立 HTML 报告文件.

数据 100% 来自本项目产出 (session state):
  - analysis.summary                      → 执行摘要
  - analysis.structured_data.findings     → 关键发现 (带 [强]/[弱]/[推测] 分级)
  - shared_context.charts                 → 图表 (echarts_option 格式)
  - shared_context.curator_report         → 数据质量/口径说明
  - analysis.structured_data (KPI)        → KPI 卡片数据

视觉布局参考 WorkBuddy 报告 (KPI 卡片/图表网格/分区), 但数据契约是本项目的.
图表复用 ECharts (本项目 charts 已是 echarts_option), 不引入 Chart.js.
"""

from __future__ import annotations

import html
import json
import logging
import re
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


def _safe_eo_json(eo: dict) -> str:
    """序列化 echarts_option 为可嵌入 <script> 的安全 JSON.

    json.dumps 默认不转义 `<` — 数据来自用户上传文件/LLM 生成,
    含 `</script>` 时可直接闭合 script 标签注入 HTML/JS。
    将 `<` 转义为 \\u003c (JSON 合法转义, JS 解析后仍是原字符, ECharts 无感).
    """
    return json.dumps(eo, ensure_ascii=False).replace("<", "\\u003c")

# ══════════════════════════════════════════════════════════════════
# 数据提取 (从 session state → 渲染数据包)
# ══════════════════════════════════════════════════════════════════

# 图表标题业务化: 英文变量名 → 中文业务标题
_CHART_TITLE_MAP = {
    "sales by region": "各区域销售额对比",
    "sales by category": "各品类销售额对比",
    "sales by channel": "各渠道销售额对比",
    "sales trend": "销售趋势",
    "sales 趋势": "月度销售趋势",
    "sales forecast": "销售预测",
    "sales 预测": "销售预测",
    "sales by date": "销售趋势",
    "sales 分组对比": "销售分组差异检验",
    "revenue by region": "各区域营收对比",
    "revenue by category": "各品类营收对比",
    "revenue trend": "营收趋势",
    "profit by region": "各区域利润对比",
    "profit by category": "各品类利润对比",
}


def _business_title(title: str) -> str:
    """图表标题业务化: 英文/变量名 → 中文业务标题."""
    if not title:
        return ""
    # 精确映射
    if title in _CHART_TITLE_MAP:
        return _CHART_TITLE_MAP[title]
    # 模式映射: "X by Y" → "各Y的X对比"
    m = re.match(r"(.+?)\s+by\s+(.+)", title)
    if m:
        metric = m.group(1).replace("_", " ")
        dim = m.group(2).replace("_", " ")
        metric_cn = {"sales": "销售额", "revenue": "营收", "profit": "利润",
                     "orders": "订单量", "customers": "客户数", "cost": "成本"}.get(metric, metric)
        dim_cn = {"region": "区域", "category": "品类", "channel": "渠道",
                  "date": "日期", "month": "月份", "supplier": "供应商"}.get(dim, dim)
        return f"各{dim_cn}{metric_cn}对比"
    # 含 趋势/预测/对比 的中文标题直接保留
    if any(kw in title for kw in ("趋势", "预测", "对比", "占比", "分布")):
        return title
    return title


def _extract_kpis(state: dict) -> list[dict]:
    """从多源提取 KPI 卡片数据.

    优先级:
      1. 实时查询数据源算 KPI (总营收/利润率/订单/客户 — 最可靠)
      2. 文本源提取 (curator_report / analysis.summary / reporter.summary)
    返回: [{"label": "总营收", "value": "6030.5万", "detail": ""}]
    """
    kpis: list[dict] = []

    # 1. 实时查询数据源 (有 source_id 且有 KPI 能力时) — 最可靠, 优先采用
    kpis = _extract_kpis_from_db(state)
    if kpis:
        # 2. 文本源兜底只**补齐缺失 label** (不覆盖 DB 结果, 防正则误匹配混入错值)
        text_kpis = _extract_kpis_from_text(state)
        seen = {k["label"] for k in kpis}
        for k in text_kpis:
            if k["label"] not in seen:
                kpis.append(k)
                seen.add(k["label"])
        return kpis[:6]

    # 无 DB 能力时纯文本兜底
    return _extract_kpis_from_text(state)[:6]


def _extract_kpis_from_text(state: dict) -> list[dict]:
    """从文本源 (curator_report / analysis.summary / reporter.summary) 正则提取 KPI.

    单位强制 (万|元 / 笔|单|个 / 人|位): 防"占比48.4%""p=0.0744"等派生值误匹配;
    负向前瞻排除"每单销售额"(=客单价) 与"18个月"(=跨度).
    """
    kpis: list[dict] = []
    shared = state.get("shared_context", {}) or {}
    data = state.get("data", {}) or {}
    analysis = state.get("analysis", {}) or {}
    reporter = state.get("reporter", {}) or {}

    texts = []
    for src in (shared.get("curator_report", ""), data.get("curator_report", ""),
                analysis.get("summary", ""), reporter.get("summary", "")):
        if isinstance(src, dict):
            src = json.dumps(src, ensure_ascii=False, default=str)
        if src and isinstance(src, str):
            texts.append(src)

    kpi_patterns = [
        (r"总营收[^0-9]*?([\d,]+\.?\d*)\s*(万|元)", "总营收"),
        (r"(?<!每单)(?<!平均每单)(?:总)?销售额[^0-9]*?([\d,]+\.?\d*)\s*(万|元)", "总销售额"),
        (r"利润[率]?[^0-9]*?([\d,]+\.?\d*)\s*(%|万|元)?", "利润率"),
        (r"订单[数]?[^0-9]*?([\d,]+\.?\d*)\s*(?!个月)(笔|单|个)", "订单数"),
        (r"客户[数]?[^0-9]*?([\d,]+\.?\d*)\s*(人|位)", "客户数"),
        (r"成本[^0-9]*?([\d,]+\.?\d*)\s*(万|元)", "总成本"),
        (r"客单价[^0-9]*?([\d,]+\.?\d*)\s*(元)?", "客单价"),
    ]

    def _add(label: str, val: str, unit: str) -> bool:
        if any(k["label"] == label for k in kpis):
            return False
        kpis.append({"label": label, "value": f"{val}{unit}", "detail": ""})
        return True

    for text in texts:
        for pat, label in kpi_patterns:
            if any(k["label"] == label for k in kpis):
                continue
            m = re.search(pat, text)
            if m:
                val = m.group(1)
                unit = m.group(2) if m.lastindex and m.lastindex >= 2 else ""
                val = val.rstrip(",").strip()
                if val:
                    _add(label, val, unit)
        if len(kpis) >= 6:
            break
    return kpis[:6]


def _extract_kpis_from_db(state: dict) -> list[dict]:
    """实时查询数据源计算 KPI (总营收/利润率/订单/客户)."""
    try:
        from dia.infrastructure.database.manager import get_datasource_manager
        source_id = (state.get("source_id")
                     or (state.get("shared_context") or {}).get("source_id")
                     or (state.get("data") or {}).get("source_id", ""))
        # 从 messages 里找 (curator 的 inspect 调用参数含 source_id)
        if not source_id:
            import re as _re
            for m in state.get("messages", []) or []:
                if not isinstance(m, dict):
                    continue
                # 持久化消息结构: {role, tool, args, result} 或 {role, text}
                args = m.get("args")
                if isinstance(args, dict) and args.get("source_id"):
                    source_id = args["source_id"]
                    break
                # tool_call 参数 (内存结构)
                for tc in (m.get("tool_calls") or []):
                    a = tc.get("args", {}) if isinstance(tc, dict) else {}
                    if isinstance(a, dict) and a.get("source_id"):
                        source_id = a["source_id"]
                        break
                if source_id:
                    break
                # 文本内容
                content = str(m.get("content", "") or m.get("text", ""))
                m2 = _re.search(r"source_id[\"':=\s]+([A-Za-z0-9_\-]+)", content)
                if m2:
                    source_id = m2.group(1)
                    break
        if not source_id:
            return []
        mgr = get_datasource_manager()
        conn = mgr.connect(source_id)
        tables = conn.list_tables()
        if not tables:
            return []
        # 找第一个有数值列的表 (通常主表)
        schema = conn.get_schema()
        kpis: list[dict] = []
        # 排序: 优先含 sales/revenue/amount 列的表 (主表), 其次含 orders/customers
        def _table_score(t: str) -> int:
            names = [c["name"].lower() for c in schema.get(t, {}).get("columns", [])]
            if any(n in ("sales", "revenue", "amount", "gmv", "销售额", "营收") for n in names):
                return 2
            if any(n in ("orders", "customers", "订单", "客户") for n in names):
                return 1
            return 0
        tables = sorted(tables, key=_table_score, reverse=True)
        for t in tables[:3]:
            cols = schema.get(t, {}).get("columns", [])
            num_cols = [c["name"] for c in cols if c.get("type", "").lower().split("(")[0] in (
                "real", "integer", "float", "double", "decimal", "numeric", "int", "number", "bigint", "smallint")]
            if not num_cols:
                continue
            # 主指标列: 优先 sales/revenue/amount/销售额/营收
            metric = next((c for c in num_cols if c.lower() in ("sales", "revenue", "amount", "gmv", "销售额", "营收")), num_cols[0])
            cost_col = next((c for c in num_cols if c.lower() in ("cost", "costs", "成本")), "")
            order_col = next((c for c in num_cols if c.lower() in ("orders", "order_count", "订单数", "订单")), "")
            cust_col = next((c for c in num_cols if c.lower() in ("customers", "customer_count", "客户数", "客户")), "")

            # 全量聚合 (SQL 层, 快)
            agg_parts = [f"SUM({metric}) AS total_metric"]
            if cost_col:
                agg_parts.append(f"SUM({cost_col}) AS total_cost")
            if order_col:
                agg_parts.append(f"SUM({order_col}) AS total_orders")
            if cust_col:
                agg_parts.append(f"SUM({cust_col}) AS total_customers")
            result = conn.query(f"SELECT {', '.join(agg_parts)} FROM {t}", max_rows=None)
            if "error" in result or not result.get("rows"):
                continue
            row = result["rows"][0]
            total = float(row.get("total_metric") or 0)
            if total <= 0:
                continue

            def _fmt(v: float) -> str:
                if abs(v) >= 10000:
                    return f"{v/10000:.1f}万"
                return f"{v:,.0f}"

            kpis.append({"label": "总营收", "value": _fmt(total), "detail": ""})
            kpis.append({"label": "总销售额", "value": _fmt(total), "detail": ""})
            cost = float(row.get("total_cost") or 0)
            if cost > 0:
                kpis.append({"label": "利润率", "value": f"{(total-cost)/total*100:.1f}%", "detail": ""})
            if order_col and row.get("total_orders"):
                kpis.append({"label": "订单数", "value": _fmt(float(row["total_orders"])), "detail": ""})
            if cust_col and row.get("total_customers"):
                kpis.append({"label": "客户数", "value": _fmt(float(row["total_customers"])), "detail": ""})
            if kpis:
                break
        return kpis
    except Exception as e:
        logger.warning(f"[renderer] KPI 实时查询失败: {e}")
        return []


def _extract_findings(state: dict) -> list[dict]:
    """提取关键发现 (带分级).

    优先级: structured_data.findings > summary 里的 [强]/[弱]/[推测] 行
    """
    analysis = state.get("analysis", {}) or {}
    sd = analysis.get("structured_data", {}) or {}
    findings = sd.get("findings", []) or []
    out = []
    for f in findings:
        if isinstance(f, str):
            f = {"claim": f}
        claim = f.get("claim", "") if isinstance(f, dict) else str(f)
        if not claim:
            continue
        # 从 claim 提取分级 [强]/[弱]/[推测]
        level = "弱"
        m = re.search(r"\[(强|弱|推测)\]", claim)
        if m:
            level = m.group(1)
            claim = claim.replace(f"[{m.group(1)}]", "").strip()
        conf = f.get("confidence", 0.5) if isinstance(f, dict) else 0.5
        out.append({
            "claim": claim,
            "evidence": f.get("evidence", "") if isinstance(f, dict) else "",
            "confidence": conf,
            "level": level,
        })

    # 兜底: 从 summary 提取分级行
    if not out or (len(out) == 1 and "\n" in str(out[0].get("claim", ""))):
        # findings[0].claim 可能是整个 summary 的多行文本 (synthesize 塞入), 逐行拆
        summary = analysis.get("summary", "") or ""
        multi = ""
        if findings and isinstance(findings[0], dict):
            multi = findings[0].get("claim", "")
            if multi.count("\n") > summary.count("\n"):
                summary = multi
        out = []
        for line in summary.split("\n"):
            line = line.strip()
            m = re.match(r"^\[?(强|弱|推测)\]?\s*(.+)$", line)
            if m:
                out.append({
                    "claim": m.group(2)[:200],
                    "evidence": "summary",
                    "confidence": 0.6 if m.group(1) == "强" else 0.4,
                    "level": m.group(1),
                })
    return out[:10]


def _extract_charts(state: dict) -> list[dict]:
    """提取图表 (echarts_option 格式, 含 fallback 的 chart_data).

    优先级:
      1. reporter.charts / shared_context.charts 里带 echarts_option 的
      2. analysis.chart_data (规则提取, 有 categories/data) → 转 echarts_option
         (reporter.charts 常只有 title/chart_type, echarts_option=null, 数据在 chart_data)
    """
    reporter = state.get("reporter", {}) or {}
    shared = state.get("shared_context", {}) or {}
    analysis = state.get("analysis", {}) or {}
    charts = (
        reporter.get("charts") or shared.get("charts") or analysis.get("charts") or []
    )
    out = []
    seen_titles = set()
    for c in charts:
        if isinstance(c, str):
            try:
                c = json.loads(c)
            except Exception:
                continue
        if not isinstance(c, dict):
            continue
        title = c.get("title", c.get("name", ""))
        eo = c.get("echarts_option") or c.get("option")
        if not eo:
            # chart_data 结构 (categories/series/data) → 转 echarts_option
            eo = _chart_data_to_echarts(c)
        if eo:
            # 业务化标题 (英文变量名 → 中文)
            biz_title = _business_title(str(title))
            # ECharts option 里也放业务标题
            eo.setdefault("title", {})["text"] = biz_title or "图表"
            out.append({"title": biz_title, "echarts_option": eo})
            if biz_title:
                seen_titles.add(biz_title)

    # 兜底: analysis.chart_data (有数据的规则提取) → 转 echarts_option
    # 只补 reporter.charts 里 echarts_option 为空的图
    chart_data = analysis.get("chart_data") or []
    for c in chart_data:
        if not isinstance(c, dict):
            continue
        title = c.get("title", "")
        # 该标题的图已有 echarts_option → 跳过
        if title and title in seen_titles:
            continue
        eo = _chart_data_to_echarts(c)
        if eo:
            biz_title = _business_title(str(title))
            eo.setdefault("title", {})["text"] = biz_title or "图表"
            out.append({"title": biz_title, "echarts_option": eo})
            if biz_title:
                seen_titles.add(biz_title)
    return out[:10]


def _chart_data_to_echarts(c: dict) -> dict | None:
    """把简单 chart_data 结构 (categories/series) 转 echarts_option."""
    ctype = c.get("chart_type", "bar")
    categories = c.get("categories") or []
    series_raw = c.get("series") or []
    data = c.get("data") or []
    if not categories and not data:
        return None
    series = []
    if series_raw:
        for s in series_raw:
            series.append({"type": ctype, "name": s.get("name", ""), "data": s.get("data", [])})
    elif data:
        if ctype == "pie":
            series.append({"type": "pie", "data": data, "radius": "60%"})
        else:
            series.append({"type": ctype, "data": data})
    return {
        "title": {"text": c.get("title", "")},
        "tooltip": {"trigger": "axis" if ctype != "pie" else "item"},
        "legend": {"data": [s.get("name", "") for s in series] or None},
        "xAxis": {"type": "category", "data": categories} if categories and ctype != "pie" else None,
        "yAxis": {"type": "value"} if ctype != "pie" else None,
        "series": series,
    }


def _extract_quality(state: dict) -> dict:
    """提取数据质量说明.

    优先级: data.quality_score (数值, 可靠) > 文本正则
    """
    shared = state.get("shared_context", {}) or {}
    data = state.get("data", {}) or {}
    curator = shared.get("curator_report", "") or data.get("curator_report", "") or ""
    if isinstance(curator, dict):
        curator = json.dumps(curator, ensure_ascii=False, default=str)
    elif not isinstance(curator, str):
        curator = str(curator)
    grade = "?"
    # 优先: shared_context.data_quality_score (curator 写, 唯一真源);
    # data.quality_score 是历史死字段, 仅作旧会话回退
    score = shared.get("data_quality_score")
    if score is None:
        score = data.get("quality_score")
    if score is not None:
        try:
            score = float(score)
            grade = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D"
        except (TypeError, ValueError):
            pass
    # 兜底: 文本正则
    if grade == "?":
        m = re.search(r"(?:质量等级|质量评级|grade)[^\n]*?([A-D]级?)", curator)
        if m:
            grade = m.group(1)
    report = (curator[:800] if curator
              else str(shared.get("quality_report", "") or "")[:800]
              or f"质量评分: {score if score is not None else '?'}")
    return {"grade": grade, "report": report}


def _extract_meta(state: dict) -> dict:
    """提取报告元信息 (数据源/时间/请求)."""
    shared = state.get("shared_context", {}) or {}
    data = state.get("data", {}) or {}
    return {
        "request": state.get("user_request", "数据分析报告"),
        "source_id": state.get("source_id", shared.get("source_id", data.get("source_id", ""))),
        "tables": data.get("tables", []),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def _extract_conclusions(state: dict) -> list[str]:
    """从 reporter.report 提取核心结论 (一、核心结论 / 1. 执行摘要 部分)."""
    reporter = state.get("reporter", {}) or {}
    shared = state.get("shared_context", {}) or {}
    report = reporter.get("report", "") or shared.get("final_report", "") or ""
    if not report:
        return []
    # 兼容新旧格式: "一、核心结论" / "1. 执行摘要"
    m = re.search(r"#+\s*(?:一、|1[.、]?)\s*(核心结论|执行摘要)[^\n]*\n(.*?)(?=\n#+\s|$)", report, re.S)
    if not m:
        m = re.search(r"^#+\s*.*\n((?:[-*]|\*\*).{5,200}(?:\n|$)){1,8}", report, re.S)
    if not m:
        return []
    lines = []
    # group(1) 是标题(执行摘要), group(2) 是内容列表
    for line in m.group(2).strip().split("\n"):
        line = line.strip().lstrip("-*").strip()
        if line and len(line) > 8 and ":" not in line[:3]:
            lines.append(line[:200])
    return lines[:5]


def _extract_suggestions(state: dict) -> list[dict]:
    """从 reporter.report 提取行动建议 (五、行动建议 部分)."""
    reporter = state.get("reporter", {}) or {}
    shared = state.get("shared_context", {}) or {}
    report = reporter.get("report", "") or shared.get("final_report", "") or ""
    if not report:
        return []
    m = re.search(r"#+\s*(?:五、|5[.、]?)\s*行动建议[^\n]*\n(.*?)(?=\n#+\s|$)", report, re.S)
    if not m:
        return []
    out = []
    for line in m.group(1).strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.lstrip("-*").strip()
        # 提取优先级
        prio = "中"
        mp = re.search(r"\[(高|中|低)\]", line)
        if mp:
            prio = mp.group(1)
            line = line.replace(f"[{prio}]", "").strip()
        if len(line) > 10:
            out.append({"priority": prio, "text": line[:250]})
    return out[:6]


def build_report_data(state: dict) -> dict:
    """session state → 渲染数据包.

    优先使用 report_blueprint (Curator 探查 + Analyst 分析后), 兜底旧逻辑.
    """
    shared = state.get("shared_context", {}) or {}
    blueprint = shared.get("report_blueprint")

    if blueprint and blueprint.get("chapters"):
        return _build_from_blueprint(state, blueprint)

    # 兜底: 旧逻辑
    analysis = state.get("analysis", {}) or {}
    reporter = state.get("reporter", {}) or {}
    summary = reporter.get("summary", "") or analysis.get("summary", "")
    return {
        "meta": _extract_meta(state),
        "kpis": _extract_kpis(state),
        "findings": _extract_findings(state),
        "charts": _extract_charts(state),
        "quality": _extract_quality(state),
        "summary": summary,
        "conclusions": _extract_conclusions(state),
        "suggestions": _extract_suggestions(state),
        "_mode": "legacy",
    }


def _build_from_blueprint(state: dict, blueprint: dict) -> dict:
    """从 report_blueprint 构建渲染数据包."""
    chapters = blueprint.get("chapters", [])

    # 收集所有图表 (扁平化, 含 echarts_option)
    all_charts = []
    for ch in chapters:
        for c in (ch.get("charts") or []):
            if c not in all_charts:
                all_charts.append(c)

    # 收集所有 findings
    all_findings = []
    for ch in chapters:
        for f in (ch.get("findings") or []):
            if f not in all_findings:
                all_findings.append(f)

    # KPI 从蓝图维度+指标推导, 或旧逻辑兜底
    kpis = _extract_kpis(state)

    quality = blueprint.get("quality", {})
    quality_data = {
        "grade": quality.get("grade", "B"),
        "blockers": quality.get("blockers", []),
        "degraded": quality.get("degraded", []),
        "report": "",
    }

    return {
        "meta": _extract_meta(state),
        "kpis": kpis,
        "findings": all_findings,
        "charts": all_charts,
        "quality": quality_data,
        "conclusions": [],  # 由 Reporter markdown 提取, 或章节 findings 兜底
        "suggestions": _extract_suggestions(state),
        "_mode": "blueprint",
        "_chapters": chapters,  # 核心: 章节驱动渲染
        "_overview": blueprint.get("overview", {}),
        "_dimensions": blueprint.get("dimensions", []),
        "_metrics": blueprint.get("metrics", []),
    }


# ══════════════════════════════════════════════════════════════════
# HTML 模板
# ══════════════════════════════════════════════════════════════════

_CSS = """
:root { --blue:#2563eb; --green:#16a34a; --orange:#d97706; --purple:#7c3aed; --teal:#0891b2; --red:#dc2626;
  --bg:#f5f7fa; --card-bg:#fff; --text:#1e293b; --text-secondary:#64748b; --border:#e2e8f0;
  --primary-light:#dbeafe; --success-light:#dcfce7; --warning-light:#fef3c7; --danger-light:#fee2e2;
  --shadow:0 1px 3px rgba(0,0,0,.08); }
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:'PingFang SC','Microsoft YaHei',sans-serif; background:var(--bg); color:var(--text); padding:24px; line-height:1.6; }
.container { max-width:1200px; margin:0 auto; }
.report-header { background:linear-gradient(135deg,#1e40af,#3b82f6); color:#fff; border-radius:16px; padding:32px 40px; margin-bottom:24px; box-shadow:0 4px 12px rgba(30,64,175,.2); }
.report-header h1 { font-size:28px; font-weight:700; margin-bottom:8px; }
.report-header .subtitle { font-size:14px; opacity:.85; }
.report-header .meta { display:flex; gap:24px; margin-top:16px; font-size:13px; opacity:.9; flex-wrap:wrap; }
.kpi-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:16px; margin-bottom:24px; }
.kpi-card { background:var(--card-bg); border-radius:12px; padding:20px 24px; box-shadow:var(--shadow); border-left:4px solid var(--blue); transition:transform .2s; }
.kpi-card:hover { transform:translateY(-2px); }
.kpi-card.green { border-left-color:var(--green); } .kpi-card.orange { border-left-color:var(--orange); } .kpi-card.purple { border-left-color:var(--purple); }
.kpi-label { font-size:13px; color:var(--text-secondary); margin-bottom:6px; }
.kpi-value { font-size:26px; font-weight:700; color:var(--text); }
.kpi-sub { font-size:12px; color:var(--text-secondary); margin-top:4px; }
.badge { display:inline-block; padding:2px 10px; border-radius:12px; font-size:12px; font-weight:600; margin-left:8px; }
.badge-A,.badge-a { background:#dcfce7; color:#166534; } .badge-B,.badge-b { background:#fef9c3; color:#854d0e; }
.badge-C,.badge-c { background:#ffedd5; color:#9a3412; } .badge-D,.badge-d { background:#fee2e2; color:#991b1b; }
.section { background:var(--card-bg); border-radius:12px; padding:24px 28px; margin-bottom:24px; box-shadow:var(--shadow); }
.section-title { font-size:18px; font-weight:700; margin-bottom:4px; display:flex; align-items:center; gap:8px; }
.section-title .icon { font-size:22px; }
.section-desc { font-size:13px; color:var(--text-secondary); margin-bottom:20px; }
.chart-row { display:grid; grid-template-columns:repeat(auto-fit,minmax(340px,1fr)); gap:20px; margin-bottom:16px; }
.chart-box { background:#fafbfc; border-radius:10px; padding:16px; border:1px solid var(--border); }
.chart-box h4 { font-size:14px; font-weight:600; margin-bottom:12px; color:var(--text); }
.chart-wrapper { position:relative; height:300px; }
.chart-wrapper.tall { height:400px; }
.dq-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:14px; margin-bottom:16px; }
.dq-item { background:#fafbfc; border-radius:10px; padding:16px 20px; border:1px solid var(--border); }
.dq-item.warning { border-color:var(--orange); background:var(--warning-light); }
.dq-item.danger { border-color:var(--red); background:var(--danger-light); }
.dq-item.ok { border-color:var(--green); background:var(--success-light); }
.dq-label { font-size:13px; color:var(--text-secondary); }
.dq-value { font-size:22px; font-weight:700; }
.dq-detail { font-size:12px; color:var(--text-secondary); margin-top:4px; }
.insight-box { background:var(--primary-light); border-left:4px solid var(--blue); border-radius:8px; padding:16px 20px; margin-top:16px; }
.insight-box h5 { font-size:14px; font-weight:700; margin-bottom:8px; }
.insight-box ul { padding-left:20px; }
.insight-box li { font-size:13px; margin-bottom:4px; }
.insight-box.warning { background:var(--warning-light); border-left-color:var(--orange); }
.insight-box.success { background:var(--success-light); border-left-color:var(--green); }
.data-table { width:100%; border-collapse:collapse; font-size:13px; }
.data-table th { background:#f1f5f9; padding:10px 12px; text-align:left; font-weight:600; color:var(--text-secondary); border-bottom:2px solid var(--border); white-space:nowrap; }
.data-table td { padding:10px 12px; border-bottom:1px solid var(--border); white-space:nowrap; }
.data-table tr:hover td { background:#f8fafc; }
.data-table .num { text-align:right; font-variant-numeric:tabular-nums; }
.rec-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:16px; }
.rec-card { background:#fafbfc; border-radius:10px; padding:20px; border:1px solid var(--border); border-top:3px solid var(--blue); }
.rec-card h5 { font-size:15px; font-weight:700; margin-bottom:8px; }
.rec-card .priority { font-size:12px; margin-bottom:8px; }
.rec-card p { font-size:13px; color:var(--text-secondary); }
.report-footer { text-align:center; padding:24px; color:var(--text-secondary); font-size:13px; }
.empty { color:#94a3b8; font-size:13px; padding:20px; text-align:center; }
.finding { display:flex; gap:12px; padding:10px 0; border-bottom:1px solid #f1f5f9; }
.finding:last-child { border-bottom:none; }
.level { flex-shrink:0; padding:2px 8px; border-radius:6px; font-size:12px; font-weight:600; height:fit-content; }
.level-强 { background:#dcfce7; color:#166534; } .level-弱 { background:#fef9c3; color:#854d0e; } .level-推测 { background:#e0e7ff; color:#3730a3; }
.finding .claim { font-size:14px; line-height:1.7; color:var(--text); }
@media (max-width:768px) { .kpi-grid { grid-template-columns:repeat(2,1fr); } .chart-row { grid-template-columns:1fr; } .rec-grid { grid-template-columns:1fr; } }
"""

_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>{css}</style>
</head>
<body>
<div class="container">
  <div class="report-header">
    <h1>{title}{grade_badge}</h1>
    <div class="subtitle">{subtitle}</div>
    <div class="meta">{meta_lines}</div>
  </div>
  {kpi_html}
  {body_html}
  <div class="report-footer">由 AI 数据分析平台自动生成 · {generated_at}</div>
</div>
<script>
{chart_js}
</script>
</body>
</html>
"""


def render_report_html(data: dict) -> str:
    """渲染完整 HTML — 蓝图驱动或旧模板兜底."""
    if data.get("_mode") == "blueprint":
        return _render_blueprint(data)
    return _render_legacy(data)


# ══════════════════════════════════════════════════════════════════
#  蓝图驱动渲染
# ══════════════════════════════════════════════════════════════════

_SECTION_ICONS = {
    "quality": "🔍",
    "time_series": "📈",
    "group_compare": "📊",
    "cross_analysis": "🔥",
    "year_over_year": "📅",
    "top_n": "🏆",
}

_CHART_IDX = {"counter": 0}


def _next_chart_id() -> str:
    _CHART_IDX["counter"] += 1
    return f"chart_{_CHART_IDX['counter']}"


def _render_blueprint(data: dict) -> str:
    meta = data["meta"]
    chapters = data.get("_chapters", [])
    overview = data.get("_overview", {})
    quality = data.get("quality", {})
    chart_js_parts: list[str] = []
    body_parts: list[str] = []

    title = html.escape(meta["request"][:60] or "数据分析报告")
    grade = quality.get("grade", "B")
    grade_badge = f'<span class="badge badge-{grade[0].lower() if grade else "a"}">质量{grade}</span>' if grade else ""

    # Subtitle
    table_count = overview.get("table_count", 1)
    row_count = overview.get("row_count", "?")
    time_span = overview.get("time_span", "")
    subtitle_parts = [f"数据源: {html.escape(str(meta.get('source_id','')))}"]
    if time_span:
        subtitle_parts.append(f"时间跨度: {html.escape(str(time_span))}")
    subtitle_parts.append(f"有效记录: {row_count} 条")
    subtitle = " | ".join(subtitle_parts)

    # Meta
    dims = data.get("_dimensions", [])
    dim_names = [d.get("label", d.get("name", "")) for d in dims if d.get("type") == "categorical"]
    meta_parts = []
    if dim_names:
        meta_parts.append(f"覆盖维度: {len(dim_names)} 个")
    meta_parts.append(f"生成时间: {html.escape(meta['generated_at'])}")
    meta_lines = " · ".join(meta_parts)

    # KPI
    kpi_html = _render_kpis(data.get("kpis", []))

    # Chapters → body
    for ch in chapters:
        ch_html, ch_js = _render_chapter(ch, data)
        if ch_html:
            body_parts.append(ch_html)
        if ch_js:
            chart_js_parts.extend(ch_js)

    # Suggestions (from reporter)
    suggestions = data.get("suggestions") or []
    if suggestions:
        body_parts.append(_render_suggestions(suggestions))

    return _TEMPLATE.format(
        title=title,
        grade_badge=grade_badge,
        subtitle=subtitle,
        meta_lines=meta_lines,
        kpi_html=kpi_html,
        body_html="\n".join(body_parts),
        chart_js="\n".join(chart_js_parts),
        css=_CSS,
        generated_at=meta["generated_at"],
    )


def _render_kpis(kpis: list[dict]) -> str:
    if not kpis:
        return ""
    colors = ["", "green", "orange", "purple"]
    cards = []
    for i, k in enumerate(kpis[:4]):
        cls = colors[i % len(colors)] if i > 0 else ""
        sub = f'<div class="kpi-sub">{html.escape(str(k.get("detail","")))}</div>' if k.get("detail") else ""
        cards.append(
            f'<div class="kpi-card {cls}">'
            f'<div class="kpi-label">{html.escape(k["label"])}</div>'
            f'<div class="kpi-value">{html.escape(str(k["value"]))}</div>{sub}</div>'
        )
    return f'<div class="kpi-grid">{"".join(cards)}</div>'


def _render_chapter(ch: dict, data: dict) -> tuple[str, list[str]]:
    """渲染一个章节 → (HTML片段, JS片段列表)."""
    ch_type = ch.get("type", "")
    ch_id = ch.get("id", "")
    title = ch.get("title", "")
    desc = ch.get("description", "")
    icon = _SECTION_ICONS.get(ch_type, "📋")
    charts = ch.get("charts") or []
    findings = ch.get("findings") or []

    parts = []
    js_parts = []

    parts.append(f'<div class="section"><div class="section-title"><span class="icon">{icon}</span>{html.escape(title)}</div>')
    if desc:
        parts.append(f'<div class="section-desc">{html.escape(desc)}</div>')

    # 质量章节特殊处理
    if ch_type == "quality":
        return _render_quality_chapter(ch, data)

    # 图表
    if charts:
        chart_boxes = []
        for c in charts:
            cid = _next_chart_id()
            chart_title = c.get("title", "图表")
            # echarts_option 可能在各种位置
            eo = c.get("echarts_option") or c.get("option") or {}
            if not eo:
                eo = _chart_data_to_echarts(c)
            if not eo:
                continue
            eo_json = _safe_eo_json(eo)
            chart_boxes.append(
                f'<div class="chart-box"><h4>{html.escape(str(chart_title))}</h4>'
                f'<div id="{cid}" class="chart-wrapper"></div></div>'
            )
            js_parts.append(
                f"(function(){{var el=document.getElementById('{cid}');"
                f"if(el){{var c=echarts.init(el);c.setOption({eo_json});"
                f"window.addEventListener('resize',function(){{c.resize();}});}}}})();"
            )
        if chart_boxes:
            parts.append(f'<div class="chart-row">{"".join(chart_boxes)}</div>')

    # Findings (insight box)
    if findings:
        insight_items = []
        for f in findings:
            claim = f.get("claim", str(f)) if isinstance(f, dict) else str(f)
            level = f.get("level", "弱") if isinstance(f, dict) else "弱"
            # 清洗: 去 [强]/[弱]/[推测] 前缀
            import re as _re
            claim = _re.sub(r"^\[(强|弱|推测)\]\s*", "", claim).strip()
            insight_items.append(f"<li>{html.escape(claim)}</li>")

        insight_class = ""
        parts.append(
            f'<div class="insight-box{insight_class}"><h5>📊 {html.escape(title)}洞察</h5>'
            f'<ul>{"".join(insight_items)}</ul></div>'
        )

    parts.append("</div>")
    return "\n".join(parts), js_parts


def _render_quality_chapter(ch: dict, data: dict) -> tuple[str, list[str]]:
    """渲染数据质量章节 (特殊布局: 卡片网格)."""
    quality = data.get("quality", {})
    grade = quality.get("grade", "B")
    blockers = quality.get("blockers", [])
    degraded = quality.get("degraded", [])
    overview = data.get("_overview", {})

    parts = [
        f'<div class="section"><div class="section-title"><span class="icon">🔍</span>'
        f'{html.escape(ch.get("title", "数据质量评估"))}</div>',
        f'<div class="section-desc">{html.escape(ch.get("description", "原始数据校验结果。"))}</div>',
    ]

    # 质量卡片
    row_count = overview.get("row_count", "?")
    cards = [
        f'<div class="dq-item ok"><div class="dq-label">原始记录数</div><div class="dq-value">{row_count}</div><div class="dq-detail">有效记录</div></div>',
        f'<div class="dq-item {"danger" if grade in ("C","D") else "ok"}"><div class="dq-label">综合质量等级</div><div class="dq-value">{grade}</div><div class="dq-detail">{len(blockers)}个阻塞 / {len(degraded)}个降级</div></div>',
    ]
    if blockers:
        cards.append(f'<div class="dq-item danger"><div class="dq-label">阻塞问题</div><div class="dq-value">{len(blockers)}</div><div class="dq-detail">{html.escape(str(blockers[0])[:50]) if blockers else ""}</div></div>')
    if degraded:
        cards.append(f'<div class="dq-item warning"><div class="dq-label">降级问题</div><div class="dq-value">{len(degraded)}</div><div class="dq-detail">{html.escape(str(degraded[0])[:50]) if degraded else ""}</div></div>')
    parts.append(f'<div class="dq-grid">{"".join(cards)}</div>')

    # Blockers detail
    if blockers:
        items = "".join(f"<li>{html.escape(str(b))}</li>" for b in blockers[:5])
        parts.append(
            f'<div class="insight-box warning"><h5>⚠️ 数据质量风险</h5><ul>{items}</ul></div>'
        )

    parts.append("</div>")
    return "\n".join(parts), []


def _render_suggestions(suggestions: list[dict]) -> str:
    """渲染行动建议卡片."""
    prio_badge = {"高": "danger", "中": "warning", "低": "success"}
    prio_color = {"高": "#dc2626", "中": "#d97706", "低": "#16a34a"}
    cards = []
    for s in suggestions:
        prio = s.get("priority", "中")
        cards.append(
            f'<div class="rec-card" style="border-top-color:{prio_color.get(prio,"#2563eb")}">'
            f'<h5>{html.escape(s.get("text","")[:80])}...</h5>'
            f'<div class="priority"><span class="badge badge-{prio_badge.get(prio,"")}">{prio}优先级</span></div>'
            f'<p>{html.escape(s.get("text","")[:200])}</p></div>'
        )
    return (
        f'<div class="section"><div class="section-title"><span class="icon">💡</span>行动建议</div>'
        f'<div class="rec-grid">{"".join(cards)}</div></div>'
    )


# ══════════════════════════════════════════════════════════════════
#  旧模板渲染 (兜底)
# ══════════════════════════════════════════════════════════════════

def _render_legacy(data: dict) -> str:
    """旧模板渲染 — 当无 blueprint 时兜底."""
    meta = data["meta"]
    title = html.escape(meta["request"][:60] or "数据分析报告")
    grade = data["quality"]["grade"]

    grade_badge = f'<span class="badge badge-{grade[0].lower() if grade else "a"}">质量{grade}</span>' if grade and grade != "?" else ""

    meta_lines_parts = []
    if meta.get("source_id"):
        meta_lines_parts.append(f"数据源: {html.escape(str(meta['source_id']))}")
    meta_lines_parts.append(f"生成时间: {html.escape(meta['generated_at'])}")
    meta_lines_html = " · ".join(meta_lines_parts)

    # KPI
    kpi_html = ""
    if data["kpis"]:
        cards = []
        for k in data["kpis"]:
            detail = f'<div class="kpi-sub">{html.escape(k.get("detail",""))}</div>' if k.get("detail") else ""
            cards.append(
                f'<div class="kpi-card"><div class="kpi-label">{html.escape(k["label"])}</div>'
                f'<div class="kpi-value">{html.escape(str(k["value"]))}</div>{detail}</div>'
            )
        kpi_html = f'<div class="kpi-grid">{"".join(cards)}</div>'

    # Charts
    chart_boxes = []
    chart_js = ""
    if data["charts"]:
        js_parts = []
        for i, c in enumerate(data["charts"]):
            cid = f"legacy_chart_{i}"
            chart_boxes.append(
                f'<div class="chart-box"><h4>{html.escape(str(c.get("title","")))}</h4>'
                f'<div id="{cid}" class="chart-wrapper"></div></div>'
            )
            eo_json = _safe_eo_json(c["echarts_option"])
            js_parts.append(
                f"var el{i}=document.getElementById('{cid}');"
                f"var chart{i}=echarts.init(el{i});chart{i}.setOption({eo_json});"
                f"window.addEventListener('resize',function(){{chart{i}.resize();}});"
            )
        chart_js = "\n".join(js_parts)

    charts_html = f'<div class="chart-row">{"".join(chart_boxes)}</div>' if chart_boxes else '<div class="empty">本次分析未生成图表</div>'

    # Findings
    findings_html = ""
    if data["findings"]:
        items = []
        for f in data["findings"]:
            level = f.get("level", "弱")
            items.append(
                f'<div class="finding"><span class="level level-{level}">{level}</span>'
                f'<div class="claim">{html.escape(f["claim"])}</div></div>'
            )
        findings_html = "".join(items)
    else:
        findings_html = '<div class="empty">无结构化发现</div>'

    # Suggestions
    suggestions_html = ""
    suggestions = data.get("suggestions") or []
    if suggestions:
        items = []
        for s in suggestions:
            items.append(
                f'<div class="suggestion"><span class="s-prio s-{s.get("priority","中")}">{s.get("priority","中")}</span>'
                f'<span>{html.escape(s.get("text",""))}</span></div>'
            )
        suggestions_html = (
            '<div class="section"><div class="section-title">🚀 行动建议</div>'
            f'<div class="section">{"".join(items)}</div></div>'
        )

    # Legacy template
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>{_CSS}</style>
</head>
<body>
<div class="container">
  <div class="report-header"><h1>{title}{grade_badge}</h1><div class="meta">{meta_lines_html}</div></div>
  {kpi_html}
  <div class="section"><div class="section-title">📈 可视化分析</div>{charts_html}</div>
  <div class="section"><div class="section-title">🔍 关键发现</div><div class="section">{findings_html}</div></div>
  {suggestions_html}
  <div class="report-footer">由 AI 数据分析平台自动生成 · {meta["generated_at"]}</div>
</div>
<script>{chart_js}</script>
</body>
</html>"""
