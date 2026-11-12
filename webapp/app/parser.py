"""详情页解析：按字段标签提取项目编号、公告类型、发布时间、投标截止时间。

解析不依赖固定页面结构，按"字段标签 -> 值"的思路依次尝试：
1. 表格行（th/td、tr 内含两个单元格）
2. dl/dt/dd 定义列表
3. 带 label 的表单项（label + input/textarea）
4. 形如「字段标签：值」的正文段落 / div
并在标签缺失时用正则兜底，以兼容不同站点的模板写法。
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from .models import Announcement
from .utils import strip_label

# 字段标签别名 -> 规范键
_LABEL_ALIASES: dict[str, str] = {
    "项目编号": "project_no",
    "招标项目编号": "project_no",
    "采购项目编号": "project_no",
    "项目编码": "project_no",
    "公告类型": "notice_type",
    "类型": "notice_type",
    "发布时间": "publish_time",
    "公告发布时间": "publish_time",
    "发布日期": "publish_time",
    "投标截止时间": "deadline",
    "投标截止日期": "deadline",
    "开标时间": "deadline",
    "投标文件递交截止时间": "deadline",
    "截止时间": "deadline",
}

_LABEL_NAMES = sorted(_LABEL_ALIASES, key=len, reverse=True)
_LABEL_PATTERN = re.compile("|".join(re.escape(name) for name in _LABEL_NAMES))

# 兜底正则
_PROJECT_NO_RE = re.compile(r"(?:项目编号|项目编码)[：:\s]*([0-9A-Za-z][0-9A-Za-z\-_/（）()\.]{2,40})")
_PUBLISH_RE = re.compile(r"发布(?:时间|日期)[：:\s]*([0-9]{4}[年/\-.][0-9]{1,2}[月/\-.][0-9]{1,2}日?(?:\s*[0-9]{1,2}[:：][0-9]{2}(?:[:：][0-9]{2})?)?)")
_DEADLINE_RE = re.compile(r"(?:投标|递交)?(?:文件)?截止(?:时间|日期)[：:\s]*([0-9]{4}[年/\-.][0-9]{1,2}[月/\-.][0-9]{1,2}日?(?:\s*[0-9]{1,2}[:：][0-9]{2}(?:[:：][0-9]{2})?)?)")
_TYPE_RE = re.compile(r"公告类型[：:\s]*([^\s<，,。；;]{2,12}公告)")

# 声明延期的正文特征
_EXTENSION_PATTERNS = (
    re.compile(r"(?:投标|开标|响应|递交|报名).{0,12}(?:截止|时间).{0,10}(?:延期|顺延|推迟|延后)"),
    re.compile(r"(?:延期|顺延|推迟|延后).{0,12}(?:投标|开标|响应|递交|截止)"),
    re.compile(r"(?:延期|顺延|推迟).{0,6}(?:公告|通知)"),
)


def _map_label(label_text: str) -> str | None:
    key = strip_label(label_text)
    return _LABEL_ALIASES.get(key)


def _collect_labeled_pairs(soup: BeautifulSoup) -> dict[str, str]:
    """从多种 HTML 结构中收集 字段标签 -> 值，每个规范键保留第一个非空值。"""
    pairs: dict[str, str] = {}

    def put(label: str, value: str) -> None:
        key = _map_label(label)
        if key and value.strip() and key not in pairs:
            pairs[key] = value.strip()

    # 1. 表格：th+td，或一行两个 td
    for row in soup.select("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) >= 2:
            put(cells[0].get_text(" ", strip=True), cells[1].get_text(" ", strip=True))
        if len(cells) >= 4:  # 一行两对字段
            put(cells[0].get_text(" ", strip=True), cells[1].get_text(" ", strip=True))
            put(cells[2].get_text(" ", strip=True), cells[3].get_text(" ", strip=True))

    # 2. dl/dt/dd
    for dl in soup.find_all("dl"):
        dts, dds = dl.find_all("dt"), dl.find_all("dd")
        for dt, dd in zip(dts, dds):
            put(dt.get_text(" ", strip=True), dd.get_text(" ", strip=True))

    # 3. label + 输入控件
    for label in soup.find_all("label"):
        target_id = label.get("for")
        control = soup.find(attrs={"id": target_id}) if target_id else label.find_next(["input", "textarea", "select"])
        if control is not None:
            value = control.get("value") if control.name == "input" else control.get_text(" ", strip=True)
            if value:
                put(label.get_text(" ", strip=True), str(value))

    # 4. 正文文本中的「标签：值」
    for element in soup.find_all(["p", "div", "li", "span"]):
        # 只取直接文本，避免父容器把子段落重复收进来
        own_text = "".join(str(child) for child in element.children if not getattr(child, "name", None))
        own_text += element.string or ""
        for match in re.finditer(
            r"([一-龥A-Za-z]{2,12}?)\s*[：:]\s*([^\n\r<，,。；;]{1,60})",
            own_text,
        ):
            put(match.group(1), match.group(2))

    return pairs


def parse_detail(html: str, url: str) -> Announcement:
    """解析一个公告详情页，返回 Announcement。

    页面没有项目编号时抛出 ValueError，调用方据此跳过非公告页。
    """
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    heading = soup.find(["h1", "h2", "h3"])
    if heading:
        heading_text = heading.get_text(" ", strip=True)
        if heading_text and len(heading_text) >= 4:
            title = heading_text

    fields = _collect_labeled_pairs(soup)
    full_text = soup.get_text(" ", strip=True)

    # 标签缺失时的正则兜底
    if "project_no" not in fields:
        match = _PROJECT_NO_RE.search(full_text)
        if match:
            fields["project_no"] = match.group(1).strip()
    if "publish_time" not in fields:
        match = _PUBLISH_RE.search(full_text)
        if match:
            fields["publish_time"] = match.group(1).strip()
    if "deadline" not in fields:
        match = _DEADLINE_RE.search(full_text)
        if match:
            fields["deadline"] = match.group(1).strip()
    if "notice_type" not in fields:
        match = _TYPE_RE.search(full_text)
        if match:
            fields["notice_type"] = match.group(1).strip()

    project_no = fields.get("project_no", "").strip()
    if not project_no:
        raise ValueError(f"页面未找到项目编号字段，可能不是公告详情页: {url}")

    notice_type = fields.get("notice_type", "")
    # 标题里的公告类型也可作为依据
    type_in_title = re.search(r"(招标|采购|更正|变更|澄清|修改|延期|顺延|中标|成交|终止|废标)公告", title)
    if not notice_type and type_in_title:
        notice_type = type_in_title.group(0)

    declares_extension = any(pattern.search(full_text) for pattern in _EXTENSION_PATTERNS)
    if re.search(r"延期|顺延", notice_type):
        declares_extension = True

    deadline = fields.get("deadline") or None

    return Announcement(
        url=url,
        project_no=project_no,
        title=title,
        notice_type=notice_type,
        publish_time=fields.get("publish_time", ""),
        deadline=deadline,
        declares_extension=declares_extension,
    )
