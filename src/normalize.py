"""
规范化与去重。

四块能力：
  1. normalize_url        —— 剥 tracking 参数，解聚合器去重失效问题
  2. parse_date           —— 相对时间 -> 绝对时间（移植自上一版 estimate_published_time）
  3. match_entity         —— 词边界 + 最长匹配（修掉旧代码「字典顺序决定归属」的隐性 bug）
  4. dedupe               —— URL / 标题相似度 / 优先非聚合器
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

UTC = timezone.utc


# ===========================================================================
# 1. URL 规范化
# ===========================================================================

def normalize_url(url: str, cfg: Dict[str, Any]) -> str:
    """
    实证案例（Firecrawl 真实返回）：
      http://www.msn.com/en-xl/news/other/ihh-singapore-goes-preventive/ar-AA27OINR
        ?apiversion=v2&domshim=1&noservercache=1&noservertelemetry=1
        &batchservertelemetry=1&renderwebcomponents=1&wcseo=1
    -> https://msn.com/en-xl/news/other/ihh-singapore-goes-preventive/ar-AA27OINR

    tracking 参数每次可能不同 -> 同一篇文章会被当成多条 -> URL 去重失效。
    """
    if not url:
        return ""
    try:
        p = urlparse(url.strip())
    except Exception:
        return url.strip()

    scheme = "https" if cfg.get("force_https", True) else (p.scheme or "https")
    netloc = p.netloc.lower()
    if cfg.get("strip_www", True) and netloc.startswith("www."):
        netloc = netloc[4:]

    path = p.path or "/"
    if cfg.get("strip_trailing_slash", True) and len(path) > 1:
        path = path.rstrip("/")

    query = "" if cfg.get("strip_query_params", True) else p.query
    frag = "" if cfg.get("strip_fragment", True) else p.fragment

    return urlunparse((scheme, netloc, path, "", query, frag))


def domain_of(url: str) -> str:
    try:
        d = urlparse(url).netloc.lower()
        return d[4:] if d.startswith("www.") else d
    except Exception:
        return ""


def is_aggregator(url: str, aggregator_domains: List[str]) -> bool:
    d = domain_of(url)
    return any(d == a or d.endswith("." + a) for a in aggregator_domains)


# ===========================================================================
# 2. 日期解析：相对 -> 绝对
# ===========================================================================
# 移植自上一版 parse_relative_date / estimate_published_time。
#
# 为什么必须转绝对时间：Firecrawl 返回的是 "4 hours ago"（抓取那一刻的相对值）。
# 存字符串的话，6 天后读出来还当 4 小时前 —— 时间会永久冻结。
# 存绝对 published_at，展示时再动态重算，才是对的。
#
# 实测见过的格式：
#   "1 hour ago" "4 hours ago" "2 days ago" "1 week ago" "1 month ago"
#   "Apr 22, 2026" "Jan 28, 2026"
#   "Fri, 29 May 2026 11:16:00 GMT"

_REL = re.compile(
    r"(\d+)\s*(second|minute|hour|day|week|month|year)s?\s+ago", re.I
)
_UNIT = {
    "second": timedelta(seconds=1),
    "minute": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=30),
    "year": timedelta(days=365),
}
_ABS_FORMATS = [
    "%b %d, %Y",           # Apr 22, 2026
    "%B %d, %Y",           # April 22, 2026
    "%d %b %Y",            # 22 Apr 2026
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m/%d/%Y",
]


def parse_date(raw: Optional[str], now: Optional[datetime] = None) -> Optional[datetime]:
    """解析成功返回 UTC datetime；无法解析返回 None（不猜、不填 now）。"""
    if not raw or not isinstance(raw, str):
        return None
    now = now or datetime.now(UTC)
    s = raw.strip()
    low = s.lower()

    if low in ("just now", "moments ago", "now"):
        return now
    if low == "yesterday":
        return now - timedelta(days=1)
    if low == "today":
        return now

    m = _REL.search(low)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        return now - _UNIT[unit] * n

    # RFC2822: "Fri, 29 May 2026 11:16:00 GMT"
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    except Exception:
        pass

    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    except Exception:
        pass

    for fmt in _ABS_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except Exception:
            continue

    return None


# ===========================================================================
# 3. 实体匹配：词边界 + 最长匹配
# ===========================================================================

@dataclass
class AliasIndex:
    """
    别名 -> 实体。长度降序，最长匹配优先。

    两档别名：
      aliases       强别名。命中即算。
      weak_aliases  弱别名。【只有同文出现医疗核心词时才算命中】。

    弱别名解决的问题（2026-07-16 实证）：
      "The minority shareholder's day in the sun"
      摘要: "Fortis' takeover by an international healthcare chain..."
      —— 这是 IHH 收购 Fortis 的分析，核心情报。但标题只说 minority shareholder，
      只有正文提到裸 "fortis"。而我们把裸 "fortis" 删了，因为
      Fortis Inc. 是加拿大上市公用事业公司，新闻量不小。
    弱别名两头兼顾：
      "Fortis Inc reports higher electricity rates"     无医疗核心词 -> 不匹配 ✅
      "Fortis' takeover by international healthcare..." 有 healthcare -> 匹配   ✅
    精度由下游模型精筛的 is_subject 兜底
    （"Apollo Tyres 与医院合作体检" 这种会被模型判掉）。
    """
    pairs: List[Tuple[str, str, re.Pattern, bool]] = field(default_factory=list)
    core_pat: Optional[re.Pattern] = None

    @classmethod
    def build(cls, entities: List[Dict[str, Any]],
              core_keywords: Optional[List[str]] = None) -> "AliasIndex":
        pairs = []
        for e in entities:
            for a in e.get("aliases", []):
                pairs.append((a.lower(), e["name"], cls._pat(a), False))
            for a in e.get("weak_aliases", []) or []:
                pairs.append((a.lower(), e["name"], cls._pat(a), True))
        # 长的排前面 —— "parkway east"(12) 要先于 "parkway"(7) 被检查
        pairs.sort(key=lambda x: len(x[0]), reverse=True)

        core_pat = None
        if core_keywords:
            wordy = [k for k in core_keywords if re.match(r"^\w.*\w$", k)]
            core_pat = re.compile(
                "|".join(rf"\b{re.escape(k.lower())}s?\b" for k in wordy), re.I)
        return cls(pairs=pairs, core_pat=core_pat)

    @staticmethod
    def _pat(a: str) -> re.Pattern:
        # s? = 允许可选复数。没有它的话 \bapollo hospital\b 匹配不上
        # "Apollo Hospitals" —— 结尾的 s 挡住了词边界。
        # 实测：Apollo / Pantai / Fortis 两轮探针都 0 条，就是栽在这里。
        return re.compile(rf"\b{re.escape(a.lower())}s?\b", re.I)

    def match(self, text: str) -> Optional[Tuple[str, str]]:
        """返回 (实体名, 命中的别名)；没命中返回 None。"""
        low = (text or "").lower()
        has_core = bool(self.core_pat.search(low)) if self.core_pat else True
        for alias, entity, pat, weak in self.pairs:
            if weak and not has_core:
                continue          # 弱别名必须有医疗核心词背书
            if pat.search(low):
                return entity, alias
        return None


# ===========================================================================
# 4. 去重
# ===========================================================================

def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def dedupe(
    items: List[Dict[str, Any]],
    threshold: float = 0.80,
    aggregator_domains: Optional[List[str]] = None,
    prefer_non_aggregator: bool = True,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    两道去重：
      1. 规范化 URL 完全相同
      2. 标题相似度 >= threshold

    第 2 道命中时，若 prefer_non_aggregator=True，
    保留非聚合器的那条（MSN 转载 vs 原发媒体 -> 留原发）。
    """
    aggregator_domains = aggregator_domains or []
    stats = {"by_url": 0, "by_title": 0}

    # --- 1. URL 去重 ---
    by_url: Dict[str, Dict[str, Any]] = {}
    for it in items:
        u = it.get("url_normalized") or it.get("url", "")
        if not u:
            continue
        if u in by_url:
            stats["by_url"] += 1
            old, new = by_url[u], it
            if prefer_non_aggregator and is_aggregator(old.get("url", ""), aggregator_domains) \
               and not is_aggregator(new.get("url", ""), aggregator_domains):
                by_url[u] = new
        else:
            by_url[u] = it
    stage1 = list(by_url.values())

    # --- 2. 标题相似度去重 ---
    kept: List[Dict[str, Any]] = []
    for it in stage1:
        dup_idx = None
        for i, k in enumerate(kept):
            if title_similarity(it.get("title", ""), k.get("title", "")) >= threshold:
                dup_idx = i
                break
        if dup_idx is None:
            kept.append(it)
            continue

        stats["by_title"] += 1
        if prefer_non_aggregator:
            old_agg = is_aggregator(kept[dup_idx].get("url", ""), aggregator_domains)
            new_agg = is_aggregator(it.get("url", ""), aggregator_domains)
            if old_agg and not new_agg:
                kept[dup_idx] = it   # 用原发媒体替掉聚合器

    return kept, stats
