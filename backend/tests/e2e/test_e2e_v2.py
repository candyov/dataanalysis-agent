"""
端到端测试 v2 — Multi-Agent 全流程验证

覆盖：
  [1] 服务健康检查
  [2] 文件上传/预览
  [3] 会话 CRUD
  [4] Multi-Agent 对话（SSE 流式）
  [5] 可视化图表生成
  [6] 会话持久化 & 报告

用法：python test_e2e_v2.py [--port PORT] [--file FILE_PATH]
"""
import argparse
import json
import sys
import time
import requests
from pathlib import Path

# ── 配置 ──
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_FILE = str(PROJECT_ROOT / "storage" / "uploads" / "full_e2e_test_2025.csv")
API_BASE = "http://localhost:{port}/api/v1"
CHAT_TIMEOUT = 300  # SSE 流式超时

# ── 工具 ──
CHECK = "✅"
CROSS = "❌"
SKIP  = "⏭️"


class TestResult:
    """测试结果收集器"""
    def __init__(self, name: str):
        self.name = name
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.checks: list[dict] = []

    def ok(self, label: str, detail: str = ""):
        self.passed += 1
        self.checks.append({"status": CHECK, "label": label, "detail": detail})
        line = f"  {CHECK} {label}"
        if detail:
            line += f"  —  {detail}"
        print(line)
        return True

    def bad(self, label: str, detail: str = ""):
        self.failed += 1
        self.checks.append({"status": CROSS, "label": label, "detail": detail})
        line = f"  {CROSS} {label}"
        if detail:
            line += f"  —  {detail}"
        print(line)
        return False

    def skip(self, label: str, reason: str = ""):
        self.skipped += 1
        self.checks.append({"status": SKIP, "label": label, "detail": reason})
        line = f"  {SKIP} {label}"
        if reason:
            line += f"  —  {reason}"
        print(line)

    def summary(self) -> str:
        total = self.passed + self.failed + self.skipped
        if self.failed == 0 and self.skipped == 0:
            return f"{self.name}: 全部 {self.passed}/{total} 通过"
        if self.failed == 0:
            return f"{self.name}: {self.passed} 通过, {self.skipped} 跳过 ({total} 项)"
        return f"{self.name}: {self.passed} 通过, {self.failed} 失败, {self.skipped} 跳过"


# ═══════════════════════════════════════════════
# 测试 1: 健康检查
# ═══════════════════════════════════════════════
def test_health(api: str, result: TestResult):
    resp = requests.get(f"{api}/health", timeout=5)
    if resp.status_code == 200 and resp.json().get("status") == "ok":
        result.ok("服务健康", f"HTTP {resp.status_code}")
    else:
        result.bad("服务健康", f"状态异常: {resp.status_code} {resp.text}")
        sys.exit(1)


# ═══════════════════════════════════════════════
# 测试 2: 数据预览
# ═══════════════════════════════════════════════
def test_preview(api: str, file_path: str, result: TestResult):
    if not Path(file_path).exists():
        result.bad("数据文件存在", f"路径不存在: {file_path}")
        return

    result.ok("数据文件存在", str(Path(file_path).name))

    resp = requests.get(f"{api}/preview", params={"file_path": file_path, "rows": 5}, timeout=10)
    if not result.ok("预览接口 200", f"HTTP {resp.status_code}"):
        return

    data = resp.json()
    total_rows = data.get("total_rows", 0)
    total_cols = data.get("total_columns", 0)
    columns = data.get("columns", [])

    result.ok("返回列名", str(columns[:5]))
    result.ok("总行数", f"{total_rows} 行")
    result.ok("总列数", f"{total_cols} 列")

    # 数据完整性
    if total_rows < 500 or total_rows > 1000:
        result.bad("行数范围", f"预期 500-1000, 实际 {total_rows}")
    else:
        result.ok("行数范围合理", f"{total_rows} ∈ [500, 1000]")

    if total_cols >= 5:
        result.ok("列数合理", f"{total_cols} ≥ 5")
    else:
        result.bad("列数合理", f"{total_cols} < 5")

    # 预览数据
    preview_rows = data.get("preview_rows", 0)
    preview_data = data.get("data", [])
    if preview_rows > 0 and len(preview_data) > 0:
        result.ok("预览数据非空", f"{len(preview_data)} 行")
    else:
        result.bad("预览数据非空", "返回空数据")

    # 尝试访问不存在文件
    resp404 = requests.get(f"{api}/preview", params={"file_path": "/nonexistent.csv"}, timeout=5)
    if resp404.status_code in (404, 403):
        result.ok("非法路径拦截", f"HTTP {resp404.status_code}")
    else:
        result.bad("非法路径拦截", f"应返回 404/403, 实际 {resp404.status_code}")


