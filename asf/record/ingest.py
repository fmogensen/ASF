"""asf.record.ingest — the evidence pass (``asf ingest``).

Matches every Feature/Story/Task/Bug/Epic to what ``asf.evidence`` found in the product repo,
derives its machine block, writes it with ``frontmatter.write_machine`` (the only function
allowed to touch the machine block), and appends one ``## History`` line per state or stage
change. Decisions and Rules have no lifecycle, so ingest never touches them.
"""
import re
import sys

from asf import env
from asf.evidence import evidence
from asf.record import frontmatter
from asf.record.core import canonicalize, load_items, now_iso, parse_sections, render_sections, section_content, today
from asf.record.index import do_index

EVIDENCE_TYPES = {'epic', 'feature', 'story', 'task', 'bug'}
LANDING_CHILD_TYPES = {'story', 'task', 'bug'}
MACHINE_KEY_ORDER = ['state', 'stage', 'stage_since', 'cost', 'evidence', 'blocked',
                     'blocked_by_open', 'updated']


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


def match_feature(meta, ev):
    """(slug, feature_evidence) by links.spec/plan path, then legacy_id, then the card's own id
    lower-cased as the slug, then branches, then PRs."""
    typed, _machine = frontmatter.split_machine(meta)
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
    own_id = typed.get('id') or ''
    if own_id:
        slug = own_id.lower()
        if slug in features:
            return slug, features[slug]
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
    """{'has_fixer': bool, 'merged_sha': str|None} from links.branches/links.prs, or None."""
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
    if branches & set(ev.get('branches') or []):
        has_fixer = True
    return {'has_fixer': has_fixer, 'merged_sha': merged_sha}


def match_ids(iid, ev):
    """(state, [evidence line]) from the id tokens naming `iid` in a branch, a PR or a commit on
    main — `(None, [])` when nothing names it. Only ever fills what the legacy-id and spec-path
    matching left unmatched; it never overrides a match."""
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
        phrase = '; '.join(ev_lines) if ev_lines else 'derived'
        history.append(f"- {stamp} ingest: state {old_state} → {new_state} ({phrase})")
    if stage is not None and stage != old_stage:
        phrase = '; '.join(ev_lines) if ev_lines else 'derived'
        history.append(f"- {stamp} ingest: stage {old_stage or 'card'} → {stage} ({phrase})")
    return ordered, history


