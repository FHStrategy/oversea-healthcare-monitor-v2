"""
飞书写入探针。

三种模式，从便宜到贵：

  --dry-run     不调模型、不写飞书。只跑抓取+规则层，组装 record 打印出来看。
                验证字段类型对不对。0 成本。

  --synthetic   写 3 条精心构造的边界用例（含 & < > 特殊字符、超长标题、
                行业动态空机构），验证完立刻【删掉】。表保持干净。

  （默认）       跑真实流水线，写真数据，【保留】。
                建议先跑前两个模式确认无误再用。
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from pipeline import collect                       # noqa: E402
from sinks import feishu                           # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc


def log(s=""):
    print(s, flush=True)


SYNTH = [
    {   # 边界1：标题含 HTML 特殊字符 + 完整机构信息
        "title": "Fortis & Apollo <partner> for R&D — 50% growth expected",
        "title_zh": "【探针测试】Fortis 与 Apollo 达成研发合作",
        "summary_zh": "这是连通性测试记录，看到它说明写入成功，稍后会被自动删除。",
        "matched_entity": "Fortis Healthcare",
        "event_type": "合作/合资",
        "url": "https://example.com/probe-test-1",
    },
    {   # 边界2：行业动态通道 —— 机构和集团都该是空
        "title": "PROBE TEST " + "x" * 200,   # 超长标题，验证链接 text 截断
        "title_zh": "【探针测试】行业动态条目（机构字段应为空）",
        "summary_zh": "这是连通性测试记录，稍后会被自动删除。",
        "matched_entity": None,
        "board": "行业动态",
        "event_type": "政策法规",
        "url": "https://example.com/probe-test-2",
    },
    {   # 边界3：IHH 系机构 —— 国家/地区应为「IHH集团」而非「新加坡」
        "title": "PROBE TEST: Mount Elizabeth item",
        "title_zh": "【探针测试】IHH 系机构（国家/地区应为 IHH集团）",
        "summary_zh": "这是连通性测试记录，稍后会被自动删除。",
        "matched_entity": "Mount Elizabeth Hospital",
        "event_type": "扩张/新设",
        "url": "https://example.com/probe-test-3",
    },
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--limit-entities", type=int, default=None)
    args = ap.parse_args()

    S = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))
    F = yaml.safe_load((ROOT / "config" / "filters.yaml").read_text(encoding="utf-8"))
    emap = {e["name"]: e for e in S["entities"]}

    # ================= 模式 A: dry-run =================
    if args.dry_run:
        log("=" * 62)
        log("模式: dry-run（不调模型、不写飞书）")
        log("=" * 62)
        F.setdefault("model_filter", {})["enabled"] = False
        items, _, stats = collect(S, F, limit_entities=args.limit_entities or 5, log=log)
        log(f"\n抓取 {stats['raw']} -> 去重 {stats['deduped']} -> 规则层后 {stats['filter_out']}")
        log(f"\n组装成飞书 record（前 3 条）：\n")
        for it in items[:3]:
            it.setdefault("title_zh", "(dry-run 未翻译)")
            it.setdefault("summary_zh", "(dry-run 未翻译)")
            log(json.dumps(feishu.to_record(it, emap), ensure_ascii=False, indent=2))
            log("")
        log("✅ dry-run 完成，未写入任何数据")
        return

    # ================= 连通 + 字段核对 =================
    log("=" * 62)
    log("飞书连通性 + 字段核对")
    log("=" * 62)
    b = feishu.Base(feishu.get_token(),
                    os.environ["FEISHU_BASE_TOKEN"], os.environ["FEISHU_TABLE_ID"])
    log("✅ tenant_access_token 换取成功")

    have = b.fields()
    need = ["时间", "国家/地区", "机构", "集团", "事件类型",
            "中文标题", "原标题", "摘要（中文）", "链接"]
    for f in need:
        t = have.get(f)
        note = {5: "  ← 日期，必须传毫秒时间戳",
                15: "  ← 超链接，必须传 {text, link}",
                3: "  ← 单选，选项名必须逐字一致"}.get(t, "")
        log(f"  {'✅' if f in have else '❌ 缺失'}  type={t}  {f}{note}")
    missing = [f for f in need if f not in have]
    if missing:
        log(f"\n❌ 缺字段: {missing}")
        sys.exit(1)

    before = b.existing_urls()
    log(f"\n表中已有 {len(before)} 条记录（读全表做 append 去重）")

    # ================= 模式 B: synthetic =================
    if args.synthetic:
        log("")
        log("=" * 62)
        log("模式: synthetic（写 3 条边界用例，验证后删除）")
        log("=" * 62)
        now = datetime.now(UTC)
        items = []
        for i, s in enumerate(SYNTH):
            items.append({**s, "published_at": (now - timedelta(hours=i)).isoformat()})

        recs = [feishu.to_record(i, emap) for i in items]
        log("\n将写入：")
        for r in recs:
            f = r["fields"]
            log(f"  时间={f['时间']}({type(f['时间']).__name__})  国家/地区={f['国家/地区']!r}  "
                f"机构={f['机构']!r}  集团={f['集团']!r}  事件类型={f['事件类型']!r}")
            log(f"     链接={type(f['链接']).__name__}  text 长度={len(f['链接']['text'])}")

        ok, errors = b.batch_create(recs)
        log(f"\n写入成功 {ok}/{len(recs)}")
        for e in errors:
            log(f"  ⚠️ {e}")
        if ok != len(recs):
            log("\n❌ 有写入失败")
            sys.exit(1)

        # 读回来验证
        after = b.existing_urls()
        added = after - before
        log(f"\n读回验证：新增 {len(added)} 条 URL")
        for u in sorted(added):
            log(f"  {u}")

        # 清理
        log("\n清理测试记录…")
        d = feishu._req("GET", b.url, b.t, params={"page_size": 500})
        killed = 0
        for rec in d["data"].get("items", []) or []:
            link = (rec.get("fields") or {}).get("链接")
            u = link.get("link") if isinstance(link, dict) else link
            if u and "example.com/probe-test" in str(u):
                feishu._req("DELETE", f"{b.url}/{rec['record_id']}", b.t)
                killed += 1
        log(f"✅ 已删除 {killed} 条测试记录，表恢复干净")
        if killed != len(recs):
            log(f"⚠️ 预期删 {len(recs)} 条，实际 {killed} 条 —— 请手动检查表")
        return

    # ================= 模式 C: 真实数据 =================
    log("")
    log("=" * 62)
    log("模式: 真实数据（会保留在表里）")
    log("=" * 62)
    items, audit, stats = collect(S, F, limit_entities=args.limit_entities, log=log)
    log(f"\n漏斗: 抓取 {stats['raw']} -> 去重 {stats['deduped']} -> "
        f"规则-{stats['filter_rule_dropped']} 模型-{stats['filter_model_dropped']} -> "
        f"入库 {stats['final']}")

    ok, skipped, errors = feishu.write(items, S)
    log(f"\n新增 {ok} 条 | 跳过重复 {skipped} 条")
    for e in errors:
        log(f"  ⚠️ {e}")

    after = b.existing_urls()
    log(f"\n表中现有 {len(after)} 条（写入前 {len(before)}，净增 {len(after)-len(before)}）")

    if errors:
        log("\n❌ 有写入错误")
        sys.exit(1)
    log("\n✅ 完成")


if __name__ == "__main__":
    main()
