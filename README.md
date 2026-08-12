# AI 数据分析平台

**自然语言驱动、多智能体协作的数据分析平台**——连接数据库，一句话提问，自动完成数据探查、统计检验、图表可视化，输出可读的分析报告。

> 输入：「全面分析这份数据，为什么华东最强，有没有异常？」
> 输出：一份带统计证据和图表的完整分析报告。

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.11 · FastAPI · LangGraph · LangChain · pandas / scipy / scikit-learn |
| 前端 | Vue 3 · TypeScript · Vite · Pinia · ECharts |
| 数据库 | MySQL(连接默认只读)+ CSV / Excel 上传 |
| LLM | DeepSeek-v4-pro(默认，兼容任意 OpenAI 协议模型，支持多模型档案运行时切换) |

## 项目结构

```
backend/src/dia/
  main.py            # FastAPI 入口
  api/
    v1/              # HTTP 路由: chat(SSE) / datasources / models / settings 
    schemas/         # 请求/响应契约 (ChatRequest 等)
    middleware/      # 鉴权 / 链路追踪
  application/       # 服务层: chat_service (SSE 编排用例)
  agents/            # Curator 探查 / Analyst 分析 / Reporter 报告
  graph/             # Supervisor 状态机 (计划路由 / 缓存复用)
  tools/             # explore / test_difference / forecast / detect / build_chart
  core/              # 状态定义 / 事件 / LLM 基础
  report/            # 蓝图 / 分段 / 渲染
  infrastructure/    # database / persistence / security / observability
  scripts/           # 演示数据库生成脚本
backend/tests/       # 单元 + smoke + e2e (181 tests)
frontend/src/
  components/        # 聊天 / 报告 / 任务列表 / 设置
  stores/            # Pinia (chatStore / pipelineStore)
  utils/             # markdown / 报告分段 / 图表内联
```

**分层依赖**: `api → application → agents/graph/tools → infrastructure/core`(单向，服务层可独立测试)

## 快速开始

### 本地开发

```bash
# 1. 后端 (Python 3.11)
cd backend
pip install -r requirements.txt
# 创建 backend/.env: 填入 LLM_API_KEY=sk-xxx (模型默认 DeepSeek, 可换任意 OpenAI 协议模型)
export PYTHONPATH=src
uvicorn dia.main:app --host 0.0.0.0 --port 8010

# 2. 前端
cd frontend
npm install
npm run dev        # http://localhost:5173 (proxy → 8010)
```

### Docker Compose

```bash
export LLM_API_KEY=sk-xxx
docker compose up --build
# 前端 http://localhost:80  后端 http://localhost:8010
```

### 演示数据(可选)

```bash
cd backend
export MYSQL_PASSWORD=你的密码
python scripts/gen_analysis_test_mysql.py   # 生成 1.5 万行零售演示库 (analysis_test)
```

## 使用流程

1. **连接数据**：上传 CSV/Excel，或连接 MySQL / PostgreSQL / SQLite
2. **提问**：如「各区域销售差异是否显著」「预测下月销售额」「利润的关键驱动因素」
3. **看过程**：任务列表实时显示 数据准备 → 分析引擎 → 报告生成
4. **看报告**：核心结论 → 关键发现(现象/证据/归因/含义)→ 行动建议(🔴🟡🟢 优先级)→ 风险局限

## 核心特性

| 特性 | 说明 |
|---|---|
| 多智能体架构 | Supervisor 计划路由 + Curator 探查 / Analyst 分析 / Reporter 报告，职责分离 |
| 图表内联契约 | 图表编号 → 确定性分段 → 前端直接渲染，图永不丢失、全部内嵌正文 |
| 统计严谨 | t 检验 / ANOVA / 非参数 + BH 校正，对比结论必须有 p 值；归因模型 + 效应量 |
| 防编造三道闸门 | 报告数字核对(5% 容差) + 图表数值同源校验 + 发现/图表引用强制 |
| 异常检测 | 日尖峰 + 周窗口突降 + 同比漂移 + 分组检测，结果限流防噪声刷屏 |
| 预测分级表达 | 可预测性评分 + 三档情景(乐观/基准/保守)，低可预测性给保守预算建议 |
| 记忆层 | glossary 跨会话缓存(7 天 TTL) + 历史结论注入(仅背景参考) |
| 生产安全 | 密码加密落盘、只读连接、CSV 公式注入防护、XSS 清理 |

## 测试

```bash
cd backend
PYTHONPATH=src python -m pytest tests/ -q --ignore=tests/e2e --ignore=tests/integration
```
