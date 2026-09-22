# -*- coding: utf-8 -*-
"""fx_crawler 视频下载节点：粘贴链接 → 下载 → 输出 ComfyUI 的 VIDEO / AUDIO。

底层用 yt-dlp，一份代码覆盖多平台：
  B站 / 抖音 / 小红书 / 快手 / 微博 / 西瓜 / 好看 / 微视 / AcFun /
  YouTube / TikTok / X(Twitter) / Instagram / Vimeo / Twitch ... (yt-dlp 支持的都行)

微信视频号走独立解析（weixin_channels.py）：yt-dlp 不支持。
分享链 / finder-preview 需腾讯元宝 cookie（或带 token&eid 的 feed 链接）。

设计要点：
- 输入框直接吃"分享文案"（抖音/小红书复制出来是一大段文字带链接），自动抽 URL；
- 下载结果落在 ComfyUI 的 output/fx_video 下并按 URL+画质 做缓存，重跑工作流不会重复下载；
- 输出 VIDEO 对象，可直接接 Save Video / Trim Video / Get Video Components；
- cookie 支持四种来源，其中"fx_crawler浏览器"复用本插件 browser_data 里的登录态。
"""

import glob
import hashlib
import json
import logging
import os
import re
import shutil
import sys

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

logger = logging.getLogger("fx_crawler.video")

try:
    import folder_paths
except Exception:  # 脱离 ComfyUI 单测时
    folder_paths = None

try:
    from comfy_api.input_impl import VideoFromFile
except Exception:  # 老版本 ComfyUI
    VideoFromFile = None

try:
    from comfy.utils import ProgressBar
except Exception:
    ProgressBar = None

try:
    from server import PromptServer
except Exception:
    PromptServer = None


def _send_progress_text(node_id, text: str):
    """在 ComfyUI 节点上显示进度文字（如「下载 3/10 · 标题」）。"""
    msg = str(text or "").strip()
    if not msg:
        return
    logger.info("[fx_video] %s", msg)
    if not node_id or PromptServer is None:
        return
    try:
        inst = PromptServer.instance
        if inst is not None:
            inst.send_progress_text(msg, node_id)
    except Exception:
        pass


class _ItemProgressBar:
    """单条视频下载时的字节进度 → 更新批量进度文案。"""

    def __init__(self, batch: "_BatchProgress", item_no: int, title: str = ""):
        self._batch = batch
        self._item_no = item_no
        self._title = title
        self._pct = 0
        self._last_shown = -1

    def update(self, delta):
        self._pct = min(100, self._pct + int(delta or 0))
        if self._pct >= 100 or self._pct - self._last_shown >= 5:
            self._last_shown = self._pct
            self._batch.show_item(self._item_no, self._title, self._pct)


class _BatchProgress:
    """批量下载进度：ComfyUI 进度条 + 节点文字。"""

    def __init__(self, total: int, node_id=None):
        self.total = max(int(total or 0), 0)
        self.node_id = node_id
        self._pbar = ProgressBar(self.total, node_id=node_id) if ProgressBar and self.total else None
        self._done = 0

    def say(self, text: str):
        _send_progress_text(self.node_id, text)

    def show_item(self, index: int, title: str = "", pct: int = None):
        t = str(title or "").replace("\n", " ")[:45]
        if pct is None:
            msg = f"下载 {index}/{self.total} · {t}" if t else f"下载 {index}/{self.total}"
        else:
            msg = f"下载 {index}/{self.total} · {pct}%"
            if t:
                msg += f" · {t}"
        self.say(msg)

    def start_item(self, index: int, title: str = ""):
        self.show_item(index, title)

    def item_pbar(self, index: int, title: str = ""):
        return _ItemProgressBar(self, index, title)

    def finish_item(self):
        self._done += 1
        if self._pbar is not None:
            self._pbar.update(1)

    def done(self, text: str = "全部下载完成"):
        self.say(text)
        if self._pbar is not None and self._done < self.total:
            self._pbar.update_absolute(self.total)

# ---------------------------------------------------------------- 常量 / 映射

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 画质 → yt-dlp format 表达式。bv*+ba 是分轨最佳，后面的 /b 是单文件兜底。
_QUALITY = {
    "最佳": "bv*+ba/b",
    "2160p": "bv*[height<=2160]+ba/b[height<=2160]/bv*+ba/b",
    "1440p": "bv*[height<=1440]+ba/b[height<=1440]/bv*+ba/b",
    "1080p": "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
    "720p": "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b",
    "480p": "bv*[height<=480]+ba/b[height<=480]/bv*+ba/b",
    "360p": "bv*[height<=360]+ba/b[height<=360]/bv*+ba/b",
    "仅音频": "ba/b",
}

# 平台识别：域名关键词 → (平台名, cookie 域)
_SITES = [
    ("channels.weixin.qq.com", "视频号", ".tencent.com"),
    ("weixin.qq.com/sph", "视频号", ".tencent.com"),
    ("bilibili.com", "B站", ".bilibili.com"),
    ("b23.tv", "B站", ".bilibili.com"),
    ("douyin.com", "抖音", ".douyin.com"),
    ("iesdouyin.com", "抖音", ".douyin.com"),
    ("xiaohongshu.com", "小红书", ".xiaohongshu.com"),
    ("xhslink.com", "小红书", ".xiaohongshu.com"),
    ("kuaishou.com", "快手", ".kuaishou.com"),
    ("weibo.com", "微博", ".weibo.com"),
    ("weibo.cn", "微博", ".weibo.cn"),
    ("ixigua.com", "西瓜", ".ixigua.com"),
    ("acfun.cn", "AcFun", ".acfun.cn"),
    ("haokan.baidu.com", "好看", ".baidu.com"),
    ("weishi.qq.com", "微视", ".qq.com"),
    ("youtube.com", "YouTube", ".youtube.com"),
    ("youtu.be", "YouTube", ".youtube.com"),
    ("tiktok.com", "TikTok", ".tiktok.com"),
    ("twitter.com", "X", ".twitter.com"),
    ("x.com", "X", ".x.com"),
    ("instagram.com", "Instagram", ".instagram.com"),
    ("vimeo.com", "Vimeo", ".vimeo.com"),
    ("twitch.tv", "Twitch", ".twitch.tv"),
]

_COOKIE_SRC = ["不用", "cookie文本", "cookies.txt文件", "本机Chrome", "fx_crawler浏览器"]

# 从采集结果条目里提取可下载链接时的字段优先级
_CONTENT_URL_KEYS = ("aweme_url", "note_url", "video_url", "url", "share_url", "link", "webpage_url")

# 按用户批量下载支持的平台（采集 creator 模式 + yt-dlp 下载）
_USER_DL_PLATFORMS = {"抖音": "dy", "小红书": "xhs", "B站": "bili"}
_USER_DL_PLATFORM_NAMES = {v: k for k, v in _USER_DL_PLATFORMS.items()}
_CONTENT_MODE_UI = ["仅视频", "仅图文", "全部"]


def _content_mode_key(ui: str) -> str:
    text = str(ui or "").strip()
    if text in ("仅图文", "图文"):
        return "image"
    if text in ("全部",):
        return "all"
    return "video"


def _normalize_list_items(pairs: list) -> list:
    """统一为 (url, title, kind)。"""
    out = []
    for p in pairs or []:
        if not p:
            continue
        if len(p) >= 3:
            out.append((str(p[0]), str(p[1] or ""), str(p[2] or "video")))
        else:
            out.append((str(p[0]), str(p[1] or ""), "video"))
    return out


# ---------------------------------------------------------------- 基础工具


def _cache_dir(custom: str = "") -> str:
    """默认放 ComfyUI 的 output/fx_video，与 Save Video 等输出目录一致。"""
    if str(custom or "").strip():
        d = os.path.abspath(os.path.expanduser(custom.strip()))
    elif folder_paths is not None:
        d = os.path.join(folder_paths.get_output_directory(), "fx_video")
    else:
        d = os.path.join(_PLUGIN_DIR, "_output", "fx_video")
    os.makedirs(d, exist_ok=True)
    return d


def _ensure_ytdlp():
    try:
        import yt_dlp  # noqa: F401
        return yt_dlp
    except ImportError as e:
        raise RuntimeError(
            "缺少 yt-dlp。请在 ComfyUI 所用的 Python 环境执行：\n"
            "    pip install -U yt-dlp\n"
            "（各平台接口变动频繁，下载失败时第一步就是升级它）"
        ) from e


