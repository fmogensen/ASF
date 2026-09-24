"""asf.briefs.preamble — the facts the runner already knows, written down instead of discovered.

The prior-art reading behind this module is blunt: **93 % of a run's cost was the agent
discovering what the runner already knew** (``docs/research/prior-art-prototype.md``, row 23) —
twelve million input tokens and ninety-six tool calls for one phase, nearly all of it a session
reading its way to the card, the spec, the plan and the branch state that the tick had in hand
before it launched anything. So the preamble is generated, never searched for: the card and its
parents, the documents and their sizes, the footprint, the tests, the last report, the branch,
the conventions and the standing rules, as text, at the top of every brief — including, for each
file the item may write, its top-level functions and classes with their line ranges (the "Where
to look" section, :func:`outline_lines`), so a session opens the ten lines it needs rather than
the whole file.

Two rules keep it honest:

* **No git, no network.** Anything that can change under the builder — a branch head, whether a
  branch exists, a file's line count, the last report — arrives in ``repo_facts``, filled by the
  caller that *does* run git. A fact nobody passed is printed as unknown, never guessed.
* **Identifiers survive truncation.** Over ``conventions.preamble_max_lines`` (default 120) the
  description goes first, then the acceptance, then the last report; the ids, paths, branch and
  footprint are never cut, because a session that loses those goes searching again — which is
  the cost this module exists to remove.
"""
import importlib
import os
import re

from asf.conventions import Conventions
from asf.feeder import rows as feeder_rows
from asf.record import frontmatter
from asf.record.core import parse_sections
from asf.views import index_reader as ix

DEFAULT_MAX_LINES = 120
DEFAULT_MAX_FILES = 12
UNKNOWN = '(not known here)'
NONE = '(none)'

#: The standing rules, when the product yaml sets no ``conventions.rules_tail``. Six lines: the
#: same six every job in this factory owes, whatever its kind.
DEFAULT_RULES = """- Commit with `git commit -s`; the sign-off is the record that you did this work.
- Never push to `{main}`, never force-push, never `--no-verify`, never open a pull request.
- Push your own branch before your turn ends — after every commit, and once at the end even if
  nothing changed.
- Keep a heartbeat: print a progress line as you go; a silent session is read as a dead one and
  relaunched on top of you.
- Finish with the typed REPORT below, as the last thing you print.
- Anything a human must decide or run: `NEEDS OPERATOR: <what> — <the command or the answer>`."""

#: What counts as "a test the card names": a path whose name carries ``test``, with or without a
#: ``::node`` suffix, or any ``file::node`` id. Deliberately narrow — a spec path in the prose is
#: not a test, and a brief that calls one a test sends the session to the wrong file.
TEST_RE = re.compile(r'(?:^|[\s`(\[])([\w./-]*test[\w./-]*\.\w+(?:::[\w.:-]+)?|'
                     r'[\w./-]+::[\w.:-]+)')


# ---- product conventions -----------------------------------------------------

def conventions(product):
    return product.conventions if product is not None else Conventions()


def max_lines(product):
    v = conventions(product).get('preamble_max_lines')
    return v if isinstance(v, int) and v > 0 else DEFAULT_MAX_LINES


def max_files(product):
    v = conventions(product).get('preamble_max_files')
    return v if isinstance(v, int) and v > 0 else DEFAULT_MAX_FILES


def rules_block(product, main='main'):
    """``conventions.rules_tail`` verbatim when set, else the six standing rules."""
    text = conventions(product).get('rules_tail')
    text = text if isinstance(text, str) and text.strip() else DEFAULT_RULES
    return text.replace('{main}', main)


#: The subject prefix per brief kind; any other kind commits as a task.
SUBJECT_KIND = {'spec': 'spec', 'plan': 'plan', 'fix-bug': 'fix', 'fixer': 'fix', 'review': 'review',
                'spec-plan': 'plan', 'direct': 'feat'}


