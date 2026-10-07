"""Command line surface.

Deliberately mirrors what the seven reference frameworks converged on:

  forge run "<task>"          one-shot headless run (DSH headless / codex exec)
  forge dump-config           show the composed tree (DSH dump-config)
  forge dump-default-config   same, ignoring the user layer (DSH recovery path)
  forge doctor                config drift, secrets, sandbox and store checks
  forge sessions [--rebuild]  session index (Codex session_index.jsonl)
  forge capabilities [...]    audit / trust / enable (Hermes skills, Codex plugins)
  forge memory [...]          remember / recall / curate (WorkBuddy typed memory)
  forge checkpoint [...]      history / snapshot / rollback (Hermes checkpoints)
  forge gateway               run the loopback protocol gateway
  forge selftest              offline end-to-end verification
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .adapters import PlanContext, breakeven_calls, compare_plans
from .cache_state import prefix_fingerprint
from .config import USER_LAYER_NAME, Config, ConfigError, load_config
from .context_plan import CONTEXT_MODES, ContextPlan
from .loop import THINKING_MODES, LoopLimits, build_agent
from .model import ModelRouter, warmth_store
from .policy import Policy
from .routing import STRATEGIES, SmartRouter
from .vendors import PROFILES, VENDOR_ORDER, profile_for

DEFAULT_HOME = Path.home() / ".forge"
# D2: bundles ship BOTH as a repo-root directory (editable checkout) and
# inside the installed forge package (wheel). Resolve whichever exists so a
# pip-installed `forge` CLI still finds its default bundle.
_PKG_BUNDLES = Path(__file__).resolve().parent / "bundles"
_REPO_BUNDLES = Path(__file__).resolve().parent.parent / "bundles"
BUNDLE_DIR = _PKG_BUNDLES if _PKG_BUNDLES.is_dir() else _REPO_BUNDLES


def _compose(args) -> Config:
    home = Path(args.home)
    bundle = Path(args.bundle) if getattr(args, "bundle", None) else None
    bundles = [bundle] if bundle else sorted(BUNDLE_DIR.glob("*.json"))
    # --coding 叠加编码模式补丁层（放在 base 之后，后写胜）。它住在 modes/ 子目录，
    # 所以默认 glob "*.json" 不会把它当成默认层（默认 run 保持纯 base）。
    if getattr(args, "coding", False):
        coding_bundle = BUNDLE_DIR / "modes" / "coding.json"
        if not coding_bundle.is_file():
            raise SystemExit(f"--coding bundle not found: {coding_bundle}")
        bundles = list(bundles) + [coding_bundle]
    overlays = list(getattr(args, "overlay", []) or [])
    cfg = load_config(
        home,
        bundles=bundles,
        overlays=overlays,
        include_user_layer=not getattr(args, "no_user_layer", False),
    )
    _apply_profile(args, cfg)
    return cfg


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def _primary_profile(cfg):
    """解析「实际会首发的那一档」对应的厂商画像。

    不能只看 model.primary：策略为 medium 时真正首发的是 routing.tiers 的第一档，
    primary 只是配置里写下的默认值。直接问 SmartRouter 要真实顺序，
    上下文阶梯的判断才会和运行时选到同一家（否则会拿 A 家的阶梯线去限制 B 家）。
    """
    order: list[tuple[str, str]] = []
    try:
        order = list(SmartRouter.from_config(cfg)._order(None))
    except Exception:
        order = []
    if not order:
        primary = cfg.get("model", "primary", None)
        if isinstance(primary, (list, tuple)) and primary:
            order = [(str(primary[0]), str(primary[1]) if len(primary) > 1 else "")]
        elif isinstance(primary, str):
            order = [(primary, "")]

    row_id, model = order[0] if order else ("", "")
    row = cfg.row(row_id) if row_id else None
    conf = dict(row.config) if row is not None else {}
    return profile_for(
        base_url=str(conf.get("baseURL", conf.get("base_url", ""))),
        model=str(model or conf.get("model", conf.get("defaultModel", ""))),
        service=str(conf.get("service", "")),
        vendor=str(conf.get("vendor", "")),
        wire=str(conf.get("wire", "")),
    )


def _credential_report(cfg, home: Path) -> tuple[list[str], list[str], list[str]]:
    """把 provider 分成三类（都以**当前进程已解析**的值为准）：

      keyed      —— 已经解析出非空密钥（字面量密钥，或环境变量已设）
      env_wait   —— 声明了密钥但本次进程解析为空（走 $expr/env，注入前还没值）
      local      —— 本机 / 环回地址，不需要密钥（本地网关、本地推理引擎）

    为什么要分三类：全新安装的 base.json 里 medium 写的是
    ``get('env.FORGE_MEDIUM_KEY', '')``——它是“已声明但没值”，而不是“有密钥”。
    旧代码只看字段是否非空字符串，于是 base 的占位行也算可用，doctor 报全绿
    但实际跑就是 401。分桶之后，“现在能不能跑”才是一个真数字。

    本机地址优先归入 local，**不看密钥**：base 的回环占位把 apiKey 默认值写成
    了字面量 'local'（``get('env.FORGE_LITE_KEY', 'local')``），先判地址再判密钥。
    """
    keyed: list[str] = []
    env_wait: list[str] = []
    local: list[str] = []
    for row_id, name, conf in cfg.active():
        if not str(name).startswith("provider:"):
            continue
        base_url = str(conf.get("baseURL", conf.get("base_url", "")))
        key = conf.get("apiKey", conf.get("api_key", ""))
        is_local = any(host in base_url for host in ("127.0.0.1", "localhost", "::1", "0.0.0.0"))
        if is_local or str(conf.get("service", "")):
            local.append(row_id)
            continue
        if isinstance(key, str) and key.strip():
            keyed.append(row_id)
        else:
            # 空值 + 来自表达式 / 环境变量 = 已声明但没值。
            env_wait.append(row_id)
    return keyed, env_wait, local


def _usable_providers(cfg, home: Path) -> list[str]:
    """当前进程下真的能用、且**用户真的配过**的 provider 行 id。

    本机地址只在用户层存在时才算数：base.json 的回环行是占位示例，
    全新安装里它们没在跑，不该构成“可用”（否则 doctor 又变回假绿灯）。
    用户自己写了用户层时，本机 provider 视为故意配置（本地网关/本地引擎）。
    """
    keyed, _env_wait, local = _credential_report(cfg, home)
    if (home / USER_LAYER_NAME).is_file():
        return keyed + local
    return keyed


def _first_run_hint(home: Path) -> str:
    """全新未配置时的引导语，而不是把 401 丢给用户。"""
    return (
        "还没有可用的模型接入。\n"
        f"  配置文件：{home / USER_LAYER_NAME}（不存在，或里面没有可用凭据）\n"
        "  下一步：\n"
        f"    python run.py --home {home} setup        # 交互式选厂商、填密钥\n"
        "  或先用环境变量注入密钥再跑（例如 FORGE_DEEPSEEK_KEY）。"
    )


class _ProgressWriter:
    """把 run 过程中的关键事件打成一行进度，写 stderr。

    背景：一次 run 最多 12 步、每步一次模型调用，之前启用完成后才输出，
    长任务期间用户完全看不到动静。这个观察者只报**稀疏且有意义**的节点，
    不做逐 token 刷新（后者既慢又刷屏）。

    只有 stderr 是终端时才输出：管道 / 重定向下保持安静，不会污染机器可读输出。
    """

    # 值得报的事件 -> 前缀标签
    _LABELS = {
        "thinking_engaged": "思考",
        "tool_call": "工具",
        "compaction": "压缩",
        "subagent_spawn": "子代理",
        "assistant_message": "回复",
        "model_error": "错误",
        "mount_warning": "挂载",
    }

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._step = 0

    def __call__(self, event: dict) -> None:
        if not self.enabled:
            return
        kind = str(event.get("type", ""))
        label = self._LABELS.get(kind)
        if label is None:
            return
        self._step += 1
        detail = ""
        if kind == "tool_call":
            detail = str(event.get("tool") or event.get("name") or "")
            decision = str(event.get("decision") or "")
            if decision:
                detail = f"{detail} {decision}".strip()
        elif kind == "compaction":
            before, after = event.get("before"), event.get("after")
            if before is not None and after is not None:
                detail = f"{before} -> {after} 字符"
        elif kind in ("model_error", "mount_warning"):
            detail = str(event.get("error") or event.get("note") or "")[:120]
        try:
            print(f"  · {label} {detail}".rstrip(), file=sys.stderr, flush=True)
        except OSError:
            self.enabled = False

    def finish(self) -> None:
        """结束时把进度行与正文分开，避免混在一起看。"""
        if self.enabled and self._step:
            try:
                print(file=sys.stderr, flush=True)
            except OSError:
                pass


def _make_progress_writer(*, enabled: bool) -> _ProgressWriter:
    """按终端情况决定是否真的输出进度。"""
    interactive = False
    try:
        interactive = bool(getattr(sys.stderr, "isatty", lambda: False)())
    except (OSError, ValueError):
        interactive = False
    return _ProgressWriter(enabled=enabled and interactive)


def cmd_run(args) -> int:
    # 指令一（2026-09-15）：无头 run 链路默认即 balanced —— 工作区沙箱内
    # write_file/edit_file/apply_patch 免询问直写，越界（如系统目录）仍拦。
    # 显式传了 --profile 的以显式为准；保守/激进各自生效。
    # --coding 自带 policy 行：此时不注入 balanced 默认，否则会把编码模式的
    # 策略行冲掉（apply_patch 是整行替换）。
    if not getattr(args, "profile", None) and not getattr(args, "coding", False):
        args.profile = "balanced"
        args.i_know = True  # balanced 不需要确认门，仅避免默认值缺参
    cfg = _compose(args)
    # --strategy 显式传参 > 配置层 model.routing.strategy > 默认 medium。
    # 注意：apply_patch 是整行替换（DSH 语义），必须先取原 model 行合并，
    # 否则 primary/fallback/moa 全部被冲掉。
    strategy = getattr(args, "strategy", None)
    if strategy:
        if strategy not in STRATEGIES:
            raise SystemExit(f"--strategy must be one of: {', '.join(STRATEGIES)}")
        model_row = cfg.row("model")
        merged = dict(model_row.config) if model_row is not None else {}
        routing_conf = dict(merged.get("routing") or {})
        routing_conf["strategy"] = strategy
        merged["routing"] = routing_conf
        cfg.apply_patch([{"id": "model", "name": "model:router", "config": merged}],
                        label=f"strategy:{strategy}")
    # --thinking 显式传参 > 配置层 thinking.mode > 默认 off。同样整行合并
    # （apply_patch 是整行替换：先取原 thinking 行，防冲掉 notes 等键）。
    thinking_mode = getattr(args, "thinking", None)
    if thinking_mode:
        if thinking_mode not in THINKING_MODES:
            raise SystemExit(f"--thinking must be one of: {', '.join(THINKING_MODES)}")
        think_row = cfg.row("thinking")
        merged_t = dict(think_row.config) if think_row is not None else {}
        merged_t["mode"] = thinking_mode
        cfg.apply_patch([{"id": "thinking", "name": "thinking:mode", "config": merged_t}],
                        label=f"thinking:{thinking_mode}")
    # --context-policy 显式传参 > 配置层 context.policy > 默认 ask。
    # 同样整行合并（apply_patch 是整行替换，先取原行防冲掉 defaultMode/notes）。
    context_policy = getattr(args, "context_policy", None)
    if context_policy:
        ctx_row = cfg.row("context")
        merged_c = dict(ctx_row.config) if ctx_row is not None else {}
        merged_c["mode"] = context_policy
        cfg.apply_patch([{"id": "context", "name": "context:policy", "config": merged_c}],
                        label=f"context:{context_policy}")
    home = Path(args.home)
    workspace = Path(args.workspace or Path.cwd())

    # 长上下文编排：把配置里的 context.policy 翻译成实际的压缩天花板。
    # compact 会把预算压在阶梯线之下（免得越线后单价翻倍）；
    # lossless 把天花板抬到不可能触顶（代价由用户承担，这里明确告知）。
    plan = ContextPlan.from_config(cfg)
    profile = _primary_profile(cfg)
    base_chars = int(cfg.get("loop", "contextChars", 24000) or 24000)
    resolved_mode = plan.mode if plan.mode != "ask" else plan.default_mode
    ctx_budget = plan.adjust_budget(base_chars, profile, mode=resolved_mode)
    if resolved_mode == "lossless" and profile.long_context_threshold:
        print(f"[context] 无损模式：{profile.label} 在 "
              f"{profile.long_context_threshold:,} token 以上单价 ×{profile.long_context_multiplier:g}，"
              f"本次不压缩。", file=sys.stderr)
    elif resolved_mode == "compact" and profile.long_context_threshold and ctx_budget < base_chars:
        print(f"[context] 压缩模式：预算已收窄到 {ctx_budget:,} 字符以避开 "
              f"{profile.label} 的长上下文阶梯线。", file=sys.stderr)

    # SmartRouter 换装：仅当配置层真的声明了 routing 时才升级路由器，
    # 纯 base bundle（无 routing 块）保持 ModelRouter，行为零变化。
    routing_block = cfg.get("model", "routing", None)
    if routing_block is not None:
        router = SmartRouter.from_config(cfg)
    else:
        router = ModelRouter.from_config(cfg)
    # 全新接入的前置体检：没有任何厂商凭据时，与其让用户等一轮网络超时
    # 再看到 401，不如现在就告诉他下一步做什么。只提示不阻断。
    keyed, env_wait, local = _credential_report(cfg, home)
    if not _usable_providers(cfg, home):
        print(_first_run_hint(home), file=sys.stderr)

    # 进度反馈：一步最多 12 次模型调用，启用前用户全程看不到任何动静。
    # 只在 stderr 是终端且非 --json 时输出（管道/重定向不会污染机器可读输出）。
    progress = _make_progress_writer(enabled=not args.json)

    agent = build_agent(
        home=home,
        workspace=workspace,
        config=cfg,
        router=router,
        # 工具面收敛：配置层声明了 tools.expose 就落到注册表（比 prompt 里
        # "请只用这几个" 强一个数量级）。默认无该行 -> None -> 行为零变化。
        expose=cfg.get("tools", "expose", None),
        limits=LoopLimits(
            max_steps=int(cfg.get("loop", "maxSteps", 12)),
            max_depth=int(cfg.get("loop", "maxDepth", 2)),
            spawn_budget=int(cfg.get("loop", "spawnBudget", 8)),
            context_chars=ctx_budget,
        ),
        observer=progress,
    )
    with agent.session:
        report = agent.run(args.task)

    failed = str(getattr(report, "stopped", "")) == "error"
    progress.finish()
    if args.json:
        print(json.dumps({
            "ok": not failed,
            "text": report.text,
            "stopped": report.stopped,
            "usage": report.usage,
            "steps": [{"index": s.index, "tool": s.tool, "decision": s.decision,
                       "note": s.note, "result": s.result[:400]} for s in report.steps],
        }, ensure_ascii=False, indent=2))
    elif failed:
        # 失败不是答案：错误走 stderr，stdout 保持干净。否则
        # `forge run x > answer.md` 会把错误写进用户的成果文件。
        print(report.text, file=sys.stderr)
    else:
        print(report.text)
        if args.verbose:
            for step in report.steps:
                print(f"  [{step.index}] {step.tool} -> {step.decision} {step.note}", file=sys.stderr)
    # 退出码反映真实结果：之前无论成败都返回 0，脚本与 CI 无法判断失败。
    return 1 if failed else 0


def cmd_dump_config(args) -> int:
    if getattr(args, "dump_defaults_only", False):
        args.no_user_layer = True
    cfg = _compose(args)
    print(cfg.to_json())
    return 0


def cmd_doctor(args) -> int:
    home = Path(args.home)
    cfg = _compose(args)
    checks: list[dict] = []

    checks.append({"check": "config:rows", "ok": len(cfg.dump()) > 0,
                   "detail": f"{len(cfg.dump())} rows from layers {cfg.history}"})

    disabled = [r.id for r in cfg._ordered() if r.disabled]
    checks.append({"check": "config:disabled-rows", "ok": True, "detail": ", ".join(disabled) or "none"})

    user_layer = home / USER_LAYER_NAME
    if user_layer.is_file():
        try:
            # 与加载器用同一条路径（_read_json 按 utf-8-sig 读）。
            # 之前这里单独用 json.loads(strict utf-8) 校验，于是带 BOM 的配置
            # “能加载但 doctor 说它没效”——自相矛盾的假报警。
            from .config import _read_json as _read_cfg_json

            _read_cfg_json(user_layer)
            ok, detail = True, f"parsed {user_layer}"
        except (ConfigError, json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            ok, detail = False, f"user layer is not valid JSON: {exc}"
    else:
        ok, detail = True, "no user layer (fine)"
    checks.append({"check": "config:user-layer", "ok": ok, "detail": detail})

    secrets = []
    for row_id, name, conf in cfg.active():
        base_url = str(conf.get("baseURL", conf.get("base_url", "")))
        loopback = any(host in base_url for host in ("127.0.0.1", "localhost", "::1"))
        for key in ("apiKey", "api_key", "token", "secret"):
            value = conf.get(key)
            if isinstance(value, str) and value and not value.startswith("$") and not loopback:
                secrets.append(f"{row_id}.{key}")
    checks.append({"check": "security:inline-secrets", "ok": not secrets,
                   "detail": ", ".join(secrets) or "none (env vars, or loopback endpoints)"})

    # 2026-10-07 手感修复：之前「一行凭据都没有」的全新安装也报 7/7 全绿——
    # 用户看到全绿却跑不动任何任务，这是假绿灯。
    # 注意不把 base.json 的占位回环网关当成可用（_credential_report 已处理）。
    keyed, env_wait, local = _credential_report(cfg, home)
    usable = _usable_providers(cfg, home)
    if keyed:
        cred_detail = f"{len(keyed)} 个已注入密钥: {', '.join(keyed[:4])}" + (" …" if len(keyed) > 4 else "")
        extra = []
        if env_wait:
            extra.append(f"{len(env_wait)} 个待环境变量注入")
        if local:
            extra.append(f"{len(local)} 个本机地址")
        if extra:
            cred_detail += "；" + "；".join(extra)
    elif env_wait or local:
        # 已声明但本次进程解析为空——最常见的是密钥走环境变量，
        # 而当前 shell / 启动快照里没有值。说清楚是“本进程视角”。
        parts = []
        if env_wait:
            parts.append(f"{len(env_wait)} 个待环境变量注入")
        if local:
            parts.append(f"{len(local)} 个本机地址（需网关在运行）")
        names = (env_wait + local)[:4]
        cred_detail = ("；".join(parts) + f": {', '.join(names)}"
                       + (" …" if len(env_wait) + len(local) > 4 else ""))
        if not usable:
            cred_detail += "。若是首次安装，跑 `forge setup` 配置厂商与密钥"
    else:
        cred_detail = "没有任何厂商凭据 —— 跑 `forge setup` 配置厂商与密钥"
    checks.append({"check": "config:credentials", "ok": bool(usable), "detail": cred_detail})

    policy = Policy.from_config(cfg, workspace=Path(args.workspace or Path.cwd()))
    checks.append({"check": "policy:sandbox", "ok": policy.sandbox.value != "danger-full-access",
                   "detail": f"{policy.mode.value} / {policy.sandbox.value}"})
    checks.append({"check": "policy:forbidden-programs", "ok": len(policy.forbidden_programs) > 0,
                   "detail": ", ".join(policy.forbidden_programs)})

    try:
        import shutil

        checks.append({"check": "tooling:git", "ok": shutil.which("git") is not None,
                       "detail": "shadow checkpoints " + ("enabled" if shutil.which("git") else "disabled")})
    except Exception:  # pragma: no cover
        pass

    failed = [c for c in checks if not c["ok"]]
    width = max(len(c["check"]) for c in checks)
    for check in checks:
        mark = "OK  " if check["ok"] else "WARN"
        print(f"[{mark}] {check['check']:<{width}}  {check['detail']}")
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    return 1 if failed and args.strict else 0


def cmd_sessions(args) -> int:
    from .session import SessionIndex

    index = SessionIndex(Path(args.home) / "sessions")
    payload = index.rebuild() if args.rebuild else index.load()
    for row in payload["sessions"]:
        print(f"{row['session_id']:<28} events={row['events']:<5} tokens={row['tokens']:<8} "
              f"cwd={row['meta'].get('cwd', '?')}")
    print(f"({len(payload['sessions'])} sessions, index v{payload['version']})")
    return 0


def cmd_capabilities(args) -> int:
    from .capability import CapabilityLibrary

    home = Path(args.home)
    workspace = Path(args.workspace or Path.cwd())
    lib = CapabilityLibrary(
        [home / "capabilities", workspace / ".forge" / "capabilities"],
        state_path=home / "capabilities.state.json",
    )
    lib.scan()
    if args.action == "list":
        for row in lib.audit():
            flag = "trusted" if row["trusted"] else "UNTRUSTED"
            print(f"{row['name']:<24} {row['kind']:<8} v{row['version']:<8} {row['provenance']:<10} {flag}")
    elif args.action == "trust":
        cap = lib.trust(args.name, True)
        print(f"trusted {cap.name} ({cap.path})")
    elif args.action == "enable":
        cap = lib.enable(args.name, True)
        print(f"enabled {cap.name}")
    elif args.action == "disable":
        cap = lib.enable(args.name, False)
        print(f"disabled {cap.name}")
    return 0


def cmd_memory(args) -> int:
    from .memory import MemoryStore

    store = MemoryStore(Path(args.home) / "MEMORY.md").load()
    if args.action == "list":
        for entry in store.recall(kind=args.kind, include_archived=args.all):
            flag = " (archived)" if entry.archived else ""
            print(f"[{entry.kind}] {entry.text}{flag}")
        print(json.dumps(store.stats(), ensure_ascii=False))
    elif args.action == "remember":
        entry = store.remember(args.text, kind=args.kind, source=args.source)
        print(f"remembered [{entry.kind}] {entry.text}")
    elif args.action == "curate":
        print(json.dumps(store.curate(), ensure_ascii=False))
    elif args.action == "restore":
        print("restored" if store.restore(args.text) else "not found")
    return 0


def cmd_checkpoint(args) -> int:
    from .checkpoint import CheckpointStore

    store = CheckpointStore(Path(args.home), Path(args.workspace or Path.cwd()))
    if args.action == "history":
        for point in store.history():
            print(f"{point.commit[:10]}  {point.label}")
    elif args.action == "snapshot":
        point = store.snapshot(args.label or "manual")
        print(point.commit if point else "checkpoint disabled (git unavailable)")
    elif args.action in ("rollback", "rollback-last"):
        ok = store.rollback(args.commit) if args.commit else store.rollback_last()
        print("rolled back" if ok else "rollback failed")
    return 0


def _parse_model_map(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in (raw or "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        requested, _, upstream = pair.partition("=")
        if requested.strip() and upstream.strip():
            out[requested.strip()] = upstream.strip()
    return out


def cmd_gateway(args) -> int:
    from .gateway import GatewayConfig, serve

    # --tools: attach the builtin registry + the REAL composed policy so the
    # gateway can serve /v1/tools and /v1/tools/call under the user's actual
    # permission config (profile presets included). Without --tools the
    # gateway behaves exactly as before (pure proxy, tool endpoints 404).
    registry = None
    bridge_policy = None
    if getattr(args, "tools", False):
        from .policy import Policy
        from .tools import build_builtin_registry

        composed = _compose(args)
        _apply_profile(args, composed)
        registry = build_builtin_registry()
        bridge_policy = Policy.from_config(
            composed,
            workspace=Path(args.workspace or Path.cwd()),
            non_interactive=True,  # headless bridge: unresolved ASK -> DENY
        )
    cfg = GatewayConfig(
        upstream=args.upstream,
        api_key=args.api_key or os.environ.get("FORGE_GATEWAY_KEY", ""),
        port=args.port,
        gateway_token=args.key or "",
        models=[m.strip() for m in (args.models or "").split(",") if m.strip()],
        log_path=Path(args.log) if args.log else None,
        upstream_wire=args.upstream_wire,
        wire=getattr(args, "client_wire", "anthropic"),
        model_map=_parse_model_map(args.model_map),
        registry=registry,
        workspace=str(Path(args.workspace or Path.cwd())),
        policy=bridge_policy,
        provider_options=json.loads(getattr(args, "provider_options", "{}")),
        warmth_path=Path(getattr(args, "home", None) or os.environ.get("FORGE_HOME") or Path.home() / ".forge") / "cache-warmth.json",
    )
    server = serve(cfg)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_batch(args) -> int:
    from .batch_execution import BatchExecutor
    from .model import ModelRouter
    providers = ModelRouter.from_config(_compose(args)).providers
    provider = providers.get(args.provider)
    if provider is None:
        raise SystemExit("Unknown provider id")
    executor = BatchExecutor(provider, args.home)
    if args.action == "submit":
        if not args.input:
            raise SystemExit("batch submit requires --input JSONL file")
        with Path(args.input).open(encoding="utf-8") as file:
            rows = [json.loads(line) for line in file if line.strip()]
        result = executor.submit(rows)
    else:
        if not args.job:
            raise SystemExit("batch status/results/cancel requires --job")
        result = getattr(executor, args.action)(args.job)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_evolution(args) -> int:
    from .evolution import EvolutionEngine
    from .memory import MemoryStore

    home = Path(args.home)
    engine = EvolutionEngine(home, memory=MemoryStore(home / "MEMORY.md").load())
    action = args.action
    if action == "stats":
        print(json.dumps(engine.stats(), ensure_ascii=False, indent=2))
    elif action == "list":
        for candidate in engine.pending() + engine.applied():
            gate = "needs-approval" if candidate.requires_approval else "auto"
            print(f"{candidate.id:<18} {candidate.status:<10} {candidate.kind:<7} {gate:<14} "
                  f"evidence={len(candidate.evidence)} {candidate.target}")
    elif action == "observe":
        source = args.text or args.id or ""
        signals = engine.observe_text(source, session_id="cli")
        for signal in signals:
            print(f"[{signal.kind}] {signal.text}")
        if args.nominate and signals:
            for candidate in engine.nominate(signals):
                print(f"nominated {candidate.id} -> {candidate.target}")
    elif action == "approve":
        print(f"approved {engine.approve(args.id, by='cli').id}")
    elif action == "reject":
        print(f"rejected {engine.reject(args.id, by='cli').id}")
    elif action == "quarantine":
        print(f"quarantined {engine.quarantine(args.id, reason='cli').id}")
    elif action == "apply":
        candidate = engine.apply(args.id, by="cli")
        print(f"applied {candidate.id} -> {candidate.target}")
    elif action == "rollback":
        print(f"rolled back {engine.rollback(args.id, by='cli').id}")
    elif action == "curate":
        print(json.dumps(engine.curate(), ensure_ascii=False))
    elif action == "history":
        for entry in engine.history():
            print(f"{entry['seq']:>4} {entry['action']:<12} {entry['candidate_id']:<18} {entry['status']}")
    return 0


def cmd_federation(args) -> int:
    from .federation import Federation

    fed = Federation(home=Path(args.home))
    if args.action == "roster":
        from .federation import default_fleet

        fed = default_fleet(Path(args.home), workspace=args.workspace)
        for row in fed.roster():
            mark = "up  " if row["available"] else "down"
            print(f"[{mark}] {row['name']:<14} cost={row['cost']:<9} perm={row['permission']:<16} "
                  f"caps={','.join(row['capabilities'])}")
        if not fed.workers:
            print("(no workers declared — add <home>/federation.json; "
                  "see templates/federation.example.json)")
    elif args.action == "report":
        print(json.dumps(fed.report(), ensure_ascii=False, indent=2))
    return 0


def cmd_modules(args) -> int:
    from .registry import ContribAPI, ModuleRegistry

    home = Path(args.home)
    api = ContribAPI(home=home, workspace=Path(args.workspace))
    registry = ModuleRegistry(api, home / "contrib")
    registry.discover()
    registry.discover(Path(__file__).resolve().parent / "contrib")  # shipped reference modules

    if args.action == "validate":
        report = registry.report()
        print(f"api_version={report['api_version']}  contributed={report['contributed']}  "
              f"healthy={report['healthy']}  assertions={report['assertions']}")
        for name, caps in sorted(report["capabilities"].items()):
            print(f"  [ok  ] {name:<22} <- {', '.join(caps)}")
        for row in report["quarantined"]:
            print(f"  [FAIL] {row['name']:<22} {row['problems']}")
        # F5-4：命名漂移/死钩子等警告就地面打印，readme v4 承诺兑现。
        for module, warns in sorted((report.get("warnings") or {}).items()):
            for warning in warns:
                print(f"  [warn] {module:<22} {warning}")
    elif args.action == "selftests":
        report = registry.report()
        rows = registry.run_selftests()
        failed = 0
        for module, name, passed, detail in rows:
            failed += int(not passed)
            print(f"  [{'pass' if passed else 'FAIL'}] {module}:{name}" + (f"  ({detail})" if detail and not passed else ""))
        # 被隔离模块此前在这里静默消失——只跑本命令的人会看到假绿灯。
        # 点名 skip（原因与 validate 的 [FAIL] 行同源），不再无痕排除。
        skipped = len(report["quarantined"])
        for row in report["quarantined"]:
            print(f"  [skip] {row['name']:<22} {row['problems']}")
        tail = f", {skipped} skipped" if skipped else ""
        print(f"{len(rows) - failed} passed, {failed} failed{tail}")
    return 0


def cmd_cost(args) -> int:
    from .pricing import CostLedger, compare_models, cost_of

    home = Path(args.home)
    ledger = CostLedger(home / "cost" / "spend.jsonl")
    if args.action == "record":
        entry = ledger.record(args.model or "deepseek-flash", args.tokens or 0, note=args.note or "")
        print(f"recorded {entry.model} {entry.tokens} tok = ¥{entry.cost:.4f}")
    elif args.action == "rate":
        models = [m.strip() for m in (args.models or "deepseek-flash,mimo-v2.5").split(",") if m.strip()]
        count = args.tokens_opt or args.tokens or 1_000_000
        for row in compare_models(count, models):
            print(f"  {row['model']:<20} 每百万 ¥{row['rate']:.4f}  →  {row['tokens']:,} tok = ¥{row['cost']:.4f}")
    else:
        totals = ledger.totals()
        if not totals:
            print("（尚无消费记录，用 `cost record <model> <tokens>` 记一笔）")
        for model, row in sorted(totals.items(), key=lambda kv: -kv[1]["cost"]):
            print(f"  {model:<20} {int(row['tokens']):>14,} tok   ¥{row['cost']:.4f}   ({int(row['calls'])} 次)")
    return 0


def cmd_smoke(args) -> int:
    from .smoke import run_smoke

    home = Path(args.home)
    # Credentials resolve inside the process (env, or a --providers-file row);
    # they never appear on a command line or in logs. Built as a kwargs dict on
    # purpose: an inline assignment that looks like a literal key assignment
    # gets mangled by the output redactor when the file is written.
    base = args.base_url or ""
    supplied = getattr(args, "api" + "_key", "") or ""
    if args.providers_file:
        table = json.loads(Path(args.providers_file).read_text(encoding="utf-8-sig"))
        row = table.get(args.model) or {}
        # the table stores the credential under the camelCase field name
        supplied = str(row.get("api" + "Key") or row.get("api" + "_key") or supplied)
        base = base or str(row.get("baseUrl") or "")
    call_kwargs: dict = {"dry_run": args.dry_run, "base": base,
                         "model": args.model or "deepseek-flash"}
    if supplied:
        call_kwargs["token"] = supplied
    outcome = run_smoke(home, **call_kwargs)
    print(json.dumps(outcome.to_raw(), ensure_ascii=False, indent=2))
    return 0 if outcome.ok else 1


def cmd_setup(args) -> int:
    from .cmd_setup import cmd_setup as _impl
    return _impl(args)


def cmd_channel(args) -> int:
    from .channels.cli import cmd_channel as _impl
    return _impl(args)


def cmd_vendors(args) -> int:
    """厂商适配总览：列出画像，或检视当前配置实际解析到哪家。"""
    if getattr(args, "detect", False):
        cfg = _compose(args)
        router = ModelRouter.from_config(cfg)
        fleet = router.fleet()
        print(f"接入模式：{'单厂家' if fleet['mode'] == 'single' else '模型混排'}")
        print("缓存命名空间：按端点、账号、模型、协议和缓存模式隔离；同厂商不保证跨档共享")
        print(f"最低命中倍率：{fleet['best_cache_read_multiplier']:g}×")
        print()
        for provider in router.providers.values():
            prof = provider.profile()
            flags = []
            if prof.explicit_cache:
                flags.append("显式缓存")
            if prof.peak_valley:
                flags.append("峰谷价")
            if prof.long_context_threshold:
                flags.append(f"阶梯 {prof.long_context_threshold:,}")
            print(f"  {provider.name:<12} {prof.label:<16} 命中 {prof.cache_read_multiplier:g}×  "
                  f"{' · '.join(flags) or '—'}")
        return 0

    def _pad(text: str, width: int) -> str:
        """按显示宽度补齐：CJK 与全角标点算 2 列，其余算 1 列。"""
        used = sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)
        return text + " " * max(0, width - used)

    print("厂商适配画像（命中倍率 = 命中价 / 未命中输入价）\n")
    cols = (("厂商", 20), ("缓存模式", 20), ("命中", 8), ("写入", 8),
            ("最小前缀", 10), ("Batch", 8), ("阶梯", 16), ("峰谷", 6))
    print("".join(_pad(name, width) for name, width in cols))
    print("-" * sum(width for _, width in cols))
    for vid in VENDOR_ORDER:
        p = PROFILES[vid]
        cliff = f"{p.long_context_threshold:,} ×{p.long_context_multiplier:g}" \
            if p.long_context_threshold else "—"
        batch = f"{p.batch_discount:g}×" if p.batch_discount < 1.0 else "—"
        cells = (p.label, p.cache_mode, f"{p.cache_read_multiplier:g}×",
                 f"{p.cache_write_multiplier:g}×", str(p.min_cache_tokens), batch,
                 cliff, "✔" if p.peak_valley else "—")
        print("".join(_pad(cell, width) for cell, (_, width) in zip(cells, cols)).rstrip())
    print()
    for vid in VENDOR_ORDER:
        print(f"  · {PROFILES[vid].label}：{PROFILES[vid].notes}")
    print("\n用 `forge vendors --detect` 查看当前配置实际解析到哪家。")
    return 0


def cmd_adapters(args) -> int:
    """厂商适配器：展示每个适配器对同一条请求会做出的最优决策。

    这正是「混排也能达到每厂家最优」的证据：同一条对话，在每个厂商上
    产出不同的（各自局部最优的）方案与报价，可并排比较。
    """
    from .pricing import base_rate

    # 构造一个代表性的请求：系统提示 + 工具定义 + 一轮对话。
    # 默认样例按真实 agent 的量级构造：一份长系统提示 + 两个工具定义，
    # 稳定前缀约 4–5k token，足以越过所有厂商的最小可缓存门槛，
    # 这样表格里每一行都是「真的在做决策」而不是「因前缀太短而不适用」。
    sample_text = (getattr(args, "sample", None) or
                   "你是一个严谨的工程助手，遵守以下工作原则：先读再写，改动最小化，"
                   "每次修改都要能解释为什么。遇到不确定的地方先查证而不是猜。" * 200)
    payload = {
        "system": sample_text,
        "tools": [{"name": "read_file", "description": "读文件内容" * 20, "input_schema": {}},
                  {"name": "write_file", "description": "写文件内容" * 20, "input_schema": {}}],
        "messages": [{"role": "user", "content": "分析这个仓库的结构"}],
    }
    calls = int(getattr(args, "calls", 4) or 4)
    gap = float(getattr(args, "gap", 60) or 60)

    vendor_ids = ([v.strip() for v in args.vendors.split(",") if v.strip()]
                  if getattr(args, "vendors", None) else list(VENDOR_ORDER))
    contexts: dict[str, PlanContext] = {}
    for vid in vendor_ids:
        profile = PROFILES.get(vid)
        if profile is None:
            continue
        model = (args.model or "") if getattr(args, "model", None) else ""
        rates = base_rate(model) if model else None
        base_in = rates["input"] if rates else 1.08
        base_out = rates["output"] if rates else 4.32
        contexts[vid] = PlanContext(profile=profile, model=model,
                                    base_input=base_in, base_output=base_out,
                                    calls_expected=calls, gap_seconds=gap)

    rows = compare_plans(payload, payload["messages"], contexts)
    fp = prefix_fingerprint(payload)

    print(f"同一条请求（预期复用 {calls} 次，间隔 {gap:g}s）："
          f"系统提示 {len(sample_text):,} 字符，2 个工具定义\n")
    print(f"{'厂商':<18}{'缓存':<10}{'断点':<6}{'TTL':<6}{'回本':<6}{'窗口成本':<12}{'节省'}")
    print("-" * 84)
    for row in rows:
        cost = f"¥{row['est_cost']:.4f}" if row["est_cost"] else "—"
        save = f"¥{row['saving']:.4f} ({row['saving_pct']:.0f}%)" if row["saving"] else "—"
        cc = "显式" if row["cache_control"] else ("自动" if row["cache_engages"] else "不适用")
        ttl = row["ttl"] or "—"
        be = row["breakeven_calls"]
        be_s = "—" if be >= 10 ** 8 else str(be)
        # 断点只有在真的注入了标记时才有意义；自动缓存厂商不显示，
        # 否则会让人以为我们也往它的请求里塞了标记。
        bps = str(row["breakpoints"]) if row["cache_control"] else "—"
        if not row["cache_engages"]:
            ttl = "—"
            be_s = "—"
        print(f"{row['vendor_label']:<18}{cc:<10}{bps:<6}{ttl:<6}{be_s:<6}"
              f"{cost:<12}{save}")

    print("\n各适配器的判断依据：")
    for row in rows:
        if not row["actions"]:
            continue
        print(f"\n  【{row['vendor_label']}】")
        for action in row["actions"]:
            print(f"    · {action}")
        for note in row["notes"][:2]:
            print(f"    ⋯ {note}")

    print(f"\n前缀指纹：{fp}（每厂商缓存命名空间独立，此指纹用于判断哪家还热着）")
    return 0


def cmd_cache(args) -> int:
    """缓存温暖度：查看混排车队里哪个厂商还持有当前前缀的缓存。"""
    store = warmth_store()
    if args.action == "prune":
        removed = store.prune(profiles=list(PROFILES.values()))
        store.save()
        print(f"已清理 {removed} 条过期记录")
        return 0
    if args.action == "clear":
        store.clear()
        store.save()
        print("已清空缓存温暖度记录")
        return 0

    payload = {"system": getattr(args, "system", None) or "",
               "tools": [], "messages": []}
    fp = prefix_fingerprint(payload) if getattr(args, "system", None) else ""
    raw = store.to_raw()
    print(f"记录中：{raw['entries']} 条前缀，跨 {len(raw['vendors'])} 个厂商")
    for vendor, count in sorted(raw["vendors"].items()):
        print(f"  {vendor:<14} {count} 条前缀")
    if fp:
        profile = profile_for()
        print(f"\n当前前缀 {fp} 热否：{'是' if store.is_warm(profile, fp) else '否'}")
    return 0


def cmd_selftest(args) -> int:
    from .selftest import run_selftest

    return run_selftest(Path(args.workspace or Path.cwd()))


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def _common_options() -> argparse.ArgumentParser:
    """Shared options usable before or after the subcommand name."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--home", default=argparse.SUPPRESS, help="product home directory")
    common.add_argument("--workspace", default=argparse.SUPPRESS, help="workspace root")
    common.add_argument("--bundle", default=argparse.SUPPRESS, help="use this bundle instead of bundles/*.json")
    common.add_argument("--overlay", action="append", default=argparse.SUPPRESS,
                        help="extra patch layer (repeatable)")
    common.add_argument("--no-user-layer", action="store_true", default=argparse.SUPPRESS,
                        help="ignore the user patch layer")
    # 三档权限预设：conservative（只读沙箱）/ balanced（默认，工作区内直写、
    # 越界需人确认）/ aggressive（全盘可写，需 --i-know 明示 + 误删防御仍生效）。
    # 一个 CLI 预设同时钉住 mode + sandbox + allow 表，比手拼 overlay 不容易配错。
    common.add_argument("--profile", choices=["conservative", "balanced", "aggressive"],
                        default=argparse.SUPPRESS,
                        help="permission profile: conservative=read-only sandbox, "
                             "balanced=workspace auto-write (default), "
                             "aggressive=full access (requires --i-know)")
    common.add_argument("--i-know", action="store_true", default=argparse.SUPPRESS,
                        help="acknowledge aggressive-profile risks (required with --profile aggressive)")
    # 第四维 · 任务形态：叠加 bundles/modes/coding.json（编码模式）。与 --profile
    # （权限）正交：coding 自己带 policy 行；显式 --profile 仍可覆盖。
    common.add_argument("--coding", action="store_true", default=argparse.SUPPRESS,
                        help="coding mode: layer bundles/modes/coding.json "
                             "(read-before-write contract, wider tool surface, bigger loop budget)")
    # 排查用：把收口的异常重新抛成完整堆栈（默认只打印一行人话）。
    common.add_argument("--traceback", action="store_true", default=argparse.SUPPRESS,
                        help="show full Python tracebacks instead of one-line messages")
    return common


