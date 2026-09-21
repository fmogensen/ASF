"""asf.doctor — one table: is this product's ASF installation sound (``asf doctor --product <p>``).

Six checks, each a row: config, product repo, backlog, scheduler job, CLI sessions (gh/git
required; gcloud/az/aws/flyctl/vercel optional, skipped if not installed), and the **one-factory
check** — no second copy of a factory tool on PATH or inside the product repo. The search set for
that last check is never a literal path in this code: it is ``legacy_paths:`` in the operator's
``~/.ASF/config.yaml``, a list of directories that once held the old, per-product tooling this
product's ``asf`` install replaces.

Exit 1 if any required row is red; optional rows that are unavailable print ``skip``, not red.
"""
import os
import shutil
import subprocess

from asf import env

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
    missing = [k for k in ('repo_dir', 'repo_slug', 'backlog_dir') if not getattr(product, k, None)]
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


def check_cli_sessions():
    """[(name, required, ok_or_None, detail)] — ``ok`` is ``None`` for an optional tool not installed."""
    rows = []
    for name, required, argv in _CLI_TOOLS:
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
    dupes.extend(f'{os.path.basename(p)} in product repo at {p}'
                 for p in _find_in_repo(product.repo_dir, names))
    if dupes:
        shown = '; '.join(dupes[:3])
        more = f' (+{len(dupes) - 3} more)' if len(dupes) > 3 else ''
        return False, shown + more
    return True, f'{len(names)} legacy tool name(s) checked, no second copy found'


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
    for name, required, ok, detail in check_cli_sessions():
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
    print(stamp('doctor'))
    return 1 if is_red(rows) else 0
