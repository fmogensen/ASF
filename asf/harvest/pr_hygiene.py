#!/usr/bin/env python3
"""pr_hygiene.py — the lane branches that wait on a person or a session, read off the lane.

The classification this module once did on its own (dirty PRs, their review, "dirty since") is
the lane's now (:mod:`asf.harvest.lane`): a PR closed unmerged, a branch gone, a superseded item
or a branch unmoved past ``conventions.lane.stale_after`` is STALE (T12), and a branch the gate
could not rebase is BACK ``kind=conflict`` (T9) — both written on the run line by the lane, never
decided here. A branch no run holds at all (an orphan) is the lane's to close or adopt
(:meth:`asf.harvest.lane.Lane.orphan_facts`). This is the read-only view over them:

  STALE → CLOSE      <branch> (<item>) PR #<n> — <why>          the lane found it stale
  CONFLICT → REBASE  <branch> (<item>) PR #<n> — back to its session (conflict)

    pr_hygiene.py            print the rows
    pr_hygiene.py --lanes    one JSON object per PR in a lane: {pr, branch, lane, since, action}
    pr_hygiene.py --close    the lane closes nothing itself: one line saying so
    pr_hygiene.py --product <name>   which product's lane to read (default: see asf.env)
"""
import json
import sys

from asf import env

STALE_CLOSE = 'STALE → CLOSE'
CONFLICT_REBASE = 'CONFLICT → REBASE'

#: row kind → (the listing's ``lane`` token, its ``action``) — the whole vocabulary a product's
#: rule check switches on, in one place (F-0119, D5/D6). A lane the hygiene view does not hold is
#: not in this table, and adding one is a row here and a branch in :func:`rows`.
LANES = {
    STALE_CLOSE:     ('stale', 'close'),
    CONFLICT_REBASE: ('conflict', 'rebase'),
}


def rows(product, state_dir=None):
    """``[{kind, branch, item, pr, why, since}]`` — the lane's STALE branches whose run did not
    land, and its BACK ``kind=conflict`` ones, in branch order.

    ``since`` is when the branch entered the lane it is in: the record's ``at``, which the lane
    stamps on a transition and does not rewrite while the state and the reason hold, else its
    ``head_at``, else None for a registry line written before either existed (F-0119, D2/D3)."""
    from asf.harvest import lane
    out = []
    for branch, rec in sorted(lane.snapshot_at(state_dir or env.state_dir(product)).items()):
        state, reason = rec.get('state'), rec.get('reason') or ''
        if state == lane.STALE and not rec.get('sha'):
            out.append({'kind': STALE_CLOSE, 'branch': branch, 'item': rec.get('item'),
                        'pr': rec.get('pr'), 'why': reason,
                        'since': rec.get('at') or rec.get('head_at')})
        elif state == lane.BACK and reason == 'kind=conflict':
            out.append({'kind': CONFLICT_REBASE, 'branch': branch, 'item': rec.get('item'),
                        'pr': rec.get('pr'), 'why': 'back to its session (conflict)',
                        'since': rec.get('at') or rec.get('head_at')})
    return out


def _entries(rows_):
    out = []
    for r in rows_:
        if not r.get('pr'):
            continue
        lane_token, action = LANES[r['kind']]
        out.append({'pr': r['pr'], 'branch': r['branch'], 'lane': lane_token,
                    'since': r.get('since'), 'action': action})
    return out


def lanes(product, state_dir=None):
    """``[{action, branch, lane, pr, since}]`` — one entry per :func:`rows` row that carries a PR
    number, in the same order: what ASF's PR-hygiene pass owns, as data.

    This is the answer a product's rule check reads to tell an open PR the factory is already
    working from one its own rule may flag (F-0119). It reads the lane registry the ``prs`` step
    already wrote (:func:`asf.harvest.lane.snapshot_at`) and makes no network call. A row with no
    PR number is not in the listing — it has no PR to answer about (D4)."""
    return _entries(rows(product, state_dir))


def render(row):
    pr = f" PR #{row['pr']}" if row.get('pr') else ''
    return f"{row['kind']}  {row['branch']} ({row.get('item') or '—'}){pr} — {row['why']}"


def main(argv):
    product_name = None
    if '--product' in argv:
        i = argv.index('--product')
        product_name = argv[i + 1] if i + 1 < len(argv) else None
    listing = '--lanes' in argv
    try:
        product = env.load_product(product_name)
        found = rows(product)
        if listing:
            for entry in _entries(found):
                print(json.dumps(entry, sort_keys=True))
            return 0
    except (OSError, ValueError, KeyError, env.ConfigError) as e:
        print(f'pr_hygiene: {e}', file=sys.stderr)
        # a listing's caller must be able to tell "ASF owns nothing" from "ASF could not be
        # read": 2 is a *check failure* to the rule contract, never a pass (F-0119, D7/D8)
        return 2 if listing else 0
    if '--close' in argv:
        print('pr_hygiene: the lane closes nothing itself — a STALE branch waits for its row')
        return 0
    for r in found:
        print(render(r))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
