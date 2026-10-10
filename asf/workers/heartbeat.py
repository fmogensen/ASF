"""asf.workers.heartbeat — a run proves it is moving by a push every few minutes; one that stops
is ended and continued from its branch head in the same tick. Every runtime: local, actions,
claude-remote.

A run that stays alive but stops making progress — idle, waiting on a prompt nobody answers, its
host gone — used to be caught only by its lane's time limit (``cloud.timeout_min``, 240) or, for a
local run, by its log going silent. The heartbeat makes the run say it is alive in the one place
the factory can read for every runtime at once: origin.

**The beat** (:func:`brief_lines`, in every brief launched with the heartbeat on). Once, at its
start, the session starts a small background loop (:data:`SCRIPT`) — not the model's memory —
that every ``heartbeat_min`` minutes snapshots the worktree (committed and uncommitted work alike,
as one commit on top of ``HEAD``) with ``NOTES.asf.md`` — what is done, what is next, open
questions — as the commit message and the ``ASF-Session`` trailer, and force-pushes it to
``refs/asf/hb/<job>``. A ref outside ``refs/heads/`` starts no CI, opens no PR and is no push of
work (the session's hook shim passes it untouched: :mod:`asf.workers.githooks`). The loop pushes
with a lease on the sha it pushed last: once origin's ref is anything else — the factory deleted
it, a continuation beat over it — the loop exits. That is the fence for the beat; a pushed
commit carrying the old ``ASF-Session`` is the fence for the work (below).

**Detection** (:class:`Beats`, :func:`observe` — no model, no API): one ``git ls-remote origin``
per health pass reads every live run's branch and beat ref. A run *moved* when either sha differs
from the one the last pass saw (``<state>/heartbeat.json``); the clock is the newest of that
change, and the run's start plus ``heartbeat_grace_min`` (setup time before the first beat). A
run is STALLED when ``now - moved > heartbeat_min * heartbeat_missed`` (defaults 5 and 2). An
unreadable origin is no answer: nothing stalls on it. A run launched without the rule (before
this release, or with no interval recorded) is never judged.

**Settle and continue** (:func:`resume`): the stalled run is stopped (a local process group
signalled, a routine disabled, a workflow run cancelled), its last beat fetched — its notes, and
for a run off this host its snapshot fast-forwarded onto the branch as a ``wip:`` commit so the
work is on the branch the continuation starts from — and the beat ref deleted. The run ends
``failed: stalled …`` and the same job is launched again at once on the same branch, the same
runtime and account, under a fresh ``ASF-Session``: the original brief plus a CONTINUE block (the
branch head, the notes, a summary of the dead run's last events — its log, or the routine's run
log). ``heartbeat_resumes`` (default 2) caps the continuations of one chain; past it the run is
only ended dead and health's own path takes it.

**Fencing.** The continuation's id is the only one harvest and the report check accept
(:func:`asf.workers.cloud.report_commit` matches the run's own session). A push of the old
session that reaches the branch after the relaunch is logged ``zombie push`` and never counts as
the new run's movement.

The lane's time limit stays the backstop: a run that keeps beating but loops still ends
``timed out``.

Config (``~/.ASF/config.yaml``; the ``cloud:`` block's keys — the operator's, or a product
file's — over it for a cloud-lane run)::

    workers:
      heartbeat_min: 5          # minutes between beats
      heartbeat_missed: 2       # beats missed before a run is stalled
      heartbeat_grace_min: 10   # setup time before the first beat is due
      heartbeat_resumes: 2      # continuations of one chain (0: end it dead, no continuation)
"""
import calendar
import dataclasses
import json
import os
import re
import subprocess
import time
import types

from asf import env

DEFAULT_MIN = 5
DEFAULT_MISSED = 2
DEFAULT_GRACE_MIN = 10
DEFAULT_RESUMES = 2
KEYS = ('heartbeat_min', 'heartbeat_missed', 'heartbeat_grace_min', 'heartbeat_resumes')
REF_PREFIX = 'refs/asf/hb/'
NOTES_FILE = 'NOTES.asf.md'
STATE_FILE = 'heartbeat.json'
STALLED = 'stalled'
#: the end reason of a stalled run (the ledger's ``end_reason``)
END_PREFIX = f'failed: {STALLED}'
#: the run-log summary a continuation is handed, at most
SUMMARY_MAX = 2048
NOTES_MAX = 4096
CONTINUE_HEAD = '\n\nCONTINUE'


