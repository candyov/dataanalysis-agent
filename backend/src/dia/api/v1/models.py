"""模型档案管理 API — 多模型切换 (生产方向).

档案: {id, name, provider, base_url, model, api_key(加密), temperature}
激活: ACTIVE_MODEL 指针 → get_llm 按档案构建 (切换即时生效, 无需重启).

兼容: 未配置档案时回退旧 LLM_* 动态配置 → 环境变量 → 默认.
"""
import logging
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from dia.infrastructure import config_store

logger = logging.getLogger(__name__)
router = APIRouter()

MODEL_PROFILES_KEY = "MODEL_PROFILES"
ACTIVE_MODEL_KEY = "ACTIVE_MODEL"

# 提供商预设: 选 provider 自动填 base_url
PROVIDER_PRESETS = {
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "default_model": "deepseek-chat"},
    "openai": {"base_url": "https://api.openai.com/v1", "default_model": "gpt-4o"},
    "ollama": {"base_url": "http://localhost:11434/v1", "default_model": "llama3.1"},
    "custom": {"base_url": "", "default_model": ""},
}


class ModelProfileIn(BaseModel):
    name: str
    provider: str = "custom"
    base_url: str = ""
    model: str = ""
    api_key: str = ""      # 空 = 不变 (更新时); 创建时必填或留空(用环境变量)
    temperature: float = 0.2


def _load_profiles() -> list[dict]:
    raw = config_store.get(MODEL_PROFILES_KEY)
    if not raw:
        return []
    try:
        profiles = __import__("json").loads(raw)
        return profiles if isinstance(profiles, list) else []
    except Exception:
        logger.warning("[Models] 模型档案解析失败, 视为空")
        return []


def _save_profiles(profiles: list[dict]) -> None:
    config_store.set(MODEL_PROFILES_KEY, __import__("json").dumps(profiles, ensure_ascii=False))


def _profile_dict(p: dict) -> dict:
    """API 返回 (api_key 脱敏)."""
    return {
        "id": p.get("id", ""),
        "name": p.get("name", ""),
        "provider": p.get("provider", "custom"),
        "base_url": p.get("base_url", ""),
        "model": p.get("model", ""),
        "api_key_set": bool(p.get("api_key")),
        "temperature": p.get("temperature", 0.2),
    }


@router.get("/models")
async def list_models():
    """模型档案列表 (api_key 脱敏) + 当前激活 id."""
    profiles = _load_profiles()
    return {
        "profiles": [_profile_dict(p) for p in profiles],
        "active_id": config_store.get(ACTIVE_MODEL_KEY, ""),
        "presets": PROVIDER_PRESETS,
    }


@router.post("/models")
async def create_model(body: ModelProfileIn):
    """创建模型档案 (API Key 加密存储)."""
    if not body.name.strip():
        raise HTTPException(400, "档案名称不能为空")
    profiles = _load_profiles()
    profile = {
        "id": str(uuid.uuid4())[:8],
        "name": body.name.strip(),
        "provider": body.provider,
        "base_url": body.base_url or PROVIDER_PRESETS.get(body.provider, {}).get("base_url", ""),
        "model": body.model,
        "api_key": body.api_key,
        "temperature": body.temperature,
    }
    profiles.append(profile)
    _save_profiles(profiles)
    # 首个档案自动激活
    if not config_store.get(ACTIVE_MODEL_KEY):
        config_store.set(ACTIVE_MODEL_KEY, profile["id"])
        logger.info(f"[Models] 首个档案 {profile['name']} 自动激活")
    logger.info(f"[Models] 创建档案: {profile['name']} ({body.provider})")
    return {"status": "ok", "id": profile["id"]}


@router.put("/models/{profile_id}")
async def update_model(profile_id: str, body: ModelProfileIn):
    """更新档案 (api_key 留空 = 不变)."""
    profiles = _load_profiles()
    for p in profiles:
        if p.get("id") == profile_id:
            p["name"] = body.name.strip() or p["name"]
            p["provider"] = body.provider
            p["base_url"] = body.base_url or PROVIDER_PRESETS.get(body.provider, {}).get("base_url", "")
            p["model"] = body.model
            if body.api_key:
                p["api_key"] = body.api_key
            p["temperature"] = body.temperature
            _save_profiles(profiles)
            return {"status": "ok", "id": profile_id}
    raise HTTPException(404, f"模型档案不存在: {profile_id}")


@router.delete("/models/{profile_id}")
async def delete_model(profile_id: str):
    """删除档案 (删除激活中的档案 → 回退旧配置链)."""
    profiles = _load_profiles()
    remaining = [p for p in profiles if p.get("id") != profile_id]
    if len(remaining) == len(profiles):
        raise HTTPException(404, f"模型档案不存在: {profile_id}")
    _save_profiles(remaining)
    if config_store.get(ACTIVE_MODEL_KEY) == profile_id:
        config_store.delete(ACTIVE_MODEL_KEY)
        logger.info(f"[Models] 档案 {profile_id} 已删除, 激活回退旧配置")
    return {"status": "ok"}


@router.post("/models/{profile_id}/activate")
async def activate_model(profile_id: str):
    """切换当前模型 (即时生效: get_llm 每次按激活档案构建)."""
    profiles = _load_profiles()
    if not any(p.get("id") == profile_id for p in profiles):
        raise HTTPException(404, f"模型档案不存在: {profile_id}")
    config_store.set(ACTIVE_MODEL_KEY, profile_id)
    name = next(p["name"] for p in profiles if p.get("id") == profile_id)
    logger.info(f"[Models] 已切换到模型: {name}")
    return {"status": "ok", "active_id": profile_id}


@router.post("/models/{profile_id}/test")
async def test_model(profile_id: str):
    """测试指定档案的模型连通性 (最小调用)."""
    profiles = _load_profiles()
    profile = next((p for p in profiles if p.get("id") == profile_id), None)
    if not profile:
        raise HTTPException(404, f"模型档案不存在: {profile_id}")
    try:
        from dia.core.base import build_llm
        llm = build_llm(profile=profile, temperature=0.1)
        resp = await llm.ainvoke("回复 OK 两个字")
        text = (resp.content or "").strip()
        return {"ok": True, "message": f"连接成功, 模型响应: {text[:50]}"}
    except Exception as e:
        return {"ok": False, "message": f"连接失败: {str(e)}"}


def get_active_profile() -> dict | None:
    """读取激活档案 (供 get_llm 使用)."""
    active_id = config_store.get(ACTIVE_MODEL_KEY, "")
    if not active_id:
        return None
    for p in _load_profiles():
        if p.get("id") == active_id:
            return p
    return None
