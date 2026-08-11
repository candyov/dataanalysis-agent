"""Reporter — 专业数据分析报告生成 (5 模块结构: 核心结论/行动建议/关键发现/维度分析/风险局限)

数据流:
  输入: user_request + Analyst 结论(findings 已剥离 [强]/[弱]) + 工具结果 + Curator 探查报告 + 图表清单
  输出: report(markdown, 流式推给前端) + final_report(shared_context 持久化)
  图表: Analyst 的 build_chart 生成 → chat.py 按编号引用分段 → 前端内联渲染 (不走 reporter 直发)
"""

import json
import logging
import re
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from dia.core.base import BaseAgent, get_llm, _safe_parse_content

logger = logging.getLogger(__name__)

# 用户请求中出现这些词 → 报告包含行动建议部分
# (注: 建议默认必含 — 决策者视角; 以下关键词仅作为历史兼容保留, 已由"排除词"逻辑取代)
SUGGEST_KEYWORDS = ["建议", "怎么办", "措施", "改进", "如何", "下一步", "方案", "优化", "提升", "应对"]

REPORTER_PROMPT = """你是一个资深商业分析师，为业务决策者撰写分析报告。读者是管理层，不是统计学家。

## 第一原则: 直接回答用户问题

用户问题: 「{user_question}」
报告第一段 (核心结论第 1 条) 必须直接回答这个问题 (结论 + 关键数字), 然后才展开细节。
禁止用"本报告分析了…"之类的开场白。

## 写作原则

1. **结论先行, 业务语言**: 每条结论用业务语言说清"发生了什么 + 意味着什么",
   统计术语(检验方法名/p值/效应量)只放在括号里作为证据, 绝不作为主句。
   ✗ 错误: "地区间销售额差异显著（Kruskal-Wallis p<0.05）"
   ✓ 正确: "华东是最强市场(1420万,占24%), 是东北的2.1倍——资源应向华东倾斜"
2. **每个结论回答"所以呢"**: 不止说数字, 要说这个数字对业务意味着什么(该做什么)。
3. **数字可视化表述**: 用"是X的N倍""占Y%""领先/落后"替代"差异显著""集中度高"。
4. **不确定的结论用业务语言标注**: 数据支持不足时写"基于近期样本""需进一步验证",
   不使用强/弱/推测等分级标记符号。
5. **每个关键数字可追溯**: 证据行注明来源 (口径或发现编号), 如 "(营收口径: SUM(revenue))"。

## 输出结构 (严格按此顺序, 用 markdown 标题, 全文 ≤ 4000 字)

### 一、核心结论
4-6 条, 每条一句话 (≤60字), 业务语言 + 数字。老板只看这段就能做决策。
第一条必须是用户问题的直接答案。

### 二、关键发现
每条发现必须包含**完整的分析链条** (四层, 缺一不可):
1. **发现标题**: 业务语言概括, 如 "华东独大, 东北垫底"
2. **现象与证据**: 具体数字 + (检验 p 值, 括号内) — 只陈述事实
3. **归因分析**: "为什么会出现这个现象" — 结合数据做因果推断
   (如: 集中度/贡献度分解、头部vs尾部差距倍数、边际贡献、结构性原因)
4. **业务含义**: 这个发现对业务决策意味着什么, 以及应对建议的方向
每条发现末尾必须用 "(见图: 图N)" 引用一张与发现直接相关的图表。
图表引用一律用下方「图表清单」中的编号, 格式 "(见图: 图1)" — 禁止自创标题或改编号,
前端按编号精确对应图表; 数字编号/自创标题无对应关系, 会被校验拦截并重新生成。
关键发现 4-6 条, 每条 ≥ 80 字 (不含图表引用行) — 只写标题+一行证据不算完整发现。

### 三、维度分析
按区域/品类/渠道等维度, 每个维度: 一句话总结 + 关键数字 + 对应图表。
突出: 头部vs尾部差距 (倍数)、集中度 (Top3 占比)、异常值。
每个维度 ≥ 60 字, 引用 1 张对应图表。

### 四、行动建议
{recommendation_note}
每条建议格式: "- 🔴 具体动作 — 依据: 发现/数字 → 预期影响"
优先级用 emoji 开头: 🔴=高优先, 🟡=中优先, 🟢=低优先 (不要写 [高]/[中]/[低] 文字)。
预期影响尽量量化 (如"预计提升5%"); 无法量化的写"预期影响: 待评估", 不要写空话。
没有数据支撑的建议不要写。建议 ≤ 6 条。

### 五、风险与局限
- 🔴 数据问题: 哪些结论可能不准
- 🟡 统计局限: 样本/因果/预测不确定性
- ⚪ 无法回答: 缺数据答不了的问题

(报告不写附录。关键数字的追溯已由"写作原则第 5 条"保证: 证据行注明口径或发现编号。)

## 规则

1. 每个结论必须有具体数字支撑, 不编造 — 报告中的数字会被程序与数据源自动核对
2. 业务语言优先, 统计术语进括号
3. 风格: 简洁有力, 像麦肯锡的数据报告
4. 中文
5. 禁止套话: "值得关注""潜力巨大""提升空间明显""应优先保证其资源配置"一律不用
6. **数字单位统一**: 大额用 "万"/"亿", 首次出现注明精确值, 禁止混用
7. **建议必须有依据**: 引用发现或具体数字, 写不出依据就不写建议
8. **篇幅预算**: 全文 ≤ 4000 字, 核心结论每条 ≤ 60 字, 建议 ≤ 6 条,
   关键发现每条 ≥ 80 字 (含归因分析, 只写现象不算完整发现)
9. **图表引用是硬性要求**: 只要用户给了图表清单, 报告必须引用至少一半的图表,
   且 **关键发现中的每条发现必须配图** (见图: 图N 紧跟发现证据) — 报告生成后
   会被程序统计引用数量, 不足半数或关键发现无图将打回重写。不要只写文字不引图。
10. **预测表述分级** (forecast 结果含 predictability_level 和 scenarios):
   - 高/中可预测性: 正常给预测值与区间 ("预计下月 X 万元, 区间 [a, b]")
   - 低可预测性 (predictability < 40): 禁止用确定语气 ("预计为 X"), 必须写
     "预测不稳定, 建议按保守情景 (区间下限) 做预算留缓冲", 并用 scenarios 的
     乐观/基准/保守三档表述业务含义。预测不确定性同时写入"五、风险与局限"。
"""


