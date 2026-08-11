"""MySQL 连接器实现"""
import logging
import sqlite3
from decimal import Decimal
from typing import Any
from dia.infrastructure.database.base import DataSourceConnector, DataSourceConfig

logger = logging.getLogger(__name__)

# 可重连的 pymysql 错误码
_RECONNECTABLE_ERRORS = (0, 2006, 2013, 2055)


def _convert_value(v: Any) -> Any:
    """Decimal → float, 其他类型原样返回 (None/str/int 保留)"""
    if isinstance(v, Decimal):
        return float(v)
    return v


def _convert_row(row: dict) -> dict:
    """转换一行的所有字段: Decimal 是 JSON 不可序列化且破坏 pandas 数值推断的类型"""
    return {k: _convert_value(v) for k, v in row.items()}


class MySQLConnector(DataSourceConnector):
    """MySQL 数据库连接器，支持自动重连"""

    def __init__(self, config: DataSourceConfig):
        super().__init__(config)
        self._conn: Any = None

    def connect(self) -> bool:
        try:
            # 优先尝试 pymysql,降级到纯 sqlite3 模拟
            try:
                import pymysql
                self._conn = pymysql.connect(
                    host=self.config.host,
                    port=self.config.port or 3306,
                    user=self.config.username,
                    password=self.config.password,
                    database=self.config.database,
                    charset="utf8mb4",
                    connect_timeout=10,
                    read_timeout=30,
                    write_timeout=30,
                    autocommit=True,
                )
                self._real_db = True
                # 生产安全基线: 只读连接 → 会话级禁止写操作 (双层防御之一)
                if self.config.read_only:
                    with self._conn.cursor() as cur:
                        cur.execute("SET SESSION TRANSACTION READ ONLY")
            except ImportError:
                # 没有 pymysql → 用 sqlite3 模拟(开发和测试环境)
                self._real_db = False
                sqlite_path = self.config.database
                self._conn = sqlite3.connect(sqlite_path, check_same_thread=False)
                self._conn.row_factory = sqlite3.Row

            self._connected = True
            return True
        except Exception as e:
            self._connected = False
            raise ConnectionError(f"MySQL 连接失败: {e}")

    def _execute_with_retry(self, operation, *args, **kwargs):
        """带重连的执行包装器"""
        self._ensure_connected()
        for attempt in range(3):  # max 3 attempts
            try:
                if self._real_db and attempt > 0:
                    self._conn.ping(reconnect=True)
                return operation(*args, **kwargs)
            except Exception as e:
                if attempt < 2 and self._is_reconnectable_error(e):
                    logger.warning(f"MySQL 操作失败 (attempt {attempt+1}): {e}, 尝试重连...")
                    if self._try_reconnect():
                        logger.info("MySQL 重连成功, 重试操作")
                        continue
                raise

    def list_tables(self) -> list[str]:
        def _do():
            if self._real_db:
                cursor = self._conn.cursor()
                cursor.execute("SHOW TABLES")
                tables = [row[0] for row in cursor.fetchall()]
            else:
                rows = self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
                tables = [r["name"] for r in rows]
            if self.config.tables_whitelist:
                tables = [t for t in tables if t in self.config.tables_whitelist]
            return tables
        return self._execute_with_retry(_do)

    def get_schema(self) -> dict[str, list[dict[str, Any]]]:
        def _do():
            schema = {}
            for table in self.list_tables():
                if self._real_db:
                    cursor = self._conn.cursor()
                    cursor.execute(f"DESCRIBE `{table}`")
                    desc_rows = cursor.fetchall()
                    columns = []
                    for row in desc_rows:
                        columns.append({
                            "name": row[0],
                            "type": row[1],
                            "nullable": row[2] == "YES",
                            "pk": row[3] == "PRI",
                        })
                    cursor.execute(f"SELECT COUNT(*) FROM `{table}`")
                    row_count = cursor.fetchone()[0]
                    try:
                        cursor.execute(f"SELECT * FROM `{table}` LIMIT 3")
                        sample_rows = cursor.fetchall()
                        col_names = [d[0] for d in cursor.description]
                        samples = [dict(zip(col_names, row)) for row in sample_rows]
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
        return self._execute_with_retry(_do)

    def _is_reconnectable_error(self, error: Exception) -> bool:
        """判断是否为可重连的错误"""
        err_msg = str(error)
        # pymysql 错误通常是 (code, message) 元组
        if hasattr(error, "args") and error.args:
            code = error.args[0]
            if isinstance(code, int) and code in _RECONNECTABLE_ERRORS:
                return True
        # 也检查字符串形式的错误
        for keyword in ("Lost connection", "read of closed file", "Connection reset",
                        "MySQL server has gone away", "Broken pipe"):
            if keyword in err_msg:
                return True
        return False

    def _try_reconnect(self) -> bool:
        """尝试重新连接"""
        try:
            self._conn = None
            self._connected = False
            return self.connect()
        except Exception as e:
            logger.warning(f"MySQL 重连失败: {e}")
            return False

    def query(self, sql: str, max_rows: int | None = 500) -> dict[str, Any]:
        """执行查询。

        Args:
            sql: SELECT 语句
            max_rows: 返回行数上限。默认 500 (防 LLM 拉全表失控);
                     None = 不截断 (统计工具 _load_df 用, 需要全量聚合)。
        """
        self._validate_sql(sql)

        def _do():
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

            # pymysql 默认把 DECIMAL/NUMERIC 列返回为 decimal.Decimal 对象,
            # pandas DataFrame 会将其推断为 object dtype → is_numeric_dtype=False,
            # 导致所有统计工具 (compare/hypothesis_test/forecast/detect/regression) 静默失效。
            # 统一转 float (None 保留), 让下游数值判断正常工作。
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

        try:
            return self._execute_with_retry(_do)
        except Exception as e:
            return {"error": str(e), "columns": [], "rows": [], "row_count": 0}

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
            self._connected = False

    def _ensure_connected(self):
        if not self._connected or not self._conn:
            raise RuntimeError("MySQL 连接未建立,请先调用 connect()")
