"""detect 工具增强单测: 滑动窗口突降/突升 + 同比漂移 + 分组检测 + 限流 + 0/1 比率列"""
import json
import sqlite3
import numpy as np
import pandas as pd
import pytest

from dia.infrastructure.database.manager import get_datasource_manager, DataSourceManager
from dia.infrastructure.database.base import DataSourceConfig
from dia.tools.analysis import detect


@pytest.fixture()
def src(tmp_path):
    """构造含注入异常的时序 SQLite 数据源.

    - 区域 A: 利润 2025Q2 同比下滑 (漂移)
    - 区域 B: 2025-05 中旬连续 3 天销售突降 (周窗口 dip)
    - 全部区域: refund_flag 2025-03 比率飙升 (窗口 spike)
    """
    db = str(tmp_path / "detect_test.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (order_date TEXT, region TEXT, sales REAL, profit REAL, refund_flag INT)")
    rows = []
    dates = pd.date_range("2024-01-01", "2025-06-30", freq="D")
    for d in dates:
        for region, base in (("A", 100.0), ("B", 80.0)):
            sales = base + np.sin(d.dayofyear / 365 * 2 * np.pi) * 10 + np.random.normal(0, 3)
            profit = sales * 0.2
            refund = 0
            # B 区 2025-05-12~14 销售突降 60%
            if region == "B" and d == pd.Timestamp("2025-05-12"):
                sales *= 0.4
            if region == "B" and d == pd.Timestamp("2025-05-13"):
                sales *= 0.4
            if region == "B" and d == pd.Timestamp("2025-05-14"):
                sales *= 0.4
            # A 区 2025Q2 利润下滑 30% (同比)
            if region == "A" and d >= pd.Timestamp("2025-04-01"):
                profit *= 0.7
            # 2025-03 全区域退货率飙升
            if pd.Timestamp("2025-03-01") <= d <= pd.Timestamp("2025-03-15"):
                refund = 1 if np.random.rand() < 0.4 else 0
            else:
                refund = 1 if np.random.rand() < 0.03 else 0
            rows.append((d.strftime("%Y-%m-%d"), region, round(sales, 2), round(profit, 2), refund))
    conn.executemany("INSERT INTO t VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    mgr = get_datasource_manager()
    mgr.add_source(DataSourceConfig(id="detect_src", name="t", db_type="sqlite", database=db))
    yield "detect_src"
    mgr.remove_source("detect_src")


def _invoke(source_id, **kw):
    return json.loads(detect.invoke({
        "source_id": source_id, "date_col": "order_date",
        "table": "t", **kw}))


def test_window_dip_detected(src):
    """B 区 5/12-14 连续突降 → 周粒度 dip_window 命中 (分组检测)."""
    r = _invoke(src, metrics=["sales"], group_by="region")
    hits = [a for a in r["anomalies"]
            if a.get("group") == "B" and a["level"] == "dip_window"]
    assert hits, f"B 区 dip_window 未命中: {r['anomalies'][:5]}"
    # 命中窗口应在 2025 年 5 月
    assert any("2025-05" in (h.get("week") or h.get("date") or "") for h in hits)


def test_drift_yoy_detected(src):
    """A 区 2025Q2 利润同比下滑 → 月粒度同比 drift down (分组检测)."""
    r = _invoke(src, metrics=["profit"], group_by="region")
    hits = [a for a in r["anomalies"]
            if a.get("group") == "A" and a["level"] == "drift" and a.get("direction") == "down"]
    assert hits, f"A 区同比下滑未命中: {r['anomalies'][:5]}"
    assert hits[0].get("compare") == "同比"


def test_ratio_col_spike(src):
    """refund_flag 0/1 列 2025-03 比率飙升 → 窗口 spike 命中 (均值聚合)."""
    r = _invoke(src, metrics=["refund_flag"])
    hits = [a for a in r["anomalies"] if a["level"] == "spike_window"]
    assert hits, "退货率飙升未命中"
    assert any("2025-03" in (h.get("week") or h.get("date") or "") for h in hits)


def test_no_group_means_all_flat_metrics_quiet(src):
    """无异常指标 (quantity 恒为 1) → 不产生无意义异常 (限流+克制)."""
    r = _invoke(src, metrics=["sales"], group_by="region")
    assert r["count"] <= 40, f"限流失效: {r['count']} 条"
    # 每个 (metric, level) 分组 ≤ 5
    from collections import Counter
    buckets = Counter((a["metric"], a["level"]) for a in r["anomalies"])
    assert all(v <= 5 for v in buckets.values()), f"单桶超 5: {buckets}"


def test_group_field_present(src):
    """分组检测的异常带 group 字段."""
    r = _invoke(src, metrics=["sales"], group_by="region")
    assert r["anomalies"], "应检出异常"
    assert all("group" in a for a in r["anomalies"])
