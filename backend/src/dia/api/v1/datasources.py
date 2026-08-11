"""
数据源管理 API

POST   /api/v1/datasources             创建数据源
GET    /api/v1/datasources             列出数据源
DELETE /api/v1/datasources/{id}        删除数据源
POST   /api/v1/datasources/test        测试连接
POST   /api/v1/datasources/upload      上传 CSV/Excel 文件 → 自动注册为数据源
"""
import os
import re
import logging
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, HTTPException
from pydantic import BaseModel

from dia.infrastructure.database.base import DataSourceConfig
from dia.infrastructure.database.manager import get_datasource_manager
from dia.core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

STORAGE_DIR = Path(settings.STORAGE_DIR)
UPLOAD_DIR = STORAGE_DIR / "uploaded"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls"}
MAX_UPLOAD_MB = 50  # 上传大小上限 (生产安全)

# CSV 公式注入: 单元格以 = + - @ 开头且长度>1 → 前缀 ' 防 Excel 执行
_FORMULA_PREFIXES = ("=", "+", "-", "@")


def _sanitize_formula_injection(df) -> "pd.DataFrame":
    """CSV/Excel 导入前清洗: 公式注入防护 (生产安全)."""
    import pandas as pd
    for col in df.columns:
        # 非数值列 (字符串) 才可能携带公式; 用 is_numeric_dtype 兼容 pandas 3.x 的 str dtype
        if not pd.api.types.is_numeric_dtype(df[col]):
            df[col] = df[col].map(
                lambda v: "'" + v
                if isinstance(v, str) and len(v) > 1 and v[0] in _FORMULA_PREFIXES
                else v
            )
    return df


class DataSourceCreate(BaseModel):
    name: str
    db_type: str = "sqlite"
    host: str = ""
    port: int = 0
    database: str = ""
    username: str = ""
    password: str = ""
    read_only: bool = True  # 生产安全基线: 默认只读


class DataSourceUpdate(BaseModel):
    name: str | None = None
    host: str | None = None
    port: int | None = None
    database: str | None = None
    username: str | None = None
    # 密码留空 = 保持不变; 传新值 = 更新
    password: str | None = None
    read_only: bool | None = None
    enabled: bool | None = None


@router.post("/datasources")
async def create_datasource(body: DataSourceCreate):
    """注册数据源连接 (非 SQLite 先测试连接, 失败拒绝注册)."""
    cfg = DataSourceConfig(
        id="", name=body.name, db_type=body.db_type, host=body.host,
        port=body.port, database=body.database, username=body.username,
        password=body.password, read_only=body.read_only,
    )
    # 网络数据库注册前强制测试 — 不注册不可达连接 (防垃圾配置)
    if body.db_type != "sqlite":
        mgr = get_datasource_manager()
        test = mgr.test_connection(cfg)
        if not test.get("success"):
            raise HTTPException(400, f"连接测试失败: {test.get('error', '未知错误')}")
    mgr = get_datasource_manager()
    mgr.add_source(cfg)
    return {"status": "ok", "id": cfg.id}


@router.put("/datasources/{source_id}")
async def update_datasource(source_id: str, body: DataSourceUpdate):
    """更新数据源 (密码留空 = 不变; 改 host/port 等可选测试)."""
    mgr = get_datasource_manager()
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    # 密码空串 = 保持不变 (与模型字段注释契约一致) — 前端留空输入会传 "",
    # 若直接写入会把真实密码静默清空
    if "password" in updates and updates["password"] == "":
        del updates["password"]
    cfg = mgr.get_source(source_id)
    if not cfg:
        raise HTTPException(404, f"数据源不存在: {source_id}")
    # 测试变更后的连接 (网络库)
    if body.db_type is None and cfg.db_type != "sqlite" and any(
        k in updates for k in ("host", "port", "database", "username", "password")
    ):
        test_cfg = DataSourceConfig(
            id="", name=cfg.name, db_type=cfg.db_type,
            host=updates.get("host", cfg.host), port=updates.get("port", cfg.port),
            database=updates.get("database", cfg.database),
            username=updates.get("username", cfg.username),
            password=updates.get("password", cfg.password) or cfg.password,
        )
        test = mgr.test_connection(test_cfg)
        if not test.get("success"):
            raise HTTPException(400, f"连接测试失败: {test.get('error', '未知错误')}")
    updated = mgr.update_source(source_id, updates)
    return {"status": "ok", "id": source_id}


