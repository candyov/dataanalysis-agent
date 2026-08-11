"""Glossary 持久化缓存 + 历史结论 — 跨会话复用 Curator 探查结果 + 记忆上次分析

背景:
  glossary (语义层) 原本只存活于会话内 shared_context, 换会话后同数据源
  再次分析要重跑 Curator (LLM 成本高)。本模块把 glossary + KPI + 探查报告
  按 source_id 落盘, Supervisor 新会话命中新鲜缓存 → 跳过 Curator 直接复用。

两套数据 (同库不同表):
  1. glossary_cache 表 — 探查语义层 (glossary/KPI/curator_report), 有新鲜度 TTL
  2. analysis_history 表 — 历史分析结论 (问题 + 核心结论摘要), 累积最近 N 条,
     Supervisor 注入 Analyst context 让 LLM 知道"上次分析过什么" (仅背景参考)

生命周期:
  - 写: Curator 探查完成后 UPSERT glossary_cache (data_curator.py);
        chat.py 保存 session 时 append_history
  - 读: Supervisor 规划时查缓存 (source_id 匹配 + 新鲜度) / load_history
  - 过期: GLOSSARY_CACHE_TTL 秒后视为陈旧, 重新探查 (防数据变更后旧口径误导)

与 metric_store 的关系: metric_store 存 KPI 数值快照 (基线), 本模块存语义元数据
(列角色/类型/口径) + 历史结论。互补, 无重叠。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from dia.core.config import settings

logger = logging.getLogger(__name__)

DB_PATH = Path(settings.STORAGE_DIR) / "glossary_cache.db"
# 缓存新鲜度: 超过该秒数 → 视为陈旧, 重新探查 (默认 7 天)
GLOSSARY_CACHE_TTL = getattr(settings, "GLOSSARY_CACHE_TTL", 7 * 24 * 3600)

_local = threading.local()


def _conn() -> sqlite3.Connection:
    """线程本地连接 (与 sessions.py 同模式)."""
    if not hasattr(_local, "conn") or _local.conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _local.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _local.conn.execute("PRAGMA journal_mode=WAL")
    return _local.conn


def _init_db() -> None:
    db = _conn()
    db.execute(
        """CREATE TABLE IF NOT EXISTS glossary_cache (
            source_id TEXT PRIMARY KEY,
            glossary_json TEXT NOT NULL,
            kpis_json TEXT NOT NULL,
            curator_report_json TEXT NOT NULL,
            updated_at REAL NOT NULL
        )"""
    )
    # 历史结论: 独立表 (source_id 键控, 累积最近 N 次分析结论) —
    # 不并入 glossary_cache 行, 避免 INSERT OR REPLACE 整行覆盖时丢 history
    db.execute(
        """CREATE TABLE IF NOT EXISTS analysis_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL,
            question TEXT DEFAULT '',
            conclusion TEXT NOT NULL,
            created_at REAL NOT NULL
        )"""
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_history_source ON analysis_history(source_id, created_at)"
    )
    db.commit()


_init_db()


def save_glossary_cache(
    source_id: str,
    glossary: dict,
    kpis: list,
    curator_report: dict,
) -> None:
    """UPSERT 探查缓存 (source_id 为键, 覆盖旧条目)."""
    if not source_id:
        return
    db = _conn()
    db.execute(
        """INSERT OR REPLACE INTO glossary_cache
           (source_id, glossary_json, kpis_json, curator_report_json, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            source_id,
            json.dumps(glossary, ensure_ascii=False),
            json.dumps(kpis, ensure_ascii=False),
            json.dumps(curator_report, ensure_ascii=False),
            time.time(),
        ),
    )
    db.commit()
    logger.info(f"[GlossaryCache] 已缓存 {source_id} ({len(glossary)} 列, {len(kpis)} KPI)")


def load_glossary_cache(source_id: str) -> dict | None:
    """按 source_id 加载缓存, 返回 {glossary, kpis, curator_report, updated_at} 或 None.

    不校验新鲜度 — 调用方 (Supervisor) 自行判断, 以便把"陈旧"作为提示注入而非静默丢弃.
    """
    if not source_id:
        return None
    db = _conn()
    row = db.execute(
        "SELECT glossary_json, kpis_json, curator_report_json, updated_at "
        "FROM glossary_cache WHERE source_id = ?",
        (source_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        return {
            "glossary": json.loads(row[0]),
            "kpis": json.loads(row[1]),
            "curator_report": json.loads(row[2]),
            "updated_at": row[3],
        }
    except json.JSONDecodeError:
        logger.warning(f"[GlossaryCache] {source_id} 缓存损坏, 忽略")
        return None


def is_fresh(cache: dict | None, now: float | None = None) -> bool:
    """缓存是否新鲜 (在 TTL 内)."""
    if not cache:
        return False
    now = now or time.time()
    return (now - cache.get("updated_at", 0)) < GLOSSARY_CACHE_TTL


def clear_glossary_cache(source_id: str | None = None) -> int:
    """清除缓存 (全部或单个 source_id), 返回删除条数."""
    db = _conn()
    if source_id:
        cur = db.execute("DELETE FROM glossary_cache WHERE source_id = ?", (source_id,))
    else:
        cur = db.execute("DELETE FROM glossary_cache")
    db.commit()
    return cur.rowcount


# ═══════════════════════════════════════════════
# 历史结论 — 按 source_id 累积最近 N 次分析结论
# ═══════════════════════════════════════════════

# 每 source_id 保留的历史条数上限
HISTORY_LIMIT = 3


def append_history(source_id: str, conclusion: str, question: str = "") -> None:
    """追加一条历史分析结论 (按 source_id), 超出上限裁掉最旧."""
    if not source_id or not conclusion:
        return
    db = _conn()
    db.execute(
        "INSERT INTO analysis_history (source_id, question, conclusion, created_at) "
        "VALUES (?, ?, ?, ?)",
        (source_id, question, conclusion.strip()[:600], time.time()),
    )
    # 只保留最近 HISTORY_LIMIT 条
    db.execute(
        "DELETE FROM analysis_history WHERE source_id = ? AND id NOT IN "
        "(SELECT id FROM analysis_history WHERE source_id = ? "
        " ORDER BY created_at DESC, id DESC LIMIT ?)",
        (source_id, source_id, HISTORY_LIMIT),
    )
    db.commit()
    logger.info(f"[GlossaryCache] 已追加历史结论 ({source_id}, 保留最近 {HISTORY_LIMIT} 条)")


def load_history(source_id: str, limit: int = HISTORY_LIMIT) -> list[dict]:
    """按 source_id 读取历史分析结论 (新的在前), 返回 [{question, conclusion, created_at}]."""
    if not source_id:
        return []
    db = _conn()
    rows = db.execute(
        "SELECT question, conclusion, created_at FROM analysis_history "
        "WHERE source_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
        (source_id, limit),
    ).fetchall()
    return [
        {"question": r[0], "conclusion": r[1], "created_at": r[2]}
        for r in rows
    ]


def clear_history(source_id: str | None = None) -> int:
    """清除历史结论 (全部或单个 source_id), 返回删除条数."""
    db = _conn()
    if source_id:
        cur = db.execute("DELETE FROM analysis_history WHERE source_id = ?", (source_id,))
    else:
        cur = db.execute("DELETE FROM analysis_history")
    db.commit()
    return cur.rowcount
