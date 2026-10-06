"""asf.stale_act — stale means act, not flag: a leftover past its deadline is acted on.

``asf stale`` reports items over their stage limit, and the lane marks a PR branch STALE — and
until now nothing closed, archived or re-planned them (a product had 19 PRs open for days; a Task
sat Active for two weeks ~1,100 commits behind its trunk). This pass owns two of those leftovers
and acts on each past its deadline, records every action in the ledger
(``state/<product>/stale-acts.jsonl``), and leaves anything no rule covers to the console:

* **pr** — an open PR on a branch the lane holds STALE (:mod:`asf.harvest.pr_hygiene`) for
  longer than ``pr_after`` (3d): its tip is kept as ``archive/pr-<n>``, the PR is closed
  with a one-line reason, the branch is deleted, and — when its item is still open — a replan is
  queued (:func:`queue_replan`).
* **task** — an Active Task past ``task_factor`` (3) times its ``task_active`` stage limit,
  whose lane branch has not moved for as long either, and whose branch is more than
  ``task_behind`` (200) commits behind the trunk: it is re-planned from the current trunk
  instead of corrected again — the branch archived as ``archive/<branch>``, its PR closed, the
  branch deleted, and the replan queued.

A replan is the record's own: a Task's ``reshape:`` decision on its Feature (the feeder's
RESHAPE → REPLAN row, :mod:`asf.record.replan`), or on the Task itself when it has no Feature.
A Bug or a Feature document has no replan rule: its branch is cleaned and the ledger says so —
the feeder starts it again from the trunk.

Never acted on: an item with a live session, an operator park, a lane state that is merging
(MERGING/QUEUED/MERGED) or parked (a draft PR).

**Who acts.** Every forge action goes through :mod:`asf.forge` (GitHub, or plain git where a
product has no PR host — the ``pr`` kind is then not applicable). The tick's health step runs
the pass every tick; it acts only where ``conventions.stale.act`` is true (default false, the
same for every product, the factory's own included: :func:`acts`) and elsewhere prints the
``would …`` lines (a dry run) until the operator turns it on per product. ``asf stale --act`` runs it by hand (``--dry-run``: the
``would …`` lines only). Each kind can be switched off (``conventions.stale.pr: false``,
``conventions.stale.task: false``); the other keys are ``pr_after``, ``task_factor`` and
``task_behind`` (:data:`DEFAULTS`).
"""
import datetime

from asf import env
from asf.tick import stale as stale_mod

LEDGER = 'stale-acts.jsonl'

#: every ``conventions.stale`` key a product may set, with its default (a small repo's values)
DEFAULTS = {
    'act': False,          # off (dry run) for every product until its own config turns it on
    'pr': True,            # the stale-PR kind
    'pr_after': '3d',      # a lane-STALE PR older than this is archived and closed
    'task': True,          # the stale-Task kind
    'task_factor': 3,      # times its task_active stage limit before a Task is re-planned
    'task_behind': 200,    # commits behind the trunk before a Task is re-planned
}

#: lane states a pass never touches: a merge in progress or done, or the owner's draft
HANDS_OFF = frozenset({'MERGING', 'QUEUED', 'MERGED', 'PARKED'})

DONE = ('Resolved', 'Closed')

#: branch kinds that carry a document, never a Task's code
DOC_KINDS = frozenset({'spec', 'plan', 'replan'})


def settings(product):
    """:data:`DEFAULTS` overlaid with the product's ``conventions.stale`` map (bad values keep
    the default). Under ``conventions:`` — whose extra keys every pinned reader keeps — so a
    product file carrying it still loads under an older ASF."""
    out = dict(DEFAULTS)
    conv = product.conventions if product is not None else None
    given = (conv.get('stale') if conv is not None else None) or {}
    if not isinstance(given, dict):
        return out
    for key in ('act', 'pr', 'task'):
        if isinstance(given.get(key), bool):
            out[key] = given[key]
    after = given.get('pr_after')
    if isinstance(after, str) and stale_mod.DURATION_RE.match(after.strip()):
        out['pr_after'] = after.strip()
    for key in ('task_factor', 'task_behind'):
        v = given.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            out[key] = v
    return out


