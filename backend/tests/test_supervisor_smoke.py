"""Smoke tests for Supervisor refactor — plan normalization + iteration semantics."""
import asyncio
import sys
sys.path.insert(0, 'src')


# ══ _normalize_plan: 去重 + 排序 + 过滤 ══

def test_normalize_plan_dedup_and_order():
    from dia.graph.supervisor import _normalize_plan

    # 乱序 + 重复 + 未知 agent
    plan = _normalize_plan({"steps": [
        {"agent": "reporter", "goal": "报告"},
        {"agent": "analyst", "goal": "分析"},
        {"agent": "analyst", "goal": "再分析"},  # 重复 → 去掉
        {"agent": "curator", "goal": "探查"},
        {"agent": "hacker", "goal": "注入"},    # 未知 → 过滤
    ]})
    agents = [s["agent"] for s in plan["steps"]]
    assert agents == ["curator", "analyst", "reporter"], agents


def test_normalize_plan_empty_fallback():
    from dia.graph.supervisor import _normalize_plan

    plan = _normalize_plan({"steps": []})
    agents = [s["agent"] for s in plan["steps"]]
    assert agents == ["analyst", "reporter"], agents

    plan = _normalize_plan({})
    assert plan["steps"][0]["agent"] == "analyst"


# ══ supervisor_node: iteration 语义 (推进不消耗, 重试消耗) ══

def _mk_state(plan, step, iteration, done_agents=()):
    state = {
        "user_request": "分析数据",
        "plan": plan,
        "plan_step": step,
        "iteration_count": iteration,
        "curator": {"done": "curator" in done_agents},
        "analysis": {"done": "analyst" in done_agents},
        "reporter": {"done": "reporter" in done_agents},
        "shared_context": {},
    }
    return state


def test_4step_plan_completes_without_iteration_exhaustion():
    """4 步 plan (curator→analyst→analyst→reporter 归一化后 3 步) 正常推进不耗 iteration"""
    from dia.graph.supervisor import supervisor_node, _normalize_plan

    plan = _normalize_plan({"steps": [
        {"agent": "curator", "goal": "探查"},
        {"agent": "analyst", "goal": "分析"},
        {"agent": "reporter", "goal": "报告"},
    ]})

    async def run():
        # 首次: 生成计划 (用已有 plan 跳过 LLM)
        out = await supervisor_node(_mk_state(plan, 0, 0))
        return out

    out = asyncio.run(run())
    assert out["next"] == "curator"
    assert out["iteration_count"] == 1  # 首次执行消耗 1


def test_advance_does_not_consume_iteration():
    """agent 完成后推进下一步, iteration 重置为 0 → 重试预算 per-agent"""
    from dia.graph.supervisor import supervisor_node, _normalize_plan

    plan = _normalize_plan({"steps": [
        {"agent": "curator", "goal": "探查"},
        {"agent": "analyst", "goal": "分析"},
        {"agent": "reporter", "goal": "报告"},
    ]})

    async def run():
        # curator 已完成 → 推进到 analyst, iteration 重置为 0
        # (per-agent 预算: 每个 agent 独立拥有 MAX_ITERATIONS 次机会,
        #  前面 agent 的失败计数不传递给下一个)
        out = await supervisor_node(_mk_state(plan, 0, 1, done_agents=("curator",)))
        return out

    out = asyncio.run(run())
    assert out["next"] == "analyst"
    assert out["plan_step"] == 1
    assert out["iteration_count"] == 0  # 重置, 不携带上一个 agent 的计数


def test_retry_consumes_iteration():
    """agent 未完成 → 重试, iteration +1"""
    from dia.graph.supervisor import supervisor_node, _normalize_plan

    plan = _normalize_plan({"steps": [
        {"agent": "analyst", "goal": "分析"},
        {"agent": "reporter", "goal": "报告"},
    ]})

    async def run():
        # analyst 未完成 (第一次跑后回来, iteration=1)
        out = await supervisor_node(_mk_state(plan, 0, 1, done_agents=()))
        return out

    out = asyncio.run(run())
    assert out["next"] == "analyst"  # 重新路由
    assert out["iteration_count"] == 2


def test_max_iterations_degraded():
    """agent 反复失败 → degraded finish, 不截断正常步骤"""
    from dia.graph.supervisor import supervisor_node, _normalize_plan
    from dia.core.config import settings

    plan = _normalize_plan({"steps": [
        {"agent": "analyst", "goal": "分析"},
        {"agent": "reporter", "goal": "报告"},
    ]})

    async def run():
        # analyst 已失败 MAX_ITERATIONS 次 → degraded
        out = await supervisor_node(_mk_state(plan, 0, settings.MAX_ITERATIONS, done_agents=()))
        return out

    out = asyncio.run(run())
    assert out["next"] == "finish"
    assert out["shared_context"]["degraded"] is True
    assert out["shared_context"]["degraded_agent"] == "analyst"


