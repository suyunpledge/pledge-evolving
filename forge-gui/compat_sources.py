"""外部生态插件目录适配层（OpenClaw / Claude Code / DeepSeek Harness / Codex）。

设计原则（与 plugin_capabilities.py 的声明式能力代理契约对齐，2026-10-04）：
1. 只读扫描本机已有安装，不执行任何外部代码、不联网。
2. 产出 parse_manifest 兼容的声明式清单（capabilities / contributions 契约）。
3. 外部生态插件在 Forge 中一律 executes_code=False 的声明式条目：
   文档随插件安装进 ~/.forge/plugins，Agent 侧经 CapabilityRuntime 受控调用。
4. Codex 正在持续改造市场核心（capabilities / grants / policy 链路）——本模块是
   旁路源：只提供 scan_*() -> list[CompatEntry]，不改 Marketplace 的授权与安装
   语义；Marketplace.external_entries() 把它合并进目录，GUI 增加生态筛选。

各生态数据形态（2026-10-04 实测本机）：
- OpenClaw skills:    ~/.openclaw(-autoclaw)/skills/<name>/SKILL.md  (frontmatter: name/description)
- Claude Code market: ~/.claude/plugins/marketplaces/<mkt>/.claude-plugin/marketplace.json
                      + plugins/<name>/.claude-plugin/plugin.json + commands/*.md + skills/
- DSH bundles:        ~/.dsh/profiles/<profile>/package.json dependencies + dsh.profile.bundles
- Codex plugins:      ~/.codex/config.toml [plugins."name@runtime"] enabled + mcp_servers.*
                      ChatGPT 正在 Codex 内改造插件市场整体功能——Codex 源是保守映射：
                      只读 config.toml 的已安装 plugins/mcp_servers 清单，不猜测在途格式；
                      刷新入口 codex_refetch() 重扫，扫描失败自动降级为空列表。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

HOME = Path.home()

OPENCLAW_SKILL_ROOTS = (
    HOME / ".openclaw" / "skills",
    HOME / ".openclaw-autoclaw" / "skills",
)
CLAUDE_MARKET_ROOT = HOME / ".claude" / "plugins" / "marketplaces"
DSH_PROFILE_ROOT = HOME / ".dsh" / "profiles"
CODEX_HOME = HOME / ".codex"


def _read_text(path: Path, limit: int = 262144) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def _read_json(path: Path):
    try:
        return json.loads(_read_text(path).lstrip("\ufeff"))
    except ValueError:
        return None


def _slug(name: str) -> str:
    s = re.sub(r"[^\w-]+", "-", str(name), flags=re.UNICODE).strip("-")
    return s[:120] or "x"


def _one_line(text: str, limit: int) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def _frontmatter(text: str) -> dict:
    """Markdown frontmatter 极简解析（不引 yaml 依赖；块标量折叠为一行）。"""
    m = re.match(r"\A---\s*\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    out: dict = {}
    pending_key = None
    buf: list = []
    for line in m.group(1).splitlines():
        stripped = line.strip()
        if pending_key is not None:
            if stripped and not line[:1].isspace():
                out[pending_key] = _one_line(" ".join(buf), 1024)
                pending_key, buf = None, []
            else:
                buf.append(stripped)
                continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if val in ("|", ">", ">-", "|-"):
            pending_key = key
            buf = []
            continue
        out[key] = val
    if pending_key is not None:
        out[pending_key] = _one_line(" ".join(buf), 1024)
    return out


@dataclass
class CompatEntry:
    """转换中的一条外部条目（尚未成为 Forge Plugin）。"""

    source_eco: str                                # openclaw | claude | dsh | codex
    origin: str                                    # 生态内标识（技能名/包名/插件名）
    manifest: dict = field(default_factory=dict)   # forge-plugin.json 契约
    install_dir: Path | None = None                # 有实体目录的条目可本地安装
    note: str = ""


# ---- OpenClaw skills ----


def scan_openclaw_skills(roots: Iterable[Path] | None = None) -> list[CompatEntry]:
    entries: list[CompatEntry] = []
    seen: set[str] = set()
    for root in roots or OPENCLAW_SKILL_ROOTS:
        root = Path(root).expanduser()
        if not root.is_dir():
            continue
        for skill_dir in sorted(root.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                continue
            text = _read_text(skill_md)
            if not text:
                continue
            fm = _frontmatter(text)
            name = fm.get("name") or skill_dir.name
            desc = _one_line(fm.get("description", ""), 200)
            if not desc:
                for ln in text.splitlines():
                    s = ln.strip().lstrip("#").strip()
                    if s and not s.startswith(("---", "!", "```")):
                        desc = s[:200]
                        break
            eco_id = f"openclaw-{_slug(skill_dir.name)}"
            if eco_id in seen:
                continue
            seen.add(eco_id)
            entries.append(CompatEntry(
                source_eco="openclaw",
                origin=skill_dir.name,
                install_dir=skill_dir,
                manifest={
                    "id": eco_id,
                    "name": f"{name}（OpenClaw 技能）",
                    "version": "1.0.0",
                    "kind": "integration",
                    "icon": "🦞",
                    "summary": desc or f"OpenClaw 技能 {skill_dir.name}",
                    "description": (
                        f"来源：OpenClaw 技能目录 {skill_dir}。SKILL.md 随插件安装进 Forge，"
                        "作为声明式说明书条目展示；不执行外部代码，不给 Agent 提供执行面。"),
                    "author": "OpenClaw 本机安装",
                    "executes_code": False,
                    "capabilities": ["repo.read"],
                    "provides": {"tools": []},
                    "contributions": {"tools": []},
                    "tags": ["OpenClaw", "技能", "声明式"],
                    "source": "builtin",
                },
            ))
    return entries


# ---- Claude Code marketplace ----


def _claude_first_command_summary(mkt_dir: Path, name: str) -> str:
    """取 Claude 插件目录里第一个 command/skill 的描述做 summary 兜底。"""
    for base in (mkt_dir / "plugins" / name, mkt_dir / "external_plugins" / name):
        for sub in ("commands", "skills"):
            d = base / sub
            if not d.is_dir():
                continue
            mds = sorted(d.rglob("*.md"))
            if mds:
                fm = _frontmatter(_read_text(mds[0], 65536))
                got = fm.get("description", "")
                if got:
                    return _one_line(got, 160)
    return ""


def scan_claude_marketplace(root: Path | None = None) -> list[CompatEntry]:
    entries: list[CompatEntry] = []
    root = Path(root) if root else CLAUDE_MARKET_ROOT
    if not root.is_dir():
        return entries
    for mkt_dir in sorted(root.iterdir()):
        if not mkt_dir.is_dir() or mkt_dir.name.startswith("."):
            continue
        meta_path = mkt_dir / ".claude-plugin" / "marketplace.json"
        if not meta_path.is_file():
            continue
        mkt = _read_json(meta_path)
        if not isinstance(mkt, dict):
            continue
        for item in mkt.get("plugins", []):
            if not isinstance(item, dict) or not item.get("name"):
                continue
            name = str(item.get("name", ""))
            src = item.get("source") or {}
            if isinstance(src, str):
                src_str = src
            elif isinstance(src, dict):
                src_str = str(src.get("url", ""))
                if src.get("path"):
                    src_str += f"#path:{src['path']}"
            else:
                src_str = ""
            desc = _one_line(item.get("description", ""), 200)
            author = item.get("author") or {}
            if isinstance(author, dict):
                author_name = str(author.get("name", ""))
            else:
                author_name = _one_line(author, 120)
            eco_id = f"claude-{_slug(name)}"
            local_dir = None
            for candidate in (mkt_dir / "plugins" / name,
                              mkt_dir / "external_plugins" / name):
                if candidate.is_dir():
                    local_dir = candidate
                    break
            if not desc:
                desc = _claude_first_command_summary(mkt_dir, name) or f"Claude Code 市场插件 {name}"
            entries.append(CompatEntry(
                source_eco="claude",
                origin=name,
                install_dir=local_dir,
                manifest={
                    "id": eco_id,
                    "name": f"{name}（Claude Code）",
                    "version": "1.0.0",
                    "kind": "tool",
                    "icon": "🎭",
                    "summary": desc,
                    "description": (
                        f"来源：Claude Code 市场 {mkt_dir.name}（本机克隆）。作者 "
                        f"{author_name or '未知'}。源 {src_str or '仓库内置'}。"
                        "commands/skills 文档随插件安装进 Forge；声明式展示，不执行外部代码。"),
                    "author": author_name or "Claude Code 生态",
                    "homepage": str(item.get("homepage", "") or "")[:2000],
                    "executes_code": False,
                    "capabilities": ["repo.read"],
                    "provides": {"tools": []},
                    "contributions": {"tools": []},
                    "tags": ["Claude Code", "市场插件", "声明式"],
                    "source": "builtin",
                },
            ))
    seen: set[str] = set()
    out: list[CompatEntry] = []
    for e in entries:
        if e.manifest.get("id") in seen:
            continue
        seen.add(e.manifest.get("id"))
        out.append(e)
    return out


# ---- DSH bundles ----


def scan_dsh_bundles(root: Path | None = None) -> list[CompatEntry]:
    """扫描 ~/.dsh/profiles/*/package.json 的依赖与 bundle 激活态。"""
    entries: list[CompatEntry] = []
    root = Path(root) if root else DSH_PROFILE_ROOT
    if not root.is_dir():
        return entries
    seen: set[str] = set()
    for prof in sorted(root.iterdir()):
        if not prof.is_dir() or prof.name.startswith("."):
            continue
        pkg = _read_json(prof / "package.json")
        if not isinstance(pkg, dict):
            continue
        deps = pkg.get("dependencies", {})
        bundles = (((pkg.get("dsh") or {}).get("profile") or {}).get("bundles")) or []
        if not isinstance(deps, dict) or not isinstance(bundles, list):
            continue
        for dep_name in sorted(deps):
            spec = str(deps[dep_name])
            eco_id = f"dsh-{_slug(dep_name)}"
            active = dep_name in bundles
            if eco_id in seen:
                # 已登记过：只升级激活态（任一 profile 里在 bundle 列表即算生效中）
                if active:
                    for prev in entries:
                        if prev.manifest.get("id") == eco_id and "未激活" in prev.manifest.get("summary", ""):
                            prev.manifest["summary"] = prev.manifest["summary"].replace("未激活", "生效中")
                continue
            seen.add(eco_id)
            local_dir = None
            if spec.startswith("file:"):
                p = Path(spec[5:]).expanduser()
                if p.is_dir():
                    local_dir = p
            entries.append(CompatEntry(
                source_eco="dsh",
                origin=dep_name,
                install_dir=local_dir,
                manifest={
                    "id": eco_id,
                    "name": f"{dep_name}（DSH 插件）",
                    "version": "1.0.0",
                    "kind": "integration",
                    "icon": "🐳",
                    "summary": f"DSH {prof.name} profile 依赖 {dep_name}（{spec}，bundle {'生效中' if active else '未激活'}）",
                    "description": (
                        f"来源：DeepSeek Harness ~/.dsh/profiles/{prof.name} 的 package.json 依赖。"
                        f"包管理器 spec：{spec}。bundle 激活状态：{'生效中' if active else '未激活'}。"
                        "在 Forge 中仅声明式展示；启用不会执行其 Node 代码。"),
                    "author": dep_name.split("/")[-1],
                    "executes_code": False,
                    "capabilities": ["repo.read"],
                    "provides": {"tools": []},
                    "contributions": {"tools": []},
                    "tags": ["DSH", "bundle", "声明式"],
                    "source": "builtin",
                },
            ))
    return entries


# ---- Codex plugins（保守映射）----


def scan_codex_plugins(home: Path | None = None) -> list[CompatEntry]:
    """读 ~/.codex/config.toml 的 [plugins.*] 与 [mcp_servers.*]。

    ChatGPT 正在 Codex 内改造插件市场整体功能，格式在演进——这里只做
    保守映射：已安装且 enabled 的条目转成声明式清单；解析不了的一律
    跳过（宁缺毋滥），不影响其他源。
    """
    entries: list[CompatEntry] = []
    home = Path(home) if home else CODEX_HOME
    cfg = _read_text(home / "config.toml")
    if not cfg:
        return entries
    # [plugins."name@runtime"] → enabled
    for m in re.finditer(
            r'^\[plugins\."([^"]+)"\]\s*$', cfg, re.M):
        plugin_id_raw = m.group(1)
        section = _toml_section(cfg, f'plugins."{plugin_id_raw}"')
        if not isinstance(section.get("enabled"), bool):
            continue
        if section["enabled"] is not True:
            continue
        short = plugin_id_raw.split("@")[0]
        eco_id = f"codex-{_slug(short)}"
        entries.append(CompatEntry(
            source_eco="codex",
            origin=plugin_id_raw,
            manifest={
                "id": eco_id,
                "name": f"{short}（Codex 插件）",
                "version": "1.0.0",
                "kind": "integration",
                "icon": "🤖",
                "summary": f"Codex 已启用插件 {plugin_id_raw}",
                "description": (
                    f"来源：Codex ~/.codex/config.toml [plugins.\"{plugin_id_raw}\"]。"
                    "Codex 插件市场正在由 ChatGPT 侧持续改造，Forge 仅做只读声明式映射；"
                    "启用不会执行其代码。刷新源后按最新 config 重新生成。"),
                "author": "Codex 本机安装",
                "executes_code": False,
                "capabilities": ["repo.read"],
                "provides": {"tools": []},
                "contributions": {"tools": []},
                "tags": ["Codex", "声明式"],
                "source": "builtin",
            },
        ))
    # [mcp_servers.name] → 集成条目（只列 command，不解析 env）
    for m in re.finditer(r'^\[mcp_servers\.([A-Za-z0-9_-]+)\]\s*$', cfg, re.M):
        srv = m.group(1)
        eco_id = f"codex-mcp-{_slug(srv)}"
        if any(e.manifest.get("id") == eco_id for e in entries):
            continue
        section = _toml_section(cfg, f"mcp_servers.{srv}")
        cmd = str(section.get("command", "") or "")
        entries.append(CompatEntry(
            source_eco="codex",
            origin=f"mcp:{srv}",
            manifest={
                "id": eco_id,
                "name": f"{srv}（Codex MCP）",
                "version": "1.0.0",
                "kind": "integration",
                "icon": "🔌",
                "summary": f"Codex MCP 服务器 {srv}" + (f"（{cmd}）" if cmd else ""),
                "description": (
                    f"来源：Codex ~/.codex/config.toml [mcp_servers.{srv}]。"
                    "Forge 只登记该集成的存在与命令路径，不启动、不执行。"),
                "author": "Codex 本机安装",
                "executes_code": False,
                "capabilities": ["repo.read"],
                "provides": {"tools": []},
                "contributions": {"tools": []},
                "tags": ["Codex", "MCP", "声明式"],
                "source": "builtin",
            },
        ))
    return entries


def _toml_section(cfg: str, dotted: str) -> dict:
    """极简 TOML 段提取：取 [dotted] 到下一段之间的 key = value 行。"""
    out: dict = {}
    pat = re.compile(r'^\[' + re.escape(dotted) + r'\]\s*$', re.M)
    m = pat.search(cfg)
    if not m:
        return out
    tail = cfg[m.end():]
    nxt = re.search(r'^\[', tail, re.M)
    body = tail if nxt is None else tail[:nxt.start()]
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip().strip("'\"")
        if val == "true":
            val = True
        elif val == "false":
            val = False
        out[key.strip()] = val
    return out


# ---- 聚合与缓存 ----

_SCAN_CACHE: dict = {}
_SCAN_CACHE_AT: dict = {}


def compat_entries(refresh: bool = False) -> list[CompatEntry]:
    """聚合四个生态的扫描结果（带 60s 缓存；任何单源失败返回空表不炸整体）。"""
    import time
    now = time.time()
    scanners = {
        "openclaw": scan_openclaw_skills,
        "claude": scan_claude_marketplace,
        "dsh": scan_dsh_bundles,
        "codex": scan_codex_plugins,
    }
    out: list[CompatEntry] = []
    for eco, fn in scanners.items():
        if not refresh and eco in _SCAN_CACHE and now - _SCAN_CACHE_AT.get(eco, 0) < 60:
            out.extend(_SCAN_CACHE[eco])
            continue
        try:
            got = fn()
        except Exception:
            got = []
        _SCAN_CACHE[eco] = got
        _SCAN_CACHE_AT[eco] = now
        out.extend(got)
    return out


def codex_refetch() -> int:
    """手动刷新 Codex 源（ChatGPT 改造中的格式演进由重扫吸收）。返回条数。"""
    got = scan_codex_plugins()
    _SCAN_CACHE["codex"] = got
    import time
    _SCAN_CACHE_AT["codex"] = time.time()
    return len(got)


def eco_labels() -> dict:
    """GUI 生态筛选标签。"""
    return {
        "openclaw": "🦞 OpenClaw",
        "claude": "🎭 Claude Code",
        "dsh": "🐳 DSH",
        "codex": "🤖 Codex",
    }
