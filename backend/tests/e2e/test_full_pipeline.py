"""全链路集成测试 — 端到端：注册源 → 分析 → 断言

运行:  cd backend && set PYTHONPATH=src && pytest tests/e2e/test_full_pipeline.py -v -s

依赖: pytest, httpx
"""

import json
import os
import sys
import time
import pytest
import threading
from pathlib import Path

# 确保 src 在 path 中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import uvicorn
from dia.main import app

# ══════════════════════════════════════════════════════════════════
#  Test server fixture
# ══════════════════════════════════════════════════════════════════

BASE_URL = "http://127.0.0.1:8099"


def _run_server():
    uvicorn.run(app, host="127.0.0.1", port=8099, log_level="warning")


@pytest.fixture(scope="session")
def server():
    """启动测试服务器（整个 session 共享）"""
    t = threading.Thread(target=_run_server, daemon=True)
    t.start()
    time.sleep(3)  # 等待启动
    yield
    # 不需要 teardown — daemon thread


# ══════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════

def _post(path: str, data: dict) -> dict:
    """同步 POST 请求"""
    import urllib.request
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=30)
    return json.loads(resp.read())


def _get(path: str) -> dict:
    import urllib.request
    resp = urllib.request.urlopen(f"{BASE_URL}{path}", timeout=10)
    return json.loads(resp.read())


def _chat(message: str, source_id: str = "", session_id: str = "") -> tuple[list[dict], list[dict]]:
    """发聊天请求，返回 (events, errors)"""
    import urllib.request
    sid = session_id or f"test_{int(time.time()*1000)}"
    body = json.dumps({
        "message": message,
        "source_id": source_id,
        "session_id": sid,
    }).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/api/v1/chat", data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=120)
    events = []
    for line in resp.read().decode().splitlines():
        if line.startswith("data:"):
            try:
                events.append(json.loads(line[5:]))
            except json.JSONDecodeError:
                pass
    errors = [e for e in events if e.get("type") == "error"]
    return events, errors


def _upload(file_path: str) -> dict:
    """上传文件"""
    import urllib.request
    boundary = "----TestBoundary"
    with open(file_path, "rb") as f:
        content = f.read()
    filename = Path(file_path).name
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: text/csv\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{BASE_URL}/api/v1/datasources/upload", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=30)
    return json.loads(resp.read())


# ══════════════════════════════════════════════════════════════════
#  Test suite
# ══════════════════════════════════════════════════════════════════

class TestHealth:
    """基础健康检查"""

    def test_health(self, server):
        result = _get("/api/v1/health")
        assert result["status"] == "ok"

    def test_sessions_empty(self, server):
        result = _get("/api/v1/sessions")
        assert "sessions" in result


class TestDataSources:
    """数据源 CRUD + MySQL 注册"""

    def test_list_sources(self, server):
        result = _get("/api/v1/datasources")
        assert "datasources" in result

    def test_register_mysql(self, server):
        """注册 Docker MySQL 数据源 (密码走环境变量 MYSQL_PASSWORD, 未设置则跳过)"""
        mysql_password = os.getenv("MYSQL_PASSWORD", "")
        if not mysql_password:
            pytest.skip("未设置 MYSQL_PASSWORD, 跳过 MySQL 注册测试")
        result = _post("/api/v1/datasources", {
            "name": "E2E Test MySQL",
            "db_type": "mysql",
            "host": "127.0.0.1",
            "port": 13306,
            "database": "test_analysis",
            "username": "root",
            "password": mysql_password,
        })
        assert result["status"] == "ok"
        assert "id" in result

        # 验证注册成功
        sources = _get("/api/v1/datasources")["datasources"]
        registered = [s for s in sources if s["db_type"] == "mysql"]
        assert len(registered) >= 1

    def test_source_info(self, server):
        """数据源详情"""
        sources = _get("/api/v1/datasources")["datasources"]
        mysql_sources = [s for s in sources if s["db_type"] == "mysql"]
        if not mysql_sources:
            pytest.skip("没有 MySQL 数据源")
        sid = mysql_sources[0]["id"]
        info = _get(f"/api/v1/datasources/{sid}/info")
        assert "tables" in info
        assert "total_rows" in info
        assert info["total_rows"] > 0, f"MySQL 表没有数据: {info}"


