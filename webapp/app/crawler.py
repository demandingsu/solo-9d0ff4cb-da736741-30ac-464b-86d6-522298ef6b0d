"""列表页爬虫：沿同站点"下一页"抓取详情页并解析字段。

安全/边界约束：
- 只跟随与入口同站点（scheme+host+port 一致）的链接；
- 禁止 file://、ftp:// 等非 http(s) 协议；
- 限制列表页数、详情页数与单请求超时，避免失控抓取。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .models import Announcement
from .parser import parse_detail
from .utils import same_origin

MAX_LIST_PAGES = 10
MAX_DETAIL_PAGES = 100
REQUEST_TIMEOUT = 15.0
USER_AGENT = "tender-extension-checker/1.0 (+local procurement audit tool)"

_NEXT_TEXT_RE = re.compile(r"^(?:下一页|下页|后一页|更多页|next|>»|»)$", re.IGNORECASE)
_DETAIL_HINT_RE = re.compile(r"(?:detail|notice|announcement|article|content|view|info)", re.IGNORECASE)
_LIST_HINT_RE = re.compile(r"(?:list|index|search|query|page)", re.IGNORECASE)


@dataclass
class CrawlReport:
    announcements: list[Announcement] = field(default_factory=list)
    visited_list_pages: list[str] = field(default_factory=list)
    skipped_urls: list[tuple[str, str]] = field(default_factory=list)  # (url, 原因)
    errors: list[str] = field(default_factory=list)


def _normalize_url(base: str, href: str) -> str | None:
    if not href:
        return None
    url = urldefrag(urljoin(base, href))[0].strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return url


def _is_next_page_link(text: str, href: str) -> bool:
    clean = text.strip().lower()
    if clean and _NEXT_TEXT_RE.match(clean):
        return True
    # 常见分页参数：page=N、pageNum=N、p=N（当前页之外的数字页）
    if re.search(r"[?&](?:page|pageNum|p)=\d+", href, re.IGNORECASE):
        return True
    return False


def _extract_list_links(html: str, page_url: str) -> tuple[list[str], str | None]:
    """从列表页提取 (详情候选链接, 下一页链接)。"""
    soup = BeautifulSoup(html, "html.parser")
    detail_candidates: list[str] = []
    next_page: str | None = None

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "").strip()
        url = _normalize_url(page_url, href)
        if not url or url == page_url:
            continue
        text = anchor.get_text(" ", strip=True)

        if _is_next_page_link(text, href):
            next_page = next_page or url
            continue

        # 公告详情的判定：锚文本较长且含编号/公告字样，或 URL 形态像详情页
        looks_like_detail = False
        if re.search(r"公告|更正|延期|变更|澄清|中标|成交", text) and len(text) >= 6:
            looks_like_detail = True
        elif _DETAIL_HINT_RE.search(urlparse(url).path) and not _LIST_HINT_RE.search(urlparse(url).path):
            looks_like_detail = True
        elif re.search(r"/(?:\d{4,}|[0-9a-f-]{8,})", urlparse(url).path):
            looks_like_detail = True

        if looks_like_detail:
            detail_candidates.append(url)

    # 去重保序
    seen: set[str] = set()
    unique: list[str] = []
    for url in detail_candidates:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique, next_page


def crawl(entry_url: str) -> CrawlReport:
    report = CrawlReport()
    entry_url = entry_url.strip()
    parsed_entry = urlparse(entry_url)
    if parsed_entry.scheme not in ("http", "https") or not parsed_entry.netloc:
        report.errors.append("入口地址必须是 http(s) 开头的完整 URL。")
        return report

    visited_lists: set[str] = set()
    visited_details: set[str] = set()
    queue = [entry_url]

    with httpx.Client(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        # 1. 沿"下一页"遍历列表页
        while queue and len(visited_lists) < MAX_LIST_PAGES:
            list_url = queue.pop(0)
            if list_url in visited_lists:
                continue
            visited_lists.add(list_url)
            report.visited_list_pages.append(list_url)

            try:
                response = client.get(list_url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                report.errors.append(f"列表页抓取失败 {list_url}：{exc}")
                continue

            detail_urls, next_page = _extract_list_links(response.text, str(response.url))

            # 2. 抓取并解析详情页
            for detail_url in detail_urls:
                if len(visited_details) >= MAX_DETAIL_PAGES:
                    report.skipped_urls.append((detail_url, "达到详情页数量上限"))
                    continue
                if detail_url in visited_details:
                    continue
                if not same_origin(entry_url, detail_url):
                    report.skipped_urls.append((detail_url, "与入口不同站点，已跳过"))
                    continue
                visited_details.add(detail_url)
                try:
                    detail_resp = client.get(detail_url)
                    detail_resp.raise_for_status()
                except httpx.HTTPError as exc:
                    report.errors.append(f"详情页抓取失败 {detail_url}：{exc}")
                    continue
                try:
                    announcement = parse_detail(detail_resp.text, str(detail_resp.url))
                except ValueError as exc:
                    report.skipped_urls.append((detail_url, str(exc)))
                    continue
                report.announcements.append(announcement)

            # 3. 同站点下一页
            if next_page and next_page not in visited_lists and same_origin(entry_url, next_page):
                queue.append(next_page)
            elif next_page and not same_origin(entry_url, next_page):
                report.skipped_urls.append((next_page, "下一页指向不同站点，已停止跟随"))

    return report
