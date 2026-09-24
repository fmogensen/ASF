"""``occ()``: an occupancy (:func:`asf.workers.lifecycle.occupancy`) built by hand, for feeder
tests that state the facts a row rests on without a ledger.

``busy`` — items whose work waits to land (no new session, no row of their own); ``corrections``
— as :func:`asf.workers.lifecycle.corrections`; ``unlanded`` — ``{item: {kind: why}}``, a
document's work waiting to land; ``open_branches`` — branches with work waiting (a PR open);
``live`` — items a live run holds; ``review`` / ``landing`` — the lane's REVIEW / other open
states by item."""


def occ(busy=None, corrections=None, unlanded=None, open_branches=None, live=None, review=None,
        landing=None):
    waiting = {i: 'pushed, waiting to land' for i in busy or ()}
    docs = {}
    for item, kinds in (unlanded or {}).items():
        docs[item] = dict(kinds)
        waiting.setdefault(item, next(iter(kinds.values()), 'pushed, waiting to land'))
    for item, h in (review or {}).items():
        waiting.setdefault(item, 'lane REVIEW')
    for item, h in (landing or {}).items():
        waiting.setdefault(item, f"lane {h.get('state')}")
    return {'busy': {i: 'session running' for i in live or ()},
            'waiting_landing': waiting, 'corrections': dict(corrections or {}),
            'lanes': {}, 'review': dict(review or {}), 'landing': dict(landing or {}),
            'branches': {b: 'pushed, PR open, waiting to land' for b in open_branches or ()},
            'docs': docs}
