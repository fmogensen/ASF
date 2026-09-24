"""asf.evidence.review — the one review reader (the contract; W1 implements it, W2 calls it).

A review is a file at ``conventions.review_pattern`` (``{reviews_dir}``, ``{n}`` the round,
``{slug}`` substituted) on a branch or on the trunk. The same reader answers for a spec, a plan
and code — spec/plan approval in ``asf.evidence``, T3/T4/T5 in :mod:`asf.harvest.lane` — and
replaces ``harvest.review_verdict``/``review_round``/``review_is_current``,
``evidence.rx_review``/``newest_review``/``verdict_of`` and ``pr_hygiene.parse_verdict``/
``branch_verdict``.

A review is *current* for a head when the head it names (its ``head:`` line) is that head; a
review of an older head is history, not a verdict.
"""

APPROVED = 'approved'
CHANGES = 'changes'
#: What :func:`verdict_of` may return, besides None (no verdict line).
VERDICTS = (APPROVED, CHANGES)


def newest(product, branch, item):
    """The newest review of ``item`` visible on ``branch`` (the trunk when ``branch`` is the
    trunk), read through ``conventions.review_pattern``: ``(round, verdict, head)`` — ``round``
    the highest ``{n}``, ``verdict`` from :func:`verdict_of`, ``head`` the sha the review names
    (None when it names none) — or None when there is no review file."""
    raise NotImplementedError('review.newest: W1')


def verdict_of(text):
    """A review's verdict from its text: :data:`APPROVED` for ``verdict: approved``,
    :data:`CHANGES` for ``verdict: changes…`` (``changes requested`` and the like), None when the
    text carries no verdict line. Case-insensitive; the first verdict line wins."""
    raise NotImplementedError('review.verdict_of: W1')
