"""Data tools — 数据访问

describe: 扫描数据库 schema
query: 执行 SQL 查询
profile: 统计画像
assess_quality: 数据质量检查
sample_rows: 随机采样真数据
date_range: 日期列时间跨度
"""

from langchain_core.tools import tool
import pandas as pd
import numpy as np
import json
import logging
import re

from dia.infrastructure.database.manager import get_datasource_manager

logger = logging.getLogger(__name__)


@tool
def inspect(source_id: str, table: str = "", depth: str = "full") -> str:
    """探查数据结构: 表/列/类型/行数 + 采样 + 列语义推断。

    depth 控制探查深度 (确定性):
    - "structure": 仅结构 (表清单/列名/类型/行数)
    - "sample": 结构 + 每列采样值 (看真实数据, 理解编码含义)
    - "full": 结构 + 采样 + 数值列统计 + 列角色推断 (dimension/metric/datetime)

    Args:
        source_id: 数据源ID
        table: 表名 (留空扫描所有表)
        depth: structure | sample | full (默认 full)
    """
    try:
        mgr = get_datasource_manager()
        conn = mgr.connect(source_id)
        schema = conn.get_schema()
        tables = conn.list_tables()

        lines = [f"数据库 {source_id}: {len(tables)} 个表"]
        targets = [table] if table else tables[:10]
        for t in targets:
            info = schema.get(t, {})
            cols = info.get("columns", [])
            samples = info.get("samples", [])
            row_count = info.get("row_count", "?")
            lines.append(f"\n  {t}: {row_count} 行, {len(cols)} 列")
            if depth == "structure":
                col_desc = ", ".join(f"{c['name']}({c.get('type','?')})" for c in cols[:15])
                lines.append(f"    列: {col_desc}")
                continue

            # sample/full: 列 + 采样值
            col_lines = []
            for c in cols[:15]:
                name, ctype = c["name"], c.get("type", "?")
                val = ""
                for s in samples:
                    if name in s:
                        val = str(s[name])[:30]
                        break
                col_lines.append(f"{name}({ctype})={val}")
            lines.append("    列: " + " | ".join(col_lines))

            if depth == "full":
                # 数值列统计 + 角色推断
                try:
                    result = conn.query(f"SELECT * FROM {t} LIMIT 500", max_rows=None)
                    if "error" not in result and result.get("rows"):
                        dff = pd.DataFrame(result["rows"])
                        stats_lines = []
                        role_lines = []
                        for col in dff.columns[:15]:
                            role = _infer_col_role(col, dff)
                            role_lines.append(f"{col}={role}")
                            if np.issubdtype(dff[col].dtype, np.number):
                                clean = dff[col].dropna()
                                if len(clean):
                                    stats_lines.append(f"{col}: mean={clean.mean():.2f} min={clean.min()} max={clean.max()}")
                        lines.append(f"    角色: {', '.join(role_lines)}")
                        if stats_lines:
                            lines.append(f"    数值统计: {' | '.join(stats_lines[:10])}")
                except Exception:
                    pass
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"[inspect] 失败: {e}")
        return json.dumps({"error": f"inspect 失败: {e}"}, ensure_ascii=False)


def _infer_col_role(col_name: str, df: pd.DataFrame) -> str:
    """列角色推断: datetime / metric / dimension / identifier / description"""
    import numpy as np
    name = col_name.lower()
    if name in ("id", "_id") or name.endswith(("_id", "_key")):
        return "identifier"
    if any(kw in name for kw in ("date", "time", "day", "month", "year", "dt", "timestamp")):
        return "datetime"
    if np.issubdtype(df[col_name].dtype, np.number):
        return "metric"
    if any(kw in name for kw in ("region", "area", "city", "category", "type",
                                 "channel", "brand", "status", "level", "grade",
                                 "segment", "store", "zone", "district",
                                 "区域", "地区", "城市", "品类", "类别", "类型", "渠道",
                                 "品牌", "状态", "等级", "门店", "分区", "部门")):
        return "dimension"
    # 数值型字符串? 看 unique 基数
    if df[col_name].nunique() <= 30:
        return "dimension"
    return "description"


