"""
网页 sink。

设计原则：视觉零变化。CSS 和 JS 从你现有产物里【原样抽出】放进 templates/，
这里只负责填数据。高层看到的页面跟今天长得一模一样，
变的是内容——干净了、标题是中文了。

保留的资产（都从旧产物提取）：
  - 三个 tab（今天 / 近3天 / 近1周），包含关系
  - 地图视图 + 国家卡片 + 点击筛选
  - 机构动态排行榜（每个 tab 一份）
  - 数据概览统计条

与旧代码的关键区别：
  旧: generate_html() 里对三个 tab 各自循环调 summarize_text()。
      今天 ⊂ 近3天 ⊂ 近1周，所以同一条新闻被【摘要 2~3 次】。
      实证：181 个 news-item 只有 144 个唯一 URL。
  新: 数据在 enrich 阶段算【一次】，这里三个 tab 从同一份内存数据渲染。
      HTML 字节数还是三份（保持 DOM 结构不变，视觉零风险），
      但 LLM 调用是一次。渲染不花钱，重复调模型才花钱。
"""

import html
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

UTC = timezone.utc
TPL_DIR = Path(__file__).resolve().parent.parent.parent / "templates"

TABS = [("today", "📅 今天", 1), ("last3days", "📆 近3天", 3), ("lastweek", "📋 近1周", 7)]


def _e(s: Any) -> str:
    """HTML 转义。旧代码用 f-string 直接拼，标题带 & 或 < 就破页。"""
    return html.escape(str(s or ""), quote=True)