def acts(product, knobs=None):
    """True when the pass acts for ``product`` unasked: ``conventions.stale.act`` — false by
    default for every product (no repository is special); a product opts in in its own file."""
    knobs = knobs or settings(product)
    return bool(knobs['act'])


# ---- the plan: pure over facts ---------------------------------------------------------------

def _age(since, now):
    at = stale_mod.parse_iso(since) if isinstance(since, str) else None
    return None if at is None else (now - at).total_seconds()


def pr_actions(lane_rows, open_prs, items, now, after_s, live=(), parked=(), lanes=None):
    """One ``pr`` action per open PR whose branch the lane holds STALE (``lane_rows``:
    :func:`asf.harvest.pr_hygiene.rows`) for more than ``after_s`` seconds. ``open_prs``:
    :meth:`asf.forge.Forge.open_prs` (None: not read yet — every row counts, its head the lane's).
    ``items``: ``{id: {type, state, removed, parent}}``."""
    from asf.harvest.pr_hygiene import STALE_CLOSE
    out = []
    for r in lane_rows or ():
        if r.get('kind') != STALE_CLOSE or not r.get('pr'):
            continue
        number = int(r['pr'])
        pr = (open_prs.get(number) if open_prs is not None
              else {'head': ((lanes or {}).get(r['branch']) or {}).get('head') or ''})
        item = r.get('item')
        if not pr or item in live or item in parked:
            continue
        if ((lanes or {}).get(r['branch']) or {}).get('state') in HANDS_OFF:
            continue
        age = _age(r.get('since'), now)
        if age is None or age <= after_s:
            continue
        out.append({'kind': 'pr', 'item': item, 'branch': r['branch'], 'pr': number,
                    'head': pr.get('head') or '', 'archive': f'pr-{number}',
                    'why': f"PR #{number} STALE for {stale_mod.format_age(age)} "
                           f"({r.get('why') or 'stale'})",
                    'replan': is_open(items.get(item))})
    return out


def task_actions(items, now, limit_s, factor, behind_limit, branch_of, behind, live=(),
                 parked=(), lanes=None):
    """One ``task`` action per Active Task past ``factor`` × ``limit_s`` in its stage, its lane
    branch unmoved as long, and ``behind(branch)`` more than ``behind_limit`` commits behind the
    trunk. ``branch_of(task_id)``: ``(branch, lane record or None)``."""
    out = []
    deadline = factor * limit_s
    for iid, v in sorted((items or {}).items()):
        if v.get('type') != 'task' or v.get('state') != 'Active' or v.get('removed'):
            continue
        if iid in live or iid in parked:
            continue
        age = _age(v.get('stage_since'), now)
        if age is None or age <= deadline:
            continue
        branch, rec = branch_of(iid)
        if not branch:
            continue
        rec = rec or (lanes or {}).get(branch) or {}
        if rec.get('state') in HANDS_OFF:
            continue
        moved = _age(rec.get('head_at') or rec.get('at'), now)
        if moved is not None and moved <= deadline:
            continue
        n = behind(branch)
        if n is None or n <= behind_limit:
            continue
        out.append({'kind': 'task', 'item': iid, 'branch': branch, 'pr': None,
                    'head': rec.get('head') or '', 'archive': branch,
                    'why': f"{iid} Active for {stale_mod.format_age(age)} (over {factor}x its "
                           f"limit), {n} commits behind the trunk",
                    'replan': True})
    return out


def is_open(item):
    return bool(item) and not item.get('removed') and item.get('state', 'New') not in DONE


# ---- the replan --------------------------------------------------------------------------------

def replan_target(items, item_id):
    """``(card id, why-not)`` the replan of ``item_id`` is written on: a Task's open Feature
    (its ``reshape:`` re-cuts the Feature's open Tasks), else the Task itself; None and the
    reason for an item no replan rule covers."""
    item = items.get(item_id) or {}
    if not is_open(item):
        return None, f'{item_id} is not open'
    if item.get('type') != 'task':
        return None, f'no replan rule for a {item.get("type") or "card"}'
    parent = items.get(item.get('parent') or '') or {}
    if parent.get('type') == 'feature' and is_open(parent):
        return parent['id'], ''
    return item_id, ''


