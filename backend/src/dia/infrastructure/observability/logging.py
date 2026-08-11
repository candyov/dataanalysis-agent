"""结构化日志配置 -- trace_id 贯穿全链路

用法:
- 普通模块: logger = logging.getLogger(__name__),自动带上 trace_id
- 需要写 trace_id: from dia.infrastructure.observability.logging import get_trace_id, set_trace_id
- FastAPI 通过 TraceMiddleware 自动注入
"""

import json
import logging
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Optional

_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="no-trace")

LOG_RECORD_BUILTIN_ATTRS = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "module", "msecs",
    "message", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName",
}


def set_trace_id(tid: str) -> None:
    """设置当前上下文的 trace_id"""
    _trace_id_var.set(tid)


def get_trace_id() -> str:
    """获取当前上下文的 trace_id"""
    return _trace_id_var.get()


def new_trace_id() -> str:
    """生成新的 trace_id(12 位短 UUID)"""
    return uuid.uuid4().hex[:12]


class StructuredFormatter(logging.Formatter):
    """JSON 行格式化器,自动注入 trace_id"""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "trace_id": get_trace_id(),
            "message": record.getMessage(),
        }

        # 附加 extra 字段
        extra_fields = {
            k: v
            for k, v in record.__dict__.items()
            if k not in LOG_RECORD_BUILTIN_ATTRS and not k.startswith("_")
        }
        if extra_fields:
            log_entry["extra"] = extra_fields

        # 异常信息
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = str(record.exc_info[1])

        return json.dumps(log_entry, ensure_ascii=False, default=str)


def setup_logging(level: int = logging.INFO) -> None:
    """初始化全局日志配置

    - 控制台输出 JSON 行格式(开发环境)
    - 日志级别可通过 settings 控制
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # 清除已有 handler
    root_logger.handlers.clear()

    handler = logging.StreamHandler()
    handler.setFormatter(StructuredFormatter())
    root_logger.addHandler(handler)

    # 抑制第三方库的噪音日志
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    # 确保 app 模块日志级别
    logging.getLogger("app").setLevel(level)
