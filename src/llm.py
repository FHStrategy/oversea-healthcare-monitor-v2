"""
阿里云百炼（DashScope）客户端。

为什么不是本地 vLLM：
  2026-07-15 在 GitHub Actions 里实测，47.117.187.203 不可达
  （runner 在微软云，到阿里云那个 IP 不通）。这是实测结论，不是推断。

与旧代码的区别 —— 旧 summarize_text 的结构是：
    try:  ...调模型...
    except Exception: pass          # 静默吞掉
    return text[:max_length] + "..."  # 回退成截断英文
这导致模型 100% 失败时页面照常生成、照常报绿灯，没人知道。
新版：失败就抛，让调用方决定，绝不伪装成成功。
"""

import json
import os
import re
import time
from typing import Any, Dict, List, Optional

import requests

API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

# 全局 token 计数器。跑完打印出来，第一天就知道每天花多少钱。
import threading
_lock = threading.Lock()
USAGE = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}


def reset_usage():
    with _lock:
        USAGE.update({"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})


class LLMError(Exception):
    pass


def _key() -> str:
    k = os.environ.get("DASHSCOPE_API_KEY")
    if not k:
        raise LLMError("环境变量 DASHSCOPE_API_KEY 缺失")
    return k


def chat(
    prompt: str,
    system: Optional[str] = None,
    model: str = "qwen-plus",
    temperature: float = 0.1,
    max_retries: int = 3,
    timeout: int = 60,
) -> str:
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})

    last = None
    for i in range(1, max_retries + 1):
        try:
            r = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {_key()}",
                         "Content-Type": "application/json"},
                json={"model": model, "messages": msgs, "temperature": temperature},
                timeout=timeout,
            )
        except Exception as e:
            last = f"请求异常: {e}"
            time.sleep(2 * i)
            continue

        if r.status_code == 429:
            last = "429 限流"
            time.sleep(5 * i)
            continue
        if r.status_code != 200:
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            if 400 <= r.status_code < 500:
                break
            time.sleep(2 * i)
            continue

        try:
            body = r.json()
            u = body.get("usage") or {}
            with _lock:
                USAGE["calls"] += 1
                USAGE["prompt_tokens"] += u.get("prompt_tokens", 0)
                USAGE["completion_tokens"] += u.get("completion_tokens", 0)
            return body["choices"][0]["message"]["content"]
        except Exception as e:
            last = f"响应结构异常: {e} / {r.text[:200]}"
            break

    raise LLMError(f"重试 {max_retries} 次仍失败: {last}")


def chat_json(prompt: str, system: Optional[str] = None, **kw) -> Dict[str, Any]:
    """要求模型返回 JSON，容忍它套 ```json 代码块。"""
    raw = chat(prompt, system=system, **kw)
    txt = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", txt, re.S)
    if m:
        txt = m.group(1).strip()
    # 兜底：抓第一个 {...}
    if not txt.startswith("{"):
        m = re.search(r"\{.*\}", txt, re.S)
        if m:
            txt = m.group(0)
    try:
        return json.loads(txt)
    except Exception as e:
        raise LLMError(f"模型没返回合法 JSON: {e}\n原文: {raw[:300]}")
