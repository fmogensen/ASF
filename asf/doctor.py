"""asf.doctor — one table: is this product's ASF installation sound (``asf doctor --product <p>``).

Six checks, each a row: config, product repo, backlog, scheduler job, CLI sessions (git required;
gh required when the product has a PR host; gcloud, az, aws, flyctl and vercel optional, skipped if
not installed), and the **one-factory check** — no second copy of a factory tool on PATH or inside
the product repo. The search set for that last check is never a literal path in this code: it is
``legacy_paths:`` in the operator's ``~/.ASF/config.yaml``, a list of directories that once held
the old, per-product tooling this product's ``asf`` install replaces. A product whose repo is this
package's own checkout skips the repo half: its tools are the factory, not a second copy of it.

A seventh row, **approvals**, reads the product's approval matrix (:func:`asf.approvals.check_doctor`,
F-0031 §2.5): red when the matrix does not load, otherwise ok with what is legal but probably not
meant — a class left to its default, a held class nothing can recognise, an empty amendable set.

An eighth row, **redaction-hooks** (:func:`check_redaction_hooks`, F-0075 §2.4, T-0025), confirms
the redaction gate's ``pre-commit`` and ``pre-push`` are in place in every repo the product
configures — read-only, unlike ``asf hooks install`` (:func:`asf.hooks.ensure_git_hooks`), which
writes the ones it finds missing; red names each missing or foreign one and the command that
installs it.

A ninth row, **approvals-hook** (:func:`check_approvals_hook`), is red when a worker account's
sessions would run without the built-in ``approvals`` hook — no guard on human-now actions.

A tenth row, **worker env** (:func:`check_worker_env`), is red when a worker session could see
what it should not: an account with ``isolate_home: false`` (the operator's HOME and every login
in it), a ``worker_pool.env_passthrough`` name that looks like a credential, or an isolated
account with no ``auth_env`` for the runtime's login variable (an isolated HOME finds no login).
**worker secrets** (:func:`check_worker_secrets`) lists each ``auth_env`` file's presence —
every account's and the product's own ``conventions.auth_env`` — by variable name only, red when
one is missing (its launches are refused). An eleventh,
**clock code** (:func:`check_clock_code`, informational), names the snapshot sha the clock last
ticked from when the package runs from a checkout (:mod:`asf.snapshot`).

A twelfth row, **console permissions** (:func:`asf.console_perms.check_doctor`, B-0131), is red
when the operator's own console — not a worker account's — would still hit a permission prompt on
``asf``, the installer, a ``launchctl`` pause/resume, a lane branch push or ``git worktree``
cleanup: neither the operator's user-level runtime settings nor the product repo's carry every
rule ``asf console-permissions offer`` shows.

The **scheduler** row is red while a pre-ASF job the operator config names
(``scheduler.launchd_label``, ``scheduler.legacy_cron``) is still loaded or in the crontab: two
factories would be ticking one product.

A product with no PR host — ``ci: {provider: none}`` — needs no ``repo_slug`` and no ``gh``.

Exit 1 if any required row is red; optional rows that are unavailable print ``skip``, not red.
"""
import os
import shutil
import subprocess
import time

from asf import approvals, drift, env, hooks, schema, scheduler, tokens
from asf.workers import pool

_SKIP_DIRS = {'.git', 'node_modules', '__pycache__', 'dist', 'build', '.next', 'vendor', 'venv',
              '.venv', 'target'}


def _run(cmd, timeout=10):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        detail = (p.stdout or p.stderr or '').strip().splitlines()
        return p.returncode == 0, (detail[0] if detail else '')
    except FileNotFoundError:
        return False, 'not found'
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)


def has_pr_host(product):
    """False only for ``ci: {provider: none}`` (or ``ci: none``): no hosted repo, no ``gh``."""
    ci = product.ci if isinstance(product.ci, dict) else {'provider': product.ci}
    provider = ci.get('provider')
    return provider is None or str(provider).strip().lower() != 'none'


def package_root():
    """The checkout (or install dir) this ``asf`` package runs from."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def installed_dist_names():
    """The distribution name(s) the running ``asf`` package was installed as — empty when it
    runs from a bare checkout, never installed."""
    try:
        from importlib import metadata
        return set(metadata.packages_distributions().get(__package__ or 'asf', ()))
    except Exception:  # noqa: BLE001 — no metadata is no install, not a failure
        return set()


def _project_name(repo_dir):
    """``[project] name`` of ``repo_dir``'s pyproject.toml, or None."""
    try:
        import tomllib
        with open(os.path.join(repo_dir, 'pyproject.toml'), 'rb') as f:
            return (tomllib.load(f).get('project') or {}).get('name')
    except (OSError, ValueError, ImportError):
        return None


def is_factory_repo(product):
    """True when the product's repo is the ASF package's own source: the checkout it runs from
    (real paths compared), or — since the clocks run the installed package, never the checkout —
    the repo whose pyproject names the distribution the running package was installed as."""
    repo = product.repo_dir
    if not repo:
        return False
    if os.path.realpath(repo) == os.path.realpath(package_root()):
        return True
    name = _project_name(repo)
    return bool(name) and name in installed_dist_names()


