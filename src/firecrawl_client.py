"""
Firecrawl v2 搜索客户端。

与旧代码的区别：
  旧: subprocess 调 `firecrawl` CLI，写临时文件再读回来，except 吞掉一切
  新: 直接调 HTTP API，错误码可读，失败抛异常

关键参数（2026-07-15 在 GitHub Actions 里实测确认）：
  sources: ["news"]  必须带。不带的话返回 web 结果，且【没有 date 字段】，
                     7 天窗口会直接失去依据。
  tbs: "qdr:d,sbd:1" 实测对 news 源【生效】：
                     带 tbs → 5 条全在 24h 内
                     不带   → 10 条里混着 1 个月前、Apr 22、Jan 28
                     （Firecrawl 文档说 tbs 只对 web 生效，实测推翻了文档）

响应结构：
  {"success": true, "data": {"news": [ {title, url, snippet, date, imageUrl, position} ]}}
"""

import os
import time
from typing import Any, Dict, List

import requests

API_URL = "https://api.firecrawl.dev/v2/search"


class FirecrawlError(Exception):
    """Firecrawl 调用失败。故意让它往上抛 —— 不静默降级。"""


def _api_key() -> str:
    key = os.environ.get("FIRECRAWL_API_KEY")
    if not key:
        raise FirecrawlError("环境变量 FIRECRAWL_API_KEY 缺失")
    return key


def search_news(
    query: str,
    limit: int = 10,
    tbs: str = "qdr:d,sbd:1",
    max_retries: int = 3,
    timeout: int = 90,
) -> List[Dict[str, Any]]:
    """
    搜一次新闻。

    返回 news 条目列表，每条形如：
      {"title":..., "url":..., "snippet":..., "date":"4 hours ago",
       "imageUrl":..., "position": 1}

    失败会抛 FirecrawlError，不返回空列表 —— 这是刻意的。
    旧代码 `except: return []` 会把「搜索挂了」伪装成「今天没新闻」。
    """
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
    }
    payload = {"query": query, "sources": ["news"], "limit": limit}
    if tbs:
        payload["tbs"] = tbs

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.post(API_URL, headers=headers, json=payload, timeout=timeout)
        except Exception as e:
            last_err = f"请求异常: {e}"
            time.sleep(2 * attempt)
            continue

        # 429 = 限流，退避重试
        if r.status_code == 429:
            wait = 5 * attempt
            last_err = f"429 限流，等待 {wait}s"
            time.sleep(wait)
            continue

        if r.status_code != 200:
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            # 4xx 基本是请求本身有问题，重试没意义
            if 400 <= r.status_code < 500:
                break
            time.sleep(2 * attempt)
            continue

        try:
            body = r.json()
        except Exception as e:
            last_err = f"响应不是合法 JSON: {e}"
            break

        data = body.get("data", {})
        # 防御：带 sources 时是 dict，理论上不会是数组，但万一 API 变了要能看出来
        if isinstance(data, list):
            raise FirecrawlError(
                f"data 是数组而非 dict —— sources 参数可能失效了。query={query!r}"
            )
        return data.get("news", []) or []

    raise FirecrawlError(f"query={query!r} 重试 {max_retries} 次仍失败: {last_err}")
