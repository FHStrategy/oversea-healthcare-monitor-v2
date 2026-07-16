"""
筛选层。五道闸门，每道都记录 reason，全部落 audit 日志。

    0. 域名黑名单 / 静态页标题
    1. excluded_event_types（带例外）
    2. healthcare_core_keywords 硬门槛
    3. 事件类型分类（priority 最小者胜；政策法规需政府实体背书）
    4. 模型精筛（长尾判断）

与旧代码最大的区别：
  旧版 is_healthcare_industry_news 只在 generate_html() 里跑 —— 也就是说
  news.json 和飞书表存的是【未经筛选】的原始数据，只有网页是干净的。
  新版筛选在写任何 sink 之前完成，飞书和网页看到的是同一批数据。

审计：被丢的每一条都带 reason 落 data/audit/*.jsonl（私有留痕），
      不进飞书主表、不进网页。高层看到的与网页完全一致。
"""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from llm import LLMError, chat_json


# ---------------------------------------------------------------------------
# 关键词匹配
# ---------------------------------------------------------------------------
# 词边界 + 可选复数。没有 s? 的话 \bapollo hospital\b 匹配不上 "Apollo Hospitals"
# —— 实测这个 bug 让 Apollo / Pantai / Fortis 连续两轮探针挂零。
# 含标点的词条（"- guide" / "2026 guide"）退回子串匹配，词边界对它们没意义。
_WORDY = re.compile(r"^\w.*\w$")
_cache: Dict[str, Any] = {}


def _matcher(term: str):
    if term in _cache:
        return _cache[term]
    t = term.lower().strip()
    if _WORDY.match(t):
        pat = re.compile(rf"\b{re.escape(t)}s?\b", re.I)
        fn = lambda text: bool(pat.search(text))
    else:
        fn = lambda text: t in text.lower()
    _cache[term] = fn
    return fn


def hit(terms: List[str], text: str) -> Optional[str]:
    """返回第一个命中的词条，没命中返回 None。"""
    for t in terms or []:
        if _matcher(t)(text):
            return t
    return None


def domain_of(url: str) -> str:
    try:
        d = urlparse(url).netloc.lower()
        return d[4:] if d.startswith("www.") else d
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 规则层
# ---------------------------------------------------------------------------

def rule_filter(item: Dict[str, Any], F: Dict[str, Any]) -> Tuple[bool, str]:
    """返回 (是否通过, 原因)。"""
    title = item.get("title", "") or ""
    snippet = item.get("snippet", "") or ""
    text = f"{title} {snippet}"
    dom = domain_of(item.get("url", ""))

    # --- 0a. 域名黑名单 ---
    for d in F.get("blocked_domains", []):
        if dom == d or dom.endswith("." + d):
            return False, f"域名黑名单[{d}]"

    # --- 0b. 静态页 / 导航页 ---
    v = hit(F.get("invalid_titles", []), title)
    if v:
        return False, f"静态页[{v}]"

    # --- 1. 事件类型排除（带例外）---
    # 旧 is_excluded_event_type 有个不对称：只在 title 里找 keywords，
    # 却在 title+summary 里找例外。新版统一在 title+snippet 里找。
    for cat, rule in (F.get("excluded_event_types") or {}).items():
        k = hit(rule.get("keywords", []), text)
        if not k:
            continue
        e = hit(rule.get("exceptions") or [], text)
        if e:
            continue          # 命中例外 -> 放行（如「医院被炸」保留）
        return False, f"{cat}[{k}]"

    # --- 2. 医疗核心词硬门槛 ---
    c = hit(F.get("healthcare_core_keywords", []), text)
    if not c:
        return False, "无医疗核心词"

    item["_core_keyword"] = c
    return True, "规则通过"


