import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from pathlib import Path

import pytest
from fx_publish.fx_publish_core.plugins.douyin.uploader import DouYinUploader


@pytest.mark.parametrize('actual,expected', [
    ('第一行\n\n第二行 #猫咪', True),
    ('第一行 第二行 #猫咪', True),
    ('第一行 第二行', False),
    ('第一行 被改写 #猫咪', False),
    ('第一行 第二行 #猫咪 第一行 第二行', False),
])
def test_multiline_copy_readback(actual, expected):
    uploader = DouYinUploader()
    for name in ('_dismiss_guides', '_capture_step_snapshot'):
        setattr(uploader, name, AsyncMock())
    for name in ('_wait_for_metadata_area', '_fill_title', '_fill_description', '_verify_title_filled'):
        setattr(uploader, name, AsyncMock(return_value=True))
    uploader._read_description = AsyncMock(return_value=actual)
    page = SimpleNamespace(wait_for_timeout=AsyncMock(), keyboard=SimpleNamespace(press=AsyncMock()), get_by_text=lambda *a, **k: SimpleNamespace(click=AsyncMock()))
    result = asyncio.run(uploader._fill_video_info(page, '标题', '第一行\n第二行', ['猫咪']))
    assert result is expected
    uploader._fill_description.assert_awaited_once_with(page, '第一行\n第二行 #猫咪')


def test_slate_editor_line_placeholders():
    from playwright.async_api import async_playwright
    chrome = Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
    if not chrome.exists():
        pytest.skip('Local Chrome is unavailable')

    async def run():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(executable_path=str(chrome), headless=True)
            try:
                page = await browser.new_page()
                await page.set_content('<div class="zone-container" contenteditable="true">'
                    '<div data-line-wrapper><span data-string>第一行</span><span data-string data-enter>\u200b</span></div>'
                    '<div data-line-wrapper><span data-string>第二行 #猫咪</span><span data-string data-enter>\u200b</span></div></div>')
                assert await DouYinUploader()._read_description(page) == '第一行\n第二行 #猫咪'
            finally:
                await browser.close()
    asyncio.run(run())
