# -*- coding: utf-8 -*-
"""独立测试入口：不经 ComfyUI 直接跑一次采集。

用法: python runner.py payload.json

payload.json 示例（关键词搜索小红书）：
{
  "platform": "xhs",
  "settings": {
    "CRAWLER_TYPE": "search",
    "KEYWORDS": "编程副业",
    "CRAWLER_MAX_NOTES_COUNT": 5,
    "ENABLE_GET_COMMENTS": true,
    "ENABLE_GET_SUB_COMMENTS": false,
    "LOGIN_TYPE": "qrcode",
    "ENABLE_CDP_MODE": true,
    "CDP_HEADLESS": false
  }
}

cookies/浏览器登录态在采集时完成（qrcode 扫码或 CDP 连已登录 Chrome）。
输出写到 save_path 并读回打印摘要。
"""

import json
import sys
import traceback
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_PLUGIN_DIR))

from crawler_runner import run_crawl_sync  # noqa: E402


def main(payload_path: str) -> int:
    payload = json.loads(Path(payload_path).read_text(encoding="utf-8"))
    platform = payload["platform"]
    settings = payload.get("settings", {})
    save_path = payload.get("save_path") or str(_PLUGIN_DIR / "_run_output")

    result = {"ok": False, "platform": platform}
    try:
        data = run_crawl_sync(platform, settings, save_path)
        result.update(
            ok=True,
            contents=len(data.get("contents", [])),
            comments=len(data.get("comments", [])),
            creators=len(data.get("creators", [])),
            save_path=save_path,
        )
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)[:500]
        result["traceback"] = traceback.format_exc()[-2000:]

    print("FX_CRAWLER_RESULT " + json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