def check_config(product_name):
    """config.yaml and products/<name>.yaml both parse and carry the fields every other check needs."""
    try:
        cfg = env.load_config()
    except env.ConfigError as e:
        return False, f'config.yaml: {e}'
    try:
        product = env.load_product(product_name)
    except env.ConfigError as e:
        return False, f'products/{product_name}.yaml: {e}'
    keys = ('repo_dir', 'repo_slug', 'backlog_dir') if has_pr_host(product) else ('repo_dir', 'backlog_dir')
    missing = [k for k in keys if not getattr(product, k, None)]
    if missing:
        return False, f'products/{product.name}.yaml missing {", ".join(missing)}', cfg, product
    return True, f'config.yaml + products/{product.name}.yaml', cfg, product


def check_repo(product):
    d = product.repo_dir
    if not d or not os.path.isdir(d):
        return False, f'repo_dir not a directory: {d}'
    ok, detail = _run(['git', '-C', d, 'rev-parse', '--is-inside-work-tree'])
    return ok, (d if ok else f'{d}: {detail or "not a git repo"}')


def check_backlog(product):
    d = product.backlog_dir
    if not d or not os.path.isdir(d):
        return False, f'backlog_dir not a directory: {d}'
    ok, detail = _run(['git', '-C', d, 'rev-parse', '--is-inside-work-tree'])
    return ok, (d if ok else f'{d}: {detail or "not a git repo"}')


def _legacy_cron_entries(sched):
    """``scheduler.legacy_cron``: the fixed strings that pick out a pre-ASF crontab line (one
    string or a list), exactly as the operator wrote them — nothing product-specific in code."""
    raw = sched.get('legacy_cron') or []
    raw = [raw] if isinstance(raw, str) else list(raw)
    return [str(e).strip() for e in raw if str(e).strip()]


def check_scheduler(cfg, run=None):
    """(ok, detail) — no pre-ASF job the operator config names is still live beside ASF's own
    jobs: two factories ticking one product is the failure this row exists for. Only what config
    names is probed — ``scheduler.launchd_label`` (``launchctl list <label>``) and
    ``scheduler.legacy_cron`` (lines of ``crontab -l`` containing an entry) — and a live one is
    RED with the command that retires it, per its scheduler kind: ``launchctl bootout`` for the
    label, a ``crontab`` rewrite for the cron line. ASF's own jobs are checked under SCHEDULER."""
    from asf.scheduler import CUTOVER_TOOL
    run = run or _run
    sched = cfg.get('scheduler') or {}
    kind = sched.get('kind') or sched.get('provider') or 'launchd'
    label = sched.get('launchd_label')
    cron_entries = _legacy_cron_entries(sched)
    live, retired = [], []
    if label:
        loaded, _ = run(['launchctl', 'list', label])
        if loaded:
            live.append(f'pre-ASF launchd job {label} still loaded — retire it: '
                        f'launchctl bootout gui/$(id -u)/{label} (or {CUTOVER_TOOL})')
        else:
            retired.append(f'pre-ASF job {label} retired')
    if cron_entries:
        try:
            p = subprocess.run(['crontab', '-l'], capture_output=True, text=True, timeout=10)
            lines = p.stdout.splitlines() if p.returncode == 0 else []
        except (OSError, subprocess.TimeoutExpired):
            lines = []
        active = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith('#')]
        for entry in cron_entries:
            if any(entry in ln for ln in active):
                live.append(f'pre-ASF cron entry {entry!r} still in the crontab — retire it: '
                            f"crontab -l | grep -vF '{entry}' | crontab -")
            else:
                retired.append(f'pre-ASF cron entry {entry!r} retired')
    if live:
        return False, '; '.join(live)
    if retired:
        return True, '; '.join(retired) + '; ASF jobs are checked under SCHEDULER'
    if kind != 'launchd':
        return True, f'scheduler kind {kind!r}: no pre-ASF job declared; ASF jobs are checked under SCHEDULER'
    return True, 'no pre-ASF job declared; ASF jobs are checked under SCHEDULER'


def check_approvals_hook(cfg, product):
    """(ok, detail) — every worker account's sessions carry the built-in ``approvals`` hook (the
    guard that refuses human-now actions), in the account's own settings or the product repo's
    own project settings. Red names each account without it and the command that writes
    it. No worker accounts, or the ``fake`` runtime (no agent session at all): nothing to guard.
    Every other backend runs Claude Code sessions, so it is checked."""
    accounts = pool.accounts_from_config(cfg)
    if not accounts:
        return True, 'no worker accounts configured (nothing to guard)'
    backend = str(((cfg or {}).get('worker_pool') or {}).get('backend') or 'claude-code')
    if backend.replace('-', '_') == 'fake':
        return True, 'worker_pool.backend fake runs no agent session (nothing to guard)'
    missing = hooks.approvals_missing(accounts, product.repo_dir)
    if missing:
        names = ', '.join(a.name for a in missing)
        return False, (f'approvals hook missing for worker account(s) {names} — '
                       f'asf hooks install --product {product.name}')
    return True, f'approvals hook in {len(accounts)} worker accounts'


def _backend_is_fake(cfg):
    backend = str(((cfg or {}).get('worker_pool') or {}).get('backend') or 'claude-code')
    return backend.replace('-', '_') == 'fake'


