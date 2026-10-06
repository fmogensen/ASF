"""``asf correct <item> --why TEXT [--from-pr N]`` — the operator's (or another session's) way
to ask for one CORRECT round on an item, with instructions.

The correction is written the way the harvest writes its own: a pending correction on the item's
newest run (kind :data:`OPERATOR`), so the feeder turns it into the item's FIX → CORRECT row and
the brief quotes the text under the usual correction head. Nothing here bypasses a rule:

* the round counts (:data:`asf.workers.lifecycle.ROUND_CAP`): at the cap the instruction is an
  operator ruling (:func:`attach_ruling`): filed on the card's ``## History`` as an
  ``adjudicate (operator)`` line, so it binds every later review (:mod:`asf.evidence.rulings`),
  and written as the correction of the run on the Task's own code branch, so the feeder gives
  it ONE code session there with the ruling as its brief — ahead of any CI wait;
* the row is a ``correct`` row, so the one-push rule (:mod:`asf.workers.pushlog`) and the
  relaunch cap (:mod:`asf.workers.relaunch`) apply to its session as to any other;
* an item parked by hand, or with a session still running, is refused — ``asf unpark`` first.

``--from-pr N`` names the commits of pull request N in the text, so the session cherry-picks the
reference fix instead of re-deriving it; that PR is a reference, never landed.
"""
import datetime
import os
import re
import subprocess

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


def cmd_correct(args, fetch=None, alive=None):
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
    open_runs = [r for r in lifecycle.item_runs(path, item) if lifecycle.is_live(r)]
    if open_runs:
        if alive is None:
            from asf.workers import health
            alive = health.alive_for(product, list(lifecycle.latest(path).values()))
        running = [r for r in open_runs if lifecycle.occupies(r, alive)]
        if running:
            print(f'asf correct: {item} has a session running — correct it when it ends')
            return 1
        # the ledger has no `ended` line, but no session answers at the pid (or the cloud run
        # is over): health has not reaped it yet, and a stale entry never blocks a correction
        print(f'asf correct: {item}: ignoring stale run(s) '
              f'{", ".join(str(r.get("job")) for r in open_runs)} — no live session behind '
              f'them (health will end them)')
    rounds = lifecycle.rounds_of(path, item)
    run = open_lane_run(product, runs) or max(runs, key=lambda r: r.get('started') or '')
    if rounds >= lifecycle.round_cap():
        if getattr(args, 'from_pr', None):
            print(f'asf correct: {item} has used its {lifecycle.round_cap()} correction rounds — '
                  f'at the cap the instruction is an operator ruling; give it without --from-pr')
            return 1
        return attach_ruling(product, item, ruling_run(product, item, runs), why)
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
    print(f'corrected {item} on {run["branch"]} (round {rounds + 1} of {lifecycle.round_cap()}, '
          f'job {run["job"]}): {why}')
    return 0


#: The head an operator ruling's correction text opens with: the correct brief quotes it.
RULING_HEAD = ('OPERATOR RULING (asf correct, {at}) — binding. The operator has ruled on this item '
               'at the correction-round cap; it is filed on the card\'s History as '
               '`adjudicate (operator)`. Carry it out in code on {branch} — this is no '
               'adjudication and no reshape: make the change the ruling names, run the pre-push '
               'check, push once, and give the ruling as your report\'s `ruling:`:\n\n')

#: Branch kinds that are never a Task's own code branch: a ruling never lands on them.
DOC_BRANCH_KINDS = ('spec', 'plan')


def own_branch(product, item):
    """The branch ``item``'s code is cut on when no run of it names one: ``fix/`` for a Bug,
    the code prefix otherwise."""
    conv = product.conventions
    return conv.branch('fix' if item.startswith('B-') else 'code', item)


def open_lane_run(product, runs):
    """The newest run of ``runs`` whose lane is still open (a lane record in no terminal state:
    its PR open, sent back, waiting on CI), or None. A run on a code branch wins over one on a
    spec, plan or replan branch; failing that, the open document lane is the item's live lane and
    the correction goes there — a product's F-0003 (round G #27): the Feature's open lane was its
    spec PR branch, and the ruling forked a fresh code branch named after the Feature instead."""
    from asf.harvest.lane import OPEN_STATES
    conv = product.conventions
    live = [r for r in runs if r.get('branch')
            and lifecycle.lane_of(r).get('state') in OPEN_STATES]
    if not live:
        return None
    code = [r for r in live if r.get('kind') != 'adjudicate'
            and conv.branch_kind(r.get('branch') or '') not in DOC_BRANCH_KINDS]
    return max(code or live, key=lambda r: r.get('started') or '')