def _strip_internal_annotations(report: str) -> str:
    """剥离报告中的内部技术标注 (管理层读者不需要看到).

    删除:
      - "(sum口径, 证据 [F2])" / "(营收口径: SUM(revenue))" — 口径括号注
      - "(证据 [F1])" / "[F2]" — 发现编号引用
      - "(sum口径)" 类残留
    保留:
      - "(见图: 图N)" — 图表引用 (前端分段依赖)
    """
    import re as _re
    # 1. 删除含"口径"的括号注 (可含嵌套括号, 如 "(营收口径: SUM(revenue))")
    #    正则: 匹配括号内含"口径"且括号配平的最外层括号对
    report = _re.sub(r"\((?:[^()]|\([^()]*\))*口径(?:[^()]|\([^()]*\))*\)", "", report)
    report = _re.sub(r"（(?:[^（）]|（[^（）]*）)*口径(?:[^（）]|（[^（）]*）)*）", "", report)
    # 2. 删除独立 "(证据 [Fk])" / "(证据: [Fk])" / "(证据 /)" 类标注 (编号可能被 LLM 写成 / 或缺失)
    report = _re.sub(r"\([（(]?\s*证据\s*[:：]?\s*[^）)]*?[）)]?\)", "", report)
    # 3. 删除残留的 [Fk] 编号 (不在括号内的)
    report = _re.sub(r"\[F\d+\]", "", report)
    # 4. 清理剥离后留下的多余**水平**空白 (不动换行 — \n\n 是 markdown 段落/标题分隔,
    #    压缩换行会导致 ### 标题失去前置空行, 渲染成段落文本)
    report = _re.sub(r"[ \t]{2,}", " ", report)
    report = _re.sub(r"[,，;；]\s*\)", ")", report)
    # 5. 修复 markdown 标题分隔: 确保每个 ### 标题前有空行 —
    #    LLM 常把 "内容 (见图: 图1) ### 二、关键发现" 写在同一行, 导致标题渲染成段落文本
    report = _re.sub(r"([^\n])\n?(?=### )", r"\1\n\n", report)
    return report.strip()