def _ffmpeg_location():
    """B站等站点音视频分轨，合并必须有 ffmpeg。优先 PATH，其次 imageio-ffmpeg 自带的。"""
    exe = shutil.which("ffmpeg")
    if exe:
        return os.path.dirname(exe)
    try:
        import imageio_ffmpeg
        return os.path.dirname(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        return None


def normalize_url(raw: str) -> str:
    """从"分享文案"里抠出真正的 URL；也支持直接填 BV号/av号。

    抖音复制出来长这样：`7.53 xxx 复制打开抖音，看看【标题】 https://v.douyin.com/xxx/ ...`
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    # 视频号优先（含 finder-preview / weixin.qq.com/sph / 裸 sph id）
    try:
        from weixin_channels import is_weixin_channels_url, normalize_channels_url
        cand = normalize_channels_url(text)
        if cand and is_weixin_channels_url(cand):
            return cand
    except Exception:
        pass
    m = re.search(r'https?://[^\s一-鿿，,、"\'）)】\]]+', text)
    if m:
        return m.group(0).rstrip(".,;")
    # 裸 ID
    m = re.fullmatch(r"(BV[0-9A-Za-z]{10})", text)
    if m:
        return f"https://www.bilibili.com/video/{m.group(1)}"
    m = re.fullmatch(r"[Aa][Vv](\d+)", text)
    if m:
        return f"https://www.bilibili.com/video/av{m.group(1)}"
    # 抖音 aweme_id：19 位纯数字（B站 av 号没这么长，不会撞）
    if re.fullmatch(r"\d{17,20}", text):
        return f"https://www.douyin.com/video/{text}"
    return text


def detect_site(url: str):
    """→ (平台名, cookie域)。未知站点按主域名兜底。"""
    low = url.lower()
    for key, name, domain in _SITES:
        if key in low:
            return name, domain
    m = re.search(r"https?://([^/:]+)", low)
    host = (m.group(1) if m else "").lstrip("www.")
    return (host or "未知"), ("." + host if host else "")


def _split_user_lines(value: str):
    items = []
    for line in str(value or "").replace(",", "\n").replace("，", "\n").splitlines():
        s = line.strip()
        if s:
            items.append(s)
    return items


def detect_user_platform(raw: str) -> str:
    """从用户主页 URL/ID 推断平台代码 dy/xhs/bili。"""
    text = str(raw or "").strip()
    if not text:
        raise RuntimeError("用户主页是空的。")
    low = text.lower()
    if "douyin.com" in low or "iesdouyin.com" in low:
        return "dy"
    if "xiaohongshu.com" in low or "xhslink.com" in low:
        return "xhs"
    if "bilibili.com" in low or "b23.tv" in low or "space.bilibili" in low:
        return "bili"
    if re.fullmatch(r"\d{5,12}", text):
        return "bili"
    if re.fullmatch(r"MS4wLj[A-Za-z0-9_-]{20,}", text):
        return "dy"
    raise RuntimeError(
        "无法识别平台。请手动选择，或填入带域名的用户主页链接。\n"
        "示例：\n"
        "  抖音 https://www.douyin.com/user/MS4wLj...\n"
        "  小红书 https://www.xiaohongshu.com/user/profile/...\n"
        "  B站 https://space.bilibili.com/123456"
    )


def _content_kind_from_crawl_item(item: dict, platform: str) -> str:
    if platform == "dy":
        if str(item.get("note_download_url") or "").strip():
            return "image"
        return "video"
    if platform == "xhs":
        if str(item.get("type") or "").lower() == "video":
            return "video"
        if str(item.get("video_url") or "").strip():
            return "video"
        return "image"
    return "video"


def _is_video_content_item(item: dict, platform: str) -> bool:
    return _content_kind_from_crawl_item(item, platform) == "video"


def _extract_content_download_url(item: dict) -> str:
    for key in _CONTENT_URL_KEYS:
        val = str(item.get(key) or "").strip()
        if val.startswith("http"):
            return val.split(",")[0].strip()
    video_id = str(item.get("video_id") or "").strip()
    if video_id.isdigit():
        return f"https://www.bilibili.com/video/av{video_id}"
    return ""


def _normalize_user_page_url(platform: str, raw: str) -> str:
    """把用户主页规范成 yt-dlp / 采集能识别的 URL。"""
    text = str(raw or "").strip()
    if platform == "bili":
        if text.isdigit():
            return f"https://space.bilibili.com/{text}/video"
        low = text.lower()
        if "space.bilibili.com" in low:
            base = text.split("?")[0].rstrip("/")
            if re.search(r"space\.bilibili\.com/\d+$", base, re.I):
                return base + "/video"
            return base
        return text
    if platform == "dy":
        if text.startswith("http"):
            return text
        if re.fullmatch(r"MS4wLj[A-Za-z0-9_-]{20,}", text):
            return f"https://www.douyin.com/user/{text}"
        return text
    return text


def _entry_to_video_url(platform: str, entry: dict) -> str:
    if not entry:
        return ""
    for key in ("url", "webpage_url"):
        val = str(entry.get(key) or "").strip()
        if val.startswith("http"):
            return val
    eid = str(entry.get("id") or "").strip()
    if platform == "bili" and eid:
        if eid.upper().startswith("BV"):
            return f"https://www.bilibili.com/video/{eid}"
        if eid.isdigit():
            return f"https://www.bilibili.com/video/av{eid}"
    if platform == "dy" and eid.isdigit():
        return f"https://www.douyin.com/video/{eid}"
    return ""


def _list_user_urls_ytdlp(platform: str, user_urls: list, max_count: int,
                          cookiefile=None, cookiesfrombrowser=None, proxy: str = ""):
    """用 yt-dlp 解析用户主页播放列表，不启动 Playwright。"""
    yt_dlp = _ensure_ytdlp()
    pairs = []
    seen = set()
    for raw in user_urls:
        page_url = _normalize_user_page_url(platform, raw)
        remaining = max_count - len(pairs)
        if remaining <= 0:
            break
        opts = _base_opts(cookiefile, cookiesfrombrowser, proxy)
        opts.update({
            "extract_flat": "in_playlist",
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "ignoreerrors": True,
            "playlistend": remaining,
        })
        logger.info("[fx_video] yt-dlp 拉列表：%s", page_url)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(page_url, download=False)
        except Exception as e:
            logger.warning("[fx_video] yt-dlp 拉列表失败 %s: %s", page_url, e)
            continue
        entries = []
        if info:
            if info.get("entries"):
                entries = [e for e in info["entries"] if e]
            elif info.get("id") or info.get("webpage_url"):
                entries = [info]
        for entry in entries:
            url = _entry_to_video_url(platform, entry)
            if not url or url in seen:
                continue
            seen.add(url)
            title = str(entry.get("title") or entry.get("description") or "")
            pairs.append((url, title, "video"))
            if len(pairs) >= max_count:
                break
    return pairs


def _crawl_user_video_list(platform: str, user_urls: list, max_count: int,
                           browser_cdp: bool = True, headless: bool = False) -> list:
    """用采集节点 creator 模式拉取用户作品列表（只采元数据，不下载、不采评论）。"""
    from crawler_runner import run_crawl_sync, PLATFORM_LIST_KEYS

    keys = PLATFORM_LIST_KEYS.get(platform, {})
    creator_key = keys.get("creator")
    if not creator_key:
        raise RuntimeError(f"平台 {_USER_DL_PLATFORM_NAMES.get(platform, platform)} 不支持按用户采集。")

    crawl_root = os.path.join(_cache_dir(""), "_user_crawl")
    os.makedirs(crawl_root, exist_ok=True)
    sig = hashlib.sha1("|".join(sorted(user_urls)).encode("utf-8")).hexdigest()[:12]
    crawl_dir = os.path.join(crawl_root, f"{platform}_{sig}")
    os.makedirs(crawl_dir, exist_ok=True)

    settings = {
        "CRAWLER_TYPE": "creator",
        creator_key: user_urls,
        "CRAWLER_MAX_NOTES_COUNT": int(max_count),
        "ENABLE_GET_COMMENTS": False,
        "ENABLE_GET_SUB_COMMENTS": False,
        "ENABLE_GET_MEIDAS": False,
        "ENABLE_CDP_MODE": bool(browser_cdp),
        "HEADLESS": bool(headless),
        "CDP_HEADLESS": bool(headless),
        "SAVE_DATA_OPTION": "json",
        "LOGIN_TYPE": "qrcode",
    }
    logger.info("[fx_video] 按用户拉列表：%s %s（上限 %d）",
                _USER_DL_PLATFORM_NAMES.get(platform, platform), user_urls, max_count)
    data = run_crawl_sync(platform, settings, crawl_dir)
    return data.get("contents") or []


def _pairs_from_crawl_contents(platform: str, contents: list, max_count: int, content_mode: str):
    from user_list_api import _match_content_mode

    pairs = []
    seen = set()
    for item in contents:
        if not isinstance(item, dict):
            continue
        kind = _content_kind_from_crawl_item(item, platform)
        if not _match_content_mode(kind, content_mode):
            continue
        url = _extract_content_download_url(item)
        if not url or url in seen:
            continue
        seen.add(url)
        title = str(item.get("title") or item.get("desc") or "")
        pairs.append((url, title, kind))
        if len(pairs) >= max_count:
            break
    return pairs


def _collect_user_download_urls(platform: str, user_urls: list, max_count: int,
                              content_mode: str = "video", list_mode: str = "ytdlp",
                              browser_cdp: bool = True, headless: bool = False,
                              cookiefile=None, cookiesfrombrowser=None, proxy: str = ""):
    """→ [(download_url, title, kind), ...]"""
    pname = _USER_DL_PLATFORM_NAMES.get(platform, platform)
    if list_mode != "playwright":
        pairs = _normalize_list_items(_list_user_urls_ytdlp(
            platform, user_urls, max_count,
            cookiefile=cookiefile, cookiesfrombrowser=cookiesfrombrowser, proxy=proxy,
        ))
        if pairs:
            if content_mode != "all":
                from user_list_api import _match_content_mode
                pairs = [p for p in pairs if _match_content_mode(p[2], content_mode)]
            if pairs:
                return pairs[:max_count]

        # yt-dlp 不支持抖音/小红书用户页 → 尝试 HTTP API（cookie + 签名，仍不开 Playwright）
        if platform in ("dy", "xhs"):
            from user_list_api import list_douyin_user_urls, list_xhs_user_urls

            logger.info("[fx_video] yt-dlp 无列表，改走 HTTP API（%s）", pname)
            if platform == "dy":
                pairs = list_douyin_user_urls(
                    user_urls, max_count, cookiefile, cookiesfrombrowser, proxy,
                    normalize_url=_normalize_user_page_url,
                    content_mode=content_mode,
                )
            else:
                pairs = list_xhs_user_urls(
                    user_urls, max_count, cookiefile, cookiesfrombrowser, proxy,
                    content_mode=content_mode,
                )
            if pairs:
                return pairs

        if platform == "dy":
            raise RuntimeError(
                f"{pname} 未能通过 yt-dlp / HTTP API 拉取作品列表（全程不启 Playwright）。\n"
                "请确认 cookie 来源已登录抖音（推荐 fx_crawler浏览器 或 cookie 文本），"
                "或改用「Playwright(备用)」/「下载视频(URL)」逐条下载。"
            )
        if platform == "xhs":
            raise RuntimeError(
                f"{pname} 未能通过 yt-dlp / HTTP API 拉取作品列表。\n"
                "请粘贴带 xsec_token 的完整用户主页链接，并配置登录 cookie；"
                "或改用「Playwright(备用)」。"
            )
        raise RuntimeError(
            f"yt-dlp 未能从 {pname} 用户主页解析出作品。\n"
            "请检查链接是否正确，并提供登录 cookie（B站 412 通常是缺 cookie）。"
        )

    contents = _crawl_user_video_list(
        platform, user_urls, max_count, browser_cdp=browser_cdp, headless=headless,
    )
    return _pairs_from_crawl_contents(platform, contents, max_count, content_mode)


def batch_download(urls: list, quality: str = "1080p", out_dir: str = "", reuse: bool = True,
                   cookiefile=None, cookiesfrombrowser=None, proxy: str = "",
                   cookie_text: str = "", pbar=None, titles=None, progress: _BatchProgress = None,
                   skip_errors: bool = True):
    """批量下载，返回 (paths, infos)。失败条目 path 为 None，info 含 error/url/title。"""
    paths, infos = [], []
    total = len(urls)
    title_list = list(titles or [])
    while len(title_list) < total:
        title_list.append("")
    for i, url in enumerate(urls):
        idx = i + 1
        title = title_list[i]
        if progress is not None:
            progress.start_item(idx, title)
            item_pbar = progress.item_pbar(idx, title)
        else:
            item_pbar = pbar
        logger.info("[fx_video] 批量 %d/%d ← %s", idx, total, url)
        try:
            path, info = download(
                url, quality=quality, out_dir=out_dir, reuse=reuse,
                cookiefile=cookiefile, cookiesfrombrowser=cookiesfrombrowser,
                proxy=proxy, cookie_text=cookie_text, pbar=item_pbar,
            )
            paths.append(path)
            infos.append(info)
        except Exception as e:
            if not skip_errors:
                raise
            err = str(e).strip() or e.__class__.__name__
            logger.warning("[fx_video] 批量 %d/%d 跳过: %s", idx, total, err.split("\n")[0])
            paths.append(None)
            infos.append({"error": err, "url": url, "title": title})
            if progress is not None:
                short = err.replace("\n", " ")[:70]
                progress.say(f"跳过 {idx}/{total} · {short}")
        if progress is not None:
            progress.finish_item()
        elif pbar is not None:
            new_pct = int(idx * 100 / max(total, 1))
            if hasattr(pbar, "update_absolute"):
                pbar.update_absolute(new_pct, 100)
            else:
                pbar.update(max(0, new_pct - getattr(pbar, "current", 0)))
    ok = sum(1 for p in paths if p)
    if progress is not None:
        if ok < total:
            progress.done(f"完成 {ok}/{total}（跳过 {total - ok}）")
        else:
            progress.done(f"完成 {total}/{total}")
    return paths, infos


def _yuanbao_cookie_text(cookie_text: str, cookiefile, cookiesfrombrowser) -> str:
    """视频号解析用的元宝 cookie：优先节点 cookie 文本，其次环境变量，再尝试 cookie 文件。"""
    text = str(cookie_text or "").strip()
    if text and not text.startswith("#") and "\t" not in text:
        return text
    env = str(os.environ.get("FX_CRAWLER_YUANBAO_COOKIE") or "").strip()
    if env:
        return env
    if cookiefile and os.path.isfile(cookiefile):
        try:
            with open(cookiefile, "r", encoding="utf-8") as f:
                raw = f.read().strip()
            if raw and not raw.startswith("#") and "\t" not in raw:
                return raw
            # Netscape → Header 串
            pairs = []
            for line in raw.splitlines():
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7 and "tencent.com" in parts[0]:
                    pairs.append(f"{parts[5]}={parts[6]}")
            if pairs:
                return "; ".join(pairs)
        except Exception as e:
            logger.warning("[fx_video] 读 cookie 文件失败：%s", e)
    _ = cookiesfrombrowser
    return ""


# ---------------------------------------------------------------- cookie


def _write_netscape_cookies(cookie_text: str, domain: str, path: str) -> str:
    """把浏览器里 copy 的 `k=v; k=v` 写成 yt-dlp 要的 Netscape cookies.txt。

    若本来就是 Netscape 格式（以 # 开头或含制表符），原样写出。
    """
    text = str(cookie_text or "").strip()
    if not text:
        raise RuntimeError("cookie来源选了『cookie文本』，但 cookie 是空的。")

    if text.startswith("#") or "\t" in text:
        content = text if text.endswith("\n") else text + "\n"
    else:
        if not domain:
            raise RuntimeError("无法从链接推断 cookie 域名，请改用 cookies.txt 文件。")
        lines = ["# Netscape HTTP Cookie File", "# generated by fx_crawler"]
        for pair in re.split(r";\s*", text):
            if "=" not in pair:
                continue
            k, v = pair.split("=", 1)
            k, v = k.strip(), v.strip()
            if not k:
                continue
            # domain  include_subdomains  path  secure  expiry  name  value
            lines.append(f"{domain}\tTRUE\t/\tTRUE\t2147483647\t{k}\t{v}")
        if len(lines) <= 2:
            raise RuntimeError("cookie 文本里没解析出任何 k=v，检查一下格式。")
        content = "\n".join(lines) + "\n"

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(path, 0o600)
    return path


def _fx_browser_profile():
    """找本插件 browser_data 下的 Chrome 用户目录（fx_crawler 登录过的那个）。"""
    root = os.path.join(_PLUGIN_DIR, "browser_data")
    if not os.path.isdir(root):
        return None
    candidates = []
    for name in os.listdir(root):
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        # Chrome 的 Cookies 可能在 Default/ 或 Default/Network/ 下
        if glob.glob(os.path.join(d, "**", "Cookies"), recursive=True):
            candidates.append((os.path.getmtime(d), d))
    if not candidates:
        return None
    return max(candidates)[1]


def _resolve_cookies(source: str, cookie_text: str, cookie_file: str,
                     domain: str, work_dir: str):
    """→ (cookiefile 路径 or None, cookiesfrombrowser 元组 or None)"""
    if source == "cookie文本":
        safe = re.sub(r"[^0-9A-Za-z.]+", "_", domain or "site")
        path = os.path.join(work_dir, ".cookies", f"{safe}.txt")
        return _write_netscape_cookies(cookie_text, domain, path), None

    if source == "cookies.txt文件":
        p = os.path.abspath(os.path.expanduser(str(cookie_file or "").strip()))
        if not p or not os.path.isfile(p):
            raise RuntimeError(f"cookies.txt 文件不存在：{p}")
        return p, None

    if source == "本机Chrome":
        return None, ("chrome", None, None, None)

    if source == "fx_crawler浏览器":
        profile = _fx_browser_profile()
        if not profile:
            raise RuntimeError(
                "browser_data 下没找到带 Cookies 的 Chrome 用户目录。"
                "先用采集节点（浏览器模式=连我的Chrome）登录一次。"
            )
        return None, ("chrome", profile, None, None)

    return None, None


# ---------------------------------------------------------------- yt-dlp 调用


class _Silent:
    """吞掉 yt-dlp 的 stdout 噪音，warning/error 转到 ComfyUI 日志。"""

    def debug(self, msg):
        if msg.startswith("[debug]"):
            return
        logger.debug(msg)

    def info(self, msg):
        logger.debug(msg)

    def warning(self, msg):
        logger.warning("[yt-dlp] %s", msg)

    def error(self, msg):
        logger.error("[yt-dlp] %s", msg)


def _base_opts(cookiefile, cookiesfrombrowser, proxy):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "logger": _Silent(),
        "http_headers": {"User-Agent": _UA},
    }
    if cookiefile:
        opts["cookiefile"] = cookiefile
    if cookiesfrombrowser:
        opts["cookiesfrombrowser"] = cookiesfrombrowser
    if str(proxy or "").strip():
        opts["proxy"] = proxy.strip()
    return opts


def probe(url: str, cookiefile=None, cookiesfrombrowser=None, proxy="",
          cookie_text: str = "") -> dict:
    """只解析不下载。"""
    url = normalize_url(url)
    try:
        from douyin_direct import is_douyin_video_url, probe_douyin
        if is_douyin_video_url(url):
            return probe_douyin(
                url, cookiefile=cookiefile, cookiesfrombrowser=cookiesfrombrowser, proxy=proxy,
            )
    except ImportError:
        pass
    try:
        from weixin_channels import is_weixin_channels_url, probe_channels
        if is_weixin_channels_url(url):
            yb = _yuanbao_cookie_text(cookie_text, cookiefile, cookiesfrombrowser)
            return probe_channels(url, yuanbao_cookie=yb, proxy=proxy)
    except ImportError:
        pass

    yt_dlp = _ensure_ytdlp()
    opts = _base_opts(cookiefile, cookiesfrombrowser, proxy)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info and info.get("_type") == "playlist" and info.get("entries"):
        entries = [e for e in info["entries"] if e]
        if entries:
            info = entries[0]
    return info or {}


def _picked_path(info: dict, fallback_glob: str):
    """从 yt-dlp 返回信息里拿最终文件路径（合并后的）。"""
    for d in (info.get("requested_downloads") or []):
        p = d.get("filepath") or d.get("_filename")
        if p and os.path.isfile(p):
            return p
    p = info.get("filepath") or info.get("_filename")
    if p and os.path.isfile(p):
        return p
    hits = [f for f in glob.glob(fallback_glob)
            if not f.endswith((".part", ".ytdl", ".temp"))]
    return max(hits, key=os.path.getmtime) if hits else None


def download(url: str, quality: str = "1080p", out_dir: str = "", reuse: bool = True,
             cookiefile=None, cookiesfrombrowser=None, proxy: str = "",
             playlist_index: int = 0, pbar=None, cookie_text: str = ""):
    """下载并返回 (文件路径, info dict)。同 URL+画质 命中缓存则直接返回。"""
    url = normalize_url(url)
    if not url:
        raise RuntimeError("视频链接是空的。")

    work = _cache_dir(out_dir)
    key = "fxv_" + hashlib.sha1(
        f"{url}|{quality}|{playlist_index}".encode("utf-8")
    ).hexdigest()[:16]
    pattern = os.path.join(work, key + ".*")

    if reuse:
        hits = [f for f in glob.glob(pattern)
                if not f.endswith((".part", ".ytdl", ".temp", ".json"))]
        if hits:
            path = max(hits, key=os.path.getmtime)
            meta = os.path.join(work, key + ".json")
            info = {}
            if os.path.isfile(meta):
                try:
                    with open(meta, "r", encoding="utf-8") as f:
                        info = json.load(f)
                except Exception:
                    pass
            logger.info("[fx_video] 命中缓存：%s", path)
            return path, info
    else:
        for f in glob.glob(pattern):
            try:
                os.remove(f)
            except OSError:
                pass

    # ---- 抖音：HTTP API 直链，不走 yt-dlp（yt-dlp 常报 Fresh cookies needed）----
    try:
        from douyin_direct import is_douyin_video_url, download_douyin
        if is_douyin_video_url(url):
            out_mp4 = os.path.join(work, key + ".mp4")
            path, slim = download_douyin(
                url, out_mp4,
                cookiefile=cookiefile, cookiesfrombrowser=cookiesfrombrowser,
                proxy=proxy, pbar=pbar,
            )
            try:
                with open(os.path.join(work, key + ".json"), "w", encoding="utf-8") as f:
                    json.dump(slim, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
            return path, slim
    except ImportError:
        pass

    # ---- 微信视频号：不走 yt-dlp ----
    try:
        from weixin_channels import is_weixin_channels_url, download_channels
        if is_weixin_channels_url(url):
            yb = _yuanbao_cookie_text(cookie_text, cookiefile, cookiesfrombrowser)
            out_mp4 = os.path.join(work, key + ".mp4")
            path, slim = download_channels(
                url, out_mp4, yuanbao_cookie=yb, proxy=proxy, pbar=pbar,
            )
            try:
                with open(os.path.join(work, key + ".json"), "w", encoding="utf-8") as f:
                    json.dump(slim, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
            return path, slim
    except ImportError:
        pass

    yt_dlp = _ensure_ytdlp()
    opts = _base_opts(cookiefile, cookiesfrombrowser, proxy)
    opts.update({
        "format": _QUALITY.get(quality, _QUALITY["1080p"]),
        "outtmpl": {"default": os.path.join(work, key + ".%(ext)s")},
        "concurrent_fragment_downloads": 4,
        "overwrites": True,
    })
    if quality != "仅音频":
        opts["merge_output_format"] = "mp4"
    ff = _ffmpeg_location()
    if ff:
        opts["ffmpeg_location"] = ff

    if playlist_index and playlist_index > 0:
        opts["noplaylist"] = False
        opts["playlist_items"] = str(int(playlist_index))

    if pbar is not None:
        state = {"last": 0}

        def _hook(d):
            if d.get("status") != "downloading":
                return
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if not total:
                return
            pct = min(100, int(done * 100 / total))
            if pct > state["last"]:
                pbar.update(pct - state["last"])
                state["last"] = pct

        opts["progress_hooks"] = [_hook]

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    if info and info.get("_type") == "playlist" and info.get("entries"):
        entries = [e for e in info["entries"] if e]
        if entries:
            info = entries[0]
    info = info or {}

    path = _picked_path(info, pattern)
    if not path:
        raise RuntimeError("yt-dlp 跑完了但没找到输出文件，换个画质或看控制台日志。")

    slim = {k: info.get(k) for k in (
        "id", "title", "uploader", "uploader_id", "duration", "width", "height",
        "fps", "ext", "format", "format_id", "filesize_approx", "webpage_url",
        "thumbnail", "description", "view_count", "like_count", "upload_date",
    ) if info.get(k) is not None}
    try:
        with open(os.path.join(work, key + ".json"), "w", encoding="utf-8") as f:
            json.dump(slim, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return path, slim


def download_images(url: str, out_dir: str = "", reuse: bool = True,
                    cookiefile=None, cookiesfrombrowser=None, proxy: str = "",
                    pbar=None):
    """下载图文作品的全部图片，返回 (paths, info)。"""
    url = normalize_url(url)
    if not url:
        raise RuntimeError("链接是空的。")
    work = _cache_dir(out_dir)
    try:
        from douyin_direct import is_douyin_video_url, download_douyin_images
        if is_douyin_video_url(url):
            return download_douyin_images(
                url, work, reuse=reuse,
                cookiefile=cookiefile, cookiesfrombrowser=cookiesfrombrowser,
                proxy=proxy, pbar=pbar,
            )
    except ImportError:
        pass
    raise RuntimeError("图文下载目前仅完整支持抖音作品链接。")


# ---------------------------------------------------------------- 媒体转换


def _decode_audio(path: str):
    """用 PyAV 解出 ComfyUI 的 AUDIO 字典；失败返回一段静音而不是让工作流崩掉。"""
    import torch
    try:
        import av
        with av.open(path) as container:
            if not container.streams.audio:
                raise ValueError("没有音轨")
            stream = container.streams.audio[0]
            sample_rate = stream.codec_context.sample_rate
            channels = stream.channels
            frames = []
            for frame in container.decode(streams=stream.index):
                buf = torch.from_numpy(frame.to_ndarray())
                if buf.shape[0] != channels:
                    buf = buf.reshape(-1, channels).t()
                frames.append(buf)
        if not frames:
            raise ValueError("没解出音频帧")
        wav = torch.cat(frames, dim=1)
        if not wav.dtype.is_floating_point:
            info = torch.iinfo(wav.dtype)
            wav = wav.float() / max(abs(info.min), info.max)
        else:
            wav = wav.float()
        return {"waveform": wav.unsqueeze(0), "sample_rate": int(sample_rate)}
    except Exception as e:
        logger.warning("[fx_video] 音频解码失败(%s)，输出静音", e)
        return {"waveform": torch.zeros(1, 2, 1), "sample_rate": 44100}


def _make_video(path: str):
    if VideoFromFile is None:
        raise RuntimeError("当前 ComfyUI 版本没有 comfy_api.input_impl.VideoFromFile，升级一下。")
    return VideoFromFile(path)


def _make_image(path: str):
    import numpy as np
    import torch
    from PIL import Image

    img = Image.open(path).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    return torch.from_numpy(arr)[None,]


def _parse_paths_json(raw: str) -> dict:
    """解析批量下载的路径 JSON，兼容旧版纯数组格式。"""
    text = str(raw or "").strip()
    if not text:
        return {"videos": [], "images": []}
    data = json.loads(text)
    if isinstance(data, list):
        return {"videos": [str(p).strip() for p in data if str(p).strip()], "images": []}
    if isinstance(data, dict):
        videos = [str(p).strip() for p in (data.get("videos") or []) if str(p).strip()]
        images = [str(p).strip() for p in (data.get("images") or []) if str(p).strip()]
        return {"videos": videos, "images": images}
    return {"videos": [], "images": []}


def _summary(path: str, info: dict, site: str) -> str:
    size = os.path.getsize(path) / 1024 / 1024 if os.path.isfile(path) else 0
    dur = info.get("duration") or 0
    mmss = f"{int(dur) // 60}:{int(dur) % 60:02d}" if dur else "?"
    wh = f"{info.get('width', '?')}x{info.get('height', '?')}"
    return "\n".join([
        f"[{site}] {info.get('title') or '(无标题)'}",
        f"作者 {info.get('uploader') or '?'} · 时长 {mmss} · {wh} · {size:.1f} MB",
        os.path.basename(path),
    ])


# ---------------------------------------------------------------- 节点


class FXVideoDownload:
    """粘贴链接 → VIDEO。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "视频链接": ("STRING", {"default": "", "multiline": True,
                                    "tooltip": "支持直接粘贴抖音/小红书的分享文案(带一堆文字也行)，"
                                               "也支持 BV号/av号。B站/抖音/小红书/快手/微博/"
                                               "YouTube/TikTok/X/微信视频号 等都可以。"
                                               "视频号示例：https://weixin.qq.com/sph/xxx "
                                               "或 channels.weixin.qq.com/finder-preview/pages/sph?id=xxx"}),
                "画质": (list(_QUALITY.keys()), {"default": "1080p",
                                              "tooltip": "B站 1080P+ 需要登录 cookie，4K/HDR 需要大会员。"
                                                         "视频号目前按平台返回的默认清晰度下载，此选项忽略。"}),
                "缓存复用": ("BOOLEAN", {"default": True, "label_on": "复用", "label_off": "重下",
                                     "tooltip": "同链接+同画质已经下过就直接用，避免每次跑图重复下载"}),
                "高级选项": ("BOOLEAN", {"default": False, "label_on": "展开", "label_off": "收起"}),
            },
            "optional": {
                "cookie来源": (_COOKIE_SRC, {"default": "不用",
                                          "tooltip": "小红书/抖音很多内容、B站高清都需要登录态。"
                                                     "『fx_crawler浏览器』复用本插件采集节点登录过的 Chrome。"
                                                     "视频号：请用「cookie文本」填 yuanbao.tencent.com 的 Cookie"
                                                     "（或设环境变量 FX_CRAWLER_YUANBAO_COOKIE）。"}),
                "cookie": ("STRING", {"default": "", "multiline": True,
                                      "tooltip": "浏览器 DevTools 里 copy 的 `k=v; k=v`，或整份 Netscape cookies.txt 内容。"
                                                 "视频号请粘贴登录 yuanbao.tencent.com 后的 Cookie。"}),
                "cookies文件": ("STRING", {"default": "", "multiline": False,
                                        "tooltip": "cookies.txt 的绝对路径"}),
                "代理": ("STRING", {"default": "", "multiline": False,
                                  "tooltip": "如 http://127.0.0.1:7890，国外站点用"}),
                "合集第几个": ("INT", {"default": 0, "min": 0, "max": 5000,
                                  "tooltip": "0=只下这一个；合集/多P/播放列表填第几个(从1开始)"}),
                "缓存目录": ("STRING", {"default": "", "multiline": False,
                                    "tooltip": "下载文件保存目录。留空=ComfyUI 的 output/fx_video。"
                                               "也可接 Save Video 另存到其他路径。"}),
            },
        }

    RETURN_TYPES = ("VIDEO", "AUDIO", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("video", "audio", "标题", "文件路径", "信息JSON", "预览")
    FUNCTION = "run"
    CATEGORY = "FX Crawler"

    @classmethod
    def IS_CHANGED(cls, **kw):
        # 只要链接/画质/缓存开关没变就复用上次结果，别每次执行都重新下
        sig = "|".join(str(kw.get(k, "")) for k in
                       ("视频链接", "画质", "缓存复用", "合集第几个", "缓存目录", "cookie来源"))
        return hashlib.sha1(sig.encode("utf-8")).hexdigest()

    def run(self, **kw):
        url = normalize_url(kw.get("视频链接", ""))
        if not url:
            raise RuntimeError("视频链接是空的。")
        site, domain = detect_site(url)
        quality = kw.get("画质", "1080p")
        out_dir = str(kw.get("缓存目录", "") or "")
        work = _cache_dir(out_dir)

        cookiefile, from_browser = _resolve_cookies(
            kw.get("cookie来源", "不用"), kw.get("cookie", ""),
            kw.get("cookies文件", ""), domain, work,
        )

        pbar = ProgressBar(100) if ProgressBar is not None else None
        logger.info("[fx_video] %s ← %s (%s)", site, url, quality)
        path, info = download(
            url, quality=quality, out_dir=out_dir, reuse=bool(kw.get("缓存复用", True)),
            cookiefile=cookiefile, cookiesfrombrowser=from_browser,
            proxy=kw.get("代理", ""), playlist_index=int(kw.get("合集第几个", 0) or 0),
            pbar=pbar, cookie_text=kw.get("cookie", ""),
        )

        video = _make_video(path)
        audio = _decode_audio(path)
        return (
            video,
            audio,
            str(info.get("title") or ""),
            path,
            json.dumps(info, ensure_ascii=False),
            _summary(path, info, site),
        )


class FXVideoProbe:
    """只解析不下载：看看标题/时长/有哪些清晰度可选。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "视频链接": ("STRING", {"default": "", "multiline": True}),
            },
            "optional": {
                "cookie来源": (_COOKIE_SRC, {"default": "不用"}),
                "cookie": ("STRING", {"default": "", "multiline": True}),
                "cookies文件": ("STRING", {"default": "", "multiline": False}),
                "代理": ("STRING", {"default": "", "multiline": False}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "INT", "STRING", "STRING")
    RETURN_NAMES = ("标题", "作者", "时长秒", "可选清晰度", "信息JSON")
    FUNCTION = "run"
    CATEGORY = "FX Crawler"
    OUTPUT_NODE = True

    def run(self, **kw):
        url = normalize_url(kw.get("视频链接", ""))
        if not url:
            raise RuntimeError("视频链接是空的。")
        _, domain = detect_site(url)
        cookiefile, from_browser = _resolve_cookies(
            kw.get("cookie来源", "不用"), kw.get("cookie", ""),
            kw.get("cookies文件", ""), domain, _cache_dir(""),
        )
        info = probe(
            url, cookiefile, from_browser, kw.get("代理", ""),
            cookie_text=kw.get("cookie", ""),
        )

        seen, lines = set(), []
        for f in (info.get("formats") or []):
            h = f.get("height")
            if not h or h in seen:
                continue
            seen.add(h)
            lines.append(f"{h}p  {f.get('ext', '?')}  {f.get('format_note') or f.get('format_id')}")
        lines.sort(key=lambda s: int(re.match(r"(\d+)", s).group(1)), reverse=True)

        slim = {k: info.get(k) for k in (
            "id", "title", "uploader", "duration", "width", "height", "webpage_url",
            "view_count", "like_count", "upload_date", "description",
        ) if info.get(k) is not None}
        return (
            str(info.get("title") or ""),
            str(info.get("uploader") or ""),
            int(info.get("duration") or 0),
            "\n".join(lines) or "(没拿到清晰度列表)",
            json.dumps(slim, ensure_ascii=False),
        )


class FXVideoPickURL:
    """把采集节点的 contents_json 里第 N 条的链接抠出来，接到下载节点。"""

    _URL_KEYS = _CONTENT_URL_KEYS

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "contents_json": ("STRING", {"default": "", "multiline": True, "forceInput": True}),
                "第几条": ("INT", {"default": 1, "min": 1, "max": 100000,
                                "tooltip": "从 1 开始"}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "INT")
    RETURN_NAMES = ("链接", "标题", "总条数")
    FUNCTION = "run"
    CATEGORY = "FX Crawler"

    def run(self, contents_json, 第几条):
        try:
            items = json.loads(contents_json or "[]")
        except json.JSONDecodeError as e:
            raise RuntimeError(f"contents_json 不是合法 JSON：{e}") from e
        if isinstance(items, dict):
            items = items.get("contents") or [items]
        if not items:
            raise RuntimeError("contents_json 是空的，先跑一次采集节点。")

        idx = max(1, int(第几条)) - 1
        if idx >= len(items):
            raise RuntimeError(f"只有 {len(items)} 条，取不到第 {第几条} 条。")
        item = items[idx] or {}

        url = ""
        for k in self._URL_KEYS:
            v = str(item.get(k) or "").strip()
            if v.startswith("http"):
                url = v
                break
        if not url:
            raise RuntimeError(f"第 {第几条} 条里没有可用链接，字段有：{list(item.keys())[:12]}")

        title = str(item.get("title") or item.get("desc") or item.get("content_text") or "")
        return (url, title, len(items))


def _scalar_int(value, default=0):
    if isinstance(value, (list, tuple)):
        value = value[0] if value else default
    try:
        return int(value if value is not None and value != "" else default)
    except (TypeError, ValueError):
        return int(default)


def _scalar_str(value, default=""):
    if isinstance(value, (list, tuple)):
        value = value[0] if value else default
    return str(value if value is not None else default)


class FXVideoPickFromList:
    """从批量下载的 videos 列表里取第 N 条（从 1 开始）。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "videos": ("VIDEO", {"forceInput": True,
                                     "tooltip": "接「批量下载视频(用户)」的 videos 输出"}),
                "第几条": ("INT", {"default": 1, "min": 1, "max": 100000,
                                "tooltip": "从 1 开始；只计成功下载的条目（跳过的失败不计入）"}),
            },
            "optional": {
                "信息JSON": ("STRING", {"default": "", "forceInput": True,
                                        "tooltip": "可选：接批量节点的「信息JSON」，预览里显示标题"}),
            },
        }

    INPUT_IS_LIST = True
    RETURN_TYPES = ("VIDEO", "INT", "STRING")
    RETURN_NAMES = ("video", "总条数", "预览")
    FUNCTION = "run"
    CATEGORY = "FX Crawler"

    @classmethod
    def IS_CHANGED(cls, **kw):
        sig = f"{_scalar_int(kw.get('第几条'), 1)}|{_scalar_str(kw.get('信息JSON'), '')}"
        return hashlib.sha1(sig.encode("utf-8")).hexdigest()

    def run(self, videos, 第几条, 信息JSON=""):
        第几条 = _scalar_int(第几条, 1)
        信息JSON = _scalar_str(信息JSON, "")
        items = [v for v in (videos or []) if v is not None]
        if not items:
            raise RuntimeError("视频列表是空的，请先跑批量下载节点。")

        total = len(items)
        idx = max(1, 第几条) - 1
        if idx >= total:
            raise RuntimeError(f"成功下载 {total} 条，取不到第 {第几条} 条。")

        video = items[idx]
        title = _pick_title_from_info_json(信息JSON, idx)
        preview = f"第 {第几条}/{total} 条"
        if title:
            preview += f" · {title[:50]}"
        return (video, total, preview)


