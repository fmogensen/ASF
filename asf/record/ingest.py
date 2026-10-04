"""asf.record.ingest — the evidence pass (``asf ingest``).

Matches every Feature/Story/Task/Bug/Epic to what ``asf.evidence`` found in the product repo and
hands that evidence to ``asf.evidence.closing``, which alone chooses the state and names the rule
that chose it (``rule: <name>`` is the last ``evidence:`` line of every item). It then writes the
machine block with ``frontmatter.write_machine`` (the only function allowed to touch the machine
block) and appends one ``## History`` line per state or stage change. The pass runs
``tasks → stories → bugs → features → descent → epics → write``; descent is what lets a landed
Feature close the children beneath it that have no evidence of their own. Ingest derives no state
of its own: ``tests.test_ingest.IngestDerivesNothing`` keeps a sixth state choice out of this
module. Decisions and Rules have no lifecycle, so ingest never touches them.
"""
import collections
import dataclasses
import json
import os
import re
import sys

from asf import env, proves, reviews
from asf.evidence import closing, evidence
from asf.record import decisions, frontmatter, writer
from asf.record.core import is_retired as core_is_retired
from asf.record.core import canonicalize, load_items, now_iso, parse_sections, render_sections, section_content, today
from asf.record.index import do_index
from asf.tick import stale

EVIDENCE_TYPES = {'epic', 'feature', 'story', 'task', 'bug'}
LANDING_CHILD_TYPES = {'story', 'task', 'bug'}
# the keys ingest derives, in the order it writes them; any other machine key (schema_version,
# spend_usd, ...) is someone else's and is carried through untouched — ingest never drops a key
MACHINE_KEY_ORDER = ['schema_version', 'state', 'stage', 'stage_since', 'cost', 'evidence',
                     'blocked', 'blocked_by_open', 'updated']
RULE_PREFIX = 'rule: '
# the `metrics/events` kind written when a Feature's stage enters `on-prod`, the durable record
# read by the rollup's first line (F-0044)
ON_PROD_EVENT = 'feature-on-prod'

# ---- the `landing:` stamp: which commit closed a Task/Bug/Story, when, and by which path -------
#: the machine key; a dict ``{sha, as_of, by}`` (``reverts:`` / ``note:`` beside it when they apply)
LANDING_KEY = 'landing'
#: the types a close stamps (a Feature/Epic closes by its children, which carry the stamps)
LANDING_TYPES = ('task', 'bug', 'story')
#: every ``by:`` a stamp may carry: the path whose evidence closed the card. ``pr-merge`` a merged
#: PR or the lane's landing of the run's branch; ``names`` a trunk commit whose subject names the
#: id; ``trunkclose/<arm>`` a run closed on verified trunk evidence, by the arm that attributed
#: the sha; ``groom`` a landing the groom's covers rule accepted; ``console`` a typed ``landed:``;
#: ``children`` a Story whose Tasks closed; ``matrix`` a Story its plan's matrix closed;
#: ``descent`` a child its closed parent closed; ``migration`` stamped after the fact by
#: ``asf migrate-landing``; ``reverted`` the landing commit was reverted on the trunk.
LANDING_BY = ('pr-merge', 'names', 'trunkclose/names', 'trunkclose/pr', 'trunkclose/covers',
              'console', 'groom', 'children', 'matrix', 'descent', 'migration', 'reverted')
_DONE_STATES = ('Resolved', 'Closed')
_FULL_SHA_RE = re.compile(r'[0-9a-f]{40}')
_KEEP, _DROP = object(), object()   # :func:`_ingest_fields`' `landing`: leave it, or clear it


def landing_stamp(sha, by, as_of, reverts=None):
    """The ``landing:`` value: ``{sha, as_of, by}`` with ``sha`` the full 40-hex commit (``''``
    when none is known). A ``sha`` the trunk has since reverted (``reverts``: ``{reverted sha:
    reverting sha}``, :func:`asf.evidence.evidence.trunk_reverts`) is written as
    ``{sha: '', as_of, by: reverted, reverts: <sha>}`` — the landing no longer stands."""
    sha = str(sha or '')
    if sha and (reverts or {}).get(sha):
        return {'sha': '', 'as_of': as_of, 'by': 'reverted', 'reverts': sha}
    return {'sha': sha if _FULL_SHA_RE.fullmatch(sha) else '', 'as_of': as_of, 'by': by}


def _by_of_ids(iev):
    """The ``by:`` of a landing read off an id's evidence (:func:`evidence.id_evidence`): a run
    closed on trunk evidence (:func:`asf.groom.policy.landing_by`), a lane merge fact or a merged
    PR (``pr-merge``), else a trunk commit whose subject names the id (``names``)."""
    if iev.get('trunk_closed'):
        from asf.groom import policy
        return policy.landing_by(iev['trunk_closed'], iev.get('trunk_arm') or '')
    if 'merge' in iev or iev.get('pr'):
        return 'pr-merge'
    return 'names'


def landing_of(iid, rule, canonical, ev, task_ev, derived, story_tasks=None):
    """``(sha, by)`` of the evidence the closing rule ``rule`` closed ``iid`` on — the sha may be
    short (a typed ``landed:``) or ``''`` (a matrix close with no landed Task)."""
    meta = canonical[iid]['meta']
    type_ = meta.get('type')
    if rule == 'reconciled':
        return str(frontmatter.split_machine(meta)[0].get('landed') or ''), 'console'
    if rule == 'parent-closed':
        # the parent's OWN landing, never a sha it borrowed from a child: a Feature closed on its
        # Tasks carries the newest Task's merge, and that merge lands that Task, not this one
        parent, seen = meta.get('parent'), set()
        while parent and parent in canonical and parent not in seen:
            seen.add(parent)
            if derived.get(parent) is not None and derived[parent].own_sha:
                return derived[parent].own_sha, 'descent'
            parent = canonical[parent]['meta'].get('parent')
        return '', 'descent'
    if type_ == 'story':
        tasks = (story_tasks if story_tasks is not None else _story_tasks(canonical)).get(iid, [])
        return _landing_sha(tasks, task_ev, ev), ('matrix' if rule.startswith('matrix')
                                                  else 'children')
    sha = derived[iid].sha if derived.get(iid) is not None else ''
    iev = _ids_of(iid, ev)
    if sha and sha == iev.get('commit'):
        return sha, _by_of_ids(iev)
    return sha, 'pr-merge'


def _landing_update(machine, new_state, now, reverts, closes):
    """What the write does to the card's ``landing:``: :data:`_KEEP`, :data:`_DROP` (the card is
    open again), or a new value — the stamp when the card closes now and carries none
    (``closes()`` gives it), the reverted form when its landing commit was reverted."""
    old = machine.get(LANDING_KEY)
    if new_state not in _DONE_STATES:
        return _DROP if old is not None else _KEEP
    if isinstance(old, dict):
        sha = str(old.get('sha') or '')
        if sha and old.get('by') != 'reverted' and (reverts or {}).get(sha):
            return landing_stamp(sha, old.get('by'), now, reverts)
        return _KEEP
    if old is None and machine.get('state', 'New') != new_state:
        return closes()
    return _KEEP


def write_on_prod_event(root, iid, from_stage, now):
    """Append one `metrics/events/<day>.jsonl` line for `iid`'s transition into `on-prod`.
    Idempotent per day (returns False and writes nothing on a repeat): the metrics clone this
    feeds is reset and re-derived every cycle. Not `asf.metrics.metrics.append_event`: its
    `natural_key` has no `events` branch and raises `KeyError` on a line with no `tick`."""
    path = os.path.join(root, 'metrics', 'events', f"{now[:10]}.jsonl")
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:  # the stream is unvalidated (§1.1); a rollup must not fail on it
                    continue
                if obj.get('kind') == ON_PROD_EVENT and obj.get('item') == iid:
                    return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps({'from': from_stage or '', 'item': iid, 'kind': ON_PROD_EVENT, 'ts': now},
                      sort_keys=True, ensure_ascii=False)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(line + '\n')
    return True


def is_retired(meta):
    """A card with `removed:` or `moved_to:` wants no stage, no Tasks and no session."""
    return core_is_retired(meta)


def _path_only(ref):
    """Strip the "rev:" prefix off an evidence path: a typed `links.spec` is a bare repo path."""
    if not ref:
        return None
    return ref.split(':', 1)[1] if ':' in ref else ref


def _feature_branches(fev):
    out = {fev.get('spec_branch'), fev.get('plan_branch')}
    for t in fev['tasks'].values():
        out.add(t.get('branch'))
    out.discard(None)
    return out


DOC_FIELDS = {'spec': ('spec', 'spec_branch', 'spec_on_main', 'spec_review'),
              'plan': ('plan', 'plan_branch', 'plan_on_main', 'plan_review', 'tasks')}


def _doc_rank(f, kind):
    """How far a document of `kind` got in `f`: on the trunk 2, on a branch 1, none 0."""
    if not f:
        return 0
    return 2 if f.get(f'{kind}_on_main') else 1 if (f.get(kind) or f.get(f'{kind}_branch')) else 0