def subject_rule(row, item):
    """The commit-subject line every brief carries, whatever ``rules_tail`` says: harvest holds a
    branch whose commits do not name the item, and an id inside the branch name does not count."""
    kind = SUBJECT_KIND.get(getattr(row, 'brief_kind', None) or '', 'task')
    iid = (item or {}).get('id') or getattr(row, 'item_id', None) or '<item>'
    return (f"- Every commit subject names the item: `{kind}({iid}): <what>`. Harvest holds a "
            f"branch whose commits do not name it; the id inside the branch name does not count.")


#: Carried by every brief of a product whose PRs its external CI gates (whatever ``rules_tail``
#: says): the full suite is that CI's, not this host's — a host running a worker's full suite
#: beside another is the load the tick's host guard holds launches for.
EXTERNAL_CI_RULE = ("- Locally, run only the targeted checks for what you changed, then push: this "
                    "product's external CI runs the full suite on the pull request, and it "
                    "merges only once that CI is green — the gate is never skipped, just run "
                    "off this host.")

#: Carried by every brief of a product that lands fast-forward, gated by harvest's own full-suite
#: run over the combined head (whatever ``rules_tail`` says): a worker running that same suite
#: again before pushing only duplicates the gate that is about to run anyway — B-0127, 6 sessions
#: on the host meant 6 full suites, one from every worker, beside the one harvest ran at landing.
LOCAL_GATE_RULE = ("- Locally, run only the targeted tests for the files you changed, then push: "
                   "harvest's gate runs the full suite on the combined head before anything "
                   "lands — nothing is skipped, it just runs once per landing, not once per "
                   "session.")


def ci_rules(product):
    """``[EXTERNAL_CI_RULE]`` when the product's PRs are gated by external CI
    (:func:`asf.harvest.harvest.external_ci`); ``[LOCAL_GATE_RULE]`` when instead harvest's own
    gate is the one that runs the full suite (:func:`asf.harvest.harvest.local_gate`); else ``[]``
    — its own process stands unchanged."""
    if product is None:
        return []
    from asf.harvest import harvest
    if harvest.external_ci(product):
        return [EXTERNAL_CI_RULE]
    if harvest.local_gate(product):
        return [LOCAL_GATE_RULE]
    return []


def review_path_for(product, slug, n):
    """Where round ``n`` of a review lives — ``conventions.review_pattern``, ``{n}``/``{slug}``."""
    return conventions(product).review_path(slug, n)


def doc_path_for(product, key, slug):
    """A spec/plan path the record does not carry yet: ``<dir>/<slug>.md``."""
    return f'{conventions(product).doc_dir(key)}/{slug}.md'


# ---- the card on disk --------------------------------------------------------

def card_path(product, item):
    """``<backlog_dir>/<folder>/<id>.md``, or None when either half is unknown."""
    root = getattr(product, 'backlog_dir', None) if product is not None else None
    folder = (item or {}).get('folder')
    if not root or not folder or not (item or {}).get('id'):
        return None
    return os.path.join(root, folder, f"{item['id']}.md")


def card_sections(product, item):
    """``{'description': text, 'acceptance': text, ...}`` off the card file, lowercased headings.

    An unreadable or absent card is not an error: the brief then carries what the index holds.
    """
    path = card_path(product, item)
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            _meta, body = frontmatter.parse(f.read(), path=path)
    except (OSError, frontmatter.FrontmatterError):
        return {}
    out = {}
    _lead, secs = parse_sections(body)
    for heading, content in secs:
        out[heading[3:].strip().lower()] = content.strip()
    return out


