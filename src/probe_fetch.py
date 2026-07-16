"""
探针：只做 抓取 -> URL规范化 -> 日期解析 -> 实体匹配 -> 去重 -> 统计。

刻意【不】碰：模型、飞书、网页生成。
目的是拿到一个干净的基线数字：今天真实召回多少、去重后剩多少、分布如何。
先把源头量清楚，再谈筛选 —— 先定位，再修复。

用法:
    python src/probe_fetch.py            # 全量
    python src/probe_fetch.py --limit-entities 5   # 只跑前 5 个机构（省额度）
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from firecrawl_client import FirecrawlError, search_news          # noqa: E402
from normalize import (AliasIndex, dedupe, domain_of, is_aggregator,  # noqa: E402
                       normalize_url, parse_date)

ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc


def log(msg=""):
    print(msg, flush=True)


def build_tasks(sources, limit_entities=None):
    """把配置展开成 (类别, 归属, query) 的扁平任务列表。"""
    tasks = []
    d = sources["defaults"]

    ents = sources["entities"]
    if limit_entities:
        ents = ents[:limit_entities]
    for e in ents:
        for q in e.get("queries", []):
            tasks.append(("entity", e["name"], q, d["limit"]))

    for s in sources.get("news_sources", []):
        for kw in s.get("keywords", []):
            tasks.append(("news_source", s["name"], f'site:{s["site"]} {kw}', 5))

    sea = sources.get("sea_health", {})
    for c in sea.get("countries", []):
        for site in c["sites"][: sea.get("max_sites_per_country", 1)]:
            for kw in sea.get("keywords", [])[: sea.get("max_keywords_per_site", 2)]:
                tasks.append(("sea", c["name"], f"site:{site} {kw}", sea.get("limit", 3)))

    return tasks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-entities", type=int, default=None)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    sources = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))
    filters = yaml.safe_load((ROOT / "config" / "filters.yaml").read_text(encoding="utf-8"))

    tbs = sources["defaults"]["tbs"]
    url_cfg = filters["url_normalize"]
    aggs = filters["aggregator_domains"]
    dd = filters["dedupe"]

    tasks = build_tasks(sources, args.limit_entities)
    log("=" * 64)
    log(f"探针启动  {datetime.now(UTC).isoformat()}")
    log(f"实体 {len(sources['entities'])} 个 | 搜索任务 {len(tasks)} 条 | tbs={tbs!r}")
    log("=" * 64)

    # ---------- 抓取（并发）----------
    raw, failures = [], []

    def run(task):
        kind, owner, query, limit = task
        return task, search_news(query, limit=limit, tbs=tbs)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(run, t): t for t in tasks}
        for i, f in enumerate(as_completed(futs), 1):
            task = futs[f]
            kind, owner, query, _ = task
            try:
                _, items = f.result()
            except FirecrawlError as e:
                failures.append((query, str(e)))
                log(f"  [{i:>2}/{len(tasks)}] ❌ {query[:44]:<44} {e}")
                continue
            for it in items:
                it["_kind"] = kind
                it["_query_owner"] = owner
                it["_query"] = query
            raw.extend(items)
            log(f"  [{i:>2}/{len(tasks)}] {len(items):>2} 条  {query[:50]}")

    log("")
    log(f"抓取完成: {len(raw)} 条原始结果, {len(failures)} 个 query 失败")

    # 失败率超 30% 直接红灯 —— 不把「搜索挂了」伪装成「今天没新闻」
    if tasks and len(failures) / len(tasks) > 0.30:
        log(f"\n❌ 失败率 {len(failures)/len(tasks):.0%} 过高，判定为抓取异常")
        for q, e in failures[:10]:
            log(f"   {q}  ->  {e}")
        sys.exit(1)

    if not raw:
        log("\n❌ 一条都没抓到 —— 不正常，请检查")
        sys.exit(1)

    # ---------- 规范化 ----------
    now = datetime.now(UTC)
    no_date, undated_samples = 0, []
    for it in raw:
        it["url_normalized"] = normalize_url(it.get("url", ""), url_cfg)
        it["is_aggregator"] = is_aggregator(it.get("url", ""), aggs)
        dt = parse_date(it.get("date"), now)
        if dt:
            it["published_at"] = dt.isoformat()
        else:
            it["published_at"] = None
            no_date += 1
            if len(undated_samples) < 5:
                undated_samples.append((it.get("date"), it.get("title", "")[:50]))

    # ---------- 实体匹配（词边界 + 最长匹配）----------
    idx = AliasIndex.build(sources["entities"])
    agree = disagree = unmatched = 0
    for it in raw:
        if it["_kind"] != "entity":
            it["matched_entity"] = None
            continue
        text = f"{it.get('title','')} {it.get('snippet','')}"
        m = idx.match(text)
        if not m:
            it["matched_entity"] = None
            it["matched_alias"] = None
            unmatched += 1
        else:
            it["matched_entity"], it["matched_alias"] = m
            if m[0] == it["_query_owner"]:
                agree += 1
            else:
                disagree += 1

    # ---------- 去重 ----------
    ent_items = [x for x in raw if x["_kind"] == "entity"]
    sea_items = [x for x in raw if x["_kind"] != "entity"]

    ent_kept, ent_stats = dedupe(ent_items, dd["by_title_similarity"], aggs, dd["prefer_non_aggregator"])
    sea_kept, sea_stats = dedupe(sea_items, dd["by_title_similarity_sea"], aggs, dd["prefer_non_aggregator"])
    kept = ent_kept + sea_kept

    # ---------- 统计 ----------
    lines = []
    def out(s=""):
        lines.append(s)
        log(s)

    out("")
    out("=" * 64)
    out("### 漏斗")
    out("=" * 64)
    out(f"原始召回          {len(raw):>4}")
    out(f"  机构新闻        {len(ent_items):>4}")
    out(f"  行业源+东南亚    {len(sea_items):>4}")
    out(f"URL 去重          -{ent_stats['by_url']+sea_stats['by_url']:>3}")
    out(f"标题相似度去重     -{ent_stats['by_title']+sea_stats['by_title']:>3}")
    out(f"去重后剩余        {len(kept):>4}   ({len(kept)/len(raw):.0%})")

    out("")
    out("=" * 64)
    out("### 实体匹配（词边界 + 最长匹配）")
    out("=" * 64)
    out(f"query 机构 == 匹配机构   {agree:>4}  ✅")
    out(f"query 机构 != 匹配机构   {disagree:>4}  (被重新归属)")
    out(f"完全没匹配上任何机构      {unmatched:>4}  ❌ 会被丢弃")
    if ent_items:
        out(f"→ query 精度: {agree/len(ent_items):.0%}")

    out("")
    out("=" * 64)
    out("### 日期")
    out("=" * 64)
    out(f"解析成功  {len(raw)-no_date:>4} / {len(raw)}")
    if no_date:
        out(f"解析失败  {no_date:>4}   ⚠️ 无 published_at 就进不了 7 天窗口")
        for d, t in undated_samples:
            out(f"    date={d!r}  {t}")

    # 板块分布
    board = defaultdict(int)
    emap = {e["name"]: e for e in sources["entities"]}
    for it in ent_kept:
        e = emap.get(it.get("matched_entity") or "")
        if not e:
            continue
        b = "IHH集团" if e.get("group") == "IHH Healthcare" else e["country"]
        board[b] += 1
    out("")
    out("=" * 64)
    out("### 板块分布（去重后）")
    out("=" * 64)
    for b in ["IHH集团", "新加坡", "马来西亚", "泰国", "印度", "其他"]:
        out(f"  {b:<8} {board.get(b,0):>3}")

    out("")
    out("=" * 64)
    out("### 机构分布（去重后，Top 15）")
    out("=" * 64)
    cnt = Counter(x["matched_entity"] for x in ent_kept if x.get("matched_entity"))
    for name, n in cnt.most_common(15):
        out(f"  {n:>3}  {name}")
    zero = [e["name"] for e in sources["entities"] if e["name"] not in cnt]
    if zero:
        out(f"\n  0 条的机构 ({len(zero)}): {', '.join(zero)}")

    out("")
    out("=" * 64)
    out("### 域名分布 Top 12（看聚合器占比）")
    out("=" * 64)
    dc = Counter(domain_of(x.get("url", "")) for x in kept)
    for d, n in dc.most_common(12):
        flag = "  ← 聚合器" if is_aggregator(f"https://{d}/", aggs) else ""
        out(f"  {n:>3}  {d}{flag}")
    agg_n = sum(1 for x in kept if x["is_aggregator"])
    out(f"\n聚合器占比: {agg_n}/{len(kept)} = {agg_n/len(kept):.0%}")

    # ---------- 落盘 ----------
    outdir = ROOT / "probe_output"
    outdir.mkdir(exist_ok=True)
    (outdir / "raw.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "deduped.json").write_text(
        json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"\n已写入 probe_output/raw.json ({len(raw)}) 和 deduped.json ({len(kept)})")

    # GitHub Step Summary
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## 抓取探针结果\n\n```\n" + "\n".join(lines) + "\n```\n")

    log("\n✅ 探针完成")


if __name__ == "__main__":
    main()
