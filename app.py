from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from cjcp_scraper import rows_to_wide_csv, scrape_cjcp_history
from comment_crawler import (
    crawl_comments,
    detect_platform,
    merge_comment_rows,
    rows_to_csv,
    split_targets,
)
from happy8_core import (
    DEFAULT_DB,
    Draw,
    OFFICIAL_RULES_URL,
    analyze_history,
    extract_opinion_counts,
    extract_valid_numbers_from_text,
    fetch_bilibili_comments,
    fetch_official_draws,
    format_probability,
    generate_reference_sets,
    load_draws,
    parse_draw_csv,
    parse_draw_excel,
    save_draws,
    top_hit_probability,
)

st.set_page_config(page_title="快乐8 · 本地数据与参考选号", page_icon="🎱", layout="wide")

st.markdown(
    """
    <style>
      .block-container {max-width: 1280px; padding-top: 1.5rem; padding-bottom: 3rem;}
      [data-testid="stMetric"] {background: rgba(22,131,74,.055); padding: .8rem 1rem; border-radius: 14px;}
      .small-note {font-size: .88rem; opacity: .82;}
      div[data-testid="stAlert"] {border-radius: 12px;}
    </style>
    """,
    unsafe_allow_html=True,
)

DB_PATH = Path(DEFAULT_DB)


def parse_uploaded_table(uploaded_file):
    raw = uploaded_file.getvalue()
    suffix = Path(uploaded_file.name).suffix.lower()
    if suffix == ".xlsx":
        return parse_draw_excel(raw)
    # Excel's “Unicode Text” export is commonly UTF-16LE with a BOM.
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
        return parse_draw_csv(text)
    for encoding in ("utf-8-sig", "gb18030", "utf-8"):
        try:
            text = raw.decode(encoding)
            return parse_draw_csv(text)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别文件编码，请另存为 UTF-8/GB18030 CSV、UTF-16 文本或 XLSX。")


def issue_table(draws) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "期号": draw.issue,
            "开奖日期": draw.date,
            "开奖号码": " ".join(f"{number:02d}" for number in draw.numbers),
            "来源": draw.source,
        }
        for draw in draws
    ])


