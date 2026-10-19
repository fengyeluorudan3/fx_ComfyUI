"""Boundary cases; no network or real platform submission."""
import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fx_publish import nodes


@pytest.mark.parametrize('flag', [None, '', 'true', 'false', 'YES', 'NO', True, False, 0, 1])
def test_invalid_submission_flag_never_creates_uploader(monkeypatch, flag):
    monkeypatch.setattr(nodes, '_get_uploader_cls', lambda _: pytest.fail('must reject before browser'))
    with pytest.raises(ValueError, match='no_publish'):
        asyncio.run(nodes._publish_async('douyin', None, '', '标题', '正文', '', None, '', '', 'yes', 0, flag))


@pytest.mark.parametrize('mode', ['', None, 'Chrome', '自动', 'unknown'])
def test_unknown_browser_mode_cannot_silently_launch(mode):
    with pytest.raises(ValueError, match='浏览器模式无效'):
        nodes._browser_attrs(mode, 9222)


@pytest.mark.parametrize('problem', ['missing_video', 'empty_video', 'missing_cover', 'empty_cover'])
def test_input_errors_fail_before_browser(tmp_path, monkeypatch, problem):
    video = tmp_path / '成片 带空格.mp4'
    cover = tmp_path / '封面.png'
    if problem != 'missing_video':
        video.write_bytes(b'' if problem == 'empty_video' else b'video')
    if problem == 'empty_cover':
        cover.touch()
    monkeypatch.setattr(nodes, '_get_uploader_cls', lambda _: pytest.fail('invalid input must not start browser'))
    with pytest.raises(ValueError):
        asyncio.run(nodes._publish_async('douyin', None, str(video), '标题', '正文', '', None, str(cover) if 'cover' in problem else '', '', 'yes', 0, 'yes'))


@pytest.mark.parametrize('source_kind', ['raw', 'dict', 'file_object'])
def test_file_backed_long_video_reuses_source_without_copy_or_decode(tmp_path, monkeypatch, source_kind):
    source = tmp_path / '长视频 中文路径.mp4'
    source.write_bytes(b'video')
    monkeypatch.setattr(nodes.shutil, 'copyfile', lambda *a: pytest.fail('no disk duplication'))
    temp_paths = []
    if source_kind == 'raw':
        result = nodes._save_raw_video_source(source, temp_paths)
    elif source_kind == 'dict':
        result = nodes._save_video_to_temp({'path': str(source)}, temp_paths)
    else:
        class FileVideo:
            _VideoFromFile__file = str(source)
            def save_to(self, *args):
                pytest.fail('file-backed video must not decode or export')
        result = nodes._save_video_to_temp(FileVideo(), temp_paths)
    assert Path(result) == source.resolve()
    assert temp_paths == []
    assert source.exists()


def test_memory_video_restores_cursor_and_registers_cleanup():
    source = io.BytesIO(b'video-bytes')
    source.seek(3)
    paths = []
    result = nodes._save_raw_video_source(source, paths)
    try:
        assert Path(result).read_bytes() == b'video-bytes'
        assert source.tell() == 3
        assert paths == [Path(result)]
    finally:
        Path(result).unlink()


@pytest.mark.parametrize('ok', [True, False])
def test_temp_video_cleanup_on_success_and_failure(tmp_path, monkeypatch, ok):
    temp = tmp_path / 'temp.mp4'
    temp.write_bytes(b'video')
    def resolve(path, obj, paths, **kwargs):
        if kwargs.get('is_image'):
            return ''
        paths.append(temp)
        return str(temp)
    class Uploader:
        run_id = 'test'
        debug_dir = tmp_path
        last_error = '模拟失败'
        def __init__(self, **kwargs): pass
        async def upload_video_flow(self, **kwargs): return ok
    monkeypatch.setattr(nodes, '_choose_path', resolve)
    monkeypatch.setattr(nodes, '_get_uploader_cls', lambda _: Uploader)
    async def run():
        return await nodes._publish_async('douyin', object(), '', '标题', '正文', '', None, '', '', 'yes', 0, 'yes')
    if ok:
        assert asyncio.run(run())['status'] == 'preview_ready'
    else:
        with pytest.raises(RuntimeError, match='模拟失败'):
            asyncio.run(run())
    assert not temp.exists()


