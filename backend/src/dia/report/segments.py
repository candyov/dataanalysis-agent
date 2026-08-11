"""报告-图表分段: 图表编号 + 清单生成 + 报告文本确定性分段 (纯函数, 无副作用)

设计契约 (根治"标题漂移"与"恢复丢图"):
  1. 图表编号由确定性代码分配 (图1..图N), 顺序 = 图表池顺序 (analyst 输出 charts).
  2. Reporter 只被允许引用编号 (见图: 图N), prompt 强制 + 生成后校验重试.
  3. 报告完成后由 chat.py 调用 split_report_segments 生成 segments (图表数据内嵌),
     存进 session state — 前端实时/恢复都直接消费 segments, 不再自行匹配图表.

匹配优先级 (三层保障, 图表永不丢失):
  A. 编号引用  (见图: 图N) → 按编号取图表池条目
  B. 标题引用  (见图: 标题) → 去空格包含匹配 (兼容旧报告/LLM 直接写标题)
  C. 末尾兜底  → 未被内联引用的图表附加到报告末尾"图表"区
"""

from __future__ import annotations
import re
from typing import Any

# 图表引用锚点: (见图: 图1) / （见图：图1）/ 见图: 图1 / (图: 标题)
# 引用内容可含多编号 "(见图: 图9, 图10)" — 拆分逻辑见 split_report_segments
_ANCHOR_RE = re.compile(r"\(?(?:见图|图|图表)[:：]\s*([^)）\n。；;]+)\)?")

_SEG_TEXT = "text"
_SEG_CHART = "chart"


def assign_chart_ids(charts: list[dict]) -> list[dict]:
    """为图表池分配稳定编号, 返回带 chart_id (图N) 的新列表 (不修改入参)."""
    out = []
    for i, c in enumerate(charts, 1):
        out.append({**c, "chart_id": f"图{i}"})
    return out


def build_chart_catalog(charts: list[dict]) -> str:
    """生成 Reporter 可见的图表清单文本 (编号 + 标题, 完整不截断)."""
    lines = []
    for i, c in enumerate(charts, 1):
        title = c.get("title") or c.get("chart_type", "图表") or "图表"
        lines.append(f"图{i}: {title}")
    return "\n".join(lines)


def _match_by_title(pool: list[dict], title: str) -> dict | None:
    """标题模糊匹配: 去空格后互相包含. 返回图表条目或 None."""
    t = title.replace(" ", "").replace("　", "")
    if not t:
        return None
    for c in pool:
        ct = (c.get("title") or "").replace(" ", "").replace("　", "")
        if ct and (ct == t or ct in t or t in ct):
            return c
    return None


def split_report_segments(report_text: str, charts: list[dict]) -> list[dict]:
    """报告文本 + 图表池 → segments 列表.

    segments 元素:
      {"type": "text", "text": str}
      {"type": "chart", "title": str, "chart_type": str, "echarts_option": dict}

    charts: 图表池 (建议传入 assign_chart_ids 后的条目, 含 chart_id).
    无图表池或报告无引用 → 返回 [{"type":"text","text":report_text}].
    """
    if not charts:
        return [{"type": _SEG_TEXT, "text": report_text}]

    pool = assign_chart_ids(charts)
    id_map: dict[str, dict] = {c["chart_id"]: c for c in pool}

    segments: list[dict] = []
    used_ids: set[str] = set()
    last = 0
    for m in _ANCHOR_RE.finditer(report_text):
        raw = m.group(1).strip()
        if not raw:
            continue
        # A. 编号引用: 支持多编号 "(见图: 图9, 图10)" / "(见图: 图9和图10)"
        #    提取引用内容中所有 图N, 逐个内联到同一位置; 无编号 → 按标题模糊匹配
        nums = [int(n) for n in re.findall(r"图(\d+)", raw)]
        matched: list[dict] = []
        if nums:
            for n in nums:
                chart = id_map.get(f"图{n}")
                if chart and chart["chart_id"] not in used_ids:
                    matched.append(chart)
        else:
            # B. 标题引用
            chart = _match_by_title(pool, raw)
            if chart and chart["chart_id"] not in used_ids:
                matched.append(chart)
        if not matched:
            continue  # 匹配不到 → 保留原文 (引用文字留在文本段)
        if m.start() > last:
            segments.append({"type": _SEG_TEXT, "text": report_text[last:m.start()]})
        for chart in matched:
            segments.append({
                "type": _SEG_CHART,
                "title": chart.get("title", ""),
                "chart_type": chart.get("chart_type", ""),
                "echarts_option": chart.get("echarts_option") or {},
            })
            used_ids.add(chart["chart_id"])
        last = m.end()

    if last < len(report_text):
        segments.append({"type": _SEG_TEXT, "text": report_text[last:]})

    # 标题边界切分: ### 标题必须是 text 段开头 — LLM 常在维度分析末段后直接写
    # "### 四、行动建议" (无图表引用分隔), 不切分会把标题渲染进上一段文本中间
    split: list[dict] = []
    for seg in segments:
        if seg["type"] != _SEG_TEXT:
            split.append(seg)
            continue
        text = seg.get("text", "")
        parts = re.split(r"(?=\n### )", text)
        for part in parts:
            part = part.lstrip("\n")  # 标题段去掉前导换行 (文本段开头干净)
            if part.strip():
                split.append({"type": _SEG_TEXT, "text": part})
    segments = split

    # C. 末尾兜底: 未被内联的图表附加到末尾 (引用格式漂移/漏引用也不丢图)
    tail = [c for c in pool if c["chart_id"] not in used_ids]
    if tail:
        if segments and segments[-1]["type"] == _SEG_TEXT:
            segments[-1]["text"] = (segments[-1]["text"] or "") + "\n\n**图表**"
        else:
            segments.append({"type": _SEG_TEXT, "text": "\n\n**图表**"})
        for c in tail:
            segments.append({
                "type": _SEG_CHART,
                "title": c.get("title", ""),
                "chart_type": c.get("chart_type", ""),
                "echarts_option": c.get("echarts_option") or {},
            })
    return segments


def extract_referenced_ids(report_text: str) -> list[int]:
    """提取报告中引用的图表编号 (用于 Reporter 校验: 引用必须合法).

    支持多编号引用 "(见图: 图9, 图10)" → [9, 10].
    """
    ids = []
    for m in _ANCHOR_RE.finditer(report_text):
        ids.extend(int(n) for n in re.findall(r"图(\d+)", m.group(1)))
    return ids
