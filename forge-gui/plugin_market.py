"""plugin_market.py — Forge 的插件市场与工具市场（零第三方依赖）。

设计取自 DSH 的插件机制，做了 Python 原生改写：

  DSH                                →  Forge
  ─────────────────────────────────────────────────────────────
  package.json dependencies          →  插件目录 + forge-plugin.json 清单
  dsh.profile.bundles（层叠激活）     →  state.json 的 enabled 列表
  cordis.patch.yml（用户层覆盖）      →  state.json 的 overrides
  pnpm add <spec>                    →  install_from_dir / install_from_catalog
  .dsh-market/state.json             →  ~/.forge/marketplace/state.json
  .dsh-market/log.ndjson             →  ~/.forge/marketplace/log.ndjson
  region 自动检测                     →  region（默认 china，离线优先）

与 DSH 的关键差异（安全口径）：

  DSH 的插件是能跑任意 Node 代码的包；Forge 这里**默认只做声明式注册**。
  清单里 `executes_code=true` 的插件必须由用户显式确认（记录 ack 指纹）后才算
  可加载——和工具桥 `--profile aggressive` 要显式 ack 是同一套思路。

目录布局：

  ~/.forge/marketplace/
      state.json          启停状态、已安装索引、市场源、region
      log.ndjson          事件流（一行一个 JSON，便于追加与排障）
      cache/<source-hash>.json   远程目录缓存（离线可用）
  ~/.forge/plugins/<id>/    插件本体（含 forge-plugin.json）

内置目录随程序分发（`catalog.builtin.json`），因此**完全离线可用**——本机按流量
计费，默认不联网拉目录；远程源属于用户显式添加后的行为。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# ── 常量 ─────────────────────────────────────────────────────────────

MANIFEST_NAME = "forge-plugin.json"
MARKET_DIRNAME = "marketplace"
PLUGINS_DIRNAME = "plugins"
STATE_NAME = "state.json"
LOG_NAME = "log.ndjson"
CACHE_DIRNAME = "cache"
TRASH_DIRNAME = "trash"

KINDS = ("tool", "theme", "panel", "integration")

#: 清单里允许出现的字段（未知字段一律忽略，不做隐式行为）
MANIFEST_FIELDS = (
    "id", "name", "version", "kind", "summary", "description", "author",
    "homepage", "icon", "provides", "permissions", "executes_code",
    "requires", "tags", "source",
)

#: 权限名 → 人话说明（UI 直接展示，避免用户看到裸标识符）
PERMISSION_LABELS = {
    "workspace:read": "读取工作区文件",
    "workspace:write": "改写工作区文件",
    "shell:exec": "执行本机命令",
    "net:outbound": "发起网络请求",
    "ui:theme": "更换界面主题",
    "ui:panel": "添加界面面板",
    "model:call": "调用模型接口",
    "clipboard": "读写剪贴板",
}

KIND_LABELS = {
    "tool": "工具",
    "theme": "主题",
    "panel": "面板",
    "integration": "集成",
}


def _runtime_base() -> Path:
    """资源基准目录。

    源码运行 = 本文件所在目录；PyInstaller 单文件打包后 = sys._MEIPASS
    （内置目录随 datas 一起解包到那里）。拿错的话 exe 里市场会是空的。
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base)
    return Path(__file__).resolve().parent


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _fingerprint(payload: Any) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ── 数据模型 ─────────────────────────────────────────────────────────


