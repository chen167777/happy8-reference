#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按页抓取彩经网（cjcp.cn）快乐8开奖列表，导出统一宽表 CSV。

用法：
    python cjcp_scraper.py --start 1 --pages 50 -o happy8_cjcp.csv

说明：
- 页面为 GB18030 编码的静态 HTML；每页 30 期，按期号降序排列；
  页码 1 为最新一页（https://www.cjcp.cn/kl8kaijiang/），页码越大越早。
- 只发起普通 HTTPS GET 请求，页与页之间随机间隔，不绕过任何访问控制。
- 输出列为：期号,开奖日期,星期,号码1..号码20,销售额(元),奖池金额(元)，
  与应用内“导入开奖表格”的宽表格式一致（该站不提供奖池金额，留空）。
"""
from __future__ import annotations

import argparse
import csv
import io
import random
import re
import time
from typing import Callable

BASE_URL = "https://www.cjcp.cn/kl8kaijiang/"
USER_AGENT = "Mozilla/5.0 (compatible; Happy8LocalReference/1.0)"
WIDE_COLUMNS = ["期号", "开奖日期", "星期"] + [f"号码{i}" for i in range(1, 21)] + ["销售额(元)", "奖池金额(元)"]

_QS_RE = re.compile(r'<div class="qs">([^<]*)</div>')
_DATE_RE = re.compile(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})")
_WEEKDAY_RE = re.compile(r"[（(]([一二三四五六日天])[)）]")
_BALL_RE = re.compile(r'<div class="num-ball[^"]*">(\d{1,2})</div>')
_MONEY_RE = re.compile(r'<div class="money[^"]*">([^<]*)</div>')


def cjcp_page_url(page: int) -> str:
    """页码 1 是不带编号的首页，其余为 /kl8kaijiang/<页码>.html。"""
    page = int(page)
    return BASE_URL if page <= 1 else f"{BASE_URL}{page}.html"


def _clean_money(text: object) -> str:
    """'20,847,282' / '1.2亿元' 等统一取数字位；取不到返回空串。"""
    return re.sub(r"[^\d]", "", str(text or ""))


def parse_cjcp_page(html: str) -> list[dict]:
    """解析一页开奖列表为宽表行（纯函数，便于测试）。

    每行结构：<div class="table-line ..."> 内含 qs（期号）、date（含星期）、
    result（20 个 num-ball）、money（销售额）。
    """
    if not html or 'class="table-line' not in html:
        raise ValueError("页面中没有找到开奖列表（table-line），可能是页面改版或访问被拦截。")
    rows: list[dict] = []
    skipped = 0
    for block in html.split('<div class="table-line')[1:]:
        qs_match = _QS_RE.search(block)
        date_cell = re.search(r'<div class="date">([^<]*)</div>', block)
        date_match = _DATE_RE.search(date_cell.group(1)) if date_cell else None
        balls = [int(value) for value in _BALL_RE.findall(block)]
        if not qs_match or not date_match or len(balls) != 20:
            skipped += 1
            continue
        issue_match = re.search(r"(\d{6,7})", qs_match.group(1))
        if not issue_match:
            skipped += 1
            continue
        weekday_match = _WEEKDAY_RE.search(date_cell.group(1))
        weekday = weekday_match.group(1) if weekday_match else ""
        if weekday == "天":
            weekday = "日"
        money_match = _MONEY_RE.search(block)
        row = {
            "期号": issue_match.group(1),
            "开奖日期": f"{date_match.group(1)}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}",
            "星期": weekday,
            "销售额(元)": _clean_money(money_match.group(1)) if money_match else "",
            "奖池金额(元)": "",  # 该站不提供奖池金额，保留列以对齐导入格式
        }
        for index, value in enumerate(balls, start=1):
            row[f"号码{index}"] = value
        rows.append(row)
    if not rows:
        raise ValueError(f"开奖行解析结果为空（跳过 {skipped} 行），页面结构可能已变化。")
    return rows


def fetch_cjcp_page(page: int, timeout: float = 20.0) -> str:
    """普通 GET 抓取一页；页面是 GB18030 编码，自动识别解码。

    先按系统/环境代理设置请求；代理不通（ProxyError/SSL 错误）时自动改为
    直连重试，避免本机代理软件误伤国内站点。
    """
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - 依赖在 requirements.txt
        raise RuntimeError("缺少 requests，请先安装 requirements.txt。") from exc

    url = cjcp_page_url(page)
    headers = {"User-Agent": USER_AGENT, "Referer": BASE_URL}
    response = None
    last_error: Exception | None = None
    for trust_env in (True, False):
        try:
            session = requests.Session()
            session.trust_env = trust_env  # False 表示忽略代理设置，直接连接
            response = session.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            break
        except Exception as exc:  # noqa: BLE001 - 两种通道都试过再报错
            last_error = exc
            response = None
    if response is None:
        raise RuntimeError(
            "无法访问彩经网（按代理与直连两种方式均失败），请检查网络后重试。"
        ) from last_error
    raw = response.content
    for encoding in ("gb18030", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("gb18030", errors="replace")


def scrape_cjcp_history(
    start_page: int = 1,
    pages: int = 50,
    delay: float = 0.6,
    timeout: float = 20.0,
    on_progress: Callable[[str], None] | None = None,
) -> list[dict]:
    """按页码顺序抓取 pages 页，返回按期号升序的宽表行。"""
    start_page = int(start_page)
    pages = int(pages)
    if not 1 <= start_page <= 500:
        raise ValueError("起始页码需在 1–500 之间。")
    if not 1 <= pages <= 200:
        raise ValueError("单次抓取页数需在 1–200 之间。")

    all_rows: list[dict] = []
    errors: list[str] = []
    for offset in range(pages):
        page = start_page + offset
        if on_progress:
            on_progress(f"正在抓取第 {page} 页（{offset + 1}/{pages}）……")
        try:
            html = fetch_cjcp_page(page, timeout=timeout)
            all_rows.extend(parse_cjcp_page(html))
        except Exception as exc:
            errors.append(f"第 {page} 页：{exc}")
        if delay and offset < pages - 1:
            time.sleep(delay * random.uniform(0.8, 1.4))  # 随机间隔，不高频轮询
    if not all_rows:
        detail = "；".join(errors[:3])
        raise RuntimeError("没有抓到任何开奖数据。" + (f"详情：{detail}" if detail else ""))
    all_rows.sort(key=lambda row: row["期号"])
    return all_rows


def rows_to_wide_csv(rows: list[dict]) -> str:
    """导出为与本地导入一致的宽表 CSV 文本（期号升序行）。"""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=WIDE_COLUMNS)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description="按页抓取彩经网快乐8开奖列表并导出宽表 CSV")
    parser.add_argument("--start", type=int, default=1, help="起始页码（1 为最新一页，默认 1）")
    parser.add_argument("--pages", type=int, default=50, help="按顺序抓取的页数（默认 50，每页 30 期）")
    parser.add_argument("--delay", type=float, default=0.6, help="页与页之间的基准间隔秒数（默认 0.6）")
    parser.add_argument("-o", "--output", default="happy8_cjcp.csv", help="输出 CSV 路径（utf-8-sig）")
    args = parser.parse_args()

    rows = scrape_cjcp_history(
        args.start,
        args.pages,
        delay=args.delay,
        on_progress=lambda message: print(message),
    )
    csv_text = rows_to_wide_csv(rows)
    with open(args.output, "w", encoding="utf-8-sig", newline="") as handle:
        handle.write(csv_text)
    first, last = rows[0]["期号"], rows[-1]["期号"]
    print(f"完成：共 {len(rows)} 期（{first} ~ {last}），已保存到 {args.output}")


if __name__ == "__main__":
    main()

