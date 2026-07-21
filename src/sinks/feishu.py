"""
飞书多维表格 sink —— 全量底稿，append-only。

定位（已确认的设计）：
  飞书表 = 全量底稿，累积不删，与网页【完全一致】的字段和筛选
  网页    = 7 天视图
  两者同源，高层看哪边都一样。
  审计明细（入库判定/被过滤条目）不在这里，落 data/audit/*.jsonl。

两个字段类型地雷（2026-07-15 实测拆出来的）：
  「时间」 type=5  日期     -> 必须传【毫秒时间戳 int】。传字符串会 DatetimeFieldConvFail
  「链接」 type=15 超链接   -> 必须传 {"text":..., "link":...}，传字符串会报错
  「事件类型」type=3 单选   -> 传选项名字符串即可，但必须与选项【逐字一致】

这两个坑都是在写连通性测试时撞上的 —— 当时我用 fields[0] 写测试记录，
正好撞上日期字段。要不是那次，就是上线当天撞。
"""

import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

BASE = "https://open.feishu.cn/open-apis"


class FeishuError(Exception):
    pass


def _req(method: str, url: str, token: str, **kw) -> Dict[str, Any]:
    for i in range(1, 4):
        r = requests.request(method, url, headers={"Authorization": f"Bearer {token}"},
                             timeout=30, **kw)
        try:
            d = r.json()
        except Exception:
            raise FeishuError(f"响应不是 JSON: HTTP {r.status_code} {r.text[:200]}")
        if d.get("code") == 0:
            return d
        # 频控退避
        if d.get("code") in (99991400, 1254291):
            time.sleep(2 * i)
            continue
        raise FeishuError(f"code={d.get('code')} msg={d.get('msg')}")
    raise FeishuError("重试 3 次仍失败（疑似频控）")


def get_token() -> str:
    app_id = os.environ.get("FEISHU_APP_ID")
    secret = os.environ.get("FEISHU_APP_SECRET")
    if not app_id or not secret:
        raise FeishuError("FEISHU_APP_ID / FEISHU_APP_SECRET 缺失")
    r = requests.post(f"{BASE}/auth/v3/tenant_access_token/internal",
                      json={"app_id": app_id, "app_secret": secret}, timeout=15)
    d = r.json()
    if d.get("code") != 0:
        msg = str(d.get("msg", ""))
        hint = " —— 疑似 IP 白名单挡住了 Actions" if "ip" in msg.lower() else ""
        raise FeishuError(f"换 token 失败: code={d.get('code')} msg={msg}{hint}")
    return d["tenant_access_token"]


class Base:
    def __init__(self, token: str, app_token: str, table_id: str):
        self.t, self.app, self.tbl = token, app_token, table_id
        self.url = f"{BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records"

    def fields(self) -> Dict[str, int]:
        d = _req("GET", f"{BASE}/bitable/v1/apps/{self.app}/tables/{self.tbl}/fields", self.t)
        return {f["field_name"]: f["type"] for f in d["data"]["items"]}

    def existing_urls(self, link_field: str = "链接") -> Set[str]:
        """
        读全表已有链接，用于 append 去重。
        飞书是全量底稿、每天追加，不读一遍的话重跑就堆重复。
        """
        urls, token, n = set(), None, 0
        while True:
            params = {"page_size": 500}
            if token:
                params["page_token"] = token
            d = _req("GET", self.url, self.t, params=params)
            data = d.get("data", {})
            for rec in data.get("items", []) or []:
                v = (rec.get("fields") or {}).get(link_field)
                if isinstance(v, dict):
                    u = v.get("link")
                elif isinstance(v, str):
                    u = v
                else:
                    u = None
                if u:
                    urls.add(u.rstrip("/"))
            n += len(data.get("items") or [])
            token = data.get("page_token")
            if not data.get("has_more"):
                break
        return urls

    def batch_create(self, records: List[Dict[str, Any]]) -> Tuple[int, List[str]]:
        """
        批量写。失败时【打印完整错误】再回退单条 ——
        不静默重试。备忘里那个「批量失败、单条成功」的老 bug
        一直没定位到根因，就是因为当时静默回退了。这次要能看见。
        """
        ok, errors = 0, []
        for i in range(0, len(records), 100):
            chunk = records[i:i + 100]
            try:
                _req("POST", f"{self.url}/batch_create", self.t,
                     json={"records": chunk})
                ok += len(chunk)
                continue
            except FeishuError as e:
                errors.append(f"批量写入失败(第{i//100+1}批, {len(chunk)}条): {e} -> 回退单条")

            for rec in chunk:
                try:
                    _req("POST", self.url, self.t, json=rec)
                    ok += 1
                except FeishuError as e:
                    title = (rec.get("fields") or {}).get("中文标题", "?")
                    errors.append(f"单条失败: {e} | {str(title)[:40]}")
        return ok, errors