def cmd_ingest(args, root):
    # the evidence is the record's product's (B-0050): the resolved --product, else the default
    # when one is configured (a record with no product configured reads evidence's own default)
    name = getattr(args, 'product', None)
    if not name:
        try:
            name = env.default_product_name()
        except env.ConfigError:
            name = None
    ev = evidence.load(fresh=getattr(args, 'fresh', False),
                       product=env.load_product(name) if name else None)
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

    ev_lines = {}
    stage_val = {}
    task_ev = {}

    # ---- Tasks: no dependency on any other item's derived state
    for iid, rec in canonical.items():
        if rec['meta'].get('type') != 'task':
            continue
        slug, tid, tev = match_task(rec['meta'], ev)
        task_ev[iid] = tev
        if tev is None:
            state, lines = match_ids(iid, ev)
            if state:
                new_state[iid] = state
            ev_lines[iid] = lines or [f"no evidence found ({date})"]
            continue
        new_state[iid] = evidence.task_state(True, tev.get('branch'), tev.get('pr_state'),
                                             tev.get('merged_sha'))
        lines = []
        if tev.get('merged_sha'):
            lines.append(f"PR #{tev.get('pr')} merged ({tev['merged_sha'][:9]})")
        elif tev.get('branch') and tev.get('pr'):
            lines.append(f"branch {tev['branch']}, PR #{tev['pr']} {tev.get('pr_state')}")
        elif tev.get('branch'):
            lines.append(f"branch {tev['branch']} exists")
        elif tev.get('pr'):
            lines.append(f"PR #{tev['pr']} {tev.get('pr_state')}")
        else:
            lines.append(f"in plan {slug} ({tid}), no branch yet")
        ev_lines[iid] = lines

    # ---- Stories: depend on whether a Task lists them while Active
    story_any_active = {}
    for iid, rec in canonical.items():
        if rec['meta'].get('type') != 'task' or new_state.get(iid) != 'Active':
            continue
        for sid in rec['meta'].get('stories') or []:
            story_any_active[sid] = True

    for iid, rec in canonical.items():
        if rec['meta'].get('type') != 'story':
            continue
        legacy, sev = match_story(rec['meta'], ev)
        if sev is None:
            state, lines = match_ids(iid, ev)
            if state:
                new_state[iid] = state
            ev_lines[iid] = lines or [f"no evidence found ({date})"]
            continue
        any_active = story_any_active.get(iid, False)
        new_state[iid] = evidence.story_state(any_active, sev['status'])
        lines = [f"matrix status {sev['status']} ({legacy})"]
        if any_active:
            lines.append("a Task lists it, Active")
        ev_lines[iid] = lines

    # ---- Bugs: independent of other items
    for iid, rec in canonical.items():
        if rec['meta'].get('type') != 'bug':
            continue
        bev = match_bug(rec['meta'], ev)
        if bev is None or not (bev['has_fixer'] or bev['merged_sha']):
            # no link, or links that show nothing: the item's own id is the evidence left
            state, lines = match_ids(iid, ev)
            if state:
                new_state[iid] = state
            elif bev is not None:
                new_state[iid] = evidence.bug_state(False, None, False)
            ev_lines[iid] = lines or [f"no evidence found ({date})"]
            continue
        merged_in_prod = bool(bev['merged_sha']) and evidence.ancestor_of(bev['merged_sha'], ev.get('prod_sha'))
        new_state[iid] = evidence.bug_state(bev['has_fixer'], bev['merged_sha'], merged_in_prod)
        lines = []
        if bev['merged_sha']:
            note = ' -- pending 3-day quiet' if merged_in_prod else ''
            lines.append(f"fix merged ({bev['merged_sha'][:9]}){note}")
        elif bev['has_fixer']:
            lines.append('fixer branch/PR open')
        ev_lines[iid] = lines or [f"no evidence found ({date})"]

    # ---- Features: depend on their own Task children's derived state
    for iid, rec in canonical.items():
        if rec['meta'].get('type') != 'feature':
            continue
        slug, fev = match_feature(rec['meta'], ev)
        if fev is None:
            # no spec/plan/legacy match: its children and its own id tokens are what is left.
            # All of its Stories/Tasks/Bugs Closed, or a commit on main naming it → landed.
            kids = [new_state.get(cid) for cid, crec in canonical.items()
                    if crec['meta'].get('parent') == iid
                    and crec['meta'].get('type') in LANDING_CHILD_TYPES]
            kids_closed = bool(kids) and all(s == 'Closed' for s in kids)
            state, lines = match_ids(iid, ev)
            named = bool(((ev.get('ids') or {}).get(iid) or {}).get('commit'))
            if kids_closed or named:
                new_state[iid] = 'Resolved'
                stage_val[iid] = 'landed'
                lines = (lines if named else []) + (
                    [f"{len(kids)}/{len(kids)} children Closed"] if kids_closed else [])
                ev_lines[iid] = lines
                continue
            # a Feature nothing in the product repo knows yet IS a card — an empty stage would
            # make a decided card invisible to the CARD → SPEC feeder row that reads this stage.
            ev_lines[iid] = lines or [f"no evidence found ({date})"]
            new_state[iid] = state or 'New'
            stage_val[iid] = 'card'
            continue
        child_ids = [cid for cid, crec in canonical.items()
                    if crec['meta'].get('type') == 'task' and crec['meta'].get('parent') == iid]
        child_states = [new_state[cid] for cid in child_ids]
        all_closed = bool(child_ids) and all(s == 'Closed' for s in child_states)
        all_merged_in_prod = all_prs_checked = False
        if all_closed:
            all_merged_in_prod = all(
                task_ev.get(cid) and evidence.ancestor_of(task_ev[cid].get('merged_sha'), ev.get('prod_sha'))
                for cid in child_ids)
            all_prs_checked = all(
                task_ev.get(cid) and task_ev[cid].get('pr') in ev['checked'] for cid in child_ids)

        spec_review = fev.get('spec_review')
        plan_review = fev.get('plan_review')
        # B-0059: the incident lane lands a spec/plan straight to main with no review round at
        # all — gating "approved" on a review verdict left every such doc reading spec-draft
        # forever. Landing on main is itself the lane's approval.
        spec_approved = bool(fev.get('spec_on_main')) or bool(spec_review and spec_review[1] == 'APPROVED')
        plan_approved = bool(fev.get('plan_on_main')) or bool(plan_review and plan_review[1] == 'APPROVED')

        # B-0059: a matched Feature with no Task children of its own (the incident lane never
        # opens one) is landed the same way an unmatched Feature already is — a code commit on
        # main naming it, with doc-lane commits (spec/plan/review/adjudicate) already excluded
        # from `ids` upstream so a spec landing does not count as the code landing.
        landed_by_commit = not child_ids and bool(((ev.get('ids') or {}).get(iid) or {}).get('commit'))
        if landed_by_commit:
            new_state[iid] = 'Resolved'
            stage_val[iid] = 'landed'
        else:
            new_state[iid] = evidence.feature_state(bool(fev.get('spec_on_main')), plan_approved,
                                                    all_closed, all_merged_in_prod, all_prs_checked)
            spec_dict = {'exists': bool(fev.get('spec')), 'approved': spec_approved,
                        'review': spec_review[:2] if spec_review else None}
            plan_dict = {'exists': bool(fev.get('plan')), 'approved': plan_approved,
                        'review': plan_review[:2] if plan_review else None}
            stage_val[iid] = evidence.feature_stage(spec_dict, plan_dict, child_states, all_merged_in_prod)

        lines = []
        if landed_by_commit:
            _named_state, named_lines = match_ids(iid, ev)
            lines.extend(named_lines)
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
        ev_lines[iid] = lines or [f"no evidence found ({date})"]

    # ---- Epics: purely a function of their children's derived state; no product-repo evidence
    for iid, rec in canonical.items():
        if rec['meta'].get('type') != 'epic':
            continue
        child_ids = [cid for cid, crec in canonical.items() if crec['meta'].get('parent') == iid]
        child_states = [new_state[cid] for cid in child_ids if cid in new_state]
        new_state[iid] = evidence.epic_state(child_states, bool(rec['meta'].get('closed')))

    # ---- write: state/stage/evidence/blocked, one write_machine + History append per changed item
    for iid, rec in canonical.items():
        type_ = rec['meta'].get('type')
        if type_ not in EVIDENCE_TYPES:
            continue
        blocked_pair = evidence.blocked_of(rec['meta'].get('blockedBy'), new_state)
        lines = ev_lines.get(iid) if type_ != 'epic' else None
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
