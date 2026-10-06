"""asf.github — the one public client for ``gh``: every call returns a :class:`Result`.

Twenty-odd modules shell out to ``gh`` on their own, and each one decides for itself what a
failure means — most of them turn it into ``[]``, ``{}``, ``''`` or ``None``, a value that reads
exactly like "the host has nothing", so an unreadable PR list could close a card or admit a job.
This module is where that stops: a call either worked (``ok``, ``data``) or its answer is
**Unknown** (``ok`` false, ``reason`` saying why), and a caller can no longer mistake the second
for an empty answer without writing the mistake down.

What every call does, once, here (lifted from :func:`asf.harvest.harvest._gh`, which is now a
shim over :func:`call`):

* :func:`asf.gh_limit.guard` before spawning, :func:`asf.gh_limit.inspect_proc` after — a rate
  limit is never a result: it raises :class:`asf.gh_limit.RateLimited` and the latch stops the
  next call from spending anything;
* :mod:`asf.mutation_guard` — a dry run in progress refuses a mutating call (``pr merge``, ``run
  cancel``, an ``api -X POST`` …) with one ``would …`` line instead of running it;
* the ``--allow-escape-sequences`` retry for a ``gh api`` read whose body holds terminal escapes;
* the hermetic environment (:func:`asf.hermetic.git_env`) — a hook's ``GIT_DIR`` never leaks in;
* a per-call ``timeout`` (60 s for a JSON read, 300 s for anything else — a job log can be long;
  ``None`` waits for ever, which is what the harvest shim keeps); past it the answer is Unknown;
* JSON parsing (``json=True``): unparseable output is Unknown (``reason='bad json'``), never a
  default.

The repository's ``check_clients`` script is the ratchet: no new raw ``gh`` argv site may appear
outside this module (and ``asf.gitops``/``asf.gitpush`` for ``git``); the later migrations lower
its baseline one file group at a time.
"""
import datetime
import json as jsonlib
import subprocess
from dataclasses import dataclass

from asf import config_keys, gh_limit, hermetic, mutation_guard

#: Seconds a JSON read may take (a list, a view, an ``api`` GET); config ``github.json_timeout_s``.
JSON_TIMEOUT_S = 60
#: Seconds any other call may take — a ``run view --log`` of a long job is legitimately slow;
#: config ``github.log_timeout_s``.
LOG_TIMEOUT_S = 300
#: Seconds a short ``gh`` call outside this module may take; config ``github.cmd_timeout_s``.
CMD_TIMEOUT_S = 30
#: How many PRs one ``gh pr list`` asks for; config ``github.pr_list_limit``. ``gh`` stops at its
#: ``--limit`` silently, so a busy repo needs more.
PR_LIST_LIMIT = 300


def cmd_timeout_s():
    """Seconds for a short ``gh`` call: config ``github.cmd_timeout_s``, else :data:`CMD_TIMEOUT_S`."""
    return config_keys.value('github.cmd_timeout_s', CMD_TIMEOUT_S)


def json_timeout_s():
    """Seconds a JSON read may take: config ``github.json_timeout_s``, else :data:`JSON_TIMEOUT_S`."""
    return config_keys.value('github.json_timeout_s', JSON_TIMEOUT_S)


def pr_list_limit(default=PR_LIST_LIMIT):
    """``--limit`` for a ``gh pr list``: config ``github.pr_list_limit``, else ``default``."""
    return config_keys.value('github.pr_list_limit', default)
#: The ``timeout`` default: :data:`JSON_TIMEOUT_S` with ``json=True``, else :data:`LOG_TIMEOUT_S`.
DEFAULT = object()

#: ``gh api`` (2.101 on) refuses to print a response holding terminal escape sequences — a CI
#: job's log, coloured by its tools — unless asked to; an older ``gh`` has no such flag. So the
#: flag is added only on that refusal: a job log read never comes back empty for its colours.
GH_ESCAPES = '--allow-escape-sequences'


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