def ruling_run(product, item, runs):
    """The run an operator ruling is written on: the item's open lane first
    (:func:`open_lane_run`) — a spec or plan PR still open is the item's live branch, and no fresh
    code branch is forked beside it. With no open lane: the newest run on the item's own code
    branch — never an adjudicate run, never a spec or plan branch (a product's T-0614: the newest
    run was the item's adjudication on a plan branch, and the ruling, which asked for code, was
    carried out there as a reshape). When no run is on a code branch, the newest run carries it
    with ``branch`` naming the item's own (:func:`own_branch`)."""
    live = open_lane_run(product, runs)
    if live is not None:
        return live
    conv = product.conventions
    code = [r for r in runs if r.get('kind') != 'adjudicate'
            and conv.branch_kind(r.get('branch') or '') not in DOC_BRANCH_KINDS]
    if code:
        return max(code, key=lambda r: r.get('started') or '')
    newest = max(runs, key=lambda r: r.get('started') or '')
    return dict(newest, branch=own_branch(product, item), _foreign=True)


def file_ruling(product, item, text, stamp):
    """Appends ``- <stamp> adjudicate (operator): <text>`` to ``item``'s card ``## History``
    in the product's record and publishes it (commit and push, when the record is a checkout).
    ``''`` when filed, else why not."""
    line = f'- {stamp} adjudicate ({OPERATOR}): {" ".join(text.split())}'
    return file_history(product, item, line, f'record: {item} operator ruling (asf correct)')


def file_history(product, item, line, message, step='correct'):
    """Appends ``line`` to ``item``'s card ``## History`` through the record's stage and
    publishes it with ``message``. ``''`` when filed, else why not."""
    from asf.evidence import rulings
    from asf.record import frontmatter, publish, stage
    from asf.record.ingest import append_history_lines
    root = getattr(product, 'backlog_dir', None)
    card = rulings._card_file(product, item)
    if not root or not card or not os.path.isfile(card):
        return f'no card for {item} in the record'
    rel = os.path.relpath(card, root)

    def _write(_root, path, relpath):
        with open(path, encoding='utf-8') as f:
            meta, body = frontmatter.parse(f.read(), path=relpath)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(frontmatter.render(meta, append_history_lines(body, [line])))

    _r, _staged, findings = stage.guarded(root, step, _write, (card, rel), product=product,
                                          only=[rel])
    if findings:
        return 'refused: ' + '; '.join(f'{f.invariant}: {f.message}' for f in findings)
    try:
        pushed = publish.publish(root, card, message)
    except (subprocess.CalledProcessError, publish.ConcurrentCommit, OSError) as e:
        # never a traceback with the card edited and nothing committed: say what is left
        return f'filed in {rel}, but the record commit failed ({str(e)[:160]}) — it is uncommitted'
    if not pushed:
        return 'filed, but the record push was refused'
    return ''


