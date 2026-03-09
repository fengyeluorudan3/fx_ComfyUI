"""
FX Google 搜索节点 - 参考 google_search_tool 的 SerpAPI 调用与结果格式化.

使用 SerpAPI 进行 Google 搜索，输出可读的搜索结果文本（精选摘要 + 知识图谱 + 列表）。
需配置环境变量 SERPAPI_API_KEY。
"""

import os
import logging
from typing import Tuple

logger = logging.getLogger(__name__)

SERPAPI_URL = "https://serpapi.com/search.json"


class FXGoogleSearch:
    """使用 SerpAPI 进行 Google 搜索，输出一条 STRING（精选摘要 + 知识图谱 + 搜索结果列表）。

    适用于：在工作流中查询实时信息、新闻、资料等。
    需配置环境变量 SERPAPI_API_KEY。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "query": ("STRING", {
                    "multiline": False,
                    "default": "",
                    "placeholder": "搜索关键词，如: 2026年春节放假安排",
                }),
            },
            "optional": {
                "num": ("INT", {
                    "default": 5,
                    "min": 1,
                    "max": 10,
                    "step": 1,
                    "display": "number",
                }),
                "lang": ("STRING", {
                    "multiline": False,
                    "default": "zh-cn",
                    "placeholder": "zh-cn / en",
                }),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)
    FUNCTION = "search"
    CATEGORY = "fx_common/search"

    def search(self, query: str, num: int = 5, lang: str = "zh-cn") -> Tuple[str]:
        """同步执行 - Google 搜索，逻辑参考 google_search_tool._run"""
        try:
            query = (query or "").strip()
            if not query:
                logger.warning("[FXGoogleSearch] query 为空")
                return ("[错误] 请提供搜索关键词",)

            api_key = os.environ.get("SERPAPI_API_KEY", "")
            if not api_key:
                logger.error("[FXGoogleSearch] SERPAPI_API_KEY 未配置")
                return ("[错误] 请设置环境变量 SERPAPI_API_KEY",)

            num = min(max(1, num), 10)
            lang = (lang or "zh-cn").strip() or "zh-cn"

            logger.info(f"[FXGoogleSearch] 搜索: '{query}', num={num}, lang={lang}")

            try:
                import requests
            except ImportError:
                return ("[错误] 需要安装 requests: pip install requests",)

            search_params = {
                "q": query,
                "api_key": api_key,
                "engine": "google",
                "num": num,
                "hl": lang,
                "gl": "cn" if "zh" in lang else "us",
            }
            response = requests.get(SERPAPI_URL, params=search_params, timeout=15)
            response.raise_for_status()
            data = response.json()

            results = []
            for item in data.get("organic_results", [])[:num]:
                results.append({
                    "title": item.get("title", ""),
                    "snippet": item.get("snippet", ""),
                    "link": item.get("link", ""),
                })

            answer_box = ""
            if "answer_box" in data:
                ab = data["answer_box"]
                answer_box = ab.get("answer", "") or ab.get("snippet", "") or ab.get("result", "")

            knowledge_graph = ""
            if "knowledge_graph" in data:
                kg = data["knowledge_graph"]
                kg_title = kg.get("title", "")
                kg_desc = kg.get("description", "")
                if kg_title or kg_desc:
                    knowledge_graph = f"{kg_title}: {kg_desc}" if kg_desc else kg_title

            content_parts = []
            if answer_box:
                content_parts.append(f"**精选摘要**: {answer_box}\n")
            if knowledge_graph:
                content_parts.append(f"**知识图谱**: {knowledge_graph}\n")
            content_parts.append(f"**搜索结果** (共 {len(results)} 条):\n")
            for i, r in enumerate(results, 1):
                content_parts.append(
                    f"{i}. **{r['title']}**\n"
                    f"   {r['snippet']}\n"
                    f"   链接: {r['link']}\n"
                )

            res_content = "\n".join(content_parts)
            logger.info(f"[FXGoogleSearch] 完成: '{query}', 返回 {len(results)} 条")
            return (res_content,)

        except Exception as e:
            logger.exception(f"[FXGoogleSearch] 失败: {e}")
            return (f"[错误] Google 搜索失败: {e}",)