@dataclass(frozen=True)
class Result:
    """One ``gh`` call's answer. ``ok`` true: ``data`` is the answer (the parsed JSON with
    ``json=True``, else ``stdout``). ``ok`` false: the answer is **Unknown** — ``reason`` says
    why (``'rc 1: <first stderr line>'``, ``'timeout'``, ``'bad json'``, ``'dry run'`` …) and
    ``data`` is ``None``; never read it as "nothing there". ``as_of`` is when the call ended."""
    ok: bool
    data: object = None
    rc: int = -1
    stdout: str = ''
    stderr: str = ''
    as_of: str = ''
    reason: str = ''

    @property
    def unknown(self):
        """True when the answer is Unknown (the call did not produce one)."""
        return not self.ok

    def triple(self):
        """``(rc, stdout, stderr)`` — the shape :func:`asf.harvest.harvest._gh` returns."""
        return self.rc, self.stdout, self.stderr

    def get(self, default=None):
        """``data`` when ``ok``, else ``default`` — the old swallow, but spelled out at the
        call site, so a reader sees that this caller chose to read Unknown as ``default``."""
        return self.data if self.ok else default


def unknown(reason, rc=-1, stdout='', stderr=''):
    """An Unknown :class:`Result` with ``reason``."""
    return Result(False, None, rc, stdout or '', stderr or '', now_iso(), reason)


def escapes_refused(args, rc, err):
    """True when ``gh api`` refused a response for its escape sequences (:data:`GH_ESCAPES`)."""
    return rc != 0 and list(args[:1]) == ['api'] and GH_ESCAPES in (err or '') \
        and GH_ESCAPES not in args


def with_escapes(args):
    return ['api', GH_ESCAPES, *args[1:]]


def _limit(timeout, json):
    if timeout is DEFAULT:
        if json:
            return json_timeout_s()
        return config_keys.value('github.log_timeout_s', LOG_TIMEOUT_S)
    return timeout


def _spawn(run, args, timeout, env):
    kw = {'capture_output': True, 'text': True, 'env': hermetic.git_env(env)}
    if timeout is not None:
        kw['timeout'] = timeout
    return (run or subprocess.run)(['gh', *args], **kw)


def _why(rc, stderr):
    first = next((ln.strip() for ln in (stderr or '').splitlines() if ln.strip()), '')
    return f'rc {rc}: {first}' if first else f'rc {rc}'


def call(args, *, timeout=None, run=None, env=None):
    """Run ``gh <args>`` once (plus the escape-sequence retry): a :class:`Result` whose ``data``
    is ``stdout``. A rate limit raises :class:`asf.gh_limit.RateLimited`; a dry run refuses a
    mutating call (``rc`` 1, ``stderr`` the ``would …`` line, also printed); past ``timeout``
    seconds the result is Unknown (``'timeout'``). A ``gh`` that cannot be spawned raises
    :class:`OSError` here — :func:`gh` turns that into Unknown too. ``run`` replaces
    :func:`subprocess.run` (looked up at call time, so a patched ``subprocess.run`` is honoured)."""
    args = list(args)
    gh_limit.guard(args)
    if mutation_guard.is_active() and mutation_guard.is_mutating_gh(args):
        line = mutation_guard.would_line('gh', args)
        print(line)
        return Result(False, None, 1, '', line, now_iso(), 'dry run')
    try:
        p = _spawn(run, args, timeout, env)
        gh_limit.inspect_proc(args, p)  # a rate limit is never a result
        if escapes_refused(args, p.returncode, getattr(p, 'stderr', '')):
            args = with_escapes(args)
            p = _spawn(run, args, timeout, env)
            gh_limit.inspect_proc(args, p)
    except subprocess.TimeoutExpired:
        return unknown('timeout')
    rc, out, err = p.returncode, p.stdout, p.stderr
    if rc != 0:
        return Result(False, None, rc, out, err, now_iso(), _why(rc, err))
    return Result(True, out, rc, out, err, now_iso(), '')