# ---- config -----------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Settings:
    interval_min: float = DEFAULT_MIN
    missed: float = DEFAULT_MISSED
    grace_min: float = DEFAULT_GRACE_MIN
    resumes: int = DEFAULT_RESUMES

    @property
    def limit_min(self):
        """Minutes without movement after which a run is stalled."""
        return self.interval_min * self.missed


def _num(v):
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def config_problems(block, prefix='workers'):
    """``[(dotted key, problem)]`` for the heartbeat keys of a ``workers:`` or ``cloud:`` block:
    each a number above 0 (``heartbeat_missed`` at least 1; ``heartbeat_resumes`` 0 or more)."""
    if not isinstance(block, dict):
        return [] if block is None or prefix != 'workers' else [(prefix, f'must be a map, not {block!r}')]
    out = []
    for key in KEYS:
        if key not in block:
            continue
        v, n = block[key], _num(block[key])
        if key == 'heartbeat_resumes':
            if n is None or n < 0 or n != int(n):
                out.append((f'{prefix}.{key}', f'must be a whole number, 0 or more, not {v!r}'))
        elif key == 'heartbeat_missed':
            if n is None or n < 1:
                out.append((f'{prefix}.{key}', f'must be a number, 1 or more, not {v!r}'))
        elif n is None or n <= 0:
            out.append((f'{prefix}.{key}', f'must be a number above 0, not {v!r}'))
    return out


def settings(cfg, product=None, lane='local'):
    """:class:`Settings` for a run of ``lane`` (``local`` | ``cloud``): the operator's
    ``workers:``, then for the cloud lane the ``cloud:`` block's heartbeat keys over it (the
    operator's, the product file's ``cloud:`` over that). An unreadable value is the default
    (the config check names it)."""
    w = (cfg or {}).get('workers')
    c = dict(w) if isinstance(w, dict) else {}
    if lane == 'cloud':
        from asf.workers import cloud  # local: cloud imports this module
        c.update({k: v for k, v in cloud.raw(cfg, product).items() if k in KEYS})
    def get(key, default):  # each key alone: one bad key never discards the good ones
        return _num(c[key]) if key in c and not config_problems({key: c[key]}) \
            else float(default)
    return Settings(interval_min=get('heartbeat_min', DEFAULT_MIN),
                    missed=get('heartbeat_missed', DEFAULT_MISSED),
                    grace_min=get('heartbeat_grace_min', DEFAULT_GRACE_MIN),
                    resumes=int(get('heartbeat_resumes', DEFAULT_RESUMES)))


def for_run(run, cfg, product=None):
    """The settings a live run is judged by: the interval it was launched with (the brief told
    it), the rest from config now. None for a run launched without the heartbeat."""
    told = _num((run or {}).get('heartbeat_min'))
    if not told or told <= 0:
        return None
    lane = 'cloud' if (run or {}).get('runtime_lane') == 'cloud' else 'local'
    return dataclasses.replace(settings(cfg, product, lane), interval_min=told)


def ref(job):
    """``refs/asf/hb/<job>``: the run's beat ref on origin."""
    return f'{REF_PREFIX}{job}'


# ---- the brief --------------------------------------------------------------------------------

#: The beat loop: ``sh <script> <seconds> <ref> <ASF-Session>``. Every ``<seconds>`` it commits the
#: worktree (tracked and untracked files, ``NOTES.asf.md`` as the message) on top of HEAD without
#: touching the index or the branch, and force-pushes it to ``<ref>`` with a lease on its own last
#: push; once origin holds anything else there it exits.
SCRIPT = r'''#!/bin/sh
secs=$1; ref=$2; sid=$3; last=
while :; do
  idx=$(mktemp) || exit 0
  GIT_INDEX_FILE=$idx git read-tree HEAD && GIT_INDEX_FILE=$idx git add -A
  tree=$(GIT_INDEX_FILE=$idx git write-tree)
  { echo "wip: heartbeat $(date -u +%Y-%m-%dT%H:%M:%SZ)"; echo; cat NOTES.asf.md 2>/dev/null
    echo; echo "ASF-Session: $sid"; } > "$idx.msg"
  c=$(git commit-tree "$tree" -p HEAD -F "$idx.msg")
  rm -f "$idx" "$idx.msg"
  if [ -n "$c" ]; then
    if [ -z "$last" ]; then lease=--force; else lease="--force-with-lease=$ref:$last"; fi
    if git push -q $lease origin "$c:$ref" 2>/dev/null; then last=$c
    elif seen=$(git ls-remote origin "$ref" 2>/dev/null); then
      [ "$(printf '%s' "$seen" | cut -f1)" = "$last" ] || exit 0
    fi
  fi
  sleep "$secs"
done
'''


