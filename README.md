# AI 数据分析平台

**自然语言驱动、多智能体协作的数据分析平台**——连接数据库，一句话提问，自动完成数据探查、统计检验、图表可视化，输出管理层可读的分析报告。

> 输入：「全面分析这份数据，为什么华东最强，有没有异常？」
> 输出：一份带统计证据和图表的完整商业分析报告。

## 演示效果

```
提问 ──▶ 数据准备(自动探查/质量评估) ──▶ 分析引擎(统计检验/归因/预测/异常检测)
      ──▶ 报告生成(四层分析链 + 图表内联) ──▶ 可直接给管理层看的报告
```

- **图表内联**：报告文字与图表按编号精确对应，图嵌在对应分析段落中，会话恢复不丢图
- **统计严谨**：每个对比结论强制统计检验(p 值/效应量/置信区间)，拒绝"凭感觉下结论"
- **防编造**：报告中的每个关键数字与数据源自动核对，LLM 想编也编不出来
- **跨会话记忆**：同一数据源二次提问自动复用探查结果，响应更快、结论可延续

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
npm run dev        # http://localhost:5173
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

1. **连接数据**：上传 CSV/Excel，或连接 MySQL / PostgreSQL / SQLite(默认只读)
2. **提问**：如「各区域销售差异是否显著」「预测下月销售额」「利润的关键驱动因素」
3. **看过程**：任务列表实时显示 数据准备 → 分析引擎 → 报告生成
4. **看报告**：核心结论 → 关键发现(现象/证据/归因/含义)→ 行动建议(🔴🟡🟢 优先级)→ 风险局限

## 核心特性

| 特性 | 说明 |
|---|---|
| 多智能体架构 | Supervisor 计划路由 + Curator 探查 / Analyst 分析 / Reporter 报告，职责分离 |
| 图表内联契约 | 图表编号 → 确定性分段 → 前端直接渲染，图永不丢失 |
| 统计检验 | t 检验 / ANOVA / 非参数 + BH 校正，对比结论必须有 p 值 |
| 数字校验闸门 | 报告数字与工具结果自动核对(5% 容差)，防 LLM 编造 |
| 记忆层 | glossary 跨会话缓存(7 天 TTL) + 历史结论注入(仅背景参考) |
| 生产安全 | 密码加密落盘、只读连接、CSV 公式注入防护、XSS 清理 |
| 模型可切换 | 多 provider/模型档案，运行时切换 |

## 技术栈

- **后端**：Python 3.11 · FastAPI · LangGraph · LangChain · pandas / scipy / scikit-learn
- **前端**：Vue 3 · TypeScript · Vite · Pinia · ECharts
- **LLM**：DeepSeek(默认，兼容任意 OpenAI 协议)

## 架构

```
Supervisor (LLM 计划 + 动态路由 + 缓存复用)
  ├── Curator   数据探查: 结构/质量/口径/KPI/蓝图
  ├── Analyst   ReAct: 探索/检验/归因/预测/异常/出图 (ECharts)
  └── Reporter  报告: 四层分析链/图表内联/数字校验/标注剥离
```

## 测试

```bash
cd backend
PYTHONPATH=src python -m pytest tests/ -q --ignore=tests/e2e --ignore=tests/integration
```

## 项目结构

```
backend/src/dia/
  agents/          # Curator / Analyst / Reporter
  api/v1/          # chat(SSE) / datasources / models / settings
  graph/           # Supervisor 状态机
  tools/           # explore / test_difference / forecast / detect / build_chart
  report/          # 蓝图 / 分段 / 渲染
  infrastructure/  # 数据库 / 持久化 / 安全
frontend/src/
  components/      # 聊天 / 报告 / 任务列表 / 设置
  stores/          # Pinia
  utils/           # markdown / 图表内联
```