# 双层防御-应用层: 只读 SQL 白名单 (连接层会话只读是另一道)
_READ_ONLY_PREFIXES = ("select", "show", "describe", "desc", "pragma", "explain", "with", "table")


def _assert_readonly_sql(sql: str) -> None:
    """校验 SQL 为只读语句: 拒绝写操作/多语句/注释绕过."""
    body = (sql or "").strip().rstrip(";").strip()
    if not body:
        raise ValueError("SQL 不能为空")
    if ";" in body:
        raise ValueError("仅支持单条 SQL 语句 (多条以分号分隔)")
    # 剥离首部注释后取第一个关键字
    cleaned = re.sub(r"^\s*(--[^\n]*\n|/\*.*?\*/|#[^\n]*\n)*", "", body, flags=re.S).lstrip()
    first = cleaned.split(None, 1)[0].lower() if cleaned.split(None, 1) else ""
    if first not in _READ_ONLY_PREFIXES:
        raise ValueError(f"只读数据源: 仅允许 SELECT/SHOW/DESCRIBE/PRAGMA/EXPLAIN/WITH, 收到 '{first}'")


@tool
def query(sql: str, source_id: str = "") -> str:
    """执行 SELECT 查询并返回结果（自动限制 500 行）。

    Args:
        sql: SELECT 语句 (建议加 LIMIT)
        source_id: 数据源ID
    """
    if not source_id:
        return json.dumps({"error": "需要提供 source_id"}, ensure_ascii=False)

    try:
        # 双层防御-应用层: 只读 SQL 校验 (连接层会话只读兜底)
        _assert_readonly_sql(sql)
        mgr = get_datasource_manager()
        conn = mgr.connect(source_id)
        result = conn.query(sql)

        if "error" in result:
            return json.dumps({"error": result["error"]}, ensure_ascii=False)

        df = pd.DataFrame(result["rows"], columns=result["columns"])
        return json.dumps({
            "columns": result["columns"],
            "row_count": result["row_count"],
            "preview": result["rows"][:10],
            "stats": _quick_stats(df),
        }, ensure_ascii=False, default=str)
    except Exception as e:
        logger.warning(f"[query] 失败: {e}")
        return json.dumps({"error": f"query 失败: {e}"}, ensure_ascii=False)


@tool
def profile(source_id: str, table: str = "", max_cols: int = 20) -> str:
    """对数据库表做统计画像: 每列的类型、分布、统计量、相关性。

    Args:
        source_id: 数据源ID
        table: 表名
        max_cols: 最大分析列数
    """
    try:
        mgr = get_datasource_manager()
        conn = mgr.connect(source_id)

        if not table:
            tables = conn.list_tables()
            if not tables: return json.dumps({"error": "未找到表"})
            schema = conn.get_schema()
            table = max(tables, key=lambda t: schema[t]["row_count"])

        result = conn.query(f"SELECT * FROM {table} LIMIT 500")
        if "error" in result:
            return json.dumps({"error": result["error"]})

        df = pd.DataFrame(result["rows"], columns=result["columns"])
        return json.dumps(_profile_df(df, max_cols), ensure_ascii=False, default=str)
    except Exception as e:
        logger.warning(f"[profile] 失败: {e}")
        return json.dumps({"error": f"profile 失败: {e}"}, ensure_ascii=False)


def _quick_stats(df: pd.DataFrame) -> dict:
    num_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    stats = {}
    for col in num_cols[:10]:
        s = df[col].dropna()
        if len(s) > 0:
            stats[col] = {
                "mean": round(float(s.mean()), 2),
                "min": round(float(s.min()), 2),
                "max": round(float(s.max()), 2),
            }
    return stats


