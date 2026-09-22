# -*- coding: utf-8 -*-
"""抖音视频：HTTP API + cookie 直链下载，不依赖 yt-dlp。"""

from __future__ import annotations

import logging
import os
import re
import urllib.parse
from typing import Dict, Optional, Tuple

import httpx

from user_list_api import cookie_header, get_cookie_dict

logger = logging.getLogger("fx_crawler.video")

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def is_douyin_video_url(url: str) -> bool:
    low = str(url or "").lower()
    if "douyin.com" in low or "iesdouyin.com" in low:
        return True
    return bool(re.fullmatch(r"\d{17,20}", str(url or "").strip()))


def _resolve_aweme_id(url: str, proxy: str = "") -> str:
    from fx_crawler_core.media_platform.douyin.help import parse_video_info_from_url

    info = parse_video_info_from_url(url)
    if info.aweme_id:
        return info.aweme_id
    if info.url_type != "short":
        raise RuntimeError(f"无法从链接解析抖音视频 ID：{url}")

    with httpx.Client(proxy=proxy or None, timeout=15.0, follow_redirects=False) as client:
        resp = client.get(url, headers={"User-Agent": _UA})
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location") or ""
            if loc:
                again = parse_video_info_from_url(loc)
                if again.aweme_id:
                    return again.aweme_id
        raise RuntimeError(f"抖音短链解析失败：{url}")


