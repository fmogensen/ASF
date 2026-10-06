"""The keys of ``~/.ASF/config.yaml`` this release reads — the registry behind ``asf doctor``'s
``config keys`` row, the way :data:`asf.conventions.KNOWN_FLAGS` is for ``conventions.flags``.

A key the operator sets that no code reads does nothing and says nothing (``worker_pool.caps.
local_max_inflight`` sat in a live config for days while ``capacity.total.sessions`` was the real
limit). :func:`unknown_keys` names each one so doctor can warn. Entries are dotted paths: ``a.b``
is a key, ``a.*`` is a free-form map (any key below it is fine), ``a[]`` is a list of maps whose
keys follow as ``a[].k``. The cutover script's keys are listed too.

:data:`TYPED` adds a kind and a range to a key: :func:`value` reads it (the code's own default when
it is unset or malformed), and :func:`problems` names each malformed one for doctor's ``config
keys`` row — so a tunable's default is the value the code always had, and a typo or a negative
timeout is said, never acted on. ``tests/test_config_keys.py`` fails when code reads a key (chained reads, any depth)
missing here, and when a registered key no code mentions.
"""
import os

KNOWN_CONFIG_KEYS = frozenset({
    'default_product', 'schema_version', 'legacy_paths',
    'connectors.*',             # asf.connectors: connectors.<kind> per kind
    'scheduler.kind', 'scheduler.provider', 'scheduler.label_prefix', 'scheduler.legacy_labels',
    'scheduler.legacy_cron', 'scheduler.launchd_label', 'scheduler.occupancy',
    'scheduler.systemd_unit_dir',
    'scheduler.interval_s',     # no longer read; doctor's scheduler row says so
    'worker_pool.backend', 'worker_pool.binary', 'worker_pool.models.*',
    'worker_pool.quota_command', 'worker_pool.settings_file', 'worker_pool.permission_mode',
    'worker_pool.env', 'worker_pool.env_passthrough', 'worker_pool.fake_script',
    'worker_pool.session_match', 'worker_pool.sessions_command', 'worker_pool.sessions',
    'worker_pool.id_range_prefixes', 'worker_pool.id_range_size', 'worker_pool.id_range_start',
    'worker_pool.launch_concurrency', 'worker_pool.worktree_buffer',
    'worker_pool.progress_sampler', 'worker_pool.reserve_for_s1', 'worker_pool.quota_guard.*',
    'worker_pool.caps.cloud_max_inflight', 'worker_pool.auth_error_patterns',
    'worker_pool.accounts[].name', 'worker_pool.accounts[].role', 'worker_pool.accounts[].cap',
    'worker_pool.accounts[].caps', 'worker_pool.accounts[].home',
    'worker_pool.accounts[].config_dir', 'worker_pool.accounts[].home_seed',
    'worker_pool.accounts[].isolate_home', 'worker_pool.accounts[].auth_env',
    'worker_pool.accounts[].note',   # annotation for the operator; nothing reads it
    'operator.tick_file', 'operator.plugin_dir', 'operator.path_prefix', 'cutover.job_timeout_s', 'legacy_steps',
    'quota_guards.seven_d_cooldown',   # old name of quota_guards.cooldown.seven_d; still honoured
    'quota_guards.five_h', 'quota_guards.seven_d', 'quota_guards.seven_d_model',
    'quota_guards.stale_after_min', 'quota_guards.stop.*', 'quota_guards.cooldown.*',
    'quota_guards.running_allowance', 'quota_guards.five_h_usd',
    'quota_guards.reclaim_cux_lock',   # old name of account_lock.reclaim; still honoured
    'account_lock.path', 'account_lock.process', 'account_lock.refresh_command',
    'account_lock.reclaim',
    'host_guards.load_per_core', 'host_guards.swap_pct',
    'capacity.total.sessions', 'capacity.total.ci', 'capacity.per_product.sessions',
    'capacity.per_product.ci', 'capacity.reserve_for_s1.*',
    'feeder.capacity',          # moved to capacity.*; doctor's capacity row says so
    'tick.step_timeout_s', 'tick.budget_s', 'tick.wave_first', 'tick.every_n.*',
    'tick.deferred_max_age_s',
    'network.probe', 'network.recover', 'network.watchdog', 'network.hosts',
    'workers.heartbeat_min', 'workers.heartbeat_missed', 'workers.heartbeat_grace_min',
    'workers.heartbeat_resumes',
    'ci_heartbeat.targets.*', 'ci_heartbeat.stale_min', 'ci_heartbeat.seen_file',
    'upgrade.min_interval_min', 'upgrade.drain_wait_s',
    'upgrade.drain_marker_max_s', 'credentials.*',
    'tune.enabled', 'tune.window_days', 'tune.min_samples', 'tune.trial_samples', 'tune.min_gain',
    'tune.max_regress', 'tune.bounds.*', 'tune.products.*', 'tune.required',
    'factory.git_identity',
    # the cloud lane (asf.workers.cloud): each key by name, so a typo (`poll_mins`) is named
    'cloud.enabled', 'cloud.runtime', 'cloud.max_inflight', 'cloud.rows', 'cloud.accounts',
    'cloud.timeout_min', 'cloud.launch_wait_s', 'cloud.runs_on', 'cloud.token_secret',
    'cloud.workflow', 'cloud.default', 'cloud.local_only', 'cloud.mode',
    'cloud.fallback_failures', 'cloud.fallback_window_min', 'cloud.fallback_cooldown_min',
    'cloud.environment_id', 'cloud.model', 'cloud.allowed_tools', 'cloud.helper_model',
    'cloud.poll_min', 'cloud.max_creates_per_tick', 'cloud.heartbeat_min',
    'cloud.heartbeat_missed', 'cloud.heartbeat_grace_min', 'cloud.heartbeat_resumes',
    'cloud.lost_after_min', 'cloud.helper_timeout_s',
}) | frozenset()   # TYPED's keys are added below