with st.sidebar:
    st.header("数据与设置")
    st.caption("开奖数据默认保存在本机 SQLite 文件，不上传到本应用服务器。")

    st.subheader("官方开奖结果")
    sync_limit = st.selectbox(
        "单次同步期数",
        [30, 100, 300, 1000, 3000, 5000],
        index=1,
        format_func=lambda value: f"最近 {value} 期" + ("（分页补齐历史）" if value > 1000 else ""),
        help="超过 1000 期时会自动分页读取公开接口（每次请求间隔片刻），用于补齐更久远的历史开奖。",
    )
    if st.button("从中国福彩网同步", type="primary", width="stretch"):
        try:
            with st.spinner("正在请求中国福彩网公开开奖接口……"):
                fetched = fetch_official_draws(sync_limit)
                saved = save_draws(fetched, DB_PATH)
            st.success(f"读取 {len(fetched)} 期，已写入/更新 {saved} 条。")
        except Exception as exc:
            st.error(str(exc))
            st.caption("若官网接口暂不可用，可使用下方 CSV/TSV/XLSX 导入；程序不会绕过登录或访问限制。")

    uploaded = st.file_uploader(
        "导入开奖表格（CSV / TSV-TXT / XLSX）",
        type=["csv", "tsv", "txt", "xlsx"],
        help="支持 issue/date/numbers；或期号、开奖日期、号码1–号码20。逗号、制表符均可；星期、销售额、奖池列会忽略。",
    )
    if uploaded and st.button("校验并导入表格", width="stretch"):
        try:
            parsed = parse_uploaded_table(uploaded)
            save_draws(parsed, DB_PATH)
            st.success(f"已导入 {len(parsed)} 期开奖数据。")
        except Exception as exc:
            st.error(str(exc))

    st.divider()
    st.subheader("彩经网历史开奖（分页抓取）")
    st.caption(
        "从彩经网快乐8开奖列表按页抓取（每页 30 期）：页码 1 为最新一页，页码越大越早；"
        "页与页之间有随机间隔。结果自动存入本地库，并可下载与导入格式一致的宽表 CSV。"
    )
    cj_col1, cj_col2 = st.columns(2)
    with cj_col1:
        cj_start = st.number_input("起始页码", min_value=1, max_value=500, value=1, step=1, key="cj_start")
    with cj_col2:
        cj_pages = st.number_input("抓取页数", min_value=1, max_value=200, value=50, step=1, key="cj_pages")
    if st.button("按顺序抓取并保存", width="stretch", key="cj_scrape_button"):
        try:
            with st.status("正在抓取彩经网……", expanded=True) as status:
                cj_rows = scrape_cjcp_history(
                    int(cj_start),
                    int(cj_pages),
                    on_progress=lambda message: status.write(message),
                )
            cj_draws = [
                Draw(
                    issue=row["期号"],
                    date=row["开奖日期"],
                    numbers=tuple(int(row[f"号码{i}"]) for i in range(1, 21)),
                    source="cjcp",
                )
                for row in cj_rows
            ]
            saved = save_draws(cj_draws, DB_PATH)
            st.session_state["cjcp_csv"] = rows_to_wide_csv(cj_rows)
            st.success(
                f"抓取 {len(cj_rows)} 期（第 {int(cj_start)}–{int(cj_start) + int(cj_pages) - 1} 页），"
                f"已写入/更新 {saved} 条。"
            )
        except Exception as exc:
            st.error(str(exc))
    if st.session_state.get("cjcp_csv"):
        st.download_button(
            "下载宽表 CSV（期号/日期/星期/号码/销售额）",
            data=st.session_state["cjcp_csv"].encode("utf-8-sig"),
            file_name="happy8_cjcp.csv",
            mime="text/csv",
        )

    st.divider()
    st.subheader("网友评论热度（可选）")
    st.caption(
        "自动获取支持抖音与B站：优先使用 B站公开接口；抖音或接口失败时自动改用「本机浏览器监听」"
        "（参考《我的中奖梦.py》的 DrissionPage 方案）——正常打开页面、被动读取浏览器已收到的评论数据包，"
        "不破解签名、不绕过验证。浏览器需保持对应平台的登录状态。原文不写入数据库。"
    )
    video_targets = st.text_area(
        "视频链接或 ID（支持抖音 / B站，多个用换行或逗号分隔）",
        value="",
        height=88,
        placeholder="每行一个，例如：\nhttps://v.douyin.com/xxxx/\n7701303921234662217\nhttps://www.bilibili.com/video/BV1xx411c7XX",
        key="video_targets",
    )
    merge_existing = st.checkbox(
        "与本会话已有评论合并（取消勾选则替换旧数据）",
        value=True,
        key="merge_existing_comments",
    )
    fetch_col1, fetch_col2 = st.columns(2)
    with fetch_col1:
        comment_limit = st.selectbox("B站接口条数上限", [50, 100, 200, 500], index=1, key="comment_limit")
    with fetch_col2:
        crawl_pages = st.selectbox("浏览器滚动页数", [5, 10, 20, 40, 60], index=1, key="crawl_pages")
    if st.button("自动获取评论数据", type="primary", width="stretch", key="fetch_comments_button"):
        targets = split_targets(video_targets)
        if not targets:
            st.error("请先粘贴至少一个视频链接或 ID。")
        fetched_rows: list = []
        success_sources = 0
        for target_index, target in enumerate(targets):
            try:
                platform = detect_platform(target)
            except Exception as exc:
                st.warning(f"跳过“{target[:24]}”：{exc}")
                continue
            rows: list = []
            if platform == "bilibili":
                try:
                    with st.spinner(f"正在尝试 B站公开接口（{target[:24]}）……"):
                        fetched = fetch_bilibili_comments(target, max_comments=comment_limit)
                    rows = [
                        {
                            "cid": f"b{target_index:02d}{index:05d}",
                            "昵称": "",
                            "点赞数": "",
                            "时间": "",
                            "IP属地": "",
                            "评论内容": text,
                            "提取号码": " ".join(f"{n:02d}" for n in extract_valid_numbers_from_text(text)),
                        }
                        for index, text in enumerate(fetched.comments)
                    ]
                    success_sources += 1
                except Exception as exc:
                    st.warning(f"B站公开接口失败（{exc}），改用本机浏览器采集。")
            if not rows:
                try:
                    with st.status(f"正在用本机浏览器采集（{target[:24]}）……", expanded=True) as status:
                        rows = crawl_comments(
                            target,
                            max_pages=crawl_pages,
                            on_progress=lambda message: status.write(message),
                        )
                    success_sources += 1
                except Exception as exc:
                    st.warning(f"“{target[:24]}”采集失败：{exc}")
                    st.caption(
                        "请确认已安装 DrissionPage（pip install DrissionPage），"
                        "并使用已登录对应平台的 Edge/Chrome；采集期间请勿操作弹出的浏览器窗口。"
                    )
            fetched_rows.extend(rows)
        if fetched_rows:
            previous_rows = st.session_state.get("comment_rows", []) or []
            merged_rows = (
                merge_comment_rows(previous_rows, fetched_rows)
                if merge_existing
                else fetched_rows
            )
            texts = [row.get("评论内容", "") for row in merged_rows if row.get("评论内容")]
            counts, parsed_lines = extract_opinion_counts("\n".join(texts))
            st.session_state["comment_rows"] = merged_rows
            st.session_state["opinion_counts"] = counts
            st.session_state["opinion_lines"] = parsed_lines
            st.session_state["opinion_preview"] = texts[:20]
            st.session_state["opinion_source"] = (
                f"{success_sources} 个视频累计 {len(merged_rows)} 条"
                if merge_existing
                else f"{success_sources} 个视频共 {len(merged_rows)} 条"
            )
            st.success(
                f"本次采集 {len(fetched_rows)} 条（{success_sources}/{len(targets)} 个目标成功），"
                f"当前合计 {len(merged_rows)} 条，其中 {parsed_lines} 条含号码。"
                + ("（已与已有评论合并，重复条目自动去重）" if merge_existing else "（已替换旧数据）")
            )
        elif targets:
            st.error("没有采集到任何评论。")

    source_label = st.session_state.get("opinion_source")
    if source_label:
        preview = st.session_state.get("opinion_preview", [])
        with st.expander(f"当前评论来源：{source_label}（预览 {len(preview)} 条）"):
            for line in preview:
                st.markdown(f"- {line}")
        comment_rows = st.session_state.get("comment_rows") or []
        if comment_rows:
            st.download_button(
                "导出采集评论 CSV",
                data=rows_to_csv(comment_rows).encode("utf-8-sig"),
                file_name="happy8_comments.csv",
                mime="text/csv",
            )

    st.markdown("##### 手动粘贴（任意平台，每行一条）")
    comment_text = st.text_area(
        "每行一条评论或一组号码",
        height=140,
        placeholder="例如：我看好 03 18 27 41；今天想选 08、22、56",
        key="comment_text",
    )
    parse_col, clear_col = st.columns(2)
    with parse_col:
        if st.button("解析号码热度", width="stretch"):
            try:
                counts, parsed_lines = extract_opinion_counts(comment_text)
                st.session_state["opinion_counts"] = counts
                st.session_state["opinion_lines"] = parsed_lines
                st.session_state["opinion_source"] = "手动粘贴"
                st.session_state["opinion_preview"] = [
                    line for line in comment_text.splitlines() if line.strip()
                ][:20]
                st.success(f"从 {parsed_lines} 条含号码的文本中提取完成。")
            except Exception as exc:
                st.error(str(exc))
    with clear_col:
        if st.button("清除讨论数据", width="stretch"):
            for key in ("opinion_counts", "opinion_lines", "opinion_source", "opinion_preview", "comment_rows"):
                st.session_state.pop(key, None)
            st.info("本会话讨论热度已清除；文本框中的文字仍留在当前页面，清空文本请手动删除。")

    st.divider()
    st.subheader("参考组合设置")
    draws_all = load_draws(DB_PATH)
    available = len(draws_all)
    max_window = max(1, available)
    window_options = [value for value in (20, 50, 100, 300, 1000) if value <= max_window]
    if available and max_window not in window_options:
        window_options.append(max_window)
    if not window_options:
        window_options = [1]
    history_window = st.selectbox(
        "统计最近期开奖范围",
        window_options,
        index=min(len(window_options) - 1, max(0, window_options.index(100) if 100 in window_options else len(window_options) - 1)),
        format_func=lambda value: f"最近 {value} 期" if available else "暂无开奖数据（均匀随机）",
    )
    history_strength = st.slider(
        "历史频率倾斜（实验）", 0.0, 0.5, 0.0, 0.05,
        help="默认 0 即均匀随机；非零时只改变本工具生成组合的抽样权重，不改变开奖号码真实概率。",
    )
    opinion_strength = st.slider(
        "讨论热度倾斜（实验）", 0.0, 0.5, 0.0, 0.05,
        help="只参考当前会话中自动获取或手动粘贴文本的号码提及次数，不代表网友意见能预测开奖。",
    )
    seed_text = st.text_input("随机种子（可选；留空每次不同）", value="", max_chars=40)

