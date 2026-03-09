# coding=utf-8
"""
榜单数据获取

从 NewsNow API 抓取各平台热榜数据，支持多平台、重试与代理。
来源：TrendRadar 核心能力。
"""

import json
import random
import time
from typing import Dict, List, Tuple, Optional, Union

try:
    import requests
except ImportError:
    requests = None


class DataFetcher:
    """热榜数据获取器"""

    DEFAULT_API_URL = "https://newsnow.busiyi.world/api/s"
    DEFAULT_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
    }

    def __init__(
        self,
        proxy_url: Optional[str] = None,
        api_url: Optional[str] = None,
    ):
        self.proxy_url = proxy_url
        self.api_url = api_url or self.DEFAULT_API_URL

    def fetch_data(
        self,
        id_info: Union[str, Tuple[str, str]],
        max_retries: int = 2,
        min_retry_wait: int = 3,
        max_retry_wait: int = 5,
    ) -> Tuple[Optional[str], str, str]:
        """
        获取指定平台热榜数据，支持重试。

        Args:
            id_info: 平台 ID 或 (平台ID, 别名) 元组
            max_retries: 最大重试次数
            min_retry_wait: 最小重试等待（秒）
            max_retry_wait: 最大重试等待（秒）

        Returns:
            (响应文本, 平台ID, 别名)，失败时响应文本为 None
        """
        if requests is None:
            raise RuntimeError("需要安装 requests: pip install requests")

        if isinstance(id_info, tuple):
            id_value, alias = id_info
        else:
            id_value = id_info
            alias = id_value

        url = f"{self.api_url}?id={id_value}&latest"
        proxies = None
        if self.proxy_url:
            proxies = {"http": self.proxy_url, "https": self.proxy_url}

        retries = 0
        while retries <= max_retries:
            try:
                response = requests.get(
                    url,
                    proxies=proxies,
                    headers=self.DEFAULT_HEADERS,
                    timeout=10,
                )
                response.raise_for_status()
                data_text = response.text
                data_json = json.loads(data_text)
                status = data_json.get("status", "未知")
                if status not in ("success", "cache"):
                    raise ValueError(f"响应状态异常: {status}")
                return data_text, id_value, alias
            except Exception as e:
                retries += 1
                if retries <= max_retries:
                    wait_time = random.uniform(min_retry_wait, max_retry_wait) + (retries - 1) * random.uniform(1, 2)
                    time.sleep(wait_time)
                else:
                    return None, id_value, alias
        return None, id_value, alias

    def crawl_websites(
        self,
        ids_list: List[Union[str, Tuple[str, str]]],
        request_interval: int = 100,
    ) -> Tuple[Dict, Dict, List]:
        """
        爬取多个平台热榜。

        Args:
            ids_list: 平台 ID 列表，元素可为字符串或 (平台ID, 别名)
            request_interval: 请求间隔（毫秒）

        Returns:
            (results, id_to_name, failed_ids)
            results: {source_id: {title: {ranks: [], url: "", mobileUrl: ""}}}
        """
        results = {}
        id_to_name = {}
        failed_ids = []

        for i, id_info in enumerate(ids_list):
            if isinstance(id_info, tuple):
                id_value, name = id_info
            else:
                id_value = id_info
                name = id_value

            id_to_name[id_value] = name
            response, _, _ = self.fetch_data(id_info)

            if response:
                try:
                    data = json.loads(response)
                    results[id_value] = {}
                    for index, item in enumerate(data.get("items", []), 1):
                        title = item.get("title")
                        if title is None or isinstance(title, float) or not str(title).strip():
                            continue
                        title = str(title).strip()
                        url = item.get("url", "")
                        mobile_url = item.get("mobileUrl", "")

                        if title in results[id_value]:
                            results[id_value][title]["ranks"].append(index)
                        else:
                            results[id_value][title] = {
                                "ranks": [index],
                                "url": url,
                                "mobileUrl": mobile_url,
                            }
                except (json.JSONDecodeError, Exception):
                    failed_ids.append(id_value)
            else:
                failed_ids.append(id_value)

            if i < len(ids_list) - 1:
                actual = max(50, request_interval + random.randint(-10, 20))
                time.sleep(actual / 1000)

        return results, id_to_name, failed_ids
