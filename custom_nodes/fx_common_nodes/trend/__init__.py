# coding=utf-8
"""
Trend 榜单节点 - 获取与分析热榜的核心能力

来源：TrendRadar 项目核心逻辑，独立为 ComfyUI 节点。
- 拉取多平台热榜（NewsNow API）
- 按关键词分组、权重排序、词频统计
"""

from .nodes import FXTrendFetchHotlist, FXTrendAnalyzeHotlist

NODE_CLASS_MAPPINGS = {
    "FXTrendFetchHotlist": FXTrendFetchHotlist,
    "FXTrendAnalyzeHotlist": FXTrendAnalyzeHotlist,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "FXTrendFetchHotlist": "FX 热榜拉取 (Trend)",
    "FXTrendAnalyzeHotlist": "FX 热榜分析 (Trend)",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
