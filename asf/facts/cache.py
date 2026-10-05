"""asf.facts.cache — the facts one pass has read, kept for that pass and keyed by head.

A tick asks the same thing of the host from several deciders (is PR #n open, what merged it);
each asking again doubles the token's load, and two answers read seconds apart can disagree. The
cache holds one answer per ``(product, fact, key, head)`` for one pass: a value read against
another head is a miss, never a stale hit. :func:`clear` starts a pass — the tick's
:class:`asf.tick.tick.Context`, the detached harvest and the ci-queue pass each call it at their
entry.

The host's open PR list is the one gh fact read here (:func:`prime`): at most **one**
``gh pr list`` per pass per product, its answer — a list, or :class:`~asf.facts.types.Unknown` —
kept for the pass whether it worked or not, so a refused read is not retried within it. A shadow
decider reads it with :func:`open_prs`, which never calls the host: a miss is Unknown (S-M13 —
shadow must not add gh load).
"""
from asf import connectors, gh_limit
from asf.facts.types import AsOf, OpenPrs, Unknown

#: What :func:`get` returns for a key the pass has not read (``None`` is a legitimate value).
MISS = object()
OPEN_PRS = 'open_prs'
#: What the one open-PR read asks for: the head (and its sha) and the title that names an item.
FIELDS = ('number', 'headRefName', 'headRefOid', 'title')

_cache = {}


def _name(product):
    return getattr(product, 'name', None) or str(product or '')


def get(product, fact, key, head=''):
    """The value read this pass for ``(fact, key)`` at ``head``, else :data:`MISS`."""
    return _cache.get((_name(product), fact, str(key), head or ''), MISS)


def put(product, fact, key, head, value):
    """Keep ``value`` for ``(fact, key)`` at ``head`` until :func:`clear`; returns it."""
    _cache[(_name(product), fact, str(key), head or '')] = value
    return value


def clear():
    """Forget every fact: a new pass reads afresh."""
    _cache.clear()


def prime(product, *, run=None):
    """The product's open PRs, read from the host at most once this pass: :class:`OpenPrs` or
    :class:`Unknown` (no slug, a refused or timed-out read, a rate limit — the latch then keeps
    every later call off the host). The answer is kept either way."""
    got = get(product, OPEN_PRS, '')
    if got is not MISS:
        return got
    slug = getattr(product, 'repo_slug', None)
    if not slug:
        return put(product, OPEN_PRS, '', '', Unknown('no repo_slug', AsOf.now()))
    try:
        r = connectors.forge().open_prs(slug, fields=FIELDS, run=run)
    except gh_limit.RateLimited:
        return put(product, OPEN_PRS, '', '', Unknown('rate limited', AsOf.now()))
    at = AsOf('', github_stamp(r.as_of))
    if not r.ok:
        return put(product, OPEN_PRS, '', '', Unknown(r.reason or 'unreadable', at))
    if r.data is not None and not isinstance(r.data, list):
        return put(product, OPEN_PRS, '', '', Unknown('bad json', at))
    prs = tuple(p for p in (r.data or ()) if isinstance(p, dict))
    return put(product, OPEN_PRS, '', '', OpenPrs(prs, at))


#: The PRs of one item, every state (:func:`item_prs`): a list this long may be cut short.
ITEM_PRS, ITEM_PR_LIMIT = 'item_prs', 100
#: What :func:`item_prs` asks for: whether a PR naming the item is open, merged or closed unmerged.
ITEM_FIELDS = ('number', 'title', 'headRefName', 'state', 'mergedAt')


def item_prs(product, item, *, run=None):
    """The PRs of every state whose title or head names ``item`` as the host listed them (one
    ``gh pr list --state all --search <item>`` per item per pass, kept either way):
    :class:`OpenPrs` (its ``prs`` carry ``state``/``mergedAt``) or :class:`Unknown` — no slug, a
    refused read, a rate limit, a page that may be cut short."""
    from asf.evidence import evidence as ev
    key = str(item or '').upper()
    got = get(product, ITEM_PRS, key)
    if got is not MISS:
        return got
    slug = getattr(product, 'repo_slug', None)
    if not isinstance(slug, str) or not slug or not key:
        return put(product, ITEM_PRS, key, '', Unknown('no repo_slug' if key else 'no item',
                                                        AsOf.now()))
    try:
        r = connectors.forge().prs(slug, state='all', search=key, limit=ITEM_PR_LIMIT, fields=ITEM_FIELDS,
                       run=run)
    except gh_limit.RateLimited:
        return put(product, ITEM_PRS, key, '', Unknown('rate limited', AsOf.now()))
    at = AsOf('', github_stamp(r.as_of))
    if not r.ok:
        return put(product, ITEM_PRS, key, '', Unknown(r.reason or 'unreadable', at))
    if r.data is not None and not isinstance(r.data, list):
        return put(product, ITEM_PRS, key, '', Unknown('bad json', at))
    if len(r.data or ()) >= ITEM_PR_LIMIT:
        return put(product, ITEM_PRS, key, '',
                   Unknown(f'{ITEM_PR_LIMIT} listed, the list may be cut short', at))
    prs = tuple(p for p in (r.data or ()) if isinstance(p, dict)
                and (key in ev.naming_ids(p.get('title') or '')
                     or key in ev.branch_ids(p.get('headRefName') or '')))
    return put(product, ITEM_PRS, key, '', OpenPrs(prs, at))


def open_prs(product):
    """The open PRs :func:`prime` read this pass; a miss is :class:`Unknown` — never a call."""
    got = get(product, OPEN_PRS, '')
    return Unknown('not read this tick', AsOf.now()) if got is MISS else got


def github_stamp(stamp):
    """``asf.github``'s ``…+00:00`` stamp in the facts' ``…Z`` form."""
    stamp = stamp or ''
    return stamp[:-6] + 'Z' if stamp.endswith('+00:00') else stamp or AsOf.now().at
