"""Analysis tools -- 全部直连数据库, 不经 state 传数据

所有工具接受 source_id + table, 内部执行 SQL + 计算.
"""

from langchain_core.tools import tool
import pandas as pd
import numpy as np
import json
import math
import logging

from dia.infrastructure.database.manager import get_datasource_manager
from dia.core.config import settings

logger = logging.getLogger(__name__)


def _resolve_table(source_id: str, table: str = "") -> str:
    mgr = get_datasource_manager()
    conn = mgr.connect(source_id)
    if not table:
        schema = conn.get_schema()
        tables = conn.list_tables()
        if tables:
            return max(tables, key=lambda t: schema.get(t, {}).get("row_count", 0))
    return table


def _build_where(filter_condition: str) -> str:
    return f" WHERE {filter_condition}" if filter_condition else ""


def _resolve_agg(metric: str, agg_func: str) -> str:
    """解析聚合口径 (与 explore._resolve_agg_func 同规则): auto 按指标名推断.

    率/价/单价类指标 (客单价/利润率) → avg, 其余 → sum.
    """
    if agg_func and agg_func != "auto":
        return agg_func if agg_func in ("sum", "avg", "count", "median") else "sum"
    m = (metric or "").lower()
    if any(k in m for k in ("率", "ratio", "rate", "价", "price", "单价", "客单价",
                            "avg", "mean", "unit", "per")):
        return "avg"
    return "sum"