def _merge_docs(primary, others):
    """`primary` with its spec and its plan each taken from whichever candidate got that document
    furthest — a Feature matched by its legacy alias to an old spec still reads the plan its own
    lane landed (`f-0047.md`, or a date-prefixed plan its lane branch carried)."""
    out = dict(primary)
    for kind, keys in DOC_FIELDS.items():
        best = primary
        for f in others:
            if _doc_rank(f, kind) > _doc_rank(best, kind):
                best = f
        if best is not primary:
            for k in keys:
                out[k] = best.get(k) if k != 'tasks' else (best.get(k) or {})
    prs = list(primary.get('prs') or [])
    for f in others:
        prs += [p for p in f.get('prs') or [] if p not in prs]
    out['prs'] = prs
    return out


def _own_candidates(iid, ev):
    """[(slug, feature_evidence)] the Feature's own id reaches: the lane's slug (`f-0047`, or
    `F-0047` off a live `plan-F-0047` branch), and every document its merged spec/plan lane PR
    landed, whatever the file is called."""
    features = ev.get('features') or {}
    out = []
    if not iid:
        return out
    for slug in dict.fromkeys((iid.lower(), iid.upper(), iid)):
        if slug in features:
            out.append((slug, features[slug]))
    lane = (ev.get('lane_docs') or {}).get(iid.upper()) or {}
    for kind in ('spec', 'plan'):
        for path in lane.get(kind) or []:
            for slug, f in features.items():
                if _path_only(f.get(kind)) == path and (slug, f) not in out:
                    out.append((slug, f))
    # a document whose own header names the id (`# F-0019 — Free plan`), whatever its file name
    for slug, f in features.items():
        if str(f.get('alias') or '').upper() == iid.upper() and (slug, f) not in out:
            out.append((slug, f))
    return out


def match_feature(meta, ev):
    """(slug, feature_evidence) by links.spec/plan path, then legacy_id, then branches, then PRs,
    then the Feature's own id (its lane's slug, its merged lane PR's documents). The spec and the
    plan are each read from whichever match carries it furthest (:func:`_merge_docs`)."""
    typed, _machine = frontmatter.split_machine(meta)
    slug, f = _match_feature(typed, meta, ev)
    # the typed intent first: a `links.plan` names the plan, whatever `links.spec` matched
    plan_link = (typed.get('links') or {}).get('plan')
    typed_plan = [(s, x) for s, x in (ev.get('features') or {}).items()
                  if plan_link and _path_only(x.get('plan')) == plan_link]
    own = []
    for s, x in typed_plan + _own_candidates(str(typed.get('id') or meta.get('id') or ''), ev):
        if s != slug and s not in [o[0] for o in own]:
            own.append((s, x))
    if not own:
        return slug, f
    if f is None:
        slug, f = own[0]
        own = own[1:]
    return slug, _merge_docs(f, [x for _s, x in own])


def _match_feature(typed, meta, ev):
    """The first match by links, legacy id, branches, PRs, then the lane's slug."""
    links = typed.get('links') or {}
    features = ev['features']

    spec_link = links.get('spec')
    if spec_link:
        for slug, f in features.items():
            if _path_only(f.get('spec')) == spec_link:
                return slug, f
    plan_link = links.get('plan')
    if plan_link:
        for slug, f in features.items():
            if _path_only(f.get('plan')) == plan_link:
                return slug, f
    legacy = typed.get('legacy_id')
    if legacy:
        for slug, f in features.items():
            if f.get('alias') == legacy:
                return slug, f
    branches = set(links.get('branches') or [])
    if branches:
        for slug, f in features.items():
            if branches & _feature_branches(f):
                return slug, f
    prs = set(links.get('prs') or [])
    if prs:
        for slug, f in features.items():
            if prs & set(f.get('prs') or []):
                return slug, f
    # the lane's own convention (B-0059): spec/<ID> lands docs/specs/<id>.md, plan/<ID> lands
    # docs/plans/<id>.md — the slug is the card's id, lower-cased, and needs no typed link
    own = str(typed.get('id') or meta.get('id') or '').lower()
    if own and own in features:
        return own, features[own]
    return None, None


TASK_LEGACY_RE = re.compile(r'^(?P<alias>.+)/(?P<tid>T\d+[a-z]?)$', re.IGNORECASE)


def match_task(meta, ev):
    """(slug, tid, task_evidence) by plan+legacy T<n>, then by id/PR/branch carrying the item's
    own id, then by links.branches/links.prs."""
    typed, _machine = frontmatter.split_machine(meta)
    links = typed.get('links') or {}
    legacy = typed.get('legacy_id') or ''

    m = TASK_LEGACY_RE.match(legacy)
    if m:
        alias, tid = m.group('alias'), m.group('tid').upper()
        for slug, f in ev['features'].items():
            if f.get('alias') == alias and tid in f['tasks']:
                return slug, tid, f['tasks'][tid]
    plan_link = links.get('plan')
    if plan_link and m:
        tid = m.group('tid').upper()
        for slug, f in ev['features'].items():
            if _path_only(f.get('plan')) == plan_link and tid in f['tasks']:
                return slug, tid, f['tasks'][tid]

    own_id = typed.get('id') or ''
    if own_id:
        for slug, f in ev['features'].items():
            for tid, t in f['tasks'].items():
                if t.get('branch') and own_id.lower() in t['branch'].lower():
                    return slug, tid, t

    branches = set(links.get('branches') or [])
    if branches:
        for slug, f in ev['features'].items():
            for tid, t in f['tasks'].items():
                if t.get('branch') in branches:
                    return slug, tid, t
    prs = set(links.get('prs') or [])
    if prs:
        for slug, f in ev['features'].items():
            for tid, t in f['tasks'].items():
                if t.get('pr') in prs:
                    return slug, tid, t
    return None, None, None


def match_story(meta, ev):
    """(legacy_id, story_evidence) — a Story's legacy_id is its matrix row id (e.g. F-ID-1)."""
    typed, _machine = frontmatter.split_machine(meta)
    legacy = typed.get('legacy_id')
    if legacy and legacy in ev['stories']:
        return legacy, ev['stories'][legacy]
    return None, None


def match_bug(meta, ev):
    """{'has_fixer', 'merged_sha', 'branches', 'open_prs'} from links.branches/links.prs, or None."""
    typed, _machine = frontmatter.split_machine(meta)
    links = typed.get('links') or {}
    prs = set(links.get('prs') or [])
    branches = set(links.get('branches') or [])
    if not prs and not branches:
        return None
    has_fixer = bool(prs)
    merged_sha = None
    for pr in prs:
        if pr in ev['merged']:
            merged_sha = ev['merged'][pr]
    live = sorted(branches & set(ev.get('branches') or []))
    if live:
        has_fixer = True
    return {'has_fixer': has_fixer, 'merged_sha': merged_sha, 'branches': live,
            'open_prs': sorted(pr for pr in prs if pr not in ev['merged'])}


def match_ids(iid, ev):
    """(state, [evidence line]) from the id tokens naming `iid` in a branch, a PR or a commit on
    main — `(None, [])` when nothing names it. A `Resolved`/`Closed` verdict here outranks every
    other source (B-0074: a landing beats a plan's task table); an `Active` one only fills what
    the legacy-id and spec-path matching left unmatched."""
    iev = (ev.get('ids') or {}).get(iid)
    return evidence.id_state(iid, iev, has_ci=bool(ev.get('ci')))


def _section_lines(content):
    """The `- ...` lines inside a section's raw content, as section_content() would round-trip."""
    return [l for l in content.split('\n') if l.strip() != '']


def append_section_lines(body, heading, new_lines):
    """`new_lines` appended inside the section named `heading` — the body unchanged if no
    section carries that heading."""
    if not new_lines:
        return body
    preamble, sections = parse_sections(body)
    n = len(sections)
    out_sections = []
    for idx, (section_heading, content) in enumerate(sections):
        if section_heading.strip() == heading:
            is_last = idx == n - 1
            content = section_content(_section_lines(content) + list(new_lines), is_last)
        out_sections.append([section_heading, content])
    return render_sections(preamble, out_sections)


def append_history_lines(body, new_lines):
    return append_section_lines(body, '## History', new_lines)


def _phrase(ev_lines):
    """The History parenthesis: the rule that decided, then the lines that show why."""
    if not ev_lines:
        return 'derived'
    rules = [l for l in ev_lines if l.startswith(RULE_PREFIX)]
    return '; '.join(rules[-1:] + [l for l in ev_lines if not l.startswith(RULE_PREFIX)])


