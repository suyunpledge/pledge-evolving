"""Configured planning/review providers, using Forge's existing vendor adapter."""
from forge.config import Config, resolve
from forge.model import ModelRouter, HttpTransport
from forge.phase_models import model_pair, phase_router
from forge.planning import TaskPlan, PlanningError, planning_prompt, token_cap
from forge.code_review import review_code
from forge_client import GenerationCancelled
from http_transport import open_response


def model_catalog(rows):
    entries = []
    for row in rows:
        conf = row.get('config') or {}
        if row.get('disabled') or not conf.get('baseURL'):
            continue
        models = [conf.get('model'), *(conf.get('models') or []), conf.get('smallModel')]
        for model in dict.fromkeys(v for v in models if isinstance(v, str) and v):
            pair = (str(row['id']), model)
            entries.append((pair, pair[0] + ' / ' + model))
    return entries


class PhaseClient:
    def __init__(self, rows, selection, environment, scope, cancel_event=None):
        pair = model_pair(selection)
        if pair is None:
            raise ValueError('Select an independent phase model first')
        row = next((r for r in rows if str(r.get('id')) == pair[0] and not r.get('disabled')), None)
        if row is None:
            raise ValueError('Phase provider is no longer enabled/configured')
        config = Config()
        # Expressions resolve in the trusted host, not in LLM context.
        conf = resolve(row.get('config') or {}, {'env': environment})
        config.apply_patch([{'id': pair[0], 'name': 'provider:phase', 'config': conf}])
        opener = lambda request, **kw: open_response(request, cancel_event=cancel_event, **kw)
        router = ModelRouter.from_config(config, transport=HttpTransport(timeout=60, response_opener=opener))
        self.router = phase_router(router, pair, router)
        self.router.retries_per_provider = 0  # one explicit manual request; don't wait in retry sleeps
        self.scope, self.cancel_event = scope, cancel_event

    def _call(self, callback):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise GenerationCancelled('已停止请求')
        try:
            result = callback()
        except Exception:
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise GenerationCancelled('已停止请求') from None
            raise
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise GenerationCancelled('已停止请求')
        return result

    def plan_task(self, messages, level, **_kwargs):
        if level == 'none': return None, None
        def request():
            contract = planning_prompt(level)
            inputs = [{'role': 'system', 'content': contract},
                      *[m.to_dict(self.scope) for m in messages if m.role in {'user', 'assistant'}]]
            result = self.router.complete(self.scope.protect(inputs), small=False, max_tokens=token_cap(level))
            if result.tool_calls: raise PlanningError('Planner attempted a tool call')
            return TaskPlan.parse(self.scope.protect_text(result.text), level), {
                'prompt_tokens': result.usage.prompt_tokens, 'completion_tokens': result.usage.completion_tokens,
                'requests': result.requests}
        return self._call(request)

    def review_code(self, code, **_kwargs):
        return self._call(lambda: review_code(code, self.scope, self.router))
