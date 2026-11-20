"""会话内的原生工具声明顺序：首次排序，新增追加，撤销立即移除。"""
from __future__ import annotations

from .toolwire import tool_declarations


class ToolSurface:
    def __init__(self):
        self._orders = {}

    def fork(self):
        child = ToolSurface()
        child._orders = dict(self._orders)  # tuple 值不可变；子会话后续更新独立。
        return child

    def project(self, specs, wire):
        current = {spec.name: spec for spec in specs}
        order = [name for name in self._orders.get(wire, ()) if name in current]
        known = set(order)
        order.extend(sorted(name for name in current if name not in known))
        self._orders[wire] = tuple(order)
        # 每次用当前定义重建，绝不为了命中保留旧 schema 或已撤销的工具。
        return tool_declarations([current[name] for name in order], wire, sort=False)