def _profile_df(df: pd.DataFrame, max_cols: int) -> dict:
    result = {"row_count": len(df), "column_count": len(df.columns), "columns": []}
    for col in df.columns[:max_cols]:
        s = df[col]
        dtype = str(s.dtype)
        info = {"name": col, "type": dtype, "null_count": int(s.isna().sum()), "null_pct": round(float(s.isna().mean()) * 100, 1)}
        if np.issubdtype(s.dtype, np.number):
            clean = s.dropna()
            if len(clean) > 0:
                info.update({"mean": round(float(clean.mean()), 2), "min": round(float(clean.min()), 2),
                             "max": round(float(clean.max()), 2), "std": round(float(clean.std()), 2)})
        else:
            info["unique_count"] = int(s.nunique())
        result["columns"].append(info)
    return result


# 高影响列: 缺失/零值直接影响核心分析结论 → 质量分层时进 blockers
_HIGH_IMPACT_KEYWORDS = ("revenue", "sales", "amount", "gmv", "profit", "cost",
                         "营收", "销售额", "利润", "成本", "金额", "price", "单价", "客单价")


def _validate_df(df: pd.DataFrame) -> dict:
    total = len(df)
    issues = []
    for col in df.columns:
        null_pct = df[col].isna().mean()
        if null_pct > 0.1:
            tag = "高影响列 " if any(k in col.lower() for k in _HIGH_IMPACT_KEYWORDS) else ""
            issues.append(f"{tag}{col}: {null_pct:.0%} 缺失")
        if pd.api.types.is_numeric_dtype(df[col]):
            zero_pct = (df[col] == 0).mean()
            if zero_pct > 0.3:
                tag = "高影响列 " if any(k in col.lower() for k in _HIGH_IMPACT_KEYWORDS) else ""
                issues.append(f"{tag}{col}: {zero_pct:.0%} 零值")
    dups = int(df.duplicated().sum())
    if dups > 0:
        issues.append(f"{dups} 行重复")
    grade = "A" if not issues else "B" if len(issues) <= 2 else "C"
    return {"total_rows": total, "quality_grade": grade, "issues": issues, "duplicate_rows": dups}


@tool
def assess_quality(source_id: str, table: str = "") -> str:
    """数据质量评估: 缺失率、重复行、异常值、零值占比，输出等级和问题列表。
    返回结构化 JSON 含 quality_grade(A/B/C) + issues 列表。

    Args:
        source_id: 数据源ID
        table: 表名
    """
    try:
        mgr = get_datasource_manager()
        conn = mgr.connect(source_id)
        if not table:
            tables = conn.list_tables()
            schema = conn.get_schema()
            table = max(tables, key=lambda t: schema[t]["row_count"])
        # max_rows=None: 质量评估必须全量 — 500 行窗口算缺失率/零值占比会失真
        result = conn.query(f"SELECT * FROM {table}", max_rows=None)
        if "error" in result:
            return json.dumps({"error": result["error"]})
        df = pd.DataFrame(result["rows"], columns=result["columns"])
        return json.dumps(_validate_df(df), ensure_ascii=False, default=str)
    except Exception as e:
        logger.warning(f"[assess_quality] 失败: {e}")
        return json.dumps({"error": f"assess_quality 失败: {e}"}, ensure_ascii=False)


@tool
def sample_rows(source_id: str, table: str = "", n: int = 5) -> str:
    """从表中随机抽取 N 行样本数据，用于理解数据的真实内容和格式。

    Args:
        source_id: 数据源ID
        table: 表名 (留空自动选最大表)
        n: 取样行数 (默认5, 最大20)
    """
    try:
        import json
        mgr = get_datasource_manager()
        conn = mgr.connect(source_id)
        if not table:
            tables = conn.list_tables()
            if not tables:
                return json.dumps({"error": "未找到表"})
            schema = conn.get_schema()
            table = max(tables, key=lambda t: schema.get(t, {}).get("row_count", 0))
        n = min(n, 20)
        # MySQL 用 RAND(), SQLite/PostgreSQL 用 RANDOM()
        rand_func = "RAND()" if getattr(conn, "config", None) and conn.config.db_type == "mysql" else "RANDOM()"
        result = conn.query(f"SELECT * FROM {table} ORDER BY {rand_func} LIMIT {n}")
        if "error" in result:
            return json.dumps({"error": result["error"]})
        return json.dumps({
            "table": table, "sample_size": len(result.get("rows", [])),
            "columns": result.get("columns", []),
            "rows": result.get("rows", []),
        }, ensure_ascii=False, default=str)
    except Exception as e:
        logger.warning(f"[sample_rows] 失败: {e}")
        return json.dumps({"error": f"sample_rows 失败: {e}"}, ensure_ascii=False)


