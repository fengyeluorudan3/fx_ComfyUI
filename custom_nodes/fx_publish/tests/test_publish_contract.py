import asyncio
import ast
import json
from pathlib import Path

import pytest

from fx_publish import nodes


def test_publish_workers_do_not_overlap():
    from concurrent.futures import ThreadPoolExecutor
    import threading
    active = 0
    peak = 0
    lock = threading.Lock()
    async def work():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(0.02)
        with lock:
            active -= 1
        return True
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: nodes._run_async(work()), range(3)))
    assert all(results)
    assert peak == 1


def test_cdp_connect_failure_is_bounded_and_cleans_driver(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fx_publish.fx_publish_core.core import browser as module
    monkeypatch.setattr(module, '_CDP_FAILURES', {})
    driver = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(side_effect=TimeoutError())), stop=AsyncMock())
    monkeypatch.setattr(module, 'async_playwright', lambda: SimpleNamespace(start=AsyncMock(return_value=driver)))
    monkeypatch.setattr(module, 'cdp_endpoint_alive', lambda _: 'ws://127.0.0.1:9222/devtools/browser/test')
    with pytest.raises(RuntimeError, match='Chrome 调试连接未完成'):
        asyncio.run(module.StealthBrowser.create(cdp_port=9222))
    assert driver.chromium.connect_over_cdp.call_args.kwargs['timeout'] == 45000
    driver.stop.assert_awaited_once()


@pytest.mark.parametrize("platform", ["douyin", "xiaohongshu", "kuaishou", "shipinhao", "bilibili"])
def test_final_submission_is_never_retried_automatically(platform):
    path = Path(nodes.__file__).parent / "fx_publish_core" / "plugins" / platform / "uploader.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_stable_step"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "publish_video"
    ]
    assert len(calls) == 1
    retries = next((kw.value.value for kw in calls[0].keywords if kw.arg == "retries" and isinstance(kw.value, ast.Constant)), None)
    assert retries == 0


def test_xiaohongshu_final_submit_has_no_second_input_fallback():
    path = Path(nodes.__file__).parent / "fx_publish_core/plugins/xiaohongshu/uploader.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    publish = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "_publish_video")
    calls = [node for node in ast.walk(publish) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
    assert sum(node.func.attr == "click" and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "mouse" for node in calls) == 1
    assert not any(node.func.attr == "press" and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "keyboard" for node in calls)


@pytest.mark.parametrize("no_publish,expected", [("yes", "preview_ready"), ("no", "success")])
def test_node_receipt_distinguishes_preview_from_submission(tmp_path, monkeypatch, no_publish, expected):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"test")

    class FakeUploader:
        run_id = "test-run"
        debug_dir = tmp_path
        last_error = ""

        def __init__(self, **kwargs):
            pass

        async def upload_video_flow(self, **kwargs):
            return True

    monkeypatch.setattr(nodes, "_get_uploader_cls", lambda platform: FakeUploader)
    result = asyncio.run(nodes._publish_async(
        "douyin", None, str(video), "标题", "文案", "", None, "", "", "no", 0, no_publish,
    ))
    assert result["status"] == expected


def test_browser_keep_open_is_per_run_not_process_global(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"test")
    seen = []

    class FakeUploader:
        run_id = "test-run"
        debug_dir = tmp_path
        last_error = ""

        def __init__(self, **kwargs):
            seen.append(self)

        async def upload_video_flow(self, **kwargs):
            return True

    monkeypatch.setattr(nodes, "_get_uploader_cls", lambda platform: FakeUploader)
    monkeypatch.delenv("SPREADO_KEEP_BROWSER_OPEN_SECONDS", raising=False)
    for seconds in (0, 7):
        asyncio.run(nodes._publish_async(
            "douyin", None, str(video), "标题", "文案", "", None, "", "", "yes", seconds, "yes",
        ))
    assert [item._keep_open_seconds for item in seen] == [0, 7]
    assert "SPREADO_KEEP_BROWSER_OPEN_SECONDS" not in __import__("os").environ