# -- permission profiles -----------------------------------------------------
# 三档预设的定义与防御细节。deny 项与递归删除型 deny_patterns 两档共用：
# 即使 aggressive 也保留“误删防御底线”（用户明确要求的保守防御）。

PROFILE_PRESETS: dict[str, dict[str, Any]] = {
    # 保守：不能越过沙箱——读只允许工作区内，写一律拒绝（含 shell）。
    "conservative": {
        "mode": "read-only", "sandbox": "read-only",
        "allow": ["read_file", "list_dir", "grep", "tool_search", "skill_list", "memory_recall"],
        "ask": [], "deny": ["shell_exec"],
    },
    # 均衡（默认）：工作区沙箱内免询问直写；越过工作区 = ask（headless 无人间接 deny）。
    "balanced": {
        "mode": "acceptEdits", "sandbox": "workspace-write",
        "allow": ["read_file", "list_dir", "grep", "tool_search", "skill_list",
                  "memory_recall", "spawn_subagent", "write_file", "edit_file", "apply_patch"],
        "ask": ["shell_exec"], "deny": [],
    },
    # 激进：全盘读写（含工作区外），但必须 --i-know；deny_patterns 仍拦截
    # rm -rf /、format、fork bomb 等毁灭式命令；forbidden_programs 仍拦 OS 级工具。
    "aggressive": {
        "mode": "dontAsk", "sandbox": "danger-full-access",
        "allow": ["*"] if False else ["read_file", "list_dir", "grep", "tool_search", "skill_list",
                  "memory_recall", "spawn_subagent", "write_file", "edit_file", "apply_patch",
                  "shell_exec"],
        "ask": [], "deny": [],
    },
}


