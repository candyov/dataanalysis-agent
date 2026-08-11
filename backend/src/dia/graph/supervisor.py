"""Supervisor -- LLM 计划驱动 + 路由

一次 LLM 调用 = 意图识别 + 任务规划 → 生成完整执行计划
Supervisor 按计划逐步执行, 每步完成后 agent → supervisor 循环

设计要点:
- plan 每个 agent 至多出现一次, 顺序 curator → analyst → reporter (reporter 必须最后)
- iteration_count 只计数"同一 agent 未完成的重试", 正常推进不消耗 → 任意步数 plan 都能走完
- MAX_ITERATIONS 是重试上限: agent 反复失败时降级结束, 而不是截断正常流程
"""

import json
import logging
import time

from langchain_core.messages import SystemMessage

from dia.core.base import get_llm
from dia.core.state import MultiAgentState, validate_state
from dia.core.config import settings

logger = logging.getLogger(__name__)

VALID_AGENTS = {"curator", "analyst", "reporter"}
KEY_MAP = {"curator": "curator", "analyst": "analysis", "reporter": "reporter"}
# 人机协同 (P2): Analyst 置信度低于此值 → 请求用户确认 (continue/reanalyze)
CONFIRM_THRESHOLD = 0.4


def _max_retries_for(error_type: str) -> int:
    """失败分类 → 重试上限 (P0-2).

    - llm_timeout / llm_generic: 瞬时故障, 给满 MAX_ITERATIONS 次重试
    - llm_auth:                  配置问题, 重试无意义 → 1 次
    - tool_sql:                  数据/语法问题, 重试可能换个 SQL 成功 → 2 次
    - 无错误 (agent 正常未完成, 如轮次耗尽): 保留原 MAX_ITERATIONS
    """
    if error_type in ("llm_auth",):
        return 1
    if error_type in ("tool_sql",):
        return 2
    return settings.MAX_ITERATIONS


def _normalize_plan(plan: dict) -> dict:
    """校验 + 去重 + 排序 plan steps。

    - 过滤未知 agent
    - 每个 agent 至多出现一次 (done 标记是全局的, 重复步骤无法执行)
    - 按 curator → analyst → reporter 固定顺序
    """
    seen = set()
    steps = []
    for s in plan.get("steps", []):
        agent = (s.get("agent") or "").strip()
        if agent in VALID_AGENTS and agent not in seen:
            seen.add(agent)
            steps.append({"agent": agent, "goal": s.get("goal", "")})

    # 固定顺序: curator → analyst → reporter (reporter 必须最后)
    order = {"curator": 0, "analyst": 1, "reporter": 2}
    steps.sort(key=lambda s: order.get(s["agent"], 9))

    if not steps:
        steps = [{"agent": "analyst", "goal": "分析数据并生成结论"},
                 {"agent": "reporter", "goal": "生成最终报告"}]

    plan["steps"] = steps
    return plan


# 意图 → 代码级调度约束 (不依赖 LLM 自觉, 保证关键路径确定性)
_INTENT_QUICK_KEYWORDS = ("简单", "快速", "简要", "大概", "概况", "一眼", "quick")
_INTENT_ATTRIBUTION_KEYWORDS = ("为什么", "原因", "归因", "驱动", "导致", "影响", "attribution", "对比")


