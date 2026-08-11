"""数据源管理器 -- 连接注册、获取、生命周期管理"""
import uuid
import json
import threading
import logging
from pathlib import Path
from dia.infrastructure.database.base import DataSourceConnector, DataSourceConfig
from dia.infrastructure.database.sqlite import SQLiteConnector
from dia.infrastructure.config_store import encrypt_value, decrypt_value

logger = logging.getLogger(__name__)

# 数据源配置持久化路径 — 统一使用 settings 的 storage 目录
from pathlib import Path as _Path
from dia.core.config import settings as _settings
CONFIG_PATH = _Path(_settings.STORAGE_DIR) / "datasources.json"


class DataSourceManager:
    """管理所有数据源连接

    SQLite 连接使用线程本地存储 (threading.local)，避免跨线程访问错误。
    """

    def __init__(self):
        self._sources: dict[str, DataSourceConfig] = {}
        self._connectors: dict[str, DataSourceConnector] = {}
        self._local = threading.local()
        self._load_config()

    # ── 配置持久化 ──

    def _load_config(self):
        """从 JSON 文件加载数据源配置 (密码: enc: 前缀解密, 旧明文向后兼容)."""
        try:
            if CONFIG_PATH.exists():
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                for item in data:
                    pwd = item.get("password", "")
                    if pwd.startswith("enc:"):
                        try:
                            item["password"] = decrypt_value(pwd[4:])
                        except Exception:
                            logger.warning(f"数据源 {item.get('id','?')} 密码解密失败, 置空")
                            item["password"] = ""
                    cfg = DataSourceConfig.from_dict(item)
                    self._sources[cfg.id] = cfg
                logger.info(f"加载 {len(self._sources)} 个数据源配置")
        except Exception as e:
            logger.warning(f"数据源配置加载失败: {e}")

    def _save_config(self):
        """持久化到 JSON 文件 (密码加密落盘)."""
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = []
        for cfg in self._sources.values():
            d = cfg.to_dict()
            if d.get("password"):
                d["password"] = "enc:" + encrypt_value(d["password"])
            data.append(d)
        CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # ── CRUD ──

    def add_source(self, config: DataSourceConfig) -> DataSourceConfig:
        if not config.id:
            config.id = str(uuid.uuid4())
        self._sources[config.id] = config
        self._save_config()
        logger.info(f"新增数据源: {config.name} ({config.db_type})")
        return config

    def update_source(self, source_id: str, updates: dict) -> DataSourceConfig | None:
        cfg = self._sources.get(source_id)
        if not cfg:
            return None
        for key, value in updates.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
        self._save_config()
        return cfg

    def remove_source(self, source_id: str) -> bool:
        if source_id not in self._sources:
            return False
        # 先关闭连接
        self.disconnect(source_id)
        del self._sources[source_id]
        self._save_config()
        return True

    def get_source(self, source_id: str) -> DataSourceConfig | None:
        return self._sources.get(source_id)

    def list_sources(self) -> list[dict]:
        # API 脱敏: 密码永不回传 (仅内存中持有)
        out = []
        for cfg in self._sources.values():
            d = cfg.to_dict()
            d["password"] = ""
            out.append(d)
        return out

    # ── 连接管理 ──

    def connect(self, source_id: str) -> DataSourceConnector:
        """获取或创建连接。所有类型统一使用线程本地存储，避免跨线程问题."""
        cfg = self._sources.get(source_id)
        if not cfg:
            raise ValueError(f"数据源不存在: {source_id}")

        if not hasattr(self._local, "connectors"):
            self._local.connectors = {}
        if source_id in self._local.connectors and self._local.connectors[source_id].connected:
            return self._local.connectors[source_id]

        connector = self._create_connector(cfg)
        connector.connect()
        self._local.connectors[source_id] = connector
        return connector

    def disconnect(self, source_id: str):
        # 关闭当前线程的连接
        if hasattr(self._local, "connectors") and source_id in self._local.connectors:
            try:
                self._local.connectors[source_id].close()
            except Exception:
                pass
            del self._local.connectors[source_id]

    def disconnect_all(self):
        if hasattr(self._local, "connectors"):
            for sid in list(self._local.connectors.keys()):
                try:
                    self._local.connectors[sid].close()
                except Exception:
                    pass
            self._local.connectors.clear()

    def _create_connector(self, config: DataSourceConfig) -> DataSourceConnector:
        from dia.infrastructure.database.mysql import MySQLConnector
        from dia.infrastructure.database.postgres import PostgresConnector

        if config.db_type == "sqlite":
            return SQLiteConnector(config)
        elif config.db_type == "mysql":
            return MySQLConnector(config)
        elif config.db_type == "postgres":
            return PostgresConnector(config)
        raise ValueError(f"不支持的数据库类型: {config.db_type}")

    # ── 测试连接 ──

    def test_connection(self, config: DataSourceConfig) -> dict:
        """测试数据源连接是否可用"""
        try:
            connector = self._create_connector(config)
            connector.connect()
            tables = connector.list_tables()
            connector.close()
            return {
                "success": True,
                "tables": tables,
                "table_count": len(tables),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


# 全局单例
_datasource_manager: DataSourceManager | None = None


def get_datasource_manager() -> DataSourceManager:
    global _datasource_manager
    if _datasource_manager is None:
        _datasource_manager = DataSourceManager()
    return _datasource_manager
