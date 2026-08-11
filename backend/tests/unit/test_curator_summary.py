"""Curator 消息瘦身测试: 前端只收 3 行摘要, 不推 CONFIRM 全文"""

import pytest

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage


class TestCuratorDisplayMsgs:
    def test_display_msgs_only_summary(self):
        """build_output 的 display_msgs 只含摘要消息, 不含 CONFIRM 全文"""
        from dia.agents.data_curator import DataCuratorAgent

        agent = DataCuratorAgent(name="curator")
        # 构造: 完整 CONFIRM 全文 + 工具消息 + 最终总结
        full_report = """[CONFIRM]
用户问题: 分析该数据
我的理解: 全面理解数据结构
口径定义: 营收=SUM(sales)
数据能回答:
- 区域营收排名
- 时间趋势
数据无法回答:
- 客户复购率 (无客户ID)

[DATA_OVERVIEW]
表清单: daily_sales 6570行
时间跨度: 2025-07-01 ~ 2026-06-30
采样发现:
- region 含华东/华南

[QUALITY]
综合等级: A
阻塞性问题:
- 无

[KPI_TREE]
基础指标:
  sales|销售额|sum|原始列|✓

[ROADMAP]
第一轮 (整体画像):
- 计算总量KPI
"""
        msgs = [
            SystemMessage(content="system"),
            HumanMessage(content="分析"),
            ToolMessage(content='{"rows": []}', name="explore", tool_call_id="tc1"),
            AIMessage(content="正在探查数据"),
            AIMessage(content=full_report),
        ]
        # 需要足够的 state 上下文 (glossary 等由 fallback 补)
        state = {
            "source_id": "src_test",
            "user_request": "分析该数据",
            "messages": [HumanMessage(content="分析")],
            "shared_context": {},
            "data": {},
        }
        # build_output 需要 result dict
        result = {"messages": msgs}
        out = agent.build_output(state, result)
        display = out["messages"]

        # 只有 1 条摘要消息 (不再有 CONFIRM 全文)
        assert len(display) == 1, f"期望 1 条摘要, 实际 {len(display)}"
        text = display[0].content
        assert "CONFIRM" not in text, "摘要不应含探查全文"
        assert "DATA_OVERVIEW" not in text
        assert "数据探查完成" in text, "摘要应含质量等级"
        assert "质量" in text

    def test_summary_contains_key_info(self):
        """摘要包含口径/能回答/无法回答 三要素"""
        from dia.agents.data_curator import DataCuratorAgent
        agent = DataCuratorAgent(name="curator")

        full_report = """[CONFIRM]
用户问题: 分析该数据
我的理解: 全面理解数据结构
口径定义: 营收=SUM(sales)
数据能回答:
- 区域营收排名
数据无法回答:
- 客户复购率

[QUALITY]
综合等级: A
"""
        msgs = [AIMessage(content=full_report)]
        state = {"source_id": "src_test", "user_request": "分析", "messages": [],
                 "shared_context": {}, "data": {}}
        out = agent.build_output(state, {"messages": msgs})
        text = out["messages"][0].content
        assert "口径" in text
        assert "能回答" in text
        assert "无法回答" in text
