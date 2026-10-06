"""asf.state.registry — every state file the factory keeps, with its owner, kind, schema and TTL.

A state file is a name under ``<ASF_HOME>/state/<product>/`` (or, for a ``shared`` name, under
``<ASF_HOME>/state/`` itself: one file every product on the host reads). The registry is the one
list of them: :mod:`asf.state.store` refuses a name it does not hold, and the reaper
(:func:`asf.state.store.reap`) calls a file nobody registered an orphan.

``Spec`` fields:

* ``owner``  — the module that writes the file (the one place its shape is defined);
* ``kind``   — ``json`` (one document), ``jsonl`` (one record a line), ``stamp`` (a marker whose
  mtime is its content), ``lock`` (an ``flock`` target), ``text`` (anything else);
* ``ttl_days`` — the age past which the reaper removes the file; ``None`` keeps it for ever;
* ``schema`` — the shape's version. The store keeps the version each file was last written with
  in the sidecar ``.schemas.json`` (never inside the file: every existing reader keeps its
  shape), so a bump here is a registry edit plus a migration where the owner reads it;
* ``shared`` — the file lives at the ``ASF_HOME`` level and every product writes it. Two code
  versions writing one file is how a seat or a quota row is lost, so the store refuses to mutate
  a shared file until every product runs the store (:func:`asf.state.store.shared_ready`).

A name is matched exactly first, then as an ``fnmatch`` pattern in the order written.
"""
import fnmatch
from collections import namedtuple

Spec = namedtuple('Spec', 'owner kind ttl_days schema shared')
Spec.__new__.__defaults__ = ('json', None, 1, False)

JSON, JSONL, STAMP, LOCK, TEXT = 'json', 'jsonl', 'stamp', 'lock', 'text'
KINDS = (JSON, JSONL, STAMP, LOCK, TEXT)

#: The sidecar holding each file's last-written schema; never a registered name of its own.
SCHEMAS = '.schemas.json'
#: Where the reaper moves what it removes: ``.trash/<YYYY-MM-DD>/``, purged after TRASH_DAYS.
TRASH = '.trash'
TRASH_DAYS = 14


def _s(owner, kind=JSON, ttl_days=None, schema=1, shared=False):
    return Spec(owner, kind, ttl_days, schema, shared)


