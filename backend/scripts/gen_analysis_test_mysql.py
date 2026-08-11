"""生成演示用 MySQL 数据库 analysis_test — 全面且能体现数据分析的零售订单数据

库: analysis_test
表: orders (订单明细事实表, 2024-01-01 ~ 2025-06-30, 18 个月)
    customers (客户维度表, 辅助)

注入的可分析信号 (供 Analyst 发现):
1. 区域梯度: 华东最强 (客单价×1.6), 东北垫底 (×0.45) → 差异 3.5 倍, test_difference 必显著
2. 季节大促: 6月(618)×1.9 / 11月(双11)×2.1 / 12月×1.35 / 2月春节×0.55
3. 品类毛利结构: 电子产品 8-13% 低毛利 vs 美妆个护 42-55% 高毛利 → attribution 可挖
4. 渠道: 线上单量过半, 线下客单价高 (+15%), 分销折扣 (-15%)
5. 异常事件 (detect 可发现):
   - 华东+电子产品 2025-03-01~15 退货率飙升 (3% → 40%)
   - 华南 2025-05-12~14 销售额突降 60% (缺货)
   - 东北 2025-04-01~06-30 利润持续下滑 (线性衰减至 60%)
6. 大单: 企业客户 3% 概率批量采购 (金额 ×3~8) → top_n 发现
7. 客户: 企业 18% 贡献更高客单价

连接参数走环境变量 (不硬编码密码):
    MYSQL_HOST (默认 127.0.0.1) / MYSQL_PORT (默认 13306)
    MYSQL_USER (默认 root) / MYSQL_PASSWORD (必填, 无默认)

用法: unset PYTHONPATH && MYSQL_PASSWORD=xxx D:/anaconda/envs/Data-Analysis_env/python.exe scripts/gen_analysis_test_mysql.py
"""
import os
import random
import pymysql
from datetime import date, timedelta

random.seed(42)

DB_NAME = "analysis_test"
MYSQL = dict(
    host=os.getenv("MYSQL_HOST", "127.0.0.1"),
    port=int(os.getenv("MYSQL_PORT", "13306")),
    user=os.getenv("MYSQL_USER", "root"),
    password=os.getenv("MYSQL_PASSWORD", ""),
    charset="utf8mb4",
)

# ── 业务配置 ──
REGIONS = ["华东", "华南", "华北", "华中", "西南", "西北", "东北"]
REGION_MULT = {"华东": 1.6, "华南": 1.3, "华北": 1.1, "华中": 0.9, "西南": 0.7, "西北": 0.5, "东北": 0.45}

# 品类: (单价区间, 毛利率区间, 单量区间, 订单频率权重)
CATEGORIES = {
    "电子产品": ((600, 5000), (0.08, 0.13), (1, 5), 0.18),
    "美妆个护": ((80, 800), (0.42, 0.55), (1, 6), 0.15),
    "服装":     ((120, 900), (0.30, 0.42), (1, 8), 0.17),
    "家居用品": ((150, 2500), (0.22, 0.35), (1, 4), 0.16),
    "运动户外": ((150, 1200), (0.25, 0.38), (1, 6), 0.15),
    "食品饮料": ((15, 120), (0.18, 0.28), (2, 20), 0.19),
}
CAT_NAMES = [c for c in CATEGORIES]
CAT_WEIGHTS = [v[3] for v in CATEGORIES.values()]

CHANNELS = [("线上", 0.52, 0.95, 0.05), ("线下", 0.30, 1.15, 0.02), ("分销", 0.18, 0.85, 0.03)]  # (名称, 权重, 客单价系数, 退货率)

def seasonal_mult(d: date) -> float:
    m = d.month
    if m == 6:      return 1.9   # 618 大促
    if m == 11:     return 2.1   # 双11
    if m == 12:     return 1.35  # 年末冲量
    if m == 2:      return 0.55  # 春节低谷
    if m in (9, 10): return 1.1  # 金九银十
    return 1.0

def region_anomaly_mult(region: str, d: date) -> float:
    """异常窗口: 华南 2025-05-12~14 销售突降 60%"""
    if region == "华南" and date(2025, 5, 12) <= d <= date(2025, 5, 14):
        return 0.40
    return 1.0

