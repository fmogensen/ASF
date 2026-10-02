"""``asf correct <item> --why TEXT [--from-pr N]`` — the operator's (or another session's) way
to ask for one CORRECT round on an item, with instructions.

The correction is written the way the harvest writes its own: a pending correction on the item's
newest run (kind :data:`OPERATOR`), so the feeder turns it into the item's FIX → CORRECT row and
the brief quotes the text under the usual correction head. Nothing here bypasses a rule:

* the round counts (:data:`asf.workers.lifecycle.ROUND_CAP`): an item that has used its rounds
  is refused — it goes to adjudication, not to another correction;
* the row is a ``correct`` row, so the one-push rule (:mod:`asf.workers.pushlog`) and the
  relaunch cap (:mod:`asf.workers.relaunch`) apply to its session as to any other;
* an item parked by hand, or with a session still running, is refused — ``asf unpark`` first.

``--from-pr N`` names the commits of pull request N in the text, so the session cherry-picks the
reference fix instead of re-deriving it; that PR is a reference, never landed.
"""
import datetime
import re

from asf import env
from asf.workers import lifecycle
from asf.workers import pool as pool_mod

#: The correction kind of a round asked for by hand.
OPERATOR = 'operator'
ITEM_RE = re.compile(r'[A-Z]+-\d+')


def pr_commits(product, number, fetch=None):
    """``(base, [sha, ...])`` of PR ``number``, oldest first; ``(None, [])`` when ``gh`` cannot say.
    ``fetch(args)`` is :func:`asf.metrics.metrics.gh_json` unless a test gives its own."""
    if fetch is None:
        from asf.metrics import metrics
        fetch = metrics.gh_json
    data = fetch(['pr', 'view', str(number), '--repo', product.repo_slug,
                  '--json', 'commits,baseRefName']) or {}
    shas = [c.get('oid') for c in data.get('commits') or [] if c.get('oid')]
    return data.get('baseRefName'), shas


def instruction(why, branch, number=None, base=None, shas=()):
    """The correction's text: ``why``, then — with a PR — the cherry-pick it names."""
    text = why.strip()
    if number:
        picks = ' '.join(shas) if shas else f'(list them with: gh pr view {number} --json commits)'
        text += (f'\n\nREFERENCE FIX: PR #{number}'
                 + (f' (stacked on {base})' if base else '')
                 + f' is a reference, never landed. Fetch it (git fetch origin pull/{number}/head), '
                   f'cherry-pick its commit(s) onto {branch} in order — {picks} — skipping any '
                   f'the branch already holds, resolve conflicts keeping its intent, run the '
                   f'pre-push check, and push once.')
    return text


def cmd_correct(args, fetch=None):
    product = env.load_product(getattr(args, 'product', None))
    why = str(getattr(args, 'why', '') or '').strip()
    item = (args.item or '').strip().upper()
    if not why:
        print('asf correct: --why is required — it is the correction the session is given')
        return 2
    if not ITEM_RE.fullmatch(item):
        print(f'asf correct: {args.item} is no item id')
        return 2
    path = pool_mod.sessions_path(product)
    runs = [r for r in lifecycle.item_runs(path, item) if r.get('job') and r.get('branch')]
    if not runs:
        print(f'asf correct: {item} has no run with a branch in the ledger — nothing to correct')
        return 1
    if lifecycle.item_park(path, item):
        print(f'asf correct: {item} is parked by hand — `asf unpark {item}` first')
        return 1
    if any(lifecycle.is_live(r) for r in lifecycle.item_runs(path, item)):
        print(f'asf correct: {item} has a session running — correct it when it ends')
        return 1
    rounds = lifecycle.rounds_of(path, item)
    if rounds >= lifecycle.ROUND_CAP:
        print(f'asf correct: {item} has used its {lifecycle.ROUND_CAP} correction rounds — it goes '
              f'to adjudication, not another correction')
        return 1
    run = max(runs, key=lambda r: r.get('started') or '')
    number = getattr(args, 'from_pr', None)
    base, shas = (None, [])
    if number:
        base, shas = pr_commits(product, number, fetch)
        if not shas:
            print(f'asf correct: PR #{number} names no commits (gh could not read it)')
            return 1
    text = instruction(why, run['branch'], number, base, shas)
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    corr = {'kind': OPERATOR, 'text': text, 'at': now, 'same': 1}
    if number:
        corr['from_pr'] = int(number)
    pool_mod.update_session(product, run['job'], correction=corr, rounds=rounds + 1)
    print(f'corrected {item} on {run["branch"]} (round {rounds + 1} of {lifecycle.ROUND_CAP}, '
          f'job {run["job"]}): {why}')
    return 0


def register(sub):
    """``asf correct <item> --why TEXT [--from-pr N] [--product P]``."""
    p = sub.add_parser('correct', help='ask for one correction round on an item, with instructions '
                                       '(--from-pr: cherry-pick that PR\'s commits)')
    p.add_argument('item', help='the item id')
    p.add_argument('--why', required=True, help='the instructions the correction session is given')
    p.add_argument('--from-pr', type=int, metavar='N',
                   help='a reference PR whose commits the session cherry-picks (never landed)')
    env.add_product_arg(p)
    p.set_defaults(func=cmd_correct)
