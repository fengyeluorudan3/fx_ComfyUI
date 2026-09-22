"""独立测试入口：不经 ComfyUI 直接跑发布流程。

用法：
    python runner.py payload.json

payload.json 示例：
{
  "platform": "douyin",
  "video_path": "/abs/path/video.mp4",
  "cover_path": "/abs/path/cover.png",
  "title": "标题",
  "content": "正文",
  "tags": ["tag1", "tag2"],
  "headed": true,
  "keep_open_seconds": 0,
  "no_publish": true
}

注意：cookies/logs 写到当前工作目录（与 ComfyUI 运行时一致，
请在 ComfyUI 根目录下执行本脚本以复用已有 cookie）。
"""

import asyncio
import importlib
import json
import sys
import traceback
from pathlib import Path

# nodes.py 使用包内相对导入，必须以 fx_publish 包的身份加载
_PLUGIN_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_PLUGIN_DIR.parent))
_nodes = importlib.import_module("fx_publish.nodes")


async def main(payload_path):
    payload = json.loads(Path(payload_path).read_text(encoding="utf-8"))
    result = {"ok": False, "platform": payload.get("platform", "")}
    try:
        out = await _nodes._publish_async(
            payload["platform"],
            None,
            payload["video_path"],
            payload.get("title", ""),
            payload.get("content", ""),
            ",".join(payload.get("tags", [])),
            None,
            payload.get("cover_path", ""),
            payload.get("cookies_path", ""),
            "yes" if payload.get("headed", True) else "no",
            payload.get("keep_open_seconds", 0),
            "yes" if payload.get("no_publish", False) else "no",
            uploader_attrs=payload.get("uploader_attrs") or None,
        )
        result.update(
            ok=out.get("status") == "success",
            run_id=out.get("run_id", ""),
            debug_dir=out.get("debug_dir", ""),
        )
    except Exception as exc:
        result["error"] = str(exc)[:500]
        result["traceback"] = traceback.format_exc()[-2000:]
    print("FX_PUBLISH_RESULT " + json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    res = asyncio.run(main(sys.argv[1]))
    sys.exit(0 if res.get("ok") else 1)
