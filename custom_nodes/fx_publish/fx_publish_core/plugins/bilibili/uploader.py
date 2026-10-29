#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""B站（bilibili）视频上传器。

覆盖 member.bilibili.com 创作中心 web 投稿页的完整流程：
上传视频 → 等待转码 → 填标题/简介/标签 → 设置封面 → 选择分区 → 立即投稿。

注意：B站投稿页是 SPA，上传完成后不跳 URL，而是在同一页面切换到编辑表单，
因此上传完成的信号以"标题输入框出现"为准，投稿成功以"投稿成功"文案/稿件管理跳转为准。
"""

from datetime import datetime
from pathlib import Path
from typing import List, Optional

from playwright.async_api import Error, Page

from ...core.base_publisher import BasePublisher


class BilibiliUploader(BasePublisher):
    """B站视频上传器。

    可选偏好（由节点输入透传，setattr 注入）：
    - partition_pref: 指定分区名称（如 "vlog"/"游戏"），空 = 用账号记忆的默认分区
    - statement_pref: 指定创作声明（如 "含AI生成内容"），空 = 默认优先"含AI生成内容"
    """

    partition_pref: str = ""
    statement_pref: str = ""

    @property
    def platform_name(self) -> str:
        return "bilibili"

    @property
    def display_name(self) -> str:
        return "B站"

    @property
    def login_url(self) -> str:
        return "https://passport.bilibili.com/login"

    @property
    def publish_url(self) -> str:
        return "https://member.bilibili.com/platform/upload/video/frame"

    @property
    def _login_selectors(self) -> List[str]:
        return [
            ".login-btn",
            'text="扫码登录"',
            'text="短信登录"',
            "#login-username",
            ".btn-login",
        ]

    @property
    def _authed_selectors(self) -> List[str]:
        # 投稿页上传区域特征（登录后才渲染）
        return [
            "input[type='file']",
            ".bcc-upload-wrapper",
            'text="拖拽到此或点击上传"',
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
                    # domcontentloaded：member 页资源重，等 load 事件经常 30s 超时，
                    # 超时重试还会触发"确定要离开吗？"弹窗挡住后续操作
                    await page.goto(
                        self.publish_url, timeout=60000, wait_until="domcontentloaded"
                    )
                    await page.wait_for_timeout(3000)
                    await self._dismiss_popups(page)
                    return True

                await self._stable_step(page, "goto_upload_page", goto_upload_page)
                await self._stable_step(
                    page, "upload_video_file",
                    lambda: self._upload_video_file(page, file_path), retries=2,
                )
                await self._stable_step(
                    page, "wait_for_upload_complete",
                    lambda: self._wait_for_upload_complete(page), retries=2, delay=2.0,
                )
                await self._stable_step(
                    page, "fill_video_info",
                    lambda: self._fill_video_info(page, title, content, tags), retries=3,
                )
                await self._stable_step(
                    page, "set_statement",
                    lambda: self._ensure_statement(page), retries=1, required=False,
                )
                await self._stable_step(
                    page, "set_partition",
                    lambda: self._ensure_partition(page), retries=1, required=False,
                )
                await self._stable_step(
                    page, "set_thumbnail",
                    lambda: self._set_thumbnail(page, thumbnail_path), retries=1, required=False,
                )

                if await self._should_skip_publish(page):
                    return True

                # retries=0：投稿只点一次。B站投稿成功跳转慢，重试会重复投稿。
                await self._stable_step(
                    page, "publish_video",
                    lambda: self._publish_video(page), retries=0,
                )
            return True
        except Exception as e:
            self.logger.error("upload_video 异常", reason=str(e)[:200])
            return False

    async def _upload_video_file(self, page: Page, file_path: str | Path) -> bool:
        """注入视频文件并验证页面确实开始上传。

        2026-07 实测坑：落地页存在多个 file input，其中部分是不触发上传的
        诱饵（如 .bcc-upload-wrapper 下的备用 input）。注入后必须验证页面
        进入了上传/编辑态（出现 上传中/上传完成/稿件标题输入框），没反应
        就换下一个 input，最后用 file chooser 兜底。
        """

        async def upload_reacted() -> bool:
            for sel in [
                "input[placeholder*='稿件标题']",
                'text="上传完成"',
                'text="上传中"',
                'text="等待上传"',
            ]:
                try:
                    if await page.locator(sel).count() > 0:
                        return True
                except Error:
                    continue
            return False

        async def wait_reacted(seconds: int) -> bool:
            for _ in range(seconds):
                await page.wait_for_timeout(1000)
                if await upload_reacted():
                    return True
            return False

        try:
            # 幂等保护：页面已有视频（本步骤被 _stable_step 重试时），
            # 绝不能再注入一份——会变成批量上传（多分P + 打扰粉丝弹窗）
            if await upload_reacted():
                self.logger.info("页面已有视频，跳过重复注入")
                return True

            # 等上传区渲染出来（goto 刚完成时 file input 可能还没挂载）
            try:
                await page.wait_for_selector("input[type='file']", state="attached", timeout=15000)
            except Error:
                pass

            # 只注入一次：多个 input 挨个注入会导致双份视频（批量上传）。
            # 选第一个视频类 input（跳过封面图/字幕 txt），注入后等足 20s。
            inputs = page.locator("input[type='file']")
            target = None
            target_idx = -1
            for i in range(await inputs.count()):
                el = inputs.nth(i)
                accept = (await el.get_attribute("accept")) or ""
                if accept and ("image" in accept or ".txt" in accept):
                    continue
                target = el
                target_idx = i
                break
            if target is not None:
                try:
                    await target.set_input_files(file_path)
                    if await wait_reacted(20):
                        self.logger.info("视频文件已注入", input_index=target_idx)
                        return True
                    self.logger.warning("input 注入后页面无反应，刷新页面改走 file chooser")
                except Exception as e:
                    self.logger.warning("input 注入失败", index=target_idx, reason=str(e)[:80])

            # 注入无反应：reload 清掉可能的隐式状态，避免 chooser 造成双份
            try:
                await page.reload(timeout=60000, wait_until="domcontentloaded")
                await page.wait_for_timeout(5000)
            except Exception:
                pass
            if await upload_reacted():
                self.logger.info("刷新后发现视频已在队列（注入延迟生效）")
                return True

            # 兜底：点击"上传视频"按钮触发 file chooser
            for sel in ['text="上传视频"', ".upload-btn", ".bcc-upload-wrapper"]:
                try:
                    btn = page.locator(sel).first
                    if await btn.count() > 0 and await btn.is_visible():
                        async with page.expect_file_chooser(timeout=5000) as fc:
                            await btn.click(force=True)
                        await (await fc.value).set_files(file_path)
                        if await wait_reacted(20):
                            self.logger.info("视频文件已通过 file chooser 注入")
                            return True
                except Error:
                    continue
            self.logger.error("所有上传入口注入后页面均无反应")
            return False
        except Exception as e:
            self.logger.error("视频文件注入失败", reason=str(e)[:200])
            return False

    async def _wait_for_upload_complete(self, page: Page) -> bool:
        """等待编辑表单出现（标题输入框）。

        B站允许边上传边填写（页面原话："信息填完后，就可投稿！不需等待上传
        完成哦~"），因此这里只等编辑表单；投稿前 _publish_video 再确认上传完成。
        """

        async def check() -> bool:
            for txt in ["上传失败", "转码失败", "格式不支持", "上传出错"]:
                if await page.locator(f"text={txt}").count() > 0:
                    raise RuntimeError(txt)
            loc = page.locator("input[placeholder*='稿件标题']").first
            if await loc.count() > 0 and await loc.is_visible():
                return True
            return False

        return await self._wait_for_condition(
            check, timeout=120.0, interval=2.0, desc="upload_complete"
        )

    async def _dismiss_popups(self, page: Page) -> None:
        """关闭挡住表单的提示弹窗（如"批量上传将生成多条动态…"）。

        特殊处理"确定要离开吗？"：它只在我们主动导航/刷新后残留，点"确定"
        （确认离开）是安全的——此时我们本来就想进入新页面。
        """
        try:
            if await page.locator('text="确定要离开吗"').count() > 0:
                leave = page.locator('button:has-text("确定"), [class*="btn"]:has-text("确定")').first
                if await leave.count() > 0 and await leave.is_visible():
                    await leave.click(force=True, timeout=2000)
                    self.logger.debug("已确认离开弹窗")
                    await page.wait_for_timeout(600)
        except Exception:
            pass
        for text in ["暂不设置", "我知道了", "知道了", "跳过", "取消"]:
            try:
                loc = page.locator(f'button:has-text("{text}")').first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(force=True, timeout=2000)
                    self.logger.debug("已关闭提示弹窗", via=text)
                    await page.wait_for_timeout(400)
            except Exception:
                continue
        # 兜底：JS 点击文本命中的元素（弹窗按钮可能不是 button 标签）
        try:
            await page.evaluate(
                """() => {
                    for (const el of document.querySelectorAll('div, span')) {
                        const t = (el.innerText || '').trim();
                        if (t === '暂不设置' && el.children.length === 0) { el.click(); return; }
                    }
                }"""
            )
        except Exception:
            pass

    async def _fill_video_info(
        self, page: Page, title: str = "", content: str = "", tags: List[str] = None,
    ) -> bool:
        try:
            await self._dismiss_popups(page)
            title = (title or "")[:80]  # B站标题上限 80
            tags = tags or []

            # 1) 标题（注意 .input-val 同时匹配标题和标签输入框，placeholder 定位最准）
            title_input = await self._find_first_element(
                page,
                [
                    "input[placeholder*='稿件标题']",
                    "input[placeholder*='标题']",
                    ".video-title input",
                ],
                timeout=15000,
            )
            if title_input is None:
                self.logger.error("未找到标题输入框")
                return False
            if title:
                await title_input.click()
                # 清空默认填充的文件名标题
                await page.keyboard.press("ControlOrMeta+A")
                await page.keyboard.press("Backspace")
                await title_input.fill(title)
                await page.wait_for_timeout(300)

            # 2) 简介（Quill 富文本 .ql-editor）
            if content:
                desc = await self._find_first_element(
                    page,
                    [
                        ".ql-editor",
                        "div[contenteditable='true'][data-placeholder*='简介']",
                        ".archive-info-editor .ql-editor",
                        "div[contenteditable='true']",
                    ],
                    timeout=8000,
                )
                if desc is not None:
                    await desc.click()
                    await page.keyboard.insert_text(content)
                    await page.wait_for_timeout(300)
                else:
                    self.logger.warning("未找到简介编辑器，跳过简介")

            # 3) 标签（placeholder="按回车键Enter创建标签"）
            added = 0
            if tags:
                tag_input = await self._find_first_element(
                    page,
                    [
                        "input[placeholder*='创建标签']",
                        "input[placeholder*='回车']",
                        "input[placeholder*='标签']",
                        ".tag-container input",
                    ],
                    timeout=5000,
                )
                if tag_input is not None:
                    for tag in tags:
                        clean = tag.lstrip("#").strip()
                        if not clean:
                            continue
                        try:
                            await tag_input.click()
                            await tag_input.fill(clean)
                            await page.wait_for_timeout(200)
                            await page.keyboard.press("Enter")
                            await page.wait_for_timeout(200)
                            added += 1
                        except Exception as e:
                            self.logger.warning("标签添加失败", tag=clean, reason=str(e)[:80])
                else:
                    self.logger.warning("未找到标签输入框，跳过标签")

            await self._capture_step_snapshot(page, "fill_video_info", "success")
            self.logger.info("标题与标签已填充", added=added, total=len(tags))
            return True
        except Exception as e:
            self.logger.error("填写视频信息失败", reason=str(e)[:200])
            return False

    async def _ensure_statement(self, page: Page) -> bool:
        """创作声明为必填项（带 *），不填会在点投稿时被校验拦截。

        2026-07 实测 DOM：`.bcc-select` 内 readonly input
        `input.bcc-select-input-inner[placeholder*='创作声明']`，
        点击展开 `.bcc-select-list-wrap` 下拉后选择声明项。
        ComfyUI 工作流产出为 AI 生成内容，默认选择"含AI生成内容"。
        """
        try:
            await self._dismiss_popups(page)
            inp = page.locator("input.bcc-select-input-inner, input[placeholder*='创作声明']").first
            if await inp.count() == 0:
                self.logger.info("未找到创作声明控件（页面可能无此必填项）")
                return True
            current = ""
            try:
                current = await inp.input_value(timeout=2000)
            except Exception:
                pass
            if current.strip():
                self.logger.info("创作声明已填写", value=current[:30])
                return True

            # 用户指定的声明优先，其后是默认偏好
            prefer = []
            pref = (getattr(self, "statement_pref", "") or "").strip()
            if pref:
                prefer.append(pref)
            for fallback in ("含AI生成内容", "内容无需标注"):
                if fallback not in prefer:
                    prefer.append(fallback)

            # 点击展开下拉：真实鼠标点击 + 等 .bcc-option 渲染，最多重试 3 次
            #（JS/force click 偶发不触发下拉；fill 刚结束时页面浮层也可能挡一拍）
            options_ready = False
            for attempt in range(3):
                try:
                    box = await inp.bounding_box()
                except Exception:
                    box = None
                if box:
                    await page.mouse.click(
                        box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                    )
                else:
                    await inp.click(force=True)
                for _ in range(6):
                    await page.wait_for_timeout(500)
                    if await page.locator(".bcc-option").count() > 0:
                        options_ready = True
                        break
                if options_ready:
                    break
            if not options_ready:
                self.logger.warning("创作声明下拉未展开")
                return True

            picked = await page.evaluate(
                """(prefer) => {
                    const opts = document.querySelectorAll('.bcc-option');
                    for (const want of prefer) {
                        for (const el of opts) {
                            if ((el.innerText || '').trim() === want) {
                                const r = el.getBoundingClientRect();
                                if (r.width > 0) { el.click(); return want; }
                            }
                        }
                    }
                    return null;
                }""",
                prefer,
            )
            await page.wait_for_timeout(800)
            if picked:
                self.logger.info("创作声明已选择", statement=picked)
                return True
            self.logger.warning("创作声明下拉项未找到")
            return True  # 不阻断，真投稿被拦时 debug 现场可见
        except Exception as e:
            self.logger.warning("创作声明设置异常（不阻断）", reason=str(e)[:120])
            return True

    async def _get_partition_text(self, page: Page) -> str:
        """读当前分区选择器显示的文本（"请选择分区" = 未选）。"""
        try:
            return await page.evaluate(
                """() => {
                    const p = document.querySelector('.select-container .select-item-cont');
                    return p ? (p.innerText || '').trim() : '';
                }"""
            ) or ""
        except Exception:
            return ""

    async def _ensure_partition(self, page: Page) -> bool:
        """B站投稿分区为必填项（带 *）。

        2026-07 实测 DOM：`.select-container > .select-controller` 是触发器，
        当前值显示在 `.select-item-cont`；下拉项为 `.select-item-cont-inserted`
        等 [class*='select-item'] 元素。账号通常记住上次分区自动填充，
        未填充时选下拉里的第一个可用项。
        """
        try:
            await self._dismiss_popups(page)
            pref = (getattr(self, "partition_pref", "") or "").strip()
            selected = await self._get_partition_text(page)
            already_valid = bool(selected) and "请选择" not in selected

            # 未指定分区：已有（账号记忆的）分区就不动
            if not pref and already_valid:
                self.logger.info("分区已自动选择", partition=selected[:30])
                return True
            # 指定了分区且当前已是目标值
            if pref and selected == pref:
                self.logger.info("分区已是指定值", partition=pref)
                return True

            # 打开分区下拉——必须真实鼠标点击（JS/force click 不触发下拉渲染，
            # 2026-07 实测：此前"选择成功"其实都是账号记忆值的假象）
            SNAP_JS = """() => {
                const arr = [];
                for (const el of document.querySelectorAll('*')) {
                    const own = Array.from(el.childNodes).filter(n => n.nodeType === 3)
                        .map(n => n.textContent.trim()).join('');
                    if (own && own.length <= 12) {
                        const r = el.getBoundingClientRect();
                        if (r.width > 10 && r.height > 8) {
                            arr.push(own + '@' + Math.round(r.x) + ',' + Math.round(r.y));
                        }
                    }
                }
                return arr;
            }"""
            trigger = None
            for sel in [
                ".select-container .select-controller",
                ".select-container",
                ".video-human-type .selector-container",
            ]:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    trigger = loc
                    break
            if trigger is None:
                self.logger.warning("未找到分区选择控件")
                return True

            before = set(await page.evaluate(SNAP_JS))
            new_items = []
            for attempt in range(3):
                try:
                    box = await trigger.bounding_box()
                except Exception:
                    box = None
                if box:
                    await page.mouse.click(
                        box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                    )
                else:
                    await trigger.click(force=True)
                await page.wait_for_timeout(1500)
                after = set(await page.evaluate(SNAP_JS))
                new_items = [s for s in after - before if "@" in s]
                if len(new_items) >= 3:  # 出现一批新短文本 = 下拉已展开
                    break
            if len(new_items) < 3:
                self.logger.warning(
                    "分区下拉未展开，保持账号记忆分区", pref=pref, now=selected[:30]
                )
                return True

            # 在新增项中找目标分区（精确 → 包含），真实鼠标点击其坐标
            def _parse(s):
                text, _, xy = s.rpartition("@")
                x, y = xy.split(",")
                return text, float(x), float(y)

            parsed = [_parse(s) for s in new_items]
            target = None
            if pref:
                target = next((p for p in parsed if p[0] == pref), None) or next(
                    (p for p in parsed if pref in p[0]), None
                )
                if target is None:
                    self.logger.warning(
                        "指定分区不在下拉中，保持现状",
                        pref=pref,
                        options=[p[0] for p in parsed][:15],
                    )
                    await page.keyboard.press("Escape")
                    return True
            elif not already_valid:
                target = parsed[0] if parsed else None

            if target is not None:
                await page.mouse.click(target[1] + 10, target[2] + 8)
                await page.wait_for_timeout(1500)

            selected = await self._get_partition_text(page)
            if selected and "请选择" not in selected and (not pref or pref in selected or selected in (target[0] if target else "")):
                self.logger.info("分区已选择", partition=selected[:30])
                return True
            self.logger.warning("分区选择未生效", now=selected[:30], pref=pref)
            return True  # 不阻断（no_publish 调试仍可继续；真投稿时平台会拦截提示）
        except Exception as e:
            self.logger.warning("分区选择异常（不阻断）", reason=str(e)[:120])
            return True

    async def _set_thumbnail(
        self, page: Page, thumbnail_path: Optional[str | Path],
    ) -> bool:
        if not thumbnail_path:
            self.logger.info("无封面，跳过")
            return True
        if not Path(thumbnail_path).exists():
            self.logger.warning("封面文件不存在，跳过", path=str(thumbnail_path))
            return True

        try:
            # 1) JS 点击封面区的"封面设置"入口（.cover-content .edit-text，
            #    hover 才显示的浮层，locator click 不可用）
            opened = await page.evaluate(
                """() => {
                    const edit = document.querySelector('.cover-content .edit-text');
                    if (edit) { edit.click(); return 'edit-text'; }
                    const cov = document.querySelector('.cover-content .cover-main, .cover-content');
                    if (cov) { cov.click(); return 'cover-main'; }
                    return null;
                }"""
            )
            if not opened:
                self.logger.warning("未找到封面入口，跳过")
                return True

            # 2) 等待封面编辑器（micro-app cover-editor）
            try:
                await page.wait_for_selector(
                    "[class*='cover-editor']", state="visible", timeout=10000,
                )
            except Error:
                self.logger.warning("封面编辑器未出现，跳过")
                return True
            await page.wait_for_timeout(1500)

            # 3) 上传自定义封面（编辑器内"上传封面"区块，input accept=image/png,jpeg）
            uploaded = await self._upload_file_to_first(
                page,
                [
                    ".cover-upload input[type='file']",
                    "input[type='file'][accept*='image']",
                ],
                thumbnail_path,
                timeout=8000,
            )
            if not uploaded:
                self.logger.warning("未找到封面上传 input，使用默认封面")
                await page.keyboard.press("Escape")
                return True

            # 等图片加载到画布
            await page.wait_for_timeout(4000)

            # 4) 点击编辑器右下角的"完成"——2026-07 实测 DOM：
            #    <div class="cover-editor-button cover-editor-content-right-bottom">
            #        <div class="button"> 取消 </div><div class="button submit"> 完成 </div>
            #    是 div 不是 button，用 JS 精确点击 .button.submit
            clicked_finish = await page.evaluate(
                """() => {
                    const submit = document.querySelector(
                        '.cover-editor-button .button.submit, .cover-editor-button .submit'
                    );
                    if (submit) {
                        const r = submit.getBoundingClientRect();
                        submit.click();
                        return { via: 'submit', x: Math.round(r.x), y: Math.round(r.y) };
                    }
                    // 兜底：全文档扫描自身文本为"完成"的可见元素
                    for (const el of document.querySelectorAll('div, button, span')) {
                        const own = Array.from(el.childNodes)
                            .filter(n => n.nodeType === 3)
                            .map(n => n.textContent.trim()).join('');
                        if (own !== '完成') continue;
                        const r = el.getBoundingClientRect();
                        if (r.width < 20 || r.height < 10) continue;
                        el.click();
                        return { via: 'scan', x: Math.round(r.x), y: Math.round(r.y) };
                    }
                    return null;
                }"""
            )
            if clicked_finish:
                self.logger.info("已点击封面完成", pos=str(clicked_finish))
            else:
                self.logger.warning("未找到封面完成按钮")

            # 5) 等编辑器关闭
            editor_closed = True
            try:
                await page.wait_for_selector(
                    "[class*='cover-editor']", state="hidden", timeout=20000,
                )
            except Error:
                editor_closed = False
                self.logger.warning("封面编辑器未在 20s 内关闭")
            if not editor_closed:
                await self._capture_step_snapshot(page, "set_thumbnail", "editor_stuck")
            self.logger.info("封面设置完成", editor_closed=editor_closed)
            return True
        except Exception as e:
            self.logger.error("封面设置失败", reason=str(e)[:200])
            return True  # 封面失败不阻断发布

    async def _publish_video(self, page: Page) -> bool:
        try:
            await self._dismiss_popups(page)
            # 投稿前确认上传/转码完成（页面允许边传边填，但真投稿前等完整更稳）
            async def upload_done() -> bool:
                if await page.locator('text="上传完成"').count() > 0:
                    return True
                # 没有"上传中"也视为完成
                return await page.locator('text="上传中"').count() == 0

            await self._wait_for_condition(
                upload_done, timeout=300.0, interval=2.0, desc="video_transcoded"
            )

            async def check_success() -> bool:
                cur = page.url
                if "/platform/upload-manager" in cur or "success" in cur:
                    return True
                for txt in ["投稿成功", "稿件投递成功", "提交成功", "稿件已提交"]:
                    if await page.locator(f"text={txt}").count() > 0:
                        return True
                return False

            # 只点一次投稿——B站投稿成功后跳转较慢（实测 ~45s+），若点完
            # 没在超时内确认就重试，会重复投稿（今晚出现过一稿变多条）。
            # 因此：点一次 → 长等待确认；确认失败也不再点第二次，直接返回。
            clicked = await self._click_first_visible(
                page,
                [
                    "span.submit-add",
                    ".submit-add",
                    'span:has-text("立即投稿")',
                    'button:has-text("立即投稿")',
                    'button:has-text("投稿")',
                ],
                force=True, timeout=8000,
            )
            if not clicked:
                self.logger.error("未找到投稿按钮")
                return False
            self.logger.info("已点击投稿按钮")
            await self._capture_step_snapshot(page, "publish_video", "after_click")

            ok = await self._wait_for_condition(
                check_success, timeout=90.0, interval=2.0, desc="publish_result"
            )
            if not ok:
                self.logger.error("投稿结果未确认（不重试以避免重复投稿）", url=page.url)
            return ok
        except Exception as e:
            self.logger.error("发布失败", reason=str(e)[:200])
            return False
