# -*- coding: utf-8 -*-
"""从已打开浏览器 / Chrome 配置读取自媒体平台 Cookie。"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import httpx

from async_util import run_sync

logger = logging.getLogger("fx_crawler.video")

# tab 关键词 → 平台名；domains 用于过滤 cookie（与 video_nodes._SITES 对齐）
PLATFORM_SPECS: Dict[str, dict] = {
    "抖音": {
        "tab_keys": ("douyin.com", "iesdouyin.com"),
        "domains": ("douyin.com", "iesdouyin.com"),
        "netscape_domain": ".douyin.com",
    },
    "小红书": {
        "tab_keys": ("xiaohongshu.com", "xhslink.com"),
        "domains": ("xiaohongshu.com",),
        "netscape_domain": ".xiaohongshu.com",
    },
    "B站": {
        "tab_keys": ("bilibili.com", "b23.tv", "space.bilibili"),
        "domains": ("bilibili.com", "b23.tv"),
        "netscape_domain": ".bilibili.com",
    },
    "快手": {
        "tab_keys": ("kuaishou.com",),
        "domains": ("kuaishou.com",),
        "netscape_domain": ".kuaishou.com",
    },
    "微博": {
        "tab_keys": ("weibo.com", "weibo.cn"),
        "domains": ("weibo.com", "weibo.cn"),
        "netscape_domain": ".weibo.com",
    },
    "视频号": {
        "tab_keys": ("channels.weixin.qq.com", "weixin.qq.com/sph", "yuanbao.tencent.com"),
        "domains": ("yuanbao.tencent.com", "channels.weixin.qq.com", "weixin.qq.com", "tencent.com"),
        "netscape_domain": ".tencent.com",
    },
    "西瓜": {
        "tab_keys": ("ixigua.com",),
        "domains": ("ixigua.com",),
        "netscape_domain": ".ixigua.com",
    },
    "AcFun": {
        "tab_keys": ("acfun.cn",),
        "domains": ("acfun.cn",),
        "netscape_domain": ".acfun.cn",
    },
    "好看": {
        "tab_keys": ("haokan.baidu.com",),
        "domains": ("haokan.baidu.com",),
        "netscape_domain": ".baidu.com",
    },
    "微视": {
        "tab_keys": ("weishi.qq.com",),
        "domains": ("weishi.qq.com",),
        "netscape_domain": ".qq.com",
    },
    "YouTube": {
        "tab_keys": ("youtube.com", "youtu.be"),
        "domains": ("youtube.com", "youtu.be"),
        "netscape_domain": ".youtube.com",
    },
    "TikTok": {
        "tab_keys": ("tiktok.com",),
        "domains": ("tiktok.com",),
        "netscape_domain": ".tiktok.com",
    },
    "X": {
        "tab_keys": ("twitter.com", "x.com"),
        "domains": ("twitter.com", "x.com"),
        "netscape_domain": ".x.com",
    },
    "Instagram": {
        "tab_keys": ("instagram.com",),
        "domains": ("instagram.com",),
        "netscape_domain": ".instagram.com",
    },
    "Vimeo": {
        "tab_keys": ("vimeo.com",),
        "domains": ("vimeo.com",),
        "netscape_domain": ".vimeo.com",
    },
    "Twitch": {
        "tab_keys": ("twitch.tv",),
        "domains": ("twitch.tv",),
        "netscape_domain": ".twitch.tv",
    },
}

# 节点下拉选项（自动 + 全部已支持平台）
COOKIE_PLATFORM_OPTIONS = ["自动(已打开标签)"] + list(PLATFORM_SPECS.keys())

DEFAULT_CDP_PORT = 9222
CDP_CONNECT_TIMEOUT_MS = 60000


@dataclass
class CdpEndpoint:
    port: int
    ws_path: str
    source: str = ""

    @property
    def ws_url(self) -> str:
        path = self.ws_path if self.ws_path.startswith("/") else f"/{self.ws_path}"
        return f"ws://127.0.0.1:{self.port}{path}"


def _devtools_active_port_paths() -> List[str]:
    home = os.path.expanduser("~")
    paths = []
    if sys.platform == "darwin":
        roots = (
            "Google/Chrome",
            "Google/Chrome Beta",
            "Google/Chrome Dev",
            "Google/Chrome Canary",
            "Microsoft Edge",
        )
        for name in roots:
            paths.append(os.path.join(home, "Library/Application Support", name, "DevToolsActivePort"))
    elif sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        for name in ("Google/Chrome", "Google/Chrome Beta", "Microsoft/Edge"):
            paths.append(os.path.join(local, name, "User Data", "DevToolsActivePort"))
    else:
        for name in ("google-chrome", "chromium", "microsoft-edge"):
            paths.append(os.path.join(home, f".config/{name}", "DevToolsActivePort"))
    return paths


def discover_chrome_cdp(port_hint: int = DEFAULT_CDP_PORT) -> CdpEndpoint:
    """
    发现 Chrome CDP 连接信息。

    Chrome 136+ 通过 chrome://inspect 开启远程调试时：
    - /json 会 404（正常）
    - 端口与 ws 路径写在 DevToolsActivePort 文件里
    """
    newest: Optional[Tuple[float, CdpEndpoint]] = None
    for path in _devtools_active_port_paths():
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = [ln.strip() for ln in f.read().splitlines() if ln.strip()]
            if len(lines) < 2:
                continue
            port = int(lines[0])
            ws_path = lines[1]
            if not ws_path.startswith("/"):
                ws_path = "/" + ws_path
            ep = CdpEndpoint(port=port, ws_path=ws_path, source=path)
            mtime = os.path.getmtime(path)
            if newest is None or mtime > newest[0]:
                newest = (mtime, ep)
        except Exception as e:
            logger.debug("[fx_cookie] 读取 DevToolsActivePort 失败 %s: %s", path, e)

    if newest:
        return newest[1]

    hint = int(port_hint or DEFAULT_CDP_PORT)
    if _cdp_port_open(hint):
        # 旧版 Chrome：--remote-debugging-port + 独立 user-data-dir
        try:
            with httpx.Client(timeout=3.0) as client:
                resp = client.get(f"http://127.0.0.1:{hint}/json/version")
                if resp.status_code == 200:
                    ws_url = resp.json().get("webSocketDebuggerUrl") or ""
                    if ws_url.startswith("ws://"):
                        path = ws_url.split(f":{hint}", 1)[-1]
                        return CdpEndpoint(port=hint, ws_path=path, source="/json/version")
        except Exception:
            pass
        return CdpEndpoint(port=hint, ws_path="/devtools/browser", source=f"port:{hint}")

    raise RuntimeError(
        "未找到可用的 Chrome 远程调试连接。\n\n"
        "Chrome 136+ 请按下面做（用你的日常 Chrome 配置即可）：\n"
        "  1. 打开 Chrome，地址栏输入 chrome://inspect/#remote-debugging\n"
        "  2. 开启「允许远程调试」\n"
        "  3. 若弹出授权框，点「允许」\n"
        "  4. 保持 Chrome 不要关，再跑本节点\n\n"
        "若仍失败，可改用来源「本机Chrome」（不用远程调试，但需先完全退出 Chrome）。\n"
        "或来源「fx_crawler浏览器」（先用采集节点登录一次）。"
    )


def _domain_matches(cookie_domain: str, filters: Sequence[str]) -> bool:
    dom = str(cookie_domain or "").lstrip(".").lower()
    if not dom:
        return False
    for f in filters:
        key = str(f or "").lstrip(".").lower()
        if not key:
            continue
        if dom == key or dom.endswith("." + key) or key in dom:
            return True
    return False


def detect_platform_from_url(url: str) -> Optional[str]:
    low = str(url or "").lower()
    for name, spec in PLATFORM_SPECS.items():
        if any(k in low for k in spec["tab_keys"]):
            return name
    return None


def _list_cdp_tabs_http(port: int) -> List[dict]:
    with httpx.Client(timeout=5.0) as client:
        for path in ("/json/list", "/json"):
            try:
                resp = client.get(f"http://127.0.0.1:{int(port)}{path}")
                if resp.status_code != 200:
                    continue
                data = resp.json()
                if isinstance(data, list):
                    return [t for t in data if isinstance(t, dict) and t.get("type") == "page"]
            except Exception:
                continue
    return []


async def _list_cdp_tabs_playwright(endpoint: CdpEndpoint) -> List[dict]:
    playwright, browser = await _connect_cdp_browser(endpoint)
    tabs: List[dict] = []
    try:
        for context in browser.contexts:
            for page in context.pages:
                try:
                    title = await page.title()
                except Exception:
                    title = ""
                tabs.append({"type": "page", "url": page.url, "title": title})
    finally:
        try:
            await browser.close()
        except Exception:
            pass
        await playwright.stop()
    return tabs


def list_cdp_tabs(port: int = DEFAULT_CDP_PORT) -> List[dict]:
    """读取 Chrome 远程调试页签列表（兼容 Chrome 136+）。"""
    endpoint = discover_chrome_cdp(port)
    tabs = _list_cdp_tabs_http(endpoint.port)
    if tabs:
        return tabs
    return run_sync(_list_cdp_tabs_playwright(endpoint))


def detect_open_platforms(tabs: List[dict]) -> Dict[str, List[dict]]:
    """从已打开标签推断平台 → 标签列表。"""
    found: Dict[str, List[dict]] = {}
    for tab in tabs:
        url = str(tab.get("url") or "")
        if not url.startswith("http"):
            continue
        name = detect_platform_from_url(url)
        if not name:
            continue
        found.setdefault(name, []).append(tab)
    return found


def _resolve_target_platforms(platform_ui: str, open_map: Dict[str, List[dict]]) -> List[str]:
    text = str(platform_ui or "").strip()
    if text in ("自动(已打开标签)", "自动", ""):
        if not open_map:
            raise RuntimeError(
                "未在已打开标签里识别到支持的自媒体平台。\n"
                "请先在 Chrome 打开并登录抖音/小红书/B站等，或手动选择平台。"
            )
        return list(open_map.keys())
    if text not in PLATFORM_SPECS:
        raise RuntimeError(f"不支持的平台：{text}")
    return [text]


def _domain_filters_for_platforms(platforms: List[str]) -> List[str]:
    keys = []
    for name in platforms:
        spec = PLATFORM_SPECS.get(name) or {}
        keys.extend(spec.get("domains") or ())
    return list(dict.fromkeys(keys))


def _dedupe_cookies(cookies: List[dict]) -> List[dict]:
    out: Dict[Tuple[str, str], dict] = {}
    for c in cookies:
        name = str(c.get("name") or "").strip()
        if not name:
            continue
        dom = str(c.get("domain") or "")
        out[(dom, name)] = c
    return list(out.values())


def cookies_to_header(cookies: List[dict]) -> str:
    parts = []
    for c in _dedupe_cookies(cookies):
        name = c.get("name")
        value = c.get("value")
        if name is None or value is None:
            continue
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def write_netscape_cookies(cookies: List[dict], path: str, default_domain: str = "") -> str:
    lines = ["# Netscape HTTP Cookie File", "# generated by fx_crawler"]
    for c in _dedupe_cookies(cookies):
        name = str(c.get("name") or "").strip()
        value = str(c.get("value") or "")
        if not name:
            continue
        dom = str(c.get("domain") or default_domain or "").strip()
        if dom and not dom.startswith("."):
            dom = "." + dom.lstrip(".")
        if not dom:
            dom = str(default_domain or ".local")
        path_attr = str(c.get("path") or "/")
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = int(c.get("expires") or 2147483647)
        if expires < 0:
            expires = 2147483647
        include = "TRUE" if dom.startswith(".") else "FALSE"
        lines.append(f"{dom}\t{include}\t{path_attr}\t{secure}\t{expires}\t{name}\t{value}")
    if len(lines) <= 2:
        raise RuntimeError("没有可写入的 Cookie。")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)
    return path


async def _connect_cdp_browser(endpoint: CdpEndpoint):
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    last_err: Optional[Exception] = None
    candidates = [endpoint.ws_url]
    if endpoint.ws_path != "/devtools/browser":
        candidates.append(f"ws://127.0.0.1:{endpoint.port}/devtools/browser")

    for ws_url in dict.fromkeys(candidates):
        try:
            browser = await playwright.chromium.connect_over_cdp(
                ws_url, timeout=CDP_CONNECT_TIMEOUT_MS,
            )
            logger.info("[fx_cookie] CDP 已连接 %s (from %s)", ws_url, endpoint.source)
            return playwright, browser
        except Exception as e:
            last_err = e
            logger.warning("[fx_cookie] CDP 连接失败 %s: %s", ws_url, e)

    await playwright.stop()
    msg = str(last_err or "未知错误")
    if "Timeout" in msg or "timeout" in msg.lower():
        raise RuntimeError(
            "Chrome 远程调试连接超时。\n"
            "请在 Chrome 里确认是否弹出「允许远程调试」对话框并点击允许，然后重新执行节点。\n"
            f"（尝试连接 {endpoint.ws_url}）"
        ) from last_err
    raise RuntimeError(
        f"无法通过 CDP 连接 Chrome：{msg}\n"
        f"发现端点：{endpoint.ws_url}（来源 {endpoint.source or '未知'}）\n"
        "请确认 chrome://inspect/#remote-debugging 已开启，或改用来源「本机Chrome」。"
    ) from last_err


async def fetch_cookies_cdp(port: int, domain_filters: Sequence[str]) -> List[dict]:
    if not domain_filters:
        return []
    endpoint = discover_chrome_cdp(port)
    playwright, browser = await _connect_cdp_browser(endpoint)
    collected: List[dict] = []
    try:
        for context in browser.contexts:
            try:
                items = await context.cookies()
            except Exception as e:
                logger.warning("[fx_cookie] 读取 context cookies 失败: %s", e)
                continue
            for c in items or []:
                if _domain_matches(c.get("domain") or "", domain_filters):
                    collected.append(c)
    finally:
        try:
            await browser.close()
        except Exception:
            pass
        await playwright.stop()
    return _dedupe_cookies(collected)


def _normalize_cookie_source(source: str) -> str:
    text = str(source or "").strip()
    if text.startswith("本机Chrome"):
        return "本机Chrome"
    if text.startswith("连我的Chrome"):
        return "连我的Chrome"
    if text.startswith("fx_crawler"):
        return "fx_crawler浏览器"
    return text


def fetch_cookies_profile(
    source: str,
    fx_profile: Optional[str],
    domain_filters: Sequence[str],
) -> List[dict]:
    source = _normalize_cookie_source(source)
    try:
        import yt_dlp
        from yt_dlp.cookies import load_cookies
    except ImportError as e:
        raise RuntimeError("缺少 yt-dlp，无法从 Chrome 配置读取 Cookie。") from e

    if source == "fx_crawler浏览器":
        if not fx_profile:
            raise RuntimeError(
                "未找到 fx_crawler browser_data 登录目录。"
                "请先用采集节点（浏览器模式=连我的Chrome）登录一次。"
            )
        from_browser = ("chrome", fx_profile, None, None)
    elif source == "本机Chrome":
        from_browser = ("chrome", None, None, None)
    else:
        raise RuntimeError(f"不支持的配置来源：{source}")

    class _Log:
        def debug(self, msg):
            logger.debug(msg)

        def info(self, msg):
            logger.debug(msg)

        def warning(self, msg):
            logger.warning("[yt-dlp-cookies] %s", msg)

        def error(self, msg):
            logger.error("[yt-dlp-cookies] %s", msg)

    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "logger": _Log()}) as ydl:
        jar = load_cookies(None, from_browser, ydl)

    out = []
    for c in jar:
        dom = getattr(c, "domain", "") or ""
        if _domain_matches(dom, domain_filters):
            out.append({
                "name": c.name,
                "value": c.value,
                "domain": dom,
                "path": getattr(c, "path", None) or "/",
                "secure": bool(getattr(c, "secure", False)),
                "expires": int(getattr(c, "expires", 2147483647) or 2147483647),
            })
    return _dedupe_cookies(out)


def _cdp_port_open(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.5)
            return s.connect_ex(("127.0.0.1", int(port))) == 0
    except Exception:
        return False


def extract_browser_cookies(
    *,
    source: str,
    platform_ui: str,
    cdp_port: int = DEFAULT_CDP_PORT,
    fx_profile: Optional[str] = None,
    cache_dir: str = "",
) -> Tuple[str, str, str, str]:
    """
    返回 (cookie_header, cookies_file_path, platform_label, preview_text)
    """
    source = _normalize_cookie_source(source)
    open_map: Dict[str, List[dict]] = {}
    tab_lines: List[str] = []

    if source == "连我的Chrome":
        endpoint = discover_chrome_cdp(cdp_port)
        logger.info("[fx_cookie] 使用 CDP 端点 %s", endpoint.ws_url)
        tabs = list_cdp_tabs(cdp_port)
        open_map = detect_open_platforms(tabs)
        for name, items in open_map.items():
            for tab in items[:2]:
                title = str(tab.get("title") or "(无标题)").replace("\n", " ")[:40]
                url = str(tab.get("url") or "")[:80]
                tab_lines.append(f"  · {name} — {title}\n    {url}")
            if len(items) > 2:
                tab_lines.append(f"  · {name} … 另有 {len(items) - 2} 个标签")

    platforms = _resolve_target_platforms(platform_ui, open_map)
    domain_filters = _domain_filters_for_platforms(platforms)
    if not domain_filters:
        raise RuntimeError("未能确定 Cookie 域名过滤条件。")

    if source == "连我的Chrome":
        cookies = run_sync(fetch_cookies_cdp(cdp_port, domain_filters))
    else:
        cookies = fetch_cookies_profile(source, fx_profile, domain_filters)

    if not cookies:
        names = "、".join(platforms)
        raise RuntimeError(
            f"未读取到 {names} 的有效 Cookie。\n"
            "请确认对应平台已在浏览器中登录，且远程调试/配置目录可访问。"
        )

    default_dom = PLATFORM_SPECS.get(platforms[0], {}).get("netscape_domain") or ".local"
    work = cache_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "_cookies")
    os.makedirs(work, exist_ok=True)
    safe = re.sub(r"[^0-9A-Za-z._-]+", "_", "_".join(platforms))
    cookie_path = os.path.join(work, f"fx_cookie_{safe}.txt")
    write_netscape_cookies(cookies, cookie_path, default_domain=default_dom)

    header = cookies_to_header(cookies)
    platform_label = "、".join(platforms)
    per_platform = []
    for name in platforms:
        spec = PLATFORM_SPECS[name]
        n = sum(1 for c in cookies if _domain_matches(c.get("domain") or "", spec["domains"]))
        per_platform.append(f"{name} {n}")

    preview_parts = [
        f"来源：{source}",
        f"平台：{platform_label}",
        f"Cookie 共 {len(cookies)} 条（{' · '.join(per_platform)}）",
    ]
    if tab_lines:
        preview_parts.extend(["", "已打开标签：", *tab_lines])
    preview_parts.extend([
        "",
        "用法：下载节点 cookie来源 选「cookie文本」并接 cookie 输出，",
        "或选「cookies.txt文件」并接 cookies文件 输出。",
    ])
    return header, cookie_path, platform_label, "\n".join(preview_parts)
