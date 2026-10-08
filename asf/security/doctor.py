"""asf.security.doctor — the ``asf doctor`` ``security`` row (:mod:`asf.doctor`): the record's
own rule card for R-0009, and the age of the last read of the host's own alert feeds. A product
that configures nothing still gets the row (P21) — an unadopted check or an unread feed is loud
rather than silent (D14).

Reads the record and the feed cache only (:mod:`asf.security.alerts`'s own cache file); it never
spends a host round trip of its own, and no line here carries an alert value or a machine
address (D11, P20).
"""
import datetime
import os

from asf.rules.rules import RulesError, load_rules
from asf.security import alerts

#: The core check's script basename a product's own rule card must name in ``check:`` (D8).
CORE_CHECK_BASENAME = 'r0009.sh'


def _adopted(product):
    """True when the product's record carries a rule card whose ``check:`` basename is
    :data:`CORE_CHECK_BASENAME` — the check is on the clock (P15/D14)."""
    root = product.backlog_dir
    if not root:
        return False
    try:
        entries = load_rules(root)
    except RulesError:
        return False
    return any(os.path.basename(e.get('check') or '') == CORE_CHECK_BASENAME for e in entries)


def _feed_age_h(product):
    """The cached feed read's age in hours, or ``None`` when the cache has never been written.
    Reads the cache file only — no call of its own."""
    cache = alerts._read_cache(alerts.cache_path(product))
    if cache is None:
        return None
    return alerts._age_h(cache.get('ts'), datetime.datetime.now(datetime.timezone.utc))


def doctor_findings(product):
    """``[(ok, detail)]`` — always exactly one finding: the record's adoption of R-0009 when it
    is missing, else the age of the last feed read when it is stale or absent, else one green
    line naming both. No alert value or machine address is ever in ``detail`` (D11, P20)."""
    if not _adopted(product):
        return [(False, f'no rule card in the record carries check: {CORE_CHECK_BASENAME}'
                         ' — R-0009 is not adopted')]
    max_age_h = product.conventions.security_alerts()['max_age_h']
    age_h = _feed_age_h(product)
    if age_h is None:
        return [(False, 'the alert feeds have never been read')]
    if age_h > max_age_h:
        return [(False, f'the alert feeds were last read {int(age_h)}h ago'
                         f' (over {int(max_age_h)}h)')]
    return [(True, f'alerts {int(age_h)}h · R-0009 adopted')]
