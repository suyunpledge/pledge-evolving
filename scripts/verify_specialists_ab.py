"""Brain alone vs brain + specialists: a live A/B on research, coding and mixed tasks.

Arms (same brain model, fresh workspace and home per run):
  brain        — today's runtime: the brain may still use plain spawn_subagent.
  specialists  — keystone.specialists enabled (delegation=advise): retrieval
                 (catalog + catalog_expand) and coding routed to a cheaper model.
  specialists-prefer  — same table with delegation=prefer (firmer routing guidance).
  specialists-require — delegation=require: retrieval/coding tools leave the brain's
                 surface, so domain work must go through the specialists.

Tasks are local fixtures, so grading is exact and repeatable:
  research — a multi-source question over a docs corpus with outdated/distractor
             files; graded on four facts plus citing at least two right files.
  coding   — a cart module with three bugs; graded by running its tests.
  mixed    — implement a rounding rule that is specified (and later amended)
             across long docs; graded by hidden tests the agent never sees.

Reported per run: success, input/output tokens per model, cost (CNY, list
prices from keystone.pricing), wall time and model requests. Credentials come
from the user's provider rows in the legacy profile (keystone.compat.default_home);
they are never printed or written.

    python scripts/verify_specialists_ab.py --self-check      # fixtures + graders only
    python scripts/verify_specialists_ab.py --live --runs 2
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from keystone import model as kmodel                       # noqa: E402
from keystone.compat import default_home                   # noqa: E402
from keystone.loop import Agent                             # noqa: E402
from keystone.policy import Mode, Policy, Sandbox          # noqa: E402
from keystone.pricing import cost_of_usage                 # noqa: E402
from keystone.runtime_types import LoopLimits              # noqa: E402
from keystone.session import Session                       # noqa: E402
from keystone.specialists import SpecialistTable           # noqa: E402
from keystone.tools import build_builtin_registry          # noqa: E402


# -- fixtures -------------------------------------------------------------------

def _filler(topic: str, n: int) -> str:
    """Deterministic, plausible-looking padding so sources are long like real docs."""
    words = ('throughput', 'latency', 'operator', 'cluster', 'shard', 'quorum', 'handshake', 'backpressure',
             'telemetry', 'rollout', 'tenant', 'lease', 'checkpoint', 'replica', 'window', 'budget')
    out = []
    for i in range(n):
        a, b, c = words[i % 16], words[(i * 7) % 16], words[(i * 11 + 3) % 16]
        out.append(f"{topic} note {i + 1}: the {a} of each {b} is monitored alongside {c}; operators should "
                   f"review dashboards after every {words[(i * 5) % 16]} change and record findings in the runbook.")
    return '\n'.join(out) + '\n'


RESEARCH_DOCS = {
    'docs/overview.md': '# Halcyon Relay overview\n\nHalcyon Relay forwards events between regions. Retries are '
                        'bounded by a configurable retry ceiling (see the configuration reference).\n\n'
                        + _filler('overview', 70),
    'docs/changelog.md': '# Changelog\n\n' + _filler('release', 40)
                         + '\n## v2.4.0 (2026-02-11)\n- Default retry ceiling raised from 5 to 7 attempts (RFC-031).\n'
                           '- Backoff jitter now uses full jitter.\n\n## v2.3.2 (2025-12-02)\n- Fixed lease renewal race.\n'
                         + _filler('release', 40),
    'docs/rfcs/rfc-031-retry-ceiling.md': '# RFC-031: Raise the default retry ceiling\n\nStatus: Approved\n'
                                          'Approved by: Mei Tanaka (platform lead), 2026-01-29\nAuthor: R. Osei\n\n'
                                          'Proposal: raise relay.retry.ceiling default from 5 to 7.\n\n'
                                          + _filler('rfc-031', 45),
    'docs/rfcs/rfc-029-timeouts.md': '# RFC-029: Connection timeout ceiling\n\nStatus: Approved\nApproved by: '
                                     'Lars Ekdahl, 2025-11-03\n\nThe connect timeout ceiling becomes 30 s.\n\n'
                                     + _filler('rfc-029', 45),
    'docs/config-reference.md': '# Configuration reference\n\n' + _filler('config', 50)
                                + '\n| key | default | since |\n|---|---|---|\n| relay.retry.ceiling | 7 | v2.4.0 |\n'
                                  '| relay.retry.backoff_ms | 250 | v1.0 |\n' + _filler('config', 30),
    'docs/faq.md': '# FAQ (last reviewed 2025-10)\n\nQ: How many times does the relay retry?\nA: Five times by '
                   'default.\n\n' + _filler('faq', 40),
    'docs/meetings/2026-01-20.md': '# Platform sync 2026-01-20\n\nDiscussed raising the retry ceiling to 9; '
                                   'rejected, revisit with RFC-031 (proposes 7).\n\n' + _filler('meeting', 30),
}
RESEARCH_TEXT = ('Halcyon Relay 现在的默认重试上限（retry ceiling）是多少？是哪个版本改成这个值的、对应哪个 RFC、'
                 '谁批准的？只依据工作区 docs/ 里的资料回答，注明每个事实的出处文件。')


def grade_research(ws: Path, answer: str) -> dict:
    text = answer or ''
    facts = {'ceiling_7': bool(re.search(r'(?<![\d.])7(?![\d.])', text)), 'version_2.4': '2.4' in text,
             'rfc_031': bool(re.search(r'RFC[- ]?0?31', text, re.I)), 'approver': 'Tanaka' in text}
    cited = [name for name in ('changelog', 'rfc-031', 'config-reference') if name in text.lower()]
    # Fail an answer that *asserts* the current value is the outdated 5 or the rejected 9;
    # mentioning the old FAQ or the "from 5 to 7" history is fine.
    plain = re.sub(r'[*`|]', ' ', text)
    stale = bool(re.search(r'(当前|现在|目前|现行|current(?:ly)?|now)[^。\n]{0,40}?(是|为|is|:|：)\s*(5|9|五|九)'
                           r'(?![\d.])\s*(次|attempts?|times)?', plain, re.I))
    ok = all(facts.values()) and len(cited) >= 2 and not stale
    why = [k for k, v in facts.items() if not v] + ([] if len(cited) >= 2 else [f'cited {cited}']) \
        + (['asserted stale value'] if stale else [])
    return {'ok': ok, 'why': '; '.join(why), 'facts': facts, 'cited': cited}


CODING_FILES = {
    'shop/__init__.py': '',
    'shop/cart.py': textwrap.dedent('''\
        """Cart arithmetic. All amounts are integer cents."""


        def line_total(price_cents, qty, discount_pct=0):
            """Line total in cents; the percentage discount is rounded half up to a whole cent."""
            gross = price_cents * qty
            return gross - gross * discount_pct // 100


        def cart_total(lines, coupon_cents=0):
            """Sum of line totals minus a coupon; never below zero."""
            total = sum(line_total(**line) for line in lines)
            return total - coupon_cents
        '''),
    'tests/__init__.py': '',
    'tests/test_cart.py': textwrap.dedent('''\
        import unittest
        from shop.cart import cart_total, line_total


        class CartTests(unittest.TestCase):
            def test_plain_line(self):
                self.assertEqual(line_total(250, 4), 1000)

            def test_discount_rounds_half_up(self):
                self.assertEqual(line_total(999, 1, 15), 849)     # 149.85 off -> 150
                self.assertEqual(line_total(1000, 1, 15), 850)

            def test_quantity_must_be_positive(self):
                with self.assertRaises(ValueError):
                    line_total(100, 0)

            def test_coupon_never_negative(self):
                self.assertEqual(cart_total([{"price_cents": 300, "qty": 1}], coupon_cents=500), 0)
                self.assertEqual(cart_total([{"price_cents": 300, "qty": 2}], coupon_cents=100), 500)


        if __name__ == "__main__":
            unittest.main()
        '''),
}
CODING_TEXT = ('tests/test_cart.py 里有失败的用例。修复 shop/cart.py，让全部测试通过；不要修改测试文件，'
               '也不要改变函数签名。完成后简要说明改了什么。')


def _unittest(ws: Path, start: str = 'tests') -> dict:
    proc = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', start, '-t', '.'], cwd=ws,
                          capture_output=True, text=True, timeout=120)
    tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ['']
    return {'ok': proc.returncode == 0, 'why': '' if proc.returncode == 0 else tail[0][:200]}


def grade_coding(ws: Path, answer: str) -> dict:
    tests = ws / 'tests' / 'test_cart.py'
    if tests.read_text(encoding='utf-8') != CODING_FILES['tests/test_cart.py']:
        return {'ok': False, 'why': 'test file was modified'}
    return _unittest(ws)


BILLING_SPEC = ('# Billing specification v3\n\n' + _filler('billing', 60)
                + '\n## §4.2 Rounding (superseded by §7.3)\nTotals used banker\'s rounding to 0.01.\n\n'
                + _filler('billing', 60)
                + '\n## §7.3 Invoice total rounding\nInvoice totals are rounded to the nearest 0.05 for CHF '
                  '(Swiss rounding) and to the nearest 0.01 for every other currency. Ties round away from zero.\n\n'
                + _filler('billing', 40))
MIXED_FILES = {
    'docs/billing-spec.md': BILLING_SPEC,
    'docs/decisions.md': '# Decision log\n\n' + _filler('decision', 40)
                         + '\n- 2026-05-06: CHF 0.05 rounding applies only to cash payments '
                           '(payment_method == "cash"); CHF card payments round to 0.01 like other currencies.\n'
                         + _filler('decision', 20),
    'docs/glossary.md': '# Glossary\n\n' + _filler('glossary', 50),
    'billing/__init__.py': '',
    'billing/invoice.py': textwrap.dedent('''\
        from decimal import Decimal


        def round_total(amount: Decimal, currency: str, payment_method: str) -> Decimal:
            """Round an invoice total according to the billing specification in docs/."""
            raise NotImplementedError
        '''),
}
MIXED_TEXT = ('按照 docs/ 里的计费规范（含后续决策）实现 billing/invoice.py 中的 round_total。'
              '返回 Decimal；不要改函数签名。完成后说明依据了哪些规范条款和文件。')
MIXED_HIDDEN_TEST = textwrap.dedent('''\
    import unittest
    from decimal import Decimal as D
    from billing.invoice import round_total


    class Hidden(unittest.TestCase):
        def test_chf_cash_swiss_rounding(self):
            self.assertEqual(round_total(D("10.024"), "CHF", "cash"), D("10.00"))
            self.assertEqual(round_total(D("10.025"), "CHF", "cash"), D("10.05"))
            self.assertEqual(round_total(D("10.074"), "CHF", "cash"), D("10.05"))
            self.assertEqual(round_total(D("-10.025"), "CHF", "cash"), D("-10.05"))

        def test_chf_card_cents(self):
            self.assertEqual(round_total(D("10.024"), "CHF", "card"), D("10.02"))

        def test_other_currency_ties_away(self):
            self.assertEqual(round_total(D("10.005"), "EUR", "card"), D("10.01"))
            self.assertEqual(round_total(D("-10.005"), "EUR", "cash"), D("-10.01"))
            self.assertEqual(round_total(D("2.345"), "USD", "cash"), D("2.35"))
    ''')


def grade_mixed(ws: Path, answer: str) -> dict:
    hidden = ws / '_hidden_tests'
    hidden.mkdir(exist_ok=True)
    (hidden / '__init__.py').write_text('', encoding='utf-8')
    (hidden / 'test_hidden.py').write_text(MIXED_HIDDEN_TEST, encoding='utf-8')
    return _unittest(ws, '_hidden_tests')


TASKS = {
    'research': {'text': RESEARCH_TEXT, 'files': RESEARCH_DOCS, 'grade': grade_research},
    'coding': {'text': CODING_TEXT, 'files': CODING_FILES, 'grade': grade_coding},
    'mixed': {'text': MIXED_TEXT, 'files': MIXED_FILES, 'grade': grade_mixed},
}


def materialise(ws: Path, files: dict) -> None:
    for rel, body in files.items():
        path = ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding='utf-8')


# -- accounting -------------------------------------------------------------------

class CountingTransport:
    """Pass-through transport that tallies requests and usage per model (thread-safe)."""

    def __init__(self, inner):
        self.inner = inner
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.by_model: dict[str, dict] = {}

    def complete(self, provider, model, messages, **options):
        value = self.inner.complete(provider, model, messages, **options)
        usage = value[1]
        with self.lock:
            row = self.by_model.setdefault(model, {'requests': 0, 'input': 0, 'output': 0, 'cached': 0,
                                                   'cost_cny': 0.0})
            row['requests'] += 1
            row['input'] += int(usage.prompt_tokens or 0)
            row['output'] += int(usage.completion_tokens or 0)
            row['cached'] += int(getattr(usage, 'cached_tokens', 0) or 0)
            row['cost_cny'] += cost_of_usage(model, input_tokens=int(usage.prompt_tokens or 0),
                                             cached_tokens=int(getattr(usage, 'cached_tokens', 0) or 0),
                                             output_tokens=int(usage.completion_tokens or 0))['total']
        return value

    def __getattr__(self, name):
        return getattr(self.inner, name)


def provider_for(model_name: str, home: Path, label: str) -> kmodel.Provider:
    sys.path.insert(0, str(ROOT / 'scripts'))
    from verify_mimo_live import user_layer
    rows = json.loads(user_layer(home).read_text(encoding='utf-8-sig'))
    secrets = json.loads((home / 'secrets.json').read_text(encoding='utf-8-sig'))
    for row in rows:
        conf = row.get('config', {})
        if row.get('disabled') or conf.get('model') != model_name or not str(row.get('name', '')).startswith('provider:'):
            continue
        key = secrets.get(row.get('id'))
        if isinstance(key, str) and key:
            return kmodel.Provider(label, conf['baseURL'], key, wire=conf.get('wire', 'openai'),
                                   models=(model_name,), default_model=model_name, small_model=model_name,
                                   temperature=conf.get('temperature'),
                                   reasoning_mode=conf.get('reasoningMode', 'auto'))
    raise ValueError(f'No enabled provider row with a stored credential serves {model_name}')


def make_agent(arm, ws, home, router, brain_steps, spec_models):
    policy = Policy(mode=Mode.ACCEPT_EDITS, sandbox=Sandbox.WORKSPACE_WRITE, workspace=ws,
                    allow=('spawn_subagent', 'catalog_expand'), non_interactive=True)
    session = Session(home / 'sessions' / f'{arm}-{ws.name}.jsonl', meta={'cwd': str(ws), 'kind': 'specialists-ab'})
    specialists = None
    if arm.startswith('specialists'):
        specialists = SpecialistTable.from_mapping({
            'enabled': True, 'delegation': arm.partition('-')[2] or 'advise', 'domains': {
            domain: {'model': ['spec', model]} for domain, model in spec_models.items()}})
    return Agent(home=home, workspace=ws, router=router, registry=build_builtin_registry(), policy=policy,
                 session=session, limits=LoopLimits(max_steps=brain_steps, spawn_budget=4), wire_hint='openai',
                 specialists=specialists)


def run_once(arm, task_name, router, counter, root, brain_steps, spec_models, brain_model):
    task = TASKS[task_name]
    ws = root / f'{task_name}-{arm}-{int(time.time() * 1000) % 10**8}'
    ws.mkdir(parents=True)
    materialise(ws, task['files'])
    home = root / f'home-{ws.name}'
    agent = make_agent(arm, ws, home, router, brain_steps, spec_models)
    counter.reset()
    started = time.time()
    error = ''
    try:
        with agent.session:
            report = agent.run(task['text'])
        text, stopped, steps = report.text or '', report.stopped, report.steps
    except Exception as exc:  # noqa: BLE001 — one failed run must not end the A/B
        text, stopped, steps, error = '', 'error', [], f'{type(exc).__name__}: {exc}'[:300]
    seconds = round(time.time() - started, 1)
    try:
        grade = task['grade'](ws, text)
    except Exception as exc:  # noqa: BLE001
        grade = {'ok': False, 'why': f'grader error: {exc}'[:200]}
    models = json.loads(json.dumps(counter.by_model))
    events = agent.events
    return {
        'arm': arm, 'task': task_name, 'stopped': stopped, 'error': error, 'seconds': seconds,
        'ok': bool(grade.get('ok')), 'grade': grade,
        'requests': sum(m['requests'] for m in models.values()),
        'input_tokens': sum(m['input'] for m in models.values()),
        'output_tokens': sum(m['output'] for m in models.values()),
        'cost_cny': round(sum(m['cost_cny'] for m in models.values()), 5),
        'brain_input_tokens': models.get(brain_model, {}).get('input', 0),
        'by_model': models,
        'tools': [s.tool for s in steps if s.tool],
        'specialist_routes': [e.get('domain') for e in events if e.get('type') == 'specialist_route'],
        'catalog_expands': sum(1 for s in steps if s.tool == 'catalog_expand'),
        'spawns': sum(1 for s in steps if s.tool == 'spawn_subagent'),
        'answer': text[:4000],
    }


def self_check() -> int:
    """Fixtures are solvable and the graders discriminate (no model calls)."""
    root = Path(tempfile.mkdtemp(prefix='specialists-check-'))
    ok = True
    for name, task in TASKS.items():
        ws = root / name
        materialise(ws, task['files'])
        before = task['grade'](ws, '')
        print(f'{name:<9} unsolved grade ok={before["ok"]} ({before["why"][:60]})')
        ok &= not before['ok']
    (root / 'coding' / 'shop' / 'cart.py').write_text(textwrap.dedent('''\
        def line_total(price_cents, qty, discount_pct=0):
            if qty <= 0:
                raise ValueError("qty must be positive")
            gross = price_cents * qty
            return gross - (gross * discount_pct + 50) // 100


        def cart_total(lines, coupon_cents=0):
            return max(0, sum(line_total(**line) for line in lines) - coupon_cents)
        '''), encoding='utf-8')
    (root / 'mixed' / 'billing' / 'invoice.py').write_text(textwrap.dedent('''\
        from decimal import Decimal, ROUND_HALF_UP


        def round_total(amount, currency, payment_method):
            step = Decimal("0.05") if currency == "CHF" and payment_method == "cash" else Decimal("0.01")
            return ((amount / step).quantize(Decimal(1), rounding=ROUND_HALF_UP) * step).quantize(Decimal("0.01"))
        '''), encoding='utf-8')
    good = ('默认重试上限是 7 次，自 v2.4.0 起（docs/changelog.md），对应 RFC-031，由 Mei Tanaka 批准'
            '（docs/rfcs/rfc-031-retry-ceiling.md）；配置见 docs/config-reference.md。')
    for name, answer in (('research', good), ('coding', ''), ('mixed', '')):
        after = TASKS[name]['grade'](root / name, answer)
        print(f'{name:<9} reference grade ok={after["ok"]} {after["why"][:80]}')
        ok &= after['ok']
    stale = TASKS['research']['grade'](root / 'research', '当前默认上限是 5 次（docs/faq.md）。' + good)
    print(f'research  stale-value answer ok={stale["ok"]}')
    ok &= not stale['ok']
    history = TASKS['research']['grade'](root / 'research', good + ' docs/faq.md 仍写 5 次，已过时；v2.4.0 从 5 改成 7。')
    print(f'research  answer mentioning history ok={history["ok"]}')
    ok &= history['ok']
    print('self-check', 'PASS' if ok else 'FAIL')
    return 0 if ok else 1


def summarise(rows):
    summary = {}
    for row in rows:
        key = f"{row['task']}/{row['arm']}"
        s = summary.setdefault(key, {'runs': 0, 'pass': 0, 'input_tokens': 0, 'output_tokens': 0,
                                     'brain_input_tokens': 0, 'cost_cny': 0.0, 'seconds': 0.0, 'requests': 0})
        s['runs'] += 1
        s['pass'] += int(row['ok'])
        for k in ('input_tokens', 'output_tokens', 'brain_input_tokens', 'cost_cny', 'seconds', 'requests'):
            s[k] += row[k]
    for s in summary.values():
        n = s['runs']
        for k in ('input_tokens', 'output_tokens', 'brain_input_tokens', 'requests'):
            s['avg_' + k] = round(s.pop(k) / n)
        s['avg_cost_cny'] = round(s.pop('cost_cny') / n, 4)
        s['avg_seconds'] = round(s.pop('seconds') / n, 1)
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--self-check', action='store_true')
    parser.add_argument('--brain', default='deepseek-v4-pro')
    parser.add_argument('--retrieval-model', default='deepseek-flash')
    parser.add_argument('--coding-model', default='deepseek-flash')
    parser.add_argument('--home', default='', help='profile with provider rows + secrets (read only); '
                                                    'default keystone.compat.default_home()')
    parser.add_argument('--tasks', default='research,coding,mixed')
    parser.add_argument('--arms', default='brain,specialists-prefer,specialists-require')
    parser.add_argument('--runs', type=int, default=2)
    parser.add_argument('--max-steps', type=int, default=30)
    parser.add_argument('--out', default='')
    parser.add_argument('--regrade', default='', help='re-apply the research grader to a saved report, in place')
    args = parser.parse_args(argv)
    if args.regrade:
        path = Path(args.regrade)
        data = json.loads(path.read_text(encoding='utf-8'))
        for row in data['runs']:
            if row['task'] == 'research':
                row['grade'] = grade_research(None, row['answer'])
                row['ok'] = row['grade']['ok']
        data['summary'] = summarise(data['runs'])
        data['regraded'] = datetime.now().isoformat(timespec='seconds')
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
        print(json.dumps(data['summary'], ensure_ascii=False, indent=1))
        return 0
    if args.self_check or not args.live:
        return self_check()

    home = Path(args.home) if args.home else default_home()
    brain = provider_for(args.brain, home, 'brain')
    spec_models = {'retrieval': args.retrieval_model, 'coding': args.coding_model}
    spec_providers = {}
    for model_name in set(spec_models.values()):
        spec_providers[model_name] = provider_for(model_name, home, 'spec')
    if len(spec_providers) != 1:
        raise SystemExit('this harness routes every specialist to one provider row; use one specialist model')
    spec = next(iter(spec_providers.values()))
    counter = CountingTransport(kmodel.HttpTransport(timeout=240))
    router = kmodel.ModelRouter([brain, spec], transport=counter, primary=('brain', args.brain),
                                retries_per_provider=2)
    root = Path(tempfile.mkdtemp(prefix='specialists-ab-'))
    rows = []
    for run in range(args.runs):
        for task in args.tasks.split(','):
            for arm in args.arms.split(','):
                row = run_once(arm, task, router, counter, root, args.max_steps, spec_models, args.brain)
                row['run'] = run
                rows.append(row)
                print(f"{task:<8} {arm:<11} run{run} {('PASS' if row['ok'] else 'FAIL'):<4} stopped={row['stopped']:<9} "
                      f"req={row['requests']:<3} in={row['input_tokens']:<7} out={row['output_tokens']:<6} "
                      f"¥{row['cost_cny']:<8} {row['seconds']:>6}s routes={row['specialist_routes']} "
                      f"expand={row['catalog_expands']} {row['grade'].get('why', '')[:70]}", flush=True)
    summary = summarise(rows)
    out = {'date': datetime.now().isoformat(timespec='seconds'), 'brain': args.brain, 'specialists': spec_models,
           'max_steps': args.max_steps, 'summary': summary, 'runs': rows}
    target = Path(args.out) if args.out else ROOT / 'reports' / f'specialists-ab-{datetime.now():%Y%m%d-%H%M}.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(out, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f'report: {target}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