def attach_ruling(product, item, run, why):
    """At the round cap: ``why`` becomes an operator ruling — filed on the card's History
    (:func:`file_ruling`) and written as ``run``'s correction (``operator_ruling``), which the
    feeder turns into ONE code session on the Task's own branch (a FIX → CORRECT row, the ruling
    as its brief) — never an adjudication — and which turns a landing wait (CI included) BACK
    on the next tick (:func:`asf.harvest.lane.correction_turns_back`). It is not marked
    ``at_cap``: that is health's hold, which goes to adjudication. No round is spent."""
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    filed = file_ruling(product, item, why, now[:16].replace('T', ' '))
    text = RULING_HEAD.format(at=now, branch=run['branch']) + why
    corr = {'kind': OPERATOR, 'text': text, 'at': now, 'same': lifecycle.round_cap(),
            'operator_ruling': True}
    if run.get('_foreign'):
        corr['branch'] = run['branch']  # no run on a code branch: the row names the Task's own
    pool_mod.update_session(product, run['job'], correction=corr)
    print(f'{item} has used its {lifecycle.round_cap()} correction rounds: the instruction is an '
          f'operator ruling — one code session on {run["branch"]} carries it out next tick '
          f'(job {run["job"]})'
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


# ---- asf reset: void a wrong landing -------------------------------------------------------

def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def landing_claim(run):
    """``(sha, pr)`` of ``run``'s landing claim — its ``harvested`` sha (never ``superseded``),
    else its lane's merge sha — or ``('', None)``."""
    lane = run.get('lane') if isinstance(run.get('lane'), dict) else {}
    sha = str(run.get('harvested') or '')
    if not sha or sha in lifecycle.NOT_A_LANDING:
        sha = str(lane.get('sha') or '') if lane.get('state') == 'MERGED' else ''
    return (sha if re.fullmatch(r'[0-9a-f]{7,40}', sha) else ''), lane.get('pr')


def _alive(product, path, alive):
    if alive is None:
        from asf.workers import health
        alive = health.alive_for(product, list(lifecycle.latest(path).values()))
    return alive


def cmd_reset(args, alive=None):
    """``asf reset <item> --why TEXT`` voids the item's newest landing claim: a reset line
    naming its ``(pr, head)`` (:func:`asf.workers.lifecycle.note_reset`), the run's
    ``harvested`` cleared, a History line on the card. ``--undo`` takes the newest void back."""
    product = env.load_product(getattr(args, 'product', None))
    why = str(getattr(args, 'why', '') or '').strip()
    item = (args.item or '').strip().upper()
    if not why:
        print('asf reset: --why is required — it is the reason the landing is void')
        return 2
    if not ITEM_RE.fullmatch(item):
        print(f'asf reset: {args.item} is no item id')
        return 2
    path = pool_mod.sessions_path(product)
    if getattr(args, 'undo', False) is True:
        return undo_reset(product, path, item, why)
    alive = _alive(product, path, alive)
    if any(lifecycle.occupies(r, alive) for r in lifecycle.item_runs(path, item)):
        print(f'asf reset: {item} has a session running — reset it when it ends')
        return 1
    every = [r for rs in lifecycle.runs(path).values() for r in rs if r.get('item') == item]
    claims = [(r, *landing_claim(r)) for r in every]
    claims = [c for c in claims if c[1]]
    if not claims:
        print(f'asf reset: {item} has no landing claim in the ledger — nothing to void')
        return 1
    standing = [c for c in claims if not lifecycle.voided_run(path, c[0], c[1])]
    if not standing:
        print(f'asf reset: {item}: its landing {claims[-1][1][:7]} is already void')
        return 0
    run, sha, pr = max(standing, key=lambda c: c[0].get('started') or '')
    now = _now()
    reset = {'pr': pr, 'branch': run.get('branch') or '', 'head': sha, 'archive': '',
             'why': why, 'by': OPERATOR}
    if not lifecycle.note_reset(path, item, reset, now=now, alive=alive):
        print(f'asf reset: {item}: landing {sha[:7]} is already void')
        return 0
    if lifecycle.latest(path).get(run['job'], {}).get('started') == run.get('started'):
        pool_mod.update_session(product, run['job'], harvested=None,
                                landing_voided={'sha': sha, 'pr': pr, 'at': now, 'why': why,
                                                'by': OPERATOR})
    stamp = now[:16].replace('T', ' ')
    filed = file_history(product, item,
                         f'- {stamp} reset ({OPERATOR}): landing {sha[:7]}'
                         + (f' (PR #{pr})' if pr else '') + f' voided — {" ".join(why.split())}',
                         f'record: {item} landing {sha[:7]} voided (asf reset)', step='reset')
    print(f'reset {item}: landing {sha[:7]}' + (f' (PR #{pr})' if pr else '')
          + f' voided, job {run["job"]} — the item starts over: {why}'
          + (f'; card History: {filed}' if filed else ''))
    return 0


def undo_reset(product, path, item, why):
    """``asf reset --undo <item>``: the newest void of ``item`` is taken back — an unreset line
    naming its ``(pr, head)``, the run's ``harvested`` restored, a History line."""
    vs = lifecycle.voids(path, item)
    if not vs:
        print(f'asf reset --undo: {item} has no voided landing to restore')
        return 1
    v = max(vs, key=lambda v: v.get('at') or '')
    now = _now()
    lifecycle.note_unreset(path, item, v, why=why, by=OPERATOR, now=now)
    for job, rs in lifecycle.runs(path).items():
        last = rs[-1] if rs else {}
        lv = last.get('landing_voided') if isinstance(last.get('landing_voided'), dict) else {}
        if last.get('item') == item and lv.get('sha') == v.get('head'):
            pool_mod.update_session(product, job, harvested=v['head'], landing_voided=None)
    stamp = now[:16].replace('T', ' ')
    filed = file_history(product, item,
                         f'- {stamp} reset undone ({OPERATOR}): landing {str(v.get("head"))[:7]} '
                         f'restored — {" ".join(why.split())}',
                         f'record: {item} landing {str(v.get("head"))[:7]} restored (asf reset --undo)',
                         step='reset')
    print(f'reset undone {item}: landing {str(v.get("head"))[:7]} restored: {why}'
          + (f'; card History: {filed}' if filed else ''))
    return 0


def register_reset(sub):
    """``asf reset <item> --why TEXT [--undo] [--product P]``."""
    p = sub.add_parser('reset', help='record that an item\'s landing claim was wrong: void it and '
                                     'start the item over (--undo takes it back)')
    p.add_argument('item', help='the item id')
    p.add_argument('--why', required=True, help='why the landing is void (or why it is restored)')
    p.add_argument('--undo', action='store_true', help='restore the newest voided landing')
    env.add_product_arg(p)
    p.set_defaults(func=cmd_reset)
    return p
