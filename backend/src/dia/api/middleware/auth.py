"""API 鉴权中间件 — 单用户 API-Key 保护 (生产方向).

安全模型:
- 所有 /api/v1 路由必须携带 X-API-Key 请求头, 与配置的 APP_API_KEY 一致
- APP_API_KEY 未配置 (空) 时放行 — 兼容本地开发/内网部署
- 固定字符串比较 (非时间恒定) — 单用户工具, 无暴力破解面 (本地信任域)
- 静态挂载 (/output) 与 /health 放行 (健康检查无敏感数据)

启用方式 (backend/.env):
    APP_API_KEY=your-secret-token
"""
import logging

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from dia.core.config import settings

logger = logging.getLogger(__name__)

# 放行路径: 健康检查 (无敏感数据) + 静态资源挂载
_PUBLIC_PREFIXES = ("/health", "/docs", "/redoc", "/openapi.json", "/output/")


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """校验 X-API-Key 请求头. APP_API_KEY 未配置时全部放行."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # 静态资源与健康检查放行 (无敏感数据, 且前端 iframe/浏览器直接加载需要)
        if any(path.startswith(p) for p in _PUBLIC_PREFIXES):
            return await call_next(request)

        expected = settings.APP_API_KEY
        if expected:
            provided = request.headers.get("X-API-Key", "")
            if provided != expected:
                from fastapi.responses import JSONResponse
                return JSONResponse(
                    status_code=401,
                    content={"detail": "无效或缺失 API Key. 请在请求头携带 X-API-Key."},
                    headers={"WWW-Authenticate": "ApiKey"},
                )
        return await call_next(request)
