# coding=utf-8
"""
频率词与词组匹配（榜单关键词过滤）

支持：普通词、必须词(+)、过滤词(!)、全局过滤、正则(/pattern/)、显示名(=>)。
来源：TrendRadar 核心能力，独立实现无外部依赖。
"""

import os
import re
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Union


def _parse_word(word: str) -> Dict:
    """解析单个词，识别正则与显示名。"""
    display_name = None
    if "=>" in word:
        parts = re.split(r"\s*=>\s*", word, 1)
        word_config = parts[0].strip()
        if len(parts) > 1 and parts[1].strip():
            display_name = parts[1].strip()
    else:
        word_config = word.strip()

    regex_match = re.match(r"^/(.+)/[a-z]*$", word_config)
    if regex_match:
        pattern_str = regex_match.group(1)
        try:
            pattern = re.compile(pattern_str, re.IGNORECASE)
            return {"word": pattern_str, "is_regex": True, "pattern": pattern, "display_name": display_name}
        except re.error:
            pass

    return {"word": word_config, "is_regex": False, "pattern": None, "display_name": display_name}


def _word_matches(word_config: Union[str, Dict], title_lower: str) -> bool:
    """检查词是否在标题中匹配。"""
    if isinstance(word_config, str):
        return word_config.lower() in title_lower
    if word_config.get("is_regex") and word_config.get("pattern"):
        return bool(word_config["pattern"].search(title_lower))
    return word_config["word"].lower() in title_lower


def load_frequency_words(
    frequency_file: Optional[str] = None,
) -> Tuple[List[Dict], List, List[str]]:
    """
    从文件加载频率词配置。
    文件不存在或为空时返回 ([全部新闻], [], [])，表示不过滤。

    Returns:
        (word_groups, filter_words, global_filters)
    """
    if frequency_file is None:
        frequency_file = os.environ.get("FREQUENCY_WORDS_PATH", "")
    if not frequency_file:
        return [{"required": [], "normal": [], "group_key": "全部新闻"}], [], []

    path = Path(frequency_file)
    if not path.exists():
        return [{"required": [], "normal": [], "group_key": "全部新闻"}], [], []

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    group_blocks = [g.strip() for g in content.split("\n\n") if g.strip()]
    processed_groups = []
    filter_words = []
    global_filters = []
    current_section = "WORD_GROUPS"

    for group in group_blocks:
        lines = [l.strip() for l in group.split("\n") if l.strip() and not l.strip().startswith("#")]
        if not lines:
            continue

        if lines[0].startswith("[") and lines[0].endswith("]"):
            section_name = lines[0][1:-1].upper()
            if section_name in ("GLOBAL_FILTER", "WORD_GROUPS"):
                current_section = section_name
                lines = lines[1:]

        if current_section == "GLOBAL_FILTER":
            for line in lines:
                if line and not line.startswith(("!", "+", "@")):
                    global_filters.append(line)
            continue

        words = lines
        group_alias = None
        if words and words[0].startswith("[") and words[0].endswith("]"):
            potential = words[0][1:-1].strip()
            if potential.upper() not in ("GLOBAL_FILTER", "WORD_GROUPS"):
                group_alias = potential
                words = words[1:]

        group_required = []
        group_normal = []
        group_max_count = 0

        for word in words:
            if word.startswith("@"):
                try:
                    c = int(word[1:])
                    if c > 0:
                        group_max_count = c
                except (ValueError, IndexError):
                    pass
            elif word.startswith("!"):
                filter_words.append(_parse_word(word[1:]))
            elif word.startswith("+"):
                group_required.append(_parse_word(word[1:]))
            else:
                group_normal.append(_parse_word(word))

        if group_required or group_normal:
            group_key = " ".join(w["word"] for w in (group_normal or group_required))
            display_name = group_alias
            if not display_name:
                parts = [w.get("display_name") or w["word"] for w in group_normal + group_required]
                display_name = " / ".join(parts) if parts else None
            processed_groups.append({
                "required": group_required,
                "normal": group_normal,
                "group_key": group_key,
                "display_name": display_name,
                "max_count": group_max_count,
            })

    if not processed_groups:
        return [{"required": [], "normal": [], "group_key": "全部新闻"}], [], global_filters
    return processed_groups, filter_words, global_filters


def parse_keywords_simple(keywords_str: str) -> List[Dict]:
    """
    从简单字符串解析词组（用于节点输入）。
    格式：逗号分隔关键词，如 "AI, 大模型, 开源"
    返回一个「全部匹配任一关键词」的 word_groups。
    """
    if not (keywords_str or "").strip():
        return [{"required": [], "normal": [], "group_key": "全部新闻"}]
    words = [w.strip() for w in keywords_str.split(",") if w.strip()]
    if not words:
        return [{"required": [], "normal": [], "group_key": "全部新闻"}]
    normal = [{"word": w, "is_regex": False, "pattern": None, "display_name": None} for w in words]
    group_key = " ".join(words)
    return [{"required": [], "normal": normal, "group_key": group_key, "display_name": None, "max_count": 0}]


def matches_word_groups(
    title: str,
    word_groups: List[Dict],
    filter_words: List,
    global_filters: Optional[List[str]] = None,
) -> bool:
    """检查标题是否匹配任一词组且未命中过滤词。"""
    if not isinstance(title, str):
        title = str(title) if title is not None else ""
    if not title.strip():
        return False
    title_lower = title.lower()

    if global_filters and any(g.lower() in title_lower for g in global_filters):
        return False
    if not word_groups:
        return True
    for filter_item in filter_words:
        if _word_matches(filter_item, title_lower):
            return False
    for group in word_groups:
        required = group.get("required") or []
        normal = group.get("normal") or []
        if required and not all(_word_matches(r, title_lower) for r in required):
            continue
        if normal and not any(_word_matches(n, title_lower) for n in normal):
            continue
        return True
    return False