def queue_replan(root, product, items, item_id, why, stamp, queued, write=None):
    """Write the replan decision for ``item_id`` (``reshape: <why>``) on its
    :func:`replan_target`, once per card a pass (``queued``); a Feature whose replan is already
    pending is left as it is. One line saying what was done."""
    from asf.record import replan as replan_mod
    target, no = replan_target(items, item_id)
    if not target:
        return f'no replan: {no}'
    if target in queued:
        return f'replan of {target} already queued this pass'
    card = items.get(target) or {}
    if replan_mod.pending(card):
        queued.add(target)
        return f'replan of {target} already pending'
    if write is None:
        from asf.tick.widen_footprint import write_card as write
    text = f'stale: {why} — re-cut from the current trunk'
    err = write(root, target, {'reshape': text}, stamp, f'reshape → {text}', product=product)
    if err:
        return f'replan of {target} not written: {err}'
    queued.add(target)
    card['reshape'] = text
    return f'replan of {target} queued'


# ---- the facts --------------------------------------------------------------------------------

def record_items(root):
    """``{id: {id, type, state, stage_since, parent, removed, reshape, reshape_applied}}``."""
    from asf.record import frontmatter
    from asf.record.core import canonicalize, load_items
    by_id, _errors = load_items(root)
    canonical, _dupes = canonicalize(by_id)
    out = {}
    for iid, rec in canonical.items():
        typed, machine = frontmatter.split_machine(rec['meta'])
        out[iid] = {'id': iid, 'type': typed.get('type'), 'parent': typed.get('parent'),
                    'removed': typed.get('removed'), 'reshape': typed.get('reshape'),
                    'reshape_applied': typed.get('reshape_applied'),
                    'state': machine.get('state', 'New'), 'stage': machine.get('stage'),
                    'stage_since': machine.get('stage_since')}
    return out


def _behind_fn(product):
    from asf import gitops
    trunk = product.conventions.main
    repo = product.repo_dir

    def behind(branch):
        if not repo:
            return None
        return gitops.rev_list_count(repo, f'origin/{branch}', f'origin/{trunk}')
    return behind


def plan(product, root, *, forge=None, now=None, items=None, state_dir=None, live=None,
         parked=None, behind=None, out=print):
    """Every action the pass would take now, gathered from the record, the lane registry, the
    run registry and the forge."""
    from asf import forge as forge_mod
    from asf.harvest import lane as lane_mod
    from asf.harvest import pr_hygiene
    from asf.harvest.harvest import sessions_path
    from asf.workers import lifecycle
    knobs = settings(product)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    forge = forge or forge_mod.for_product(product)
    items = items if items is not None else record_items(root)
    state_dir = state_dir or env.state_dir(product)
    path = sessions_path(state_dir)
    if live is None:
        live = {r.get('item') for r in lifecycle.inflight(path)}
    if parked is None:
        parked = {p.get('item') for p in lifecycle.parks(path)
                  if (p.get('scope') or 'item') == 'item'}
    lanes = lane_mod.snapshot_at(state_dir)
    actions = []
    if knobs['pr']:
        actions += pr_actions(pr_hygiene.rows(product, state_dir), None, items, now,
                              stale_mod.limit_seconds(knobs['pr_after']), live, parked, lanes)
    if knobs['task']:
        limits = stale_mod.load_limits(product)
        conv = product.conventions
        by_item = {}
        for b, rec in sorted(lanes.items()):
            # the Task's code branch: a spec, plan or replan document branch is not its work
            if rec.get('item') and conv.branch_kind(b) not in DOC_KINDS:
                by_item.setdefault(rec['item'], (b, rec))

        def branch_of(iid):
            return by_item.get(iid) or (conv.branch('code', iid), None)
        done = {a['item'] for a in actions}
        actions += [a for a in task_actions(
            items, now, stale_mod.limit_seconds(limits['task_active']), knobs['task_factor'],
            knobs['task_behind'], branch_of, behind or _behind_fn(product), live, parked,
            lanes) if a['item'] not in done]
    if not actions:
        return []
    # the forge is read only when something is due: one open-PR list a pass, never on a quiet one
    open_prs = forge.open_prs() if forge.has_prs else {}
    kept = []
    for a in actions:
        if a['kind'] == 'pr':
            if not forge.has_prs:
                out(f"stale: PR #{a['pr']} — not applicable (no pull requests on this forge)")
                continue
            if open_prs is None:
                out(f"stale: PR #{a['pr']} — the open PRs did not read; left for the next pass")
                continue
            pr = open_prs.get(a['pr'])
            if not pr:
                continue        # closed or merged since the lane last read it
            a = dict(a, head=pr.get('head') or a.get('head'))
        else:
            numbers = {v.get('branch'): n for n, v in (open_prs or {}).items()}
            a = dict(a, pr=numbers.get(a['branch']))
        kept.append(a)
    return kept