def classify_event(item: Dict[str, Any], F: Dict[str, Any]) -> str:
    """事件类型分类。priority 最小者胜。返回值必须与飞书单选选项逐字一致。"""
    text = f"{item.get('title','')} {item.get('snippet','')}"
    gov = hit(F.get("government_keywords", []), text)

    best, best_pri, default = None, 10**9, "行业新闻"
    for rule in F.get("event_types", []):
        if rule.get("is_default"):
            default = rule["name"]
            continue
        if rule.get("require_government_entity") and not gov:
            continue          # 政策法规必须有政府实体背书，专治标签滥用
        if hit(rule.get("keywords", []), text) and rule["priority"] < best_pri:
            best, best_pri = rule["name"], rule["priority"]
    return best or default


# ---------------------------------------------------------------------------
# 模型精筛
# ---------------------------------------------------------------------------
# 规则层拦不住的长尾交给它。实证案例：
#   "Pop Meals Celebrates Halal Certification ... at Pantai Hospital Outlet"
#      -> 餐饮品牌在医院门店办活动，Pantai 只是【地点】不是主体
#   "Thalassemia Treatment - Dr. Gaurav Kharya" @apollohospitals.com
#      -> 医生介绍页，不是新闻

_SYS = "你是医疗行业情报分析师。只输出 JSON，不要任何解释、不要 markdown 代码块。"

_TPL = """判断这条搜索结果是否应进入海外医疗集团动态情报库。

标题：{title}
摘要：{snippet}
来源：{domain}
关注机构：{entity}

三问，全部为 true 才收录：
1. is_news —— 是一则真实的新闻报道吗？（广告、SEO列表页、导航页、行情播报、
   医生介绍页、化验项目页、科普长文 -> false）
2. is_subject —— {subject_q}
3. is_industry —— 是医疗行业的经营/战略/监管动态吗？
   （患者个案、纯医学科普、活动通稿 -> false）

另外给出 event_type，【必须】从下面 11 个里原样选一个，不得自创、不得改字：
{types}
优先级从高到低即上面的顺序；一条新闻符合多类时选靠前的。
「政策法规」必须有政府/卫生部/监管机构作为主体才能选。

只输出：
{{"is_news":true/false,"is_subject":true/false,"is_industry":true/false,"event_type":"...","reason":"不超过25字的中文理由"}}"""


def _judge_one(item: Dict[str, Any], cfg: Dict[str, Any],
               type_names: List[str]) -> Dict[str, Any]:
    ent = item.get("matched_entity")
    subject_q = (
        f"「{ent}」是这条新闻的主体吗？（只是提到名字、或仅作为事件发生地点 -> false）"
        if ent else
        "这条新闻的主体是医疗行业的机构或监管方吗？（其他行业蹭医疗词 -> false）"
    )
    prompt = _TPL.format(
        title=item.get("title", ""),
        snippet=(item.get("snippet", "") or "")[:400],
        domain=domain_of(item.get("url", "")),
        entity=ent or "（无 —— 行业动态通道）",
        subject_q=subject_q,
        types="\n".join(f"  {i}. {n}" for i, n in enumerate(type_names, 1)),
    )
    return chat_json(prompt, system=_SYS,
                     model=cfg.get("model", "qwen-plus"),
                     timeout=cfg.get("timeout", 30))