def test_node_failure_exposes_actionable_reason(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"test")

    class FakeUploader:
        run_id = "test-run"
        debug_dir = tmp_path
        last_error = "复用的浏览器没有登录态，请先登录"

        def __init__(self, **kwargs):
            pass

        async def upload_video_flow(self, **kwargs):
            return False

    monkeypatch.setattr(nodes, "_get_uploader_cls", lambda platform: FakeUploader)
    with pytest.raises(RuntimeError, match="请先登录"):
        asyncio.run(nodes._publish_async(
            "douyin", None, str(video), "标题", "文案", "", None, "", "", "no", 0, "no",
        ))


def test_empty_metadata_fails_before_upload(tmp_path, monkeypatch):
    def unexpected(platform):
        pytest.fail("Empty metadata must fail before creating an uploader")
    monkeypatch.setattr(nodes, "_get_uploader_cls", unexpected)
    with pytest.raises(ValueError, match="标题和文案均为空"):
        asyncio.run(nodes._publish_async("douyin", None, str(tmp_path / "video.mp4"), "", "", "", None, "", "", "yes", 0, "yes"))


def test_node_writes_receipt_into_comfy_history(monkeypatch):
    def fake_run(coro):
        coro.close()
        return {"status": "preview_ready", "run_id": "run-1", "debug_dir": "/tmp/debug"}

    monkeypatch.setattr(nodes, "_run_async", fake_run)
    result = nodes._publish("douyin", None, "/tmp/video.mp4", "标题", "文案", "", None, "", "", "no", 0, "yes")
    receipt = json.loads(result["ui"]["text"][0])
    assert receipt == {"platform": "douyin", "status": "preview_ready", "run_id": "run-1", "debug_dir": "/tmp/debug"}


def test_unavailable_chrome_attach_never_opens_another_browser(monkeypatch):
    from fx_publish.fx_publish_core.core import uploader as module
    from fx_publish.fx_publish_core.plugins.douyin.uploader import DouYinUploader

    calls = []

    async def fake_create(**kwargs):
        calls.append(kwargs)
        if kwargs["cdp_port"]:
            raise RuntimeError("Chrome 正在运行，但没有开调试端口 9222")
        return object()

    monkeypatch.setattr(module.StealthBrowser, "create", fake_create)
    uploader = DouYinUploader(headless=False)
    uploader._cdp_port = 9222
    uploader._cdp_autostart = True
    with pytest.raises(RuntimeError, match="调试端口"):
        asyncio.run(uploader._new_browser(headless=False))
    assert [call["cdp_port"] for call in calls] == [9222]
    assert calls[0]["cdp_autostart"] is False


def test_missing_cdp_fails_before_starting_playwright(monkeypatch):
    from fx_publish.fx_publish_core.core import browser as module
    monkeypatch.setattr(module, "cdp_endpoint_alive", lambda port: None)
    def unexpected():
        pytest.fail("Missing CDP must not launch Playwright or Chrome")
    monkeypatch.setattr(module, "async_playwright", unexpected)
    with pytest.raises(RuntimeError, match="本次未打开新浏览器"):
        asyncio.run(module.StealthBrowser.create(cdp_port=9222))


@pytest.mark.parametrize("value", ["#猫咪 #萌宠动画 #搞笑短片", "猫咪,萌宠动画,搞笑短片", "猫咪，萌宠动画；搞笑短片\n猫咪"])
def test_tags_accept_chat_and_node_formats(value):
    assert nodes._split_tags(value) == ["猫咪", "萌宠动画", "搞笑短片"]


def test_context_manager_does_not_start_browser_twice():
    from fx_publish.fx_publish_core.core.browser import StealthBrowser
    browser = StealthBrowser()
    marker = object()
    browser.browser = marker
    assert asyncio.run(browser.__aenter__()) is browser
    assert browser.browser is marker