def check_worker_env(cfg):
    """(ok, detail) — the ``worker env`` row: what a worker session inherits. Red when an account
    runs on the operator's own HOME (``isolate_home: false``: every login on the machine is the
    session's, and a ``home:`` path is then the operator's word, not the factory's), when a
    ``worker_pool.env_passthrough`` name looks like a credential
    (:func:`asf.hermetic.looks_like_credential` — a token handed to every session), or when an
    isolated account's ``auth_env`` sets none of the runtime's login variables
    (:data:`asf.workers.runtime.RUNTIME_AUTH_VARS`): its HOME holds no login and, on macOS, the
    runtime's own sits in the keychain the isolated HOME cannot find — every session fails "Not
    logged in". Ok names each account's home and any ``home_seed`` path that does not exist."""
    from asf import hermetic
    from asf.workers import runtime
    problems, notes = [], []
    fake = _backend_is_fake(cfg)
    for acct in pool.accounts_from_config(cfg):
        if not acct.isolate_home:
            problems.append(f'account {acct.name} has isolate_home: false'
                            + ('' if acct.home else " (the operator's HOME)"))
            continue
        if not fake and not any(v in acct.auth_env for v in runtime.RUNTIME_AUTH_VARS):
            var = runtime.RUNTIME_AUTH_VARS[0]
            problems.append(f'account {acct.name} is isolated but has no auth_env {var} (its '
                            f'sessions cannot log in) — run `{runtime.RUNTIME_TOKEN_COMMAND}` as '
                            f'that account, save the token to ~/.ASF/secrets/{acct.name}.token '
                            f'and set auth_env: {{{var}: ~/.ASF/secrets/{acct.name}.token}}')
        home = runtime.session_home(acct)
        missing = [p for p in acct.home_seed if not os.path.exists(p)]
        notes.append(f'{acct.name}: {home}' + (f" (home_seed missing: {', '.join(missing)})"
                                               if missing else ''))
    creds = [n for n in env.env_passthrough(cfg) if hermetic.looks_like_credential(n)]
    if creds:
        problems.append(f"env_passthrough hands a credential to every session: {', '.join(creds)}")
    if problems:
        return False, '; '.join(problems) + ' — config.yaml worker_pool'
    passthrough = ', '.join(env.env_passthrough(cfg)) or 'none'
    return True, (f'allow-list + passthrough ({passthrough}); '
                  + ('; '.join(notes) if notes else 'no worker accounts'))


def check_worker_secrets(cfg, product=None):
    """(ok, detail) — the ``worker secrets`` row: each account's ``auth_env`` files, and
    ``product``'s own ``conventions.auth_env`` files (labelled ``product:<name>:<VAR>``), by
    variable name and presence only (a value is never read into the table). Red when one is
    missing or empty: that account's, or the product's, launches are refused until it exists."""
    present, missing = [], []
    for acct in pool.accounts_from_config(cfg):
        for var, path in sorted(acct.auth_env.items()):
            try:
                ok = os.path.isfile(path) and os.path.getsize(path) > 0
            except OSError:
                ok = False
            (present if ok else missing).append(f'{acct.name}:{var}' + ('' if ok else f' ({path})'))
    for var, path in sorted(env.product_auth_env(product).items()):
        try:
            ok = os.path.isfile(path) and os.path.getsize(path) > 0
        except OSError:
            ok = False
        (present if ok else missing).append(f'product:{product.name}:{var}'
                                            + ('' if ok else f' ({path})'))
    if missing:
        return False, 'missing: ' + ', '.join(missing) + (
            f"; present: {', '.join(present)}" if present else '')
    return True, ('present: ' + ', '.join(present)) if present else 'no auth_env files configured'


def check_clock_code(product):
    """(ok, detail) — the code the product's clock runs: the snapshot sha it last ticked from
    and the checkout's HEAD (the next tick takes that), or the installed package's root."""
    info = scheduler.clock_code(product.name)
    if not info.get('snapshot'):
        return True, f"installed package {info.get('root')}"
    sha, head = info.get('sha'), info.get('head')
    if not sha:
        return True, f"snapshot of {info['root']}: no tick has run from one yet (HEAD {(head or '?')[:12]})"
    detail = f"snapshot {sha[:12]} ({format_age(_age_s_since(info.get('at')))} ago)"
    if head and head != sha:
        detail += f'; checkout HEAD {head[:12]} (the next tick takes it)'
    return True, detail


def _age_s_since(at):
    return None if at is None else max(0.0, time.time() - at)


# name -> (required, probe argv); required tools missing/failing are red, optional ones are skip
_CLI_TOOLS = [
    ('git', True, ['git', '--version']),
    ('gh', True, ['gh', 'auth', 'status']),
    ('gcloud', False, ['gcloud', 'auth', 'list']),
    ('az', False, ['az', 'account', 'show']),
    ('aws', False, ['aws', 'sts', 'get-caller-identity']),
    ('flyctl', False, ['flyctl', 'auth', 'whoami']),
    ('vercel', False, ['vercel', 'whoami']),
]