def _ingest_fields(machine, new_state, stage, ev_lines, blocked_pair, now, landing=_KEEP):
    """(ordered_machine_or_None, [history_line, ...]) — None means "no change, skip the write".
    ``landing`` is :func:`_landing_update`'s answer: keep the stamp, drop it, or write this one."""
    old_state = machine.get('state', 'New')
    old_stage = machine.get('stage')
    fields = dict(machine)
    if landing is _DROP:
        fields.pop(LANDING_KEY, None)
    elif landing is not _KEEP:
        fields[LANDING_KEY] = landing
    fields['state'] = new_state
    if stage is not None:
        fields['stage'] = stage
    else:
        fields.pop('stage', None)
    if ev_lines is not None:
        fields['evidence'] = ev_lines
    else:
        fields.pop('evidence', None)
    blocked, open_blockers = blocked_pair
    if blocked:
        fields['blocked'] = True
        fields['blocked_by_open'] = open_blockers
    else:
        fields.pop('blocked', None)
        fields.pop('blocked_by_open', None)

    tracked_changed = (stage != old_stage) if stage is not None else (new_state != old_state)
    fields['stage_since'] = now if (tracked_changed or 'stage_since' not in machine) else machine['stage_since']

    old_cmp = {k: v for k, v in machine.items() if k != 'updated'}
    new_cmp = {k: v for k, v in fields.items() if k != 'updated'}
    if new_cmp == old_cmp:
        return None, []

    fields['updated'] = now
    ordered = {k: fields[k] for k in MACHINE_KEY_ORDER if k in fields}
    ordered.update((k, v) for k, v in fields.items() if k not in ordered)

    history = []
    stamp = now[:16].replace('T', ' ')
    if new_state != old_state:
        phrase = _phrase(ev_lines)
        history.append(f"- {stamp} ingest: state {old_state} → {new_state} ({phrase})")
    if stage is not None and stage != old_stage:
        phrase = _phrase(ev_lines)
        history.append(f"- {stamp} ingest: stage {old_stage or 'card'} → {stage} ({phrase})")
    if isinstance(landing, dict) and landing.get('by') == 'reverted' \
            and (machine.get(LANDING_KEY) or {}).get('by') != 'reverted':
        history.append(f"- {stamp} ingest: landing {str(landing.get('reverts'))[:9]} reverted "
                       f"on the trunk")
    return ordered, history


def write_fields(path, machine, ordered):
    """Merge ingest's fields into the card's machine block (I1): only the keys whose value
    changed are re-rendered, only the derivable keys ingest no longer derives are dropped, and
    every other key — ``schema_version``, ``cost``, one a later version adds — keeps its line
    byte for byte. The block is never rebuilt."""
    updates = {k: v for k, v in ordered.items() if k not in machine or machine[k] != v
               or type(machine[k]) is not type(v)}
    drop = [k for k in machine if k not in ordered]
    return frontmatter.merge_machine(path, updates, drop=drop, order=MACHINE_KEY_ORDER)


def _ids_of(iid, ev):
    return (ev.get('ids') or {}).get(iid) or {}


def _own_ids(iid, ev):
    """An id token naming `iid` in a branch or an open PR: evidence of its own that it is moving."""
    iev = _ids_of(iid, ev)
    return bool(iev.get('branches') or iev.get('open_prs'))


def _child_landing(cid, task_ev, ev):
    """``(sha, pr)`` a child Task landed by: its plan row's merged PR, else the merge fact or
    trunk commit naming its id (a Task no plan row lists still lands, T-0366)."""
    tev, iev = task_ev.get(cid) or {}, _ids_of(cid, ev)
    return (tev.get('merged_sha') or iev.get('commit') or '',
            tev.get('pr') if tev.get('merged_sha') else (iev.get('pr') or tev.get('pr')))


def _prod_reach(ev, product=None):
    """``sha -> bool``: is ``sha`` inside the deployed ``prod_sha``. With a product, one
    ``rev-list`` of the deploy answers every question a derive asks, read the first time one is
    asked (a ``merge-base`` fork per Task, per tick, was the alternative); without one, each
    question is :func:`evidence.ancestor_of`'s, as it always was."""
    base = ev.get('prod_sha')
    if product is None:
        return lambda sha: evidence.ancestor_of(sha, base)
    reach = []

    def check(sha):
        if not sha or not base:
            return False
        if not reach:
            reach.append(evidence.ancestry(product, [base]))
        return reach[0](sha)
    return check


def _trunk_reach(ev, product=None):
    """``sha -> bool``: is ``sha`` on the trunk — an ancestor of (or equal to) ``main_sha``, the
    ``origin/<main>`` the evidence was read at. One ``rev-list`` with a product, read the first
    time one is asked; without one, :func:`evidence.ancestor_of`'s answer. No ``main_sha``, or a
    sha git cannot place, is not on the trunk: nothing closes on a sha the trunk does not carry."""
    base = ev.get('main_sha')
    if product is None:
        return lambda sha: bool(sha and base) and evidence.ancestor_of(sha, base)
    reach = []

    def check(sha):
        if not sha or not base:
            return False
        if not reach:
            reach.append(evidence.ancestry(product, [base]))
        return reach[0](sha)
    return check


def _voided_landings(product):
    """``{item: (shas, prs)}`` — the landing claims ``asf reset`` voided, off the product's run
    lines: a void's own ``head``/``pr`` (:func:`asf.workers.lifecycle.voids`), and every run whose
    merge claim :func:`asf.workers.lifecycle.voided_run` voids (its merge sha, its lane's head and
    sha) — the check :func:`asf.evidence.evidence.merge_facts` and ``trunkclose`` make. ``{}``
    with no product or no ledger."""
    if product is None:
        return {}
    from asf.workers import lifecycle
    try:
        path = os.path.join(env.state_dir(product), 'sessions.jsonl')
    except Exception:
        return {}
    if not os.path.exists(path):
        return {}
    out = {}

    def add(item, shas=(), pr=None):
        rec = out.setdefault(str(item).upper(), (set(), set()))
        rec[0].update(str(x) for x in shas if x)
        if pr:
            rec[1].add(pr)
    for rs in lifecycle.runs(path).values():
        for run in rs:
            item = run.get('item')
            if not item or not isinstance(item, str):
                continue
            sha = evidence._merge_sha(run)
            if sha is None or not lifecycle.voided_run(path, run, sha):
                continue
            lane = run.get('lane') if isinstance(run.get('lane'), dict) else {}
            add(item, (sha, lane.get('head'), lane.get('sha')))
    for item in list(out) + [str(i).upper() for i in lifecycle.resets(path)]:
        for v in lifecycle.voids(path, item):
            add(item, (v.get('head'),), v.get('pr'))
    return out


def _void_ids(ev, voided):
    """``(ev, hit)``: ``ev`` with every id's trunk evidence that is a voided landing claim
    (:func:`_voided_landings`) taken out — its commit, merge, PR and green — and ``{id: sha}`` of
    what was taken. A reset ``(pr, head)`` is never a merge fact, whatever the host says (W4-PR5):
    the commit naming the id, or the PR the host merged, is that same voided claim."""
    if not voided:
        return ev, {}
    ids, hit = dict(ev.get('ids') or {}), {}
    for iid, iev in (ev.get('ids') or {}).items():
        shas, prs = voided.get(str(iid).upper(), ((), ()))
        c = iev.get('commit') or ''
        if c and (any(_sha_prefix(c, v) for v in shas) or (iev.get('pr') and iev['pr'] in prs)):
            rec = {k: v for k, v in iev.items() if k not in ('merge', 'trunk_closed', 'trunk_arm')}
            rec.update(commit=None, pr=None, green=False)
            ids[iid], hit[iid] = rec, c
    return (dict(ev, ids=ids) if hit else ev), hit


def _sha_prefix(a, b):
    a, b = str(a or ''), str(b or '')
    return len(a) >= 7 and len(b) >= 7 and (a.startswith(b) or b.startswith(a))


def _typed_landed(meta):
    """The card's typed ``landed:`` sha (§2.5) when it is shaped like one (``asf check``'s
    :data:`asf.record.check.LANDED_SHA_RE`), lower-cased; ``''`` otherwise."""
    from asf.record import check
    value = str(frontmatter.split_machine(meta)[0].get('landed') or '').strip()
    return value.lower() if value and check.LANDED_SHA_RE.fullmatch(value) else ''


def _merged_in_prod(child_ids, task_ev, ev, reach=None):
    """Every child Task's landing sha is an ancestor of the deploy. False for a child nothing
    says landed: nothing says where its merge is. `reach` is a :func:`_prod_reach` to ask."""
    reach = reach or _prod_reach(ev)
    return bool(child_ids) and all(
        reach(_child_landing(cid, task_ev, ev)[0]) for cid in child_ids)


def _deploys(ev):
    """The product deploys to prod. ``prod_deploys`` is discover()'s answer; an evidence cache
    written before it existed says so by ``prod_sha`` alone, as it always did."""
    return bool(ev.get('prod_deploys')) if 'prod_deploys' in ev else bool(ev.get('prod_sha'))


def _in_prod(child_ids, task_ev, ev, merged=None, own_sha='', reach=None):
    """What "in production" means is the product's own (B-0077). A package, library or tool
    configures no deploy — its trunk IS production, and its Tasks already closed on a commit
    there with CI green (D-0047), so there is nothing further to wait for. Without this, no
    Feature of such a product could ever leave `Resolved`.

    A product that deploys needs every child's landing sha (or, with no children, ``own_sha``)
    inside the sha recorded as deployed — and a deploy it configures but cannot read (no
    successful prod run known) is never "in prod". Then ``deploy_sha.prod.mode`` decides:
    ``auto`` — ASF dispatched that deploy itself, so containment is the whole proof; ``manual``
    — the operator also ticked each child's PR into ``checked.txt``.
    `merged` hands back a `_merged_in_prod` the caller already has; `reach` a `_prod_reach`."""
    if not _deploys(ev):
        return True
    if not ev.get('prod_sha'):
        return False
    if not child_ids:
        return ev.get('prod_mode') == 'auto' and bool(own_sha) and \
            (reach or _prod_reach(ev))(own_sha)
    if ev.get('prod_mode') != 'auto':
        checked = ev.get('checked') or ()
        if not all(_child_landing(cid, task_ev, ev)[1] in checked for cid in child_ids):
            return False
    return bool(_merged_in_prod(child_ids, task_ev, ev, reach) if merged is None else merged)