def _apply_intent_policy(plan: dict, user_request: str = "", source_id: str = "",
                         shared: dict | None = None) -> dict:
    """按意图调整 plan steps — 意图字段从"死字段"变为真正的调度约束.

    必须在 _apply_reuse 之后调用: quick 移除 curator 需要知道语义层是否可复用
    (此时 shared 已注入会话内/缓存 curator_report, 判断才准确).

    - intent=quick 或请求含快速词 → 有语义层则移除 curator (省探查);
      无语义层则保留 (否则 Analyst 缺 glossary → 工具失败率飙升 → revisit, 反而更慢)
    - intent=attribution 或含归因词 → analyst goal 追加归因要求
    仅在 LLM 规划的 steps 基础上做减法/增强, 不新增 agent.
    """
    intent = str(plan.get("intent", "")).lower()
    steps = plan.get("steps", [])
    req = user_request.lower()

    # quick: 有条件移除 curator
    is_quick = intent == "quick" or any(kw in req for kw in _INTENT_QUICK_KEYWORDS)
    if is_quick and any(s["agent"] == "curator" for s in steps):
        if _has_semantic_layer(source_id, shared):
            plan["intent"] = "quick"
            steps = [s for s in steps if s["agent"] != "curator"]
            logger.info("[Supervisor] 意图=quick 且有语义层 → 移除 curator (省探查)")
        else:
            logger.info("[Supervisor] 意图=quick 但无语义层 → 保留 curator (避免 Analyst 工具失败)")

    # attribution: analyst goal 追加归因
    is_attr = intent == "attribution" or any(kw in req for kw in _INTENT_ATTRIBUTION_KEYWORDS)
    if is_attr:
        plan["intent"] = "attribution"
        for s in steps:
            if s["agent"] == "analyst":
                if "归因" not in s.get("goal", ""):
                    s["goal"] = (s.get("goal", "") + "；重点做归因分析(驱动因素/贡献度)").strip()
                    logger.info("[Supervisor] 意图=attribution → analyst goal 追加归因要求")

    plan["steps"] = steps
    return plan


def _has_semantic_layer(source_id: str, shared: dict | None) -> bool:
    """是否有可复用的语义层 (会话内 curator_report 或新鲜 glossary 缓存)."""
    shared = shared or {}
    report = shared.get("curator_report") or {}
    if report.get("source_id") == source_id:
        return True
    try:
        from dia.infrastructure.persistence.glossary_cache import (
            load_glossary_cache, is_fresh,
        )
        cache = load_glossary_cache(source_id)
        return bool(cache and is_fresh(cache))
    except Exception:
        return False


async def _generate_plan(user_request: str) -> dict:
    """生成执行计划。文件上传统一走 /datasources/upload API, 图内无 ingestor。"""
    llm = await get_llm(temperature=0.1)

    agents_desc = (
        "系统有 3 个 Agent:\n"
        "- curator: 数据准备: 扫描 schema + 质量检查 + 注册 KPI\n"
        "- analyst: 数据分析 + 图表生成 (核心)\n"
        "- reporter: 生成最终分析报告\n"
    )

    examples = (
        '// 完整分析: {"intent":"attribution","summary":"一句话","steps":[{"agent":"curator","goal":"扫描结构,检查质量,注册KPI"},{"agent":"analyst","goal":"查询数据,逐区域下钻分析,生成图表"},{"agent":"reporter","goal":"生成分析报告"}]}\n'
        '// 简单分析: {"intent":"quick","summary":"一句话","steps":[{"agent":"analyst","goal":"快速分析数据并出结论"},{"agent":"reporter","goal":"生成分析报告"}]}'
    )

    prompt = (
        f"{agents_desc}\n"
        f"用户请求: {user_request}\n\n"
        f"按需组合步骤, 每个 agent 最多出现一次, 必须按 curator → analyst → reporter 顺序, reporter 必须最后. 参考示例 (只返回 JSON):\n{examples}"
    )

    fallback = {"intent": "general", "summary": "数据分析", "steps": [
        {"agent": "analyst", "goal": "分析数据并生成结论"},
        {"agent": "reporter", "goal": "生成最终报告"},
    ]}

    try:
        resp = await llm.ainvoke([SystemMessage(content=prompt)])
        raw = resp.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        plan = json.loads(raw)
        plan.setdefault("intent", "general")
        plan.setdefault("summary", "")
        plan.setdefault("steps", [{"agent": "analyst", "goal": "分析数据"}])
        plan = _normalize_plan(plan)
        # 意图驱动调度: intent 是代码级约束, 不依赖 LLM 自觉
        plan = _apply_intent_policy(plan, user_request)
        logger.info(f"[Supervisor] Plan: intent={plan['intent']}, steps={[s['agent'] for s in plan['steps']]}")
        return plan
    except Exception as e:
        logger.warning(f"[Supervisor] Plan generation failed: {e}")
        return _normalize_plan(fallback)


