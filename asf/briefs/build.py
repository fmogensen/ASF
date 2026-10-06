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

The kinds are the feeder's row kinds (``review`` among them: the PUSHED → REVIEW row a PR-lane
harvest asks for) plus ``fixer``; ``KIND_ALIASES`` maps a row's ``brief_kind`` onto a template name (the feeder emits
``task`` for PLAN → CODE, whose template is ``coder``).
"""
import argparse
import dataclasses
import hashlib
import json
import os
import re
import string

from asf import conventions as conventions_mod
from asf import env
from asf.briefs import facts as facts_mod
from asf.briefs import preamble as preamble_mod
from asf.conventions import HEAVY, LIGHT
from asf.feeder import rows as feeder_rows
from asf.groom import sticky as sticky_mod
from asf.views import index_reader as ix
from asf.workers.stall import CORRECTION_HEAD

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')

#: The operator's model labels. ``asf.workers`` maps them onto real model names, so no
#: vendor's model id is ever written down here. ``CHEAP`` is the bookkeeping tier: a rebase, a
#: close, the groom's clerical pass — work with a right answer that no judgement reaches.
CHEAP = 'cheap'

KINDS = ('spec', 'spec-amend', 'plan', 'coder', 'review', 'fixer', 'rebase', 'close', 'adjudicate',
         'fix-bug', 'correct', 'groom', 'groom-clerk', 'reshape', 'spec-plan', 'direct',
         'delivery-plan', 'delivery-code', 'replan')
KIND_ALIASES = {'task': 'coder', 'code': 'coder', 'fix': 'fixer', 'bug': 'fix-bug',
                'fix_bug': 'fix-bug', 'spec_plan': 'spec-plan'}
#: The class of an item, for picking its model within a kind: a Bug's severity, else its type;
#: ``cardless`` for a pull request the factory adopted with no card of its own (``PR-<n>``).
MODEL_CLASSES = ('S1', 'S2', 'S3', 'task', 'story', 'feature', 'epic', 'cardless')
#: The built-in model per brief kind AND item class — the one place the defaults live, so the
#: saving is a code default, not a setting someone has to remember. ``default`` is the label for
#: a class the kind does not name (and for a brief with no item). Judgement over a small change
#: (the review, correction and adjudication of an S2/S3 Bug or of a Task) runs light; an S1 Bug,
#: a spec, a plan and a Feature-level review run heavy. Adjudicate runs light for every class but
#: S1 (operator policy 2026-09-27: 41% of a product's repair sessions were adjudicate, on heavy).
#: A rebase, a close and the groom's clerical pass run cheap — no judgement, a right answer.
#: A cardless PR's review runs light (no card, no spec: a diff read against its own description
#: — 78 of them, every one on heavy, cost $212 in 5.5 days), and so do reshape and replan: a
#: re-cut of a plan already written, not a plan.
#: ``conventions.models.<kind>`` overrides a row, as one label or as a map of this shape
#: (:func:`model_for`).
MODEL_TABLE = {
    'spec':       {'default': HEAVY},
    'spec-amend': {'default': HEAVY},
    'plan':       {'default': HEAVY},
    'spec-plan':  {'default': HEAVY},
    'direct':     {'default': HEAVY},
    'groom':      {'default': HEAVY},
    'groom-clerk': {'default': CHEAP},
    'reshape':    {'default': LIGHT},
    'replan':     {'default': LIGHT},
    'review':     {'default': HEAVY, 'S1': HEAVY, 'S2': LIGHT, 'S3': LIGHT, 'task': LIGHT,
                   'cardless': LIGHT},
    'adjudicate': {'default': LIGHT, 'S1': HEAVY, 'S2': LIGHT, 'S3': LIGHT, 'task': LIGHT},
    'correct':    {'default': LIGHT, 'S1': HEAVY, 'S2': LIGHT, 'S3': LIGHT, 'task': LIGHT},
    'fix-bug':    {'default': LIGHT, 'S1': HEAVY, 'S2': LIGHT, 'S3': LIGHT},
    'coder':      {'default': LIGHT},
    'fixer':      {'default': LIGHT},
    'rebase':     {'default': CHEAP},
    'close':      {'default': CHEAP},
    'delivery-plan': {'default': HEAVY},
    'delivery-code': {'default': LIGHT},
}
#: The label per kind for a brief with no item — what ``model_for(product, kind)`` returns.
DEFAULT_MODELS = {kind: row['default'] for kind, row in MODEL_TABLE.items()}
#: The kinds that may mint new cards (Stories, Tasks, Decisions) and so need an id range.
ID_RANGE_KINDS = ('spec', 'spec-amend', 'plan', 'adjudicate', 'fix-bug', 'groom', 'reshape',
                  'spec-plan')

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
comes back to its session, and the worktree was rebased onto `origin/{main}` before you started
only if the branch needed it — trunk history on it, or a conflict; a branch behind the trunk is
fine: never rebase or update it just to catch up. If `git status` shows a rebase in progress,
finish it first). A lane branch is straight commits
on the trunk: never merge `origin/{branch}` or `origin/{main}` into it, never force-push, never
recut it or open another branch. Push with `git push origin {branch}`. If that is refused as
non-fast-forward, the rebase is why: stop there — do not merge, do not force — and write
`pushed: rebased <sha> — the factory publishes` in the report; the factory publishes a rebased
lane branch itself (B-0056). Publishing and landing are the factory's, never yours: commit, and
the lane publishes and lands the branch — never run `asf land` or any other `asf` command to
publish, and a refused push is never a `NEEDS OPERATOR`.
Never invent an id: a card id comes from `asf new` or the
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
needs writes: <coder/correct only — repo paths outside writes: that must change too, space-separated, or none>
proves: <code only — the Proves: trailers you wrote, one per line; or none — <why>>
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

def is_cardless(item_id):
    """True for a ``PR-<n>`` item: a pull request adopted with no card
    (:func:`asf.harvest.lane.is_pr_item`; imported here, the lane imports the feeder)."""
    from asf.harvest.lane import is_pr_item
    return is_pr_item(item_id)


def item_class(item, item_id=None):
    """The model class of an index item: ``cardless`` for a ``PR-<n>`` item (``item_id``, else
    the item's own ``id``), a Bug's severity (``S1``..``S3``), else its type
    (``task``/``story``/``feature``/``epic``); ``default`` when the item says neither."""
    item = item if isinstance(item, dict) else {}
    if is_cardless(item_id or item.get('id')):
        return 'cardless'
    kind = str(item.get('type') or '').strip().lower()
    if kind == 'bug':
        sev = str(item.get('severity') or '').strip().upper()
        return sev if sev in MODEL_CLASSES else 'default'
    return kind if kind in MODEL_CLASSES else 'default'


def _pick(row, cls):
    return row.get(cls) or row.get('default')


def model_for(product, kind, item=None, item_id=None):
    """The model label for a ``kind`` brief about ``item`` (an index item, or None; ``item_id``
    names it when the item has no card — a ``PR-<n>``).

    ``conventions.models.<kind>`` wins: one label for every class, or a map by class whose
    ``default:`` covers the classes it does not name. A class it covers neither way — and a
    value of any other shape, which doctor reports — falls to :data:`MODEL_TABLE`."""
    cls = item_class(item, item_id)
    builtin = MODEL_TABLE.get(kind, {'default': LIGHT})
    override = preamble_mod.conventions(product).map_of('models').get(kind)
    if not conventions_mod.model_value_ok(override):
        override = None
    if isinstance(override, str):
        return override.strip()
    if cls != 'S1' and kind in cheap_kinds(product):
        return (override and _pick(override, cls)) or CHEAP
    return (override and _pick(override, cls)) or _pick(builtin, cls)


def cheap_kinds(product):
    """``conventions.flags.models.cheap_kinds``: the brief kinds that run on the ``cheap`` label
    (a list, or one string of names split on commas/spaces/brackets). An S1 row never does, and a
    ``conventions.models.<kind>`` override still wins. Unset: none — the built-in table stands."""
    try:
        v = preamble_mod.conventions(product).flag('models.cheap_kinds')
    except Exception:  # noqa: BLE001 — an unreadable flag is unset
        v = None
    if isinstance(v, str):
        v = re.split(r'[\s,\[\]]+', v)
    if not isinstance(v, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(KIND_ALIASES.get(str(k).strip(), str(k).strip()) for k in v if str(k).strip())


def model_table(product):
    """``{kind: {class: label}}`` — every kind × class resolved for this product (doctor)."""
    return {kind: {cls: model_for(product, kind, _class_item(cls))
                   for cls in MODEL_CLASSES}
            for kind in MODEL_TABLE}


def _class_item(cls):
    if cls == 'cardless':
        return {'id': 'PR-0'}
    return {'type': 'bug', 'severity': cls} if cls.startswith('S') else {'type': cls}


def add_dirs_for(product, row=None, kind=None):
    """``job_grants`` from the product yaml — the directories a session may read outside its
    worktree, expanded but not checked (the runtime is what fails on a missing one).

    A ``groom`` or ``groom-clerk`` row also grants the directories of ``groom_file`` and ``answers_file`` (PD7): the
    session reads the one and writes the other, and neither sits inside its worktree."""
    grants = []
    if product is not None:
        raw = product._get('job_grants') if hasattr(product, '_get') else None
        grants = [os.path.expanduser(str(d)) for d in (raw or [])]
    if kind in ('groom', 'groom-clerk') and row is not None:
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


#: What a coder runs before pushing. A product whose PRs its external CI gates runs the full gate
#: there, never on this host (the approvals hook refuses its full-suite commands): the session runs
#: the Task's acceptance tests, targeted, and leaves the Gate to CI.
GATE_LOCAL = ("BEFORE THE PUSH: the Task's Gate commands, and its acceptance tests byte-identical "
              "from the plan\nand passing.")
GATE_REMOTE = ("BEFORE THE PUSH: the Task's acceptance tests, byte-identical from the plan and passing, "
               "run on the\nfiles they cover only. The Task's Gate runs in remote CI on the pull "
               "request, never here: do not run it.")


#: The same two for a direct-lane Feature (``direct``): no plan, so no Task Gate — the session's
#: own tests and the ones covering what it changed, targeted; the full suite is the landing gate's.
DIRECT_GATE_LOCAL = ("BEFORE THE PUSH: every test you added and the ones covering the files you "
                     "changed, passing.")
DIRECT_GATE_REMOTE = ("BEFORE THE PUSH: the tests you added and the ones covering the files you "
                      "changed, run on those files only.\nThe full suite runs in remote CI on the "
                      "pull request, never here: do not run it.")


#: The product's ``conventions.pre_push_check`` as a code brief names it: the cheap slice of
#: the CI gate (rule scripts, lint, spec and changeset checks) a push must pass here first — a
#: branch pushed without it went red on one of those rules after the heavy run, one correction
#: and one re-review later. Rendered as its own paragraph, only when the product sets it.
PRE_PUSH_RULE = ("MUST RUN BEFORE EVERY PUSH, AND PASS: `{command}` — the product's fast gate "
                 "checks, next to the targeted tests above. It is not the full suite (the "
                 "approvals hook allows it). Red here is fixed before the push, never pushed for "
                 "CI to find; paste its last line in the report.")


#: The doc kinds' steps (``conventions.pre_push_check: {spec: [...], plan: [...]}``): each one
#: after rebasing onto the trunk, in order, with the product's note on why.
PRE_PUSH_DOC_RULE = ("MUST RUN BEFORE PUSH, in this order, after rebasing onto `origin/{main}` "
                     "(the approvals hook allows exactly these commands; red is fixed before the "
                     "push, never pushed for CI to find; paste each last line in the report):")


def pre_push_doc_block(product, kind):
    """The paragraph :data:`PRE_PUSH_DOC_RULE` (a blank line after it) with the numbered steps the product names for
    doc brief ``kind``, or ``''`` when it names none."""
    from asf import approvals
    steps = approvals.pre_push_steps(product, kind) if product is not None else []
    if not steps:
        return ''
    main = getattr(product, 'main', 'main')
    lines = [PRE_PUSH_DOC_RULE.format(main=main)]
    for i, (run, note) in enumerate(steps, 1):
        lines.append(f'  {i}. `{run}`' + (f' — {note}' if note else ''))
    return '\n'.join(lines) + '\n\n'


def pre_push_block(product):
    """A blank line and :data:`PRE_PUSH_RULE` naming the product's ``pre_push_check``, or ``''``
    when it sets none — the template places it right after a paragraph, so an unset check leaves
    no trace."""
    if product is None:
        return ''
    from asf import approvals
    command = approvals.pre_push_check(product)
    return '\n\n' + PRE_PUSH_RULE.format(command=command) if command else ''


def _external_ci(product):
    if product is None:
        return False
    from asf.harvest import harvest
    return bool(harvest.external_ci(product))


def gate_before_push(product):
    """:data:`GATE_REMOTE` when the product's PRs are gated by external CI, else :data:`GATE_LOCAL`."""
    return GATE_REMOTE if _external_ci(product) else GATE_LOCAL


def gate_before_push_direct(product):
    """:data:`DIRECT_GATE_REMOTE` under external CI, else :data:`DIRECT_GATE_LOCAL`."""
    return DIRECT_GATE_REMOTE if _external_ci(product) else DIRECT_GATE_LOCAL


def context(product, row, kind, facts):
    """Every name a template may use. One flat dict, so a missing key is a missing key."""
    item, feature, epic = facts['item'], facts['feature'], facts['epic']
    sections = facts['sections']
    return {
        'kind': kind,
        'gate_before_push': gate_before_push(product),
        'gate_before_push_direct': gate_before_push_direct(product),
        'pre_push_check': pre_push_block(product),
        'pre_push_doc': pre_push_doc_block(product, kind),
        'test_command': preamble_mod.conventions(product).test_command
        or '(none set — run the tests you add)',
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
        'writes': writes_text(product, facts['writes']),
        'tests': ', '.join(facts['tests']) if facts['tests'] else '(name the test you add)',
        'delivers': ', '.join(facts['delivers']) or '(none)',
        'delivery_count': len(facts['delivers']),
        'delivery_checks': delivery_checks(facts),
        'stories': '; '.join(facts['stories']) if facts['stories'] else '(none yet)',
        'proves': facts['proves_lines'] or '(this Task lists no Story — say so in the report)',
        'description': (sections.get('description') or item.get('title') or '—').strip(),
        'acceptance': (sections.get('acceptance') or '—').strip(),
        'fix': (sections.get('fix')
                or '(the card carries no `## Fix` — write one line saying what you did instead, '
                   'and why)').strip(),
        'groom_file': getattr(row, 'groom_file', '') or '—',
        'answers_file': getattr(row, 'answers_file', '') or '—',
        'open_questions': '\n'.join(getattr(row, 'open_questions', ()) or ()) or '(none)',
        'rank_grammar': ('`rank <n>`, ' if sticky_mod.rank_owner(product) == 'adjudicator'
                         else ''),
        **replan_context(product, feature, facts.get('items') or {}),
    }


def replan_context(product, feature, items):
    """What a ``replan`` brief names: the Feature's ``reshape:`` text (the binding input), its
    digest, the replan's path (:func:`asf.record.replan.doc_path`), and one line per Task of the
    Feature — the landed ones kept as they are, the open ones the replan may rewrite or drop.
    Every key renders for any brief (a missing key is an error), empty-worded when the brief's
    Feature carries no ``reshape:``."""
    from asf.record import replan as replan_mod
    feature = feature or {}
    how = ' '.join(str(feature.get('reshape') or '').split())
    d = replan_mod.digest(how) if how else ''
    plans_dir = preamble_mod.conventions(product).plans_dir
    fid = feature.get('id') or ''
    lines = []
    for t in sorted(ix.feature_tasks(items, feature) if fid else (), key=lambda v: v['id']):
        done = t.get('state') in feeder_rows.DONE_STATES
        after = ', '.join(t.get('after') or ()) or 'none'
        lead = (f"; delivers {', '.join(t['delivers'])}" if t.get('delivers') else
                f"; delivered by {t['delivered_by']}" if t.get('delivered_by') else '')
        lines.append(f"- {t['id']} [{'landed — keep' if done else t.get('state') or 'New'}] "
                     f"{' '.join(str(t.get('title') or '').split())}; writes: "
                     f"{', '.join(t.get('writes') or ()) or '(none)'}; after: {after}{lead}")
    return {
        'reshape': how or '(none)',
        'reshape_digest': d or '(none)',
        'replan_path': replan_mod.doc_path(plans_dir, fid, d) if fid and d else '(none)',
        'feature_tasks': '\n'.join(lines) or '(none)',
    }


#: The one-push rule a correction round's brief ends with (:mod:`asf.workers.pushlog`).
ONE_PUSH_RULE = ('\n\nONE PUSH: this is a correction round. Answer every point above in this one '
                 'session, commit as you go, and push once — `git push` is your last act, never '
                 'a push per fix. Each push starts the product\'s CI again and cancels the run '
                 'before it; a second push in this session is recorded as a defect of the run.')


def correction_text(row, kind):
    """A ``correct`` brief — or a spec/plan brief a refused landing sent back, or the adjudicate
    brief of a branch held at the round cap — ends with the failure the harvest recorded, under
    stall's head; a correction round (:data:`asf.workers.pushlog.ONE_PUSH_KINDS`) then with
    :data:`ONE_PUSH_RULE`."""
    from asf.workers import pushlog
    text = getattr(row, 'correction', '') or ''
    if not (kind in ('correct', 'spec', 'plan', 'adjudicate', 'delivery-code') and text):
        return ''
    return CORRECTION_HEAD + text.rstrip() + (ONE_PUSH_RULE if kind in pushlog.ONE_PUSH_KINDS
                                              else '')


def checks_section(product, kind, head):
    """For a review or correct brief: the product's own ``conventions.check_commands`` results
    for ``head``, already run by the harvest itself (G2 ask 3,
    :mod:`asf.harvest.product_checks`) — the session reads them, it never reruns them. ``''``
    when the kind is not review/correct, the product names no ``check_commands``, there is no
    head, or the harvest has no result for it yet (the first pass right after a push: the check
    still runs in the background, and the next round's brief carries it)."""
    if kind not in ('review', 'correct') or not head or product is None:
        return ''
    commands = product.conventions.get('check_commands')
    repo = getattr(product, 'repo_dir', None)
    if not isinstance(commands, list) or not commands or not repo:
        return ''
    from asf import env as env_mod
    from asf.harvest import product_checks
    setup = getattr(product.conventions, 'worktree_setup', None)
    k = product_checks.key(repo, head, commands, setup)
    got = product_checks.read_result(env_mod.state_dir(product), k) if k else None
    if got is None:
        return ''
    lines = [f'Checks on {head[:9]} (run by the harvest — do not re-run):']
    for r in got['results']:
        status = 'passed' if r['rc'] == 0 else f"failed (exit {r['rc']})"
        lines.append(f"- `{r['command']}` — {status}")
        for t in r.get('tail') or []:
            lines.append(f'    {t}')
    return '\n\n' + '\n'.join(lines)


def delivery_checks(facts):
    """The review's per-item check blocks for a delivery lead (``delivers:``): one table per
    member — the same six checks, judged on that member's commit and its section of the plan
    — and, under each, one row per acceptance line of the Stories its Task proves. ``''`` for
    a branch that delivers one item: the one table above is the verdict. Rendered right after
    the table's last row, so it carries its own leading blank line."""
    members = facts.get('members') or []
    if len(members) < 2:
        return ''
    out = ['', '',
           'THE BRANCH DELIVERS SEVERAL ITEMS — one block per item, in this order, each its own '
           'table of the six checks above, judged on that item\'s own commit and its own section '
           'of the plan. An item no commit names is a block of `fail` rows, not a missing block. '
           'A C names the item it belongs to.', '']
    for member in members:
        mid, mtype, title, writes, acceptance, _fix = member[:6]
        proves = member[6] if len(member) > 6 else ''
        out.append(f"### {mid} — {title} ({mtype}); writes: "
                   f"{', '.join(writes) if writes else '(none declared)'}")
        if acceptance:
            out += ['acceptance:'] + list(acceptance)
        if proves:
            out += ['one row per Story line below — `proves: <S-id> line <n>` pass | fail, with '
                    'the test that proves it:', proves]
        out.append('')
    return '\n'.join(out).rstrip()


def customer_section(product, kind, branch):
    """A review of a diff touching ``conventions.customer_content.paths`` carries the required
    ``read as the customer`` section (:func:`asf.customer_content.review_brief_section`); the
    lane holds a review without its check row as incomplete."""
    if kind != 'review' or product is None or not branch:
        return ''
    from asf import customer_content
    text = customer_content.review_brief_section(product, branch)
    return '\n\n' + text if text else ''


def refusal_section(product, item_id):
    """A relaunch's brief names what the approvals hook refused the item's last run, and why,
    so the session does not repeat it (:func:`asf.approvals.refusal_text`) — ``''`` when
    nothing was refused, when there is no product, or when the ledger cannot be read."""
    if product is None or not item_id or item_id == 'none':
        return ''
    from asf import approvals  # local: approvals imports the workers, which import briefs
    try:
        text = approvals.refusal_text(product, item_id)
    except (OSError, ValueError, KeyError):   # a brief is never lost to the audit ledger
        return ''
    return '\n\n' + text if text else ''


def answer_section(product, item_id):
    """The operator's answers to the item's earlier questions (``asf answer``,
    :func:`asf.workers.answer.brief_section`) — ``''`` when none were given or the file cannot
    be read."""
    if product is None or not item_id or item_id == 'none':
        return ''
    from asf.workers import answer  # local: the workers import the briefs
    try:
        text = answer.brief_section(product, item_id)
    except (OSError, ValueError, KeyError):  # a brief is never lost to the answers file
        return ''
    return '\n\n' + text if text else ''


#: A review of a PR no factory item made (``conventions.merge: auto``, :func:`asf.harvest.lane.
#: pr_item`): what stands in for the card, the plan and the Gate the review table reads against.
FOREIGN_REVIEW = (
    "\n\nTHIS PR WAS OPENED OUTSIDE THE FACTORY — PR #{number} on `{branch}`: no card, no spec, "
    "no plan.\nRead its description (`gh pr view {number}`) as the plan: the scope, Step and "
    "acceptance rows judge the\ndiff against what the description says it does; the Gate row is "
    "the PR's required checks (`gh pr checks {number}`).\nThe merge waits on your verdict: "
    "write `{review_path}` and leave it uncommitted —\nthe factory files it off `{branch}`; a "
    "push there would restart the PR's CI.")


#: The job of a :data:`asf.feeder.rows.NO_STORIES` row — a ``spec-amend`` session in place of the
#: spec-amend template: the Feature has no Story, and it may not be planned until it has one
#: (``feeder.stories_before_plan``).
STORIES_FIRST = """## Your job: derive the Stories of {item_id}

{item_id} has no Story. It may not be planned — no plan, replan or delivery-plan session starts —
until the record holds at least one Story under it with checkbox acceptance lines. Producing
them is this session's job; work already in build keeps building beside it.

DERIVE THE STORIES FROM THE SPEC, THE PLAN AND THE LANDED TASKS. Read the Feature's spec
(`{spec_path}`), its plan (`{plan_path}`) and its landed Tasks before writing one line. The tests
of a landed Task are proof candidates: open them and see what they already assert. The Tasks:
{feature_tasks}

MINT each Story through the record's normal id path, one command per Story:
`asf new story --parent {item_id} --title "<title>" --acceptance "<line>" [--acceptance …]`
Use the id that command prints, and only that id. NO INVENTED IDS: never write an S- id by hand,
never take one from the spec's text, never reuse an id the record already carries. Stories the
record already holds for this Feature: {stories}

EACH ACCEPTANCE LINE IS TESTABLE: one observable behaviour a named test can assert, not a goal
or a quality. A landed test that already proves a line is cited on that line as
`<the line> — proven by <path>` (the test file's path in the repo); a line nothing proves yet
carries no citation, and the plan that follows mints the Task that proves it.

Then amend the spec's `## Stories` block in place so each line carries its minted id, and
leave every other section as it stands. Every commit subject names the card —
`spec({item_id}): <what>`.

{pre_push_doc}Final message: the pushed sha, the minted Story ids, each with its acceptance-line
count and the lines cited as proven."""


def job_template(row, kind):
    """The job text of ``row``'s brief: :data:`STORIES_FIRST` for a
    :data:`asf.feeder.rows.NO_STORIES` row, else the kind's template."""
    if getattr(row, 'kind', '') == feeder_rows.NO_STORIES:
        return STORIES_FIRST
    return load_template(kind)


def foreign_review_text(kind, ctx):
    """:data:`FOREIGN_REVIEW` for a review of a :func:`asf.harvest.lane.pr_item` id, else ''."""
    from asf.harvest.lane import PR_ITEM_RE  # local: the lane imports the briefs
    m = PR_ITEM_RE.match(str(ctx.get('item_id') or ''))
    if kind != 'review' or not m:
        return ''
    return FOREIGN_REVIEW.format(number=int(m.group(1)), branch=ctx['branch'],
                                 review_path=ctx['review_path'])


#: The kinds whose brief quotes the branch's newest review when the factory keeps it off the
#: branch (:mod:`asf.evidence.review_store`): the session cannot open it in its worktree.
STORED_REVIEW_KINDS = ('review', 'fixer', 'adjudicate', 'correct')


def stored_review_section(product, kind, branch, item_id):
    """The newest review of ``item_id`` filed off ``branch``, quoted whole for a
    :data:`STORED_REVIEW_KINDS` brief — a reviewer's previous round, the list a fixer or a
    correction answers, the finding an adjudicator rules on. ``''`` when none is filed."""
    if kind not in STORED_REVIEW_KINDS or product is None or not branch or not item_id \
            or item_id == 'none':
        return ''
    from asf.evidence import review_store
    slug = str(item_id).lower()
    stored = review_store.newest(review_store.root(product), slug, branch)
    if not stored:
        return ''
    path = preamble_mod.review_path_for(product, slug, stored['round'])
    text = stored['text'].rstrip('\n')
    fence = '~' * max(4, max((len(m) for m in re.findall(r'~{3,}', text)), default=0) + 1)
    return (f"\n\nTHE NEWEST REVIEW — `{path}`, round {stored['round']}, of head "
            f"{stored['head'][:12]}. The factory keeps reviews off the branch, so it is not in "
            f"your worktree; this is its text:\n\n{fence}\n{text}\n{fence}")


#: The kinds whose brief carries the item's standing adjudicate rulings verbatim
#: (:mod:`asf.evidence.rulings`): the review that might re-raise a settled point, and the
#: correction or fixer that might be sent to answer one.
RULINGS_KINDS = ('review', 'correct', 'fixer', 'adjudicate')


def rulings_section(product, kind, item):
    """The item's standing rulings (:func:`asf.evidence.rulings.brief_section`) for a
    :data:`RULINGS_KINDS` brief, or ''."""
    if kind not in RULINGS_KINDS or product is None or not (item or {}).get('id'):
        return ''
    from asf.evidence import rulings
    text = rulings.brief_section(rulings.standing(product, item))
    return '\n\n' + text if text else ''


def writes_text(product, writes):
    """The ``{writes}`` a brief prints: the item's footprint, then the product's
    ``conventions.shared_writes`` — append-only files any Task may add to without a widening
    (:func:`asf.feeder.footprint.shared_writes`). ``(none declared)`` for an empty footprint."""
    from asf.feeder import footprint
    if not writes:
        return '(none declared)'
    shared = [w for w in footprint.shared_writes(product) if w not in writes]
    text = ', '.join(writes)
    if shared:
        text += (f" — plus the shared append-only files any Task may add to: {', '.join(shared)}"
                 f" (append your own rows; never rewrite or reorder another's)")
    return text


def build(product, row, index, inflight=None, repo_facts=None):
    """The brief for one feeder row."""
    kind = normalize_kind(getattr(row, 'brief_kind', '') or getattr(row, 'kind', ''))
    facts = preamble_mod.collect(product, row, index, inflight, repo_facts)
    facts['kind'] = kind
    ctx = context(product, row, kind, facts)
    parts = [item_line(row, facts['item']),
             preamble_mod.build(product, row, index, inflight, repo_facts, facts=facts),
             render(job_template(row, kind), ctx).rstrip() + correction_text(row, kind)
             + checks_section(product, kind, facts['head'])
             + customer_section(product, kind, facts['branch'])
             + foreign_review_text(kind, ctx) + refusal_section(product, ctx['item_id'])
             + answer_section(product, ctx['item_id'])
             + rulings_section(product, kind, facts['item'])
             + stored_review_section(product, kind, facts['branch'], ctx['item_id']),
             render(TAIL, ctx)]
    return Brief(kind=kind, item_id=ctx['item_id'], text='\n\n'.join(p.strip() for p in parts) + '\n',
                 model=model_for(product, kind, facts['item'], ctx['item_id']), add_dirs=add_dirs_for(product, row, kind),
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
