# -*- coding: utf-8 -*-
"""在 ComfyUI 已有 event loop 时安全运行 async 协程。"""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import TypeVar

T = TypeVar("T")


def run_sync(coro) -> T:
    """同步上下文直接 asyncio.run；已在 loop 内则开线程跑。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()
