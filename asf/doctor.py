"""asf.doctor — one table: is this product's ASF installation sound (``asf doctor --product <p>``).

Six checks, each a row: config, product repo, backlog, scheduler job, CLI sessions (git required;
gh required when the product has a PR host; gcloud, az, aws, flyctl and vercel optional, skipped if
not installed), and the **one-factory check** — no second copy of a factory tool on PATH or inside
the product repo. The search set for that last check is never a literal path in this code: it is
``legacy_paths:`` in the operator's ``~/.ASF/config.yaml``, a list of directories that once held
the old, per-product tooling this product's ``asf`` install replaces. A product whose repo is this
package's own checkout skips the repo half: its tools are the factory, not a second copy of it.

A product with no PR host — ``ci: {provider: none}`` — needs no ``repo_slug`` and no ``gh``.

Exit 1 if any required row is red; optional rows that are unavailable print ``skip``, not red.
"""
import os
import shutil
import subprocess
import time

from asf import env, scheduler

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


def is_factory_repo(product):
    """True when the product's repo is the ASF package's own checkout (real paths compared)."""
    return bool(product.repo_dir) and os.path.realpath(product.repo_dir) == os.path.realpath(package_root())


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


def check_scheduler(cfg):
    sched = cfg.get('scheduler') or {}
    kind = sched.get('kind') or sched.get('provider') or 'launchd'
    if kind != 'launchd':
        return True, f'scheduler kind {kind!r} (not launchd; launchctl check skipped)'
    label = sched.get('launchd_label')
    if not label:
        return False, 'scheduler.launchd_label not set in config.yaml'
    ok, detail = _run(['launchctl', 'list', label])
    return ok, (label if ok else f'{label} not in `launchctl list` ({detail or "not found"})')


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
    kind = scheduler.kind(cfg)
    if kind != 'launchd':
        return [(OK, f'kind:{kind}',
                 f'scheduler kind {kind!r} has no launchd adapter — nothing to read back')]

    jobs = scheduler.loaded_jobs(cfg=cfg) if jobs is None else jobs
    rows = []
    if not jobs:
        rows.append((RED, '(none)', 'no factory job is loaded — nothing ticks this product'))
    for job in sorted(jobs, key=lambda j: j['label']):
        label = job['label']
        info = scheduler.status(label)
        data = _job_plist(job)
        log = data.get('StandardOutPath') or job.get('log')
        interval = data.get('StartInterval') or scheduler.interval_s(cfg)
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
        if info.get('never_exited') and installed_age is not None \
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
    return rows


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
    from asf.cli import stamp
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

    print(stamp('doctor'))
    return 1 if red else 0
