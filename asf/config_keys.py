"""The keys of ``~/.ASF/config.yaml`` this release reads — the registry behind ``asf doctor``'s
``config keys`` row, the way :data:`asf.conventions.KNOWN_FLAGS` is for ``conventions.flags``.

A key the operator sets that no code reads does nothing and says nothing (``worker_pool.caps.
local_max_inflight`` sat in a live config for days while ``capacity.total.sessions`` was the real
limit). :func:`unknown_keys` names each one so doctor can warn. Entries are dotted paths: ``a.b``
is a key, ``a.*`` is a free-form map (any key below it is fine), ``a[]`` is a list of maps whose
keys follow as ``a[].k``. The cutover script's keys are listed too. ``tests/test_config_keys.py`` fails when code reads a key (chained reads, any depth)
missing here, and when a registered key no code mentions.
"""

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
    'cloud.*', 'upgrade.min_interval_min', 'upgrade.drain_wait_s',
    'upgrade.drain_marker_max_s', 'credentials.*',
    'tune.enabled', 'tune.window_days', 'tune.min_samples', 'tune.trial_samples', 'tune.min_gain',
    'tune.max_regress', 'tune.bounds.*', 'tune.products.*',
})


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
