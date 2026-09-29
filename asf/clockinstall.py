"""asf.clockinstall — which install each product clock runs, read from that clock's own plist.

`asf upgrade` reinstalls a pipx venv; a clock runs whatever its plist's ProgramArguments name,
and on a machine that holds a pinned live install beside an editable dev one those are not the
same install (F-0111 P3). This module is the one answer to "what ticks this product, at what
commit, and is it merged" — the doctor's `clock install` row and the upgrade's target set both
read it, and neither guesses.
"""
import dataclasses
import glob
import json
import os
import plistlib
import subprocess
import urllib.parse

from asf import cli
from asf import drift
from asf import env
from asf import scheduler
from asf import snapshot

#: The distribution's pipx venv name; a suffixed install is this plus its ``--suffix``.
DIST = cli.DIST_NAME          # 'asf-factory'

KINDS = ('pinned', 'editable', 'snapshot', 'checkout', 'unknown')


@dataclasses.dataclass
class ClockInstall:
    label: str          # the launchd label, '' when the product has no plist
    kind: str           # one of KINDS
    venv: str           # the pipx venv dir name ('asf-factory', 'asf-factory-live'), else ''
    suffix: str         # the pipx --suffix that venv carries, else ''
    interpreter: str    # ProgramArguments[0] as the plist holds it
    repo: str           # snapshot/checkout: the working tree whose HEAD it runs, else ''
    sha: str            # the commit that install runs, '' when it cannot be told
    why: str            # one phrase: how the kind was told


def read(path):
    """``(argv, env_vars)`` from the plist at ``path`` — ``([], {})`` for a missing, unreadable
    or non-dict plist. The ``SCHEDULER`` section already reports a plist launchd cannot hold, so
    an unreadable plist is a missing one here too (``asf.doctor._job_plist``'s own rule)."""
    try:
        with open(path, 'rb') as f:
            data = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException):
        return [], {}
    if not isinstance(data, dict):
        return [], {}
    argv = data.get('ProgramArguments')
    env_vars = data.get('EnvironmentVariables')
    return (list(argv) if isinstance(argv, list) else [],
            dict(env_vars) if isinstance(env_vars, dict) else {})