#: Kinds :data:`TYPED` uses: what a value must be.
INT, NUM, STR, LIST, MAP, BOOL = 'int', 'number', 'str', 'list', 'map', 'bool'

#: ``{dotted key: (kind, lowest, highest)}`` — ``None`` is no bound. The default is the code's
#: own (the constant beside each read): :func:`value` falls back to it.
TYPED = {
    # the item lifecycle's caps (asf.workers.lifecycle, asf.workers.relaunch)
    'harvest.round_cap': (INT, 1, None),
    'worker_pool.caps.relaunch': (INT, 1, None),
    'worker_pool.caps.empty': (INT, 1, None),
    'worker_pool.caps.incomplete': (INT, 1, None),
    'worker_pool.caps.same_head_loop': (INT, 1, None),
    'worker_pool.caps.hook_refusal': (INT, 1, None),
    # the quota cost model (asf.workers.headroom)
    'quota_guards.default_cost': (MAP, None, None),
    'quota_guards.model_families': (LIST, None, None),
    'quota_guards.min_runs': (INT, 1, None),
    # the worker session's environment and its worktree setup
    'worker_pool.env': (MAP, None, None),
    'worker_pool.worktree_setup_timeout_s': (NUM, 1, None),
    # subprocess timeouts and list sizes (asf.gitops, asf.github, asf.workers.landing)
    'git.timeout_s': (NUM, 1, None),
    'git.fetch_timeout_s': (NUM, 1, None),
    'github.json_timeout_s': (NUM, 1, None),
    'github.log_timeout_s': (NUM, 1, None),
    'github.cmd_timeout_s': (NUM, 1, None),
    'github.pr_list_limit': (INT, 1, None),
    # the tick's locks and its hung-tick watchdog (asf.tick.tick, asf.tick.watchdog)
    'tick.daily_lock_wait_s': (NUM, 0, None),
    'tick.record_lock_wait_s': (NUM, 0, None),
    'tick.lock_poll_s': (NUM, 1, None),
    'tick.budget_s': (NUM, 0, None),
    'tick.step_timeout_s': (NUM, 1, None),
    'tick.deferred_max_age_s': (NUM, 1, None),
    'tick.wave_first': (BOOL, None, None),
    'tick.watchdog.factor': (NUM, 1, None),
    'tick.watchdog.floor_s': (NUM, 60, None),
    'tick.watchdog.default_s': (NUM, 60, None),
    # the CI queue's timings (asf.ci_queue)
    'ci.stuck_retry_s': (NUM, 1, None),
    'ci.stale_s': (NUM, 1, None),
    'ci.pickup_s': (NUM, 1, None),
    'ci.urgent_hold_s': (NUM, 0, None),
    'ci.expect_ttl_s': (NUM, 1, None),
    'ci.phantom.gap_s': (NUM, 1, None),
    'ci.phantom.red_s': (NUM, 1, None),
    'ci.stall.gap_s': (NUM, 1, None),
    'ci.stall.rerun_max': (INT, 0, None),
    # the flaky-test pass (asf.tick.flaky)
    'flaky.title_prefix': (STR, None, None),
    # slice 2: the rest of the audit's tunables
    'merge.timeout_s': (NUM, 1, None),
    'merge.fetch_timeout_s': (NUM, 1, None),
    'ci.measure.window_days': (NUM, 1, None),
    'ci.measure.min_readings': (INT, 1, None),
    'ci.measure.min_green_rate': (NUM, 0, 1),
    'ci.job_timeout_min': (NUM, 1, None),
    'trunk_red.seen_hours': (NUM, 1, None),
    'trunk_red.read_every_s': (NUM, 1, None),
    'trunk_red.find_wait_s': (NUM, 1, None),
    'trunk_red.max_tries': (INT, 1, None),
    'trunk_red.list_every_s': (NUM, 1, None),
    'bugs.ci_red_window_h': (NUM, 1, None),
    'bugs.check_failure_runs': (INT, 1, None),
    'bugs.ci_red_title': (STR, None, None),
    'flaky.s1_runs': (INT, 1, None),
    'flaky.window_days': (NUM, 1, None),
    'flaky.lookback_days': (NUM, 1, None),
    'flaky.max_runs': (INT, 1, None),
    'worker_pool.model_fallback': (MAP, None, None),
    'worker_pool.orphan_grace_s': (NUM, 0, None),
    'worker_pool.stop_grace_s': (NUM, 0, None),
    'worker_pool.progress.sample_every_s': (NUM, 1, None),
    'worker_pool.progress.window_min': (NUM, 1, None),
    'worker_pool.progress.keep_days': (NUM, 1, None),
    'worker_pool.progress.watch_max_h': (NUM, 1, None),
    'worker_pool.seats.claim_ttl_s': (NUM, 1, None),
    'worker_pool.seats.lock_wait_s': (NUM, 0, None),
    'upgrade.repo_url': (STR, None, None),
    'upgrade.pending_ttl_s': (NUM, 1, None),
    'stale_ref.wait_s': (NUM, 1, None),
    'stale_ref.keep_s': (NUM, 1, None),
    'github.rate_limit.latch_s': (NUM, 1, None),
    'github.rate_limit.memo_s': (NUM, 0, None),
    'trunk_watch.days': (NUM, 1, None),
    'trunk_watch.ruleset_read_s': (NUM, 1, None),
    'harvest.transplant.cap': (INT, 0, None),
    'harvest.transplant.regen_timeout_s': (NUM, 1, None),
    'tick.dry_run_min_free_bytes': (INT, 0, None),
    'brief.reservations_max': (INT, 1, None),
    'brief.answers_max': (INT, 1, None),
    'brief.judged_commits_max': (INT, 1, None),
    'groom.near_duplicate_overlap': (NUM, 0, 1),
    'groom.templated_overlap': (NUM, 0, 1),
    'scheduler.min_every_s': (NUM, 1, None),
    'scheduler.queue_every_s': (NUM, 1, None),
    'changelog.notes_max_commits': (INT, 1, None),
    'changelog.branch': (STR, None, None),
    # registered before this table: their kind now checked too
    'factory.git_identity': (STR, None, None),
    'upgrade.min_interval_min': (NUM, 0, None),
    'upgrade.drain_wait_s': (NUM, 0, None),
    'host_guards.load_per_core': (NUM, 0, None),
    'host_guards.swap_pct': (NUM, 0, 100),
    'capacity.total.sessions': (INT, 0, None),
    'capacity.total.ci': (INT, 0, None),
    'capacity.per_product.sessions': (INT, 0, None),
    'capacity.per_product.ci': (INT, 0, None),
    'worker_pool.env_passthrough': (LIST, None, None),
    'worker_pool.launch_concurrency': (INT, 1, None),
    'worker_pool.worktree_buffer': (INT, 0, None),
    'cutover.job_timeout_s': (NUM, 1, None),
    'ci_heartbeat.stale_min': (NUM, 1, None),
    'cloud.enabled': (BOOL, None, None),
    'cloud.max_inflight': (INT, 0, None),
    'cloud.timeout_min': (NUM, 1, None),
    'cloud.launch_wait_s': (NUM, 0, None),
    'cloud.fallback_failures': (INT, 1, None),
    'cloud.fallback_window_min': (NUM, 0, None),
    'cloud.fallback_cooldown_min': (NUM, 0, None),
    'cloud.poll_min': (NUM, 1, None),
    'cloud.max_creates_per_tick': (INT, 0, None),
    'cloud.lost_after_min': (NUM, 1, None),
    'cloud.helper_timeout_s': (NUM, 1, None),
}
KNOWN_CONFIG_KEYS = KNOWN_CONFIG_KEYS | frozenset(TYPED)


