"""公示镜像站 FastAPI 服务。

对外接口（供采购代理网页后端调用）：
- POST /api/publications          发布（按来源 URL 幂等）
- GET  /api/publications          按来源 URL 查询
- POST /api/publications/query    查询并判断内容是否一致
- GET  /notice/{id}               公示页面（镜像链接）
- GET  /                          本地示例控制台
"""
from __future__ import annotations

import html
import json
import os
import threading

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from .storage import MirrorStore, Publication, content_hash

INTERRUPT_HEADERS = ("x-simulate-interrupt", "x-simulate-receipt-interruption")
QUERY_FAIL_HEADER = "x-simulate-query-failure"


class PublicationIn(BaseModel):
    project_no: str
    source_url: str
    content: str
    title: str = ""


class QueryIn(BaseModel):
    source_url: str
    project_no: str = ""
    content: str | None = None


class InterruptedResponse(Response):
    """先写入数据库，再发送一段不完整的响应体并断开连接。

    客户端会收到传输层错误（读超时 / 远端断开 / 不完整响应），
    因而无法确定发布是否成功——这正是"回执中断"的场景。
    """

    def __init__(self, payload: dict) -> None:
        self.payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        super().__init__(content=b"", status_code=200)

    async def __call__(self, scope, receive, send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(self.payload)).encode()),
                ],
            }
        )
        # 只发送一部分正文，且不发送结束帧；服务端会关闭连接。
        partial = self.payload[: max(1, len(self.payload) // 3)]
        await send({"type": "http.response.body", "body": partial, "more_body": True})


def _public_link(request: Request, publication_id: int) -> str:
    base = os.getenv("MIRROR_PUBLIC_BASE", "").rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")
    return f"{base}/notice/{publication_id}"


def _serialize(request: Request, record: Publication) -> dict:
    return {
        "id": record.id,
        "source_url": record.source_url,
        "project_no": record.project_no,
        "title": record.title,
        "content": record.content,
        "content_hash": record.content_hash,
        "created_at": record.created_at,
        "link": _public_link(request, record.id),
        "path": f"/notice/{record.id}",
    }


def _wants_interrupt(request: Request, next_interrupt: threading.Event) -> bool:
    if any(
        request.headers.get(name, "").lower() in ("1", "true", "yes", "on")
        for name in INTERRUPT_HEADERS
    ):
        # 显式请求头优先，同时消费掉待触发的演示标记，避免影响下一次发布。
        next_interrupt.clear()
        return True
    if next_interrupt.is_set():
        next_interrupt.clear()
        return True
    return False


def _wants_query_failure(request: Request) -> bool:
    return request.headers.get(QUERY_FAIL_HEADER, "").lower() in ("1", "true", "yes", "on")


def _demo_enabled() -> bool:
    return os.getenv("MIRROR_DEMO_MODE", "1").lower() in ("1", "true", "yes", "on")


def create_app(store: MirrorStore | None = None) -> FastAPI:
    mirror_store = store or MirrorStore()
    next_interrupt = threading.Event()

    app = FastAPI(title="招标公示镜像站")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/publications")
    def create_publication(payload: PublicationIn, request: Request):
        try:
            record, created, conflict = mirror_store.publish(
                project_no=payload.project_no,
                source_url=payload.source_url,
                content=payload.content,
                title=payload.title,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        link = _public_link(request, record.id)
        body = {
            "created": created,
            "record": _serialize(request, record),
            "link": link,
        }
        if conflict:
            body["error"] = "conflict"
            body["message"] = "同一来源 URL 已存在内容不同的公示记录"
            if _wants_interrupt(request, next_interrupt):
                return InterruptedResponse(body)
            return JSONResponse(status_code=409, content=body)

        if _wants_interrupt(request, next_interrupt):
            return InterruptedResponse(body)
        return JSONResponse(status_code=201 if created else 200, content=body)

    @app.get("/api/publications")
    def get_publication(request: Request, source_url: str):
        if _wants_query_failure(request):
            return JSONResponse(status_code=503, content={"error": "query_failed", "message": "查询暂不可用"})
        try:
            record = mirror_store.get_by_source_url(source_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if record is None:
            return {"found": False, "record": None}
        return {"found": True, "record": _serialize(request, record), "link": _public_link(request, record.id)}

    @app.post("/api/publications/query")
    def query_publication(payload: QueryIn, request: Request):
        if _wants_query_failure(request):
            return JSONResponse(status_code=503, content={"error": "query_failed", "message": "查询暂不可用"})
        try:
            record = mirror_store.get_by_source_url(payload.source_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if record is None:
            return {"found": False, "same_content": None, "record": None}

        same_content = None
        if payload.content is not None:
            project_no = payload.project_no.strip() or record.project_no
            same_content = record.content_hash == content_hash(project_no, payload.content)
        return {
            "found": True,
            "same_content": same_content,
            "record": _serialize(request, record),
            "link": _public_link(request, record.id),
        }

    @app.get("/notice/{publication_id}", response_class=HTMLResponse)
    def notice_page(publication_id: int, request: Request) -> HTMLResponse:
        record = mirror_store.get_by_id(publication_id)
        if record is None:
            return HTMLResponse("<h1>404</h1><p>公示记录不存在。</p>", status_code=404)
        source = html.escape(record.source_url)
        content = html.escape(record.content).replace("\n", "<br>")
        return HTMLResponse(
            f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>公示 · {html.escape(record.project_no)}</title>
<style>
 body {{ margin:0; background:#f0f2f5; font-family:"PingFang SC","Microsoft YaHei",sans-serif; color:#1f2937; }}
 .wrap {{ max-width:900px; margin:28px auto; background:#fff; border-radius:10px; padding:26px 32px;
          box-shadow:0 1px 4px rgba(0,0,0,.08); }}
 h1 {{ font-size:20px; margin:0 0 16px; }}
 .meta {{ font-size:13px; color:#475569; background:#f8fafc; border:1px solid #e2e8f0;
          border-radius:8px; padding:12px 16px; margin-bottom:18px; }}
 .meta div {{ margin:4px 0; word-break:break-all; }}
 .content {{ line-height:1.9; font-size:14px; border-top:1px solid #e2e8f0; padding-top:18px; }}
 .tag {{ display:inline-block; background:#dcfce7; color:#166534; padding:3px 10px;
         border-radius:999px; font-size:12px; font-weight:600; }}
 a {{ color:#1d4ed8; }}
</style></head><body><div class="wrap">
 <h1>{html.escape(record.title or '公示记录')} <span class="tag">已刊登</span></h1>
 <div class="meta">
  <div>项目编号：{html.escape(record.project_no)}</div>
  <div>来源 URL：<a href="{source}" target="_blank" rel="noopener">{source}</a></div>
  <div>刊登时间：{html.escape(record.created_at)}</div>
  <div>公示编号：{record.id}</div>
 </div>
 <div class="content">{content}</div>
</div></body></html>"""
        )

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        records = mirror_store.list_all()
        state = "下一次发布将中断回执" if next_interrupt.is_set() else "正常"
        body = (
            "".join(
                f'<li><a href="/notice/{r.id}">#{r.id} · {html.escape(r.project_no)}</a>'
                f'<div class="sub">{html.escape(r.title or "（无标题）")} · {html.escape(r.created_at)}</div></li>'
                for r in records
            )
            or '<li class="empty">暂无公示记录</li>'
        )
        demo_block = (
            f"""
 <div class="demo">
  <h2>本地示例控制</h2>
  <p>模拟"发布已写入、回执在网络中断开"：下一次发布先落库，再向调用方发送不完整响应。</p>
  <form method="post" action="/demo/interrupt"><button type="submit">模拟下一次回执中断</button></form>
  <form method="post" action="/demo/reset"><button type="submit" class="danger">清空演示数据</button></form>
  <p class="state">当前状态：{html.escape(state)}</p>
 </div>"""
            if _demo_enabled()
            else ""
        )
        return HTMLResponse(
            f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>招标公示镜像站</title>
<style>
 body {{ margin:0; background:#f0f2f5; font-family:"PingFang SC","Microsoft YaHei",sans-serif; color:#1f2937; }}
 .wrap {{ max-width:900px; margin:28px auto; background:#fff; border-radius:10px; padding:26px 32px;
          box-shadow:0 1px 4px rgba(0,0,0,.08); }}
 h1 {{ font-size:22px; margin:0 0 6px; }} p.lead {{ color:#64748b; font-size:13px; margin-top:0; }}
 h2 {{ font-size:16px; margin:24px 0 10px; }}
 ul {{ list-style:none; padding:0; margin:0; }}
 li {{ border-bottom:1px solid #eef1f5; padding:11px 4px; }}
 li a {{ color:#1d4ed8; text-decoration:none; font-weight:600; }}
 .sub {{ color:#64748b; font-size:12px; margin-top:3px; }}
 .empty {{ color:#94a3b8; }}
 .demo {{ background:#fff7e6; border:1px solid #ffd591; border-radius:8px; padding:14px 18px; margin-top:22px; }}
 .demo h2 {{ margin:0 0 8px; color:#ad4e00; }}
 .demo p {{ font-size:13px; color:#7c4a03; margin:0 0 12px; }}
 form {{ display:inline; }} button {{ padding:8px 16px; border-radius:6px; border:1px solid #cbd5e1;
          background:#fff; cursor:pointer; font-size:13px; margin-right:8px; }}
 .danger {{ color:#991b1b; }}
 .state {{ font-size:12px; color:#64748b; }}
</style></head><body><div class="wrap">
 <h1>招标公示镜像站</h1>
 <p class="lead">以来源 URL 唯一保存公示记录；同一 URL 内容一致返回原链接，内容不同返回冲突。</p>
 <h2>已刊登记录（{len(records)}）</h2>
 <ul>{body}</ul>
{demo_block}
</div></body></html>"""
        )

    @app.post("/demo/interrupt")
    def demo_interrupt() -> RedirectResponse:
        if not _demo_enabled():
            raise HTTPException(status_code=404, detail="演示功能未启用")
        next_interrupt.set()
        return RedirectResponse(url="/", status_code=303)

    @app.post("/demo/reset")
    def demo_reset() -> RedirectResponse:
        if not _demo_enabled():
            raise HTTPException(status_code=404, detail="演示功能未启用")
        next_interrupt.clear()
        mirror_store.reset()
        return RedirectResponse(url="/", status_code=303)

    return app


def create_default_app() -> FastAPI:
    """uvicorn --factory 入口：延迟到启动时再创建默认数据库连接。"""
    return create_app()