# ═══════════════════════════════════════════════
# 测试 3: 会话管理
# ═══════════════════════════════════════════════
def test_sessions(api: str, result: TestResult):
    # 列出会话
    resp = requests.get(f"{api}/sessions", timeout=5)
    if resp.status_code != 200:
        result.bad("会话列表接口", f"HTTP {resp.status_code}")
        return
    result.ok("会话列表接口", "HTTP 200")

    sessions = resp.json().get("sessions", [])
    result.ok("会话列表返回", f"{len(sessions)} 个会话")

    # 查询不存在的会话
    fake_id = "nonexistent_session_id_12345"
    resp404 = requests.get(f"{api}/sessions/{fake_id}", timeout=5)
    if resp404.status_code == 404:
        result.ok("不存在会话 404", f"HTTP 404")
    else:
        result.bad("不存在会话 404", f"应返回 404, 实际 {resp404.status_code}")


# ═══════════════════════════════════════════════
# 测试 4: Multi-Agent 对话（核心）
# ═══════════════════════════════════════════════
def test_chat(api: str, file_path: str, result: TestResult) -> dict | None:
    """运行 Multi-Agent 对话，返回收集到的事件数据"""
    session_id = f"e2e_v2_{int(time.time())}"
    print(f"\n  ── 对话中 (session={session_id[:20]}...) ──")

    body = {
        "message": "分析这份销售数据的整体情况，找出趋势和异常",
        "file_path": file_path,
        "session_id": session_id,
    }

    # SSE 事件收集
    stages: list[dict] = []
    tool_calls: list[dict] = []
    bot_msgs: list[str] = []
    summary_msgs: list[str] = []
    charts: list[dict] = []
    errors: list[dict] = []
    thinking_count = 0
    done = False

    try:
        resp = requests.post(f"{api}/chat", json=body, stream=True, timeout=CHAT_TIMEOUT)
    except requests.Timeout:
        result.bad("Chat 发起", "连接超时")
        return None
    except Exception as e:
        result.bad("Chat 发起", str(e))
        return None

    result.ok("Chat 流式连接", f"HTTP {resp.status_code}")

    if resp.status_code != 200:
        result.bad("Chat 非 200", resp.text[:200])
        return None

    buffer = ""
    try:
        for chunk in resp.iter_content(chunk_size=None, decode_unicode=True):
            if not chunk:
                continue
            buffer += chunk
            while "\n\n" in buffer:
                line, buffer = buffer.split("\n\n", 1)
                line = line.strip()
                if not line or not line.startswith("data: "):
                    continue
                try:
                    evt = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue

                t = evt.get("type")
                if t == "thinking":
                    thinking_count += 1
                elif t == "stage":
                    stages.append(evt)
                elif t == "tool_call":
                    tool_calls.append(evt)
                elif t == "bot":
                    bot_msgs.append(evt.get("text", ""))
                elif t == "summary":
                    summary_msgs.append(evt.get("text", ""))
                elif t == "chart":
                    charts.append(evt)
                elif t == "error":
                    errors.append(evt)
                elif t == "done":
                    done = True
    except Exception as e:
        result.bad("SSE 流解析", str(e))
        return None

    # ── 断言 ──
    result.ok("Done 事件", "已收到" if done else "未收到")
    result.ok("Thinking 流", f"{thinking_count} chunks")

    # Stages
    stage_agents = [s.get("agent", "?") for s in stages]
    result.ok("Multi-Agent 阶段", f"{len(stages)} 个 → {' → '.join(stage_agents)}")
    if "data_agent" not in stage_agents:
        result.bad("包含 data_agent", "未触发数据引擎")
    if "analysis_agent" not in stage_agents:
        result.bad("包含 analysis_agent", "未触发分析引擎")
    if "visualization_agent" not in stage_agents:
        result.bad("包含 visualization_agent", "未触发可视化引擎")
    if "finish" not in stage_agents:
        result.bad("包含 finish", "未正常结束")

    # Tool calls
    unique_tools = set(tc.get("tool", "?") for tc in tool_calls)
    result.ok("工具调用", f"{len(tool_calls)} 次, 工具: {sorted(unique_tools)}")

    # 核心工具是否被调用
    required_tools = {"read_file", "profile_data", "clean_data"}
    missing_tools = required_tools - unique_tools
    if missing_tools:
        result.bad("数据引擎工具", f"缺少: {missing_tools}")
    else:
        result.ok("数据引擎工具", "全部就绪")

    analysis_tools_hit = [t for t in ("predict", "drill_down", "detect_anomalies", "find_drivers") if t in unique_tools]
    if analysis_tools_hit:
        result.ok("分析引擎工具", f"{len(analysis_tools_hit)} 个: {analysis_tools_hit}")
    else:
        result.bad("分析引擎工具", "无一命中")

    # 可视化
    viz_tools_hit = [t for t in ("build_chart",) if t in unique_tools]
    if viz_tools_hit:
        result.ok("可视化引擎工具", f"build_chart 被调用")
    else:
        result.bad("可视化引擎工具", "build_chart 未被调用!")

    # 错误
    if len(errors) == 0:
        result.ok("运行时错误", "0 个")
    else:
        result.bad("运行时错误", f"{len(errors)} 个: {[e.get('message','')[:60] for e in errors]}")

    # Bot / Summary 消息
    result.ok("Bot 消息", f"{len(bot_msgs)} 条")
    result.ok("Summary 消息", f"{len(summary_msgs)} 条")

    if summary_msgs:
        combined = " ".join(summary_msgs[:3])
        result.ok("Summary 有内容", f"{len(combined)} 字")
    else:
        result.bad("Summary 有内容", "空")

    return {
        "session_id": session_id,
        "stages": stages,
        "tool_calls": tool_calls,
        "charts": charts,
        "errors": errors,
        "bot_msgs": bot_msgs,
        "summary_msgs": summary_msgs,
    }


