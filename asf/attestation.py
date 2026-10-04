"""A trunk sha's attestation, read: the merge queue's commit status that every required check
passed on that exact sha's batch run.

The merge queue sets :data:`CONTEXT` (``asf/attested``, renamed by ``conventions.ci
.attest_status``) = ``success`` on the batch sha before the trunk moves to it
(:func:`asf.merge_queue.attest`). A product's CI then skips its heavy jobs on the trunk push of
that sha: they were judged already, on the very same sha. So every reader of the trunk's required
checks — the deploy's pick (:mod:`asf.harvest.deploy`), the lane's ``trunk_red`` and
``pr_checks``, the CI queue's trunk relief — counts a required job that concluded ``skipped`` on
an attested sha as green. Only ``skipped``: a required job that ran there and failed (the gate,
the rules job) is red as before, and one still queued or running is still pending — attestation
covers the heavy jobs the trunk run skipped, never a real verdict of that run.

Only a ``success`` status counts: ``pending``, ``failure``, ``error`` or no status at all is not
an attestation. Unreadable is **Unknown** — :func:`attested` returns ``None``, never ``False``
(S-M23): ``False`` would read as "skipped, and nobody attested it", and a reader would then judge
an attested sha by its skipped heavy jobs — or walk past it to an older red. Each reader maps
``None`` to "hold, read again": the deploy picks nothing this tick, the lane's ``trunk_red`` gives
the check no verdict on that sha, the CI queue's trunk relief cancels nothing, ``pr_checks``
neither passes nor reds the skipped check. ``None`` is falsy, so a caller that only asks "is it
attested?" still never counts an unreadable sha as attested.
"""
from asf import github

#: the commit status context a landed batch sha carries (``conventions.ci.attest_status``'s
#: default); :data:`asf.merge_queue.ATTEST_CONTEXT` is this one
CONTEXT = 'asf/attested'
#: the conclusion a job the attestation stands in for ends with on the trunk push
ATTESTED_CONCLUSIONS = frozenset({'skipped'})

#: ``{(slug, sha, context)}`` read as attested this process: a success status is final
_SEEN = set()


def context(product):
    """``conventions.ci.attest_status`` of ``product``, else :data:`CONTEXT`."""
    conv = getattr(product, 'conventions', None)
    get = getattr(conv, 'attest_status', None)
    if callable(get):
        value = get()
    else:
        ci = conv.get('ci') if isinstance(conv, dict) else None
        value = ci.get('attest_status') if isinstance(ci, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else CONTEXT


def state_of(combined, context_name=CONTEXT):
    """The state of ``context_name`` in a combined status (``commits/<sha>/status``: one entry
    per context, the newest), or None when it carries none."""
    statuses = combined.get('statuses') if isinstance(combined, dict) else None
    for s in statuses if isinstance(statuses, list) else ():
        if isinstance(s, dict) and s.get('context') == context_name:
            return s.get('state')
    return None


def _read(path):
    """The parsed JSON of ``gh api <path>`` through :mod:`asf.github`, or None when Unknown."""
    r = github.api(path)
    return r.data if r.ok else None


def attested(slug, sha, context_name=CONTEXT, read=None):
    """True when ``sha`` carries ``context_name`` = ``success``, False when it does not (another
    state, or no such status), ``None`` when the status could not be read — Unknown, never
    False. ``read(path)`` returns the parsed JSON of a ``gh api`` path (None when unreadable);
    the default reads through :func:`asf.github.api`. Never raises."""
    if not slug or not sha or not context_name:
        return False
    key = (slug, sha, context_name)
    if key in _SEEN:
        return True
    try:
        got = (read or _read)(f'repos/{slug}/commits/{sha}/status?per_page=100')
    except Exception:  # noqa: BLE001 — a read that raised (a rate limit included): Unknown
        return None
    if not isinstance(got, dict):
        return None     # unreadable, or not a combined status: Unknown
    ok = state_of(got, context_name) == 'success'
    if ok:
        _SEEN.add(key)
    return ok


def product_attested(product, sha, read=None):
    """:func:`attested` for ``product`` (its ``repo_slug`` and :func:`context`): True, False or
    ``None`` (Unknown)."""
    return attested(getattr(product, 'repo_slug', None), sha, context(product), read)