# ══ supervisor_router ══

def test_router_valid_and_fallback():
    from dia.graph.supervisor import supervisor_router

    assert supervisor_router({"next": "curator"}) == "curator"
    assert supervisor_router({"next": "analyst"}) == "analyst"
    assert supervisor_router({"next": "finish"}) == "__end__"
    assert supervisor_router({"next": "hacker"}) == "analyst"  # 未知 → fallback


# ══ 动态路由: suggest_next 消费 ══

def test_suggest_skip_reporter():
    """analyst 建议 skip reporter → 跳过最后一步直接 finish"""
    from dia.graph.supervisor import supervisor_node, _normalize_plan

    plan = _normalize_plan({"steps": [
        {"agent": "analyst", "goal": "分析"},
        {"agent": "reporter", "goal": "报告"},
    ]})
    state = _mk_state(plan, 0, 1, done_agents=("analyst",))
    state["shared_context"] = {"suggest_next": {"action": "skip", "target": "reporter", "reason": "用户只要图表"}}

    out = asyncio.run(supervisor_node(state))
    assert out["next"] == "finish"  # reporter 被跳过
    assert out["shared_context"]["suggest_next"] is None  # 建议已消费


def test_suggest_revisit_curator():
    """analyst 建议 revisit curator → 重置下游 done + 重路由"""
    from dia.graph.supervisor import supervisor_node, _normalize_plan

    plan = _normalize_plan({"steps": [
        {"agent": "curator", "goal": "探查"},
        {"agent": "analyst", "goal": "分析"},
        {"agent": "reporter", "goal": "报告"},
    ]})
    state = _mk_state(plan, 1, 2, done_agents=("curator", "analyst"))
    state["shared_context"] = {"suggest_next": {"action": "revisit", "target": "curator", "reason": "工具大面积失败"}}

    out = asyncio.run(supervisor_node(state))
    assert out["next"] == "curator"
    assert out["plan_step"] == 0  # 回退到 curator 步骤
    assert out["curator"]["done"] is False  # 重置
    assert out["analysis"]["done"] is False  # 下游也重置
    assert out["shared_context"]["suggest_next"] is None


def test_invalid_suggest_cleared_on_normal_advance():
    """无效建议 (target 不在 plan) → 正常推进 + 建议清除 (防跨轮残留)"""
    from dia.graph.supervisor import supervisor_node, _normalize_plan

    plan = _normalize_plan({"steps": [
        {"agent": "analyst", "goal": "分析"},
        {"agent": "reporter", "goal": "报告"},
    ]})
    state = _mk_state(plan, 0, 1, done_agents=("analyst",))
    # skip target="curator" 不在 plan 中 → 无效建议
    state["shared_context"] = {"suggest_next": {"action": "skip", "target": "curator", "reason": "无效"}}

    out = asyncio.run(supervisor_node(state))
    assert out["next"] == "reporter"  # 正常推进
    assert out["shared_context"]["suggest_next"] is None  # 已清除


def test_reuse_curator_report_skips_curator():
    """多轮追问: 已有 curator_report → plan 自动移除 curator"""
    from dia.graph.supervisor import _apply_reuse, _normalize_plan

    plan = _normalize_plan({"steps": [
        {"agent": "curator", "goal": "探查"},
        {"agent": "analyst", "goal": "分析"},
        {"agent": "reporter", "goal": "报告"},
    ]})
    # 已有探查报告 → curator 移除
    out, _ = _apply_reuse(plan, {"curator_report": {"confirm": {}}})
    assert [s["agent"] for s in out["steps"]] == ["analyst", "reporter"]

    # 无探查报告 → 保持原样
    plan2 = _normalize_plan({"steps": [
        {"agent": "curator", "goal": "探查"},
        {"agent": "analyst", "goal": "分析"},
    ]})
    out2, _ = _apply_reuse(plan2, {})
    assert [s["agent"] for s in out2["steps"]] == ["curator", "analyst"]


