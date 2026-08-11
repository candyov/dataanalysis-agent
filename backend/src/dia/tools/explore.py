"""能力工具 — explore / test_difference / attribution

能力导向设计: 工具按"分析任务"划分, 不按算法。
LLM 只识别任务+填参数; 算法选择(检验方法/聚合粒度/归并阈值)由确定性代码决定。

- explore:          探索/聚合/交叉表/趋势/top_n/占比/描述统计 (吸收 drill_down/rank/compare/decompose/percentile)
- test_difference:  差异显著性检验, 自动选方法 (2组→Welch t, >2组→ANOVA, 非正态→非参) + BH 校正
- attribution:      归因分析: 相关扫描→多元回归(VIF)→贡献度拆解 (吸收 find_drivers/regression_analysis)
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any

import numpy as np
import pandas as pd
from langchain_core.tools import tool

from dia.infrastructure.database.manager import get_datasource_manager
from dia.core.config import settings

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
#  公共辅助
# ══════════════════════════════════════════════════════════════════

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


def _load_df(source_id: str, table: str, filter_condition: str = "", limit: int = None) -> pd.DataFrame:
    """全量加载 (max_rows=None 不走 500 行截断, 能力工具需要全量聚合)."""
    table = _resolve_table(source_id, table)
    where = _build_where(filter_condition)
    conn = get_datasource_manager().connect(source_id)
    limit_sql = f" LIMIT {limit}" if limit else ""
    result = conn.query(f"SELECT * FROM {table}{where}{limit_sql}", max_rows=None)
    if "error" in result:
        raise ValueError(result["error"])
    return pd.DataFrame(result["rows"])


def _col_check(df: pd.DataFrame, cols: list[str], table: str) -> dict | None:
    """列存在性校验, 缺失返回 error dict"""
    missing = [c for c in cols if c and c not in df.columns]
    if missing:
        return {"error": f"列不存在: {missing}. 实际列: {', '.join(sorted(df.columns)[:20])}"}
    return None


def _safe_json(data: dict) -> str:
    """序列化并兜底 NaN/Inf → None"""
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
#  口径 (聚合方式) — 能力参数化: LLM 按指标类型声明, auto 代码推断
# ══════════════════════════════════════════════════════════════════

_AGG_MAP = {"sum": "sum", "avg": "mean", "count": "count", "median": "median"}


def _resolve_agg_func(metric: str, agg_func: str) -> str:
    """解析聚合口径: 显式指定优先; auto 按指标名推断.

    率/价/单价类指标 (客单价/利润率/均价) 用 sum 会算错账,
    auto 推断把这些均值型指标映射到 avg, 其余默认 sum.
    count/median 必须显式传 (auto 不会猜).
    """
    if agg_func and agg_func != "auto":
        return agg_func if agg_func in _AGG_MAP else "sum"
    m = (metric or "").lower()
    if any(k in m for k in ("率", "ratio", "rate", "价", "price", "单价", "客单价",
                            "avg", "mean", "unit", "per")):
        return "avg"
    return "sum"


# ══════════════════════════════════════════════════════════════════
#  explore — 探索/聚合/交叉/趋势/占比/描述
# ══════════════════════════════════════════════════════════════════

@tool
def explore(operation: str, metric: str = "", source_id: str = "", table: str = "",
            group_by: str = "", group_by2: str = "", date_col: str = "",
            agg_func: str = "auto", filter_condition: str = "", top_n: int = 10) -> str:
    """探索分析: 一个工具 6 种操作, 全部确定性实现。

    Args:
        operation: 必填, 操作类型:
            - aggregate: 单维度分组汇总 → [{group, value, sum, avg, count}]
            - cross_tab: 双维度交叉表 → {行维度: {列维度: 值}} (如 区域×品类)
            - trend: 按日期粒度趋势 → {periods, values, trend_direction, change_pct}
            - top_n: 按指标取 TopN 行 (整行返回, 用于大单特征分析)
            - share: 构成占比 → [{group, value, pct}], 尾部自动归并为"其他" (口径固定 sum)
            - describe: 描述统计 → 均值/分位/偏度/集中度/分布形态
        metric: 数值列名
        source_id: 数据源ID
        table: 表名 (留空自动选最大表)
        group_by: 维度列 (aggregate/cross_tab/share 用)
        group_by2: 第二维度列 (cross_tab 用)
        date_col: 日期列 (trend 用)
        agg_func: 聚合口径 auto|sum|avg|count|median.
            auto 按指标名推断: 率/价/单价类 (客单价/利润率) → avg, 其余 → sum.
            **均值型指标 (客单价/均价/利润率) 必须用 avg, 用 sum 会算错账.**
        filter_condition: SQL WHERE 子句 (可选)
        top_n: 返回前 N (aggregate/top_n/share 用)
    """
    try:
        df = _load_df(source_id, table, filter_condition)
        if df.empty:
            return _safe_json({"error": "数据为空"})

        if operation == "aggregate":
            err = _col_check(df, [metric, group_by], table)
            if err: return _safe_json(err)
            func = _resolve_agg_func(metric, agg_func)
            g = df.groupby(group_by)[metric].agg(["sum", "mean", "median", "count"]) \
                .sort_values(_AGG_MAP[func], ascending=False).head(top_n)
            rows = [{"group": str(i), "value": round(float(r[_AGG_MAP[func]]), 2),
                     "sum": round(float(r["sum"]), 2), "avg": round(float(r["mean"]), 2),
                     "median": round(float(r["median"]), 2), "count": int(r["count"])}
                    for i, r in g.iterrows()]
            return _safe_json({"metric": metric, "group_by": group_by, "agg_func": func, "groups": rows})

        if operation == "cross_tab":
            err = _col_check(df, [metric, group_by, group_by2], table)
            if err: return _safe_json(err)
            func = _resolve_agg_func(metric, agg_func)
            pivot = df.pivot_table(index=group_by, columns=group_by2, values=metric,
                                   aggfunc=_AGG_MAP[func]).fillna(0)
            out = {str(idx): {str(c): round(float(v), 2) for c, v in row.items()} for idx, row in pivot.iterrows()}
            return _safe_json({"metric": metric, "row_dim": group_by, "col_dim": group_by2,
                               "agg_func": func, "table": out})

        if operation == "trend":
            err = _col_check(df, [metric, date_col], table)
            if err: return _safe_json(err)
            func = _resolve_agg_func(metric, agg_func)
            dates = pd.to_datetime(df[date_col], errors="coerce")
            df = df.assign(_date=dates).dropna(subset=["_date"])
            if df.empty:
                return _safe_json({"error": f"日期列 {date_col} 无有效日期"})
            span_days = max((df["_date"].max() - df["_date"].min()).days + 1, 1)
            # 自动选粒度: 跨度>300天→月, >60天→周, 否则日 (避免 LLM 传错 freq)
            freq = "ME" if span_days > 300 else ("W" if span_days > 60 else "D")
            s = df.groupby(pd.Grouper(key="_date", freq=freq))[metric].agg(_AGG_MAP[func]).dropna()
            if len(s) < 2:
                return _safe_json({"error": f"按粒度聚合后不足 2 期 (跨度 {span_days} 天)"})
            periods = [str(d)[:10] for d in s.index]
            values = [round(float(v), 2) for v in s.values]
            current, prev = values[-1], values[-2]
            change = (current - prev) / abs(prev) * 100 if prev != 0 else None
            direction = "上升" if (change or 0) > 0 else "下降" if (change or 0) < 0 else "平稳"
            return _safe_json({
                "metric": metric, "date_col": date_col, "agg_func": func,
                "grain": {"ME": "month", "W": "week", "D": "day"}[freq],
                "periods": periods, "values": values, "periods_covered": len(s),
                "current_period_value": current, "prev_period_value": prev,
                "change_pct": round(change, 2) if change is not None else None,
                "trend_direction": direction,
            })

        if operation == "top_n":
            err = _col_check(df, [metric], table)
            if err: return _safe_json(err)
            top = df.nlargest(top_n, metric)
            rows = []
            for _, r in top.iterrows():
                row = {k: (round(float(v), 2) if isinstance(v, (int, float, np.number)) else str(v))
                       for k, v in r.items()}
                rows.append(row)
            return _safe_json({"metric": metric, "top_n": len(rows), "rows": rows})

        if operation == "share":
            err = _col_check(df, [metric, group_by], table)
            if err: return _safe_json(err)
            g = df.groupby(group_by)[metric].sum().sort_values(ascending=False)
            total = float(g.sum())
            if total == 0:
                return _safe_json({"error": "指标合计为 0, 无法计算占比"})
            head = g.head(top_n)
            rows = [{"group": str(i), "value": round(float(v), 2), "pct": round(float(v) / total * 100, 1)}
                    for i, v in head.items()]
            rest = total - float(head.sum())
            if rest > 0:
                rows.append({"group": "其他", "value": round(rest, 2), "pct": round(rest / total * 100, 1)})
            return _safe_json({"metric": metric, "group_by": group_by, "agg_func": "sum",
                               "total": round(total, 2), "groups": rows})

        if operation == "describe":
            err = _col_check(df, [metric], table)
            if err: return _safe_json(err)
            try:
                from scipy import stats as sp_stats
            except ImportError:
                sp_stats = None
            v = pd.to_numeric(df[metric], errors="coerce").dropna().values
            if len(v) < 5:
                return _safe_json({"error": f"{metric} 有效值不足 5 个"})
            q = np.percentile(v, [5, 25, 50, 75, 95])
            skew = float(sp_stats.skew(v)) if sp_stats else 0.0
            sorted_v = np.sort(v)[::-1]
            top20_share = float(sorted_v[: max(1, len(sorted_v) // 5)].sum() / v.sum()) if v.sum() != 0 else 0
            shape = ("右偏(大量小值+少量极大值)" if skew > 0.5 else
                     "左偏(大量大值+少量极小值)" if skew < -0.5 else "近似对称")
            return _safe_json({
                "metric": metric, "n": int(len(v)),
                "p5": round(float(q[0]), 2), "p25": round(float(q[1]), 2), "median": round(float(q[2]), 2),
                "p75": round(float(q[3]), 2), "p95": round(float(q[4]), 2),
                "skewness": round(skew, 3), "distribution_shape": shape,
                "top20_concentration": round(top20_share * 100, 1),
            })

        return _safe_json({"error": f"未知 operation: {operation}. 可选: aggregate/cross_tab/trend/top_n/share/describe"})
    except Exception as e:
        return _safe_json({"error": f"explore 失败: {e}"})


# ══════════════════════════════════════════════════════════════════
#  test_difference — 差异显著性检验 (自动选方法)
# ══════════════════════════════════════════════════════════════════

@tool
def test_difference(metric: str, group_by: str, source_id: str, table: str = "",
                    top_n: int = 0, filter_condition: str = "") -> str:
    """验证分组间指标差异是否统计显著。自动选择检验方法。

    确定性算法选择:
    - 2 组 → Welch t 检验 (方差不齐也适用) + Cohen's d 效应量 + 均值差 95% CI
    - >2 组 → 单因素 ANOVA + 事后两两 Welch t
    - 任一组不满足正态 (Shapiro, n<5000) → Mann-Whitney U / Kruskal-Wallis (非参)
    - 多对比较自动做 Benjamini-Hochberg FDR 校正 (防 α 膨胀)
    输出: 每对 p 值(校正后)、效应量、均值差 CI、是否显著。p<0.05 → 差异显著, 结论可信。

    Args:
        metric: 数值指标列名
        group_by: 分组维度列名
        source_id: 数据源ID
        table: 表名 (留空自动选最大表)
        top_n: 只比较按总和最大的前 N 组 (0 = 全部, 但上限 8 组;
            组数过多时两两比较在 BH 校正后功效大降, 建议 ≤5)
        filter_condition: SQL WHERE 子句 (可选)
    """
    try:
        from scipy import stats as sp_stats
        df = _load_df(source_id, table, filter_condition)
        err = _col_check(df, [metric, group_by], table)
        if err: return _safe_json(err)
        if not pd.api.types.is_numeric_dtype(df[metric]):
            return _safe_json({"error": f"{metric} 不是数值列 (dtype={df[metric].dtype})"})

        # 每组 ≥10 个非空值才参与检验 (样本 <10 时检验功效≈0, p 值无意义)
        groups = {str(k): v[metric].dropna().values
                  for k, v in df.groupby(group_by) if len(v[metric].dropna()) >= 10}
        if len(groups) < 2:
            return _safe_json({"error": f"有效分组不足 2 个 (每组需 ≥10 个非空值)"})

        names = sorted(groups, key=lambda k: -groups[k].sum())
        # 组数上限: 两两比较组合爆炸 → BH 校正后功效大降
        if top_n and top_n > 0:
            names = names[:top_n]
        elif len(names) > 8:
            names = names[:8]
        if len(names) < 2:
            return _safe_json({"error": f"top_n 截断后有效分组不足 2 个"})
        n_groups = len(names)

        # 正态性检查 (n<5000 时 Shapiro, 否则跳过 — 大样本 CLT 适用)
        check_normal = all(len(groups[k]) < 5000 for k in names)
        non_normal = False
        if check_normal:
            for k in names:
                if len(groups[k]) >= 8:
                    _, p = sp_stats.shapiro(groups[k])
                    if p < 0.05:
                        non_normal = True
                        break

        method = ""
        pairs = []
        if n_groups == 2:
            a, b = groups[names[0]], groups[names[1]]
            if non_normal:
                method = "Mann-Whitney U (非参, 因不满足正态)"
                stat, p = sp_stats.mannwhitneyu(a, b, alternative="two-sided")
                eff = _effect_size_nonparam(a, b)
            else:
                method = "Welch t-test"
                stat, p = sp_stats.ttest_ind(a, b, equal_var=False)
                eff = _cohens_d(a, b)
            pairs.append(_mk_pair(names[0], a, names[1], b, float(p), eff, method, stat))
        else:
            # 多组: 先 Kruskal-Wallis 或 ANOVA 做整体检验
            if non_normal:
                method = "Kruskal-Wallis (非参)"
                stat, p = sp_stats.kruskal(*[groups[k] for k in names])
            else:
                method = "单因素 ANOVA"
                stat, p = sp_stats.f_oneway(*[groups[k] for k in names])
            overall = {"test": method, "statistic": round(float(stat), 3), "p_value": round(float(p), 4),
                       "significant": bool(p < 0.05), "n_groups": n_groups}
            # 事后两两比较 (全部对)
            for i in range(len(names)):
                for j in range(i + 1, len(names)):
                    a, b = groups[names[i]], groups[names[j]]
                    if non_normal:
                        s2, p2 = sp_stats.mannwhitneyu(a, b, alternative="two-sided")
                        eff = _effect_size_nonparam(a, b)
                        m2 = "Mann-Whitney U"
                    else:
                        s2, p2 = sp_stats.ttest_ind(a, b, equal_var=False)
                        eff = _cohens_d(a, b)
                        m2 = "Welch t-test"
                    pairs.append(_mk_pair(names[i], a, names[j], b, float(p2), eff, m2, s2))

        # BH FDR 校正 (多对比较时)
        if len(pairs) > 1:
            p_vals = np.array([p["p_value"] for p in pairs])
            n = len(p_vals)
            order = np.argsort(p_vals)
            adjusted = np.empty(n)
            for rank, idx in enumerate(order, start=1):
                adjusted[idx] = p_vals[idx] * n / rank
            # 保序修正必须沿排序顺序 (order) 取 min, 否则 adjusted p 错位
            for i in range(n - 2, -1, -1):
                adjusted[order[i]] = min(adjusted[order[i]], adjusted[order[i + 1]])
            adjusted = np.minimum(adjusted, 1.0)
            for p, ap in zip(pairs, adjusted):
                p["p_value_adjusted"] = round(float(ap), 4)
                p["significant"] = bool(ap < 0.05)
        else:
            for p in pairs:
                p["p_value_adjusted"] = p["p_value"]
                p["significant"] = bool(p["p_value"] < 0.05)

        out: dict[str, Any] = {
            "metric": metric, "group_by": group_by, "n_groups": n_groups,
            "method": method if n_groups == 2 else f"{method} + BH FDR 校正",
            "alpha": 0.05, "pairs": pairs,
            "conclusion": "差异显著" if any(p["significant"] for p in pairs) else "无显著差异",
        }
        if n_groups > 2:
            out["overall"] = overall
        return _safe_json(out)
    except ImportError:
        return _safe_json({"error": "scipy 未安装"})
    except Exception as e:
        return _safe_json({"error": f"test_difference 失败: {e}"})


def _cohens_d(a: np.ndarray, b: np.ndarray) -> float:
    pooled = ((a.std(ddof=1) ** 2 + b.std(ddof=1) ** 2) / 2) ** 0.5
    return round(float((a.mean() - b.mean()) / pooled), 3) if pooled > 0 else 0.0


def _effect_size_nonparam(a: np.ndarray, b: np.ndarray) -> float:
    """非参效应量: r = Z/sqrt(N)"""
    from scipy import stats as sp_stats
    stat, _ = sp_stats.mannwhitneyu(a, b, alternative="two-sided")
    n = len(a) + len(b)
    z = sp_stats.norm.ppf(sp_stats.norm.cdf(stat))  # 近似
    try:
        # 更稳: 用 U 统计量转 Z
        mu = len(a) * len(b) / 2
        sigma = math.sqrt(len(a) * len(b) * (n + 1) / 12)
        z = (stat - mu) / sigma if sigma > 0 else 0
    except Exception:
        pass
    return round(float(z / math.sqrt(n)), 3) if n > 0 else 0.0


def _mk_pair(na, a, nb, b, p, eff, method, stat) -> dict:
    """构造一对比较的结果, 含均值差 95% CI (Welch-Satterthwaite)."""
    from scipy import stats as sp_stats
    mean_a, mean_b = float(np.mean(a)), float(np.mean(b))
    n_a, n_b = len(a), len(b)
    se_a = float(np.std(a, ddof=1)) / math.sqrt(n_a) if n_a > 1 else 0.0
    se_b = float(np.std(b, ddof=1)) / math.sqrt(n_b) if n_b > 1 else 0.0
    se = math.sqrt(se_a ** 2 + se_b ** 2)
    diff = mean_a - mean_b
    dof = ((se_a ** 2 + se_b ** 2) ** 2 /
           (se_a ** 4 / max(n_a - 1, 1) + se_b ** 4 / max(n_b - 1, 1))) if se > 0 else 1
    t_crit = float(sp_stats.t.ppf(0.975, max(dof, 1)))
    ci_low, ci_high = diff - t_crit * se, diff + t_crit * se
    return {
        "group_a": na, "group_b": nb,
        "mean_a": round(mean_a, 2), "mean_b": round(mean_b, 2),
        "mean_diff": round(diff, 2),
        "mean_diff_ci": [round(ci_low, 2), round(ci_high, 2)],
        "n_a": int(n_a), "n_b": int(n_b),
        "statistic": round(float(stat), 3), "p_value": round(float(p), 4),
        "effect_size": eff, "pair_method": method,
    }


# ══════════════════════════════════════════════════════════════════
#  attribution — 归因: 相关扫描 → 多元回归(VIF) → 贡献度
# ══════════════════════════════════════════════════════════════════

@tool
def attribution(target: str, source_id: str, table: str = "", features: list[str] = None,
                filter_condition: str = "") -> str:
    """归因分析: 什么驱动了目标指标。

    三步确定性分析:
    1. 相关扫描: 目标与所有数值列的相关性, 排序出候选驱动因素
    2. 多元回归: 候选因素进 OLS (缺失行整行删除), 输出系数/p 值/VIF, 控混淆变量
    3. 结论: 显著特征 = 真正的驱动因素 (相关≠因果, 回归后仍显著才是)

    Args:
        target: 目标指标列名
        source_id: 数据源ID
        table: 表名
        features: 候选特征列 (留空自动选全部数值列, 最多8个;
            可传分类列如 region/category, ≤8 类自动 one-hot 进回归)
        filter_condition: SQL WHERE 子句 (可选)
    """
    try:
        from scipy import stats as sp_stats
        df = _load_df(source_id, table, filter_condition)
        err = _col_check(df, [target], table)
        if err: return _safe_json(err)
        if not pd.api.types.is_numeric_dtype(df[target]):
            return _safe_json({"error": f"{target} 不是数值列"})

        # 1. 相关扫描
        num_cols = df.select_dtypes(include=[np.number]).columns.tolist()
        num_cols = [c for c in num_cols if c != target]
        correlations = {}
        for col in num_cols:
            corr = df[[target, col]].dropna().corr().iloc[0, 1]
            if not np.isnan(corr):
                correlations[col] = round(float(corr), 3)
        top_corr = sorted(correlations.items(), key=lambda x: abs(x[1]), reverse=True)[:10]

        # 2. 候选特征 (显式传入 or 相关性前8): 数值列 + 显式分类列 (≤8 类 one-hot)
        feat_cols = [c.strip() for c in (features or []) if c.strip()]
        if not feat_cols:
            feat_cols = [c for c, _ in top_corr[:8]]
        num_feats = [c for c in feat_cols if c in df.columns and pd.api.types.is_numeric_dtype(df[c])][:6]
        cat_feats = [c for c in feat_cols
                     if c in df.columns and not pd.api.types.is_numeric_dtype(df[c])
                     and df[c].nunique() <= 8][:3]
        if not num_feats and not cat_feats:
            return _safe_json({"error": "没有可用的特征列 (数值列或 ≤8 类的分类列)", "correlations": top_corr})

        # one-hot 编码分类特征 (drop_first 防共线; 区域/品类这类最强驱动不再被排除)
        parts = [df[num_feats].reset_index(drop=True)] if num_feats else []
        cat_labels: list[str] = []
        for c in cat_feats:
            dummies = pd.get_dummies(df[c], prefix="", prefix_sep="", drop_first=True).astype(float)
            dummies.columns = [f"{c}={v}" for v in dummies.columns]
            parts.append(dummies)
            cat_labels += list(dummies.columns)
        reg_df = pd.concat(parts + [df[[target]].reset_index(drop=True)], axis=1).dropna()
        feat_labels = num_feats + cat_labels
        X = reg_df[feat_labels].values
        y = reg_df[target].values
        n = len(y)
        if n < len(feat_labels) + 2:
            return _safe_json({"error": f"样本量不足: {n} 行 (one-hot 后 {len(feat_labels)} 个特征), 至少需要 {len(feat_labels)+2}",
                               "correlations": top_corr})

        # 3. OLS
        X_design = np.column_stack([np.ones(n), X])
        coef, *_ = np.linalg.lstsq(X_design, y, rcond=None)
        y_pred = X_design @ coef
        resid = y - y_pred
        ss_res = float(np.sum(resid ** 2))
        ss_tot = float(np.sum((y - y.mean()) ** 2))
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
        dof = n - len(coef)
        mse = ss_res / dof if dof > 0 else 0
        cov = mse * np.linalg.inv(X_design.T @ X_design) if mse > 0 else np.zeros_like(X_design.T @ X_design)
        se = np.sqrt(np.diag(cov))
        t_stats = coef / se
        p_values = 2 * (1 - sp_stats.t.cdf(np.abs(t_stats), dof))

        # VIF (仅数值列: one-hot 列间本身相关, VIF 无意义)
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
        for i, name in enumerate(feat_labels):
            coefs_out.append({
                "feature": name,
                "coefficient": round(float(coef[i + 1]), 4),
                "p_value": round(float(p_values[i + 1]), 4),
                "significant": bool(p_values[i + 1] < 0.05),
                "vif": vif.get(name, 1.0),
                "correlation": correlations.get(name),
            })
        sig_feats = [c for c in coefs_out if c["significant"]]
        high_vif = {k: v for k, v in vif.items() if v > 10}

        return _safe_json({
            "target": target, "method": "相关扫描 + OLS 多元回归", "n": n,
            "r_squared": round(r2, 4),
            "correlations_top": [{"feature": k, "correlation": v} for k, v in top_corr],
            "coefficients": coefs_out,
            "significant_features": [c["feature"] for c in sig_feats],
            "vif_warning": (f"高共线性 VIF>10: {high_vif}, 这些特征系数不可靠, 勿解读为独立影响"
                            if high_vif else "无高共线性 (VIF 均 ≤10)"),
            "interpretation": ("显著特征(p<0.05)在控制其他变量后仍影响目标 = 真正的驱动因素; "
                               "相关≠因果, 回归显著才可信"),
        })
    except ImportError:
        return _safe_json({"error": "scipy 未安装"})
    except Exception as e:
        return _safe_json({"error": f"attribution 失败: {e}"})