_MERGED_INTO = re.compile(r'\bmerged into ([Tt]-\d{4})\b')
_REMOVED_ON = re.compile(r'\((?:groom|replan)?\s*(\d{4}-\d{2}-\d{2})')


def orphaned_tasks(canonical, derived):
    """The removed Tasks whose scope no live Task carries any more: each was merged into a
    survivor (the survivor's ``merged:`` names it, or its own ``removed: merged into T-…``), and
    following survivors ends at a Task that is itself removed and never landed — its own rule,
    before the terminal hold, says neither Resolved nor Closed (a survivor a wrong descent once
    closed stays held Closed, and must not vouch for the scope folded into it). A groom that
    removes the survivor — "the deliverable is already on main" — speaks for the survivor's own
    scope, never for the Tasks folded into it: those are still the Feature's work to do. Such a
    Task still counts among its Feature's children, and a closing parent never descends onto it
    — until a replan of its Feature is applied on or after the survivor's removal: the replan
    re-cuts every open Task of the Feature, so the scope lives on in the Tasks it minted."""
    into = {}
    for iid, rec in canonical.items():
        meta = rec['meta']
        if meta.get('type') != 'task':
            continue
        for other in meta.get('merged') or ():
            into.setdefault(str(other).upper(), iid)
    for iid, rec in canonical.items():
        m = _MERGED_INTO.search(str(rec['meta'].get('removed') or ''))
        if rec['meta'].get('type') == 'task' and m:
            into.setdefault(iid, m.group(1).upper())
    out = set()
    for iid, rec in canonical.items():
        if rec['meta'].get('type') != 'task' or not rec['meta'].get('removed') or iid not in into:
            continue
        cur, seen = into[iid], {iid}
        while cur in into and cur not in seen and (canonical.get(cur) or {}).get(
                'meta', {}).get('removed'):
            seen.add(cur)
            cur = into[cur]
        survivor = (canonical.get(cur) or {}).get('meta') or {}
        own = derived.get(cur)
        if not survivor.get('removed') or (own.raw.state if own else closing.NEW) in (
                closing.RESOLVED, closing.CLOSED):
            continue
        feature = (canonical.get(rec['meta'].get('parent')) or {}).get('meta') or {}
        replanned = str(feature.get('reshape_applied_at') or '')[:10]
        when = _REMOVED_ON.search(str(survivor.get('removed') or ''))
        if replanned and (not when or replanned >= when.group(1)):
            continue
        out.add(iid)
    return out


def _landing_sha(child_ids, task_ev, ev):
    """The newest commit or merge that landed one of `child_ids`, for the line that names it."""
    for cid in reversed(child_ids):
        sha = _ids_of(cid, ev).get('commit') or (task_ev.get(cid) or {}).get('merged_sha')
        if sha:
            return sha
    return ''


def _green_after(ev, product):
    """`sha -> bool`: CI on main is green at or after `sha`. True for a product with no CI; the
    green runs are read once, the first time a merge asks."""
    if not ev.get('ci'):
        return lambda sha: True
    reach = []

    def check(sha):
        if not reach:
            runs = evidence.ci_green_runs(product) if product is not None else []
            reach.append(evidence.ancestry(product, runs) if runs else (lambda _sha: False))
        return reach[0](sha)
    return check


def _seconds_since(stamp, now):
    """Seconds from a card's date or ISO stamp to `now`; 0 when it cannot be read, which is the
    timid answer for a quiet period (not quiet yet)."""
    text = str(stamp or '')
    then = stale.parse_iso(text) or (stale.parse_iso(text[:10] + 'T00:00:00Z') if len(text) >= 10 else None)
    at = stale.parse_iso(now)
    return max((at - then).total_seconds(), 0.0) if then and at else 0.0


def _own_evidence(ev_obj):
    return bool(ev_obj.commit or ev_obj.merged_sha or ev_obj.branch or ev_obj.pr_state
                or ev_obj.open_prs)


def _spec_home(meta, fev, ev, product):
    """``(on_trunk, carrier_branch)`` for a matched Feature's spec. The lane's discovery answers
    first: on the trunk, or on its spec/plan branch. A typed ``links.spec`` the lane never saw —
    a migrated card's spec on a pre-lane branch with no PR — is looked for on the trunk and on
    every remote branch, so a spec reached only by its link counts where it really is."""
    if fev.get('spec_on_main'):
        return True, ''
    rev = (fev.get('spec') or '').split(':', 1)[0] if ':' in (fev.get('spec') or '') else ''
    carrier = fev.get('spec_branch') or (rev[len('origin/'):] if rev.startswith('origin/') else '')
    if carrier:
        return False, carrier
    typed, _machine = frontmatter.split_machine(meta)
    link = (typed.get('links') or {}).get('spec')
    if not link or product is None:
        return False, ''
    on_trunk, carriers = evidence.doc_carriers(_path_only(link), ev.get('branches') or (), product)
    return on_trunk, ('' if on_trunk else (carriers[0] if carriers else ''))


def _plan_home(meta, fev, ev, product):
    """``(on_trunk, carrier_branch)`` for a matched Feature's plan. The lane's discovery answers
    first: on the trunk, or on its plan branch. A typed ``links.plan`` the lane never saw —
    a migrated card's plan on a pre-lane branch with no PR — is looked for on the trunk and on
    every remote branch, so a plan reached only by its link counts where it really is."""
    if fev.get('plan_on_main'):
        return True, ''
    rev = (fev.get('plan') or '').split(':', 1)[0] if ':' in (fev.get('plan') or '') else ''
    carrier = fev.get('plan_branch') or (rev[len('origin/'):] if rev.startswith('origin/') else '')
    if carrier:
        return False, carrier
    typed, _machine = frontmatter.split_machine(meta)
    link = (typed.get('links') or {}).get('plan')
    if not link or product is None:
        return False, ''
    on_trunk, carriers = evidence.doc_carriers(_path_only(link), ev.get('branches') or (), product)
    return on_trunk, ('' if on_trunk else (carriers[0] if carriers else ''))


@dataclasses.dataclass
class _Derived:
    """What descent needs to know about an item after its own rule spoke."""
    raw: closing.Closing      #: `state_of`'s answer, before `sticky`
    sha: str = ''             #: the commit that landed it, for the line that names it
    own: bool = False         #: it carries a branch, a PR or a commit of its own
    own_sha: str = ''         #: a landing that names it (typed, commit, merged PR) — never a
    #:                          child's: the only sha descent may hand down


def borrowed(meta):
    """The card's Closed is descent's (``rule: parent-closed`` on disk, or a landing stamped
    ``by: descent``): its parent's close, not evidence of its own. Such a close is never held by
    the terminal hold and never counts toward the parent's ``children-closed`` — the two rules
    would otherwise prove each other (a parent Closed because its children are, each child Closed
    because the parent is). While the parent stays Closed, descent gives it back every pass."""
    machine = frontmatter.split_machine(meta)[1]
    if machine.get('state') != closing.CLOSED:
        return False
    stamp = machine.get(LANDING_KEY)
    if isinstance(stamp, dict) and stamp.get('by') == 'descent':
        return True
    return RULE_PREFIX + 'parent-closed' in (machine.get('evidence') or [])


def descend(canonical, new_state, closings, derived, keep=(), evs=None):
    """A Feature that closed closes the children beneath it that have no evidence of their own.

    A child is evidence-free when its rule was `no-rule`, or its state is New with no branch, no
    PR and no commit naming it. A child with any evidence of its own — an Active state, an open
    PR, a branch — keeps the state its own rule gave it: descent may not overrule evidence, only
    reach where there is none. One level at a time, to a fixed point (Feature → Story → Task): an
    item descent closed passes it on. A Bug is never descended onto — its own `quiet` rule is what
    says the defect is gone. Nor is a Task in `keep` (:func:`orphaned_tasks`): its scope was
    folded into a survivor that never landed, so its parent's close proves nothing for it.
    Rewrites `new_state` and `closings` in place. ``evs`` (the Ev each item was settled from):
    a Story whose Ev names an unproved acceptance line is never descended onto."""
    evs = evs or {}
    reached = {iid for iid, c in closings.items()
               if c.state == closing.CLOSED and canonical[iid]['meta'].get('type') == 'feature'}
    # the parent's own landing only (see `landing_of`): a sibling Task's merge is no child's
    sha = {iid: derived[iid].own_sha for iid in reached}
    grew = True
    while grew:
        grew = False
        for iid, rec in canonical.items():
            type_ = rec['meta'].get('type')
            parent = rec['meta'].get('parent')
            if iid in reached or parent not in reached or type_ not in ('story', 'task') \
                    or iid not in derived or iid in keep:
                continue
            d = derived[iid]
            if not (d.raw.rule == closing.NO_RULE or (d.raw.state == closing.NEW and not d.own)):
                continue
            # a matrix row that says todo is the item's own evidence that it was never built:
            # the parent's close is no proof, so the item keeps the state its own rule gave it
            if any(str(l).startswith('matrix status todo') for l in closings[iid].lines):
                continue
            # a Story with an unproved acceptance line is not done because its parent is
            if evs.get(iid) is not None and getattr(evs[iid], 'unproved', ()):
                continue
            hit = closing.state_of(type_, closing.Ev(parent_closed=True))
            named = f"{parent} Closed" + (f" (commit {sha[parent][:7]})" if sha.get(parent) else '')
            kept = [l for l in closings[iid].lines
                    if not l.startswith(('no evidence found', 'held Closed'))]
            closings[iid] = dataclasses.replace(hit, lines=tuple(kept + [named]))
            new_state[iid] = closings[iid].state
            reached.add(iid)
            sha[iid] = sha.get(parent, '')
            grew = True