def _known(path, registry):
    """Is the dotted ``path`` (list items written ``[]``) a registered key, or below one?"""
    if path in registry:
        return True
    parts = path.split('.')
    for i in range(1, len(parts)):
        if '.'.join(parts[:i]) + '.*' in registry:
            return True
    return False


def _walk(node, prefix, registry, out):
    if isinstance(node, dict):
        for key, value in node.items():
            path = f'{prefix}.{key}' if prefix else str(key)
            if _known(path, registry):
                continue
            if any(k.startswith(path + '.') or k.startswith(path + '[]') for k in registry):
                _walk(value, path, registry, out)    # a section: look inside
            else:
                out.append(path)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, dict):
                _walk(item, prefix + '[]', registry, out)


def unknown_keys(cfg, registry=KNOWN_CONFIG_KEYS):
    """The dotted keys of ``cfg`` (the loaded operator config) that no code reads, sorted. List
    items are ``a[].k``; a key a registered section does not list is named whole (``a.k``)."""
    out = []
    _walk(cfg or {}, '', registry, out)
    return sorted(set(out))


# ---- typed keys -------------------------------------------------------------

_MISSING = object()


def lookup(cfg, key):
    """The value at dotted ``key`` in ``cfg``, or :data:`_MISSING` when any part is absent."""
    node = cfg
    for part in key.split('.'):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def check(key, v, typed=None):
    """``None`` when ``v`` fits ``key``'s kind and range (:data:`TYPED`), else why not."""
    spec = (typed or TYPED).get(key)
    if spec is None or v is None:
        return None
    kind, lo, hi = spec
    if kind == BOOL:
        return None if isinstance(v, bool) else f'must be true or false, not {v!r}'
    if kind in (INT, NUM):
        ok = (not isinstance(v, bool) and isinstance(v, (int, float))
              and (kind == NUM or float(v) == int(v)))
        if not ok:
            return f"must be {'a whole number' if kind == INT else 'a number'}, not {v!r}"
        if lo is not None and v < lo:
            return f'must be {lo} or more, not {v!r}'
        if hi is not None and v > hi:
            return f'must be {hi} or less, not {v!r}'
        return None
    if kind == STR:
        return None if isinstance(v, str) else f'must be text, not {v!r}'
    if kind == LIST:
        return None if isinstance(v, list) else f'must be a list, not {v!r}'
    if kind == MAP:
        return None if isinstance(v, dict) else f'must be a map, not {v!r}'
    return None