REGISTRY = {
    # the merge queue and its requests
    'land-requests.json': _s('asf.merge_queue'),
    'merge-queue.json': _s('asf.merge_queue'),
    'merge-queue-rebuild.json': _s('asf.merge_queue'),
    # CI
    'ci-queue.json': _s('asf.ci_queue'),
    'ci-queue.lock': _s('asf.ci_queue', LOCK),
    'ci-cancels.json': _s('asf.ci_queue'),
    'ci-trials.json': _s('asf.ci_pool'),
    'ci-trials.jsonl': _s('asf.ci_pool', JSONL),
    'ci-census.json': _s('asf.ci_census'),
    'ci-places.jsonl': _s('asf.ci_census', JSONL),
    'ci-baselines.json': _s('asf.ci_measure'),
    'trunk-red.json': _s('asf.trunk_red'),
    'trunk-watch.json': _s('asf.trunk_watch'),
    'stale-ref.json': _s('asf.stale_ref'),
    'flaky.json': _s('asf.tick.flaky'),
    'flake-triage.json': _s('asf.flake'),
    # workers
    'sessions.jsonl': _s('asf.workers.pool', JSONL),
    'worktrees.json': _s('asf.workers.worktrees'),
    'spawn-failures.json': _s('asf.workers.wave'),
    'branch-retention.json': _s('asf.workers.retention'),
    'cache-prs.json': _s('asf.workers.lifecycle'),
    'id-ranges.tsv': _s('asf.workers.spawn', TEXT),
    'reservations.json': _s('asf.reservations'),
    'demand.json': _s('asf.capacity'),
    # harvest and landing
    'harvest.json': _s('asf.tick.step_harvest'),
    'harvest-items.json': _s('asf.tick.step_harvest'),
    'harvest.lock': _s('asf.harvest.harvest', LOCK),
    'gates.jsonl': _s('asf.harvest.harvest', JSONL),
    'gate-visits.json': _s('asf.harvest.lane'),
    'gate-slow.json': _s('asf.harvest.lane'),
    'ref-push-failed.json': _s('asf.harvest.lane'),
    'landing-required-checks.json': _s('asf.harvest.lane'),
    'landing-missing-since.json': _s('asf.harvest.lane'),
    'transplants.jsonl': _s('asf.harvest.transplant', JSONL),
    'deploys.json': _s('asf.harvest.deploy'),
    # the record, its checks and the scorecard
    'record-health.json': _s('asf.tick.record_health'),
    'rule-check-failures.json': _s('asf.tick.file_bugs'),
    'i9-seen.json': _s('asf.invariants'),
    'invariants-report.jsonl': _s('asf.invariants', JSONL),
    'approvals.jsonl': _s('asf.approvals', JSONL),
    'redactions.jsonl': _s('asf.redact', JSONL),
    'scorecard.jsonl': _s('asf.scorecard.loop', JSONL),
    'tune.json': _s('asf.tune'),
    'tune.jsonl': _s('asf.tune', JSONL),
    'stale-acts.jsonl': _s('asf.stale_act', JSONL),
    'scorecard-queue.jsonl': _s('asf.scorecard.loop', JSONL),
    'scorecard-causes.json': _s('asf.scorecard.loop'),
    'facts-disagree.jsonl': _s('asf.facts.disagree', JSONL),
    '*-disagree.jsonl': _s('asf.shadow', JSONL),
    'cache-evidence.json': _s('asf.evidence.sources'),
    'cache-board-prs.json': _s('asf.evidence.sources'),
    'cache-backlog-evidence.json': _s('asf.evidence.sources'),
    # the clock, installs and upgrades
    'tick.lock': _s('asf.tick.tick', LOCK),
    'tick-*.lock': _s('asf.tick.tick', LOCK),
    'daily.stamp': _s('asf.tick.steps', STAMP),
    'summary-*.stamp': _s('asf.tick.summary', STAMP, ttl_days=7),
    'paused-clocks.json': _s('asf.scheduler'),
    'network.json': _s('asf.tick.network'),
    'install.json': _s('asf.installs'),
    'upgrade-pending.json': _s('asf.upgrade'),
    'upgrade-expired.json': _s('asf.upgrade'),
    'alerts.json': _s('asf.security.alerts'),
    # leftovers of a hand or tool edit: kept a month, then reaped
    '*.lock': _s('asf.state.store', LOCK),
    '*.bak*': _s('asf.state.store', TEXT, ttl_days=30),
    '*.corrupt-*': _s('asf.state.store', TEXT, ttl_days=30),
}

#: The ``<ASF_HOME>/state/``-level names: one file per host, every product writes it.
SHARED = {
    'seats.json': _s('asf.workers.seats', shared=True),
    'seats.lock': _s('asf.workers.seats', LOCK, shared=True),
    'quota-*': _s('asf.workers.headroom', shared=True),
    'run-costs.json': _s('asf.workers.headroom', shared=True),
    'cloud-sessions.json': _s('asf.workers.cloudpid', shared=True),
    'upgrade-last.json': _s('asf.upgrade', shared=True),
}


def _match(table, name):
    spec = table.get(name)
    if spec is not None:
        return spec
    for pattern, spec in table.items():
        if any(c in pattern for c in '*?[') and fnmatch.fnmatchcase(name, pattern):
            return spec
    return None


def spec(name):
    """The :class:`Spec` for ``name`` (a shared name first: those live a level up), or ``None``
    when nobody registered it."""
    if not name or '/' in name or name in (SCHEMAS, TRASH):
        return None
    return _match(SHARED, name) or _match(REGISTRY, name)


def is_shared(name):
    s = spec(name)
    return bool(s and s.shared)