def restamp(root):
    """A migrated record (``index.json`` stamped by ``asf schema-migrate``) keeps every card at
    that stamp: a card below it — stripped by an older ingest that dropped ``schema_version``
    from the machine block — is brought back up here, so the operator never reruns the migration
    by hand. A record with no ``index.json`` yet is stamped with this package's schema, as its
    first index will be (:func:`asf.record.index.do_index`); an unstamped one is left for
    ``schema-migrate``. Returns how many cards changed."""
    from asf import schema
    version = schema.record_version(root)
    if version is None:
        version = schema.SCHEMA_VERSION
    if not version:
        return 0
    return schema.stamp_cards(root, version)


def reset_line(reset):
    """The evidence line of a Task whose PR was closed unmerged (``ev['resets']``)."""
    kept = f"; its head kept as {reset['archive']}" if reset.get('archive') else ''
    return (f"PR #{reset.get('pr')} closed unmerged on {reset.get('branch')}{kept} "
            f"— reset to ready")


def apply_resets(ev, product, out=print, alive=None):
    """Write one registry reset line (:func:`asf.workers.lifecycle.note_reset`) per item the
    evidence names in ``ev['resets']`` — a PR closed unmerged, its branch still at that head —
    so that item's old rounds, corrections, rulings and waits stop counting and its next session
    cuts the branch fresh from the trunk. Idempotent: an item already reset for that PR and
    head is skipped, and so is one a live session holds. Returns the items reset now."""
    if product is None:
        return []
    from asf.workers import lifecycle, pool as pool_mod
    path = pool_mod.sessions_path(product)
    if not os.path.isfile(path):
        return []
    done = [iid for iid, r in sorted((ev.get('resets') or {}).items())
            if lifecycle.note_reset(path, iid, r, alive=alive)]
    if done:
        out(f"ingest: reset {len(done)} item(s) whose PR closed unmerged: {', '.join(done)}")
    return done


def cmd_ingest(args, root):
    # the evidence is the record's product's (B-0050): the resolved --product, else the default
    # when one is configured (a record with no product configured reads evidence's own default)
    name = getattr(args, 'product', None)
    if not name:
        try:
            name = env.default_product_name()
        except env.ConfigError:
            name = None
    product = env.load_product(name) if name else None
    ev = evidence.load(fresh=getattr(args, 'fresh', False), product=product)
    # one writer through the stage (R14): what an invariant refuses is put back, the rest stands
    from asf.record import stage
    rc, staged, _findings = stage.guarded(root, 'ingest', ingest_into, (ev, product),
                                          product=product)
    if staged.refused:
        do_index(root)  # the derived sections and index.json over the cards that stood
    if getattr(args, 'registry', True):
        apply_resets(ev, product)
    return rc