def check_cli_sessions(product=None):
    """[(name, required, ok_or_None, detail)] — ``ok`` is ``None`` for an optional tool not
    installed. ``gh`` is optional for a product with no PR host."""
    rows = []
    for name, required, argv in _CLI_TOOLS:
        if name == 'gh' and product is not None and not has_pr_host(product):
            required = False
        if not required and shutil.which(argv[0]) is None:
            rows.append((name, required, None, 'not installed'))
            continue
        ok, detail = _run(argv)
        rows.append((name, required, ok, detail))
    return rows


def _legacy_tool_names(legacy_paths):
    names, dirs = set(), []
    for lp in legacy_paths or []:
        d = os.path.expanduser(lp)
        dirs.append(os.path.normpath(d))
        if not os.path.isdir(d):
            continue
        for entry in os.listdir(d):
            full = os.path.join(d, entry)
            if os.path.isfile(full) and not entry.startswith('.'):
                names.add(entry)
    return names, dirs


def _find_in_repo(repo_dir, names, limit=3):
    hits = []
    if not repo_dir or not os.path.isdir(repo_dir):
        return hits
    for dirpath, dirnames, filenames in os.walk(repo_dir):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for f in filenames:
            if f in names:
                hits.append(os.path.join(dirpath, f))
                if len(hits) >= limit:
                    return hits
    return hits


def check_one_factory(cfg, product):
    """No file named like an old factory tool (``legacy_paths:``) is duplicated on PATH or in the
    product repo — the names come entirely from config, never from a literal in this module."""
    legacy_paths = cfg.get('legacy_paths') or []
    if not legacy_paths:
        return True, 'no legacy_paths configured (nothing to check)'
    names, legacy_dirs = _legacy_tool_names(legacy_paths)
    if not names:
        return True, 'legacy_paths configured but no tool files found there'
    dupes = []
    for name in sorted(names):
        found = shutil.which(name)
        if found and os.path.normpath(os.path.dirname(found)) not in legacy_dirs:
            dupes.append(f'{name} on PATH at {found}')
    own = is_factory_repo(product)
    if not own:
        dupes.extend(f'{os.path.basename(p)} in product repo at {p}'
                     for p in _find_in_repo(product.repo_dir, names))
    if dupes:
        shown = '; '.join(dupes[:3])
        more = f' (+{len(dupes) - 3} more)' if len(dupes) > 3 else ''
        return False, shown + more
    return True, (f'{len(names)} legacy tool name(s) checked, no second copy found'
                  + (' (repo skipped: it is the factory itself)' if own else ''))


def check_redaction_hooks(product):
    """(ok, detail) — the redaction gate's git hooks (F-0075, T-0025): every one of
    ``product.repo_dir`` and ``product.backlog_dir`` that is set has an asf pre-commit and
    pre-push in place. Read-only — unlike ``asf hooks install`` (:func:`asf.hooks.ensure_git_hooks`)
    this never writes a hook file; a missing or foreign one stays red until the operator runs the
    command the detail names."""
    repos = [r for r in (product.repo_dir, product.backlog_dir) if r]
    if not repos:
        return True, 'no repo_dir or backlog_dir configured'
    problems = []
    for repo in repos:
        hooks_dir = hooks.git_hooks_dir(repo)
        if hooks_dir is None:
            problems.append(f'{repo} is not a git repo')
            continue
        for name in hooks.GIT_HOOK_NAMES:
            path = os.path.join(hooks_dir, name)
            if not os.path.isfile(path):
                problems.append(f'{path} missing')
                continue
            with open(path, encoding='utf-8') as f:
                text = f.read()
            if hooks.init_hook_upgrade(text, name) is not None:
                problems.append(f'{path} is asf init\'s, from before it ran the redaction gate')
            elif not hooks.is_git_hook_ours(text, name):
                problems.append(f'{path} foreign')
    if problems:
        return False, '; '.join(problems) + f' — asf hooks install --product {product.name}'
    return True, f'pre-commit, pre-push in {len(repos)} repos'


def check_drift(product, installed=None):
    """(ok, detail) — the running install against the trunk's head (B-0086); red when behind.
    Not the factory's source, or an install whose commit cannot be told: ok, and says so."""
    d = drift.check(product, installed=installed)
    if d is None:
        return True, 'not the factory source (nothing to compare)'
    if d.is_behind:
        return False, drift.line(d) + ('' if not d.package_changed else f' — asf upgrade ({d.old} → {d.new})')
    return True, drift.line(d)


# ---- the capacity row --------------------------------------------------------
#
# Spec f-0079 §2.5 has this row read the operator's `capacity.total.sessions`, every product's
# own `capacity.sessions`, `worker_pool.accounts[].cap` and the two deprecated keys through the
# shared resolver `asf.capacity` (F-0079 Task 1). That module has not landed on this branch yet
# (T-0008 is Task 6, out of wave order ahead of it), so the two helpers below read the same data
# straight off the config/product dicts instead. Once `asf.capacity` exists, `check_capacity`
# should delegate to its `product_sessions`/`deprecations` rather than keep this copy.

