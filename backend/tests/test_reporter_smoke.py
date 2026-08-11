"""Smoke tests for Reporter refactor — 6-module report, context assembly, suggestion trigger."""
import json
import sys
sys.path.insert(0, 'src')


def _state(user_request="分析华东营收下滑", **overrides):
    state = {
        "user_request": user_request,
        "source_id": "test_src",
        "analysis": {
            "summary": "[强] 华东营收同比下滑 8.3% (p=0.003)",
            "structured_data": {"findings": [
                {"claim": "[强] 华东vs华北差异显著 (p=0.003)", "evidence": "hypothesis_test", "confidence": 0.85},
                {"claim": "[弱] 女装在华东占比最高", "evidence": "drill_down", "confidence": 0.7},
            ]},
            "chart_data": [{"chart_type": "bar", "title": "华东vs华北对比", "categories": ["A", "B"], "data": [1, 2]}],
        },
        "shared_context": {
            "data_quality_score": 80,
            "quality_report": {
                "grade": "B",
                "blockers": ["revenue列15%缺失 → 总营收可能低报"],
                "degraded": ["email列30%缺失 → 不影响营收分析"],
            },
            "curator_report": {
                "confirm": {
                    "caliber": "营收=SUM(revenue)",
                    "understanding": "分析华东营收下滑原因",
                    "cannot_answer": ["客户流失分析: 无客户ID"],
                },
                "quality": {
                    "grade": "B",
                    "blockers": ["revenue列15%缺失 → 总营收可能低报"],
                },
            },
            "charts": [{"chart_type": "bar", "title": "华东vs华北对比", "categories": ["A", "B"], "data": [1, 2]}],
        },
        "messages": [],
    }
    state.update(overrides)
    return state


# ══ extract_input: context 组装 ══

def test_extract_input_assembles_6_modules():
    from dia.agents.reporter import ReporterAgent

    agent = ReporterAgent(name="reporter")
    out = agent.extract_input(_state())

    ctx = out["context"]
    # 用户问题
    assert "华东营收下滑" in ctx
    # CONFIRM 口径透传
    assert "营收=SUM(revenue)" in ctx
    assert "客户流失分析" in ctx
    # QUALITY 分层透传
    assert "revenue列15%缺失" in ctx
    assert "阻塞问题" in ctx
    # 分析结论 + findings 分级保留
    assert "华东vs华北差异显著 (p=0.003)" in ctx
    assert "[强]" not in ctx  # 分级标记已剥离 (业务语言替代)
    # 图表数据
    assert "华东vs华北对比" in ctx
    # 建议触发字段
    assert "recommendation_note" in out


def test_extract_input_suggestion_trigger():
    """建议默认必含 (决策者视角); 用户明确排除才不含"""
    from dia.agents.reporter import ReporterAgent, SUGGEST_KEYWORDS

    agent = ReporterAgent(name="reporter")

    # 普通请求 (无关键词) → 默认必含建议
    out = agent.extract_input(_state(user_request="分析一下华东营收"))
    assert "必须包含行动建议" in out["recommendation_note"]

    # 含"建议"关键词 → 必含
    out = agent.extract_input(_state(user_request="营收下滑, 有什么建议?"))
    assert "必须包含行动建议" in out["recommendation_note"]

    # 明确排除 → 不含
    out = agent.extract_input(_state(user_request="只要分析, 不用给建议"))
    assert "不含建议" in out["recommendation_note"]

    # 关键词配置保留 (兼容)
    assert SUGGEST_KEYWORDS


def test_extract_input_handles_empty_curator():
    from dia.agents.reporter import ReporterAgent

    agent = ReporterAgent(name="reporter")
    # 无 curator_report / 无 analysis → 不崩, 基本 context 仍在
    out = agent.extract_input(_state(
        shared_context={"data_quality_score": 90},
        analysis={"summary": "", "structured_data": {}},
    ))
    ctx = out["context"]
    assert "华东营收下滑" in ctx  # 用户问题始终在
    assert "数据质量" in ctx
    assert out["_tool_results"] == []


# ══ 工具结果: Analyst 结构化传递 (不再扫全链) ══

