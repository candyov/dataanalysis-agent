"""图数字闸门单测: LLM 直出图的大额数值必须同源于工具结果 (防编造)."""
import json
import pytest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from dia.agents.analyst import _verify_chart_numbers, serial_tool_node


# ── 单元: _verify_chart_numbers ──

def test_real_value_passes():
    """真实工具结果中的值 (元) ↔ 图数据 (万元) → 通过."""
    tool_results = ['{"rows": [{"region": "华东", "sales": 14547994.36}, {"region": "华南", "sales": 11051100.0}]}']
    data = {"series": [{"data": [1454.8, 1105.11]}]}  # 万元
    assert _verify_chart_numbers(data, tool_results) == []


def test_fabricated_value_rejected():
    """编造的大额数值无来源 → 拒绝."""
    tool_results = ['{"rows": [{"sales": 14547994.36}]}']
    data = {"series": [{"data": [9999999.0]}]}
    bad = _verify_chart_numbers(data, tool_results)
    assert "9999999.0" in bad


def test_small_derived_values_skipped():
    """占比/率/单价等小值 (派生值) 不拦."""
    tool_results = ['{"rows": [{"sales": 14547994.36}]}']
    data = {"series": [{"data": [{"name": "华东", "value": 29.4}, {"name": "华南", "value": 22.3}]}]}
    assert _verify_chart_numbers(data, tool_results) == []


def test_wan_conversion():
    """万元 → 元换算匹配 (2399.12万 ↔ 23991243.56)."""
    tool_results = ['{"rows": [{"category": "电子", "sales": 23991243.56}]}']
    data = {"series": [{"data": [2399.12]}]}
    assert _verify_chart_numbers(data, tool_results) == []


def test_heatmap_values():
    """heatmap 三元组 [x, y, 万元值] → 值需同源."""
    tool_results = ['{"cross": [[0, 0, 6895300.0]]}']
    data = {"series": [{"data": [[0, 0, 689.53]]}]}
    assert _verify_chart_numbers(data, tool_results) == []


def test_empty_or_small_data_passes():
    """空数据 / 无大额数值 → 通过."""
    assert _verify_chart_numbers({}, []) == []
    assert _verify_chart_numbers({"data": [1.0, 50.0, 0.5]}, []) == []


# ── 集成: serial_tool_node 拦截 (项目模式: asyncio.run 同步包装) ──
import asyncio


def test_serial_tool_node_blocks_fabricated_chart():
    """LLM 发 build_chart 且 data 含编造大额数值 → 不执行工具, 返回错误 ToolMessage."""
    msgs = [
        HumanMessage(content="分析"),
        ToolMessage(content=json.dumps({"rows": [{"sales": 1000000.0}]}), tool_call_id="t1", name="explore"),
        AIMessage(content="", tool_calls=[{
            "name": "build_chart", "id": "c1", "type": "tool_call",
            "args": {"title": "销售额", "chart_type": "bar",
                     "data": {"series": [{"data": [99999999.0]}]}},
        }]),
    ]
    out = asyncio.run(serial_tool_node({"messages": msgs}))
    new_msgs = out["messages"]  # 节点只返回新增 (add_messages 追加)
    assert len(new_msgs) == 1
    assert isinstance(new_msgs[0], ToolMessage)
    assert "疑似编造" in new_msgs[0].content


def test_serial_tool_node_passes_real_chart():
    """data 数值同源 → 正常执行 build_chart."""
    msgs = [
        HumanMessage(content="分析"),
        ToolMessage(content=json.dumps({"rows": [{"sales": 1000000.0}]}), tool_call_id="t1", name="explore"),
        AIMessage(content="", tool_calls=[{
            "name": "build_chart", "id": "c2", "type": "tool_call",
            "args": {"title": "销售额", "chart_type": "bar",
                     "data": {"series": [{"data": [100.0]}]}},  # 小值, 不校验
        }]),
    ]
    out = asyncio.run(serial_tool_node({"messages": msgs}))
    new_msgs = out["messages"]  # 节点只返回新增 (add_messages 追加)
    assert len(new_msgs) == 1
    assert isinstance(new_msgs[0], ToolMessage)
    assert "疑似编造" not in new_msgs[0].content