def problems(cfg, typed=None):
    """``[(dotted key, problem)]`` for each set key of ``cfg`` whose value breaks its kind or
    range (:data:`TYPED`), sorted; [] when every one fits."""
    out = []
    for key in sorted(typed or TYPED):
        v = lookup(cfg or {}, key)
        if v is _MISSING:
            continue
        why = check(key, v, typed)
        if why:
            out.append((key, why))
    return out


_CACHE = {}


def operator_config():
    """``~/.ASF/config.yaml`` as loaded, re-read when the file changes (by mtime and size);
    ``{}`` when it is absent or unreadable — a tunable then keeps its default."""
    from asf import env  # local: env imports modules that read tunables
    path = env.config_path()
    try:
        st = os.stat(path)
        stamp = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    if _CACHE.get('stamp') != stamp:
        try:
            cfg = env.load_file(path)
        except Exception:   # noqa: BLE001 — a broken file is the config check's to name
            cfg = {}
        _CACHE.clear()
        _CACHE.update(stamp=stamp, cfg=cfg if isinstance(cfg, dict) else {})
    return _CACHE['cfg']


def value(key, default, cfg=None):
    """The value of the tunable ``key`` (registered in :data:`TYPED`): ``cfg``'s (the operator
    config when None), else ``default`` — also when it is set but malformed (doctor's ``config
    keys`` row names it). A whole-number kind comes back an int."""
    if key not in TYPED:
        raise KeyError(f'{key} is not a registered tunable (asf.config_keys.TYPED)')
    v = lookup(operator_config() if cfg is None else cfg, key)
    if v is _MISSING or v is None or check(key, v) is not None:
        return default
    if TYPED[key][0] == INT:
        return int(v)
    return v