# Reload after possible sidebar imports.
draws_all = load_draws(DB_PATH)
draws_window = draws_all[:history_window] if draws_all else []
opinion_counts = st.session_state.get("opinion_counts", {})

st.title("🎱 快乐8｜本地开奖数据与参考选号")
st.caption("一款用于整理官方开奖、查看历史分布和生成随机参考组合的 Python 小工具。不是预测软件，也不提供中奖保证。")
st.warning(
    "彩票开奖具有随机性。历史冷热号、遗漏值和网友讨论都不能可靠预测下一期开奖；"
    "请将组合视为娱乐参考，量力而行，不借贷、不追损。"
)

latest = draws_all[0] if draws_all else None
metric_cols = st.columns(4)
metric_cols[0].metric("已保存期数", len(draws_all))
metric_cols[1].metric("最近期号", latest.issue if latest else "暂无")
metric_cols[2].metric("最近开奖日期", latest.date if latest and latest.date else "暂无")
metric_cols[3].metric("本会话已解析评论行", st.session_state.get("opinion_lines", 0))

reference_tab, history_tab, odds_tab = st.tabs(["参考组合", "开奖与分布", "玩法概率与说明"])

with reference_tab:
    st.subheader("选一到选十 · 随机参考组合")
    st.write("点击生成后，为每种玩法各给一组号码。默认等概率抽样；可选倾斜只是可复现的实验权重，不是预测模型。")
    if st.button("生成 10 组参考组合", type="primary", key="generate_button"):
        try:
            seed = seed_text.strip() or None
            refs = generate_reference_sets(
                draws_window,
                opinion_counts,
                history_strength=history_strength,
                opinion_strength=opinion_strength,
                seed=seed,
            )
            st.session_state["references"] = refs
            st.session_state["reference_config"] = {
                "draws": len(draws_window),
                "history_strength": history_strength,
                "opinion_strength": opinion_strength,
                "seed": seed,
            }
        except Exception as exc:
            st.error(f"生成失败：{exc}")

    refs = st.session_state.get("references")
    if refs:
        rows = []
        for pick_count in range(1, 11):
            numbers = refs[pick_count]
            rows.append({
                "玩法": f"选{pick_count}",
                "号码": " ".join(f"{number:02d}" for number in numbers),
                "全中概率（理论）": format_probability(top_hit_probability(pick_count)),
            })
        ref_df = pd.DataFrame(rows)
        st.dataframe(ref_df, hide_index=True, width="stretch")
        config = st.session_state.get("reference_config", {})
        st.caption(
            f"本次设置：使用 {config.get('draws', 0)} 期开奖；历史倾斜 {config.get('history_strength', 0):.2f}；"
            f"讨论倾斜 {config.get('opinion_strength', 0):.2f}。每种玩法单独生成，跨玩法号码可能重复。"
        )
        csv_bytes = ref_df.to_csv(index=False).encode("utf-8-sig")
        st.download_button("下载参考组合 CSV", data=csv_bytes, file_name="happy8_reference.csv", mime="text/csv")
    else:
        st.info("尚未生成组合。即使没有历史数据，也可以使用默认均匀随机方式生成参考号码。")

    st.markdown("#### 组合如何生成")
    st.write(
        "每组从 1–80 中不放回抽取对应数量。默认权重完全相同；若启用历史/讨论倾斜，程序只用标准化后的出现次数或提及次数调整抽样权重。"
        "这种做法便于做数据实验，但不意味着号码更可能开出。"
    )

