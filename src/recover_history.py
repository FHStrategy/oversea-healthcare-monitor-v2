import json
import os
from pathlib import Path
from datetime import date

import yaml

ROOT = Path(__file__).resolve().parent.parent
START = date(2026, 9, 16)
END = date(2026, 10, 12)
EXCLUDE = {date(2026, 10, 11)}
REPORT = ROOT / "data" / "recovery-history-report.json"


def log(message):
    print(message, flush=True)


def norm_url(value):
    return (value or "").strip().rstrip("/")


def count_value(value):
    """兼容项目函数返回整数计数或列表。"""
    return value if isinstance(value, int) else len(value)


def error_samples(value, limit=100):
    """只有错误详情是列表时才提取样例。"""
    if isinstance(value, (list, tuple)):
        return [str(e)[:300] for e in value[:limit]]
    return []


def read_audit():
    audit_dir = ROOT / "data" / "audit"
    records = []
    files_seen = 0
    malformed = 0

    for path in sorted(audit_dir.glob("*.jsonl")):
        try:
            file_date = date.fromisoformat(path.stem)
        except ValueError:
            continue

        if not (START <= file_date <= END) or file_date in EXCLUDE:
            continue

        files_seen += 1

        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue

            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue

            if row.get("included") is not True:
                continue

            url = norm_url(row.get("url") or row.get("link"))
            title = (row.get("title") or row.get("原标题") or "").strip()

            if not url or not title:
                continue

            row["_audit_date"] = file_date.isoformat()
            row["url"] = url
            row["title"] = title
            row["matched_entity"] = (
                row.get("matched_entity")
                or row.get("entity")
                or row.get("机构")
                or ""
            )
            row["published_at"] = (
                row.get("published_at")
                or row.get("published")
                or row.get("date")
                or file_date.isoformat()
            )
            row["snippet"] = row.get("snippet") or row.get("description") or ""
            records.append(row)

    # 同一链接只保留审计日期较新的记录。
    by_url = {}
    for row in records:
        key = norm_url(row["url"])
        if (
            key not in by_url
            or row["_audit_date"] > by_url[key]["_audit_date"]
        ):
            by_url[key] = row

    return list(by_url.values()), files_seen, malformed


def main():
    mode = os.environ.get("RECOVERY_MODE", "preview").strip().lower()
    if mode not in {"preview", "write"}:
        raise SystemExit("RECOVERY_MODE 只能是 preview 或 write")

    sources = yaml.safe_load(
        (ROOT / "config" / "sources.yaml").read_text(encoding="utf-8")
    )
    filters = yaml.safe_load(
        (ROOT / "config" / "filters.yaml").read_text(encoding="utf-8")
    )

    candidates, files_seen, malformed = read_audit()

    log(f"恢复模式：{mode}")
    log(f"审计文件：{files_seen} 个；无法解析的 JSON 行：{malformed}")
    log(f"去重后候选记录：{len(candidates)} 条")

    if not candidates:
        log("没有找到符合日期范围且 included=true 的审计记录。")
        return

    from filter import model_filter
    from enrich import run as enrich_run
    import llm

    llm.reset_usage()

    # 审计记录中的复核标记属于上一次运行的结果。
    # 清除旧标记，允许本次模型重新判断；本次失败仍会重新标记。
    for item in candidates:
        item.pop("needs_review", None)

    screened, dropped, errors = model_filter(candidates, filters)

    log(
        f"模型筛选：通过 {len(screened)} 条；"
        f"剔除 {count_value(dropped)} 条；"
        f"筛选错误 {count_value(errors)} 条"
    )

    ready = []
    review_count = 0

    for item in screened:
        if item.get("needs_review"):
            review_count += 1
            continue
        if not norm_url(item.get("url")) or not item.get("title"):
            continue
        ready.append(item)

    log(f"本次筛选后待复核、未自动入库：{review_count} 条")
    log(f"进入中文富化：{len(ready)} 条")

    enriched, enrich_failed = enrich_run(ready, model="qwen3")

    log(
        f"中文富化成功：{len(enriched)} 条；"
        f"失败：{len(enrich_failed)} 条"
    )

    valid = []
    for item in enriched:
        if (
            item.get("published_at")
            and item.get("url")
            and item.get("title_zh")
            and item.get("summary_zh")
        ):
            valid.append(item)

    REPORT.parent.mkdir(parents=True, exist_ok=True)

    report = {
        "mode": mode,
        "date_start": START.isoformat(),
        "date_end": END.isoformat(),
        "excluded_dates": sorted(d.isoformat() for d in EXCLUDE),
        "audit_files": files_seen,
        "malformed_lines": malformed,
        "unique_candidates": len(candidates),
        "screened_passed": len(screened),
        "screened_dropped": count_value(dropped),
        "screen_errors": count_value(errors),
        "needs_review_skipped": review_count,
        "enrichment_success": len(enriched),
        "enrichment_failed": len(enrich_failed),
        "valid_for_feishu": len(valid),
        "failed_enrichment_samples": [
            {"title": title, "error": error}
            for title, error in enrich_failed[:100]
        ],
        "screen_error_samples": error_samples(errors),
        "usage": dict(llm.USAGE),
    }

    if mode == "preview":
        report["feishu_write_attempted"] = False
        REPORT.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log("预览模式：未写入飞书，也未修改 articles.json 或网页。")
        log(f"预览报告：{REPORT}")
        log("请检查恢复报告后，再决定是否正式写入。")
        return

    # 正式写入复用项目已有的飞书写入器。
    from sinks import feishu

    written, skipped, write_errors = feishu.write(valid, sources)

    report.update({
        "feishu_write_attempted": True,
        "feishu_written": written,
        "feishu_skipped_duplicates": skipped,
        "feishu_write_errors": write_errors,
    })

    REPORT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log(
        f"飞书恢复完成：新增 {written} 条；"
        f"重复跳过 {skipped} 条；错误 {len(write_errors)} 条"
    )

    for err in write_errors[:20]:
        log(f"飞书写入错误：{err}")

    log(f"恢复报告：{REPORT}")

    if write_errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
