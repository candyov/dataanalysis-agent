"""forecast 可预测性评分 + 三档情景 + reporter 分级表述规则 — 单测."""
import json
import sqlite3

import numpy as np
import pandas as pd
import pytest

from dia.infrastructure.database.manager import get_datasource_manager
from dia.infrastructure.database.base import DataSourceConfig
from dia.tools.analysis import forecast
from dia.agents.reporter import REPORTER_PROMPT


@pytest.fixture()
def src(tmp_path):
    """两个序列: 强趋势 (可预测性高) + 纯噪声 (可预测性低)."""
    db = str(tmp_path / "fc.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (d TEXT, signal REAL, noise REAL)")
    dates = pd.date_range("2024-01-01", "2025-06-30", freq="D")
    rows = []
    for i, d in enumerate(dates):
        rows.append((d.strftime("%Y-%m-%d"), round(100 + i * 0.5, 2), round(np.random.normal(100, 50), 2)))
    conn.executemany("INSERT INTO t VALUES (?,?,?)", rows)
    conn.commit()
    conn.close()
    mgr = get_datasource_manager()
    mgr.add_source(DataSourceConfig(id="fc_src", name="t", db_type="sqlite", database=db))
    yield "fc_src"
    mgr.remove_source("fc_src")


def _fc(source_id, metric):
    return json.loads(forecast.invoke({
        "metric": metric, "source_id": source_id, "date_col": "d", "table": "t"}))


def test_forecast_has_new_fields(src):
    """输出含 predictability / predictability_level / scenarios 三档."""
    r = _fc(src, "signal")
    assert "predictability" in r and "predictability_level" in r and "scenarios" in r
    assert r["predictability_level"] in ("高", "中", "低")
    assert 0 <= r["predictability"] <= 100
    s0 = r["scenarios"][0]
    assert set(s0) == {"period", "optimistic", "baseline", "conservative"}
    # 三档大小关系: 乐观 ≥ 基准 ≥ 保守
    assert s0["optimistic"] >= s0["baseline"] >= s0["conservative"]


def test_strong_trend_high_predictability(src):
    """强线性趋势 → 可预测性高."""
    r = _fc(src, "signal")
    assert r["predictability"] >= 70, f"强趋势应高可预测: {r['predictability']}"


def test_noise_low_predictability(src):
    """纯噪声 → 可预测性低 (如实表达不确定性)."""
    r = _fc(src, "noise")
    assert r["predictability"] < 60, f"噪声序列应低可预测: {r['predictability']}"


def test_reporter_prompt_has_tiered_expression_rule():
    """Reporter prompt 必须含预测表述分级规则 (低可预测性 → 保守预算表述)."""
    assert "预测表述分级" in REPORTER_PROMPT
    assert "保守情景" in REPORTER_PROMPT
    assert "低可预测性" in REPORTER_PROMPT
