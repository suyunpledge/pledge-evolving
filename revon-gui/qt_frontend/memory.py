"""长期记忆（分级检索插件）在 Qt 对话里的入口，与 Tk 版共用 ``memory_retrieval`` 的桥。

插件未安装或未启用时返回 None，不发任何网络请求；端口变化时重建桥，
保证「服务挂了」的提示只出现一次（桥内部记着）。
"""
from __future__ import annotations

import threading

_LOCK = threading.Lock()
_BRIDGES: dict[str, object] = {}


def bridge_for(home):
    try:
        import memory_retrieval
        port = memory_retrieval.plugin_port(home)
    except Exception:
        return None
    key = str(home)
    with _LOCK:
        if port is None:
            _BRIDGES.pop(key, None)
            return None
        bridge = _BRIDGES.get(key)
        if bridge is None or bridge.client.base_url != f'http://127.0.0.1:{port}':
            try:
                bridge = memory_retrieval.memory_bridge_for(home)
            except Exception:
                return None
            _BRIDGES[key] = bridge
        return bridge