# ---- the act ----------------------------------------------------------------------------------

def _ledger(product, rec):
    from asf.state import store
    try:
        store.append(product, LEDGER, rec)
    except Exception as e:  # noqa: BLE001 — the action stands; the line says it was not kept
        return f'ledger not written: {e}'
    return ''


def act(product, root, actions, *, forge=None, dry_run=False, items=None, out=print,
        now=None, write=None):
    """Carry ``actions`` out (or, ``dry_run``, print the ``would …`` line of each). Every action
    taken is one ledger line ``{at, kind, item, branch, pr, archive, why, did}``. Returns the
    ledger records (dry: the would-records, not written)."""
    from asf import forge as forge_mod
    forge = forge or forge_mod.for_product(product)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    stamp = now.strftime('%Y-%m-%d %H:%M')
    at = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    items = items if items is not None else (record_items(root) if root else {})
    queued, done = set(), []
    for a in actions:
        what = (f"PR #{a['pr']} " if a.get('pr') else '') + a['branch']
        if dry_run:
            steps = [f"archive {what} as archive/{a['archive']}"]
            steps += [f"close PR #{a['pr']}"] if a.get('pr') else []
            steps += [f"delete {a['branch']}"]
            steps += [f"queue a replan of {a['item']}"] if a.get('replan') else []
            out(f"stale: would {', '.join(steps)} — {a['why']}")
            done.append(dict(a, at=at, did=['dry run']))
            continue
        did = []
        tip = a.get('head') or ''
        if tip:
            ok, detail = forge.archive(a['archive'], tip, f"archive({a.get('item') or a['branch']}"
                                       f"): {a['branch']} — {a['why']}; kept for reference "
                                       f"[skip ci]")
            if not ok:
                out(f"stale: held {what} — archive not made: {detail}")
                rec = dict(a, at=at, did=[f'held: archive not made ({detail})'])
                _ledger(product, rec)
                done.append(rec)
                continue
            did.append(f"archived as archive/{a['archive']} ({str(detail)[:9]})")
        if a.get('pr') and forge.has_prs:
            ok, detail = forge.close_pr(a['pr'], f"Closed by the factory: {a['why']}. "
                                                 f"Its tip is kept as `archive/{a['archive']}`.")
            did.append(f"closed PR #{a['pr']}" if ok else f"PR #{a['pr']} not closed: {detail}")
        if tip:
            ok, detail = forge.delete_branch(a['branch'], tip)
            did.append(f"deleted {a['branch']}" if ok else f"{a['branch']} kept: {detail}")
        if a.get('replan') and a.get('item'):
            did.append(queue_replan(root, product, items, a['item'], a['why'], stamp, queued,
                                    write=write))
        rec = dict(a, at=at, did=did)
        miss = _ledger(product, rec)
        out(f"stale: {what} — {'; '.join(did)} ({a['why']})" + (f' [{miss}]' if miss else ''))
        done.append(rec)
    return done


def run(product, root, *, act_now=None, dry_run=None, out=print, forge=None, now=None,
        **facts):
    """The pass: :func:`plan`, then :func:`act`. ``act_now``: None — :func:`acts` decides (the
    tick); True — act (``asf stale --act``). ``dry_run`` wins over both."""
    knobs = settings(product)
    if dry_run is None:
        dry_run = not (act_now if act_now is not None else acts(product, knobs))
    from asf import forge as forge_mod
    forge = forge or forge_mod.for_product(product)
    if facts.get('items') is None:
        facts['items'] = record_items(root)
    actions = plan(product, root, forge=forge, now=now, out=out, **facts)
    if not actions:
        return []
    return act(product, root, actions, forge=forge, dry_run=dry_run, out=out, now=now,
               items=facts['items'])