def derive(canonical, ev, product=None, now=None, date=None, bypass_sticky=()):
    """The read-only half of the ingest pass: every item's state (and a Feature's stage) from
    the evidence ``ev``, in the same ``tasks → stories → bugs → features → descent → epics``
    order :func:`ingest_into` writes in. Returns ``(new_state, closings, derived, stage_val,
    task_ev, evs)`` — nothing is written; :func:`ingest_into` is the only writer.

    ``bypass_sticky`` names ids whose :func:`settle` skips ``closing.sticky`` — the item's rule
    speaks even though its on-disk ``state`` is ``Closed``, the terminal hold (§1.4) turned off
    for exactly those ids. ``ingest_into`` passes none, so its behavior is unchanged; this is
    :mod:`asf.record.reopen`'s one legitimate use — re-deriving a falsely closed item as if it
    had never closed, without touching the rule the rest of the record still holds to."""
    now = now_iso() if now is None else now
    date = today() if date is None else date
    bypass_sticky = set(bypass_sticky)

    new_state = {}
    for iid, rec in canonical.items():
        new_state[iid] = frontmatter.split_machine(rec['meta'])[1].get('state', 'New')
    # descent's closes from the last pass: re-derived from scratch, never held (`borrowed`)
    lent = {iid for iid, rec in canonical.items()
            if rec['meta'].get('type') in ('story', 'task') and borrowed(rec['meta'])}

    closings = {}     # iid -> the Closing that gets written: state, rule, evidence lines
    derived = {}      # iid -> _Derived, what descent reads
    stage_val = {}
    task_ev = {}
    evs = {}          # iid -> the Ev its closing was chosen from, what `predates` reads
    since = product.conventions.get('id_in_subject_since') if product is not None else None
    # an `asf reset` void holds against the ingest's own read of the host (W4-PR5): the voided
    # claim's commit, merge and PR are no evidence, for every rule below
    voided = _voided_landings(product)
    ev, void_hit = _void_ids(ev, voided)
    reach = _prod_reach(ev, product)  # one rev-list of the deploy for every in-prod question
    on_trunk = _trunk_reach(ev, product)  # and one of the trunk, for a typed `landed:` sha
    green_after = None
    # a typed `landed:` (§2.5's last paragraph) hands `reconciled` its sha through the same
    # `{id: evidence}` map a commit subject would have produced — one sha the trunk does not
    # carry is a claim, never a close, so only an on-trunk one is folded in; everything past
    # this point reads `commit`/`green`/`landed` off that one merged map, for every type
    reconciled = {iid: sha for iid, rec in canonical.items()
                  for sha in (_typed_landed(rec['meta']),) if sha and on_trunk(sha)}
    if reconciled:
        # `ev['ci']` is `discover()`'s own `ci_provider(product)` read at evidence-gather time
        # (W4-PR3a); handing it to `merge_landed` as `green` answers "is there CI" without
        # `merge_landed` re-asking a `product` that, off a bare ingest call, may carry no `ci`
        # field at all
        green = evidence.ci_green_runs(product) if ev.get('ci') else None
        ev = dict(ev, ids=evidence.merge_landed(ev.get('ids') or {}, reconciled, product,
                                                green=green))
    # a typed `landed:` the trunk does not carry is a claim, never a close (see above) — but it
    # is still named in the item's evidence, the way the old Task-only block used to
    off_trunk = {iid: sha for iid, rec in canonical.items()
                 for sha in (_typed_landed(rec['meta']),) if sha and not on_trunk(sha)}

    def settle(iid, type_, ev_obj, lines, sha=''):
        """The one place a state is chosen: `closing.state_of`, held by `closing.sticky` — unless
        `iid` is in `bypass_sticky`, the terminal hold `asf reopen` lifts for this one item."""
        old = new_state[iid]
        # a borrowed close is no state to carry or hold: the item's own rule speaks from New, and
        # descent (below) closes it again only if its parent is still Closed
        raw = closing.state_of(type_, ev_obj, closing.NEW if iid in lent else old)
        c = dataclasses.replace(raw, lines=tuple(lines))
        final = c if iid in bypass_sticky or iid in lent else closing.sticky(old, c)
        closings[iid] = final
        evs[iid] = ev_obj
        new_state[iid] = final.state
        own_sha = ev_obj.landed or ev_obj.commit or ev_obj.merged_sha
        derived[iid] = _Derived(raw, sha or ev_obj.commit or ev_obj.merged_sha, _own_evidence(ev_obj),
                                own_sha)
        return final

    # ---- Tasks: no dependency on any other item's derived state
    for iid, rec in canonical.items():
        if rec['meta'].get('type') != 'task':
            continue
        slug, tid, tev = match_task(rec['meta'], ev)
        task_ev[iid] = tev
        iev = _ids_of(iid, ev)
        commit = iev.get('commit') or ''
        merged = (tev or {}).get('merged_sha') or ''
        vshas, vprs = voided.get(iid.upper(), ((), ()))
        if merged and (any(_sha_prefix(merged, v) for v in vshas) or (tev or {}).get('pr') in vprs):
            void_hit.setdefault(iid, merged)
            merged = ''
        _st, id_lines = match_ids(iid, ev)
        if iid in void_hit:
            id_lines = id_lines + [f"landing {void_hit[iid][:9]} voided (asf reset)"]
        if iid in off_trunk:
            id_lines = id_lines + [f"typed landed {off_trunk[iid][:9]} is not on the trunk"]
        if tev is not None:
            branch, pr_state = tev.get('branch') or '', tev.get('pr_state') or ''
        else:  # nothing matched it by document: the id tokens naming it are what is left
            branch = (iev.get('branches') or [''])[0]
            pr_state = 'OPEN' if iev.get('open_prs') else ''
        if commit:
            # A commit on the trunk (with a green run where there is CI) is the strongest evidence
            # there is, for every type — a plan's task table says what was intended, never what
            # landed, and must not outrank the landing (B-0074). The rule order says so now:
            # `landed-green`/`landed` are read before `in-flight`; the plan's line stays as context.
            lines = id_lines + ([f"in plan {slug} ({tid})"] if tev is not None else [])
        elif tev is None:
            lines = id_lines or [f"no evidence found ({date})"]
        elif merged:
            lines = [f"PR #{tev.get('pr')} merged ({merged[:9]})"]
        elif tev.get('branch') and tev.get('pr'):
            lines = [f"branch {tev['branch']}, PR #{tev['pr']} {tev.get('pr_state')}"]
        elif tev.get('branch'):
            lines = [f"branch {tev['branch']} exists"]
        elif tev.get('pr'):
            lines = [f"PR #{tev['pr']} {tev.get('pr_state')}"]
        else:
            lines = [f"in plan {slug} ({tid}), no branch yet"]
        reset = (ev.get('resets') or {}).get(iid)
        if reset and not commit and not merged:
            # its PR was closed unmerged and its branch still sits at that head: the evidence
            # above no longer counts that branch, so the Task derives back to ready (New)
            lines = [ln for ln in lines if not ln.startswith('no evidence found')] + [reset_line(reset)]
        # a merged PR is its own green: a plan's Task never waited on CI to close
        green = bool(iev.get('green')) if commit else bool(merged)
        ev_obj = closing.Ev(commit=commit, green=green, merged_sha=merged, branch=branch,
                            pr_state=pr_state, open_prs=tuple(iev.get('open_prs') or ()),
                            landed=iev.get('landed') or '')
        settle(iid, 'task', ev_obj, lines, sha=ev_obj.landed or '')

    # ---- Stories: their Tasks are the ones whose `stories:` name them (a removed Task covers nothing)
    story_tasks = _story_tasks(canonical)
    review_claims = review_proven(canonical, task_ev)
    register = decisions.register(canonical, product)
    repo = decisions.repo_dir(product)
    proof_cache = {}

    for iid, rec in canonical.items():
        if rec['meta'].get('type') != 'story':
            continue
        legacy, sev = match_story(rec['meta'], ev)
        task_ids = story_tasks.get(iid, [])
        states = [new_state[cid] for cid in task_ids]
        all_closed = bool(states) and all(s == closing.CLOSED for s in states)
        iev = _ids_of(iid, ev)
        ev_obj = closing.Ev(children=tuple(states), matrix_status=(sev or {}).get('status') or '',
                            child_evidence=any(derived[cid].own for cid in task_ids)
                            or (sev is None and _own_ids(iid, ev)),
                            in_prod=all_closed and _in_prod(task_ids, task_ev, ev, reach=reach),
                            landed=iev.get('landed') or '', green=bool(iev.get('green')))
        if sev is None:
            _st, id_lines = match_ids(iid, ev)
            lines = id_lines or [f"no evidence found ({date})"]
        else:
            lines = [f"matrix status {sev['status']} ({legacy})"]
            if closing.ACTIVE in states:
                lines.append("a Task lists it, Active")
        claims = review_claims.get(iid)
        if claims:
            m = len(proves.bullets(rec['body']))
            lines.append(f"acceptance {len(claims)}/{m} proven ({_newest_review_path(claims, task_ev)})")
        # "no test, no done": Task closure is necessary, never sufficient — every acceptance
        # line needs its proved-line entry (or a registered deferral) before a done rule fires
        open_lines = unproved_of(iid, rec, register, ev, claims, repo_dir=repo, _cache=proof_cache)
        if open_lines and closing.state_of('story', ev_obj, new_state[iid]).state in closing._DONE:
            # only where it decides something: the lines a done rule would have closed over
            lines.extend(unproved_line(*u) for u in open_lines)
        ev_obj.unproved = tuple(n for n, _t, _w in open_lines)
        settle(iid, 'story', ev_obj, lines)

    # ---- Bugs: independent of other items
    for iid, rec in canonical.items():
        if rec['meta'].get('type') != 'bug':
            continue
        typed, machine = frontmatter.split_machine(rec['meta'])
        bev = match_bug(rec['meta'], ev)
        iev = _ids_of(iid, ev)
        linked = bev is not None and bool(bev['has_fixer'] or bev['merged_sha'])
        if linked:
            merged_sha = bev['merged_sha'] or ''
            if merged_sha and green_after is None:
                green_after = _green_after(ev, product)
            ev_obj = closing.Ev(merged_sha=merged_sha, green=bool(merged_sha) and green_after(merged_sha),
                                branch=(bev['branches'] or [''])[0], open_prs=tuple(bev['open_prs']),
                                landed=iev.get('landed') or '')
        else:
            # no link, or links that show nothing: the item's own id is the evidence left
            commit = iev.get('commit') or ''
            ev_obj = closing.Ev(commit=commit, green=bool(commit and iev.get('green')),
                                branch=(iev.get('branches') or [''])[0],
                                open_prs=tuple(iev.get('open_prs') or ()),
                                landed=iev.get('landed') or '')
            _st, id_lines = match_ids(iid, ev)
            lines = id_lines or [f"no evidence found ({date})"]
        ev_obj.signature = typed.get('signature') or ''
        if ev_obj.signature:
            # P6: `last_filed` is the last day the signature was seen; a hand-filed Bug that was
            # never bumped is quiet from the day it was written
            seen = typed.get('last_filed') or typed.get('created') or machine.get('stage_since')
            ev_obj.quiet_for = _seconds_since(seen, now)
            ev_obj.quiet_limit = float(stale.limit_seconds(
                stale.load_limits(product).get('bug_quiet') or closing.BUG_QUIET_DEFAULT))
        if linked and bev['merged_sha']:
            in_prod = reach(bev['merged_sha'])
            quiet = closing.state_of('bug', ev_obj, new_state[iid]).state == closing.CLOSED
            lines = [f"fix merged ({bev['merged_sha'][:9]})"
                     + (' -- pending 3-day quiet' if in_prod and not quiet else '')]
        elif linked:
            lines = ['fixer branch/PR open']
        settle(iid, 'bug', ev_obj, lines)

    # ---- Deliveries: a lead of any type but Feature (a Feature already runs the ladder below)
    # stages itself off its own plan document alone. No closing code here: the type's own rule,
    # settled above, already chose `new_state[iid]` — a Bug is still `Resolved` when a commit
    # names it. A delivery lead has no spec step, so `spec_dict` is `plan_dict` itself: with both
    # arguments carrying the same `approved` bit, `feature_stage`'s spec gate
    # (``not on_trunk and (plan approved or started)``) can never hold, and the ladder runs
    # `card` -> `plan-draft` -> `plan-review rN` -> `plan-approved` off the plan alone.
    for iid, rec in canonical.items():
        meta = rec['meta']
        if meta.get('type') in ('feature', 'task') or not meta.get('delivers'):
            # a Task lead (a Feature delivery's slice, asf.record.slice) rides its Feature's
            # plan, already approved: it needs no ladder of its own, and a stage on a Task
            # would move its stage_since off its state changes
            continue
        _slug, fev = match_feature(meta, ev)
        if fev is None:
            stage_val[iid] = 'card'
            continue
        plan_review = fev.get('plan_review')
        plan_approved = bool(plan_review and plan_review[1] == 'APPROVED') or bool(fev.get('plan_on_main'))
        plan_dict = {'exists': bool(fev.get('plan')), 'approved': plan_approved,
                     'review': plan_review[:2] if plan_review else None}
        spec_dict = plan_dict
        stage_val[iid] = evidence.feature_stage(spec_dict, plan_dict, [], False)

    # ---- Features: depend on their own children's derived state
    orphans = orphaned_tasks(canonical, derived)
    retired = set()
    for iid, rec in canonical.items():
        if rec['meta'].get('type') != 'feature':
            continue
        if is_retired(rec['meta']):
            # removed, or moved to another record: no stage is derived for it, and its machine
            # block is left as it stands (the feeder skips it the same way)
            retired.add(iid)
            continue
        slug, fev = match_feature(rec['meta'], ev)
        iev = _ids_of(iid, ev)
        commit = iev.get('commit') or ''
        green = bool(commit and iev.get('green'))
        _st, id_lines = match_ids(iid, ev)
        if fev is None:
            # no spec/plan/legacy match: its children and its own id tokens are what is left
            kid_ids = [cid for cid, crec in canonical.items()
                       if crec['meta'].get('parent') == iid
                       and crec['meta'].get('type') in LANDING_CHILD_TYPES]
            kid_ids += [t for t in _story_descendants(canonical, iid, story_tasks)
                        if t not in kid_ids]
            kids = [new_state[cid] for cid in kid_ids]
            kids_closed = bool(kids) and all(s == closing.CLOSED for s in kids)
            # …and it is Closed, not merely Resolved, when the product deploys nothing (B-0078):
            # the trunk IS production there (B-0077), so children Closed or a green commit naming
            # it is the whole of the evidence. A product that configures `deploy_sha` still waits
            # for the deploy and the operator's tick.
            # a Closed Story is already in prod by its own rule; the rest carry a landing sha
            landing = [cid for cid in kid_ids if canonical[cid]['meta'].get('type') != 'story']
            if kids_closed and not landing:
                in_prod = True
            else:
                in_prod = (kids_closed or (not kids and bool(commit))) and _in_prod(
                    landing, task_ev, ev, own_sha=commit, reach=reach)
            c = settle(iid, 'feature', closing.Ev(children=tuple(kids), commit=commit, green=green,
                                                  in_prod=in_prod, landed=iev.get('landed') or ''),
                      [], sha=commit)
            if c.state in (closing.RESOLVED, closing.CLOSED):
                stage_val[iid] = 'landed'
                lines = (id_lines if commit else []) + (
                    [f"{len(kids)}/{len(kids)} children Closed"] if kids_closed else [])
            else:
                # a Feature nothing in the product repo knows yet IS a card — an empty stage would
                # make a decided card invisible to the CARD → SPEC feeder row that reads this stage.
                stage_val[iid] = 'card'
                lines = id_lines or [f"no evidence found ({date})"]
            closings[iid] = dataclasses.replace(c, lines=c.lines + tuple(lines))
            continue
        child_ids = [cid for cid, crec in canonical.items()
                     if crec['meta'].get('type') == 'task' and crec['meta'].get('parent') == iid
                     and (not crec['meta'].get('removed') or cid in orphans)]
        # its Stories' Tasks are its Tasks too: a Task filed under a Story (or naming it) is
        # work of this Feature, and a New one keeps the Feature building (F-0106/F-0108)
        direct_n = len(child_ids)
        child_ids += [t for t in _story_descendants(canonical, iid, story_tasks)
                      if t not in child_ids]
        child_states = [new_state[cid] for cid in child_ids]
        # the ladder of a Feature already held Closed reads its own Tasks only, as before: the
        # descent below may still close an evidence-free grandchild, and the stage must not
        # move again on the next pass because of it
        stage_states = child_states[:direct_n] if new_state[iid] == closing.CLOSED else child_states
        # …and its Stories are children of the Feature's rule: an open Story keeps it open
        story_ids = [cid for cid, crec in canonical.items()
                     if crec['meta'].get('type') == 'story' and crec['meta'].get('parent') == iid
                     and not crec['meta'].get('removed')]
        story_states = [new_state[cid] for cid in story_ids]
        all_closed = bool(child_ids) and all(s == closing.CLOSED for s in child_states)
        merged_in_prod = in_prod = False
        if all_closed:
            merged_in_prod = _merged_in_prod(child_ids, task_ev, ev, reach)
            in_prod = _in_prod(child_ids, task_ev, ev, merged=merged_in_prod, reach=reach)
        elif not child_ids and commit:
            in_prod = _in_prod(child_ids, task_ev, ev, own_sha=commit, reach=reach)
        # the board's ladder still says `landed`, not `on-prod`, for a product that deploys
        # nothing — "on prod" names a deployment, and there is none to name
        on_prod_for_stage = bool(merged_in_prod) and bool(ev.get('prod_sha'))

        spec_review = fev.get('spec_review')
        plan_review = fev.get('plan_review')
        # a document the lane landed on the trunk is approved (B-0059): the fast-forward lane
        # has no reviewer row — harvest's gate is its review, and a spec on main that still read
        # "spec-draft" sent the feeder back to write the same spec again
        spec_on_main, spec_carrier = _spec_home(rec['meta'], fev, ev, product)
        spec_approved = bool(spec_review and spec_review[1] == 'APPROVED') or spec_on_main
        plan_on_main, plan_branch = _plan_home(rec['meta'], fev, ev, product)
        plan_approved = bool(plan_review and plan_review[1] == 'APPROVED') or plan_on_main
        if story_states and not child_ids:
            # Stories only, all Closed: a Closed Story is already in prod by its own rule
            in_prod = all(s == closing.CLOSED for s in story_states)
        ev_obj = closing.Ev(children=tuple(child_states + story_states), commit=commit, green=green,
                            in_prod=in_prod, spec_on_main=spec_on_main, plan_approved=plan_approved,
                            landed=iev.get('landed') or '')
        if not child_ids and commit:
            # no Tasks to judge by, and a code commit on main names it: landed, as for a Feature
            # the documents never matched (a document-lane commit never counts, B-0059)
            stage_val[iid] = 'landed'
            lines = id_lines
        else:
            spec_dict = {'exists': bool(fev.get('spec') or spec_carrier), 'approved': spec_approved,
                         'on_trunk': spec_on_main,
                         'review': spec_review[:2] if spec_review else None}
            plan_dict = {'exists': bool(fev.get('plan')), 'approved': plan_approved,
                         'review': plan_review[:2] if plan_review else None}
            stage_val[iid] = evidence.feature_stage(spec_dict, plan_dict, stage_states, on_prod_for_stage)
            from asf.feeder.rows import plan_on_trunk, trunk_of
            lines = []
            if spec_on_main:
                lines.append(f"spec on origin/{trunk_of(product)}")
            elif spec_carrier:
                r = f" (review r{spec_review[0]} {spec_review[1]})" if spec_review else ''
                lines.append(f"spec on {spec_carrier}{r}")
            if plan_on_main:
                lines.append(plan_on_trunk(product))
            elif plan_branch:
                r = f" (review r{plan_review[0]} {plan_review[1]})" if plan_review else ''
                lines.append(f"plan on {plan_branch}{r}")
            if new_state[iid] == closing.CLOSED:
                # held Closed: its lines read its own Tasks, as before (see stage_states)
                child_ids, child_states = child_ids[:direct_n], child_states[:direct_n]
            if child_ids:
                lines.append(f"{sum(1 for s in child_states if s == 'Closed')}/{len(child_ids)} tasks Closed")
            open_s = [cid for cid, st in zip(story_ids, story_states) if st not in closing._DONE]
            if open_s and new_state[iid] != closing.CLOSED:
                lines.append(f"{len(open_s)}/{len(story_ids)} stories open: {', '.join(open_s)}")
            lines = lines or [f"no evidence found ({date})"]
        settle(iid, 'feature', ev_obj, lines, sha=commit or _landing_sha(child_ids, task_ev, ev))

    # ---- Descent: a Feature that closed closes the children beneath it that nothing else names
    descend(canonical, new_state, closings, derived, keep=orphans, evs=evs)

    # ---- Epics: purely a function of their children's derived state; no product-repo evidence
    for iid, rec in canonical.items():
        if rec['meta'].get('type') != 'epic':
            continue
        states = [new_state[cid] for cid, crec in canonical.items()
                  if crec['meta'].get('parent') == iid and cid in new_state
                  and crec['meta'].get('type') in EVIDENCE_TYPES]
        settle(iid, 'epic', closing.Ev(children=tuple(states),
                                       typed_closed=bool(rec['meta'].get('closed'))), [])

    return new_state, closings, derived, stage_val, task_ev, evs