def brief_lines(job_name, sid, s, runtime_liveness=False):
    """The HEARTBEAT block of a brief: the notes file, and the one command that starts the
    beat loop in the background. Pure (golden-tested). ``runtime_liveness``: a runtime that
    reports the session's liveness itself (claude-remote: its ``worker_status``), whose
    session refuses a background process as persistence — told to start none."""
    secs = max(1, int(round(s.interval_min * 60)))
    r = ref(job_name)
    every = f'{s.interval_min:g} minute' + ('' if s.interval_min == 1 else 's')
    if runtime_liveness:
        return [
            '', 'HEARTBEAT',
            '- Liveness comes from the runtime: the factory reads this session\'s status '
            'itself. Do NOT start a detached or background process (no loop, nothing left '
            'running after its command returns), for a heartbeat or anything else.',
            f'- Commit and push your work on your branch as each piece stands: a run '
            f'that is neither running nor moving for {s.limit_min:g} minutes is ended and '
            'continued by a new session from your branch.',
        ]
    return [
        '', 'HEARTBEAT',
        f'- Every {every} this session proves it is moving: a background loop pushes your work '
        f'in progress and `{NOTES_FILE}` to `{r}` on origin. A run with no beat for '
        f'{s.limit_min:g} minutes is ended and continued by a new session from your branch and '
        'your notes.',
        '- As your very first command, start it exactly like this (one Bash call, in the '
        'background):',
        '```sh',
        f'mkdir -p "$(git rev-parse --git-common-dir)/info" && echo {NOTES_FILE} >> '
        '"$(git rev-parse --git-common-dir)/info/exclude"',
        "cat > \"$(git rev-parse --git-dir)/asf-heartbeat.sh\" <<'ASF_HB'",
        *SCRIPT.rstrip('\n').splitlines(),
        'ASF_HB',
        f'nohup sh "$(git rev-parse --git-dir)/asf-heartbeat.sh" {secs} {r} \'{sid}\' '
        '>/dev/null 2>&1 &',
        '```',
        f'- Keep `{NOTES_FILE}` (never committed) current as you work: what is done, what is next, '
        'open questions. It is what a continuation of this session starts from.',
    ]


# ---- detection --------------------------------------------------------------------------------

def _git(args, cwd, timeout=120):
    """``git <args>`` through :func:`asf.gitops.git`, as a :class:`subprocess.CompletedProcess`."""
    from asf import gitops
    r = gitops.git(list(args), cwd, timeout=timeout)
    rc = 0 if r.ok else (r.rc if isinstance(getattr(r, 'rc', None), int) and r.rc else 1)
    return subprocess.CompletedProcess(list(args), rc, getattr(r, 'stdout', '') or '',
                                       getattr(r, 'stderr', '') or getattr(r, 'reason', '') or '')


def refs_of(run):
    """The two refs on origin a run moves: its branch and its beat ref."""
    out = [ref(run.get('job'))]
    if run.get('branch'):
        out.insert(0, f"refs/heads/{run['branch']}")
    return out


class Beats:
    """origin's sha of every live run's branch and beat ref, read with one ``git ls-remote`` for
    the pass (:meth:`read`, lazily, once). ``None`` from :meth:`get` while origin is
    unreadable."""

    def __init__(self, product, runs=None, ls_remote=None):
        self.product = product
        self._runs = runs
        self._ls = ls_remote
        self._data = False
        self.calls = 0

    def read(self):
        if self._data is not False:
            return self._data
        from asf.workers import pool as pool_mod
        runs = self._runs if self._runs is not None else [
            r for r in pool_mod.live_sessions(self.product) if _num(r.get('heartbeat_min'))]
        want = sorted({x for r in runs for x in refs_of(r)})
        self.calls += 1
        if not want:
            self._data = {}
            return self._data
        if self._ls is not None:
            self._data = self._ls(want)
            return self._data
        p = _git(['ls-remote', 'origin', *want], getattr(self.product, 'repo_dir', None))
        if p.returncode != 0:
            self._data = None
            return None
        got = {}
        for line in p.stdout.splitlines():
            sha, _, name = line.partition('\t')
            if name in want:
                got[name.strip()] = sha.strip()
        self._data = got
        return got

    def get(self, name):
        data = self.read()
        return None if data is None else data.get(name, '')


