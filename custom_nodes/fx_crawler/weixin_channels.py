# -*- coding: utf-8 -*-
"""微信视频号（Channels）解析与下载。

finder-preview / sph 分享页匿名接口只返回封面，不返回 videoUrl。
可用路径：
1) 链接自带 token + eid（feed 预览页）→ 直接 get_feed_info；
2) 提供腾讯元宝 yuanbao.tencent.com 的 cookie → 先解析出 playable_url，
   再拿 token/eid 调 get_feed_info（与 wx_channels_download 的 parse_sph 同思路）。

环境变量（可选）：FX_CRAWLER_YUANBAO_COOKIE
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

logger = logging.getLogger("fx_crawler.weixin_channels")

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_SPH_HOST_KEYS = (
    "channels.weixin.qq.com",
    "weixin.qq.com/sph/",
    "weixin.qq.com/sph?",
)

_YUANBAO_PARSE = "https://yuanbao.tencent.com/api/weixin/get_parse_result"
_FEED_INFO = "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"


def is_weixin_channels_url(url: str) -> bool:
    low = (url or "").lower()
    return any(k in low for k in _SPH_HOST_KEYS)


def normalize_channels_url(raw: str) -> str:
    """从分享文案抽出视频号链接；短链统一成 weixin.qq.com/sph/<id>。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    m = re.search(r"https?://[^\s一-鿿，,、\"'）)】\]]+", text)
    url = (m.group(0) if m else text).rstrip(".,;")
    if not is_weixin_channels_url(url):
        # 裸 sph id：export/... 或 AFVgkzJulP
        if re.fullmatch(r"export/[A-Za-z0-9_\-/=]+", text):
            return f"https://channels.weixin.qq.com/finder-preview/pages/feed?eid={urllib.parse.quote(text, safe='')}"
        if re.fullmatch(r"[A-Za-z0-9_-]{6,32}", text):
            return f"https://weixin.qq.com/sph/{text}"
        return url

    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    host_path = (parsed.netloc + parsed.path).lower()

    if "weixin.qq.com" in host_path and "/sph/" in host_path:
        sid = parsed.path.rstrip("/").split("/")[-1]
        if sid:
            return f"https://weixin.qq.com/sph/{sid}"

    # finder-preview/pages/sph?id=xxx → 标准分享链，方便走元宝解析
    if "finder-preview" in host_path and "/sph" in host_path:
        sid = (qs.get("id") or [""])[0].strip()
        if sid:
            return f"https://weixin.qq.com/sph/{sid}"

    return url


def _rid() -> str:
    return f"{int(time.time()):x}-{''.join(random.choice('0123456789abcdef') for _ in range(8))}"


def _http_json(method: str, url: str, body: Optional[dict], headers: dict, timeout: float = 30) -> dict:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"HTTP {e.code} {url}: {detail}") from e
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"接口返回非 JSON：{raw[:200]!r}") from e


def _plain_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return " ".join(urllib.parse.unquote(text).split())


def _check_feed_err(payload: dict) -> None:
    err_code = payload.get("errCode")
    if err_code not in (0, None, ""):
        msg = _plain_html(str(payload.get("errMsg") or ""))
        raise RuntimeError(f"get_feed_info errCode={err_code}: {msg or 'unknown'}")
    err = ((payload.get("data") or {}).get("errMsg") or {})
    etype = err.get("type")
    title = _plain_html(str(err.get("title") or ""))
    content = _plain_html(str(err.get("content") or ""))
    if etype not in (0, None, "") or title or content:
        # type=0 且无文案视为成功
        if etype in (0, None, "") and not title and not content:
            return
        raise RuntimeError(f"视频号内容不可用：{title or content or f'type={etype}'}")


def get_feed_info(export_id: str, general_token: str = "") -> dict:
    export_id = (export_id or "").strip()
    general_token = (general_token or "").strip()
    if not export_id:
        raise RuntimeError("缺少 exportId / eid。")

    rid = _rid()
    api = (
        f"{_FEED_INFO}?_rid={rid}"
        f"&_pageUrl=https:%2F%2Fchannels.weixin.qq.com%2Ffinder-preview%2Fpages%2Ffeed"
    )
    referer = (
        "https://channels.weixin.qq.com/finder-preview/pages/feed"
        f"?entry_card_type=48&comment_scene=39&appid=0"
        f"&token={urllib.parse.quote(general_token)}"
        f"&entry_scene=0&eid={urllib.parse.quote(export_id)}"
    )
    payload = {"baseReq": {"generalToken": general_token}, "exportId": export_id}
    data = _http_json(
        "POST",
        api,
        payload,
        {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://channels.weixin.qq.com",
            "Referer": referer,
            "User-Agent": _UA,
        },
    )
    _check_feed_err(data)
    return data


