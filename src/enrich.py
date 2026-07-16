"""
富化层：英文标题 -> 中文标题 + 中文摘要。

关键设计：【一次调用同时产出两个】。

旧代码是两套独立逻辑，而且都有问题：
  1. translate_text  -> item["snippet_zh"]
     全文搜索 snippet_zh：6 处写、0 处读。generate_html 从来不用它。
     那个每次 13 秒、跑几百次、把 cron 打爆到 1 小时超时的串行翻译循环，
     产物【没有任何消费者】。纯粹在烧时间。
  2. summarize_text  -> 网页上真正显示的中文
     它在 generate_html 里跑，而三个 tab（今天/近3天/近1周）是包含关系，
     各自循环调用 -> 同一条新闻被摘要 2~3 次。
     实证：181 个 news-item 只有 144 个唯一 URL。

  另外：标题从来没被翻译过。这才是「新闻显然没翻译」的真相 ——
  实证 report_2_.html：181 条摘要 100% 中文，181 条标题 0% 中文。

新版：
  - 一次调用出 title_zh + summary_zh，省掉一半 token 和一半往返
  - 只对【最终入库】的条目调用（筛选已在上游完成）
  - 数据算一次，三个 tab 前端过滤同一份 -> 不重复
  - 并发 8 路
  - 失败就是失败，绝不回退成英文
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Tuple

from llm import LLMError, chat_json

_SYS = "你是医疗行业情报翻译。只输出 JSON，不要解释，不要 markdown 代码块。"

_TPL = """把这条医疗行业新闻处理成中文情报条目。

原标题：{title}
原摘要：{snippet}
涉及机构：{entity}

要求：
- title_zh：标题的中文翻译。保留机构名的通用中文译名（如 Mount Elizabeth Hospital -> 伊丽莎白医院）；
  没有通用译名的机构名保留英文原文。不要加书名号，不要意译发挥，不超过 40 字。
- summary_zh：2 句以内的中文摘要，说清「谁、做了什么、有什么影响」。
  只依据原摘要，不要脑补原文没有的信息。不超过 80 字。

只输出：
{{"title_zh":"...","summary_zh":"..."}}"""


def _has_cn(s: str) -> bool:
    return any("\u4e00" <= c <= "\u9fff" for c in (s or ""))


def _one(item: Dict[str, Any], model: str, timeout: int) -> Dict[str, str]:
    v = chat_json(
        _TPL.format(
            title=item.get("title", ""),
            snippet=(item.get("snippet", "") or "")[:600],
            entity=item.get("matched_entity") or "（行业动态，无特定机构）",
        ),
        system=_SYS, model=model, timeout=timeout,
    )
    t, s = (v.get("title_zh") or "").strip(), (v.get("summary_zh") or "").strip()
    if not t or not s:
        raise LLMError(f"字段缺失: title_zh={t!r} summary_zh={s!r}")
    # 校验真的产出了中文 —— 旧 summarize_text 就是靠这道校验，
    # 但它校验失败后 except: pass 回退成英文；这里失败就抛。
    if not _has_cn(t) or not _has_cn(s):
        raise LLMError(f"产出里没有中文: {t!r} / {s!r}")
    return {"title_zh": t, "summary_zh": s}


def run(items: List[Dict[str, Any]], model: str = "qwen-plus",
        max_workers: int = 8, timeout: int = 30
        ) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]]]:
    """
    原地写入 title_zh / summary_zh。
    返回 (成功的条目, 失败列表[(标题, 错误)])。

    失败的条目【不入库】—— 一条标题是英文的记录混进给高层看的表里，
    比少一条更糟。失败会计入统计，失败率过高时上游应该红灯。
    """
    if not items:
        return [], []

    ok, failed = [], []

    def work(it):
        return it, _one(it, model, timeout)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(work, it) for it in items]
        for f in as_completed(futs):
            try:
                it, v = f.result()
            except LLMError as e:
                failed.append(("(未知)", str(e)[:120]))
                continue
            except Exception as e:
                failed.append(("(未知)", f"{type(e).__name__}: {e}"[:120]))
                continue
            it.update(v)
            ok.append(it)

    return ok, failed
