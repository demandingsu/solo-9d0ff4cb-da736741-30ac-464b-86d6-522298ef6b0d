"""发布流程测试：镜像站幂等/冲突/并发、回执中断复查、查询失败待确认、网页发布入口。

运行方式：
    python3 -I tests/test_publish.py
"""
from __future__ import annotations

import functools
import http.server
import json
import os
import socket
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# -I 隔离模式下 PYTHONPATH 不生效，显式注入依赖目录与项目路径
for candidate in ("/tmp/pylibs", "/tmp/pyroot/lib/python3.11/site-packages"):
    if Path(candidate).exists() and candidate not in sys.path:
        sys.path.insert(0, candidate)

ROOT = Path(__file__).resolve().parent.parent
for candidate in (str(ROOT), str(ROOT / "webapp")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.crawler import crawl  # noqa: E402
from app.main import app as webapp  # noqa: E402
from app.parser import parse_detail  # noqa: E402
from app.publisher import confirm_pending, publish_announcement  # noqa: E402
from mirror.app.main import create_app as create_mirror_app  # noqa: E402
from mirror.app.storage import MirrorStore  # noqa: E402

SITE_DIR = ROOT / "example-site" / "site"


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def wait_until_ready(base: str, path: str = "/health", attempts: int = 100) -> None:
    for _ in range(attempts):
        try:
            httpx.get(base + path, timeout=0.3)
            return
        except httpx.HTTPError:
            time.sleep(0.05)
    raise RuntimeError(f"服务未在预期时间内启动：{base}")


def start_mirror(db_path: str | None = None):
    store = MirrorStore(db_path or tempfile.mktemp(suffix=".db"))
    port = free_port()
    app = create_mirror_app(store)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    wait_until_ready(base)
    return server, thread, base, store


def start_static_site() -> tuple[http.server.ThreadingHTTPServer, str]:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/list/page_1.html"


def stop_server(server, thread) -> None:
    if isinstance(server, uvicorn.Server):
        server.should_exit = True
        thread.join(timeout=5)
    else:
        server.shutdown()
        thread.join(timeout=5)


def assert_equal(actual, expected, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}：期望 {expected!r}，实际 {actual!r}")
    print(f"  ✓ {message}")


def assert_true(condition, message: str) -> None:
    if not condition:
        raise AssertionError(f"{message}：条件不成立")
    print(f"  ✓ {message}")


class FakeMirrorState:
    def __init__(self) -> None:
        self.records: dict[str, dict] = {}
        self.query_fails = False
        self.interrupt_on_publish = False
        self.fail_query_after_publish = False
        self.publish_count = 0
        self.lock = threading.Lock()


class FakeMirrorHandler(http.server.BaseHTTPRequestHandler):
    state: FakeMirrorState
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:  # 静默测试服务日志
        pass

    def _read_json(self) -> dict:
        length = int(self.headers.get("content-length", "0") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        payload = self._read_json()
        if self.path == "/api/publications/query":
            if self.state.query_fails:
                self._send_json(503, {"error": "query_failed"})
                return
            record = self.state.records.get(payload.get("source_url"))
            if record is None:
                self._send_json(200, {"found": False, "same_content": None, "record": None})
                return
            same = (
                record["project_no"] == payload.get("project_no")
                and record["content"] == payload.get("content")
            )
            self._send_json(
                200,
                {"found": True, "same_content": same, "record": record,
                 "link": f"/notice/{record['id']}"},
            )
            return

        if self.path == "/api/publications":
            with self.state.lock:
                self.state.publish_count += 1
                source_url = payload.get("source_url")
                record = self.state.records.get(source_url)
                if record is None:
                    record = {
                        "id": len(self.state.records) + 1,
                        "source_url": source_url,
                        "project_no": payload.get("project_no"),
                        "content": payload.get("content"),
                        "title": payload.get("title", ""),
                    }
                    self.state.records[source_url] = record
                    status, created, conflict = 201, True, False
                else:
                    same = (
                        record["project_no"] == payload.get("project_no")
                        and record["content"] == payload.get("content")
                    )
                    status, created, conflict = (200, False, False) if same else (409, False, True)

                if self.state.fail_query_after_publish:
                    self.state.query_fails = True

            body = json.dumps(
                {"created": created, "record": record, "link": f"/notice/{record['id']}"},
                ensure_ascii=False,
            ).encode("utf-8")
            if self.state.interrupt_on_publish:
                # 先“落库”，再发送带完整 Content-Length 的不完整响应并断开。
                self.send_response(status)
                self.send_header("content-type", "application/json; charset=utf-8")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body[: len(body) // 3])
                self.wfile.flush()
                self.close_connection = True
                self.connection.close()
                return
            if conflict:
                self._send_json(409, {"error": "conflict", "record": record})
            else:
                self._send_json(status, {"created": created, "record": record})
            return

        self._send_json(404, {"error": "not_found"})

    def do_GET(self) -> None:  # noqa: N802
        self._send_json(200, {"status": "ok"})


def start_fake_mirror() -> tuple[http.server.ThreadingHTTPServer, FakeMirrorState, str]:
    state = FakeMirrorState()
    handler = type("BoundFakeMirrorHandler", (FakeMirrorHandler,), {"state": state})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, state, f"http://127.0.0.1:{server.server_address[1]}"


def test_body_text_extraction() -> None:
    print("[1] 正文可见文字提取（排除面包屑与字段表）...")
    html = (SITE_DIR / "detail" / "notice_002.html").read_text(encoding="utf-8")
    ann = parse_detail(html, "http://example-site/detail/notice_002.html")
    assert_true("因采购需求调整" in ann.body_text, "正文包含公告正文段落")
    assert_true("采购代理人请以本平台正式公告内容为准" in ann.body_text, "正文包含完整可见文字")
    assert_true("返回公告列表" not in ann.body_text, "正文不含列表页面包屑")
    assert_true("项目编号" not in ann.body_text, "正文不含字段表标签")


def test_mirror_idempotent_conflict_and_concurrency() -> None:
    print("[2] 镜像站：同 URL 幂等、内容冲突、并发只生成一条记录 ...")
    server, thread, base, store = start_mirror()
    try:
        payload = {
            "project_no": "CG-2026-001",
            "source_url": "http://example-site/detail/notice_002.html#top",
            "content": "正文第一段\n正文第二段",
            "title": "更正公告",
        }
        first = httpx.post(base + "/api/publications", json=payload, timeout=10)
        assert_equal(first.status_code, 201, "首次发布创建记录")
        first_record = first.json()["record"]
        link = first.json()["link"]
        assert_equal(first.json()["created"], True, "首次发布标记为新建")

        second = httpx.post(base + "/api/publications", json=payload, timeout=10)
        assert_equal(second.status_code, 200, "相同内容再次发布返回已有记录")
        assert_equal(second.json()["created"], False, "相同内容不重复创建")
        assert_equal(second.json()["record"]["id"], first_record["id"], "返回同一记录 ID")
        assert_equal(second.json()["link"], link, "返回原镜像链接")

        conflict = httpx.post(
            base + "/api/publications",
            json={**payload, "content": "内容已经不同"},
            timeout=10,
        )
        assert_equal(conflict.status_code, 409, "同 URL 不同内容返回冲突")
        assert_equal(conflict.json()["error"], "conflict", "冲突响应带 error=conflict")

        query = httpx.post(
            base + "/api/publications/query",
            json={"source_url": payload["source_url"], "project_no": payload["project_no"],
                  "content": payload["content"]},
            timeout=10,
        )
        assert_equal(query.status_code, 200, "查询接口可正常响应")
        assert_equal(query.json()["same_content"], True, "查询判断内容一致")

        # 并发发布同一 URL：全部请求都成功，但数据库只有一条记录。
        concurrent_payload = {
            "project_no": "CG-CONC",
            "source_url": "http://example-site/detail/notice_010.html",
            "content": "并发正文",
        }

        def publish_once() -> httpx.Response:
            return httpx.post(base + "/api/publications", json=concurrent_payload, timeout=15)

        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: publish_once(), range(8)))

        assert_equal({r.status_code for r in responses}, {200, 201}, "并发请求均成功或命中已有记录")
        ids = {r.json()["record"]["id"] for r in responses}
        assert_equal(len(ids), 1, "并发发布返回同一个记录 ID")
        matching = [r for r in store.list_all() if r.source_url == concurrent_payload["source_url"]]
        assert_equal(len(matching), 1, "并发发布只写入一条记录")
    finally:
        stop_server(server, thread)


def test_publisher_interrupted_receipt() -> None:
    print("[3] 网页发布逻辑：回执中断 -> 复查确认已刊登，不重发 ...")
    server, thread, base, store = start_mirror()
    old_env = {k: os.environ.get(k) for k in ("MIRROR_BASE_URL", "MIRROR_PUBLIC_BASE")}
    os.environ["MIRROR_BASE_URL"] = base
    os.environ["MIRROR_PUBLIC_BASE"] = base
    try:
        outcome = publish_announcement(
            project_no="CG-2026-002",
            source_url="http://example-site/detail/notice_004.html",
            content="延期公告正文",
            title="延期公告",
            simulate_interrupt=True,
        )
        assert_equal(outcome.status, "published", "回执中断后复查确认为已发布")
        assert_equal(outcome.interrupted, True, "结果标记回执曾中断")
        assert_true(bool(outcome.mirror_link), "返回镜像链接")
        records = [r for r in store.list_all()
                   if r.source_url == "http://example-site/detail/notice_004.html"]
        assert_equal(len(records), 1, "数据库中只有一条记录（未重发产生重复）")
        assert_true(any("复查" in step for step in outcome.steps), "处理步骤包含复查记录")

        # 并发调用网页发布逻辑：镜像站仍只保存一条记录，所有调用返回同一链接。
        concurrent_url = "http://example-site/detail/notice_005.html"

        def worker() -> object:
            return publish_announcement(
                project_no="CG-2026-005",
                source_url=concurrent_url,
                content="并发发布正文",
                title="招标公告",
            )

        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(lambda _: worker(), range(6)))

        assert_equal({o.status for o in outcomes}, {"published"}, "并发发布全部成功")
        assert_equal(len({o.mirror_link for o in outcomes}), 1, "并发发布返回同一镜像链接")
        matching = [r for r in store.list_all() if r.source_url == concurrent_url]
        assert_equal(len(matching), 1, "并发发布只生成一条记录")
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        stop_server(server, thread)


