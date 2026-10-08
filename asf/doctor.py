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
one is missing (its launches are refused). **worker push auth**
(:func:`check_worker_push_auth`) actually probes each account's push credential — a real
``git ls-remote`` and ``push --dry-run`` under that account's own environment — since a file
present is not proof the token still works, or that no keychain a spawned session cannot reach is
still in the way. An eleventh, **clock code** (:func:`check_clock_code`, informational), names
the snapshot sha the clock last ticked from, how many snapshots the code dir holds, and whether
the installed launcher is stale, when the package runs from a checkout (:mod:`asf.snapshot`).
**clock install** (:func:`check_clock_installs`) reads each clock's own
plist — which install will run the next tick, its sha, its distance from ``origin/main`` — and is
red when what ticks is editable, a bare checkout, unplaceable, an unmerged sha, or a snapshot on a
product that is not the factory's own source (:mod:`asf.clockinstall`, F-0104). Each clock's
*effective import* is probed as well — the plist's own interpreter, environment and working
directory run ``import asf`` — and a pinned product's clock must land in ``install.json``'s venv;
a damaged pin (a record that does not parse, or a clock on a per-product venv no record names) is
red. **product loads under venv** loads the product file with the loader of the venv the clocks
run (the pin's, else the clock plist's); **cli dispatcher** (:func:`check_cli_dispatcher`) runs
``~/.local/bin/asf`` and requires the CLI it resolves for this product to sit in the pin;
**agent homes asf** (:func:`check_agent_homes`) is red for any agent home whose
``$HOME/.local/bin/asf`` does not resolve to an executable; the **product** rows name unknown keys (:attr:`asf.env.Product.warnings`) and unknown
``conventions.flags`` names as ``warn``, never red.

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
import datetime
import os
import re
import shutil
import subprocess
import time

from asf import approvals, clockinstall, conventions, drift, env, hooks, schema, scheduler, tokens
from asf import pause as pause_mod
from asf.workers import lifecycle, pool

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
        from asf.connectors import launchd
        loaded, _ = run(launchd.list_argv(label))
        if loaded:
            live.append(f'pre-ASF launchd job {label} still loaded — retire it: '
                        f'{launchd.bootout_hint(label)} (or {CUTOVER_TOOL})')
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


#: The detail prefix of a row that waits on a login no install can make — a ``gh auth login``, a
#: worker account's runtime token, the operator's own console allow list. Such a row is ``skip``
#: (``ok`` None), never RED: a clean install with no login yet is not a broken one (F-0109).
NOT_CONFIGURED = 'not configured'


def _no_login_accounts(cfg):
    """The isolated worker accounts whose ``auth_env`` sets none of the runtime's login
    variables, when that is true of *every* account — a machine where no worker login was ever
    set up. One account with a login and another without is a broken pool, not an unconfigured
    one: then this is empty and the rows stay red."""
    from asf.workers import runtime
    if _backend_is_fake(cfg):
        return []
    accounts = pool.accounts_from_config(cfg)
    bare = [a for a in accounts if a.isolate_home
            and not any(v in a.auth_env for v in runtime.RUNTIME_AUTH_VARS)]
    return bare if accounts and len(bare) == len(accounts) else []


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
    unset = {a.name for a in _no_login_accounts(cfg)}
    for acct in pool.accounts_from_config(cfg):
        if not acct.isolate_home:
            problems.append(f'account {acct.name} has isolate_home: false'
                            + ('' if acct.home else " (the operator's HOME)"))
            continue
        if not fake and acct.name not in unset \
                and not any(v in acct.auth_env for v in runtime.RUNTIME_AUTH_VARS):
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
    if unset:
        var = runtime.RUNTIME_AUTH_VARS[0]
        return None, (f"{NOT_CONFIGURED}: no worker login yet ({', '.join(sorted(unset))}) — run "
                      f'`{runtime.RUNTIME_TOKEN_COMMAND}`, save the token under ~/.ASF/secrets/ '
                      f'and set auth_env: {{{var}: <that file>}} in config.yaml worker_pool')
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


#: The ref a push probe targets: ``--dry-run`` never creates it, so the name is never seen on
#: origin — it only has to be a ref name git accepts.
PUSH_AUTH_PROBE_REF = 'refs/heads/asf-doctor-push-probe'


def _push_probe(job_env, repo_dir):
    """``(ok, line)``: ``git ls-remote origin`` then, only once that succeeds, ``git push
    --dry-run --no-verify origin HEAD:...`` under ``job_env`` — neither call touches a remote
    ref or runs the product's own pre-push hook. ``line`` is git's own last non-blank line on
    failure, else ''."""
    job_env = dict(job_env, GIT_TERMINAL_PROMPT='0')  # a broken credential fails at once, never hangs
    probe = subprocess.run(['git', 'ls-remote', 'origin'], cwd=repo_dir, env=job_env,
                           capture_output=True, text=True, timeout=15)
    if probe.returncode == 0:
        probe = subprocess.run(
            ['git', 'push', '--dry-run', '--no-verify', 'origin', f'HEAD:{PUSH_AUTH_PROBE_REF}'],
            cwd=repo_dir, env=job_env, capture_output=True, text=True, timeout=15)
    if probe.returncode == 0:
        return True, ''
    lines = [l for l in (probe.stderr or probe.stdout or '').splitlines() if l.strip()]
    return False, lines[-1].strip() if lines else 'push auth failed'


def _relaunch_job_env(product, cfg, acct, product_auth_env):
    """The environment a correction's relaunch builds (:func:`asf.workers.stall.correct_once`),
    reproduced with no live session: ``env=`` from :func:`asf.workers.githooks.item_env` (no
    item or branch — the hook env it adds beyond identity needs neither to probe),
    ``hooks_dir`` from :func:`asf.workers.githooks.ensure`, and ``passthrough`` from
    ``worker_pool.env_passthrough`` — every field :func:`asf.workers.runtime.build_env` reads
    beyond the bare first-launch Job (#47: a product's #845, the correction relaunch's git
    losing the product's own credential, went uncaught because the doctor's probe never built
    a Job this way)."""
    from asf.workers import githooks, runtime
    retry_env = githooks.item_env(getattr(product, 'conventions', None), None, None)
    hooks_dir = githooks.ensure(product)
    return runtime.build_env(runtime.Job(
        product.name, 'doctor-probe-correction', product.repo_dir, None, None, account=acct,
        env=retry_env, hooks_dir=hooks_dir, passthrough=env.env_passthrough(cfg),
        product_auth_env=product_auth_env))


def probe_account_push_auth(acct, product, product_auth_env=None):
    """(ok, detail) — one worker account's git push credential (credential helper, SSH or
    token, however it is configured): ``git ls-remote origin`` and a ``git push --dry-run
    --no-verify origin HEAD:...`` against the product's own repo, under exactly the environment
    that account's sessions get (:func:`asf.workers.runtime.build_env`) — never the operator's
    HOME or its keychain. Neither call touches a remote ref (``--dry-run``) or runs the
    product's own pre-push hook (``--no-verify``, the same skip a ref-only factory push takes,
    :mod:`asf.gitpush`).

    Catches what ``worker secrets`` cannot: a file present and non-empty but a token expired,
    revoked or wrong for this repo, or an account with no ``GH_TOKEN`` at all silently falling
    back to a credential store a spawned, non-interactive session cannot reach (2026-09-26: 4
    local sessions failed to push with ``could not read Username for 'https://github.com': Device
    not configured`` — the osxkeychain helper unreachable from the launched process).

    The one probe both :func:`check_worker_push_auth` (doctor's ``worker push auth`` row, every
    account at once) and the launch preflight (:func:`asf.workers.spawn.spawn`, B-0040, one
    account before it spends a session) run — so a credential judged broken reads the same
    either place."""
    from asf.workers import runtime
    if product_auth_env is None:
        product_auth_env = env.product_auth_env(product)
    try:
        job_env = runtime.build_env(runtime.Job(product.name, 'doctor-probe', product.repo_dir,
                                                 None, None, account=acct,
                                                 product_auth_env=product_auth_env))
    except runtime.AuthEnvError as e:
        return False, str(e)
    ok, line = _push_probe(job_env, product.repo_dir)
    return (True, 'authenticates') if ok else (False, line)