def _state_path(product):
    try:
        return os.path.join(env.state_dir(product), STATE_FILE)
    except (OSError, AttributeError, TypeError):
        return None


def load_state(product):
    path = _state_path(product)
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(product, data):
    path = _state_path(product)
    if not path:
        return
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, sort_keys=True, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass


def parse_ts(text):
    try:
        return float(calendar.timegm(time.strptime(str(text), '%Y-%m-%dT%H:%M:%SZ')))
    except (TypeError, ValueError):
        return None


def judge(started, moved, now, s):
    """``(stalled, minutes since the last movement)``. ``started`` the run's start (epoch s),
    ``moved`` the last movement seen (or None). The first beat is due ``grace_min`` after the
    start. Pure."""
    base = (started or now) + s.grace_min * 60
    last = max(base, moved or 0)
    quiet = (now - max(started or now, moved or 0)) / 60.0
    return (now - last) / 60.0 > s.limit_min, quiet


def _trailer(body, key):
    for ln in reversed((body or '').strip().splitlines()):
        k, sep, v = ln.partition(':')
        if not sep or ' ' in k.strip():
            break
        if k.strip().lower() == key.lower():
            return v.strip()
    return None


def _head_session(product, run, sha):
    """The ``ASF-Session`` trailer of commit ``sha`` on the run's branch (fetched), or None."""
    cwd = run.get('worktree') if run.get('worktree') and os.path.isdir(run['worktree']) \
        else getattr(product, 'repo_dir', None)
    _git(['fetch', '-q', 'origin', run.get('branch') or ''], cwd)
    p = _git(['log', '-n', '1', '--format=%B', sha], cwd)
    return _trailer(p.stdout, 'ASF-Session') if p.returncode == 0 else None


def observe(product, run, beats, now, s, out=print, state=None):
    """``(stalled, minutes quiet, line)`` for one live run, its movement recorded in
    ``<state>/heartbeat.json``. ``(False, None, …)`` while origin is unreadable."""
    job, sid = run.get('job'), run.get('session') or ''
    data = state if state is not None else load_state(product)
    rec = data.get(job) if isinstance(data.get(job), dict) else {}
    if rec.get('session') != sid:  # a new run of the job: a fresh record
        rec = {'session': sid}
    names = refs_of(run)
    shas = {n: beats.get(n) for n in names}
    if any(v is None for v in shas.values()):
        return False, None, 'origin unreadable: not judged'
    seen = rec.get('shas') if isinstance(rec.get('shas'), dict) else None
    moved = _num(rec.get('moved'))
    line = ''
    if seen is None:
        if shas.get(ref(job)):
            moved = now  # a beat is there at first sight: benefit of the doubt
    else:
        for n, sha in shas.items():
            if sha == seen.get(n) or not sha:
                continue
            if n.startswith('refs/heads/') and run.get('resumed_from') \
                    and _head_session(product, run, sha) == run['resumed_from']:
                if rec.get('zombie') != sha:
                    line = (f'zombie push {job}: {sha[:9]} on {run.get("branch")} carries the '
                            f'stalled session {run["resumed_from"]} — ignored')
                    out(f'heartbeat {line}')
                    rec['zombie'] = sha
                continue
            moved = now
    rec.update(shas=shas, moved=moved, seen=now)
    data[job] = rec
    if state is None:
        _save_state(product, data)
    stalled, quiet = judge(parse_ts(run.get('started')), moved, now, s)
    return stalled, quiet, line


# ---- settle -----------------------------------------------------------------------------------

def _guard():
    from asf import refguard
    return refguard.Guard()