def refund_prob(region: str, category: str, channel_refund: float, d: date) -> float:
    """异常窗口: 华东+电子产品 2025-03-01~15 退货率飙升"""
    if region == "华东" and category == "电子产品" and date(2025, 3, 1) <= d <= date(2025, 3, 15):
        return 0.40
    return channel_refund

def profit_decay(region: str, d: date) -> float:
    """异常窗口: 东北 2025-04-01 起利润线性衰减至 60%"""
    if region == "东北" and d >= date(2025, 4, 1):
        days = (d - date(2025, 4, 1)).days
        return max(1.0 - days * 0.0045, 0.60)
    return 1.0

def generate_orders(start: date, end: date) -> list[tuple]:
    """生成订单行. 返回 executemany 需要的元组列表."""
    rows = []
    order_seq = 0
    day = start
    while day <= end:
        season = seasonal_mult(day)
        for region in REGIONS:
            # 每日单量: 区域权重 ^0.6 × 季节 × 基础 (×3.2 → 全库 ~1.7 万行, 每区域每天 3-10 单,
            # 保证 test_difference 样本量 + 异常窗口信号不被随机方差吃掉)
            n = int(random.uniform(0.5, 1.5) * season * (REGION_MULT[region] ** 0.6) * 3.2) + 1
            for _ in range(n):
                order_seq += 1
                # 品类
                category = random.choices(CAT_NAMES, weights=CAT_WEIGHTS, k=1)[0]
                price_range, margin_range, qty_range, _ = CATEGORIES[category]
                # 渠道
                channel, ch_w, ch_mult, ch_refund = random.choices(
                    CHANNELS, weights=[c[1] for c in CHANNELS], k=1)[0]
                # 客户类型
                customer_type = "企业" if random.random() < 0.18 else "个人"
                customer_id = random.randint(1, 200) if customer_type == "企业" else random.randint(201, 1200)

                quantity = random.randint(*qty_range)
                unit_price = random.uniform(*price_range) * REGION_MULT[region] * ch_mult
                sales = quantity * unit_price

                # 企业大单: 3% 概率批量采购
                if customer_type == "企业" and random.random() < 0.03:
                    sales *= random.uniform(3, 8)
                    quantity = max(quantity, int(quantity * random.uniform(2, 5)))

                # 异常窗口系数
                sales *= region_anomaly_mult(region, day)
                # 利润: 品类毛利率 × 东北衰减窗口
                margin = random.uniform(*margin_range) * profit_decay(region, day)
                cost = sales * (1 - margin)
                profit = sales - cost

                refund = 1 if random.random() < refund_prob(region, category, ch_refund, day) else 0

                rows.append((
                    f"ORD-{day.strftime('%Y%m%d')}-{order_seq:05d}",
                    day.isoformat(), region, category, channel, customer_type,
                    customer_id, quantity, round(unit_price, 2),
                    round(sales, 2), round(cost, 2), round(profit, 2), refund,
                ))
        day += timedelta(days=1)
    return rows


def generate_customers() -> list[tuple]:
    """客户维度表: customer_id, 注册日期, 等级, 城市"""
    cities = {"华东": ["上海", "杭州", "南京"], "华南": ["广州", "深圳"], "华北": ["北京", "天津"],
              "华中": ["武汉", "长沙"], "西南": ["成都", "重庆"], "西北": ["西安"], "东北": ["沈阳", "大连"]}
    rows = []
    for cid in range(1, 1201):
        region = random.choices(REGIONS, weights=[REGION_MULT[r] for r in REGIONS], k=1)[0]
        city = random.choice(cities[region])
        reg_date = date(2023, 1, 1) + timedelta(days=random.randint(0, 900))
        level = random.choices(["普通", "银卡", "金卡", "钻石"], weights=[0.55, 0.25, 0.15, 0.05], k=1)[0]
        rows.append((cid, reg_date.isoformat(), level, city, region))
    return rows


