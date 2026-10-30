import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from openpyxl import Workbook

from cjcp_scraper import cjcp_page_url, parse_cjcp_page, rows_to_wide_csv
from comment_crawler import (
    detect_platform,
    merge_comment_rows,
    normalize_video_url,
    parse_comment_payload,
    rows_to_csv,
    split_targets,
)
from happy8_core import (
    Draw,
    _parse_reply_page,
    analyze_history,
    extract_opinion_counts,
    extract_valid_numbers_from_text,
    fetch_official_draws,
    generate_reference_sets,
    load_draws,
    parse_bilibili_target,
    parse_draw_csv,
    parse_draw_excel,
    parse_official_record,
    save_draws,
    top_hit_probability,
    validate_draw_numbers,
)


class Happy8CoreTests(unittest.TestCase):
    def make_draw(self, issue="2026001", date="2026-01-01"):
        return Draw(issue, date, tuple(range(1, 21)))

    def test_draw_validation(self):
        self.assertEqual(validate_draw_numbers(range(1, 21)), tuple(range(1, 21)))
        with self.assertRaises(ValueError):
            validate_draw_numbers(range(1, 20))
        with self.assertRaises(ValueError):
            validate_draw_numbers([1] * 20)
        with self.assertRaises(ValueError):
            validate_draw_numbers(list(range(1, 20)) + [81])

    def test_parse_official_record(self):
        draw = parse_official_record({
            "code": "2026001",
            "date": "2026-01-01",
            "red": "01,02,03,04,05,06,07,08,09,10,11,12,13,14,15,16,17,18,19,20",
        })
        self.assertEqual(draw.issue, "2026001")
        self.assertEqual(draw.numbers, tuple(range(1, 21)))

    def test_parse_csv_number_list_and_columns(self):
        numbers = " ".join(f"{n:02d}" for n in range(1, 21))
        csv_text = f"issue,date,numbers\n2026001,2026-01-01,\"{numbers}\"\n"
        parsed = parse_draw_csv(csv_text)
        self.assertEqual(parsed[0].numbers, tuple(range(1, 21)))
        columns = "issue,date," + ",".join(f"n{i}" for i in range(1, 21))
        values = "2026002,2026-01-02," + ",".join(str(n) for n in range(1, 21))
        parsed_columns = parse_draw_csv(columns + "\n" + values + "\n")
        self.assertEqual(parsed_columns[0].issue, "2026002")

    def test_parse_user_chinese_wide_csv_schema(self):
        headers = ["期号", "开奖日期", "星期"] + [f"号码{i}" for i in range(1, 21)] + ["销售额(元)", "奖池金额(元)"]
        values = ["2023335", "##########", "五"] + [str(n) for n in range(1, 21)] + ["133553506", "146510260"]
        parsed = parse_draw_csv(",".join(headers) + "\n" + ",".join(values) + "\n")
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].issue, "2023335")
        self.assertEqual(parsed[0].date, "")
        self.assertEqual(parsed[0].numbers, tuple(range(1, 21)))

    def test_parse_tab_delimited_export_with_title_and_bom(self):
        headers = ["\ufeff期号", "开奖日期", "星期"] + [f"号码{i}" for i in range(1, 21)] + ["销售额(元)", "奖池金额(元)"]
        values = ["2023337", "##########", "日"] + [str(n) for n in range(1, 21)] + ["112061284", "160544258"]
        text = "快乐8历史数据\n" + "\t".join(headers) + "\n" + "\t".join(values) + "\n"
        parsed = parse_draw_csv(text)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].issue, "2023337")
        self.assertEqual(parsed[0].numbers, tuple(range(1, 21)))

    def test_parse_user_chinese_wide_xlsx_schema(self):
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.append(["期号", "开奖日期", "星期"] + [f"号码{i}" for i in range(1, 21)] + ["销售额(元)", "奖池金额(元)"])
        worksheet.append([2023336, "##########", "六"] + list(range(1, 21)) + [119616798, 151393192])
        stream = BytesIO()
        workbook.save(stream)
        parsed = parse_draw_excel(stream.getvalue())
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].issue, "2023336")
        self.assertEqual(parsed[0].source, "xlsx")
        self.assertEqual(parsed[0].numbers, tuple(range(1, 21)))

    def test_history_stats(self):
        draws = [self.make_draw("2", "2026-01-02"), self.make_draw("1", "2026-01-01")]
        rows = analyze_history(draws)
        row_1 = rows[0]
        row_21 = rows[20]
        self.assertEqual(row_1["count"], 2)
        self.assertEqual(row_1["gap"], 0)
        self.assertEqual(row_21["count"], 0)
        self.assertEqual(row_21["gap"], 2)

    def test_opinion_counts_deduplicate_per_line_and_ignore_issue_id(self):
        text = "第2026281期我选 08、18、18、80\n喜欢 8 18 27\n没有号码"
        counts, parsed = extract_opinion_counts(text)
        self.assertEqual(parsed, 2)
        self.assertEqual(counts[8], 2)
        self.assertEqual(counts[18], 2)
        self.assertEqual(counts[80], 1)
        self.assertEqual(counts[28], 0)

    def test_reference_sets_are_valid_and_seeded(self):
        first = generate_reference_sets(seed=123)
        second = generate_reference_sets(seed=123)
        self.assertEqual(first, second)
        self.assertEqual(set(first), set(range(1, 11)))
        for pick_count, values in first.items():
            self.assertEqual(len(values), pick_count)
            self.assertEqual(len(set(values)), pick_count)
            self.assertTrue(all(1 <= value <= 80 for value in values))

    def test_top_hit_probability(self):
        self.assertAlmostEqual(top_hit_probability(1), 0.25)
        self.assertGreater(top_hit_probability(1), top_hit_probability(10))
        with self.assertRaises(ValueError):
            top_hit_probability(11)

    def test_sqlite_upsert_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "draws.sqlite3"
            draw = self.make_draw()
            save_draws([draw], database)
            save_draws([Draw(draw.issue, "2026-01-03", tuple(range(1, 21)), source="csv")], database)
            loaded = load_draws(database)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].date, "2026-01-03")
            self.assertEqual(loaded[0].source, "csv")

    def test_parse_bilibili_target(self):
        self.assertEqual(
            parse_bilibili_target("https://www.bilibili.com/video/BV1xx411c7XX?spm_id_from=333"),
            {"bvid": "BV1xx411c7XX"},
        )
        self.assertEqual(parse_bilibili_target("BV1xx411c7XX"), {"bvid": "BV1xx411c7XX"})
        self.assertEqual(
            parse_bilibili_target("https://www.bilibili.com/video/av170001"),
            {"aid": "170001"},
        )
        with self.assertRaises(ValueError):
            parse_bilibili_target("https://example.com/watch/123")
        with self.assertRaises(ValueError):
            parse_bilibili_target("   ")

    def test_parse_reply_page_extracts_messages(self):
        payload = {
            "code": 0,
            "data": {
                "page": {"count": 2},
                "replies": [
                    {"content": {"message": "我看好 03 18 27"}},
                    {"content": {"message": "   "}},
                    {"content": {"message": "今天想选 08、22、56"}},
                    {"content": None},
                ],
            },
        }
        texts, total = _parse_reply_page(payload)
        self.assertEqual(total, 2)
        self.assertEqual(len(texts), 2)
        empty_texts, empty_total = _parse_reply_page(
            {"code": 0, "data": {"replies": None, "page": {"count": 0}}}
        )
        self.assertEqual(empty_texts, [])
        self.assertEqual(empty_total, 0)
        with self.assertRaises(RuntimeError):
            _parse_reply_page({"code": -412, "message": "请求被拦截"})

    def test_fetch_official_draws_validates_limit(self):
        with self.assertRaises(ValueError):
            fetch_official_draws(0)
        with self.assertRaises(ValueError):
            fetch_official_draws(5001)

    def test_extract_valid_numbers_smart(self):
        self.assertEqual(
            extract_valid_numbers_from_text("第2026281期我选 08、18、18、80"),
            [8, 18, 80],
        )
        self.assertEqual(extract_valid_numbers_from_text("二十三 七十九 廿五"), [23, 79, 25])
        self.assertEqual(extract_valid_numbers_from_text("① ❷ 3️⃣"), [1, 2, 3])
        self.assertEqual(extract_valid_numbers_from_text("01082379"), [1, 8, 23, 79])
        self.assertEqual(extract_valid_numbers_from_text("2024-05-18 12:30 80元 第1名 100个赞"), [])

    def test_detect_platform(self):
        self.assertEqual(detect_platform("https://www.douyin.com/video/7618257619753066203"), "douyin")
        self.assertEqual(detect_platform("https://v.douyin.com/iRNBho6/"), "douyin")
        self.assertEqual(detect_platform("https://www.bilibili.com/video/BV1xx411c7XX"), "bilibili")
        with self.assertRaises(ValueError):
            detect_platform("https://example.com/watch/1")
        with self.assertRaises(ValueError):
            detect_platform("")

    def test_parse_comment_payload_douyin_and_bilibili(self):
        douyin_packet = {
            "comments": [
                {
                    "cid": "c1",
                    "text": "看好 03 18",
                    "user": {"nickname": "甲"},
                    "digg_count": 3,
                    "create_time": 1700000000,
                    "ip_label": "广东",
                }
            ]
        }
        rows = parse_comment_payload("douyin", douyin_packet)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["昵称"], "甲")
        self.assertEqual(rows[0]["提取号码"], "03 18")

        bili_packet = {
            "code": 0,
            "data": {
                "replies": [
                    {
                        "rpid": 5,
                        "content": {"message": "08 22"},
                        "member": {"uname": "乙"},
                        "like_count": 1,
                        "ctime": 1700000000,
                        "replies": [],
                    }
                ]
            },
        }
        rows = parse_comment_payload("bilibili", bili_packet)
        self.assertEqual(rows[0]["评论内容"], "08 22")
        self.assertEqual(rows[0]["提取号码"], "08 22")

    def test_rows_to_csv(self):
        text_csv = rows_to_csv([
            {
                "cid": "c1",
                "昵称": "甲",
                "点赞数": 3,
                "时间": "2023-11-15 06:13:20",
                "IP属地": "广东",
                "评论内容": "看好 03 18",
                "提取号码": "03 18",
            }
        ])
        self.assertIn("cid,昵称,点赞数", text_csv)
        self.assertIn("看好 03 18", text_csv)

    def test_normalize_video_url(self):
        # 博主主页 + modal_id → 直达视频详情页（用户实际遇到的链接）
        self.assertEqual(
            normalize_video_url(
                "https://www.douyin.com/user/MS4wLjABAAAARMmOkOSwsvBZ7T4RWGChnjBtNyf6zCif71IpIjFESpj91E6SRojdZQWVjAbUjWsA"
                "?from_tab_name=main&modal_id=7701303921234662217&vid=7701307635463696870",
                "douyin",
            ),
            "https://www.douyin.com/video/7701303921234662217",
        )
        self.assertEqual(
            normalize_video_url("https://www.douyin.com/video/7349362406147001610", "douyin"),
            "https://www.douyin.com/video/7349362406147001610",
        )
        self.assertEqual(
            normalize_video_url("https://www.douyin.com/note/7349362406147001610", "douyin"),
            "https://www.douyin.com/note/7349362406147001610",
        )
        self.assertEqual(
            normalize_video_url("https://v.douyin.com/iRNBho6/", "douyin"),
            "https://v.douyin.com/iRNBho6/",
        )
        self.assertEqual(
            normalize_video_url("7701303921234662217", "douyin"),
            "https://www.douyin.com/video/7701303921234662217",
        )
        self.assertEqual(detect_platform("7701303921234662217"), "douyin")
        with self.assertRaises(ValueError):
            normalize_video_url("https://www.douyin.com/user/MS4wLjABAAAA", "douyin")

    CJCP_FIXTURE = (
        '<div class="table-body">'
        '<div class="table-line line-66px">'
        '<div class="qs">快乐8第2021002期开奖结果</div>'
        '<div class="date">2021-01-02(六)</div>'
        '<div class="result"><div class="kl8-num-wrap">'
        + "".join(f'<div class="num-ball red-ball">{n:02d}</div>' for n in
                  [6, 10, 11, 13, 14, 28, 30, 36, 39, 43, 44, 47, 48, 59, 67, 69, 71, 73, 75, 79])
        + '</div></div>'
        '<div class="money color-red">20,847,282</div>'
        '</div>'
        '<div class="table-line line-66px">'
        '<div class="qs">快乐8第2021001期开奖结果</div>'
        '<div class="date">2021-01-01(五)</div>'
        '<div class="result"><div class="kl8-num-wrap">'
        + "".join(f'<div class="num-ball red-ball">{n:02d}</div>' for n in
                  [9, 12, 16, 18, 33, 36, 40, 41, 46, 48, 51, 53, 54, 55, 59, 61, 62, 63, 68, 75])
        + '</div></div>'
        '<div class="money color-red">21,000,001</div>'
        '</div>'
        '</div>'
    )

    def test_parse_cjcp_page(self):
        rows = parse_cjcp_page(self.CJCP_FIXTURE)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["期号"], "2021002")
        self.assertEqual(rows[0]["开奖日期"], "2021-01-02")
        self.assertEqual(rows[0]["星期"], "六")
        self.assertEqual(rows[0]["号码1"], 6)
        self.assertEqual(rows[0]["号码20"], 79)
        self.assertEqual(rows[0]["销售额(元)"], "20847282")
        self.assertEqual(rows[0]["奖池金额(元)"], "")
        self.assertEqual(rows[1]["期号"], "2021001")
        self.assertEqual(rows[1]["星期"], "五")
        with self.assertRaises(ValueError):
            parse_cjcp_page("<html>nothing here</html>")

    def test_cjcp_page_url_and_wide_csv(self):
        self.assertEqual(cjcp_page_url(1), "https://www.cjcp.cn/kl8kaijiang/")
        self.assertEqual(cjcp_page_url(69), "https://www.cjcp.cn/kl8kaijiang/69.html")
        csv_text = rows_to_wide_csv(parse_cjcp_page(self.CJCP_FIXTURE))
        header = csv_text.splitlines()[0]
        self.assertTrue(header.startswith("期号,开奖日期,星期,号码1"))
        self.assertTrue(header.endswith("销售额(元),奖池金额(元)"))
        self.assertIn("2021002,2021-01-02,六,6", csv_text)

    def test_split_targets_and_merge_rows(self):
        self.assertEqual(
            split_targets(" https://v.douyin.com/a/，7701303921234662217\n7701303921234662217 BV1xx411c7XX"),
            ["https://v.douyin.com/a/", "7701303921234662217", "BV1xx411c7XX"],
        )
        self.assertEqual(split_targets("  ,，  "), [])
        row_a = {"cid": "c1", "昵称": "甲", "评论内容": "看好 03"}
        row_b = {"cid": "c1", "昵称": "甲", "评论内容": "看好 03"}
        row_c = {"cid": "", "昵称": "乙", "评论内容": "08 22"}
        row_d = {"cid": "", "昵称": "乙", "评论内容": "08 22"}
        merged = merge_comment_rows([row_a], [row_b, row_c, row_d])
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["cid"], "c1")


if __name__ == "__main__":
    unittest.main()
