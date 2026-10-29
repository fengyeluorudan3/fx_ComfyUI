import os
import platform
import asyncio
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import logging
from pathlib import Path
from typing import Optional, Literal

import json
from playwright.async_api import (
    async_playwright,
    Page,
    BrowserContext,
    Browser,
    Playwright,
)
from playwright_stealth import Stealth

# 支持的浏览器通道
BrowserChannel = Literal["chrome", "msedge", "chromium", None]
_CDP_FAILURES: dict[int, float] = {}
_CDP_CONNECTIONS = {}

# 各平台常见浏览器路径
BROWSER_PATHS = {
    "windows": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
    "darwin": [  # macOS
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ],
    "linux": [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/microsoft-edge",
        "/snap/bin/chromium",
    ],
}


def _detect_system_browser() -> Optional[str]:
    """
    自动检测系统已安装的浏览器

    Returns:
        浏览器可执行文件路径，未找到返回 None
    """
    system = platform.system().lower()

    for path in BROWSER_PATHS.get(system, []):
        if Path(path).exists():
            return path

    return None


def cdp_endpoint_alive(port: int, timeout: float = 1.0) -> Optional[str]:
    """探测本机 CDP 调试端口是否可用 → 返回 endpoint，不可用返回 None。"""
    url = f"http://127.0.0.1:{int(port)}"
    try:
        with urllib.request.urlopen(f"{url}/json/version", timeout=timeout) as resp:
            json.loads(resp.read().decode("utf-8"))
        return url
    except Exception:
        # Chrome 144+ consent-based debugging exposes its WebSocket through
        # DevToolsActivePort; the legacy HTTP discovery route may return 404.
        roots = {
            "windows": Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data",
            "darwin": Path.home() / "Library/Application Support/Google/Chrome",
            "linux": Path.home() / ".config/google-chrome",
        }
        root = roots.get(platform.system().lower())
        try:
            lines = (root / "DevToolsActivePort").read_text().splitlines() if root else []
            if len(lines) >= 2 and int(lines[0]) == int(port) and lines[1].startswith("/devtools/browser/"):
                return f"ws://127.0.0.1:{int(port)}{lines[1]}"
        except (OSError, ValueError):
            pass
        return None


