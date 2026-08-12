"""导出会话报告为静态演示 HTML (与聊天流 1:1: Reporter segments 文字 + 图表内联).

用法: python scripts/export_demo_html.py <session_id> [输出路径]
默认输出: 仓库根 docs/index.html (GitHub Pages 演示页)
"""
import json
import sqlite3
import sys
from pathlib import Path

import markdown

BACKEND = Path(__file__).resolve().parent.parent
DB_PATH = BACKEND / "storage" / "sessions.db"
OUT_DEFAULT = BACKEND.parent / "docs" / "index.html"

CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:'PingFang SC','Microsoft YaHei',sans-serif; background:#f5f7fa; color:#1e293b; padding:24px; line-height:1.7; }
.container { max-width:960px; margin:0 auto; }
.report-header { background:linear-gradient(135deg,#1e40af,#3b82f6); color:#fff; border-radius:14px; padding:24px 32px; margin-bottom:20px; }
.report-header h1 { font-size:22px; margin-bottom:6px; }
.report-header .meta { font-size:13px; opacity:.85; }
.section { background:#fff; border-radius:12px; padding:22px 26px; margin-bottom:18px; box-shadow:0 1px 3px rgba(0,0,0,.08); }
.chart-box { margin-top:16px; }
.chart-box h4 { font-size:14px; color:#475569; margin-bottom:8px; }
.chart-wrapper { height:320px; }
.report-footer { text-align:center; padding:18px; color:#94a3b8; font-size:13px; }
.section table { border-collapse:collapse; width:100%; margin:12px 0; font-size:14px; }
.section th, .section td { border:1px solid #e2e8f0; padding:8px 12px; text-align:left; }
.section th { background:#f1f5f9; }
.section ul, .section ol { padding-left:24px; margin:8px 0; }
.section h1 { font-size:20px; margin:14px 0 8px; }
.section h2 { font-size:18px; margin:14px 0 8px; }
.section h3 { font-size:16px; margin:14px 0 8px; }
.section h4 { font-size:14px; margin:12px 0 6px; }
.section p { margin:8px 0; }
"""


def load_segments(session_id: str) -> tuple[list, dict]:
    conn = sqlite3.connect(str(DB_PATH))
    row = conn.execute(
        "SELECT state_json FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    conn.close()
    if row is None:
        raise SystemExit(f"会话不存在: {session_id}")
    st = json.loads(row[0])
    segs = (st.get("reporter") or {}).get("segments") or []
    user_request = st.get("user_request", "数据分析报告")
    return segs, {"request": user_request, "session_id": session_id}


def render(segs: list, meta: dict) -> str:
    md = markdown.Markdown(extensions=["tables", "sane_lists"])
    parts = [f"<!DOCTYPE html><html lang='zh-CN'><head><meta charset='UTF-8'>",
             f"<title>{meta['request'][:40]}</title>",
             "<script src='https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js'></script>",
             f"<style>{CSS}</style></head><body><div class='container'>",
             f"<div class='report-header'><h1>{meta['request']}</h1>",
             f"<div class='meta'>数据源: analysis_test · 会话 {meta['session_id'][:20]}</div></div>"]

    js_parts = []
    chart_no = 0
    for s in segs:
        if s.get("type") == "chart":
            chart_no += 1
            cid = f"c{chart_no}"
            parts.append(
                f"<div class='chart-box'><h4>{s.get('title','')}</h4>"
                f"<div id='{cid}' class='chart-wrapper'></div></div>")
            eo = json.dumps(s.get("echarts_option") or {}, ensure_ascii=False)
            js_parts.append(
                f"var el=document.getElementById('{cid}');if(el){{var ch=echarts.init(el);"
                f"ch.setOption({eo});window.addEventListener('resize',function(){{ch.resize();}});}}")
        else:
            text = s.get("text", "")
            if text.strip():
                html = md.convert(text)
                parts.append(f"<div class='section'>{html}</div>")

    parts.append("<div class='report-footer'>由 AI 数据分析平台自动生成</div></div>")
    parts.append(f"<script>{''.join(js_parts)}</script></body></html>")
    return "".join(parts)


def main():
    session_id = sys.argv[1] if len(sys.argv) > 1 else None
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else OUT_DEFAULT
    if not session_id:
        # 默认取最近会话
        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute("SELECT session_id FROM sessions ORDER BY last_access DESC LIMIT 1").fetchone()
        conn.close()
        if row is None:
            raise SystemExit("无会话可导出")
        session_id = row[0]
    segs, meta = load_segments(session_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(segs, meta), encoding="utf-8")
    print(f"导出完成: {out} ({out.stat().st_size} bytes, {len(segs)} 段, 会话 {session_id})")


if __name__ == "__main__":
    main()
