"""asf.installs — which build of the factory each product runs: one pipx venv per product and
commit, named ``asf-factory-<product>-<sha7>``, recorded in ``state/<product>/install.json``.

The record is the pin. A product whose record exists runs that venv — its clocks, its hooks and
its sessions' ``asf`` — and nothing a trunk merge does reaches it until ``asf upgrade --product
<p> --to <sha>`` moves it. The record keeps the venv it replaced as ``previous``, still on disk,
so ``asf upgrade --product <p> --rollback`` is a local switch that needs no network.

``install.json``::

    {
      "sha": "<40 hex>",
      "venv": "<absolute venv dir>",
      "previous": {"sha": "<40 hex>", "venv": "<absolute venv dir>"},
      "at": "<iso time>",
      "by": "<who moved it>",
      "policy": "pinned"
    }

The top-level ``venv`` is always written before ``previous``, one key per line: a shell reader
that cannot parse JSON takes the *first* ``"venv":`` line and gets the current venv.

A product with no record (a fresh ``asf install``) runs the shared install, as before; a record
that is missing, unreadable or of an older shape reads as ``None`` — never an error.

This module imports nothing but :mod:`asf.env` at load time: the scheduler and the dispatcher
read it on every render.
"""
import dataclasses
import datetime
import glob
import json
import os
import subprocess

from asf import env

#: the pip distribution (and pipx venv) name — the same as :data:`asf.cli.DIST_NAME`
DIST = 'asf-factory'
RECORD_FILE = 'install.json'
POLICY = 'pinned'
#: how many venvs of one product :func:`prune` keeps (never fewer than the current and previous)
KEEP = 3


@dataclasses.dataclass(frozen=True)
class Install:
    """One product's pin as ``install.json`` holds it."""
    product: str
    sha: str
    venv: str
    previous: dict = None
    at: str = ''
    by: str = ''
    policy: str = POLICY
    #: the release the record wrote at pin time (``0.1.121``); the factory's tags win when read
    version: str = ''

    @property
    def label(self):
        """The pin as the operator reads it: ``0.1.121 (267264dbe)``."""
        from asf import version
        return version.pin_label(self.sha, recorded=self.version or None)

    @property
    def previous_label(self):
        from asf import version
        return version.pin_label(self.previous_sha, recorded=None) if self.previous_sha else '-'

    @property
    def interpreter(self):
        return interpreter(self.venv)

    @property
    def cli_path(self):
        return cli_path(self.venv)

    @property
    def previous_sha(self):
        return (self.previous or {}).get('sha') or ''

    @property
    def previous_venv(self):
        return (self.previous or {}).get('venv') or ''


def record_path(product_name):
    return os.path.join(env.ASF_HOME, 'state', product_name, RECORD_FILE)


