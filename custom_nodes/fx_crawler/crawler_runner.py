# -*- coding: utf-8 -*-
"""fx_crawler 运行封装：把设置注入 vendored 的全局 config，跑一次采集，读回结果。

MediaCrawler 用全局 config 单例驱动。这里在运行前 setattr 覆盖需要的键，
跑完从 SAVE_DATA_PATH 读回 json 结果，返回结构化数据。CLI(runner.py) 与
ComfyUI 节点(nodes.py) 都调用 run_crawl_sync。
"""

import asyncio
import glob
import importlib
import json
import os
import sys
import threading
import time
from pathlib import Path

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

# platform -> (module, class)
PLATFORM_CRAWLERS = {
    "xhs": ("fx_crawler_core.media_platform.xhs.core", "XiaoHongShuCrawler"),
    "dy": ("fx_crawler_core.media_platform.douyin.core", "DouYinCrawler"),
    "ks": ("fx_crawler_core.media_platform.kuaishou.core", "KuaishouCrawler"),
    "bili": ("fx_crawler_core.media_platform.bilibili.core", "BilibiliCrawler"),
    "wb": ("fx_crawler_core.media_platform.weibo.core", "WeiboCrawler"),
    "tieba": ("fx_crawler_core.media_platform.tieba.core", "TieBaCrawler"),
    "zhihu": ("fx_crawler_core.media_platform.zhihu.core", "ZhihuCrawler"),
}

# 平台代码 -> MediaCrawler 各 store 硬编码的落盘目录名(短代码 ≠ 目录名)。
# store/*/_store_impl.py 里 AsyncFileWriter(platform=...) 的值,决定
# {SAVE_DATA_PATH}/{目录名}/{ext}/ 的目录名。dy/ks/wb 与短代码不同。
PLATFORM_STORE_DIR = {
    "xhs": "xhs", "dy": "douyin", "ks": "kuaishou", "bili": "bili",
    "wb": "weibo", "tieba": "tieba", "zhihu": "zhihu",
}

# 每平台 detail(指定内容ID) / creator(创作者) 输入对应的 config 列表变量名
PLATFORM_LIST_KEYS = {
    "xhs": {"detail": "XHS_SPECIFIED_NOTE_URL_LIST", "creator": "XHS_CREATOR_ID_LIST"},
    "dy": {"detail": "DY_SPECIFIED_ID_LIST", "creator": "DY_CREATOR_ID_LIST"},
    "ks": {"detail": "KS_SPECIFIED_ID_LIST", "creator": "KS_CREATOR_ID_LIST"},
    "bili": {"detail": "BILI_SPECIFIED_ID_LIST", "creator": "BILI_CREATOR_ID_LIST"},
    "wb": {"detail": "WEIBO_SPECIFIED_ID_LIST", "creator": "WEIBO_CREATOR_ID_LIST"},
    "tieba": {"detail": "TIEBA_SPECIFIED_ID_LIST", "creator": "TIEBA_CREATOR_URL_LIST",
              "search_names": "TIEBA_NAME_LIST"},
    "zhihu": {"detail": "ZHIHU_SPECIFIED_ID_LIST", "creator": "ZHIHU_CREATOR_URL_LIST"},
}


def _apply_settings(config, platform: str, settings: dict) -> None:
    """把 settings 覆盖到全局 config；未提供的键保持默认。"""
    config.PLATFORM = platform
    for key, value in (settings or {}).items():
        setattr(config, key, value)


def _collect_results(save_path: str, platform: str) -> dict:
    """从 SAVE_DATA_PATH 读回 json 输出，按 contents/comments/creators 归类。"""
    store_dir = PLATFORM_STORE_DIR.get(platform, platform)
    base = Path(save_path) / store_dir / "json"
    out = {"contents": [], "comments": [], "creators": []}
    if not base.exists():
        return out
    for fp in sorted(base.glob("*.json")):
        name = fp.name.lower()
        try:
            data = json.loads(fp.read_text(encoding="utf-8") or "[]")
        except Exception:
            continue
        if not isinstance(data, list):
            data = [data]
        if "comments" in name:
            out["comments"].extend(data)
        elif "creator" in name:
            out["creators"].extend(data)
        else:  # contents / notes / videos 等归为内容
            out["contents"].extend(data)
    return out


async def run_crawl(platform: str, settings: dict, save_path: str) -> dict:
    """核心异步入口：应用设置 → 建库(如需) → 跑采集 → 清理 → 读回结果。"""
    if platform not in PLATFORM_CRAWLERS:
        raise ValueError(f"不支持的平台: {platform}. 可选: {list(PLATFORM_CRAWLERS)}")

    import fx_crawler_core.config as config
    from fx_crawler_core.database import db

    settings = dict(settings or {})
    settings.setdefault("SAVE_DATA_OPTION", "json")
    settings["SAVE_DATA_PATH"] = save_path
    _apply_settings(config, platform, settings)

    # 数据库模式先建表
    if config.SAVE_DATA_OPTION in ("sqlite", "mysql", "db", "postgres"):
        await db.init_db(config.SAVE_DATA_OPTION)

    mod_name, cls_name = PLATFORM_CRAWLERS[platform]
    Crawler = getattr(importlib.import_module(mod_name), cls_name)
    crawler = Crawler()

    try:
        await crawler.start()
    finally:
        # 清理浏览器（参考 MediaCrawler main.async_cleanup）
        try:
            cdp_manager = getattr(crawler, "cdp_manager", None)
            if cdp_manager:
                await cdp_manager.cleanup(force=True)
            else:
                ctx = getattr(crawler, "browser_context", None)
                if ctx:
                    await ctx.close()
        except Exception as e:
            msg = str(e).lower()
            if "closed" not in msg and "disconnected" not in msg:
                print(f"[fx_crawler] 浏览器清理告警: {e}")
        if config.SAVE_DATA_OPTION in ("db", "sqlite"):
            try:
                await db.close()
            except Exception:
                pass

    return _collect_results(save_path, platform)


def run_crawl_sync(platform: str, settings: dict, save_path: str) -> dict:
    """同步入口：在独立线程的新事件循环里跑，供 ComfyUI 节点(同步 run)调用。"""
    result = {}

    def _runner():
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            result["value"] = loop.run_until_complete(run_crawl(platform, settings, save_path))
        except BaseException as exc:  # noqa: BLE001
            result["error"] = exc
        finally:
            try:
                loop.close()
            except Exception:
                pass

    t = threading.Thread(target=_runner, daemon=False)
    t.start()
    t.join()
    if "error" in result:
        raise result["error"]
    return result.get("value", {})