@dataclass
class Plugin:
    """一条市场条目 / 已安装插件。

    `installed` 与 `enabled` 是两件独立的事：装到 ~/.forge/plugins 之后默认
    不激活，要用户显式启用才进 enabled 列表（对应 DSH 的 bundles 层叠）。
    """

    id: str
    name: str
    version: str = "0.0.0"
    kind: str = "tool"
    summary: str = ""
    description: str = ""
    author: str = ""
    homepage: str = ""
    icon: str = "🧩"
    provides: dict[str, Any] = field(default_factory=dict)
    permissions: list[str] = field(default_factory=list)
    executes_code: bool = False
    requires: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    source: str = "builtin"

    # 运行期状态（不写进清单）
    installed: bool = False
    enabled: bool = False
    path: str = ""
    acked: bool = False
    ack_fingerprint: str = ""
    error: str = ""

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def needs_ack(self) -> bool:
        """需要显式确认才算可用的插件：会执行代码，且指纹变了（或从没确认过）。"""
        if not self.executes_code:
            return False
        return not (self.acked and self.ack_fingerprint == self.ack_of())

    def ack_of(self) -> str:
        return _fingerprint({
            "id": self.id, "version": self.version, "kind": self.kind,
            "permissions": sorted(self.permissions),
            "executes_code": self.executes_code,
        })

    def permission_labels(self) -> list[str]:
        return [PERMISSION_LABELS.get(p, p) for p in self.permissions]

    def to_dict(self) -> dict[str, Any]:
        out = {k: getattr(self, k) for k in MANIFEST_FIELDS if hasattr(self, k)}
        out.update({
            "installed": self.installed, "enabled": self.enabled,
            "path": self.path, "acked": self.acked,
            "ack_fingerprint": self.ack_fingerprint, "error": self.error,
            "needs_ack": self.needs_ack,
        })
        return out


def parse_manifest(raw: dict[str, Any], *, source: str = "local") -> Plugin:
    """把一份清单 dict 收敛成 Plugin。

    缺 id/name 直接抛错（这种条目没有意义）；其余字段一律给安全默认值——
    清单是外部输入，不能让它把未知字段带进对象里。
    """
    if not isinstance(raw, dict):
        raise ValueError("清单必须是一个 JSON 对象")
    pid = str(raw.get("id") or "").strip()
    name = str(raw.get("name") or "").strip()
    if not pid:
        raise ValueError("清单缺少 id")
    if not name:
        name = pid
    kind = str(raw.get("kind") or "tool").strip().lower()
    if kind not in KINDS:
        kind = "tool"
    provides = raw.get("provides")
    if not isinstance(provides, dict):
        provides = {}
    perms = raw.get("permissions")
    if not isinstance(perms, list):
        perms = []
    perms = [str(p) for p in perms if str(p).strip()]
    tags = raw.get("tags")
    if not isinstance(tags, list):
        tags = []
    requires = raw.get("requires")
    if not isinstance(requires, dict):
        requires = {}
    return Plugin(
        id=pid,
        name=name,
        version=str(raw.get("version") or "0.0.0"),
        kind=kind,
        summary=str(raw.get("summary") or ""),
        description=str(raw.get("description") or ""),
        author=str(raw.get("author") or ""),
        homepage=str(raw.get("homepage") or ""),
        icon=str(raw.get("icon") or "🧩"),
        provides=provides,
        permissions=perms,
        # 只有明确写了 true 才算；字符串 "true"/1 不收（避免 YAML/JSON 混用踩坑）
        executes_code=raw.get("executes_code") is True,
        requires=requires,
        tags=[str(t) for t in tags if str(t).strip()],
        source=str(raw.get("source") or source),
    )


# ── 市场本体 ─────────────────────────────────────────────────────────