def _apply_reuse(plan: dict, shared: dict, source_id: str = "") -> tuple[dict, dict]:
    """多轮复用: 已有**同数据源**的探查报告 → 从 plan 移除 curator.

    复用来源 (优先级):
      1. 会话内 curator_report (shared_context, 本轮之前已探查)
      2. 持久化 glossary 缓存 (glossary_cache.db, 跨会话 — 方案 A)

    数据源变了 (source_id 不匹配) → 不复用, 重新探查 —
    防止旧数据的探查报告被用来分析新数据 (全链路结论全错).
    缓存陈旧 (超 TTL) → 不复用, 重新探查 (数据可能已变更).

    Returns:
        (plan, shared) — 缓存命中时 shared 已注入 glossary/kpis/curator_report,
        调用方必须把 shared 写回 state (首次规划分支), 否则 Analyst 拿不到语义层.
    """
    if not any(s["agent"] == "curator" for s in plan["steps"]):
        return plan, shared

    report = shared.get("curator_report") or {}
    report_src = report.get("source_id", "") if isinstance(report, dict) else ""
    if shared.get("curator_report") and report_src == source_id:
        logger.info("[Supervisor] 已有同数据源探查报告, 跳过 curator (会话内复用)")
        plan["steps"] = [s for s in plan["steps"] if s["agent"] != "curator"]
        return plan, shared

    # 会话内无 → 查持久化缓存 (跨会话复用)
    try:
        from dia.infrastructure.persistence.glossary_cache import (
            load_glossary_cache, is_fresh,
        )
        cache = load_glossary_cache(source_id)
        if cache and is_fresh(cache):
            # 注入缓存内容到 shared_context, 供 Analyst/Reporter 使用 (与 Curator 输出同构)
            shared["glossary"] = cache["glossary"]
            shared["registered_kpis"] = cache["kpis"]
            shared["curator_report"] = cache["curator_report"]
            if cache["curator_report"].get("quality"):
                shared["quality_report"] = cache["curator_report"]["quality"]
            # 蓝图恢复: glossary 缓存不存 report_blueprint (设计如此, 表里只有
            # glossary/kpis/curator_report), 缓存命中跳过 curator → 新会话没有蓝图 →
            # /sessions/{id}/data 404 → 全屏报告视图 KPI/质量卡片缺失。从同 source_id
            # 最近会话的 state 取蓝图补注入 (会话量小, LIKE 扫描可接受)
            try:
                from dia.infrastructure.persistence.sessions import _conn
                row = _conn().execute(
                    "SELECT state_json FROM sessions"
                    " WHERE state_json LIKE ? AND state_json LIKE ?"
                    " ORDER BY last_access DESC LIMIT 1",
                    (f"%{source_id}%", "%report_blueprint%"),
                ).fetchone()
                if row:
                    bp = (json.loads(row[0]).get("shared_context") or {}).get("report_blueprint")
                    if bp:
                        shared["report_blueprint"] = bp
                        logger.info(f"[Supervisor] 从历史会话恢复 report_blueprint ({source_id})")
            except Exception as e:
                logger.warning(f"[Supervisor] blueprint 恢复失败 (忽略): {e}")
            logger.info(f"[Supervisor] 命中 glossary 缓存 ({source_id}), 跳过 curator (跨会话复用)")
            plan["steps"] = [s for s in plan["steps"] if s["agent"] != "curator"]
        elif cache:
            age_h = (time.time() - cache["updated_at"]) / 3600
            logger.info(f"[Supervisor] glossary 缓存已陈旧 ({age_h:.1f}h), 重新探查")
    except Exception as e:
        logger.warning(f"[Supervisor] glossary 缓存读取失败 (忽略, 正常探查): {e}")
    return plan, shared