def read(product_name):
    """The product's :class:`Install`, or ``None`` — no file, unreadable JSON, or a shape that
    names no ``sha`` and ``venv`` (an older or hand-damaged file is no pin, never an error)."""
    try:
        with open(record_path(product_name), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    sha, venv = data.get('sha'), data.get('venv')
    if not (isinstance(sha, str) and sha and isinstance(venv, str) and venv):
        return None
    prev = data.get('previous')
    if not (isinstance(prev, dict) and isinstance(prev.get('sha'), str)
            and isinstance(prev.get('venv'), str) and prev.get('venv')):
        prev = None
    return Install(product=product_name, sha=sha, venv=venv,
                   previous={'sha': prev['sha'], 'venv': prev['venv']} if prev else None,
                   at=str(data.get('at') or ''), by=str(data.get('by') or ''),
                   policy=str(data.get('policy') or POLICY),
                   version=str(data.get('version') or ''))


def pinned(product_name):
    return read(product_name) is not None


def write(product_name, sha, venv, previous=None, by='', now=None):
    """Write the record atomically, ``venv`` before ``previous`` (see the module doc). Returns
    the :class:`Install` written."""
    at = (now or datetime.datetime.now()).astimezone().isoformat(timespec='seconds')
    prev = ({'sha': previous.get('sha') or '', 'venv': previous['venv']}
            if previous and previous.get('venv') else None)
    from asf import version as versions
    release = versions.of_commit(versions.factory_repo(), sha) or ''
    data = {'sha': sha, 'venv': venv, 'previous': prev, 'at': at, 'by': by, 'policy': POLICY}
    if release:
        data['version'] = release
    path = record_path(product_name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)  # insertion order: sha, venv, previous, …
        f.write('\n')
    os.replace(tmp, path)
    return Install(product=product_name, sha=sha, venv=venv, previous=prev, at=at, by=by,
                   version=release)


# ---- venv names and places ----------------------------------------------------------------

def suffix(product_name, sha):
    """pipx's ``--suffix`` for the product's venv at ``sha``: ``-<product>-<sha7>``."""
    return f'-{product_name}-{sha[:7]}'


def venv_name(product_name, sha):
    return f'{DIST}{suffix(product_name, sha)}'


def venvs_root(run=subprocess.run):
    """Where pipx keeps its venvs: ``$PIPX_HOME/venvs`` when set (pipx's own rule), else
    ``pipx environment --value PIPX_LOCAL_VENVS``, else pipx's default under the home dir."""
    home = os.environ.get('PIPX_HOME')
    if home:
        return os.path.join(home, 'venvs')
    try:
        p = run(['pipx', 'environment', '--value', 'PIPX_LOCAL_VENVS'],
                capture_output=True, text=True, timeout=30)
        text = (p.stdout or '').strip() if p.returncode == 0 and isinstance(p.stdout, str) else ''
    except (OSError, subprocess.SubprocessError):
        text = ''
    return text or os.path.expanduser('~/.local/pipx/venvs')


def venv_dir(product_name, sha, run=subprocess.run, root=None):
    return os.path.join(root or venvs_root(run), venv_name(product_name, sha))


def shared_venv(run=subprocess.run, root=None):
    """The shared, unsuffixed venv every unpinned product runs."""
    return os.path.join(root or venvs_root(run), DIST)


def interpreter(venv):
    return os.path.join(venv, 'bin', 'python')


def cli_path(venv):
    return os.path.join(venv, 'bin', 'asf')


def site_packages(venv):
    """The venv's ``lib/python*/site-packages`` dir, or ``None`` when there is none."""
    found = sorted(glob.glob(os.path.join(venv, 'lib', 'python*', 'site-packages')))
    return found[0] if found else None


def commit_of(venv):
    """The commit ``venv`` was installed from, read off disk (:func:`asf.clockinstall.venv_commit`
    — no subprocess for a pinned install); ``''`` when it cannot be told."""
    from asf import clockinstall  # local: clockinstall imports the scheduler, which imports us
    sha, _editable, _repo = clockinstall.venv_commit(venv)
    return sha or ''


def usable(venv, sha=None):
    """True when ``venv`` holds a runnable ``asf`` (and, given ``sha``, was installed from it)."""
    if not (venv and os.access(cli_path(venv), os.X_OK) and os.path.exists(interpreter(venv))):
        return False
    if not sha:
        return True
    got = commit_of(venv)
    return bool(got) and (got.startswith(sha) or sha.startswith(got))


def on_disk(product_name, sha, run=subprocess.run, root=None):
    """The product's venv at ``sha`` when it is on disk and holds that commit, else ``None``."""
    venv = venv_dir(product_name, sha, run, root)
    return venv if usable(venv, sha) else None


def list_venvs(product_name, run=subprocess.run, root=None):
    """The product's venv dirs on disk, oldest first (by mtime)."""
    pattern = os.path.join(root or venvs_root(run), f'{DIST}-{product_name}-*')
    found = [d for d in glob.glob(pattern) if os.path.isdir(d)
             and len(os.path.basename(d)) == len(f'{DIST}-{product_name}-') + 7]
    return sorted(found, key=lambda d: (os.path.getmtime(d), d))


def prune(product_name, keep=KEEP, run=subprocess.run, root=None, out=print):
    """Uninstall the product's oldest venvs beyond ``keep`` — never the current or the previous
    one. Returns the venv dirs removed."""
    rec = read(product_name)
    protect = {os.path.realpath(v) for v in (rec.venv if rec else '',
                                              rec.previous_venv if rec else '') if v}
    venvs = list_venvs(product_name, run, root)
    removable = [v for v in venvs if os.path.realpath(v) not in protect]
    excess = max(0, min(len(removable), len(venvs) - keep))
    removed = []
    for venv in removable[:excess]:
        name = os.path.basename(venv)
        try:
            rc = run(['pipx', 'uninstall', name], capture_output=True, text=True,
                     timeout=120).returncode
        except (OSError, subprocess.SubprocessError):
            rc = 1
        if rc != 0:
            out(f'installs: could not prune {name} (pipx uninstall exit {rc})')
            continue
        out(f'installs: pruned {name}')
        removed.append(venv)
    return removed
