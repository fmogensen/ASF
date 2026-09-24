"""asf.briefs.build — one row in, one brief out.

``build(product, row, index, inflight, repo_facts=None) -> Brief``. The row is an
:class:`asf.feeder.rows.Row`; the index is the loaded ``index.json``; ``inflight`` is the running
sessions; ``repo_facts`` is the optional dict the *caller* fills from git (``head``,
``branch_exists``, ``files``, ``tests``, ``last_report``; :func:`asf.briefs.facts.repo_facts`
is the filler) — this module never runs git, never
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
import hashlib
import json
import os
import string

from asf import env
from asf.briefs import facts as facts_mod
from asf.briefs import preamble as preamble_mod
from asf.feeder import rows as feeder_rows
from asf.views import index_reader as ix
from asf.workers.stall import CORRECTION_HEAD

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')

#: The operator's two model labels. ``asf.workers`` maps them onto real model names, so no
#: vendor's model id is ever written down here.
HEAVY = 'heavy'
LIGHT = 'light'

KINDS = ('spec', 'plan', 'coder', 'review', 'fixer', 'rebase', 'close', 'adjudicate', 'fix-bug',
         'correct', 'groom', 'reshape', 'delivery-plan', 'delivery-code')
KIND_ALIASES = {'task': 'coder', 'code': 'coder', 'fix': 'fixer', 'bug': 'fix-bug',
                'fix_bug': 'fix-bug'}
DEFAULT_MODELS = {'spec': HEAVY, 'plan': HEAVY, 'adjudicate': HEAVY, 'review': HEAVY,
                  'coder': LIGHT, 'fixer': LIGHT, 'rebase': LIGHT, 'close': LIGHT,
                  'fix-bug': LIGHT, 'correct': LIGHT, 'groom': HEAVY, 'reshape': HEAVY,
                  'delivery-plan': HEAVY, 'delivery-code': LIGHT}
#: The kinds that may mint new cards (Stories, Tasks, Decisions) and so need an id range.
ID_RANGE_KINDS = ('spec', 'plan', 'adjudicate', 'fix-bug', 'groom', 'reshape')

#: The typed fields a brief states about its card, and so the ones whose change makes a brief
#: stale (F-0090 D4). ``state``, ``evidence``, ``stage_since`` and ``updated`` are not here: they
#: move on every ingest and would stale every brief.
DIGEST_FIELDS = ('title', 'writes', 'tests', 'after', 'blockedBy', 'decided', 'severity')
#: The card sections the brief renders; ``## History`` is deliberately not one.
DIGEST_SECTIONS = ('description', 'acceptance', 'fix', 'links')

TAIL = """## The heartbeat, the marker, and the report

Print a progress line as you go — what you are doing, not that you are doing something. A session
whose output has gone quiet is read as stalled and may be relaunched on top of you, which throws
away everything you have not pushed. Never run a command in the background and never end your
turn waiting for one.

Run the gate in the foreground and wait for it. Your last act is `git push`. Never start a
background task you do not wait for. A result with uncommitted or unpushed work is a failed
session (B-0051) and comes back to you as a correction. Say so yourself in the report's
`pushed:` line: `pushed: no` is read as that failure at once.

Your branch is `{branch}`, and it may already be on origin (`exists:` above — a held branch
comes back to its session, and the worktree was rebased onto `origin/{main}` before you started;
if `git status` shows a rebase in progress, finish it first). A lane branch is straight commits
on the trunk: never merge `origin/{branch}` or `origin/{main}` into it, never force-push, never
recut it or open another branch. Push with `git push origin {branch}`. If that is refused as
non-fast-forward, the rebase is why: stop there — do not merge, do not force — and write
`pushed: rebased <sha> — the factory publishes` in the report; the factory publishes a rebased
lane branch itself (B-0056). Never invent an id: a card id comes from `asf new` or the
`BACKLOG_ID_RANGE` this session was given, and a ruling is never a commit in this repo.

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
pushed: yes <the sha origin/{branch} now points at> | rebased <sha> — the factory publishes | no — <why>
commits: <sha> <subject> (one per line, or none)
tests: <what you ran — and its last line>
left out: <what and why, or none>
ruling: <adjudicate only — one paragraph: what was disputed, what now holds, what changes; else omit>
blocked_on: <adjudicate only — the id this item must wait for, or none>
writes: <adjudicate only — the corrected footprint, space-separated globs, or none>
superseded_by: <adjudicate only — the id that replaces this item, or none>
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
    card_digest: str = ''


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