def test_closed_browser_connection_is_replaced(monkeypatch):
    from fx_publish.fx_publish_core.core import browser as module
    old_driver = SimpleNamespace(stop=AsyncMock())
    new_context = object()
    new_browser = SimpleNamespace(contexts=[new_context])
    new_driver = SimpleNamespace(stop=AsyncMock(), chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=new_browser)))
    monkeypatch.setattr(module, '_CDP_CONNECTIONS', {})
    monkeypatch.setattr(module, '_CDP_FAILURES', {})
    monkeypatch.setattr(module, 'cdp_endpoint_alive', lambda _: 'http://127.0.0.1:12345')
    monkeypatch.setattr(module, 'async_playwright', lambda: SimpleNamespace(start=AsyncMock(return_value=new_driver)))
    async def run():
        loop = asyncio.get_running_loop()
        loop._fx_publish_keep_cdp = True
        module._CDP_CONNECTIONS[(loop,12345)] = (old_driver, SimpleNamespace(is_connected=lambda:False), object())
        instance = await module.StealthBrowser.create(cdp_port=12345, keep_open_seconds=0)
        assert instance.context is new_context
        await instance.close()
    asyncio.run(run())
    old_driver.stop.assert_awaited_once()
    new_driver.chromium.connect_over_cdp.assert_awaited_once()

@pytest.mark.parametrize('error', ['Page crashed', 'Timeout 60000ms exceeded', 'net::ERR_NAME_NOT_RESOLVED'])
def test_connection_error_is_not_reported_as_logout(error):
    from fx_publish.fx_publish_core.plugins.douyin.uploader import DouYinUploader
    page = SimpleNamespace(goto=AsyncMock(side_effect=RuntimeError(error)))
    with pytest.raises(RuntimeError, match='不能判定为未登录'):
        asyncio.run(DouYinUploader()._verify_cookie_on_page(page))


@pytest.mark.parametrize('content', ['', 'not-a-port\n/devtools/browser/a', '80\n/devtools/browser/a', '12345\nwrong-path'])
def test_managed_stale_or_invalid_discovery_is_rejected(tmp_path, content):
    from fx_publish.fx_publish_core.core import managed_browser as module
    (tmp_path / 'DevToolsActivePort').write_text(content)
    assert module._profile_endpoint(tmp_path) is None


def test_managed_profile_is_stable_and_separate_from_daily_chrome(tmp_path, monkeypatch):
    from fx_publish.fx_publish_core.core import managed_browser as module
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    expected = tmp_path / 'FXAgent/publish-browser/User Data'
    assert module.profile_directory() == expected
    assert module.profile_directory() == expected
    assert 'Google' not in expected.parts


def test_managed_running_but_unreachable_does_not_spawn_duplicate(tmp_path, monkeypatch):
    from fx_publish.fx_publish_core.core import managed_browser as module
    monkeypatch.setattr(module, 'profile_directory', lambda: tmp_path)
    monkeypatch.setattr(module, '_profile_process_running', lambda _: True)
    monkeypatch.setattr(module, '_profile_endpoint', lambda _: None)
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **k: pytest.fail('do not spawn duplicate profile'))
    with pytest.raises(RuntimeError, match='已保留账号目录'):
        module.ensure_publish_browser('chrome.exe', timeout=0)
def test_publication_receipts_are_not_reused_from_comfy_cache():
    import math
    from fx_publish import nodes
    for cls in (nodes.FXPublishDouyin, nodes.FXPublishKuaishou, nodes.FXPublishShipinhao,
                nodes.FXPublishXiaohongshu, nodes.FXPublishBilibili):
        assert math.isnan(cls.IS_CHANGED(no_publish='yes'))
        assert math.isnan(cls.IS_CHANGED(no_publish='no'))

