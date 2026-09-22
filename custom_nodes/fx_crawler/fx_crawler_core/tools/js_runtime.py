# -*- coding: utf-8 -*-
"""PyExecJS / Node.js 运行时：ComfyUI GUI 启动时 PATH 常不含 node，需主动查找。"""

from __future__ import annotations

import glob
import json
import logging
import os
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

_node_prepared = False


def find_node_executable() -> Optional[str]:
    env_node = str(os.environ.get("FX_CRAWLER_NODE") or "").strip()
    if env_node and os.path.isfile(env_node):
        return env_node
    node = shutil.which("node")
    if node:
        return node
    patterns = [
        os.path.expanduser("~/.nvm/versions/node/*/bin/node"),
        os.path.expanduser("~/.volta/bin/node"),
        "/opt/homebrew/bin/node",
        "/usr/local/bin/node",
    ]
    candidates = []
    for pat in patterns:
        candidates.extend(glob.glob(pat))
    candidates = [p for p in candidates if os.path.isfile(p)]
    return sorted(candidates)[-1] if candidates else None


def prepare_execjs_node() -> Optional[str]:
    """把 Node 放进 PATH 并强制 PyExecJS 使用 Node 运行时。"""
    global _node_prepared
    if _node_prepared:
        return find_node_executable()
    node = find_node_executable()
    if node:
        node_dir = os.path.dirname(node)
        path = os.environ.get("PATH", "")
        if node_dir not in path.split(os.pathsep):
            os.environ["PATH"] = node_dir + os.pathsep + path
        os.environ["EXECJS_RUNTIME"] = "Node"
        logger.debug("[js_runtime] using Node.js at %s", node)
    else:
        logger.warning("[js_runtime] Node.js not found; execjs may fall back to ES5-only runtime")
    _node_prepared = True
    return node


def run_js_file_call(js_path: str, fn_name: str, *args: str) -> str:
    """用 node -e 直接调用 JS 文件里的函数（不依赖 PyExecJS 运行时选择）。"""
    node = find_node_executable()
    if not node:
        raise RuntimeError(
            "抖音签名需要 Node.js，但当前进程找不到 node。\n"
            "请安装 Node.js，或在启动 ComfyUI 前设置 FX_CRAWLER_NODE=/path/to/node"
        )
    arg_json = ", ".join(json.dumps(a) for a in args)
    script = (
        "const fs=require('fs');"
        f"eval(fs.readFileSync({json.dumps(js_path)},'utf8'));"
        f"const r={fn_name}({arg_json});"
        "if(typeof r==='undefined'){process.exit(2);}"
        "process.stdout.write(String(r));"
    )
    proc = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"Node 执行 JS 失败: {err[:500]}")
    return proc.stdout.strip()