def check_worker_push_auth(cfg, product):
    """(ok, detail) — the ``worker push auth`` row: :func:`probe_account_push_auth` run once per
    worker account **and** once more under the environment a correction's relaunch builds
    instead (:func:`_relaunch_job_env`, :func:`asf.workers.stall.correct_once`): the two are not
    the same Job (the relaunch's carries ``env=``, ``hooks_dir`` and ``passthrough`` the first
    launch's bare probe never set), and a regression in one has already shipped invisibly to the
    other (#47, following #845). Red names each account and env whose probe fails, with git's
    own last line of output. No worker accounts, no product repo, or the ``fake`` backend (no
    agent session ever pushes): nothing to probe."""
    from asf.workers import runtime
    accounts = pool.accounts_from_config(cfg)
    if not accounts or product is None or not product.repo_dir or not os.path.isdir(product.repo_dir):
        return True, 'no worker accounts or product repo to probe'
    if _backend_is_fake(cfg):
        return True, 'worker_pool.backend fake runs no agent session (nothing to push)'
    if all(not a.auth_env for a in accounts):
        return None, (f'{NOT_CONFIGURED}: no worker account has an auth_env yet (see worker env) '
                      '— nothing to probe')
    product_auth_env = env.product_auth_env(product)
    failures, oks = [], []
    for acct in accounts:
        ok, detail = probe_account_push_auth(acct, product, product_auth_env)
        (oks if ok else failures).append(acct.name if ok else f'{acct.name}: {detail}')
        try:
            relaunch_env = _relaunch_job_env(product, cfg, acct, product_auth_env)
        except runtime.AuthEnvError as e:
            failures.append(f'{acct.name} (correction relaunch): {e}')
            continue
        ok, line = _push_probe(relaunch_env, product.repo_dir)
        if ok:
            oks.append(f'{acct.name} (correction relaunch)')
        else:
            failures.append(f'{acct.name} (correction relaunch): {line}')
    if failures:
        return False, '; '.join(failures)
    return True, 'authenticates for ' + ', '.join(oks)


def check_credentials(cfg, product):
    """(ok, detail) — the ``credentials`` row: every provider this product needs, from the
    cache (never a fresh probe: the doctor is a view). Red when any is invalid or inside its
    window, and its detail carries the renew command.

    Config problems from :func:`asf.credentials.config_problems` are reported first and red,
    naming each dotted key — a misconfigured section cannot be judged. A
    :class:`asf.env.ConfigError` from :func:`asf.credentials.for_product` (a product names a
    provider ``config.yaml`` does not define) is caught and is the red detail. A provider with
    no cache entry reads as ``not probed yet`` and is not red — an empty cache is a factory that
    has not ticked, not a bad credential. No providers configured, or a product that names
    none: ``(True, 'no providers configured')``."""
    from asf import credentials
    problems = credentials.config_problems(cfg)
    if problems:
        return False, '; '.join(f'{key}: {why}' for key, why in problems)
    try:
        wanted = credentials.for_product(product, cfg)
    except env.ConfigError as e:
        return False, str(e)
    if not wanted:
        return True, 'no providers configured'
    now = datetime.datetime.now(datetime.timezone.utc)
    cache = credentials.read_cache(product)
    notes, bad = [], []
    for provider in wanted:
        result = cache.get(provider.name)
        if result is None:
            notes.append(f'{provider.name} not probed yet')
            continue
        v = credentials.verdict(result, now, provider.window_days)
        if v == 'expiring':
            left = credentials.days_left(result, now)
            bad.append(f'{provider.name} expires {result.expires} ({int(left)}d) — renew: '
                       f'{provider.renew}')
        elif v == 'invalid':
            bad.append(f'{provider.name} is not valid — renew: {provider.renew}')
        else:
            notes.append(provider.name)
    if bad:
        return False, '; '.join(bad)
    return True, '; '.join(notes) if notes else 'ok'


def check_clock_code(product):
    """(ok, detail) — the code the product's clock runs: the snapshot sha it last ticked from,
    the checkout's HEAD (the next tick takes that), how many snapshots the code dir holds, and
    whether the installed launcher is stale, or the installed package's root. Always ``True``:
    a stale launcher is a lag the next tick repairs, not a breakage, and this row never reddens
    a product over it."""
    info = scheduler.clock_code(product.name)
    if not info.get('snapshot'):
        return True, f"installed package {info.get('root')}"
    sha, head = info.get('sha'), info.get('head')
    if not sha:
        detail = f"snapshot of {info['root']}: no tick has run from one yet (HEAD {(head or '?')[:12]})"
    else:
        detail = f"snapshot {sha[:12]} ({format_age(_age_s_since(info.get('at')))} ago)"
        if head and head != sha:
            detail += f'; checkout HEAD {head[:12]} (the next tick takes it)'
    detail += f' · {info.get("snapshots", 0)} snapshots'
    stale, reason = info.get('launcher_stale', (False, ''))
    if stale:
        detail += (f' — launcher is stale: {reason}; the next tick refreshes it, or '
                    f'`asf scheduler install --product {product.name}`')
    return True, detail


def check_install_checkout(product):
    """[(ok, detail)] — red when the checkout the product's clock runs from is not clean on
    ``main`` at ``origin/main`` (:func:`asf.upgrade.checkout_off_main`). No row unless a clock
    launcher is installed under this ASF_HOME: only then does a checkout run a live factory."""
    from asf import scheduler, upgrade
    if not os.path.exists(scheduler.launcher_path(product.name)):
        return []
    off = upgrade.checkout_off_main()
    return [(off is None, off or 'clean on main at origin/main, or not a checkout')]


def _age_s_since(at):
    return None if at is None else max(0.0, time.time() - at)


def _clock_install_phrase(inst):
    """The kind/venv/sha (or checkout) phrase a ``clock install`` row's detail opens with."""
    from asf import version
    sha = version.pin_label(inst.sha) if inst.sha else ''
    if inst.kind == 'pinned':
        return f'pinned {inst.venv} @ {sha or "(unknown)"}'
    if inst.kind == 'editable':
        return f'editable {inst.venv} @ {sha or "(unknown)"} ({inst.repo})'
    if inst.kind == 'snapshot':
        return f'snapshot of {inst.repo} @ {sha}' if sha else f'snapshot of {inst.repo} (no tick yet)'
    if inst.kind == 'checkout':
        return f'checkout {inst.repo} @ {sha or "(unknown)"}'
    return 'unknown'


def _clock_install_red(inst, factory, merged):
    """``(ok, why)`` — the R1-R4 RED rule, in one place, applied per clock (F-0104 spec §3):
    RED for a ``kind`` of ``editable``/``checkout``/``unknown`` (R1), a ``snapshot`` clock on a
    product that is not the factory's own source (R2), or a sha that is not an ancestor of
    ``origin/main`` (R3); ok otherwise (R4)."""
    if inst.kind in ('editable', 'checkout'):
        return False, 'the clock ticks a working tree'
    if inst.kind == 'unknown':
        return False, inst.why
    if inst.kind == 'snapshot' and not factory:
        return False, "this product is not the factory's own source"
    if merged is False:
        return False, 'NOT on origin/main — unmerged work is ticking this product'
    return True, ''


# ---- the effective import: what a clock's plist really runs ------------------------------

#: The one line a probe interpreter runs: where ``import asf`` lands under that environment.
_IMPORT_PROBE = 'import asf,sys;print(asf.__file__)'


def _plist_cwd(path):
    """The plist's ``WorkingDirectory`` (the probe's cwd: ``python -c`` puts it first on
    ``sys.path``), else ``/``."""
    import plistlib
    try:
        with open(path, 'rb') as f:
            data = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return '/'
    cwd = data.get('WorkingDirectory') if isinstance(data, dict) else None
    return cwd if isinstance(cwd, str) and os.path.isdir(cwd) else '/'