class Marketplace:
    """插件市场的读写核心。

    home 参数是 Forge 的家目录（默认 ~/.forge），测试里传临时目录即可，
    **不会碰真实用户数据**。
    """

    def __init__(self, home: str | os.PathLike | None = None,
                 builtin_catalog: Path | None = None):
        self.home = Path(home) if home else Path.home() / ".forge"
        self.market_dir = self.home / MARKET_DIRNAME
        self.plugins_dir = self.home / PLUGINS_DIRNAME
        self.state_path = self.market_dir / STATE_NAME
        self.log_path = self.market_dir / LOG_NAME
        self.cache_dir = self.market_dir / CACHE_DIRNAME
        # 备份与回收站放在 plugins/ 之外：留在 plugins/ 里的同名清单会被
        # local_entries() 再扫一遍，把新版本覆盖回旧版本（实测踩过）。
        self.trash_dir = self.market_dir / TRASH_DIRNAME
        self._builtin_catalog = builtin_catalog or (
            _runtime_base() / "catalog.builtin.json")
        self._state = self._load_state()

    # ── 状态 ────────────────────────────────────────────────────────

    def _default_state(self) -> dict[str, Any]:
        return {
            "enabled": [],
            "installed": {},          # id → {version, path, installed_at, source}
            "acked": {},              # id → 指纹
            "sources": [],            # 远程目录 URL（默认空 = 纯离线）
            "region": "china",
            "region_auto": True,
            "groups": {},
            "disabled": [],
        }

    def _load_state(self) -> dict[str, Any]:
        state = self._default_state()
        if not self.state_path.exists():
            return state
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 状态文件坏了就当空的重来，但要留痕（别静默吞掉）
            self._log_raw("warn", "state-unreadable",
                          f"{self.state_path} 解析失败，已按空状态继续")
            return state
        if not isinstance(raw, dict):
            return state
        for key, default in state.items():
            value = raw.get(key, default)
            if isinstance(default, list) and isinstance(value, list):
                state[key] = value
            elif isinstance(default, dict) and isinstance(value, dict):
                state[key] = value
            elif isinstance(default, str) and isinstance(value, str):
                state[key] = value
            elif isinstance(default, bool) and isinstance(value, bool):
                state[key] = value
        return state

    def _stash(self, path: Path, suffix: str) -> Path:
        """把目录挪到 marketplace/trash/<name>.<suffix>-<时间戳>，可恢复。"""
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d%H%M%S")
        target = self.trash_dir / f"{path.name}.{suffix}-{stamp}"
        n = 1
        while target.exists():   # 同秒内多次操作也别撞名
            target = self.trash_dir / f"{path.name}.{suffix}-{stamp}-{n}"
            n += 1
        shutil.move(str(path), str(target))
        return target

    def save(self) -> None:
        self.market_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self.state_path)

    def state(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._state))  # 深拷贝，别让调用方改内部

    # ── 事件日志 ────────────────────────────────────────────────────

    def _log_raw(self, level: str, event: str, detail: str = "") -> None:
        try:
            self.market_dir.mkdir(parents=True, exist_ok=True)
            row = {"at": _now(), "level": level, "event": event, "detail": detail}
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 日志不该把主流程带崩

    def log_tail(self, limit: int = 30) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        try:
            for line in self.log_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        except OSError:
            return []
        return rows[-limit:]

    # ── 目录读取 ────────────────────────────────────────────────────

    def builtin_entries(self) -> list[Plugin]:
        """随程序分发的离线目录。缺文件/坏文件都只当空，不影响启动。"""
        if not self._builtin_catalog.exists():
            return []
        try:
            raw = json.loads(self._builtin_catalog.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._log_raw("warn", "catalog-unreadable", str(exc))
            return []
        entries = raw.get("entries") if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            return []
        out: list[Plugin] = []
        for item in entries:
            try:
                out.append(parse_manifest(item, source="builtin"))
            except ValueError:
                continue
        return out

    def local_entries(self) -> list[Plugin]:
        """已装到 ~/.forge/plugins 的插件（目录名即 id 兜底）。"""
        out: list[Plugin] = []
        if not self.plugins_dir.is_dir():
            return out
        for child in sorted(self.plugins_dir.iterdir()):
            if not child.is_dir():
                continue
            # 老版本残留的备份/回收目录（早期实现留在 plugins/ 里）一律跳过
            if ".bak-" in child.name or ".removed-" in child.name:
                continue
            manifest_path = child / MANIFEST_NAME
            if not manifest_path.exists():
                continue
            try:
                raw = json.loads(manifest_path.read_text(encoding="utf-8"))
                plugin = parse_manifest(raw, source="local")
            except (OSError, ValueError) as exc:
                out.append(Plugin(id=child.name, name=child.name,
                                  source="local", installed=True,
                                  path=str(child), error=str(exc)))
                continue
            plugin.installed = True
            plugin.path = str(child)
            out.append(plugin)
        return out

    def catalog(self) -> list[Plugin]:
        """内置目录 + 本地已装 → 合并成一张榜单。

        同一 id 以本地为准（用户装过的版本更高优先），但保留内置条目里
        本地清单没写的展示字段（作者/描述等），免得装完信息变空。
        """
        merged: dict[str, Plugin] = {}
        for plugin in self.builtin_entries():
            merged[plugin.id] = plugin
        for plugin in self.local_entries():
            base = merged.get(plugin.id)
            if base is not None:
                for key in ("summary", "description", "author", "homepage",
                            "tags", "icon"):
                    if not getattr(plugin, key):
                        setattr(plugin, key, getattr(base, key))
                if not plugin.provides:
                    plugin.provides = base.provides
                if not plugin.permissions:
                    plugin.permissions = base.permissions
                if not plugin.requires:
                    plugin.requires = base.requires
            merged[plugin.id] = plugin
        # 状态回填
        installed = self._state.get("installed", {})
        enabled = set(self._state.get("enabled", []))
        acked = self._state.get("acked", {})
        for pid, plugin in merged.items():
            if pid in installed:
                plugin.installed = True
                plugin.path = plugin.path or str(installed[pid].get("path", ""))
            plugin.enabled = pid in enabled
            if pid in acked:
                plugin.acked = True
                plugin.ack_fingerprint = str(acked[pid])
        return sorted(merged.values(), key=lambda p: (not p.enabled, p.name))

    def find(self, pid: str) -> Plugin | None:
        for plugin in self.catalog():
            if plugin.id == pid:
                return plugin
        return None

    # ── 安装 / 卸载 / 启停 ──────────────────────────────────────────

    def install_from_dir(self, src: str | os.PathLike, *,
                         enable: bool = False) -> Plugin:
        """从一个本地目录安装（对应 DSH 的 `plugin add file:<路径>`）。"""
        src_path = Path(src).expanduser().resolve()
        manifest_path = src_path / MANIFEST_NAME
        if not manifest_path.is_file():
            raise FileNotFoundError(f"{src_path} 里没有 {MANIFEST_NAME}")
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        plugin = parse_manifest(raw, source="local")
        if plugin.id in ("", ".", "..") or os.sep in plugin.id or "/" in plugin.id:
            raise ValueError(f"非法插件 id：{plugin.id!r}")

        dest = self.plugins_dir / plugin.id
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            # 升级：旧目录挪进 trash 备份，失败可回滚（对应 DSH reconcile 的幂等语义）
            self._stash(dest, "bak")
        try:
            shutil.copytree(src_path, dest,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc",
                                                          ".git", "node_modules"))
        except OSError as exc:
            raise OSError(f"复制插件失败：{exc}") from exc

        installed = self._state.setdefault("installed", {})
        installed[plugin.id] = {
            "version": plugin.version,
            "path": str(dest),
            "installed_at": _now(),
            "source": plugin.source,
        }
        if enable and not plugin.needs_ack:
            self._enable_in_state(plugin.id)
        self.save()
        self._log_raw("info", "install",
                      f"{plugin.id}@{plugin.version} → {dest}")
        return self.find(plugin.id) or plugin

    def install_from_catalog(self, pid: str, *, enable: bool = False) -> Plugin:
        """从内置目录安装。

        内置条目是**声明式**的（没有代码体），安装 = 在 plugins 目录里落一份
        清单，让它进可启用集合。带代码体的条目必须用 install_from_dir。
        """
        plugin = self.find(pid)
        if plugin is None:
            raise KeyError(f"目录里没有 {pid}")
        if plugin.executes_code:
            raise ValueError(
                f"{pid} 声明 executes_code=true，不能从目录静默安装；"
                "请用 install_from_dir 指向你信任的本地目录")
        dest = self.plugins_dir / plugin.id
        if not dest.exists():
            dest.mkdir(parents=True, exist_ok=True)
            payload = {k: getattr(plugin, k) for k in MANIFEST_FIELDS
                       if hasattr(plugin, k)}
            payload["source"] = "builtin"
            (dest / MANIFEST_NAME).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8")
        installed = self._state.setdefault("installed", {})
        installed[plugin.id] = {
            "version": plugin.version, "path": str(dest),
            "installed_at": _now(), "source": "builtin",
        }
        if enable:
            self._enable_in_state(plugin.id)
        self.save()
        self._log_raw("info", "install", f"{plugin.id}@{plugin.version}（目录）")
        return self.find(plugin.id) or plugin

    def uninstall(self, pid: str) -> bool:
        dest = self.plugins_dir / pid
        if dest.exists():
            self._stash(dest, "removed")
        self._state.get("installed", {}).pop(pid, None)
        self._state.get("acked", {}).pop(pid, None)
        self._disable_in_state(pid)
        self.save()
        self._log_raw("info", "uninstall", pid)
        return True

    def enable(self, pid: str) -> Plugin:
        plugin = self.find(pid)
        if plugin is None:
            raise KeyError(pid)
        if not plugin.installed:
            raise ValueError(f"{pid} 尚未安装，不能启用")
        if plugin.needs_ack:
            raise PermissionError(
                f"{pid} 会执行本机代码（权限：{'、'.join(plugin.permission_labels()) or '未声明'}），"
                "必须先在界面上确认信任才能启用")
        self._enable_in_state(pid)
        self.save()
        self._log_raw("info", "enable", pid)
        return self.find(pid) or plugin

    def disable(self, pid: str) -> Plugin:
        self._disable_in_state(pid)
        self.save()
        self._log_raw("info", "disable", pid)
        return self.find(pid) or Plugin(id=pid, name=pid)

    def _enable_in_state(self, pid: str) -> None:
        enabled = self._state.setdefault("enabled", [])
        if pid not in enabled:
            enabled.append(pid)
        disabled = self._state.setdefault("disabled", [])
        if pid in disabled:
            disabled.remove(pid)

    def _disable_in_state(self, pid: str) -> None:
        enabled = self._state.setdefault("enabled", [])
        if pid in enabled:
            enabled.remove(pid)
        disabled = self._state.setdefault("disabled", [])
        if pid not in disabled:
            disabled.append(pid)

    def ack(self, pid: str) -> Plugin:
        """用户在界面上显式确认信任这个插件（记录当前指纹）。"""
        plugin = self.find(pid)
        if plugin is None:
            raise KeyError(pid)
        self._state.setdefault("acked", {})[pid] = plugin.ack_of()
        self.save()
        self._log_raw("info", "ack", f"{pid} 指纹 {plugin.ack_of()}")
        return self.find(pid) or plugin

    def revoke_ack(self, pid: str) -> None:
        self._state.get("acked", {}).pop(pid, None)
        self._disable_in_state(pid)
        self.save()
        self._log_raw("info", "revoke-ack", pid)

    # ── 汇总 ────────────────────────────────────────────────────────

    def summary(self) -> dict[str, Any]:
        items = self.catalog()
        return {
            "total": len(items),
            "installed": sum(1 for p in items if p.installed),
            "enabled": sum(1 for p in items if p.enabled),
            "need_ack": sum(1 for p in items if p.needs_ack),
            "kinds": {k: sum(1 for p in items if p.kind == k) for k in KINDS},
            "region": self._state.get("region", "china"),
            "sources": list(self._state.get("sources", [])),
        }

    def enabled_tools(self) -> list[str]:
        """所有已启用插件声明的工具名——供工具市场去重展示。"""
        names: list[str] = []
        for plugin in self.catalog():
            if not plugin.enabled:
                continue
            for tool in _as_list(plugin.provides.get("tools")):
                if tool not in names:
                    names.append(tool)
        return names

    def render_text(self) -> str:
        """纯文本摘要（终端/测试用，也是没有 GUI 时的兜底视图）。"""
        items = self.catalog()
        lines = [f"Forge 市场 · 共 {len(items)} 条"]
        for plugin in items:
            marks = []
            if plugin.enabled:
                marks.append("启用")
            elif plugin.installed:
                marks.append("已装")
            if plugin.needs_ack:
                marks.append("待确认")
            suffix = f"［{' / '.join(marks)}］" if marks else ""
            lines.append(f"  {plugin.icon} {plugin.name} v{plugin.version} "
                         f"· {plugin.kind_label}{suffix}")
            if plugin.summary:
                lines.append(f"      {plugin.summary}")
        return "\n".join(lines)


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value:
        return [value]
    return []


