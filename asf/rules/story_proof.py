"""asf.rules.story_proof — F-0040's backstop: every landed Task ticks the acceptance line its
tests prove. The landing's own refusal (:func:`asf.harvest.lane.lane_refusal`) catches a Task
before it lands; this check catches what the landing deliberately does not refuse — a missing
record (D4), a Task that lists no Story (D5) — and what it cannot see: a landing that slipped
through a merge queue before the gate existed, or a Story's acceptance list rewritten after the
Task that claimed it landed.

Read by ``rules/story-proof.sh`` through ``asf proves check`` — the rule card itself is minted
by hand (D9), never by an id written here.
"""
import json
import sys

from asf import env, gitops, proves
from asf.record import core as record_core
from asf.views import index_reader


def _since(task):
    """The date part of ``task``'s ``updated``, or today when it carries none."""
    updated = (task or {}).get('updated') or ''
    return updated[:10] if len(updated) >= 10 else record_core.today()


def violations(items, ev, root):
    """One violation line per landed Task whose Stories are not proven, in the
    ``<what> <where> <since>`` shape ``asf/rules/rules.py`` expects — the rule id left to the
    runner to prefix, so this never carries one. A Task is *landed* when
    ``ev['ids'][<T-id>]['commit']`` is set (P11); a Task with no landing evidence at all is not
    reported here — that is the landing's own proof, still to come. Three shapes:

    * ``task lists no story`` — the Task covers no Story at all (:func:`asf.proves.stories_of_task`:
      neither ``stories:`` nor a Story ``parent:``) — D5's non-refusal, reported here instead.
    * ``task landed proving nothing`` — the Task covers one or more Stories and none of
      ``ev['proves']``'s claims names it.
    * ``claim names a line … has no more`` — one of the Task's landed claims names a line past
      the end of its Story's current acceptance list (the list was rewritten after the Task
      landed).

    Sorted, so two runs over one record print the same lines in the same order."""
    ids_ev = (ev or {}).get('ids') or {}
    proven = (ev or {}).get('proves') or {}
    out = []
    for iid, task in sorted((items or {}).items()):
        if task.get('type') != 'task' or not (ids_ev.get(iid) or {}).get('commit'):
            continue
        since = _since(task)
        stories = proves.stories_of_task(task, items)
        if not stories:
            out.append(f'task lists no story {iid} since {since}')
            continue
        task_claims = {sid: [c for c in proven.get(sid) or [] if c.get('task') == iid]
                       for sid in stories}
        if not any(task_claims.values()):
            out.append(f'task landed proving nothing {iid} ({", ".join(stories)}) since {since}')
            continue
        for sid, claims in task_claims.items():
            bullets = proves.card_bullets(root, (items or {}).get(sid) or {})
            for claim in claims:
                if not (1 <= claim['line'] <= len(bullets)):
                    out.append(f'claim names a line {sid} has no more {sid} line '
                              f'{claim["line"]} ({iid}) since {since}')
    return sorted(out)


def _git(repo, *args):
    return gitops.git(list(args), repo).stdout


def _product(args):
    name = getattr(args, 'product', None)
    if not name:
        try:
            name = env.default_product_name()
        except env.ConfigError:
            return None
    return env.load_product(name)


def cmd_check(args, root):
    from asf.evidence import evidence
    product = _product(args)
    ev = evidence.load(product=product) if product else {}
    items, _generated = index_reader.load(root)
    lines = violations(items, ev, root)
    if args.json:
        print(json.dumps({'violations': lines}, indent=2, sort_keys=True))
    else:
        for line in lines:
            print(line)
    return 1 if lines else 0


def _branch_for(product, task_id, branch):
    """``branch`` when given; else the product's own code branch for ``task_id`` when it is on
    origin; else None — the caller falls back to ``origin/<main>..HEAD`` of the checkout in
    hand, which is what a session mid-run has before it ever pushes."""
    if branch:
        return branch
    guess = product.conventions.prefix('code') + task_id
    return guess if _git(product.repo_dir, 'rev-parse', '--verify', '-q',
                        f'origin/{guess}').strip() else None


def cmd_show(args, root):
    items, _generated = index_reader.load(root)
    task = items.get(args.item) or {}
    product = _product(args)
    repo = product.repo_dir if product else None
    if not repo:
        print(f'{args.item}: no product repo in hand — pass --product')
        return 2
    branch = _branch_for(product, args.item, args.branch)
    if branch:
        claims = proves.claims_on_branch(lambda *a: _git(repo, *a), product.main, branch)
        tree = _git(repo, 'ls-tree', '-r', '--name-only', f'origin/{branch}').split()
    else:
        claims = proves.parse(_git(repo, 'log', '--format=%B', f'origin/{product.main}..HEAD'))
        tree = _git(repo, 'ls-tree', '-r', '--name-only', 'HEAD').split()
    good, problems = proves.validate(claims, task, items, root, tree)
    for problem in problems:
        print(problem)
    for claim in good:
        print(f'{claim.story} line {claim.line} — {claim.test}')
    return 1 if problems else 0


def cmd_proves(args, root):
    if args.proves_command == 'show':
        if not args.item:
            print('proves show: a Task id is required', file=sys.stderr)
            return 2
        return cmd_show(args, root)
    return cmd_check(args, root)