def test_query_failure_keeps_pending_without_send() -> None:
    print("[4] 网页发布逻辑：查询失败保持待确认，不盲目重发 ...")
    server, state, base = start_fake_mirror()
    old_env = {k: os.environ.get(k) for k in ("MIRROR_BASE_URL", "MIRROR_PUBLIC_BASE")}
    os.environ["MIRROR_BASE_URL"] = base
    os.environ["MIRROR_PUBLIC_BASE"] = base
    try:
        # 4.1 发送前查询失败：不得发送
        state.query_fails = True
        outcome = publish_announcement(
            project_no="CG-FAIL-1",
            source_url="http://example-site/detail/notice_006.html",
            content="正文",
        )
        assert_equal(outcome.status, "pending", "发送前查询失败 -> 待确认")
        assert_equal(state.publish_count, 0, "发送前查询失败时不发送发布请求")
        assert_true(any("发布前查询失败" in step for step in outcome.steps),
                    "步骤记录发布前查询失败")

        # 4.2 提交时回执中断、且随后复查失败：保持待确认，且只发过一次
        state.query_fails = False
        state.interrupt_on_publish = True
        state.fail_query_after_publish = True
        outcome = publish_announcement(
            project_no="CG-FAIL-2",
            source_url="http://example-site/detail/notice_008.html",
            content="中断正文",
        )
        assert_equal(outcome.status, "pending", "回执中断且复查失败 -> 保持待确认")
        assert_equal(outcome.interrupted, True, "结果标记回执曾中断")
        assert_equal(state.publish_count, 1, "只提交过一次，没有盲目重发")
        assert_equal(len(state.records), 1, "镜像站实际只保存一条记录")
        assert_true(any("复查失败" in step for step in outcome.steps),
                    "步骤记录复查失败")

        # 4.3 查询恢复后，人工“重新查询确认”可确认已刊登（仍然只查询）
        state.query_fails = False
        confirmed = confirm_pending(
            project_no="CG-FAIL-2",
            source_url="http://example-site/detail/notice_008.html",
            content="中断正文",
            interrupted=True,
        )
        assert_equal(confirmed.status, "published", "人工复查确认记录已刊登")
        assert_equal(confirmed.interrupted, True, "复查结果保留回执中断标记")
        assert_equal(state.publish_count, 1, "人工复查没有再次提交发布")
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        server.shutdown()
        server.server_close()


