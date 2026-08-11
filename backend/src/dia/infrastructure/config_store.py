"""动态配置存储 — 生产级: Fernet 加密 + 运行时热更新.

读取优先级: 动态配置 (app_settings 表) > 环境变量/.env > 代码默认.
敏感项 (API Key/密码) 加密落库, API 层只暴露"是否已设置".
"""
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet

from dia.core.config import settings

logger = logging.getLogger(__name__)

DB_PATH = Path(settings.STORAGE_DIR) / "settings.db"
KEY_FILE = Path(settings.STORAGE_DIR) / ".encryption_key"

# 敏感项: 值加密存储, get_all 只返回是否已设置
SENSITIVE_KEYS = {"LLM_API_KEY", "DATABASE_PASSWORD", "MODEL_PROFILES"}

_local = threading.local()


def _conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS app_settings ("
            "key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"
        )
        conn.commit()
        _local.conn = conn
    return _local.conn


def _fernet() -> Fernet:
    """加密密钥: DATA_ENCRYPTION_KEY 环境变量优先, 否则本地密钥文件 (自动生成, 重启不丢)."""
    key = os.environ.get("DATA_ENCRYPTION_KEY", "")
    if key:
        try:
            return Fernet(key.encode() if isinstance(key, str) else key)
        except Exception:
            logger.warning("DATA_ENCRYPTION_KEY 无效, 回退本地密钥文件")
    if KEY_FILE.exists():
        return Fernet(KEY_FILE.read_bytes())
    k = Fernet.generate_key()
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_bytes(k)
    try:
        os.chmod(KEY_FILE, 0o600)
    except OSError:
        pass
    logger.info(f"生成加密密钥: {KEY_FILE}")
    return Fernet(k)


def encrypt_value(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_value(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def get(key: str, default=None):
    """读动态配置 (敏感项自动解密; 解密失败视为未设置)."""
    row = _conn().execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    v = row[0]
    if key in SENSITIVE_KEYS:
        try:
            return decrypt_value(v)
        except Exception:
            logger.warning(f"配置项 {key} 解密失败 (密钥变更?), 视为未设置")
            return default
    return v


def set(key: str, value: str) -> None:
    """写动态配置 (敏感项加密落库)."""
    stored = encrypt_value(str(value)) if key in SENSITIVE_KEYS else str(value)
    now = datetime.now(timezone.utc).isoformat()
    _conn().execute(
        "INSERT INTO app_settings(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, stored, now),
    )
    _conn().commit()


def delete(key: str) -> None:
    _conn().execute("DELETE FROM app_settings WHERE key=?", (key,))
    _conn().commit()


def get_all() -> dict:
    """全部动态配置 (敏感项脱敏: 只给 set 状态, 不给值)."""
    out = {}
    for row in _conn().execute("SELECT key, value, updated_at FROM app_settings"):
        key, value, updated_at = row
        if key in SENSITIVE_KEYS:
            out[key] = {"value": None, "sensitive": True, "set": True, "updated_at": updated_at}
        else:
            out[key] = {"value": value, "sensitive": False, "set": True, "updated_at": updated_at}
    return out
