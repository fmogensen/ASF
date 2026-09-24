"""asf.tick.step_wave — the tick's ``wave`` step: what the feeder says to start, launched.

0. ``approvals.raise_holds`` — the open holds said aloud, and the items they park (§2.4);
1. the index from the record clone (the tick's own, made once per tick — :class:`Context`);
2. ``inflight``: the sessions in ``~/.ASF/state/<product>/sessions.jsonl`` with no ``ended``; and
   ``busy``, the items whose pushed branch waits for harvest — no slot, but no second session;
3. ``feeder.plan_rows(index, product, inflight, capacity)`` — ``capacity`` is the resolver's
   ceiling (``asf.capacity.resolve``, spec §2.2's session law, bounded by the product's fair
   share of the usable pool); the feeder still takes this product's in-flight sessions off it
   itself (P5). Each launching row the fair share cut prints ``waits … — fair share: <n> of
   <usable> usable slots across <k> products``;
4. per launching row, a brief (``asf.briefs.build``) with the facts of its branch on the product
   repo's origin — whether it is pushed and its last commit, two ``git`` calls at most;
5. one ``workers.wave`` over every briefed row, so the pool's S1 reserve sees them all; it prints
   the ``launched`` / ``waits`` lines (``reserved for S1`` among them). A row the feeder holds
   back (``WAITS ON …``, no slot) prints its own ``waits`` line here, as does one an approval
   class holds.

Each launch appends a ``launch`` event (item, job, model, brief kind) to ``metrics/events``.
"""
import os
import re
import subprocess

from asf import approvals, env
from asf import capacity as capacity_mod
from asf.groom import policy as groom_policy
from asf.record import plan_order
from asf.workers import lifecycle
from asf.workers import pool as pool_mod

#: PD9 — for a kind whose job name is not ``<brief kind>-<item id>``, the Row attribute that
#: carries the job's key instead (the groom brief's job is ``groom-<date>``, D7).
KIND_JOB_KEY = {'groom': 'groom_date'}
_GROOM_FILE_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})\.md$')


def capacity(product=None):
    return capacity_mod.resolve(product or env.load_product()).sessions


def inflight(product):
    """The feeder's ``inflight`` list off the session ledger — :func:`asf.workers.lifecycle.inflight`."""
    return lifecycle.inflight(pool_mod.sessions_path(product))


def attempts(product):
    """``{item: runs the ledger holds for it}`` — :func:`asf.workers.lifecycle.attempts`."""
    return lifecycle.attempts(pool_mod.sessions_path(product))


def awaiting_harvest(product):
    """Items whose pushed branch waits for harvest: busy, but holding no slot
    (:func:`asf.workers.lifecycle.awaiting_harvest`)."""
    return lifecycle.awaiting_harvest(pool_mod.sessions_path(product))


def corrections(product):
    """``{item: {kind, text, at, rounds}}`` — the newest correction still waiting for its session
    (:func:`asf.workers.lifecycle.corrections`)."""
    return lifecycle.corrections(pool_mod.sessions_path(product))


def _newest_groom_file(root):
    d = os.path.join(root, 'groom')
    if not os.path.isdir(d):
        return None, None
    dates = [m.group(1) for name in os.listdir(d)
             for m in [_GROOM_FILE_RE.match(name)] if m]
    if not dates:
        return None, None
    date = sorted(dates)[-1]
    return date, os.path.join(d, f'{date}.md')


def groom_state(product, root):
    """§2.5's fact the feeder cannot derive from ``index.json`` (P5): the newest groom day's
    still-open questions, and how many ``groom-<date>`` sessions the ledger already holds for
    it. ``None`` when no groom file exists yet."""
    date, path = _newest_groom_file(root)
    if not date:
        return None
    with open(path, encoding='utf-8') as f:
        text = f.read()
    # the file may predate the index (answers applied since): what the factory already acts on
    # is nobody's question — the same suppression the groom ran when it wrote the file
    from asf.views import index_reader
    items = index_reader.load(root)[0] if os.path.isfile(os.path.join(root, 'index.json')) else {}
    sections, _n = groom_policy.suppress({'open': text.splitlines()}, items, inflight(product),
                                         product)
    pairs = groom_policy.open_questions('\n'.join(sections['open']))
    job = f'groom-{date}'
    # sessions, not ledger lines: a run's end and harvest lines are no second attempt
    attempts = sum(1 for rec in lifecycle.read_lines(pool_mod.sessions_path(product))
                   if rec.get('job') == job and lifecycle.is_launch(rec))
    open_ids = [iid for iid, _line in pairs]
    return {'date': date, 'file': path,
            'answers': os.path.join(env.state_dir(product), 'groom', f'{date}.answers'),
            'open': open_ids, 'lines': [line for _iid, line in pairs],
            'oldest': next((iid for iid, _l in pairs if not iid.startswith('inbox:')),
                           pairs[0][0] if pairs else None),
            'attempts': attempts,
            'new': _not_yet_put(product, job, open_ids) if attempts else list(open_ids)}


def _not_yet_put(product, job, open_ids):
    """The open questions the day's last adjudicate session was not given — asked since its
    brief was written. Its brief (``briefs/<job>.md``, rewritten per launch) lists the lines it
    was handed; no brief to read, and none counts as new."""
    path = os.path.join(env.state_dir(product), 'briefs', f'{job}.md')
    try:
        with open(path, encoding='utf-8') as f:
            given = {iid for iid, _l in groom_policy.open_questions(f.read())}
    except OSError:
        return []
    return [iid for iid in open_ids if iid not in given]


