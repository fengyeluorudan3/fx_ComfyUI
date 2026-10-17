import asyncio
import ast
import json
from pathlib import Path

import pytest

from fx_publish import nodes


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


def test_node_writes_receipt_into_comfy_history(monkeypatch):
    def fake_run(coro):
        coro.close()
        return {"status": "preview_ready", "run_id": "run-1", "debug_dir": "/tmp/debug"}

    monkeypatch.setattr(nodes, "_run_async", fake_run)
    result = nodes._publish("douyin", None, "/tmp/video.mp4", "标题", "文案", "", None, "", "", "no", 0, "yes")
    receipt = json.loads(result["ui"]["text"][0])
    assert receipt == {"platform": "douyin", "status": "preview_ready", "run_id": "run-1", "debug_dir": "/tmp/debug"}


def test_unavailable_chrome_attach_opens_login_browser(monkeypatch):
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
    asyncio.run(uploader._new_browser(headless=False))
    assert [call["cdp_port"] for call in calls] == [9222, None]


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
