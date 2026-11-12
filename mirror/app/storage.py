"""镜像站持久层：以来源 URL 唯一保存公示记录。

并发安全：
- 进程内用互斥锁串行化"查重 + 写入"；
- 进程间依赖 SQLite 的 UNIQUE(source_url) 约束与 BEGIN IMMEDIATE 写锁。
即使两个请求同时到达，也只会生成一条记录。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urldefrag, urlparse, urlunparse


def normalize_source_url(url: str) -> str:
    """规整来源 URL：去首尾空白与 #片段，协议/主机小写，空路径补 /。

    查询与保存都走同一套规整，确保"同一来源 URL"只对应一条记录。
    """
    raw = (url or "").strip()
    raw = urldefrag(raw)[0]
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("来源 URL 必须是 http(s) 开头的完整地址")
    return urlunparse(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, "", "")
    )


def normalize_content(content: str) -> str:
    """规整正文：统一换行、去掉行尾空白与首尾空行。"""
    text = (content or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def content_hash(project_no: str, content: str) -> str:
    """按"项目编号 + 正文"计算内容指纹，用于判断同一来源 URL 的内容是否一致。"""
    canonical = f"{project_no.strip()}\n{normalize_content(content)}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class Publication:
    id: int
    source_url: str
    project_no: str
    title: str
    content: str
    content_hash: str
    created_at: str


class MirrorStore:
    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self.db_path = str(
            db_path
            or os.getenv("MIRROR_DB")
            or (Path(__file__).resolve().parent.parent / "mirror.db")
        )
        parent = Path(self.db_path).expanduser().resolve().parent
        parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS publications (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_url   TEXT NOT NULL UNIQUE,
                    project_no   TEXT NOT NULL,
                    title        TEXT NOT NULL DEFAULT '',
                    content      TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at   TEXT NOT NULL
                )
                """
            )
        finally:
            conn.close()

    @staticmethod
    def _to_publication(row: sqlite3.Row) -> Publication:
        return Publication(
            id=row["id"],
            source_url=row["source_url"],
            project_no=row["project_no"],
            title=row["title"],
            content=row["content"],
            content_hash=row["content_hash"],
            created_at=row["created_at"],
        )

    def get_by_source_url(self, source_url: str) -> Publication | None:
        normalized = normalize_source_url(source_url)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM publications WHERE source_url = ?", (normalized,)
            ).fetchone()
            return self._to_publication(row) if row else None
        finally:
            conn.close()

    def get_by_id(self, publication_id: int) -> Publication | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM publications WHERE id = ?", (publication_id,)
            ).fetchone()
            return self._to_publication(row) if row else None
        finally:
            conn.close()

    def list_all(self) -> list[Publication]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM publications ORDER BY id").fetchall()
            return [self._to_publication(row) for row in rows]
        finally:
            conn.close()

    def publish(
        self,
        project_no: str,
        source_url: str,
        content: str,
        title: str = "",
    ) -> tuple[Publication, bool, bool]:
        """保存一条公示记录。

        返回 (记录, 是否新建, 是否内容冲突)。
        同一来源 URL 已存在且内容一致 -> (原记录, False, False)；
        已存在但内容不同 -> (原记录, False, True)。
        """
        normalized_url = normalize_source_url(source_url)
        project_no = (project_no or "").strip()
        title = (title or "").strip()
        normalized_content = normalize_content(content)
        if not project_no:
            raise ValueError("项目编号不能为空")
        if not normalized_content:
            raise ValueError("正文可见文字不能为空")
        digest = content_hash(project_no, normalized_content)
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        # 进程内串行化；跨进程由 UNIQUE 约束兜底。
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM publications WHERE source_url = ?", (normalized_url,)
                ).fetchone()
                if row:
                    existing = self._to_publication(row)
                    conn.execute("COMMIT")
                    return existing, False, existing.content_hash != digest

                try:
                    cursor = conn.execute(
                        """
                        INSERT INTO publications
                            (source_url, project_no, title, content, content_hash, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (normalized_url, project_no, title, normalized_content, digest, created_at),
                    )
                    conn.execute("COMMIT")
                except sqlite3.IntegrityError:
                    # 另一个进程刚好抢先写入：回读并按其内容判断是否一致。
                    conn.execute("ROLLBACK")
                    row = conn.execute(
                        "SELECT * FROM publications WHERE source_url = ?", (normalized_url,)
                    ).fetchone()
                    if not row:
                        raise
                    existing = self._to_publication(row)
                    return existing, False, existing.content_hash != digest

                return self.get_by_id(int(cursor.lastrowid)), True, False  # type: ignore[return-value]
            finally:
                conn.close()

    def reset(self) -> None:
        """清空数据（仅用于本地示例演示）。"""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM publications")
                conn.execute("DELETE FROM sqlite_sequence WHERE name = 'publications'")
            finally:
                conn.close()