# ---------------------------------------------------------------------------
# 字段组装
# ---------------------------------------------------------------------------

def _board_of(item: Dict[str, Any], emap: Dict[str, Any]) -> str:
    e = emap.get(item.get("matched_entity") or "")
    if e:
        return "IHH集团" if e.get("group") == "IHH Healthcare" else e.get("country", "")
    return item.get("board") or "行业动态"


def to_record(item: Dict[str, Any], emap: Dict[str, Any]) -> Dict[str, Any]:
    e = emap.get(item.get("matched_entity") or "")
    dt = datetime.fromisoformat(item["published_at"])
    title = item.get("title") or ""
    return {"fields": {
        # type=5 日期：毫秒时间戳 int。传字符串 -> DatetimeFieldConvFail
        "时间": int(dt.timestamp() * 1000),
        "国家/地区": _board_of(item, emap),
        "机构": item.get("matched_entity") or "",     # 行业动态留空（已确认）
        "集团": (e.get("group") or "") if e else "",
        "事件类型": item.get("event_type") or "行业新闻",   # type=3 单选
        "中文标题": item.get("title_zh") or "",
        "原标题": title,
        "摘要（中文）": item.get("summary_zh") or "",
        # type=15 超链接：必须是对象
        "链接": {"text": title[:60] or item.get("url", ""), "link": item.get("url", "")},
    }}


def write(items: List[Dict[str, Any]], sources: Dict[str, Any]
          ) -> Tuple[int, int, List[str]]:
    """
    返回 (新增条数, 跳过的重复条数, 错误列表)。
    错误不为空时上游应该红灯 —— 不把「写失败」伪装成「今天没新闻」。
    """
    app_token = os.environ.get("FEISHU_BASE_TOKEN")
    table_id = os.environ.get("FEISHU_TABLE_ID")
    if not app_token or not table_id:
        raise FeishuError("FEISHU_BASE_TOKEN / FEISHU_TABLE_ID 缺失")

    b = Base(get_token(), app_token, table_id)

    # 字段名核对 —— 表结构改了要立刻知道，不要写一半才报错
    have = b.fields()
    need = ["时间", "国家/地区", "机构", "集团", "事件类型",
            "中文标题", "原标题", "摘要（中文）", "链接"]
    missing = [f for f in need if f not in have]
    if missing:
        raise FeishuError(f"飞书表缺字段: {missing} | 现有: {list(have)}")

    emap = {e["name"]: e for e in sources["entities"]}
    seen = b.existing_urls()
    new = [i for i in items
           if i.get("published_at") and (i.get("url") or "").rstrip("/") not in seen]
    skipped = len(items) - len(new)
    if not new:
        return 0, skipped, []

    ok, errors = b.batch_create([to_record(i, emap) for i in new])
    return ok, skipped, errors


# ---------------------------------------------------------------------------
# 失败告警
# ---------------------------------------------------------------------------

def send_alert(text: str) -> bool:
    """
    发飞书告警给 ALERT_OPEN_ID。发送本身失败不抛异常（避免"告警失败"
    掩盖"原始失败"），只返回 bool 并打印。飞书机器人主动发消息不需要
    用户先建会话 —— 2026-07 whoami 验证过。
    """
    import json
    open_id = os.environ.get("ALERT_OPEN_ID")
    if not open_id:
        print("⚠️ 未配置 ALERT_OPEN_ID，跳过飞书告警")
        return False
    try:
        token = get_token()
        r = requests.post(
            f"{BASE}/im/v1/messages?receive_id_type=open_id",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"receive_id": open_id, "msg_type": "text",
                  "content": json.dumps({"text": text}, ensure_ascii=False)},
            timeout=15,
        )
        ok = r.json().get("code") == 0
        print("✅ 告警已发送" if ok else f"⚠️ 告警发送失败: {r.text[:200]}")
        return ok
    except Exception as e:
        print(f"⚠️ 告警发送异常: {e}")
        return False