def test_structured_tool_results_from_analyst():
    """Reporter 从 analysis.structured_data.tool_results 读 (Analyst 阶段结果), 不扫外层全链"""
    from dia.agents.reporter import ReporterAgent

    agent = ReporterAgent(name="reporter")
    state = _state(analysis={
        "summary": "[强] 华东营收同比下滑",
        "structured_data": {
            "findings": [],
            "tool_results": [
                {"tool": "explore", "data": {"metric": "revenue", "groups": []}},
                {"tool": "test_difference", "data": {"pairs": [{"significant": True}]}},
            ],
        },
        "chart_data": [],
    })
    out = agent.extract_input(state)
    ctx = out["context"]
    assert "explore" in ctx and "test_difference" in ctx
    assert out["_tool_results"][0]["tool"] == "explore"


def test_structured_tool_results_empty():
    """无结构化工具结果 → 空列表, 不崩"""
    from dia.agents.reporter import ReporterAgent

    agent = ReporterAgent(name="reporter")
    out = agent.extract_input(_state(analysis={"summary": "", "structured_data": {}}))
    assert out["_tool_results"] == []


def test_extract_input_blueprint_chapters():
    """report_blueprint 章节注入报告 context (分析维度章节, quality 不注入)"""
    from dia.agents.reporter import ReporterAgent

    agent = ReporterAgent(name="reporter")
    state = _state(shared_context={
        "data_quality_score": 80,
        "report_blueprint": {
            "chapters": [
                {"type": "group_compare", "title": "各区域营收对比分析", "description": "按区域拆解核心指标"},
                {"type": "time_series", "title": "时间趋势分析", "description": "按月展示变化趋势"},
                {"type": "quality", "title": "数据质量评估", "description": "完整性校验"},
            ],
        },
    })
    out = agent.extract_input(state)
    ctx = out["context"]
    assert "报告必须覆盖的章节" in ctx
    assert "各区域营收对比分析" in ctx
    assert "时间趋势分析" in ctx
    assert "数据质量评估" not in ctx  # quality 章节不注入 (非分析维度)


# ══ Reporter 修复 (v2) — 引用强制 / 截断优先级 / 图表实况 ══

def test_extract_input_findings_numbered_and_priority():
    """findings 编号化 [Fk] + 工具结果按报告引用重要性排序 (test_difference 在 explore 前)"""
    from dia.agents.reporter import ReporterAgent

    agent = ReporterAgent(name="reporter")
    state = _state(analysis={
        "summary": "[强] 华东营收同比下滑",
        "structured_data": {
            "findings": [{"claim": "[强] 差异显著 (p=0.003)", "evidence": "test_difference", "confidence": 0.9}],
            "tool_results": [
                {"tool": "explore", "data": {"groups": [{"group": "A", "sum": 100}] * 50}},
                {"tool": "test_difference", "data": {"pairs": [{"significant": True, "p_value": 0.003}]}},
            ],
        },
        "chart_data": [],
    })
    out = agent.extract_input(state)
    ctx = out["context"]
    assert "[F1] 差异显著 (p=0.003)" in ctx  # 编号化 + [强] 已剥离
    assert "引用规则" in ctx and "F1" in ctx
    assert ctx.index("[test_difference]") < ctx.index("[explore]")  # 统计验证优先
    assert out["findings"][0]["claim"].startswith("[强]")  # 原始 claim 保留 (仅展示层剥离)


def test_extract_input_chart_reality():
    """charts_generated=0 → 图表说明注入 (禁止'见下图'); 有图 → 无禁止提示"""
    from dia.agents.reporter import ReporterAgent

    agent = ReporterAgent(name="reporter")
    state = _state(analysis={
        "summary": "x", "charts_generated": 0,
        "structured_data": {"findings": [], "tool_results": []},
        "chart_data": [],
    })
    out = agent.extract_input(state)
    assert "未生成任何图表" in out["context"]
    assert "严禁写" in out["context"]

    state2 = _state(analysis={
        "summary": "x", "charts_generated": 2,
        "structured_data": {"findings": [], "tool_results": []},
        "chart_data": [{"chart_type": "bar", "title": "t", "categories": [], "data": []}],
    })
    out2 = agent.extract_input(state2)
    assert "未生成任何图表" not in out2["context"]


