"""
仓库自检:导入所有模块、校验关键签名、校验配置完整性。
放 src/selfcheck.py,单独跑一个 workflow,或本地 python src/selfcheck.py。
不调任何外部 API,几秒出结果。一次性暴露文件版本不同步问题。
"""
import sys, importlib, inspect
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

ROOT = Path(__file__).resolve().parent.parent
ok = True

def check(name, cond, detail=""):
    global ok
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail and not cond else ""))
    if not cond: ok = False

print("=" * 56); print("1. 模块导入"); print("=" * 56)
mods = ["firecrawl_client","normalize","llm","filter","enrich",
        "pipeline","probe_fetch","probe_pipeline","probe_feishu"]
loaded = {}
for m in mods:
    try:
        loaded[m] = importlib.import_module(m)
        check(m, True)
    except Exception as e:
        check(m, False, f"{type(e).__name__}: {e}")

try:
    from sinks import feishu, site
    check("sinks.feishu / sinks.site", True)
except Exception as e:
    check("sinks", False, f"{type(e).__name__}: {e}")

print("=" * 56); print("2. 关键函数签名(版本同步检查)"); print("=" * 56)
if "normalize" in loaded:
    sig = str(inspect.signature(loaded["normalize"].AliasIndex.build))
    check("AliasIndex.build 支持 core_keywords(弱别名)",
          "core_keywords" in sig, f"实际: {sig}")

print("=" * 56); print("3. 配置完整性"); print("=" * 56)
import yaml
S = yaml.safe_load((ROOT/"config"/"sources.yaml").read_text(encoding="utf-8"))
F = yaml.safe_load((ROOT/"config"/"filters.yaml").read_text(encoding="utf-8"))
check("sources: 26 实体", len(S["entities"])==26, f"实际 {len(S['entities'])}")
check("sources: boards 板块", "boards" in S and len(S["boards"])==10)
check("sources: 有弱别名(Fortis/Apollo/Pantai)",
      any(e.get("weak_aliases") for e in S["entities"]))
check("filters: 11 事件类型", len(F["event_types"])==11)
check("filters: 域名黑名单", len(F.get("blocked_domains",[]))>0)
check("filters: 行业动态通道", F.get("industry_channel",{}).get("enabled"))
check("filters: 地区门槛", "scope_regions" in F.get("industry_channel",{}))

print("=" * 56); print("4. 模板"); print("=" * 56)
for f in ["style.css","app.js"]:
    p = ROOT/"templates"/f
    check(f"templates/{f}", p.exists() and p.stat().st_size>0)

print()
print("=" * 56)
print("✅ 全部通过 —— 文件版本同步,可以跑真实流水线" if ok else "❌ 有问题 —— 见上面 ❌ 项,补齐对应文件")
print("=" * 56)
sys.exit(0 if ok else 1)
