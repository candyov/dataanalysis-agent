"""生成用于持续监控验证的零售数据库（SQLite）

生成 30 天日粒度聚合数据，注入异常：
- 第 25-30 天：华东利润持续下滑（趋势告警）
- 第 28 天：华东退货率飙升（阈值告警）
- 第 20 天：华南销售额突降（环比告警）
"""
import sqlite3
import random
from pathlib import Path
from datetime import date, timedelta

random.seed(42)

DB_PATH = Path(__file__).resolve().parent.parent / "storage" / "retail_monitor.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

regions = ["华东", "华南", "华北", "西南", "西北"]
categories = ["家居用品", "服装", "电子产品", "食品饮料", "办公用品"]


def generate_daily_data():
    """生成 30 天日粒度数据"""
    rows = []
    start_date = date(2025, 6, 1)

    for day_offset in range(30):
        d = start_date + timedelta(days=day_offset)
        date_str = d.isoformat()

        for region in regions:
            # 基础值
            region_factors = {"华东": 1.5, "华南": 1.2, "华北": 1.0, "西南": 0.7, "西北": 0.5}
            base_sales = random.randint(40000, 60000) * region_factors[region]
            base_profit = base_sales * random.uniform(0.12, 0.22)
            base_refund_rate = random.uniform(0.01, 0.04)

            # ── 注入异常 ──
            anomaly_label = ""

            # 华东第 25-30 天：利润持续下滑
            if region == "华东" and day_offset >= 24:
                decay = 1.0 - (day_offset - 23) * 0.06  # 每天下降 6%
                base_profit *= max(decay, 0.5)
                base_sales *= (1.0 - (day_offset - 23) * 0.02)
                anomaly_label = "profit_trend_drop"

            # 华东第 28 天：退货率飙升
            if region == "华东" and day_offset == 27:
                base_refund_rate = 0.12
                anomaly_label = "refund_spike"

            # 华南第 20 天：销售额突降
            if region == "华南" and day_offset == 19:
                base_sales *= 0.65
                base_profit *= 0.60
                anomaly_label = "revenue_drop"

            # 添加随机波动
            sales = round(base_sales * random.uniform(0.92, 1.08), 2)
            cost = round(sales * random.uniform(0.75, 0.88), 2)
            profit = round(sales - cost, 2)
            refund_rate = round(base_refund_rate * random.uniform(0.9, 1.1), 4)
            qty = random.randint(80, 500)
            customers = random.randint(40, 300)

            rows.append({
                "date": date_str,
                "region": region,
                "sales": sales,
                "profit": profit,
                "cost": cost,
                "refund_rate": refund_rate,
                "quantity": qty,
                "customers": customers,
                "anomaly_label": anomaly_label,
            })

    return rows


def create_database():
    """创建 SQLite 数据库并写入数据"""
    if DB_PATH.exists():
        DB_PATH.unlink()
        print(f"已删除旧数据库: {DB_PATH}")

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE daily_sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            region TEXT NOT NULL,
            sales REAL,
            profit REAL,
            cost REAL,
            refund_rate REAL,
            quantity INTEGER,
            customers INTEGER,
            anomaly_label TEXT
        )
    """)

    # 索引
    conn.execute("CREATE INDEX idx_date ON daily_sales(date)")
    conn.execute("CREATE INDEX idx_region ON daily_sales(region)")

    rows = generate_daily_data()
    for r in rows:
        conn.execute(
            """INSERT INTO daily_sales (date, region, sales, profit, cost, refund_rate, quantity, customers, anomaly_label)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (r["date"], r["region"], r["sales"], r["profit"], r["cost"],
             r["refund_rate"], r["quantity"], r["customers"], r["anomaly_label"]),
        )

    conn.commit()

    # 验证
    total = conn.execute("SELECT COUNT(*) FROM daily_sales").fetchone()[0]
    anomalies = conn.execute(
        "SELECT date, region, anomaly_label FROM daily_sales WHERE anomaly_label != ''"
    ).fetchall()

    print(f"数据库创建完成: {DB_PATH}")
    print(f"总行数: {total} (5 区域 × 30 天)")
    print(f"异常标注数: {len(anomalies)}")
    for a in anomalies:
        print(f"  {a[0]} {a[1]}: {a[2]}")
    print(f"\n表结构:")
    for col in conn.execute("PRAGMA table_info(daily_sales)"):
        print(f"  {col[1]} ({col[2]})")

    conn.close()
    return DB_PATH


if __name__ == "__main__":
    create_database()
