"""设置管理 API — 模型配置热更新 (生产方向).

GET /api/v1/settings        读全部动态配置 (敏感项脱敏)
PUT /api/v1/settings        更新 (API Key 传空 = 不变)
DELETE /api/v1/settings     清除某配置 (回退 env)

读取链: 动态配置 > 环境变量 > 代码默认 (get_llm 每次解析, 热生效无需重启).
"""
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from dia.core.config import settings as env_settings
from dia.infrastructure import config_store

logger = logging.getLogger(__name__)
router = APIRouter()

# 可动态配置的模型项: 前端设置面板写入
MODEL_KEYS = ("LLM_MODEL", "LLM_API_KEY", "LLM_API_BASE", "LLM_DEFAULT_TEMPERATURE")


class SettingsUpdate(BaseModel):
    LLM_MODEL: str | None = None
    LLM_API_KEY: str | None = None  # 空/None = 不变; 显式空串会清除? 约定: None 不变
    LLM_API_BASE: str | None = None
    LLM_DEFAULT_TEMPERATURE: float | None = None


@router.get("/settings")
async def get_settings():
    """读配置: 动态值 + 环境默认值 + 敏感项脱敏状态."""
    dyn = config_store.get_all()
    out = {
        "dynamic": dyn,
        "model": {
            "provider": "deepseek",
            "model": dyn.get("LLM_MODEL", {}).get("value", env_settings.LLM_MODEL),
            "base_url": dyn.get("LLM_API_BASE", {}).get("value", env_settings.LLM_API_BASE),
            "temperature": float(dyn.get("LLM_DEFAULT_TEMPERATURE", {}).get("value", env_settings.LLM_DEFAULT_TEMPERATURE)),
            # 敏感项: 只暴露是否已设置
            "api_key_set": bool(
                (dyn.get("LLM_API_KEY", {}).get("set") if "LLM_API_KEY" in dyn else False)
                or env_settings.LLM_API_KEY
            ),
        },
    }
    return out


@router.put("/settings")
async def update_settings(body: SettingsUpdate):
    """更新动态配置 (API Key 传 None = 不变; 传空串 = 清除回退 env)."""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(400, "没有需要更新的配置项")
    for key, value in updates.items():
        if key == "LLM_API_KEY" and value == "":
            config_store.delete(key)
            logger.info("[Settings] LLM_API_KEY 已清除 (回退环境变量)")
            continue
        config_store.set(key, str(value))
        logger.info(f"[Settings] 已更新 {key}")
    return {"status": "ok"}


@router.post("/settings/test")
async def test_model_connection():
    """测试当前模型配置连通性 (用动态配置发起一次最小调用)."""
    try:
        from dia.core.base import get_llm
        llm = await get_llm(temperature=0.1)
        resp = await llm.ainvoke("回复 OK 两个字")
        text = (resp.content or "").strip()
        return {"ok": True, "message": f"连接成功, 模型响应: {text[:50]}"}
    except Exception as e:
        return {"ok": False, "message": f"连接失败: {str(e)}"}
