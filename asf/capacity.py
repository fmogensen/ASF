"""asf.capacity — the resolver: how many sessions and CI runs a product may hold at once.

Two blocks feed it. ``~/.ASF/config.yaml``'s ``capacity:`` carries the operator's totals and
per-product defaults::

    capacity:
      total: {sessions: 6, ci: 4}             # across every product under ~/.ASF/products/
      per_product: {sessions: 2, ci: 1}       # the default for a product whose file declares none
      reserve_for_s1: {local: 1, cloud: 1}    # the lane reserve (moved from worker_pool)

``~/.ASF/products/<name>.yaml``'s ``capacity:`` carries that product's own ceilings and CI batch
shape::

    capacity:
      sessions: 3
      ci: 2
      weight: 3          # this product's share of the pool against the others' (default 1)
      batch: {per_run: 8, parallel: 2, runners: 4}

This is the one module that reads those keys — no caller reads them raw.

**The session law.** :func:`resolve` hands back a *ceiling*, not a free count:
``asf.feeder.tiers.free_slots`` subtracts this product's own in-flight sessions itself.
``sessions = max(0, min(product_sessions(product, cfg), total_sessions(cfg) -
inflight_sessions_elsewhere(product.name)))``, the second term only when the operator set a
total; ``sessions_bound`` names whichever term won.

**The CI law.** Symmetrical, and skipped entirely when neither ``capacity.ci``, a declared
``ci.pool`` nor ``capacity.total.ci`` is configured — no ``gh`` call. A declared pool sets the
product's CI ceiling to the sum of its runners' slots; an explicit ``capacity.ci`` overrides it. An unreadable or unconfigured count is
``None`` (unknown), and unknown never lowers a ceiling nor blocks a caller.

**The fair share.** Every product draws on one worker pool, so a product's ceiling is also
bounded by its share of what the pool can take *now*: ``usable`` is the sum over accounts of
``0`` at stop, ``1`` in cooldown and ``cap`` when free (:mod:`asf.workers.quota`), and the share
is ``ceil(usable / active)`` — ``active`` counting the products whose wave is asf's and on a
clock (:func:`wave_active`), this one always among them. With one active product, or no pool
accounts configured, there is no share. A product over its share keeps its running sessions; the
feeder only stops handing it new slots (``free_slots`` never goes below zero).

**Borrowing.** The share is work-conserving: what another active product leaves idle is lent
to this one. Each wave records its product's *demand* (:func:`write_demand`: its sessions in
flight and the launching rows it would start given room) in ``state/<name>/demand.json``; a
partner's *claim* is ``min(its share, its in-flight now + its wanted rows)``, and the rest of its
share is borrowable. A partner with no fresh record (older than :data:`DEMAND_FRESH_S`, or none)
claims its whole share — nothing is lent on a guess. A partner that wakes records its demand on
its next wave, its claim rises, and the borrower's share falls back: the borrower keeps its
running sessions and gets no new slot until the lender has its own.
"""
import dataclasses
import datetime
import json
import math
import os
import subprocess

from asf import env

DEFAULT_SESSIONS = 4
DEMAND_FRESH_S = 30 * 60   # a partner's demand record older than this lends nothing
DEMAND_FILE = 'demand.json'
DEFAULT_RESERVE = {'local': 1, 'cloud': 1}
CI_TIMEOUT_S = 30


@dataclasses.dataclass
class Resolved:
    sessions: int
    sessions_bound: str
    ci: object          # int | None
    ci_bound: object     # str | None
    ci_inflight: object  # int | None
    batch: dict
    reserve: dict
    ceiling: object = None      # int: the configured/total-bounded ceiling before the share
    fair_share: object = None   # int | None: this product's share of the usable pool
    usable: object = None       # int | None: the slots the pool can take now
    active: int = 1             # products with an active wave, this one included
    borrowed: int = 0           # of fair_share: the slots lent by idle partners

    @property
    def fair_share_reason(self):
        """The wave's reason for a row the share holds back, ``''`` when no share applies."""
        if self.fair_share is None:
            return ''
        lent = f', {self.borrowed} borrowed from idle products' if self.borrowed else ''
        return (f'fair share: {self.fair_share} of {self.usable} usable slots across '
                f'{self.active} products{lent}')


def _product_capacity(product):
    cap = product._get('capacity', {})
    return cap if isinstance(cap, dict) else {}