def _story_tasks(canonical):
    """``{story_id: [task_id, ...]}`` — the Tasks whose ``stories:`` name each Story, in canonical
    order (a removed Task covers nothing)."""
    out = collections.defaultdict(list)
    for iid, rec in canonical.items():
        if rec['meta'].get('type') != 'task' or rec['meta'].get('removed'):
            continue
        for sid in rec['meta'].get('stories') or []:
            out[sid].append(iid)
    return out


def unproved_of(sid, rec, register, ev=None, review=None, repo_dir=None, _cache=None):
    """``[(line, text, why), …]``: Story ``sid``'s acceptance lines that no ``proved line`` entry,
    no claim this pass records (``ev['proves']``, ``review``), no registered deferral and no
    inline ``proven by`` a file in ``repo_dir`` covers (:func:`asf.proves.unproved`)."""
    body = rec['body']
    m = len(proves.bullets(body))
    claims = list(((ev or {}).get('proves') or {}).get(sid) or []) + list(review or [])
    pending = {int(c.get('line') or 0) for c in claims}
    pending = {n for n in pending if 1 <= n <= m}
    return proves.unproved(body, register, also_proved=pending, repo_dir=repo_dir, _cache=_cache)


def unproved_line(n, text, why):
    """The evidence line naming one unproved acceptance line."""
    text = text if len(text) <= 80 else text[:77] + '...'
    return f"unproved line {n} — {text} ({why})"


def review_proven(canonical, task_ev):
    """``{story: [{line, test, task, path, source: 'review'}, ...]}`` (§2.5, PD10): for each
    Story, the Tasks whose ``stories:`` name it (:func:`_story_tasks`), each Task's ``checks``
    from ``task_ev`` folded into ``{normalized check name: (task, path)}`` over its passing rows.
    A Story's acceptance line whose normalized text (:func:`asf.reviews.normalize`) is a key
    yields a claim naming that bullet's 1-based ordinal and the review's path — ticked or not (a
    line's own ``— <path>`` suffix, once one is added, is stripped before matching, so a later
    round still finds it): :func:`proves.tick` is what makes an already-ticked line a no-op. Two
    Tasks proving the same line: the first by Task id order wins. A Task whose ``checks`` is None
    or missing contributes nothing."""
    story_tasks = _story_tasks(canonical)
    out = {}
    for iid, rec in canonical.items():
        if rec['meta'].get('type') != 'story':
            continue
        passing = {}
        for tid in story_tasks.get(iid, []):
            checks = (task_ev.get(tid) or {}).get('checks')
            if not checks:
                continue
            for name in checks['passed']:
                passing.setdefault(name, (tid, checks['path']))
        if not passing:
            continue
        claims = []
        for line_no, text in enumerate(proves.bullets(rec['body']), start=1):
            hit = passing.get(reviews.normalize(text))
            if not hit and ' — ' in text:
                hit = passing.get(reviews.normalize(text.rsplit(' — ', 1)[0]))
            if hit:
                tid, path = hit
                claims.append({'line': line_no, 'test': path, 'task': tid, 'path': path,
                               'source': 'review'})
        if claims:
            out[iid] = claims
    return out


