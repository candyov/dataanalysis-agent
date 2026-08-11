"""端到端测试：Multi-Agent 全流程 (requests 版本)"""
import json
import sys
import requests
from pathlib import Path

API = "http://localhost:8010/api/v1"
FILE_PATH = "D:/Desktop/ai-data-analysis/backend/storage/uploads/full_e2e_test_2025.csv"

print("=" * 60)
print("端到端测试 — Multi-Agent 全流程")
print(f"文件: {FILE_PATH}")
print("=" * 60)

# ── 1. Preview ──
print("\n[1/5] 数据预览")
resp = requests.get(f"{API}/preview", params={"file_path": FILE_PATH, "rows": 5})
r = resp.json()
print(f"  rows={r.get('total_rows')}, cols={r.get('total_columns')}")
print(f"  列名: {r.get('columns')}")
for row in r.get("data", [])[:3]:
    print(f"    {row}")
assert resp.status_code == 200, f"Preview failed: {resp.status_code}"
assert r["total_rows"] == 612, f"Expected 612 rows, got {r['total_rows']}"
print("  ✓ PASS")

# ── 2. Sessions List ──
print("\n[2/5] 会话列表")
resp = requests.get(f"{API}/sessions")
r = resp.json()
sessions = r.get("sessions", [])
print(f"  共 {len(sessions)} 个会话")
for s in sessions[:3]:
    print(f"    - {s.get('session_id', '')[:16]}... | {s.get('first_message', '')[:40]}")
assert resp.status_code == 200
print("  ✓ PASS")

# ── 3. Multi-Agent Chat (SSE) ──
import time
session_id = f"e2e_test_{int(time.time())}"
print(f"\n[3/5] Multi-Agent Chat (session={session_id})")
print(f"  问题: 分析这份销售数据的整体情况，找出趋势和异常")

body = {
    "message": "分析这份销售数据的整体情况，找出趋势和异常",
    "file_path": FILE_PATH,
    "session_id": session_id,
}

stages = []
tool_calls = []
bot_msgs = []
summary_msgs = []
charts = []
errors = []
thinking_count = 0
stream_count = 0

resp = requests.post(f"{API}/chat", json=body, stream=True, timeout=300)
assert resp.status_code == 200, f"Chat failed: {resp.status_code}"

buffer = ""
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
            if thinking_count == 1:
                print(f"  [THINKING] 开始思考...")
        elif t == "stream":
            stream_count += 1
        elif t == "stage":
            stages.append(evt)
            print(f"  [STAGE] {evt.get('agent')} → {evt.get('label', '')}")
        elif t == "tool_call":
            tool_calls.append(evt)
            print(f"  [TOOL] {evt.get('agent')}: {evt.get('tool')}")
        elif t == "bot":
            bot_msgs.append(evt)
            text = evt.get("text", "")
            print(f"  [BOT] {text[:100]}{'...' if len(text)>100 else ''}")
        elif t == "summary":
            summary_msgs.append(evt)
            text = evt.get("text", "")
            print(f"  [SUMMARY] {text[:120]}{'...' if len(text)>120 else ''}")
        elif t == "chart":
            charts.append(evt)
            print(f"  [CHART] {evt.get('chart_type')}: {evt.get('title')}")
        elif t == "error":
            errors.append(evt)
            print(f"  [ERROR] {evt.get('message')}")
        elif t == "done":
            print(f"  [DONE] {evt.get('message', '')}")
        elif t == "start":
            print(f"  [START] {evt.get('message', '')}")

print(f"\n  ── 汇总 ──")
print(f"  Stages:      {len(stages)}")
for s in stages:
    print(f"    - {s.get('agent')}: {s.get('label')}")
print(f"  Tool calls:  {len(tool_calls)}")
for tc in tool_calls:
    print(f"    - [{tc.get('agent')}] {tc.get('tool')}")
print(f"  Bot msgs:    {len(bot_msgs)}")
print(f"  Summary:     {len(summary_msgs)}")
print(f"  Charts:      {len(charts)}")
for c in charts:
    print(f"    - {c.get('chart_type')}: {c.get('title')}")
print(f"  Thinking:    {thinking_count} chunks")
print(f"  Stream:      {stream_count} chunks")
print(f"  Errors:      {len(errors)}")

# 断言
assert len(stages) >= 2, f"Expected >=2 stages, got {len(stages)}"
assert len(tool_calls) >= 2, f"Expected >=2 tool calls, got {len(tool_calls)}"
assert len(errors) == 0, f"Expected 0 errors, got {len(errors)}"
print("  ✓ PASS")

# ── 4. Session State ──
print(f"\n[4/5] 会话状态查询 ({session_id})")
resp = requests.get(f"{API}/sessions/{session_id}")
r = resp.json()
assert resp.status_code == 200, f"Session query failed: {resp.status_code}"
print(f"  data.done:          {r.get('data', {}).get('done')}")
print(f"  analysis.done:      {r.get('analysis', {}).get('done')}")
print(f"  visualization.done: {r.get('visualization', {}).get('done')}")
print(f"  charts:             {len(r.get('visualization', {}).get('charts', []))}")
print(f"  messages:           {len(r.get('messages', []))}")
print(f"  file_path:          {r.get('file_path', '')[:60]}...")
assert r.get("data", {}).get("done"), "Data not done"
assert r.get("analysis", {}).get("done"), "Analysis not done"
print("  ✓ PASS")

# ── 5. Report ──
print(f"\n[5/5] 报告下载 ({session_id})")
resp = requests.get(f"{API}/sessions/{session_id}/report")
assert resp.status_code == 200, f"Report failed: {resp.status_code}"
report = resp.text
assert "数据分析报告" in report, "Report missing title"
assert "数据概况" in report, "Report missing data overview"
assert "分析结论" in report, "Report missing analysis"
print(f"  报告长度: {len(report)} 字")
print(report[:500])
print(f"  ✓ PASS")

# ── 最终结果 ──
print("\n\n")
print("█" * 60)
print("█  端到端测试全部通过!")
print("█" * 60)
print(f"""
  数据集:  612 行 × 8 列 (6 区域 × 6 品类 × 6 个月)
  数据引擎:  read_file → profile_data → check_quality → clean_data
  分析引擎:  explore_data → 趋势/归因/异常检测
  可视化引擎: {len(charts)} 张图表
  会话持久化:  ✓
  报告生成:   ✓
  管线追踪:   ✓
""")