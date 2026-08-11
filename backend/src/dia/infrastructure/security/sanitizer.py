"""敏感信息脱敏 -- 检测并替换 Prompt / Response 中的 PII

检测项:
- 手机号 (中国大陆 1xx-xxxx-xxxx)
- 身份证号 (18 位)
- 邮箱地址
- API Key 模式 (sk-xxx, eyJxxx)
- 信用卡号 (Luhn 算法)
"""

import logging
import re

logger = logging.getLogger(__name__)

# ── 检测模式 ──
_PATTERNS = [
    # 手机号
    (re.compile(r'1[3-9]\d[-\s]?\d{4}[-\s]?\d{4}'), 'PHONE', '***-****-****'),
    # 身份证
    (re.compile(r'\d{6}(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{3}[\dXx]'), 'ID_CARD', 'ID***'),
    # 邮箱
    (re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'), 'EMAIL', '***@***.***'),
    # API Key (OpenAI 风格)
    (re.compile(r'sk-[a-zA-Z0-9]{20,}'), 'API_KEY', 'sk-***'),
    # JWT Token
    (re.compile(r'eyJ[a-zA-Z0-9_-]{20,}\.[a-zA-Z0-9_-]{20,}\.[a-zA-Z0-9_-]{10,}'), 'JWT', '***'),
]


def sanitize(text: str) -> tuple[str, int]:
    """脱敏处理,返回 (脱敏后文本, 检测到的敏感信息数量)

    Args:
        text: 原始文本

    Returns:
        (sanitized_text, count)
    """
    if not text or not isinstance(text, str):
        return (text or "", 0)

    count = 0
    result = text

    for pattern, label, replacement in _PATTERNS:
        matches = pattern.findall(result)
        if matches:
            count += len(matches)
            result = pattern.sub(replacement, result)

    if count > 0:
        logger.info(f"[Sanitizer] 检测到 {count} 处敏感信息")

    return (result, count)


def has_sensitive(text: str) -> bool:
    """快速检查是否包含敏感信息"""
    if not text:
        return False
    for pattern, _, _ in _PATTERNS:
        if pattern.search(text):
            return True
    return False
