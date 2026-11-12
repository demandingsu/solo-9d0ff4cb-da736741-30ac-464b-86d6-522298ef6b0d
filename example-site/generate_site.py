"""生成本地示例招标站点（纯静态 HTML，构建时运行一次）。

覆盖的业务场景：
  P1 更正公告给出新截止时间        -> 有效，截止时间被替换
  P2 延期公告给出新截止时间        -> 有效，截止时间被替换
  P3 延期公告声明延期但未给新时间  -> 待核实，旧时间不再显示
  P4 两次更正：先改时间、后延期无时间 -> 待核实
  P5 只有原始招标公告              -> 有效，沿用原截止时间
  P6 公告未公布任何截止时间        -> 未公布
列表页分两页，通过“下一页”链接串联，验证分页抓取。
"""
from __future__ import annotations

import html
import random
from pathlib import Path

OUTPUT = Path(__file__).resolve().parent / "site"

# (项目编号, 项目名称, 公告类型, 发布时间, 截止时间或 None, 标题, 正文补充)
NOTICES: list[tuple[str, str, str, str, str | None, str, str]] = [
    # --- P1：更正公告写明新截止时间 ---
    ("CG-2026-001", "市政道路养护设备采购", "招标公告", "2026-10-08 09:00:00",
     "2026-10-30 14:30:00", "市政道路养护设备采购招标公告",
     "欢迎符合条件的供应商参加投标。"),
    ("CG-2026-001", "市政道路养护设备采购", "更正公告", "2026-10-15 16:20:00",
     "2026-11-06 10:00:00", "市政道路养护设备采购更正公告",
     "因采购需求调整，现将投标截止时间延期至更正公告载明的新时间，请各投标人知悉。"),

    # --- P2：延期公告写明新截止时间 ---
    ("CG-2026-002", "校园智慧图书馆系统采购", "招标公告", "2026-09-25 10:00:00",
     "2026-10-20 09:30:00", "校园智慧图书馆系统采购项目招标公告",
     "本项目采用公开招标方式，详见招标文件。"),
    ("CG-2026-002", "校园智慧图书馆系统采购", "延期公告", "2026-10-16 11:00:00",
     "2026-10-28 15:00:00", "校园智慧图书馆系统采购项目延期公告",
     "因接到答疑申请，投标截止时间顺延至本公告载明的新时间，开标时间同步调整。"),

    # --- P3：延期公告声明延期，但没有给出新截止时间 ---
    ("CG-2026-003", "医院影像设备维保服务采购", "招标公告", "2026-10-02 08:45:00",
     "2026-10-25 09:00:00", "医院影像设备维保服务采购招标公告",
     "投标人须在截止时间前完成电子投标文件递交。"),
    ("CG-2026-003", "医院影像设备维保服务采购", "延期公告", "2026-10-21 17:30:00",
     None, "医院影像设备维保服务采购延期公告",
     "因评审安排冲突，本项目投标截止时间需要延期，具体新的投标截止时间将另行公告通知。"),

    # --- P4：两次更正，先给新时间、后只声明延期 ---
    ("CG-2026-004", "政务云平台扩容采购", "招标公告", "2026-09-20 09:00:00",
     "2026-10-12 14:00:00", "政务云平台扩容采购招标公告",
     "本项目不接受联合体投标。"),
    ("CG-2026-004", "政务云平台扩容采购", "更正公告", "2026-10-01 15:10:00",
     "2026-10-22 14:00:00", "政务云平台扩容采购更正公告",
     "现对招标文件部分参数进行更正，投标截止时间相应延期至新时间。"),
    ("CG-2026-004", "政务云平台扩容采购", "延期公告", "2026-10-19 10:05:00",
     None, "政务云平台扩容采购延期公告",
     "因上级系统升级，开标场地无法按时使用，本项目投标及开标将顺延，新时间另行通知。"),

    # --- P5：仅有原始招标公告 ---
    ("CG-2026-005", "公办幼儿园教具采购", "招标公告", "2026-10-10 13:30:00",
     "2026-11-02 09:30:00", "公办幼儿园教具采购招标公告",
     "欢迎合格供应商参与投标。"),

    # --- P6：公告未公布任何投标截止时间 ---
    ("CG-2026-006", "档案馆数字化加工服务采购", "招标公告", "2026-10-12 10:40:00",
     None, "档案馆数字化加工服务采购招标公告",
     "本项目采用竞争性方式采购，具体投标安排详见后续通知。"),
]

FIELD_LABELS = [
    ("project_no", "项目编号"),
    ("notice_type", "公告类型"),
    ("publish_time", "发布时间"),
    ("deadline", "投标截止时间"),
]


def detail_filename(index: int) -> str:
    return f"notice_{index:03d}.html"


