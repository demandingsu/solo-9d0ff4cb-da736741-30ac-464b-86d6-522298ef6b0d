"""发布到公示镜像站：先查后发、回执中断复查、禁止盲目重发。

流程：
1. 发送前按来源 URL 查询：已有相同内容 -> 直接返回原链接；内容不同 -> 冲突。
2. 查询失败 -> 保持"待确认"，不发送。
3. 提交后若回执中断（超时/断连/残缺响应）或响应无法判定：
   立即按来源 URL 复查；复查确认已刊登 -> 已发布；
   复查未发现记录 -> 保持"待确认"，不自动重发；
   复查本身失败 -> 保持"待确认"，不自动重发。
4. 并发发布由镜像站的 UNIQUE(source_url) + 写锁保证只生成一条记录。
"""
from __future__ import annotations

import os
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

STATUS_PUBLISHED = "published"
STATUS_CONFLICT = "conflict"
STATUS_PENDING = "pending"

STATUS_LABELS = {
    STATUS_PUBLISHED: "已发布",
    STATUS_CONFLICT: "冲突",
    STATUS_PENDING: "待确认",
}

OUTCOME_RETENTION = 200


class MirrorQueryError(Exception):
    """发布前 / 复查查询失败。"""


@dataclass
class PublishOutcome:
    status: str
    message: str
    project_no: str
    source_url: str
    content: str
    title: str = ""
    notice_type: str = ""
    publish_time: str = ""
    mirror_link: str | None = None
    entry_url: str = ""
    interrupted: bool = False
    steps: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)


def _mirror_base_url() -> str:
    return os.getenv("MIRROR_BASE_URL", "http://localhost:8091").rstrip("/")


def _mirror_public_base() -> str:
    return os.getenv("MIRROR_PUBLIC_BASE", "").rstrip("/")


def _timeout() -> float:
    try:
        return float(os.getenv("MIRROR_TIMEOUT", "15"))
    except ValueError:
        return 15.0


def _public_link(link: str | None, record: dict[str, Any] | None) -> str | None:
    """优先使用镜像站返回的链接；若其指向容器内网地址则改写为宿主可访问地址。"""
    if not link and record:
        path = record.get("path")
        if path:
            link = _mirror_base_url() + path
    if not link:
        return None
    public_base = _mirror_public_base()
    internal_base = _mirror_base_url()
    if public_base and link.startswith(internal_base + "/"):
        return public_base + link[len(internal_base):]
    return link


def _parse_json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _query(source_url: str, project_no: str, content: str) -> dict[str, Any]:
    """按来源 URL 查询，并让镜像站判断内容是否一致。"""
    try:
        with httpx.Client(timeout=_timeout()) as client:
            response = client.post(
                f"{_mirror_base_url()}/api/publications/query",
                json={
                    "source_url": source_url,
                    "project_no": project_no,
                    "content": content,
                },
            )
    except httpx.HTTPError as exc:
        raise MirrorQueryError(f"{type(exc).__name__}: {exc}") from exc

    if response.status_code != 200:
        raise MirrorQueryError(f"查询返回 HTTP {response.status_code}")
    data = _parse_json(response)
    if data is None:
        raise MirrorQueryError("查询响应不是有效的 JSON")
    return data


def _post(source_url: str, project_no: str, content: str, title: str,
          simulate_interrupt: bool) -> httpx.Response:
    headers = {"X-Simulate-Interrupt": "1"} if simulate_interrupt else None
    with httpx.Client(timeout=_timeout()) as client:
        return client.post(
            f"{_mirror_base_url()}/api/publications",
            json={
                "source_url": source_url,
                "project_no": project_no,
                "content": content,
                "title": title,
            },
            headers=headers,
        )


def _same_content(data: dict[str, Any]) -> bool:
    return data.get("same_content") is True


def _published_from(data: dict[str, Any], message: str, base: dict[str, Any],
                    steps: list[str], interrupted: bool = False) -> PublishOutcome:
    record = data.get("record") or {}
    return PublishOutcome(
        status=STATUS_PUBLISHED,
        message=message,
        mirror_link=_public_link(data.get("link"), record),
        interrupted=interrupted,
        steps=steps,
        **base,
    )


def _conflict_from(data: dict[str, Any], message: str, base: dict[str, Any],
                   steps: list[str], interrupted: bool = False) -> PublishOutcome:
    record = data.get("record") or {}
    return PublishOutcome(
        status=STATUS_CONFLICT,
        message=message,
        mirror_link=_public_link(data.get("link"), record),
        interrupted=interrupted,
        steps=steps,
        **base,
    )


def _pending(message: str, steps: list[str], base: dict[str, Any],
             interrupted: bool = False) -> PublishOutcome:
    return PublishOutcome(
        status=STATUS_PENDING,
        message=message,
        interrupted=interrupted,
        steps=steps,
        **base,
    )