def effective_import(interpreter, env_vars, cwd='/', timeout=60):
    """``(path, error)`` — where ``import asf`` resolves when ``interpreter`` runs with exactly
    the plist's ``EnvironmentVariables`` (launchd passes nothing else) from ``cwd``: the real
    path of ``asf/__init__.py``, or ``('', why)``."""
    probe_env = {k: str(v) for k, v in (env_vars or {}).items()}
    probe_env.setdefault('PATH', '/usr/bin:/bin')
    try:
        p = subprocess.run([interpreter, '-c', _IMPORT_PROBE], capture_output=True, text=True,
                           env=probe_env, cwd=cwd, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return '', f'{interpreter} did not run ({e})'
    lines = (p.stdout or '').strip().splitlines()
    if p.returncode != 0 or not lines:
        err = (p.stderr or '').strip().splitlines()
        return '', f"import asf failed under {interpreter} ({err[-1] if err else f'exit {p.returncode}'})"
    return os.path.realpath(lines[-1]), ''


def _under_dir(path, root):
    root = os.path.realpath(root)
    return bool(path) and (path == root or path.startswith(root.rstrip(os.sep) + os.sep))


def _pin_venv(rec):
    """The pin's venv as an absolute, real dir (a bare name is under pipx's venvs dir)."""
    from asf import installs
    venv = os.path.expanduser(rec.venv)
    if not os.path.isabs(venv):
        venv = os.path.join(installs.venvs_root(), venv)
    return os.path.realpath(venv)


def pin_state(product_name):
    """``(rec, damage)`` — the product's :class:`asf.installs.Install` (or None) and, when the
    pin is damaged, one phrase saying how. :func:`asf.installs.read` reads a record that is
    there but will not parse as *no pin* (the scheduler and the dispatcher then run the shared
    install), so a damaged record is told apart here: the file exists and is no pin."""
    from asf import installs
    rec = installs.read(product_name)
    path = installs.record_path(product_name)
    if rec is not None or not os.path.exists(path):
        return rec, ''
    try:
        own = [os.path.basename(v) for v in installs.list_venvs(product_name)]
    except OSError:
        own = []
    tail = (f'; per-product venv(s) on disk: {", ".join(own)}' if own else '')
    return None, (f'pin unreadable: {path} is there but names no sha and venv — the scheduler '
                  f'and the dispatcher treat {product_name} as unpinned{tail}')


def _pin_phrase(rec):
    return (f'pin={rec.label} venv={os.path.basename(rec.venv.rstrip(os.sep))} '
            f'previous={rec.previous_label}')


def _per_product_venv(product_name, path):
    """The ``asf-factory-<product>-<sha7>`` dir name ``path`` sits in, else ''."""
    from asf import installs
    prefix = f'{installs.DIST}-{product_name}-'
    for part in path.split(os.sep):
        if part.startswith(prefix) and len(part) == len(prefix) + 7:
            return part
    return ''


def _import_verdict(product_name, inst, rec, damage, probe):
    """``(ok, phrase)`` for one clock's effective import against the product's pin: ``ok`` None
    when nothing was probed (a snapshot launcher, or an interpreter that is not on disk on an
    unpinned product — the clock-install rule already judges those)."""
    path_ = scheduler.plist_path(inst.label)
    interpreter = inst.interpreter
    if inst.kind == 'snapshot' or not interpreter:
        return None, ''
    if not os.path.exists(interpreter) and rec is None and not damage:
        return None, ''
    _argv, env_vars = clockinstall.read(path_)
    got, err = probe(interpreter, env_vars, _plist_cwd(path_))
    if err:
        return False, err
    if rec is not None:
        venv = _pin_venv(rec)
        if not _under_dir(got, venv):
            return False, f'clock {inst.label} imports asf from {got}, pin is {venv}'
        return True, 'effective import ok'
    own = _per_product_venv(product_name, got)
    if own:
        return False, (f'clock {inst.label} imports asf from {got} ({own}) but no pin names '
                       f'it — {damage or "install.json is missing"}')
    return True, f'unpinned · imports asf from {got}'


def check_clock_installs(cfg, product, probe=None):
    """[(required, ok, detail)] — one row per product clock: which install it runs, its sha, how
    far behind origin/main, and RED when what ticks is not a pinned, merged install (F-0104).

    Each clock's *effective import* is probed too (:func:`effective_import`: the plist's own
    interpreter, environment and working directory): for a pinned product it must land inside
    ``install.json``'s venv, else RED ``clock <label> imports asf from <path>, pin is <venv>``.
    A damaged pin is RED as well — a record that is there but does not parse (the scheduler and
    the dispatcher read it as *unpinned*), or a clock importing a per-product venv no record
    names."""
    probe = probe or effective_import
    rec, damage = pin_state(product.name)
    job_kind = scheduler.kind(cfg)
    if job_kind != 'launchd':
        return [(False, None, f'kind:{job_kind} · no launchd plist to read')]
    insts = clockinstall.for_product(product.name, cfg)
    if not insts:
        rows = [(False, None, '(none) · no clock plist in ~/Library/LaunchAgents — the '
                              'SCHEDULER section names the clocks')]
        if damage:
            rows.insert(0, (True, False, damage))
        return rows
    repo, head = clockinstall.trunk(cfg)
    factory = is_factory_repo(product)
    ref = head[:7] if head else 'main'
    repair = f'install.sh {product.name} {ref}'
    rows = []
    for inst in insts:
        behind, merged = clockinstall.against_trunk(inst.sha, repo, head)
        ok, why = _clock_install_red(inst, factory, merged)
        phrase = _clock_install_phrase(inst)
        if inst.kind == 'unknown':
            detail = f'{inst.label} · unknown · {why}'
        elif inst.kind in ('editable', 'checkout'):
            detail = f'{inst.label} · {phrase} · {why} — reinstall it pinned: {repair}'
        elif not ok:
            trunk_phrase = (f'origin/main {head[:7]}' if head else
                            'origin/main unknown (no ASF checkout on this host)')
            tail = why if merged is False else f'{why} — reinstall it pinned: {repair}'
            detail = f'{inst.label} · {phrase} · {trunk_phrase} · {tail}'
        elif not head:
            detail = (f'{inst.label} · {phrase} · origin/main unknown '
                      '(no ASF checkout on this host) — distance not measured')
        else:
            trunk_phrase = f'origin/main {head[:7]}'
            if behind is None:
                tail = 'not found in origin/main — distance not measured'
            elif behind == 0:
                tail = 'current'
            else:
                tail = f'behind by {behind}'
            detail = f'{inst.label} · {phrase} · {trunk_phrase} · {tail}'
        imp_ok, imp = _import_verdict(product.name, inst, rec, damage, probe)
        if rec is not None and imp_ok:
            detail = (f'{inst.label} · pinned {os.path.basename(_pin_venv(rec))} ({imp}) · '
                      f'{_pin_phrase(rec)} · ' + detail.split(' · ', 2)[-1])
        elif imp_ok is False:
            ok = False
            detail = f'{detail} · {imp}' + (f' · {_pin_phrase(rec)}' if rec is not None else '')
        elif imp:
            detail = f'{detail} · {imp}'
        rows.append((True, ok, detail))
    if damage and not any(damage in d for _r, _o, d in rows):
        rows.insert(0, (True, False, damage))
    return rows


#: Loads the product the way the venv's own clock would and prints one JSON line. Written for
#: every release's reader: one before ``Product.warnings`` existed answers no warnings.
_LOAD_PROBE = r'''
import json, sys
import asf
from asf import env
out = {'asf': asf.__file__}
try:
    p = env.load_product(sys.argv[1])
except Exception as e:
    out.update(ok=False, error=(str(e) or type(e).__name__).strip().splitlines()[0])
else:
    out.update(ok=True, warnings=[list(w) for w in getattr(p, 'warnings', ()) or ()])
print(json.dumps(out))
'''


def _venv_of(interpreter):
    """``<venv>`` for ``<venv>/bin/python``."""
    return os.path.dirname(os.path.dirname(interpreter))


def _clean_env(extra=None):
    """A child environment with no ``PYTHONPATH`` (a venv interpreter finds its own ``asf``) and
    this process's ``ASF_HOME``."""
    child = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME')}
    child.update(extra or {})
    child['ASF_HOME'] = env.ASF_HOME
    return child


def load_under(interpreter, product_name, env_vars=None, cwd='/', timeout=120):
    """``(result, error)`` — :data:`_LOAD_PROBE` run by ``interpreter``: ``result`` is its JSON
    (``ok``, ``error`` or ``warnings``, ``asf``), ``error`` a phrase when the probe itself did
    not answer."""
    import json
    child = _clean_env() if env_vars is None else dict(
        {k: str(v) for k, v in env_vars.items()}, ASF_HOME=env.ASF_HOME)
    child.setdefault('PATH', '/usr/bin:/bin')
    try:
        p = subprocess.run([interpreter, '-c', _LOAD_PROBE, product_name], capture_output=True,
                           text=True, env=child, cwd=cwd, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return None, f'{interpreter} did not run ({e})'
    for line in reversed((p.stdout or '').strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and 'ok' in data:
            return data, ''
    err = (p.stderr or '').strip().splitlines()
    return None, f"{interpreter}: {err[-1] if err else f'exit {p.returncode}'}"


def _load_target(product_name, cfg):
    """``(interpreter, env_vars, cwd, label)`` the product's clocks load it with: the pin's own
    interpreter (no ``PYTHONPATH``), else the first clock plist whose interpreter is on disk —
    with that plist's environment — else None."""
    from asf import installs
    rec = installs.read(product_name)
    if rec is not None:
        venv = _pin_venv(rec)
        return installs.interpreter(venv), None, '/', f'pin {os.path.basename(venv)}'
    try:
        insts = (clockinstall.for_product(product_name, cfg)
                 if scheduler.product_labels(product_name, cfg) else [])
    except Exception:  # noqa: BLE001 — no plist to read is no target, the row says so
        insts = []
    for inst in insts:
        if inst.kind == 'snapshot' or not inst.interpreter or not os.path.exists(inst.interpreter):
            continue
        path_ = scheduler.plist_path(inst.label)
        _argv, env_vars = clockinstall.read(path_)
        return inst.interpreter, env_vars, _plist_cwd(path_), f'clock {inst.label} (unpinned)'
    return None


def check_product_loads_under_venv(cfg, product, target=None):
    """[(required, ok, detail)] — the product file loads under the venv its clocks run (the
    pin's, else the clock plist's): a key this checkout tolerates but that venv's loader
    refuses is a clock that ticks ``ConfigError``. Its warnings ride on the detail."""
    target = target or _load_target(product.name, cfg)
    if target is None:
        return [(False, None, 'no pinned venv and no clock plist to load it under')]
    interpreter, env_vars, cwd, label = target
    if not os.path.exists(interpreter):
        return [(True, False, f'{label}: {interpreter} is not on disk')]
    data, err = load_under(interpreter, product.name, env_vars, cwd)
    if data is None:
        return [(True, False, f'{label}: {err}')]
    from asf import installs
    sha = installs.commit_of(_venv_of(interpreter))[:7]
    at = f' @ {sha}' if sha else ''
    if not data.get('ok'):
        return [(True, False, f'{label}{at}: refuses products/{product.name}.yaml — '
                              f'{data.get("error")}')]
    warns = data.get('warnings') or []
    tail = f' · {len(warns)} warning(s): ' + ', '.join(str(w[1]) for w in warns) if warns else ''
    return [(True, True, f'{label}{at}: loads{tail}')]


def check_product_warnings(product):
    """[(ok, detail)] — the ``product`` rows: each unknown key the file carries (a warning, it
    still loads — :attr:`asf.env.Product.warnings`) and each ``conventions.flags`` name this
    release does not read (:meth:`asf.conventions.Conventions.unknown_flags`, a typo is a switch
    that never flips). ``'warn'`` rows, never red; one ok row when there is neither."""
    rows = []
    for line, key, problem in getattr(product, 'warnings', ()) or ():
        at = f'line {line}: ' if line else ''
        rows.append(('warn', f'{at}{key} {problem}'))
    conv = getattr(product, 'conventions', None)
    unknown = conv.unknown_flags() if hasattr(conv, 'unknown_flags') else []
    if unknown:
        rows.append(('warn', f'conventions.flags: {", ".join(unknown)} — not a flag this release '
                             f'reads ({", ".join(conventions.KNOWN_FLAGS)})'))
    return rows or [(True, 'no unknown keys, no unknown flags')]


def check_plan_headings(product):
    """[(ok, detail)] — the ``plans`` row: a plan in ``plans_dir`` with Task-like headings that
    parse to 0 Tasks mints no Task cards (a format drift). ``'warn'`` per plan, one ok row when
    every plan parses or has no Task headings. Reads the checkout read-only."""
    from asf.tick import migrate
    d = product.repo_dir
    conv = getattr(product, 'conventions', None)
    rel = conv.doc_dir('plan') if hasattr(conv, 'doc_dir') else 'plans'
    root = os.path.join(d, rel) if d else ''
    if not root or not os.path.isdir(root):
        return [(True, 'no plans directory')]
    rows, n = [], 0
    for dirpath, _dirs, files in sorted(os.walk(root)):
        for name in sorted(files):
            if not name.endswith('.md'):
                continue
            path = os.path.join(dirpath, name)
            try:
                with open(path, encoding='utf-8') as f:
                    text = f.read()
            except (OSError, UnicodeDecodeError):
                continue
            n += 1
            like = migrate.task_like_headings(text)
            if like:
                rows.append(('warn', f'{os.path.relpath(path, d)}: {len(like)} Task-like heading(s) '
                                     f'parse to 0 Tasks (e.g. {like[0]!r}) — no Task cards will mint'))
    return rows or [(True, f'{n} plan(s): every Task-like heading parses')]


def check_connectors(cfg):
    """[(required, ok, detail)] — the ``connectors`` row: the active implementation of each
    connector kind (:func:`asf.connectors.active`) and where that choice came from, on one line;
    one RED row per configured kind whose implementation cannot be found (an unknown name, a
    command form without its command, a third-party package that fails to import)."""
    from asf import connectors
    rows = [(False, True, ', '.join(f'{kind} {name}' + ('' if source == 'default' else f' ({source})')
                                    for kind, name, source in connectors.active(cfg)))]
    for _kind, message in connectors.problems(cfg):
        rows.append((True, False, message))
    return rows


def check_config_keys(cfg):
    """[('warn', detail)] — the ``config keys`` row: the keys of ``config.yaml`` no code reads
    (:func:`asf.config_keys.unknown_keys`), one row naming them all, and one naming each set key
    whose value breaks its kind or range (:func:`asf.config_keys.problems`); none when there are
    none. Neither changes what runs (a malformed tunable keeps its default), so ``warn``, never red."""
    from asf import config_keys
    rows = []
    unknown = config_keys.unknown_keys(cfg)
    if unknown:
        rows.append(('warn', f'config.yaml: {", ".join(unknown)} — not read by this release (a '
                             f'setting that silently does nothing: remove it, or see asf.config_keys)'))
    bad = config_keys.problems(cfg)
    if bad:
        rows.append(('warn', 'config.yaml: ' + '; '.join(f'{k} {why}' for k, why in bad)
                     + ' — the default is used instead (asf.config_keys.TYPED)'))
    return rows


def check_cli_dispatcher(product, path=None, timeout=60):
    """[(required, ok, detail)] — the CLI every hook and session calls (``~/.local/bin/asf``,
    :mod:`asf.dispatch`) resolves this product into its pin. The dispatcher is run with
    ``ASF_DISPATCH_TRACE=1 … --product <p> --version`` and the CLI its trace names must sit in
    ``install.json``'s venv: RED when it does not. A pinned product whose path still holds the
    shared install's link is RED (F-0283: its hooks run the shared install, never the pin —
    ``asf hooks install`` replaces the link)."""
    from asf import dispatch
    path = path or dispatch.default_path()
    rec, damage = pin_state(product.name)
    if damage:
        return [(True, False, damage)]
    if not os.path.lexists(path):
        if rec is None:
            return [(False, None, f'{path} absent (unpinned)')]
        return [(True, False, f'{path} absent — hooks and sessions have no asf to run')]
    if not dispatch.is_ours(path):
        target = os.path.realpath(path)
        if rec is None:
            return [(True, True, f'{path} → {target} (shared install; unpinned, so the same '
                                 'one the clocks run)')]
        return [(True, False, f'{path} → {target} is not the dispatcher — hooks run that, '
                              f'not the pin {os.path.basename(_pin_venv(rec))}; asf hooks install '
                              f'--product {product.name} replaces it')]
    child = _clean_env({'ASF_DISPATCH_TRACE': '1'})
    child.pop('ASF_PRODUCT', None)
    try:
        p = subprocess.run(['/bin/sh', path, '--product', product.name, '--version'],
                           capture_output=True, text=True, env=child, cwd='/', timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return [(True, False, f'{path} did not run ({e})')]
    cli = ''
    for line in (p.stderr or '').splitlines():
        if line.startswith('asf-dispatch:') and ' cli=' in line:
            cli = line.split(' cli=', 1)[1].strip()
    if not cli or cli == 'none':
        return [(True, False, f'{path} resolves no asf for {product.name} (exit {p.returncode})')]
    real = os.path.realpath(cli)
    if rec is None:
        return [(True, True, f'{path} → {real} (unpinned: the default)')]
    venv = _pin_venv(rec)
    if not _under_dir(real, venv):
        return [(True, False, f'{path} runs {real} for {product.name}, pin is {venv}')]
    return [(True, True, f'ok · {product.name} → {os.path.basename(venv)} ({_pin_phrase(rec)})')]


def check_agent_homes(cfg=None):
    """[(required, ok, detail)] — every agent home's ``$HOME/.local/bin/asf`` resolves to an
    executable (:func:`asf.workers.runtime.home_cli_problems`). A session runs under its own HOME
    and its git hooks call that path: a dangling link there refuses every commit and push of that
    agent (2026-10-03). RED naming each broken home; skip when there are no agent homes or the
    pool's backend is ``fake`` (no agent session runs a hook)."""
    from asf.workers import runtime
    if cfg and _backend_is_fake(cfg):
        return [(False, None, 'worker_pool.backend fake runs no agent session (no hook to call asf)')]
    homes = runtime.agent_homes()
    if not homes:
        return [(False, None, 'no agent homes')]
    bad = runtime.home_cli_problems(homes)
    if bad:
        return [(True, False, '; '.join(f'{os.path.basename(h)}: {why}' for h, why in bad)
                 + ' — asf hooks install --product <p> relinks every home')]
    return [(True, True, f'{len(homes)} homes resolve .local/bin/asf')]


# name -> (required, probe argv); required tools missing/failing are red, optional ones are skip
_CLI_TOOLS = [
    ('git', True, ['git', '--version']),
    ('gh', True, None),  # :func:`_gh_auth` — through asf.github, never a raw argv
    ('gcloud', False, ['gcloud', 'auth', 'list']),
    ('az', False, ['az', 'account', 'show']),
    ('aws', False, ['aws', 'sts', 'get-caller-identity']),
    ('flyctl', False, ['flyctl', 'auth', 'whoami']),
    ('vercel', False, ['vercel', 'whoami']),
]


def _gh_auth(timeout=10):
    """``(ok, detail)`` of ``gh auth status`` read through :func:`asf.github.gh`: ok only on a
    real answer; an Unknown (not runnable, a timeout, a rate limit) is never ok."""
    from asf import connectors, gh_limit
    try:
        r = connectors.forge().auth_status(timeout=timeout)
    except gh_limit.RateLimited:
        return False, 'rate limited'
    detail = (r.stdout or r.stderr or '').strip().splitlines()
    if r.ok:
        return True, detail[0] if detail else ''
    return False, detail[0] if detail else r.reason


#: ``gh auth status``'s own words for "no login on this machine" — a login not yet made, as
#: opposed to one that is broken (expired, revoked, rate limited), which stays red.
_GH_NOT_LOGGED_IN = re.compile(r'not logged in(to any)?|gh auth login', re.I)


def check_cli_sessions(product=None):
    """[(name, required, ok_or_None, detail)] — ``ok`` is ``None`` for an optional tool not
    installed. ``gh`` is optional for a product with no PR host."""
    rows = []
    for name, required, argv in _CLI_TOOLS:
        if name == 'gh' and product is not None and not has_pr_host(product):
            required = False
        if not required and shutil.which(argv[0] if argv else name) is None:
            rows.append((name, required, None, 'not installed'))
            continue
        ok, detail = _run(argv) if argv else _gh_auth()
        if name == 'gh' and ok is False and _GH_NOT_LOGGED_IN.search(detail or ''):
            ok, detail = None, f'{NOT_CONFIGURED}: gh is not logged in — gh auth login'
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
    command the detail names. For a pinned product each hook's exec path is read too
    (:func:`asf.hooks.hook_entry`): one that is not the dispatcher runs another install than the
    pin (F-0283), and is red."""
    from asf import dispatch
    repos = [r for r in (product.repo_dir, product.backlog_dir) if r]
    if not repos:
        return True, 'no repo_dir or backlog_dir configured'
    pinned = os.path.isfile(dispatch.record_path(product.name))
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
            elif pinned:
                entry = hooks.hook_entry(text, name)
                if entry is None or not dispatch.is_ours(entry):
                    problems.append(f'{path} execs {entry or "asf on PATH"}, not the dispatcher '
                                    f'(F-0283: it does not run the pin)')
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


def check_readme(product):
    """``(ok, detail)``, or ``None`` to skip the row entirely — the committed README against its
    committed facts (``asf readme --check --json``, F-0030 §2.7). Skipped for a product whose
    README carries no span, or whose repo dir does not resolve. A page that has never been
    refreshed at all — spans, but no committed facts file yet, nothing to have drifted *from* —
    is ``'warn'`` (never red on its own); otherwise required, so a page a contributor forgot to
    refresh after is RED, naming the first complaint."""
    from asf.views import readme
    repo_dir = product.repo_dir
    if not repo_dir or not os.path.isdir(repo_dir):
        return None
    conv = product.conventions
    try:
        with open(os.path.join(repo_dir, conv.readme), encoding='utf-8') as f:
            text = f.read()
    except OSError:
        return None
    if not readme.spans(text):
        return None
    facts_path = os.path.join(repo_dir, conv.readme_facts)
    if not os.path.isfile(facts_path):
        return 'warn', f'{conv.readme_facts} is missing — run `asf readme --refresh`'
    import json
    try:
        with open(facts_path, encoding='utf-8') as f:
            facts_data = json.load(f)
        problems = readme.complaints(text, facts_data, repo_dir)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if problems:
        return False, f'{len(problems)} complaints — {problems[0]}'
    return True, 'ok'


def check_release(product, health=None):
    """(ok, detail) — the factory's own release rule (:func:`asf.version.health`): red when a
    merge to main that changed the package carries no ``v<x.y.z>`` tag, or ``CHANGELOG.md`` has
    no entry for the newest tag. A product that is not the factory's source: ok, and says so."""
    from asf import version
    repo = product.repo_dir
    if not repo or not os.path.isdir(repo) or not drift.is_factory_source(repo):
        return True, 'not the factory source (its releases are the rollup\'s)'
    return (health or version.health)(repo, f'origin/{product.main}')


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


def check_release_floor_seats(product):
    """The release gate's criteria 9 (floor clean) and 10 (seats used), as
    :func:`asf.release.doctor_rows` reads them; one warn row when they cannot be read."""
    from asf import release
    try:
        return release.doctor_rows(product)
    except Exception as e:  # noqa: BLE001 — an unreadable gate is a warning, never a doctor crash
        return [('release floor/seats', False, f'not read ({type(e).__name__}: {e})')]


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


def check_savings(product):
    """[(ok, detail)] — the ``savings`` doctor row's findings (spec f-0100 §2.9): every key of
    a product's ``conventions.savings`` block is a key of ``conventions.DEFAULT_SAVINGS`` and
    every value is a number greater than zero, so a misspelled or misvalued threshold is told to
    the operator rather than silently read as the default. A block written in some shape other
    than a map (``savings`` is not in ``MAP_CONVENTIONS``, so nothing upstream reshapes it) is its
    own red finding rather than a crash. Green names the resolved count and how many of the eight
    were overridden."""
    conv = getattr(product, 'conventions', None)
    savings = getattr(conv, 'savings', None)
    findings = []
    if savings is not None and not isinstance(savings, dict):
        findings.append((False, f'conventions.savings must be a map, not {savings!r}'))
        savings = None
    savings = savings or {}
    overridden = 0
    for key, value in savings.items():
        if key not in conventions.DEFAULT_SAVINGS:
            known = ', '.join(sorted(conventions.DEFAULT_SAVINGS))
            findings.append((False, f'conventions.savings.{key} is not a known threshold '
                                     f'(one of {known})'))
            continue
        if value != conventions.DEFAULT_SAVINGS[key]:
            overridden += 1
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            findings.append((False, f'conventions.savings.{key} {value!r} must be a number '
                                     f'greater than zero'))
    if not findings:
        findings.append((True, f'savings: {len(conventions.DEFAULT_SAVINGS)} thresholds, '
                                f'{overridden} overridden'))
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
    """[(ok, detail)] — the ``models`` doctor row (plan F-0093 P11): one ``warn`` finding when
    ``worker_pool.models`` has no ``cheap`` entry (every cheap row falls back to ``light``),
    none when it does. It prints ``warn`` and never turns doctor red. Reads the dict only.
    """
    models = (cfg.get('worker_pool') or {}).get('models') or {}
    if 'cheap' in models:
        return []
    return [('warn', "worker_pool.models has no `cheap` entry — rebase, close and the groom's "
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

#: how much of a tick log's end the doctor reads. The log is never rotated, so reading all of it
#: to find one line is an unbounded cost on every `asf doctor` (F-0142, D7). A step that prints
#: more than this after its start line shows no `step:` field — the row says what it says today.
LOG_TAIL_BYTES = 1024 * 1024

#: the tick's per-step lines (`asf.tick.tick.step_start_line` / `step_end_line`). `[step:<name>]
#: FAILED …` matches neither, and the end pattern also matches the pre-F-0142 shape, so a log
#: written by an older install reads as "no step running" rather than as a running one.
_STEP_START_RE = re.compile(r'^\[step:([a-z-]+)\] start owner=(\S+) pid=(\d+) at=(\S+)')
_STEP_END_RE = re.compile(r'^\[step:([a-z-]+)\] \d+\.\ds')


def _log_lines(path, limit=LOG_TAIL_BYTES):
    """The log's last ``limit`` bytes as non-empty rstripped lines, oldest first; ``[]`` when there
    is no log or it cannot be read."""
    if not path or not os.path.exists(path):
        return []
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            f.seek(max(0, size - limit))
            data = f.read()
    except OSError:
        return []
    text = data.decode('utf-8', errors='replace')
    parts = text.split('\n')
    if size > limit and '\n' in text:
        parts = parts[1:]
    return [line.rstrip() for line in parts if line.strip()]


def _log_tail(path):
    """The log's last non-empty line, or None when there is no log yet."""
    if not os.path.exists(path or ''):
        return None
    lines = _log_lines(path)
    return lines[-1] if lines else ''


def _since(at, now=None):
    """Seconds between ``at`` (`tick._stamp`'s ``%Y-%m-%dT%H:%M:%SZ``) and ``now`` (default: the
    live clock), or ``None`` when ``at`` does not parse."""
    try:
        then = datetime.datetime.strptime(at, '%Y-%m-%dT%H:%M:%SZ')
    except (ValueError, TypeError):
        return None
    epoch = then.replace(tzinfo=datetime.timezone.utc).timestamp()
    return max(0.0, (now or time.time()) - epoch)


def current_step(path, alive=None, now=None):
    """The step a tick is inside, read off the tick log's own start line (F-0142):
    ``{step, owner, pid, alive, seconds}``, or ``None``.

    ``None`` when there is no log, when the last per-step line in the tail is an *end* line (the
    tick is between steps, or over), and when no per-step line is in the tail at all. A start line
    whose pid no longer answers is returned with ``alive`` false: that tick died inside that step,
    which is the one failure launchd's own `last exit code` cannot show (D6). ``seconds`` is how
    long ago the line's ``at`` was, or None when it cannot be parsed."""
    for line in reversed(_log_lines(path)):
        if _STEP_END_RE.match(line):
            return None
        m = _STEP_START_RE.match(line)
        if m:
            step, owner, pid, at = m.groups()
            return {'step': step, 'owner': owner, 'pid': int(pid),
                    'alive': (alive or lifecycle.pid_alive)(int(pid)),
                    'seconds': _since(at, now)}
    return None


def step_detail(running):
    """The row's ``step:`` field for a :func:`current_step` result."""
    age = '' if running['seconds'] is None else f" {format_age(running['seconds'])}"
    if not running['alive']:
        started = '' if running['seconds'] is None else \
            f", started {format_age(running['seconds'])} ago"
        return (f"{running['step']} unfinished — its tick (pid {running['pid']}) is gone"
                f"{started}")
    return f"{running['step']} running{age} ({running['owner']}, pid {running['pid']})"


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
    """[(level, label, detail)] — the pending upgrade while one holds *this* product's ticks, one
    line per loaded factory job, then the yellow dir lines."""
    from asf import upgrade
    rows = []
    # a clock can fire on time into a tick that skips every time on a pending upgrade (B-0141):
    # the wait is the section's first line, so the jobs' `ok` is never read as a ticking factory.
    # The marker's owner is the one product still ticking, so it reads no wait here.
    marker = upgrade.held(product.name)
    if marker is not None:
        rows.append((YELLOW, 'upgrade', upgrade.held_label(marker)))
    record = pause_mod.held(product)            # F-0137: the operator's own launch pause
    if record is not None:
        rows.append((YELLOW, 'PAUSED', f'launches {pause_mod.text(record)} — `asf resume '
                     f'--product {product.name}` lifts it; the tick still records and harvests'))
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
    loaded_labels = {j['label'] for j in jobs}
    try:
        declared = scheduler.clocks(product)
    except scheduler.SchedulerError:
        declared = []
    # a clock the product declares that launchd does not currently hold (a bootstrap that failed
    # silently, or one an install never got to, B-0136) names itself here — the loop below only
    # ever sees what's loaded, so this is the one place that would otherwise be silent
    missing = sorted(scheduler.label_for(product.name, c.name, cfg) for c in declared
                     if scheduler.label_for(product.name, c.name, cfg) not in loaded_labels)
    paused = {label: scheduler.pause_record(label, cfg)
              for label in scheduler.product_labels(product.name, cfg) + missing}
    paused = {label: record for label, record in paused.items()
              if record is not None and label not in loaded_labels}
    for label in sorted(paused):
        rows.append((YELLOW, 'PAUSED', f'{label} {scheduler.pause_text(paused[label])} — '
                     f'{scheduler.resume_hint(label, cfg)} to start it'))
    missing = [label for label in missing if label not in paused]
    if not jobs and not missing and not paused:
        rows.append((RED, '(none)', 'no factory job is loaded — nothing ticks this product'))
    for label in missing:
        rows.append((RED, label, f'declared in products/{product.name}.yaml but not loaded'))
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
        running = current_step(log)
        if running is not None:
            detail += f"  step: {step_detail(running)}"
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
        if running is not None and not running['alive'] and level == OK:
            # a tick killed inside a step leaves `last exit code` green (F-0142, D6): a warning,
            # never the doctor's own red, which gates an install
            level = YELLOW
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
    ok, detail = check_worker_push_auth(cfg, product)
    rows.append(('worker push auth', True, ok, detail))
    ok, detail = check_credentials(cfg, product)
    rows.append(('credentials', True, ok, detail))
    for ok, detail in check_install_checkout(product):
        rows.append(('install checkout', True, ok, detail))
    ok, detail = check_clock_code(product)
    rows.append(('clock code', False, ok, detail))
    for required, ok, detail in check_clock_installs(cfg, product):
        rows.append(('clock install', required, ok, detail))
    for required, ok, detail in check_product_loads_under_venv(cfg, product):
        rows.append(('product loads under venv', required, ok, detail))
    for required, ok, detail in check_cli_dispatcher(product):
        rows.append(('cli dispatcher', required, ok, detail))
    for required, ok, detail in check_agent_homes(cfg):
        rows.append(('agent homes asf', required, ok, detail))
    for ok, detail in check_product_warnings(product):
        rows.append(('product', False, ok, detail))
    for ok, detail in check_plan_headings(product):
        rows.append(('plans', False, ok, detail))
    for required, ok, detail in check_connectors(cfg):
        rows.append(('connectors', required, ok, detail))
    for level, detail in check_config_keys(cfg):
        rows.append(('config keys', False, level, detail))
    net = check_network(cfg)
    if net is not None:
        rows.append(('network', False, net[0], net[1]))
    host = check_host_clock(cfg)
    if host is not None:
        rows.append(('network clock', False, host[0], host[1]))
    ok, detail = check_drift(product)
    rows.append(('drift', True, ok, detail))
    readme_result = check_readme(product)
    if readme_result is not None:
        rows.append(('readme', True, readme_result[0], readme_result[1]))
    ok, detail = check_release(product)
    rows.append(('release', True, ok, detail))
    for name, ok, detail in check_release_floor_seats(product):
        rows.append((name, False, ok, detail))
    ok, detail = check_rule_checks(product)
    rows.append(('rule-checks', False, ok, detail))
    ok, detail = check_briefs(product)
    rows.append(('briefs', True, ok, detail))
    for ok, detail in check_capacity(cfg, product):
        rows.append(('capacity', False, ok, detail))
    for ok, detail in check_savings(product):
        rows.append(('savings', False, ok, detail))
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
    from asf import security  # the record's adoption of R-0009, and the age of its last feed read
    for ok, detail in security.doctor_findings(product):
        rows.append(('security', False, ok, detail))
    for ok, detail in check_token_caps(cfg, product):
        rows.append(('token-caps', False, ok, detail))
    for required, ok, detail in check_ci_pool(product):
        rows.append(('ci pool', required, ok, detail))
    from asf import ci_census
    for required, ok, detail in ci_census.census_rows(product):
        rows.append(('ci census', required, ok, detail))
    for required, ok, detail in check_ci_measure(product):
        label = 'ci baseline' if detail.startswith('ci baseline:') else 'ci measure'
        rows.append((label, required, ok, detail))
    for required, ok, detail in check_ci_runners(product):
        rows.append(('ci runners', required, ok, detail))
    for required, ok, detail in check_ci_heartbeat(product):
        rows.append(('ci heartbeat', required, ok, detail))
    for required, ok, detail in check_queue_bypass(product):
        rows.append(('queue bypass', required, ok, detail))
    for required, ok, detail in check_queue_cancels(product):
        rows.append(('queue cancels', required, ok, detail))
    for required, ok, detail in check_queue_pause(product):
        rows.append(('queue pause', required, ok, detail))
    for required, ok, detail in check_trunk_ruleset(product):
        rows.append(('trunk ruleset', required, ok, detail))
    for required, ok, detail in check_trunk_stall(product):
        rows.append(('trunk stall', required, ok, detail))
    for required, ok, detail in check_trunk_red(product):
        rows.append(('trunk red', required, ok, detail))
    for required, ok, detail in check_ci_classes(product):
        rows.append(('ci classes', required, ok, detail))
    for required, ok, detail in check_cloud(cfg, product):
        rows.append(('cloud lane', required, ok, detail))
    rows.append(('lane split', False, True, check_lane_split(cfg, product)))
    slow = check_gate_speed(product)
    if slow:
        rows.append(('gate', False, False, slow))
    refused = check_ref_pushes(product)
    if refused:
        rows.append(('lane pushes', False, False, refused))
    for ok, detail in check_ab_pairs(product):
        rows.append(('ab pairs', False, ok, detail))
    lock = check_account_lock(cfg)
    if lock is not None:
        rows.append(('quota lock', False, lock[0], lock[1]))
    auth = check_account_auth()
    if auth is not None:
        rows.append(('account auth', False, auth[0], auth[1]))
    pushed = check_pushed_work(product)
    if pushed is not None:
        rows.append(('pushed work', False, pushed[0], pushed[1]))
    ok, detail = check_worktrees(product)
    rows.append(('worktrees', False, ok, detail))
    branches = check_branches(product)
    if branches:
        rows.append(('branches', False, branches[0], branches[1]))
    dead = check_dead_sessions(product)
    if dead:
        rows.append(('dead sessions', False, dead[0], dead[1]))
    return rows


def check_host_clock(cfg):
    """``(ok, detail)`` for the host clock ``asf.host.net-probe`` while ``network.probe`` is on
    (installed, loaded or paused); None while it is off."""
    from asf import scheduler
    from asf.tick import network
    if not network.enabled(cfg):
        return None
    label = scheduler.host_label(cfg)
    if not os.path.exists(scheduler.definition_path(label, cfg)):
        return False, (f'{label} is not installed — `asf scheduler install --host` writes it')
    record = scheduler.pause_record(label, cfg)
    if record is not None:
        return False, f'{label} {scheduler.pause_text(record)} — `asf scheduler resume --host`'
    if not scheduler.status(label).get('loaded'):
        return False, f'{label} is installed but not loaded — `asf scheduler install --host`'
    return True, f'{label} installed and loaded'


def check_network(cfg, now=None):
    """``(ok, detail)`` from the host probe's last record (:func:`asf.tick.network.doctor_row`),
    or None while ``network.probe`` is off and nothing was ever recorded."""
    from asf.tick import network
    return network.doctor_row(env.ASF_HOME, cfg, now=now)


def check_account_lock(cfg=None):
    """``(ok, detail)`` for the account manager's usage lock (:mod:`asf.workers.account_lock`),
    or None when none is configured (``account_lock.path``) or its file is absent. Wedged: not ok,
    and names the holder and the opt-in reclaim."""
    from asf.workers import account_lock
    try:
        w = account_lock.probe_wedge(cfg if cfg is not None else env.load_config())
    except Exception as e:  # noqa: BLE001 — an unreadable probe is one unknown row
        return None, f'cannot read the account lock — {e}'
    if w is None:
        return None
    if w.wedged:
        return False, (f'{w.label} — quota readings go stale; account_lock.reclaim: '
                       f'true lets the tick reclaim it')
    return True, w.label


def check_account_auth():
    """``(ok, detail)``: one red row while an auth error keeps any account out of the pool
    (:mod:`asf.workers.account_auth`), or None when none is."""
    from asf.workers import account_auth
    return account_auth.doctor_row()


def pushed_work_line(orphans):
    """``(ok, detail)`` for the pushed-work invariant: red while an open Task/Bug with an open PR
    has no NEXT row and no session (:func:`asf.feeder.rows.orphaned_pushed`)."""
    orphans = list(orphans or ())
    if not orphans:
        return True, 'pushed work: every open Task/Bug with an open PR has a NEXT row'
    shown = ', '.join(orphans[:8]) + (f' +{len(orphans) - 8}' if len(orphans) > 8 else '')
    return False, (f'pushed work: {len(orphans)} open Task/Bug with an open PR and no NEXT row '
                   f'— nothing reviews, lands or reaps them: {shown}')


def check_pushed_work(product):
    """``(ok, detail)`` — :func:`pushed_work_line` over the plan ``asf next`` prints, or None when
    the plan cannot be drawn (no record yet)."""
    root = getattr(product, 'backlog_dir', None)
    if not root or not os.path.isfile(os.path.join(root, 'index.json')):
        return None
    try:
        from asf import capacity as capacity_mod
        from asf.feeder import rows as feeder_rows
        from asf.tick import step_wave
        from asf.views import index_reader
        index, _ = index_reader.load(root)
        inflight = step_wave.inflight(product)
        inputs = step_wave.plan_inputs(product, root)
        plan = feeder_rows.plan_rows(index, product, inflight,
                                     capacity_mod.resolve(product).sessions, **inputs)
        return pushed_work_line(feeder_rows.orphaned_pushed(index, plan, inputs.get('occupancy'),
                                                            inflight))
    except Exception as e:  # noqa: BLE001 — an unreadable plan is one unknown row
        return None, f'pushed work: cannot draw the plan — {e}'


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
    """``(ok, 'branches: N of M heads carry no open PR — …')`` — the census of origin's heads by
    the rule that covers each one, as the last retention pass took it
    (:func:`asf.workers.retention.doctor_line`), else None before one ran."""
    from asf.workers import retention
    try:
        return retention.doctor_line(product)
    except (OSError, ValueError):
        return None


def check_dead_sessions(product):
    """``(ok, 'dead sessions: N in 14 days — …')`` — the window's ``dead pid`` runs by the class
    F-0234 writes on them (:func:`asf.workers.health.dead_census_line`), or None when the ledger
    could not be read. ``ok`` is False while any death in the window is ``unknown`` — a count of
    things that already happened is not a broken installation (C8), so the row is non-required."""
    from asf.workers import health
    try:
        return health.dead_census_line(health.dead_census(product))
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


def check_ci_measure(product, now=None, root=None):
    """[(required, ok, detail)] — three advisory row groups off the ``ci`` stream and the
    published baseline (:mod:`asf.ci_measure`), with no CI host call at all (PD8): a slow runner
    and a flaky runner, both off :func:`asf.ci_measure.scores`, and a green reading that has
    quietly grown past its own baseline, off :func:`asf.ci_measure.regressions` against the
    **converted** published baselines (R1 — :func:`asf.ci_measure.read_baselines` returns the
    file's own ``{runner: {kind: {'p50_s', 'n'}}}`` shape, not the ``(seconds, n)`` tuples
    ``regressions`` wants). Every row is advisory (D19): the demotion a slow or flaky box earns
    is already the action a required row cannot outrun any faster. ``[]`` with no stream, no
    census (:func:`asf.ci_census.cached`, D2's degradation applied here too), or on any
    exception reading either file — a diagnostic command that dies on a state file is worse than
    one that says nothing."""
    from asf import ci_census, ci_measure
    from asf.metrics import metrics
    from asf.tick import shadow
    root = root if root is not None else shadow.record_dir(product.name)
    try:
        tiers_list = ci_census.cached(product)
        if not tiers_list:
            return []
        window_days = ci_measure.tunable('WINDOW_DAYS')
        events = metrics.read_stream(root, 'ci', metrics.days_back(metrics.today(), window_days))
        rdgs = ci_measure.readings(events, now=now)
        sc = ci_measure.scores(rdgs)
        tiers = {e.runner: e.role for e in tiers_list}
        per = ci_measure.read_baselines(product).get('per') or {}
        base = {r: {k: (v['p50_s'], v['n']) for k, v in kinds.items()} for r, kinds in per.items()}
        regress = ci_measure.regressions(rdgs, base)
    except Exception:  # noqa: BLE001 — a diagnostic row is never worth a doctor crash
        return []
    out = []
    for name in sorted(sc):
        score = sc[name]
        if score.ratio is not None and score.ratio >= ci_census.DEMOTE_AT and score.ratios:
            kind = max(score.ratios, key=lambda k: score.ratios[k])
            n = score.n
            out.append((False, False,
                        f'ci measure: {name} runs {kind} at {score.medians[kind]:g}s, '
                        f'{score.ratios[kind]:.2g}× the fleet best ({n} green reading'
                        f'{"s" if n != 1 else ""}, {window_days} d) — '
                        f'{tiers.get(name, ci_census.BULK)}'))
    for name in sorted(sc):
        score = sc[name]
        if score.flaky:
            out.append((False, False,
                        f'ci measure: {name} green {score.green_rate} over {score.n} readings '
                        f'— flaky, never {ci_census.FAST}'))
    for runner_name, kind, latest, base_s in regress:
        n = base.get(runner_name, {}).get(kind, (None, 0))[1]
        out.append((False, False,
                    f'ci baseline: {kind} on {runner_name} last ran {latest:g}s against its '
                    f'{base_s:g}s baseline ({latest / base_s:.2g}×) — {n} green reading'
                    f'{"s" if n != 1 else ""}'))
    return out


def check_ci_heartbeat(product, now=None):
    """[(required, ok, detail)] — every ``ci.pool`` box with a heartbeat in the last
    ``ci_heartbeat.stale_min`` minutes (:func:`asf.ci_heartbeat.doctor_rows`, off the watchdog's
    seen file, no ssh): red naming a box with none. No rows without a pool or a seen file."""
    from asf import ci_heartbeat
    try:
        return ci_heartbeat.doctor_rows(product, now=now)
    except Exception as e:  # noqa: BLE001 — an unreadable file is one unknown row
        return [(False, None, f'cannot read the ci heartbeat file — {e}')]


def check_ci_runners(product, now=None):
    """[(required, ok, detail)] — the runners the CI queue's pass found busy with no job
    (:func:`asf.ci_queue.runner_rows`, off its file, no ``gh`` call): red after 10 min. No rows
    for a product the queue does not watch."""
    from asf import ci_queue
    try:
        return ci_queue.runner_rows(product, now=now)
    except Exception as e:  # noqa: BLE001 — an unreadable file is one unknown row
        return [(False, None, f'cannot read the ci queue file — {e}')]


def check_queue_bypass(product):
    """[(required, ok, detail)] — the trunk commits that did not come through the merge queue
    (:func:`asf.trunk_watch.doctor_rows`, off the tick's state file): one red row each with its
    sha, author and PR. No rows for a product not on ``merge: queue``."""
    from asf import trunk_watch
    try:
        return trunk_watch.doctor_rows(product)
    except Exception as e:  # noqa: BLE001 — an unreadable file is one unknown row
        return [(True, None, f'cannot read the trunk watch — {e}')]


def check_queue_cancels(product):
    """[(required, ok, detail)] — one red row per merge-queue batch cut again because a required
    check judged no code (cancelled, timed out, stale, never started) twice on one sha
    (:func:`asf.merge_queue.doctor_rows`, off ``ci-cancels.json``). No rows otherwise."""
    from asf import merge_queue
    try:
        return merge_queue.doctor_rows(product)
    except Exception as e:  # noqa: BLE001 — an unreadable file is one unknown row
        return [(True, None, f'cannot read the merge queue cancels — {e}')]


def check_queue_pause(product):
    """[(required, ok, detail)] — one ``warn`` row while ``merge_queue.inflight: 0`` pauses new
    cuts (:func:`asf.merge_queue.paused`): the first-class pause for a pin move (#36) — batches
    already in flight still land, a dead one is still dropped, only the next cut waits. No rows
    otherwise (never red: a deliberate pause is not a defect)."""
    from asf import merge_queue
    conv = getattr(product, 'conventions', None)
    if conv is None:
        return []
    try:
        on = merge_queue.paused(conv)
    except Exception as e:  # noqa: BLE001 — an unreadable convention is one unknown row
        return [(False, None, f'cannot read merge_queue.inflight — {e}')]
    if not on:
        return []
    return [(False, 'warn', 'merge_queue.inflight: 0 — no new batch is cut; batches already in '
                            'flight still land, a dead one is still dropped')]


def check_trunk_stall(product):
    """[(required, ok, detail)] — red while the trunk has stood still past
    ``conventions.ci.trunk_stall_hours`` with landings waiting (:func:`asf.trunk_watch.stall_rows`,
    off the tick's state files). No rows for a product not on ``merge: queue``."""
    from asf import trunk_watch
    try:
        return trunk_watch.stall_rows(product)
    except Exception as e:  # noqa: BLE001 — an unreadable file is one unknown row
        return [(True, None, f'cannot read the trunk watch — {e}')]


def check_trunk_red(product):
    """[(required, ok, detail)] — ``TRUNK RED: <check> (seen on #a, #b)`` while a required check
    is red on the trunk itself, suspected from unrelated landings or confirmed by a full run
    (:func:`asf.trunk_red.doctor_rows`, off its state file); else the last full run's age. No rows
    for a product not on ``merge: queue``."""
    from asf import trunk_red
    try:
        return trunk_red.doctor_rows(product)
    except Exception as e:  # noqa: BLE001 — an unreadable file is one unknown row
        return [(True, None, f'cannot read the trunk red watch — {e}')]


def check_trunk_ruleset(product, gh=None):
    """[(required, ok, detail)] — the host's ruleset on the trunk requires the merge queue's
    status (:func:`asf.trunk_ruleset.doctor_rows`): green names the ruleset and its break-glass
    call, red when it is missing or disabled. No rows for a product not on ``merge: queue``."""
    from asf import trunk_ruleset
    try:
        return trunk_ruleset.doctor_rows(product, gh=gh)
    except Exception as e:  # noqa: BLE001 — an unreadable host is one unknown row
        return [(True, None, f'cannot read the trunk ruleset — {e}')]


def check_ci_classes(product, backend=None):
    """[(required, ok, detail)] — every required CI job resolves to exactly one runner class,
    and no runner carries a provider label its declared provider contradicts
    (:func:`asf.runner_classes.doctor_rows`). Read-only; no rows without a declared pool."""
    from asf import runner_classes
    try:
        return runner_classes.doctor_rows(product, backend=backend)
    except Exception as e:  # noqa: BLE001 — an unreadable host is one unknown row
        return [(True, None, f'cannot judge runner classes — {e}')]


def check_cloud(cfg, product):
    """[(required, ok, detail)] — the cloud lane (:func:`asf.workers.cloud.doctor_rows`): its
    runtime, seats, environment id, accounts, the runtime CLI's cloud flags and the product's
    GitHub origin. No rows while ``cloud.enabled`` is not set."""
    from asf.workers import cloud
    return cloud.doctor_rows(cfg, product)


def check_lane_split(cfg, product):
    """The ``lane split`` row: how this product's work splits between the host and the cloud
    lane now, and why (:func:`asf.workers.cloud.lane_split`) — informational, never red."""
    from asf.workers import cloud
    try:
        return cloud.lane_split(cfg, product)
    except Exception as e:  # noqa: BLE001 — an unreadable split is one row, not a crash
        return f'cannot read the lane split — {type(e).__name__}: {e}'


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
        elif ok == 'warn':
            status = 'warn'
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