def build_detail(notice: tuple, index: int) -> str:
    project_no, project_name, notice_type, publish_time, deadline, title, body = notice
    rng = random.Random(index)

    # 打乱字段顺序，模拟不同页面模板；无新时间的延期页不输出截止时间字段
    labels = [item for item in FIELD_LABELS if not (item[0] == "deadline" and deadline is None)]
    rng.shuffle(labels)

    rows = [f"<tr><th>项目名称</th><td>{html.escape(project_name)}</td></tr>"]
    for key, label in labels:
        value = {
            "project_no": project_no,
            "notice_type": notice_type,
            "publish_time": publish_time,
            "deadline": deadline,
        }[key]
        rows.append(f"<tr><th>{label}</th><td>{html.escape(str(value))}</td></tr>")
    rng.shuffle(rows)
    table = "\n      ".join(rows)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="../assets/style.css">
</head>
<body>
<div class="wrap">
  <p class="crumb"><a href="../list/page_1.html">返回公告列表</a></p>
  <h1>{html.escape(title)}</h1>
  <table class="fields">
      {table}
  </table>
  <div class="content">
    <p>{html.escape(body)}</p>
    <p>采购代理人请以本平台正式公告内容为准，按时完成投标文件递交。</p>
  </div>
</div>
</body>
</html>
"""


def build_list_page(page_no: int, page_items: list[tuple], has_next: bool) -> str:
    items_html = []
    for index, notice in page_items:
        title = notice[5]
        publish_time = notice[3]
        href = f"../detail/{detail_filename(index)}"
        items_html.append(
            f'    <li><a class="notice-link" href="{href}">'
            f'<span class="t">{html.escape(title)}</span>'
            f'<span class="d">{publish_time}</span></a></li>'
        )
    pager = (
        '<div class="pager">'
        f'<a class="next-page" href="page_{page_no + 1}.html">下一页</a>'
        "</div>"
        if has_next
        else '<div class="pager"><span class="disabled">下一页</span></div>'
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>招标采购公告列表 - 第{page_no}页</title>
<link rel="stylesheet" href="../assets/style.css">
</head>
<body>
<div class="wrap">
  <h1>招标采购公告列表</h1>
  <ul class="notice-list">
{chr(10).join(items_html)}
  </ul>
{pager}
</div>
</body>
</html>
"""


def build_index() -> str:
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>示例采购信息平台</title>
<link rel="stylesheet" href="assets/style.css">
</head>
<body>
<div class="wrap">
  <h1>示例采购信息平台</h1>
  <p><a href="list/page_1.html">进入招标采购公告列表</a></p>
</div>
</body>
</html>
"""


CSS = """
body { margin:0; background:#f5f7fa; font-family:"PingFang SC","Microsoft YaHei",sans-serif; color:#1f2937; }
.wrap { max-width:960px; margin:24px auto; background:#fff; padding:28px 36px; border-radius:10px; box-shadow:0 1px 4px rgba(0,0,0,.08); }
h1 { font-size:20px; margin:0 0 18px; }
.crumb a { color:#1d4ed8; text-decoration:none; font-size:13px; }
table.fields { border-collapse:collapse; width:100%; margin:16px 0; }
table.fields th, table.fields td { border:1px solid #d7dde5; padding:9px 12px; font-size:14px; text-align:left; }
table.fields th { background:#f3f6fb; width:160px; white-space:nowrap; }
.content { line-height:1.8; font-size:14px; }
ul.notice-list { list-style:none; margin:0; padding:0; }
ul.notice-list li { border-bottom:1px solid #eef1f5; }
.notice-link { display:flex; justify-content:space-between; padding:13px 6px; text-decoration:none; color:#1f2937; }
.notice-link:hover { background:#f8fafc; }
.notice-link .t { font-size:14px; }
.notice-link .d { color:#64748b; font-size:13px; white-space:nowrap; margin-left:20px; }
.pager { margin-top:20px; text-align:right; }
.pager a, .pager .disabled { display:inline-block; padding:7px 16px; border:1px solid #cbd5e1; border-radius:6px; font-size:13px; }
.pager a { background:#1d4ed8; color:#fff; text-decoration:none; border-color:#1d4ed8; }
.pager .disabled { color:#94a3b8; background:#f1f5f9; }
"""


def main() -> None:
    (OUTPUT / "list").mkdir(parents=True, exist_ok=True)
    (OUTPUT / "detail").mkdir(parents=True, exist_ok=True)
    (OUTPUT / "assets").mkdir(parents=True, exist_ok=True)

    indexed = list(enumerate(NOTICES, start=1))
    # 列表页按发布时间倒序（最新在前），分两页
    ordered = sorted(indexed, key=lambda pair: pair[1][3], reverse=True)
    half = (len(ordered) + 1) // 2
    pages = [ordered[:half], ordered[half:]]

    for page_no, page_items in enumerate(pages, start=1):
        has_next = page_no < len(pages)
        (OUTPUT / "list" / f"page_{page_no}.html").write_text(
            build_list_page(page_no, page_items, has_next), encoding="utf-8"
        )

    for index, notice in indexed:
        (OUTPUT / "detail" / detail_filename(index)).write_text(
            build_detail(notice, index), encoding="utf-8"
        )

    (OUTPUT / "index.html").write_text(build_index(), encoding="utf-8")
    (OUTPUT / "assets" / "style.css").write_text(CSS, encoding="utf-8")
    print(f"示例站点已生成：{OUTPUT}（{len(NOTICES)} 条公告，{len(pages)} 个列表页）")


if __name__ == "__main__":
    main()
