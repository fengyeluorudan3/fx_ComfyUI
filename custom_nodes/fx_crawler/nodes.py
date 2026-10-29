# -*- coding: utf-8 -*-
"""fx_crawler ComfyUI 节点：每平台一个采集节点（关键词搜索 / 指定链接 / 指定用户）。

移植自 MediaCrawler。I/O 面向用户重设计：
- 采集模式选场景，配套的输入桩点由前端 web/fx_crawler.js 动态显隐；
- 布尔项用开关；技术项收进"高级选项"；补上"下载视频图片""每条评论上限"；
- 各平台专属项（小红书排序 / 微博搜索类型 / 贴吧限定吧名）按平台声明。
节点把 UI 输入映射为 vendored 全局 config，调用 crawler_runner 跑一次采集。
"""

import json
import os
import sys

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from crawler_runner import (  # noqa: E402
    run_crawl_sync,
    PLATFORM_LIST_KEYS,
    PLATFORM_STORE_DIR,
)

# ---- 中文选项 → MediaCrawler 内部值 的映射 ----
_MODE = {"关键词搜索": "search", "指定链接": "detail", "指定用户": "creator"}
_LOGIN = {"扫码": "qrcode", "cookie": "cookie"}
_BROWSER_CDP = "连我的Chrome"  # 否则独立浏览器
_XHS_SORT = {"综合": "general", "最热": "popularity_descending", "最新": "time_descending"}
_WB_TYPE = {"综合": "default", "实时": "real_time", "热门": "popular", "视频": "video"}


def _default_output_dir():
    """默认输出到 ComfyUI 的 output/fx_crawler。找不到 ComfyUI 根就退回插件 _output。"""
    comfy_root = os.path.dirname(os.path.dirname(_PLUGIN_DIR))
    out = os.path.join(comfy_root, "output")
    if os.path.isdir(out):
        return os.path.join(out, "fx_crawler")
    return os.path.join(_PLUGIN_DIR, "_output")


def _split_lines(value: str):
    """多行/逗号文本 → 去空列表。"""
    items = []
    for line in str(value or "").replace(",", "\n").replace("，", "\n").splitlines():
        s = line.strip()
        if s:
            items.append(s)
    return items


def _preview(contents, comments, creators):
    """人类可读摘要：条数 + Top3 标题/互动。"""
    lines = [f"内容 {len(contents)} 条 · 评论 {len(comments)} 条 · 创作者 {len(creators)} 个"]
    for it in contents[:3]:
        title = it.get("title") or it.get("desc") or it.get("content_text") or "(无标题)"
        title = str(title).replace("\n", " ")[:40]
        like = (it.get("liked_count") or it.get("voteup_count")
                or it.get("total_replay_num") or it.get("video_play_count") or "")
        lines.append(f"· {title}" + (f"  [{like}]" if like != "" else ""))
    return "\n".join(lines)