def last_beat(cwd, job):
    """``{sha, notes, parent}`` of the run's last beat on origin (fetched), or None."""
    if not cwd or not os.path.isdir(cwd):
        return None
    r = ref(job)
    if _git(['fetch', '-q', 'origin', f'+{r}:{r}'], cwd).returncode != 0:
        return None
    p = _git(['log', '-n', '1', '--format=%H%x1f%P%x1f%B', r], cwd)
    if p.returncode != 0 or not p.stdout.strip():
        return None
    sha, parent, body = (p.stdout.split('\x1f') + ['', ''])[:3]
    lines = body.strip().splitlines()
    if lines and lines[0].startswith('wip: heartbeat'):
        lines = lines[1:]
    lines = [ln for ln in lines if not ln.startswith('ASF-Session:')]
    return {'sha': sha.strip(), 'parent': parent.strip().split(' ')[0],
            'notes': '\n'.join(lines).strip()[:NOTES_MAX]}


def land_snapshot(cwd, branch, beat):
    """Fast-forward origin's ``branch`` onto the beat's snapshot when it holds work the branch
    lacks — the ``wip:`` commit the continuation starts from. ``(sha, line)``: the branch head
    after, and what was done."""
    if not beat or not branch:
        return None, 'no beat'
    _git(['fetch', '-q', 'origin', branch], cwd)
    head = _git(['rev-parse', '-q', '--verify', f'refs/remotes/origin/{branch}'], cwd).stdout.strip()
    if head and _git(['merge-base', '--is-ancestor', head, beat['sha']], cwd).returncode != 0:
        return head, 'the branch moved past the last beat: left as it is'
    tree = lambda c: _git(['rev-parse', f'{c}^{{tree}}'], cwd).stdout.strip()  # noqa: E731
    if head and tree(head) == tree(beat['sha']):
        return head, 'nothing beyond the branch head'
    from asf import gitpush
    p = gitpush.push(['-q', 'origin', f"{beat['sha']}:refs/heads/{branch}"], cwd, refs_only=True,
                     guard=_guard())
    if p.returncode != 0:
        return head, f'the snapshot was not pushed ({(p.stderr or "").strip()[:120]})'
    _git(['fetch', '-q', 'origin', branch], cwd)
    return beat['sha'], f'snapshot {beat["sha"][:9]} pushed onto {branch}'


def delete_ref(cwd, job):
    """Delete the run's beat ref on origin. True when origin no longer holds it."""
    from asf import gitpush
    if not cwd or not os.path.isdir(cwd):
        return False
    return gitpush.push(['-q', 'origin', f':{ref(job)}'], cwd, refs_only=True,
                        guard=_guard()).returncode == 0


def summarize_events(records, limit=SUMMARY_MAX):
    """A short, newest-last account of a run's last events — tool calls, errors, its last text —
    from stream-json records (a local log) or a run log's events. At most ``limit`` chars."""
    lines = []
    for rec in records or ():
        if not isinstance(rec, dict):
            continue
        msg = rec.get('message') if isinstance(rec.get('message'), dict) else rec
        content = msg.get('content') if isinstance(msg, dict) else None
        if isinstance(content, list):
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get('type') == 'tool_use':
                    lines.append(f"tool {c.get('name')}: {_short(c.get('input'))}")
                elif c.get('type') == 'tool_result' and c.get('is_error'):
                    lines.append(f"error: {_short(c.get('content'))}")
                elif c.get('type') == 'text' and str(c.get('text') or '').strip():
                    lines.append(f"said: {_short(c.get('text'))}")
            continue
        kind = str(rec.get('type') or rec.get('event_type') or rec.get('kind') or '')
        if kind in ('', 'system') and not rec.get('error'):
            continue
        text = rec.get('error') or rec.get('text') or rec.get('summary') or rec.get('name') \
            or rec.get('subtype') or ''
        if text or kind:
            lines.append(f'{kind}: {_short(text)}'.strip(': '))
    out, size = [], 0
    for ln in reversed(lines):
        if size + len(ln) + 1 > limit:
            break
        out.append(ln)
        size += len(ln) + 1
    return '\n'.join(reversed(out))


def _short(v, n=160):
    if isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False)
    v = ' '.join(str(v or '').split())
    return v if len(v) <= n else v[:n - 1] + '…'


