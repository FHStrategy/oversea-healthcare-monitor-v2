"""
流水线核心：collect() = 抓取 -> 规范化 -> 去重 -> 实体匹配 -> 筛选 -> 富化。

从 probe_pipeline.py 里抽出来，好让 probe_feishu.py 和 run.py 复用。
它【不】负责写任何 sink，也不管 7 天窗口 —— 那是调用方的事。
"""

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).parent))
import enrich                                              # noqa: E402
import filter as flt                                       # noqa: E402
from firecrawl_client import FirecrawlError, search_news    # noqa: E402
from normalize import (AliasIndex, dedupe, domain_of,       # noqa: E402
                       is_aggregator, normalize_url, parse_date)
from probe_fetch import build_tasks                         # noqa: E402

UTC = timezone.utc


def collect(S: Dict[str, Any], F: Dict[str, Any], *,
            limit_entities: int = None, workers: int = 6,
            log=print) -> Tuple[List[Dict], List[Dict], Dict[str, Any]]:
    """返回 (入库条目, 审计记录, 统计)。任何一步硬失败都会抛异常。"""
    stats: Dict[str, Any] = {}
    tbs = S["defaults"]["tbs"]
    tasks, searched = build_tasks(S, limit_entities)
    stats["searched_entities"] = len(searched)
    stats["queries"] = len(tasks)

    # ---- 1. 抓取 ----
    raw, failures = [], []

    def go(t):
        kind, owner, q, lim = t
        return t, search_news(q, limit=lim, tbs=tbs)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for f in as_completed([ex.submit(go, t) for t in tasks]):
            try:
                t, items = f.result()
            except FirecrawlError as e:
                failures.append(str(e))
                continue
            kind, owner, q, _ = t
            for it in items:
                it["_kind"], it["_query_owner"], it["_query"] = kind, owner, q
            raw.extend(items)

    stats["fetch_failures"] = len(failures)
    # 失败率过高就是抓取异常，不能伪装成「今天没新闻」
    if tasks and len(failures) / len(tasks) > 0.30:
        raise RuntimeError(f"抓取失败率 {len(failures)/len(tasks):.0%} 过高: {failures[:3]}")

    # Firecrawl 偶尔漏出 Google News 相对跳转链接（/goto?url=CAESuQEB...），
    # 解析不出域名，规范化和去重都会失效
    raw = [x for x in raw if domain_of(x.get("url", ""))]
    stats["raw"] = len(raw)
    if not raw:
        raise RuntimeError("一条都没抓到 —— 不正常")

    # ---- 2. 规范化 ----
    now = datetime.now(UTC)
    url_cfg, aggs, dd = F["url_normalize"], F["aggregator_domains"], F["dedupe"]
    # 日期合理性门槛：我们搜的是 qdr:d（过去24h），排序 sbd:1，
    # 正常不该出现很久以前的东西。出现 = 脏数据（静态页的"最后修改时间"、
    # 乱填的 date 字段）。2026-07-20 dry-run 实证：混进过 2018-04 和 2024-07 的条目。
    # 放宽到 max_days（默认7）+ 一点缓冲，既挡脏数据又不误杀 sea_health 里
    # 官方发布稍慢的政策。未来日期也挡（时区/解析错误会产生未来时间戳）。
    from datetime import timedelta
    stale_days = F.get("retention", {}).get("max_days", 7) + 3
    stale = now - timedelta(days=stale_days)
    from_future = now + timedelta(days=1)
    n_stale = n_future = 0
    for it in raw:
        it["url_normalized"] = normalize_url(it.get("url", ""), url_cfg)
        it["is_aggregator"] = is_aggregator(it.get("url", ""), aggs)
        d = parse_date(it.get("date"), now)
        if d and d < stale:
            it["published_at"] = None
            it["_date_reason"] = f"日期过旧({d.date()})，qdr:d 不该有"
            n_stale += 1
        elif d and d > from_future:
            it["published_at"] = None
            it["_date_reason"] = f"日期在未来({d.date()})，解析异常"
            n_future += 1
        else:
            it["published_at"] = d.isoformat() if d else None
    stats["no_date"] = sum(1 for x in raw if not x["published_at"])
    stats["stale_dropped"] = n_stale
    stats["future_dropped"] = n_future
    if n_stale or n_future:
        log(f"⚠️ 日期门槛剔除: 过旧 {n_stale} 条, 未来 {n_future} 条")

    # ---- 3. 实体匹配（词边界 + 最长匹配 + 弱别名）----
    idx = AliasIndex.build(S["entities"], F.get("healthcare_core_keywords"))
    for it in raw:
        m = idx.match(f"{it.get('title','')} {it.get('snippet','')}") \
            if it["_kind"] == "entity" else None
        it["matched_entity"] = m[0] if m else None
        it["matched_alias"] = m[1] if m else None

    # ---- 4. 去重 ----
    ent = [x for x in raw if x["_kind"] == "entity"]
    sea = [x for x in raw if x["_kind"] != "entity"]
    ent_k, es = dedupe(ent, dd["by_title_similarity"], aggs, dd["prefer_non_aggregator"])
    sea_k, ss = dedupe(sea, dd["by_title_similarity_sea"], aggs, dd["prefer_non_aggregator"])
    deduped = ent_k + sea_k
    stats["dedup_url"] = es["by_url"] + ss["by_url"]
    stats["dedup_title"] = es["by_title"] + ss["by_title"]
    stats["deduped"] = len(deduped)

    # ---- 5. 筛选 ----
    passed, audit, fstats = flt.run(deduped, F)
    stats.update({f"filter_{k}": v for k, v in fstats.items()})

    # ---- 6. 富化 ----
    enriched, failed = enrich.run(passed, max_workers=8)
    stats["enrich_failed"] = len(failed)
    stats["final"] = len(enriched)

    # 富化失败的不入库 —— 一条英文标题混进给高层看的表里，比少一条更糟
    if failed:
        log(f"⚠️ 富化失败 {len(failed)} 条（未入库）:")
        for _, e in failed[:5]:
            log(f"   {e}")

    # 时间缺失的进不了 7 天窗口，也进不了飞书日期字段
    no_dt = [x for x in enriched if not x.get("published_at")]
    if no_dt:
        log(f"⚠️ {len(no_dt)} 条无 published_at，已剔除")
        enriched = [x for x in enriched if x.get("published_at")]

    return enriched, audit, stats

    def go(t):
        kind, owner, q, lim = t
        return t, search_news(q, limit=lim, tbs=tbs)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for f in as_completed([ex.submit(go, t) for t in tasks]):
            try:
                t, items = f.result()
            except FirecrawlError as e:
                failures.append(str(e))
                continue
            kind, owner, q, _ = t
            for it in items:
                it["_kind"], it["_query_owner"], it["_query"] = kind, owner, q
            raw.extend(items)

    stats["fetch_failures"] = len(failures)
    # 失败率过高就是抓取异常，不能伪装成「今天没新闻」
    if tasks and len(failures) / len(tasks) > 0.30:
        raise RuntimeError(f"抓取失败率 {len(failures)/len(tasks):.0%} 过高: {failures[:3]}")

    # Firecrawl 偶尔漏出 Google News 相对跳转链接（/goto?url=CAESuQEB...），
    # 解析不出域名，规范化和去重都会失效
    raw = [x for x in raw if domain_of(x.get("url", ""))]
    stats["raw"] = len(raw)
    if not raw:
        raise RuntimeError("一条都没抓到 —— 不正常")

    # ---- 2. 规范化 ----
    now = datetime.now(UTC)
    url_cfg, aggs, dd = F["url_normalize"], F["aggregator_domains"], F["dedupe"]
    for it in raw:
        it["url_normalized"] = normalize_url(it.get("url", ""), url_cfg)
        it["is_aggregator"] = is_aggregator(it.get("url", ""), aggs)
        d = parse_date(it.get("date"), now)
        it["published_at"] = d.isoformat() if d else None
    stats["no_date"] = sum(1 for x in raw if not x["published_at"])

    # ---- 3. 实体匹配（词边界 + 最长匹配 + 弱别名）----
    idx = AliasIndex.build(S["entities"], F.get("healthcare_core_keywords"))
    for it in raw:
        m = idx.match(f"{it.get('title','')} {it.get('snippet','')}") \
            if it["_kind"] == "entity" else None
        it["matched_entity"] = m[0] if m else None
        it["matched_alias"] = m[1] if m else None

    # ---- 4. 去重 ----
    ent = [x for x in raw if x["_kind"] == "entity"]
    sea = [x for x in raw if x["_kind"] != "entity"]
    ent_k, es = dedupe(ent, dd["by_title_similarity"], aggs, dd["prefer_non_aggregator"])
    sea_k, ss = dedupe(sea, dd["by_title_similarity_sea"], aggs, dd["prefer_non_aggregator"])
    deduped = ent_k + sea_k
    stats["dedup_url"] = es["by_url"] + ss["by_url"]
    stats["dedup_title"] = es["by_title"] + ss["by_title"]
    stats["deduped"] = len(deduped)

    # ---- 5. 筛选 ----
    passed, audit, fstats = flt.run(deduped, F)
    stats.update({f"filter_{k}": v for k, v in fstats.items()})

    # ---- 6. 富化 ----
    enriched, failed = enrich.run(passed, max_workers=8)
    stats["enrich_failed"] = len(failed)
    stats["final"] = len(enriched)

    # 富化失败的不入库 —— 一条英文标题混进给高层看的表里，比少一条更糟
    if failed:
        log(f"⚠️ 富化失败 {len(failed)} 条（未入库）:")
        for _, e in failed[:5]:
            log(f"   {e}")

    # 时间缺失的进不了 7 天窗口，也进不了飞书日期字段
    no_dt = [x for x in enriched if not x.get("published_at")]
    if no_dt:
        log(f"⚠️ {len(no_dt)} 条无 published_at，已剔除")
        enriched = [x for x in enriched if x.get("published_at")]

    return enriched, audit, stats