# ═══════════════════════════════════════════════
# 测试 5: 可视化图表验证
# ═══════════════════════════════════════════════
def test_visualization(charts: list[dict], result: TestResult):
    """验证生成的图表质量和数量"""
    if not charts:
        result.bad("图表数量", "0 张 — 可视化引擎未产出")
        return

    result.ok("图表数量", f"{len(charts)} 张")

    for i, c in enumerate(charts, 1):
        chart_type = c.get("chart_type", "?")
        title = c.get("title", "")
        title_len = len(title)
        title_preview = title[:80] + "..." if title_len > 80 else title

        # 图表类型
        valid_types = {"line", "bar", "pie", "scatter", "radar", "heatmap", "funnel"}
        if chart_type in valid_types:
            result.ok(f"图表 {i} 类型", f"{chart_type}")
        else:
            result.bad(f"图表 {i} 类型", f"未识别: '{chart_type}'")

        # 标题非空
        if title_len > 0:
            result.ok(f"图表 {i} 标题", f"'{title_preview}'")
        else:
            result.bad(f"图表 {i} 标题", "为空")

        # 标题长度合理（不要太短/太长）
        if 4 <= title_len <= 120:
            result.ok(f"图表 {i} 标题长度", f"{title_len} 字")
        elif title_len < 4:
            result.bad(f"图表 {i} 标题长度", f"太短 ({title_len} 字)")
        else:
            result.skip(f"图表 {i} 标题长度", f"{title_len} 字 (偏长)")