def log_records(path, n=200):
    """The last ``n`` JSON records of a run log."""
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            lines = f.readlines()[-n:]
    except (OSError, TypeError):
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def log_alive_at(path, now, limit_min):
    """The epoch of the job log's newest event when it is at most ``limit_min`` minutes old —
    its last record's ``timestamp``, failing that the file's mtime — else None (no log, an
    unreadable one, or a quiet one). A local run streaming events is working: no beat needed."""
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            tail = f.read().decode('utf-8', errors='replace')
            mtime = os.fstat(f.fileno()).st_mtime
    except (OSError, TypeError, ValueError):
        return None
    from asf.workers import remote  # local: the cloud lane's timestamp parser
    ts = None
    lines = [ln for ln in tail.splitlines() if ln.strip()]
    if lines:
        try:
            last = json.loads(lines[-1])
        except ValueError:
            last = None
        if isinstance(last, dict) and last.get('timestamp'):
            ts = remote._event_ts(last.get('timestamp'))
    if ts is None:
        ts = mtime
    if now - ts > limit_min * 60.0:
        return None
    return min(ts, now)


def continue_text(original, why, branch, head, notes, summary, local):
    """The continuation's brief: the original, then the CONTINUE block. Pure."""
    where = ('Your worktree is the stalled run\'s own: its commits and the files it left '
             'uncommitted are there.' if local else
             f'Check `{branch}` out from origin as above: it holds the stalled run\'s pushed '
             'work and its last work-in-progress snapshot.')
    lines = [CONTINUE_HEAD.strip('\n'),
             f'- This session continues a run of the same job that stopped beating ({why}). '
             'Pick up where it stopped; never redo what the branch already holds.',
             f'- The branch `{branch}` head is `{(head or "unknown")[:12]}`. {where}',
             '- Its pushes from now on are ignored: only commits carrying your own '
             '`ASF-Session` count, and only your report commit ends this job.']
    lines.append(f'- Its notes ({NOTES_FILE} at its last beat):')
    lines += [f'    {ln}' for ln in (notes or '(none were pushed)').splitlines()]
    lines.append('- What it was doing last (its run log, newest last):')
    lines += [f'    {ln}' for ln in (summary or '(no events recorded)').splitlines()]
    return str(original or '').rstrip('\n') + '\n\n' + '\n'.join(lines) + '\n'


def _continue_path(brief):
    base = brief[:-3] if str(brief).endswith('.md') else str(brief)
    base = re.sub(r'\.continue\d*$', '', base)
    n = 1
    while os.path.exists(f'{base}.continue{n}.md'):
        n += 1
    return f'{base}.continue{n}.md'


# ---- the continuation -------------------------------------------------------------------------

def _account(name, cfg):
    from asf.workers import pool as pool_mod
    if not name:
        return None
    for a in pool_mod.accounts_from_config(cfg):
        if a.name == name:
            return a
    return pool_mod.Account(name)


