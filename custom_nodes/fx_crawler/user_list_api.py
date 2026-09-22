# -*- coding: utf-8 -*-
"""用户主页作品列表：纯 HTTP + cookie，不启动 Playwright。

抖音：/aweme/v1/web/aweme/post/ + douyin.js a_bogus 签名
小红书：/api/sns/web/v1/user_posted + xhshow 签名（需 URL 带 xsec_token）
"""

from __future__ import annotations

import asyncio
import logging
import os
import urllib.parse
from typing import Dict, List, Optional, Tuple

import httpx

from async_util import run_sync

logger = logging.getLogger("fx_crawler.video")

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

Pair = Tuple[str, str]  # (download_url, title)
Item = Tuple[str, str, str]  # (download_url, title, kind) kind: video | image


def get_cookie_dict(cookiefile=None, cookiesfrombrowser=None) -> Dict[str, str]:
    """从 Netscape cookies 文件或本机/插件 Chrome 配置读取 cookie 字典。"""
    try:
        import yt_dlp
        from yt_dlp.cookies import load_cookies
    except ImportError:
        return _load_netscape_cookie_dict(cookiefile)

    class _Log:
        def debug(self, msg):
            logger.debug(msg)

        def info(self, msg):
            logger.debug(msg)

        def warning(self, msg):
            logger.warning("[yt-dlp-cookies] %s", msg)

        def error(self, msg):
            logger.error("[yt-dlp-cookies] %s", msg)

    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "logger": _Log()}) as ydl:
            jar = load_cookies(cookiefile, cookiesfrombrowser, ydl)
        return {c.name: c.value for c in jar}
    except Exception as e:
        logger.warning("[fx_video] yt-dlp 读 cookie 失败: %s", e)
        return _load_netscape_cookie_dict(cookiefile)


def _load_netscape_cookie_dict(cookiefile: Optional[str]) -> Dict[str, str]:
    if not cookiefile or not os.path.isfile(cookiefile):
        return {}
    out: Dict[str, str] = {}
    with open(cookiefile, "r", encoding="utf-8") as f:
        for line in f:
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                out[parts[5]] = parts[6].strip()
    return out


def cookie_header(cookie_dict: Dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookie_dict.items() if k and v)


def _douyin_item_kind(item: dict) -> str:
    if item.get("images"):
        return "image"
    return "video"


def _match_content_mode(kind: str, content_mode: str) -> bool:
    mode = str(content_mode or "video").strip().lower()
    if mode in ("all", "全部"):
        return True
    if mode in ("image", "images", "仅图文", "图文"):
        return kind == "image"
    return kind == "video"


def list_douyin_user_urls(
    user_urls: List[str],
    max_count: int,
    cookiefile=None,
    cookiesfrombrowser=None,
    proxy: str = "",
    normalize_url=None,
    content_mode: str = "video",
) -> List[Item]:
    """抖音用户主页 → [(url, title, kind), ...]"""
    from fx_crawler_core.media_platform.douyin.help import (
        get_a_bogus_from_js,
        get_web_id,
        parse_creator_info_from_url,
    )

    cookie_dict = get_cookie_dict(cookiefile, cookiesfrombrowser)
    if not cookie_dict:
        logger.warning("[fx_video] 抖音 API 拉列表：没有可用 cookie")
        return []

    headers = {
        "User-Agent": _UA,
        "Cookie": cookie_header(cookie_dict),
        "Referer": "https://www.douyin.com/",
        "Origin": "https://www.douyin.com",
        "Accept": "application/json, text/plain, */*",
    }
    pairs: List[Item] = []
    seen = set()
    norm = normalize_url or (lambda _p, u: u)

    with httpx.Client(proxy=proxy or None, timeout=30.0, follow_redirects=True) as client:
        for raw in user_urls:
            try:
                page = norm("dy", raw)
                creator = parse_creator_info_from_url(page)
                sec_user_id = creator.sec_user_id
            except Exception as e:
                logger.warning("[fx_video] 抖音解析用户主页失败 %s: %s", raw, e)
                continue

            max_cursor = ""
            has_more = 1
            while has_more == 1 and len(pairs) < max_count:
                params = {
                    "sec_user_id": sec_user_id,
                    "count": str(min(18, max_count - len(pairs))),
                    "max_cursor": max_cursor,
                    "locate_query": "false",
                    "publish_video_strategy_type": "2",
                    "device_platform": "webapp",
                    "aid": "6383",
                    "channel": "channel_pc_web",
                    "version_code": "190600",
                    "version_name": "19.6.0",
                    "update_version_code": "170400",
                    "pc_client_type": "1",
                    "cookie_enabled": "true",
                    "browser_language": "zh-CN",
                    "browser_platform": "MacIntel",
                    "browser_name": "Chrome",
                    "browser_version": "131.0.0.0",
                    "browser_online": "true",
                    "engine_name": "Blink",
                    "os_name": "Mac OS",
                    "os_version": "10.15.7",
                    "cpu_core_num": "8",
                    "device_memory": "8",
                    "engine_version": "109.0",
                    "platform": "PC",
                    "screen_width": "2560",
                    "screen_height": "1440",
                    "effective_type": "4g",
                    "round_trip_time": "50",
                    "webid": get_web_id(),
                    "msToken": cookie_dict.get("msToken") or cookie_dict.get("xmst") or "",
                }
                uri = "/aweme/v1/web/aweme/post/"
                query_string = urllib.parse.urlencode(params)
                params["a_bogus"] = get_a_bogus_from_js(uri, query_string, _UA)
                url = f"https://www.douyin.com{uri}?" + urllib.parse.urlencode(params)
                try:
                    resp = client.get(url, headers=headers)
                    data = resp.json()
                except Exception as e:
                    logger.warning("[fx_video] 抖音 API 请求失败: %s", e)
                    break

                if not data or data.get("status_code") not in (0, None):
                    msg = data.get("status_msg") or data
                    logger.warning("[fx_video] 抖音 API 返回异常: %s", msg)
                    break

                has_more = data.get("has_more", 0)
                max_cursor = str(data.get("max_cursor") or "")
                aweme_list = data.get("aweme_list") or []
                for item in aweme_list:
                    kind = _douyin_item_kind(item)
                    if not _match_content_mode(kind, content_mode):
                        continue
                    aweme_id = str(item.get("aweme_id") or "").strip()
                    if not aweme_id:
                        continue
                    vurl = f"https://www.douyin.com/video/{aweme_id}"
                    if vurl in seen:
                        continue
                    seen.add(vurl)
                    title = str(item.get("desc") or item.get("preview_title") or "")
                    pairs.append((vurl, title, kind))
                    if len(pairs) >= max_count:
                        break

    logger.info("[fx_video] 抖音 API 拉列表 %d 条", len(pairs))
    return pairs


