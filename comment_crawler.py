#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""浏览器监听式公开评论采集（抖音 / B站）。

方案参考项目内《我的中奖梦.py》：用 DrissionPage 驱动本机 Edge/Chrome 正常
打开页面（使用用户自己的浏览状态），被动监听页面已收到的评论 JSON 数据包，
不破解签名、不绕过验证、不自动登录。采集结果只在内存中使用；是否导出 CSV
由调用方/用户自行决定。
"""
from __future__ import annotations

import csv
import datetime
import io
import json
import os
import random
import re
import time
from typing import Callable, Sequence

from happy8_core import extract_valid_numbers_from_text

PLATFORM_PATTERNS: dict[str, list[str]] = {
    "douyin": ["aweme/v1/web/comment/list/", "aweme/v1/web/comment/list/reply/"],
    "bilibili": ["api.bilibili.com/x/v2/reply"],
}

CSV_FIELDS = ["cid", "昵称", "点赞数", "时间", "IP属地", "评论内容", "提取号码"]


def detect_platform(url: str) -> str:
    """判断链接所属平台，当前支持抖音与B站。"""
    cleaned = str(url or "").strip().lower()
    if not cleaned:
        raise ValueError("请先粘贴视频链接。")
    if re.fullmatch(r"\d{15,25}", cleaned):
        return "douyin"  # 纯数字视为抖音视频 ID
    if "bilibili.com" in cleaned or "b23.tv" in cleaned or re.search(r"bv[0-9a-z]{10}", cleaned):
        return "bilibili"
    if "douyin.com" in cleaned or "iesdouyin.com" in cleaned:
        return "douyin"
    raise ValueError("暂支持抖音（douyin.com）与B站（bilibili.com）链接；其他平台请手动粘贴评论。")


def normalize_video_url(url: str, platform: str) -> str:
    """把「主页 + modal_id」「分享短链」等归一化为视频详情页地址。

    弹窗（modal）模式的评论区藏在局部滚动容器里，且常常要点开视频才渲染；
    /video/<id> 详情页的评论区就在右侧、可直接滚动，采集成功率高得多。
    """
    cleaned = str(url or "").strip()
    if platform != "douyin":
        return cleaned
    if re.fullmatch(r"\d{15,25}", cleaned):
        return f"https://www.douyin.com/video/{cleaned}"  # 纯数字视频 ID
    if "v.douyin.com" in cleaned or "iesdouyin.com" in cleaned:
        return cleaned  # 分享短链会自动跳转到视频页
    match = re.search(r"modal_id=(\d+)", cleaned)
    if match:
        return f"https://www.douyin.com/video/{match.group(1)}"
    match = re.search(r"douyin\.com/(video|note)/(\d+)", cleaned)
    if match:
        return f"https://www.douyin.com/{match.group(1)}/{match.group(2)}"
    raise ValueError(
        "请提供具体视频链接：粘贴分享链接，或含 /video/<id>（或 modal_id=<id>）的地址；"
        "纯博主主页链接无法定位到某个视频。"
    )


# 评论按钮候选选择器（页面版本差异较大，逐个尝试，失败静默）
COMMENT_BUTTON_SELECTORS = (
    'css:div[data-e2e="feed-comment-icon"]',
    'css:span[data-e2e="feed-comment-icon"]',
    'css:div[data-e2e="video-player-comment-icon"]',
    'css:button[aria-label*="评论"]',
)

# 评论区局部滚动容器候选（抖音网页版评论区是独立滚动的）
COMMENT_CONTAINER_SELECTORS = (
    '[data-e2e="comment-list"]',
    ".route-scroll-container",
    '[class*="route-scroll"]',
    ".comment-mainContent",
    '[class*="comment-main"]',
    "#comment",
    ".reply-list",
)


def _scroll_comment_js() -> str:
    """生成滚动评论容器的 JS：优先滚评论区容器，找不到再整页滚动。"""
    selectors = json.dumps(list(COMMENT_CONTAINER_SELECTORS), ensure_ascii=False)
    return (
        "var sels = " + selectors + ";\n"
        "for (var i = 0; i < sels.length; i++) {\n"
        "  var el = document.querySelector(sels[i]);\n"
        "  if (el && el.scrollHeight > el.clientHeight + 10) {\n"
        "    el.scrollTop = el.scrollHeight;\n"
        "    return el.scrollHeight;\n"
        "  }\n"
        "}\n"
        "window.scrollTo(0, document.body.scrollHeight);\n"
        "return 0;"
    )


def _click_comment_button(page, on_progress: Callable[[str], None] | None = None) -> bool:
    """尝试点开评论按钮——不点有时评论区不渲染（用户反馈的关键问题）。"""
    for selector in COMMENT_BUTTON_SELECTORS:
        try:
            button = page.ele(selector, timeout=1)
        except Exception:
            continue
        if not button:
            continue
        try:
            button.click()
        except Exception:
            try:
                button.parent().click()
            except Exception:
                continue
        if on_progress:
            on_progress("已点击评论按钮，展开评论区。")
        time.sleep(1.0)
        return True
    return False


def get_browser_options(headless: bool = False):
    """自动探测 Windows/macOS 上的 Edge/Chrome（与《我的中奖梦.py》一致）。"""
    from DrissionPage import ChromiumOptions

    co = ChromiumOptions()
    if headless:
        co.headless()
    candidate_paths = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for path in candidate_paths:
        if os.path.exists(path):
            co.set_browser_path(path)
            return co
    return co


def _format_time(ts: object) -> str:
    try:
        value = int(ts)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return datetime.datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def parse_comment_payload(platform: str, json_data: object) -> list[dict]:
    """把一条评论数据包解析为统一行（含智能提取号码）。"""
    if not isinstance(json_data, dict):
        return []
    rows: list[dict] = []

    def make_row(cid, text, nickname, digg, time_text, ip_label) -> dict:
        text = str(text or "").strip()
        numbers = extract_valid_numbers_from_text(text)
        return {
            "cid": str(cid or ""),
            "昵称": str(nickname or "匿名用户"),
            "点赞数": digg if digg is not None else 0,
            "时间": time_text,
            "IP属地": str(ip_label or ""),
            "评论内容": text,
            "提取号码": " ".join(f"{n:02d}" for n in numbers),
        }

    if platform == "douyin":
        for comment in json_data.get("comments") or []:
            if not isinstance(comment, dict):
                continue
            user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
            rows.append(
                make_row(
                    comment.get("cid"),
                    comment.get("text"),
                    user.get("nickname"),
                    comment.get("digg_count", 0),
                    _format_time(comment.get("create_time", 0)),
                    comment.get("ip_label"),
                )
            )
    else:  # bilibili
        data = json_data.get("data") if isinstance(json_data.get("data"), dict) else {}

        def walk(replies) -> None:
            for reply in replies or []:
                if not isinstance(reply, dict):
                    continue
                content = reply.get("content") if isinstance(reply.get("content"), dict) else {}
                member = reply.get("member") if isinstance(reply.get("member"), dict) else {}
                control = reply.get("reply_control") if isinstance(reply.get("reply_control"), dict) else {}
                rows.append(
                    make_row(
                        reply.get("rpid"),
                        content.get("message"),
                        member.get("uname"),
                        reply.get("like_count", 0),
                        _format_time(reply.get("ctime", 0)),
                        control.get("location"),
                    )
                )
                walk(reply.get("replies"))

        walk(data.get("replies"))
    return rows


def crawl_comments(
    url: str,
    *,
    max_pages: int = 20,
    headless: bool = False,
    delay: tuple[float, float] = (1.2, 2.5),
    listen_timeout: float = 4.0,
    max_consecutive_empty: int = 5,
    on_progress: Callable[[str], None] | None = None,
) -> list[dict]:
    """打开本机浏览器收集评论数据包，返回按 cid 去重的行列表。

    流程（针对“提供链接却抓不到评论”问题的修复）：
    1. 把主页+modal_id 等链接归一化为 /video/<id> 详情页，直达视频；
    2. 尝试点开评论按钮，让评论区渲染出来；
    3. 滚动评论区的局部滚动容器（而非整页）触发懒加载；
    4. listen.wait(timeout) 收包（带超时防死锁），解析去重。
    连续多次空包即认为已到评论区底部。
    """
    try:
        from DrissionPage import ChromiumPage
    except ImportError as exc:  # pragma: no cover - 依赖在 requirements.txt
        raise RuntimeError("缺少 DrissionPage，请先执行：pip install DrissionPage") from exc

    platform = detect_platform(url)
    patterns = PLATFORM_PATTERNS[platform]
    page = ChromiumPage(get_browser_options(headless=headless))
    page.listen.start(patterns)
    page.get(normalize_video_url(url, platform))
    time.sleep(2.0)  # 等待播放器与评论区渲染

    # 用户反馈：很多情况下要点一下评论按钮，评论区才会渲染出来
    _click_comment_button(page, on_progress)

    rows_by_cid: dict[str, dict] = {}
    order: list[str] = []
    consecutive_empty = 0
    scroll_js = _scroll_comment_js()

    for page_index in range(1, int(max_pages) + 1):
        if on_progress:
            on_progress(f"正在滚动加载第 {page_index}/{int(max_pages)} 页评论……")
        # 评论区是局部滚动容器：滚它的 scrollTop 才会触发懒加载；
        # 找不到容器时 JS 会退回整页滚动。
        scrolled = page.run_js(scroll_js)
        if not scrolled and on_progress and page_index == 1:
            on_progress("未找到评论滚动容器，已退回整页滚动。")
        # 关键：必须带超时，防止 listen.wait() 无包时死锁
        responses = page.listen.wait(timeout=listen_timeout)
        if not responses:
            consecutive_empty += 1
            if consecutive_empty >= max_consecutive_empty:
                if on_progress:
                    on_progress("连续多次未捕获新评论，已到评论区底部或抓取完毕。")
                break
            time.sleep(1.0)
            continue
        consecutive_empty = 0
        packet_list = responses if isinstance(responses, list) else [responses]
        for packet in packet_list:
            try:
                json_data = packet.response.body
            except Exception:
                continue
            for row in parse_comment_payload(platform, json_data):
                cid = row.get("cid") or f"row-{len(order)}"
                if cid in rows_by_cid:
                    continue
                rows_by_cid[cid] = row
                order.append(cid)
        time.sleep(random.uniform(delay[0], delay[1]))

    return [rows_by_cid[cid] for cid in order]


def rows_to_csv(rows: Sequence[dict]) -> str:
    """导出为 Excel 友好的 CSV 文本（utf-8-sig）。"""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def split_targets(text: str) -> list[str]:
    """把多行/逗号分隔的链接或 ID 拆成列表（去重，保持输入顺序）。"""
    parts = re.split(r"[\s,，;；、]+", str(text or "").strip())
    result: list[str] = []
    for part in parts:
        part = part.strip()
        if part and part not in result:
            result.append(part)
    return result


def merge_comment_rows(*row_groups: Sequence[dict]) -> list[dict]:
    """合并多组采集行，按 cid 去重（无 cid 时按 昵称+内容），保持采集顺序。"""
    merged: list[dict] = []
    seen: set[str] = set()
    for group in row_groups:
        for row in group or []:
            key = str(row.get("cid") or "")
            if not key:
                key = f"{row.get('昵称', '')}|{row.get('评论内容', '')}"
            if key in seen:
                continue
            seen.add(key)
            merged.append(row)
    return merged