def launch(product, run, why, runtime, cfg, notes='', summary='', head=None, out=print):
    """End ``run`` (stalled) and launch its continuation on ``runtime``: the same job, branch,
    worktree and account, a fresh ``ASF-Session``. Returns the new ledger record, or None when
    the continuation could not start (the run is then left to health's own dead path)."""
    from asf import prepush
    from asf.workers import githooks
    from asf.workers import lifecycle
    from asf.workers import pool as pool_mod
    from asf.workers import pushlog
    from asf.workers import refusals
    from asf.workers import runtime as runtime_mod
    from asf.workers import spawn as spawn_mod
    from asf.workers import stopgate
    job_name = run['job']
    local = getattr(runtime, 'lane', 'local') != 'cloud'
    try:
        with open(run['brief'], encoding='utf-8') as f:
            original = f.read()
    except (OSError, KeyError, TypeError) as e:
        out(f'heartbeat {job_name}: no continuation — its brief is unreadable ({e})')
        return None
    original = original.split(CONTINUE_HEAD, 1)[0]
    path = _continue_path(run['brief'])
    with open(path, 'w', encoding='utf-8') as f:
        f.write(continue_text(original, why, run.get('branch'), head, notes, summary, local))
    started = pool_mod.now_iso()
    while started <= str(run.get('started') or ''):  # a fresh id even within the same second
        started = time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                time.gmtime(parse_ts(started) + 1))
    sid = lifecycle.session_id(product.name, job_name, started)
    conv = getattr(product, 'conventions', None)
    row = types.SimpleNamespace(kind=run.get('kind'), item=run.get('item'), job=job_name,
                                branch=run.get('branch'))
    job_env = {**env.worker_env(cfg, product),
               **githooks.item_env(conv, run.get('item'), run.get('branch')),
               **pushlog.env_for(product, job_name, run.get('kind')),
               **refusals.env_for(product, job_name),
               **prepush.env_for(product, job_name),
               'ASF_SESSION': sid}
    allow = spawn_mod.push_allow(product, row, run.get('branch'))
    if allow:
        job_env['ASF_PUSH_ALLOW'] = allow
    if run.get('id_range'):
        job_env['BACKLOG_ID_RANGE'] = run['id_range']
    s = for_run(run, cfg, product) or settings(cfg, product, 'local' if local else 'cloud')
    wp = (cfg or {}).get('worker_pool') or {}
    try:  # the same fences the first launch ran under: its permission rules and grants
        settings_file = spawn_mod.settings_file(wp)
    except OSError:
        settings_file = None
    add_dirs = [os.path.expanduser(d) for d in (product._get('job_grants') or [])] \
        if hasattr(product, '_get') else []
    job_env['ASF_READ_ROOTS'] = os.pathsep.join(add_dirs)
    job = runtime_mod.Job(product.name, job_name, run.get('worktree'), path, run.get('model'),
                          account=_account(run.get('account'), cfg), env=job_env,
                          add_dirs=add_dirs, settings_file=settings_file,
                          permission_mode=wp.get('permission_mode')
                          or runtime_mod.DEFAULT_PERMISSION_MODE,
                          hooks_dir=githooks.ensure(product) if local else None,
                          passthrough=env.env_passthrough(cfg),
                          product_auth_env=env.product_auth_env(product),
                          branch=run.get('branch'), base=product.main,
                          setup=getattr(conv, 'worktree_setup', None))
    job.heartbeat = s
    stopgate.clear(product, job_name)
    pushlog.clear(product, job_name)
    refusals.clear(product, job_name)
    prepush.clear(product, job_name)
    try:
        result = runtime.run(job)
    except Exception as e:  # noqa: BLE001 — a failed relaunch leaves the run to health
        out(f'heartbeat {job_name}: continuation not launched — {type(e).__name__}: {e}')
        return None
    now = pool_mod.now_iso()
    pool_mod.update_session(product, job_name, ended=now, end_reason=end_reason(why),
                            rc=1)
    chain = int(_num(run.get('resumes')) or 0) + 1
    record = {'job': job_name, 'item': run.get('item'), 'feature': run.get('feature'),
              'kind': run.get('kind'), 'account': run.get('account'), 'model': job.model,
              'pid': result.pid, 'pgid': result.pid, 'worktree': run.get('worktree'),
              'branch': run.get('branch'), 'started': started, 'log': result.log_path,
              'brief': path, 'id_range': run.get('id_range'), 'runtime': runtime.name,
              'session': sid, 'product': product.name,
              'card_digest': run.get('card_digest') or '', 'cause': run.get('cause') or '',
              'heartbeat_min': s.interval_min, 'resumed_from': run.get('session'),
              'resumes': chain}
    if head:
        record['launch_head'] = head
    record.update({k: v for k, v in (getattr(result, 'extra', None) or {}).items()
                   if v is not None})
    pool_mod.append_session(product, record)
    out(f'heartbeat {job_name}: continued as {sid} ({runtime.name}) from '
        f'{(head or "the branch head")[:12]}')
    return record


def seed(state, record, branch_sha):
    """The continuation's first sight, taken at its launch: origin's branch as the relaunch left
    it and no beat — so a push of the old session after the relaunch is compared, never taken
    as the baseline."""
    if not record:
        return
    state[record['job']] = {'session': record.get('session'), 'moved': None,
                            'shas': {n: (branch_sha or '') if n.startswith('refs/heads/') else ''
                                     for n in refs_of(record)}}


def end_reason(why):
    """The ledger's ``end_reason`` of a stalled run: ``failed: stalled: no beat <n>m …``."""
    why = str(why or STALLED)
    return f'failed: {why}' if why.startswith(STALLED) else f'{END_PREFIX}: {why}'


def resumable(run, s):
    """A stalled run may be continued: its chain is under ``heartbeat_resumes``."""
    return s is not None and int(_num(run.get('resumes')) or 0) < s.resumes


# ---- the local lane's pass --------------------------------------------------------------------