def test_attached_cleanup_only_closes_owned_tabs():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fx_publish.fx_publish_core.core.browser import StealthBrowser
    instance = StealthBrowser(keep_open_seconds=0)
    instance.attached = True
    instance.browser = SimpleNamespace(close=AsyncMock())
    instance.context = SimpleNamespace(close=AsyncMock())
    instance.playwright = SimpleNamespace(stop=AsyncMock())
    user_browser, user_context, driver = instance.browser, instance.context, instance.playwright
    page = SimpleNamespace(close=AsyncMock())
    instance._own_pages = [page]
    asyncio.run(instance.close())
    page.close.assert_awaited_once()
    driver.stop.assert_awaited_once()
    user_browser.close.assert_not_awaited()
    user_context.close.assert_not_awaited()


@pytest.mark.parametrize("texts,expected", [(["点击发布后，如作品还在上传中，请勿关闭页面"], True), (["横/竖双封面缺失"], False), (["横/竖双封面缺失 封面设置中", "封面设置中", "视频已处理"], True), (["预览转码中", "视频已处理"], True), (["文件解析中，0% 取消上传"], False)])
def test_douyin_preview_waits_for_processing_and_rejects_missing_cover(monkeypatch, texts, expected):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fx_publish.fx_publish_core.plugins.douyin.uploader import DouYinUploader
    body = SimpleNamespace(inner_text=AsyncMock(side_effect=texts))
    page = SimpleNamespace(locator=lambda selector: body)
    uploader = DouYinUploader()
    monkeypatch.setattr(uploader, "_capture_step_snapshot", AsyncMock())
    monkeypatch.setattr(uploader, "_has_video_preview", AsyncMock(return_value=True))
    monkeypatch.setattr(uploader, "_find_main_publish_button", AsyncMock(return_value=SimpleNamespace(is_enabled=AsyncMock(return_value=True))))
    async def poll(check, **kwargs):
        for _ in texts:
            if await check():
                return True
        return False
    monkeypatch.setattr(uploader, "_wait_for_condition", poll)
    assert asyncio.run(uploader._wait_for_publish_ready(page)) is expected


@pytest.mark.parametrize("drop_tag", [False, True])
def test_kuaishou_metadata_retries_replace_and_verify_all_fields(monkeypatch, drop_tag):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fx_publish.fx_publish_core.plugins.kuaishou.uploader import KuaiShouUploader
    text = {"value": "旧文案"}
    async def press(key):
        if key == "Backspace":
            text["value"] = ""
        elif key == "Digit3":
            text["value"] += "#"
        elif key == "Enter":
            text["value"] += "\n"
    async def insert(value):
        if not (drop_tag and value == "#猫咪 "):
            text["value"] += value
    async def inner_text(**kwargs):
        return text["value"]
    editor = SimpleNamespace(click=AsyncMock(), inner_text=inner_text)
    page = SimpleNamespace(
        locator=lambda selector: editor, wait_for_timeout=AsyncMock(),
        keyboard=SimpleNamespace(press=press, insert_text=insert, down=AsyncMock(), up=AsyncMock()),
    )
    uploader = KuaiShouUploader()
    monkeypatch.setattr(uploader, "_dismiss_overlays", AsyncMock())
    async def run():
        assert await uploader._fill_video_info(page, "标题", "正文", ["猫咪"]) is (not drop_tag)
        assert await uploader._fill_video_info(page, "标题", "正文", ["猫咪"]) is (not drop_tag)
    asyncio.run(run())
    assert text["value"].count("标题") == 1
    assert "旧文案" not in text["value"]


