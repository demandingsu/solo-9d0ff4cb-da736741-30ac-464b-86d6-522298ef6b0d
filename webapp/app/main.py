"""FastAPI 入口：列表页地址录入、抓取归并、结果展示、页面代理。"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from .crawler import REQUEST_TIMEOUT, USER_AGENT, crawl
from .merger import merge_announcements

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="招标延期核对助手")


@app.get("/", response_class=HTMLResponse)
def index(request: Request, url: str | None = None) -> HTMLResponse:
    context = {
        "request": request,
        "entry_url": url or "",
        "report": None,
        "results": None,
    }
    if url and url.strip():
        report = crawl(url.strip())
        context["report"] = report
        context["results"] = merge_announcements(report.announcements)
    return templates.TemplateResponse("index.html", context)


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