with history_tab:
    st.subheader("官方开奖数据")
    if draws_all:
        st.dataframe(issue_table(draws_all[:50]), hide_index=True, width="stretch")
        history_csv = issue_table(draws_all).to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "导出全部开奖数据 CSV",
            data=history_csv,
            file_name="happy8_history.csv",
            mime="text/csv",
        )
        stats_rows = analyze_history(draws_window, opinion_counts)
        stats_df = pd.DataFrame(stats_rows)
        stats_df = stats_df.rename(columns={
            "number": "号码", "count": "近窗出现次数", "rate": "每期出现率",
            "expected_count": "随机期望次数", "gap": "当前遗漏期数", "mentions": "讨论提及数",
        })
        stats_df["号码"] = stats_df["号码"].map(lambda value: f"{value:02d}")
        st.markdown("#### 号码分布（描述性统计，不是预测）")
        chart_df = pd.DataFrame(stats_rows).set_index("number")[["count"]].rename(columns={"count": "出现次数"})
        st.bar_chart(chart_df, height=240)
        st.dataframe(stats_df, hide_index=True, width="stretch", height=420)
        st.caption(f"当前统计窗口：最近 {len(draws_window)} 期；单个号码理论上每期出现概率为 20/80 = 25%。")
    else:
        st.info("尚无本地开奖记录。可在左侧点击同步，或导入官方渠道的 CSV/XLSX 表格。")