@router.post("/datasources/{source_id}/toggle")
async def toggle_datasource(source_id: str):
    """启用/禁用数据源 (禁用后 Agent 不可连接)."""
    mgr = get_datasource_manager()
    cfg = mgr.get_source(source_id)
    if not cfg:
        raise HTTPException(404, f"数据源不存在: {source_id}")
    cfg.enabled = not cfg.enabled
    mgr.update_source(source_id, {"enabled": cfg.enabled})
    return {"status": "ok", "id": source_id, "enabled": cfg.enabled}


@router.get("/datasources")
async def list_datasources():
    mgr = get_datasource_manager()
    sources = mgr.list_sources()
    return {"datasources": sources}


@router.delete("/datasources/{source_id}")
async def delete_datasource(source_id: str):
    mgr = get_datasource_manager()
    if not mgr.remove_source(source_id):
        raise HTTPException(404, f"数据源不存在: {source_id}")
    return {"status": "ok"}


@router.post("/datasources/test")
async def test_connection(body: DataSourceCreate):
    """测试数据源连接 (不持久化, 不落库)."""
    import time
    t0 = time.monotonic()
    try:
        cfg = DataSourceConfig(
            id="", name="test", db_type=body.db_type, host=body.host,
            port=body.port, database=body.database, username=body.username,
            password=body.password, read_only=body.read_only,
        )
        mgr = get_datasource_manager()
        res = mgr.test_connection(cfg)
        latency_ms = round((time.monotonic() - t0) * 1000)
        if res.get("success"):
            return {
                "ok": True,
                "latency_ms": latency_ms,
                "message": f"连接成功, 发现 {res.get('table_count', 0)} 张表",
                "table_count": res.get("table_count", 0),
            }
        return {"ok": False, "latency_ms": latency_ms, "message": res.get("error", "连接失败")}
    except Exception as e:
        return {"ok": False, "latency_ms": round((time.monotonic() - t0) * 1000), "message": str(e)}


@router.post("/datasources/upload")
async def upload_file(file: UploadFile = File(...)):
    """上传 CSV/Excel 文件 → 导入为临时 SQLite → 自动注册为数据源.

    Returns:
        {"status": "ok", "source_id": "file_xxx", "name": "...", "rows": 100, "columns": [...]}
    """
    if not file.filename:
        raise HTTPException(400, "文件名不能为空")

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"不支持的文件格式: {ext}。仅支持 .csv / .xlsx / .xls")

    # 保存到 storage/uploaded/
    safe_name = re.sub(r'[^\w\-_\. ]', '_', file.filename)
    save_path = UPLOAD_DIR / safe_name
    content = await file.read()
    # 生产安全: 上传大小上限
    if len(content) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(400, f"文件超过大小限制 ({MAX_UPLOAD_MB}MB)")
    save_path.write_bytes(content)

    # 解析文件生成 preview
    try:
        import pandas as pd
        if ext == ".csv":
            df = pd.read_csv(save_path)
        else:
            df = pd.read_excel(save_path)
        # 生产安全: CSV 公式注入防护 (= + - @ 开头 → 前缀 ')
        df = _sanitize_formula_injection(df)
        rows = len(df)
        columns = df.columns.tolist()
    except Exception as e:
        save_path.unlink(missing_ok=True)
        raise HTTPException(400, f"文件解析失败: {str(e)}")

    # 生成 source_id 并导入 SQLite
    import sqlite3
    table_name = re.sub(r'[^\w]', '_', os.path.splitext(safe_name)[0])[:40]
    uploads_db = STORAGE_DIR / "uploads.db"
    conn = sqlite3.connect(str(uploads_db), check_same_thread=False)
    df.to_sql(table_name, conn, if_exists="replace", index=False)
    conn.close()

    source_id = f"file_{table_name}"
    mgr = get_datasource_manager()
    cfg = DataSourceConfig(
        id=source_id,
        name=f"上传文件: {safe_name}",
        db_type="sqlite",
        database=str(uploads_db),
    )
    mgr.add_source(cfg)

    return {
        "status": "ok",
        "source_id": source_id,
        "name": cfg.name,
        "rows": rows,
        "columns": columns,
    }


