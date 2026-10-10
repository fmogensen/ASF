"""asf.rules.story_proof — the backstop for F-0040's story-proof gate (§2.8).

The landing (:mod:`asf.harvest.lane`) refuses a Task whose claims prove nothing, but it
deliberately lets two cases through rather than block the trunk on them: a missing
``index.json`` (D4) and a Task that lists no Story at all (D5). A third case the landing cannot
see at all: a Task that landed through a merge queue before this gate existed.
:func:`violations` is the rule card's check for all three, read from the record's own index and
the evidence the ingest pass already gathers (:func:`asf.evidence.evidence.landed_proves`) —
never from a session's working tree.

``asf proves show <T-id>`` is the same shape of check a session can run on its own branch,
before the push: :func:`asf.proves.validate` over the claims found there.
"""
import json
import sys

from asf import gitops, proves
from asf.conventions import Conventions
from asf.record import core, match


def _since(item):
    """The date part of ``item``'s ``updated``, or :func:`asf.record.core.today` when it has
    none — the ``since`` every violation line ends with."""
    updated = str((item or {}).get('updated') or '').strip()
    return updated.split('T', 1)[0] if updated else core.today()


def violations(items, ev, root):
    """One line per violation, ``<what> <where> <since>`` (the shape ``asf/rules/rules.py:9``
    expects — the rule id itself left for the runner to prefix). Only a *landed* Task is read —
    ``ev['ids'][<T-id>]['commit']`` set (P11) — so a Task still in flight is never reported,
    whatever its claims:

    * ``task lists no story`` — the landing's own D5: nothing to check its claims against.
    * ``task landed proving nothing`` — a Story the Task names has no landed claim of its own
      (``ev['proves']``, keyed by Story and filtered to this Task's id here) — the backstop for
      D4, and for a landing that slipped through a merge queue before this gate existed.
    * ``claim names a line … has no more … line N`` — a landed claim whose line number is no
      longer on the Story's ``## Acceptance`` (the Story shrank since the claim landed).

    Sorted, so two runs over one record print the same lines in the same order.
    """
    items = items or {}
    ids_ev = (ev or {}).get('ids') or {}
    proves_ev = (ev or {}).get('proves') or {}
    out = []
    for iid, item in items.items():
        if (item or {}).get('type') != 'task':
            continue
        if not (ids_ev.get(iid) or {}).get('commit'):
            continue
        since = _since(item)
        stories = proves.stories_of_task(item, items)
        if not stories:
            out.append(f'task lists no story {iid} since {since}')
            continue
        for story in stories:
            entries = [e for e in (proves_ev.get(story) or []) if e.get('task') == iid]
            if not entries:
                out.append(f'task landed proving nothing {iid} ({story}) since {since}')
                continue
            bullets = proves.card_bullets(root, items.get(story))
            for entry in entries:
                line = entry.get('line')
                if not (isinstance(line, int) and 1 <= line <= len(bullets)):
                    out.append(f'claim names a line {story} has no more {story} line {line} '
                              f'({iid}) since {since}')
    out.sort()
    return out


def _product(args):
    from asf import env
    try:
        return env.load_product(getattr(args, 'product', None))
    except (env.ConfigError, OSError):
        return None


def _git_callable(repo):
    """The injected ``git`` :func:`asf.proves.claims_on_branch` reads (D11): every call goes
    through :mod:`asf.gitops`, the repo's one client for read-only git, never raw ``subprocess``."""
    def run(*argv):
        r = gitops.git(list(argv), repo)
        return r.data if r.ok else ''
    return run


def _branch_claims(repo, main, branch):
    """The claims on ``branch`` above ``main``: ``origin/<branch>`` when that ref exists on the
    product's origin, else ``origin/<main>..HEAD`` of the working tree — what a session has in
    hand mid-run, before its own branch is pushed."""
    git = _git_callable(repo)
    if gitops.rev_parse(repo, f'origin/{branch}'):
        return proves.claims_on_branch(git, main, branch)
    return proves.parse(git('log', '--format=%B', f'origin/{main}..HEAD') or '')


def _cmd_show(args, root):
    task_id = (getattr(args, 'item', None) or '').upper()
    if not task_id:
        print('proves show: a Task id is required', file=sys.stderr)
        return 2
    items = match.load_index(root)
    task = items.get(task_id)
    if not task or task.get('type') != 'task':
        print(f'{task_id}: not a Task in this record', file=sys.stderr)
        return 2
    product = _product(args)
    conv = product.conventions if product is not None else Conventions()
    repo = product.repo_dir if product is not None else None
    branch = getattr(args, 'branch', None) or conv.branch('code', task_id)
    claims = _branch_claims(repo, conv.main, branch) if repo else []
    good, problems = proves.validate(claims, task, items, root)
    if getattr(args, 'json', False):
        payload = {
            'good': [{'story': c.story, 'line': c.line, 'test': c.test} for c in good],
            'problems': problems,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if (good and not problems) else 1
    for c in good:
        print(f'proves {c.story} line {c.line} — {c.test}')
    for p in problems:
        print(p)
    return 0 if (good and not problems) else 1


def cmd_proves(args, root):
    """``asf proves check|show`` — :mod:`asf.cli`'s ``proves`` dispatch (P10).

    ``check``: :func:`violations` against the record's own ``index.json`` and the evidence pass
    (:func:`asf.evidence.evidence.load`) — nothing on stdout and exit 0 with none, one line each
    and exit 1 otherwise, the contract :func:`asf.rules.rules.run_check` reads so ``asf
    file-bugs`` can turn a line into a Bug the way every other rule already does.

    ``show``: one Task's claims and their problems, read off its own branch before the push.
    """
    if getattr(args, 'proves_command', None) == 'show':
        return _cmd_show(args, root)

    product = _product(args)
    items = match.load_index(root)
    ev = {}
    if product is not None:
        from asf.evidence import evidence
        ev = evidence.load(product=product)
    lines = violations(items, ev, root)
    if getattr(args, 'json', False):
        print(json.dumps({'violations': [{'line': l} for l in lines]}, indent=2, sort_keys=True))
        return 1 if lines else 0
    for line in lines:
        print(line)
    return 1 if lines else 0
