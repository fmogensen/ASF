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
import re
import sys

from asf import env
from asf.evidence import closing, evidence
from asf.record import frontmatter
from asf.record.core import canonicalize, load_items, now_iso, parse_sections, render_sections, section_content, today
from asf.record.index import do_index
from asf.tick import stale

EVIDENCE_TYPES = {'epic', 'feature', 'story', 'task', 'bug'}
LANDING_CHILD_TYPES = {'story', 'task', 'bug'}
MACHINE_KEY_ORDER = ['state', 'stage', 'stage_since', 'cost', 'evidence', 'blocked',
                     'blocked_by_open', 'updated']
RULE_PREFIX = 'rule: '


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


def append_history_lines(body, new_lines):
    if not new_lines:
        return body
    preamble, sections = parse_sections(body)
    n = len(sections)
    out_sections = []
    for idx, (heading, content) in enumerate(sections):
        if heading.strip() == '## History':
            is_last = idx == n - 1
            content = section_content(_section_lines(content) + list(new_lines), is_last)
        out_sections.append([heading, content])
    return render_sections(preamble, out_sections)


def _phrase(ev_lines):
    """The History parenthesis: the rule that decided, then the lines that show why."""
    if not ev_lines:
        return 'derived'
    rules = [l for l in ev_lines if l.startswith(RULE_PREFIX)]
    return '; '.join(rules[-1:] + [l for l in ev_lines if not l.startswith(RULE_PREFIX)])


def _ingest_fields(machine, new_state, stage, ev_lines, blocked_pair, now):
    """(ordered_machine_or_None, [history_line, ...]) — None means "no change, skip the write"."""
    old_state = machine.get('state', 'New')
    old_stage = machine.get('stage')
    fields = dict(machine)
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

    history = []
    stamp = now[:16].replace('T', ' ')
    if new_state != old_state:
        phrase = _phrase(ev_lines)
        history.append(f"- {stamp} ingest: state {old_state} → {new_state} ({phrase})")
    if stage is not None and stage != old_stage:
        phrase = _phrase(ev_lines)
        history.append(f"- {stamp} ingest: stage {old_stage or 'card'} → {stage} ({phrase})")
    return ordered, history


def _ids_of(iid, ev):
    return (ev.get('ids') or {}).get(iid) or {}


def _own_ids(iid, ev):
    """An id token naming `iid` in a branch or an open PR: evidence of its own that it is moving."""
    iev = _ids_of(iid, ev)
    return bool(iev.get('branches') or iev.get('open_prs'))


def _merged_in_prod(child_ids, task_ev, ev):
    """Every child Task's merge is an ancestor of the deploy. False for a Task the plan does not
    list: nothing says where its merge is."""
    return bool(child_ids) and all(
        task_ev.get(cid) and evidence.ancestor_of(task_ev[cid].get('merged_sha'), ev.get('prod_sha'))
        for cid in child_ids)


def _in_prod(child_ids, task_ev, ev, merged=None):
    """What "in production" means is the product's own (B-0077). A service configures
    `deploy_sha`, and its work is in production when every child's merge is in that deploy and the
    operator ticked its PR. A package, library or tool configures none — its trunk IS production,
    and its Tasks already closed on a commit there with CI green (D-0047), so there is nothing
    further to wait for. Without this, no Feature of such a product could ever leave `Resolved`.
    `merged` hands back a `_merged_in_prod` the caller already has."""
    if not ev.get('prod_sha'):
        return True
    if merged is None:
        merged = _merged_in_prod(child_ids, task_ev, ev)
    return bool(merged) and all(
        task_ev.get(cid) and task_ev[cid].get('pr') in (ev.get('checked') or ()) for cid in child_ids)


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


@dataclasses.dataclass
class _Derived:
    """What descent needs to know about an item after its own rule spoke."""
    raw: closing.Closing      #: `state_of`'s answer, before `sticky`
    sha: str = ''             #: the commit that landed it, for the line that names it
    own: bool = False         #: it carries a branch, a PR or a commit of its own


