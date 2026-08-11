"""P2 人机协同测试: 低置信度触发 confirm / confirmation 恢复 / 事件序列"""

import asyncio

import pytest

from dia.graph.supervisor import CONFIRM_THRESHOLD, supervisor_node


def _state(**overrides):
    state = {
        "user_request": "分析该数据",
        "source_id": "src1",
        "shared_context": {},
        "plan": {"intent": "general", "summary": "", "steps": [
            {"agent": "analyst", "goal": "分析"},
            {"agent": "reporter", "goal": "报告"},
        ]},
        "plan_step": 0,
        "iteration_count": 0,
        "next": "",
        "messages": [],
        "analysis": {},
    }
    state.update(overrides)
    return state


class TestLowConfidenceConfirm:
    def test_low_confidence_triggers_confirm(self):
        """analyst done + confidence < 阈值 → next=confirm"""
        state = _state(
            plan_step=0,
            analysis={"done": True, "confidence": 0.3, "summary": "部分结果"},
        )
        out = asyncio.run(supervisor_node(state))
        assert out["next"] == "confirm"
        shared = out["shared_context"]
        assert "confirm_reason" in shared
        assert shared["confirm_confidence"] == pytest.approx(0.3)

    def test_high_confidence_normal_progress(self):
        """analyst done + confidence 高 → 正常推进 reporter"""
        state = _state(
            plan_step=0,
            analysis={"done": True, "confidence": 0.85, "summary": "稳定结论"},
        )
        out = asyncio.run(supervisor_node(state))
        assert out["next"] == "reporter"
        assert out["plan_step"] == 1

    def test_threshold_boundary_ok(self):
        """恰好等于阈值 → 不触发 confirm (正常推进)"""
        state = _state(
            plan_step=0,
            analysis={"done": True, "confidence": CONFIRM_THRESHOLD, "summary": "x"},
        )
        out = asyncio.run(supervisor_node(state))
        assert out["next"] == "reporter"

    def test_no_confirm_when_suggest_next_present(self):
        """有动态路由建议 (如 revisit) → 不触发 confirm, 走建议"""
        state = _state(
            plan_step=0,
            analysis={"done": True, "confidence": 0.2},
            shared_context={"suggest_next": {"action": "revisit", "target": "curator"}},
        )
        out = asyncio.run(supervisor_node(state))
        assert out["next"] == "curator"  # 走 revisit, 不是 confirm

    def test_non_analyst_no_confirm(self):
        """curator done + 低分 → 不触发 confirm (只有 analyst 检查)"""
        state = _state(
            plan_step=0,
            curator={"done": True, "confidence": 0.1},
            analysis={},
        )
        # plan_step=0 是 analyst... 构造 curator 为当前步骤
        state["plan"] = {"intent": "general", "summary": "", "steps": [
            {"agent": "curator", "goal": "探查"},
            {"agent": "analyst", "goal": "分析"},
        ]}
        out = asyncio.run(supervisor_node(state))
        assert out["next"] != "confirm"


class TestConfirmEvent:
    def test_confirm_event_serializes(self):
        """ConfirmRequiredEvent SSE 序列化含 reason/confidence/options"""
        from dia.core.events import ConfirmRequiredEvent
        evt = ConfirmRequiredEvent(reason="置信度低", confidence=0.31)
        sse = evt.to_sse()
        assert '"type": "confirm_required"' in sse
        assert '"confidence": 0.31' in sse
        assert "置信度低" in sse