def get_feed_info_by_short_uri(short_uri: str, general_token: str = "") -> dict:
    short_uri = (short_uri or "").strip()
    if not short_uri:
        raise RuntimeError("缺少 shortUri / sph id。")
    rid = _rid()
    api = (
        f"{_FEED_INFO}?_rid={rid}"
        f"&_pageUrl=https:%2F%2Fchannels.weixin.qq.com%2Ffinder-preview%2Fpages%2Fsph"
    )
    payload = {"baseReq": {"generalToken": general_token or ""}, "shortUri": short_uri}
    data = _http_json(
        "POST",
        api,
        payload,
        {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://channels.weixin.qq.com",
            "Referer": f"https://channels.weixin.qq.com/finder-preview/pages/sph?id={urllib.parse.quote(short_uri)}",
            "User-Agent": _UA,
        },
    )
    _check_feed_err(data)
    return data


def parse_share_via_yuanbao(share_url: str, cookie: str) -> dict:
    """→ {wx_export_id, playable_url, author, desc, cover_url, ...}"""
    cookie = (cookie or "").strip()
    if not cookie:
        raise RuntimeError(
            "视频号分享链需要腾讯元宝 cookie 才能解析出可播放 token。\n"
            "做法：浏览器登录 https://yuanbao.tencent.com ，DevTools 复制 Cookie，"
            "填到节点的「cookie」框（cookie来源选「cookie文本」），"
            "或设置环境变量 FX_CRAWLER_YUANBAO_COOKIE。"
        )
    body = {"type": "video_channel_url", "url": share_url, "scene": 1}
    data = _http_json(
        "POST",
        _YUANBAO_PARSE,
        body,
        {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://yuanbao.tencent.com",
            "referer": "https://yuanbao.tencent.com/",
            "user-agent": _UA,
            "cookie": cookie,
        },
    )
    if int(data.get("code") or 0) != 0:
        raise RuntimeError(
            f"元宝解析失败 code={data.get('code')}: {data.get('msg') or data}"
        )
    result = data.get("data") or {}
    if not result.get("playable_url") and not result.get("wx_export_id"):
        raise RuntimeError(f"元宝未返回 playable_url：{json.dumps(data, ensure_ascii=False)[:300]}")
    return result


def _pick_video_url(feed_info: dict) -> str:
    for key in ("videoUrl",):
        u = str(feed_info.get(key) or "").strip()
        if u.startswith("http"):
            return u
    for nested in ("h264VideoInfo", "h265VideoInfo"):
        info = feed_info.get(nested) or {}
        if isinstance(info, dict):
            u = str(info.get("videoUrl") or "").strip()
            if u.startswith("http"):
                return u
    return ""


def clean_video_url(video_url: str) -> str:
    """保留 encfilekey + token，去掉多余 query（更稳的直链）。"""
    try:
        u = urllib.parse.urlparse(video_url)
        qs = urllib.parse.parse_qs(u.query)
        filekey = (qs.get("encfilekey") or [""])[0]
        token = (qs.get("token") or [""])[0]
        if filekey and token:
            q = urllib.parse.urlencode({"encfilekey": filekey, "token": token})
            return f"{u.scheme}://{u.netloc}{u.path}?{q}"
    except Exception:
        pass
    return video_url