def _rel(dt: datetime, now: datetime) -> str:
    """从绝对 published_at 动态重算相对时间 —— 移植自你写的 format_relative_time。"""
    d = now - dt
    sec = d.total_seconds()
    if sec < 60:
        return "刚刚"
    if sec < 3600:
        return f"{int(sec // 60)}分钟前"
    if sec < 86400:
        return f"{int(sec // 3600)}小时前"
    days = int(sec // 86400)
    if days == 1:
        return "昨天"
    if days < 7:
        return f"{days}天前"
    if days < 30:
        return f"{days // 7}周前"
    return dt.strftime("%Y-%m-%d")


def _board_of(item: Dict[str, Any], emap: Dict[str, Any], default: str) -> str:
    e = emap.get(item.get("matched_entity") or "")
    if e:
        # group == "IHH Healthcare" 的机构一律归 IHH集团，不按国家细分（已确认的决策）
        return "IHH集团" if e.get("group") == "IHH Healthcare" else e.get("country", default)
    return item.get("board") or item.get("_query_owner") or default


def _news_item(it: Dict[str, Any], now: datetime) -> str:
    dt = datetime.fromisoformat(it["published_at"])
    tag = it.get("matched_entity") or it.get("event_type") or "行业动态"
    sea_cls = "" if it.get("matched_entity") else " sea-tag"
    review = ' <span class="date-tag">🔍待复核</span>' if it.get("needs_review") else ""
    return f"""
            <div class="news-item">
                <div class="news-meta">
                    <span class="entity-tag{sea_cls}">{_e(tag)}</span>
                    <span class="date-tag">{_e(_rel(dt, now))}</span>{review}
                </div>
                <h4 class="news-title"><a href="{_e(it.get('url'))}" target="_blank">{_e(it.get('title_zh') or it.get('title'))}</a></h4>
                <div class="news-summary">{_e(it.get('summary_zh'))}</div>
                <div class="news-meta"><span class="date-tag">{_e(it.get('event_type'))}</span><span class="date-tag">{_e((it.get('title') or '')[:110])}</span></div>
            </div>"""


def _groups(items: List[Dict], boards: List[Dict], emap: Dict, now: datetime) -> str:
    by_board = defaultdict(list)
    for it in items:
        by_board[_board_of(it, emap, "其他")].append(it)

    parts = []
    for b in boards:
        rows = by_board.get(b["name"], [])
        if not rows:
            continue
        by_ent = defaultdict(list)
        for it in rows:
            by_ent[it.get("matched_entity") or "__sea__"].append(it)

        secs = []
        for ent, its in sorted(by_ent.items(), key=lambda x: (x[0] == "__sea__", -len(x[1]))):
            is_sea = ent == "__sea__"
            head = "🌏 行业/卫生动态" if is_sea else f"📍 {_e(ent)}"
            secs.append(f"""
        <div class="entity-section{' sea-health-section' if is_sea else ''}">
            <div class="entity-header">
                <h3>{head} ({len(its)} 条)</h3>
            </div>
{''.join(_news_item(i, now) for i in its)}
        </div>""")

        parts.append(f"""
    <div class="group-section">
        <div class="group-header">
            <h2>{b['flag']} {_e(b['name'])}</h2>
            <span class="count">{len(rows)} 条动态</span>
        </div>
{''.join(secs)}
    </div>""")

    return "".join(parts) or """
    <div class="empty-state"><div class="icon">📭</div><div>该时间段暂无动态</div></div>"""


def _ranking(items: List[Dict], title: str) -> str:
    cnt = Counter(i["matched_entity"] for i in items if i.get("matched_entity"))
    if not cnt:
        return f"""
            <div class="ranking-section">
                <div class="ranking-title">{title}</div>
                <div class="ranking-empty">暂无机构动态</div>
            </div>"""
    top = cnt.most_common(8)
    mx = top[0][1]
    rows = "".join(f"""
                    <div class="ranking-item">
                        <span class="rank">{i}</span>
                        <span class="entity-name">{_e(n)}</span>
                        <div class="bar-container">
                            <div class="bar" style="width: {int(c / mx * 100)}%"></div>
                        </div>
                        <span class="bar-count">{c} 条</span>
                    </div>""" for i, (n, c) in enumerate(top, 1))
    return f"""
            <div class="ranking-section">
                <div class="ranking-title">{title}</div>
                <div class="ranking-list">{rows}
                </div>
            </div>"""


def render(items: List[Dict[str, Any]], sources: Dict[str, Any],
           now: datetime = None) -> str:
    now = now or datetime.now(UTC)
    boards = sources["boards"]
    emap = {e["name"]: e for e in sources["entities"]}
    items = [i for i in items if i.get("published_at")]

    buckets = {k: [i for i in items
                   if datetime.fromisoformat(i["published_at"]) >= now - timedelta(days=d)]
               for k, _, d in TABS}
    for v in buckets.values():
        v.sort(key=lambda i: i["published_at"], reverse=True)

    rank_titles = {"today": "📈 今日机构动态排行", "last3days": "📈 近3天机构动态排行",
                   "lastweek": "📈 近1周机构动态排行"}
    labels = {"today": "今日动态", "last3days": "近3天动态", "lastweek": "近1周动态"}

    # 各 tab 各板块计数（地图用）
    loc_counts = {}
    for k in buckets:
        c = defaultdict(int)
        for it in buckets[k]:
            c[_board_of(it, emap, "其他")] += 1
        loc_counts[k] = {b["name"]: c.get(b["name"], 0) for b in boards}

    css = (TPL_DIR / "style.css").read_text(encoding="utf-8")
    js = (TPL_DIR / "app.js").read_text(encoding="utf-8")

    stats_js = ",\n            ".join(
        f"{k}: {{ label: '{labels[k]}', value: '{len(buckets[k])} 条新闻' }}" for k in buckets)
    import json as _j
    loc_js = ",\n            ".join(f"{k}: {_j.dumps(v, ensure_ascii=False)}" for k, v in loc_counts.items())

    tab_btns = "".join(f"""
        <button class="tab-btn{' active' if i == 0 else ''}" onclick="showTab('{k}', this)">
            {lbl} <span class="count">{len(buckets[k])}</span>
        </button>""" for i, (k, lbl, _) in enumerate(TABS))

    map_tabs = "".join(f"""
            <button class="map-tab{' active' if i == 0 else ''}" onclick="selectMapTime('{k}', this)">
                {lbl} <span class="count">{len(buckets[k])}</span>
            </button>""" for i, (k, lbl, _) in enumerate(TABS))

    cards = "".join(f"""
            <div class="region-card {'has-news' if loc_counts['today'][b['name']] else 'no-news'}" data-location="{_e(b['name'])}" onclick="filterByLocation('{_e(b['name'])}')">
                <div class="region-flag">{b['flag']}</div>
                <div class="region-name">{_e(b['name'])}</div>
                <div class="region-count"><span class="count-num">{loc_counts['today'][b['name']]}</span> 条动态</div>
            </div>""" for b in boards)

    contents = "".join(f"""
    <div id="tab-{k}" class="tab-content{' active' if i == 0 else ''}">
{_groups(buckets[k], boards, emap, now)}
    </div>""" for i, (k, _, _) in enumerate(TABS))

    hidden = "".join(f"""
        <div id="ranking-{k}-data" style="display:none;">{_ranking(buckets[k], rank_titles[k])}
        </div>""" for k in buckets)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>海外医疗集团动态监测</title>
    <style>
{css}
    </style>
</head>
<body>
    <h1>🏥 海外医疗集团动态监测</h1>
    <div class="summary">
        <div class="summary-title">📊 数据概览</div>
        <div class="summary-stats">
            <div class="stat-item">
                <span class="stat-label">更新时间</span>
                <span class="stat-value">{now.astimezone(timezone(timedelta(hours=8))).strftime('%Y年%m月%d日 %H:%M')}</span>
            </div>
            <div class="stat-item">
                <span class="stat-label">监测范围</span>
                <span class="stat-value">{len(sources['entities'])} 家机构</span>
            </div>
            <div class="stat-item" id="stat-active">
                <span class="stat-label" id="stat-label">今日动态</span>
                <span class="stat-value" id="stat-value">{len(buckets['today'])} 条新闻</span>
            </div>
        </div>

        <div id="ranking-container">{_ranking(buckets['today'], rank_titles['today'])}
        </div>
{hidden}
    </div>

    <div class="view-toggle">
        <button class="view-btn active" onclick="switchView('list', this)">📋 列表视图</button>
        <button class="view-btn" onclick="switchView('map', this)">🗺️ 地图视图</button>
    </div>

    <div id="map-view" class="map-container">
        <div class="map-title">🌏 选择国家/地区查看动态</div>
        <div class="map-tabs">{map_tabs}
        </div>
        <div class="region-grid" id="region-grid">{cards}
        </div>
    </div>

    <div id="country-filter" class="country-filter">
        <span id="filter-name"></span>
        <button onclick="clearFilter()">✕ 清除筛选</button>
    </div>

    <div class="tabs">{tab_btns}
    </div>
{contents}
    <script>
        const statsData = {{
            {stats_js}
        }};
        const locationCounts = {{
            {loc_js}
        }};
{js}
    </script>
</body>
</html>"""


def write(items: List[Dict[str, Any]], sources: Dict[str, Any], out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    h = render(items, sources)
    out_path.write_text(h, encoding="utf-8")
    return len(h)
