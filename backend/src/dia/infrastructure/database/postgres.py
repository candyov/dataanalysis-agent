"""PostgreSQL 连接器实现"""
import sqlite3
from decimal import Decimal
from typing import Any
from dia.infrastructure.database.base import DataSourceConnector, DataSourceConfig


def _convert_value(v: Any) -> Any:
    """Decimal → float (psycopg2 的 NUMERIC/DECIMAL 返回 Decimal, 破坏 pandas 数值推断)"""
    if isinstance(v, Decimal):
        return float(v)
    return v


def _convert_row(row: dict) -> dict:
    return {k: _convert_value(v) for k, v in row.items()}


class PostgresConnector(DataSourceConnector):
    """PostgreSQL 连接器实现（psycopg2 + SQLite 降级）"""

    def __init__(self, config: DataSourceConfig):
        super().__init__(config)
        self._conn: Any = None
        self._real_db = False

    def connect(self) -> bool:
        try:
            try:
                import psycopg2
                self._conn = psycopg2.connect(
                    host=self.config.host,
                    port=self.config.port or 5432,
                    user=self.config.username,
                    password=self.config.password,
                    database=self.config.database,
                    # 生产安全基线: 只读连接 → 会话级禁止写操作 (双层防御之一)
                    options="-c default_transaction_read_only=on" if self.config.read_only else "",
                )
                self._real_db = True
            except ImportError:
                # 降级到 SQLite 模拟
                self._conn = sqlite3.connect(self.config.database, check_same_thread=False)
                self._conn.row_factory = sqlite3.Row

            self._connected = True
            return True
        except Exception as e:
            self._connected = False
            raise ConnectionError(f"PostgreSQL 连接失败: {e}")

    def list_tables(self) -> list[str]:
        self._ensure_connected()
        if self._real_db:
            cursor = self._conn.cursor()
            cursor.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_type='BASE TABLE'"
            )
            tables = [row[0] for row in cursor.fetchall()]
        else:
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
            if self._real_db:
                cursor = self._conn.cursor()
                cursor.execute(
                    "SELECT column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=%s "
                    "ORDER BY ordinal_position",
                    (table,),
                )
                columns = []
                for row in cursor.fetchall():
                    columns.append({
                        "name": row[0],
                        "type": row[1],
                        "nullable": row[2] == "YES",
                        "pk": False,
                    })
                cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
                row_count = cursor.fetchone()[0]
                try:
                    cursor.execute(f'SELECT * FROM "{table}" LIMIT 3')
                    col_names = [d[0] for d in cursor.description]
                    samples = [dict(zip(col_names, row)) for row in cursor.fetchall()]
                except Exception:
                    samples = []
            else:
                pragma = self._conn.execute(f"PRAGMA table_info('{table}')").fetchall()
                columns = [{"name": c["name"], "type": c["type"],
                           "nullable": not c["notnull"], "pk": bool(c["pk"])} for c in pragma]
                cnt = self._conn.execute(f"SELECT COUNT(*) FROM `{table}`").fetchone()
                row_count = cnt[0] if cnt else 0
                try:
                    rows = self._conn.execute(f"SELECT * FROM `{table}` LIMIT 3").fetchall()
                    samples = [dict(zip([c["name"] for c in columns], row)) for row in rows]
                except Exception:
                    samples = []

            schema[table] = {"columns": columns, "samples": samples, "row_count": row_count}
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
        try:
            if self._real_db:
                cursor = self._conn.cursor()
                cursor.execute(sql)
                columns = [d[0] for d in cursor.description] if cursor.description else []
                rows_raw = cursor.fetchall()
                rows = [dict(zip(columns, row)) for row in rows_raw]
            else:
                cursor = self._conn.execute(sql)
                columns = [d[0] for d in cursor.description] if cursor.description else []
                rows_raw = cursor.fetchall()
                rows = [dict(zip(columns, row)) for row in rows_raw]

            # Decimal → float, 避免 object dtype 导致统计工具静默失效 (与 mysql.py 一致)
            rows = [_convert_row(r) for r in rows]

            max_return = 500 if max_rows is None else max_rows
            truncated = len(rows) > max_return if max_rows is not None else False
            rows_out = rows if max_rows is None else rows[:max_return]
            return {
                "columns": columns,
                "rows": rows_out,
                "row_count": len(rows),
                "truncated": truncated,
                "note": f"返回前 {max_return} 行,共 {len(rows)} 行" if truncated else "",
            }
        except Exception as e:
            return {"error": str(e), "columns": [], "rows": [], "row_count": 0}

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
            self._connected = False

    def _ensure_connected(self):
        if not self._connected or not self._conn:
            raise RuntimeError("PostgreSQL 连接未建立,请先调用 connect()")
