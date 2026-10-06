"""asf.gitops — the one client for read-only ``git``: every call returns a :class:`Result`.

A raw ``subprocess.run(['git', …])`` that fails returns a code, and most call sites read any
non-zero code as "no": not an ancestor, no such branch, nothing changed. A git that hung, a
repository that is not there, or an object not fetched yet then reads exactly like a definite
answer — and a landing check that reads "no" where it meant "could not tell" files a NEEDS
DECISION row, or reads "no branch left" and closes a card. Here the two are kept apart:

* :func:`git` runs one command with a timeout and the hermetic environment
  (:func:`asf.hermetic.git_env` — a hook's ``GIT_DIR`` never leaks in) and returns the
  :class:`asf.github.Result` shape: ``ok`` with ``data`` (stdout, stripped), or not ``ok`` with
  ``rc`` (``-1``: git never answered — a timeout, a missing directory) and ``reason``;
* the helpers answer the questions the landing checks ask, and each returns ``None`` —
  **Unknown** — when git could not answer, never ``False``/``''``/``0``.

:func:`fetch` is the one write here, to remote-tracking refs; pushing stays in
:mod:`asf.gitpush`. The repository's ``check_clients`` script counts the raw ``git`` argv sites
left outside these modules; each migration lowers its baseline.
"""
import subprocess

from asf import config_keys, hermetic
from asf.github import Result, now_iso

#: Seconds one git read may take before its answer is Unknown (config ``git.timeout_s``).
TIMEOUT_S = 120
#: ``timeout``'s default: the configured :data:`TIMEOUT_S` (or :data:`FETCH_TIMEOUT_S`).
DEFAULT = object()


def timeout_s():
    """Seconds one git call may take: config ``git.timeout_s``, else :data:`TIMEOUT_S`."""
    return config_keys.value('git.timeout_s', TIMEOUT_S)


def fetch_timeout_s():
    """Seconds a fetch may take: config ``git.fetch_timeout_s``, else :data:`FETCH_TIMEOUT_S`."""
    return config_keys.value('git.fetch_timeout_s', FETCH_TIMEOUT_S)


def git(args, cwd, *, timeout=DEFAULT, env=None):
    """Run ``git <args>`` in ``cwd``. ``ok`` with ``data`` = stdout stripped when git exits 0;
    otherwise not ``ok`` — ``rc`` is git's code (a definite answer some helpers read), or ``-1``
    when git never answered (``reason`` ``'timeout'`` or the error). ``timeout`` defaults to
    :func:`timeout_s`."""
    if timeout is DEFAULT:
        timeout = timeout_s()
    try:
        p = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=hermetic.git_env(env))
    except subprocess.TimeoutExpired:
        return Result(False, as_of=now_iso(), reason='timeout')
    except (OSError, ValueError) as e:
        return Result(False, as_of=now_iso(), reason=f'error: {e}')
    out, err = p.stdout or '', p.stderr or ''
    if p.returncode != 0:
        first = (err.strip().splitlines() or [''])[0]
        return Result(False, rc=p.returncode, stdout=out, stderr=err, as_of=now_iso(),
                      reason=f'rc {p.returncode}: {first}' if first else f'rc {p.returncode}')
    return Result(True, data=out.strip(), rc=0, stdout=out, stderr=err, as_of=now_iso())


def rev_parse(cwd, ref):
    """``ref`` resolved to its sha; ``''`` when git says there is no such ref (rc 1); ``None``
    when git could not answer (no repository, a timeout)."""
    r = git(['rev-parse', '--verify', '-q', ref], cwd)
    if r.ok:
        return r.data
    return '' if r.rc == 1 else None


def is_ancestor(cwd, sha, ref):
    """True when ``sha`` is an ancestor of ``ref``, False when it is not, ``None`` when git
    could not tell — an object it does not have (not fetched), no repository, a timeout."""
    r = git(['merge-base', '--is-ancestor', sha, ref], cwd)
    if r.ok:
        return True
    return False if r.rc == 1 else None


def log1(cwd, sha, fmt):
    """``git log -1 --format=<fmt> <sha>``, stripped, or ``None`` when git could not answer."""
    r = git(['log', '-1', f'--format={fmt}', sha], cwd)
    return r.data if r.ok else None


def rev_list_count(cwd, a, b):
    """How many commits ``b`` carries that ``a`` does not (``a..b``), or ``None`` when git could
    not answer (a ref missing, a timeout)."""
    r = git(['rev-list', '--count', f'{a}..{b}'], cwd)
    return int(r.data) if r.ok and r.data.isdigit() else None


def head_ref(branch):
    """The full ref ``git ls-remote --heads origin`` is asked for: ``refs/heads/<branch>``. A
    bare ``<branch>`` pattern is matched by git against the *tail* of every ref, so
    ``lane/x`` also answers ``refs/heads/archive/lane/x`` — which sorts first (2026-10-04:
    the archive step's ``archive/<branch>`` was read as ``origin/<branch>``, and the Stop
    gate held a session whose head origin already had as "1 unpushed commit")."""
    return f'refs/heads/{branch}'


def head_sha(ls_remote_out, branch):
    """The sha ``ls-remote`` output names for exactly ``refs/heads/<branch>`` — ``''`` when no
    line is that ref. Pure: a line for any other ref (``refs/heads/x/<branch>``) is never it."""
    want = head_ref(branch)
    for line in (ls_remote_out or '').splitlines():
        sha, _, ref = line.strip().partition('\t')
        if ref.strip() == want and sha:
            return sha.strip()
    return ''


#: Seconds a fetch may take (config ``git.fetch_timeout_s``).
FETCH_TIMEOUT_S = 120


def fetch(cwd, remote='origin', ref=None, *, timeout=DEFAULT):
    """``git fetch -q <remote> [<ref>]`` — the one write here, to remote-tracking refs only. The
    :class:`Result`; not ``ok`` (offline, no such remote, a timeout) leaves the refs as they
    were, and a caller reads what it then cannot find as Unknown."""
    if timeout is DEFAULT:
        timeout = fetch_timeout_s()
    return git(['fetch', '-q', remote, *([ref] if ref else [])], cwd, timeout=timeout)