def _operator_capacity(cfg):
    cap = (cfg or {}).get('capacity')
    return cap if isinstance(cap, dict) else {}


def product_sessions(product, cfg):
    """A product's session ceiling: its own file, else the operator default, else the deprecated
    ``feeder.capacity``, else :data:`DEFAULT_SESSIONS`. ``(int, source)``."""
    v = _product_capacity(product).get('sessions')
    if isinstance(v, int) and v >= 0:
        return v, 'product'
    v = (_operator_capacity(cfg).get('per_product') or {}).get('sessions')
    if isinstance(v, int) and v >= 0:
        return v, 'operator default'
    v = ((cfg or {}).get('feeder') or {}).get('capacity')
    if isinstance(v, int) and v >= 0:
        return v, 'feeder.capacity'
    return DEFAULT_SESSIONS, 'default'


def product_ci(product, cfg):
    """A product's CI ceiling: its own ``capacity.ci``, else the slots of its declared runner
    pool (``ci.pool``, :func:`asf.ci_pool.pool_ci` — the source names each role's slots), else
    the operator default, else ``(None, None)`` — no built-in default (unlike sessions, an
    unconfigured CI budget means no ceiling at all). An explicit ``capacity.ci`` beside a pool
    wins, and its source says which pool figure it overrides."""
    from asf import ci_pool
    slots, pool_source = ci_pool.pool_ci(product)
    v = _product_capacity(product).get('ci')
    if isinstance(v, int) and v >= 0:
        return v, ('product' if slots is None else f'product (overrides ci.pool {slots})')
    if slots is not None:
        return slots, pool_source
    v = (_operator_capacity(cfg).get('per_product') or {}).get('ci')
    if isinstance(v, int) and v >= 0:
        return v, 'operator default'
    return None, None


def total_sessions(cfg):
    v = (_operator_capacity(cfg).get('total') or {}).get('sessions')
    return v if isinstance(v, int) and v >= 0 else None


def total_ci(cfg):
    v = (_operator_capacity(cfg).get('total') or {}).get('ci')
    return v if isinstance(v, int) and v >= 0 else None


def inflight_sessions(name):
    """Live rows in ``state/<name>/sessions.jsonl`` — :func:`asf.workers.lifecycle.inflight`."""
    from asf.workers import lifecycle
    from asf.workers import pool as pool_mod
    return len(lifecycle.inflight(pool_mod.sessions_path(name)))


def inflight_sessions_elsewhere(name):
    """The sum of :func:`inflight_sessions` for every OTHER product under
    ``~/.ASF/products/`` (P7: the enumeration :func:`asf.schema._all_products` uses)."""
    from asf import schema
    return sum(inflight_sessions(other) for other in schema._all_products() if other != name)


def wave_active(product):
    """True when ``product``'s wave is asf's own and some clock in its ``clocks:`` runs it —
    the product competes for the shared pool. A command-owned or ``off`` wave does not."""
    from asf.tick import steps as tick_steps
    (_step, owner, _cmd), = tick_steps.resolve(product, ['wave'])
    if owner != 'asf':
        return False
    for entry in (product._get('clocks') or {}).values():
        if not isinstance(entry, dict):
            continue
        steps = entry.get('steps')
        if 'wave' in (steps if isinstance(steps, list) else [steps]):
            return True
    return False


def active_products(name):
    """The names of the products with an active wave, ``name`` always among them. A product file
    that does not load is not counted."""
    from asf import schema
    out = {name}
    for other in schema._all_products():
        if other == name:
            continue
        try:
            if wave_active(env.load_product(other)):
                out.add(other)
        except Exception:  # noqa: BLE001 — a broken sibling file must not stop this product
            continue
    return sorted(out)


def usable_slots(cfg, quota_source=None):
    """The slots the pool can take now: per account ``0`` at stop, ``1`` in cooldown, ``cap``
    when free. ``None`` when no pool account is configured."""
    from asf.workers import pool as pool_mod
    from asf.workers import quota as quota_mod
    accounts = pool_mod.accounts_from_config(cfg)
    if not accounts:
        return None
    source = quota_source or quota_mod.source_from_config(cfg)
    guards = quota_mod.guards_from_config(cfg)
    total = 0
    for a in accounts:
        state, _why = quota_mod.band(source.read(a), guards)
        total += a.cap if state == quota_mod.FREE else 1 if state == quota_mod.COOLDOWN else 0
    return total