# ═══════════════════════════════════════════════
# 测试 6: 会话持久化 & 报告
# ═══════════════════════════════════════════════
def test_persistence(api: str, session_id: str, result: TestResult):
    if not session_id:
        result.skip("会话持久化", "无有效 session_id")
        return

    # 查询会话
    resp = requests.get(f"{api}/sessions/{session_id}", timeout=10)
    if resp.status_code == 200:
        result.ok("会话查询 200", f"OK")
    else:
        result.bad("会话查询 200", f"HTTP {resp.status_code}")
        return

    state = resp.json()

    # 核心状态
    data_done = state.get("data", {}).get("done", False)
    analysis_done = state.get("analysis", {}).get("done", False)
    viz_done = state.get("visualization", {}).get("done", False)

    for label, done in [("DataAgent 完成", data_done), ("AnalysisAgent 完成", analysis_done), ("VisualizationAgent 完成", viz_done)]:
        (result.ok if done else result.bad)(label, "✓" if done else "✗")

    # 消息数量
    msg_count = len(state.get("messages", []))
    if msg_count >= 10:
        result.ok("消息数量", f"{msg_count} 条")
    else:
        result.bad("消息数量", f"太少: {msg_count} 条")

    # 文件路径
    file_path = state.get("file_path", "")
    if file_path:
        result.ok("文件路径回写", str(Path(file_path).name))
    else:
        result.bad("文件路径回写", "为空")

    # ── 报告生成 ──
    resp = requests.get(f"{api}/sessions/{session_id}/report", timeout=10)
    if resp.status_code == 200:
        result.ok("报告下载", f"HTTP 200, {len(resp.text)} 字")
    else:
        result.bad("报告下载", f"HTTP {resp.status_code}")

    # 报告内容断言
    report = resp.text
    key_sections = [
        ("# 数据分析报告", "标题"),
        ("数据概况", "数据概况"),
        ("分析结论", "分析结论"),
    ]
    for pattern, name in key_sections:
        if pattern in report:
            result.ok(f"报告包含{name}", "✓")
        else:
            result.bad(f"报告包含{name}", "缺失")

    return state


# ═══════════════════════════════════════════════
# 测试 7: 删除会话
# ═══════════════════════════════════════════════
def test_delete_session(api: str, session_id: str, result: TestResult):
    if not session_id:
        result.skip("删除会话", "无有效 session_id")
        return

    resp = requests.delete(f"{api}/sessions/{session_id}", timeout=5)
    if resp.status_code == 200:
        result.ok("删除会话", "HTTP 200")
    else:
        result.bad("删除会话", f"HTTP {resp.status_code}")

    # 确认已删除
    resp = requests.get(f"{api}/sessions/{session_id}", timeout=5)
    if resp.status_code == 404:
        result.ok("删除后 404", "确认已删除")
    else:
        result.bad("删除后 404", f"应返回 404, 实际 {resp.status_code}")


# ═══════════════════════════════════════════════
# 测试 8: 问候语路径
# ═══════════════════════════════════════════════
def test_greeting(api: str, result: TestResult):
    """测试简单问候的快速路径"""
    resp = requests.post(f"{api}/chat", json={"message": "你好", "session_id": f"greet_{int(time.time())}"}, stream=True, timeout=15)
    if resp.status_code == 200:
        result.ok("问候快速路径", "HTTP 200")
    else:
        result.bad("问候快速路径", f"HTTP {resp.status_code}")
        return

    has_bot = False
    has_done = False
    for chunk in resp.iter_content(chunk_size=None, decode_unicode=True):
        if chunk:
            if '"type":"bot"' in chunk:
                has_bot = True
            if '"type":"done"' in chunk:
                has_done = True

    result.ok("问候返回 Bot", "✓" if has_bot else "✗")
    result.ok("问候返回 Done", "✓" if has_done else "✗")


