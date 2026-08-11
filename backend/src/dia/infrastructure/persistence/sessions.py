"""会话持久化 -- SQLite 存储,跨重启保留 Agent 状态."""
import json
import logging
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from dia.core.config import settings

logger = logging.getLogger(__name__)

# 会话库放 STORAGE_DIR 根 (与 checkpoints.db 同级), 绝不放 STORAGE_OUTPUT_DIR —
# 后者被 /output 静态挂载 (main.py), 旧路径会暴露全部会话数据
DB_PATH = Path(settings.STORAGE_DIR) / "sessions.db"
_LEGACY_DB_PATH = Path(settings.STORAGE_OUTPUT_DIR) / "sessions.db"
SESSION_TTL = settings.SESSION_TTL
_local = threading.local()


def _migrate_legacy_db() -> None:
    """旧版 sessions.db 在 STORAGE_OUTPUT_DIR 下 (被 /output 挂载暴露) → 迁移到新路径.

    仅在服务启动时执行一次: 新路径无有效数据时, 移动旧库及其 WAL/SHM 到新路径.
    """
    try:
        if not _LEGACY_DB_PATH.exists():
            return
        # 新路径已有有效数据 → 不迁移 (避免覆盖), 只删旧库消除暴露面
        if DB_PATH.exists() and DB_PATH.stat().st_size > 0:
            _LEGACY_DB_PATH.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(str(_LEGACY_DB_PATH) + suffix).unlink(missing_ok=True)
            logger.info("[Session] 新路径已有会话库, 删除旧路径库 (消除 /output 暴露)")
            return
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        # 旧库 0 字节(空库) → 直接删除, 不迁移
        if _LEGACY_DB_PATH.stat().st_size == 0:
            _LEGACY_DB_PATH.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(str(_LEGACY_DB_PATH) + suffix).unlink(missing_ok=True)
            logger.info("[Session] 旧路径空库已删除")
            return
        shutil.move(str(_LEGACY_DB_PATH), str(DB_PATH))
        for suffix in ("-wal", "-shm"):
            legacy_side = Path(str(_LEGACY_DB_PATH) + suffix)
            if legacy_side.exists():
                shutil.move(str(legacy_side), str(DB_PATH) + suffix)
        logger.info(f"[Session] 会话库已迁移: {_LEGACY_DB_PATH} → {DB_PATH}")
    except Exception as e:
        logger.warning(f"[Session] 旧库迁移失败 (忽略): {e}")


def _conn() -> sqlite3.Connection:
    """线程本地连接."""
    if not hasattr(_local, "conn") or _local.conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _local.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA busy_timeout=5000")
    return _local.conn


_migrate_legacy_db()


def _init_db() -> None:
    db = _conn()
    db.execute(
        """CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            state_json TEXT NOT NULL,
            last_access REAL NOT NULL
        )"""
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_last_access ON sessions(last_access)"
    )
    db.commit()


_init_db()


def _init_traces_table() -> None:
    """初始化 trace 快照表"""
    db = _conn()
    db.execute(
        """CREATE TABLE IF NOT EXISTS traces (
            trace_id TEXT PRIMARY KEY,
            session_id TEXT,
            state_json TEXT NOT NULL,
            agent_path TEXT NOT NULL,
            created_at REAL NOT NULL
        )"""
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_traces_session ON traces(session_id)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_traces_created ON traces(created_at)"
    )
    db.commit()


_init_traces_table()


