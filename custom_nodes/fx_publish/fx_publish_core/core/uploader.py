"""上传器基类。

提供：
- 公共流程：login_flow / verify_cookie_flow / upload_video_flow
- 通用工具：_find_first_element / _click_first_visible / _upload_file_to_first
            _wait_for_condition / _wait_until_attached / _click_and_wait_for_url
- 登录检测：cookie 文件过期预检 + positive DOM + negative DOM 兜底
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import traceback
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional
from urllib.parse import urlparse

from playwright.async_api import Error, Locator, Page

try:
    from playwright.async_api import TargetClosedError
except ImportError:  # 兼容旧版 playwright
    TargetClosedError = None

from ..conf import COOKIES_DIR
from ..utils.log import StepLogger, get_uploader_logger
from .browser import StealthBrowser

WaitState = Literal["visible", "attached", "hidden", "detached"]


def _is_page_closed_error(exc: BaseException) -> bool:
    """页面/浏览器已被关闭（用户手关窗口、浏览器崩溃）→ 不可恢复，重试无意义。"""
    if TargetClosedError is not None and isinstance(exc, TargetClosedError):
        return True
    msg = str(exc)
    return "has been closed" in msg or "Target closed" in msg


class BaseUploader(ABC):
    """所有平台上传器的基类，定义通用流程与可复用工具方法。"""

    logger: StepLogger
    cookie_file_path: Path

    def __init__(
        self,
        logger: Optional[StepLogger] = None,
        cookie_file_path: str | Path | None = None,
        headless: bool = True,
    ):
        self.logger = logger or get_uploader_logger(self.platform_name)
        if cookie_file_path is None:
            self.cookie_file_path = (
                COOKIES_DIR / f"{self.platform_name}_uploader" / "account.json"
            )
        else:
            self.cookie_file_path = Path(cookie_file_path)
        self._headless = headless
        # no_publish 调试模式：完成所有填写但不点击最终发布。
        # 必须在基类声明，所有平台的发布步骤都要检查它（见 _should_skip_publish）。
        self._skip_publish = False
        self._run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        self._debug_dir = (
            Path.cwd() / "logs" / "fx_publish_debug" / self.platform_name / self._run_id
        )

    async def _new_browser(self, headless: bool = True, channel=None):
        """所有浏览器都从这里出。附加模式（复用你已登录的 Chrome）在这里统一生效。

        self._cdp_port 由节点层注入；为 None 时行为和以前完全一致（新开隐身浏览器）。
        """
        return await StealthBrowser.create(
            headless=headless,
            channel=channel,
            cdp_port=getattr(self, "_cdp_port", None),
            cdp_autostart=getattr(self, "_cdp_autostart", True),
        )

    async def _should_skip_publish(self, page: Page) -> bool:
        """no_publish 模式：保存现场并返回 True，调用方直接跳过发布步骤。"""
        if not getattr(self, "_skip_publish", False):
            return False
        self.logger.info("no_publish 模式：已跳过最终发布", url=page.url)
        try:
            await self._capture_step_snapshot(page, "skip_publish", "no_publish")
        except Exception:
            pass
        return True

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def debug_dir(self) -> Path:
        return self._debug_dir

    @property
    def _browser_channel(self) -> Optional[str]:
        """登录时使用的浏览器通道。None = 使用系统 Chrome（默认）。

        子类可覆盖以使用 Playwright 内置 Chromium，
        例如快手因 system Chrome 会话冲突需要使用 Chromium。
        """
        return None

    @property
    def _headless_upload(self) -> bool:
        """上传时是否使用 headless 浏览器。默认 True，可通过构造参数 headless 覆盖。

        子类可覆盖为 False 以对抗反爬检测，也可由 CLI --headed 参数控制。
        """
        return self._headless

    # ---------------------------------------------------------------- 抽象 API

    @property
    @abstractmethod
    def platform_name(self) -> str: ...

    @property
    @abstractmethod
    def login_url(self) -> str: ...

    @property
    @abstractmethod
    def publish_url(self) -> str: ...

    @property
    @abstractmethod
    def _login_selectors(self) -> List[str]:
        """登录页特征元素（negative 信号；找到 = 未登录）。"""

    @property
    def _authed_selectors(self) -> List[str]:
        """登录后才出现的元素（positive 信号；找到 = 已登录）。

        默认空列表 = 仅依赖 negative 检测。子类应覆盖以提升鲁棒性。
        """
        return []

    @abstractmethod
    async def _upload_video(
        self,
        page: Page,
        file_path: str | Path,
        title: str = "",
        content: str = "",
        tags: List[str] = None,
        publish_date: Optional[datetime] = None,
        thumbnail_path: Optional[str | Path] = None,
    ) -> bool: ...

    # ----------------------------------------------------------------- 公共流程

    async def login_flow(self) -> bool:
        try:
            with self.logger.step("login_flow", platform=self.platform_name):
                async with await self._new_browser(
                    headless=False, channel=self._browser_channel
                ) as browser:
                    page = await browser.new_page()
                    await page.goto(self.login_url)
                    self.logger.info("等待用户在浏览器内完成登录…")
                    if not await self._wait_for_login(page, timeout=120.0):
                        raise RuntimeError("登录超时")
                    # 导航到发布页，让页面设置所需的 cookie（如 cp 平台 cookie）
                    await page.goto(self.publish_url, timeout=30000)
                    await page.wait_for_timeout(5000)
                    if await self._check_login_required(page):
                        raise RuntimeError(
                            f"登录检测通过但 cookie 无效：发布页 {self.publish_url} 仍要求登录"
                        )
                    # 发布页 cookie 已设置，现在保存完整的 storage_state
                    self.cookie_file_path.parent.mkdir(parents=True, exist_ok=True)
                    await page.context.storage_state(path=self.cookie_file_path)
                    self.logger.info("cookie 已保存", path=str(self.cookie_file_path))
                    self.logger.info("cookie 验证通过", publish_url=self.publish_url)
                    return True
        except Exception as e:
            self.logger.error("登录失败", reason=str(e)[:200])
            return False

    async def _wait_for_login(self, page: Page, *, timeout: float = 120.0) -> bool:
        """等待登录完成。

        策略：用户在浏览器中登录后，页面会跳转离开登录页。
        检测 positive DOM（authed 元素）或 negative DOM（登录表单消失）。

        防抖机制：登录表单消失后等待 5 秒稳定期，避免 QR 扫码中间态误判。
        """
        await page.wait_for_timeout(3000)

        _no_login_since: float = 0.0  # 闭包变量：登录表单首次消失的时间戳

        async def check() -> bool:
            nonlocal _no_login_since
            cur_url = page.url
            # Chrome 错误页 → 页面加载失败，继续等待
            if cur_url.startswith(("chrome-error://", "edge://")):
                return False
            # positive 检测：authed 元素出现
            if self._authed_selectors:
                for sel in self._authed_selectors:
                    try:
                        el = page.locator(sel)
                        if await el.count() > 0 and await el.first.is_visible():
                            return True
                    except Error:
                        continue
            # negative 检测：登录表单仍存在 → 继续等待
            if await self._check_login_required(page):
                _no_login_since = 0.0
                return False
            # 登录表单已消失，需要防抖：等待 5 秒稳定期（扫码中间态可能短暂消失）
            now = time.monotonic()
            if _no_login_since == 0.0:
                _no_login_since = now
                self.logger.debug("登录表单消失，进入防抖期...")
                return False
            if now - _no_login_since < 5.0:
                return False
            # 防抖通过，导航到发布页二次确认
            try:
                await page.goto(self.publish_url, timeout=15000)
                await page.wait_for_timeout(3000)
                if not await self._check_login_required(page):
                    return True
            except Exception:
                pass
            # 发布页仍要求登录，重置状态继续等待
            _no_login_since = 0.0
            return False

        return await self._wait_for_condition(
            check, timeout=timeout, interval=2.0, desc="login"
        )

    async def verify_cookie_flow(self, auto_login: bool = False) -> bool:
        if not self.cookie_file_path.exists():
            self.logger.warning("cookie 文件不存在", path=str(self.cookie_file_path))
            return await self.login_flow() if auto_login else False

        if self._is_cookie_file_expired():
            self.logger.warning("cookie 文件已过期（本地预检）")
            return await self.login_flow() if auto_login else False

        if await self._verify_cookie():
            return True
        return await self.login_flow() if auto_login else False

    async def upload_video_flow(
        self,
        file_path: str | Path,
        title: str = "",
        content: str = "",
        tags: List[str] = None,
        publish_date: Optional[datetime] = None,
        thumbnail_path: Optional[str | Path] = None,
        auto_login: bool = False,
    ) -> bool:
        try:
            with self.logger.step("upload_video_flow", title=title) as step:
                cookie_usable = (
                    self.cookie_file_path.exists()
                    and not self._is_cookie_file_expired()
                )
                # 浏览器可见性决策：
                # - 真正发布（非 no_publish 调试）→ 强制有头，让用户看到发布全过程，
                #   避免无头后台把内容真发出去而用户看不到；
                # - no_publish 调试 → 尊重节点 headed 参数（cookie 可用可无头后台跑），
                #   但 cookie 不可用时仍强制有头以便扫码登录。
                if not getattr(self, "_skip_publish", False):
                    headless = False
                else:
                    headless = self._headless_upload if cookie_usable else False
                async with await self._new_browser(
                    headless=headless, channel=self._browser_channel
                ) as browser:
                    self.logger.info(
                        "浏览器已启动",
                        browser_id=getattr(browser, "browser_id", "unknown"),
                        headless=headless,
                    )
                    page = await browser.new_page()
                    cookie_ok = False

                    if getattr(browser, "attached", False):
                        # 附加模式：用的就是你自己那个浏览器，登录态是现成的。
                        # 绝不能把 cookie 文件 add_cookies 进去——那会覆盖你的实时会话。
                        self.logger.info("附加到已有浏览器，直接用其登录态，不注入 cookie 文件")
                        cookie_ok = await self._verify_cookie_on_page(page)
                        if not cookie_ok:
                            raise RuntimeError(
                                f"复用的浏览器里没有 {self.platform_name} 的登录态。"
                                f"请在那个浏览器窗口里手动登录一次，再重跑本节点。"
                            )
                    elif cookie_usable:
                        await browser.load_cookies_from_file(self.cookie_file_path)
                        cookie_ok = await self._verify_cookie_on_page(page)
                    else:
                        if not self.cookie_file_path.exists():
                            self.logger.warning("cookie 文件不存在", path=str(self.cookie_file_path))
                        else:
                            self.logger.warning("cookie 文件已过期（本地预检）")

                    if cookie_ok:
                        ok = await self._upload_video(
                            page=page,
                            file_path=file_path,
                            title=title,
                            content=content,
                            tags=tags,
                            publish_date=publish_date,
                            thumbnail_path=thumbnail_path,
                        )
                        step.add_field(result="success" if ok else "failure")
                        return ok

                    if not auto_login:
                        raise RuntimeError("cookie 无效")

                    self.logger.info("cookie 无效，复用当前浏览器启动登录流程")
                    ok = await self._login_and_upload_on_page(
                        page=page,
                        file_path=file_path,
                        title=title,
                        content=content,
                        tags=tags,
                        publish_date=publish_date,
                        thumbnail_path=thumbnail_path,
                    )
                    step.add_field(result="success" if ok else "failure")
                    return ok
        except Exception as e:
            self.logger.error("上传流程异常", reason=str(e)[:200])
            return False

    async def _login_and_upload(
        self,
        file_path: str | Path,
        title: str = "",
        content: str = "",
        tags: List[str] = None,
        publish_date: Optional[datetime] = None,
        thumbnail_path: Optional[str | Path] = None,
    ) -> bool:
        """在同一个浏览器中完成登录 + 上传（解决 fingerprint 不兼容问题）。"""
        try:
            with self.logger.step("login_and_upload"):
                async with await self._new_browser(
                    headless=False, channel=self._browser_channel
                ) as browser:
                    # 1) 登录
                    page = await browser.new_page()

                    async def goto_login_page():
                        await page.goto(self.login_url, timeout=30000)
                        return True

                    async def wait_for_login_done():
                        self.logger.info("等待用户在浏览器内完成登录…")
                        if not await self._wait_for_login(page, timeout=120.0):
                            raise RuntimeError("登录超时")
                        return True

                    async def save_cookie_state():
                        self.cookie_file_path.parent.mkdir(parents=True, exist_ok=True)
                        await page.context.storage_state(path=self.cookie_file_path)
                        self.logger.info("cookie 已保存", path=str(self.cookie_file_path))
                        return True

                    async def goto_publish_after_login():
                        await page.goto(self.publish_url, timeout=30000)
                        await page.wait_for_timeout(3000)
                        if await self._check_login_required(page):
                            raise RuntimeError("登录后发布页仍要求登录")
                        return True

                    await self._stable_step(page, "login_goto_page", goto_login_page, retries=2)
                    await self._stable_step(page, "login_wait_user", wait_for_login_done, retries=0)
                    await self._stable_step(page, "login_save_cookie", save_cookie_state, retries=1)
                    await self._stable_step(page, "login_goto_publish_page", goto_publish_after_login, retries=2)

                    ok = await self._upload_video(
                        page=page,
                        file_path=file_path,
                        title=title,
                        content=content,
                        tags=tags,
                        publish_date=publish_date,
                        thumbnail_path=thumbnail_path,
                    )
                    return ok
        except Exception as e:
            self.logger.error("登录并上传失败", reason=str(e)[:200])
            return False

    async def _login_and_upload_on_page(
        self,
        page: Page,
        file_path: str | Path,
        title: str = "",
        content: str = "",
        tags: List[str] = None,
        publish_date: Optional[datetime] = None,
        thumbnail_path: Optional[str | Path] = None,
    ) -> bool:
        """复用当前浏览器 page 完成登录 + 上传，避免单次发布打开两个浏览器。"""
        try:
            with self.logger.step("login_and_upload"):
                async def goto_login_page():
                    await page.goto(self.login_url, timeout=30000)
                    return True

                async def wait_for_login_done():
                    self.logger.info("等待用户在浏览器内完成登录…")
                    if not await self._wait_for_login(page, timeout=120.0):
                        raise RuntimeError("登录超时")
                    return True

                async def save_cookie_state():
                    self.cookie_file_path.parent.mkdir(parents=True, exist_ok=True)
                    await page.context.storage_state(path=self.cookie_file_path)
                    self.logger.info("cookie 已保存", path=str(self.cookie_file_path))
                    return True

                async def goto_publish_after_login():
                    await page.goto(self.publish_url, timeout=30000)
                    await page.wait_for_timeout(3000)
                    if await self._check_login_required(page):
                        raise RuntimeError("登录后发布页仍要求登录")
                    return True

                await self._stable_step(page, "login_goto_page", goto_login_page, retries=2)
                await self._stable_step(page, "login_wait_user", wait_for_login_done, retries=0)
                await self._stable_step(page, "login_save_cookie", save_cookie_state, retries=1)
                await self._stable_step(page, "login_goto_publish_page", goto_publish_after_login, retries=2)

                return await self._upload_video(
                    page=page,
                    file_path=file_path,
                    title=title,
                    content=content,
                    tags=tags,
                    publish_date=publish_date,
                    thumbnail_path=thumbnail_path,
                )
        except Exception as e:
            self.logger.error("登录并上传失败", reason=str(e)[:200])
            return False

    # -------------------------------------------------------- 登录检测：内部实现

    def _is_cookie_file_expired(self) -> bool:
        """读 storage_state JSON，判断认证 cookie 是否全部过期。

        规则：
        - 解析失败 → True（视为过期，触发重登）
        - 任一 cookie 的 expires <= 0（session）→ 视为有效，返回 False
        - 任一 cookie 的 expires > now → 返回 False
        - 否则全部过期 → True
        """
        try:
            data = json.loads(self.cookie_file_path.read_text(encoding="utf-8"))
        except Exception as e:
            self.logger.debug("cookie 文件解析失败", reason=str(e)[:100])
            return True

        cookies = data.get("cookies") or []
        if not cookies:
            return True
        now = time.time()
        for c in cookies:
            exp = c.get("expires", -1)
            if exp is None or exp <= 0:
                return False  # session cookie，无法判断 → 交给浏览器层
            if exp > now:
                return False
        return True

    async def _check_login_required(self, page: Page) -> bool:
        """negative 检测：登录页特征元素是否可见。"""
        for selector in self._login_selectors:
            try:
                el = page.locator(selector)
                if await el.count() > 0 and await el.first.is_visible():
                    return True
            except Error:
                continue
        return False

    async def _check_authed(self, page: Page, timeout: int = 8000) -> Optional[bool]:
        """positive 检测：等待任一登录后特征元素出现。

        Returns:
            True   登录后元素已出现
            False  超时未出现
            None   未配置 _authed_selectors，跳过
        """
        if not self._authed_selectors:
            return None
        per = max(1000, timeout // max(1, len(self._authed_selectors)))
        for selector in self._authed_selectors:
            try:
                await page.wait_for_selector(selector, state="visible", timeout=per)
                return True
            except Error:
                continue
        return False

    async def _verify_cookie(self) -> bool:
        """启动浏览器加载 cookie，先 positive 后 negative 双重判定。"""
        try:
            with self.logger.step("verify_cookie"):
                async with await self._new_browser(headless=True) as browser:
                    await browser.load_cookies_from_file(self.cookie_file_path)
                    async with await browser.new_page() as page:
                        return await self._verify_cookie_on_page(page)
        except Exception as e:
            self.logger.error("verify_cookie 异常", reason=str(e)[:200])
            return False

    async def _verify_cookie_on_page(self, page: Page) -> bool:
        """在既有 page 上验证 cookie，避免验证和上传各启动一个浏览器。"""
        try:
            with self.logger.step("verify_cookie"):
                # domcontentloaded：重型创作后台（如 B站 member 页）等全部资源 load
                # 会 30s 超时误判 cookie 失效；DOM 就绪即可，后续检查自带轮询。
                await page.goto(
                    self.publish_url, timeout=60000, wait_until="domcontentloaded"
                )
                await page.wait_for_timeout(3000)
                pub_domain = urlparse(self.publish_url).netloc
                cur_domain = urlparse(page.url).netloc
                # 1) positive 检测优先
                authed = await self._check_authed(page)
                if authed is True:
                    self.logger.info("cookie 有效", method="authed_dom")
                    return True
                # 2) negative 检测：登录表单可见 → cookie 失效
                if await self._check_login_required(page):
                    self.logger.warning("cookie 失效", method="login_dom")
                    return False
                # 3) URL 仍在发布域名下 + 无登录表单 → cookie 有效
                if pub_domain and cur_domain == pub_domain:
                    self.logger.info(
                        "cookie 有效",
                        method="same_domain",
                        url=page.url,
                    )
                    return True
                # 4) 既无 positive 也无 negative：保守判为有效
                if authed is None:
                    self.logger.info("cookie 有效", method="no_login_dom")
                    return True
                self.logger.warning("cookie 状态不明，视为失效")
                return False
        except Exception as e:
            self.logger.error("verify_cookie 异常", reason=str(e)[:200])
            return False

    # ------------------------------------------------------------- 通用工具方法

    async def _find_first_element(
        self,
        page: Page,
        selectors: List[str],
        *,
        timeout: int = 5000,
        state: WaitState = "visible",
        callback: Optional[
            Callable[[Locator, Page, Dict[str, Any]], Awaitable[Any]]
        ] = None,
        on_not_found: Optional[Callable[[Page, List[str]], Awaitable[None]]] = None,
    ) -> Optional[Locator]:
        """按顺序尝试 selectors，返回首个达到 state 的 Locator。"""
        for idx, selector in enumerate(selectors):
            try:
                el = page.locator(selector).first
                if await el.count() == 0:
                    self.logger.debug(
                        "选择器未匹配", idx=idx + 1, total=len(selectors), sel=selector
                    )
                    continue
                await el.wait_for(state=state, timeout=timeout)
                self.logger.debug(
                    "选择器命中", idx=idx + 1, total=len(selectors), sel=selector
                )
                if callback:
                    info = {
                        "selector": selector,
                        "index": idx,
                        "total": len(selectors),
                        "state": state,
                    }
                    await callback(el, page, info)
                return el
            except Exception as e:
                self.logger.debug(
                    "选择器失败",
                    idx=idx + 1,
                    total=len(selectors),
                    sel=selector,
                    reason=str(e)[:100],
                )
                continue
        self.logger.warning("所有选择器均未命中", count=len(selectors))
        if on_not_found:
            await on_not_found(page, selectors)
        return None

    async def _wait_until_attached(
        self,
        page: Page,
        selectors: List[str],
        *,
        timeout: int = 10000,
    ) -> bool:
        """对每个选择器调用 wait_for_selector(state='attached')，命中即返回 True。"""
        per = max(1000, timeout // max(1, len(selectors)))
        for selector in selectors:
            try:
                await page.wait_for_selector(selector, state="attached", timeout=per)
                return True
            except Error:
                continue
        return False

    async def _click_first_visible(
        self,
        page: Page,
        selectors: List[str],
        *,
        timeout: int = 5000,
        force: bool = False,
    ) -> bool:
        """点击首个可见的 selector，未命中返回 False。"""
        el = await self._find_first_element(
            page, selectors, timeout=timeout, state="visible"
        )
        if el is None:
            return False
        await el.click(force=force, timeout=timeout)
        return True

    async def _upload_file_to_first(
        self,
        page: Page,
        selectors: List[str],
        file_path: str | Path,
        *,
        timeout: int = 10000,
    ) -> bool:
        """向首个 attached 的 file input 注入文件。

        显式 wait_for_selector(state='attached') 解决"input 异步挂载"的竞态。
        """
        if not await self._wait_until_attached(page, selectors, timeout=timeout):
            self.logger.warning("file input 未找到", selectors=selectors)
            return False
        el = await self._find_first_element(
            page, selectors, timeout=timeout, state="attached"
        )
        if el is None:
            return False
        await el.set_input_files(file_path)
        return True

    async def _wait_for_condition(
        self,
        check: Callable[[], Awaitable[bool]],
        *,
        timeout: float = 60.0,
        interval: float = 1.0,
        desc: str = "condition",
    ) -> bool:
        """通用轮询：每 interval 秒调用 check()，True 即返回。"""
        deadline = time.monotonic() + timeout
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            try:
                if await check():
                    self.logger.debug(
                        "wait_for_condition 命中", desc=desc, attempt=attempt
                    )
                    return True
            except Exception as e:
                self.logger.debug(
                    "wait_for_condition check 异常",
                    desc=desc,
                    reason=str(e)[:100],
                )
            await asyncio.sleep(interval)
        self.logger.warning("wait_for_condition 超时", desc=desc, timeout=timeout)
        return False

    async def _click_and_wait_for_url(
        self,
        page: Page,
        button: Locator,
        url_pattern: str | re.Pattern,
        *,
        timeout: int = 30000,
        wait_until: str = "load",
    ) -> bool:
        """点击按钮并等待跳转到匹配 url_pattern 的页面，超时则兜底检查当前 URL。"""
        pattern = (
            url_pattern
            if isinstance(url_pattern, re.Pattern)
            else re.compile(url_pattern)
        )
        try:
            async with page.expect_navigation(
                url=pattern, wait_until=wait_until, timeout=timeout
            ):
                await button.click(force=True)
            return True
        except Error:
            current = page.url
            if pattern.search(current):
                self.logger.info("导航超时但 URL 已匹配", url=current)
                return True
            self.logger.error("导航超时且 URL 未匹配", url=current)
            return False

    async def _capture_step_debug(self, page: Page, step: str, attempt: int, reason: str) -> None:
        """保存失败现场，方便定位页面结构变化、弹窗、崩溃等问题。"""
        try:
            self._debug_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            safe_step = re.sub(r"[^a-zA-Z0-9_.-]+", "_", step)
            base = self._debug_dir / f"{stamp}_{safe_step}_attempt{attempt}"
            meta = {
                "step": step,
                "attempt": attempt,
                "url": page.url,
                "reason": reason,
            }
            (base.with_suffix(".json")).write_text(
                json.dumps(meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            try:
                await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
            except Exception as shot_error:
                self.logger.warning("失败截图保存失败", step=step, reason=str(shot_error)[:160])
            try:
                html = await page.content()
                (base.with_suffix(".html")).write_text(html, encoding="utf-8")
            except Exception as html_error:
                self.logger.warning("失败 HTML 保存失败", step=step, reason=str(html_error)[:160])
            self.logger.error(
                "步骤失败现场已保存",
                step=step,
                attempt=attempt,
                base=str(base),
                url=page.url,
            )
        except Exception as debug_error:
            self.logger.warning("保存失败现场异常", step=step, reason=str(debug_error)[:160])

    async def _capture_step_snapshot(self, page: Page, step: str, label: str = "success") -> str:
        """保存成功现场，用于人工确认页面状态。"""
        self._debug_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe_step = re.sub(r"[^a-zA-Z0-9_.-]+", "_", step)
        base = self._debug_dir / f"{stamp}_{safe_step}_{label}"
        meta = {"step": step, "label": label, "url": page.url}
        (base.with_suffix(".json")).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        except Exception as shot_error:
            self.logger.warning("成功截图保存失败", step=step, reason=str(shot_error)[:160])
        try:
            html = await page.content()
            (base.with_suffix(".html")).write_text(html, encoding="utf-8")
        except Exception as html_error:
            self.logger.warning("成功 HTML 保存失败", step=step, reason=str(html_error)[:160])
        self.logger.info("步骤成功现场已保存", step=step, base=str(base), url=page.url)
        return str(base)

    async def _stable_step(
        self,
        page: Page,
        name: str,
        action: Callable[[], Awaitable[Any]],
        *,
        retries: int = 2,
        delay: float = 1.0,
        required: bool = True,
    ) -> Any:
        """带日志、重试、现场捕获的稳定步骤包装器。"""
        last_error: Exception | None = None
        for attempt in range(1, retries + 2):
            try:
                self.logger.info("步骤开始", step=name, attempt=attempt, max_attempts=retries + 1, url=page.url)
                result = await action()
                if result is False:
                    raise RuntimeError(f"{name} returned False")
                self.logger.info("步骤完成", step=name, attempt=attempt, url=page.url)
                return result
            except Exception as exc:
                last_error = exc
                # 页面/浏览器已关闭：重试和截图都不可能成功，立即中止整个流程
                if _is_page_closed_error(exc):
                    self.logger.error(
                        "页面已关闭，中止流程", step=name, attempt=attempt, reason=str(exc)[:160]
                    )
                    raise
                self.logger.error(
                    "步骤失败",
                    step=name,
                    attempt=attempt,
                    max_attempts=retries + 1,
                    reason=str(exc)[:240],
                )
                self.logger.debug("步骤失败 traceback", step=name, traceback=traceback.format_exc()[-2000:])
                await self._capture_step_debug(page, name, attempt, str(exc)[:500])
                if attempt <= retries:
                    await page.wait_for_timeout(int(delay * 1000 * attempt))
                    continue
                if required:
                    raise
                return False
        if required and last_error:
            raise last_error
        return False
