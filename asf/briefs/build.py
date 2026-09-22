"""asf.briefs.build — one row in, one brief out.

``build(product, row, index, inflight, repo_facts=None) -> Brief``. The row is an
:class:`asf.feeder.rows.Row`; the index is the loaded ``index.json``; ``inflight`` is the running
sessions; ``repo_facts`` is the optional dict the *caller* fills from git (``head``,
``branch_exists``, ``files``, ``tests``, ``last_report``) — this module never runs git, never
opens a socket and never guesses a fact nobody gave it.

The text is four parts, always in this order::

    Backlog item: <id> — <title>        the line every session registry reads, and nothing else
    <the preamble>                      asf.briefs.preamble — the facts, generated not discovered
    <the kind template>                 templates/<kind>.md, with {placeholders} filled
    <the common tail>                   the heartbeat, the operator marker, the typed REPORT

A template is plain markdown with ``{placeholders}``; every name in it must be a key of the
context :func:`context` builds, so a typo in a template is a test failure rather than a brief
that ships with ``{spec_path}`` printed literally at a session.

The kinds are the feeder's row kinds plus the two the harvest loop raises (``review``,
``fixer``); ``KIND_ALIASES`` maps a row's ``brief_kind`` onto a template name (the feeder emits
``task`` for PLAN → CODE, whose template is ``coder``).
"""
import argparse
import dataclasses
import json
import os
import string

from asf import env
from asf.briefs import preamble as preamble_mod
from asf.workers.stall import CORRECTION_HEAD

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')

#: The operator's two model labels. ``asf.workers`` maps them onto real model names, so no
#: vendor's model id is ever written down here.
HEAVY = 'heavy'
LIGHT = 'light'

KINDS = ('spec', 'plan', 'coder', 'review', 'fixer', 'rebase', 'close', 'adjudicate', 'fix-bug',
         'correct')
KIND_ALIASES = {'task': 'coder', 'code': 'coder', 'fix': 'fixer', 'bug': 'fix-bug',
                'fix_bug': 'fix-bug'}
DEFAULT_MODELS = {'spec': HEAVY, 'plan': HEAVY, 'adjudicate': HEAVY, 'review': HEAVY,
                  'coder': LIGHT, 'fixer': LIGHT, 'rebase': LIGHT, 'close': LIGHT,
                  'fix-bug': LIGHT, 'correct': LIGHT}
#: The kinds that may mint new cards (Stories, Tasks, Decisions) and so need an id range.
ID_RANGE_KINDS = ('spec', 'plan', 'adjudicate', 'fix-bug')

TAIL = """## The heartbeat, the marker, and the report

Print a progress line as you go — what you are doing, not that you are doing something. A session
whose output has gone quiet is read as stalled and may be relaunched on top of you, which throws
away everything you have not pushed. Never run a command in the background and never end your
turn waiting for one.

Anything a human must decide, answer or run is never guessed and never buried in a comment:
print `NEEDS OPERATOR: <what> — <the command or the answer needed>` on its own line, then carry on
with every part of the job that does not depend on it.

Finish with this, and nothing after it:

```
REPORT
item: {item_id}
kind: {kind}
status: done | partial | blocked
branch: {branch}
commits: <sha> <subject> (one per line, or none)
tests: <what you ran — and its last line>
left out: <what and why, or none>
NEEDS OPERATOR: <only if something needs a human, else omit>
```
"""


class BriefError(Exception):
    pass


@dataclasses.dataclass
class Brief:
    kind: str
    item_id: str
    text: str
    model: str
    add_dirs: list
    id_ranges_needed: bool


# ---- the kind ----------------------------------------------------------------

def normalize_kind(kind):
    k = str(kind or '').strip().lower()
    k = KIND_ALIASES.get(k, k)
    if k not in KINDS:
        raise BriefError(f'no brief template for kind {kind!r} (have: {", ".join(KINDS)})')
    return k


def template_path(kind):
    return os.path.join(TEMPLATES_DIR, f'{kind}.md')


def load_template(kind):
    path = template_path(kind)
    if not os.path.exists(path):
        raise BriefError(f'missing brief template: {path}')
    with open(path, encoding='utf-8') as f:
        return f.read()


def placeholders(text):
    """Every ``{name}`` a template asks for."""
    return [f.split('.')[0].split('[')[0]
            for _lit, f, _spec, _conv in string.Formatter().parse(text) if f]


def render(text, ctx):
    """Fill a template. A placeholder the context has no key for is an error, not an empty
    string: a brief that ships with a literal ``{spec_path}`` sends a session searching."""
    unknown = sorted({f for f in placeholders(text) if f not in ctx})
    if unknown:
        raise BriefError(f'unknown placeholder(s) in the template: {", ".join(unknown)}')
    return text.format(**ctx)


# ---- model and grants --------------------------------------------------------

def model_for(product, kind):
    """``conventions.models.<kind>``, else the default label for that kind."""
    table = (preamble_mod.conventions(product).get('models') or {})
    return table.get(kind) or DEFAULT_MODELS.get(kind, LIGHT)


def add_dirs_for(product):
    """``job_grants`` from the product yaml — the directories a session may read outside its
    worktree, expanded but not checked (the runtime is what fails on a missing one)."""
    grants = []
    if product is not None:
        raw = product._get('job_grants') if hasattr(product, '_get') else None
        grants = [os.path.expanduser(str(d)) for d in (raw or [])]
    return grants


def id_ranges_needed(kind):
    return kind in ID_RANGE_KINDS


# ---- the text ----------------------------------------------------------------