async def supervisor_node(state: MultiAgentState) -> dict:
    validate_state(state)  # 启动时校验 state key 拼写
    iteration = state.get("iteration_count", 0)
    user_request = state.get("user_request", "")
    shared = state.get("shared_context", {}) or {}
    plan = state.get("plan")

    # 首次: 生成计划
    if not plan or not plan.get("steps"):
        plan = await _generate_plan(user_request)
        plan, shared = _apply_reuse(plan, shared, state.get("source_id", ""))
        # 意图驱动调度 (P0-1): 必须在 _apply_reuse 之后 — quick 移除 curator
        # 需要知道语义层是否可复用 (此时 shared 已注入会话内/缓存 curator_report)
        plan = _apply_intent_policy(plan, user_request, state.get("source_id", ""), shared)
        # 历史结论记忆: 无论是否命中 glossary 缓存, 都注入同数据源的历史分析结论 —
        # Analyst 据此知道"上次分析过什么", 可延续/对比 (带时间标注, 仅背景参考)
        try:
            from dia.infrastructure.persistence.glossary_cache import load_history
            history = load_history(state.get("source_id", ""))
            if history:
                shared["analysis_history"] = history
                logger.info(f"[Supervisor] 注入历史结论 {len(history)} 条 ({state.get('source_id', '')})")
        except Exception as e:
            logger.warning(f"[Supervisor] 历史结论读取失败 (忽略): {e}")
        # shared 可能被 _apply_reuse 注入缓存 (glossary/kpis/curator_report) → 写回 state
        return {"plan": plan, "plan_step": 0, "iteration_count": 0, "next": plan["steps"][0]["agent"],
                "shared_context": shared}

    # 后续: 逐步执行
    steps = plan.get("steps", [])
    step_idx = state.get("plan_step", 0)

    if step_idx >= len(steps):
        return {"next": "finish"}

    current_step = steps[step_idx]
    current_agent = current_step["agent"]
    goal = current_step.get("goal", "")

    state_key = KEY_MAP.get(current_agent, current_agent)
    agent_output = state.get(state_key, {}) or {}

    # 已完成 → 消费动态路由建议, 否则正常推进
    if agent_output.get("done"):
        # 人机协同 (P2): Analyst 低置信度 (<0.4) 且非 revisit 场景 → 请求用户确认
        # 发 confirm_required 事件并提前结束流; 用户选择后重发 (confirmation 参数)
        if current_agent == "analyst" and not shared.get("suggest_next"):
            conf = agent_output.get("confidence", 0.0)
            if isinstance(conf, (int, float)) and conf < CONFIRM_THRESHOLD:
                reason = (f"分析置信度仅 {conf:.2f} (数据质量或方法限制), 结论可能不稳. "
                          f"已有部分结果: {str(agent_output.get('summary', ''))[:80]}")
                logger.info(f"[Supervisor] 低置信度 {conf:.2f} → 请求用户确认 (P2)")
                return {"next": "confirm", "plan_step": step_idx,
                        "shared_context": {**shared, "suggest_next": None,
                                           "confirm_reason": reason,
                                           "confirm_confidence": float(conf)}}

        suggest = shared.get("suggest_next")
        if suggest and isinstance(suggest, dict):
            action, target = suggest.get("action"), suggest.get("target")
            logger.info(f"[Supervisor] 动态路由: {action} → {target} ({suggest.get('reason', '')})")
            if action == "skip":
                # 跳过 target agent 的步骤: 推进到 target 之后
                skip_idx = next((i for i, s in enumerate(steps) if s["agent"] == target), None)
                if skip_idx is not None and skip_idx >= step_idx:
                    new_idx = skip_idx + 1
                    nxt = steps[new_idx]["agent"] if new_idx < len(steps) else "finish"
                    logger.info(f"[Supervisor] 跳过 {target} (step {skip_idx}) → {nxt}")
                    return {"next": nxt, "plan_step": new_idx,
                            "iteration_count": 0, "current_goal": "",
                            "shared_context": {**shared, "suggest_next": None}}
            if action == "revisit" and target in VALID_AGENTS:
                # 重置 target 及其下游 agent 的 done → 全链路重跑
                target_idx = next((i for i, s in enumerate(steps) if s["agent"] == target), step_idx)
                reset = {}
                for i, s in enumerate(steps):
                    if i >= target_idx:
                        reset[KEY_MAP.get(s["agent"], s["agent"])] = {"done": False}
                logger.info(f"[Supervisor] 重路由到 {target} (step {target_idx}), 重置下游: {list(reset.keys())}")
                return {"next": target, "plan_step": target_idx,
                        "iteration_count": 0, "current_goal": "",
                        **reset,
                        # charts 清空: 重跑后旧图表作废 (SSE 收集器重新收集)
                        "shared_context": {**shared, "suggest_next": None, "charts": []}}
            # 未知/无效建议 → 忽略, 回退正常推进 (且清除, 防跨轮残留)

        # 正常推进下一步 (不消耗 iteration, 且重置为 0 → 重试预算 per-agent,
        # 每个 agent 独立拥有 MAX_ITERATIONS 次机会, 前面的 agent 失败不再吃掉后面的)
        nxt = steps[step_idx + 1]["agent"] if step_idx + 1 < len(steps) else "finish"
        logger.info(f"[Supervisor] Step {step_idx+1}/{len(steps)} {current_agent} 完成 → {nxt}")
        out = {"next": nxt, "plan_step": step_idx + 1, "iteration_count": 0, "current_goal": ""}
        if suggest:
            out["shared_context"] = {**shared, "suggest_next": None}  # 消费后清除, 防跨轮残留
        return out

    # 未完成 → 执行 (首次或重试)。iteration 只在此处增长 = 重试计数
    # 失败分类重试 (P0-2): 按 error_type 决定重试次数, 而不是一刀切 MAX_ITERATIONS
    # error_type 由 BaseAgent.run 失败时顶层返回 (不走 build_output, 直接 merge 进 state)
    error_type = state.get("error_type", "") or ""
    if iteration >= _max_retries_for(error_type):
        logger.warning(f"[Supervisor] {current_agent} 反复未完成 {iteration} 次 (err={error_type or 'none'}) → degraded finish")
        return {"next": "finish",
                "shared_context": {**shared, "degraded": True,
                                   "degraded_agent": current_agent,
                                   "degraded_reason": f"{current_agent} 执行失败超过 {_max_retries_for(error_type)} 次"
                                                      + (f" ({error_type})" if error_type else ""),
                                   # 与其他收尾路径一致: 消费并清除, 防跨轮残留
                                   "suggest_next": None}}
    logger.info(f"[Supervisor] Step {step_idx+1}/{len(steps)} → {current_agent}: {goal}")
    return {"next": current_agent, "plan_step": step_idx, "iteration_count": iteration + 1, "current_goal": goal,
            # 清除上一轮残留的 error_type (本轮重新执行, 失败会重新写入)
            "error_type": ""}


def supervisor_router(state: MultiAgentState) -> str:
    from langgraph.graph import END
    nxt = state.get("next", "analyst")
    if nxt == "finish":
        return END
    # 人机协同 (P2): confirm 不是 agent — 路由到 END, chat.py 捕获后发事件并结束流
    if nxt == "confirm":
        return END
    if nxt not in VALID_AGENTS:
        logger.warning(f"[Supervisor] Unknown agent '{nxt}' → fallback analyst")
        return "analyst"
    return nxt