def test_failed_browser_start_cleans_up_playwright(monkeypatch):
    from fx_publish.fx_publish_core.core.browser import StealthBrowser

    closed = []

    async def fail_start(self):
        raise RuntimeError("调试端口不可用")

    async def fake_exit(self, exc_type, exc, tb):
        closed.append(True)

    monkeypatch.setattr(StealthBrowser, "__aenter__", fail_start)
    monkeypatch.setattr(StealthBrowser, "__aexit__", fake_exit)
    with pytest.raises(RuntimeError, match="调试端口不可用"):
        asyncio.run(StealthBrowser.create(cdp_port=9222))
    assert closed == [True]

def test_publish_worker_reuses_event_loop():
    async def identify():
        return asyncio.get_running_loop()
    assert nodes._run_async(identify()) is nodes._run_async(identify())


def test_cdp_pool_reuses_authorization_and_only_closes_owned_tabs(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fx_publish.fx_publish_core.core import browser as module
    monkeypatch.setattr(module, '_CDP_CONNECTIONS', {})
    monkeypatch.setattr(module, '_CDP_FAILURES', {})
    context = SimpleNamespace(close=AsyncMock())
    browser = SimpleNamespace(contexts=[context], is_connected=lambda: True, close=AsyncMock())
    driver = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser)), stop=AsyncMock())
    monkeypatch.setattr(module, 'async_playwright', lambda: SimpleNamespace(start=AsyncMock(return_value=driver)))
    monkeypatch.setattr(module, 'cdp_endpoint_alive', lambda _: 'ws://127.0.0.1:9222/devtools/browser/test')
    async def run():
        asyncio.get_running_loop()._fx_publish_keep_cdp = True
        first = await module.StealthBrowser.create(cdp_port=9222, keep_open_seconds=0)
        page = SimpleNamespace(close=AsyncMock())
        first._own_pages.append(page)
        await first.close()
        second = await module.StealthBrowser.create(cdp_port=9222, keep_open_seconds=0)
        assert second.browser is browser
        await second.close()
        page.close.assert_awaited_once()
    asyncio.run(run())
    driver.chromium.connect_over_cdp.assert_awaited_once()
    driver.stop.assert_not_awaited()
    browser.close.assert_not_awaited()
    context.close.assert_not_awaited()


