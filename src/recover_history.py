"""
一次性历史新闻恢复。
只允许修改本文件；不抓取新新闻，不修改 daily 共用模块。
先 preview，确认内容质量后再 write。
"""

import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from collections import Counter

import yaml

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import llm
from filter import model_filter

START = date(2026, 9, 16)
END = date(2026, 10, 12)
EXCLUDE = {date(2026, 10, 11)}
REPORT_PATH = ROOT / "data" / "recovery-history-report.json"

SYSTEM_PROMPT = "你是医疗行业情报翻译。只输出 JSON，不要解释，不要 markdown 代码块。"

ENRICH_PROMPT = """把这条医疗行业新闻处理成中文情报条目。

原标题：{title}
原摘要：{snippet}
涉及机构：{entity}

要求：
- title_zh：标题的中文翻译。保留机构名的通用中文译名；
  没有通用译名的机构名保留英文原文。不要加书名号，不要意译发挥，不超过40字。
- summary_zh：2句以内的中文摘要，说清谁、做了什么、有什么影响。
  只依据原摘要，不要脑补原文没有的信息。不超过80字。

只输出 JSON 对象：
{{"title_zh":"...","summary_zh":"..."}}"""


def log(message):
    print(message, flush=True)


def parse_day(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def norm_url(value):
    return (value or "").strip().rstrip("/")


def has_chinese(value):
    return any("\u4e00" <= ch <= "\u9fff" for ch in (value or ""))


def extract_json(raw):
    """只在本恢复脚本中解析模型响应，不修改 llm.py。"""
    text = (raw or "").strip()

    # 删除成对的 think 区块；如果有未闭合 think，保留后续可用 JSON 的机会。
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.I | re.S).strip()

    # 优先读取 Markdown JSON 代码块。
    block = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.I | re.S)
    if block:
        text = block.group(1).strip()

    # 从每个左花括号尝试解码一个完整 JSON 对象。
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value

    raise ValueError("模型响应中未找到合法 JSON 对象")


def enrich_one(item):
    title = str(item.get("title") or "").strip()
    snippet = str(item.get("snippet") or "").strip()
    entity = str(
        item.get("matched_entity")
        or item.get("entity")
        or "（行业动态，无特定机构）"
    ).strip()

    if not title:
        raise ValueError("原始标题为空")
    if not snippet:
        raise ValueError("原始摘要为空，无法可靠生成中文摘要")

    prompt = ENRICH_PROMPT.format(
        title=title,
        snippet=snippet[:600],
        entity=entity,
    )

    # 使用与现有项目相同的 LLM 客户端，但不调用共用 chat_json。
    raw = llm.chat(
        prompt,
        system=SYSTEM_PROMPT,
        model="qwen3",
        timeout=60,
    )
    data = extract_json(raw)

    title_zh = str(data.get("title_zh") or "").strip()
    summary_zh = str(data.get("summary_zh") or "").strip()

    if not title_zh or not summary_zh:
        raise ValueError("模型结果缺少 title_zh 或 summary_zh")
    if not has_chinese(title_zh) or not has_chinese(summary_zh):
        raise ValueError("中文标题或摘要未通过中文字符检查")
    if len(title_zh) > 80 or len(summary_zh) > 200:
        raise ValueError("中文标题或摘要长度异常")
    if "<think>" in title_zh.lower() or "<think>" in summary_zh.lower():
        raise ValueError("中文结果中包含模型推理标签")
    if title_zh.startswith("{") or summary_zh.startswith("{"):
        raise ValueError("中文结果疑似包含原始 JSON")

    result = dict(item)
    result["title_zh"] = title_zh
    result["summary_zh"] = summary_zh
    result.pop("needs_review", None)
    return result


def load_candidates():
    audit_dir = ROOT / "data" / "audit"
    files = []
    for path in sorted(audit_dir.glob("*.jsonl")):
        day = parse_day(path.stem)
        if day and START <= day <= END and day not in EXCLUDE:
            files.append(path)

    by_url = {}
    malformed = 0
    for path in files:
        audit_day = parse_day(path.stem)
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue

                if not row.get("included"):
                    continue

                # 审计结构通常把原始新闻放在 item 中；兼容字段直接位于记录顶层的情况。
                item = row.get("item")
                if not isinstance(item, dict):
                    item = row.get("article")
                if not isinstance(item, dict):
                    item = row

                record = dict(item)
                for key in (
                    "url", "url_normalized", "title", "snippet",
                    "published_at", "matched_entity", "matched_alias",
                    "group", "event_type", "country",
                ):
                    if not record.get(key) and row.get(key):
                        record[key] = row[key]

                url = norm_url(record.get("url_normalized") or record.get("url"))
                title = str(record.get("title") or "").strip()
                if not url or not title:
                    continue

                record["url"] = url
                record["_audit_date"] = str(audit_day)

                # 同 URL 选择审计日期较新的记录。
                previous = by_url.get(url)
                if previous is None or record["_audit_date"] >= previous["_audit_date"]:
                    by_url[url] = record

    return files, malformed, list(by_url.values())