class TestChatWithMySQL:
    """MySQL 数据源分析"""

    @pytest.fixture(autouse=True)
    def _ensure_source(self, server):
        """确保 test_analysis_mysql 数据源存在 (密码走环境变量, 未设置则跳过)"""
        mysql_password = os.getenv("MYSQL_PASSWORD", "")
        if not mysql_password:
            pytest.skip("未设置 MYSQL_PASSWORD, 跳过 MySQL 分析测试")
        sources = _get("/api/v1/datasources")["datasources"]
        exist = any(s["id"] == "test_analysis_mysql" for s in sources)
        if not exist:
            _post("/api/v1/datasources", {
                "name": "MySQL 测试数据库 (Docker)",
                "db_type": "mysql",
                "host": "127.0.0.1",
                "port": 13306,
                "database": "test_analysis",
                "username": "root",
                "password": mysql_password,
            })

    def test_chat_returns_no_errors(self, server):
        """分析 MySQL 数据 — 无错误"""
        events, errors = _chat(
            "查华东区域销售额",
            source_id="test_analysis_mysql",
        )
        assert len(errors) == 0, f"有错误: {[e.get('message','?') for e in errors[:3]]}"

    def test_chat_has_bot_reply(self, server):
        """分析 MySQL 数据 — 有 bot 回复"""
        events, _ = _chat(
            "查华东区域销售额",
            source_id="test_analysis_mysql",
        )
        bot_events = [e for e in events if e.get("type") == "bot"]
        assert len(bot_events) >= 1, "应该有 bot 回复"
        text = "".join(e.get("text", "") for e in bot_events)
        assert len(text) > 20, f"bot 回复太短: '{text[:50]}...'"

    def test_chat_uses_describe_tool(self, server):
        """分析 MySQL — 工具调用包含 describe 或 query"""
        events, _ = _chat(
            "查华东区域销售额",
            source_id="test_analysis_mysql",
        )
        tool_events = [e for e in events if e.get("type") == "tool_call"]
        tools = [e.get("tool") for e in tool_events]
        # check for describe OR drill_down WITH source_id (both valid paths)
        assert any("describe" in (t or "") or "query" in (t or "") or "drill_down" in (t or "") for t in tools), \
            f"应该分析数据，实际工具: {tools[:6]}"

    def test_chat_no_1146_error(self, server):
        """分析 MySQL — 没有 'table not found' 错误"""
        events, _ = _chat(
            "查daily_sales华东销售额",
            source_id="test_analysis_mysql",
        )
        errors = [e for e in events if e.get("type") == "error"]
        for e in errors:
            msg = str(e.get("message", ""))
            assert "1146" not in msg, f"出现了表不存在错误: {msg[:200]}"
            assert "doesn't exist" not in msg.lower()

    def test_chat_has_done_event(self, server):
        """分析 MySQL — 有 done 事件"""
        events, _ = _chat(
            "查华东区域销售额",
            source_id="test_analysis_mysql",
        )
        done = [e for e in events if e.get("type") == "done"]
        assert len(done) >= 1, "应该有 done 事件"


class TestMultiTurn:
    """多轮对话"""

    def test_multi_turn(self, server):
        """同一 session 两轮对话"""
        sid = f"multi_{int(time.time())}"
        # Turn 1
        events1, errors1 = _chat("查华东区域销售额", source_id="test_analysis_mysql", session_id=sid)
        assert len(errors1) == 0
        # Turn 2 — 引用上一轮上下文
        events2, errors2 = _chat("华南呢", source_id="test_analysis_mysql", session_id=sid)
        assert len(errors2) == 0
        bot_text = "".join(
            e.get("text", "") for e in events2 if e.get("type") == "bot"
        )
        assert "华南" in bot_text, f"第二轮应该提到华南, 实际: {bot_text[:100]}..."


class TestFileUpload:
    """文件上传 + 分析"""

    @pytest.fixture
    def test_csv(self):
        """创建测试 CSV 文件"""
        import tempfile
        import pandas as pd
        tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w",
                                          encoding="utf-8", prefix="e2e_test_")
        df = pd.DataFrame({
            "region": ["华东", "华南", "华北", "华东", "华南"] * 6,
            "product": ["A", "B", "A", "C", "B"] * 6,
            "sales": [100, 200, 150, 300, 250] * 6,
            "cost": [60, 120, 90, 180, 150] * 6,
        })
        df.to_csv(tmp, index=False)
        tmp.close()
        yield tmp.name
        Path(tmp.name).unlink(missing_ok=True)

    def test_upload_csv(self, server, test_csv):
        """上传 CSV → 验证返回"""
        result = _upload(test_csv)
        print(f"Upload result: {result}")
        assert result["status"] == "ok"
        assert result["source_id"].startswith("file_"), f"source_id 应该以 file_ 开头: {result['source_id']}"
        assert result["rows"] > 0
        assert len(result["columns"]) > 0

    def test_uploaded_source_available(self, server, test_csv):
        """上传后数据源可查"""
        result = _upload(test_csv)
        source_id = result["source_id"]
        info = _get(f"/api/v1/datasources/{source_id}/info")
        assert info["total_rows"] > 0, f"上传的数据源应该有数据: {info}"

    def test_analyze_uploaded_file(self, server, test_csv):
        """分析上传的 CSV"""
        result = _upload(test_csv)
        source_id = result["source_id"]
        events, errors = _chat("数据概览", source_id=source_id)
        assert len(errors) == 0, f"分析上传文件有错误: {errors}"


class TestErrors:
    """异常路径"""

    def test_empty_source_skips_curator(self, server):
        """没有数据源 — curator 被跳过，不应该卡住"""
        events, errors = _chat("数据分析", source_id="")
        # 可能有一两个错误（无数据源），但不应该反复重试
        retry_errors = [e for e in errors if "retry" in str(e.get("message", "")).lower()]
        assert len(retry_errors) == 0, f"不应该重试，但重试了 {len(retry_errors)} 次"

    def test_invalid_source_returns_error(self, server):
        """不存在的 source_id"""
        events, errors = _chat("数据分析", source_id="nonexistent_source_xyz")
        # 应该有错误
        assert len(errors) >= 1, "不存在的源应该报错"

    def test_no_ingestor_with_source(self, server):
        """有 MySQL 源时不走 ingestor"""
        events, _ = _chat("查华东区域销售额", source_id="test_analysis_mysql")
        tool_events = [e for e in events if e.get("type") == "tool_call"]
        all_tools = [e.get("tool") for e in tool_events]
        assert "describe" in all_tools, f"应该调用 describe 而不是 ingestor, tools={all_tools}"


class TestStateValidation:
    """State key 校验"""

    def test_valid_state(self):
        from dia.core.state import validate_state
        validate_state({
            "user_request": "test",
            "source_id": "db1",
            "plan_step": 0,
            "next": "analyst",
        })

    def test_typo_in_state_key(self):
        """拼错的 state key 会直接抛异常"""
        from dia.core.state import validate_state
        with pytest.raises(TypeError, match="非法 key"):
            validate_state({"plan_setp": 0})


# ══════════════════════════════════════════════════════════════════
#  Main runner (direct: python test_full_pipeline.py)
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