class FXVideoPickFromPaths:
    """从批量下载的「文件路径JSON」里取第 N 条，输出 VIDEO。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "文件路径JSON": ("STRING", {"default": "", "forceInput": True,
                                         "tooltip": "接「批量下载视频(用户)」的文件路径JSON"}),
                "第几条": ("INT", {"default": 1, "min": 1, "max": 100000,
                                "tooltip": "从 1 开始"}),
            },
            "optional": {
                "信息JSON": ("STRING", {"default": "", "forceInput": True}),
            },
        }

    RETURN_TYPES = ("VIDEO", "STRING", "STRING", "INT")
    RETURN_NAMES = ("video", "文件路径", "标题", "总条数")
    FUNCTION = "run"
    CATEGORY = "FX Crawler"

    def run(self, 文件路径JSON, 第几条, 信息JSON=""):
        第几条 = _scalar_int(第几条, 1)
        信息JSON = _scalar_str(信息JSON, "")
        文件路径JSON = _scalar_str(文件路径JSON, "")
        try:
            paths = _parse_paths_json(文件路径JSON).get("videos") or []
        except json.JSONDecodeError as e:
            raise RuntimeError(f"文件路径JSON 不是合法 JSON：{e}") from e
        if not paths:
            raise RuntimeError("文件路径JSON 里没有视频路径。")

        total = len(paths)
        idx = max(1, 第几条) - 1
        if idx >= total:
            raise RuntimeError(f"只有 {total} 条，取不到第 {第几条} 条。")

        path = paths[idx]
        if not os.path.isfile(path):
            raise RuntimeError(f"文件不存在：{path}")

        title = _pick_title_from_info_json(信息JSON, idx)
        return (_make_video(path), path, title, total)


class FXImagePickFromList:
    """从批量下载的 images 列表里取第 N 条（从 1 开始）。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE", {"forceInput": True,
                                     "tooltip": "接「批量下载视频(用户)」的 images 输出"}),
                "第几条": ("INT", {"default": 1, "min": 1, "max": 100000,
                                "tooltip": "从 1 开始；每条对应一张图片"}),
            },
            "optional": {
                "信息JSON": ("STRING", {"default": "", "forceInput": True}),
            },
        }

    INPUT_IS_LIST = True
    RETURN_TYPES = ("IMAGE", "INT", "STRING")
    RETURN_NAMES = ("image", "总条数", "预览")
    FUNCTION = "run"
    CATEGORY = "FX Crawler"

    def run(self, images, 第几条, 信息JSON=""):
        第几条 = _scalar_int(第几条, 1)
        items = [v for v in (images or []) if v is not None]
        if not items:
            raise RuntimeError("图片列表是空的，请先跑批量下载节点（作品类型含图文）。")

        total = len(items)
        idx = max(1, 第几条) - 1
        if idx >= total:
            raise RuntimeError(f"共 {total} 张图片，取不到第 {第几条} 条。")

        preview = f"第 {第几条}/{total} 张"
        return (items[idx], total, preview)


