#!/usr/bin/env python3
"""pr_hygiene.py — the lane branches that wait on a person or a session, read off the lane.

The classification this module once did on its own (dirty PRs, their review, "dirty since") is
the lane's now (:mod:`asf.harvest.lane`): a PR closed unmerged, a branch gone, a superseded item
or a branch unmoved past ``conventions.lane.stale_after`` is STALE (T12), and a branch the gate
could not rebase is BACK ``kind=conflict`` (T9) — both written on the run line by the lane, never
decided here. This is the read-only view over them:

  STALE → CLOSE      <branch> (<item>) PR #<n> — <why>          the lane found it stale
  CONFLICT → REBASE  <branch> (<item>) PR #<n> — back to its session (conflict)

    pr_hygiene.py            print the rows
    pr_hygiene.py --lanes    the PR numbers in either lane, one per line
    pr_hygiene.py --close    the lane closes nothing itself: one line saying so
    pr_hygiene.py --product <name>   which product's lane to read (default: see asf.env)
"""
import sys

from asf import env

STALE_CLOSE = 'STALE → CLOSE'
CONFLICT_REBASE = 'CONFLICT → REBASE'


def rows(product, state_dir=None):
    """``[{kind, branch, item, pr, why}]`` — the lane's STALE branches whose run did not land,
    and its BACK ``kind=conflict`` ones, in branch order."""
    from asf.harvest import lane
    out = []
    for branch, rec in sorted(lane.snapshot_at(state_dir or env.state_dir(product)).items()):
        state, reason = rec.get('state'), rec.get('reason') or ''
        if state == lane.STALE and not rec.get('sha'):
            out.append({'kind': STALE_CLOSE, 'branch': branch, 'item': rec.get('item'),
                        'pr': rec.get('pr'), 'why': reason})
        elif state == lane.BACK and reason == 'kind=conflict':
            out.append({'kind': CONFLICT_REBASE, 'branch': branch, 'item': rec.get('item'),
                        'pr': rec.get('pr'), 'why': 'back to its session (conflict)'})
    return out


def render(row):
    pr = f" PR #{row['pr']}" if row.get('pr') else ''
    return f"{row['kind']}  {row['branch']} ({row.get('item') or '—'}){pr} — {row['why']}"


def main(argv):
    product_name = None
    if '--product' in argv:
        i = argv.index('--product')
        product_name = argv[i + 1] if i + 1 < len(argv) else None
    try:
        product = env.load_product(product_name)
        found = rows(product)
    except (OSError, ValueError, KeyError, env.ConfigError) as e:
        print(f'pr_hygiene: {e}', file=sys.stderr)
        return 0
    if '--lanes' in argv:
        for r in found:
            if r.get('pr'):
                print(r['pr'])
        return 0
    if '--close' in argv:
        print('pr_hygiene: the lane closes nothing itself — a STALE branch waits for its row')
        return 0
    for r in found:
        print(render(r))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