@router.get("/datasources/{source_id}/tables")
async def list_tables(source_id: str):
    """列出指定数据源的所有表."""
    mgr = get_datasource_manager()
    try:
        conn = mgr.connect(source_id)
        tables = conn.list_tables()
        return {"tables": tables}
    except Exception as e:
        raise HTTPException(404, f"数据源不存在或无法连接: {str(e)}")


# ══════════════════════════════════════════════════════════════
#  文件管理 (上传文件数据源的生命周期)
# ══════════════════════════════════════════════════════════════

@router.get("/files")
async def list_files():
    """文件数据源列表: 名称/大小/行数/列数/上传时间."""
    mgr = get_datasource_manager()
    files = []
    for cfg in mgr.list_sources():
        if not cfg.get("id", "").startswith("file_"):
            continue
        name = cfg.get("name", "").replace("上传文件: ", "")
        physical = UPLOAD_DIR / name if UPLOAD_DIR.joinpath(name).exists() else None
        size = physical.stat().st_size if physical and physical.is_file() else 0
        created_at = physical.stat().st_mtime if physical and physical.is_file() else 0
        rows = cols = 0
        try:
            conn = mgr.connect(cfg["id"])
            schema = conn.get_schema()
            for t, info in schema.items():
                rows += int(info.get("row_count", 0) or 0)
                cols = max(cols, int(info.get("column_count", 0) or 0))
        except Exception:
            pass
        files.append({
            "source_id": cfg["id"], "name": name, "size": size,
            "rows": rows, "columns": cols,
            "db_type": cfg.get("db_type", "sqlite"),
            "enabled": cfg.get("enabled", True),
            "created_at": created_at,
        })
    # 按上传时间倒序
    files.sort(key=lambda f: f["created_at"], reverse=True)
    return {"files": files}


@router.delete("/files/{source_id}")
async def delete_file(source_id: str):
    """删除上传文件: 删配置 + 删 uploads.db 表 + 删物理文件 (二次确认由前端负责)."""
    mgr = get_datasource_manager()
    cfg = mgr.get_source(source_id)
    if not cfg or not source_id.startswith("file_"):
        raise HTTPException(404, f"文件数据源不存在: {source_id}")
    # 1. 删 uploads.db 表
    try:
        import sqlite3
        uploads_db = STORAGE_DIR / "uploads.db"
        if uploads_db.exists():
            conn = sqlite3.connect(str(uploads_db))
            table = source_id.replace("file_", "")
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')
            conn.commit()
            conn.close()
    except Exception as e:
        logger.warning(f"删除 uploads.db 表失败: {e}")
    # 2. 删物理文件
    name = cfg.name.replace("上传文件: ", "")
    physical = UPLOAD_DIR / name
    if physical.exists():
        physical.unlink(missing_ok=True)
    # 3. 删配置
    mgr.remove_source(source_id)
    return {"status": "ok"}


@router.get("/datasources/{source_id}/info")
async def source_info(source_id: str):
    """获取数据源详细信息：表、列、行数."""
    mgr = get_datasource_manager()
    try:
        conn = mgr.connect(source_id)
        tables = conn.list_tables()
        schema = conn.get_schema()
        total_rows = sum(schema.get(t, {}).get("row_count", 0) or 0 for t in tables)
        total_cols = sum(schema.get(t, {}).get("column_count", 0) or 0 for t in tables)
        return {
            "tables": tables,
            "table_count": len(tables),
            "total_rows": total_rows,
            "total_cols": total_cols,
        }
    except Exception as e:
        raise HTTPException(404, f"数据源不存在或无法连接: {str(e)}")
