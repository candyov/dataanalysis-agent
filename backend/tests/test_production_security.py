"""生产安全测试: 配置加密/脱敏/热更新 + 只读 SQL 校验 + 数据源密码加密落盘."""
import json
import os
import sys
import tempfile

sys.path.insert(0, 'src')


def test_config_store_encrypt_roundtrip():
    """敏感项加密落库 + 解密读取 + get_all 脱敏"""
    from dia.infrastructure import config_store as cs

    cs.set("LLM_MODEL", "deepseek-test")
    cs.set("LLM_API_KEY", "sk-secret-123")
    assert cs.get("LLM_MODEL") == "deepseek-test"
    assert cs.get("LLM_API_KEY") == "sk-secret-123"  # 解密读回

    # 落库是密文, 无明文
    row = cs._conn().execute("SELECT value FROM app_settings WHERE key='LLM_API_KEY'").fetchone()
    assert "sk-secret-123" not in row[0]

    # get_all 脱敏: 敏感项只给 set 状态
    all_ = cs.get_all()
    assert all_["LLM_API_KEY"]["value"] is None and all_["LLM_API_KEY"]["sensitive"] is True
    assert all_["LLM_MODEL"]["value"] == "deepseek-test"

    cs.delete("LLM_MODEL")
    cs.delete("LLM_API_KEY")
    assert cs.get("LLM_MODEL", "default-back") == "default-back"


def test_get_llm_hot_reload():
    """动态配置优先级: 设置 LLM_MODEL → get_llm 用动态值 (签名变更 → 缓存失效)"""
    import asyncio

    from dia.core import base
    from dia.infrastructure import config_store as cs

    base._llm_instances.clear()
    cs.set("LLM_MODEL", "dynamic-model-x")
    try:
        async def _get():
            llm = await base.get_llm(temperature=0.5)
            return llm.model_name
        model = asyncio.run(_get())
        assert model == "dynamic-model-x", f"热更新未生效: {model}"
    finally:
        cs.delete("LLM_MODEL")
        base._llm_instances.clear()


def test_assert_readonly_sql():
    """双层防御-应用层: SELECT/WITH 放行, 写操作/多语句/注释绕过拒绝"""
    from dia.tools.data import _assert_readonly_sql

    # 放行
    _assert_readonly_sql("SELECT * FROM t")
    _assert_readonly_sql("  with x as (select 1) select * from x")
    _assert_readonly_sql("EXPLAIN SELECT * FROM t")
    _assert_readonly_sql("PRAGMA table_info('t')")

    # 拒绝
    for bad in [
        "UPDATE t SET a=1",
        "DELETE FROM t",
        "INSERT INTO t VALUES (1)",
        "DROP TABLE t",
        "CREATE TABLE x (a int)",
        "SELECT * FROM t; DELETE FROM t",  # 多语句
        "/*c*/ UPDATE t SET a=1",          # 注释绕过
        "-- hi\nUPDATE t SET a=1",
    ]:
        try:
            _assert_readonly_sql(bad)
            assert False, f"应拒绝: {bad}"
        except ValueError:
            pass


def test_datasource_password_encrypted_on_disk():
    """数据源密码加密落盘: JSON 无明文, 重载解密正确, 旧明文向后兼容"""
    from dia.infrastructure.database import manager as mgr_mod
    from dia.infrastructure.database.base import DataSourceConfig

    fd, tmp = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    orig = mgr_mod.CONFIG_PATH
    mgr_mod.CONFIG_PATH = type(orig)(tmp)
    try:
        mgr = mgr_mod.DataSourceManager()
        cfg = DataSourceConfig(id="sec_test", name="t", db_type="sqlite",
                               database=":memory:", password="p@ssw0rd")
        mgr.add_source(cfg)

        raw = json.loads(open(tmp, encoding="utf-8").read())
        assert "p@ssw0rd" not in json.dumps(raw), "密码明文落盘!"
        assert raw[0]["password"].startswith("enc:")

        # 重载 → 解密正确
        mgr2 = mgr_mod.DataSourceManager()
        assert mgr2.get_source("sec_test").password == "p@ssw0rd"
        mgr2.remove_source("sec_test")

        # 旧明文向后兼容: 手写明文 JSON → 加载不崩
        plain = json.dumps([{"id": "legacy", "name": "l", "db_type": "sqlite",
                             "database": ":memory:", "password": "oldplain"}])
        open(tmp, "w", encoding="utf-8").write(plain)
        mgr3 = mgr_mod.DataSourceManager()
        assert mgr3.get_source("legacy").password == "oldplain"
        mgr3.remove_source("legacy")
    finally:
        mgr_mod.CONFIG_PATH = orig
        try:
            os.unlink(tmp)
        except PermissionError:
            pass