def sweep(product, cfg=None, now=None, beats=None, runtime_fn=None, alive=None, out=print,
          stop=None):
    """Every live local run launched with the heartbeat: judged, and a stalled one stopped,
    ended and continued in the same pass. ``[(job, why, new record or None)]``. The cloud lane's
    runs are :func:`asf.workers.cloud.sync`'s (the same rule, read in that pass)."""
    from asf.workers import cloudpid
    from asf.workers import pool as pool_mod
    from asf.workers import runtime as runtime_mod
    if cfg is None:
        try:
            cfg = env.load_config()
        except env.ConfigError:
            cfg = {}
    now = time.time() if now is None else now
    from asf.workers import lifecycle
    alive = alive or lifecycle.pid_alive
    clear_ended(product, out=out)
    # a live local run with no result yet whose pid answers: a dead pid is health's own path
    runs = [r for r in pool_mod.live_sessions(product)
            if not cloudpid.is_token(r.get('pid')) and for_run(r, cfg, product) is not None
            and runtime_mod.read_result(r.get('log')) is None and alive(r.get('pid'))]
    if not runs:
        return []
    beats = beats or Beats(product)
    state = load_state(product)
    found = []
    for run in runs:
        s = for_run(run, cfg, product)
        stalled, quiet, _line = observe(product, run, beats, now, s, out=out, state=state)
        if stalled:  # liveness from the job log, before the beat (the cloud lane's own rule)
            seen = log_alive_at(run.get('log'), now, s.limit_min)
            if seen is not None:
                stalled = False
                rec = state.get(run['job'])
                if isinstance(rec, dict):  # the event is the beat: ``quiet`` resets
                    rec['moved'] = max(seen, _num(rec.get('moved')) or 0)
                out(f"heartbeat {run['job']}: alive by job log (last event "
                    f'{max(0.0, now - seen) / 60.0:.0f}m ago); no beat needed')
        if not stalled:
            continue
        why = f'{STALLED}: no beat {int(quiet)}m'
        out(f'heartbeat {run["job"]}: {why}')
        (stop or _stop_local)(run, alive)
        beat = last_beat(run.get('worktree'), run['job'])
        notes = _local_notes(run) or (beat or {}).get('notes') or ''
        delete_ref(run.get('worktree') or product.repo_dir, run['job'])
        state.pop(run['job'], None)
        summary = summarize_events(log_records(run.get('log')))
        new = None
        if resumable(run, s):
            head = _git(['rev-parse', 'HEAD'], run.get('worktree') or '.').stdout.strip() or None
            rt = runtime_fn() if runtime_fn else runtime_mod.from_config(cfg)
            new = launch(product, run, why, rt, cfg, notes=notes, summary=summary, head=head,
                         out=out)
            seed(state, new, beats.get(f"refs/heads/{run.get('branch')}"))
        if new is None:
            pool_mod.update_session(product, run['job'], ended=pool_mod.now_iso(),
                                    end_reason=end_reason(why), rc=1)
        found.append((run['job'], why, new))
    _save_state(product, state)
    return found


def clear_ended(product, out=print):
    """Every beat ref of a run that is no longer live deleted on origin, its record dropped: a
    beat loop still running after its session (a local process tree, a cloud host) meets the
    missing ref and exits, and nothing of a run outlives it on origin. Returns the jobs cleared."""
    from asf.workers import pool as pool_mod
    state = load_state(product)
    if not state:
        return []
    from asf.workers import lifecycle
    latest = pool_mod.load_sessions(product)
    done = []
    for job, rec in list(state.items()):
        run = latest.get(job) or {}
        if lifecycle.is_live(run) and run.get('session') == (rec or {}).get('session'):
            continue
        beat = ((rec or {}).get('shas') or {}).get(ref(job))
        if beat and not delete_ref(getattr(product, 'repo_dir', None), job):
            continue  # origin unreachable: the next pass tries again
        state.pop(job, None)
        done.append(job)
    if done:
        _save_state(product, state)
    return done


def _local_notes(run):
    wt = run.get('worktree')
    try:
        with open(os.path.join(wt, NOTES_FILE), encoding='utf-8') as f:
            return f.read().strip()[:NOTES_MAX]
    except (OSError, TypeError):
        return ''


def _stop_local(run, alive):
    """SIGTERM (then SIGKILL) to the run's process group — only when its pid is still a headless
    worker of the runtime: a pid reused by anything else is never signalled."""
    from asf.workers import health as health_mod  # local: health imports this module
    from asf.workers import stall as stall_mod
    if not health_mod.is_print_worker(health_mod.command_line(run.get('pid'))):
        return
    try:
        stall_mod.stop_session(run, alive, grace_s=2)
    except (OSError, ValueError, TypeError):
        pass
