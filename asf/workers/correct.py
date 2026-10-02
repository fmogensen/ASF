"""``asf correct <item> --why TEXT [--from-pr N]`` — the operator's (or another session's) way
to ask for one CORRECT round on an item, with instructions.

The correction is written the way the harvest writes its own: a pending correction on the item's
newest run (kind :data:`OPERATOR`), so the feeder turns it into the item's FIX → CORRECT row and
the brief quotes the text under the usual correction head. Nothing here bypasses a rule:

* the round counts (:data:`asf.workers.lifecycle.ROUND_CAP`): an item that has used its rounds
  goes to adjudication, not to another correction — and the instruction goes with it as an
  operator ruling (:func:`attach_ruling`): filed on the card's ``## History`` as an
  ``adjudicate (operator)`` line, so it binds every later review (:mod:`asf.evidence.rulings`),
  and written as the held run's correction at the cap, so the feeder's STALEMATE → ADJUDICATE
  row carries it and the adjudicate brief quotes it;
* the row is a ``correct`` row, so the one-push rule (:mod:`asf.workers.pushlog`) and the
  relaunch cap (:mod:`asf.workers.relaunch`) apply to its session as to any other;
* an item parked by hand, or with a session still running, is refused — ``asf unpark`` first.

``--from-pr N`` names the commits of pull request N in the text, so the session cherry-picks the
reference fix instead of re-deriving it; that PR is a reference, never landed.
"""
import datetime
import os
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
    run = max(runs, key=lambda r: r.get('started') or '')
    if rounds >= lifecycle.ROUND_CAP:
        if getattr(args, 'from_pr', None):
            print(f'asf correct: {item} has used its {lifecycle.ROUND_CAP} correction rounds — '
                  f'at the cap the instruction is an operator ruling; give it without --from-pr')
            return 1
        return attach_ruling(product, item, run, why)
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


#: The head an operator ruling's correction text opens with: the adjudicate brief quotes it.
RULING_HEAD = ('OPERATOR RULING (asf correct, {at}) — binding. The operator has ruled on this item '
               'at the correction-round cap; it is filed on the card\'s History as '
               '`adjudicate (operator)`. Rule every open finding by it, carry it out on {branch}, '
               'and give it as your report\'s `ruling:`:\n\n')


def file_ruling(product, item, text, stamp):
    """Appends ``- <stamp> adjudicate (operator): <text>`` to ``item``'s card ``## History``
    in the product's record and publishes it (commit and push, when the record is a checkout).
    ``''`` when filed, else why not."""
    from asf.evidence import rulings
    from asf.record import frontmatter, publish, stage
    from asf.record.ingest import append_history_lines
    root = getattr(product, 'backlog_dir', None)
    card = rulings._card_file(product, item)
    if not root or not card or not os.path.isfile(card):
        return f'no card for {item} in the record'
    line = f'- {stamp} adjudicate ({OPERATOR}): {" ".join(text.split())}'
    rel = os.path.relpath(card, root)

    def _write(_root, path, relpath):
        with open(path, encoding='utf-8') as f:
            meta, body = frontmatter.parse(f.read(), path=relpath)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(frontmatter.render(meta, append_history_lines(body, [line])))

    _r, _staged, findings = stage.guarded(root, 'correct', _write, (card, rel), product=product,
                                          only=[rel])
    if findings:
        return 'refused: ' + '; '.join(f'{f.invariant}: {f.message}' for f in findings)
    if not publish.publish(root, card, f'record: {item} operator ruling (asf correct)'):
        return 'filed, but the record push was refused'
    return ''


def attach_ruling(product, item, run, why):
    """At the round cap: ``why`` becomes an operator ruling — filed on the card's History
    (:func:`file_ruling`) and written as ``run``'s correction at the cap (``same`` =
    :data:`~asf.workers.lifecycle.ROUND_CAP`, ``at_cap``), so the feeder's ADJUDICATE row takes
    it with its text. No round is spent."""
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    filed = file_ruling(product, item, why, now[:16].replace('T', ' '))
    text = RULING_HEAD.format(at=now, branch=run['branch']) + why
    corr = {'kind': OPERATOR, 'text': text, 'at': now, 'same': lifecycle.ROUND_CAP,
            'at_cap': True, 'operator_ruling': True}
    pool_mod.update_session(product, run['job'], correction=corr)
    print(f'{item} has used its {lifecycle.ROUND_CAP} correction rounds: the instruction goes to '
          f'its adjudication as an operator ruling (job {run["job"]}, {run["branch"]})'
          + (f'; card History: {filed}' if filed else '; filed on the card\'s History'))
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
