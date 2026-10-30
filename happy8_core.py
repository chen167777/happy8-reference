"""Core helpers for a local 快乐8 results and reference-number dashboard.

The module deliberately provides descriptive statistics and transparent random
reference sets only. It does not claim to predict a lottery draw.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime
import math
import random
import re
import sqlite3
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

OFFICIAL_API = (
    "https://www.cwl.gov.cn/cwl_admin/front/cwlkj/search/kjxx/findDrawNotice"
)
OFFICIAL_RULES_URL = "https://www.cwl.gov.cn/fcpz/yxjs/kl8/"
DEFAULT_DB = Path(__file__).with_name("happy8.sqlite3")
USER_AGENT = "Mozilla/5.0 (compatible; Happy8LocalReference/1.0)"
BILIBILI_VIEW_API = "https://api.bilibili.com/x/web-interface/view"
BILIBILI_REPLY_API = "https://api.bilibili.com/x/v2/reply"


@dataclass(frozen=True)
class Draw:
    issue: str
    date: str
    numbers: tuple[int, ...]
    source: str = "official"

    def __post_init__(self) -> None:
        object.__setattr__(self, "issue", str(self.issue).strip())
        object.__setattr__(self, "date", str(self.date or "").strip())
        object.__setattr__(self, "numbers", validate_draw_numbers(self.numbers))
        object.__setattr__(self, "source", str(self.source or "official").strip())


def validate_draw_numbers(values: Iterable[int | str]) -> tuple[int, ...]:
    """Validate and return one 快乐8 draw: 20 unique values from 1 to 80."""
    try:
        numbers = tuple(int(value) for value in values)
    except (TypeError, ValueError) as exc:
        raise ValueError("开奖号码必须是 1–80 的整数。") from exc
    if len(numbers) != 20:
        raise ValueError(f"快乐8每期开奖应有 20 个号码；当前读到 {len(numbers)} 个。")
    if any(number < 1 or number > 80 for number in numbers):
        raise ValueError("号码必须在 1–80 之间。")
    if len(set(numbers)) != 20:
        raise ValueError("同一期的 20 个号码不能重复。")
    return tuple(sorted(numbers))


def _parse_number_field(value: object) -> tuple[int, ...]:
    if isinstance(value, (list, tuple)):
        return validate_draw_numbers(value)
    if value is None:
        raise ValueError("缺少开奖号码字段。")
    text = str(value).strip()
    # Some feeds encode 20 two-digit values without separators.
    if text.isdigit() and len(text) == 40:
        return validate_draw_numbers(text[index:index + 2] for index in range(0, 40, 2))
    tokens = re.findall(r"(?<!\d)\d{1,2}(?!\d)", text)
    return validate_draw_numbers(tokens)


def parse_official_record(record: Mapping[str, object]) -> Draw:
    """Convert one JSON record from the official 快乐8 feed into a Draw."""
    issue = record.get("code") or record.get("issue") or record.get("期号")
    if issue is None or not str(issue).strip():
        raise ValueError("开奖记录缺少期号。")
    raw_numbers = None
    for key in ("red", "numbers", "number", "openCode", "开奖号码", "号码"):
        if record.get(key) not in (None, ""):
            raw_numbers = record[key]
            break
    numbers = _parse_number_field(raw_numbers)
    draw_date = record.get("date") or record.get("开奖日期") or record.get("drawDate") or ""
    return Draw(issue=str(issue), date=str(draw_date), numbers=numbers, source="official")


def _request_official_page(params: Mapping[str, str], timeout: float) -> list[dict]:
    """Issue one ordinary HTTPS GET to the public official feed (no cookies)."""
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - dependency is in requirements.txt
        raise RuntimeError("缺少 requests，请先安装 requirements.txt。") from exc

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        "Referer": OFFICIAL_RULES_URL,
        "User-Agent": USER_AGENT,
    }
    try:
        response = requests.get(
            OFFICIAL_API,
            params=dict(params),
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise RuntimeError(
            "无法从中国福彩网读取开奖数据。可稍后重试，或导入从官方渠道取得的 CSV；"
            "本程序不会尝试绕过站点访问限制。"
        ) from exc

    records = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        message = payload.get("message", "接口未返回开奖列表") if isinstance(payload, dict) else "响应格式异常"
        raise RuntimeError(f"官方接口返回异常：{message}")
    return records


def _append_parsed_records(
    records: Sequence[dict],
    draws: list[Draw],
    errors: list[str],
) -> None:
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            errors.append(f"第 {len(draws) + index} 条记录格式不正确")
            continue
        try:
            draws.append(parse_official_record(record))
        except (ValueError, TypeError) as exc:
            errors.append(f"第 {len(draws) + index} 条：{exc}")


def fetch_official_draws(limit: int = 100, timeout: float = 20.0, *, page_size: int = 200) -> list[Draw]:
    """Fetch recent results from the China Welfare Lottery official site.

    Only ordinary HTTPS requests are used — no login cookies, no anti-bot
    evasion, no aggressive retries. Syncs up to 1000 draws in one request;
    larger syncs paginate politely (a short pause between pages) so a single
    call can backfill longer history. If the endpoint is unavailable, the
    caller should offer the CSV import path instead.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("单次同步数量需在 1–5000 之间的整数。") from exc
    if not 1 <= limit <= 5000:
        raise ValueError("单次同步数量需在 1–5000 之间。")

    draws: list[Draw] = []
    errors: list[str] = []
    if limit <= 1000:
        # One request keeps the long-validated parameter shape for small syncs.
        params = {
            "name": "kl8",
            "issueCount": str(limit),
            "pageNo": "1",
            "pageSize": str(limit),
            "systemType": "PC",
        }
        _append_parsed_records(_request_official_page(params, timeout), draws, errors)
    else:
        chunk = max(1, min(int(page_size), 500))
        page_no = 1
        while len(draws) < limit:
            want = min(chunk, limit - len(draws))
            params = {
                "name": "kl8",
                "pageNo": str(page_no),
                "pageSize": str(want),
                "systemType": "PC",
            }
            records = _request_official_page(params, timeout)
            if not records:
                break
            _append_parsed_records(records, draws, errors)
            if len(records) < want:
                break  # the feed is exhausted
            page_no += 1
            if len(draws) < limit:
                time.sleep(0.3)  # polite pause; never a tight polling loop
    if not draws:
        detail = "；".join(errors[:3])
        raise RuntimeError("接口没有返回可识别的有效开奖记录。" + (f"详情：{detail}" if detail else ""))
    return draws[:limit]


