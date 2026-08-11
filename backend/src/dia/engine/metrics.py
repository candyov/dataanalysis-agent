"""L1 Metric Store -- 指标物化存储 + 快照引擎 v2

核心升级:
- 指标注册: 自动发现 KPI → 注册为可复用指标 → 建立依赖关系
- 物化缓存: 每日自动计算并缓存,查询时秒出
- 时间序列基线: 支持日/周/月/季任意窗口对比
"""

import json
import logging
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any

from dia.core.config import settings

logger = logging.getLogger(__name__)

DB_PATH = Path(settings.STORAGE_DIR) / "metric_store.db"


def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_metric_store():
    """初始化指标存储表"""
    conn = _get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            label TEXT,
            source_id TEXT,
            table_name TEXT,
            column_name TEXT,
            agg_func TEXT DEFAULT 'sum',
            is_derived INTEGER DEFAULT 0,
            formula TEXT,
            dependencies TEXT,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metric_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            metric_name TEXT NOT NULL,
            source_id TEXT,
            date TEXT NOT NULL,
            group_key TEXT DEFAULT '',
            value REAL,
            created_at TEXT,
            UNIQUE(metric_name, source_id, date, group_key)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS time_series_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            metric_name TEXT NOT NULL,
            source_id TEXT,
            window_type TEXT NOT NULL,
            window_start TEXT NOT NULL,
            group_key TEXT DEFAULT '',
            mean REAL,
            std REAL,
            min REAL,
            max REAL,
            last_value REAL,
            trend_slope REAL,
            calculated_at TEXT,
            UNIQUE(metric_name, source_id, window_type, window_start, group_key)
        )
    """)
    conn.commit()
    conn.close()


# ── 指标注册 ──

def register_metrics_from_glossary(glossary: dict, source_id: str, table_name: str = ""):
    """从语义层 glossary 自动注册指标"""
    conn = _get_db()
    for name, entry in glossary.items():
        if entry.get("role") != "metric":
            continue

        conn.execute(
            """INSERT OR REPLACE INTO metrics (name, label, source_id, table_name,
               column_name, agg_func, is_derived, formula, dependencies, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                name,
                entry.get("label", name),
                source_id,
                entry.get("table", table_name),
                name,
                entry.get("agg_default", "sum"),
                1 if entry.get("is_derived") else 0,
                entry.get("formula", ""),
                json.dumps(entry.get("dependencies", [])),
                datetime.now().isoformat(),
            ),
        )

    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
    conn.close()
    logger.info(f"指标注册完成: {count} 个指标")


def get_metrics(source_id: str = "") -> list[dict]:
    """获取已注册的指标列表"""
    conn = _get_db()
    if source_id:
        rows = conn.execute("SELECT * FROM metrics WHERE source_id = ?", (source_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM metrics").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── 快照引擎 v2 ──

def snapshot_metrics(metrics_data: dict[str, float], source_id: str, snap_date: str = ""):
    """保存单日指标快照"""
    if not snap_date:
        snap_date = date.today().isoformat()

    conn = _get_db()
    now = datetime.now().isoformat()

    for key, value in metrics_data.items():
        conn.execute(
            """INSERT OR REPLACE INTO metric_snapshots
               (metric_name, source_id, date, value, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (key, source_id, snap_date, value, now),
        )

    conn.commit()
    conn.close()


def get_time_series(
    metric_name: str,
    source_id: str = "",
    window_days: int = 30,
    group_key: str = "",
) -> dict[str, Any]:
    """
    获取指标时间序列和统计基线

    Returns:
        {dates: [...], values: [...], stats: {mean, std, min, max, trend_slope, last_value}}
    """
    conn = _get_db()
    rows = conn.execute(
        """SELECT date, value FROM metric_snapshots
           WHERE metric_name = ? AND source_id = ?
           ORDER BY date DESC LIMIT ?""",
        (metric_name, source_id, window_days),
    ).fetchall()
    conn.close()

    if not rows:
        return {"dates": [], "values": [], "stats": {}}

    rows = list(reversed(rows))  # 时间正序
    dates = [r["date"] for r in rows]
    values = [r["value"] for r in rows]

    import statistics
    stats = {}
    if len(values) >= 2:
        stats["mean"] = round(statistics.mean(values), 2)
        stats["std"] = round(statistics.stdev(values), 3) if len(values) > 2 else 0
        stats["min"] = round(min(values), 2)
        stats["max"] = round(max(values), 2)
        stats["last_value"] = values[-1]
        stats["prev_value"] = values[-2] if len(values) >= 2 else values[-1]
        # 简单趋势斜率
        n = len(values)
        if n >= 3:
            x_mean = (n - 1) / 2
            y_mean = stats["mean"]
            try:
                slope = sum((i - x_mean) * (values[i] - y_mean) for i in range(n)) / sum((i - x_mean) ** 2 for i in range(n))
                stats["trend_slope"] = round(slope, 4)
                stats["trend_direction"] = "up" if slope > 0 else "down" if slope < 0 else "flat"
            except ZeroDivisionError:
                stats["trend_slope"] = 0
                stats["trend_direction"] = "flat"

    return {"dates": dates, "values": values, "stats": stats}


def build_comprehensive_baselines(
    metric_name: str,
    source_id: str = "",
    group_key: str = "",
) -> dict[str, float]:
    """
    构建多层基线参照系(取代旧的 build_baselines)

    支持:
    - _prev: 昨日值
    - _wow: 上周同日
    - _mom: 上月同日
    - _mean_7d: 7日均值
    - _mean_30d: 30日均值
    - _std_30d: 30日标准差
    - _d1~_d7: 最近7天日值
    """
    conn = _get_db()
    today = date.today()

    def _get_value(d: str) -> float | None:
        row = conn.execute(
            """SELECT value FROM metric_snapshots
               WHERE metric_name=? AND source_id=? AND date=? AND group_key=?""",
            (metric_name, source_id, d, group_key),
        ).fetchone()
        return row["value"] if row else None

    baselines = {}

    # prev
    v = _get_value((today - timedelta(days=1)).isoformat())
    if v is not None:
        baselines["prev"] = v

    # wow
    v = _get_value((today - timedelta(days=7)).isoformat())
    if v is not None:
        baselines["wow"] = v

    # mom
    v = _get_value((today - timedelta(days=30)).isoformat())
    if v is not None:
        baselines["mom"] = v

    # 7d/30d mean/std
    rows_30 = conn.execute(
        """SELECT value FROM metric_snapshots
           WHERE metric_name=? AND source_id=? AND group_key=?
           ORDER BY date DESC LIMIT 30""",
        (metric_name, source_id, group_key),
    ).fetchall()

    if rows_30:
        vals_30 = [r["value"] for r in rows_30]
        import statistics
        baselines["mean_30d"] = round(statistics.mean(vals_30), 2)
        if len(vals_30) > 2:
            baselines["std_30d"] = round(statistics.stdev(vals_30), 3)

        vals_7 = vals_30[:7]
        if len(vals_7) >= 2:
            baselines["mean_7d"] = round(statistics.mean(vals_7), 2)

        # 最近7天日值
        for i, v in enumerate(vals_30[:7], 1):
            baselines[f"d{i}"] = v

    conn.close()
    return baselines