def _douyin_common_params(cookie_dict: Dict[str, str]) -> Dict[str, str]:
    from fx_crawler_core.media_platform.douyin.help import get_web_id

    return {
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


def fetch_aweme_detail(
    aweme_id: str,
    cookiefile=None,
    cookiesfrombrowser=None,
    proxy: str = "",
) -> dict:
    from fx_crawler_core.media_platform.douyin.help import get_a_bogus_from_js

    cookie_dict = get_cookie_dict(cookiefile, cookiesfrombrowser)
    if not cookie_dict:
        raise RuntimeError(
            "抖音下载需要登录 cookie。\n"
            "请在节点里选「cookie文本」或「fx_crawler浏览器」（先用采集节点登录抖音）。"
        )

    headers = {
        "User-Agent": _UA,
        "Cookie": cookie_header(cookie_dict),
        "Referer": f"https://www.douyin.com/video/{aweme_id}",
        "Accept": "application/json, text/plain, */*",
    }
    params = {"aweme_id": aweme_id}
    params.update(_douyin_common_params(cookie_dict))

    uri = "/aweme/v1/web/aweme/detail/"
    query_string = urllib.parse.urlencode(params)
    params["a_bogus"] = get_a_bogus_from_js(uri, query_string, _UA)
    url = f"https://www.douyin.com{uri}?" + urllib.parse.urlencode(params)

    with httpx.Client(proxy=proxy or None, timeout=30.0, follow_redirects=True) as client:
        resp = client.get(url, headers=headers)
        try:
            data = resp.json()
        except Exception as e:
            raise RuntimeError(f"抖音详情 API 返回非 JSON（HTTP {resp.status_code}）") from e

    if not data or data.get("status_code") not in (0, None):
        msg = data.get("status_msg") or data
        raise RuntimeError(f"抖音详情 API 失败：{msg}")

    detail = data.get("aweme_detail") or {}
    if not detail:
        raise RuntimeError(
            "抖音详情为空（cookie 可能过期或未登录）。\n"
            "请重新登录抖音后再试，或改用「Playwright(备用)」拉列表。"
        )
    return detail


def _extract_video_download_url(aweme_detail: dict) -> str:
    from fx_crawler_core.store.douyin import _extract_video_download_url as _store_extract

    return _store_extract(aweme_detail)


def _slim_info(detail: dict, webpage_url: str) -> dict:
    video = detail.get("video") or {}
    author = detail.get("author") or {}
    stats = detail.get("statistics") or {}
    return {
        k: v for k, v in {
            "id": str(detail.get("aweme_id") or ""),
            "title": str(detail.get("desc") or detail.get("preview_title") or ""),
            "uploader": str(author.get("nickname") or ""),
            "uploader_id": str(author.get("unique_id") or author.get("sec_uid") or ""),
            "duration": video.get("duration"),
            "width": video.get("width"),
            "height": video.get("height"),
            "ext": "mp4",
            "webpage_url": webpage_url,
            "thumbnail": str((video.get("cover") or {}).get("url_list", [""])[-1] or ""),
            "description": str(detail.get("desc") or ""),
            "view_count": stats.get("play_count"),
            "like_count": stats.get("digg_count"),
        }.items() if v is not None and v != ""
    }


def _download_bytes(url: str, dest: str, proxy: str = "", pbar=None) -> str:
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".part"
    headers = {
        "User-Agent": _UA,
        "Referer": "https://www.douyin.com/",
    }
    with httpx.Client(proxy=proxy or None, timeout=120.0, follow_redirects=True) as client:
        with client.stream("GET", url, headers=headers) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            last_pct = 0
            with open(tmp, "wb") as out:
                for chunk in resp.iter_bytes(256 * 1024):
                    if not chunk:
                        continue
                    out.write(chunk)
                    done += len(chunk)
                    if pbar is not None and total > 0:
                        pct = min(100, int(done * 100 / total))
                        if pct > last_pct:
                            pbar.update(pct - last_pct)
                            last_pct = pct
    os.replace(tmp, dest)
    return dest


def probe_douyin(
    url: str,
    cookiefile=None,
    cookiesfrombrowser=None,
    proxy: str = "",
) -> dict:
    aweme_id = _resolve_aweme_id(url, proxy=proxy)
    detail = fetch_aweme_detail(
        aweme_id, cookiefile=cookiefile, cookiesfrombrowser=cookiesfrombrowser, proxy=proxy,
    )
    page = f"https://www.douyin.com/video/{aweme_id}"
    return _slim_info(detail, page)


def _extract_note_image_urls(aweme_detail: dict) -> list[str]:
    from fx_crawler_core.store.douyin import _extract_note_image_list

    return [u for u in _extract_note_image_list(aweme_detail) if isinstance(u, str) and u.strip()]


def download_douyin_images(
    url: str,
    out_dir: str = "",
    reuse: bool = True,
    cookiefile=None,
    cookiesfrombrowser=None,
    proxy: str = "",
    pbar=None,
) -> Tuple[list[str], dict]:
    """下载抖音图文作品的全部图片，返回 (paths, info)。"""
    import hashlib

    aweme_id = _resolve_aweme_id(url, proxy=proxy)
    page = f"https://www.douyin.com/video/{aweme_id}"
    work = out_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "_image")
    os.makedirs(work, exist_ok=True)
    key_base = "fximg_" + hashlib.sha1(aweme_id.encode("utf-8")).hexdigest()[:16]

    detail = fetch_aweme_detail(
        aweme_id, cookiefile=cookiefile, cookiesfrombrowser=cookiesfrombrowser, proxy=proxy,
    )
    image_urls = _extract_note_image_urls(detail)
    if not image_urls:
        raise RuntimeError("抖音图文未返回可下载图片（可能已下架或不是图文作品）。")

    paths: list[str] = []
    state = {"last": 0}
    total = len(image_urls)

    for idx, img_url in enumerate(image_urls):
        ext = ".jpg"
        low = img_url.lower()
        for cand in (".webp", ".png", ".jpeg", ".jpg"):
            if cand in low:
                ext = cand if cand != ".jpeg" else ".jpg"
                break
        out_path = os.path.join(work, f"{key_base}_{idx}{ext}")
        pattern = os.path.join(work, key_base + f"_{idx}.*")

        if reuse:
            hits = [f for f in __import__("glob").glob(pattern)
                    if not f.endswith((".part", ".ytdl", ".temp"))]
            if hits:
                paths.append(max(hits, key=os.path.getmtime))
                continue

        logger.info("[fx_video] 抖音图文 %s 第 %d/%d 张", aweme_id, idx + 1, total)
        item_pbar = None
        if pbar is not None:
            base_pct = int(idx * 100 / max(total, 1))

            class _Slice:
                def update(self, delta):
                    pct = min(100, base_pct + int(int(delta or 0) / max(total, 1)))
                    if pct > state["last"]:
                        pbar.update(pct - state["last"])
                        state["last"] = pct

            item_pbar = _Slice()

        path = _download_bytes(img_url, out_path, proxy=proxy, pbar=item_pbar)
        paths.append(path)

    info = _slim_info(detail, page)
    info["kind"] = "image"
    info["image_count"] = len(paths)
    info["image_paths"] = paths
    return paths, info


def download_douyin(
    url: str,
    out_path: str,
    cookiefile=None,
    cookiesfrombrowser=None,
    proxy: str = "",
    pbar=None,
) -> Tuple[str, dict]:
    aweme_id = _resolve_aweme_id(url, proxy=proxy)
    page = f"https://www.douyin.com/video/{aweme_id}"
    detail = fetch_aweme_detail(
        aweme_id, cookiefile=cookiefile, cookiesfrombrowser=cookiesfrombrowser, proxy=proxy,
    )
    video_url = _extract_video_download_url(detail)
    if not video_url:
        raise RuntimeError("抖音 API 未返回可下载的视频地址（可能是图文或已下架）。")

    logger.info("[fx_video] 抖音直链下载 %s", aweme_id)
    path = _download_bytes(video_url, out_path, proxy=proxy, pbar=pbar)
    return path, _slim_info(detail, page)
