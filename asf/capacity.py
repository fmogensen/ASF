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
      batch: {per_run: 8, parallel: 2, runners: 4}

This is the one module that reads those keys — no caller reads them raw.

**The session law.** :func:`resolve` hands back a *ceiling*, not a free count:
``asf.feeder.tiers.free_slots`` subtracts this product's own in-flight sessions itself.
``sessions = max(0, min(product_sessions(product, cfg), total_sessions(cfg) -
inflight_sessions_elsewhere(product.name)))``, the second term only when the operator set a
total; ``sessions_bound`` names whichever term won.

**The CI law.** Symmetrical, and skipped entirely when neither ``capacity.ci`` nor
``capacity.total.ci`` is configured — no ``gh`` call. An unreadable or unconfigured count is
``None`` (unknown), and unknown never lowers a ceiling nor blocks a caller.
"""
import dataclasses
import subprocess

from asf import env

DEFAULT_SESSIONS = 4
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
    """A product's CI ceiling: its own file, else the operator default, else ``(None, None)`` —
    no built-in default (unlike sessions, an unconfigured CI budget means no ceiling at all)."""
    v = _product_capacity(product).get('ci')
    if isinstance(v, int) and v >= 0:
        return v, 'product'
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


def resolve(product, cfg=None, ci_source=None):
    """The one call every caller makes: a :class:`Resolved` snapshot of this product's ceilings,
    what is in flight, and what the S1 reserve holds. ``ci_source`` is what a test injects; left
    unset, it is chosen the way :func:`ci_source` (the module function) chooses it."""
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
        ci_inflight=ci_inflight, batch=batch_shape(product, cfg), reserve=reserve(cfg))