def _verify_numbers(report: str, context: str) -> list[str]:
    """报告数字一致性校验: 返回报告中找不到数据来源的数字 (疑似转录/编造).

    校验对象: 带小数的数值 (均值/p值/系数/CI) + 带"万/亿"单位的整数 (大额绝对值),
    以及 ≥1000 的整数。派生数字 (百分比/倍数/年份/计数/序号) 跳过 —
    它们由真实数字计算得出, 转录错误风险低。

    单位换算: 报告 "1420万" → 按 ×10000 换算后与 context 原始值比对 (容差 5%).
    """
    ctx_nums = set()
    for m in re.finditer(r"(?<![\w.])(\d+(?:\.\d+)?)", context):
        try:
            ctx_nums.add(round(float(m.group(1)), 4))  # 4 位精度: 保住 p=0.003 这类小值
        except ValueError:
            continue

    bad = []
    for m in re.finditer(r"(?<![\w.])(\d+(?:\.\d+)?)", report):
        raw = m.group(1)
        v = float(raw)
        tail = report[m.end():m.end() + 3]
        # 跳过派生数字: 百分比 / 倍数 / 年份 / 日期片段 / 低风险整数计数
        if tail.startswith("%") or "倍" in tail:
            continue
        if 1900 <= v <= 2100 and "." not in raw:
            continue
        # 单位换算: 万/亿 → 原始值
        if tail.startswith("万"):
            v *= 10000
        elif tail.startswith("亿"):
            v *= 100000000
        # 只校验: 带小数 (精确统计值) 或 大额整数 (≥1000, 或带万/亿单位的 ≥100)
        if "." not in raw and v < 1000 and not (tail.startswith("万") or tail.startswith("亿")):
            continue
        if any(abs(v - cv) / max(abs(v), 1e-9) < 0.05 for cv in ctx_nums):
            continue
        bad.append(raw)
    return bad


def _strip_level(text: str) -> str:
    """剥离 [强]/[弱]/[推测] 分级标记 — 报告面向决策者, 置信度用业务语言表达."""
    return re.sub(r"\[(强|弱|推测)\]\s*", "", text)