def section_lines(sections, name, limit_chars=2000):
    """One card section as lines, its paragraph breaks kept — a card's prose reads as the author
    wrote it, or the session reads a wall."""
    text = (sections.get(name) or '').strip()
    if not text:
        return []
    lines = [l.rstrip() for l in text[:limit_chars].splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return lines


# ---- facts -------------------------------------------------------------------

#: Kinds whose preamble carries a review path at all, and of those, the ones that ANSWER the
#: round already on the branch rather than writing the next one. A fact belonging to neither is
#: left out of the preamble: an irrelevant path is a line the session pays for on every turn.
REVIEW_KINDS = ('review', 'fixer', 'adjudicate')
ANSWER_KINDS = ('fixer', 'adjudicate')
STORY_KINDS = ('spec', 'plan', 'review', 'adjudicate', 'spec-plan', 'direct')

#: The keys of ``repo_facts`` that :func:`collect` reads. :func:`asf.briefs.facts.repo_facts` is
#: the one function that fills them, and ``tests.test_brief_facts.ContractTests`` holds the two
#: equal — the drift between what a caller fills and what the preamble reads is the defect that
#: left every brief printing ``(not known here)``.
REPO_FACT_KEYS = ('head', 'branch_exists', 'files', 'tests', 'last_report', 'outlines')


def _strip_rev(value):
    """A link may be recorded as ``<rev>:<path>``; the path is what a session opens."""
    if isinstance(value, str) and ':' in value and not value.startswith(('http://', 'https://')):
        return value.rpartition(':')[2]
    return value if isinstance(value, str) else ''


def _links_of(items, item):
    """The item's own links, with the Feature's spec/plan filled in for a Task or a Story."""
    links = dict((item or {}).get('links') or {})
    feature = feeder_rows.feature_of(items, item) if item else None
    if feature and feature.get('id') != (item or {}).get('id'):
        for key in ('spec', 'plan'):
            if not links.get(key) and (feature.get('links') or {}).get(key):
                links[key] = feature['links'][key]
    return links


def _line_count(repo_facts, path):
    files = (repo_facts or {}).get('files')
    if isinstance(files, dict):
        n = files.get(path)
        return n if isinstance(n, int) else None
    return None


def named_tests(sections, item, repo_facts):
    """Every test the card names — its typed ``tests:`` field, the Fix/Acceptance prose, and
    whatever the caller found in the checkout — once each, in first-seen order."""
    out = []
    for v in (item or {}).get('tests') or []:
        if v and v not in out:
            out.append(str(v))
    for name in ('fix', 'acceptance', 'description'):
        for m in TEST_RE.finditer(sections.get(name) or ''):
            v = m.group(1).strip('`.,;')
            if v and v not in out:
                out.append(v)
    for v in (repo_facts or {}).get('tests') or []:
        if v and v not in out:
            out.append(str(v))
    return out


def _path_of(test):
    """A test name without its ``::node`` suffix — the file a session opens."""
    return str(test).split('::', 1)[0]


def wanted_paths(facts, limit=None, product=None):
    """The paths whose sizes the brief prints, in the order it prints them: the spec, the plan,
    the review file for a review kind, each named test, then each ``writes:`` entry that is a
    literal path. Deduped, cut to ``limit`` (default ``preamble_max_files``). A glob is never
    listed — expanding one could mean thousands of paths for a line nothing prints."""
    paths = [facts.get('spec_path'), facts.get('plan_path')]
    if facts.get('kind') in REVIEW_KINDS:
        paths.append(facts.get('review_path'))
    paths += [_path_of(t) for t in facts.get('tests') or []]
    paths += [w for w in facts.get('writes') or [] if not any(c in w for c in '*?[')]
    out = []
    for p in paths:
        if p and p not in out:
            out.append(p)
    return out[:limit or max_files(product)]


def _test_line(name, files):
    """One named test with its size, or ``(new)`` when the tree was read and lacks it. An empty
    ``files`` means nothing was measured, and the bare name is printed — never a guess."""
    path = _path_of(name)
    if path in files:
        return f'{name} ({files[path]} lines)'
    return f'{name} (new)' if files else str(name)


def unknown_count(text):
    """How many facts the text prints as unknown."""
    return str(text).count(UNKNOWN)


def stories_of(items, feature):
    """``S-0001 title`` per Story of the Feature — the ids a spec's ``## Stories`` block and a
    plan's ``stories:`` lines are checked against."""
    if not feature:
        return []
    return [f"{c['id']} {c.get('title', '')}".rstrip()
            for c in ix.children(items, feature, 'story')]


def kind_of(row):
    """The row's template kind, or ``''`` when it names none.

    Imported late and by name: :mod:`asf.briefs.build` owns the kind table and imports this
    module, and the package re-exports its ``build`` function under the submodule's own name.
    """
    build_mod = importlib.import_module('asf.briefs.build')
    try:
        return build_mod.normalize_kind(getattr(row, 'brief_kind', '')
                                        or getattr(row, 'kind', ''))
    except build_mod.BriefError:
        return ''


def member_facts(product, items, item_id):
    """``(id, type, title, writes, acceptance, fix)`` for one card of a delivery, its sections read
    off the card file and clipped by :func:`section_lines`."""
    member = items.get(item_id) or {}
    sections = card_sections(product, member)
    return (item_id, member.get('type') or '?', member.get('title') or '—',
            list(member.get('writes') or []),
            section_lines(sections, 'acceptance'), section_lines(sections, 'fix'))


def member_lines(members):
    """One block per card of a delivery: title, ``writes:``, ``## Acceptance``, and a Bug's
    ``## Fix``."""
    out = []
    for n, (mid, mtype, title, writes, acceptance, fix) in enumerate(members):
        out += ([''] if n else []) + [
            f"#### {mid} — {title} ({mtype})",
            f"writes: {', '.join(writes) if writes else '(none declared)'}"]
        if acceptance:
            out += ['acceptance:'] + acceptance
        if fix:
            out += ['fix:'] + fix
    return out


def collect(product, row, index, inflight=None, repo_facts=None):
    """Every fact the preamble and the kind templates draw on, as one flat dict."""
    items = feeder_rows.items_of(index) if index else {}
    item_id = getattr(row, 'item_id', '') or ''
    item = items.get(item_id) or {}
    feature = items.get(getattr(row, 'feature_id', '') or '') or \
        (feeder_rows.feature_of(items, item) if item else None) or {}
    epic = (ix.epic_of(items, item) if item else None) or {}
    sections = card_sections(product, item)
    links = _links_of(items, item)
    slug = (item_id or 'item').lower()
    spec_recorded = bool(_strip_rev(links.get('spec')))
    plan_recorded = bool(_strip_rev(links.get('plan')))
    spec_path = _strip_rev(links.get('spec')) or doc_path_for(product, 'spec', slug)
    plan_path = _strip_rev(links.get('plan')) or doc_path_for(product, 'plan', slug)
    rnd = feeder_rows.review_round(feature or item)[1]
    if getattr(row, 'review_round', 0):  # a PR's review: the round harvest asked for
        rnd = int(row.review_round) - 1
    kind = kind_of(row)
    # a reviewer writes the NEXT round's file; a fixer and an adjudicator answer the one that is
    # already on the branch — pointing either at the other's file is how a round gets lost
    read_round = rnd or 1
    review_path = review_path_for(product, slug,
                                  read_round if kind in ANSWER_KINDS else rnd + 1 if rnd else 1)
    delivers = [str(i) for i in item.get('delivers') or []]
    return {
        'kind': kind,
        'items': items,
        'item': item,
        'feature': feature,
        'epic': epic,
        'sections': sections,
        'links': links,
        'spec_path': spec_path,
        'plan_path': plan_path,
        'spec_recorded': spec_recorded,
        'plan_recorded': plan_recorded,
        'files': dict((repo_facts or {}).get('files') or {}),
        'spec_lines': _line_count(repo_facts, spec_path),
        'plan_lines': _line_count(repo_facts, plan_path),
        'writes': list(item.get('writes') or []),
        'outlines': dict((repo_facts or {}).get('outlines') or {}),
        'merged': list(item.get('merged') or []),
        'delivers': delivers,
        'members': [member_facts(product, items, i) for i in delivers],
        'tests': named_tests(sections, item, repo_facts),
        'stories': stories_of(items, feature),
        'round': read_round,
        'next_round': rnd + 1 if rnd else 1,
        'review_path': review_path,
        'branch': getattr(row, 'branch', '') or '',
        'head': (repo_facts or {}).get('head') or '',
        'branch_exists': (repo_facts or {}).get('branch_exists'),
        'last_report': (repo_facts or {}).get('last_report') or '',
        'inflight': list(inflight or []),
    }


# ---- assembling the text -----------------------------------------------------

class Section:
    """One block of the preamble. ``trimmable`` blocks lose lines, worst last, when the
    preamble is over the cap; the rest never do."""

    def __init__(self, name, heading, body, trimmable=False, marker=''):
        self.name = name
        self.heading = heading
        self.body = list(body)
        self.trimmable = trimmable
        self.marker = marker
        self.truncated = False

    def lines(self):
        if not self.body:
            # cut to nothing: one line saying so beats a heading with nothing under it
            return [f'{self.heading} {self.marker}'.strip()] if self.truncated else []
        head = ['', self.heading] if self.heading.startswith('###') else \
            ([self.heading] if self.heading else [])
        out = head + list(self.body)
        if self.truncated:
            out.append(self.marker)
        return out

    def trim(self):
        if not self.body:
            return False
        self.body.pop()
        self.truncated = True
        return True


TRIM_ORDER = ('description', 'acceptance', 'members', 'last_report', 'where')

#: Whether a role reaches a worker session as a launchable sub-agent today — it does not
#: (``asf.roles.roles`` renders a role into the brief's text; nothing here spawns one as a
#: separate, tool-restricted session). :func:`outline_lines` reads this so its closing line never
#: promises a sub-agent no session can actually reach.
LOCATOR_AVAILABLE = False


def fit(sections, limit):
    """Cut the trimmable blocks, in :data:`TRIM_ORDER`, until the whole thing fits."""
    def total():
        return sum(len(s.lines()) for s in sections)

    for name in TRIM_ORDER:
        block = next((s for s in sections if s.name == name), None)
        if block is None:
            continue
        guard = len(block.body) + 2
        while total() > limit and guard > 0 and block.trim():
            guard -= 1
    return [l for s in sections for l in s.lines()]


def _card_marker(product, item):
    path = card_path(product, item)
    return f"…truncated — the whole card is `{path}`" if path else '…truncated'


def _flag(value):
    return {True: 'yes', False: 'no'}.get(value, UNKNOWN)


def _doc_line(label, path, lines, recorded=True):
    """A path the record carries is a fact; one derived from the conventions is where the
    document *goes*, and says so rather than passing for a file that exists."""
    if not recorded:
        return f"{label}: not in the record — its place is `{path}`"
    size = f' ({lines} lines)' if isinstance(lines, int) else ''
    return f"{label}: `{path}`{size}"


def identity_lines(row, facts):
    item, feature, epic = facts['item'], facts['feature'], facts['epic']
    out = [f"Item: {item.get('id', getattr(row, 'item_id', '') or UNKNOWN)} — "
           f"{item.get('title', UNKNOWN)} ({item.get('type', '?')}, "
           f"state {item.get('state', 'New')}, stage {item.get('stage') or '—'})"]
    if item.get('severity'):
        out.append(f"Severity: {item['severity']}")
    out.append(f"Feature: {feature.get('id', '—')} — {feature.get('title', '—')}"
               if feature else 'Feature: —')
    out.append(f"Epic: {epic.get('id', '—')} — {epic.get('title', '—')}" if epic else 'Epic: —')
    out.append(f"Why this session exists: {getattr(row, 'kind', '?')} — "
               f"{getattr(row, 'reason', '') or '—'}")
    return out


def state_lines(product, facts):
    kind = facts.get('kind') or ''
    out = [f"Branch: `{facts['branch'] or '—'}` (exists: {_flag(facts['branch_exists'])})",
           f"Head: {facts['head'] or UNKNOWN}",
           _doc_line('Spec', facts['spec_path'], facts['spec_lines'], facts['spec_recorded']),
           _doc_line('Plan', facts['plan_path'], facts['plan_lines'], facts['plan_recorded'])]
    if kind in REVIEW_KINDS:
        verb, n = ('to answer', facts['round']) if kind in ANSWER_KINDS \
            else ('to write', facts['next_round'])
        out.append(f"Review file {verb}: `{facts['review_path']}` (round {n})")
    tests = '; '.join(_test_line(t, facts['files']) for t in facts['tests']) or '(none named)'
    out += [f"Writes (the footprint this job may touch): "
            f"{', '.join(facts['writes']) if facts['writes'] else '(none declared)'}",
            f"Tests named by the card: {tests}"]
    if facts.get('merged'):
        out.append(f"Also delivers: {', '.join(facts['merged'])} — their sections of "
                   f"{facts['plan_path']}, acceptance byte-identical")
    if facts.get('delivers'):
        out.append(f"Delivery: {len(facts['delivers'])} items, in this order — "
                   f"{', '.join(facts['delivers'])}")
    if facts['stories'] and kind in STORY_KINDS:
        out.append(f"Stories of the Feature: {'; '.join(facts['stories'])}")
    busy = [f"{s.get('item', '?')} ({s.get('kind', '?')}, {s.get('age', '?')})"
            for s in facts['inflight']]
    out.append(f"Sessions in flight: {'; '.join(busy) if busy else NONE}")
    return out


def _outline_range(start, end):
    return str(start) if start == end else f'{start}-{end}'


def outline_lines(facts):
    """The "Where to look" body: for each of the item's ``writes`` that exists on the trunk
    (:func:`asf.briefs.facts.outlines_of`), its line count and its top-level functions/classes
    with their line ranges — at most :data:`asf.briefs.facts.OUTLINE_LIMIT` per file, in the order
    ``writes:`` names them. ``[]`` when nothing in ``writes`` was found on the trunk: no heading
    is printed over an empty list (b110 — a trace once printed a whole file where a pointer would
    have done; an empty section is the same mistake in reverse, a heading over nothing)."""
    outlines = facts.get('outlines') or {}
    out = []
    seen = set()
    for path in facts.get('writes') or []:
        info = outlines.get(path)
        if info is None or path in seen:
            continue
        seen.add(path)
        out.append(f"`{path}` ({info['lines']} lines)")
        for name, kind, start, end in info.get('defs') or []:
            out.append(f'- {name} ({kind}) L{_outline_range(start, end)}')
    if out:
        out.append('Use the locator agent to find anything else before opening whole files.'
                   if LOCATOR_AVAILABLE else 'Read only these line ranges first.')
    return out


def convention_lines(product):
    conv = conventions(product)
    prefixes = conv.map_of('branch_prefixes')
    out = [f"Specs live in `{conv.specs_dir}`, plans in "
           f"`{conv.plans_dir}`, reviews in "
           f"`{conv.reviews_dir}`."]
    if prefixes:
        out.append('Branch prefixes: '
                   + ', '.join(f'{k} → `{v}`' for k, v in sorted(prefixes.items())) + '.')
    main = getattr(product, 'main', 'main') if product is not None else 'main'
    out.append(f"The trunk is `{main}`; every commit is signed off (`git commit -s`).")
    return out


def build(product, row, index, inflight=None, repo_facts=None, facts=None):
    """The preamble text for one row — capped, identifiers intact."""
    facts = facts if facts is not None else collect(product, row, index, inflight, repo_facts)
    main = getattr(product, 'main', 'main') if product is not None else 'main'
    marker = _card_marker(product, facts['item'])
    sections = [
        Section('identity', '## What is already known (do not go looking for it)',
                identity_lines(row, facts)),
        Section('state', '', state_lines(product, facts)),
        Section('conventions', '', convention_lines(product)),
        Section('description', '### Description',
                section_lines(facts['sections'], 'description') or
                ([facts['item'].get('title')] if facts['item'].get('title') else []),
                trimmable=True, marker=marker),
        Section('acceptance', '### Acceptance',
                section_lines(facts['sections'], 'acceptance'), trimmable=True, marker=marker),
        Section('members', '### The items of this delivery', member_lines(facts['members']),
                trimmable=True, marker=marker),
        Section('links', '### Links the card names',
                section_lines(facts['sections'], 'links', limit_chars=600)),
        Section('where', '### Where to look', outline_lines(facts),
                trimmable=True, marker='…truncated'),
        Section('last_report', '### The last report for this item',
                [l.rstrip() for l in str(facts['last_report']).splitlines() if l.strip()],
                trimmable=True, marker='…truncated'),
        Section('rules', '### Standing rules',
                rules_block(product, main).splitlines() + [subject_rule(row, facts['item'])]
                + ci_rules(product)),
    ]
    return '\n'.join(fit(sections, max_lines(product)))