def test_generate_node_force_reference_retry():
    """引用强制: 报告未引用过半 findings → 带缺失列表重生成一次"""
    import asyncio
    from langchain_core.messages import AIMessage
    from dia.agents.reporter import ReporterAgent
    import dia.agents.reporter as rp

    class FakeLLM:
        def __init__(self):
            self.calls = 0
            self.retry_seen = False
        async def ainvoke(self, messages, config=None):
            self.calls += 1
            ctx = messages[1].content
            if "校验失败" in ctx:
                self.retry_seen = True
                return AIMessage(content=("[F1] 发现一. [F2] 发现二. [F3] 发现三. "
                                          "结论完整字数够六百字以上了." + "分析内容充实篇幅充足。" * 30))
            return AIMessage(content=("这是第一份报告正文内容写得很完整但没有引用任何发现编号" * 30))

    fake = FakeLLM()
    async def fake_get_llm(temperature=None):
        return fake
    orig = rp.get_llm
    rp.get_llm = fake_get_llm
    try:
        agent = ReporterAgent(name="reporter")
        out = asyncio.run(agent._generate_node({
            "context": "x", "recommendation_note": "",
            "findings": [{"claim": "a"}, {"claim": "b"}, {"claim": "c"}],
        }))
        assert fake.calls == 2, f"期望重生成一次, 实际 {fake.calls}"
        assert fake.retry_seen, "重试时未携带缺失列表"
        assert "[F1]" in out["report"] and "[F2]" in out["report"]
    finally:
        rp.get_llm = orig


def test_build_output_degraded_flag():
    """报告生成降级 → shared_context 写入 degraded (前端提示)"""
    from dia.agents.reporter import ReporterAgent

    agent = ReporterAgent(name="reporter")
    out = agent.build_output({}, {"report": "x", "messages": [],
                                  "degraded": True, "degraded_reason": "引用不足 (1/3)"})
    assert out["shared_context"]["degraded"] is True
    assert out["shared_context"]["degraded_agent"] == "reporter"
    assert "引用不足" in out["shared_context"]["degraded_reason"]

    out2 = agent.build_output({}, {"report": "x", "messages": []})
    assert "degraded" not in out2["shared_context"]


# ══ Reporter 修复 (v3) — 数字一致性校验 / 用户问题聚焦 / 无 [强][弱] ══

def test_verify_numbers_basics():
    """数字校验: 有来源的通过, 编造的数字检出, 派生数字跳过, 万/亿单位换算"""
    from dia.agents.reporter import _verify_numbers

    ctx = ('{"metric":"revenue","groups":[{"group":"A","value":14204983.5}],'
           '"pairs":[{"p_value":0.003,"mean_diff":9.98}]}')
    report = "华东营收 1420万, 占 24%, 是东北的 2.1 倍, 差异显著 (p=0.003), 组均值差 9.98"
    bad = _verify_numbers(report, ctx)
    assert bad == [], f"有来源数字被误报: {bad}"

    # 编造数字 (context 中不存在的大额数值)
    report_fake = "华东营收 9999万, 差异显著 (p=0.003)"
    bad2 = _verify_numbers(report_fake, ctx)
    assert "9999" in bad2, f"编造数字未检出: {bad2}"

    # 派生数字 (百分比/倍数/年份) 跳过
    report_derived = "占比 24%, 2.1 倍, 2026 年, 共 3 个区域, 见图2"
    assert _verify_numbers(report_derived, ctx) == []


def test_generate_node_number_verify_retry():
    """数字校验: 报告含无来源数字 → 带纠错提示重生成一次"""
    import asyncio
    from langchain_core.messages import AIMessage
    from dia.agents.reporter import ReporterAgent
    import dia.agents.reporter as rp

    class FakeLLM:
        def __init__(self):
            self.calls = 0
            self.retry_seen = False
        async def ainvoke(self, messages, config=None):
            self.calls += 1
            ctx = messages[1].content
            if "校验失败" in ctx:
                self.retry_seen = True
                return AIMessage(content=("华东营收 1420万, 差异显著 (p=0.003). "
                                          "报告结论完整字数够六百字以上了。" + "分析内容充实篇幅充足。" * 30))
            return AIMessage(content=("华东营收 9999万, 差异显著 (p=0.003). "
                                      "报告正文内容凑够六百字以上字数编号编号编号" * 30))

    fake = FakeLLM()
    async def fake_get_llm(temperature=None):
        return fake
    orig = rp.get_llm
    rp.get_llm = fake_get_llm
    try:
        agent = ReporterAgent(name="reporter")
        out = asyncio.run(agent._generate_node({
            "context": '{"groups":[{"group":"A","value":14204983.5}],"pairs":[{"p_value":0.003}]}',
            "recommendation_note": "", "user_question": "华东营收如何",
            "findings": [],
        }))
        assert fake.calls == 2, f"期望数字校验重生成一次, 实际 {fake.calls}"
        assert fake.retry_seen, "重试时未携带数字纠错提示"
        assert "9999" not in out["report"]
    finally:
        rp.get_llm = orig