@tool
def date_range(source_id: str, table: str = "") -> str:
    """检测表中所有日期/时间列的时间跨度和粒度。

    自动发现日期列 → 计算 MIN/MAX → 推断粒度(年/月/日/时)。

    Args:
        source_id: 数据源ID
        table: 表名 (留空自动选最大表)
    """
    try:
        import json, re
        mgr = get_datasource_manager()
        conn = mgr.connect(source_id)
        if not table:
            tables = conn.list_tables()
            if not tables:
                return json.dumps({"error": "未找到表"})
            schema = conn.get_schema()
            table = max(tables, key=lambda t: schema.get(t, {}).get("row_count", 0))

        schema = conn.get_schema()
        cols = schema.get(table, {}).get("columns", [])
        date_cols = [c["name"] for c in cols if any(
            kw in c.get("type", "").upper() for kw in ("DATE", "TIME", "TIMESTAMP")
        ) or any(kw in c["name"].lower() for kw in ("date", "time", "day", "month", "year"))]

        if not date_cols:
            return json.dumps({"table": table, "date_columns": [], "note": "未检测到日期列"})

        results = []
        for col in date_cols:
            try:
                r = conn.query(f"SELECT MIN({col}) as min_val, MAX({col}) as max_val, COUNT(DISTINCT {col}) as distinct_count FROM {table}")
                if "error" in r or not r.get("rows"):
                    continue
                row = r["rows"][0]
                min_v, max_v, distinct = row.get("min_val"), row.get("max_val"), row.get("distinct_count", 0)
                # 推断粒度: 优先用 日期跨度(days) vs 去重数 的比值,
                # 比纯 distinct 阈值可靠 (一年多点的日数据 distinct>400 不再误判为 hour)
                grain = "day"
                try:
                    import pandas as _pd
                    d_min = _pd.to_datetime(str(min_v), errors="coerce")
                    d_max = _pd.to_datetime(str(max_v), errors="coerce")
                    if d_min is None or d_max is None or _pd.isna(d_min) or _pd.isna(d_max) or d_max < d_min:
                        raise ValueError("invalid date range")
                    span_days = max((d_max - d_min).days + 1, 1)
                    ratio = distinct / span_days
                    if ratio > 1.5:
                        grain = "hour"       # 每天多个时间点
                    elif ratio > 0.75:
                        grain = "day"        # 基本每天一个点
                    elif ratio > 0.3:
                        grain = "week"       # 约每周一个点
                    elif ratio > 0.05:
                        grain = "month"      # 约每月一个点
                    elif ratio > 0.01:
                        grain = "quarter"    # 约每季一个点
                    else:
                        grain = "year"
                except Exception:
                    # 日期解析失败 → 回退阈值法 (阈值放宽, 不再 400→hour)
                    if distinct <= 2:
                        grain = "year"
                    elif distinct <= 8:
                        grain = "quarter"
                    elif distinct <= 45:
                        grain = "month"
                    elif distinct <= 2000:
                        grain = "day"
                    else:
                        grain = "hour"
                results.append({"column": col, "min": str(min_v), "max": str(max_v), "distinct_values": distinct, "inferred_grain": grain})
            except Exception as e:
                results.append({"column": col, "error": str(e)})

        return json.dumps({"table": table, "date_columns": results}, ensure_ascii=False, default=str)
    except Exception as e:
        logger.warning(f"[date_range] 失败: {e}")
        return json.dumps({"error": f"date_range 失败: {e}"}, ensure_ascii=False)
