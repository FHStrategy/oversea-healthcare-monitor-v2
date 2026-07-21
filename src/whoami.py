"""
--whoami: 用手机号/邮箱换 open_id，并立刻发一条测试消息验证告警链路。

一步验证三件事：
  1. contact:user.id:readonly 权限通 -> 能换到 open_id
  2. im:message:send_as_bot 权限通 -> 能发消息
  3. open_id 正确 -> 你手机真能收到

拿到 open_id 后填进 GitHub Secret: ALERT_OPEN_ID，run.py 的失败告警就用它。
"""
import argparse
import json
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from sinks import feishu   # noqa: E402

BASE = "https://open.feishu.cn/open-apis"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mobile", default="", help="手机号，如 +8613800138000 或 13800138000")
    ap.add_argument("--email", default="", help="邮箱")
    args = ap.parse_args()

    if not args.mobile and not args.email:
        sys.exit("❌ 至少提供 --mobile 或 --email 之一")

    token = feishu.get_token()
    print("✅ tenant_access_token 换取成功")

    # 1. 换 open_id
    body = {"mobiles": [], "emails": []}
    if args.mobile:
        m = args.mobile if args.mobile.startswith("+") else "+86" + args.mobile
        body["mobiles"] = [m]
    if args.email:
        body["emails"] = [args.email]

    r = requests.post(
        f"{BASE}/contact/v3/users/batch_get_id?user_id_type=open_id",
        headers={"Authorization": f"Bearer {token}"},
        json=body, timeout=15,
    )
    d = r.json()
    if d.get("code") != 0:
        msg = d.get("msg", "")
        hint = ""
        if "permission" in str(msg).lower() or d.get("code") == 99991672:
            hint = "\n   → 缺 contact:user.id:readonly 权限，或加了权限后没重新发版"
        sys.exit(f"❌ 换 open_id 失败: code={d.get('code')} msg={msg}{hint}")

    users = d["data"]["user_list"]
    found = [u for u in users if u.get("user_id")]
    if not found:
        sys.exit(f"❌ 没查到用户 —— 手机号/邮箱可能不在通讯录里\n   返回: {json.dumps(users, ensure_ascii=False)}")

    open_id = found[0]["user_id"]
    print(f"\n{'='*50}")
    print(f"✅ 你的 open_id: {open_id}")
    print(f"{'='*50}")
    print(f"→ 把它填进 GitHub Secret: ALERT_OPEN_ID")

    # 2. 立刻发测试消息
    print("\n发送测试消息…")
    content = json.dumps({"text":
        "🔔 海外医疗监测系统 · 告警链路测试\n\n"
        "如果你收到这条消息，说明失败告警已配置成功。\n"
        "以后流水线跑挂了，你会在这里收到通知。"}, ensure_ascii=False)
    r = requests.post(
        f"{BASE}/im/v1/messages?receive_id_type=open_id",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"receive_id": open_id, "msg_type": "text", "content": content},
        timeout=15,
    )
    d = r.json()
    if d.get("code") != 0:
        msg = d.get("msg", "")
        hint = ""
        if "permission" in str(msg).lower():
            hint = "\n   → 缺 im:message:send_as_bot 权限，或没发版"
        if d.get("code") == 230002:
            hint = "\n   → 你还没和这个机器人建立会话。先在飞书里搜到这个应用/机器人并发它一句话，再重试"
        sys.exit(f"❌ 发消息失败: code={d.get('code')} msg={msg}{hint}")

    print("✅ 测试消息已发送 —— 检查你的飞书，应该收到了一条通知")
    print("\n收到 = 告警链路完全打通。把 open_id 填进 Secret 就行。")


if __name__ == "__main__":
    main()
