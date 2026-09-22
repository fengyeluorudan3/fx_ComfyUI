# -*- coding: utf-8 -*-
"""fx_crawler: ComfyUI 多平台数据采集节点（移植自 MediaCrawler）。"""
import os
import sys

# 让 vendored 的 fx_crawler_core 成为顶层可导入包（名字足够独特，避免与其它插件冲突）
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

# 前端动态桩点脚本目录（web/fx_crawler.js）
WEB_DIRECTORY = "./web"

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

# 采集节点与视频下载节点各自独立注册：一边挂了不影响另一边（便于开发期）
for _mod in ("nodes", "video_nodes"):
    try:
        _m = __import__(f"{__name__}.{_mod}", fromlist=["*"])
        NODE_CLASS_MAPPINGS.update(_m.NODE_CLASS_MAPPINGS)
        NODE_DISPLAY_NAME_MAPPINGS.update(_m.NODE_DISPLAY_NAME_MAPPINGS)
    except Exception as _e:
        import logging
        logging.getLogger("fx_crawler").warning("加载 %s 失败：%s", _mod, _e)
