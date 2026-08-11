"""数据库连接器抽象基类"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DataSourceConfig:
    """数据源配置(持久化到 JSON, 密码加密)"""
    id: str
    name: str
    db_type: str = "sqlite"          # sqlite | mysql | postgres
    host: str = ""
    port: int = 0
    database: str = ""
    username: str = ""
    password: str = ""
    tables_whitelist: list[str] = field(default_factory=list)
    enabled: bool = True
    # 生产安全基线: 默认只读 — Agent 自动执行 SQL 不能改数据
    read_only: bool = True
    # 资源归属 (单用户预留: 多用户时按 owner 隔离)
    owner: str = "local"

    def connection_string(self) -> str:
        if self.db_type == "sqlite":
            return self.database
        elif self.db_type == "mysql":
            return f"mysql+aiomysql://{self.username}:{self.password}@{self.host}:{self.port}/{self.database}"
        elif self.db_type == "postgres":
            return f"postgresql+asyncpg://{self.username}:{self.password}@{self.host}:{self.port}/{self.database}"
        raise ValueError(f"Unsupported db_type: {self.db_type}")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "db_type": self.db_type,
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "username": self.username,
            "password": self.password,
            "tables_whitelist": self.tables_whitelist,
            "enabled": self.enabled,
            "read_only": self.read_only,
            "owner": self.owner,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DataSourceConfig":
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            db_type=d.get("db_type", "sqlite"),
            host=d.get("host", ""),
            port=d.get("port", 0),
            database=d.get("database", ""),
            username=d.get("username", ""),
            password=d.get("password", ""),
            tables_whitelist=d.get("tables_whitelist", []),
            enabled=d.get("enabled", True),
            read_only=d.get("read_only", True),
            owner=d.get("owner", "local"),
        )


class DataSourceConnector(ABC):
    """数据源连接器抽象基类"""

    def __init__(self, config: DataSourceConfig):
        self.config = config
        self._connected = False

    @abstractmethod
    def connect(self) -> bool:
        """建立连接,返回是否成功"""
        ...

    @abstractmethod
    def list_tables(self) -> list[str]:
        """列出所有表名"""
        ...

    @abstractmethod
    def get_schema(self) -> dict[str, list[dict[str, Any]]]:
        """
        返回完整的 Schema 信息
        {table_name: [{name, type, nullable, sample_values}]}
        """
        ...

    @abstractmethod
    def query(self, sql: str) -> dict[str, Any]:
        """
        执行 SQL 查询(仅 SELECT / DESCRIBE / SHOW)
        返回 {"columns": [...], "rows": [...], "row_count": N}
        """
        ...

    @abstractmethod
    def close(self):
        """关闭连接"""
        ...

    @property
    def connected(self) -> bool:
        return self._connected

    def _validate_sql(self, sql: str):
        """安全校验:只允许只读操作"""
        sql_upper = sql.strip().upper()
        forbidden = ["DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "CREATE", "TRUNCATE", "GRANT", "REVOKE"]
        for word in forbidden:
            if sql_upper.startswith(word) or f" {word} " in f" {sql_upper} ":
                raise ValueError(f"只读数据源禁止执行 {word} 操作.仅支持 SELECT/DESCRIBE/SHOW.")
