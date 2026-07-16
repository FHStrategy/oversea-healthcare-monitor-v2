"""
端到端探针：抓取 -> 规范化 -> 去重 -> 实体匹配 -> 筛选 -> 富化 -> 打印。

刻意【不】写飞书、【不】生成网页。
先看内容对不对，再谈往哪写。

用法:
    python src/probe_pipeline.py
    python src/probe_pipeline.py --limit-entities 5   # 省额度
    python src/probe_pipeline.py --no-model           # 跳过模型，只跑规则层
"""

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
import enrich                                          # noqa: E402
import filter as flt                                   # noqa: E402
import llm                                             # noqa: E402
from firecrawl_client import FirecrawlError, search_news   # noqa: E402
from normalize import (AliasIndex, dedupe, domain_of,      # noqa: E402
                       is_aggregator, normalize_url, parse_date)
from probe_fetch import build_tasks                     # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc
LINES = []


def out(s=""):
    LINES.append(s)
    print(s, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-entities", type=int, default=None)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--no-model", action="store_true")
    args = ap.parse_args()

    S = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))
    F = yaml.safe_load((ROOT / "config" / "filters.yaml").read_text(encoding="utf-8"))
    if args.no_model:
        F.setdefault("model_filter", {})["enabled"] = False

    t_start = time.time()
    tbs = S["defaults"]["tbs"]
    tasks, searched = build_tasks(S, args.limit_entities)

    out("=" * 66)
    out(f"端到端探针  {datetime.now(UTC).isoformat()}")
    out(f"实体 {len(searched)}/{len(S['entities'])} | 搜索 {len(tasks)} 条 | tbs={tbs!r}")
    out(f"模型: {'❌ 跳过' if args.no_model else '✅ qwen-plus'}")
    out("=" * 66)

    # ---------- 1. 抓取 ----------
    raw, failures = [], []

    def go(t):
        kind, owner, q, lim = t
        return t, search_news(q, limit=lim, tbs=tbs)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
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

    if tasks and len(failures) / len(tasks) > 0.30:
        out(f"\n❌ 抓取失败率 {len(failures)/len(tasks):.0%} 过高")
        sys.exit(1)
    if not raw:
        out("\n❌ 一条都没抓到")
        sys.exit(1)
    t_fetch = time.time()

    # ---------- 2. 规范化 ----------
    raw = [x for x in raw if domain_of(x.get("url", ""))]   # 剔 Google News 跳转链接
    now = datetime.now(UTC)
    url_cfg, aggs, dd = F["url_normalize"], F["aggregator_domains"], F["dedupe"]
    for it in raw:
        it["url_normalized"] = normalize_url(it.get("url", ""), url_cfg)
        it["is_aggregator"] = is_aggregator(it.get("url", ""), aggs)
        d = parse_date(it.get("date"), now)
        it["published_at"] = d.isoformat() if d else None

    # ---------- 3. 实体匹配 ----------
    idx = AliasIndex.build(S["entities"])
    for it in raw:
        m = idx.match(f"{it.get('title','')} {it.get('snippet','')}") \
            if it["_kind"] == "entity" else None
        it["matched_entity"] = m[0] if m else None
        it["matched_alias"] = m[1] if m else None

    # ---------- 4. 去重 ----------
    ent = [x for x in raw if x["_kind"] == "entity"]
    sea = [x for x in raw if x["_kind"] != "entity"]
    ent_k, es = dedupe(ent, dd["by_title_similarity"], aggs, dd["prefer_non_aggregator"])
    sea_k, ss = dedupe(sea, dd["by_title_similarity_sea"], aggs, dd["prefer_non_aggregator"])
    deduped = ent_k + sea_k

    # ---------- 5. 筛选 ----------
    llm.reset_usage()
    passed, audit, fstats = flt.run(deduped, F)
    filter_usage = dict(llm.USAGE)
    t_filter = time.time()

    # ---------- 6. 富化 ----------
    enriched, enr_failed = ([], [])
    if not args.no_model:
        enriched, enr_failed = enrich.run(passed, max_workers=8)
    else:
        enriched = passed
    t_end = time.time()

    # ======================= 报告 =======================
    out("")
    out("=" * 66)
    out("### 漏斗")
    out("=" * 66)
    out(f"  原始召回                {len(raw):>4}")
    out(f"  去重后                  {len(deduped):>4}   (-{es['by_url']+ss['by_url']} URL, -{es['by_title']+ss['by_title']} 标题)")
    out(f"  规则层拦截             -{fstats['rule_dropped']:>4}")
    out(f"  模型层判掉             -{fstats['model_dropped']:>4}")
    if fstats["model_errors"]:
        out(f"  模型调用失败(保留待复核) {fstats['model_errors']:>4}")
    out(f"  筛选后                  {fstats['out']:>4}")
    if not args.no_model:
        out(f"  富化失败(不入库)       -{len(enr_failed):>4}")
    out(f"  ─────────────────────────")
    out(f"  最终入库                {len(enriched):>4}   ({len(enriched)/len(raw):.0%} of 原始)")

    if not args.no_model:
        out("")
        out("=" * 66)
        out("### 事件类型：规则 vs 模型")
        out("=" * 66)
        out(f"  分歧          {fstats.get('type_disagree',0):>3} / {fstats['out']}  ← 分歧高的类别就是规则表该补词的地方")
        out(f"  模型给了非法值 {fstats.get('type_invalid',0):>3}       (已回退规则结果)")

    out("")
    out("=" * 66)
    out("### 成本")
    out("=" * 66)
    tot = dict(llm.USAGE)
    out(f"  筛选   {filter_usage['calls']:>3} 次调用, {filter_usage['prompt_tokens']+filter_usage['completion_tokens']:>6} tokens")
    out(f"  富化   {tot['calls']-filter_usage['calls']:>3} 次调用, {tot['prompt_tokens']+tot['completion_tokens']-filter_usage['prompt_tokens']-filter_usage['completion_tokens']:>6} tokens")
    out(f"  合计   {tot['calls']:>3} 次调用, {tot['prompt_tokens']+tot['completion_tokens']:>6} tokens "
        f"(in {tot['prompt_tokens']} / out {tot['completion_tokens']})")
    out(f"  耗时   抓取 {t_fetch-t_start:.0f}s | 筛选 {t_filter-t_fetch:.0f}s | 富化 {t_end-t_filter:.0f}s | 总 {t_end-t_start:.0f}s")
    out(f"         (旧系统 ~325 次串行调用 x 13s ≈ 70 分钟，撞穿 1 小时 cron 上限)")

    # 板块分布
    emap = {e["name"]: e for e in S["entities"]}
    board = defaultdict(list)
    for it in enriched:
        e = emap.get(it.get("matched_entity") or "")
        b = ("IHH集团" if e.get("group") == "IHH Healthcare" else e["country"]) if e \
            else it.get("board", "行业动态")
        board[b].append(it)

    out("")
    out("=" * 66)
    out("### 最终入库内容（这就是会进飞书和网页的东西）")
    out("=" * 66)
    order = ["IHH集团", "新加坡", "马来西亚", "泰国", "印度", "行业动态", "其他"]
    for b in order + [k for k in board if k not in order]:
        rows = board.get(b, [])
        if not rows:
            continue
        out(f"\n┌─ {b}  ({len(rows)} 条)")
        for it in rows:
            src = it.get("_event_type_source", "rule")
            mark = "🔍" if it.get("needs_review") else ""
            out(f"│")
            out(f"│  【{it.get('event_type','?')}】{it.get('matched_entity') or '—'}  "
                f"{mark}  {domain_of(it.get('url',''))}")
            out(f"│  {it.get('title_zh') or '(未翻译)'}")
            out(f"│    原: {(it.get('title') or '')[:70]}")
            out(f"│    摘: {it.get('summary_zh') or '(无)'}")
            if it.get("_event_type_model") and it["_event_type_model"] != it.get("_event_type_rule"):
                out(f"│    ⚖️ 类型分歧: 规则={it['_event_type_rule']} 模型={it['_event_type_model']} → 用了{src}")
        out("└─")

    # 被判掉的
    m_drop = [a for a in audit if a["stage"] == "model" and not a["included"]]
    if m_drop:
        out("")
        out("=" * 66)
        out(f"### 模型判掉的 {len(m_drop)} 条（请复核有无误杀）")
        out("=" * 66)
        for a in m_drop:
            out(f"  ❌ {a['reason']}")
            out(f"     {(a['title'] or '')[:76]}")

    r_drop = Counter(a["reason"].split("[")[0] for a in audit
                     if a["stage"] == "rule" and not a["included"])
    out("")
    out("=" * 66)
    out(f"### 规则层拦截原因 Top（共 {fstats['rule_dropped']} 条）")
    out("=" * 66)
    for k, v in r_drop.most_common(12):
        out(f"  {v:>3}  {k}")

    if enr_failed:
        out("")
        out(f"### ⚠️ 富化失败 {len(enr_failed)} 条（未入库）")
        for _, e in enr_failed[:8]:
            out(f"  {e}")

    # ---------- 落盘 ----------
    od = ROOT / "probe_output"
    od.mkdir(exist_ok=True)
    (od / "final.json").write_text(json.dumps(enriched, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(od / "audit.jsonl", "w", encoding="utf-8") as f:
        for a in audit:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
    out(f"\n已写入 probe_output/final.json ({len(enriched)}) 和 audit.jsonl ({len(audit)})")

    if s := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(s, "a", encoding="utf-8") as f:
            f.write("## 端到端探针\n\n```\n" + "\n".join(LINES) + "\n```\n")

    out("\n✅ 完成")


if __name__ == "__main__":
    main()