def main():
    conn = pymysql.connect(**MYSQL)
    cur = conn.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS {DB_NAME}")
    cur.execute(f"CREATE DATABASE {DB_NAME} DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    conn.select_db(DB_NAME)

    cur.execute("""
        CREATE TABLE orders (
            order_id      VARCHAR(24) PRIMARY KEY,
            order_date    DATE        NOT NULL,
            region        VARCHAR(8)  NOT NULL,
            category      VARCHAR(16) NOT NULL,
            channel       VARCHAR(8)  NOT NULL,
            customer_type VARCHAR(4)  NOT NULL,
            customer_id   INT         NOT NULL,
            quantity      INT         NOT NULL,
            unit_price    DECIMAL(10,2) NOT NULL,
            sales         DECIMAL(12,2) NOT NULL,
            cost          DECIMAL(12,2) NOT NULL,
            profit        DECIMAL(12,2) NOT NULL,
            refund_flag   TINYINT     NOT NULL DEFAULT 0,
            INDEX idx_date (order_date),
            INDEX idx_region (region),
            INDEX idx_category (category),
            INDEX idx_channel (channel)
        ) ENGINE=InnoDB
    """)
    cur.execute("""
        CREATE TABLE customers (
            customer_id   INT PRIMARY KEY,
            reg_date      DATE NOT NULL,
            level         VARCHAR(8) NOT NULL,
            city          VARCHAR(16) NOT NULL,
            region        VARCHAR(8) NOT NULL
        ) ENGINE=InnoDB
    """)

    orders = generate_orders(date(2024, 1, 1), date(2025, 6, 30))
    customers = generate_customers()

    # 分批插入
    BATCH = 500
    for i in range(0, len(orders), BATCH):
        cur.executemany(
            "INSERT INTO orders (order_id, order_date, region, category, channel, customer_type,"
            " customer_id, quantity, unit_price, sales, cost, profit, refund_flag)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            orders[i:i + BATCH])
    cur.executemany(
        "INSERT INTO customers (customer_id, reg_date, level, city, region) VALUES (%s,%s,%s,%s,%s)",
        customers)
    conn.commit()
    print(f"orders: {len(orders)} 行, customers: {len(customers)} 行, 已提交")

    # ── 验证: 信号是否可发现 ──
    def q(sql):
        cur.execute(sql)
        return cur.fetchall()

    print("\n[1] 区域销售额 (期望 华东≈东北 3.5 倍):")
    for r in q("SELECT region, COUNT(*), ROUND(SUM(sales)), ROUND(AVG(sales)) FROM orders GROUP BY region ORDER BY SUM(sales) DESC"):
        print("   ", r)
    print("\n[2] 月度销售额 Top6 (期望 2024-11 / 2025-06 大促峰值):")
    for r in q("SELECT DATE_FORMAT(order_date,'%Y-%m') m, ROUND(SUM(sales)) s FROM orders GROUP BY m ORDER BY s DESC LIMIT 6"):
        print("   ", r)
    print("\n[3] 品类毛利率 (期望 美妆最高 电子最低):")
    for r in q("SELECT category, ROUND(SUM(profit)/SUM(sales)*100,1) FROM orders GROUP BY category ORDER BY 2 DESC"):
        print("   ", r)
    print("\n[4] 异常校验:")
    for label, sql in [
        ("华东电子 3月1-15日退货率 (期望 ≈40%)",
         "SELECT ROUND(AVG(refund_flag)*100,1) FROM orders WHERE region='华东' AND category='电子产品' AND order_date BETWEEN '2025-03-01' AND '2025-03-15'"),
        ("华南 5月12-14 销售额 (期望 ≈前后 3 天的 40%)",
         "SELECT ROUND(SUM(sales)) FROM orders WHERE region='华南' AND order_date BETWEEN '2025-05-12' AND '2025-05-14'"),
        ("华南 5月9-11 销售额 (对照窗口)",
         "SELECT ROUND(SUM(sales)) FROM orders WHERE region='华南' AND order_date BETWEEN '2025-05-09' AND '2025-05-11'"),
        ("东北 2025Q2 利润率 (期望 < 2024 同期)",
         "SELECT ROUND(SUM(profit)/SUM(sales)*100,1) FROM orders WHERE region='东北' AND order_date>='2025-04-01'"),
        ("东北 2024Q2 利润率 (对照)",
         "SELECT ROUND(SUM(profit)/SUM(sales)*100,1) FROM orders WHERE region='东北' AND order_date BETWEEN '2024-04-01' AND '2024-06-30'"),
    ]:
        print(f"   {label}: {q(sql)[0][0]}")
    print("\n[5] 大单 Top3 (期望企业客户):")
    for r in q("SELECT order_id, region, category, customer_type, sales FROM orders ORDER BY sales DESC LIMIT 3"):
        print("   ", r)

    conn.close()
    print("\n完成: 数据库 analysis_test 就绪")


if __name__ == "__main__":
    main()
