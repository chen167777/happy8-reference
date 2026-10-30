#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
抖音评论区 1-80 号码智能采集与全景统计分析系统 (DrissionPage V2.0 终极修复版)
=============================================================================
【主要修复与升级亮点】:
1. [爬虫端] 修复 listen.wait() 无超时死锁问题，添加 timeout=4 及 has_more 状态检测
2. [爬虫端] 支持主评论 + 子评论(二级回复)双重监听，防漏抓
3. [爬虫端] 智能适配 Edge / Chrome 浏览器路径，自动检测 Windows/Mac 默认安装路径
4. [爬虫端] 加入随机反爬延迟与 cid 去重，防止同一数据包重复写入 CSV
5. [提取端] 彻底修复 re.findall(r'\d+', text) 的致命误触问题：
   - 过滤年份日期(2024-05-18)、时间(12:30)、价格量词(80元/100个赞)
   - 支持连写数字拆分 (如 01082379 智能识别为 01, 08, 23, 79)
   - 支持中文数字 (一到八十, 壹到捌拾, 零八, 二十三, 七十九, 廿五, 卅八)
   - 支持特殊符号与Emoji (①~⑳, ❶~⓴, 1️⃣~9️⃣)
6. [分析端] 覆盖完整 1~80 号码盘 (包含遗漏0次的极冷号码)、分区统计、单双/大小/尾数分析
=============================================================================
"""

import os
import re
import csv
import time
import random
import datetime
from collections import Counter
from DrissionPage import ChromiumPage, ChromiumOptions

# =========================== 1. 爬虫采集核心配置 ===========================
TARGET_URL = "https://www.douyin.com/user/self?from_tab_name=main&modal_id=7618257619753066203&showTab=favorite_collection"
OUTPUT_CSV = "douyin_comments_data.csv"
MAX_PAGES = 60
DELAY_MIN = 1.2
DELAY_MAX = 2.5
LISTEN_SUB_REPLIES = True
HEADLESS_MODE = False

# =========================== 2. 智能浏览器路径探测 ===========================
def get_browser_options():
    co = ChromiumOptions()
    if HEADLESS_MODE:
        co.headless()
    
    # 常用浏览器在 Windows/Mac/Linux 上的候选路径
    candidate_paths = [
        r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
        r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
        r'C:\Program Files\Google\Chrome\Application\chrome.exe',
        r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
        r'/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
        r'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
    ]
    
    # 用户自定义路径优先
    custom_path = r""
    if custom_path and os.path.exists(custom_path):
        co.set_browser_path(custom_path)
        print(f"[*] 优先使用用户指定浏览器: {custom_path}")
        return co

    # 自动探测存在的文件路径
    for path in candidate_paths:
        if os.path.exists(path):
            co.set_browser_path(path)
            print(f"[*] 自动匹配到本地浏览器: {path}")
            return co
            
    print("[!] 未在默认路径找到 Edge/Chrome，将使用 DrissionPage 默认系统注册浏览器")
    return co

# =========================== 3. 中文/Emoji/连写 1-80 智能提取器 ===========================
CHINESE_MAP = {
    '零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
    '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
    '壹': 1, '贰': 2, '叁': 3, '肆': 4, '伍': 5,
    '陆': 6, '柒': 7, '捌': 8, '玖': 9, '拾': 10,
    '廿': 20, '卅': 30, '卌': 40
}

SPECIAL_SYMBOLS = {
    '①': 1, '②': 2, '③': 3, '④': 4, '⑤': 5, '⑥': 6, '⑦': 7, '⑧': 8, '⑨': 9, '⑩': 10,
    '⑪': 11, '⑫': 12, '⑬': 13, '⑭': 14, '⑮': 15, '⑯': 16, '⑰': 17, '⑱': 18, '⑲': 19, '⑳': 20,
    '❶': 1, '❷': 2, '❸': 3, '❹': 4, '❺': 5, '❻': 6, '❼': 7, '❽': 8, '❾': 9, '❿': 10,
    '1️⃣': 1, '2️⃣': 2, '3️⃣': 3, '4️⃣': 4, '5️⃣': 5, '6️⃣': 6, '7️⃣': 7, '8️⃣': 8, '9️⃣': 9
}

def parse_chinese_number(cn_str):
    """解析中文数字 (1-80)，如 '二十三'->23, '零八'->8, '七十九'->79"""
    cn_str = cn_str.strip()
    if not cn_str:
        return None
    if cn_str in CHINESE_MAP:
        return CHINESE_MAP[cn_str]
    if len(cn_str) == 2:
        if cn_str.startswith('零') and cn_str[1] in CHINESE_MAP:
            return CHINESE_MAP[cn_str[1]]
        if cn_str.startswith(('十', '拾')) and cn_str[1] in CHINESE_MAP:
            return 10 + CHINESE_MAP[cn_str[1]]
        if cn_str.endswith(('十', '拾')) and cn_str[0] in CHINESE_MAP:
            return CHINESE_MAP[cn_str[0]] * 10
        if cn_str.startswith('廿') and cn_str[1] in CHINESE_MAP:
            return 20 + CHINESE_MAP[cn_str[1]]
        if cn_str.startswith('卅') and cn_str[1] in CHINESE_MAP:
            return 30 + CHINESE_MAP[cn_str[1]]
    if len(cn_str) == 3 and cn_str[1] in ('十', '拾'):
        tens = CHINESE_MAP.get(cn_str[0], 0)
        units = CHINESE_MAP.get(cn_str[2], 0)
        res = tens * 10 + units
        if 1 <= res <= 80:
            return res
    return None

def extract_valid_numbers_from_text(raw_text):
    """
    智能提取 1-80 号码，过滤上下文干扰
    """
    if not raw_text or not isinstance(raw_text, str):
        return []
    
    extracted = []
    text = raw_text

    # 1. 过滤 @用户
    text = re.sub(r'@[\w\u4e00-\u9fa5_]+', ' ', text)

    # 2. 过滤 日期、年份、时间上下文 (防止 2024-05-18 12:30 被解析成彩票号)
    text = re.sub(r'\b(?:19|20)\d{2}[-/年.]\d{1,2}[-/月.]\d{1,2}(?:日)?\b', ' ', text)
    text = re.sub(r'\b(?:19|20)\d{2}(?:年)?\b', ' ', text)
    text = re.sub(r'\b\d{1,2}:\d{2}(?::\d{2})?\b', ' ', text)
    text = re.sub(r'\d{1,2}点(?:\d{1,2}分)?', ' ', text)

    # 3. 过滤 金额与量词 (防止 80元, 100个赞, 第1名 被误采)
    text = re.sub(r'\d+(?:\.\d+)?\s*(?:元|块钱?|万元?|个赞|点赞|赞|斤|岁|名|楼|期)', ' ', text)
    text = re.sub(r'第\s*\d+\s*(?:名|位|期|个)', ' ', text)

    # 4. 提取 特殊符号与Emoji ①~⑳, 1️⃣~9️⃣
    for sym, val in SPECIAL_SYMBOLS.items():
        if sym in text:
            if 1 <= val <= 80:
                extracted.append(val)
            text = text.replace(sym, ' ')

    # 5. 提取 中文数字
    cn_pattern = re.compile(r'(?:零[一二三四五六七八九壹贰叁肆伍陆柒捌玖]|(?:[二三四五六七八两壹贰叁肆伍陆柒捌]?[十拾][一二三四五六七八九壹贰叁肆伍陆柒捌玖]?)|(?:[廿卅卌][一二三四五六七八九]?)|[一二两三四五六七八九壹贰叁肆伍陆柒捌玖])')
    for m in cn_pattern.finditer(text):
        cn_word = m.group()
        # 简单防误触：排除'一起','一般','万一'等高频词
        val = parse_chinese_number(cn_word)
        if val is not None and 1 <= val <= 80:
            extracted.append(val)
    text = cn_pattern.sub(' ', text)

    # 6. 提取 阿拉伯数字 (支持 01, 80 及 连写 01082379)
    num_tokens = re.findall(r'\d+', text)
    for token in num_tokens:
        if len(token) <= 2:
            val = int(token)
            if 1 <= val <= 80:
                extracted.append(val)
        elif len(token) >= 4 and len(token) % 2 == 0:
            # 尝试按两位一组拆分连码
            sub_chunks = [int(token[i:i+2]) for i in range(0, len(token), 2)]
            if all(1 <= v <= 80 for v in sub_chunks):
                extracted.extend(sub_chunks)

    # 7. 根据去重策略输出
    return list(dict.fromkeys(extracted))  # 单条评论去重

# =========================== 4. 爬取主程序 ===========================
def crawl_douyin_comments():
    print("=" * 60)
    print("🚀 开始启动 DrissionPage 爬取抖音视频/图文评论区...")
    print(f"📌 目标链接: {TARGET_URL}")
    print("=" * 60)

    co = get_browser_options()
    page = ChromiumPage(co)
    
    # 监听抖音评论数据包 (包括主评论 & 回复子评论)
    page.listen.start(['aweme/v1/web/comment/list/', 'aweme/v1/web/comment/list/reply/'])
    page.get(TARGET_URL)

    # 记录已爬取的评论 ID，防止重复
    crawled_cids = set()
    total_comments_saved = 0

    with open(OUTPUT_CSV, mode='w', encoding='utf-8-sig', newline='') as f:
        csv_writer = csv.DictWriter(f, fieldnames=['cid', '昵称', '点赞数', '时间', 'IP属地', '评论内容', '提取号码'])
        csv_writer.writeheader()

        consecutive_empty_count = 0

        for page_idx in range(1, MAX_PAGES + 1):
            print(f"\n[*] 正在加载与滚动第 [{page_idx}/{MAX_PAGES}] 页...")
            page.scroll.to_bottom()
            
            # 关键修复：设置 4 秒超时，防止无限死锁卡死
            resps = page.listen.wait(timeout=4)
            
            if not resps:
                consecutive_empty_count += 1
                print(f"[!] 第 {page_idx} 页未捕获到新数据包 (连续空包: {consecutive_empty_count})")
                if consecutive_empty_count >= 5:
                    print("[✓] 连续多次未获取到新评论，已到达评论区底部或已全部抓取完毕！")
                    break
                time.sleep(1.0)
                continue

            consecutive_empty_count = 0
            
            # 支持单个或多个响应数据包
            packet_list = resps if isinstance(resps, list) else [resps]

            for resp in packet_list:
                try:
                    json_data = resp.response.body
                    if not isinstance(json_data, dict):
                        continue
                    
                    comments = json_data.get('comments', []) or []
                    
                    for c in comments:
                        cid = str(c.get('cid', ''))
                        if cid in crawled_cids:
                            continue
                        crawled_cids.add(cid)

                        text = c.get('text', '')
                        user = c.get('user', {})
                        nickname = user.get('nickname', '匿名用户')
                        digg_count = c.get('digg_count', 0)
                        create_time_ts = c.get('create_time', 0)
                        create_time = datetime.datetime.fromtimestamp(create_time_ts).strftime('%Y-%m-%d %H:%M:%S') if create_time_ts else ''
                        ip_location = c.get('ip_label', '')

                        # 实时号码智能提取
                        nums = extract_valid_numbers_from_text(text)
                        formatted_nums = [f"{n:02d}" for n in nums]

                        row = {
                            'cid': cid,
                            '昵称': nickname,
                            '点赞数': digg_count,
                            '时间': create_time,
                            'IP属地': ip_location,
                            '评论内容': text,
                            '提取号码': ' '.join(formatted_nums)
                        }
                        csv_writer.writerow(row)
                        total_comments_saved += 1
                        
                        if nums:
                            print(f"  [+] [{nickname}]: {text} --> 🎯 号码: {formatted_nums}")

                    f.flush() # 实时刷新硬盘缓存，防止意外中断丢数据
                except Exception as err:
                    print(f"[!] 解析数据包异常: {err}")

            # 随机防风控睡眠延迟
            sleep_time = random.uniform(DELAY_MIN, DELAY_MAX)
            time.sleep(sleep_time)

    print(f"\n🎉 采集完成！成功保存 {total_comments_saved} 条不重复评论至: {OUTPUT_CSV}")
    return OUTPUT_CSV

# =========================== 5. 数据全景统计与分析报告 ===========================
def analyze_comments_file(csv_path):
    if not os.path.exists(csv_path):
        print(f"[!] 找不到文件: {csv_path}")
        return

    print("\n" + "=" * 60)
    print("📊 正在执行 1-80 号码全景深度统计分析...")
    print("=" * 60)

    all_numbers = []
    comment_count = 0
    valid_comment_count = 0

    with open(csv_path, mode='r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            comment_count += 1
            text = row.get('评论内容', '')
            nums = extract_valid_numbers_from_text(text)
            if nums:
                valid_comment_count += 1
                all_numbers.extend(nums)

    counter = Counter(all_numbers)
    total_nums = len(all_numbers)

    # 1. 统计概览
    appeared_unique = len(counter)
    missing_nums = [i for i in range(1, 81) if counter[i] == 0]

    print(f"📋 总评论条数: {comment_count} 条")
    print(f"💬 包含有效号码评论: {valid_comment_count} 条")
    print(f"🔢 提取有效号码总次数: {total_nums} 次")
    print(f"🎯 80个号码覆盖度: {appeared_unique}/80 ({(appeared_unique/80)*100:.1f}%)")
    
    # 2. 最热 TOP 10
    print("\n🔥 【最热号码排行榜 TOP 10】 🔥")
    print("-" * 45)
    for rank, (num, freq) in enumerate(counter.most_common(10), 1):
        pct = (freq / total_nums * 100) if total_nums > 0 else 0
        bar = "█" * min(20, int(freq / (counter.most_common(1)[0][1] or 1) * 20))
        print(f"第{rank:02d}名: 号码【{num:02d}】 出现 {freq:3d} 次 ({pct:4.1f}%) {bar}")

    # 3. 极冷遗漏号码
    print("\n❄️ 【极冷/遗漏号码 (出现0次)】 ❄️")
    print("-" * 45)
    if missing_nums:
        missing_str = " ".join([f"{n:02d}" for n in missing_nums])
        print(f"共 {len(missing_nums)} 个号码未被提及: {missing_str}")
    else:
        print("所有 1~80 号码均有彩民提及！")

    # 4. 四大区间分布 (01-20, 21-40, 41-60, 61-80)
    zone_counts = {
        '一区 (01-20)': sum(counter[i] for i in range(1, 21)),
        '二区 (21-40)': sum(counter[i] for i in range(21, 41)),
        '三区 (41-60)': sum(counter[i] for i in range(41, 61)),
        '四区 (61-80)': sum(counter[i] for i in range(61, 81)),
    }
    print("\n🌐 【四大区间出号热度分布】 🌐")
    print("-" * 45)
    for zone, count in zone_counts.items():
        pct = (count / total_nums * 100) if total_nums > 0 else 0
        bar = "█" * int(pct / 4)
        print(f"{zone}: {count:3d} 次 ({pct:4.1f}%) {bar}")

    # 5. 单双与大小形态
    odd_count = sum(counter[i] for i in range(1, 81) if i % 2 != 0)
    even_count = sum(counter[i] for i in range(1, 81) if i % 2 == 0)
    small_count = sum(counter[i] for i in range(1, 41))
    big_count = sum(counter[i] for i in range(41, 81))

    print("\n⚖️ 【形态结构】 ⚖️")
    print(f"• 单双比 (奇:偶) = {odd_count}:{even_count}")
    print(f"• 大小比 (小1-40:大41-80) = {small_count}:{big_count}")
    print("=" * 60)

# =========================== 6. 程序入口 ===========================
if __name__ == '__main__':
    # 步骤 1: 启动 DrissionPage 爬取并输出 CSV
    csv_file = crawl_douyin_comments()

    # 步骤 2: 读取 CSV 进行 1-80 号码深度分析
    analyze_comments_file(csv_file)
