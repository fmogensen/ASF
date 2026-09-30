"""asf.ci_cancels — every cancelled CI run gets exactly one cause (F-0230).

The four cancels the factory makes with its own ``gh run cancel`` — relief, step-silence stall,
duplicate-push dedupe, and the post-merge cancel — write a durable claim the moment they are made
(:func:`asf.ci_queue.claim_cancel`, landed under #482): the factory's own cancels are claimed by
the code that made them and never inferred. Every other cancel is the host's, and is told apart by
the pair of heads either side of it — a content-free re-push (a rebase, a reword, a sign-off) named
``rewrite``, a real push named ``newhead``, and a head the clone cannot resolve named
``unresolved`` rather than guessed.

The ledger this module reads is the one the factory already writes: ``asf.ci_queue`` owns
``CANCELS_FILE``, ``claim_cancel`` and ``load_claims``; this module claims nothing of its own and
mints no second ledger. ``rewrite`` is the number F-0203 — already approved and planned — drives
to zero.
"""
import json
import os
import re
import subprocess
import datetime

from asf import env

#: every cause a cancelled run can have, in the order they are tried: the factory's own claims
#: first (it knows), then the run's own limit, then the two inferences, then the head pair, then
#: the honest nothing. First match wins; a run has exactly one.
CAUSES = ('relief', 'stall', 'dedupe', 'merged', 'timeout', 'rewrite', 'newhead',
          'unresolved', 'unclaimed')

#: what the operator does about each. `wasted` is the one this card exists to drive to zero.
GROUPS = {
    'traded':  ('relief', 'stall'),        # thrown away on purpose, to buy the trunk its runners
    'saved':   ('dedupe', 'merged'),       # the run was moot; the cancel kept the minutes
    'wasted':  ('rewrite',),               # a content-free re-push cut it short — nobody chose it
    'sound':   ('newhead', 'timeout'),     # the head is gone, or the job hit its own limit
    'unknown': ('unresolved', 'unclaimed'),
}

#: the landed cause strings that are the factory's own word about a cancel it made (F-0203 PD2),
#: plus the host's own annotation for a job that hit its declared limit — stronger evidence than
#: any supersede rule, so it is honoured as a claim rather than re-read from the head pair.
HONOURED = {'relief': 'relief', 'stall': 'stall', 'duplicate-push': 'dedupe',
            'merged-pr': 'merged', 'job-timeout': 'timeout'}

#: the landed cause strings that are `explain_cancels`' own readings of the evidence, not a claim
#: of authorship, and are re-classified here from the head pair instead. `superseded` in
#: particular is never honoured: it asks only whether a later run exists, never what that run's
#: head carried, which is the weak reading this card exists to replace.
REREAD = frozenset({'superseded', 'cancelled-left', 'orphan-rerun', 'orphan-refused'})

#: the footer goes RED at or above this share of cancelled runner-minutes spent on `rewrite`.
WASTED_ALERT_PCT = 20


def claims(state_dir):
    """The factory's own claims for cancels it made, ``{run id: claim}`` — a thin read of
    :func:`asf.ci_queue.load_claims`, dropping any value that is not a dict or carries no
    ``cause``. Never raises: ``load_claims`` already returns ``{}`` for a missing, unreadable or
    non-dict file, and this adds no second failure mode. Written by
    :func:`asf.ci_queue.claim_cancel`; a claim older than ``ci_queue.CLAIM_TTL_S`` (two days) is
    pruned on the next write, so a window wider than that reads empty here."""
    from asf import ci_queue
    got = ci_queue.load_claims(state_dir)
    return {k: v for k, v in got.items() if isinstance(v, dict) and v.get('cause')}