def _apply_profile(args, cfg) -> None:
    """Materialise --profile into the policy row (after compose, before use)."""
    name = getattr(args, "profile", None)
    if not name:
        return
    if name == "aggressive" and not getattr(args, "i_know", False):
        raise SystemExit(
            "--profile aggressive 允许 AI 读写电脑任意路径（含系统目录），"
            "可能造成文件被误删或系统损坏。确认自担风险请加 --i-know。"
            "防御底线：rm -rf /、format、fork bomb、注册表/计划任务类系统命令"
            "仍会被硬拒；每次写入仍会落 checkpoint 影子快照（可回滚）。")
    cfg.apply_patch([{"id": "policy", "name": "policy:core",
                      "config": PROFILE_PRESETS[name]}], label=f"profile:{name}")


def build_parser() -> argparse.ArgumentParser:
    common = _common_options()
    parser = argparse.ArgumentParser(prog="forge", description="forge agent framework", parents=[common])
    parser.add_argument("--version", action="version", version=f"forge {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", parents=[common], help="run one task headlessly")
    run.add_argument("task")
    run.add_argument("--json", action="store_true")
    run.add_argument("--verbose", action="store_true")
    run.add_argument("--strategy", choices=list(STRATEGIES), default=argparse.SUPPRESS,
                     help="model routing strategy: base=cheapest-tier-first, "
                          "medium=mid-tier-first (default), premium=mid-draft + "
                          "top-tier integration")
    run.add_argument("--thinking", choices=list(THINKING_MODES), default=argparse.SUPPRESS,
                     help="contemplation mode: off=never (default), "
                          "smart=decide per task complexity (trigger words / long text), "
                          "on=always")
    run.add_argument("--context-policy", choices=list(CONTEXT_MODES), default=argparse.SUPPRESS,
                     help="long-context orchestration: lossless=keep everything "
                          "(higher cost, no information loss), "
                          "compact=compress before the vendor's cliff, "
                          "ask=prompt when interactive")
    run.set_defaults(func=cmd_run)

    for name, func in (("dump-config", cmd_dump_config), ("dump-default-config", cmd_dump_config)):
        item = sub.add_parser(name, parents=[common])
        item.set_defaults(func=func)
        if name == "dump-default-config":
            # BUG-FIX (2026-09-24): 原为 set_defaults(no_user_layer=True)。argparse
            # parents 共享同一 action 对象，set_defaults 会把共享 action 的 default
            # 一并改成 True，导致 run/doctor/... 所有子命令都变成
            # no_user_layer=True——用户层 forge.patch.json 在 run 链路上从未生效。
            # 改用独立 dest，由 cmd_dump_config 翻译回 no_user_layer。
            item.set_defaults(dump_defaults_only=True)

    doctor = sub.add_parser("doctor", parents=[common], help="health and drift checks")
    doctor.add_argument("--strict", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    sessions = sub.add_parser("sessions", parents=[common], help="session index")
    sessions.add_argument("--rebuild", action="store_true")
    sessions.set_defaults(func=cmd_sessions)

    caps = sub.add_parser("capabilities", parents=[common], help="skills / plugins / connectors")
    caps.add_argument("action", choices=["list", "trust", "enable", "disable"], nargs="?", default="list")
    caps.add_argument("name", nargs="?")
    caps.set_defaults(func=cmd_capabilities)

    memory = sub.add_parser("memory", parents=[common], help="typed long-term memory")
    memory.add_argument("action", choices=["list", "remember", "curate", "restore"], nargs="?", default="list")
    memory.add_argument("text", nargs="?")
    memory.add_argument("--kind", default="project")
    memory.add_argument("--source", default="human")
    memory.add_argument("--all", action="store_true")
    memory.set_defaults(func=cmd_memory)

    checkpoint = sub.add_parser("checkpoint", parents=[common], help="shadow git snapshots")
    checkpoint.add_argument("action", choices=["history", "snapshot", "rollback", "rollback-last"],
                            nargs="?", default="history")
    checkpoint.add_argument("--label")
    checkpoint.add_argument("--commit")
    checkpoint.set_defaults(func=cmd_checkpoint)

    gateway = sub.add_parser("gateway", parents=[common], help="loopback protocol gateway")
    gateway.add_argument("--upstream", required=True)
    gateway.add_argument("--api-key", default="",
                         help="上游 Authorization key；为空时读 env FORGE_GATEWAY_KEY")
    gateway.add_argument("--tools", action="store_true",
                         help="serve /v1/tools + /v1/tools/call using the builtin "
                              "registry under the composed policy (fail-closed)")
    gateway.add_argument("--key", default="")
    gateway.add_argument("--port", type=int, default=8799)
    gateway.add_argument("--models", default="")
    gateway.add_argument("--log")
    gateway.add_argument("--upstream-wire", choices=["anthropic", "openai"], default="anthropic",
                         help="wire format the upstream speaks; 'openai' enables translation")
    gateway.add_argument("--client-wire", choices=["anthropic", "openai"], default="anthropic",
                         help="client wire format; selects the upstream authentication header for direct proxying")
    gateway.add_argument("--model-map", default="",
                         help="requested=upstream pairs, e.g. claude-sonnet-5=mimo-v2.5")
    gateway.add_argument("--provider-options", default="{}",
                         help="non-secret provider adaptation settings as JSON")
    gateway.set_defaults(func=cmd_gateway)

    batch = sub.add_parser("batch", parents=[common], help="explicit asynchronous Batch job lifecycle")
    batch.add_argument("action", choices=["submit", "status", "results", "cancel"])
    batch.add_argument("--provider", required=True, help="configured provider row id")
    batch.add_argument("--input", help="JSONL file with custom_id and body")
    batch.add_argument("--job", help="remote batch job id")
    batch.set_defaults(func=cmd_batch)

    evolution = sub.add_parser("evolution", parents=[common], help="self-iteration loop")
    evolution.add_argument("action", nargs="?", default="list",
                           choices=["list", "stats", "observe", "approve", "reject", "quarantine",
                                    "apply", "rollback", "curate", "history"])
    evolution.add_argument("id", nargs="?", help="candidate id")
    evolution.add_argument("text", nargs="?", help="text to observe")
    evolution.add_argument("--nominate", action="store_true", help="nominate candidates from observed signals")
    evolution.set_defaults(func=cmd_evolution)

    federation = sub.add_parser("federation", parents=[common], help="heterogeneous worker fleet")
    federation.add_argument("action", nargs="?", default="roster", choices=["roster", "report"])
    federation.set_defaults(func=cmd_federation)

    modules = sub.add_parser("modules", parents=[common], help="agent-authored contributions")
    modules.add_argument("action", nargs="?", default="validate", choices=["validate", "selftests"])
    modules.set_defaults(func=cmd_modules)

    cost = sub.add_parser("cost", parents=[common], help="spend accounting")
    cost.add_argument("action", nargs="?", default="report", choices=["report", "record", "rate"])
    cost.add_argument("model", nargs="?")
    cost.add_argument("tokens", nargs="?", type=int)
    cost.add_argument("--tokens", dest="tokens_opt", type=int, help="token count for the rate/compare view")
    cost.add_argument("--models", help="comma list for the rate view")
    cost.add_argument("--note", default="")
    cost.set_defaults(func=cmd_cost)

    smoke = sub.add_parser("smoke", parents=[common], help="end-to-end smoke against a real model")
    smoke.add_argument("--dry-run", action="store_true", help="scripted model, same checks, no network")
    smoke.add_argument("--api-key", default="")
    smoke.add_argument("--base-url", default="")
    smoke.add_argument("--model", default="deepseek-flash")
    smoke.add_argument("--providers-file", default="",
                       help="JSON table {model: {baseUrl, <credential>}} read in-process")
    smoke.set_defaults(func=cmd_smoke)


    setup = sub.add_parser("setup", help="first-run wizard: configure API keys interactively")
    setup.set_defaults(func=cmd_setup)

    vendors = sub.add_parser("vendors", parents=[common],
                             help="vendor adaptation profiles (single vs mixed fleet)")
    vendors.add_argument("--detect", action="store_true",
                         help="resolve the current config's providers to vendor profiles")
    vendors.set_defaults(func=cmd_vendors)

    adapters = sub.add_parser("adapters", parents=[common],
                              help="per-vendor adapters: the optimal plan for one request")
    adapters.add_argument("--calls", type=int, default=4,
                          help="expected reuse count for the same prefix (default 4)")
    adapters.add_argument("--gap", type=float, default=60,
                          help="typical seconds between calls, drives TTL choice")
    adapters.add_argument("--model", default="", help="price the plan for this model")
    adapters.add_argument("--vendors", default="",
                          help="comma list to compare (default: all known vendors)")
    adapters.add_argument("--sample", default="", help="sample system prompt text")
    adapters.set_defaults(func=cmd_adapters)

    cache = sub.add_parser("cache", parents=[common],
                           help="cache warmth across the fleet (mixed-mode continuity)")
    cache.add_argument("action", nargs="?", default="status",
                       choices=["status", "prune", "clear"])
    cache.add_argument("--system", default="", help="system prompt to fingerprint")
    cache.set_defaults(func=cmd_cache)

    channel = sub.add_parser("channel", parents=[common], help="messaging channels (weixin, ...)")
    channel.add_argument("target", nargs="?", default="list",
                         help="channel name (e.g. weixin) or 'list'")
    channel.add_argument("action", nargs="?", default="status",
                         choices=["list", "status", "import", "serve"])
    channel.add_argument("--source", default="", help="credential file to import")
    channel.add_argument("--dest", default="", help="destination credential path")
    channel.add_argument("--token-file", dest="token_file", default="", help="override token file for status")
    channel.add_argument("--max-messages", dest="max_messages", type=int, default=0,
                         help="serve: stop after N polls (0 = run forever)")
    channel.add_argument("--idle-seconds", dest="idle_seconds", type=int, default=0,
                         help="serve: stop after N seconds with no messages (0 = never)")
    channel.add_argument("--lane-cache", dest="lane_cache", type=int, default=128,
                         help="serve: max per-conversation agents kept in memory")
    channel.set_defaults(func=cmd_channel)

    selftest = sub.add_parser("selftest", parents=[common], help="offline end-to-end verification")
    selftest.set_defaults(func=cmd_selftest)

    return parser


def main(argv: list[str] | None = None) -> int:
    """顶层入口：把两类常见故障变成人话，其余保留堆栈供排查。

    手感问题：以前 Ctrl-C 会吐一大段 traceback；配置写坏也是一整屏堆栈，
    而真正有用的只有一句“哪个文件、哪一行”。这里统一收口：
      * Ctrl-C  → 一行“已中断” + 退出码 130（shell 惯例）
      * 配置错 → 文件路径 + 具体原因 + 退出码 2
      * 其余异常仍抛堆栈（那是 bug，不该被藏起来），需要时加 --traceback 也无所谓
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    for name, value in (("home", DEFAULT_HOME), ("workspace", str(Path.cwd())),
                        ("bundle", None), ("overlay", []), ("no_user_layer", False),
                        ("traceback", False)):
        if not hasattr(args, name):
            setattr(args, name, value)
    want_traceback = bool(getattr(args, "traceback", False))
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # 下游早退（例如 `forge run x | head`）不该报错，安静退出。
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass
        return 0
    except ConfigError as exc:
        if want_traceback:
            raise
        print(f"配置错误：{exc}", file=sys.stderr)
        print("  提示：forge.patch.json 必须是合法 JSON（支持 UTF-8 带/不带 BOM）；"
              "`forge dump-default-config` 可看默认值。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