def item_line(row, item):
    """The first line, and the only one a session registry reads."""
    if item and item.get('id'):
        title = ' '.join(str(item.get('title') or '').split())
        return f"Backlog item: {item['id']} — {title}" if title else f"Backlog item: {item['id']}"
    reason = ' '.join(str(getattr(row, 'reason', '') or 'no card in the index').split())
    iid = getattr(row, 'item_id', '') or ''
    why = f'{iid} is not in the index' if iid else reason
    return f"Backlog item: none ({why})"


def context(product, row, kind, facts):
    """Every name a template may use. One flat dict, so a missing key is a missing key."""
    item, feature, epic = facts['item'], facts['feature'], facts['epic']
    sections = facts['sections']
    return {
        'kind': kind,
        'row_kind': getattr(row, 'kind', '') or '',
        'reason': getattr(row, 'reason', '') or '—',
        'branch': facts['branch'] or '—',
        'main': (getattr(product, 'main', 'main') if product is not None else 'main'),
        'item_id': item.get('id') or getattr(row, 'item_id', '') or 'none',
        'item_title': item.get('title') or '—',
        'item_type': item.get('type') or '—',
        'item_state': item.get('state') or 'New',
        'severity': item.get('severity') or '—',
        'feature_id': feature.get('id') or '—',
        'feature_title': feature.get('title') or '—',
        'epic_id': epic.get('id') or '—',
        'epic_title': epic.get('title') or '—',
        'spec_path': facts['spec_path'],
        'plan_path': facts['plan_path'],
        'review_path': facts['review_path'],
        'specs_dir': preamble_mod.conventions(product).get('specs_dir')
        or preamble_mod.DEFAULT_SPECS_DIR,
        'plans_dir': preamble_mod.conventions(product).get('plans_dir')
        or preamble_mod.DEFAULT_PLANS_DIR,
        'reviews_dir': preamble_mod.conventions(product).get('reviews_dir')
        or preamble_mod.DEFAULT_REVIEWS_DIR,
        'round': facts['round'],
        'next_round': facts['next_round'],
        'head': facts['head'] or preamble_mod.UNKNOWN,
        'writes': ', '.join(facts['writes']) if facts['writes'] else '(none declared)',
        'tests': ', '.join(facts['tests']) if facts['tests'] else '(name the test you add)',
        'stories': '; '.join(facts['stories']) if facts['stories'] else '(none yet)',
        'description': (sections.get('description') or item.get('title') or '—').strip(),
        'acceptance': (sections.get('acceptance') or '—').strip(),
        'fix': (sections.get('fix')
                or '(the card carries no `## Fix` — write one line saying what you did instead, '
                   'and why)').strip(),
    }


def correction_text(row, kind):
    """A ``correct`` brief ends with the failure the harvest recorded, under stall's head."""
    text = getattr(row, 'correction', '') or ''
    return CORRECTION_HEAD + text.rstrip() if kind == 'correct' and text else ''


def build(product, row, index, inflight=None, repo_facts=None):
    """The brief for one feeder row."""
    kind = normalize_kind(getattr(row, 'brief_kind', '') or getattr(row, 'kind', ''))
    facts = preamble_mod.collect(product, row, index, inflight, repo_facts)
    facts['kind'] = kind
    ctx = context(product, row, kind, facts)
    parts = [item_line(row, facts['item']),
             preamble_mod.build(product, row, index, inflight, repo_facts, facts=facts),
             render(load_template(kind), ctx).rstrip() + correction_text(row, kind),
             render(TAIL, ctx)]
    return Brief(kind=kind, item_id=ctx['item_id'], text='\n\n'.join(p.strip() for p in parts) + '\n',
                 model=model_for(product, kind), add_dirs=add_dirs_for(product),
                 id_ranges_needed=id_ranges_needed(kind))


# ---- the CLI verb ------------------------------------------------------------

def _load_index(product):
    root = getattr(product, 'backlog_dir', None)
    if not root or not os.path.exists(os.path.join(root, 'index.json')):
        raise BriefError(f'no index.json under {root!r} — run `asf index` first')
    with open(os.path.join(root, 'index.json'), encoding='utf-8') as f:
        return json.load(f)


def cmd_brief(args):
    """``asf brief`` — print the brief the tick would hand the next row, without launching it."""
    from asf.feeder import render as feeder_render
    from asf.feeder import rows as feeder_rows
    product = env.load_product(args.product)
    index = _load_index(product)
    inflight = feeder_render.load_inflight(getattr(args, 'inflight', None))
    candidates = feeder_rows.candidates(index, product, inflight)
    row = next((r for r in candidates
                if not args.item or r.item_id == args.item), None)
    if row is None:
        print(f"no row to brief{' for ' + args.item if args.item else ''}")
        return 1
    if args.kind:
        row = dataclasses.replace(row, brief_kind=args.kind)
    brief = build(product, row, index, inflight)
    if args.json:
        print(json.dumps(dataclasses.asdict(brief), indent=2))
    else:
        print(brief.text, end='')
    return 0


def register(subparsers):
    """The controller adds one import and one call for this."""
    p = subparsers.add_parser('brief', help='print the brief for the next row (or one item)',
                              formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    env.add_product_arg(p)
    p.add_argument('--item', default=None, help='brief this item id instead of the first row')
    p.add_argument('--kind', default=None, help=f'override the brief kind ({", ".join(KINDS)})')
    p.add_argument('--inflight', default=None, help='JSON file: the running sessions')
    p.add_argument('--json', action='store_true', help='the Brief as JSON, not the text')
    p.set_defaults(func=cmd_brief)
    return p