def add_dirs_for(product, row=None, kind=None):
    """``job_grants`` from the product yaml — the directories a session may read outside its
    worktree, expanded but not checked (the runtime is what fails on a missing one).

    A ``groom`` row also grants the directories of ``groom_file`` and ``answers_file`` (PD7): the
    session reads the one and writes the other, and neither sits inside its worktree."""
    grants = []
    if product is not None:
        raw = product._get('job_grants') if hasattr(product, '_get') else None
        grants = [os.path.expanduser(str(d)) for d in (raw or [])]
    if kind == 'groom' and row is not None:
        dirs = [os.path.dirname(os.path.expanduser(getattr(row, attr, '') or ''))
                for attr in ('groom_file', 'answers_file') if getattr(row, attr, '')]
        groom_file = getattr(row, 'groom_file', '') or ''
        if groom_file and product is not None:  # the cards its `inbox:` lines name
            record = os.path.dirname(os.path.dirname(os.path.expanduser(groom_file)))
            dirs.append(os.path.join(record, product.conventions.intake_dir))
        for d in dirs:
            if d and d not in grants:
                grants.append(d)
    return grants


def id_ranges_needed(kind):
    return kind in ID_RANGE_KINDS


# ---- the digest --------------------------------------------------------------

def _digest_value(value):
    """One typed value as text: a list keeps its order, an absent key is the empty string."""
    if value is None:
        return ''
    if isinstance(value, (list, tuple)):
        return json.dumps([str(v) for v in value], ensure_ascii=False)
    return str(value)


def _digest_lines(product, item_id, items):
    """The lines one card contributes to a digest — see :func:`card_digest`."""
    item = items.get(item_id) or {}
    links = item.get('links') or {}
    lines = [f'{key}={_digest_value(item.get(key))}' for key in DIGEST_FIELDS]
    lines += [f'links.{key}={_digest_value(links.get(key))}' for key in ('spec', 'plan')]
    if item.get('type') == 'feature':
        lines.append('stories=' + json.dumps(sorted(c['id'] for c in ix.children(items, item, 'story'))))
    sections = preamble_mod.card_sections(product, item)
    for name in DIGEST_SECTIONS:
        lines.append(f'## {name}\n{sections.get(name) or ""}')
    return lines


def card_digest(product, item_id, index):
    """sha256, first 16 hex: what a brief states about this card — ``DIGEST_FIELDS``,
    ``links.spec``, ``links.plan``, the Feature's Story ids, and the card's Description /
    Acceptance / Fix / Links text (:func:`asf.briefs.preamble.card_sections`). A card carrying
    ``delivers:`` folds in each member's own lines, so a member's acceptance changing stales the
    delivery's brief.

    ``## History`` and the machine block are not read: filing a ruling or ingesting a push must
    not stale every brief (D4). An absent key renders empty, so a card that gains a field changes
    the digest and a card that never had one does not."""
    items = feeder_rows.items_of(index) if index else {}
    lines = _digest_lines(product, item_id, items)
    for member in (items.get(item_id) or {}).get('delivers') or []:
        if member != item_id:
            lines += [f'member {member}'] + _digest_lines(product, member, items)
    return hashlib.sha256('\n'.join(lines).encode('utf-8')).hexdigest()[:16]


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
        'specs_dir': preamble_mod.conventions(product).specs_dir,
        'plans_dir': preamble_mod.conventions(product).plans_dir,
        'reviews_dir': preamble_mod.conventions(product).reviews_dir,
        'round': facts['round'],
        'next_round': facts['next_round'],
        'head': facts['head'] or preamble_mod.UNKNOWN,
        'writes': ', '.join(facts['writes']) if facts['writes'] else '(none declared)',
        'tests': ', '.join(facts['tests']) if facts['tests'] else '(name the test you add)',
        'delivers': ', '.join(facts['delivers']) or '(none)',
        'delivery_count': len(facts['delivers']),
        'stories': '; '.join(facts['stories']) if facts['stories'] else '(none yet)',
        'description': (sections.get('description') or item.get('title') or '—').strip(),
        'acceptance': (sections.get('acceptance') or '—').strip(),
        'fix': (sections.get('fix')
                or '(the card carries no `## Fix` — write one line saying what you did instead, '
                   'and why)').strip(),
        'groom_file': getattr(row, 'groom_file', '') or '—',
        'answers_file': getattr(row, 'answers_file', '') or '—',
        'open_questions': '\n'.join(getattr(row, 'open_questions', ()) or ()) or '(none)',
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
                 model=model_for(product, kind), add_dirs=add_dirs_for(product, row, kind),
                 id_ranges_needed=id_ranges_needed(kind),
                 card_digest=card_digest(product, ctx['item_id'], index))


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
    brief = build(product, row, index, inflight,
                  repo_facts=facts_mod.repo_facts(product, row, index, inflight))
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