class ReporterAgent(BaseAgent):
    """报告生成 Agent — 6 模块专业报告"""

    def build_graph(self):
        from langgraph.graph import StateGraph, START, END
        g = StateGraph(dict)
        g.add_node("generate", self._generate_node)
        g.add_edge(START, "generate")
        g.add_edge("generate", END)
        return g.compile()

    async def _generate_node(self, state: dict, config=None) -> dict:
        llm = await get_llm(temperature=0.3)
        context = state.get("context", "")[:16000]
        findings = state.get("findings", []) or []
        last_error = None
        report = ""
        for attempt in range(2):
            resp = await llm.ainvoke([
                SystemMessage(content=REPORTER_PROMPT.format(
                    recommendation_note=state.get("recommendation_note", "本次报告不含建议部分 (用户未要求)."),
                    user_question=state.get("user_question", "") or "",
                )),
                HumanMessage(content=context),
            ])
            report = _safe_parse_content(resp.content)
            # 报告过短阈值: 全文预算 4000 字, 低于 600 字说明没展开分析链 (只写了标题/提纲)
            if len(report) < 600:
                last_error = f"报告过短 ({len(report)} 字符, 需 ≥600)"
                continue
            # 引用强制: 报告须引用过半 findings 编号, 不足重生成一次 (带缺失列表)
            if findings:
                used = {int(m) for m in re.findall(r"\[F(\d+)\]", report)}
                need = len(findings)
                if len(used) < max(1, need // 2) and attempt == 0:
                    missing = [f"F{i}" for i in range(1, need + 1) if i not in used]
                    context += (f"\n\n校验失败: 报告只引用了 {len(used)}/{need} 个发现编号, "
                                f"缺失 {missing}. 请重新生成并逐条覆盖, 结论必须引用 (证据 [Fk]).")
                    last_error = f"发现引用不足 ({len(used)}/{need})"
                    continue
            # 图表编号校验: (见图: 图N) 必须落在清单内, 且引用数量须过半 — 否则重生成.
            # 只查"非法编号"有漏洞: LLM 一个都不引用时 bad_ids 为空, 校验会误通过.
            chart_ids = state.get("_chart_ids", [])
            if chart_ids:
                from dia.report.segments import extract_referenced_ids
                ref_ids = extract_referenced_ids(report)
                bad_ids = [i for i in ref_ids if i not in chart_ids]
                if bad_ids and attempt == 0:
                    context += (f"\n\n校验失败: 报告引用了不存在的图表编号 {bad_ids}, "
                                f"合法编号为 {chart_ids}. 请把图表引用改为清单中的编号, 格式 (见图: 图N).")
                    last_error = f"图表编号非法 ({bad_ids})"
                    continue
                # 引用数量闸门: 有图但引用不足 → 重生成 (防"清单给了却一张不引")
                need_charts = max(1, len(chart_ids) // 2)
                if len(ref_ids) < need_charts and attempt == 0:
                    context += (f"\n\n校验失败: 本次分析生成了 {len(chart_ids)} 张图表 (清单见上), "
                                f"但报告只引用了 {len(ref_ids)} 张. 请在关键发现/维度分析处用 "
                                f"(见图: 图N) 至少引用 {need_charts} 张图表, 编号必须来自清单.")
                    last_error = f"图表引用不足 ({len(ref_ids)}/{need_charts})"
                    continue
                # 重复引用闸门: 同一图引用 >2 次 = 硬凑引用 (发现没配对应图),
                # 说明图表覆盖度不够 — 打回重写, 要求引用更多不同图表.
                from collections import Counter
                ref_counts = Counter(ref_ids)
                overused = [i for i, c in ref_counts.items() if c > 2]
                if overused and attempt == 0:
                    context += (f"\n\n校验失败: 图表 图{','.join(map(str, overused))} 被引用了 "
                                f"{[ref_counts[i] for i in overused]} 次, 疑似多个发现硬凑同一张图. "
                                f"本次生成了 {len(chart_ids)} 张图表 (清单见上), 请为每个发现引用不同的图表, "
                                f"覆盖更多图表编号, 格式 (见图: 图N).")
                    last_error = f"图表重复引用 ({overused} 超 2 次)"
                    continue
            # 数字一致性: 报告中的大额/精确数字必须在数据中有来源 (防转录错误/编造)
            bad_nums = _verify_numbers(report, context)
            if bad_nums and attempt == 0:
                context += (f"\n\n校验失败: 以下数字在数据中找不到来源, 疑似编造或转录错误: "
                            f"{bad_nums[:10]}. 请用数据中的实际数字重新生成.")
                last_error = f"数字校验失败 ({len(bad_nums)} 个无来源)"
                continue
            # 校验全部通过 → 剥离内部技术标注后输出 (管理层读者不需要口径/证据编号)
            # - 删除 "(sum口径, 证据 [F2])" / "(营收口径: SUM(revenue))" / "(证据 [F1])" 类内部标注
            # - 保留 "(见图: 图N)" 图表引用 (前端分段依赖)
            report = _strip_internal_annotations(report)
            return {"messages": [AIMessage(content=report)], "report": report}
        # 两次均失败 → 返回最后一份报告 + 降级标记 (build_output 写入 shared_context)
        logger.warning(f"[Reporter] 报告生成降级: {last_error}")
        return {"messages": [AIMessage(content=report)], "report": report,
                "degraded": True, "degraded_reason": last_error}

    def extract_input(self, state: dict) -> dict:
        """组装报告 context: 用户问题 + 口径 + 质量 + 分析结论 + 工具结果 + 图表"""
        analysis = state.get("analysis", {}) or {}
        shared = state.get("shared_context", {}) or {}
        curator_report = shared.get("curator_report", {}) or {}
        source_id = state.get("source_id", "")
        user_request = state.get("user_request", "")

        # ── 1. 用户问题 ──
        parts = [f"## 用户问题\n{user_request}"]

        # ── 1.5 图表清单 (紧跟用户问题, 确保在 context 截断窗口内 — 清单被截断
        #     → Reporter 看不到编号 → 无法引用 → 图全堆末尾) ──
        chart_data = (analysis.get("charts")
                      or shared.get("charts")
                      or analysis.get("chart_data", []))
        if chart_data:
            from dia.report.segments import build_chart_catalog
            catalog = build_chart_catalog(chart_data)
            parts.append("## 图表清单 (引用图表必须用编号, 如 (见图: 图1), 禁止自创标题)\n" + catalog)

        # ── 2. 口径与数据边界 (Curator CONFIRM) ──
        confirm = curator_report.get("confirm", {}) or {}
        if confirm:
            confirm_lines = []
            if confirm.get("caliber"):
                confirm_lines.append(f"口径: {confirm['caliber']}")
            if confirm.get("understanding"):
                confirm_lines.append(f"问题理解: {confirm['understanding']}")
            if confirm.get("cannot_answer"):
                confirm_lines.append(f"数据无法回答: {'; '.join(confirm['cannot_answer'][:5])}")
            if confirm_lines:
                parts.append("## 口径与数据边界\n" + "\n".join(confirm_lines))

        # ── 3. 数据质量 (Curator QUALITY 分层) ──
        quality = shared.get("quality_report") or curator_report.get("quality") or {}
        quality_lines = [f"综合等级: {quality.get('grade', shared.get('data_quality_score', '?'))}"]
        if quality.get("blockers"):
            quality_lines.append("阻塞问题:")
            quality_lines += [f"  - {b}" for b in quality["blockers"][:5]]
        if quality.get("degraded"):
            quality_lines.append("降级问题:")
            quality_lines += [f"  - {d}" for d in quality["degraded"][:3]]
        if not quality.get("blockers") and not quality.get("degraded"):
            quality_lines.append("数据质量良好, 无明显问题")
        parts.append("## 数据质量\n" + "\n".join(quality_lines))

        # ── 4. 分析结论 (Analyst summary + findings 编号化 [Fk], 报告必须引用) ──
        raw_findings = analysis.get("structured_data", {}).get("findings", [])
        findings = raw_findings if isinstance(raw_findings, list) else []
        conclusion_parts = []
        if analysis.get("summary"):
            conclusion_parts.append(_strip_level(str(analysis["summary"])))
        for i, f in enumerate(findings[:10], 1):
            conclusion_parts.append(f"[F{i}] {_strip_level(f.get('claim', str(f)))}")
        if conclusion_parts:
            parts.append("## 分析结论\n" + "\n".join(conclusion_parts))
        if findings:
            parts.append(f"## 引用规则\n报告结论必须逐条引用发现编号 [F1]..[F{min(len(findings), 10)}], "
                         f"格式如 (证据 [Fk]) — 未引用的编号会被校验拦截并重新生成.")

        # ── 5. 工具执行结果 (证据链) — 按报告引用重要性排序后再截断 ──
        # 统计验证/归因是报告核心证据, 优先占配额; 探索类大 JSON 靠后被截
        tool_results = (analysis.get("structured_data", {}) or {}).get("tool_results", [])
        _TOOL_PRIORITY = {"test_difference": 0, "hypothesis_test": 0, "attribution": 1,
                          "compare": 1, "detect": 2, "forecast": 2, "seasonal_analysis": 2,
                          "explore": 3, "drill_down": 3, "query": 3}
        ordered = sorted(tool_results, key=lambda tr: _TOOL_PRIORITY.get(tr.get("tool", ""), 9))
        # 限流: 单条 700 字符 × 最多 12 条 (大 JSON 挤占 context 会截掉图表清单)
        tool_text = ""
        for tr in ordered[:12]:
            tool_text += f"\n\n[{tr['tool']}]\n{json.dumps(tr['data'], ensure_ascii=False, default=str)[:700]}"
        if tool_text:
            parts.append(f"## 工具执行结果{tool_text}")

        # ── 6. 图表说明 (清单已在 1.5 节置于 context 前部; 这里只做兜底说明) ──
        chart_count = int(analysis.get("charts_generated", len(chart_data) if chart_data else 0))
        if chart_count <= 0:
            parts.append("## 图表说明\n本次分析未生成任何图表 — 报告图表章节写说明性文字, 严禁写\"见图: 图N\"等引用.")

        # ── 6.5 报告蓝图: 必须覆盖的分析章节 (Curator 探查 → Analyst 填充 → 本报告体现) ──
        blueprint = shared.get("report_blueprint")
        if blueprint and blueprint.get("chapters"):
            ch_lines = []
            for ch in blueprint["chapters"]:
                if ch.get("type") in ("group_compare", "time_series", "cross_analysis",
                                      "year_over_year", "top_n"):
                    title = ch.get("title", "")
                    desc = ch.get("description", "")
                    ch_lines.append(f"- {title}" + (f": {desc}" if desc else ""))
            if ch_lines:
                parts.append("## 报告必须覆盖的章节\n" + "\n".join(ch_lines))

        # ── 7. Metric Store KPI 摘要 ──
        try:
            from dia.engine.metrics import get_metrics, get_time_series
            metrics = get_metrics(source_id)
            if metrics:
                ms_lines = []
                for m in metrics[:8]:
                    ts = get_time_series(m["name"], source_id, window_days=14)
                    stats = ts.get("stats", {})
                    ms_lines.append(f"- {m.get('label', m['name'])}: 最新={stats.get('last_value', '?')}, 趋势={stats.get('trend_direction', '?')}")
                if ms_lines:
                    parts.append("## 已注册指标\n" + "\n".join(ms_lines))
        except Exception:
            pass

        # ── 8. 建议触发判断: 默认必含建议 (决策者视角 — 分析没有建议就是信息搬运),
        #    除非用户明确排除 (不需要建议/只要分析等) ──
        no_suggest = any(kw in user_request for kw in
                         ("不需要建议", "不用建议", "不要建议", "只要分析", "仅分析", "只分析", "不用给建议"))
        if no_suggest:
            recommendation_note = "本次报告不含建议部分 (用户明确要求). 直接在文中删除该模块标题."
        else:
            recommendation_note = "本次报告必须包含行动建议部分, 每条建议: 具体动作 + 依据的发现编号或数字 + 预期影响."

        return {
            "context": "\n\n".join(filter(None, parts)),
            "recommendation_note": recommendation_note,
            "_tool_results": tool_results,
            # 引用强制校验用: _generate_node 检查报告是否引用了过半编号
            "findings": findings[:10],
            # 图表编号校验用: 合法编号集合 (清单内 图1..图N)
            "_chart_ids": list(range(1, len(chart_data) + 1)) if chart_data else [],
            # 第一原则: prompt 直接引用用户问题, 强制首段回答
            "user_question": user_request,
        }

    def build_output(self, state: dict, result: dict) -> dict:
        report = result.get("report", "")
        # 降级保留部分结果 (P0-3): 报告生成失败/为空 → 用 Analyst summary 兜底,
        # 保证用户至少拿到分析结论 (而不是什么都看不到)
        degraded = bool(result.get("degraded"))
        if degraded and (not report or len(report) < 600):
            fallback = (state.get("analysis", {}) or {}).get("summary", "")
            if fallback:
                report = f"### 核心结论 (简版 — 报告生成降级)\n\n{fallback}"
                logger.info("[Reporter] 报告降级 → 用 analysis.summary 兜底")
        out = {
            "messages": result.get("messages", []),
            "reporter": {
                "done": True,
                "report": report,
            },
            "shared_context": {
                **(state.get("shared_context", {})),
                "final_report": report,
            },
        }
        # 报告生成降级 (引用校验两次失败/报告过短) → 通知前端
        if degraded:
            out["shared_context"]["degraded"] = True
            out["shared_context"]["degraded_agent"] = "reporter"
            out["shared_context"]["degraded_reason"] = result.get("degraded_reason", "报告生成质量不足")
        return out


_reporter = ReporterAgent(name="reporter")


async def reporter_node(state: dict, config=None) -> dict:
    return await _reporter.run(state, config)
