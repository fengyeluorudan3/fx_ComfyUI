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


_DETAIL_URI = "/aweme/v1/web/aweme/detail/"
_MOBILE_UA = (
    "com.ss.android.ugc.aweme/290100 (Linux; U; Android 12; zh_CN; Pixel 6; "
    "Build/SP1A.210812.016; Cronet/TTNetVersion:3ed4f6a5 2022-06-30 QuicVersion:0144d358 2022-03-29)"
)
_MOBILE_FEED_HOSTS = ("https://aweme.snssdk.com", "https://api5-normal-c-hl.amemv.com")


class _DetailRouteFailed(RuntimeError):
    """单条路由失败（换下一条），最终都失败才对用户抛。"""


def _parse_detail_response(resp, route: str) -> dict:
    if resp.status_code != 200:
        raise _DetailRouteFailed(
            f"{route}: HTTP {resp.status_code} {resp.text[:80].replace(chr(10), ' ')}"
        )
    try:
        data = resp.json()
    except Exception as e:
        raise _DetailRouteFailed(f"{route}: 非 JSON 响应 {resp.text[:80]!r}") from e
    if not isinstance(data, dict) or data.get("status_code") not in (0, None):
        raise _DetailRouteFailed(f"{route}: status {data.get('status_msg') or data.get('status_code')}")
    detail = data.get("aweme_detail")
    if not detail and isinstance(data.get("aweme_list"), list) and data["aweme_list"]:
        detail = data["aweme_list"][0]
    if not detail or not isinstance(detail, dict):
        raise _DetailRouteFailed(f"{route}: aweme_detail 为空")
    return detail


def _fetch_detail_open_route(aweme_id: str, cookie_dict: Dict[str, str], proxy: str) -> dict:
    """路由 1：open.douyin.com 来源的免签名请求。

    2026-09-14 起 www.douyin.com 的 detail 接口挂在 Argus 网关后面，脚本请求带 cookie +
    a_bogus 也会被拦（"Blocked by ArgusSecurityPlugin Uifid Not Found / Signature Not Found"）。
    以 open.douyin.com 为 Origin、只带 aweme_id + aid 的裸请求不经这套校验，且返回的
    bit_rate 列表完整（含 1080p）。不需要 cookie；有 UIFID 时顺带以 header 送上。
    """
    headers = {
        "User-Agent": _UA,
        "Origin": "https://open.douyin.com",
        "Referer": "https://open.douyin.com/",
        "Accept": "application/json, text/plain, */*",
    }
    uifid = (cookie_dict or {}).get("UIFID") or (cookie_dict or {}).get("UIFID_TEMP")
    if uifid:
        headers["uifid"] = uifid
    with httpx.Client(proxy=proxy or None, timeout=30.0, follow_redirects=True) as client:
        resp = client.get(
            f"https://www.douyin.com{_DETAIL_URI}",
            params={"aweme_id": aweme_id, "aid": "6383"},
            headers=headers,
        )
    return _parse_detail_response(resp, "open.douyin 免签名")


def _fetch_detail_mobile_route(aweme_id: str, proxy: str) -> dict:
    """路由 2：移动端 feed 接口（app 协议），无 cookie / 签名，但 bit_rate 只有一档。"""
    params = {
        "aweme_id": aweme_id,
        "aid": "1128",
        "version_name": "29.1.0",
        "device_platform": "android",
    }
    last: Optional[Exception] = None
    for host in _MOBILE_FEED_HOSTS:
        try:
            with httpx.Client(proxy=proxy or None, timeout=30.0, follow_redirects=True) as client:
                resp = client.get(f"{host}/aweme/v1/feed/", params=params, headers={"User-Agent": _MOBILE_UA})
            return _parse_detail_response(resp, f"移动端 feed {host}")
        except Exception as e:  # noqa: PERF203
            last = e
    raise _DetailRouteFailed(f"移动端 feed 全部失败：{last}")


def fetch_aweme_detail(
    aweme_id: str,
    cookiefile=None,
    cookiesfrombrowser=None,
    proxy: str = "",
) -> dict:
    """
    抖音作品详情，按顺序尝试：
      1. open.douyin.com 免签名请求（无需 cookie，画质列表完整）
      2. 移动端 feed 接口（无需 cookie，单一画质）
      3. 旧版 www.douyin.com + cookie + a_bogus 签名路由（Argus 网关上线后基本不通，仅兜底）
    """
    cookie_dict: Dict[str, str] = {}
    if cookiefile or cookiesfrombrowser:
        try:
            cookie_dict = get_cookie_dict(cookiefile, cookiesfrombrowser) or {}
        except Exception as e:
            logger.warning("[fx_video] 抖音 cookie 读取失败，按无 cookie 继续：%s", e)

    errors = []
    for name, fn in (
        ("open", lambda: _fetch_detail_open_route(aweme_id, cookie_dict, proxy)),
        ("mobile", lambda: _fetch_detail_mobile_route(aweme_id, proxy)),
    ):
        try:
            detail = fn()
            logger.info("[fx_video] 抖音详情路由 %s 成功 %s", name, aweme_id)
            return detail
        except Exception as e:
            logger.warning("[fx_video] 抖音详情路由 %s 失败：%s", name, e)
            errors.append(str(e))

    if not cookie_dict:
        raise RuntimeError(
            "抖音详情获取失败（免签名路由与移动端路由都不通）。\n"
            + "\n".join(f"  - {e}" for e in errors)
            + "\n可在节点里接入登录 cookie（cookie文本 / fx_crawler浏览器）后重试兜底路由。"
        )
    try:
        return _fetch_aweme_detail_signed(aweme_id, cookie_dict, proxy)
    except Exception as e:
        errors.append(str(e))
        raise RuntimeError(
            "抖音详情获取失败：\n" + "\n".join(f"  - {x}" for x in errors)
        ) from e


def _fetch_aweme_detail_signed(aweme_id: str, cookie_dict: Dict[str, str], proxy: str = "") -> dict:
    """路由 3（兜底）：www.douyin.com + cookie + a_bogus 签名。"""
    from fx_crawler_core.media_platform.douyin.help import get_a_bogus_from_js

    headers = {
        "User-Agent": _UA,
        "Cookie": cookie_header(cookie_dict),
        "Referer": f"https://www.douyin.com/video/{aweme_id}",
        "Accept": "application/json, text/plain, */*",
    }
    uifid = cookie_dict.get("UIFID") or cookie_dict.get("UIFID_TEMP")
    if uifid:
        headers["uifid"] = uifid
    params = {"aweme_id": aweme_id}
    params.update(_douyin_common_params(cookie_dict))

    uri = _DETAIL_URI
    query_string = urllib.parse.urlencode(params)
    params["a_bogus"] = get_a_bogus_from_js(uri, query_string, _UA)
    url = f"https://www.douyin.com{uri}?" + urllib.parse.urlencode(params)

    with httpx.Client(proxy=proxy or None, timeout=30.0, follow_redirects=True) as client:
        resp = client.get(url, headers=headers)
    return _parse_detail_response(resp, "www.douyin 签名路由")


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
