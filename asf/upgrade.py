"""asf.upgrade — ``asf upgrade``: reinstall the package at the trunk's head, then the schema check
for every product, one table: product · record schema · package · action.

The install is pinned (the install script runs ``pipx install --force git+<url>@<sha>``), so
``pipx upgrade`` reinstalls the same pin and changes nothing. The upgrade reinstalls at a named
commit instead: the head the tick's drift check read, or ``main``'s head for a manual run. It is
deferred while another tick runs (a reinstall under a running tick tore it: ImportError
mid-tick), refused on a head whose remote CI is red, verified by the new install's own commit,
and followed by reloading any product clock that is on disk but not loaded.

A deferral alone never finds a gap: two products whose ticks run for minutes, launched every few
minutes, always overlap. So a tick's deferred upgrade writes the *pending* marker
(``state/upgrade-pending.json``: the sha, when, and the owning product); every other tick reads
it at its start and exits without a step while it is fresh (:func:`waiting`), running ticks
finish, and the owner's next tick finds the gap and installs, clearing the marker. A marker
older than :data:`PENDING_TTL_S`, or whose sha is already installed, is removed and ignored —
a stuck upgrade never stops the factory.

Every upgrade pauses every product's ticks, so the tick's upgrades are batched: after one
installs (``state/upgrade-last.json``), no tick starts another until ``upgrade.min_interval_min``
(``~/.ASF/config.yaml``, default :data:`DEFAULT_MIN_INTERVAL_MIN`) has passed — by then several
commits have landed and one install carries them all. A head any of whose new commits carries
an ``Urgent: yes`` trailer upgrades regardless (:func:`batch_hold`).
"""
import glob
import json
import os
import re
import subprocess
import time

from asf import __version__, env, schema
from asf.drift import DEFERRED  # noqa: F401 — the upgrade waits for the next tick (EX_TEMPFAIL)

PACKAGE_NAME = 'asf-factory'
DEFAULT_REPO_URL = 'https://github.com/fmogensen/ASF.git'
#: a tick's command line: ``python -m asf.cli tick …`` (the clocks) or ``…/bin/asf tick …`` —
#: anchored on the interpreter's argv, so a shell whose script merely mentions them does not match
TICK_PATTERN = r'(-m asf\.cli|/asf) tick( |$)'
#: a pending marker older than this is stale: removed and ignored
PENDING_TTL_S = 30 * 60
#: ``upgrade.min_interval_min`` when the operator config leaves it out
DEFAULT_MIN_INTERVAL_MIN = 30


def pending_path():
    return os.path.join(env.ASF_HOME, 'state', 'upgrade-pending.json')


def read_pending():
    try:
        with open(pending_path(), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get('sha') else None


def write_pending(sha, owner, now=None):
    """Mark the upgrade to ``sha`` pending, owned by product ``owner``. An existing marker keeps
    its first timestamp (a newer head never extends the wait past :data:`PENDING_TTL_S`)."""
    old = read_pending()
    at = old.get('at') if old and isinstance(old.get('at'), (int, float)) else None
    data = {'sha': sha, 'owner': owner, 'at': at if at is not None else (now or time.time())}
    path = pending_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    os.replace(tmp, path)
    return data


def clear_pending():
    try:
        os.remove(pending_path())
    except OSError:
        pass


def _installed(sha, installed):
    return bool(installed and sha and (installed.startswith(sha) or sha.startswith(installed)))


def pending(now=None, installed=None):
    """The fresh pending marker, or ``None``. A stale one (older than :data:`PENDING_TTL_S`, or
    its sha already installed) is removed."""
    data = read_pending()
    if data is None:
        return None
    if installed is None:
        from asf import drift
        installed = drift.installed_commit()
    at = data.get('at')
    age = (now or time.time()) - at if isinstance(at, (int, float)) else None
    if age is None or age > PENDING_TTL_S or age < -60 or _installed(data['sha'], installed):
        clear_pending()
        return None
    return data


def waiting(product_name, out=print, now=None, installed=None):
    """True when this tick must not start: an upgrade owned by another product is pending, and
    this tick's running would only keep the gap the upgrade needs from coming. The owner's ticks
    go on — the owner retries the upgrade at its start."""
    data = pending(now, installed)
    if data is None or data.get('owner') == product_name:
        return False
    out(f'tick: waiting — upgrade to {data["sha"][:7]} pending')
    return True


def last_path():
    return os.path.join(env.ASF_HOME, 'state', 'upgrade-last.json')


def read_last():
    """``{'sha', 'at'}`` of the last install, or ``None`` when none is recorded."""
    try:
        with open(last_path(), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get('at'), (int, float)) else None


def record_last(sha, now=None):
    path = last_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'sha': sha or '', 'at': now or time.time()}, f)
    os.replace(tmp, path)


