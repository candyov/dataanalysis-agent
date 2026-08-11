"""Supervisor P0 三项测试: 意图驱动调度 / 失败分类重试 / 降级保留部分结果"""

import asyncio

import pytest

from dia.graph.supervisor import (
    _apply_intent_policy,
    _max_retries_for,
    _normalize_plan,
)


def _plan(*agents, intent="general"):
    steps = [{"agent": a, "goal": f"{a} 任务"} for a in agents]
    return _normalize_plan({"intent": intent, "summary": "", "steps": steps})


# ═══════════════════════════════════════════════
# P0-1 意图驱动调度
# ═══════════════════════════════════════════════

class TestIntentPolicy:
    def test_quick_intent_removes_curator_with_semantic(self):
        """quick + 有会话内语义层 → 移除 curator"""
        plan = _plan("curator", "analyst", "reporter", intent="quick")
        out = _apply_intent_policy(plan, source_id="src1",
                                   shared={"curator_report": {"source_id": "src1"}})
        assert [s["agent"] for s in out["steps"]] == ["analyst", "reporter"]
        assert out["intent"] == "quick"

    def test_quick_keyword_in_request_removes_curator(self):
        plan = _plan("curator", "analyst", "reporter", intent="general")
        out = _apply_intent_policy(plan, "简单看一下数据", source_id="src1",
                                   shared={"curator_report": {"source_id": "src1"}})
        assert [s["agent"] for s in out["steps"]] == ["analyst", "reporter"]

    def test_quick_no_semantic_keeps_curator(self):
        """quick 但无语义层 → 保留 curator (避免 Analyst 工具失败 revisit)"""
        plan = _plan("curator", "analyst", "reporter", intent="quick")
        out = _apply_intent_policy(plan, source_id="src1", shared={})
        assert [s["agent"] for s in out["steps"]] == ["curator", "analyst", "reporter"]

    def test_no_quick_keeps_curator(self):
        plan = _plan("curator", "analyst", "reporter", intent="general")
        out = _apply_intent_policy(plan, "全面分析该数据", source_id="src1",
                                   shared={"curator_report": {"source_id": "src1"}})
        assert [s["agent"] for s in out["steps"]] == ["curator", "analyst", "reporter"]

    def test_attribution_adds_goal_to_analyst(self):
        plan = _plan("analyst", "reporter", intent="attribution")
        out = _apply_intent_policy(plan)
        analyst_goal = next(s["goal"] for s in out["steps"] if s["agent"] == "analyst")
        assert "归因" in analyst_goal

    def test_attribution_keyword_in_request(self):
        plan = _plan("analyst", "reporter", intent="general")
        out = _apply_intent_policy(plan, "为什么6月营收下滑")
        analyst_goal = next(s["goal"] for s in out["steps"] if s["agent"] == "analyst")
        assert "归因" in analyst_goal

    def test_attribution_does_not_duplicate_goal(self):
        plan = _plan("analyst", "reporter", intent="attribution")
        plan["steps"][0]["goal"] = "已含归因字样的目标"
        out = _apply_intent_policy(plan)
        analyst_goal = next(s["goal"] for s in out["steps"] if s["agent"] == "analyst")
        assert analyst_goal.count("归因") == 1


# ═══════════════════════════════════════════════
# P0-2 失败分类重试
# ═══════════════════════════════════════════════

class TestMaxRetriesFor:
    def test_auth_fails_fast(self):
        assert _max_retries_for("llm_auth") == 1

    def test_sql_gives_medium_retry(self):
        assert _max_retries_for("tool_sql") == 2

    def test_timeout_gets_full_retry(self):
        assert _max_retries_for("llm_timeout") == 3

    def test_generic_gets_full_retry(self):
        assert _max_retries_for("llm_generic") == 3

    def test_no_error_keeps_default(self):
        assert _max_retries_for("") == 3


# ═══════════════════════════════════════════════
# P0-3 降级保留部分结果
# ═══════════════════════════════════════════════

class TestReporterDegradeFallback:
    def test_degraded_empty_report_uses_summary(self):
        from dia.agents.reporter import ReporterAgent
        agent = ReporterAgent(name="reporter")
        out = agent.build_output(
            {"analysis": {"summary": "华东最强, 占比24%"}, "shared_context": {}},
            {"report": "", "degraded": True, "degraded_reason": "引用校验失败"},
        )
        report = out["reporter"]["report"]
        assert "华东最强" in report
        assert "简版" in report
        assert out["shared_context"]["degraded"] is True

    def test_degraded_short_report_uses_summary(self):
        from dia.agents.reporter import ReporterAgent
        agent = ReporterAgent(name="reporter")
        out = agent.build_output(
            {"analysis": {"summary": "结论摘要"}, "shared_context": {}},
            {"report": "太短", "degraded": True},
        )
        assert "结论摘要" in out["reporter"]["report"]

    def test_normal_report_untouched(self):
        from dia.agents.reporter import ReporterAgent
        agent = ReporterAgent(name="reporter")
        out = agent.build_output(
            {"analysis": {"summary": "摘要"}, "shared_context": {}},
            {"report": "完整报告内容..." * 40, "degraded": False},  # >600 字符
        )
        assert "完整报告内容" in out["reporter"]["report"]
        assert "简版" not in out["reporter"]["report"]
        assert "degraded" not in out["shared_context"]

    def test_degraded_no_summary_keeps_empty(self):
        from dia.agents.reporter import ReporterAgent
        agent = ReporterAgent(name="reporter")
        out = agent.build_output(
            {"analysis": {}, "shared_context": {}},
            {"report": "", "degraded": True},
        )
        assert out["reporter"]["report"] == ""