def test_sanitize_formula_injection():
    """CSV 公式注入防护: = + - @ 开头字符串单元格 → 前缀 '"""
    from dia.api.v1.datasources import _sanitize_formula_injection
    import pandas as pd

    df = pd.DataFrame({
        "name": ["正常", "=HYPERLINK(evil)", "+cmd", "-5", "@sum"],
        "value": [1, 2, 3, -5, 4],  # 数值列不受影响
    })
    out = _sanitize_formula_injection(df)
    assert out["name"].tolist() == ["正常", "'=HYPERLINK(evil)", "'+cmd", "'-5", "'@sum"]
    assert out["value"].tolist() == [1, 2, 3, -5, 4]  # 数值列不动


# ══ 模型档案 (多模型切换) ══

def test_model_profiles_crud_and_encryption():
    """模型档案: 创建/激活/切换/更新/删除 + api_key 加密 + 列表脱敏"""
    import asyncio

    from dia.api.v1 import models as m
    from dia.infrastructure import config_store as cs

    def _run(coro):
        return asyncio.run(coro)

    # 清理
    cs.delete(m.MODEL_PROFILES_KEY)
    cs.delete(m.ACTIVE_MODEL_KEY)

    try:
        # 创建两个档案
        r1 = _run(m.create_model(m.ModelProfileIn(name="DeepSeek 主力", provider="deepseek",
                                                  model="deepseek-chat", api_key="sk-ds-1")))
        r2 = _run(m.create_model(m.ModelProfileIn(name="OpenAI 备用", provider="openai",
                                                  model="gpt-4o", base_url="https://api.openai.com/v1",
                                                  api_key="sk-oa-2")))
        # 首个自动激活
        assert cs.get(m.ACTIVE_MODEL_KEY) == r1["id"]

        # 加密落库: 直接查表, 密文无明文
        row = cs._conn().execute("SELECT value FROM app_settings WHERE key=?", (m.MODEL_PROFILES_KEY,)).fetchone()
        assert row is not None and "sk-ds-1" not in row[0] and "sk-oa-2" not in row[0]

        # 列表脱敏
        lst = _run(m.list_models())
        assert len(lst["profiles"]) == 2
        assert all(p["api_key_set"] is True for p in lst["profiles"])
        assert lst["active_id"] == r1["id"]

        # 切换
        _run(m.activate_model(r2["id"]))
        assert cs.get(m.ACTIVE_MODEL_KEY) == r2["id"]
        active = m.get_active_profile()
        assert active["name"] == "OpenAI 备用" and active["api_key"] == "sk-oa-2"

        # 更新 (api_key 留空不变)
        _run(m.update_model(r1["id"], m.ModelProfileIn(name="DeepSeek 主力", provider="deepseek",
                                                       model="deepseek-v4-pro")))
        p1 = next(p for p in m._load_profiles() if p["id"] == r1["id"])
        assert p1["model"] == "deepseek-v4-pro" and p1["api_key"] == "sk-ds-1"

        # 删除激活中的档案 → 回退
        _run(m.delete_model(r2["id"]))
        assert cs.get(m.ACTIVE_MODEL_KEY, None) is None

        # 删除全部 → 空
        _run(m.delete_model(r1["id"]))
        assert m._load_profiles() == []
    finally:
        cs.delete(m.MODEL_PROFILES_KEY)
        cs.delete(m.ACTIVE_MODEL_KEY)


def test_get_llm_uses_active_profile():
    """get_llm 按激活档案构建 (provider=deepseek → DeepSeekChatOpenAI)"""
    import asyncio

    from dia.api.v1 import models as m
    from dia.core import base
    from dia.infrastructure import config_store as cs

    cs.delete(m.MODEL_PROFILES_KEY)
    cs.delete(m.ACTIVE_MODEL_KEY)
    base._llm_instances.clear()
    try:
        rid = asyncio.run(m.create_model(m.ModelProfileIn(
            name="T", provider="deepseek", model="deepseek-chat", api_key="sk-x")))["id"]
        async def _get():
            return await base.get_llm(temperature=0.3)
        llm = asyncio.run(_get())
        assert llm.model_name == "deepseek-chat"
        assert isinstance(llm, base.DeepSeekChatOpenAI)
    finally:
        cs.delete(m.MODEL_PROFILES_KEY)
        cs.delete(m.ACTIVE_MODEL_KEY)
        base._llm_instances.clear()