def test_douyin_empty_upload_cannot_report_ready(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fx_publish.fx_publish_core.plugins.douyin.uploader import DouYinUploader
    uploader = DouYinUploader()
    monkeypatch.setattr(uploader, "_capture_step_snapshot", AsyncMock())
    monkeypatch.setattr(uploader, '_has_video_preview', AsyncMock(return_value=False))
    body = SimpleNamespace(inner_text=AsyncMock(return_value='点击上传 选择封面 发布'))
    page = SimpleNamespace(locator=lambda _: body, get_by_text=lambda *a, **k: SimpleNamespace(count=AsyncMock(return_value=0)))
    async def poll(check, **kwargs):
        return await check()
    monkeypatch.setattr(uploader, '_wait_for_condition', poll)
    assert not asyncio.run(uploader._wait_for_publish_ready(page))

def test_douyin_navigation_interrupts_wait_without_reporting_success(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fx_publish.fx_publish_core.plugins.douyin.uploader import DouYinUploader
    uploader = DouYinUploader()
    page = SimpleNamespace(url='https://creator.douyin.com/creator-micro/content/manage')
    async def poll(check, **kwargs):
        return await check()
    monkeypatch.setattr(uploader, '_wait_for_condition', poll)
    assert not asyncio.run(uploader._wait_for_publish_ready(page))
    assert '编辑页已离开' in uploader.last_error


def test_douyin_switches_from_cover_to_video_before_accepting_preview(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fx_publish.fx_publish_core.plugins.douyin.uploader import DouYinUploader
    uploader = DouYinUploader()
    monkeypatch.setattr(uploader, '_capture_step_snapshot', AsyncMock())
    monkeypatch.setattr(uploader, '_has_video_preview', AsyncMock(side_effect=[False, True]))
    monkeypatch.setattr(uploader, '_find_main_publish_button', AsyncMock(return_value=SimpleNamespace(is_enabled=AsyncMock(return_value=True))))
    tab = SimpleNamespace(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True), click=AsyncMock())
    tab.first = tab
    body = SimpleNamespace(inner_text=AsyncMock(return_value='预览视频 预览封面/标题'))
    page = SimpleNamespace(url='https://creator.douyin.com/creator-micro/content/post/video', locator=lambda _: body, get_by_text=lambda *a, **k: tab)
    async def poll(check, **kwargs):
        assert not await check()
        return await check()
    monkeypatch.setattr(uploader, '_wait_for_condition', poll)
    assert asyncio.run(uploader._wait_for_publish_ready(page))
    tab.click.assert_awaited_once()

def test_managed_mode_is_default_and_keeps_legacy_reuse():
    assert nodes._BasePublishNode.INPUT_TYPES()['required']['browser_mode'][1]['default'] == '专用发布浏览器'
    assert nodes._browser_attrs('专用发布浏览器', 9222)['_managed_browser'] is True
    assert nodes._browser_attrs('复用我的Chrome', 9222)['_cdp_port'] == 9222


def test_managed_browser_reconnects_without_launch(monkeypatch, tmp_path):
    from fx_publish.fx_publish_core.core import managed_browser as module
    monkeypatch.setattr(module, 'profile_directory', lambda: tmp_path)
    monkeypatch.setattr(module, '_profile_process_running', lambda _: True)
    monkeypatch.setattr(module, '_profile_endpoint', lambda _: 34567)
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **k: pytest.fail('must reuse running profile'))
    assert module.ensure_publish_browser('chrome.exe') == 34567


def test_managed_browser_launch_uses_fixed_profile_and_loopback(monkeypatch, tmp_path):
    from fx_publish.fx_publish_core.core import managed_browser as module
    calls = []
    monkeypatch.setattr(module, 'profile_directory', lambda: tmp_path)
    monkeypatch.setattr(module, '_profile_process_running', lambda _: False)
    monkeypatch.setattr(module, '_profile_endpoint', lambda _: 34567)
    monkeypatch.setattr(module.subprocess, 'Popen', lambda args, **kwargs: calls.append(args))
    assert module.ensure_publish_browser('chrome.exe') == 34567
    assert len(calls) == 1
    assert f'--user-data-dir={tmp_path}' in calls[0]
    assert '--remote-debugging-address=127.0.0.1' in calls[0]
    assert '--remote-debugging-port=0' in calls[0]
    assert 'about:blank' not in calls[0]


def test_managed_browser_reuses_platform_tab_and_keeps_login_page():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fx_publish.fx_publish_core.core.browser import StealthBrowser
    page = SimpleNamespace(url='https://cp.kuaishou.com/', is_closed=lambda: False, close=AsyncMock(), evaluate=AsyncMock(return_value=1))
    browser = StealthBrowser(keep_open_seconds=0)
    browser.managed = True
    browser.attached = True
    browser.context = SimpleNamespace(pages=[page], new_page=AsyncMock())
    async def run():
        assert await browser.new_page('https://cp.kuaishou.com/') is page
        await browser.close()
    asyncio.run(run())
    page.close.assert_not_awaited()

def test_managed_browser_replaces_crashed_tab():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fx_publish.fx_publish_core.core.browser import StealthBrowser
    crashed = SimpleNamespace(url='https://channels.weixin.qq.com/', is_closed=lambda: False, close=AsyncMock(), evaluate=AsyncMock(side_effect=RuntimeError('Page crashed')))
    replacement = object()
    browser = StealthBrowser(keep_open_seconds=0)
    browser.managed = True
    browser.context = SimpleNamespace(pages=[crashed], new_page=AsyncMock(return_value=replacement))
    assert asyncio.run(browser.new_page('https://channels.weixin.qq.com/')) is replacement
    crashed.close.assert_awaited_once()
