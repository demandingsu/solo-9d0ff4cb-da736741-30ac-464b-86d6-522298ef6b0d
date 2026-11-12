"""数据模型：公告与项目归并结果。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ResultStatus(str, Enum):
    """项目当前截止时间的处理结果。"""

    EFFECTIVE = "有效"          # 有明确截止时间且未被"延期但无新时间"的公告覆盖
    PENDING = "待核实"          # 较晚的延期/更正公告声明延期，却没有给出新截止时间
    UNKNOWN = "未公布"          # 所有公告均未给出截止时间


@dataclass
class Announcement:
    """单条招标公告（从详情页按字段标签解析得到）。"""

    url: str
    project_no: str
    title: str = ""
    notice_type: str = ""       # 公告类型，如 招标公告 / 更正公告 / 延期公告
    publish_time: str = ""      # 原始发布时间文本
    deadline: Optional[str] = None   # 投标截止时间原文；未给出时为 None
    declares_extension: bool = False  # 正文是否明确声明延期/顺延


@dataclass
class ProjectResult:
    """按项目编号归并后的当前结果。"""

    project_no: str
    project_name: str = ""
    status: ResultStatus = ResultStatus.UNKNOWN
    current_deadline: Optional[str] = None   # 当前有效的投标截止时间；待核实时为 None
    note: str = ""                            # 状态说明
    announcements: list[Announcement] = field(default_factory=list)
