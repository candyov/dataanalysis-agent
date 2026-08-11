"""数据脱敏 — 在返回 LLM 前过滤敏感信息

规则:
- 身份证号  → *** 遮蔽
- 手机号    → *** 遮蔽
- 邮箱      → *** 遮蔽
- 银行卡号  → *** 遮蔽
- 固话      → *** 遮蔽
"""

import re
from typing import Any

# ── PII 正则 ──
_PII_PATTERNS: list[tuple[str, str]] = [
    (r"\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx]\b", "[身份证号]"),
    (r"\b1[3-9]\d{9}\b", "[手机号]"),
    (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[邮箱]"),
    (r"\b\d{16,19}\b", "[银行卡号]"),
    (r"\b0\d{2,3}-\d{7,8}\b", "[固话]"),
]

# ── 敏感列名 (下划线/中英文) ──
_SENSITIVE_COLUMN_KEYWORDS = [
    "id_number", "身份证", "身份证号", "social_id",
    "phone", "手机", "手机号", "telephone",
    "email", "邮箱", "mail",
    "password", "密码", "passwd",
    "address", "地址", "addr",
    "bank_card", "银行卡", "card_no",
    "real_name", "姓名", "name", "full_name",
]


def _mask_value(val: Any) -> Any:
    """对单个值做 PII 遮蔽."""
    if not isinstance(val, str):
        return val
    result = val
    for pattern, replacement in _PII_PATTERNS:
        result = re.sub(pattern, replacement, result)
    return result


def mask_string(text: str) -> str:
    """对字符串做 PII 遮蔽."""
    return _mask_value(text)


def sanitize_rows(rows: list[dict], columns: list[str] | None = None) -> list[dict]:
    """对查询结果行做脱敏:
    - 敏感列名 → 整列遮蔽为 ***
    - 非敏感列中的值 → 正则匹配后遮蔽
    """
    if not rows:
        return rows

    # 识别敏感列
    sensitive_indexes: set[int] = set()
    if columns:
        for idx, col in enumerate(columns):
            col_lower = col.lower().replace("_", " ")
            for kw in _SENSITIVE_COLUMN_KEYWORDS:
                if kw.lower().replace("_", " ") in col_lower:
                    sensitive_indexes.add(idx)
                    break

    cleaned: list[dict] = []
    for row in rows:
        cleaned_row: dict = {}
        if isinstance(row, dict):
            for idx, (col_name, val) in enumerate(row.items()):
                if columns and idx in sensitive_indexes:
                    cleaned_row[col_name] = "***"
                else:
                    cleaned_row[col_name] = _mask_value(val)
        else:
            # 非 dict 行 (极少见), 整体遮蔽
            cleaned_row = {"value": _mask_value(str(row))}
        cleaned.append(cleaned_row)

    return cleaned