def _newest_review_path(claims, task_ev):
    """The review path of ``claims``' newest round — the one path an aggregate evidence or
    History line names."""
    best = max(claims, key=lambda c: ((task_ev.get(c['task']) or {}).get('checks') or {}).get('round', 0))
    return best['path']


def _story_descendants(canonical, fid, story_tasks):
    """The Tasks of ``fid``'s Stories, in canonical order, deduplicated: the ones whose
    ``stories:`` name a Story of it (:func:`_story_tasks`) and the ones filed under one
    (``parent:`` the Story, :func:`asf.proves.stories_of_task`). A removed Task is none."""
    stories = [sid for sid, srec in canonical.items()
               if srec['meta'].get('type') == 'story' and srec['meta'].get('parent') == fid]
    out = []
    for sid in stories:
        for tid in story_tasks.get(sid, []):
            if tid not in out:
                out.append(tid)
    for tid, trec in canonical.items():
        meta = trec['meta']
        if (meta.get('type') == 'task' and not meta.get('removed') and meta.get('parent') in stories
                and tid not in out):
            out.append(tid)
    return out


def tick_proven(canonical, ev, stamp, task_ev=None):
    """The pass that turns a landed or review-proven claim into a fact on the card (§2.5, §2.7):
    over the Story cards only, the ``## Acceptance`` line each of ``ev['proves']``'s claims and
    each of :func:`review_proven`'s names is flipped to ``- [x]`` — once, never in reverse (D7).
    Runs after the write loop and before ``do_index`` (P14): that loop may have just rewritten the
    very card this pass ticks, so the card is re-read off ``rec['path']`` and parsed afresh rather
    than reusing the body the write loop already holds. One ``frontmatter.render`` write per Story
    that changed, and one ``## History`` line per landed claim newly ticked (the ``PR #<n>`` clause
    omitted where the claim's ``pr`` is null, a fast-forward landing having no pull request to
    name) plus one aggregate ``## History`` line, in §2.5's shape, when a review claim ticked
    anything."""
    review_claims = review_proven(canonical, task_ev or {})
    for iid, rec in canonical.items():
        if rec['meta'].get('type') != 'story':
            continue
        claims = (ev.get('proves') or {}).get(iid) or []
        rclaims = review_claims.get(iid) or []
        if not claims and not rclaims:
            continue
        with open(rec['path'], encoding='utf-8') as f:
            text = f.read()
        meta, body = frontmatter.parse(text, path=rec['relpath'])
        history = []
        credited = proves.proved_lines(body)
        for claim in claims:
            body, changed = proves.tick(body, claim['line'], None)
            # re-credit: a line already ticked (by hand, or before the entry existed) still gets
            # its one proved-line entry — else it could never count as proved
            if not changed and (claim['line'] in credited or not proves.is_ticked(body, claim['line'])):
                continue
            credited.add(claim['line'])
            pr_clause = f", PR #{claim['pr']}" if claim.get('pr') else ''
            history.append(f"- {stamp} ingest: proved line {claim['line']} — "
                           f"{claim['task']}{pr_clause} ({claim['test']})")
        review_ticked = False
        for claim in rclaims:
            body, changed = proves.tick(body, claim['line'], None, suffix=claim['path'])
            review_ticked = review_ticked or changed
            if claim['line'] not in credited and proves.is_ticked(body, claim['line']):
                credited.add(claim['line'])
                history.append(f"- {stamp} ingest: proved line {claim['line']} — "
                               f"{claim['task']} ({claim['path']})")
        if review_ticked:
            m = len(proves.bullets(body))
            path = _newest_review_path(rclaims, task_ev or {})
            history.append(f"- {stamp} ingest: acceptance {len(rclaims)}/{m} ticked ({path})")
        if not history:
            continue
        body = append_history_lines(body, history)
        writer.write_card(rec['path'], frontmatter.render(meta, body))


def ingest_into(root, ev, product=None):
    """The ingest pass over ``root`` with the evidence ``ev``: restamp, :func:`derive`, merge
    every changed machine block, re-index. Returns the exit code. A writer of the record: run it
    through :func:`asf.record.stage.guarded` (as :func:`cmd_ingest` does)."""
    restamp(root)
    by_id, parse_errors = load_items(root)
    if parse_errors:
        for f, line, why in parse_errors:
            print(f"{f}:{line}: {why}", file=sys.stderr)
        return 1
    canonical, _dupes = canonicalize(by_id)

    now = now_iso()
    date = today()
    new_state, closings, derived, stage_val, task_ev, evs = derive(canonical, ev, product, now, date)
    since = product.conventions.get('id_in_subject_since') if product is not None else None
    story_tasks = _story_tasks(canonical)
    reverts = ev.get('reverts') or {}

    def closes(iid):
        """The stamp for ``iid``, closing now: its rule's evidence, spelled in full."""
        def stamp():
            sha, by = landing_of(iid, closings[iid].rule, canonical, ev, task_ev, derived,
                                 story_tasks)
            if sha and not _FULL_SHA_RE.fullmatch(sha):
                sha = evidence.full_shas(product, [sha]).get(sha, '')
            return landing_stamp(sha, by, now, reverts)
        return stamp

    # ---- write: state/stage/evidence/blocked, one write_machine + History append per changed item
    for iid, rec in canonical.items():
        type_ = rec['meta'].get('type')
        if type_ not in EVIDENCE_TYPES or (type_ == 'feature' and is_retired(rec['meta'])):
            continue
        blocked_pair = evidence.blocked_of(rec['meta'].get('blockedBy'), new_state)
        c = closings[iid]
        # an Epic carries no evidence of its own: only a rule that derived something is worth a line
        lines = None if type_ == 'epic' and c.rule == 'typed' else list(c.lines) + [RULE_PREFIX + c.rule]
        if lines is not None and closing.predates(dict(rec['meta'], state=new_state[iid]),
                                                  evs[iid], since):
            lines.insert(len(lines) - 1, closing.PREDATES_LINE % closing.created_of(rec['meta']))
        _typed, machine = frontmatter.split_machine(rec['meta'])
        old = machine.get('stage')
        landing = (_landing_update(machine, new_state[iid], now, reverts, closes(iid))
                   if type_ in LANDING_TYPES else _KEEP)
        ordered, history = _ingest_fields(machine, new_state[iid], stage_val.get(iid), lines,
                                          blocked_pair, now, landing)
        if ordered is None:
            continue
        write_fields(rec['path'], machine, ordered)
        if history:
            with open(rec['path'], encoding='utf-8') as f:
                text = f.read()
            meta2, body2 = frontmatter.parse(text, path=rec['relpath'])
            new_body = append_history_lines(body2, history)
            if new_body != body2:
                writer.write_card(rec['path'], frontmatter.render(meta2, new_body))
        if type_ == 'feature' and stage_val.get(iid) == 'on-prod' and old != 'on-prod':
            write_on_prod_event(root, iid, old, now)

    tick_proven(canonical, ev, now[:16].replace('T', ' '), task_ev)
    note_dead_after(canonical, now)

    return do_index(root)


def dead_after_lines(canonical):
    """``{id: [line]}``: for each live, open Task whose ``after:`` names a card groom removed,
    the line its History records once — the survivor the feeder reads it as when that took a
    chain of merges (a survivor removed in turn), or that the edge is dropped (removed outright: the work is not
    coming). The feeder already reads the edge so (:func:`asf.feeder.rows.after_of`); the line
    is the record of why the Task stopped waiting on an id that no longer lands."""
    from asf.feeder import rows as feeder_rows  # local: the feeder reads the record's modules
    raw = {iid: dict(rec['meta'], id=iid) for iid, rec in canonical.items()}
    items = feeder_rows.items_of(raw)
    absorbed = feeder_rows.absorbers(items)
    gone = feeder_rows._retired(items)
    out = {}
    for iid, item in items.items():
        if item.get('type') != 'task' or not feeder_rows.is_open(item):
            continue
        lines = []
        dead = dict(feeder_rows.dead_after(items, item, absorbed))
        for a in item.get('after') or ():
            if a not in gone:
                continue
            why = str(gone[a].get('removed') or 'removed')
            if a in dead:
                lines.append(f"after: {a} removed ({why}) — dropped, its work is not coming")
                continue
            seen, cur = [a], a
            while cur in absorbed and absorbed[cur] not in seen:
                cur = absorbed[cur]
                seen.append(cur)
            if len(seen) > 2:  # one hop the survivor's own merged: always said; a chain is news
                lines.append(f"after: {a} removed ({why}) — read as {cur} "
                             f"(via {', '.join(seen[1:-1])})")
        if lines:
            out[iid] = lines
    return out


def note_dead_after(canonical, now):
    """Append :func:`dead_after_lines` to each Task's History, each line once."""
    stamp = now[:16].replace('T', ' ')
    for iid, lines in dead_after_lines(canonical).items():
        rec = canonical[iid]
        with open(rec['path'], encoding='utf-8') as f:
            text = f.read()
        meta, body = frontmatter.parse(text, path=rec['relpath'])
        new = [f"- {stamp} ingest: {line}" for line in lines
               if f"ingest: {line.split(' — ')[0]}" not in body]
        if not new:
            continue
        body2 = append_history_lines(body, new)
        if body2 != body:
            writer.write_card(rec['path'], frontmatter.render(meta, body2))