def descend(canonical, new_state, closings, derived):
    """A Feature that closed closes the children beneath it that have no evidence of their own.

    A child is evidence-free when its rule was `no-rule`, or its state is New with no branch, no
    PR and no commit naming it. A child with any evidence of its own — an Active state, an open
    PR, a branch — keeps the state its own rule gave it: descent may not overrule evidence, only
    reach where there is none. One level at a time, to a fixed point (Feature → Story → Task): an
    item descent closed passes it on. A Bug is never descended onto — its own `quiet` rule is what
    says the defect is gone. Rewrites `new_state` and `closings` in place."""
    reached = {iid for iid, c in closings.items()
               if c.state == closing.CLOSED and canonical[iid]['meta'].get('type') == 'feature'}
    sha = {iid: derived[iid].sha for iid in reached}
    grew = True
    while grew:
        grew = False
        for iid, rec in canonical.items():
            type_ = rec['meta'].get('type')
            parent = rec['meta'].get('parent')
            if iid in reached or parent not in reached or type_ not in ('story', 'task') \
                    or iid not in derived:
                continue
            d = derived[iid]
            if not (d.raw.rule == closing.NO_RULE or (d.raw.state == closing.NEW and not d.own)):
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
    by_id, parse_errors = load_items(root)
    if parse_errors:
        for f, line, why in parse_errors:
            print(f"{f}:{line}: {why}", file=sys.stderr)
        return 1
    canonical, _dupes = canonicalize(by_id)

    now = now_iso()
    date = today()

    new_state = {}
    for iid, rec in canonical.items():
        new_state[iid] = frontmatter.split_machine(rec['meta'])[1].get('state', 'New')

    closings = {}     # iid -> the Closing that gets written: state, rule, evidence lines
    derived = {}      # iid -> _Derived, what descent reads
    stage_val = {}
    task_ev = {}

    def settle(iid, type_, ev_obj, lines, sha=''):
        """The one place a state is chosen: `closing.state_of`, held by `closing.sticky`."""
        old = new_state[iid]
        raw = closing.state_of(type_, ev_obj, old)
        final = closing.sticky(old, dataclasses.replace(raw, lines=tuple(lines)))
        closings[iid] = final
        new_state[iid] = final.state
        derived[iid] = _Derived(raw, sha or ev_obj.commit or ev_obj.merged_sha, _own_evidence(ev_obj))
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
        _st, id_lines = match_ids(iid, ev)
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
        elif tev.get('merged_sha'):
            lines = [f"PR #{tev.get('pr')} merged ({tev['merged_sha'][:9]})"]
        elif tev.get('branch') and tev.get('pr'):
            lines = [f"branch {tev['branch']}, PR #{tev['pr']} {tev.get('pr_state')}"]
        elif tev.get('branch'):
            lines = [f"branch {tev['branch']} exists"]
        elif tev.get('pr'):
            lines = [f"PR #{tev['pr']} {tev.get('pr_state')}"]
        else:
            lines = [f"in plan {slug} ({tid}), no branch yet"]
        # a merged PR is its own green: a plan's Task never waited on CI to close
        green = bool(iev.get('green')) if commit else bool(merged)
        settle(iid, 'task', closing.Ev(commit=commit, green=green, merged_sha=merged,
                                       branch=branch, pr_state=pr_state,
                                       open_prs=tuple(iev.get('open_prs') or ())), lines)

    # ---- Stories: their Tasks are the ones whose `stories:` name them (a removed Task covers nothing)
    story_tasks = collections.defaultdict(list)
    for iid, rec in canonical.items():
        if rec['meta'].get('type') != 'task' or rec['meta'].get('removed'):
            continue
        for sid in rec['meta'].get('stories') or []:
            story_tasks[sid].append(iid)

    for iid, rec in canonical.items():
        if rec['meta'].get('type') != 'story':
            continue
        legacy, sev = match_story(rec['meta'], ev)
        task_ids = story_tasks.get(iid, [])
        states = [new_state[cid] for cid in task_ids]
        all_closed = bool(states) and all(s == closing.CLOSED for s in states)
        ev_obj = closing.Ev(children=tuple(states), matrix_status=(sev or {}).get('status') or '',
                            child_evidence=any(derived[cid].own for cid in task_ids)
                            or (sev is None and _own_ids(iid, ev)),
                            in_prod=all_closed and _in_prod(task_ids, task_ev, ev))
        if sev is None:
            _st, id_lines = match_ids(iid, ev)
            lines = id_lines or [f"no evidence found ({date})"]
        else:
            lines = [f"matrix status {sev['status']} ({legacy})"]
            if closing.ACTIVE in states:
                lines.append("a Task lists it, Active")
        settle(iid, 'story', ev_obj, lines)

    # ---- Bugs: independent of other items
    green_after = None
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
                                branch=(bev['branches'] or [''])[0], open_prs=tuple(bev['open_prs']))
        else:
            # no link, or links that show nothing: the item's own id is the evidence left
            commit = iev.get('commit') or ''
            ev_obj = closing.Ev(commit=commit, green=bool(commit and iev.get('green')),
                                branch=(iev.get('branches') or [''])[0],
                                open_prs=tuple(iev.get('open_prs') or ()))
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
            in_prod = evidence.ancestor_of(bev['merged_sha'], ev.get('prod_sha'))
            quiet = closing.state_of('bug', ev_obj, new_state[iid]).state == closing.CLOSED
            lines = [f"fix merged ({bev['merged_sha'][:9]})"
                     + (' -- pending 3-day quiet' if in_prod and not quiet else '')]
        elif linked:
            lines = ['fixer branch/PR open']
        settle(iid, 'bug', ev_obj, lines)

    # ---- Features: depend on their own children's derived state
    for iid, rec in canonical.items():
        if rec['meta'].get('type') != 'feature':
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
            kids = [new_state[cid] for cid in kid_ids]
            kids_closed = bool(kids) and all(s == closing.CLOSED for s in kids)
            # …and it is Closed, not merely Resolved, when the product deploys nothing (B-0078):
            # the trunk IS production there (B-0077), so children Closed or a green commit naming
            # it is the whole of the evidence. A product that configures `deploy_sha` still waits
            # for the deploy and the operator's tick.
            in_prod = (kids_closed or (not kids and bool(commit))) and _in_prod(kid_ids, task_ev, ev)
            c = settle(iid, 'feature', closing.Ev(children=tuple(kids), commit=commit, green=green,
                                                  in_prod=in_prod), [], sha=commit)
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
                     and not crec['meta'].get('removed')]
        child_states = [new_state[cid] for cid in child_ids]
        all_closed = bool(child_ids) and all(s == closing.CLOSED for s in child_states)
        merged_in_prod = in_prod = False
        if all_closed:
            merged_in_prod = _merged_in_prod(child_ids, task_ev, ev)
            in_prod = _in_prod(child_ids, task_ev, ev, merged=merged_in_prod)
        elif not child_ids and commit:
            in_prod = _in_prod(child_ids, task_ev, ev)
        # the board's ladder still says `landed`, not `on-prod`, for a product that deploys
        # nothing — "on prod" names a deployment, and there is none to name
        on_prod_for_stage = bool(merged_in_prod) and bool(ev.get('prod_sha'))

        spec_review = fev.get('spec_review')
        plan_review = fev.get('plan_review')
        # a document the lane landed on the trunk is approved (B-0059): the fast-forward lane
        # has no reviewer row — harvest's gate is its review, and a spec on main that still read
        # "spec-draft" sent the feeder back to write the same spec again
        spec_approved = bool(spec_review and spec_review[1] == 'APPROVED') or bool(fev.get('spec_on_main'))
        plan_approved = bool(plan_review and plan_review[1] == 'APPROVED') or bool(fev.get('plan_on_main'))
        ev_obj = closing.Ev(children=tuple(child_states), commit=commit, green=green, in_prod=in_prod,
                            spec_on_main=bool(fev.get('spec_on_main')), plan_approved=plan_approved)
        if not child_ids and commit:
            # no Tasks to judge by, and a code commit on main names it: landed, as for a Feature
            # the documents never matched (a document-lane commit never counts, B-0059)
            stage_val[iid] = 'landed'
            lines = id_lines
        else:
            spec_dict = {'exists': bool(fev.get('spec')), 'approved': spec_approved,
                         'review': spec_review[:2] if spec_review else None}
            plan_dict = {'exists': bool(fev.get('plan')), 'approved': plan_approved,
                         'review': plan_review[:2] if plan_review else None}
            stage_val[iid] = evidence.feature_stage(spec_dict, plan_dict, child_states, on_prod_for_stage)
            lines = []
            if fev.get('spec_on_main'):
                lines.append('spec on origin/main')
            elif fev.get('spec_branch'):
                r = f" (review r{spec_review[0]} {spec_review[1]})" if spec_review else ''
                lines.append(f"spec on {fev['spec_branch']}{r}")
            if fev.get('plan_on_main'):
                lines.append('plan on origin/main')
            elif fev.get('plan_branch'):
                r = f" (review r{plan_review[0]} {plan_review[1]})" if plan_review else ''
                lines.append(f"plan on {fev['plan_branch']}{r}")
            if child_ids:
                lines.append(f"{sum(1 for s in child_states if s == 'Closed')}/{len(child_ids)} tasks Closed")
            lines = lines or [f"no evidence found ({date})"]
        settle(iid, 'feature', ev_obj, lines, sha=commit or _landing_sha(child_ids, task_ev, ev))

    # ---- Descent: a Feature that closed closes the children beneath it that nothing else names
    descend(canonical, new_state, closings, derived)

    # ---- Epics: purely a function of their children's derived state; no product-repo evidence
    for iid, rec in canonical.items():
        if rec['meta'].get('type') != 'epic':
            continue
        states = [new_state[cid] for cid, crec in canonical.items()
                  if crec['meta'].get('parent') == iid and cid in new_state
                  and crec['meta'].get('type') in EVIDENCE_TYPES]
        settle(iid, 'epic', closing.Ev(children=tuple(states),
                                       typed_closed=bool(rec['meta'].get('closed'))), [])

    # ---- write: state/stage/evidence/blocked, one write_machine + History append per changed item
    for iid, rec in canonical.items():
        type_ = rec['meta'].get('type')
        if type_ not in EVIDENCE_TYPES:
            continue
        blocked_pair = evidence.blocked_of(rec['meta'].get('blockedBy'), new_state)
        c = closings[iid]
        # an Epic carries no evidence of its own: only a rule that derived something is worth a line
        lines = None if type_ == 'epic' and c.rule == 'typed' else list(c.lines) + [RULE_PREFIX + c.rule]
        _typed, machine = frontmatter.split_machine(rec['meta'])
        ordered, history = _ingest_fields(machine, new_state[iid], stage_val.get(iid), lines,
                                          blocked_pair, now)
        if ordered is None:
            continue
        frontmatter.write_machine(rec['path'], ordered)
        if history:
            with open(rec['path'], encoding='utf-8') as f:
                text = f.read()
            meta2, body2 = frontmatter.parse(text, path=rec['relpath'])
            new_body = append_history_lines(body2, history)
            if new_body != body2:
                with open(rec['path'], 'w', encoding='utf-8') as f:
                    f.write(frontmatter.render(meta2, new_body))

    return do_index(root)