def gh(args, *, json=False, timeout=DEFAULT, run=None, env=None):
    """``gh <args>`` as a :class:`Result` — the call every new reader uses. ``json=True`` parses
    ``stdout`` (empty output is ``ok`` with ``data`` ``None``; unparseable output is Unknown,
    ``'bad json'``). ``timeout``: seconds, default :data:`JSON_TIMEOUT_S` for a JSON read and
    :data:`LOG_TIMEOUT_S` otherwise; ``None`` waits for ever. A rate limit raises
    :class:`asf.gh_limit.RateLimited` — it is never a result."""
    try:
        r = call(args, timeout=_limit(timeout, json), run=run, env=env)
    except OSError as e:
        return unknown(f'gh not runnable: {e}')
    if not (r.ok and json):
        return r
    if not (r.stdout or '').strip():
        return Result(True, None, r.rc, r.stdout, r.stderr, r.as_of, '')
    try:
        data = jsonlib.loads(r.stdout)
    except ValueError:
        return Result(False, None, r.rc, r.stdout, r.stderr, r.as_of, 'bad json')
    return Result(True, data, r.rc, r.stdout, r.stderr, r.as_of, '')


# --- the readers the migrations move to (each a thin, named call over gh) -------------------

def pr(slug, number, fields, **kw):
    """``gh pr view <number> -R <slug> --json <fields>`` — a dict."""
    return gh(['pr', 'view', str(number), '-R', slug, '--json', ','.join(fields)], json=True, **kw)


def open_prs(slug, limit=None, fields=('number', 'headRefName', 'headRefOid'), **kw):
    """``gh pr list --state open`` for ``slug`` — a list (an empty list is a real "none open").
    ``limit`` defaults to :func:`pr_list_limit`."""
    limit = pr_list_limit() if limit is None else limit
    return gh(['pr', 'list', '-R', slug, '--state', 'open', '--limit', str(limit), '--json',
               ','.join(fields)], json=True, **kw)


def prs(slug, *, state='all', search='', limit=100,
        fields=('number', 'title', 'headRefName', 'state', 'mergedAt'), **kw):
    """``gh pr list --state <state> [--search <search>]`` for ``slug`` — a list, newest first (an
    empty list is a real "none")."""
    args = ['pr', 'list', '-R', slug, '--state', state, '--limit', str(limit), '--json',
            ','.join(fields)]
    if search:
        args += ['--search', search]
    return gh(args, json=True, **kw)


def merge_commit(slug, number, **kw):
    """The sha PR ``number`` merged at, as ``data`` (``''`` when it has not merged)."""
    r = pr(slug, number, ['mergeCommit'], **kw)
    if not r.ok:
        return r
    oid = ((r.data or {}).get('mergeCommit') or {}).get('oid') or ''
    return Result(True, oid, r.rc, r.stdout, r.stderr, r.as_of, '')


def checks(slug, sha, **kw):
    """The check runs on ``sha`` (``commits/<sha>/check-runs``) — the ``check_runs`` list."""
    r = api(f'repos/{slug}/commits/{sha}/check-runs?per_page=100', **kw)
    if not r.ok:
        return r
    if not isinstance(r.data, dict) or not isinstance(r.data.get('check_runs'), list):
        return Result(False, None, r.rc, r.stdout, r.stderr, r.as_of, 'bad json')
    return Result(True, r.data['check_runs'], r.rc, r.stdout, r.stderr, r.as_of, '')


def runs(slug, *, fields=('databaseId', 'status', 'conclusion', 'headSha', 'event'), limit=50,
         **query):
    """``gh run list -R <slug>`` filtered by ``query`` (``workflow``, ``branch``, ``commit``,
    ``status``, ``event`` …, each ``--<key> <value>``) — a list. ``timeout``/``run``/``env`` are
    passed through."""
    kw = {k: query.pop(k) for k in ('timeout', 'run', 'env') if k in query}
    args = ['run', 'list', '-R', slug, '--limit', str(limit), '--json', ','.join(fields)]
    for key, value in query.items():
        if value is not None:
            args += [f'--{key}', str(value)]
    return gh(args, json=True, **kw)


def run_log(slug, run_id, *, failed=True, **kw):
    """A run's log (``--log-failed`` by default) as text — the long read: 300 s by default."""
    return gh(['run', 'view', str(run_id), '-R', slug, '--log-failed' if failed else '--log'],
              **kw)


def api(path, method='GET', fields=None, **kw):
    """``gh api <path>`` (``-X <method>`` and ``-f key=value`` per ``fields``) — parsed JSON."""
    args = ['api', path]
    if method != 'GET':
        args += ['-X', method]
    for key, value in (fields or {}).items():
        args += ['-f', f'{key}={value}']
    return gh(args, json=True, **kw)