def _product_declared_sessions(name):
    """This product's own ``capacity.sessions``, or ``None`` for an absent, malformed or
    unreadable value. Reads the file straight (``env.load_file``, not ``env.load_product``),
    because ``capacity:`` is not yet a declared ``PRODUCT_FIELDS`` key on this branch (F-0079
    Task 2) — going through the validated loader would reject the block as unknown."""
    try:
        data = env.load_file(env.product_path(name))
    except (env.ConfigError, OSError):
        return None
    val = ((data or {}).get('capacity') or {}).get('sessions')
    return val if isinstance(val, int) and val >= 0 else None


def _capacity_deprecations(cfg):
    """The two deprecated-key lines of spec f-0079 §4.2, named only when the old key is set."""
    lines = []
    if (cfg.get('feeder') or {}).get('capacity') is not None:
        lines.append('feeder.capacity is set — move it to capacity.per_product.sessions')
    if (cfg.get('worker_pool') or {}).get('reserve_for_s1') is not None:
        lines.append('worker_pool.reserve_for_s1 is set — move it to capacity.reserve_for_s1')
    return lines


def check_capacity(cfg, product):
    """[(ok, detail)] — the ``capacity`` doctor row's findings (spec f-0079 §2.5): every
    product's declared sessions summed against ``capacity.total.sessions``, that total against
    the worker pool's account caps, and any deprecated key still in use. Empty findings collapse
    to one ok row naming the resolved numbers. None of these is a refusal — ``run()`` appends
    each as ``required=False`` (PD6 of the F-0079 plan), so a capacity row never turns doctor red.
    """
    findings = []
    total_sessions = ((cfg.get('capacity') or {}).get('total') or {}).get('sessions')
    if not (isinstance(total_sessions, int) and total_sessions >= 0):
        total_sessions = None

    if total_sessions is not None:
        declared = [(name, n) for name in schema._all_products()
                    for n in [_product_declared_sessions(name)] if n is not None]
        subscribed = sum(n for _name, n in declared)
        if subscribed > total_sessions:
            named = ', '.join(f'{name} {n}' for name, n in declared)
            findings.append((False, f'products declare {subscribed} sessions ({named}) above '
                                     f'capacity.total.sessions {total_sessions}'))

        cap_sum = sum(a.cap for a in pool.accounts_from_config(cfg))
        if total_sessions > cap_sum:
            findings.append((False, f'capacity.total.sessions {total_sessions} is above the '
                                     f'worker_pool account caps ({cap_sum})'))

    for line in _capacity_deprecations(cfg):
        findings.append((False, f'capacity: {line}'))

    if not findings:
        detail = ('no capacity: block configured (nothing to check)' if total_sessions is None
                  else f'capacity.total.sessions {total_sessions}, no oversubscription or '
                       f'deprecated keys')
        findings.append((True, detail))
    return findings


def check_convention_shapes(product):
    """[(ok, detail)] — one RED finding per map-valued convention the product file wrote in
    another shape (``conventions.models: light``, ``landing_checks_missing: '{docs: wait}'``),
    naming the key and its line in the product file. The readers never raise on it; this row is
    what keeps it from being a silent fallback."""
    conv = getattr(product, 'conventions', None)
    findings = conv.shape_findings() if conv is not None and hasattr(conv, 'shape_findings') else []
    out = []
    for key, why in findings:
        line = env.key_line(env.product_path(product.name), f'conventions.{key}')
        where = f'products/{product.name}.yaml:{line}: ' if line else ''
        out.append((False, f'{where}conventions.{key} {why}'))
    return out


def model_table_lines(product):
    """One line per brief kind: the model label each item class resolves to for this product —
    the built-in kind × class table with ``conventions.models`` applied
    (``review: heavy (S1 story feature epic) · light (S2 S3 task)``)."""
    from asf.briefs.build import model_table
    out = []
    for kind, by_class in model_table(product).items():
        labels = {}
        for cls, label in by_class.items():
            labels.setdefault(label, []).append(cls)
        if len(labels) == 1:
            out.append(f'{kind}: {next(iter(labels))}')
        else:
            out.append(f'{kind}: ' + ' · '.join(f"{label} ({' '.join(classes)})"
                                                for label, classes in labels.items()))
    return out


def check_models(cfg):
    """[(ok, detail)] — the ``models`` doctor row (plan F-0093 P11): one finding when
    ``worker_pool.models`` has no ``cheap`` entry, none when it does. ``run()`` appends it as
    ``required=False``, so it prints ``skip`` and never turns doctor red. Reads the dict only.
    """
    models = (cfg.get('worker_pool') or {}).get('models') or {}
    if 'cheap' in models:
        return []
    return [(False, "worker_pool.models has no `cheap` entry — rebase, close and the groom's "
                    "clerical pass fall back to `light`. Map it to your pool's smallest model "
                    "to take the saving F-0093 measures.")]


# ---- the scheduler section --------------------------------------------------
#
# The six rows above answer "is the install sound". They cannot answer "is the factory running",
# because an installed job that never runs looks exactly like a healthy one from the outside:
# `launchctl list` shows the label either way. So every loaded factory job — ours by label
# prefix, the operator's old ones by `scheduler.legacy_labels` — gets a line of its own with
# what launchd knows about it and the last line of its log, and two situations are red:
#
#   * last exit != 0 — it ran and failed.
#   * never exited, more than two intervals after it was installed — it never ran at all. That
#     is B-0014 (b): a job whose ProgramArguments could not resolve an interpreter.
#
# A `legacy_paths` directory that a loaded job still points at is yellow, not red: nothing is
# broken yet, but retiring that directory would break it (which is what the cutover script's —
# `scheduler.CUTOVER_TOOL` — gate 1 refuses to do).