def resolve_channels_media(url: str, yuanbao_cookie: str = "") -> dict:
    """解析视频号链接 → 统一 info（含直链）。

    返回字段对齐 yt-dlp slim info，额外带 video_url / raw。
    """
    url = normalize_channels_url(url)
    if not url or not is_weixin_channels_url(url):
        raise RuntimeError("不是微信视频号链接。")

    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    token = (qs.get("token") or [""])[0].strip()
    eid = (qs.get("eid") or qs.get("exportId") or [""])[0].strip()
    short_id = ""

    if "weixin.qq.com" in parsed.netloc and "/sph/" in parsed.path:
        short_id = parsed.path.rstrip("/").split("/")[-1]
    elif "finder-preview" in parsed.path and "/sph" in parsed.path:
        short_id = (qs.get("id") or [""])[0].strip()

    feed_payload: dict = {}
    author = ""
    description = ""
    cover = ""

    if token and eid:
        feed_payload = get_feed_info(eid, token)
    elif short_id:
        cookie = (yuanbao_cookie or os.environ.get("FX_CRAWLER_YUANBAO_COOKIE") or "").strip()
        share = f"https://weixin.qq.com/sph/{short_id}"
        parsed_yb = parse_share_via_yuanbao(share, cookie)
        playable = str(parsed_yb.get("playable_url") or "").strip()
        pu = urllib.parse.urlparse(playable)
        pqs = urllib.parse.parse_qs(pu.query)
        token = (pqs.get("token") or [""])[0].strip() or token
        eid = (pqs.get("eid") or [""])[0].strip() or str(parsed_yb.get("wx_export_id") or "").strip()
        if not eid:
            raise RuntimeError("元宝解析成功但没有 eid / wx_export_id。")
        feed_payload = get_feed_info(eid, token)
        author = str(parsed_yb.get("author") or "")
        description = str(parsed_yb.get("desc") or "")
        cover = str(parsed_yb.get("cover_url") or "")
    elif eid:
        # 只有 eid、没有 token：大概率播不了，但仍尝试
        feed_payload = get_feed_info(eid, token)
    else:
        raise RuntimeError(
            "无法识别视频号链接。支持：\n"
            "- https://weixin.qq.com/sph/xxxx\n"
            "- https://channels.weixin.qq.com/finder-preview/pages/sph?id=xxxx\n"
            "- 带 token&eid 的 feed 预览链接"
        )

    data = feed_payload.get("data") or {}
    feed = data.get("feedInfo") or {}
    author_info = data.get("authorInfo") or {}
    author = author or str(author_info.get("nickname") or "")
    description = description or str(feed.get("description") or "")
    cover = cover or str(feed.get("coverUrl") or "")

    video_url = _pick_video_url(feed)
    if not video_url:
        raise RuntimeError(
            "已拿到视频号详情，但没有 videoUrl（可能 cookie 失效，或内容仅图片/已下架）。"
        )
    video_url = clean_video_url(video_url)

    return {
        "id": eid or short_id or "",
        "title": (description.split("\n", 1)[0].strip() or author or "微信视频号"),
        "uploader": author,
        "description": description,
        "thumbnail": cover,
        "webpage_url": url,
        "video_url": video_url,
        "ext": "mp4",
        "raw": feed_payload,
    }


def download_url_to_file(video_url: str, dest_path: str, proxy: str = "", pbar=None) -> str:
    """直链下载到 dest_path（.mp4）。"""
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    tmp = dest_path + ".part"
    headers = {
        "User-Agent": _UA,
        "Referer": "https://channels.weixin.qq.com/",
        "Origin": "https://channels.weixin.qq.com",
    }
    opener = urllib.request.build_opener()
    if str(proxy or "").strip():
        opener.add_handler(urllib.request.ProxyHandler({
            "http": proxy.strip(),
            "https": proxy.strip(),
        }))

    req = urllib.request.Request(video_url, headers=headers)
    with opener.open(req, timeout=60) as resp, open(tmp, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        last_pct = 0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if pbar is not None and total > 0:
                pct = min(100, int(done * 100 / total))
                if pct > last_pct:
                    pbar.update(pct - last_pct)
                    last_pct = pct

    # 简单魔数检查：加密文件可能仍能下下来但无法播放；mp4 通常以 ftyp 开头
    with open(tmp, "rb") as f:
        head = f.read(32)
    if b"ftyp" not in head and not head.startswith(b"\x00\x00\x00"):
        logger.warning("[weixin_channels] 文件头不像标准 mp4：%r", head[:16])

    os.replace(tmp, dest_path)
    return dest_path


def probe_channels(url: str, yuanbao_cookie: str = "", proxy: str = "") -> dict:
    _ = proxy  # 预留
    return resolve_channels_media(url, yuanbao_cookie=yuanbao_cookie)


def download_channels(
    url: str,
    out_path: str,
    yuanbao_cookie: str = "",
    proxy: str = "",
    pbar=None,
) -> tuple[str, dict]:
    info = resolve_channels_media(url, yuanbao_cookie=yuanbao_cookie)
    path = download_url_to_file(info["video_url"], out_path, proxy=proxy, pbar=pbar)
    slim = {k: info.get(k) for k in (
        "id", "title", "uploader", "description", "thumbnail", "webpage_url", "ext", "video_url",
    ) if info.get(k) is not None}
    return path, slim
