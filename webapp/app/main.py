"""FastAPI 入口：列表页地址录入、抓取归并、结果展示、页面代理、发布到镜像站。"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from .crawler import REQUEST_TIMEOUT, USER_AGENT, crawl
from .merger import is_correction, merge_announcements
from .publisher import (
    confirm_pending,
    publish_announcement,
    recall_outcome,
    remember_outcome,
)
from .utils import parse_time

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="招标延期核对助手")


def _demo_mode() -> bool:
    return os.getenv("DEMO_MODE", "1").lower() in ("1", "true", "yes", "on")


def _is_truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


@app.get("/", response_class=HTMLResponse)
def index(request: Request, url: str | None = None) -> HTMLResponse:
    context = {
        "request": request,
        "entry_url": url or "",
        "report": None,
        "results": None,
        "corrections": None,
        "demo_mode": _demo_mode(),
    }
    if url and url.strip():
        report = crawl(url.strip())
        results = merge_announcements(report.announcements)
        context["report"] = report
        context["results"] = results

        # 本次抓取结果中的更正/延期公告，供选择一条发布到镜像站（最新在前）。
        corrections = [
            ann
            for result in results
            for ann in result.announcements
            if is_correction(ann)
        ]
        corrections.sort(
            key=lambda a: (parse_time(a.publish_time) is None, parse_time(a.publish_time) or 0),
            reverse=True,
        )
        context["corrections"] = corrections
    return templates.TemplateResponse("index.html", context)


@app.post("/publish")
def publish(
    project_no: str = Form(...),
    source_url: str = Form(...),
    body_text: str = Form(...),
    title: str = Form(""),
    notice_type: str = Form(""),
    publish_time: str = Form(""),
    entry_url: str = Form(""),
    simulate_interrupt: str = Form(""),
) -> RedirectResponse:
    """把选中的一条更正公告发布到公示镜像站（先查后发、回执中断复查）。"""
    if not _validate_proxy_url(source_url):
        raise HTTPException(status_code=400, detail="来源 URL 必须是 http(s) 合法地址")
    if not project_no.strip() or not body_text.strip():
        raise HTTPException(status_code=400, detail="项目编号与正文可见文字不能为空")

    outcome = publish_announcement(
        project_no=project_no.strip(),
        source_url=source_url.strip(),
        content=body_text,
        title=title.strip(),
        notice_type=notice_type.strip(),
        publish_time=publish_time.strip(),
        entry_url=entry_url.strip(),
        simulate_interrupt=_is_truthy(simulate_interrupt),
    )
    token = remember_outcome(outcome)
    # PRG：刷新结果页只读，不会再次提交发布请求。
    return RedirectResponse(url=f"/publish/result/{token}", status_code=303)


@app.post("/publish/confirm")
def publish_confirm(
    project_no: str = Form(...),
    source_url: str = Form(...),
    body_text: str = Form(...),
    title: str = Form(""),
    notice_type: str = Form(""),
    publish_time: str = Form(""),
    entry_url: str = Form(""),
    interrupted: str = Form(""),
) -> RedirectResponse:
    """待确认状态下的人工复查：只查询，不重发。"""
    if not _validate_proxy_url(source_url):
        raise HTTPException(status_code=400, detail="来源 URL 必须是 http(s) 合法地址")
    outcome = confirm_pending(
        project_no=project_no.strip(),
        source_url=source_url.strip(),
        content=body_text,
        title=title.strip(),
        notice_type=notice_type.strip(),
        publish_time=publish_time.strip(),
        entry_url=entry_url.strip(),
        interrupted=_is_truthy(interrupted),
    )
    token = remember_outcome(outcome)
    return RedirectResponse(url=f"/publish/result/{token}", status_code=303)


@app.get("/publish/result/{token}", response_class=HTMLResponse)
def publish_result(request: Request, token: str) -> HTMLResponse:
    outcome = recall_outcome(token)
    if outcome is None:
        return HTMLResponse(
            "<h1>发布结果已过期</h1><p>请返回核对页重新操作。</p>", status_code=404
        )
    return templates.TemplateResponse(
        "publish_result.html",
        {"request": request, "outcome": outcome, "demo_mode": _demo_mode()},
    )



@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _validate_proxy_url(target: str) -> str | None:
    parsed = urlparse(target)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return target


@app.get("/view", response_class=HTMLResponse)
def view(url: str) -> Response:
    """代理抓取目标 HTML 并重写链接，供宿主机浏览器查看容器内站点的公告。"""
    target = _validate_proxy_url(url)
    if not target:
        return HTMLResponse("仅支持 http(s) 地址", status_code=400)
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True,
                          headers={"User-Agent": USER_AGENT}) as client:
            resp = client.get(target)
    except httpx.HTTPError as exc:
        return HTMLResponse(f"代理抓取失败：{exc}", status_code=502)

    content_type = resp.headers.get("content-type", "")
    if "html" not in content_type.lower():
        return Response(content=resp.content, media_type=content_type or "application/octet-stream")

    soup = BeautifulSoup(resp.text, "html.parser")
    # 页面内资源走 /asset，跳转链接走 /view
    for tag, attr in (("a", "href"), ("link", "href"), ("script", "src"), ("img", "src")):
        for element in soup.find_all(tag, attrs={attr: True}):
            absolute = urljoin(str(resp.url), element[attr])
            if urlparse(absolute).scheme not in ("http", "https"):
                continue
            if tag == "a":
                element["href"] = f"/view?url={absolute}"
                element.attrs.pop("target", None)
            else:
                element[attr] = f"/asset?url={absolute}"

    note = soup.new_tag(
        "div",
        style="background:#fff7e6;border:1px solid #ffd591;padding:8px 12px;"
              "font-size:13px;color:#ad4e00;margin-bottom:12px;",
    )
    note.string = f"代理视图 · 原始地址：{resp.url}"
    if soup.body:
        soup.body.insert(0, note)
    return HTMLResponse(str(soup))


@app.get("/asset")
def asset(url: str) -> Response:
    target = _validate_proxy_url(url)
    if not target:
        return HTMLResponse("仅支持 http(s) 地址", status_code=400)
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True,
                          headers={"User-Agent": USER_AGENT}) as client:
            resp = client.get(target)
    except httpx.HTTPError as exc:
        return HTMLResponse(f"资源抓取失败：{exc}", status_code=502)
    return Response(content=resp.content, media_type=resp.headers.get("content-type", "application/octet-stream"))