async def _list_xhs_user_urls_async(
    user_urls: List[str],
    max_count: int,
    cookiefile=None,
    cookiesfrombrowser=None,
    proxy: str = "",
    content_mode: str = "video",
) -> List[Item]:
    from fx_crawler_core.media_platform.xhs.client import XiaoHongShuClient
    from fx_crawler_core.media_platform.xhs.help import parse_creator_info_from_url

    cookie_dict = get_cookie_dict(cookiefile, cookiesfrombrowser)
    if not cookie_dict:
        logger.warning("[fx_video] 小红书 API 拉列表：没有可用 cookie")
        return []

    headers = {
        "User-Agent": _UA,
        "Cookie": cookie_header(cookie_dict),
        "Origin": "https://www.xiaohongshu.com",
        "Referer": "https://www.xiaohongshu.com/",
    }
    client = XiaoHongShuClient(
        proxy=proxy or None,
        headers=headers,
        playwright_page=None,
        cookie_dict=cookie_dict,
    )

    pairs: List[Item] = []
    seen = set()
    for raw in user_urls:
        try:
            creator = parse_creator_info_from_url(raw)
        except Exception as e:
            logger.warning("[fx_video] 小红书解析用户主页失败 %s: %s", raw, e)
            continue
        if not creator.xsec_token:
            logger.warning(
                "[fx_video] 小红书用户主页 URL 缺少 xsec_token，"
                "请从 App/网页复制完整分享链接（带 ?xsec_token=...）"
            )
            continue

        cursor = ""
        has_more = True
        xsec_source = creator.xsec_source or "pc_feed"
        while has_more and len(pairs) < max_count:
            try:
                res = await client.get_notes_by_creator(
                    creator.user_id,
                    cursor,
                    page_size=min(30, max_count - len(pairs)),
                    xsec_token=creator.xsec_token,
                    xsec_source=xsec_source,
                )
            except Exception as e:
                logger.warning("[fx_video] 小红书 API 请求失败: %s", e)
                break
            if not res:
                break
            has_more = bool(res.get("has_more"))
            cursor = str(res.get("cursor") or "")
            for note in res.get("notes") or []:
                ntype = str(note.get("type") or note.get("note_card", {}).get("type") or "").lower()
                kind = "video" if ntype == "video" else "image"
                if not _match_content_mode(kind, content_mode):
                    continue
                note_id = str(
                    note.get("note_id") or note.get("id")
                    or (note.get("note_card") or {}).get("note_id") or ""
                ).strip()
                if not note_id:
                    continue
                xsec = str(note.get("xsec_token") or creator.xsec_token or "")
                nurl = (
                    f"https://www.xiaohongshu.com/explore/{note_id}"
                    f"?xsec_token={urllib.parse.quote(xsec)}&xsec_source={xsec_source}"
                )
                if nurl in seen:
                    continue
                seen.add(nurl)
                title = str(
                    note.get("display_title") or note.get("title")
                    or (note.get("note_card") or {}).get("display_title") or ""
                )
                pairs.append((nurl, title, kind))
                if len(pairs) >= max_count:
                    break

    logger.info("[fx_video] 小红书 API 拉列表 %d 条", len(pairs))
    return pairs


def list_xhs_user_urls(
    user_urls: List[str],
    max_count: int,
    cookiefile=None,
    cookiesfrombrowser=None,
    proxy: str = "",
    content_mode: str = "video",
) -> List[Item]:
    return run_sync(
        _list_xhs_user_urls_async(
            user_urls, max_count, cookiefile, cookiesfrombrowser, proxy, content_mode,
        )
    )