with odds_tab:
    st.subheader("快乐8全中概率（理论）")
    st.write("快乐8从 1–80 中开出 20 个号码。固定选择 n 个号码时，‘全中’概率为 C(20,n) / C(80,n)。此处只算组合概率，不含奖金、派奖或其他奖级；派奖规则请以官方公告为准。")
    odds_df = pd.DataFrame([
        {"玩法": f"选{n}", "选择数": n, "全中概率": top_hit_probability(n), "约为": format_probability(top_hit_probability(n))}
        for n in range(1, 11)
    ])
    odds_df["全中概率"] = odds_df["全中概率"].map(lambda value: f"{value:.8%}")
    st.dataframe(odds_df, hide_index=True, width="stretch")
    st.link_button("打开中国福彩网快乐8规则页", OFFICIAL_RULES_URL)
    st.markdown("#### 数据与隐私边界")
    st.markdown(
        "- 开奖数据：通过中国福彩网公开页面/接口读取；接口不可用时可手动导入 CSV/XLSX。\n"
        "- 网友评论：B站公开评论仅以普通 HTTPS 读取（不登录、不使用 Cookie、不绕过验证）；抖音/快手等平台仍只分析用户主动粘贴的文本。\n"
        "- 本机存储：开奖期号、日期和号码保存到项目目录下的 SQLite；评论原文不会落库。\n"
        "- 预测能力：程序不知道开奖机构的意图，也不能辨认所谓‘主任思路’；历史数据不能保证提高未来中奖率。"
    )

st.divider()
st.caption("本软件仅用于学习与数据整理，不代购彩票、不自动下单、不承诺中奖。请以中国福彩网公布的游戏规则和开奖结果为准。")