def test_prompt_no_strong_weak_markers():
    """报告提示词不再包含 [强]/[弱] 分级标记, 建议前置, 首段回答用户问题"""
    from dia.agents.reporter import REPORTER_PROMPT

    assert "[强]" not in REPORTER_PROMPT and "[弱]" not in REPORTER_PROMPT
    assert "user_question" in REPORTER_PROMPT  # 第一原则占位符
    # 行动建议在关键发现/维度分析之后 (先分析后建议, 管理阅读顺序)
    assert REPORTER_PROMPT.index("### 四、行动建议") > REPORTER_PROMPT.index("### 三、维度分析")
    # 建议优先级用 emoji, 不用 [高]/[中]/[低] 文本
    assert "[高/中/低]" not in REPORTER_PROMPT
    assert "🔴" in REPORTER_PROMPT
    # 图表引用按标题, 不用"见图N"
    assert "见图N" not in REPORTER_PROMPT
    # 篇幅预算 (2500 → 4000: 支持每条发现含归因分析的完整分析链)
    assert "4000" in REPORTER_PROMPT


# ══ build_output: 四件套不变 ══
def test_build_output_shape():
    from langchain_core.messages import AIMessage
    from dia.agents.reporter import ReporterAgent

    agent = ReporterAgent(name="reporter")
    state = _state()
    result = {
        "messages": [AIMessage(content="报告正文")],
        "report": "报告正文",
        "_chart_data": state["analysis"]["chart_data"],
    }
    out = agent.build_output(state, result)

    assert out["reporter"]["done"] is True
    assert out["reporter"]["report"] == "报告正文"
    assert "html" not in out["reporter"]  # HTML 看板已移除, 图表走 ChartEvent 路径
    assert out["shared_context"]["final_report"] == "报告正文"
    # shared_context 合并不覆盖
    assert out["shared_context"]["data_quality_score"] == 80


def test_build_output_no_charts_no_html():
    from langchain_core.messages import AIMessage
    from dia.agents.reporter import ReporterAgent

    agent = ReporterAgent(name="reporter")
    state = _state()
    state["analysis"]["chart_data"] = []
    state["shared_context"]["charts"] = []
    result = {"messages": [AIMessage(content="报告")], "report": "报告"}
    out = agent.build_output(state, result)
    assert "html" not in out["reporter"]  # 无 html 输出


# ══ 内部技术标注剥离 ══

def test_strip_internal_annotations():
    """管理层报告不含口径/证据编号内部标注, 保留图表引用"""
    from dia.agents.reporter import _strip_internal_annotations

    report = (
        "华东是最强市场, 是东北的2.1倍 (sum口径, 证据 [F2])。"
        "月度趋势平稳 (见图: 图1)。"
        "品类差异显著 (证据 [F1])。"
        "残留下标 [F3] 也要删。"
    )
    cleaned = _strip_internal_annotations(report)
    assert "(sum口径" not in cleaned
    assert "证据 [F" not in cleaned
    assert "[F" not in cleaned
    assert "(见图: 图1)" in cleaned  # 图表引用保留 (前端分段依赖)


def test_strip_keeps_chart_refs():
    """多图表引用全部保留"""
    from dia.agents.reporter import _strip_internal_annotations
    report = "发现1 (见图: 图1) 发现2 (见图: 图2, 图3) 无标注段"
    cleaned = _strip_internal_annotations(report)
    assert cleaned.count("见图") == 2
    assert "图2, 图3" in cleaned
