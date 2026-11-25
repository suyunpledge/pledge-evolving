"""Work team context, sharing the legacy sidecar engine without legacy widgets."""
import os
from pathlib import Path
from config_model import load_user_layer
from interaction_model import select_provider
from revon.knowledge import KnowledgeBase
from revon_client import GenerationCancelled
import secret_store
import sub_agent as team
from .store import SessionStore
from revon.compat import desktop_config


def work_context(home, messages, model, cancel, status):
    home = Path(home)
    prompt = next((m.content for m in reversed(messages) if m.role == 'user'), '')
    context = KnowledgeBase(home / 'MEMORY.md').context_for(prompt)
    cfg = team.load_config(home / 'agent-cluster.json')
    mode = SessionStore.read(desktop_config(home), {}).get('team_mode', 'off')
    hints = ('对比', '评测', '评估', '调研', '全面', '多角度', '多方案', '审查', '评审', '利弊', '架构', '选型', 'review', 'compare', 'trade-off')
    if mode == 'off' or mode == 'auto' and len(prompt) < 120 and not any(h in prompt for h in hints):
        return context
    rows = load_user_layer(home)
    provider = select_provider(rows, model)
    environment = {**os.environ, **secret_store.env_for(secret_store.load(path=home / 'secrets.json', strict=True))}
    shared = '\n'.join(f'{m.role}: {m.content[:400]}' for m in messages[-6:]) if cfg['memory_mode'] == 'unified' else None
    options = dict(default_provider=provider, default_model=(provider or {}).get('model', ''), cancel_event=cancel,
                   communication=cfg['communication']['enabled'], communication_rounds=cfg['communication']['rounds'])
    presets = team.enabled_sub_agent_presets(cfg)
    if presets:
        agents = team.build_sub_agents(presets, prompt, context_text=shared)
        for agent, preset in zip(agents, presets):
            agent.provider, agent.model_name = team.resolve_selection(rows, preset, provider)
        status(f'子 Agent 分工 · {len(agents)} 路')
        results = team.run_sub_agents(agents, environment, **options)
        injection = team.sub_agent_injection(results)
    elif cfg['cluster']['enabled']:
        lanes = []
        for lane in cfg['cluster']['lanes']:
            selected, mid = team.resolve_selection(rows, lane, provider)
            lanes.append({'provider': selected, 'model': mid})
        status(f'Agent 协作 · {cfg["cluster"]["count"]} 路')
        results = team.run_cluster(prompt, cfg['cluster']['count'], lanes, environment, context_text=shared, **options)
        injection = team.cluster_injection(results)
    else:
        return context
    if cancel.is_set():
        raise GenerationCancelled('已停止')
    status('协作完成 · ' + ' / '.join(r.role + (' 完成' if r.ok else ' 失败') for r in results))
    return context + '\n' + injection if any(r.ok for r in results) else context
