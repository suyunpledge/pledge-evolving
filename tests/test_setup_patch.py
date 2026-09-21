# -*- coding: utf-8 -*-
"""
测试目标：forge setup 生成的用户层补丁（NOT 网络行为）
样本来源：真实 bug——早期版本写入 tiers=[["lite",模型],["medium",模型]]，
          但只覆盖 medium 的 provider 行，lite 仍指向 base.json 的环回占位
          网关（127.0.0.1:8810），导致 economy 策略打空。
期望行为：
  - tiers 只包含用户真正配置的档位，不塞未配置的假档
  - 整行显式：apply_patch 是整行替换，未列出的键会被丢掉，所以要写全
  - primary 指向被配置的 provider 行
边界说明：只验证补丁结构，不发起网络请求，不写真实用户层。
"""

import sys

sys.path.insert(0, r"C:\Users\匡溯昀\pledge-evolving")

from forge.cmd_setup import PROVIDERS, _build_patch

deepseek = next(p for p in PROVIDERS if p["id"] == "deepseek")
patch = _build_patch(deepseek, "sk-test")

rows = {r["id"]: r for r in patch}

# provider 行绑到 medium（唯一被覆盖的那一行）
assert "medium" in rows, list(rows)
assert rows["medium"]["config"]["baseURL"] == "https://api.deepseek.com"
assert rows["medium"]["config"]["apiKey"] == "sk-test"

# model 行
model = rows["model"]["config"]
routing = model["routing"]

# 关键回归：不得出现未配置的档位
tiers_ids = [pair[0] for pair in routing["tiers"]]
assert "lite" not in tiers_ids, (
    f"setup 不得写入未配置的 lite 档（其 provider 仍指向占位网关）: {routing['tiers']}"
)
assert tiers_ids == ["medium"], routing["tiers"]

# 所有 tier 的 provider 都必须有对应的 provider 行
for pid, _model in routing["tiers"]:
    assert pid in rows, f"tier {pid!r} 没有对应的 provider 行 -> 会路由到不存在的 provider"

# primary 指向被配置的行
assert model["primary"][0] == "medium", model["primary"]
assert model["primary"][1] == "deepseek-flash"

# 整行显式：这些键必须存在，否则整行替换会把 base 的值丢掉而不自知
for key in ("primary", "fallback", "moa", "moaModels", "routing"):
    assert key in model, f"整行替换语义要求显式写出 {key!r}: {sorted(model)}"
assert "strategy" in routing and "small" in routing, sorted(routing)

# fallback 为空是刻意的：单 provider 用户没有可用的兜底档
assert model["fallback"] == [], model["fallback"]

# 自定义 provider：URL/模型名走用户输入
custom = next(p for p in PROVIDERS if p["id"] == "custom")
p2 = {r["id"]: r for r in _build_patch(custom, "k2", "http://localhost:9000/v1", "my-model")}
assert p2["medium"]["config"]["baseURL"] == "http://localhost:9000/v1"
assert p2["medium"]["config"]["model"] == "my-model"
assert p2["model"]["config"]["routing"]["tiers"] == [["medium", "my-model"]]
assert p2["model"]["config"]["primary"] == ["medium", "my-model"]

print("setup patch structure: OK")
print("  tiers =", routing["tiers"])
print("  primary =", model["primary"])
print("  explicit keys =", sorted(model))

print("\nAll setup-patch checks passed")