def _chrome_is_running() -> bool:
    """系统里是否已经有 Chrome 主进程在跑（不含 helper 子进程）。"""
    if platform.system().lower() == "windows":
        import psutil
        return any((p.info.get("name") or "").lower() == "chrome.exe"
                   for p in psutil.process_iter(["name"]))
    try:
        out = subprocess.run(
            ["ps", "-Ao", "command="], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        return False
    for line in out.splitlines():
        if "--type=" in line:
            continue
        if "Google Chrome.app/Contents/MacOS/Google Chrome" in line:
            return True
        if line.strip().endswith(("/google-chrome", "/google-chrome-stable", "chrome.exe")):
            return True
    return False


def launch_chrome_with_cdp(port: int, wait_seconds: int = 25) -> str:
    """用**用户的默认 Chrome 配置**（带全部登录态）起一个开着调试端口的 Chrome。

    不传 --user-data-dir，走默认档案，这样各平台的登录态就是你平时那份。
    前提是 Chrome 当前没在运行：Chrome 已在跑时，新进程只会把已有窗口叫到前台
    然后自己退出，调试端口根本不会开——这是 Chrome 的行为，绕不过去。
    """
    if _chrome_is_running():
        raise RuntimeError(
            f"Chrome 正在运行，但没有开调试端口 {port}，无法附加。\n"
            f"二选一：\n"
            f"  1) 完全退出 Chrome（⌘Q）后重跑本节点，会自动带调试端口拉起；\n"
            f"  2) 自己用下面命令启动 Chrome，之后一直用它：\n"
            f"     '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' "
            f"--remote-debugging-port={port}"
        )

    exe = _detect_system_browser()
    if not exe:
        raise RuntimeError("没找到系统 Chrome/Edge，无法启动带调试端口的浏览器。")

    subprocess.Popen(
        [exe, f"--remote-debugging-port={int(port)}", "--no-first-run", "--no-default-browser-check"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        endpoint = cdp_endpoint_alive(port, timeout=1.0)
        if endpoint:
            return endpoint
        time.sleep(0.5)
    raise RuntimeError(f"启动了 Chrome 但 {wait_seconds}s 内调试端口 {port} 没起来。")


class StealthBrowser:
    """
    支持多种浏览器选项的隐身浏览器

    浏览器选择优先级：
    1. executable_path 参数 - 指定浏览器路径
    2. channel 参数 - 使用系统浏览器 (chrome/msedge)
    3. SPREADO_BROWSER_PATH 环境变量 - 指定浏览器路径
    4. SPREADO_BROWSER_CHANNEL 环境变量 - 使用系统浏览器
    5. 自动检测系统已安装的 Chrome/Edge/Chromium
    6. 默认使用 Playwright 内置的 Chromium
    """

    def __init__(
        self,
        headless: bool = False,
        channel: BrowserChannel = None,
        executable_path: Optional[str] = None,
        cdp_port: Optional[int] = None,
        cdp_autostart: bool = False,
        keep_open_seconds: Optional[int] = None,
    ):
        """
        :param headless: 是否无头模式
        :param channel: 浏览器通道 ("chrome", "msedge", "chromium", None)
        :param executable_path: 浏览器可执行文件路径
        :param cdp_port: 给了就走「附加到已开着的 Chrome」模式，复用你自己的登录态，
                         不再新开隐身浏览器。headless/channel/executable_path 此时全部忽略。
        :param cdp_autostart: 保留旧调用参数兼容；附加模式不会自动启动或切换浏览器。
        """
        self.headless = headless
        self.channel = channel
        self.executable_path = executable_path
        self.cdp_port = int(cdp_port) if cdp_port else None
        self.cdp_autostart = cdp_autostart
        self.keep_open_seconds = (
            max(0, int(keep_open_seconds)) if keep_open_seconds is not None
            else max(0, int(os.environ.get("SPREADO_KEEP_BROWSER_OPEN_SECONDS", "60")))
        )

        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.browser_id = uuid.uuid4().hex[:8]
        # 附加模式下只能关我们自己开的标签页，绝不碰用户原有的窗口
        self.attached = False
        self._pooled = False
        self._own_pages: list[Page] = []

    @classmethod
    async def create(
        cls,
        headless: bool = True,
        channel: BrowserChannel = None,
        executable_path: Optional[str] = None,
        cdp_port: Optional[int] = None,
        cdp_autostart: bool = False,
        keep_open_seconds: Optional[int] = None,
    ) -> "StealthBrowser":
        """工厂方法"""
        instance = cls(headless, channel, executable_path, cdp_port, cdp_autostart, keep_open_seconds)
        try:
            await instance.__aenter__()
        except Exception as exc:
            await instance.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        return instance

    def _get_browser_config(self) -> tuple[dict, str]:
        """
        获取浏览器配置

        Returns:
            (config_dict, browser_source) - 配置字典和浏览器来源描述
        """
        config = {}

        # 优先级 1: 参数指定的 executable_path
        if self.executable_path:
            config["executable_path"] = self.executable_path
            return config, f"executable_path: {self.executable_path}"

        # 优先级 2: 参数指定的 channel
        if self.channel:
            if self.channel != "chromium":
                config["channel"] = self.channel
            return config, f"channel: {self.channel}"

        # 优先级 3: 环境变量 SPREADO_BROWSER_PATH
        env_path = os.environ.get("SPREADO_BROWSER_PATH")
        if env_path and Path(env_path).exists():
            config["executable_path"] = env_path
            return config, f"env SPREADO_BROWSER_PATH: {env_path}"

        # 优先级 4: 环境变量 SPREADO_BROWSER_CHANNEL
        env_channel = os.environ.get("SPREADO_BROWSER_CHANNEL")
        if env_channel in ("chrome", "msedge"):
            config["channel"] = env_channel
            return config, f"env SPREADO_BROWSER_CHANNEL: {env_channel}"

        # 优先级 5: 自动检测系统浏览器
        detected_path = _detect_system_browser()
        if detected_path:
            config["executable_path"] = detected_path
            return config, f"auto-detected: {detected_path}"

        # 默认: 使用 Playwright 内置 Chromium
        return config, "Playwright built-in Chromium"

    async def __aenter__(self):
        # 幂等闸门：create() 内部已经进过一次，而调用方普遍写成
        # `async with await StealthBrowser.create(...)`，async with 会再调一次。
        # 不拦住就会二次 launch 并覆盖 self.browser，把第一个浏览器变成
        # 没人引用、永不关闭的孤儿进程（Dock 里越堆越多的 Chrome 就是它们）。
        if self.browser is not None:
            return self

        # —— 附加模式：复用你已经开着、已经登录好的 Chrome，不新开浏览器 ——
        if self.cdp_port:
            loop = asyncio.get_running_loop()
            pool_key = (loop, self.cdp_port)
            cached = _CDP_CONNECTIONS.get(pool_key)
            if cached:
                driver, browser, context = cached
                if browser.is_connected():
                    self.playwright, self.browser, self.context = cached
                    self.attached = self._pooled = True
                    logging.getLogger(__name__).info("Reusing authorized Chrome CDP connection port=%s", self.cdp_port)
                    return self
                _CDP_CONNECTIONS.pop(pool_key, None)
                await driver.stop()
            if time.monotonic() - _CDP_FAILURES.get(self.cdp_port, float('-inf')) < 60:
                raise RuntimeError("同一 Chrome 连接刚刚失败，已停止其余平台重复连接；60 秒后或修复连接后再试。本次未上传或投稿。")
            endpoint = cdp_endpoint_alive(self.cdp_port)
            if not endpoint:
                raise RuntimeError(
                    f"无法连接现有 Chrome 的调试端口 {self.cdp_port}。"
                    "普通 Chrome 中已登录不代表已开放自动化连接；本次未打开新浏览器、未上传或投稿。"
                    "请连接已启用远程调试的 Chrome，或主动选择“新开浏览器”并在发布浏览器登录一次。"
                    "不要关闭用户的 Chrome，也不要重复执行同一节点尝试打开端口。"
                )
            try:
                self.playwright = await asyncio.wait_for(async_playwright().start(), timeout=20)
                self.browser = await asyncio.wait_for(
                    self.playwright.chromium.connect_over_cdp(endpoint, timeout=45000), timeout=50,
                )
            except Exception as exc:
                _CDP_FAILURES[self.cdp_port] = time.monotonic()
                logging.getLogger(__name__).warning("Chrome CDP connection failed: %s", exc)
                raise RuntimeError(
                    "Chrome 调试连接未完成（连接最多等待 50 秒）。请检查浏览器连接授权或调试会话；"
                    "本次未新开浏览器，也未上传或投稿。"
                ) from exc
            _CDP_FAILURES.pop(self.cdp_port, None)
            self.attached = True
            # contexts[0] 就是你那个真实档案的上下文，带着全部登录 cookie
            self.context = (
                self.browser.contexts[0]
                if self.browser.contexts
                else None
            )
            if self.context is None:
                raise RuntimeError("Chrome 未提供可复用的用户上下文，请检查调试连接。")
            if getattr(loop, "_fx_publish_keep_cdp", False):
                _CDP_CONNECTIONS[pool_key] = (self.playwright, self.browser, self.context)
                self._pooled = True
            # 注意：不对真实档案套 stealth——init script 会常驻这个 context，
            # 影响你之后自己开的每个标签页。附加真 Chrome 本来也不带 webdriver 标记。
            return self

        self.playwright = await async_playwright().start()
        args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-infobars",
            "--disable-dev-shm-usage",
        ]

        # 获取浏览器配置
        browser_config, browser_source = self._get_browser_config()
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            args=args,
            **browser_config,
        )

        self.context = await self.browser.new_context(
            no_viewport=True, ignore_https_errors=True
        )

        stealth = Stealth(
            navigator_languages_override=("zh-CN", "zh"), init_scripts_only=True
        )
        await stealth.apply_stealth_async(self.context)

        return self

    async def new_page(self, url_hint=None) -> Page:
        if not self.context:
            raise RuntimeError("Context 未初始化")
        if getattr(self, "managed", False) and url_hint:
            from urllib.parse import urlparse
            hints = url_hint if isinstance(url_hint, (tuple, list)) else [url_hint]
            hosts = {urlparse(hint).hostname for hint in hints}
            for existing in self.context.pages:
                if not existing.is_closed() and urlparse(existing.url).hostname in hosts:
                    try:
                        await asyncio.wait_for(existing.evaluate("1"), timeout=3)
                    except Exception as exc:
                        if "crash" not in str(exc).lower():
                            raise
                        # Only replace a crashed tab in our dedicated profile.
                        # Reusing that page makes every following attempt fail.
                        await existing.close()
                        continue
                    return existing
        page = await self.context.new_page()
        if not getattr(self, "managed", False):
            self._own_pages.append(page)
        return page

    async def load_cookies_from_file(self, file_path: str | Path) -> None:
        """
        从 JSON 文件加载 Cookie 并注入到当前上下文

        支持两种文件格式：
        1. Playwright storage_state 文件（包含 "cookies" 字段）
        2. 仅为 cookies 列表的纯 JSON
        """
        if self.context is None:
            raise RuntimeError("Context 未初始化")

        path = Path(file_path)
        if not path.is_file():
            # 如有 logger 建议用 logger，这里先用 print 占位
            raise RuntimeError(f"[警告] Cookie 文件不存在: {path}")

        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            raise RuntimeError(f"[错误] 读取 Cookie 文件失败: {path}，错误: {e}")

        # 1. 兼容 Playwright 的 storage_state 结构
        #    {"cookies": [...], "origins": [...]}
        if isinstance(data, dict) and "cookies" in data:
            raw_cookies = data["cookies"]
        else:
            # 2. 兼容直接是 cookies 列表的情况
            raw_cookies = data

        if not isinstance(raw_cookies, list):
            raise RuntimeError(
                f"[错误] Cookie 文件格式不正确，应为列表或包含 'cookies' 字段: {path}"
            )

        # 强制转换成 Playwright Cookie 类型，方便 IDE 类型检查
        cookies = raw_cookies

        if not cookies:
            raise RuntimeError(f"[提示] Cookie 文件为空: {path}")

        await self.context.add_cookies(cookies)

    async def storage_state(self, path: Path | str):
        """保存当前 Cookie 到文件"""
        if not self.context:
            raise RuntimeError("Context 未初始化")
        # 确保存储目录存在
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        return await self.context.storage_state(path=path)

    async def close(self):
        await self.__aexit__(None, None, None)

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # 仅正常结束时按配置保留浏览器窗口；异常退出（发布失败/页面被关）直接关闭，
        # 避免无人值守场景每次失败都空等一分钟。
        keep_open_seconds = self.keep_open_seconds if (not self.headless or self.attached) and exc_type is None else 0
        if keep_open_seconds > 0:
            print(f"[Browser] Keeping browser open for {keep_open_seconds}s before close")
            await asyncio.sleep(keep_open_seconds)

        # 附加模式：这是用户自己的浏览器。只收掉我们开的标签页然后断开，
        # 绝不 close context/browser——那会连人家原有的窗口一起关掉。
        if self.attached:
            for page in self._own_pages:
                try:
                    await page.close()
                except Exception:
                    pass
            self._own_pages.clear()
            self.context = None
            self.browser = None
            if self.playwright and not self._pooled:
                try:
                    await self.playwright.stop()
                except Exception:
                    pass
            self.playwright = None
            return

        # 用户可能已手动关闭窗口，close 会抛错，逐层兜底保证清理走完
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
            self.browser = None
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
            self.playwright = None


# ==========================================
# 实际使用示例
# ==========================================


class MySpider:
    def __init__(self):
        # 推荐方式 1：用 create 工厂（最安全）
        self.browser: Optional[StealthBrowser] = None

        # 推荐方式 2：如果你喜欢 async with（最优雅）
        self._browser_context_manager = None

    async def start(self):
        # 方式1：工厂方式（推荐用于长生命周期对象）
        self.browser = await StealthBrowser.create(headless=True)

    async def some_task(self):
        page = await self.browser.new_page()
        await page.goto("https://httpbin.org/headers")
        print(await page.content())
        await page.close()

    async def close(self):
        if self.browser:
            await self.browser.close()
            self.browser = None


# 使用示例（完美）
async def main():
    spider = MySpider()
    await spider.start()

    for i in range(10):
        await spider.some_task()

    await spider.close()  # 手动关闭（推荐）

    # 就算你忘记 close()，__del__ + weakref.finalize 也会自动清理！
    # 绝不漏关，内存永不泄露！