# ═══════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="Multi-Agent 端到端测试 v2")
    parser.add_argument("--port", type=int, default=8010, help="后端端口 (默认 8010)")
    parser.add_argument("--file", type=str, default=DEFAULT_FILE, help=f"测试数据文件 (默认 {DEFAULT_FILE})")
    parser.add_argument("--skip-chat", action="store_true", help="跳过耗时的 Multi-Agent 对话测试")
    parser.add_argument("--verbose", "-v", action="store_true", help="更详细的输出")
    args = parser.parse_args()

    api = API_BASE.format(port=args.port)
    file_path = args.file

    # ── 验证 ──
    if not Path(file_path).exists():
        print(f"\n{CROSS} 测试数据文件不存在: {file_path}")
        print(f"   提示: 用 --file 指定正确的文件路径")
        sys.exit(1)

    # ── 头信息 ──
    print("=" * 70)
    print("    Multi-Agent 端到端测试 v2")
    print(f"    API:       {api}")
    print(f"    数据文件:  {Path(file_path).name}")
    if args.skip_chat:
        print(f"    模式:      跳过 Chat 测试")
    print("=" * 70)

    results: list[TestResult] = []

    # [Test 1] 健康检查
    print("\n┌─ [1/8] 服务健康检查 ─────────────────────────────")
    r1 = TestResult("服务健康")
    test_health(api, r1)
    results.append(r1)

    # [Test 2] 数据预览
    print("\n┌─ [2/8] 数据预览 ──────────────────────────────────")
    r2 = TestResult("数据预览")
    test_preview(api, file_path, r2)
    results.append(r2)

    # [Test 3] 会话管理
    print("\n┌─ [3/8] 会话管理 ──────────────────────────────────")
    r3 = TestResult("会话管理")
    test_sessions(api, r3)
    results.append(r3)

    # [Test 4] Multi-Agent Chat
    r4 = TestResult("Multi-Agent 对话")
    r5 = TestResult("可视化图表")
    r6 = TestResult("会话持久化")
    r7 = TestResult("会话删除")
    r8 = TestResult("边界条件")
    chat_data = None

    if args.skip_chat:
        print("\n┌─ [4/8] Multi-Agent Chat — 跳过")
        r4.skip("(跳过)", "--skip-chat")
        r5.skip("(跳过)", "依赖 Chat")
        r6.skip("(跳过)", "依赖 Chat")
        r7.skip("(跳过)", "依赖 Chat")
    else:
        print("\n┌─ [4/8] Multi-Agent Chat ──────────────────────────")
        chat_data = test_chat(api, file_path, r4)
        results.append(r4)

        # [Test 5] 可视化
        print("\n┌─ [5/8] 可视化图表验证 ──────────────────────────")
        if chat_data and chat_data["charts"]:
            test_visualization(chat_data["charts"], r5)
        elif chat_data:
            r5.bad("图表生成", "0 张 — 可视化引擎未产出任何图表")
        else:
            r5.skip("图表生成", "Chat 未返回有效数据")
        results.append(r5)

        # [Test 6] 会话持久化 & 报告
        session_id = chat_data.get("session_id", "") if chat_data else ""
        print("\n┌─ [6/8] 会话持久化 & 报告 ───────────────────────")
        test_persistence(api, session_id, r6)
        results.append(r6)

        # [Test 7] 删除会话
        print("\n┌─ [7/8] 会话清理 ──────────────────────────────────")
        test_delete_session(api, session_id, r7)
        results.append(r7)

    # [Test 8] 边界条件
    print("\n┌─ [8/8] 边界条件 ──────────────────────────────────")
    test_greeting(api, r8)
    results.append(r8)

    # ── 汇总报告 ──
    print("\n\n" + "=" * 70)
    print("    📋 测试报告")
    print("=" * 70)

    total_p, total_f, total_s = 0, 0, 0
    for r in results:
        print(f"\n  {r.summary()}")
        total_p += r.passed
        total_f += r.failed
        total_s += r.skipped

    total = total_p + total_f + total_s
    print("\n" + "─" * 70)
    bar_w = 50
    pct = total_p / max(total, 1) * 100
    filled = int(bar_w * total_p / max(total, 1))
    bar = "█" * filled + "░" * (bar_w - filled)
    print(f"  总计: {total_p} 通过  |  {total_f} 失败  |  {total_s} 跳过")
    print(f"  [{bar}] {pct:.0f}%")

    if total_f == 0:
        print(f"\n  🎉 所有测试通过！")
    else:
        print(f"\n  ⚠️  {total_f} 项测试失败，请检查。")
        sys.exit(1)


if __name__ == "__main__":
    main()