def test_webapp_publish_endpoint() -> None:
    print("[5] 网页发布入口：抓取 -> 选取更正 -> 发布 -> 展示状态/镜像链接/原公告链接 ...")
    mirror_server, mirror_thread, mirror_base, _store = start_mirror()
    static_server, entry = start_static_site()
    old_env = {k: os.environ.get(k) for k in ("MIRROR_BASE_URL", "MIRROR_PUBLIC_BASE", "DEMO_MODE")}
    os.environ["MIRROR_BASE_URL"] = mirror_base
    os.environ["MIRROR_PUBLIC_BASE"] = mirror_base
    os.environ["DEMO_MODE"] = "1"
    try:
        report = crawl(entry)
        correction = next(
            a for a in report.announcements
            if a.project_no == "CG-2026-001" and a.notice_type == "更正公告"
        )

        client = TestClient(webapp)
        page = client.get("/", params={"url": entry})
        assert_equal(page.status_code, 200, "核对结果页可访问")
        assert_true("发布更正公告到公示镜像站" in page.text, "结果页出现发布入口")
        assert_true("CG-2026-001" in page.text, "发布入口列出本次抓取的更正公告")
        assert_true('action="/publish"' in page.text, "发布表单提交到 /publish")
        assert_true("simulate_interrupt" in page.text, "演示模式提供回执中断模拟选项")

        form = {
            "project_no": correction.project_no,
            "source_url": correction.url,
            "body_text": correction.body_text,
            "title": correction.title,
            "notice_type": correction.notice_type,
            "publish_time": correction.publish_time,
            "entry_url": entry,
            "simulate_interrupt": "1",
        }
        response = client.post("/publish", data=form, follow_redirects=False)
        assert_equal(response.status_code, 303, "发布后重定向到结果页（PRG，避免刷新重发）")
        location = response.headers["location"]
        result = client.get(location)
        assert_equal(result.status_code, 200, "结果页可访问")
        assert_true("已发布" in result.text, "页面显示发布状态：已发布")
        assert_true("回执曾中断" in result.text, "页面标记回执曾中断并完成复查")
        assert_true(correction.url in result.text, "页面显示原公告链接")
        assert_true("/notice/" in result.text, "页面显示镜像链接")
        assert_true(correction.body_text.splitlines()[0] in result.text, "页面显示正文可见文字")

        # 原公告链接通过 /view 代理打开，镜像链接是宿主可访问地址。
        assert_true(f"/view?url=" in result.text, "原公告链接经代理打开")
        assert_true(mirror_base + "/notice/" in result.text, "镜像链接指向镜像站")
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        stop_server(mirror_server, mirror_thread)
        static_server.shutdown()
        static_server.server_close()


def main() -> int:
    test_body_text_extraction()
    test_mirror_idempotent_conflict_and_concurrency()
    test_publisher_interrupted_receipt()
    test_query_failure_keeps_pending_without_send()
    test_webapp_publish_endpoint()
    print("\n全部发布流程测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
