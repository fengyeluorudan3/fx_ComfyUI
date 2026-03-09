"""
FX Todo 节点 - 参考 todo_tool 的待办结构与状态格式化.

将多行输入解析为 todo 列表（content + status），输出与 write_todos 一致的格式化字符串。
"""

import logging
from typing import Tuple, List, Dict, Any

logger = logging.getLogger(__name__)

VALID_STATUS = ("pending", "in_progress", "completed")


class FXTodoFormat:
    """将多行文本解析为待办列表并格式化为可读字符串。

    每行格式：`content` 或 `status: content`。
    status 取 pending / in_progress / completed，未写则默认 pending。
    输出格式与 todo_tool 一致：1. [status] content ...
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "tasks": ("STRING", {
                    "multiline": True,
                    "default": "第一步\nin_progress: 第二步\ncompleted: 第三步",
                    "placeholder": "每行: content 或 status: content\nstatus: pending / in_progress / completed",
                }),
            },
            "optional": {
                "default_status": (["pending", "in_progress", "completed"], {
                    "default": "pending",
                }),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)
    FUNCTION = "format_todos"
    CATEGORY = "fx_common/todo"

    def format_todos(self, tasks: str, default_status: str = "pending") -> Tuple[str]:
        """解析多行 tasks，校验 status，输出与 todo_tool 一致的格式化字符串。"""
        try:
            tasks = (tasks or "").strip()
            default_status = default_status or "pending"
            if default_status not in VALID_STATUS:
                default_status = "pending"

            normalized_todos: List[Dict[str, str]] = []
            for line in tasks.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" in line:
                    part = line.split(":", 1)
                    status = part[0].strip().lower()
                    content = part[1].strip()
                    if status not in VALID_STATUS:
                        status = default_status
                else:
                    content = line
                    status = default_status
                if not content:
                    continue
                normalized_todos.append({"content": content, "status": status})

            if not normalized_todos:
                logger.warning("[FXTodoFormat] 无有效任务行")
                return ("[空] 未解析到任何任务",)

            lines = []
            for i, t in enumerate(normalized_todos):
                lines.append(f"{i + 1}. [{t['status']}] {t['content']}")

            res_content = "Todo 列表：\n" + "\n".join(lines)
            logger.info(f"[FXTodoFormat] 解析 {len(normalized_todos)} 条任务")
            return (res_content,)

        except Exception as e:
            logger.exception(f"[FXTodoFormat] 失败: {e}")
            return (f"[错误] 格式化失败: {e}",)
