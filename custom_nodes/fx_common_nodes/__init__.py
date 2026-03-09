"""
fx_common_nodes - ComfyUI 节点：Jina 爬虫、Google 搜索、Todo 格式化、Trend 热榜.

- FXJinaReadURL: 参考 jina_reader_tool，网页正文提取
- FXGoogleSearch: 参考 google_search_tool，SerpAPI Google 搜索
- FXTodoFormat: 参考 todo_tool，待办列表格式化
- FXTrendFetchHotlist / FXTrendAnalyzeHotlist: TrendRadar 榜单拉取与分析
"""

from .fx_crawler import FXJinaReadURL
from .fx_search import FXGoogleSearch
from .fx_todo import FXTodoFormat
from .trend import NODE_CLASS_MAPPINGS as TREND_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS as TREND_DISPLAY

NODE_CLASS_MAPPINGS = {
    "FXJinaReadURL": FXJinaReadURL,
    "FXGoogleSearch": FXGoogleSearch,
    "FXTodoFormat": FXTodoFormat,
    **TREND_MAPPINGS,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "FXJinaReadURL": "FX 网页正文提取 (Jina)",
    "FXGoogleSearch": "FX Google 搜索",
    "FXTodoFormat": "FX Todo 列表格式化",
    **TREND_DISPLAY,
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