OK, RED, YELLOW = 'ok', 'RED', 'YELLOW'
NEVER_EXITED_INTERVALS = 2


def _log_tail(path):
    """The log's last non-empty line, or None when there is no log yet."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            lines = [line.rstrip() for line in f if line.strip()]
    except OSError:
        return None
    return lines[-1] if lines else ''


def _age_s(path):
    if not path:
        return None
    try:
        return max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        return None


def format_age(seconds):
    if seconds is None:
        return '-'
    if seconds < 90:
        return f'{int(seconds)}s'
    if seconds < 5400:
        return f'{int(seconds // 60)}m'
    if seconds < 172800:
        return f'{int(seconds // 3600)}h'
    return f'{int(seconds // 86400)}d'


def _job_plist(job):
    path = job.get('plist')
    if not path or not os.path.exists(path):
        return {}
    try:
        import plistlib
        with open(path, 'rb') as f:
            return plistlib.load(f)
    except Exception:  # an unreadable plist is a missing one for this section's purposes
        return {}


def scheduler_rows(cfg, product, jobs=None):
    """[(level, label, detail)] — one line per loaded factory job, then the yellow dir lines."""
    rows = []
    if 'interval_s' in (cfg.get('scheduler') or {}):
        rows.append((YELLOW, 'config',
                     'scheduler.interval_s is set but no longer read — clocks live in '
                     f'products/{product.name}.yaml'))

    kind = scheduler.kind(cfg)
    if kind != 'launchd':
        rows.append((OK, f'kind:{kind}',
                     f'scheduler kind {kind!r} has no launchd adapter — nothing to read back'))
        return rows

    jobs = scheduler.loaded_jobs(cfg=cfg) if jobs is None else jobs
    # only this product's clocks: another product's red job is that product's doctor row, and a
    # product's install must not fail on a clock it does not own
    own = f'asf.{product.name}.'
    jobs = [j for j in jobs if not str(j.get('label', '')).startswith('asf.')
            or str(j.get('label', '')).startswith(own)]
    if not jobs:
        rows.append((RED, '(none)', 'no factory job is loaded — nothing ticks this product'))
    for job in sorted(jobs, key=lambda j: j['label']):
        label = job['label']
        info = scheduler.status(label)
        data = _job_plist(job)
        log = data.get('StandardOutPath') or job.get('log')
        interval = data.get('StartInterval') or (86400 if data.get('StartCalendarInterval')
                                                  else None)
        installed_age = _age_s(job.get('plist')) if job.get('plist') else None

        if not info.get('loaded'):
            rows.append((RED, label, 'listed by launchd but `launchctl print` does not know it'))
            continue
        exit_text = 'never exited' if info.get('never_exited') else str(info.get('last_exit'))
        detail = (f"state={info.get('state')}  runs={info.get('runs') or 0}  "
                  f"last-exit={exit_text}  last-run={format_age(_age_s(log))}")
        tail = _log_tail(log)
        detail += f"  log: {tail}" if tail else "  log: (empty)"

        level = OK
        if info.get('never_exited') and installed_age is not None and interval is not None \
                and installed_age > NEVER_EXITED_INTERVALS * int(interval):
            level = RED
            detail += (f"  — never ran in {format_age(installed_age)}, over "
                       f"{NEVER_EXITED_INTERVALS} intervals")
        elif info.get('last_exit'):
            level = RED
        rows.append((level, label, detail))

    for raw in cfg.get('legacy_paths') or []:
        directory = os.path.expanduser(raw)
        for label, path in scheduler.jobs_using(directory, jobs):
            rows.append((YELLOW, directory, f'still in use by {label} ({path})'))
    return rows


def check_clock_steps(product):
    """One ``NEEDS OPERATOR`` line per ``asf``-owned step no clock names. A step owned by a command
    is the operator's own; one declared ``off`` is deliberate — neither is reported. A product whose
    clocks do not load has nothing to compare: the scheduler section reports that."""
    from asf.tick import steps as tick_steps
    try:
        ticked = {step for clock in scheduler.clocks(product) for step in clock.steps}
    except scheduler.SchedulerError:
        return []
    lines = []
    for step, owner, _command in tick_steps.resolve(product):
        if owner == 'asf' and step not in ticked:
            lines.append(f'NEEDS OPERATOR: step {step} is on no clock — add it to a clock in '
                         f'products/{product.name}.yaml (steps: [..., {step}])')
    return lines


def format_scheduler(product_name, rows):
    lines = [f'== SCHEDULER {product_name}']
    width = max((len(r[1]) for r in rows), default=8)
    for level, label, detail in rows:
        lines.append(f'{level.ljust(6)}  {label.ljust(width)}  {detail}')
    return '\n'.join(lines)


def scheduler_is_red(rows):
    return any(level == RED for level, _label, _detail in rows)


def run(product_name):
    """[(row, required, ok, detail)] for every doctor row, in table order."""
    rows = []
    result = check_config(product_name)
    ok, detail = result[0], result[1]
    rows.append(('config', True, ok, detail))
    if not ok:
        return rows
    cfg, product = result[2], result[3]

    ok, detail = check_repo(product)
    rows.append(('repo', True, ok, detail))
    ok, detail = check_backlog(product)
    rows.append(('backlog', True, ok, detail))
    ok, detail = check_scheduler(cfg)
    rows.append(('scheduler', True, ok, detail))
    for name, required, ok, detail in check_cli_sessions(product):
        rows.append((f'cli:{name}', required, ok, detail))
    ok, detail = check_one_factory(cfg, product)
    rows.append(('one-factory', True, ok, detail))
    ok, detail = approvals.check_doctor(cfg, product)
    rows.append(('approvals', True, ok, detail))
    ok, detail = check_redaction_hooks(product)
    rows.append(('redaction-hooks', True, ok, detail))
    ok, detail = check_approvals_hook(cfg, product)
    rows.append(('approvals-hook', True, ok, detail))
    from asf import console_perms
    ok, detail = console_perms.check_doctor(product)
    rows.append(('console permissions', True, ok, detail))
    ok, detail = check_worker_env(cfg)
    rows.append(('worker env', True, ok, detail))
    ok, detail = check_worker_secrets(cfg, product)
    rows.append(('worker secrets', True, ok, detail))
    ok, detail = check_clock_code(product)
    rows.append(('clock code', False, ok, detail))
    ok, detail = check_drift(product)
    rows.append(('drift', True, ok, detail))
    ok, detail = check_rule_checks(product)
    rows.append(('rule-checks', False, ok, detail))
    ok, detail = check_briefs(product)
    rows.append(('briefs', True, ok, detail))
    for ok, detail in check_capacity(cfg, product):
        rows.append(('capacity', False, ok, detail))
    for ok, detail in check_models(cfg):
        rows.append(('models', False, ok, detail))
    for detail in model_table_lines(product):
        rows.append(('model table', False, True, detail))
    for ok, detail in check_convention_shapes(product):
        rows.append(('conventions', True, ok, detail))
    from asf.harvest import deploy  # each environment's deploy mode, and a deprecated key
    for ok, detail in deploy.findings(product):
        rows.append(('deploy', False, ok, detail))
    from asf import customer_content  # a product that deploys names its customer pages
    for ok, detail in customer_content.findings(product):
        rows.append(('customer content', False, ok, detail))
    for ok, detail in check_token_caps(cfg, product):
        rows.append(('token-caps', False, ok, detail))
    for required, ok, detail in check_ci_pool(product):
        rows.append(('ci pool', required, ok, detail))
    for required, ok, detail in check_cloud(cfg, product):
        rows.append(('cloud lane', required, ok, detail))
    slow = check_gate_speed(product)
    if slow:
        rows.append(('gate', False, False, slow))
    refused = check_ref_pushes(product)
    if refused:
        rows.append(('lane pushes', False, False, refused))
    for ok, detail in check_ab_pairs(product):
        rows.append(('ab pairs', False, ok, detail))
    ok, detail = check_worktrees(product)
    rows.append(('worktrees', False, ok, detail))
    branches = check_branches(product)
    if branches:
        rows.append(('branches', False, branches[0], branches[1]))
    return rows


def check_worktrees(product):
    """``(ok, 'worktrees: N, X GB, R removable, …')`` — the worker worktrees now, their sizes and
    what is removable as the tick's last reaper pass recorded them
    (:func:`asf.workers.worktrees.doctor_line`), with the product's pnpm store when it has one.
    Reads a state file; measures nothing."""
    from asf.workers import worktrees
    try:
        return worktrees.doctor_line(product)
    except (OSError, ValueError) as e:
        return False, f'worktrees: unreadable ({e})'


def check_branches(product):
    """``(ok, 'unowned branches: N (prefixes …)')`` — origin's heads no configured pattern owns,
    as the last retention pass counted them (:func:`asf.workers.retention.doctor_line`), else
    None before one ran."""
    from asf.workers import retention
    try:
        return retention.doctor_line(product)
    except (OSError, ValueError):
        return None


def check_ab_pairs(product):
    """[(ok, detail)] — the lane experiment's pairs (:func:`asf.scorecard.pairs.doctor_rows`): a
    warning per pair whose two Features touch the same files, since the overlap spoils the
    comparison. No rows while no card names an ``ab_pair``."""
    from asf.scorecard import pairs
    try:
        return pairs.doctor_rows(product)
    except (OSError, ValueError):
        return []


def check_ci_pool(product):
    """[(required, ok, detail)] — the declared runner pool against the CI host, read-only
    (:func:`asf.ci_pool.doctor_rows`): stranded runners, unsatisfiable ``runs-on``, role drift,
    provider-like labels in ``runs-on``, missing or offline runners. No rows without a pool."""
    from asf import ci_pool
    return ci_pool.doctor_rows(product)


def check_cloud(cfg, product):
    """[(required, ok, detail)] — the cloud lane (:func:`asf.workers.cloud.doctor_rows`): its
    runtime, seats, environment id, accounts, the runtime CLI's cloud flags and the product's
    GitHub origin. No rows while ``cloud.enabled`` is not set."""
    from asf.workers import cloud
    return cloud.doctor_rows(cfg, product)


def check_gate_speed(product):
    """The ``gate too slow: <n>s vs gate_timeout_s`` line after two gate timeouts in a row
    (:func:`asf.harvest.lane.gate_slow_line`, §12), else None — no row while the gate keeps time."""
    from asf.harvest import lane
    try:
        return lane.gate_slow_line(product)
    except (OSError, ValueError):
        return None


def check_ref_pushes(product):
    """The ``ref pushes failing: …`` line when the last lane pass could not push an archive or a
    branch delete (:func:`asf.harvest.lane.ref_push_line`), else None."""
    from asf.harvest import lane
    try:
        return lane.ref_push_line(product)
    except (OSError, ValueError):
        return None


def check_briefs(product, kinds=None):
    """The config smoke test: one brief of every kind rendered for the product (a synthetic row,
    no card, no git) — a product yaml, template or convention that breaks a brief is found here,
    not by the first session of that kind. ``(True, 'N kinds render')``, or ``(False, '<kind>:
    <error>; …')`` naming every kind that raised."""
    from asf import briefs
    from asf.feeder.rows import LAUNCH, Row
    kinds = tuple(kinds or briefs.KINDS)
    failed = []
    for kind in kinds:
        row = Row(tier=0, kind='DOCTOR → SMOKE', item_id='', feature_id='', action=LAUNCH,
                  brief_kind=kind, branch='', reason='doctor: brief smoke test')
        try:
            briefs.build(product, row, {'items': {}}, [], None)
        except Exception as e:  # noqa: BLE001 — every failure is the finding, by kind
            failed.append(f'{kind}: {type(e).__name__}: {e}')
    if failed:
        return False, '; '.join(failed)
    return True, f'{len(kinds)} brief kinds render'


def check_rule_checks(product, path=None):
    """The rule checks that timed out or crashed on the last ``file-bugs`` run (the factory's
    problem, never a product Bug): ``rule check timed out: R-nnnn`` per rule, from the ledger
    ``asf.tick.file_bugs`` keeps in the product's state dir."""
    from asf.tick import file_bugs
    path = path or file_bugs.ledger_path(product)
    checks = (file_bugs._read_ledger(path).get('checks') or {})
    if not checks:
        return True, 'every rule check ran to a pass or a violation on the last file-bugs run'
    parts = [f"rule check {e.get('kind', 'failed')}: {rid} ({e.get('runs', 1)} runs since "
             f"{e.get('since', '?')})" for rid, e in sorted(checks.items())]
    return False, '; '.join(parts)


def _tokens_m(n):
    return 'off' if n is None else f'{n / 1_000_000:.1f} M'


def check_token_caps(cfg, product):
    """[(ok, detail)] — the ``token-caps`` doctor row's findings: the resolved default cap, or one
    refusal per bad ``token_caps:`` entry, and one finding per dimension an operator set to
    ``off`` (an uncapped dimension stays visible every run). ``run()`` appends each as
    ``required=False``, so none of these turns doctor red. ``cfg`` is unused (PD12): the caps
    come from the product alone."""
    try:
        table = tokens.caps(product)
    except tokens.TokenCapError as e:
        return [(False, f'token_caps: {why}') for why in str(e).split('; ')]
    d = table['default']
    kinds = sum(1 for k in table if k != 'default')
    findings = [(True, f'default: in {_tokens_m(d["input"])} / out {_tokens_m(d["output"])} / '
                       f'cache rd {_tokens_m(d["cache_read"])} / cache wr {_tokens_m(d["cache_write"])}; '
                       f'{kinds} kind(s) overridden')]
    for kind, row in (product._get('token_caps') or {}).items():
        for dim, v in row.items():
            if v == tokens.OFF:
                findings.append((False, f'token_caps: {kind}.{dim} is off — uncapped'))
    return findings


def format_table(product_name, rows):
    lines = [f'== DOCTOR {product_name}']
    width = max((len(r[0]) for r in rows), default=8)
    for name, required, ok, detail in rows:
        if ok is None:
            status = 'skip'
        elif ok:
            status = 'ok'
        else:
            status = 'RED' if required else 'skip'
        lines.append(f'{name.ljust(width)}  {status.ljust(4)}  {detail}')
    return '\n'.join(lines)


def is_red(rows):
    return any(required and ok is False for _name, required, ok, _detail in rows)


def cmd_doctor(args, root):
    from asf.cli import stamp, version_string
    product_name = args.product or env.default_product_name()
    rows = run(product_name)
    print(format_table(product_name, rows))

    red = is_red(rows)
    if rows[0][2]:  # config parsed: the scheduler section has a config and a product to read
        cfg = env.load_config()
        srows = scheduler_rows(cfg, env.load_product(product_name))
        print()
        print(format_scheduler(product_name, srows))
        red = red or scheduler_is_red(srows)
        for line in check_clock_steps(env.load_product(product_name)):
            print(line)

    # the doctor reports on asf's install, so its stamp names asf's version, not the product's HEAD
    print(stamp('doctor', version=version_string()))
    return 1 if red else 0
