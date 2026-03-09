# coding=utf-8
"""
榜单统计与权重分析

- calculate_news_weight: 按排名与出现次数计算热度权重
- count_word_frequency: 按关键词分组统计热榜词频并排序
来源：TrendRadar 核心能力。
"""

from typing import Dict, List, Tuple, Optional

from .frequency import matches_word_groups, _word_matches


def calculate_news_weight(
    title_data: Dict,
    rank_threshold: int,
    weight_config: Dict,
) -> float:
    """
    计算单条新闻权重，用于排序。

    Args:
        title_data: 含 ranks、count
        rank_threshold: 高排名阈值
        weight_config: RANK_WEIGHT, FREQUENCY_WEIGHT, HOTNESS_WEIGHT

    Returns:
        权重值
    """
    ranks = title_data.get("ranks", [])
    if not ranks:
        return 0.0
    count = title_data.get("count", len(ranks))

    rank_scores = [11 - min(r, 10) for r in ranks]
    rank_weight = sum(rank_scores) / len(ranks)

    frequency_weight = min(count, 10) * 10
    high_rank_count = sum(1 for r in ranks if r <= rank_threshold)
    hotness_weight = (high_rank_count / len(ranks)) * 100 if ranks else 0

    return (
        rank_weight * weight_config["RANK_WEIGHT"]
        + frequency_weight * weight_config["FREQUENCY_WEIGHT"]
        + hotness_weight * weight_config["HOTNESS_WEIGHT"]
    )


def count_word_frequency(
    results: Dict,
    word_groups: List[Dict],
    filter_words: List,
    id_to_name: Dict,
    rank_threshold: int = 3,
    global_filters: Optional[List[str]] = None,
    weight_config: Optional[Dict] = None,
    max_news_per_keyword: int = 0,
    sort_by_position_first: bool = False,
    quiet: bool = True,
) -> Tuple[List[Dict], int]:
    """
    按词组统计热榜词频（当日汇总模式）。

    Args:
        results: {source_id: {title: {ranks, url, mobileUrl}}}
        word_groups: 词组列表（来自 frequency.load_frequency_words 或 parse_keywords_simple）
        filter_words: 过滤词列表
        id_to_name: 平台 ID -> 名称
        rank_threshold: 排名阈值
        global_filters: 全局过滤词
        weight_config: 权重配置
        max_news_per_keyword: 每组最多展示条数，0 不限制
        sort_by_position_first: 是否先按配置位置再按条数排序
        quiet: 是否静默

    Returns:
        (stats, total_titles)
        stats: [{"word", "count", "position", "titles": [...], "percentage"}, ...]
    """
    if weight_config is None:
        weight_config = {
            "RANK_WEIGHT": 0.4,
            "FREQUENCY_WEIGHT": 0.3,
            "HOTNESS_WEIGHT": 0.3,
        }

    if not word_groups:
        word_groups = [{"required": [], "normal": [], "group_key": "全部新闻"}]
        filter_words = []

    word_stats = {g["group_key"]: {"count": 0, "titles": {}} for g in word_groups}
    total_titles = 0
    processed_titles: Dict[str, Dict[str, bool]] = {}

    for source_id, titles_data in results.items():
        total_titles += len(titles_data)
        if source_id not in processed_titles:
            processed_titles[source_id] = {}

        for title, title_data in titles_data.items():
            if processed_titles.get(source_id, {}).get(title):
                continue
            if not matches_word_groups(title, word_groups, filter_words, global_filters):
                continue

            source_ranks = title_data.get("ranks", []) or [99]
            source_url = title_data.get("url", "")
            source_mobile_url = title_data.get("mobileUrl", "")
            title_lower = title.lower() if isinstance(title, str) else str(title).lower()

            for group in word_groups:
                required = group.get("required") or []
                normal = group.get("normal") or []
                group_key = group["group_key"]

                if len(word_groups) == 1 and group_key == "全部新闻":
                    pass
                else:
                    if required and not all(_word_matches(r, title_lower) for r in required):
                        continue
                    if normal and not any(_word_matches(n, title_lower) for n in normal):
                        continue

                if source_id not in word_stats[group_key]["titles"]:
                    word_stats[group_key]["titles"][source_id] = []

                word_stats[group_key]["count"] += 1
                word_stats[group_key]["titles"][source_id].append({
                    "title": title,
                    "source_name": id_to_name.get(source_id, source_id),
                    "first_time": "",
                    "last_time": "",
                    "time_display": "",
                    "count": 1,
                    "ranks": source_ranks,
                    "rank_threshold": rank_threshold,
                    "url": source_url,
                    "mobileUrl": source_mobile_url,
                    "is_new": False,
                    "rank_timeline": [],
                })
                if source_id not in processed_titles:
                    processed_titles[source_id] = {}
                processed_titles[source_id][title] = True
                break

    group_key_to_position = {g["group_key"]: i for i, g in enumerate(word_groups)}
    group_key_to_max_count = {g["group_key"]: g.get("max_count", 0) for g in word_groups}
    group_key_to_display_name = {g["group_key"]: g.get("display_name") for g in word_groups}

    stats = []
    for group_key, data in word_stats.items():
        all_titles = []
        for source_id, title_list in data["titles"].items():
            all_titles.extend(title_list)

        sorted_titles = sorted(
            all_titles,
            key=lambda x: (
                -calculate_news_weight(x, rank_threshold, weight_config),
                min(x["ranks"]) if x["ranks"] else 999,
                -x["count"],
            ),
        )
        group_max = group_key_to_max_count.get(group_key, 0) or max_news_per_keyword
        if group_max > 0:
            sorted_titles = sorted_titles[:group_max]

        display_word = group_key_to_display_name.get(group_key) or group_key
        stats.append({
            "word": display_word,
            "count": data["count"],
            "position": group_key_to_position.get(group_key, 999),
            "titles": sorted_titles,
            "percentage": round(data["count"] / total_titles * 100, 2) if total_titles > 0 else 0,
        })

    if sort_by_position_first:
        stats.sort(key=lambda x: (x["position"], -x["count"]))
    else:
        stats.sort(key=lambda x: (-x["count"], x["position"]))

    return stats, total_titles
