"""asf.workers.pushlog — one push per correction round, counted.

Every push a session's own ``git push`` makes, once the product's ``pre-push`` hook passed it, is
one line (the pushed local sha) in ``state/<product>/gates/<job>.pushes`` — written by the
session's hook shim (:mod:`asf.workers.githooks`, ``ASF_PUSH_LOG``), cleared at each launch
(:func:`clear`), so the file holds one run's pushes. A push of a sha already logged (a retry after
a network blip) is not a second push.

Why a correction round pushes once: each push to a PR starts the product's CI anew, and the
product's own supersede cancels the run before it — two thirds of one product's PR runs were
cancelled that way, most after their heavy jobs had started (2026-09-29). A correction session
(:data:`ONE_PUSH_KINDS`) commits as it goes and pushes once, as its last act; the brief says so
(:data:`asf.briefs.build.ONE_PUSH_RULE`), the shim warns the session on its second push
(``ASF_ONE_PUSH``), and the health pass writes a second push into the run's ended record as a
defect (:func:`defect`) — ``pushes: N`` and ``defect: …`` on its ledger line.

The factory's own pushes (:func:`asf.workers.lifecycle.publish`, the lane's ref pushes) never
carry ``ASF_PUSH_LOG`` and are not counted: a publish is the factory's one push for the session.
"""
import os

from asf import env

#: The run kinds that answer a correction round and so push once (:func:`defect`).
ONE_PUSH_KINDS = ('correct', 'adjudicate')
#: The directory the counters sit in, under ``env.state_dir(product)`` — the stop gate's own.
PUSHES_DIR = 'gates'


def path(product, job):
    """``~/.ASF/state/<p>/gates/<job>.pushes``; ``job`` through :func:`os.path.basename`, so no
    value can walk the path outside :data:`PUSHES_DIR`."""
    return os.path.join(env.state_dir(product), PUSHES_DIR,
                        f'{os.path.basename(job or "")}.pushes')


def env_for(product, job, kind):
    """The session environment the hook shim reads: ``ASF_PUSH_LOG`` (every session) and
    ``ASF_ONE_PUSH=1`` for a kind in :data:`ONE_PUSH_KINDS`."""
    p = path(product, job)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    out = {'ASF_PUSH_LOG': p}
    if kind in ONE_PUSH_KINDS:
        out['ASF_ONE_PUSH'] = '1'
    return out


def shas(product, job):
    """The distinct shas the run pushed, in push order; ``[]`` for no file. Never raises."""
    try:
        with open(path(product, job), encoding='utf-8') as f:
            lines = [l.strip() for l in f]
    except OSError:
        return []
    out = []
    for sha in lines:
        if sha and sha not in out:
            out.append(sha)
    return out


def count(product, job):
    return len(shas(product, job))


def clear(product, job):
    """Remove ``job``'s log — at launch, so each run counts its own pushes. Never raises."""
    try:
        os.remove(path(product, job))
    except OSError:
        pass


def defect_text(kind, n):
    """The defect line for a run of ``kind`` that pushed ``n`` times, or ``''``."""
    if kind not in ONE_PUSH_KINDS or n <= 1:
        return ''
    return (f'{n} pushes in one correction round — each push started CI again and cancelled '
            f'the run before it; commit as you go and push once, at the end')


def defect(product, run):
    """``(n, text)`` for an ended run: its push count, and the defect line when a correction
    round pushed more than once (``''`` otherwise)."""
    n = count(product, (run or {}).get('job'))
    return n, defect_text((run or {}).get('kind'), n)