def test_apply_reuse_cross_session_glossary_cache():
    """跨会话复用: 会话内无 curator_report, 但 glossary 缓存命中 → 跳过 curator + 注入 shared"""
    import pytest
    from dia.graph.supervisor import _apply_reuse, _normalize_plan
    from dia.infrastructure.persistence import glossary_cache as gc

    gc.clear_glossary_cache()
    try:
        gc.save_glossary_cache(
            "src1",
            {"region": {"name": "region", "role": "dimension"}},
            ["total_sales"],
            {"confirm": {"caliber": "sum(sales)"}, "source_id": "src1",
             "quality": {"grade": "A"}},
        )
        plan = _normalize_plan({"steps": [
            {"agent": "curator", "goal": "探查"},
            {"agent": "analyst", "goal": "分析"},
        ]})
        shared = {}
        out, shared = _apply_reuse(plan, shared, "src1")
        # curator 被移除
        assert [s["agent"] for s in out["steps"]] == ["analyst"]
        # 缓存内容注入 shared_context
        assert shared["glossary"]["region"]["role"] == "dimension"
        assert shared["registered_kpis"] == ["total_sales"]
        assert shared["curator_report"]["source_id"] == "src1"
        assert shared["quality_report"]["grade"] == "A"
    finally:
        gc.clear_glossary_cache()


def test_apply_reuse_stale_cache_reprobes():
    """陈旧缓存 (超 TTL) → 不复用, 保留 curator 步骤"""
    import time
    from dia.graph.supervisor import _apply_reuse, _normalize_plan
    from dia.infrastructure.persistence import glossary_cache as gc

    gc.clear_glossary_cache()
    try:
        gc.save_glossary_cache("src1", {"a": 1}, [], {})
        # 直接改库回拨 updated_at 模拟过期
        import sqlite3
        from dia.infrastructure.persistence.glossary_cache import DB_PATH
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("UPDATE glossary_cache SET updated_at = ? WHERE source_id = 'src1'",
                     (time.time() - gc.GLOSSARY_CACHE_TTL - 100,))
        conn.commit()
        conn.close()

        plan = _normalize_plan({"steps": [
            {"agent": "curator", "goal": "探查"},
            {"agent": "analyst", "goal": "分析"},
        ]})
        out, shared = _apply_reuse(plan, {}, "src1")
        assert [s["agent"] for s in out["steps"]] == ["curator", "analyst"]
        assert "glossary" not in shared  # 陈旧缓存不注入
    finally:
        gc.clear_glossary_cache()


def test_supervisor_injects_analysis_history():
    """首次规划: 同数据源有历史结论 → 注入 shared_context.analysis_history"""
    from dia.graph.supervisor import supervisor_node
    from dia.infrastructure.persistence import glossary_cache as gc

    gc.clear_history()
    try:
        gc.append_history("src_h", "华东是头部市场, 占比23.6%", "分析区域表现")
        state = {
            "user_request": "分析该数据",
            "source_id": "src_h",
            "shared_context": {},
            "plan": {},
            "plan_step": 0,
            "iteration_count": 0,
            "next": "",
            "messages": [],
        }
        out = asyncio.run(supervisor_node(state))
        shared = out.get("shared_context", {})
        hist = shared.get("analysis_history", [])
        assert len(hist) == 1
        assert hist[0]["question"] == "分析区域表现"
        assert "华东" in hist[0]["conclusion"]
    finally:
        gc.clear_history()


# ══ _suggest_next 单元测试 ══

def test_suggest_next_skip_report_keywords():
    from dia.agents.analyst import _suggest_next

    # 用户只要图表 → skip reporter
    suggest = _suggest_next({"user_request": "只要图表"}, [], [])
    assert suggest == {"action": "skip", "target": "reporter", "reason": "用户只要图表/简要结论, 无需完整报告"}

    # 正常分析请求 → 无建议
    assert _suggest_next({"user_request": "分析华东营收下滑原因"}, [], []) is None


def test_suggest_next_revisit_on_tool_failure():
    from langchain_core.messages import ToolMessage
    from dia.agents.analyst import _suggest_next

    # 60%+ 工具失败 → revisit curator
    tool_msgs = [
        ToolMessage(content='{"error": "列不存在"}', name="drill_down", tool_call_id="t1"),
        ToolMessage(content='{"error": "数据不足"}', name="forecast", tool_call_id="t2"),
        ToolMessage(content='{"groups": [{"a": 1}]}', name="drill_down", tool_call_id="t3"),
    ]
    suggest = _suggest_next({"user_request": "分析数据"}, [], tool_msgs)
    assert suggest["action"] == "revisit"
    assert suggest["target"] == "curator"

    # 少量失败 → 无建议
    tool_msgs = [
        ToolMessage(content='{"error": "列不存在"}', name="drill_down", tool_call_id="t1"),
        ToolMessage(content='{"groups": [{"a": 1}]}', name="drill_down", tool_call_id="t2"),
    ]
    assert _suggest_next({"user_request": "分析数据"}, [], tool_msgs) is None
