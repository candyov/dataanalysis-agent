"""SQLite 连接器实现 — 安全加固版"""
import sqlite3
import threading
from typing import Any
from dia.infrastructure.database.base import DataSourceConnector, DataSourceConfig
from dia.infrastructure.security.sanitize import sanitize_rows
from dia.core.config import settings


class SQLiteConnector(DataSourceConnector):
    """SQLite 数据库连接器 (只读 + 超时 + 行数限制)"""

    def __init__(self, config: DataSourceConfig):
        super().__init__(config)
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> bool:
        try:
            # 连接级只读: URI 参数 `?mode=ro` 从文件系统层面阻止写操作
            uri = self.config.database
            if not uri.startswith("file:"):
                uri = f"file:{uri}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._connected = True
            return True
        except Exception as e:
            self._connected = False
            raise ConnectionError(f"SQLite 连接失败: {e}")

    def list_tables(self) -> list[str]:
        self._ensure_connected()
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        tables = [r["name"] for r in rows]
        if self.config.tables_whitelist:
            tables = [t for t in tables if t in self.config.tables_whitelist]
        return tables

    def get_schema(self) -> dict[str, list[dict[str, Any]]]:
        self._ensure_connected()
        schema = {}
        for table in self.list_tables():
            pragma = self._conn.execute(f"PRAGMA table_info('{table}')").fetchall()
            columns = [{
                "name": col["name"], "type": col["type"],
                "nullable": not col["notnull"], "pk": bool(col["pk"]),
            } for col in pragma]

            try:
                sample_rows = self._conn.execute(
                    f'SELECT * FROM "{table}" LIMIT 3'
                ).fetchall()
                sample_values = []
                if sample_rows:
                    for col_info in columns:
                        vals = [row[col_info["name"]] for row in sample_rows]
                        sample_values.append({"column": col_info["name"], "samples": vals})
            except Exception:
                sample_values = []

            try:
                cnt = self._conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
                row_count = cnt[0] if cnt else 0
            except Exception:
                row_count = -1

            schema[table] = {
                "columns": columns, "samples": sample_values,
                "row_count": row_count,
            }
        return schema

    def query(self, sql: str, max_rows: int | None = 500) -> dict[str, Any]:
        """执行查询。

        Args:
            sql: SELECT 语句
            max_rows: 返回行数上限。默认 500 (防 LLM 拉全表失控);
                     None = 不截断 (统计工具 _load_df 用, 需要全量聚合)。
        """
        self._validate_sql(sql)
        self._ensure_connected()

        result_container = {}
        exception_container = {}

        def _run():
            try:
                cursor = self._conn.execute(sql)
                columns = [d[0] for d in cursor.description] if cursor.description else []
                rows_raw = cursor.fetchall()
                rows = [dict(zip(columns, row)) for row in rows_raw]
                max_return = 500 if max_rows is None else max_rows
                truncated = len(rows) > max_return if max_rows is not None else False
                rows_out = rows if max_rows is None else rows[:max_return]
                result_container["data"] = {
                    "columns": columns,
                    "rows": sanitize_rows(rows_out, columns),
                    "row_count": len(rows),
                    "truncated": truncated,
                    "note": f"返回前 {max_return} 行,共 {len(rows)} 行" if truncated else "",
                }
            except Exception as e:
                exception_container["error"] = str(e)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout=settings.QUERY_TIMEOUT)

        if thread.is_alive():
            return {"error": f"查询超时 ({settings.QUERY_TIMEOUT}s)", "columns": [], "rows": [], "row_count": 0}
        if "error" in exception_container:
            return {"error": exception_container["error"], "columns": [], "rows": [], "row_count": 0}
        return result_container.get("data", {"error": "未知错误", "columns": [], "rows": [], "row_count": 0})

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
            self._connected = False

    def _ensure_connected(self):
        if not self._connected or not self._conn:
            raise RuntimeError("SQLite 连接未建立,请先调用 connect()")
