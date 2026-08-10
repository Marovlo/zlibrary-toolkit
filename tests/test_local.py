"""本地验证脚本：检查 import、配置、订阅解析、账号逻辑。"""
import sys
sys.path.insert(0, "src")

import zlib
import zlibrary.config as config_mod
import zlibrary.subscription as sub_mod
import zlibrary.proxy_manager as pm_mod
import zlibrary.client as client_mod
import zlibrary.accounts as acc_mod
import zlibrary.site_checker as sc_mod
import zlibrary.site_finder as sf_mod
import zlibrary.cli as cli_mod

print("ALL IMPORTS OK")

cfg = config_mod.Config.load()
print(f"site: {cfg.default_site}")
print(f"download_dir: {cfg.download_dir_abs()}")
print(f"mihomo binary: {cfg.mihomo.binary_abs()}")
print(f"proxy url: http://127.0.0.1:{cfg.mihomo.http_port}")

# 订阅解析（用mock）
root = config_mod.project_root()
mock_sub = root / "tests" / "fixtures" / "mock_sub.yaml"
nodes = sub_mod.parse_subscription(mock_sub)
print(f"parsed nodes: {len(nodes)}")
for n in nodes:
    print(f"  - {n.name} ({n.type})")

# 账号逻辑
store = acc_mod.AccountStore.load(root / "accounts.yaml")
print(f"accounts loaded: {len(store.accounts)}")
for a in store.accounts:
    a.maybe_reset()
    print(f"  - {a.email}: downloads_today={a.downloads_today} available={a.available(store.limit)}")

# BookResult 匹配逻辑
from zlibrary.client import BookResult
b1 = BookResult("三体", "刘慈欣", "2008", "zh", "epub", "5MB", 4.5, "1", "abc", "/book/1/abc")
print(f"match '三体' -> {b1.match_score('三体')} (期望 100)")
print(f"match '三体三部曲' -> {b1.match_score('三体三部曲')} (期望 50)")
print(f"match '银河帝国' -> {b1.match_score('银河帝国')} (期望 0)")

# 生成 mihomo 配置（用 mock）
import yaml
from pathlib import Path
pm = pm_mod.ProxyManager(cfg)
pm.sub_path = mock_sub
pm.nodes = sub_mod.parse_subscription(pm.sub_path)
cfg_path = pm.generate_config()
print(f"mihomo config generated: {cfg_path}")
data = yaml.safe_load(open(cfg_path))
print(f"  nodes in config: {len(data['proxies'])}")
print(f"  selector group: {data['proxy-groups'][0]['name']}")
print(f"  rules: {data['rules']}")

print("\nALL LOCAL CHECKS PASSED")
