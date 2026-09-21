"""asf.tick.stale — items over their configured stage limit (``asf stale``).

The stage limits (README's "Stale" rule) come from the product yaml's `stage_limits` (durations
like `3d`, `24h`, `45m`), each key falling back to `DEFAULT_LIMITS`, and are checked against
`stage_since`, the one clock ingest already keeps for every item. `card_undecided`/`undecided_close` read `decided`; a
Feature's spec/plan/build limits read `stage`; `task_active` and the two Bug severities read
`state`.
"""
import datetime
import json
import re
import sys

from asf import env
from asf.record import frontmatter
from asf.record.core import canonicalize, load_items

DURATION_RE = re.compile(r'^(\d+)([smhd])$')
_DURATION_SECS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}

CLOSED_LIKE = {'Resolved', 'Closed'}


# The limits every product gets unless its `stage_limits` says otherwise.
DEFAULT_LIMITS = {
    'card_undecided': '3d',
    'spec-draft': '24h',
    'spec-review': '12h',
    'plan-draft': '24h',
    'plan-review': '12h',
    'plan-approved': '24h',
    'task_active': '45m',
    'pr_approved_unbatched': '10m',
    'bug_S1': '10m',
    'bug_S2': '24h',
    'undecided_close': '14d',
}


def load_limits(product=None):
    """DEFAULT_LIMITS overlaid with the product's `stage_limits`. A value that is not a duration
    (a key another step reads, like a plain number) is left out."""
    limits = dict(DEFAULT_LIMITS)
    given = (product.stage_limits if product is not None else None) or {}
    for key, value in given.items():
        if isinstance(value, str) and DURATION_RE.match(value.strip()):
            limits[key] = value.strip()
    return limits


def product_of(args):
    """The product `--product`, `$ASF_PRODUCT` or the config default names; None when there is
    no config at all, which leaves the defaults in force."""
    try:
        return env.load_product(getattr(args, 'product', None))
    except env.ConfigError:
        return None


def limit_seconds(limit):
    m = DURATION_RE.match(str(limit))
    if not m:
        raise ValueError(f"bad duration {limit!r}")
    return int(m.group(1)) * _DURATION_SECS[m.group(2)]


def format_age(seconds):
    seconds = int(seconds)
    if seconds >= 86400:
        return f"{seconds // 86400}d"
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    return f"{max(seconds // 60, 0)}m"


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.datetime.strptime(s, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def stage_limit_key(stage):
    if stage is None:
        return None
    if stage == 'spec-draft' or stage == 'plan-draft' or stage == 'plan-approved':
        return stage
    if stage.startswith('spec-review'):
        return 'spec-review'
    if stage.startswith('plan-review'):
        return 'plan-review'
    return None


def find_stale(canonical, limits, now):
    """[(id, label, age_seconds, limit_key, limit)] over every item past its limit."""
    out = []

    def consider(iid, label, age, key):
        limit = limits.get(key)
        if limit is None:
            return
        if age > limit_seconds(limit):
            out.append((iid, label, age, key, limit))

    for iid, rec in canonical.items():
        typed, machine = frontmatter.split_machine(rec['meta'])
        if typed.get('removed'):
            continue
        type_ = typed.get('type')
        since = parse_iso(machine.get('stage_since'))
        if since is None:
            continue
        age = (now - since).total_seconds()
        state = machine.get('state', 'New')
        stage = machine.get('stage')
        label = stage or state

        if type_ not in ('decision', 'rule') and typed.get('decided') is not True:
            consider(iid, label, age, 'card_undecided')
            consider(iid, label, age, 'undecided_close')

        if type_ == 'feature':
            key = stage_limit_key(stage)
            if key:
                consider(iid, label, age, key)

        if type_ == 'task' and state == 'Active':
            consider(iid, label, age, 'task_active')

        if type_ == 'bug' and state not in CLOSED_LIKE:
            sev = typed.get('severity')
            if sev == 'S1':
                consider(iid, label, age, 'bug_S1')
            elif sev == 'S2':
                consider(iid, label, age, 'bug_S2')

    out.sort(key=lambda r: r[0])
    return out


def cmd_stale(args, root):
    by_id, parse_errors = load_items(root)
    if parse_errors:
        for f, line, why in parse_errors:
            print(f"{f}:{line}: {why}", file=sys.stderr)
        return 1
    canonical, _dupes = canonicalize(by_id)
    limits = load_limits(product_of(args))
    now = datetime.datetime.now(datetime.timezone.utc)
    rows = find_stale(canonical, limits, now)

    if args.json:
        payload = [{'id': iid, 'label': label, 'age_s': int(age), 'limit': limit, 'rule': key}
                   for iid, label, age, key, limit in rows]
        print(json.dumps(payload, indent=2))
        return 0

    for iid, label, age, _key, limit in rows:
        print(f"{iid} {label} {format_age(age)} > {limit}")
    return 0