def min_interval_s(cfg=None):
    """``upgrade.min_interval_min`` from the operator config, in seconds (default 30 minutes)."""
    if cfg is None:
        try:
            cfg = env.load_config()
        except Exception:  # noqa: BLE001 — a config problem leaves the default in force
            cfg = {}
    block = (cfg or {}).get('upgrade')
    value = block.get('min_interval_min') if isinstance(block, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        value = DEFAULT_MIN_INTERVAL_MIN
    return value * 60


def urgent(repo, installed, head):
    """True when a commit in ``installed..head`` carries an ``Urgent: yes`` trailer."""
    if not (repo and installed and head):
        return False
    try:
        p = subprocess.run(['git', '-C', repo, 'log', '--format=%(trailers:key=Urgent,valueonly)',
                            f'{installed}..{head}'], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0 and any(ln.strip().lower() == 'yes' for ln in p.stdout.splitlines())


def batch_hold(repo, installed, head, now=None, cfg=None):
    """When the tick's upgrade to ``head`` waits for the batching interval: the epoch time it is
    due, else ``None`` (no install recorded yet, the interval has passed, or the head is urgent)."""
    last = read_last()
    if last is None:
        return None
    due = last['at'] + min_interval_s(cfg)
    if (now or time.time()) >= due or urgent(repo, installed, head):
        return None
    return due


def upgrade_command(url, ref):
    return ['pipx', 'install', '--force', f'git+{url}@{ref}']


def _out(run, cmd, timeout=60):
    """stdout of ``cmd``, or ``None`` when it fails or cannot start."""
    try:
        p = run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 and isinstance(p.stdout, str) else None


def repo_url(run=subprocess.run):
    """The git url the current install came from (its pipx spec), else install.sh's default."""
    text = _out(run, ['pipx', 'list', '--json'])
    try:
        spec = json.loads(text)['venvs'][PACKAGE_NAME]['metadata']['main_package']['package_or_url']
    except (TypeError, ValueError, KeyError):
        spec = ''
    m = re.match(r'git\+(.+?)(@[^@/]+)?$', spec or '')
    return m.group(1) if m else os.environ.get('ASF_REPO_URL', DEFAULT_REPO_URL)


def remote_head(url, run=subprocess.run, branch='main'):
    text = _out(run, ['git', 'ls-remote', url, f'refs/heads/{branch}'])
    return (text or '').split('\t')[0].strip() or None


def other_ticks(run=subprocess.run, me=None):
    """Pids of the asf tick processes other than this one (and its parent)."""
    me = me if me is not None else {os.getpid(), os.getppid()}
    text = _out(run, ['pgrep', '-f', TICK_PATTERN], timeout=10) or ''
    return [int(x) for x in text.split() if x.isdigit() and int(x) not in me]


def ci_red(url, ref, run=subprocess.run):
    """True when a finished remote CI run on ``ref`` failed; False when green, pending, or not
    knowable (no ``gh``, not a GitHub url) — the check is skipped, never guessed."""
    m = re.search(r'github\.com[/:]([^/]+/[^/]+?)(\.git)?/?$', url)
    if not m:
        return False
    text = _out(run, ['gh', 'run', 'list', '--repo', m.group(1), '--commit', ref,
                      '--limit', '20', '--json', 'conclusion'], timeout=30)
    try:
        runs = json.loads(text) if text else []
    except ValueError:
        return False
    return any((r or {}).get('conclusion') in ('failure', 'timed_out') for r in runs)


_COMMIT_PY = ("import json;from importlib import metadata as m;"
              "t=m.distribution('asf-factory').read_text('direct_url.json') or '{}';"
              "print((json.loads(t).get('vcs_info') or {}).get('commit_id') or '')")


def new_install_commit(run=subprocess.run):
    """The commit the pipx venv now holds — read by that venv's interpreter, since this process
    still has the old package's metadata loaded."""
    venvs = (_out(run, ['pipx', 'environment', '--value', 'PIPX_LOCAL_VENVS']) or '').strip()
    if not venvs:
        return None
    python = os.path.join(venvs, PACKAGE_NAME, 'bin', 'python')
    return (_out(run, [python, '-c', _COMMIT_PY]) or '').strip() or None


def reload_clocks(names, run=subprocess.run):
    """Bootstrap every product clock whose plist is on disk but that launchd has not loaded (an
    install once left the tick clock unloaded). Loaded jobs are never booted out — one of them
    may be the tick running this upgrade. Returns the lines to print."""
    from asf import scheduler
    try:
        cfg = env.load_config()
        if scheduler.kind(cfg) != 'launchd':
            return []
        prefix = scheduler.label_prefix(cfg)
    except env.ConfigError:
        return []
    listed = _out(run, ['launchctl', 'list'])
    if listed is None:
        return ['upgrade: launchctl list failed — clocks not checked']
    loaded = set(scheduler.parse_list(listed))
    lines = []
    for name in names:
        for path in sorted(glob.glob(os.path.join(scheduler.launch_agents_dir(), f'{prefix}.{name}.*.plist'))):
            label = os.path.basename(path)[:-len('.plist')]
            if label in loaded:
                continue
            ok, err = scheduler.bootstrap(path)
            lines.append(f'upgrade: reloaded clock {label}' if ok
                         else f'upgrade: clock {label} is not loaded and bootstrap failed ({err})')
    return lines


def install(ref=None, run=subprocess.run, out=print, owner=None):
    """Reinstall at ``ref`` (default: ``main``'s head). 0 installed and verified, DEFERRED when
    it waits for the next tick, else non-zero. A tick's upgrade (``owner``: its product) that
    defers for other ticks marks itself pending, so the other ticks stop starting; any other
    outcome clears the mark."""
    others = other_ticks(run)
    if others:
        out(f'upgrade: deferred to the next tick — another asf tick is running '
            f'(pid {", ".join(str(p) for p in others)})')
        if owner and ref:
            write_pending(ref, owner)
            out(f'upgrade: pending {ref[:7]} — other ticks wait until it installs')
        return DEFERRED
    rc = _install(ref, run, out)
    if rc == 0:
        record_last(ref)
    if owner:
        clear_pending()
    return rc


def _install(ref, run, out):
    url = repo_url(run)
    ref = ref or remote_head(url, run)
    if not ref:
        out(f'NEEDS OPERATOR: cannot read main\'s head from {url}')
        return 2
    if ci_red(url, ref, run):
        out(f'upgrade: skipped — remote CI is red at {ref[:7]}; the next green head installs')
        return DEFERRED
    cmd = upgrade_command(url, ref)
    out('upgrade: ' + ' '.join(cmd))
    try:
        rc = run(cmd).returncode
    except OSError:
        out('NEEDS OPERATOR: pipx is not on PATH — install pipx, then rerun the install script')
        return 2
    if rc != 0:
        return rc
    got = new_install_commit(run)
    if not got or not (got.startswith(ref) or ref.startswith(got)):
        out(f'upgrade: FAILED — the install is at {(got or "unknown")[:7]}, not {ref[:7]}')
        return 1
    out(f'upgrade: installed {ref[:7]}')
    for line in reload_clocks(products(), run):
        out(line)
    return 0


def products():
    d = os.path.join(env.ASF_HOME, 'products')
    if not os.path.isdir(d):
        return []
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith('.yaml'))


def row(name):
    """``(product, record schema, package, action)`` for one product."""
    try:
        product = env.load_product(name)
    except env.ConfigError as e:
        return (name, '?', str(schema.SCHEMA_VERSION), f'fix the config: {e}')
    versions = [schema.record_version(d) for _label, d in schema.record_dirs(product)]
    versions = [v for v in versions if v is not None]
    record = ','.join(str(v) for v in sorted(set(versions))) or 'none'
    ok, _detail = schema.check(product)
    if ok:
        action = 'none'
    elif any(v > schema.SCHEMA_VERSION for v in versions):
        action = 'record is newer than this package: pipx install the newer asf'
    else:
        action = f'asf schema-migrate --product {name}'
    return (name, record, str(schema.SCHEMA_VERSION), action)


def render(rows):
    head = ('product', 'record schema', 'package', 'action')
    out = ['| ' + ' | '.join(head) + ' |', '| ' + ' | '.join('---' for _ in head) + ' |']
    out += ['| ' + ' | '.join(r) + ' |' for r in rows]
    return '\n'.join(out) + '\n'


def cmd_upgrade(args, run=subprocess.run):
    if not args.skip_pipx:
        rc = install(getattr(args, 'ref', None), run=run, owner=getattr(args, 'owner', None))
        if rc != 0:
            return rc
    print(f'upgrade: package {__version__}, schema {schema.SCHEMA_VERSION}')
    print(render([row(n) for n in products()]), end='')
    return 0


def register(subparsers):
    p = subparsers.add_parser('upgrade', help="reinstall asf at main's head, then the schema check for every product")
    p.add_argument('--skip-pipx', action='store_true', help='only the schema table')
    p.add_argument('--ref', help="the commit to install (default: main's head)")
    p.set_defaults(run=cmd_upgrade)
    return p