class FXImagePickFromPaths:
    """从批量下载的「文件路径JSON」里取第 N 张图片，输出 IMAGE。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "文件路径JSON": ("STRING", {"default": "", "forceInput": True,
                                         "tooltip": "接「批量下载视频(用户)」的文件路径JSON"}),
                "第几条": ("INT", {"default": 1, "min": 1, "max": 100000,
                                "tooltip": "从 1 开始；在 images 数组里计数"}),
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING", "INT")
    RETURN_NAMES = ("image", "文件路径", "总条数")
    FUNCTION = "run"
    CATEGORY = "FX Crawler"

    def run(self, 文件路径JSON, 第几条):
        第几条 = _scalar_int(第几条, 1)
        文件路径JSON = _scalar_str(文件路径JSON, "")
        try:
            paths = _parse_paths_json(文件路径JSON).get("images") or []
        except json.JSONDecodeError as e:
            raise RuntimeError(f"文件路径JSON 不是合法 JSON：{e}") from e
        if not paths:
            raise RuntimeError("文件路径JSON 里没有图片路径。")

        total = len(paths)
        idx = max(1, 第几条) - 1
        if idx >= total:
            raise RuntimeError(f"只有 {total} 张图片，取不到第 {第几条} 条。")

        path = paths[idx]
        if not os.path.isfile(path):
            raise RuntimeError(f"文件不存在：{path}")
        return (_make_image(path), path, total)


def _pick_title_from_info_json(info_json: str, index: int) -> str:
    raw = str(info_json or "").strip()
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    items = data.get("ok") if isinstance(data, dict) and "ok" in data else data
    if not isinstance(items, list) or index >= len(items):
        return ""
    item = items[index] or {}
    return str(item.get("title") or item.get("desc") or "")


def _download_user_works_until(
    target: int,
    platform: str,
    user_lines: list,
    list_mode: str,
    *,
    content_mode: str = "video",
    browser_cdp: bool = True,
    headless: bool = False,
    cookiefile=None,
    cookiesfrombrowser=None,
    proxy: str = "",
    quality: str = "1080p",
    out_dir: str = "",
    reuse: bool = True,
    cookie_text: str = "",
    node_id=None,
):
    """拉列表并下载，直到成功作品数达到 target 或主页列表耗尽。"""
    target = max(1, int(target))
    max_fetch = 500
    ok_video_paths: list = []
    ok_image_paths: list = []
    ok_infos: list = []
    attempts: list = []
    seen_urls: set = set()
    fetch_limit = target
    list_exhausted = False

    def _success_count() -> int:
        return len(ok_infos)

    _send_progress_text(node_id, f"目标成功 {target} 条，失败自动补下…")
    logger.info("[fx_video] 按用户下载，目标成功 %d 条（%s / %s）",
                target, _USER_DL_PLATFORM_NAMES.get(platform, platform), content_mode)
    pbar = ProgressBar(target, node_id=node_id) if ProgressBar and target else None

    while _success_count() < target and not list_exhausted:
        pairs = _normalize_list_items(_collect_user_download_urls(
            platform, user_lines, fetch_limit,
            content_mode=content_mode, list_mode=list_mode,
            browser_cdp=browser_cdp, headless=headless,
            cookiefile=cookiefile, cookiesfrombrowser=cookiesfrombrowser,
            proxy=proxy,
        ))
        if len(pairs) < fetch_limit:
            list_exhausted = True

        new_pairs = [p for p in pairs if p[0] not in seen_urls]
        if not new_pairs:
            break

        for url, title, kind in new_pairs:
            seen_urls.add(url)
            if _success_count() >= target:
                break

            n_ok = _success_count() + 1
            t = str(title or "").replace("\n", " ")[:45]
            kind_label = "图文" if kind == "image" else "视频"
            _send_progress_text(
                node_id,
                f"下载 {n_ok}/{target} · {kind_label} · {t}" if t
                else f"下载 {n_ok}/{target} · {kind_label}",
            )
            logger.info("[fx_video] 用户批量 %d/%d (%s) ← %s", n_ok, target, kind_label, url)
            try:
                if kind == "image":
                    paths, info = download_images(
                        url, out_dir=out_dir, reuse=reuse,
                        cookiefile=cookiefile, cookiesfrombrowser=cookiesfrombrowser,
                        proxy=proxy,
                    )
                    info = dict(info or {})
                    info.setdefault("kind", "image")
                    info.setdefault("title", title)
                    attempts.append((url, title, kind, paths, info))
                    if paths:
                        ok_image_paths.extend(paths)
                        ok_infos.append(info)
                else:
                    path, info = download(
                        url, quality=quality, out_dir=out_dir, reuse=reuse,
                        cookiefile=cookiefile, cookiesfrombrowser=cookiesfrombrowser,
                        proxy=proxy, cookie_text=cookie_text,
                    )
                    info = dict(info or {})
                    info.setdefault("kind", "video")
                    attempts.append((url, title, kind, path, info))
                    if path:
                        ok_video_paths.append(path)
                        ok_infos.append(info)
                _send_progress_text(node_id, f"已成功 {_success_count()}/{target}")
                if pbar is not None:
                    pbar.update_absolute(_success_count())
            except Exception as e:
                err = str(e).strip() or e.__class__.__name__
                logger.warning("[fx_video] 跳过: %s", err.split("\n")[0])
                info = {"error": err, "url": url, "title": title, "kind": kind}
                attempts.append((url, title, kind, None, info))
                short = err.replace("\n", " ")[:70]
                _send_progress_text(
                    node_id,
                    f"跳过 · {short}（已成功 {_success_count()}/{target}）",
                )

        if _success_count() >= target:
            break
        if list_exhausted:
            break
        need = target - _success_count()
        fetch_limit = min(max_fetch, max(fetch_limit + 1, len(seen_urls) + need * 3))

    if _success_count() >= target:
        _send_progress_text(node_id, f"完成 {target}/{target}")
        if pbar is not None:
            pbar.update_absolute(target)
    else:
        _send_progress_text(node_id, f"仅成功 {_success_count()}/{target}")
        if pbar is not None and _success_count():
            pbar.update_absolute(_success_count())

    return ok_video_paths, ok_image_paths, ok_infos, attempts


class FXVideoDownloadByUser:
    """用户主页 → 拉作品列表 → 批量下载为本地视频/图文文件。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "用户主页": ("STRING", {"default": "", "multiline": True,
                                    "tooltip": "创作者主页 URL 或 ID，一行一个。\n"
                                               "支持：抖音 / 小红书 / B站"}),
                "平台": (["自动识别"] + list(_USER_DL_PLATFORMS.keys()),
                        {"default": "自动识别",
                         "tooltip": "自动识别失败时请手动选择"}),
                "作品类型": (_CONTENT_MODE_UI, {"default": "仅视频",
                                           "tooltip": "仅视频=只要视频；仅图文=只要图文笔记；"
                                                      "全部=两种都要。图文下载目前完整支持抖音。"}),
                "条数上限": ("INT", {"default": 10, "min": 1, "max": 500,
                                  "tooltip": "目标成功下载条数（1条作品=1条，图文可能含多张图）；"
                                             "单条失败会从列表继续补下，直到凑满或主页无更多作品"}),
                "拉列表方式": (["不开浏览器(推荐)", "Playwright(备用)"],
                           {"default": "不开浏览器(推荐)",
                            "tooltip": "默认：yt-dlp → 抖音/小红书 HTTP API（需 cookie），全程不启 Playwright。\n"
                                       "备用：Playwright 采集拉列表（会开浏览器）。"}),
                "画质": (list(_QUALITY.keys()), {"default": "1080p"}),
                "缓存复用": ("BOOLEAN", {"default": True, "label_on": "复用", "label_off": "重下"}),
                "高级选项": ("BOOLEAN", {"default": False, "label_on": "展开", "label_off": "收起"}),
            },
            "optional": {
                "cookie来源": (_COOKIE_SRC, {"default": "fx_crawler浏览器",
                                          "tooltip": "拉列表和下载都建议带登录态。"
                                                     "默认复用 fx_crawler 采集节点登录过的 Chrome"}),
                "cookie": ("STRING", {"default": "", "multiline": True}),
                "cookies文件": ("STRING", {"default": "", "multiline": False}),
                "代理": ("STRING", {"default": "", "multiline": False}),
                "缓存目录": ("STRING", {"default": "", "multiline": False,
                                    "tooltip": "留空=ComfyUI 的 output/fx_video"}),
                "浏览器模式": (["连我的Chrome", "独立浏览器"], {"default": "连我的Chrome",
                                                    "tooltip": "仅「Playwright」拉列表时生效"}),
                "无头运行": ("BOOLEAN", {"default": False, "label_on": "无头", "label_off": "有界面"}),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("VIDEO", "IMAGE", "STRING", "STRING", "INT", "STRING", "STRING")
    RETURN_NAMES = ("videos", "images", "文件路径JSON", "信息JSON", "条数", "目录", "预览")
    OUTPUT_IS_LIST = (True, True, False, False, False, False, False)
    FUNCTION = "run"
    CATEGORY = "FX Crawler"

    @classmethod
    def IS_CHANGED(cls, **kw):
        sig = "|".join(str(kw.get(k, "")) for k in (
            "用户主页", "平台", "作品类型", "条数上限", "拉列表方式", "画质", "缓存复用", "缓存目录",
            "cookie来源", "浏览器模式",
        ))
        return hashlib.sha1(sig.encode("utf-8")).hexdigest()

    def run(self, **kw):
        user_lines = _split_user_lines(kw.get("用户主页", ""))
        if not user_lines:
            raise RuntimeError("用户主页是空的。")

        platform_ui = kw.get("平台", "自动识别")
        if platform_ui == "自动识别":
            platforms = {detect_user_platform(u) for u in user_lines}
            if len(platforms) != 1:
                raise RuntimeError(
                    "多个用户主页属于不同平台，请分开跑，或手动选择平台。"
                    f"识别到：{', '.join(_USER_DL_PLATFORM_NAMES.get(p, p) for p in platforms)}"
                )
            platform = platforms.pop()
        else:
            platform = _USER_DL_PLATFORMS[platform_ui]

        target_count = int(kw.get("条数上限", 10) or 10)
        content_mode = _content_mode_key(kw.get("作品类型", "仅视频"))
        quality = kw.get("画质", "1080p")
        out_dir = str(kw.get("缓存目录", "") or "")
        work = _cache_dir(out_dir)
        browser_cdp = kw.get("浏览器模式", "连我的Chrome") == "连我的Chrome"
        headless = bool(kw.get("无头运行", False))
        node_id = kw.get("unique_id")
        list_mode_ui = kw.get("拉列表方式", "不开浏览器(推荐)")
        list_mode = "playwright" if "Playwright" in list_mode_ui else "ytdlp"

        list_url = _normalize_user_page_url(platform, user_lines[0])
        _, domain = detect_site(list_url)
        cookiefile, from_browser = _resolve_cookies(
            kw.get("cookie来源", "fx_crawler浏览器"), kw.get("cookie", ""),
            kw.get("cookies文件", ""), domain, work,
        )

        if list_mode == "playwright":
            _send_progress_text(node_id, "Playwright 拉取用户作品列表…")
        else:
            _send_progress_text(node_id, "拉取作品列表（yt-dlp / API，不开浏览器）…")

        ok_video_paths, ok_image_paths, ok_infos, attempts = _download_user_works_until(
            target_count, platform, user_lines, list_mode,
            content_mode=content_mode,
            browser_cdp=browser_cdp, headless=headless,
            cookiefile=cookiefile, cookiesfrombrowser=from_browser,
            proxy=kw.get("代理", ""),
            quality=quality, out_dir=out_dir,
            reuse=bool(kw.get("缓存复用", True)),
            cookie_text=kw.get("cookie", ""),
            node_id=node_id,
        )

        ok_count = len(ok_infos)
        if ok_count == 0:
            if not attempts:
                raise RuntimeError(
                    f"没有在用户主页下找到可下载的作品（平台={_USER_DL_PLATFORM_NAMES.get(platform)}）。\n"
                    "请检查链接是否正确、是否需登录，或调整「作品类型」。"
                )
            failed = [a for a in attempts if a[3] is None]
            samples = [
                str(a[4].get("error") or "未知错误").replace("\n", " ")[:120]
                for a in failed[:3]
            ]
            raise RuntimeError(
                f"未能成功下载任何作品（目标 {target_count} 条，尝试 {len(attempts)} 条）。\n"
                + "\n".join(f"  · {s}" for s in samples)
                + ("\n  …" if len(failed) > 3 else "")
                + "\n请检查链接、cookie，或调整「作品类型」。"
            )

        if ok_count < target_count:
            raise RuntimeError(
                f"目标成功 {target_count} 条，实际仅 {ok_count} 条。"
                f"已尝试 {len(attempts)} 条，用户主页已无更多作品可补。"
            )

        lines = [
            f"[{_USER_DL_PLATFORM_NAMES.get(platform, platform)}] "
            f"成功下载 {ok_count}/{target_count} 条作品",
            f"视频 {len(ok_video_paths)} · 图片 {len(ok_image_paths)} 张",
            f"用户：{user_lines[0]}" + (f" 等 {len(user_lines)} 个" if len(user_lines) > 1 else ""),
            "",
        ]
        video_idx = 0
        for i, info in enumerate(ok_infos, 1):
            t = str(info.get("title") or "(无标题)").replace("\n", " ")[:50]
            kind = info.get("kind") or "video"
            if kind == "image":
                nimg = info.get("image_count") or len(info.get("image_paths") or [])
                lines.append(f"{i}. ✓ [图文·{nimg}张] {t}")
                for p in (info.get("image_paths") or [])[:3]:
                    lines.append(f"   {os.path.basename(p)}")
                if nimg > 3:
                    lines.append(f"   …共 {nimg} 张")
            else:
                lines.append(f"{i}. ✓ [视频] {t}")
                if video_idx < len(ok_video_paths):
                    lines.append(f"   {os.path.basename(ok_video_paths[video_idx])}")
                    video_idx += 1
        skipped = len(attempts) - ok_count
        if skipped:
            lines.append("")
            lines.append(f"（共尝试 {len(attempts)} 条，跳过 {skipped} 条失败）")
        preview = "\n".join(lines)
        videos = [_make_video(p) for p in ok_video_paths]
        images = [_make_image(p) for p in ok_image_paths]
        paths_payload = {"videos": ok_video_paths, "images": ok_image_paths}
        all_infos = [a[4] for a in attempts]

        return (
            videos,
            images,
            json.dumps(paths_payload, ensure_ascii=False),
            json.dumps({"ok": ok_infos, "all": all_infos}, ensure_ascii=False, indent=2),
            ok_count,
            work,
            preview,
        )


try:
    from browser_cookies import COOKIE_PLATFORM_OPTIONS as _COOKIE_PLATFORMS
except ImportError:
    _COOKIE_PLATFORMS = ["自动(已打开标签)"] + list(_USER_DL_PLATFORMS.keys())
_COOKIE_SOURCES = ["本机Chrome(推荐)", "连我的Chrome", "fx_crawler浏览器"]


class FXGetBrowserCookie:
    """从已打开 Chrome 或本机浏览器配置读取自媒体 Cookie，供下载节点使用。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "来源": (_COOKIE_SOURCES, {
                    "default": "本机Chrome(推荐)",
                    "tooltip": "本机Chrome=直接读系统 Chrome 登录态（最简单，推荐）；"
                               "连我的Chrome=通过远程调试读当前标签（Chrome 136+ 需在浏览器点「允许」）；"
                               "fx_crawler浏览器=插件采集时保存的登录目录",
                }),
                "平台": (_COOKIE_PLATFORMS, {
                    "default": "自动(已打开标签)",
                    "tooltip": "自动=根据 Chrome 已打开标签识别平台；"
                               "也可手动指定单个平台。"
                               "已支持：抖音/小红书/B站/快手/微博/视频号/西瓜/AcFun/"
                               "好看/微视/YouTube/TikTok/X/Instagram/Vimeo/Twitch",
                }),
            },
            "optional": {
                "CDP端口": ("INT", {
                    "default": 9222, "min": 1024, "max": 65535,
                    "tooltip": "仅「连我的Chrome」时有效。Chrome 远程调试端口，默认 9222",
                }),
                "缓存目录": ("STRING", {
                    "default": "", "multiline": False,
                    "tooltip": "cookies.txt 保存位置，留空=ComfyUI output/fx_video/.cookies",
                }),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING", "INT", "STRING")
    RETURN_NAMES = ("cookie", "cookies文件", "平台", "条数", "预览")
    FUNCTION = "run"
    CATEGORY = "FX Crawler"

    def run(self, **kw):
        from browser_cookies import extract_browser_cookies

        source = kw.get("来源", "连我的Chrome")
        platform_ui = kw.get("平台", "自动(已打开标签)")
        cdp_port = int(kw.get("CDP端口", 9222) or 9222)
        out_dir = str(kw.get("缓存目录", "") or "")
        work = os.path.join(_cache_dir(out_dir), ".cookies")

        header, cookie_path, platform_label, preview = extract_browser_cookies(
            source=source,
            platform_ui=platform_ui,
            cdp_port=cdp_port,
            fx_profile=_fx_browser_profile(),
            cache_dir=work,
        )

        count = len([p for p in header.split("; ") if "=" in p]) if header else 0
        return (header, cookie_path, platform_label, count, preview)


NODE_CLASS_MAPPINGS = {
    "fx_video_download": FXVideoDownload,
    "fx_video_probe": FXVideoProbe,
    "fx_video_pick_url": FXVideoPickURL,
    "fx_video_pick_from_list": FXVideoPickFromList,
    "fx_video_pick_from_paths": FXVideoPickFromPaths,
    "fx_image_pick_from_list": FXImagePickFromList,
    "fx_image_pick_from_paths": FXImagePickFromPaths,
    "fx_video_download_by_user": FXVideoDownloadByUser,
    "fx_browser_cookie": FXGetBrowserCookie,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "fx_video_download": "下载视频(URL)",
    "fx_video_probe": "解析视频信息(URL)",
    "fx_video_pick_url": "从采集结果取链接",
    "fx_video_pick_from_list": "从视频列表取一条",
    "fx_video_pick_from_paths": "从路径JSON取视频",
    "fx_image_pick_from_list": "从图片列表取一张",
    "fx_image_pick_from_paths": "从路径JSON取图片",
    "fx_video_download_by_user": "批量下载视频(用户)",
    "fx_browser_cookie": "获取浏览器Cookie",
}