# ── 工具市场：把网关工具桥和插件声明合成一张表 ──────────────────────


@dataclass
class ToolEntry:
    name: str
    source: str          # gateway | plugin
    summary: str = ""
    plugin_id: str = ""
    danger: bool = False

    @property
    def label(self) -> str:
        return self.name

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "source": self.source, "summary": self.summary,
                "plugin_id": self.plugin_id, "danger": self.danger}


#: 网关工具的粗分类：写/执行类给个警示色，读类保持中性
_DANGEROUS_HINTS = ("write", "exec", "delete", "remove", "move", "patch", "edit",
                    "kill", "run", "install", "shell")


def classify_tool(name: str) -> bool:
    low = name.lower()
    return any(hint in low for hint in _DANGEROUS_HINTS)


TOOL_SUMMARY_HINTS = {
    "read_file": "读取文件内容",
    "write_file": "写入文件",
    "edit_file": "按片段改写文件",
    "list_dir": "列出目录",
    "grep": "按正则搜索文本",
    "shell_exec": "执行 shell 命令",
    "datetime": "取本机当前时间",
    "python": "执行 Python 片段",
}


def build_tool_catalog(gateway_tools: Iterable[str] | None,
                       marketplace: Marketplace | None = None,
                       runtime_tools: list[tuple[str, str, str]] | None = None,
                       ) -> list[ToolEntry]:
    """工具市场 = 网关工具桥的工具 + 插件声明的工具 + 插件运行时的工具。

    runtime_tools：[(name, plugin_id, plugin_name), ...]，来自 PluginRuntime
    实际 register() 出来的可执行工具——这部分不在 provides 里（provides 是
    静态声明，runtime 是代码注册的），市场视图两边都要显示。
    gateway_tools 为 None 表示桥不可用（未启动/没开 --tools）——这时仍返回
    插件工具，UI 再单独提示桥状态，不要因为一半拿不到就整表空掉。
    """
    entries: list[ToolEntry] = []
    seen: set[str] = set()
    for name in (gateway_tools or []):
        clean = str(name)
        if clean.startswith("forge_"):
            clean = clean.removeprefix("forge_")
        if clean in seen:
            continue
        seen.add(clean)
        entries.append(ToolEntry(
            name=clean, source="gateway",
            summary=TOOL_SUMMARY_HINTS.get(clean, ""),
            danger=classify_tool(clean),
        ))
    if marketplace is not None:
        for plugin in marketplace.catalog():
            if not plugin.enabled:
                continue
            for tool in _as_list(plugin.provides.get("tools")):
                if tool in seen:
                    continue
                seen.add(tool)
                entries.append(ToolEntry(
                    name=tool, source="plugin", plugin_id=plugin.id,
                    summary=f"来自插件「{plugin.name}」",
                    danger=classify_tool(tool),
                ))
    for name, plugin_id, plugin_name in (runtime_tools or []):
        if name in seen:
            continue
        seen.add(name)
        entries.append(ToolEntry(
            name=name, source="plugin", plugin_id=plugin_id,
            summary=f"来自插件「{plugin_name}」",
            danger=classify_tool(name),
        ))
    return sorted(entries, key=lambda e: (e.source != "gateway", e.name))


def default_marketplace() -> Marketplace:
    return Marketplace()


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - 手跑入口
    """`python plugin_market.py [list|summary|log]` —— 不开 GUI 时的速查。"""
    import argparse
    parser = argparse.ArgumentParser(description="Forge 插件市场速查")
    parser.add_argument("cmd", nargs="?", default="list",
                        choices=("list", "summary", "log"))
    args = parser.parse_args(argv)
    market = default_marketplace()
    if args.cmd == "summary":
        print(json.dumps(market.summary(), ensure_ascii=False, indent=2))
    elif args.cmd == "log":
        for row in market.log_tail():
            print(f"{row.get('at')} [{row.get('level')}] "
                  f"{row.get('event')} {row.get('detail', '')}")
    else:
        print(market.render_text())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
