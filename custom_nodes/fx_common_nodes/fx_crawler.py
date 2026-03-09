"""
FX 爬虫节点 - 参考 jina_reader_tool 的请求/错误处理方式.

使用 Jina Reader API 提取网页正文，返回纯文本/ Markdown，供后续节点使用。
"""

import logging
from typing import Tuple

logger = logging.getLogger(__name__)

JINA_READER_BASE = "https://r.jina.ai/"


class FXJinaReadURL:
    """使用 Jina Reader API 提取网页正文，输出为 STRING。

    自动去除广告与噪音，返回 Markdown 格式正文。
    适用于：把网页内容接入工作流、提示词增强、摘要等。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "url": ("STRING", {
                    "multiline": False,
                    "default": "",
                    "placeholder": "https://example.com/article",
                }),
            },
            "optional": {
                "max_length": ("INT", {
                    "default": 5000,
                    "min": 100,
                    "max": 50000,
                    "step": 500,
                    "display": "number",
                }),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)
    FUNCTION = "fetch_url_text"
    CATEGORY = "fx_common/crawler"

    def fetch_url_text(self, url: str, max_length: int = 5000) -> Tuple[str]:
        """同步执行 - 提取网页文本，风格参考 jina_reader_tool._run"""
        try:
            url = (url or "").strip()
            if not url:
                logger.warning("[FXJinaReadURL] url 为空")
                return ("",)

            logger.info(f"[FXJinaReadURL] 开始提取: {url}")

            try:
                import requests
            except ImportError:
                logger.error("[FXJinaReadURL] 需要安装 requests: pip install requests")
                return ("[错误] 需要安装 requests",)

            jina_url = f"{JINA_READER_BASE}{url}"
            headers = {"Accept": "text/markdown", "X-No-Cache": "true"}
            response = requests.get(jina_url, headers=headers, timeout=30)
            response.raise_for_status()

            content = response.text.strip()
            truncated = False
            if len(content) > max_length:
                content = content[:max_length] + "\n\n... (内容已截断)"
                truncated = True

            logger.info(f"[FXJinaReadURL] 成功: {url}, 长度={len(content)}, 截断={truncated}")
            return (content,)

        except Exception as e:
            logger.exception(f"[FXJinaReadURL] 失败: {e}")
            return (f"[错误] 网页提取失败: {e}",)
