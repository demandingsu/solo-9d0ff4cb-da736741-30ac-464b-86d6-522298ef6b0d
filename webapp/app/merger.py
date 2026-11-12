"""按项目编号归并公告，按发布时间处理投标截止时间。

规则（需求口径）：
1. 同一项目编号下的公告按发布时间从早到晚处理。
2. 公告明确写出新的投标截止时间时，用新值替换旧值。
3. 较晚的更正/延期公告声明延期，却没有给出新截止时间时，
   结果标记为"待核实"，且不再展示/沿用旧截止时间。
4. 从未出现过截止时间的项目标记为"未公布"。
"""
from __future__ import annotations

import re

from .models import Announcement, ProjectResult, ResultStatus
from .utils import parse_time

_CORRECTION_TYPE_RE = re.compile(r"更正|变更|澄清|修改|延期|顺延|补充")


def _is_correction(ann: Announcement) -> bool:
    return bool(_CORRECTION_TYPE_RE.search(ann.notice_type)) or ann.declares_extension


def _extract_project_name(ann: Announcement) -> str:
    """从公告标题中尽量取出项目名称。"""
    if not ann.title:
        return ""
    name = re.sub(r"[-_—–|].{0,4}(?:招标|采购|更正|变更|澄清|修改|延期|顺延|中标|成交|终止).{0,4}公告$", "", ann.title)
    name = re.sub(r"^(?:.*?关于)?(.*?)(?:项目)?(?:的)?(?:招标|采购|更正|变更|澄清|修改|延期|顺延|中标|成交|终止|废标)公告.*$", r"\1", name)
    return name.strip("-_—–| ")


def merge_announcements(announcements: list[Announcement]) -> list[ProjectResult]:
    grouped: dict[str, list[Announcement]] = {}
    for ann in announcements:
        grouped.setdefault(ann.project_no, []).append(ann)

    results: list[ProjectResult] = []
    for project_no, items in grouped.items():
        # 按发布时间升序；解析不出时间的排到最后，保证"较晚公告"判定稳定
        items.sort(key=lambda a: (parse_time(a.publish_time) is None, parse_time(a.publish_time) or 0))

        current_deadline: str | None = None
        pending = False
        pending_note = ""

        for ann in items:
            if ann.deadline:
                # 明确给出新截止时间 -> 替换旧值，并解除待核实
                current_deadline = ann.deadline
                pending = False
                pending_note = ""
            elif ann.declares_extension and _is_correction(ann):
                # 声明延期却没有新时间 -> 待核实，旧时间失效
                current_deadline = None
                pending = True
                pending_note = (
                    f"较晚公告（{ann.notice_type or '更正公告'}，{ann.publish_time or '发布时间未知'}）"
                    "声明延期但未写明新的投标截止时间，旧时间不再视为有效，需人工核实。"
                )

        if pending:
            status = ResultStatus.PENDING
            note = pending_note
        elif current_deadline:
            status = ResultStatus.EFFECTIVE
            note = "当前截止时间取自主明截止时间的最新公告。"
        else:
            status = ResultStatus.UNKNOWN
            note = "已抓取公告中均未公布投标截止时间。"

        results.append(
            ProjectResult(
                project_no=project_no,
                project_name=_extract_project_name(items[-1]) or items[-1].title,
                status=status,
                current_deadline=current_deadline,
                note=note,
                announcements=items,
            )
        )

    # 项目编号排序，保证展示稳定
    results.sort(key=lambda r: r.project_no)
    return results
