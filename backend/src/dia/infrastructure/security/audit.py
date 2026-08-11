"""审计日志 -- 记录每次工具调用的完整信息

存储: storage/audit/audit.db (独立 SQLite)
格式: trace_id | timestamp | agent | tool | args_summary | result_summary | status | duration_ms
"""

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from dia.core.config import settings

logger = logging.getLogger(__name__)

AUDIT_DIR = Path(settings.STORAGE_OUTPUT_DIR).parent / "audit"
DB_PATH = AUDIT_DIR / "audit.db"
_local = threading.local()


def _init() -> None:
    """确保数据库和表存在"""
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    db = _conn()
    db.execute(
        """CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL,
            timestamp REAL NOT NULL,
            agent TEXT NOT NULL,
            tool TEXT NOT NULL,
            args_json TEXT DEFAULT '',
            result_summary TEXT DEFAULT '',
            status TEXT DEFAULT 'success',
            duration_ms REAL DEFAULT 0
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_audit_trace ON audit_log(trace_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_audit_agent ON audit_log(agent)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(timestamp)")
    db.commit()


def _conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _local.conn.execute("PRAGMA journal_mode=WAL")
    return _local.conn


_init()


def log_tool_call(
    trace_id: str,
    agent: str,
    tool: str,
    args: dict,
    result: Any = None,
    status: str = "success",
    duration_ms: float = 0,
) -> None:
    """记录一次工具调用"""
    args_summary = _summarize(args, 200)
    result_summary = _summarize(result, 300)

    try:
        db = _conn()
        db.execute(
            """INSERT INTO audit_log (trace_id, timestamp, agent, tool, args_json, result_summary, status, duration_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (trace_id, time.time(), agent, tool, args_summary, result_summary, status, round(duration_ms, 1)),
        )
        db.commit()
    except Exception as e:
        logger.warning(f"[Audit] 写入失败: {e}")


def query_audit(trace_id: str = "", agent: str = "", limit: int = 100) -> list[dict]:
    """查询审计日志"""
    db = _conn()
    conditions = []
    params: list = []

    if trace_id:
        conditions.append("trace_id = ?")
        params.append(trace_id)
    if agent:
        conditions.append("agent = ?")
        params.append(agent)

    where = " AND ".join(conditions) if conditions else "1=1"
    query = f"SELECT trace_id, timestamp, agent, tool, args_json, result_summary, status, duration_ms FROM audit_log WHERE {where} ORDER BY id DESC LIMIT ?"
    params.append(limit)

    rows = db.execute(query, params).fetchall()
    return [
        {
            "trace_id": r[0],
            "timestamp": r[1],
            "agent": r[2],
            "tool": r[3],
            "args": r[4],
            "result": r[5],
            "status": r[6],
            "duration_ms": r[7],
        }
        for r in rows
    ]


def cleanup_old_audit(max_age_days: int = 7) -> int:
    """清理旧审计日志"""
    db = _conn()
    cutoff = time.time() - max_age_days * 86400
    cursor = db.execute("DELETE FROM audit_log WHERE timestamp < ?", (cutoff,))
    db.commit()
    return cursor.rowcount


def _summarize(obj: Any, max_len: int) -> str:
    """安全摘要,防止过大数据"""
    if obj is None:
        return ""
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = str(obj)
    if len(s) > max_len:
        s = s[:max_len - 3] + "..."
    return s
