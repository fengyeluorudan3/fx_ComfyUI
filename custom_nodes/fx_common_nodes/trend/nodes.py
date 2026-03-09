# coding=utf-8
"""
Trend 榜单节点 - ComfyUI 节点

- FXTrendFetchHotlist: 拉取多平台热榜原始数据
- FXTrendAnalyzeHotlist: 按关键词统计热榜并计算权重排序
"""

import json
import logging
from typing import Tuple, List, Union

from .fetcher import DataFetcher
from .frequency import load_frequency_words, parse_keywords_simple
from .analyzer import count_word_frequency

logger = logging.getLogger(__name__)


def _parse_platform_ids(platform_ids: str) -> List[Union[str, Tuple[str, str]]]:
    """
    解析平台 ID 字符串为列表。
    支持: "toutiao,baidu,weibo" 或 "toutiao:头条,baidu:百度"
    """
    if not (platform_ids or "").strip():
        return []
    out = []
    for part in platform_ids.strip().split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            id_val, name = part.split(":", 1)
            out.append((id_val.strip(), name.strip()))
        else:
            out.append(part)
    return out


class FXTrendFetchHotlist:
    """拉取多平台热榜数据（NewsNow API），输出 JSON 供后续节点使用。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "platform_ids": ("STRING", {
                    "multiline": False,
                    # 默认对齐 NewsNow 首页所有 type=hottest 的信息渠道
                    "default": (
                        "zhihu,weibo,coolapk,wallstreetcn-hot,36kr-renqi,douyin,hupu,tieba,"
                        "toutiao,thepaper,cls-hot,xueqiu,xueqiu-hotstock,hackernews,producthunt,"
                        "github,github-trending-today,bilibili,bilibili-hot-search,bilibili-hot-video,"
                        "bilibili-ranking,kuaishou,baidu,nowcoder,sspai,juejin,ifeng,chongbuluo-hot,"
                        "douban,steam,tencent,tencent-hot,freebuf,qqvideo,qqvideo-tv-hotsearch,"
                        "iqiyi,iqiyi-hot-ranklist"
                    ),
                    "placeholder": "逗号分隔平台 ID，如 github,zhihu,steam",
                }),
            },
            "optional": {
                "proxy_url": ("STRING", {"default": "", "placeholder": "http://127.0.0.1:7890"}),
                "api_url": ("STRING", {"default": "", "placeholder": "留空用默认 NewsNow API"}),
                "request_interval_ms": ("INT", {"default": 100, "min": 50, "max": 2000, "step": 50}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("results_json", "id_to_name_json", "failed_ids_json")
    FUNCTION = "fetch_hotlist"
    CATEGORY = "fx_common/trend"

    def fetch_hotlist(
        self,
        platform_ids: str,
        proxy_url: str = "",
        api_url: str = "",
        request_interval_ms: int = 100,
    ) -> Tuple[str, str, str]:
        try:
            ids_list = _parse_platform_ids(platform_ids)
            if not ids_list:
                logger.warning("[FXTrendFetchHotlist] platform_ids 为空")
                return "{}", "{}", "[]"

            fetcher = DataFetcher(
                proxy_url=proxy_url.strip() or None,
                api_url=api_url.strip() or None,
            )
            results, id_to_name, failed_ids = fetcher.crawl_websites(
                ids_list,
                request_interval=request_interval_ms,
            )

            # 简要日志，方便在控制台观察节点输出规模
            total_titles = sum(len(titles) for titles in results.values())
            logger.info(
                "[FXTrendFetchHotlist] fetched platforms=%s total_titles=%s failed=%s api=%s",
                list(id_to_name.keys()),
                total_titles,
                failed_ids,
                getattr(fetcher, "api_url", None),
            )

            return (
                json.dumps(results, ensure_ascii=False),
                json.dumps(id_to_name, ensure_ascii=False),
                json.dumps(failed_ids, ensure_ascii=False),
            )
        except Exception as e:
            logger.exception("[FXTrendFetchHotlist] %s", e)
            return "{}", "{}", "[]"


class FXTrendAnalyzeHotlist:
    """对热榜数据按关键词分组统计并按权重排序（当日汇总模式）。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "results_json": ("STRING", {
                    "multiline": True,
                    "default": "{}",
                    "placeholder": "来自 FXTrendFetchHotlist 的 results_json",
                }),
                "id_to_name_json": ("STRING", {
                    "multiline": False,
                    "default": "{}",
                    "placeholder": "来自 FXTrendFetchHotlist 的 id_to_name_json",
                }),
            },
            "optional": {
                "keywords": ("STRING", {
                    "multiline": False,
                    "default": "",
                    "placeholder": "逗号分隔关键词，如 AI,大模型；留空表示全部",
                }),
                "frequency_file": ("STRING", {
                    "multiline": False,
                    "default": "",
                    "placeholder": "频率词配置文件路径，留空则用 keywords",
                }),
                "rank_threshold": ("INT", {"default": 3, "min": 1, "max": 50, "step": 1}),
                "max_news_per_keyword": ("INT", {"default": 0, "min": 0, "max": 100, "step": 1}),
                "sort_by_position_first": ("BOOLEAN", {"default": False}),
            },
        }

    RETURN_TYPES = ("STRING", "INT")
    RETURN_NAMES = ("stats_json", "total_titles")
    FUNCTION = "analyze_hotlist"
    CATEGORY = "fx_common/trend"

    def analyze_hotlist(
        self,
        results_json: str,
        id_to_name_json: str,
        keywords: str = "",
        frequency_file: str = "",
        rank_threshold: int = 3,
        max_news_per_keyword: int = 0,
        sort_by_position_first: bool = False,
    ) -> Tuple[str, int]:
        try:
            results = json.loads(results_json or "{}")
            id_to_name = json.loads(id_to_name_json or "{}")

            if not results:
                logger.info("[FXTrendAnalyzeHotlist] 空结果，跳过分析")
                return "[]", 0

            if (frequency_file or "").strip():
                word_groups, filter_words, global_filters = load_frequency_words(frequency_file.strip())
            else:
                word_groups = parse_keywords_simple(keywords)
                filter_words = []
                global_filters = []

            stats, total_titles = count_word_frequency(
                results=results,
                word_groups=word_groups,
                filter_words=filter_words,
                id_to_name=id_to_name,
                rank_threshold=rank_threshold,
                global_filters=global_filters or None,
                max_news_per_keyword=max_news_per_keyword,
                sort_by_position_first=sort_by_position_first,
                quiet=True,
            )

            logger.info(
                "[FXTrendAnalyzeHotlist] analyzed total_titles=%s groups=%s keywords=%s freq_file=%s",
                total_titles,
                len(stats),
                (keywords or "").strip(),
                (frequency_file or "").strip() or None,
            )

            # 序列化时 titles 里每条保留必要字段，避免过大
            def _serialize_stat(s):
                out = dict(s)
                titles = []
                for t in out.get("titles", []):
                    titles.append({
                        "title": t.get("title"),
                        "source_name": t.get("source_name"),
                        "ranks": t.get("ranks"),
                        "url": t.get("url"),
                        "count": t.get("count"),
                    })
                out["titles"] = titles
                return out

            stats_serializable = [_serialize_stat(s) for s in stats]
            return json.dumps(stats_serializable, ensure_ascii=False), total_titles

        except json.JSONDecodeError as e:
            logger.warning("[FXTrendAnalyzeHotlist] JSON 解析失败: %s", e)
            return "[]", 0
        except Exception as e:
            logger.exception("[FXTrendAnalyzeHotlist] %s", e)
            return "[]", 0
