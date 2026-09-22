"""小红书视频上传器。"""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from playwright.async_api import Error, Page

from ...core.base_publisher import BasePublisher


class XiaoHongShuUploader(BasePublisher):
    """小红书视频上传器。"""

    @property
    def platform_name(self) -> str:
        return "xiaohongshu"

    @property
    def display_name(self) -> str:
        return "小红书"

    @property
    def login_url(self) -> str:
        return "https://creator.xiaohongshu.com/"

    @property
    def publish_url(self) -> str:
        return "https://creator.xiaohongshu.com/publish/publish"

    @property
    def _video_upload_url(self) -> str:
        return f"{self.publish_url}?from=homepage&target=video"

    @property
    def _login_selectors(self) -> List[str]:
        return [
            'text="短信登录"',
            'text="扫码登录"',
            'button:has-text("登")',
            ".login-btn",
        ]

    @property
    def _authed_selectors(self) -> List[str]:
        # 上传页才会渲染的元素：视频上传 input + 顶部的"上传视频"按钮
        return ["input.upload-input", 'button:has-text("上传视频")']

    # ---------------------------------------------------------------- 主流程

    async def _upload_video(
        self,
        page: Page,
        file_path: str | Path,
        title: str = "",
        content: str = "",
        tags: List[str] = None,
        publish_date: Optional[datetime] = None,
        thumbnail_path: Optional[str | Path] = None,
    ) -> bool:
        try:
            with self.logger.step("upload_video", title=title, file=str(file_path)):
                async def goto_upload_page():
                    await page.goto(self._video_upload_url)
                    try:
                        await page.wait_for_url(self._video_upload_url, timeout=5000)
                    except Error:
                        pass
                    return True

                await self._stable_step(page, "goto_upload_page", goto_upload_page)
                await self._stable_step(
                    page,
                    "upload_video_file_and_wait_complete",
                    lambda: self._upload_video_file_and_wait(page, file_path),
                    retries=2,
                    delay=2.0,
                )
                await self._stable_step(page, "fill_video_info", lambda: self._fill_video_info(page, title, content, tags), retries=3)
                await self._stable_step(page, "set_thumbnail", lambda: self._set_thumbnail(page, thumbnail_path), retries=1, required=False)

                if publish_date:
                    await self._stable_step(page, "set_schedule_time", lambda: self._set_schedule_time(page, publish_date), retries=2)

                if await self._should_skip_publish(page):
                    return True

                await self._stable_step(page, "publish_video", lambda: self._publish_video(page), retries=2, delay=2.0)
            return True
        except Exception as e:
            self.logger.error("upload_video 异常", reason=str(e)[:200])
            return False

    # ---------------------------------------------------------------- 子步骤

    async def _upload_video_file(self, page: Page, file_path: str | Path) -> bool:
        try:
            inp = page.locator("input.upload-input")
            await inp.wait_for(state="attached", timeout=10000)
            await inp.set_input_files(file_path)
            return True
        except Exception as e:
            self.logger.error("视频文件注入失败", reason=str(e)[:200])
            return False

    _UPLOAD_FAILURE_TEXTS = [
        "上传失败",
        "网络错误",
        "请稍后刷新重试",
        "上传图文，请先切换到图片tab",
        "上传图文，请先切换到图片 tab",
        "上传视频失败",
    ]

    async def _detect_upload_failure(self, page: Page) -> str:
        """返回页面上可见的上传失败文案，无失败返回空串。"""
        for txt in self._UPLOAD_FAILURE_TEXTS:
            try:
                loc = page.locator(f"text={txt}").first
                if await loc.count() > 0 and await loc.is_visible():
                    return txt
            except Exception:
                continue
        return ""

    async def _retry_upload_via_reupload_button(
        self, page: Page, file_path: str | Path
    ) -> bool:
        """上传失败后点击视频卡片右上角的「重新上传」再次注入文件。"""
        btn = page.get_by_text("重新上传", exact=False).first
        if await btn.count() == 0 or not await btn.is_visible():
            self.logger.warning("未找到重新上传按钮")
            return False
        try:
            # 新版「重新上传」触发系统文件选择器
            async with page.expect_file_chooser(timeout=5000) as fc_info:
                await btn.click(force=True)
            await (await fc_info.value).set_files(file_path)
            self.logger.info("已通过重新上传按钮再次注入视频")
            return True
        except Exception:
            # 备选：点击后页面回到上传初始态，走 input 注入
            try:
                inp = page.locator("input.upload-input").first
                await inp.wait_for(state="attached", timeout=5000)
                await inp.set_input_files(file_path)
                self.logger.info("已通过 upload-input 再次注入视频")
                return True
            except Exception as e:
                self.logger.error("重新上传注入失败", reason=str(e)[:160])
                return False

    async def _upload_video_file_and_wait(self, page: Page, file_path: str | Path) -> bool:
        """小红书上传失败常异步出现，上传和等待必须作为一个可重试单元。

        等待期间检测到"上传失败/网络错误"时，自动点击「重新上传」原地重试，
        单次注入最多重试 2 次。
        """
        if not await self._upload_video_file(page, file_path):
            return False
        for retry in range(3):
            result = await self._wait_for_upload_complete(page)
            if result == "success":
                return True
            if result == "failed" and retry < 2:
                self.logger.warning("上传失败，尝试重新上传", retry=retry + 1)
                if not await self._retry_upload_via_reupload_button(page, file_path):
                    return False
                continue
            return False
        return False

    async def _wait_for_upload_complete(self, page: Page) -> str:
        """轮询直到视频真正上传完成。

        返回 "success" / "failed" / "timeout"。

        竞态陷阱（2026-07 实测踩坑）：小红书新版页面在视频还在上传甚至尚未开始时
        就渲染出标题/正文/封面区域，且失败提示是异步延迟出现的。所以：
        1. 不能把"标题输入框可见"当作上传完成——必须有明确的正向完成信号；
        2. 没有正向信号时，需要长稳定窗口 + 二次确认失败文案未出现。
        """

        async def positive_done() -> bool:
            # 2026-07 实测：上传完成后没有"上传成功"文案，
            # 视频卡片顶部出现常驻的「重新上传」按钮是最可靠的完成信号
            # （外层已保证此时无 上传中/当前速度 标记、无失败文案）。
            try:
                reupload = page.get_by_text("重新上传", exact=False).first
                if await reupload.count() > 0 and await reupload.is_visible():
                    return True
            except Exception:
                pass
            # 兼容其它形态：明确成功文案 / 视频预览播放器
            for txt in ["上传成功", "已上传"]:
                loc = page.locator(f"text={txt}").first
                if await loc.count() > 0 and await loc.is_visible():
                    return True
            for sel in ["video", ".xgplayer", '[class*="preview-container"] video']:
                loc = page.locator(sel).first
                try:
                    if await loc.count() > 0 and await loc.is_visible():
                        return True
                except Exception:
                    continue
            return False

        async def uploading_now() -> bool:
            body_text = ""
            try:
                body_text = await page.locator("body").inner_text(timeout=2000)
            except Exception:
                return True  # 页面还没稳定，视为仍在进行
            for marker in ["上传中", "当前速度", "转码中", "处理中"]:
                # "封面上传中"是封面生成流程，不算视频上传中
                if marker in body_text.replace("封面上传中", ""):
                    return True
            for sel in [".el-progress-bar", '[class*="progress-bar"]']:
                loc = page.locator(sel).first
                try:
                    if await loc.count() > 0 and await loc.is_visible():
                        return True
                except Exception:
                    continue
            return False

        deadline = time.monotonic() + 300.0
        started = time.monotonic()
        stable_count = 0
        while time.monotonic() < deadline:
            try:
                failed_txt = await self._detect_upload_failure(page)
                if failed_txt:
                    self.logger.error("小红书上传失败态", reason=failed_txt)
                    return "failed"

                if await uploading_now():
                    stable_count = 0
                elif await positive_done():
                    # 正向信号 + 再次确认无失败文案
                    await page.wait_for_timeout(1000)
                    if await self._detect_upload_failure(page):
                        return "failed"
                    self.logger.info("小红书上传完成（正向信号）")
                    return "success"
                else:
                    # 无上传中标记也无正向信号：长稳定窗口兜底
                    stable_count += 1
                    if stable_count >= 8 and time.monotonic() - started > 25:
                        if await self._detect_upload_failure(page):
                            return "failed"
                        self.logger.info("小红书上传状态长期稳定，视为完成")
                        return "success"
            except Exception as e:
                self.logger.warning("小红书上传状态检查异常", reason=str(e)[:120])
            await page.wait_for_timeout(1500)
        self.logger.error("小红书上传完成等待超时")
        return "timeout"

    async def _fill_video_info(
        self,
        page: Page,
        title: str = "",
        content: str = "",
        tags: List[str] = None,
    ) -> bool:
        try:
            title_value = (title or "")[:20]
            content_value = content or ""
            tags = tags or []

            title_input = await self._find_first_element(
                page,
                [
                    "input[placeholder*='填写标题']",
                    "textarea[placeholder*='填写标题']",
                    "input[placeholder*='标题']",
                    "textarea[placeholder*='标题']",
                ],
                timeout=15000,
            )
            if title_input is None:
                self.logger.error("未找到小红书标题输入框")
                return False
            if title_value:
                await title_input.click(force=True)
                await title_input.fill(title_value)
                await page.wait_for_timeout(300)

            desc = await self._find_first_element(
                page,
                [
                    "#post-textarea",
                    "div.tiptap-container div[contenteditable='true']",
                    "div[contenteditable='true'][data-placeholder*='正文']",
                    "div[contenteditable='true']",
                ],
                timeout=10000,
            )
            if (content_value or tags) and desc is None:
                self.logger.error("未找到小红书正文编辑器")
                return False

            if content_value and desc is not None:
                await desc.click(force=True)
                await page.keyboard.press("ControlOrMeta+A")
                await page.keyboard.press("Backspace")
                await page.keyboard.insert_text(content_value)
                await page.wait_for_timeout(300)

            # 填写标签 — 通过 TipTap 编辑器输入
            added = 0
            for tag in tags:
                clean_tag = tag.lstrip("#")
                try:
                    # 聚焦编辑器末尾
                    if desc is None:
                        raise RuntimeError("正文编辑器不存在")
                    await desc.focus()
                    await page.keyboard.press("End")
                    await page.wait_for_timeout(200)
                    # 输入 "#标签"——注意不能带尾随空格，空格会中断话题联想下拉，
                    # 导致标签落成纯文本而非真话题
                    await page.keyboard.insert_text(f" #{clean_tag}")
                    # 等话题联想下拉（2026-07 实测 DOM：浮层为 .items 列表、
                    # 每项 .item、首项自动 .item.is-selected）
                    dropdown_item = None
                    for _ in range(10):
                        await page.wait_for_timeout(300)
                        try:
                            loc = page.locator(".item.is-selected").first
                            if await loc.count() > 0 and await loc.is_visible():
                                dropdown_item = loc
                                break
                        except Exception:
                            continue
                    if dropdown_item is not None:
                        # 点击选中项（比 Enter 更稳，不依赖编辑器焦点状态）
                        await dropdown_item.click(force=True)
                    else:
                        self.logger.warning("话题联想未出现，尝试 Enter", tag=clean_tag)
                        await page.keyboard.press("Enter")
                    await page.wait_for_timeout(400)
                    # 校验是否生成真话题元素 a.tiptap-topic
                    try:
                        is_topic = await desc.evaluate(
                            """(el, name) => {
                                for (const a of el.querySelectorAll('a.tiptap-topic')) {
                                    if ((a.getAttribute('data-topic') || '').includes(name)
                                        || (a.innerText || '').includes(name)) return true;
                                }
                                return false;
                            }""",
                            clean_tag,
                        )
                    except Exception:
                        is_topic = False
                    if is_topic:
                        added += 1
                    else:
                        self.logger.warning("标签未成为话题（纯文本）", tag=clean_tag)
                except Exception as e:
                    self.logger.warning(
                        "标签添加失败", tag=clean_tag, reason=str(e)[:100]
                )
                await page.wait_for_timeout(200)

            await page.wait_for_timeout(500)
            actual_title = ""
            try:
                actual_title = await title_input.input_value()
            except Exception:
                actual_title = ""

            actual_body = ""
            if desc is not None:
                try:
                    actual_body = await desc.inner_text(timeout=2000)
                except Exception:
                    actual_body = ""

            title_ok = not title_value or title_value in actual_title
            content_ok = not content_value or content_value[:20] in actual_body
            if not title_ok or not content_ok:
                self.logger.error(
                    "小红书信息填充校验失败",
                    title_ok=title_ok,
                    content_ok=content_ok,
                    actual_title=actual_title[:50],
                    actual_body=actual_body[:80],
                )
                return False

            await self._capture_step_snapshot(page, "fill_video_info", "success")
            self.logger.info(
                "标题与标签已填充",
                title=title_ok,
                content=content_ok,
                added=added,
                total=len(tags),
            )
            return True
        except Exception as e:
            self.logger.error("填写视频信息失败", reason=str(e)[:200])
            return False

    async def _set_thumbnail(
        self, page: Page, thumbnail_path: Optional[str | Path]
    ) -> bool:
        """设置自定义封面。

        2026-07 实测的新版 UI：
        - 封面区是内联的 .cover-plugin-preview 区块，「修改封面」operator 是
          hover 才显示的浮层，普通 click 会因不可见失败，必须 JS 强点；
        - 点开后出现 .d-modal.cover-modal 弹窗（设置封面），内含
          accept="image/*" 的 file input（"上传图片"入口）和 取消/确定 按钮。
        """
        if not thumbnail_path:
            self.logger.info("无封面，跳过")
            return True
        if not Path(thumbnail_path).exists():
            self.logger.warning("封面文件不存在，跳过", path=str(thumbnail_path))
            return True

        try:
            # 1) JS 强点「修改封面」浮层打开弹窗（hover 浮层，locator click 不可用）
            clicked = await page.evaluate(
                """() => {
                    const op = document.querySelector('.cover-plugin-preview .operator');
                    if (op) { op.click(); return 'operator'; }
                    const prev = document.querySelector('.cover-plugin-preview');
                    if (prev) { prev.click(); return 'preview'; }
                    return null;
                }"""
            )
            if not clicked:
                self.logger.error("未找到封面入口(.cover-plugin-preview)")
                return False
            self.logger.debug("封面入口已点击", via=clicked)

            # 2) 等待封面设置弹窗
            try:
                await page.wait_for_selector(
                    ".d-modal.cover-modal, .d-modal:has-text('设置封面')",
                    state="visible",
                    timeout=10000,
                )
            except Error:
                self.logger.error("封面弹窗未出现")
                return False

            # 3) 上传自定义封面图片（弹窗打开后 image input 才存在）
            upload_input_selectors = [
                'input[type="file"][accept*="image"]',
                '.d-modal input[type="file"]',
            ]
            if not await self._upload_file_to_first(
                page, upload_input_selectors, thumbnail_path, timeout=10000
            ):
                self.logger.error("未找到封面图片上传 input")
                return False

            # 等图片上传并应用到画布
            await page.wait_for_timeout(3000)

            # 4) 点击弹窗内的确定
            if not await self._click_first_visible(
                page,
                [
                    '.d-modal.cover-modal button:has-text("确定")',
                    '.d-modal button:has-text("确定")',
                    'button:has-text("确定")',
                ],
                force=True,
            ):
                self.logger.error("未找到确定按钮")
                return False

            # 5) 等待弹窗关闭（封面生成需要转码，放宽到 30s）
            try:
                await page.wait_for_selector(
                    ".d-modal.cover-modal",
                    state="hidden",
                    timeout=30000,
                )
            except Error:
                self.logger.warning("封面弹窗未在 30s 内关闭，可能仍在生成")

            self.logger.info("封面设置完成")
            return True
        except Exception as e:
            self.logger.error("封面设置失败", reason=str(e)[:200])
            return False

    async def _set_schedule_time(self, page: Page, publish_date: datetime) -> bool:
        try:
            publish_date_str = publish_date.strftime("%Y-%m-%d %H:%M")

            # 1) 开启定时发布开关
            switch_container = page.locator(".post-time-switch-container")
            if await switch_container.count() == 0:
                switch_container = page.locator(
                    ".custom-switch-wrapper:has-text('定时发布')"
                )

            # 找到 .d-switch 组件（兼容新旧 UI）
            switch = switch_container.locator(".d-switch").first
            if await switch.count() == 0:
                switch = page.locator(
                    ".custom-switch-wrapper:has-text('定时发布') .d-switch"
                ).first

            if await switch.count() > 0:
                await switch.scroll_into_view_if_needed()
                try:
                    checked = await switch.locator("input").evaluate("el => el.checked")
                except Error:
                    checked = False
                if not checked:
                    await switch.locator(".d-switch-simulator").click(force=True)
                    await page.wait_for_timeout(800)
            else:
                self.logger.warning("未找到定时发布开关")

            # 2) 等待日期选择器渲染
            try:
                await page.wait_for_selector(
                    ".date-picker-container, .d-datepicker, [class*='datepicker']",
                    state="visible",
                    timeout=5000,
                )
            except Error:
                self.logger.warning("日期选择器未出现")

            # 3) 填入发布时间
            datetime_selectors = [
                ".date-picker-container .d-text",
                ".d-datepicker-input-filter input",
                ".d-datepicker-input-filter",
                "input[placeholder*='时间']",
                "input[placeholder*='日期']",
            ]
            datetime_elem = None
            for sel in datetime_selectors:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    datetime_elem = loc
                    break

            if datetime_elem is None:
                self.logger.error("未找到日期输入框")
                return False

            await datetime_elem.wait_for(state="visible", timeout=5000)
            # 判断是否为 INPUT 元素
            is_input = await datetime_elem.evaluate("el => el.tagName === 'INPUT'")
            if not is_input:
                datetime_elem = datetime_elem.locator("input").first
                if await datetime_elem.count() == 0:
                    self.logger.error("日期输入框内无 input 元素")
                    return False

            await datetime_elem.click(force=True)
            await datetime_elem.fill(publish_date_str)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(500)
            self.logger.info("定时发布时间设置完成")
            return True
        except Exception as e:
            self.logger.error("定时发布设置失败", reason=str(e)[:200])
            return False

    async def _publish_video(self, page: Page) -> bool:
        try:
            import asyncio

            # 发布前最后防线：视频若处于失败态，禁止发布残缺笔记
            failed_txt = await self._detect_upload_failure(page)
            if failed_txt:
                raise RuntimeError(f"视频处于失败态，拒绝发布: {failed_txt}")

            # 检测发布成功
            async def _check_publish_success() -> bool:
                cur = page.url
                if re.search(r"/success|published=true|/content/|/manage", cur):
                    return True
                for t in ["发布成功", "笔记已发布", "已发布", "审核中"]:
                    if await page.locator(f'text="{t}"').count() > 0:
                        return True
                return False

            # 先滚动到底部，触发 Vue 懒加载渲染发布按钮
            await page.evaluate("""() => {
                const containers = document.querySelectorAll(
                    '.publish-page-content, .publish-page-container, .publish-vue-container, .outarea'
                );
                for (const c of containers) { c.scrollTop = c.scrollHeight; }
                window.scrollTo(0, document.body.scrollHeight);
            }""")
            await page.wait_for_timeout(500)

            # 1) 查找并点击发布按钮（最明确的动作，优先）
            self.logger.info("查找发布按钮...")
            publish_btn = await self._find_publish_button(page)
            if publish_btn is not None:
                try:
                    await publish_btn.scroll_into_view_if_needed()
                except Exception:
                    pass
                await self._capture_step_snapshot(page, "publish_video", "before_click")
                # 发布按钮是 <xhs-publish-btn> 自定义 Web Component，内容渲染在
                # closed shadow DOM 里（innerText 为空、locator 摸不到内部按钮）。
                # 2026-07 实测：组件是整条按钮容器（"暂存离开"+"发布"两颗按钮），
                # "发布"红按钮中心在容器宽度 ~60% 处，容器正中(50%)是两按钮间的
                # 空隙——所以只能用真实鼠标坐标按比例点击，多个偏移依次兜底。
                try:
                    box = await publish_btn.bounding_box()
                except Exception:
                    box = None
                if box:
                    cy = box["y"] + box["height"] / 2
                    for ratio in (0.60, 0.68, 0.55, 0.75):
                        cx = box["x"] + box["width"] * ratio
                        await page.mouse.click(cx, cy)
                        self.logger.info(
                            "已坐标点击发布按钮", ratio=ratio, x=round(cx), y=round(cy)
                        )
                        await page.wait_for_timeout(3000)
                        if await _check_publish_success():
                            self.logger.info("发布成功(按钮点击)", ratio=ratio)
                            return True
                        # 可能弹出确认弹窗，交给下方步骤 2 处理后再检测
                        confirm = page.locator(
                            'button:has-text("确认发布"), button:has-text("确认")'
                        ).last
                        if await confirm.count() > 0 and await confirm.is_visible():
                            break
                else:
                    await publish_btn.click(timeout=5000, force=True)
                    self.logger.info("已点击发布按钮(降级 force click)")
                    await page.wait_for_timeout(3000)
                await self._capture_step_snapshot(page, "publish_video", "after_click")
                if await _check_publish_success():
                    self.logger.info("发布成功(按钮点击)")
                    return True

            # 2) 检查确认弹窗（发布时常有二次确认）
            for t in ["确认发布", "确认"]:
                btn = page.locator(f'button:has-text("{t}")').last
                if await btn.count() > 0 and await btn.is_visible():
                    self.logger.info("发现确认弹窗按钮", text=t)
                    await btn.click(force=True)
                    await page.wait_for_timeout(3000)
                    if await _check_publish_success():
                        self.logger.info("发布成功(确认弹窗)")
                        return True
                    break

            # 3) 快捷键兜底（mac 上是 Cmd+Enter，用 ControlOrMeta 跨平台）
            self.logger.info("尝试 ControlOrMeta+Enter 发布...")
            await page.keyboard.press("ControlOrMeta+Enter")
            await page.wait_for_timeout(3000)
            if await _check_publish_success():
                self.logger.info("发布成功(快捷键)")
                return True

            # 4) 等待结果（兜底）
            success = False
            start = time.monotonic()
            while time.monotonic() - start < 15:
                if await _check_publish_success():
                    success = True
                    break
                await asyncio.sleep(1)

            if not success:
                self.logger.warning("发布结果检测超时", url=page.url)
            return success
        except Exception as e:
            self.logger.error("发布异常", reason=str(e)[:200])
            return False

    async def _find_publish_button(self, page: Page):
        """查找小红书底部发布按钮。

        新版页面使用 <xhs-publish-btn> 自定义组件，不一定暴露 button 标签。
        """
        candidates = [
            "xhs-publish-btn[submit-text='发布'][submit-disabled='false']",
            "xhs-publish-btn[submit-text='发布']",
            "[class*='publish-btn']",
            "button:has-text('发布')",
            "[role='button']:has-text('发布')",
            "text=发布",
        ]
        for sel in candidates:
            try:
                loc = page.locator(sel).last
                if await loc.count() > 0 and await loc.is_visible():
                    self.logger.info("发布按钮候选已选择", selector=sel)
                    return loc
            except Exception as e:
                self.logger.debug("发布按钮候选失败", selector=sel, reason=str(e)[:80])

        try:
            handle = await page.evaluate_handle(
                """() => {
                    const els = Array.from(document.querySelectorAll('*'));
                    const candidates = els
                      .filter(el => {
                        const rect = el.getBoundingClientRect();
                        const text = (el.innerText || el.textContent || '').trim();
                        const disabled = el.getAttribute('submit-disabled') === 'true'
                          || el.getAttribute('disabled') !== null
                          || el.getAttribute('aria-disabled') === 'true';
                        return !disabled && text === '发布'
                          && rect.width > 40 && rect.height > 20
                          && rect.bottom > window.innerHeight * 0.55;
                      })
                      .sort((a, b) => b.getBoundingClientRect().bottom - a.getBoundingClientRect().bottom);
                    return candidates[0] || null;
                }"""
            )
            element = handle.as_element()
            if element is not None:
                self.logger.info("发布按钮候选已选择", selector="dom_scan")
                return element
        except Exception as e:
            self.logger.debug("DOM 扫描发布按钮失败", reason=str(e)[:100])
        self.logger.error("未找到发布按钮")
        return None
