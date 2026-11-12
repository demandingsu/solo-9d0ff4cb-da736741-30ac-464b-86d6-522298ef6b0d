"""通用工具：文本清洗、时间解析、同源判断。"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlparse

_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)


def strip_label(text: str) -> str:
    """去掉字段标签末尾的冒号与空白。"""
    return re.sub(r"[\s:：*]+$", "", text or "").strip()


def parse_time(text: str) -> datetime | None:
    """尽量把中文/常见格式的时间文本解析为 datetime，失败返回 None。"""
    if not text:
        return None
    normalized = (
        text.strip()
        .replace("年", "-")
        .replace("月", "-")
        .replace("日", " ")
        .replace("时", ":")
        .replace("分", "")
        .replace("/", "-")
        .replace(".", "-")
    )
    normalized = re.sub(r"\s+", " ", normalized).strip()
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    # 最后兜底：直接抓取日期片段
    match = re.search(r"(\d{4}-\d{1,2}-\d{1,2})(?:[ T](\d{1,2}:\d{2}(?::\d{2})?))?", normalized)
    if match:
        fragment = match.group(1) + ((" " + match.group(2)) if match.group(2) else "")
        for fmt in _TIME_FORMATS:
            try:
                return datetime.strptime(fragment, fmt)
            except ValueError:
                continue
    return None


def same_origin(url_a: str, url_b: str) -> bool:
    """判断两个 URL 是否同站点（协议+主机+端口一致）。"""
    parsed_a, parsed_b = urlparse(url_a), urlparse(url_b)
    if not parsed_a.scheme or not parsed_a.netloc:
        return False
    return (parsed_a.scheme, parsed_a.netloc) == (parsed_b.scheme, parsed_b.netloc)
