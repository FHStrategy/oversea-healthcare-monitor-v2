"""
主流程。每天跑一次。

    抓取 -> 筛选 -> 富化
      -> 合并进 data/articles.json（7天滚动窗口）
      -> 写飞书（全量底稿，append-only）
      -> 生成 site/index.html（7天视图）
      -> 审计落盘 data/audit/YYYY-MM-DD.jsonl
    任一硬失败 -> 发飞书告警 + 非零退出（Actions 红叉）

设计原则（贯穿整个项目）：失败要响亮。
  旧系统每一步都 except: pass，跑挂了还报绿灯，产出英文页面没人发现。
  新系统反过来：任何一步出问题就停、就告警、就红叉。
  data/articles.json 和 data/audit/ 由 workflow commit 回仓
  —— runner 无状态，不 commit 回来，明天就没有 7 天历史。
"""

import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
import llm                                         # noqa: E402
from pipeline import collect                       # noqa: E402
from sinks import feishu, site                     # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc
CN = timezone(timedelta(hours=8))
SUMMARY = []


def log(s=""):
    print(s, flush=True)
    SUMMARY.append(str(s))


def load_store(path: Path) -> list:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"⚠️ 读取 {path.name} 失败（当空处理）: {e}")
        return []


def merge_window(old: list, new: list, max_days: int, now: datetime) -> tuple:
    """
    7 天滚动窗口合并。
      - 按规范化 URL 去重（新数据优先，可能有更新的 published_at）
      - 剔除超过 max_days 的
    返回 (合并后列表, 新增数, 过期剔除数)。
    """
    cutoff = now - timedelta(days=max_days)

    def norm(u):
        return (u or "").rstrip("/")

    by_url = {}
    for it in old:
        u = norm(it.get("url_normalized") or it.get("url"))
        if u:
            by_url[u] = it

    added = 0
    for it in new:
        u = norm(it.get("url_normalized") or it.get("url"))
        if not u:
            continue
        if u not in by_url:
            added += 1
        by_url[u] = it   # 新数据覆盖旧（published_at 可能更准）

    merged, expired = [], 0
    for it in by_url.values():
        pa = it.get("published_at")
        if not pa:
            continue
        try:
            if datetime.fromisoformat(pa) >= cutoff:
                merged.append(it)
            else:
                expired += 1
        except Exception:
            continue

    merged.sort(key=lambda x: x.get("published_at", ""), reverse=True)
    return merged, added, expired


def main():
    t0 = datetime.now(UTC)
    log("=" * 60)
    log(f"海外医疗动态监测 · {t0.astimezone(CN).strftime('%Y-%m-%d %H:%M')} (北京)")
    log("=" * 60)

    S = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))
    F = yaml.safe_load((ROOT / "config" / "filters.yaml").read_text(encoding="utf-8"))
    max_days = F.get("retention", {}).get("max_days", 7)

    # ---- 1. 抓取 + 筛选 + 富化 ----
    llm.reset_usage()
    today_items, audit, stats = collect(S, F, log=log)
    usage = dict(llm.USAGE)

    log("")
    log(f"漏斗: 抓取 {stats['raw']} → 去重 {stats['deduped']} → "
        f"规则-{stats['filter_rule_dropped']} 模型-{stats['filter_model_dropped']} "
        f"→ 富化{'-' + str(stats['enrich_failed']) if stats['enrich_failed'] else ''} "
        f"→ 今日入库 {len(today_items)}")
    log(f"成本: {usage['calls']} 次调用, "
        f"{usage['prompt_tokens'] + usage['completion_tokens']} tokens, "
        f"耗时 {(datetime.now(UTC) - t0).seconds}s")

    # ---- 2. 7 天窗口合并 ----
    store_path = ROOT / "data" / "articles.json"
    old = load_store(store_path)
    merged, added, expired = merge_window(old, today_items, max_days, t0)
    log(f"窗口: 历史 {len(old)} + 今日新增 {added} - 过期 {expired} = {len(merged)} 条（{max_days}天）")

    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 3. 审计落盘 ----
    audit_dir = ROOT / "data" / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / f"{t0.astimezone(CN).strftime('%Y-%m-%d')}.jsonl"
    with open(audit_path, "w", encoding="utf-8") as f:
        for a in audit:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
    kept = sum(1 for a in audit if a.get("included"))
    log(f"审计: {len(audit)} 条记录（入库 {kept} / 过滤 {len(audit) - kept}）→ {audit_path.name}")

    # ---- 4. 写飞书（全量底稿，只写今天入库的新条目）----
    fb_ok, fb_skip, fb_err = feishu.write(today_items, S)
    log(f"飞书: 新增 {fb_ok} 条, 跳过重复 {fb_skip} 条")
    if fb_err:
        for e in fb_err[:5]:
            log(f"  ⚠️ {e}")

    # ---- 5. 生成网页（7天窗口全量）----
    site_path = ROOT / "site" / "index.html"
    n = site.write(merged, S, site_path)
    log(f"网页: {site_path.relative_to(ROOT)} ({n:,} 字节, {len(merged)} 条)")

    # ---- 6. 统计 → Step Summary ----
    if gs := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(gs, "a", encoding="utf-8") as f:
            f.write("## 每日运行\n\n```\n" + "\n".join(SUMMARY) + "\n```\n")

    # 飞书写入报错视为硬失败（数据没进底稿）
    if fb_err:
        raise RuntimeError(f"飞书写入有 {len(fb_err)} 条失败")

    log("\n✅ 完成")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        tb = traceback.format_exc()
        log(f"\n❌ 流水线失败: {e}")
        print(tb, flush=True)
        # 发告警（尽力而为，失败不掩盖原始错误）
        try:
            from sinks import feishu
            when = datetime.now(CN).strftime("%m-%d %H:%M")
            feishu.send_alert(
                f"🔴 海外医疗监测流水线失败\n\n"
                f"时间: {when}\n"
                f"错误: {e}\n\n"
                f"详情见 GitHub Actions 运行日志。"
            )
        except Exception as ae:
            print(f"⚠️ 告警也失败了: {ae}", flush=True)
        sys.exit(1)
