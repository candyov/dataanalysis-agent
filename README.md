# Data Intelligence Agent

多 Agent 驱动的 AI 数据分析平台：连接数据源 → 自动探查 → 假设驱动分析 → 图表可视化 → 生成管理层可读的分析报告。

## 架构

```
Supervisor (LLM 计划 + 动态路由)
  ├── Curator   — 数据探查: 结构扫描 / 质量评估 / 口径定义 / KPI 设计
  ├── Analyst   — 假设驱动分析: 统计检验 / 归因 / 预测 / 图表生成 (ECharts)
  └── Reporter  — 报告生成: 结论先行 / 业务语言 / 图表内联 / 数字校验
```

- **后端**: Python 3.11 + FastAPI + LangGraph + LangChain + SQLite
- **前端**: Vue 3 + TypeScript + Vite + ECharts
- **LLM**: DeepSeek (兼容任意 OpenAI 协议模型，支持多模型档案切换)

## 快速开始

### Docker Compose (推荐)

```bash
export LLM_API_KEY=sk-xxx
docker compose up --build
# 前端 http://localhost:80  后端 http://localhost:8010
```

### 本地开发

```bash
# 后端 (需 Python 3.11)
cd backend
pip install -r requirements.txt
export PYTHONPATH=src
uvicorn dia.main:app --host 0.0.0.0 --port 8010 --reload

# 前端
cd frontend
npm install
npm run dev   # http://localhost:5173
```

## 使用流程

1. 上传 CSV/Excel 或连接数据库（MySQL/PostgreSQL/SQLite）
2. 输入分析问题（如"分析该数据"、"各区域销售差异是否显著"）
3. 观察多 Agent 流水线：数据准备 → 分析引擎 → 报告生成
4. 查看报告：图表与分析内联，支持会话历史恢复

## 核心特性

| 特性 | 说明 |
|---|---|
| 多 Agent 协作 | Supervisor 计划驱动，Curator/Analyst/Reporter 职责分离，工具不重叠 |
| 图表内联 | 报告引用图表编号 → 确定性分段，图表夹在分析文本中间，会话恢复不丢图 |
| 数据质量把关 | Curator 探查评分 + 质量分层，阻塞问题阻断分析 |
| 数字校验 | 报告中的关键数字与数据源自动核对，防 LLM 编造 |
| 记忆机制 | glossary 语义层跨会话缓存（省探查成本）+ 历史结论注入（知道上次分析过什么） |
| 安全 | 数据源密码加密存储、默认只读连接、公式注入防护、XSS 清理 |
| 多模型 | 模型档案管理，运行时切换 provider/model |

## 测试

```bash
cd backend
PYTHONPATH=src python -m pytest tests/ -q --ignore=tests/e2e --ignore=tests/integration
```

## 项目结构

```
backend/
  src/dia/
    agents/       # Curator / Analyst / Reporter
    api/v1/       # chat / datasources / models / settings API
    core/         # LLM 工厂 / 事件类型 / 状态定义
    engine/       # Metric Store (KPI 物化 + 时序基线)
    graph/        # Supervisor 状态图
    infrastructure/  # 数据库 / 持久化 (sessions / glossary_cache) / 安全
    report/       # 蓝图生成 / 报告分段 / HTML 渲染
frontend/
  src/
    components/   # 聊天 / 报告 / 设置
    stores/       # Pinia (chat / pipeline)
    utils/        # markdown / 图表内联
```
