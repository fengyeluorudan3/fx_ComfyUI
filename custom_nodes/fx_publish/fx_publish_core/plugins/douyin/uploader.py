from datetime import datetime
from pathlib import Path
from typing import List, Optional
import re
import time

from playwright.async_api import Page, Error
from ...core.base_publisher import BasePublisher


class DouYinUploader(BasePublisher):
    """
    抖音视频上传器
    """

    @property
    def platform_name(self) -> str:
        return "douyin"

    @property
    def display_name(self) -> str:
        return "抖音"

    @property
    def login_url(self) -> str:
        return "https://creator.douyin.com/"

    @property
    def publish_url(self) -> str:
        return "https://creator.douyin.com/creator-micro/content/upload"

    @property
    def _login_selectors(self) -> List[str]:
        return ['text="手机号登录"', 'text="扫码登录"', 'text="登录"', ".login-btn"]

    @property
    def _authed_selectors(self) -> List[str]:
        return [
            "input[placeholder*='填写作品标题']",
            "input[placeholder*='作品标题']",
            "div.semi-upload",
        ]

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
                    await page.goto(self.publish_url)
                    try:
                        await page.wait_for_url(self.publish_url, timeout=5000)
                    except Error:
                        pass
                    return True

                await self._stable_step(page, "goto_upload_page", goto_upload_page)

                await self._stable_step(
                    page,
                    "upload_video_file",
                    lambda: self._upload_video_file(page, file_path),
                    retries=2,
                )

                await self._stable_step(
                    page,
                    "wait_for_upload_complete",
                    lambda: self._wait_for_upload_complete(page),
                    retries=2,
                    delay=2.0,
                )

                await self._stable_step(
                    page,
                    "fill_video_info",
                    lambda: self._fill_video_info(page, title, content, tags),
                    retries=3,
                )

                await self._stable_step(
                    page,
                    "set_thumbnail",
                    lambda: self._set_thumbnail(page, thumbnail_path),
                    retries=1,
                    required=False,
                )

                await self._stable_step(
                    page,
                    "set_third_party_platforms",
                    lambda: self._set_third_party_platforms(page),
                    retries=1,
                    required=False,
                )

                if publish_date:
                    await self._stable_step(
                        page,
                        "set_schedule_time",
                        lambda: self._set_schedule_time(page, publish_date),
                        retries=2,
                    )

                await self._stable_step(
                    page,
                    "handle_auto_video_cover",
                    lambda: self._handle_auto_video_cover(page),
                    retries=1,
                    required=False,
                )

                if await self._should_skip_publish(page):
                    return True

                await self._stable_step(
                    page,
                    "publish_video",
                    lambda: self._publish_video(page),
                    retries=2,
                    delay=2.0,
                )
            return True
        except Exception as e:
            self.logger.error("upload_video 异常", reason=str(e)[:200])
            return False

    async def _upload_video_file(self, page: Page, file_path: str | Path) -> bool:
        try:
            inp = page.locator("input[type='file'][accept*='video']").first
            if await inp.count() == 0:
                inp = page.locator("div[class^='container'] input[type='file']").first
            await inp.wait_for(state="attached", timeout=10000)
            await inp.set_input_files(file_path)
            return True
        except Exception as e:
            self.logger.error("视频文件注入失败", reason=str(e)[:200])
            return False

    async def _wait_for_upload_complete(self, page: Page) -> bool:
        """轮询直到视频上传处理完成，避免表单初始元素造成误判。

        2026-07 实测的可靠信号（headless 下也稳定）：
        - 文件被接收后 URL 从 /content/upload 跳到 /content/post/video 编辑页；
        - 编辑页出现「重新上传」按钮 / 「选择封面」封面区；
        - 底部主「发布」按钮可点击（无 disabled）。
        旧版依赖 猜类名的 preview 选择器 + "上传成功"暂态 toast，headless 下超时。
        """

        async def check() -> bool:
            for txt in ["上传失败", "处理失败", "视频格式不支持"]:
                if await page.locator(f"text={txt}").count() > 0:
                    raise RuntimeError(txt)

            # 明确完成文案（暂态 toast，逮到就赢）
            for txt in ["上传成功", "已上传", "处理完成"]:
                if await page.locator(f"text={txt}").count() > 0:
                    return True

            state = await page.evaluate(
                """() => {
                    const bodyText = document.body ? document.body.innerText : '';
                    // 编辑页特征：重新上传按钮 / 封面设置区
                    const hasEditMarkers = bodyText.includes('重新上传')
                        || bodyText.includes('选择封面')
                        || bodyText.includes('智能推荐封面');
                    // 可用的主发布按钮
                    let publishReady = false;
                    for (const el of document.querySelectorAll("button, [role='button']")) {
                        const text = (el.innerText || '').trim();
                        if (text !== '发布') continue;
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        const visible = rect.width > 20 && rect.height > 20
                            && style.visibility !== 'hidden' && style.display !== 'none';
                        const disabled = el.disabled
                            || el.getAttribute('aria-disabled') === 'true'
                            || /disabled/.test(el.className || '');
                        if (visible && !disabled) { publishReady = true; break; }
                    }
                    return { hasEditMarkers, publishReady, url: location.href };
                }"""
            )
            on_edit_page = "/content/post/video" in state.get("url", "")
            if (on_edit_page or state.get("hasEditMarkers")) and state.get("publishReady"):
                return True
            return False

        return await self._wait_for_condition(
            check, timeout=180.0, interval=1.5, desc="upload_complete"
        )

    async def _fill_video_info(
        self,
        page: Page,
        title: str = "",
        content: str = "",
        tags: List[str] = None,
    ) -> bool:
        try:
            await self._dismiss_guides(page)
            if not await self._wait_for_metadata_area(page, timeout=15000):
                raise RuntimeError("未检测到作品描述输入区域")

            title_text = title[:30]
            title_filled = await self._fill_title(page, title_text)

            desc_parts = []
            if content:
                desc_parts.append(content)
            added = 0
            for tag in tags or []:
                clean_tag = tag.lstrip("#")
                if clean_tag:
                    desc_parts.append(f"#{clean_tag}")
                    added += 1
            desc_text = " ".join(desc_parts)
            desc_filled = await self._fill_description(page, desc_text)

            await page.wait_for_timeout(500)
            # 标题在 <input>/<textarea> 里，值不出现在 body.inner_text，
            # 需要读控件自身的值；正文/标签在 contenteditable 区域，用 inner_text 校验。
            title_ok = not title_text or await self._verify_title_filled(page, title_text)
            if not title_ok:
                raise RuntimeError("标题填写后未在输入框检测到标题文本")
            page_text = await page.locator("body").inner_text(timeout=5000)
            if desc_text and not any(part in page_text for part in desc_parts if part):
                raise RuntimeError("简介/标签填写后页面未检测到对应文本")

            self.logger.info(
                "标题与标签已填充",
                title=title_filled,
                description=desc_filled,
                added=added,
                total=len(tags or []),
            )
            await self._capture_step_snapshot(page, "fill_video_info", "success")
            return True
        except Exception as e:
            self.logger.error("填写视频信息失败", reason=str(e)[:200])
            return False

    async def _verify_title_filled(self, page: Page, title_text: str) -> bool:
        """校验标题已填入：读输入框/可编辑区的实际值（不依赖 body inner_text）。"""
        for sel in [
            "input[placeholder*='作品标题']",
            "textarea[placeholder*='作品标题']",
        ]:
            loc = page.locator(sel).first
            try:
                if await loc.count() > 0:
                    val = await loc.input_value(timeout=2000)
                    if title_text in (val or ""):
                        return True
            except Exception:
                continue
        # contenteditable 形态的标题
        try:
            editable = page.locator(".notranslate, [contenteditable='true']").first
            if await editable.count() > 0:
                txt = await editable.inner_text(timeout=2000)
                if title_text in (txt or ""):
                    return True
        except Exception:
            pass
        return False

    async def _dismiss_guides(self, page: Page) -> None:
        """关闭抖音发布页的新手引导、预览提示等浮层。"""
        for _ in range(3):
            clicked = False
            for text in ["我知道了", "知道了", "跳过"]:
                loc = page.get_by_text(text, exact=False).first
                try:
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click(force=True, timeout=2000)
                        await page.wait_for_timeout(300)
                        clicked = True
                except Exception:
                    continue
            if not clicked:
                break
        try:
            await page.evaluate("""
() => {
  document.querySelectorAll(
    '.shepherd-element, .shepherd-modal-overlay-container, .semi-popover-wrapper, .douyin-creator-pc-master__wrap'
  ).forEach((el) => el.remove());
}
""")
        except Exception:
            pass

    async def _wait_for_metadata_area(self, page: Page, timeout: int = 15000) -> bool:
        selectors = [
            "input[placeholder*='作品标题']",
            "textarea[placeholder*='作品标题']",
            "textarea[placeholder*='作品简介']",
            "[contenteditable='true']",
            ".input-text-kyWuPE",
        ]
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            for text in ["作品描述", "填写作品标题", "添加作品简介", "#添加话题"]:
                loc = page.get_by_text(text, exact=False).first
                try:
                    if await loc.count() > 0 and await loc.is_visible():
                        return True
                except Exception:
                    pass
            for selector in selectors:
                loc = page.locator(selector).first
                try:
                    if await loc.count() > 0 and await loc.is_visible():
                        return True
                except Exception:
                    pass
            await page.wait_for_timeout(500)
        return False

    async def _fill_title(self, page: Page, title: str) -> bool:
        if not title:
            return True
        for sel in [
            "input[placeholder*='作品标题']",
            "textarea[placeholder*='作品标题']",
        ]:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.fill(title)
                return True

        placeholder = page.get_by_text("填写作品标题", exact=False).first
        if await placeholder.count() > 0 and await placeholder.is_visible():
            await placeholder.click(force=True)
            await page.keyboard.insert_text(title)
            await page.wait_for_timeout(300)
            return True

        fallback = page.locator(".notranslate, [contenteditable='true']").first
        if await fallback.count() > 0 and await fallback.is_visible():
            await fallback.click(force=True)
            await page.keyboard.insert_text(title)
            await page.wait_for_timeout(300)
            return True
        raise RuntimeError("未找到标题输入区域")

    async def _fill_description(self, page: Page, text: str) -> bool:
        if not text:
            return True
        for sel in [
            "textarea[placeholder*='作品简介']",
            "textarea[placeholder*='简介']",
            ".zone-container",
        ]:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(force=True)
                try:
                    await loc.fill(text)
                except Exception:
                    await page.keyboard.insert_text(text)
                await page.wait_for_timeout(300)
                return True

        placeholder = page.get_by_text("添加作品简介", exact=False).first
        if await placeholder.count() > 0 and await placeholder.is_visible():
            await placeholder.click(force=True)
            await page.keyboard.insert_text(text)
            await page.wait_for_timeout(300)
            return True

        editable = page.locator("[contenteditable='true']").nth(1)
        if await editable.count() > 0 and await editable.is_visible():
            await editable.click(force=True)
            await page.keyboard.insert_text(text)
            await page.wait_for_timeout(300)
            return True
        raise RuntimeError("未找到简介输入区域")

    async def _set_thumbnail(
        self,
        page: Page,
        thumbnail_path: Optional[str | Path],
    ) -> bool:
        if not thumbnail_path:
            self.logger.info("未指定封面，跳过")
            return True
        if not Path(thumbnail_path).exists():
            self.logger.warning("封面文件不存在，跳过", path=str(thumbnail_path))
            return True

        modal_sel = "[class*='dy-creator-content-modal']"
        try:
            # 0) 先关闭新手引导浮层，否则会挡住封面入口
            await self._dismiss_guides(page)

            # 1) 点击"选择封面"打开弹窗；弹窗有时首次点击不响应，做最多 3 次点击+等待
            opened = False
            for attempt in range(3):
                await self._dismiss_guides(page)
                # 精确定位封面区的"选择封面"文字元素并点击（JS 点击最稳）
                clicked = await page.evaluate(
                    """() => {
                        const nodes = Array.from(document.querySelectorAll('*'));
                        const target = nodes.find(el => {
                            const t = (el.innerText || '').trim();
                            if (t !== '选择封面') return false;
                            const r = el.getBoundingClientRect();
                            return r.width > 20 && r.height > 20;
                        });
                        if (target) { target.click(); return true; }
                        return false;
                    }"""
                )
                if not clicked:
                    await self._click_first_visible(
                        page,
                        ['text="选择封面"', 'button:has-text("选择封面")'],
                        force=True,
                        timeout=3000,
                    )
                try:
                    await page.wait_for_selector(modal_sel, state="visible", timeout=6000)
                    opened = True
                    break
                except Error:
                    self.logger.debug("封面弹窗未出现，重试点击", attempt=attempt + 1)
                    await page.wait_for_timeout(1000)
            if not opened:
                await self._capture_step_snapshot(page, "set_thumbnail", "modal_not_open")
                self.logger.warning("封面弹窗未出现，跳过")
                return True
            await page.wait_for_timeout(1000)

            # 3) 弹窗内上传自定义封面图片（image input 是 .semi-upload-hidden-input）
            upload_selectors = [
                "[class*='dy-creator-content-modal'] input.semi-upload-hidden-input",
                "[class*='dy-creator-content-modal'] input[type='file'][accept*='image']",
                "input.semi-upload-hidden-input",
                "input[type='file'][accept*='image']",
            ]
            if not await self._upload_file_to_first(
                page, upload_selectors, thumbnail_path, timeout=10000
            ):
                self.logger.error("未找到封面图片上传 input")
                return False

            # 等待图片上传 + 封面检测完成
            await page.wait_for_timeout(3000)

            # 4) 点击弹窗内的"完成"
            if not await self._click_first_visible(
                page,
                [
                    "[class*='dy-creator-content-modal'] button:has-text('完成')",
                    "button:has-text('完成')",
                ],
                force=True,
                timeout=5000,
            ):
                self.logger.error("未能点击完成按钮")
                return False

            # 5) 点完成后常弹出"设置横封面获得更多流量"二次提示，会挡住发布按钮，
            #    点"暂不设置"关闭它（找不到则忽略）
            await page.wait_for_timeout(1500)
            for _ in range(2):
                closed = await self._dismiss_horizontal_cover_hint(page)
                if not closed:
                    break
                await page.wait_for_timeout(500)

            self.logger.info("封面设置完成")
            return True
        except Exception as e:
            self.logger.error("封面设置失败", reason=str(e)[:200])
            return True  # 封面失败不影响发布

    async def _dismiss_horizontal_cover_hint(self, page: Page) -> bool:
        """关闭"设置横封面获得更多流量"提示弹窗，返回是否点到了。"""
        for text in ["暂不设置", "暂不", "取消"]:
            loc = page.locator(f'button:has-text("{text}")').first
            try:
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(force=True, timeout=2000)
                    self.logger.debug("已关闭横封面提示弹窗", via=text)
                    return True
            except Exception:
                continue
        # 兜底：点弹窗右上角关闭 X
        try:
            x = page.locator("[class*='dy-creator-content-modal'] [class*='close']").first
            if await x.count() > 0 and await x.is_visible():
                await x.click(force=True, timeout=2000)
                return True
        except Exception:
            pass
        return False

    async def _set_schedule_time(self, page: Page, publish_date: datetime) -> bool:
        try:
            await page.locator("[class^='radio']:has-text('定时发布')").click()
            await page.wait_for_selector(
                '.semi-input[placeholder="日期和时间"]', state="visible", timeout=5000
            )
            time_str = publish_date.strftime("%Y-%m-%d %H:%M")
            inp = page.locator('.semi-input[placeholder="日期和时间"]')
            await inp.click()
            await page.keyboard.press("ControlOrMeta+A")
            await page.keyboard.insert_text(time_str)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(500)
            self.logger.info("定时发布时间设置完成")
            return True
        except Exception as e:
            self.logger.error("定时发布设置失败", reason=str(e)[:200])
            return False

    async def _set_third_party_platforms(self, page: Page) -> bool:
        """关闭第三方平台同步开关。"""
        try:
            switch = page.locator(
                '[class^="info"] > [class^="first-part"] div div.semi-switch'
            )
            if await switch.count() > 0:
                is_checked = "semi-switch-checked" in (
                    await switch.evaluate("el => el.className")
                )
                if not is_checked:
                    await switch.locator("input.semi-switch-native-control").click()
            return True
        except Exception as e:
            self.logger.error("第三方平台设置失败", reason=str(e)[:100])
            return True  # 不影响整体上传

    async def _handle_auto_video_cover(self, page: Page) -> bool:
        """处理必须设置封面的情况：选择推荐封面并确认。"""
        try:
            if not await page.get_by_text("请设置封面后再发布").first.is_visible():
                return True

            recommend_cover = page.locator('[class^="recommendCover-"]').first
            if await recommend_cover.count() == 0:
                return True

            await recommend_cover.click()
            await page.wait_for_timeout(500)

            # 确认弹窗
            if await page.get_by_text("是否确认应用此封面？").first.is_visible():
                await page.get_by_role("button", name="确定").click()
                await page.wait_for_timeout(500)
            return True
        except Exception as e:
            self.logger.error("自动封面设置失败", reason=str(e)[:200])
            return False

    async def _set_location(self, page: Page, location: str) -> bool:
        """
        设置地理位置

        Args:
            page: 页面实例
            location: 地理位置

        Returns:
            是否成功设置地理位置
        """
        try:
            await page.locator('div.semi-select span:has-text("输入地理位置")').click()
            await page.keyboard.press("Backspace")
            await page.wait_for_timeout(2000)
            await page.keyboard.type(location)
            await page.wait_for_selector(
                'div[role="listbox"] [role="option"]', timeout=5000
            )
            await page.locator('div[role="listbox"] [role="option"]').first.click()
            self.logger.info(f"成功设置地理位置: {location}")
            return True
        except Exception as e:
            self.logger.error(f"设置地理位置时出错: {e}")
            return False

    async def _set_product_link(
        self, page: Page, product_link: str, product_title: str
    ):
        """
        设置商品链接

        Args:
            page: 页面实例
            product_link: 商品链接
            product_title: 商品标题
        """
        await page.wait_for_timeout(2000)
        try:
            await page.wait_for_selector("text=添加标签", timeout=10000)
            dropdown = (
                page.get_by_text("添加标签")
                .locator("..")
                .locator("..")
                .locator("..")
                .locator(".semi-select")
                .first
            )
            if not await dropdown.count():
                self.logger.error("未找到标签下拉框")
                return False

            self.logger.debug("找到标签下拉框，准备选择'购物车'")
            await dropdown.click()
            await page.wait_for_selector('[role="listbox"]', timeout=5000)
            await page.locator('[role="option"]:has-text("购物车")').click()
            self.logger.debug("成功选择'购物车'")

            await page.wait_for_selector(
                'input[placeholder="粘贴商品链接"]', timeout=5000
            )
            input_field = page.locator('input[placeholder="粘贴商品链接"]')
            await input_field.fill(product_link)
            self.logger.debug(f"已输入商品链接: {product_link}")

            add_button = page.locator('span:has-text("添加链接")')
            button_class = await add_button.get_attribute("class")
            if "disable" in button_class:
                self.logger.error("'添加链接'按钮不可用")
                return False

            await add_button.click()
            self.logger.debug("成功点击'添加链接'按钮")
            await page.wait_for_timeout(2000)

            error_modal = page.locator("text=未搜索到对应商品")
            if await error_modal.count():
                confirm_button = page.locator('button:has-text("确定")')
                await confirm_button.click()
                self.logger.error("商品链接无效")
                return False

            if not await self._handle_product_dialog(page, product_title):
                return False

            self.logger.debug("成功设置商品链接")
            return True

        except Exception as e:
            self.logger.error(f"设置商品链接时出错: {str(e)}")
            return False

    async def _handle_product_dialog(self, page: Page, product_title: str) -> bool:
        """
        处理商品编辑弹窗

        Args:
            page: 页面实例
            product_title: 商品标题

        Returns:
            是否成功处理
        """
        await page.wait_for_timeout(2000)
        await page.wait_for_selector(
            'input[placeholder="请输入商品短标题"]', timeout=10000
        )
        short_title_input = page.locator('input[placeholder="请输入商品短标题"]')
        if not await short_title_input.count():
            self.logger.error("未找到商品短标题输入框")
            return False

        product_title = product_title[:10]
        await short_title_input.fill(product_title)
        await page.wait_for_timeout(1000)

        finish_button = page.locator('button:has-text("完成编辑")')
        if "disabled" not in await finish_button.get_attribute("class"):
            await finish_button.click()
            self.logger.debug("成功点击'完成编辑'按钮")
            await page.wait_for_selector(
                ".semi-modal-content", state="hidden", timeout=5000
            )
            return True
        else:
            self.logger.error("'完成编辑'按钮处于禁用状态，尝试直接关闭对话框")
            cancel_button = page.locator('button:has-text("取消")')
            if await cancel_button.count():
                await cancel_button.click()
            else:
                close_button = page.locator(".semi-modal-close")
                await close_button.click()

            await page.wait_for_selector(
                ".semi-modal-content", state="hidden", timeout=5000
            )
            return False

    async def _publish_video(self, page: Page) -> bool:
        try:
            await self._dismiss_guides(page)
            publish_button = await self._find_main_publish_button(page)
            if publish_button is None:
                self.logger.error("未找到发布按钮")
                return False

            if not await publish_button.is_enabled():
                self.logger.error("发布按钮不可用")
                return False

            before_url = page.url
            await self._capture_step_snapshot(page, "publish_video", "before_click")
            await publish_button.scroll_into_view_if_needed(timeout=5000)
            await publish_button.click(force=True, timeout=10000)
            self.logger.info("已点击发布按钮", url=page.url)
            await page.wait_for_timeout(1000)
            await self._capture_step_snapshot(page, "publish_video", "after_click")

            async def check_publish_result() -> bool:
                current_url = page.url
                if re.search(r"/content/manage|/manage/video|/creator-micro/content/manage", current_url):
                    self.logger.info("发布后已跳转", url=current_url)
                    return True
                for txt in ["发布成功", "提交成功", "作品发布成功", "发布后将进入审核", "审核中"]:
                    if await page.locator(f"text={txt}").count() > 0:
                        self.logger.info("检测到发布成功提示", text=txt)
                        return True
                for txt in ["请设置封面", "上传失败", "发布失败", "网络异常", "请先上传视频", "标题不能为空"]:
                    loc = page.locator(f"text={txt}").first
                    if await loc.count() > 0 and await loc.is_visible():
                        raise RuntimeError(f"发布被页面拦截: {txt}")
                return False

            ok = await self._wait_for_condition(
                check_publish_result, timeout=45.0, interval=1.5, desc="publish_result"
            )
            if not ok:
                self.logger.error("发布结果未确认", before_url=before_url, current_url=page.url)
            return ok
        except Error as e:
            self.logger.error("发布失败", reason=str(e)[:200])
            return False
        except Exception as e:
            self.logger.error("发布失败", reason=str(e)[:200])
            return False

    async def _find_main_publish_button(self, page: Page):
        """选择页面底部真正的主发布按钮，避免命中预览区/提示里的文字。"""
        candidates = await page.locator("button, [role='button']").evaluate_all("""
(nodes) => nodes.map((el, index) => {
  const rect = el.getBoundingClientRect();
  const text = (el.innerText || el.textContent || '').trim();
  const style = window.getComputedStyle(el);
  return {
    index,
    text,
    x: rect.x,
    y: rect.y,
    width: rect.width,
    height: rect.height,
    visible: rect.width > 20 && rect.height > 20 && style.visibility !== 'hidden' && style.display !== 'none',
    disabled: el.disabled || el.getAttribute('aria-disabled') === 'true' || /disabled/.test(el.className || '')
  };
}).filter((item) => item.visible && !item.disabled && item.text === '发布')
  .sort((a, b) => b.y - a.y || b.width - a.width)
""")
        if not candidates:
            self.logger.error("未找到可见可点击发布按钮候选")
            return None
        chosen = candidates[0]
        self.logger.info(
            "发布按钮候选已选择",
            count=len(candidates),
            x=round(chosen["x"], 1),
            y=round(chosen["y"], 1),
            width=round(chosen["width"], 1),
            height=round(chosen["height"], 1),
        )
        return page.locator("button, [role='button']").nth(chosen["index"])
