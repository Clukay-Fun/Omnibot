from __future__ import annotations

import re
from datetime import datetime
from typing import Any

_DATE_ONLY_FORMATS = ("%Y/%m/%d", "%Y-%m-%d", "%Y.%m.%d")
_DATE_CN_RE = re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日$")
_CURRENCY_RE = re.compile(r"^[￥¥$]?\s*([+-]?\d+(?:\.\d+)?)\s*([万wW千kK]?)(?:元|rmb|人民币)?$", re.IGNORECASE)


def normalize_date_string(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    for fmt in _DATE_ONLY_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    match = _DATE_CN_RE.fullmatch(text)
    if match:
        year, month, day = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        try:
            return datetime(year=year, month=month, day=day).date().isoformat()
        except ValueError:
            return value
    return value


def normalize_amount_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip().replace(",", "")
    if not text:
        return value
    match = _CURRENCY_RE.fullmatch(text)
    if not match:
        return value
    number = float(match.group(1))
    unit = str(match.group(2) or "").strip().lower()
    multiplier = 1
    if unit in {"万", "w"}:
        multiplier = 10000
    elif unit in {"千", "k"}:
        multiplier = 1000
    normalized = number * multiplier
    if normalized.is_integer():
        return int(normalized)
    return normalized


def normalize_option_value(value: Any, options: list[str]) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or not options:
        return value
    if text in options:
        return text
    for option in options:
        if text in option or option in text:
            return option
    lowered = text.lower()
    for option in options:
        candidate = option.strip().lower()
        if lowered == candidate:
            return option
    return value
