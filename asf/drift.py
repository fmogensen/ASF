"""asf.drift — which version of itself the factory is running, against the trunk's (B-0086).

The scheduled tick runs the *installed* package; the product repo is another copy, and a fix
that lands on the trunk is not in the running factory until ``asf upgrade`` reinstalls it.
Nothing used to compare them. :func:`check` does, for the one product whose repo is the
factory's own source: the commit the install was built from (pip's ``direct_url.json`` — what
``pipx install git+<repo>`` records — or, running from a checkout, that checkout's ``HEAD``)
against the trunk's head. The tick prints :func:`line` first; ``asf doctor`` has a row.
"""
import dataclasses
import json
import os
import re
import subprocess
import time
from importlib import metadata

from asf import __version__

PACKAGE_NAME = 'asf-factory'
#: ``asf.upgrade.DEFERRED``: the upgrade waits for the next tick
DEFERRED = 75
#: A trunk change under these paths changes what an install runs.
PACKAGE_PATHS = ('asf/', 'pyproject.toml')


def _git(repo, *argv):
    p = subprocess.run(['git', '-C', repo, *argv], capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        raise subprocess.CalledProcessError(p.returncode, argv, p.stdout, p.stderr)
    return p.stdout.strip()


def installed_commit():
    """The commit the running package was built from, or ``None`` when it cannot be told (an
    install from a plain directory or an index records none)."""
    try:
        text = metadata.distribution(PACKAGE_NAME).read_text('direct_url.json')
        commit = ((json.loads(text) if text else {}).get('vcs_info') or {}).get('commit_id')
        if commit:
            return commit
    except (metadata.PackageNotFoundError, ValueError, OSError):
        pass
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.exists(os.path.join(root, '.git')):  # a linked worktree (a clock snapshot) too
        try:
            return _git(root, 'rev-parse', 'HEAD')
        except (subprocess.SubprocessError, OSError):
            pass
    return None


def is_factory_source(repo):
    """True when ``repo`` holds the factory's own source (its ``pyproject.toml`` names the package)."""
    try:
        with open(os.path.join(repo, 'pyproject.toml'), encoding='utf-8') as f:
            return re.search(rf'^name\s*=\s*"{PACKAGE_NAME}"', f.read(), re.M) is not None
    except OSError:
        return False


def trunk_head(product):
    """The trunk's head sha in the product repo: ``origin/<main>`` when it has one, else ``<main>``."""
    for ref in (f'origin/{product.main}', product.main):
        try:
            return _git(product.repo_dir, 'rev-parse', '--verify', '-q', f'{ref}^{{commit}}')
        except (subprocess.SubprocessError, OSError):
            continue
    return None


def _trunk_version(repo, head):
    try:
        m = re.search(r'^__version__\s*=\s*["\']([^"\']+)', _git(repo, 'show', f'{head}:asf/__init__.py'), re.M)
    except (subprocess.SubprocessError, OSError):
        return __version__
    return m.group(1) if m else __version__


@dataclasses.dataclass
class Drift:
    version: str
    installed: str          # sha the install was built from, or None when unknown
    head: str
    behind: int             # commits on the trunk the install lacks, or None when unknown
    package_changed: bool   # …and any of them touched the package
    trunk_version: str

    @property
    def is_behind(self):
        return bool(self.behind)

    @property
    def old(self):
        return f'{self.version}@{(self.installed or "unknown")[:7]}'

    @property
    def new(self):
        return f'{self.trunk_version}@{self.head[:7]}'


def check(product, installed=None):
    """The :class:`Drift` of the running install against ``product``'s trunk, or ``None`` when the
    product's repo is not the factory's source (or has no trunk to read)."""
    repo = product.repo_dir
    if not repo or not os.path.isdir(repo) or not is_factory_source(repo):
        return None
    head = trunk_head(product)
    if head is None:
        return None
    installed = installed or installed_commit()
    behind, changed = None, False
    if installed:
        try:
            behind = int(_git(repo, 'rev-list', '--count', f'{installed}..{head}'))
            names = _git(repo, 'diff', '--name-only', installed, head).splitlines()
            changed = any(n == p or n.startswith(p) for n in names for p in PACKAGE_PATHS)
        except (subprocess.SubprocessError, OSError, ValueError):
            behind = None
    return Drift(__version__, installed, head, behind, changed, _trunk_version(repo, head))


def line(d):
    """The tick's first line: the running version, the trunk's head, and how far apart."""
    tail = ('installed commit unknown' if d.behind is None
            else f'BEHIND by {d.behind} commits' if d.behind else 'current')
    return f'factory: asf {d.version} @ {(d.installed or "?")[:7]} · trunk {d.head[:7]} · {tail}'


def report(product, out=print, autonomy='human-now', upgrade=None):
    """Print :func:`line` and, when the trunk changed the package, ``UPGRADE AVAILABLE``; under
    ``autonomy == 'auto'`` run ``upgrade(head)`` (``asf upgrade --ref <head>``) and say so. Returns the :class:`Drift`
    (``None`` for a product that is not the factory)."""
    try:
        d = check(product)
    except Exception as e:  # noqa: BLE001 — a drift check never stops a tick
        out(f'factory: version check failed ({str(e).strip() or type(e).__name__})')
        return None
    if d is None:
        return None
    out(line(d))
    if d.is_behind and d.package_changed:
        out(f'UPGRADE AVAILABLE {d.old} → {d.new}')
        if autonomy == 'auto' and upgrade is not None:
            from asf import upgrade as upgrading  # local: asf.upgrade imports this module
            due = upgrading.batch_hold(product.repo_dir, d.installed, d.head)
            if due is not None:
                out(f'upgrade due at {time.strftime("%H:%M", time.localtime(due))} (batching)')
                return d
            rc = upgrade(d.head)
            if rc != DEFERRED:  # a deferred upgrade said why itself and is due again next tick
                out(f'tick: ran asf upgrade ({d.old} → {d.new}), exit {rc}'
                    + ('; the next tick runs the new one' if not rc else ''))
    return d