def venv_commit(venv_dir):
    """``(sha, editable, repo)`` off the one ``*.dist-info/direct_url.json`` this distribution's
    site-packages holds under ``venv_dir`` — read off disk, no subprocess for a pinned install
    (C14). A pinned install answers ``vcs_info.commit_id``; an editable one (PEP 610 gives it no
    ``vcs_info``) answers the HEAD of the tree its ``dir_info.editable`` url names, through
    :func:`asf.snapshot.head_sha`, which raises :class:`asf.snapshot.SnapshotError` rather than
    answering junk — the one place this reader shells git, at most once per editable venv.
    ``('', False, '')`` for a missing file, a missing dist, unparseable JSON or a tree that is
    gone."""
    pattern = os.path.join(venv_dir, 'lib', 'python*', 'site-packages',
                            f'{DIST.replace("-", "_")}-*.dist-info', 'direct_url.json')
    matches = sorted(glob.glob(pattern))
    if not matches:
        return '', False, ''
    try:
        with open(matches[0], encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return '', False, ''
    if not isinstance(data, dict):
        return '', False, ''
    dir_info = data.get('dir_info') or {}
    if dir_info.get('editable'):
        parsed = urllib.parse.urlparse(data.get('url') or '')
        repo = urllib.parse.unquote(parsed.path) if parsed.scheme == 'file' else ''
        if not repo:
            return '', True, ''
        try:
            return snapshot.head_sha(repo), True, repo
        except snapshot.SnapshotError:
            return '', True, repo
    sha = (data.get('vcs_info') or {}).get('commit_id') or ''
    return sha, False, ''


def venvs_dir(run=subprocess.run):
    """``pipx environment --value PIPX_LOCAL_VENVS``, stripped; ``''`` when ``pipx`` is not on
    PATH or the call fails — every clock then classifies by :func:`classify`'s step 1, 3 or 4,
    and a pinned install reads ``unknown`` with ``why`` saying ``pipx is not on PATH``."""
    try:
        p = run(['pipx', 'environment', '--value', 'PIPX_LOCAL_VENVS'],
                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ''
    return p.stdout.strip() if p.returncode == 0 and isinstance(p.stdout, str) else ''


def _under(path, root):
    """True when ``path`` sits inside the directory tree ``root``."""
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    return rel != '.' and not rel.startswith('..' + os.sep) and rel != '..'


def _interpreter_root(interpreter):
    """The tree a bare interpreter path suggests as its package root when the plist carries no
    ``PYTHONPATH`` — two directories up from the interpreter, the ``<venv>/bin/python`` shape
    every venv this reader otherwise walks already has."""
    return os.path.dirname(os.path.dirname(interpreter)) if interpreter else ''


def classify(argv, env_vars, venvs_dir):
    """One :class:`ClockInstall` from a plist's ``argv``/``env_vars`` alone, first match wins:

    1. ``argv[1]``'s basename is :data:`asf.snapshot.LAUNCHER` → ``snapshot``; ``repo`` is the
       value after ``--repo``, ``sha`` is ``snapshot.current(<the --code-dir value>)[0]`` — the
       sha the launcher last ran, ``''`` when it has not ticked yet.
    2. ``argv[0]`` is under ``venvs_dir`` → the venv is the first path component below it,
       ``suffix`` is that name past :data:`DIST`, and ``sha``/``editable``/``repo`` come from
       :func:`venv_commit`. ``editable`` when that answered editable, else ``pinned``.
    3. ``env_vars['PYTHONPATH']`` (else the interpreter's own package root) is a git working
       tree (:func:`asf.snapshot.is_checkout`) → ``checkout``, ``repo`` that tree, ``sha`` its
       ``HEAD``.
    4. otherwise ``unknown``, ``why`` naming the interpreter that could not be placed.

    Every string field is set with ``or ''`` at the point of assignment, never at the point of
    use. ``label`` is left ``''`` — the caller, :func:`for_product`, knows it."""
    interpreter = argv[0] if argv else ''
    if not argv:
        return ClockInstall('', 'unknown', '', '', '', '', '', 'the plist named no program')

    if len(argv) > 1 and os.path.basename(argv[1]) == snapshot.LAUNCHER:
        repo = ''
        code_dir_val = ''
        for i, a in enumerate(argv):
            if a == '--repo' and i + 1 < len(argv):
                repo = argv[i + 1]
            elif a == '--code-dir' and i + 1 < len(argv):
                code_dir_val = argv[i + 1]
        sha = (snapshot.current(code_dir_val)[0] or '') if code_dir_val else ''
        return ClockInstall('', 'snapshot', '', '', interpreter, repo, sha,
                            f'snapshot launcher of {repo}')

    if venvs_dir and _under(interpreter, venvs_dir):
        rel = os.path.relpath(os.path.abspath(interpreter), os.path.abspath(venvs_dir))
        venv = rel.split(os.sep)[0]
        suffix = venv[len(DIST):] if venv.startswith(DIST) else venv
        sha, editable, repo = venv_commit(os.path.join(venvs_dir, venv))
        kind = 'editable' if editable else 'pinned'
        return ClockInstall('', kind, venv, suffix, interpreter, repo or '', sha or '',
                            f'venv {venv}')

    checkout_root = env_vars.get('PYTHONPATH') or _interpreter_root(interpreter)
    if checkout_root and snapshot.is_checkout(checkout_root):
        try:
            sha = snapshot.head_sha(checkout_root)
        except snapshot.SnapshotError:
            sha = ''
        return ClockInstall('', 'checkout', '', '', interpreter, checkout_root, sha or '',
                            f'checkout {checkout_root}')

    why = ('pipx is not on PATH' if not venvs_dir else
           f'{interpreter} is not a pipx venv, a snapshot launcher or a checkout')
    return ClockInstall('', 'unknown', '', '', interpreter, '', '', why)


def for_product(product_name, cfg=None, run=subprocess.run):
    """One :class:`ClockInstall` per label in :func:`asf.scheduler.product_labels`, sorted by
    label; ``[]`` when there is no plist, and ``[]`` for a scheduler kind that is not
    ``launchd`` (there is no plist to read — the caller says so). :func:`venvs_dir` is read
    once for the whole list, not per label."""
    try:
        cfg = env.load_config() if cfg is None else cfg
        job_kind = scheduler.kind(cfg)
    except env.ConfigError:
        return []
    if job_kind != 'launchd':
        return []
    labels = scheduler.product_labels(product_name, cfg)
    if not labels:
        return []
    vdir = venvs_dir(run)
    out = []
    for label in labels:
        argv, env_vars = read(scheduler.plist_path(label))
        inst = classify(argv, env_vars, vdir)
        inst.label = label
        out.append(inst)
    return out


def _product_names():
    """Every ``products/<name>.yaml`` under ``ASF_HOME``, sorted — the same glob
    :mod:`asf.schema`, :mod:`asf.redact` and :mod:`asf.upgrade` each read privately."""
    d = os.path.join(env.ASF_HOME, 'products')
    if not os.path.isdir(d):
        return []
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith('.yaml'))


def _head(repo, branch):
    """The commit ``origin/<branch>`` names, else ``<branch>``'s own, else ``''`` — the same
    two-ref fallback, in the same order, as :func:`asf.drift.trunk_head`, which takes a
    :class:`asf.env.Product` and so cannot answer for the running package's own tree: there is
    no Product for it. No ``git fetch`` and no ``git ls-remote`` (C8)."""
    for ref in (f'origin/{branch}', branch):
        try:
            p = subprocess.run(['git', '-C', repo, 'rev-parse', '--verify', '-q',
                                f'{ref}^{{commit}}'], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
    return ''


def trunk(cfg=None):
    """``(repo, head)`` — the ASF source checkout on this host and its ``origin/main`` sha, or
    ``('', '')`` when this host has none. :func:`asf.drift.factory_root` first (the running
    package's own tree), else the first configured product whose ``repo_dir`` is the factory's
    source (:func:`asf.drift.is_factory_source`). The ref is read locally: no fetch, no
    ls-remote (C8) — a stale ref cannot make the row silently wrong, because the row prints the
    sha it compared against."""
    root = drift.factory_root()
    if root:
        return root, _head(root, 'main')
    for name in _product_names():
        try:
            product = env.load_product(name)
        except env.ConfigError:
            continue
        repo = product.repo_dir
        if repo and drift.is_factory_source(repo):
            return repo, (drift.trunk_head(product) or '')
    return '', ''


#: :func:`against_trunk`'s memo, per ``(sha, head)`` for the process — cleared by tests in
#: ``setUp``, the way :data:`asf.redact._DEFAULT_CACHE` is.
_TRUNK_CACHE = {}


def against_trunk(sha, repo, head):
    """``(behind, merged)`` for one install's ``sha``:
    ``behind``  — ``int(git -C repo rev-list --count <sha>..<head>)``, or ``None`` when either
                  commit is not in that repo (a sha the host has never fetched);
    ``merged``  — ``git -C repo merge-base --is-ancestor <sha> <head>`` succeeded; ``None`` when
                  it could not be asked.
    Both ``None`` for an empty ``sha``, an empty ``repo`` or an empty ``head``. Memoised per
    ``(sha, head)`` for the run, so two clocks on one install cost one pair."""
    if not sha or not repo or not head:
        return None, None
    key = (sha, head)
    if key in _TRUNK_CACHE:
        return _TRUNK_CACHE[key]
    behind = None
    try:
        p = subprocess.run(['git', '-C', repo, 'rev-list', '--count', f'{sha}..{head}'],
                           capture_output=True, text=True, timeout=30)
        if p.returncode == 0:
            behind = int(p.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        behind = None
    merged = None
    try:
        p = subprocess.run(['git', '-C', repo, 'merge-base', '--is-ancestor', sha, head],
                           capture_output=True, text=True, timeout=30)
        if p.returncode in (0, 1):
            merged = p.returncode == 0
    except (OSError, subprocess.SubprocessError):
        merged = None
    result = (behind, merged)
    _TRUNK_CACHE[key] = result
    return result