class _BaseCrawlerNode:
    PLATFORM = ""
    EXTRA = {}  # 平台专属 widget，由子类覆盖

    @classmethod
    def INPUT_TYPES(cls):
        required = {
            # —— 场景（前端据此显隐下面的输入桩点）——
            "采集模式": (["关键词搜索", "指定链接", "指定用户"], {"default": "关键词搜索"}),
            "关键词": ("STRING", {"default": "", "multiline": False,
                                "tooltip": "关键词搜索模式：多个词用逗号分隔"}),
            "链接或ID": ("STRING", {"default": "", "multiline": True,
                                 "tooltip": "指定链接模式：内容 URL 或 ID，一行一个"}),
            "用户主页": ("STRING", {"default": "", "multiline": True,
                                "tooltip": "指定用户模式：创作者主页 URL 或 ID，一行一个"}),
            # —— 常用 ——
            "内容条数上限": ("INT", {"default": 20, "min": 1, "max": 100000,
                                "tooltip": "采集多少条内容(不含评论)。搜索模式为每个关键词的上限，且按整页向上取整(最少约一页)"}),
            "每条评论上限": ("INT", {"default": 10, "min": 0, "max": 100000,
                                "tooltip": "每条内容最多采多少评论(0=尽量多)"}),
            "采集评论": ("BOOLEAN", {"default": True, "label_on": "开", "label_off": "关"}),
            "采集二级评论": ("BOOLEAN", {"default": False, "label_on": "开", "label_off": "关"}),
            "下载视频图片": ("BOOLEAN", {"default": False, "label_on": "下载", "label_off": "不下",
                                   "tooltip": "把图片/视频文件存到本地(小红书/抖音/B站支持视频)"}),
            "高级选项": ("BOOLEAN", {"default": False, "label_on": "展开", "label_off": "收起",
                                 "tooltip": "展开登录/浏览器/保存格式等高级项"}),
        }
        # 平台专属项(仅本平台节点声明；前端仅在搜索模式显示)
        required.update(cls.EXTRA)
        optional = {
            # —— 高级(前端在"高级选项=收起"时隐藏)——
            "登录方式": (["扫码", "cookie"], {"default": "扫码"}),
            "浏览器模式": (["连我的Chrome", "独立浏览器"], {"default": "连我的Chrome",
                                                "tooltip": "连我的Chrome=CDP复用登录态(推荐)"}),
            "无头运行": ("BOOLEAN", {"default": False, "label_on": "无头", "label_off": "有界面"}),
            "保存格式": (["json", "jsonl", "csv", "sqlite"], {"default": "json"}),
            "cookie": ("STRING", {"default": "", "multiline": True,
                                  "tooltip": "登录方式=cookie 时填"}),
            "输出目录": ("STRING", {"default": "", "multiline": False,
                                 "tooltip": "留空=ComfyUI 的 output/fx_crawler"}),
        }
        return {"required": required, "optional": optional}

    RETURN_TYPES = ("STRING", "STRING", "STRING", "INT", "STRING", "STRING")
    RETURN_NAMES = ("contents_json", "comments_json", "creators_json", "条数", "目录", "预览")
    FUNCTION = "run"
    CATEGORY = "FX Crawler"
    OUTPUT_NODE = True

    def run(self, **kw):
        platform = self.PLATFORM
        mode = _MODE.get(kw.get("采集模式", "关键词搜索"), "search")
        save_option = kw.get("保存格式", "json")
        save_dir = str(kw.get("输出目录", "") or "").strip() or _default_output_dir()

        settings = {
            "CRAWLER_TYPE": mode,
            "KEYWORDS": str(kw.get("关键词", "") or ""),
            "CRAWLER_MAX_NOTES_COUNT": int(kw.get("内容条数上限", 20)),
            "CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES": int(kw.get("每条评论上限", 10)),
            "ENABLE_GET_COMMENTS": bool(kw.get("采集评论", True)),
            "ENABLE_GET_SUB_COMMENTS": bool(kw.get("采集二级评论", False)),
            "ENABLE_GET_MEIDAS": bool(kw.get("下载视频图片", False)),
            "LOGIN_TYPE": _LOGIN.get(kw.get("登录方式", "扫码"), "qrcode"),
            "ENABLE_CDP_MODE": kw.get("浏览器模式", _BROWSER_CDP) == _BROWSER_CDP,
            "HEADLESS": bool(kw.get("无头运行", False)),
            "CDP_HEADLESS": bool(kw.get("无头运行", False)),
            "SAVE_DATA_OPTION": save_option,
        }
        cookie_str = str(kw.get("cookie", "") or "").strip()
        if settings["LOGIN_TYPE"] == "cookie" and cookie_str:
            settings["COOKIES"] = cookie_str

        # detail/creator 的输入 → 平台对应 config 列表
        keys = PLATFORM_LIST_KEYS.get(platform, {})
        if mode == "detail" and keys.get("detail"):
            settings[keys["detail"]] = _split_lines(kw.get("链接或ID", ""))
        elif mode == "creator" and keys.get("creator"):
            settings[keys["creator"]] = _split_lines(kw.get("用户主页", ""))

        # 平台专属项
        if platform == "xhs" and kw.get("排序方式"):
            settings["SORT_TYPE"] = _XHS_SORT.get(kw["排序方式"], "general")
        if platform == "wb" and kw.get("搜索类型"):
            settings["WEIBO_SEARCH_TYPE"] = _WB_TYPE.get(kw["搜索类型"], "default")
        if platform == "tieba" and str(kw.get("限定吧名", "") or "").strip():
            settings["TIEBA_NAME_LIST"] = _split_lines(kw.get("限定吧名", ""))

        data = run_crawl_sync(platform, settings, save_dir)
        contents = data.get("contents", [])
        comments = data.get("comments", [])
        creators = data.get("creators", [])
        data_dir = os.path.join(save_dir, PLATFORM_STORE_DIR.get(platform, platform), save_option)
        return (
            json.dumps(contents, ensure_ascii=False),
            json.dumps(comments, ensure_ascii=False),
            json.dumps(creators, ensure_ascii=False),
            len(contents),
            data_dir,
            _preview(contents, comments, creators),
        )


class FXCrawlerXhs(_BaseCrawlerNode):
    PLATFORM = "xhs"
    EXTRA = {"排序方式": (["综合", "最热", "最新"], {"default": "综合"})}


class FXCrawlerDouyin(_BaseCrawlerNode):
    PLATFORM = "dy"


class FXCrawlerKuaishou(_BaseCrawlerNode):
    PLATFORM = "ks"


class FXCrawlerBilibili(_BaseCrawlerNode):
    PLATFORM = "bili"


class FXCrawlerWeibo(_BaseCrawlerNode):
    PLATFORM = "wb"
    EXTRA = {"搜索类型": (["综合", "实时", "热门", "视频"], {"default": "综合"})}


class FXCrawlerTieba(_BaseCrawlerNode):
    PLATFORM = "tieba"
    EXTRA = {"限定吧名": ("STRING", {"default": "", "multiline": False,
                                 "tooltip": "只在这些吧里搜(逗号分隔)，留空=全站"})}


class FXCrawlerZhihu(_BaseCrawlerNode):
    PLATFORM = "zhihu"


NODE_CLASS_MAPPINGS = {
    "fx_crawl_xhs": FXCrawlerXhs,
    "fx_crawl_douyin": FXCrawlerDouyin,
    "fx_crawl_kuaishou": FXCrawlerKuaishou,
    "fx_crawl_bilibili": FXCrawlerBilibili,
    "fx_crawl_weibo": FXCrawlerWeibo,
    "fx_crawl_tieba": FXCrawlerTieba,
    "fx_crawl_zhihu": FXCrawlerZhihu,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "fx_crawl_xhs": "爬取小红书",
    "fx_crawl_douyin": "爬取抖音",
    "fx_crawl_kuaishou": "爬取快手",
    "fx_crawl_bilibili": "爬取B站",
    "fx_crawl_weibo": "爬取微博",
    "fx_crawl_tieba": "爬取贴吧",
    "fx_crawl_zhihu": "爬取知乎",
}