def product_weight(product):
    """The product's ``capacity.weight`` (an int >= 1), else 1: its share of the pool against the
    other active products' weights — the operator's product-over-factory lever."""
    v = _product_capacity(product).get('weight')
    return v if isinstance(v, int) and v >= 1 else 1


def _weight_of(name, product):
    if name == product.name:
        return product_weight(product)
    try:
        return product_weight(env.load_product(name))
    except Exception:  # noqa: BLE001 — a broken sibling file weighs the default
        return 1


def _demand_path(name):
    return os.path.join(env.state_dir(name), DEMAND_FILE)


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def write_demand(name, inflight, wanted):
    """Record ``name``'s demand on the pool: ``inflight`` sessions and ``wanted`` launching rows
    it would start given room. Written by every wave; read by the partners' :func:`fair_share`.
    A write that fails is ignored — a missing record lends nothing."""
    rec = {'at': _now().strftime('%Y-%m-%dT%H:%M:%SZ'), 'inflight': int(inflight),
           'wanted': int(wanted)}
    path = _demand_path(name)
    try:
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(rec, f, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass


def read_demand(name, now=None):
    """``name``'s ``wanted`` rows from its fresh demand record, else ``None`` (none, unreadable,
    or older than :data:`DEMAND_FRESH_S`)."""
    try:
        with open(_demand_path(name), encoding='utf-8') as f:
            rec = json.load(f)
        at = datetime.datetime.strptime(rec['at'], '%Y-%m-%dT%H:%M:%SZ').replace(
            tzinfo=datetime.timezone.utc)
        wanted = int(rec['wanted'])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if ((now or _now()) - at).total_seconds() > DEMAND_FRESH_S:
        return None
    return max(0, wanted)


def claim(name, share):
    """The slots of its ``share`` a partner holds or wants now: ``min(share, in flight + wanted)``,
    its whole share when its demand is unknown."""
    wanted = read_demand(name)
    if wanted is None:
        return share
    return min(share, inflight_sessions(name) + wanted)


def fair_share(product, cfg, quota_source=None):
    """``(share, usable, active, borrowed)``, or ``None`` when fewer than two products are active
    or the pool has no accounts. The base share is ``ceil(usable × weight / Σ weights)`` over the
    active products (every weight 1 unless a product file sets ``capacity.weight``); ``borrowed``
    is what the partners' shares hold beyond their :func:`claim`, added to it, and the whole never
    exceeds ``usable``."""
    active = active_products(product.name)
    if len(active) < 2:
        return None
    usable = usable_slots(cfg, quota_source)
    if usable is None:
        return None
    total = sum(_weight_of(n, product) for n in active)
    share = math.ceil(usable * product_weight(product) / total)
    idle = 0
    for other in active:
        if other == product.name:
            continue
        theirs = math.ceil(usable * _weight_of(other, product) / total)
        idle += max(0, theirs - claim(other, theirs))
    borrowed = max(0, min(share + idle, usable) - share)
    return share + borrowed, usable, len(active), borrowed


def batch_shape(product, cfg):
    """The product's CI batch shape — only the keys of §2.1 whose value is an ``int >= 0``."""
    batch = _product_capacity(product).get('batch')
    if not isinstance(batch, dict):
        return {}
    return {k: v for k, v in batch.items()
            if k in ('per_run', 'parallel', 'runners') and isinstance(v, int) and v >= 0}


def reserve(cfg):
    """The S1 lane reserve: ``capacity.reserve_for_s1`` wins, else the deprecated
    ``worker_pool.reserve_for_s1``, else :data:`DEFAULT_RESERVE` — merged per lane exactly as
    ``asf.workers.pool.reserve_from_config`` did before this module took over its body."""
    cfg = cfg or {}
    out = dict(DEFAULT_RESERVE)
    r = _operator_capacity(cfg).get('reserve_for_s1')
    if not isinstance(r, dict):
        r = (cfg.get('worker_pool') or {}).get('reserve_for_s1')
    if isinstance(r, dict):
        out.update({k: int(v) for k, v in r.items()})
    return out


def deprecations(cfg):
    """One line per old key still in use (D3), the text of §4.2."""
    cfg = cfg or {}
    out = []
    if isinstance((cfg.get('feeder') or {}).get('capacity'), int):
        out.append('feeder.capacity is set — move it to capacity.per_product.sessions')
    if isinstance((cfg.get('worker_pool') or {}).get('reserve_for_s1'), dict):
        out.append('worker_pool.reserve_for_s1 is set — move it to capacity.reserve_for_s1')
    return out


def env_overlay(resolved, product):
    """The ``ASF_CAPACITY_*`` map for a command step's environment (D10) — only the keys that
    resolved, so a script's own default still wins where this module has no opinion."""
    out = {'ASF_PRODUCT': product.name, 'ASF_CAPACITY_SESSIONS': str(resolved.sessions)}
    if resolved.ci is not None:
        out['ASF_CAPACITY_CI'] = str(resolved.ci)
    batch = resolved.batch or {}
    for key, env_key in (('per_run', 'ASF_CAPACITY_BATCH_PER_RUN'),
                         ('parallel', 'ASF_CAPACITY_BATCH_PARALLEL'),
                         ('runners', 'ASF_CAPACITY_RUNNERS')):
        if key in batch:
            out[env_key] = str(batch[key])
    return out


class CiRuns:
    """Counts a product's non-completed GitHub Actions runs via ``gh run list``. Never raises
    (D8): a non-zero exit, a timeout, an ``OSError`` or unparsable output all read as unknown."""

    def __init__(self, timeout=CI_TIMEOUT_S):
        self.timeout = timeout

    def read(self, product):
        ci = product.ci if isinstance(product.ci, dict) else {}
        workflow = ci.get('workflow')
        repo_slug = product.repo_slug
        if not workflow or not repo_slug:
            return None
        try:
            p = subprocess.run(
                ['gh', 'run', 'list', '-R', repo_slug, '--workflow', workflow, '--limit', '50',
                 '--json', 'status', '--jq', '[.[] | select(.status != "completed")] | length'],
                capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if p.returncode != 0:
            return None
        out = p.stdout.strip()
        return int(out) if out.isdigit() else None


class NoCiRuns:
    """No CI configured for this product: nothing to count, and no ``gh`` call ever made."""

    def read(self, product):
        return None


def _pick_ci_source(product, cfg):
    ci = product.ci
    return CiRuns() if isinstance(ci, dict) and ci else NoCiRuns()


def ci_source(product, cfg):
    """``NoCiRuns()`` when the product's ``ci`` is ``none`` or unset, else ``CiRuns()`` — the
    shape :func:`asf.workers.quota.source_from_config` already uses."""
    return _pick_ci_source(product, cfg)


def resolve(product, cfg=None, ci_source=None, quota_source=None):
    """The one call every caller makes: a :class:`Resolved` snapshot of this product's ceilings,
    what is in flight, and what the S1 reserve holds. ``ci_source`` is what a test injects; left
    unset, it is chosen the way :func:`ci_source` (the module function) chooses it; likewise
    ``quota_source`` for the fair share's band reads."""
    cfg = env.load_config() if cfg is None else cfg

    ceiling, sess_source = product_sessions(product, cfg)
    total = total_sessions(cfg)
    if total is not None:
        headroom = total - inflight_sessions_elsewhere(product.name)
        sessions = max(0, min(ceiling, headroom))
        sessions_bound = 'operator total' if headroom < ceiling else sess_source
    else:
        sessions = max(0, ceiling)
        sessions_bound = sess_source
    bounded = sessions
    share = fair_share(product, cfg, quota_source)
    if share is not None and share[0] < sessions:
        sessions, sessions_bound = share[0], 'fair share'

    p_ci, p_ci_source = product_ci(product, cfg)
    t_ci = total_ci(cfg)
    if p_ci is None and t_ci is None:
        ci, ci_bound, ci_inflight = None, None, None
    else:
        terms = [(v, s) for v, s in ((p_ci, p_ci_source), (t_ci, 'operator total')) if v is not None]
        ci, ci_bound = min(terms, key=lambda t: t[0])
        source = ci_source if ci_source is not None else _pick_ci_source(product, cfg)
        ci_inflight = source.read(product)

    return Resolved(
        sessions=sessions, sessions_bound=sessions_bound, ci=ci, ci_bound=ci_bound,
        ci_inflight=ci_inflight, batch=batch_shape(product, cfg), reserve=reserve(cfg),
        ceiling=bounded,
        fair_share=share[0] if share is not None and sessions_bound == 'fair share' else None,
        usable=share[1] if share is not None else None,
        active=share[2] if share is not None else 1,
        borrowed=share[3] if share is not None and sessions_bound == 'fair share' else 0)
