# AI 数据分析平台

自然语言驱动的多智能体数据分析平台：连接数据源 → 自动探查 → 统计检验 → 图表可视化 → 生成管理层可读的分析报告。

> 一句话提问，自动出分析报告。

## 架构

```
Supervisor (LLM 计划 + 动态路由 + 缓存复用)
  ├── Curator   — 数据探查: 结构扫描 / 质量评估 / 口径定义 / KPI 设计 / 报告蓝图
  ├── Analyst   — ReAct 分析: 探索 / 统计检验 (t·ANOVA·非参·BH 校正) / 归因 / 预测 / 异常检测 / 出图
  └── Reporter  — 报告生成: 结论先行 / 四层分析链 / 图表内联 / 数字校验 / 内部标注剥离
```

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.11 · FastAPI · LangGraph · LangChain · pandas/scipy/scikit-learn |
| 前端 | Vue 3 · TypeScript · Vite · Pinia · ECharts |
| LLM | DeepSeek(默认), 兼容任意 OpenAI 协议模型, 支持模型档案运行时切换 |
| 数据库 | MySQL / PostgreSQL / SQLite, 另支持 CSV/Excel 上传(自动入库) |

## 快速开始(本地开发)

### 1. 后端

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 配置 (backend/.env)
cp /dev/null .env   # 或手动创建, 填入:
#   LLM_API_KEY=sk-xxx
#   LLM_MODEL=deepseek-v4-pro
#   LLM_API_BASE=https://api.deepseek.com/v1
#   APP_API_KEY=            # 单用户鉴权, 空 = 本地开发不鉴权

export PYTHONPATH=src
uvicorn dia.main:app --host 0.0.0.0 --port 8010
# 或直接运行 start.bat (Windows)
```

> 端口固定 **8010**，前端 Vite 已配置代理指向它。

### 2. 前端

```bash
cd frontend
npm install
npm run dev     # http://localhost:5173
```

### 3. Docker Compose

```bash
export LLM_API_KEY=sk-xxx
docker compose up --build
# 前端 http://localhost:80  后端 http://localhost:8010
# 注意: compose 只起前后端, 数据源需自行上传文件或连接外部数据库
```

## 准备演示数据(可选)

仓库提供 MySQL 演示数据生成脚本(零售订单 1.5 万行, 18 个月, 内置区域梯度/大促季节/品类毛利差异/异常事件等可分析信号):

```bash
cd backend
export MYSQL_PASSWORD=你的MySQL密码      # 连接 127.0.0.1:13306 (或改 MYSQL_HOST/PORT)
D:/anaconda/envs/Data-Analysis_env/python.exe scripts/gen_analysis_test_mysql.py
```

生成后在前端「数据源管理」中注册该 MySQL 库(`analysis_test`), 即可提问。

## 使用流程

1. 上传 CSV/Excel 或连接数据库(MySQL/PostgreSQL/SQLite, 默认只读)
2. 选择数据源, 输入分析问题(如「全面分析这份数据」「各区域销售差异是否显著」「为什么华东最强」)
3. 观察多 Agent 流水线: 数据准备 → 分析引擎 → 报告生成(任务列表实时显示)
4. 查看报告: 文字与分析图表内联(见图: 图N), 支持全屏报告视图与会话历史恢复

## 核心特性

| 特性 | 说明 |
|---|---|
| 多 Agent 协作 | Supervisor 计划驱动 + 状态机路由; Curator/Analyst/Reporter 职责分离, 工具不重叠 |
| 统计严谨 | 对比结论强制统计检验(test_difference, 自动选方法 + BH 校正), gap_fill 代码补缺 |
| 图表内联契约 | 图表编号 → 后端确定性分段 → 前端直接渲染, 图永不丢失、会话恢复不丢图 |
| 数字校验闸门 | 报告中的关键数字与工具执行结果自动核对(5% 容差), 防 LLM 编造/转录错误 |
| 记忆机制 | glossary 语义层跨会话缓存(命中跳过探查, 7 天 TTL)+ 历史结论注入(仅背景参考) |
| 安全 | 数据源密码加密落盘、默认只读连接(双层防御)、CSV 公式注入防护、XSS 清理 |
| 模型档案 | 多 provider/模型管理, 运行时切换, 不影响进行中分析 |
| 管理化报告 | 四层分析链(现象→证据→归因→含义)、emoji 优先级、行动建议后移、内部标注剥离 |

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `LLM_API_KEY` | 空 | LLM API Key(必填) |
| `LLM_MODEL` | `deepseek-v4-pro` | 默认模型 |
| `LLM_API_BASE` | `https://api.deepseek.com/v1` | OpenAI 协议兼容端点 |
| `APP_API_KEY` | 空 | 单用户 API 鉴权(生产建议配置) |
| `SESSION_TTL` | 7 天 | 会话保留时长 |

## 测试

```bash
cd backend
PYTHONPATH=src python -m pytest tests/ -q --ignore=tests/e2e --ignore=tests/integration
# 164 tests; E2E 需 MySQL 容器 (MYSQL_PASSWORD 环境变量)
```

## 项目结构

```
backend/
  scripts/          # 演示数据生成 (gen_analysis_test_mysql.py / gen_monitor_db.py)
  src/dia/
    agents/         # Curator / Analyst / Reporter
    api/v1/         # chat(SSE) / datasources / models / settings
    core/           # LLM 工厂 / 事件类型 / 状态定义 / 配置
    engine/         # Metric Store (KPI 物化 + 时序基线)
    graph/          # Supervisor 状态机 (意图路由 / 重试分档 / 降级 / 缓存复用)
    infrastructure/ # 数据库 (sqlite/mysql/postgres) / 持久化 (sessions/glossary_cache) / 安全
    report/         # 蓝图生成 / 报告分段(segments) / HTML 渲染
    tools/          # explore / test_difference / attribution / forecast / detect / build_chart
frontend/
  src/
    components/     # 聊天 / 报告(全屏视图) / 任务列表 / 设置
    stores/         # Pinia (chat / pipeline)
    utils/          # markdown 渲染 / 图表内联
```