def get_session(session_id: str) -> dict | None:
    """获取会话状态,过期返回 None."""
    db = _conn()
    row = db.execute(
        "SELECT state_json, last_access FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    if time.time() - row[1] > SESSION_TTL:
        db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        db.commit()
        logger.info(f"[Session] {session_id[:8]} 已过期")
        return None
    # 刷新访问时间
    db.execute(
        "UPDATE sessions SET last_access = ? WHERE session_id = ?",
        (time.time(), session_id),
    )
    db.commit()
    state = json.loads(row[0])
    # 还原 LangChain 消息对象
    from langchain_core.messages import messages_from_dict
    if "messages" in state and isinstance(state["messages"], list):
        # Only deserialize if it's a dict list (serialized), not already messages
        if state["messages"] and isinstance(state["messages"][0], dict):
            state["messages"] = messages_from_dict(state["messages"])
    return state


def save_session(session_id: str, state: dict) -> None:
    """保存会话状态."""
    db = _conn()
    # 序列化消息对象
    serializable = dict(state)
    if "messages" in serializable and serializable["messages"]:
        from langchain_core.messages import messages_to_dict
        try:
            serializable["messages"] = messages_to_dict(serializable["messages"])
        except Exception:
            # Fallback for non-message items
            serializable["messages"] = [
                m.model_dump() if hasattr(m, "model_dump") else m
                for m in serializable["messages"]
            ]
    db.execute(
        """INSERT OR REPLACE INTO sessions (session_id, state_json, last_access)
           VALUES (?, ?, ?)""",
        (session_id, json.dumps(serializable, ensure_ascii=False), time.time()),
    )
    db.commit()
    msg_count = len(state.get("messages", []))
    logger.info(f"[Session] {session_id[:8]} 已保存 ({msg_count} 条消息)")


def delete_session(session_id: str) -> None:
    """删除会话."""
    db = _conn()
    db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
    db.commit()
    logger.info(f"[Session] {session_id[:8]} 已删除")


def cleanup_expired() -> int:
    """清理所有过期会话,返回清理数量."""
    db = _conn()
    cutoff = time.time() - SESSION_TTL
    cursor = db.execute("DELETE FROM sessions WHERE last_access < ?", (cutoff,))
    db.commit()
    count = cursor.rowcount
    if count:
        logger.info(f"[Session] 清理 {count} 个过期会话")
    return count


def list_sessions() -> list[dict]:
    """列出所有有效会话."""
    db = _conn()
    cutoff = time.time() - SESSION_TTL
    db.execute("DELETE FROM sessions WHERE last_access < ?", (cutoff,))
    db.commit()

    rows = db.execute(
        "SELECT session_id, state_json, last_access FROM sessions ORDER BY last_access DESC LIMIT 50"
    ).fetchall()

    result = []
    for sid, state_json, last_access in rows:
        try:
            state = json.loads(state_json)
        except json.JSONDecodeError:
            continue
        msgs = state.get("messages", [])
        first = ""
        for m in msgs:
            if isinstance(m, dict):
                t = m.get("type", "")
                content = ""
                if t == "human":
                    content = str(m.get("data", {}).get("content", "") if isinstance(m.get("data"), dict) else m.get("content", ""))
                    first = content[:40]
                    break
                elif t == "user":
                    first = str(m.get("content", ""))[:40]
                    break
            elif hasattr(m, "type") and m.type == "human":
                first = str(getattr(m, "content", ""))[:40]
                break
        result.append({
            "session_id": sid,
            "first_message": first or "(空会话)",
            "msg_count": len(msgs),
            "last_access": last_access,
        })
    return result


# ═══════════════════════════════════════════════
# Trace 快照 -- 请求级状态快照,用于调试和回放
# ═══════════════════════════════════════════════

def save_trace_snapshot(trace_id: str, session_id: str, state: dict, agent_path: str = "") -> None:
    """保存请求级 MultiAgentState 快照"""
    db = _conn()
    serializable = dict(state)
    if "messages" in serializable and serializable["messages"]:
        from langchain_core.messages import messages_to_dict
        try:
            serializable["messages"] = messages_to_dict(serializable["messages"])
        except Exception:
            serializable["messages"] = [
                m.model_dump() if hasattr(m, "model_dump") else m
                for m in serializable["messages"]
            ]
    db.execute(
        """INSERT OR REPLACE INTO traces (trace_id, session_id, state_json, agent_path, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (trace_id, session_id, json.dumps(serializable, ensure_ascii=False), agent_path, time.time()),
    )
    db.commit()
    logger.info(f"[Trace] 快照已保存: {trace_id} agent={agent_path}")


def get_trace_snapshot(trace_id: str) -> dict | None:
    """获取 trace 快照"""
    db = _conn()
    row = db.execute(
        "SELECT state_json, session_id, agent_path, created_at FROM traces WHERE trace_id = ?",
        (trace_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "trace_id": trace_id,
        "session_id": row[1],
        "agent_path": row[2],
        "created_at": row[3],
        "state": json.loads(row[0]),
    }


def list_traces(limit: int = 50) -> list[dict]:
    """列出最近的 trace 快照"""
    db = _conn()
    rows = db.execute(
        "SELECT trace_id, session_id, agent_path, created_at FROM traces ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {
            "trace_id": r[0],
            "session_id": r[1],
            "agent_path": r[2],
            "created_at": r[3],
        }
        for r in rows
    ]


def cleanup_old_traces(max_age_hours: int = 24) -> int:
    """清理旧 trace 快照"""
    db = _conn()
    cutoff = time.time() - max_age_hours * 3600
    cursor = db.execute("DELETE FROM traces WHERE created_at < ?", (cutoff,))
    db.commit()
    count = cursor.rowcount
    if count:
        logger.info(f"[Trace] 清理 {count} 个旧快照")
    return count