@tool
def drill_down(metric: str, group_by: str, source_id: str, table: str = "",
               filter_condition: str = "", top_n: int = 10) -> str:
    """按维度分组对比指标值。工具内部执行 SQL GROUP BY。

    Args:
        metric: 聚合指标列名
        group_by: 分组维度列名
        source_id: 数据源ID
        table: 表名 (留空自动选最大表)
        filter_condition: SQL WHERE 子句 (如 "date BETWEEN '2026-01' AND '2026-03'")
        top_n: 返回前 N 组
    """
    try:
        table = _resolve_table(source_id, table)
        where = _build_where(filter_condition)
        conn = get_datasource_manager().connect(source_id)
        schema = conn.get_schema()

        # 自主校验列名是否存在
        table_info = schema.get(table, {})
        real_cols = {c["name"] for c in table_info.get("columns", [])}
        if metric not in real_cols:
            return json.dumps({"error": f"列 '{metric}' 不存在。表 {table} 的列为: {', '.join(sorted(real_cols))}"}, ensure_ascii=False)
        if group_by not in real_cols:
            return json.dumps({"error": f"列 '{group_by}' 不存在。表 {table} 的列为: {', '.join(sorted(real_cols))}"}, ensure_ascii=False)

        sql = f"SELECT {group_by}, SUM({metric}) as _sum, AVG({metric}) as _avg, COUNT(*) as _cnt FROM {table}{where} GROUP BY {group_by} ORDER BY _sum DESC LIMIT {top_n}"
        result = conn.query(sql)
        if "error" in result:
            return json.dumps({"error": result["error"]})
        rows = result.get("rows", [])
        return json.dumps({
            "metric": metric, "group_by": group_by, "table": table,
            "groups": [{group_by: row.get(group_by, "?"), "sum": row.get("_sum", 0), "avg": row.get("_avg", 0), "count": row.get("_cnt", 0)} for row in rows],
        }, ensure_ascii=False, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def rank(metric: str, source_id: str, table: str = "", by: str = "",
         filter_condition: str = "", top_n: int = 5, ascending: bool = False) -> str:
    """Top/Bottom N 排名。工具内部执行 SQL。

    Args:
        metric: 排名列
        source_id: 数据源ID
        table: 表名
        by: 分组列 (可选)
        filter_condition: SQL WHERE 子句
        top_n: 返回前 N
        ascending: True=升序
    """
    try:
        table = _resolve_table(source_id, table)
        where = _build_where(filter_condition)
        conn = get_datasource_manager().connect(source_id)
        schema = conn.get_schema()

        # 自主校验列名是否存在
        table_info = schema.get(table, {})
        real_cols = {c["name"] for c in table_info.get("columns", [])}
        if metric not in real_cols:
            return json.dumps({"error": f"列 '{metric}' 不存在。表 {table} 的列为: {', '.join(sorted(real_cols))}"}, ensure_ascii=False)
        if by and by not in real_cols:
            return json.dumps({"error": f"列 '{by}' 不存在。表 {table} 的列为: {', '.join(sorted(real_cols))}"}, ensure_ascii=False)

        order = "ASC" if ascending else "DESC"
        if by:
            sql = f"SELECT {by}, SUM({metric}) as _total FROM {table}{where} GROUP BY {by} ORDER BY _total {order} LIMIT {top_n}"
        else:
            sql = f"SELECT {metric} FROM {table}{where} ORDER BY {metric} {order} LIMIT {top_n}"
        result = conn.query(sql)
        if "error" in result:
            return json.dumps({"error": result["error"]})
        rows = result.get("rows", [])
        label = f"{'Bottom' if ascending else 'Top'} {top_n}"
        if by:
            return json.dumps({label: [{"value": str(row.get(by, "?")), "metric": row.get("_total", 0)} for row in rows]}, ensure_ascii=False, default=str)
        else:
            return json.dumps({label: [{"value": str(i), "metric": row.get(metric, 0)} for i, row in enumerate(rows)]}, ensure_ascii=False, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def decompose(metric: str, dimension: str, source_id: str, table: str = "",
              filter_condition: str = "", top_n: int = 5) -> str:
    """贡献度拆解: 每个维度的值占总量的百分比。

    Args:
        metric: 目标指标
        dimension: 拆解维度
        source_id: 数据源ID
        table: 表名
        filter_condition: SQL WHERE 子句
        top_n: 返回前 N
    """
    try:
        table = _resolve_table(source_id, table)
        where = _build_where(filter_condition)
        conn = get_datasource_manager().connect(source_id)
        schema = conn.get_schema()

        # 列名校验 (与 drill_down/rank 一致, 防止拼错列名导致 SQL 报错/注入)
        table_info = schema.get(table, {})
        real_cols = {c["name"] for c in table_info.get("columns", [])}
        if metric not in real_cols:
            return json.dumps({"error": f"列 '{metric}' 不存在。表 {table} 的列为: {', '.join(sorted(real_cols))}"}, ensure_ascii=False)
        if dimension not in real_cols:
            return json.dumps({"error": f"列 '{dimension}' 不存在。表 {table} 的列为: {', '.join(sorted(real_cols))}"}, ensure_ascii=False)

        # 先查全量总和作为占比分母 (不能用 top-N 部分和 — 会虚高)
        total_result = conn.query(f"SELECT SUM({metric}) as _grand FROM {table}{where}")
        grand_total = 0.0
        if "error" not in total_result and total_result.get("rows"):
            v = total_result["rows"][0].get("_grand")
            if v is not None:
                grand_total = float(v)

        sql = f"SELECT {dimension}, SUM({metric}) as _total FROM {table}{where} GROUP BY {dimension} ORDER BY _total DESC LIMIT {top_n}"
        result = conn.query(sql)
        if "error" in result:
            return json.dumps({"error": result["error"]})
        rows = result.get("rows", [])
        contributions = [{
            "dimension": dimension, "value": str(row.get(dimension, "?")),
            "value_num": round(float(row.get("_total", 0)), 2),
            "contribution_pct": round(float(row.get("_total", 0)) / grand_total * 100, 1) if grand_total > 0 else 0,
        } for row in rows]
        return json.dumps({"metric": metric, "total": round(grand_total, 2), "top_contributors": contributions}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def find_drivers(target: str, source_id: str, table: str = "", top_n: int = 5, filter_condition: str = "") -> str:
    """计算各特征与目标的相关性。工具内部执行 SQL。

    Args:
        target: 目标列名
        source_id: 数据源ID
        table: 表名
        top_n: 返回前 N
        filter_condition: SQL WHERE 子句
    """
    try:
        table = _resolve_table(source_id, table)
        where = _build_where(filter_condition)
        conn = get_datasource_manager().connect(source_id)
        result = conn.query(f"SELECT * FROM {table}{where} LIMIT {settings.ANALYSIS_MAX_ROWS}")
        if "error" in result:
            return json.dumps({"error": result["error"]})
        df = pd.DataFrame(result["rows"])
        if target not in df.columns:
            return json.dumps({"error": f"列 {target} 不存在"})
        num_cols = df.select_dtypes(include=[np.number]).columns
        correlations = {}
        for col in num_cols:
            if col == target: continue
            corr = df[[target, col]].dropna().corr().iloc[0, 1]
            if not np.isnan(corr):
                correlations[col] = round(float(corr), 3)
        top = sorted(correlations.items(), key=lambda x: abs(x[1]), reverse=True)[:top_n]
        return json.dumps({"target": target, "data_points": len(df), "drivers": [{"feature": k, "correlation": v, "importance": abs(v)} for k, v in top]}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def forecast(metric: str, source_id: str, table: str = "", date_col: str = "date",
             periods: int = 3, agg_func: str = "auto", filter_condition: str = "") -> str:
    """趋势预测: 季节感知线性回归 + 预测置信区间。

    确定性算法:
    1. 按日期 SQL 聚合 (口径 agg_func: sum/avg/count, auto 按指标名推断)
    2. 自相关检测主周期 (月→12, 日→7); 数据 ≥2 个完整周期 → 加法季节分解,
       去季节后线性回归, 外推时加回季节指数
    3. 输出趋势斜率 + 显著性 p 值 + 标准预测区间 (95%, 含外推不确定性)

    Args:
        metric: 预测指标
        source_id: 数据源ID
        table: 表名
        date_col: 日期列名
        periods: 预测期数
        agg_func: 聚合口径 auto|sum|avg|count (auto: 率/价类→avg, 其余→sum)
        filter_condition: SQL WHERE 子句
    """
    try:
        from scipy import stats as sp_stats
        table = _resolve_table(source_id, table)
        where = _build_where(filter_condition)
        conn = get_datasource_manager().connect(source_id)

        # 列名校验 (与 drill_down/rank 一致)
        schema = conn.get_schema()
        table_info = schema.get(table, {})
        real_cols = {c["name"] for c in table_info.get("columns", [])}
        if metric not in real_cols:
            return json.dumps({"error": f"列 '{metric}' 不存在。表 {table} 的列为: {', '.join(sorted(real_cols))}"}, ensure_ascii=False)
        if date_col not in real_cols:
            return json.dumps({"error": f"列 '{date_col}' 不存在。表 {table} 的列为: {', '.join(sorted(real_cols))}"}, ensure_ascii=False)

        # 口径: auto 按指标名推断 (率/价类 → avg, 其余 sum); median 无标准 SQL 聚合
        agg_exprs = {"sum": f"SUM({metric})", "avg": f"AVG({metric})", "count": "COUNT(*)"}
        agg_func = _resolve_agg(metric, agg_func)
        if agg_func not in agg_exprs:
            return json.dumps({"error": f"forecast 不支持口径 {agg_func}, 可用: sum/avg/count"}, ensure_ascii=False)

        # 先按日期聚合再预测: 明细表(一天多行)不能把行号当时间轴,
        # 否则同一天重复值会被当成连续时间点, 趋势与预测全部失真
        agg_sql = (
            f"SELECT {date_col} AS _d, {agg_exprs[agg_func]} AS _v "
            f"FROM {table}{where} GROUP BY {date_col} ORDER BY _d"
        )
        result = conn.query(agg_sql)
        if "error" in result:
            return json.dumps({"error": result["error"]})
        rows = result.get("rows", [])
        if len(rows) < 3:
            return json.dumps({"error": "数据点不足"})
        df = pd.DataFrame(rows, columns=["_d", "_v"])
        values = pd.to_numeric(df["_v"], errors="coerce").dropna().values
        if len(values) < 3:
            return json.dumps({"error": "有效数据点不足"})

        n = len(values)
        x = np.arange(n)

        # ── 季节感知: 检测周期 → 分解 → 去季节回归 ──
        period = _detect_period(values)
        season_adjusted = False
        seasonal_idx = None
        if period >= 3 and n >= 2 * period:
            _, seasonal_idx = _seasonal_decompose(values, period)
            y_reg = values - seasonal_idx[x % period]  # 去季节
            season_adjusted = True
        else:
            y_reg = values
            period = 0

        coeffs = np.polyfit(x, y_reg, deg=1)
        trend = coeffs[0]
        slope_desc = "上升趋势" if trend > 0 else "下降趋势" if trend < 0 else "平稳"

        # ── 趋势显著性: t = slope / se(slope), df = n-2 ──
        fitted = np.polyval(coeffs, x)
        resid = y_reg - fitted
        resid_std = float(np.std(resid, ddof=2)) if n > 2 else 0.0
        Sxx = float(np.sum((x - x.mean()) ** 2))
        se_slope = resid_std / math.sqrt(Sxx) if Sxx > 0 else 0.0
        t_stat = coeffs[0] / se_slope if se_slope > 0 else 0.0
        trend_p = float(2 * (1 - sp_stats.t.cdf(abs(t_stat), max(n - 2, 1))))
        t_crit = float(sp_stats.t.ppf(0.975, max(n - 2, 1)))

        # ── 外推 + 标准预测区间 (含 1/n 与 (x-x̄)²/Sxx 外推不确定性) ──
        future_x = np.arange(n, n + periods)
        base_pred = np.polyval(coeffs, future_x)
        if season_adjusted:
            base_pred = base_pred + seasonal_idx[future_x % period]
        xbar = x.mean()
        se_fit = resid_std * np.sqrt(1 + 1 / n + (future_x - xbar) ** 2 / Sxx) if Sxx > 0 else resid_std
        predictions = [round(float(v), 2) for v in base_pred]
        intervals = [
            {"period": int(xi - n + 1),
             "lower": round(float(base_pred[k] - t_crit * se_fit[k]), 2),
             "upper": round(float(base_pred[k] + t_crit * se_fit[k]), 2)}
            for k, xi in enumerate(future_x)
        ]

        note = (f"按 {date_col} 聚合 ({agg_func} 口径) 为 {n} 个时间点; "
                + ("季节感知: 检测到周期 " + str(period) + ", 已去季节后回归" if season_adjusted
                   else "线性回归, 未检测到显著季节周期") + "; 95%预测区间")

        return json.dumps({
            "metric": metric, "data_points": n, "agg_func": agg_func,
            "period": period, "season_adjusted": season_adjusted,
            "trend": slope_desc, "slope": round(float(trend), 4),
            "trend_p_value": round(trend_p, 4),
            "current": round(float(values[-1]), 2),
            "predictions": predictions,
            "intervals": intervals,
            "note": note,
        }, ensure_ascii=False)
    except ImportError:
        return json.dumps({"error": "scipy 未安装"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@tool
def detect(metrics: list[str] = None, source_id: str = "", table: str = "",
           date_col: str = "", threshold: float = settings.ANOMALY_THRESHOLD,
           agg_func: str = "auto", filter_condition: str = "",
           group_by: str = "") -> str:
    """多维异常检测: Z-score 尖峰 + 周窗口突降/突升 + 月粒度趋势漂移 + 分组检测。

    自动多粒度 (粒度随数据跨度自适应, 避免单粒度噪声误报):
      - Z-score 尖峰: 日粒度 (跨度 ≤400 天) — 大额异常日
      - 窗口突降/突升: 周粒度, 1 周 vs 前 4 周基线中位数, 变化 >25% —
        连续 N 天突降 (如缺货/系统故障) 在日粒度会被噪声淹没, 周粒度显形
      - 趋势漂移: 月粒度, 最近 6 个月单调 + 线性回归显著 + 变化 >15% —
        日粒度窗口太短检不出月度级持续下滑
    0/1 比率列 (refund_flag 等) 自动按均值聚合 → 检测"比率突变"而非计数。
    结果限流: 每 (指标, 类型) 最多 5 条, 总数 ≤ 40 — 防小样本分组噪声刷屏。

    Args:
        metrics: 检测指标列名列表 (留空自动选所有数值列, 含 0/1 比率列)
        source_id: 数据源ID
        table: 表名
        date_col: 日期列 (可选; 提供则按日期聚合检测)
        threshold: Z-score 阈值 (默认 3.0)
        agg_func: 聚合口径 auto|sum|avg|count|median (auto: 率/价类→avg, 其余→sum)
        filter_condition: SQL WHERE 子句
        group_by: 分类列 (可选; 各组独立检测 — 分组异常如"某区域利润下滑/
            某品类退货率飙升"在全量检测中会被其他组稀释, 分组后各自显形)
    """
    try:
        table = _resolve_table(source_id, table)
        where = _build_where(filter_condition)
        conn = get_datasource_manager().connect(source_id)
        result = conn.query(f"SELECT * FROM {table}{where}", max_rows=None)
        if "error" in result:
            return json.dumps({"error": result["error"]})
        df = pd.DataFrame(result["rows"])
        cols = [c.strip() for c in (metrics or []) if c.strip()]
        if not cols:
            cols = df.select_dtypes(include=[np.number]).columns.tolist()
        anomalies = []

        def _is_ratio_col(series: pd.Series) -> bool:
            """0/1 比率列 (refund_flag 类): 值域 ⊆ {0,1} → 按均值聚合测比率."""
            v = series.dropna()
            return len(v) > 0 and v.isin([0, 1]).all()

        def _agg_of(col: str, gdf: pd.DataFrame) -> str:
            func = _resolve_agg(col, agg_func)
            if _is_ratio_col(gdf[col]):
                return "mean"  # 比率列: 退货率等, 均值聚合
            return {"sum": "sum", "avg": "mean", "count": "count", "median": "median"}.get(func, "sum")

        def _zscore(seq: pd.Series, metric: str, common: dict, date_key: str) -> None:
            """日/周粒度 Z-score 尖峰."""
            values = seq.values.astype(float)
            if len(values) < 5:
                return
            mean, std = np.mean(values), np.std(values) or 1
            z_scores = (values - mean) / std
            for i, z in enumerate(z_scores):
                if abs(z) > threshold:
                    anomalies.append({**common, "metric": metric, date_key: str(seq.index[i])[:10],
                                      "value": round(float(values[i]), 2),
                                      "z_score": round(float(z), 2), "level": "spike"})

        def _window(seq: pd.Series, metric: str, common: dict, date_key: str,
                    thr: float = 0.25, win: int = 1, base: int = 4) -> None:
            """周粒度窗口突降/突升: win 期窗口 vs 前 base 期基线中位数."""
            values = seq.values.astype(float)
            if len(values) < base + win + 2:
                return
            last_report = -win
            for i in range(win, len(values)):
                if i - last_report < win + 1:
                    continue
                baseline = values[max(0, i - base - win):i - win]
                if len(baseline) < base:
                    continue
                bm = float(np.median(baseline))
                if bm == 0:
                    continue
                change = (float(np.mean(values[i - win:i])) - bm) / bm
                if abs(change) > thr:
                    anomalies.append({**common, "metric": metric, date_key: str(seq.index[i])[:10],
                                      "level": "spike_window" if change > 0 else "dip_window",
                                      "change_pct": round(change * 100, 1),
                                      "window_mean": round(float(np.mean(values[i - win:i])), 2),
                                      "baseline_median": round(bm, 2)})
                    last_report = i

        def _drift(seq: pd.Series, metric: str, common: dict, date_key: str,
                   thr: float = 0.15) -> None:
            """月粒度趋势漂移: 最近 3 个月 vs 对比基期, 变化>thr 且后半段单调.

            对比基期: 跨年数据用**去年同期 3 个月** (同比, 排除大促/春节季节效应);
            不足跨年用前 3 个月 (环比). 后半段须单调 — 排除单月脉冲 (大促只影响 1 个月).
            """
            values = seq.values.astype(float)
            n = len(values)
            if n < 6:
                return
            recent = values[-3:]
            if n >= 15:
                base = values[-15:-12]  # 去年同期 3 个月
                compare = "同比"
            else:
                base = values[n - 6:n - 3]  # 前 3 个月
                compare = "环比"
            fm = float(np.mean(base))
            if fm == 0:
                return
            change = (float(np.mean(recent)) - fm) / fm
            if abs(change) <= thr:
                return
            # 单调检查只在环比场景需要 (同比已排除季节, 单月脉冲两侧都有不会被误报)
            if compare == "环比":
                d = np.diff(recent)
                if not (np.all(d > 0) or np.all(d < 0)):
                    return
            anomalies.append({**common, "metric": metric, date_key: str(seq.index[-1])[:10],
                              "level": "drift",
                              "direction": "up" if change > 0 else "down",
                              "total_change_pct": round(change * 100, 1),
                              "compare": compare})

        def _detect_group(gdf: pd.DataFrame, group: str = "") -> None:
            common = {"group": group} if group else {}
            if date_col and date_col in gdf.columns:
                gdf = gdf.assign(_date=pd.to_datetime(gdf[date_col], errors="coerce")).dropna(subset=["_date"])
                if len(gdf) < 10:
                    return  # 组内样本太少, 检测无意义 (噪声会刷屏)
                span = (gdf["_date"].max() - gdf["_date"].min()).days
                for col in cols:
                    if col not in gdf.columns:
                        continue
                    agg = _agg_of(col, gdf)
                    # 1) 日粒度 Z-score 尖峰 (大额异常日; 跨度 >400 天用周)
                    #    注: 不做日粒度滑动窗口 — 小样本区域日销售额噪声极大
                    #    (实测 3 天窗口正常波动 ±50%+), 窗口比值无法区分信号与噪声
                    if span <= 400:
                        dseq = gdf.groupby(pd.Grouper(key="_date", freq="D"))[col].agg(agg).dropna()
                        _zscore(dseq, col, common, "date")
                    else:
                        wseq = gdf.groupby(pd.Grouper(key="_date", freq="W"))[col].agg(agg).dropna()
                        _zscore(wseq, col, common, "week")
                    # 2) 周粒度窗口突降/突升 (1 周 vs 前 4 周基线, 连续多日突降周粒度显形)
                    if span > 40:
                        wseq = gdf.groupby(pd.Grouper(key="_date", freq="W"))[col].agg(agg).dropna()
                        _window(wseq, col, common, "week")
                    # 3) 月粒度漂移 (最近 3 月 vs 去年同期, 同比排除季节效应)
                    if span > 150:
                        mseq = gdf.groupby(pd.Grouper(key="_date", freq="ME"))[col].agg(agg).dropna()
                        _drift(mseq, col, common, "month")
            else:
                # 行级模式 (无日期列)
                for col in cols:
                    if col not in gdf.columns:
                        continue
                    values = gdf[col].dropna().values
                    if len(values) < 5:
                        continue
                    mean, std = np.mean(values), np.std(values) or 1
                    z_scores = (values - mean) / std
                    for i, z in enumerate(z_scores):
                        if abs(z) > threshold:
                            anomalies.append({**common, "metric": col, "index": int(i),
                                              "value": round(float(values[i]), 2),
                                              "z_score": round(float(z), 2), "level": "spike"})

        if group_by and group_by in df.columns:
            for gname, gdf in df.groupby(group_by, dropna=False):
                _detect_group(gdf, str(gname))
        else:
            _detect_group(df)

        # 限流: 每 (metric, level) 最多 5 条按严重度排序, 总数 ≤ 40
        from collections import defaultdict
        buckets: dict = defaultdict(list)
        for a in anomalies:
            buckets[(a.get("metric", ""), a.get("level", ""))].append(a)
        capped = []
        for key, items in buckets.items():
            items.sort(key=lambda a: abs(a.get("change_pct", a.get("z_score", 0))), reverse=True)
            capped.extend(items[:5])
        capped.sort(key=lambda a: -abs(a.get("change_pct", a.get("z_score", 0))))
        anomalies = capped[:40]

        return json.dumps({"data_points": len(df), "anomalies": anomalies, "count": len(anomalies)}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
@tool
def segment(source_id: str, table: str = "", n_clusters: int = settings.CLUSTER_N_CLUSTERS, features: list[str] = None, filter_condition: str = "") -> str:
    """KMeans 聚类。工具内部执行 SQL。

    Args:
        source_id: 数据源ID
        table: 表名
        n_clusters: 聚类数
        features: 特征列列表 (如 ["revenue","quantity"], 留空自动选数值列)
        filter_condition: SQL WHERE 子句
    """
    try:
        from sklearn.cluster import KMeans
        from sklearn.preprocessing import StandardScaler
        table = _resolve_table(source_id, table)
        where = _build_where(filter_condition)
        conn = get_datasource_manager().connect(source_id)
        feat_cols = [c.strip() for c in (features or []) if c.strip()]
        if feat_cols:
            cols = feat_cols
            result = conn.query(f"SELECT {', '.join(cols)} FROM {table}{where} LIMIT {settings.ANALYSIS_MAX_ROWS}")
        else:
            result = conn.query(f"SELECT * FROM {table}{where} LIMIT {settings.ANALYSIS_MAX_ROWS}")
        if "error" in result:
            return json.dumps({"error": result["error"]})
        df = pd.DataFrame(result["rows"])
        if cols:
            cols = [c for c in cols if c in df.columns]
        else:
            cols = df.select_dtypes(include=[np.number]).columns.tolist()[:5]
        if not cols:
            return json.dumps({"error": "无数值特征列可用"})
        X = df[cols].fillna(0).values
        X_scaled = StandardScaler().fit_transform(X)
        kmeans = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
        labels = kmeans.fit_predict(X_scaled)
        cluster_info = {}
        for i in range(n_clusters):
            mask = labels == i
            cluster_info[f"cluster_{i}"] = {"size": int(mask.sum()), "pct": round(float(mask.mean()) * 100, 1), "centers": {cols[j]: round(float(kmeans.cluster_centers_[i][j]), 2) for j in range(len(cols))}}
        return json.dumps({"n_clusters": n_clusters, "data_points": len(df), "features": cols, "clusters": cluster_info}, ensure_ascii=False)
    except ImportError:
        return json.dumps({"error": "sklearn 未安装"})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ══════════════════════════════════════════════════════════════════
#  统计严谨性工具 — 假设检验 / 回归 / 时序分解 / 异常解释 / 分位数
# ══════════════════════════════════════════════════════════════════

def _load_df(source_id: str, table: str, filter_condition: str = "", limit: int = None) -> pd.DataFrame:
    """通用数据加载: 解析表名 + 构建 WHERE + 查询 + DataFrame。

    max_rows=None 传给连接器 → 不截断 500 行限制。
    统计工具需要全量聚合 (compare 按月聚合、hypothesis_test 分组检验等),
    若只拿前 500 行, 时间序列/分组分布会被截断成数据集开头一小段。
    """
    table = _resolve_table(source_id, table)
    where = _build_where(filter_condition)
    conn = get_datasource_manager().connect(source_id)
    limit_sql = f" LIMIT {limit}" if limit else ""
    result = conn.query(f"SELECT * FROM {table}{where}{limit_sql}", max_rows=None)
    if "error" in result:
        raise ValueError(result["error"])
    return pd.DataFrame(result["rows"])


def _safe_json(data: dict) -> str:
    """序列化并兜底 NaN/Inf → None。"""
    import math
    def _clean(v):
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
        if isinstance(v, dict):
            return {k: _clean(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [_clean(x) for x in v]
        return v
    return json.dumps(_clean(data), ensure_ascii=False, default=str)


# ══════════════════════════════════════════════════════════════════
#  时间序列辅助 — 周期检测 + 加法季节分解 (纯 numpy/pandas, 无 statsmodels)
# ══════════════════════════════════════════════════════════════════

def _detect_period(values: np.ndarray, max_lag: int = 26) -> int:
    """自相关检测主周期: 找 lag ∈ [2, max_lag] 中自相关最强的周期.

    月度数据 → 12, 周数据 → 52 (受 max_lag 限制), 日数据 → 7.
    自相关 < 0.3 视为无显著周期 → 返回 0 (纯趋势).
    """
    n = len(values)
    if n < 8:
        return 0
    v = values - values.mean()
    denom = float(np.sum(v ** 2))
    if denom == 0:
        return 0
    best_lag, best_ac = 0, 0.3
    max_lag = min(max_lag, n // 2)
    for lag in range(2, max_lag + 1):
        ac = float(np.sum(v[lag:] * v[:-lag]) / denom)
        if ac > best_ac:
            best_ac, best_lag = ac, lag
    return best_lag


def _seasonal_decompose(values: np.ndarray, period: int) -> tuple[np.ndarray, np.ndarray]:
    """加法季节分解: 中心移动平均趋势 + 周期位置季节指数 (均值归零).

    Returns: (trend_series, seasonal_index[period])
    """
    trend = pd.Series(values).rolling(period, center=True, min_periods=1).mean().values
    detrended = values - trend
    seasonal = np.zeros(period)
    counts = np.zeros(period)
    for i in range(len(values)):
        seasonal[i % period] += detrended[i]
        counts[i % period] += 1
    seasonal = np.where(counts > 0, seasonal / np.maximum(counts, 1), 0.0)
    seasonal = seasonal - seasonal.mean()  # 加法模型均值归零
    return trend, seasonal


@tool
def hypothesis_test(metric: str, group_by: str, source_id: str, table: str = "",
                    filter_condition: str = "", top_n: int = 2) -> str:
    """统计显著性检验: 对比分组间的指标差异是否真实(而非随机波动)。

    对 top_n 个分组做 Welch t 检验(方差不齐也适用), 输出 p 值。
    p < 0.05 → 差异显著, 结论可信; p >= 0.05 → 差异可能只是噪声。

    Args:
        metric: 数值指标列名
        group_by: 分组维度列名
        source_id: 数据源ID
        table: 表名 (留空自动选最大表)
        filter_condition: SQL WHERE 子句
        top_n: 比较前 N 组 (默认2, 即最大 vs 次大)
    """
    try:
        from scipy import stats as sp_stats
        df = _load_df(source_id, table, filter_condition)
        if metric not in df.columns or group_by not in df.columns:
            return _safe_json({"error": f"列不存在: {metric} 或 {group_by}"})
        if not pd.api.types.is_numeric_dtype(df[metric]):
            return _safe_json({"error": f"{metric} 不是数值列"})

        # 取指标和最大的 N 组
        groups = df.groupby(group_by)[metric].sum().sort_values(ascending=False).head(top_n)
        if len(groups) < 2:
            return _safe_json({"error": f"分组数不足: 只有 {len(groups)} 组"})

        pairs = []
        names = list(groups.index)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a = df.loc[df[group_by] == names[i], metric].dropna()
                b = df.loc[df[group_by] == names[j], metric].dropna()
                if len(a) < 3 or len(b) < 3:
                    continue
                t_stat, p_value = sp_stats.ttest_ind(a, b, equal_var=False)
                # Cohen's d 效应量
                pooled_std = ((a.std() ** 2 + b.std() ** 2) / 2) ** 0.5
                d = (a.mean() - b.mean()) / pooled_std if pooled_std > 0 else 0
                pairs.append({
                    "group_a": str(names[i]), "group_b": str(names[j]),
                    "mean_a": round(float(a.mean()), 2), "mean_b": round(float(b.mean()), 2),
                    "t_stat": round(float(t_stat), 3), "p_value": round(float(p_value), 4),
                    "significant": bool(p_value < 0.05),
                    "effect_size": round(float(d), 3),
                })
        if not pairs:
            return _safe_json({"error": "各组样本量不足(n<3)"})

        # Benjamini-Hochberg FDR 校正: 多对比较时控制假发现率,
        # 避免 top_n>2 时 α 膨胀导致"碰巧显著"的虚假结论
        if len(pairs) > 1:
            p_vals = np.array([p["p_value"] for p in pairs])
            n = len(p_vals)
            order = np.argsort(p_vals)
            adjusted = np.empty(n)
            for rank, idx in enumerate(order, start=1):
                adjusted[idx] = p_vals[idx] * n / rank
            # 保序 (沿排序顺序从大到小取 min): 保证 adjusted p 的单调性 —
            # 必须按 order 索引访问, 否则 adjusted p 错位 (与 explore.py 同逻辑一致)
            for i in range(n - 2, -1, -1):
                adjusted[order[i]] = min(adjusted[order[i]], adjusted[order[i + 1]])
            adjusted = np.minimum(adjusted, 1.0)
            for p, ap in zip(pairs, adjusted):
                p["p_value_adjusted"] = round(float(ap), 4)
                p["significant"] = bool(ap < 0.05)
            method_desc = "Welch t-test + BH FDR 校正"
        else:
            for p in pairs:
                p["p_value_adjusted"] = p["p_value"]
            method_desc = "Welch t-test (单对比较, 无需校正)"

        return _safe_json({
            "metric": metric, "group_by": group_by, "method": method_desc,
            "alpha": 0.05, "pairs": pairs,
            "conclusion": "差异显著" if any(p["significant"] for p in pairs) else "无显著差异",
        })
    except ImportError:
        return _safe_json({"error": "scipy 未安装"})
    except Exception as e:
        return _safe_json({"error": str(e)})


@tool
def regression_analysis(target: str, features: list[str], source_id: str, table: str = "",
                        filter_condition: str = "") -> str:
    """多元线性回归: 判断各特征对目标的真实影响(控制混淆变量)。

    比 find_drivers(单变量相关)更严谨 — 回归能排除特征间的相互干扰。
    输出每个特征的系数、p 值、以及整体 R²。

    Args:
        target: 目标指标列名
        features: 特征列名列表 (如 ["price","quantity"], 最多8个)
        source_id: 数据源ID
        table: 表名
        filter_condition: SQL WHERE 子句
    """
    try:
        from scipy import stats as sp_stats
        df = _load_df(source_id, table, filter_condition, limit=2000)
        if target not in df.columns:
            return _safe_json({"error": f"目标列不存在: {target}"})

        feat_cols = [c.strip() for c in (features or []) if c.strip()]
        # 只保留数值特征(分类特征无法直接进线性回归)
        num_feats = [c for c in feat_cols if c in df.columns and pd.api.types.is_numeric_dtype(df[c])]
        if not num_feats:
            return _safe_json({"error": "没有可用的数值特征列"})
        if len(num_feats) > 8:
            num_feats = num_feats[:8]

        # listwise deletion (整行删除): 缺失值直接删行, 不做均值填充,
        # 避免 fillna(median) 人为注入数据扭曲回归系数
        reg_df = df[num_feats + [target]].dropna()
        X = reg_df[num_feats].values
        y = reg_df[target].values
        n = len(y)
        if n < len(num_feats) + 2:
            return _safe_json({"error": f"样本量不足: {n} 行 (含缺失删行后), 至少需要 {len(num_feats)+2}"})

        X_design = np.column_stack([np.ones(n), X])
        coef, *_ = np.linalg.lstsq(X_design, y, rcond=None)
        y_pred = X_design @ coef
        resid = y - y_pred
        ss_res = float(np.sum(resid ** 2))
        ss_tot = float(np.sum((y - y.mean()) ** 2))
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0

        # 标准误 + p 值 (t 分布)
        dof = n - len(coef)
        mse = ss_res / dof if dof > 0 else 0
        cov = mse * np.linalg.inv(X_design.T @ X_design) if mse > 0 else np.zeros_like(X_design.T @ X_design)
        se = np.sqrt(np.diag(cov))
        t_stats = coef / se
        p_values = 2 * (1 - sp_stats.t.cdf(np.abs(t_stats), dof))

        # VIF 共线性诊断: 每个特征对其他特征回归取 R², VIF=1/(1-R²)
        # VIF > 10 → 特征高度共线, 系数不可靠 (提示 LLM 不要解读单个系数)
        vif: dict[str, float] = {}
        if len(num_feats) > 1:
            for feat in num_feats:
                others = [f for f in num_feats if f != feat]
                X_o = reg_df[others].values
                X_o_design = np.column_stack([np.ones(n), X_o])
                coef_o, *_ = np.linalg.lstsq(X_o_design, reg_df[feat].values, rcond=None)
                pred_o = X_o_design @ coef_o
                ss_res_o = float(np.sum((reg_df[feat].values - pred_o) ** 2))
                ss_tot_o = float(np.sum((reg_df[feat].values - reg_df[feat].values.mean()) ** 2))
                r2_o = 1 - ss_res_o / ss_tot_o if ss_tot_o > 0 else 0
                vif[feat] = round(float(1.0 / (1.0 - r2_o)) if r2_o < 1 else 999.0, 2)

        coefs_out = []
        for i, name in enumerate(num_feats):
            coefs_out.append({
                "feature": name,
                "coefficient": round(float(coef[i + 1]), 4),
                "p_value": round(float(p_values[i + 1]), 4),
                "significant": bool(p_values[i + 1] < 0.05),
                "vif": vif.get(name, 1.0),
            })
        adj_r2 = 1 - (1 - r2) * (n - 1) / dof if dof > 0 else r2
        high_vif = {k: v for k, v in vif.items() if v > 10}

        return _safe_json({
            "target": target, "method": "OLS 多元线性回归", "n": n,
            "r_squared": round(r2, 4), "adj_r_squared": round(float(adj_r2), 4),
            "coefficients": coefs_out,
            "significant_features": [c["feature"] for c in coefs_out if c["significant"]],
            "missing_handling": "listwise deletion (缺失行整行删除, 未做填充)",
            "vif_warning": (f"高共线性特征 VIF>10: {high_vif}, 这些特征的单个系数不可靠, 勿解读为独立影响"
                            if high_vif else "无高共线性 (VIF 均 ≤10)"),
            "interpretation": (
                "R²越高, 模型解释力越强; 系数p<0.05的特征对目标有统计显著影响"
            ),
        })
    except ImportError:
        return _safe_json({"error": "scipy 未安装"})
    except Exception as e:
        return _safe_json({"error": str(e)})


@tool
def seasonal_analysis(metric: str, date_col: str, source_id: str, table: str = "",
                      period: int = 0, agg_func: str = "auto",
                      filter_condition: str = "") -> str:
    """时间序列分解: 拆出 趋势 + 季节性 + 残差, 判断波动构成。

    加法分解: 先按日期聚合 (口径 agg_func), 趋势=移动平均, 季节=周期内均值, 残差=扣除后剩余。
    period 不传 (0) 时自动检测主周期 (月→12, 日→7), 与 forecast 共用同一检测逻辑。
    回答 "这个指标是稳定增长, 还是被季节波动主导?"

    Args:
        metric: 指标列名
        date_col: 日期列名
        source_id: 数据源ID
        table: 表名
        period: 季节周期 (0=自动检测; 7=周, 12=月, 4=季)
        agg_func: 聚合口径 auto|sum|avg|count (auto: 率/价类→avg, 其余→sum)
        filter_condition: SQL WHERE 子句
    """
    try:
        df = _load_df(source_id, table, filter_condition)
        if metric not in df.columns or date_col not in df.columns:
            return _safe_json({"error": f"列不存在: {metric} 或 {date_col}"})

        # 先按日期聚合再分解 — 明细行不是时间序列 (同一天多行会破坏周期)
        func = _resolve_agg(metric, agg_func)
        agg_map = {"sum": "sum", "avg": "mean", "count": "count"}
        dates = pd.to_datetime(df[date_col], errors="coerce")
        df = df.assign(_date=dates).dropna(subset=["_date"])
        if df.empty:
            return _safe_json({"error": f"日期列 {date_col} 无有效日期"})
        s = df.groupby(pd.Grouper(key="_date", freq="D"))[metric].agg(agg_map[func]).dropna()
        values = s.values.astype(float)
        if len(values) < 8:
            return _safe_json({"error": f"按日聚合后有效数据点不足 8 个"})

        # 自动检测周期 (与 forecast 共用)
        auto_detected = False
        if period <= 0:
            period = _detect_period(values) or 7
            auto_detected = True
        if len(values) < period * 2:
            return _safe_json({"error": f"数据量不足: {len(values)} 个时间点, 需要至少 {period*2} 做分解"})

        # 趋势: 中心移动平均
        trend = pd.Series(values).rolling(window=period, center=True).mean().values
        trend = np.array([v if not np.isnan(v) else values[i] for i, v in enumerate(trend)])

        # 季节: 残差中按周期位置取均值
        detrended = values - trend
        seasonal = np.zeros_like(values)
        for p in range(period):
            idx = np.arange(p, len(values), period)
            if len(idx) > 0:
                seasonal[idx] = detrended[idx].mean()
        resid = values - trend - seasonal

        # 强度指标 (0~1): 季节方差占比 / 总去趋势方差
        var_detrended = np.var(detrended)
        strength = float(np.var(seasonal) / var_detrended) if var_detrended > 0 else 0
        strength = max(0, min(1, strength))

        # 季节模式
        period_labels = [f"period_{p+1}" for p in range(period)]
        pattern = [
            {"period": period_labels[p], "avg_deviation": round(float(seasonal[list(range(p, len(values), period))].mean()), 4)}
            for p in range(period) if len(list(range(p, len(values), period))) > 0
        ]

        # 趋势方向
        half = len(trend) // 2
        start_mean, end_mean = trend[:half].mean(), trend[half:].mean()
        slope = end_mean - start_mean

        return _safe_json({
            "metric": metric, "period": period,
            "period_source": "auto_detected" if auto_detected else "explicit",
            "agg_func": func, "n": len(values),
            "seasonality_strength": round(strength, 3),
            "seasonality_level": "强" if strength > 0.5 else "中" if strength > 0.25 else "弱",
            "trend_direction": "上升" if slope > 0 else "下降" if slope < 0 else "平稳",
            "trend_change": round(float(slope), 4),
            "seasonal_pattern": pattern,
            "residual_std": round(float(np.std(resid)), 4),
            "interpretation": "seasonality_strength>0.5 → 波动主要由季节驱动; forecast 会自动做季节调整",
        })
    except Exception as e:
        return _safe_json({"error": str(e)})


@tool
def explain_anomaly(metric: str, date_col: str, source_id: str, table: str = "",
                    threshold: float = settings.ANOMALY_THRESHOLD, top_dim: int = 3,
                    filter_condition: str = "") -> str:
    """异常解释: 定位异常点的同时, 找出它是被哪个维度拖动的。

    对每个 Z-score 超阈值的异常行, 逐维度对比该行值 vs 该维度整体均值,
    偏差最大的维度就是异常的主要来源 (回答 "为什么这天/这行异常")。

    Args:
        metric: 指标列名
        date_col: 日期列名 (用于标记异常点)
        source_id: 数据源ID
        table: 表名
        threshold: Z-score 阈值
        top_dim: 输出偏差最大的 N 个维度
        filter_condition: SQL WHERE 子句
    """
    try:
        df = _load_df(source_id, table, filter_condition)
        if metric not in df.columns:
            return _safe_json({"error": f"列不存在: {metric}"})
        if not pd.api.types.is_numeric_dtype(df[metric]):
            return _safe_json({"error": f"{metric} 不是数值列"})

        values = df[metric].dropna()
        if len(values) < 5:
            return _safe_json({"error": "数据量不足"})
        mean, std = values.mean(), values.std() or 1
        z_scores = (df[metric] - mean) / std

        # 找异常行
        anomaly_mask = z_scores.abs() > threshold
        anomaly_indices = df.index[anomaly_mask].tolist()
        if not anomaly_indices:
            return _safe_json({"metric": metric, "anomaly_count": 0, "anomalies": [], "explanations": [], "note": "未检测到异常"})

        # 维度列: 非数值、非指标、基数适中的列
        dim_cols = []
        for col in df.columns:
            if col == metric or col == date_col:
                continue
            if not pd.api.types.is_numeric_dtype(df[col]):
                if df[col].nunique() <= 30:
                    dim_cols.append(col)
        dim_cols = dim_cols[:top_dim]

        explanations = []
        for idx in anomaly_indices[:5]:  # 最多解释5个异常
            row = df.loc[idx]
            z = float(z_scores.loc[idx])
            dims = []
            for col in dim_cols:
                val = row.get(col)
                if val is None or pd.isna(val):
                    continue
                group_mean = df.loc[df[col] == val, metric].mean()
                overall_mean = mean
                deviation = (group_mean - overall_mean) / std if std > 0 else 0
                dims.append({"dimension": col, "value": str(val), "deviation_z": round(float(deviation), 3)})
            dims.sort(key=lambda d: abs(d["deviation_z"]), reverse=True)
            explanations.append({
                "index": int(idx),
                "date": str(row.get(date_col, "?")) if date_col in df.columns else "?",
                "metric_value": round(float(row[metric]), 2),
                "z_score": round(z, 2),
                "top_driving_dimensions": dims[:3],
            })

        return _safe_json({
            "metric": metric, "anomaly_count": len(anomaly_indices),
            "explanations": explanations,
            "interpretation": "top_driving_dimensions 中 deviation_z 绝对值最大的维度, 是异常的主要来源",
        })
    except Exception as e:
        return _safe_json({"error": str(e)})


@tool
def percentile_analysis(metric: str, source_id: str, table: str = "",
                        filter_condition: str = "") -> str:
    """分位数分布分析: 输出 5/25/50/75/95 分位、IQR、偏度, 判断数据形态。

    用于: 识别偏态分布(如营收集中在少数大客户)、判断是否有离群值。

    Args:
        metric: 数值指标列名
        source_id: 数据源ID
        table: 表名
        filter_condition: SQL WHERE 子句
    """
    try:
        from scipy import stats as sp_stats
        df = _load_df(source_id, table, filter_condition)
        if metric not in df.columns:
            return _safe_json({"error": f"列不存在: {metric}"})
        if not pd.api.types.is_numeric_dtype(df[metric]):
            return _safe_json({"error": f"{metric} 不是数值列"})

        values = df[metric].dropna().values
        if len(values) < 5:
            return _safe_json({"error": "数据量不足"})

        q = np.percentile(values, [5, 25, 50, 75, 95])
        iqr = q[3] - q[1]
        skew = float(sp_stats.skew(values))

        # 集中度: top 20% 占总量比例
        sorted_v = np.sort(values)[::-1]
        top20_share = float(sorted_v[: max(1, len(sorted_v) // 5)].sum() / values.sum()) if values.sum() != 0 else 0

        shape = "右偏(大量小值+少量极大值)" if skew > 0.5 else \
                "左偏(大量大值+少量极小值)" if skew < -0.5 else "近似对称"

        return _safe_json({
            "metric": metric, "n": len(values),
            "p5": round(float(q[0]), 2), "p25": round(float(q[1]), 2),
            "median": round(float(q[2]), 2), "p75": round(float(q[3]), 2),
            "p95": round(float(q[4]), 2), "iqr": round(float(iqr), 2),
            "skewness": round(skew, 3), "distribution_shape": shape,
            "top20_concentration": round(top20_share * 100, 1),
            "interpretation": "top20_concentration>60% → 高度集中; 偏度>0.5 → 均值被极值拉高",
        })
    except ImportError:
        return _safe_json({"error": "scipy 未安装"})
    except Exception as e:
        return _safe_json({"error": str(e)})


@tool
def compare(metric: str, date_col: str, source_id: str, table: str = "",
            period: str = "mom", agg_func: str = "auto", filter_condition: str = "") -> str:
    """环比/同比对比: 当前周期 vs 上一周期, 从原始表按日期聚合。

    两期对比 + 变化率; 序列足够时附**两段 Welch t 检验** (最近 N 期 vs 前 N 期),
    判断变化是真实的还是噪声。期数不足时标注描述性 (结论应标 [弱])。

    Args:
        metric: 指标列名
        date_col: 日期列名
        source_id: 数据源ID
        table: 表名 (留空自动选最大表)
        period: "dod"(日环比), "wow"(周环比), "mom"(月环比), "qoq"(季环比), "yoy"(年同比)
        agg_func: 聚合口径 auto|sum|avg|count (auto: 率/价类→avg, 其余→sum)
        filter_condition: SQL WHERE 子句
    """
    try:
        from scipy import stats as sp_stats
        df = _load_df(source_id, table, filter_condition)
        if metric not in df.columns or date_col not in df.columns:
            return _safe_json({"error": f"列不存在: {metric} 或 {date_col}"})
        if not pd.api.types.is_numeric_dtype(df[metric]):
            return _safe_json({"error": f"{metric} 不是数值列"})

        func = _resolve_agg(metric, agg_func)
        agg_map = {"sum": "sum", "avg": "mean", "count": "count"}

        # 解析日期并按周期聚合
        dates = pd.to_datetime(df[date_col], errors="coerce")
        df = df.assign(_date=dates).dropna(subset=["_date"])
        if df.empty:
            return _safe_json({"error": f"日期列 {date_col} 无有效日期"})

        # pandas 3.0: M→ME(月末), Q→QE(季末), W/D 不变
        freq = {"dod": "D", "wow": "W", "mom": "ME", "qoq": "QE", "yoy": "Y"}.get(period, "ME")
        series = df.groupby(pd.Grouper(key="_date", freq=freq))[metric].agg(agg_map[func]).dropna()
        if len(series) < 2:
            return _safe_json({"error": f"按 {period} 聚合后不足 2 期, 数据时间跨度不够"})

        current, prev = float(series.iloc[-1]), float(series.iloc[-2])
        change = (current - prev) / abs(prev) * 100 if prev != 0 else None
        trend = "上升" if (change or 0) > 0 else "下降" if (change or 0) < 0 else "平稳"

        # 显著性: 最近 N 期 vs 前 N 期 (两段 Welch t); 期数不足则描述性
        win = {"dod": 7, "wow": 4, "mom": 3, "qoq": 2, "yoy": 2}.get(period, 3)
        significant, p_value = None, None
        vals = series.values.astype(float)
        if len(vals) >= 2 * win:
            a, b = vals[-win:], vals[-2 * win:-win]
            _, p_value = sp_stats.ttest_ind(a, b, equal_var=False)
            significant = bool(p_value < 0.05)
            p_value = round(float(p_value), 4)

        return _safe_json({
            "metric": metric, "date_col": date_col, "period": period, "agg_func": func,
            "current_period_value": round(current, 2),
            "prev_period_value": round(prev, 2),
            "change_pct": round(change, 2) if change is not None else None,
            "trend": trend,
            "significant": significant,
            "p_value": p_value,
            "window_periods": win,
            "test_note": (f"两段 Welch t (最近 {win} 期 vs 前 {win} 期)"
                          if significant is not None
                          else f"期数不足 {2 * win}, 变化为描述性, 结论应标 [弱]"),
            "periods_covered": len(series),
            "period_labels": [str(d)[:10] for d in series.index],
        })
    except ImportError:
        return _safe_json({"error": "scipy 未安装"})
    except Exception as e:
        return _safe_json({"error": str(e)})
