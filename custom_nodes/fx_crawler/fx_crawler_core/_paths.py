# -*- coding: utf-8 -*-
"""包内资源路径助手：让 libs/docs 等相对路径不依赖进程 cwd。"""
import os

CORE_DIR = os.path.dirname(os.path.abspath(__file__))


def pkg_path(rel: str) -> str:
    """把相对包根(fx_crawler_core/)的路径解析为绝对路径。"""
    return os.path.join(CORE_DIR, rel.lstrip("./"))
