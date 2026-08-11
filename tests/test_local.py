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

# 出口节点抖动容忍：_handle_transport_error 应该先原地重试 SAME_NODE_RETRIES 次
# （不调用 rotate_node），仍失败才真的换节点。
import time as time_mod

rotate_calls = []


def fake_rotate() -> str:
    rotate_calls.append(1)
    return "备用节点"


client_for_rotate_test = client_mod.ZLibraryClient(
    site="https://zh.z-library.sk", proxy_url=None, user_agent="test",
    rotate_node=fake_rotate,
)
_orig_sleep = time_mod.sleep
time_mod.sleep = lambda *_a, **_k: None  # 测试不真的等待
try:
    same_node_retries = client_mod.SAME_NODE_RETRIES
    for i in range(1, same_node_retries + 1):
        ok = client_for_rotate_test._handle_transport_error(i, "TestError")
        assert ok, f"第 {i} 次应返回 True（原地重试）"
        assert not rotate_calls, f"第 {i} 次不应真的调用 rotate_node"
    ok = client_for_rotate_test._handle_transport_error(same_node_retries + 1, "TestError")
    assert ok, "达到阈值那次应触发换节点并返回 True"
    assert rotate_calls == [1], f"应恰好调用 rotate_node 一次，实际 {len(rotate_calls)} 次"
finally:
    time_mod.sleep = _orig_sleep
print(f"抖动容忍验证通过: 前 {same_node_retries} 次原地重试未换节点，第 {same_node_retries + 1} 次才真的换节点")

print("\nALL LOCAL CHECKS PASSED")