def main():
    mode = os.environ.get("RECOVERY_MODE", "preview").strip().lower()
    if mode not in {"preview", "write"}:
        raise ValueError("RECOVERY_MODE 只能是 preview 或 write")

    log(f"恢复模式：{mode}")
    files, malformed, candidates = load_candidates()
    log(f"审计文件：{len(files)} 个；无法解析的 JSON 行：{malformed}")
    log(f"去重后候选记录：{len(candidates)} 条")

    filters_path = ROOT / "config" / "filters.yaml"
    sources_path = ROOT / "config" / "sources.yaml"
    filters = yaml.safe_load(filters_path.read_text(encoding="utf-8")) or {}
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8")) or {}

    # 清理旧审计遗留的 needs_review 标记，避免把历史失败状态误当成新筛选结果。
    for item in candidates:
        item.pop("needs_review", None)

    screened, dropped, screen_errors = model_filter(candidates, filters)

    def count_value(value):
        if isinstance(value, int):
            return value
        try:
            return len(value)
        except TypeError:
            return 0

    screen_error_count = count_value(screen_errors)
    log(
        f"模型筛选：通过 {count_value(screened)} 条；"
        f"剔除 {count_value(dropped)} 条；筛选错误 {screen_error_count} 条"
    )

    review_items = [item for item in screened if item.get("needs_review")]
    ready = [item for item in screened if not item.get("needs_review")]
    log(f"筛选后待复核、未自动入库：{len(review_items)} 条")
    log(f"进入中文富化：{len(ready)} 条")

    enriched = []
    enrichment_failures = []

    # 独立于共用 enrich.py；逐条调用，错误不会影响其他记录。
    for index, item in enumerate(ready, start=1):
        try:
            enriched.append(enrich_one(item))
        except Exception as exc:
            enrichment_failures.append({
                "title": str(item.get("title") or "(未知)")[:160],
                "url": item.get("url", ""),
                "error": f"{type(exc).__name__}: {exc}"[:500],
            })
        if index % 25 == 0 or index == len(ready):
            log(f"富化进度：{index}/{len(ready)}")

    # 日期缺失的记录不能进入飞书日期字段。
    valid = [item for item in enriched if item.get("published_at")]
    missing_date = len(enriched) - len(valid)

    report = {
        "mode": mode,
        "date_start": START.isoformat(),
        "date_end": END.isoformat(),
        "excluded_dates": sorted(d.isoformat() for d in EXCLUDE),
        "audit_files": len(files),
        "malformed_lines": malformed,
        "unique_candidates": len(candidates),
        "screened_passed": count_value(screened),
        "screened_dropped": count_value(dropped),
        "screen_errors": screen_error_count,
        "needs_review_skipped": len(review_items),
        "enrichment_success": len(enriched),
        "enrichment_failed": len(enrichment_failures),
        "missing_published_at": missing_date,
        "valid_for_feishu": len(valid),
        "failed_enrichment_samples": enrichment_failures[:30],
        "llm_usage": dict(llm.USAGE),
    }

    # 先保存报告，即使后续写入失败也保留诊断信息。
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log(f"中文富化成功：{len(enriched)} 条；失败：{len(enrichment_failures)} 条")
    log(f"无发布时间剔除：{missing_date} 条")
    log(f"可写入飞书：{len(valid)} 条")
    log(f"报告已保存：{REPORT_PATH}")

    if mode == "preview":
        log("Preview 模式：未写入飞书。")
        return

    # 正式写入前采取保守策略：只要有筛选错误或富化失败，就停止写入。
    if screen_error_count or enrichment_failures:
        raise RuntimeError(
            "存在筛选错误或富化失败；为避免不完整恢复，本次未写入飞书。请先检查报告。"
        )

    from sinks import feishu
    ok, skipped, errors = feishu.write(valid, sources)
    report["feishu_added"] = ok
    report["feishu_skipped_duplicates"] = skipped
    report["feishu_errors"] = errors
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log(f"飞书写入：新增 {ok} 条；跳过重复 {skipped} 条；失败 {len(errors)} 条")
    if errors:
        raise RuntimeError(f"飞书写入有 {len(errors)} 条失败")


if __name__ == "__main__":
    main()