def plan_inputs(product, root):
    """The ledger's and the record's facts ``plan_rows`` takes beside the index — one place, so
    the tick, ``asf next`` and the status cell plan the same rows."""
    return {'attempts': attempts(product), 'corrections': corrections(product),
            'busy': awaiting_harvest(product),
            'groom_state': groom_state(product, root) if groom_policy.groom_auto(product) else None}


def held_by_share(items, product, running, resolved, planned, inputs):
    """The launching rows the fair share cut: planned at the ceiling the share lowered, and not
    in ``planned``. Empty when no share bounds this product."""
    from asf.feeder import rows as feeder_rows
    if not resolved.fair_share_reason or resolved.ceiling is None:
        return []
    seen = {(r.item_id, r.kind) for r in planned}
    wider = feeder_rows.plan_rows(items, product, running, resolved.ceiling, **inputs)
    return [r for r in wider if r.launches and (r.item_id, r.kind) not in seen]


def _git(repo, args):
    p = subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ''


def repo_facts(product, branch):
    """``{branch, pushed, remote_sha, last_commit}`` for ``branch`` on the product repo's origin."""
    repo = product.repo_dir
    if not repo or not branch:
        return {'branch': branch, 'pushed': False, 'remote_sha': '', 'last_commit': ''}
    heads = _git(repo, ['ls-remote', '--heads', 'origin', branch])
    sha = heads.split()[0] if heads else ''
    last = _git(repo, ['log', '-1', '--format=%H %cI %s', f'origin/{branch}']) if sha else ''
    return {'branch': branch, 'pushed': bool(sha), 'remote_sha': sha, 'last_commit': last}


def _build(*a, **kw):
    from asf import briefs
    return briefs.build(*a, **kw)


def _wave(*a, **kw):
    from asf.workers import wave
    return wave.wave(*a, **kw)


def job_name(brief_kind, item_id, key=None):
    """``<brief kind>-<item id>``, lowercased — unless ``key`` is given (PD9: a kind whose job
    must stay stable while ``item_id`` drifts, e.g. the groom day's oldest question), in which
    case the job carries ``key`` instead."""
    return f'{brief_kind}-{item_id if key is None else key}'.lower()


def worker_row(row, brief, items):
    """The workers' row for a feeder row and its brief."""
    item = items.get(row.item_id) or {}
    state, _, action = row.kind.partition(' → ')
    attr = KIND_JOB_KEY.get(brief.kind)
    key = getattr(row, attr) if attr else None
    return pool_mod.Row(job_name(brief.kind, row.item_id, key=key), row.item_id, state=state,
                        action=action, title=item.get('title', ''), model=brief.model,
                        kind=brief.kind, severity=item.get('severity'),
                        feature=row.feature_id or None, branch=row.branch or None,
                        add_dirs=getattr(brief, 'add_dirs', None) or ())


def run(ctx, out=print):
    from asf.feeder import rows as feeder_rows
    from asf.views import index_reader
    product = ctx.product
    held = approvals.raise_holds(ctx, out)
    items, _generated = index_reader.load(ctx.record_root())
    if product.repo_dir:  # defence in depth: a Task whose card lacks `after:` waits on its plan's order
        items = plan_order.overlay(items, plan_order.trunk_reader(product))
    running = inflight(product)
    r = capacity_mod.resolve(product)
    inputs = plan_inputs(product, ctx.record_root())
    planned = feeder_rows.plan_rows(items, product, running, r.sessions, **inputs)
    for row in held_by_share(items, product, running, r, planned, inputs):
        job = job_name(row.brief_kind, row.item_id)
        out(f'waits    {job:<24} {row.item_id:<10} — {r.fair_share_reason}')
    worker_rows, texts, kinds = [], {}, {}
    for row in planned:
        if not row.launches:
            out(f"waits    {'-':<24} {row.item_id:<10} — {row.action}")
            continue
        if row.item_id in held:                 # §2.4: a held item waits for a person, not a slot
            cls, level = held[row.item_id]
            job = job_name(row.brief_kind, row.item_id)
            out(f'waits    {job:<24} {row.item_id:<10} — held {cls} ({level})')
            continue
        brief = _build(product, row, items, running,
                       repo_facts=repo_facts(product, row.branch))
        wrow = worker_row(row, brief, items)
        worker_rows.append(wrow)
        texts[wrow.job] = brief.text
        kinds[wrow.job] = brief.kind
    ctx.event('capacity', sessions=r.sessions, sessions_inflight=len(running),
              sessions_bound_by=r.sessions_bound, fair_share=r.fair_share, usable=r.usable,
              active_products=r.active, ci=r.ci, ci_inflight=r.ci_inflight,
              ci_bound_by=r.ci_bound)
    if not worker_rows:
        out('wave: nothing to launch')
        return 0
    launched, _waits = _wave(product, worker_rows, len(worker_rows),
                             brief_fn=lambda r: texts[r.job], out=out)
    for wrow, rec in launched:
        ctx.event('launch', item=wrow.item, job=wrow.job,
                  model=rec.get('model'), brief_kind=kinds[wrow.job])
    ctx.counts['launches'] += len(launched)
    return 0
