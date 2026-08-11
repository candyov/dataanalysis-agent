"""Trace ID 中间件 -- 每个 HTTP 请求自动分配 trace_id"""

import logging
import time
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from dia.infrastructure.observability.logging import set_trace_id, new_trace_id

logger = logging.getLogger(__name__)


class TraceMiddleware(BaseHTTPMiddleware):
    """为每个请求注入 trace_id 并记录请求耗时"""

    async def dispatch(self, request: Request, call_next):
        tid = new_trace_id()
        set_trace_id(tid)
        request.state.trace_id = tid  # 供 SSE 生成器在独立 task 中恢复

        start = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - start) * 1000

        logger.info(
            "request completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "elapsed_ms": round(elapsed_ms, 1),
            },
        )

        # 在响应头中加入 trace_id,方便前端调试
        response.headers["X-Trace-Id"] = tid
        return response