def publish_announcement(
    *,
    project_no: str,
    source_url: str,
    content: str,
    title: str = "",
    notice_type: str = "",
    publish_time: str = "",
    entry_url: str = "",
    simulate_interrupt: bool = False,
) -> PublishOutcome:
    """把一条更正公告发布到镜像站，并返回可审计的处理结果。"""
    base = {
        "project_no": project_no,
        "source_url": source_url,
        "content": content,
        "title": title,
        "notice_type": notice_type,
        "publish_time": publish_time,
        "entry_url": entry_url,
    }
    steps: list[str] = []

    # 1. 发送前先查。
    try:
        existing = _query(source_url, project_no, content)
    except MirrorQueryError as exc:
        steps.append(f"发布前查询失败：{exc}")
        return _pending(
            "发布前查询失败，无法确认该来源 URL 是否已刊登；已保持待确认，未发送。",
            steps, base,
        )

    if existing.get("found"):
        if _same_content(existing):
            steps.append("发布前查询：来源 URL 已存在且内容一致，返回原链接")
            return _published_from(
                existing, "镜像站已有相同内容，直接返回原链接（未重复发送）。", base, steps
            )
        steps.append("发布前查询：来源 URL 已存在但内容不同，判定冲突")
        return _conflict_from(
            existing, "该来源 URL 已刊登内容不同的公示，请人工核对后再处理。", base, steps
        )

    steps.append("发布前查询：未发现同来源 URL 的记录，开始提交")

    # 2. 提交；回执中断时进入复查，而不是重发。
    try:
        response = _post(source_url, project_no, content, title, simulate_interrupt)
    except httpx.HTTPError as exc:
        steps.append(f"提交后回执中断：{type(exc).__name__}: {exc}")
        return _confirm_after_interruption(base, steps)

    if response.status_code == 409:
        data = _parse_json(response) or {}
        steps.append("镜像站返回 409：同一来源 URL 内容不一致")
        return _conflict_from(
            data, "该来源 URL 已刊登内容不同的公示，请人工核对后再处理。", base, steps
        )

    if 200 <= response.status_code < 300:
        data = _parse_json(response)
        if data and (data.get("link") or data.get("record")):
            created = data.get("created")
            steps.append("镜像站已返回成功回执")
            message = "镜像站已刊登该更正公告。" if created else "镜像站已有相同内容，返回原链接。"
            return _published_from(data, message, base, steps)
        steps.append("镜像站返回成功状态，但回执内容无法解析")

    steps.append(f"镜像站回执异常（HTTP {response.status_code}），转入复查")

    # 3. 回执异常/中断 -> 复查确认。
    return _confirm_after_interruption(base, steps)


def _confirm_after_interruption(base: dict[str, Any], steps: list[str]) -> PublishOutcome:
    """回执中断后的复查：只查询，绝不自动重发。"""
    try:
        check = _query(base["source_url"], base["project_no"], base["content"])
    except MirrorQueryError as exc:
        steps.append(f"回执中断后的复查失败：{exc}")
        return _pending(
            "回执中断且复查失败，无法确认是否已刊登；保持待确认，未重发。",
            steps, base, interrupted=True,
        )

    if check.get("found"):
        if _same_content(check):
            steps.append("复查：记录已存在且内容一致，确认发布成功")
            return _published_from(
                check, "回执中断后复查确认：镜像站已刊登该公告。", base, steps, interrupted=True
            )
        steps.append("复查：记录已存在但内容不同，判定冲突")
        return _conflict_from(
            check, "回执中断后复查发现同来源 URL 的内容不同，请人工核对。", base, steps,
            interrupted=True,
        )

    steps.append("复查：未发现该来源 URL 的记录")
    return _pending(
        "回执中断且复查未发现记录，无法确认是否已刊登；保持待确认，未自动重发。",
        steps, base, interrupted=True,
    )


def confirm_pending(
    *,
    project_no: str,
    source_url: str,
    content: str,
    title: str = "",
    notice_type: str = "",
    publish_time: str = "",
    entry_url: str = "",
    interrupted: bool = False,
) -> PublishOutcome:
    """人工触发"重新查询确认"，仅查询，不重发。"""
    base = {
        "project_no": project_no,
        "source_url": source_url,
        "content": content,
        "title": title,
        "notice_type": notice_type,
        "publish_time": publish_time,
        "entry_url": entry_url,
    }
    steps: list[str] = []
    try:
        check = _query(source_url, project_no, content)
    except MirrorQueryError as exc:
        steps.append(f"复查失败：{exc}")
        return _pending(
            "复查失败，仍无法确认是否已刊登；保持待确认，未重发。",
            steps, base, interrupted=interrupted,
        )

    if check.get("found"):
        if _same_content(check):
            steps.append("复查：记录已存在且内容一致，确认发布成功")
            return _published_from(
                check, "复查确认：镜像站已刊登该公告。", base, steps, interrupted=interrupted
            )
        steps.append("复查：记录已存在但内容不同，判定冲突")
        return _conflict_from(
            check, "复查发现同来源 URL 的内容不同，请人工核对。", base, steps,
            interrupted=interrupted,
        )

    steps.append("复查：未发现该来源 URL 的记录")
    return _pending(
        "复查未发现记录，仍无法确认是否已刊登；保持待确认，未重发。",
        steps, base, interrupted=interrupted,
    )


# ---------------------------------------------------------------------------
# 发布结果暂存（POST/Redirect/GET，避免刷新页面触发重复发布）
# ---------------------------------------------------------------------------

_OUTCOMES: dict[str, PublishOutcome] = {}
_OUTCOMES_LOCK = threading.Lock()


def remember_outcome(outcome: PublishOutcome) -> str:
    token = uuid.uuid4().hex
    with _OUTCOMES_LOCK:
        _OUTCOMES[token] = outcome
        while len(_OUTCOMES) > OUTCOME_RETENTION:
            oldest = next(iter(_OUTCOMES))
            del _OUTCOMES[oldest]
    return token


def recall_outcome(token: str) -> PublishOutcome | None:
    with _OUTCOMES_LOCK:
        return _OUTCOMES.get(token)
