"""asf.upgrade — ``asf upgrade``: reinstall the package at the trunk's head, then the schema check
for every product, one table: product · record schema · package · action.

The install is pinned (``tools/install.sh`` runs ``pipx install --force git+<url>@<sha>``), so
``pipx upgrade`` reinstalls the same pin and changes nothing. The upgrade reinstalls at a named
commit instead: the head the tick's drift check read, or ``main``'s head for a manual run. It is
deferred while another tick runs (a reinstall under a running tick tore it: ImportError
mid-tick), refused on a head whose remote CI is red, verified by the new install's own commit,
and followed by reloading any product clock that is on disk but not loaded.
"""
import glob
import json
import os
import re
import subprocess

from asf import __version__, env, schema
from asf.drift import DEFERRED  # noqa: F401 — the upgrade waits for the next tick (EX_TEMPFAIL)

PACKAGE_NAME = 'asf-factory'
DEFAULT_REPO_URL = 'https://github.com/fmogensen/ASF.git'
#: a tick's command line: ``python -m asf.cli tick …`` (the clocks) or ``…/bin/asf tick …``
TICK_PATTERN = r'asf(\.cli)? tick( |$)'


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


def install(ref=None, run=subprocess.run, out=print):
    """Reinstall at ``ref`` (default: ``main``'s head). 0 installed and verified, DEFERRED when
    it waits for the next tick, else non-zero."""
    others = other_ticks(run)
    if others:
        out(f'upgrade: deferred to the next tick — another asf tick is running '
            f'(pid {", ".join(str(p) for p in others)})')
        return DEFERRED
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
        out('NEEDS OPERATOR: pipx is not on PATH — install pipx, then bash tools/install.sh <product>')
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
        rc = install(getattr(args, 'ref', None), run=run)
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