def _normalise_header(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    # Excel/CSV exports sometimes contain BOM, zero-width characters, or NULs.
    return re.sub(r"[\s\ufeff\u200b\u200c\u200d\x00]+", "", text).lower()


def _clean_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    if re.fullmatch(r"#+", text):
        # Excel displays hashes when a date/number column is too narrow.
        return ""
    if re.fullmatch(r"\d+\.0+", text):
        return text.split(".", 1)[0]
    return text


def _parse_import_row(row: Mapping[str, object], row_index: int, source: str) -> Draw:
    normalized = {
        _normalise_header(key): value
        for key, value in row.items()
        if key is not None
    }
    issue_value = next(
        (normalized[key] for key in ("issue", "code", "期号") if _clean_cell(normalized.get(key))),
        None,
    )
    date_value = next(
        (normalized[key] for key in ("date", "drawdate", "开奖日期", "日期") if _clean_cell(normalized.get(key))),
        "",
    )
    number_field = next(
        (
            normalized[key]
            for key in ("numbers", "red", "number", "开奖号", "开奖号码", "号码")
            if _clean_cell(normalized.get(key))
        ),
        None,
    )
    if not issue_value:
        raise ValueError(f"第 {row_index} 行：缺少 issue/期号 列。")

    try:
        if number_field is not None:
            numbers = _parse_number_field(number_field)
        else:
            columns = None
            for prefix in ("n", "num", "red", "号码"):
                candidates = []
                for index in range(1, 21):
                    value = normalized.get(f"{prefix}{index}")
                    if value in (None, ""):
                        value = normalized.get(f"{prefix}{index:02d}")
                    candidates.append(value)
                if all(value not in (None, "") for value in candidates):
                    columns = candidates
                    break
            if columns is None:
                raise ValueError(
                    "找不到开奖号码列；支持 numbers/开奖号码，或 n1–n20、num1–num20、red1–red20、号码1–号码20。"
                )
            numbers = validate_draw_numbers(columns)
    except ValueError as exc:
        raise ValueError(f"第 {row_index} 行：{exc}") from exc

    issue = _clean_cell(issue_value)
    draw_date = _clean_cell(date_value)
    return Draw(issue=issue, date=draw_date, numbers=numbers, source=source)


def _parse_import_rows(rows: Iterable[tuple[int, Mapping[str, object]]], source: str) -> list[Draw]:
    errors: list[str] = []
    draws: list[Draw] = []
    for row_index, row in rows:
        if not row or not any(_clean_cell(value) for value in row.values()):
            continue
        try:
            draws.append(_parse_import_row(row, row_index, source))
        except (ValueError, TypeError) as exc:
            errors.append(str(exc))
    if errors:
        preview = "；".join(errors[:5])
        extra = f"（另有 {len(errors) - 5} 行错误未显示）" if len(errors) > 5 else ""
        raise ValueError(f"表格校验失败：{preview}{extra}")
    if not draws:
        raise ValueError("表格中没有有效开奖数据行。")
    return draws


def _is_result_header(values: Sequence[object]) -> bool:
    names = [_normalise_header(value) for value in values]
    has_issue = any(name in {"issue", "code", "期号"} for name in names)
    has_numbers = any(
        name in {"numbers", "red", "number", "开奖号", "开奖号码", "号码"}
        or re.fullmatch(r"(?:n|num|red|号码)0?\d+", name)
        for name in names
    )
    return has_issue and has_numbers


def _detect_delimited_table(text: str) -> tuple[list[list[str]], int]:
    """Detect comma/TSV/semicolon/pipe delimiter and locate the header row."""
    choices = []
    header_examples = []
    for preference, delimiter in enumerate((",", "\t", ";", "|")):
        try:
            rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        except csv.Error:
            continue
        for header_index, row in enumerate(rows[:20]):
            if not any(str(value or "").strip() for value in row):
                continue
            if _is_result_header(row):
                # Prefer the table with the most columns, then the earliest header;
                # comma is only a final tie-breaker (TSV exports are supported too).
                choices.append(((len(row), -header_index, -preference), rows, header_index))
                break
            if len(header_examples) < 4 and header_index == 0:
                header_examples.append(" | ".join(str(value)[:24] for value in row[:5]))
    if not choices:
        sample = "；".join(header_examples) if header_examples else "无法读取表头"
        raise ValueError(
            "没有识别到包含‘期号’和‘号码1…号码20’（或 numbers 列）的表头。"
            f"检测到的表头片段：{sample}。请上传原始 .xlsx，或另存为 UTF-8/GB18030 编码的 CSV/制表符文本。"
        )
    _, rows, header_index = max(choices, key=lambda choice: choice[0])
    return rows, header_index


def parse_draw_csv(csv_text: str) -> list[Draw]:
    """Read comma-, tab-, semicolon-, or pipe-delimited text tables.

    It scans the first 20 rows for the header, so a title row before the table is
    okay. Extra weekday, sales, and prize-pool columns are ignored.
    """
    if not csv_text or not csv_text.strip():
        raise ValueError("CSV/文本文件为空。")
    rows, header_index = _detect_delimited_table(csv_text)
    headers = [str(value or "").strip() for value in rows[header_index]]
    import_rows = []
    for row_index, values in enumerate(rows[header_index + 1:], start=header_index + 2):
        row = {
            headers[column]: value
            for column, value in enumerate(values[:len(headers)])
            if headers[column]
        }
        import_rows.append((row_index, row))
    return _parse_import_rows(import_rows, source="csv")


def parse_draw_excel(content: bytes) -> list[Draw]:
    """Read an .xlsx workbook with 期号、开奖日期、号码1…号码20 columns."""
    if not content:
        raise ValueError("Excel 文件为空。")
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - dependency is in requirements.txt
        raise RuntimeError("缺少 openpyxl，请先安装 requirements.txt。") from exc
    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:
        raise ValueError("无法读取此 Excel 文件；请另存为 .xlsx 或 CSV。") from exc

    try:
        for worksheet in workbook.worksheets:
            rows = list(worksheet.iter_rows(values_only=True))
            header_index = None
            headers: list[str] = []
            # Allow a few title/blank rows above the table header.
            for index, values in enumerate(rows[:15]):
                candidate = [_normalise_header(value) for value in values]
                has_issue = any(name in {"issue", "code", "期号"} for name in candidate)
                has_number = any(
                    name in {"numbers", "red", "number", "开奖号", "开奖号码", "号码"}
                    or re.fullmatch(r"(?:n|num|red|号码)0?\d+", name)
                    for name in candidate
                )
                if has_issue and has_number:
                    header_index = index
                    headers = [_clean_cell(value) for value in values]
                    break
            if header_index is None:
                continue
            import_rows = []
            for row_index, values in enumerate(rows[header_index + 1:], start=header_index + 2):
                row = {
                    headers[column]: value
                    for column, value in enumerate(values[:len(headers)])
                    if headers[column]
                }
                import_rows.append((row_index, row))
            return _parse_import_rows(import_rows, source="xlsx")
        raise ValueError("工作簿中没有找到包含期号和号码列的表格。")
    finally:
        workbook.close()


def init_db(db_path: str | Path = DEFAULT_DB) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS draws (
                issue TEXT PRIMARY KEY,
                draw_date TEXT NOT NULL DEFAULT '',
                numbers_json TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'official'
            )"""
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_draw_date ON draws(draw_date DESC, issue DESC)")


def save_draws(draws: Iterable[Draw], db_path: str | Path = DEFAULT_DB) -> int:
    batch = list(draws)
    if not batch:
        return 0
    init_db(db_path)
    rows = [
        (draw.issue, draw.date, json.dumps(draw.numbers), draw.source)
        for draw in batch
    ]
    with sqlite3.connect(Path(db_path)) as connection:
        connection.executemany(
            """INSERT INTO draws(issue, draw_date, numbers_json, source)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(issue) DO UPDATE SET
                 draw_date=excluded.draw_date,
                 numbers_json=excluded.numbers_json,
                 source=excluded.source""",
            rows,
        )
    return len(rows)


def load_draws(db_path: str | Path = DEFAULT_DB, limit: int | None = None) -> list[Draw]:
    init_db(db_path)
    query = "SELECT issue, draw_date, numbers_json, source FROM draws ORDER BY draw_date DESC, issue DESC"
    params: tuple[object, ...] = ()
    if limit is not None:
        if int(limit) < 1:
            return []
        query += " LIMIT ?"
        params = (int(limit),)
    with sqlite3.connect(Path(db_path)) as connection:
        rows = connection.execute(query, params).fetchall()
    return [
        Draw(issue=row[0], date=row[1], numbers=tuple(json.loads(row[2])), source=row[3])
        for row in rows
    ]


# ===== 智能 1–80 号码提取（算法取自用户脚本《我的中奖梦.py》）=====
CHINESE_MAP = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5,
    "陆": 6, "柒": 7, "捌": 8, "玖": 9, "拾": 10,
    "廿": 20, "卅": 30, "卌": 40,
}

SPECIAL_SYMBOLS = {
    "①": 1, "②": 2, "③": 3, "④": 4, "⑤": 5, "⑥": 6, "⑦": 7, "⑧": 8, "⑨": 9, "⑩": 10,
    "⑪": 11, "⑫": 12, "⑬": 13, "⑭": 14, "⑮": 15, "⑯": 16, "⑰": 17, "⑱": 18, "⑲": 19, "⑳": 20,
    "❶": 1, "❷": 2, "❸": 3, "❹": 4, "❺": 5, "❻": 6, "❼": 7, "❽": 8, "❾": 9, "❿": 10,
    "1️⃣": 1, "2️⃣": 2, "3️⃣": 3, "4️⃣": 4, "5️⃣": 5, "6️⃣": 6, "7️⃣": 7, "8️⃣": 8, "9️⃣": 9,
}


def parse_chinese_number(cn_str: str) -> int | None:
    """解析中文数字（1–80）：二十三→23、零八→8、廿五→25、七十九→79。"""
    cn_str = cn_str.strip()
    if not cn_str:
        return None
    if cn_str in CHINESE_MAP:
        return CHINESE_MAP[cn_str]
    if len(cn_str) == 2:
        if cn_str.startswith("零") and cn_str[1] in CHINESE_MAP:
            return CHINESE_MAP[cn_str[1]]
        if cn_str.startswith(("十", "拾")) and cn_str[1] in CHINESE_MAP:
            return 10 + CHINESE_MAP[cn_str[1]]
        if cn_str.endswith(("十", "拾")) and cn_str[0] in CHINESE_MAP:
            return CHINESE_MAP[cn_str[0]] * 10
        if cn_str.startswith("廿") and cn_str[1] in CHINESE_MAP:
            return 20 + CHINESE_MAP[cn_str[1]]
        if cn_str.startswith("卅") and cn_str[1] in CHINESE_MAP:
            return 30 + CHINESE_MAP[cn_str[1]]
    if len(cn_str) == 3 and cn_str[1] in ("十", "拾"):
        tens = CHINESE_MAP.get(cn_str[0], 0)
        units = CHINESE_MAP.get(cn_str[2], 0)
        res = tens * 10 + units
        if 1 <= res <= 80:
            return res
    return None


def extract_valid_numbers_from_text(raw_text: str) -> list[int]:
    """从一条文本中智能提取 1–80 号码（每条内去重，保持出现顺序）。

    支持中文数字、Emoji 序号与连写拆分；过滤 @用户、日期、时间、金额量词
    等常见干扰。算法参考用户脚本《我的中奖梦.py》。
    """
    if not raw_text or not isinstance(raw_text, str):
        return []

    extracted: list[int] = []
    text = raw_text

    # 1. 过滤 @用户
    text = re.sub(r"@[\w\u4e00-\u9fa5_]+", " ", text)

    # 2. 过滤 日期、年份、时间上下文（防止 2024-05-18 12:30 被解析成号码）
    text = re.sub(r"\b(?:19|20)\d{2}[-/年.]\d{1,2}[-/月.]\d{1,2}(?:日)?\b", " ", text)
    text = re.sub(r"\b(?:19|20)\d{2}(?:年)?\b", " ", text)
    text = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", " ", text)
    text = re.sub(r"\d{1,2}点(?:\d{1,2}分)?", " ", text)

    # 3. 过滤 金额与量词（防止 80元、100个赞、第1名 被误采）
    text = re.sub(r"\d+(?:\.\d+)?\s*(?:元|块钱?|万元?|个赞|点赞|赞|斤|岁|名|楼|期)", " ", text)
    text = re.sub(r"第\s*\d+\s*(?:名|位|期|个)", " ", text)

    # 4. 提取 特殊符号与Emoji ①~⑳、❶~❿、1️⃣~9️⃣
    for symbol, value in SPECIAL_SYMBOLS.items():
        if symbol in text:
            if 1 <= value <= 80:
                extracted.append(value)
            text = text.replace(symbol, " ")

    # 5. 提取 中文数字
    cn_pattern = re.compile(
        r"(?:零[一二三四五六七八九壹贰叁肆伍陆柒捌玖]"
        r"|(?:[二三四五六七八两壹贰叁肆伍陆柒捌]?[十拾][一二三四五六七八九壹贰叁肆伍陆柒捌玖]?)"
        r"|(?:[廿卅卌][一二三四五六七八九]?)"
        r"|[一二两三四五六七八九壹贰叁肆伍陆柒捌玖])"
    )
    for match in cn_pattern.finditer(text):
        value = parse_chinese_number(match.group())
        if value is not None and 1 <= value <= 80:
            extracted.append(value)
    text = cn_pattern.sub(" ", text)

    # 6. 提取 阿拉伯数字（支持 01、80 及连写 01082379）
    for token in re.findall(r"\d+", text):
        if len(token) <= 2:
            value = int(token)
            if 1 <= value <= 80:
                extracted.append(value)
        elif len(token) >= 4 and len(token) % 2 == 0:
            # 尝试按两位一组拆分连码
            sub_chunks = [int(token[index:index + 2]) for index in range(0, len(token), 2)]
            if all(1 <= chunk <= 80 for chunk in sub_chunks):
                extracted.extend(sub_chunks)

    # 7. 单条内去重，保持出现顺序
    return list(dict.fromkeys(extracted))


def extract_opinion_counts(text: str) -> tuple[dict[int, int], int]:
    """Count number mentions from user-pasted public comments, in memory only.

    Each non-empty line is treated as one comment, and each number contributes
    at most one mention per line to limit repetition/spam effects. Number
    recognition uses the smart extractor (Chinese numerals, emoji digits,
    connected digits; dates/times/prices filtered). The raw text and user
    identifiers are not written to disk by this function.
    """
    if len(text) > 1_000_000:
        raise ValueError("粘贴文本过长，请控制在 1 MB 以内。")
    counts: Counter[int] = Counter()
    parsed_comments = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        values = set(extract_valid_numbers_from_text(line))
        if values:
            parsed_comments += 1
            counts.update(values)
    return {number: counts[number] for number in range(1, 81)}, parsed_comments


@dataclass(frozen=True)
class CommentFetchResult:
    """In-memory result of one comment fetch; raw texts are never persisted."""

    comments: tuple[str, ...]
    counts: dict[int, int]
    parsed_lines: int
    source_label: str
    errors: tuple[str, ...] = ()


_BV_PATTERN = re.compile(r"BV[0-9A-Za-z]{10}", re.IGNORECASE)
_AV_PATTERN = re.compile(r"(?:bilibili\.com/video/)?av(\d+)", re.IGNORECASE)


def parse_bilibili_target(text: str) -> dict[str, str]:
    """Extract a B站 video identifier (BV id or avid) from a URL or bare id."""
    cleaned = str(text or "").strip()
    if not cleaned:
        raise ValueError("请先粘贴 B站视频链接或 BV 号。")
    match = _BV_PATTERN.search(cleaned)
    if match:
        return {"bvid": match.group(0)}
    match = _AV_PATTERN.search(cleaned)
    if match:
        return {"aid": match.group(1)}
    raise ValueError("无法识别 B站视频链接；示例：https://www.bilibili.com/video/BV1xx411c7XX")


def _parse_reply_page(payload: Mapping[str, object]) -> tuple[list[str], int]:
    """Return comment texts and the reported total from one comment API page."""
    if not isinstance(payload, dict):
        raise ValueError("评论接口响应格式异常。")
    code = payload.get("code")
    if code not in (0, None):
        message = payload.get("message") or payload.get("msg") or code
        raise RuntimeError(f"B站评论接口返回错误：{message}")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    page_info = data.get("page") if isinstance(data.get("page"), dict) else {}
    try:
        total = int(page_info.get("count") or 0)
    except (TypeError, ValueError):
        total = 0
    texts: list[str] = []
    for reply in data.get("replies") or []:
        if not isinstance(reply, dict):
            continue
        content = reply.get("content")
        message = content.get("message") if isinstance(content, dict) else None
        if message and str(message).strip():
            texts.append(str(message).strip())
    return texts, total


def fetch_bilibili_comments(
    target: str,
    max_comments: int = 200,
    timeout: float = 15.0,
    request_delay: float = 0.25,
) -> CommentFetchResult:
    """Fetch public comment texts of one B站 video with ordinary HTTPS GETs.

    Policy: no login, no cookies, no signature/captcha bypass, and only a
    handful of politely spaced requests. 抖音/快手 are intentionally
    unsupported because their feeds require signed clients or login state and
    this project will not evade those controls. Raw comment texts stay in
    memory only and are never written to the database.
    """
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - dependency is in requirements.txt
        raise RuntimeError("缺少 requests，请先安装 requirements.txt。") from exc

    try:
        limit = int(max_comments)
    except (TypeError, ValueError) as exc:
        raise ValueError("单次获取评论数量需在 1–500 之间的整数。") from exc
    if not 1 <= limit <= 500:
        raise ValueError("单次获取评论数量需在 1–500 之间。")

    ids = parse_bilibili_target(target)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.bilibili.com/",
        "User-Agent": USER_AGENT,
    }

    def get_json(url: str, params: Mapping[str, object]) -> dict:
        try:
            response = requests.get(
                url,
                params={key: str(value) for key, value in params.items()},
                headers=headers,
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise RuntimeError(
                "无法访问 B站公开评论接口，请稍后重试；也可以手动粘贴评论文本。"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError("B站接口响应格式异常，请稍后重试。")
        return payload

    info_payload = get_json(BILIBILI_VIEW_API, ids)
    if info_payload.get("code") not in (0, None):
        message = info_payload.get("message") or info_payload.get("code")
        raise RuntimeError(f"无法读取该视频信息：{message}。请确认链接是否有效。")
    info = info_payload.get("data") if isinstance(info_payload.get("data"), dict) else {}
    title = str(info.get("title") or "").strip()
    aid = info.get("aid") or ids.get("aid")
    if not aid:
        raise RuntimeError("未能解析视频 avid，请更换链接重试。")
    source_label = f"B站视频《{title or ids.get('bvid') or ('av' + str(aid))}》"

    comments: list[str] = []
    errors: list[str] = []
    page_size = 20
    page = 1
    while len(comments) < limit and page <= 25:
        payload = get_json(
            BILIBILI_REPLY_API,
            {"type": "1", "oid": aid, "pn": page, "ps": page_size, "sort": "2"},
        )
        try:
            texts, _total = _parse_reply_page(payload)
        except ValueError as exc:
            errors.append(str(exc))
            break
        comments.extend(texts)
        if len(texts) < page_size:
            break
        page += 1
        if request_delay and request_delay > 0 and len(comments) < limit:
            time.sleep(request_delay)

    comments = comments[:limit]
    joined = "\n".join(" ".join(text.split()) for text in comments)
    counts, parsed_lines = extract_opinion_counts(joined)
    return CommentFetchResult(
        comments=tuple(comments),
        counts=counts,
        parsed_lines=parsed_lines,
        source_label=source_label,
        errors=tuple(errors),
    )


def analyze_history(
    draws: Sequence[Draw],
    opinion_counts: Mapping[int, int] | None = None,
) -> list[dict[str, int | float]]:
    """Descriptive frequency/gap table. Draws should be newest first."""
    mentions = opinion_counts or {}
    frequency: Counter[int] = Counter()
    last_seen: dict[int, int | None] = {number: None for number in range(1, 81)}
    for age, draw in enumerate(draws):
        for number in draw.numbers:
            frequency[number] += 1
            if last_seen[number] is None:
                last_seen[number] = age
    total_draws = len(draws)
    rows: list[dict[str, int | float]] = []
    for number in range(1, 81):
        count = frequency[number]
        rows.append({
            "number": number,
            "count": count,
            "rate": count / total_draws if total_draws else 0.0,
            "expected_count": 0.25 * total_draws,
            "gap": last_seen[number] if last_seen[number] is not None else total_draws,
            "mentions": int(mentions.get(number, 0)),
        })
    return rows


def _z_scores(values: Mapping[int, float]) -> dict[int, float]:
    numbers = list(range(1, 81))
    raw = [float(values.get(number, 0.0)) for number in numbers]
    mean = sum(raw) / len(raw)
    variance = sum((value - mean) ** 2 for value in raw) / len(raw)
    deviation = math.sqrt(variance)
    if deviation == 0:
        return {number: 0.0 for number in numbers}
    return {
        number: max(-3.0, min(3.0, (float(values.get(number, 0.0)) - mean) / deviation))
        for number in numbers
    }


def _weighted_sample_without_replacement(
    population: Sequence[int],
    weights: Mapping[int, float],
    count: int,
    rng: random.Random,
) -> tuple[int, ...]:
    pool = list(population)
    selected: list[int] = []
    for _ in range(count):
        total = sum(max(0.000001, float(weights[number])) for number in pool)
        point = rng.random() * total
        chosen = pool[-1]
        for number in pool:
            point -= max(0.000001, float(weights[number]))
            if point <= 0:
                chosen = number
                break
        selected.append(chosen)
        pool.remove(chosen)
    return tuple(sorted(selected))


def generate_reference_sets(
    draws: Sequence[Draw] = (),
    opinion_counts: Mapping[int, int] | None = None,
    *,
    history_strength: float = 0.0,
    opinion_strength: float = 0.0,
    seed: int | str | None = None,
) -> dict[int, tuple[int, ...]]:
    """Generate one transparent random reference set for each 选一…选十 play.

    Strength values are optional experimental tilts applied to standardized
    historical frequency and pasted-comment mentions. At 0.0 the sampler is
    uniform over 1..80. Neither tilt changes the true draw probabilities.
    """
    if not 0.0 <= history_strength <= 0.5 or not 0.0 <= opinion_strength <= 0.5:
        raise ValueError("权重范围为 0.0–0.5。")
    recent = list(draws)
    history_counts = {row["number"]: row["count"] for row in analyze_history(recent)}
    mentions = opinion_counts or {}
    history_z = _z_scores(history_counts)
    opinion_z = _z_scores({number: float(mentions.get(number, 0)) for number in range(1, 81)})
    weights = {
        number: math.exp(max(-3.0, min(3.0,
            history_strength * history_z[number] + opinion_strength * opinion_z[number]
        )))
        for number in range(1, 81)
    }
    rng = random.Random(seed)
    population = tuple(range(1, 81))
    return {
        pick_count: _weighted_sample_without_replacement(population, weights, pick_count, rng)
        for pick_count in range(1, 11)
    }


def top_hit_probability(pick_count: int) -> float:
    """Probability all picked numbers are among a 20-of-80 draw."""
    if not 1 <= int(pick_count) <= 10:
        raise ValueError("玩法数量需在 1–10 之间。")
    return math.comb(20, int(pick_count)) / math.comb(80, int(pick_count))


def format_probability(probability: float) -> str:
    if probability <= 0:
        return "0"
    return f"{probability:.6%}（约 1 / {1 / probability:,.0f}）"