def model_filter(items: List[Dict[str, Any]], F: Dict[str, Any]
                 ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
    """
    返回 (通过的, 被判掉的, 调用失败数)。

    判定失败时【保留】该条并标记 needs_review —— 宁可让人多看一条，
    也不让「模型挂了」表现成「今天新闻少」。这正是旧代码的失败模式。
    """
    cfg = F.get("model_filter", {})
    if not cfg.get("enabled", True) or not items:
        return items, [], 0

    passed, dropped, errors = [], [], 0
    type_names = [r["name"] for r in sorted(
        F.get("event_types", []), key=lambda x: x["priority"])]

    def work(it):
        return it, _judge_one(it, cfg, type_names)

    with ThreadPoolExecutor(max_workers=cfg.get("max_workers", 8)) as ex:
        futs = [ex.submit(work, it) for it in items]
        for f in as_completed(futs):
            try:
                it, v = f.result()
            except LLMError as e:
                errors += 1
                continue
            ok = all([v.get("is_news"), v.get("is_subject"), v.get("is_industry")])
            it["_model_reason"] = v.get("reason", "")
            it["_model_verdict"] = v
            it["_model_event_type"] = (v.get("event_type") or "").strip()
            (passed if ok else dropped).append(it)

    # 失败的条目找回来，标记待复核后放行
    done = {id(x) for x in passed} | {id(x) for x in dropped}
    for it in items:
        if id(it) not in done:
            it["_model_reason"] = "模型判定失败，保留待人工复核"
            it["needs_review"] = True
            passed.append(it)

    return passed, dropped, errors


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------

def run(items: List[Dict[str, Any]], F: Dict[str, Any]
        ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, int]]:
    """
    返回 (入库的, 审计记录, 统计)。
    审计记录 = 每一条被丢的，带 stage + reason，落 data/audit/*.jsonl
    """
    stats = {"in": len(items), "rule_dropped": 0, "model_dropped": 0,
             "model_errors": 0, "out": 0}
    audit: List[Dict[str, Any]] = []

    survived = []
    for it in items:
        ok, reason = rule_filter(it, F)
        if ok:
            survived.append(it)
        else:
            stats["rule_dropped"] += 1
            audit.append({
                "included": False, "stage": "rule", "reason": reason,
                "title": it.get("title"), "url": it.get("url"),
                "entity": it.get("matched_entity"),
                "published_at": it.get("published_at"),
            })

    passed, dropped, errors = model_filter(survived, F)
    stats["model_dropped"] = len(dropped)
    stats["model_errors"] = errors

    for it in dropped:
        v = it.get("_model_verdict", {})
        flags = [k for k in ("is_news", "is_subject", "is_industry") if not v.get(k)]
        audit.append({
            "included": False, "stage": "model",
            "reason": f"{'/'.join(flags)} = false | {it.get('_model_reason','')}",
            "title": it.get("title"), "url": it.get("url"),
            "entity": it.get("matched_entity"),
            "published_at": it.get("published_at"),
        })

    # ---- 事件类型裁决（折中方案）----
    # 模型结果优先，但【必须】在 11 个合法值里，否则回退规则结果。
    # 规则值和模型值都进 audit，方便日后查分歧、反哺规则表。
    # 注意：飞书只收到最终的 event_type 一个字符串，单选字段不受影响。
    valid = {r["name"] for r in F.get("event_types", [])}
    stats["type_disagree"] = 0
    stats["type_invalid"] = 0

    for it in passed:
        rule_t = classify_event(it, F)
        model_t = it.get("_model_event_type", "")

        if model_t in valid:
            final_t, src = model_t, "model"
        else:
            final_t, src = rule_t, "rule"
            if model_t:
                stats["type_invalid"] += 1   # 模型编了个不在清单里的值
        if model_t and model_t != rule_t:
            stats["type_disagree"] += 1

        it["event_type"] = final_t
        it["_event_type_rule"] = rule_t
        it["_event_type_model"] = model_t
        it["_event_type_source"] = src

        # 没匹配上机构的 -> 行业动态通道
        if not it.get("matched_entity"):
            it["board"] = F.get("industry_channel", {}).get("board_name", "行业动态")

        audit.append({
            "included": True, "stage": "passed",
            "reason": it.get("_model_reason", ""),
            "title": it.get("title"), "url": it.get("url"),
            "entity": it.get("matched_entity"),
            "event_type": final_t,
            "event_type_rule": rule_t,      # ← 审计用
            "event_type_model": model_t,    # ← 审计用
            "event_type_source": src,
            "needs_review": it.get("needs_review", False),
            "published_at": it.get("published_at"),
        })

    stats["out"] = len(passed)
    return passed, audit, stats
