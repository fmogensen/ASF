"""asf.workers.continuation — may this row be answered by continuing a session (F-0039).

One rule, one reason string, three readers: the wave, ``asf brief --continue`` and the tests.
The rule makes exactly one git call (:func:`resumable`, the trailers on the branch above the
trunk) and no decision the feeder could have made, because the feeder may read neither the
ledger nor git (P5).

``target(product, row, root)`` is the question the wave asks before it builds a brief:
``(run, session_id, round, review_path)`` when the row's branch has a writer whose conversation
may be continued, else ``(None, None, 0, why)`` — and the row launches as it always did.

``resumable(product, run, now)`` runs these tests in this order, each with the reason it prints:

* no run on the branch → ``no session on <branch>``
* :func:`dead` — live, and its log silent past :func:`heartbeat_min` → ``heartbeat stale (14m > 6m)``
* still live → ``still running``
* no ``runtime_session`` recorded on the run → ``no runtime session id recorded``
* the run's worktree is gone → ``worktree reaped``
* an ``ASF-Session`` trailer other than the run's own above the trunk → ``another session moved
  the branch``
* harvested → ``already landed``
* the run's account has left ``worker_pool.accounts`` → ``account <name> no longer in the pool``

A run that *ended* is not dead: its conversation is exactly what a correction wants, and the
heartbeat is the liveness test for a run that is still supposed to be running.
"""
import os
import re
import subprocess
import time

from asf import env as env_mod
from asf.briefs import preamble as preamble_mod
from asf.evidence import review as review_mod
from asf.feeder import rows as feeder_rows
from asf.views import index_reader
from asf.workers import githooks
from asf.workers import lifecycle
from asf.workers import pool as pool_mod
from asf.workers import runtime as runtime_mod
from asf.workers import spawn as spawn_mod
from asf.workers.stall import CORRECTION_HEAD

#: Row kinds that may answer a branch by continuing its session. Everything else launches.
ANSWERING = ('correct', 'spec', 'plan')
#: Never continued, whatever the branch says (§1.4).
NEVER = ('adjudicate', 'review', 'groom', 'reshape', 'rebase', 'close', 'fix-bug', 'task')

DEFAULT_HEARTBEAT_MIN = 6

_ITEM_RE = re.compile(r'^\s*(?:[-*]\s+)?\**([CI])(\d+)\b')
_HEADING_RE = re.compile(r'^\s{0,3}#{1,6}\s')
_MISSED_RE = re.compile(r'^\s{0,3}#{1,6}\s*missed\s+in\s+round\b', re.I)
_CHECK_HEADER_RE = re.compile(r'^\s*\|\s*check\s*\|', re.I)

_DURATION_RE = re.compile(r'^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$')
_UNIT_MIN = {'s': 1 / 60, 'm': 1, '': 1, 'h': 60, 'd': 1440}


def heartbeat_min(product):
    """``stage_limits.heartbeat_min`` — an int is minutes, a duration string
    (``90s``/``6m``/``1h``) works too, exactly as ``stall.silent_minutes`` reads its own."""
    v = (product.stage_limits or {}).get('heartbeat_min', DEFAULT_HEARTBEAT_MIN)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    m = _DURATION_RE.match(str(v))
    if not m:
        return float(DEFAULT_HEARTBEAT_MIN)
    return float(m.group(1)) * _UNIT_MIN[m.group(2)]


def writer(product, branch):
    """The latest run on ``branch`` (:func:`lifecycle.by_branch`), or None. A branch's writer is
    whoever holds it now — a held ``spec/F-0039`` is corrected on ``spec/F-0039`` (the lane state
    machine's own ruling), so this is the same run the correction was written on."""
    return lifecycle.by_branch(pool_mod.sessions_path(product)).get(branch)


def dead(run, product, now):
    """The card's dead session: live (no ``ended`` line) and its log silent past
    :func:`heartbeat_min`. ``(True, 'heartbeat stale (14m > 6m)')`` or ``(False, '')``."""
    if not lifecycle.is_live(run):
        return False, ''
    try:
        mtime = os.path.getmtime(run.get('log'))
    except (OSError, TypeError):
        return False, ''
    limit = heartbeat_min(product)
    silent = (now - mtime) / 60
    if silent <= limit:
        return False, ''
    return True, f'heartbeat stale ({int(silent)}m > {limit:g}m)'


def _other_session(product, run, repo):
    """True when a commit on ``origin/<branch>`` above the trunk carries an ``ASF-Session``
    trailer that is not this run's own (P6). The module's one git call."""
    repo = repo or product.repo_dir
    if not repo:
        return False
    p = subprocess.run(
        ['git', '-C', repo, 'log', '--format=%(trailers:key=ASF-Session,valueonly)',
         f'origin/{product.main}..origin/{run["branch"]}'],
        capture_output=True, text=True)
    if p.returncode != 0:
        return False
    return any(line.strip() and line.strip() != run.get('session')
               for line in p.stdout.splitlines())


def resumable(product, run, now, repo=None, cfg=None, branch=None):
    """``(session_id, '')`` when ``run``'s conversation may be continued, else ``(None, why)``.
    The tests, in order, are the module docstring's. ``branch`` names the branch a missing
    ``run`` was looked up under; ``cfg`` is the operator config (default: the file)."""
    if not run:
        return None, f'no session on {branch}' if branch else 'no session on the branch'
    is_dead, why = dead(run, product, now)
    if is_dead:
        return None, why
    if lifecycle.is_live(run):
        return None, 'still running'
    if not run.get('runtime_session'):
        return None, 'no runtime session id recorded'
    if not run.get('worktree') or not os.path.isdir(run['worktree']):
        return None, 'worktree reaped'
    if _other_session(product, run, repo):
        return None, 'another session moved the branch'
    if lifecycle.landed(run):
        return None, 'already landed'
    name = run.get('account')
    if name:
        cfg = spawn_mod.load_cfg() if cfg is None else cfg
        if name not in {a.name for a in pool_mod.accounts_from_config(cfg)}:
            return None, f'account {name} no longer in the pool'
    return run['runtime_session'], ''


def target(product, row, root, runs=None, now=None, repo=None, cfg=None):
    """``(run, session_id, round, review_path)`` for a row that may be continued, else
    ``(None, None, 0, why)``. ``row.brief_kind`` in ``NEVER`` → ``'kind <k> never continues'``;
    a ``spec``/``plan`` row whose Feature is not at ``<doc>-review rN`` → ``'no review round'``
    (nothing to send: that row is a first draft, not an answer). ``runs`` is a
    ``{branch: run}`` map (default: the registry's); the round is N, never N+1 (PD13)."""
    kind = row.brief_kind
    if kind in NEVER or kind not in ANSWERING:
        return None, None, 0, f'kind {kind} never continues'
    rnd, review_path = 0, ''
    if kind in ('spec', 'plan'):
        try:
            items, _ = index_reader.load(root)
        except (OSError, ValueError, KeyError):
            items = {}
        doc, rnd = feeder_rows.review_round(items.get(row.feature_id or row.item_id) or {})
        if doc != kind:
            return None, None, 0, 'no review round'
        review_path = product.conventions.review_path(row.item_id.lower(), rnd)
    now = time.time() if now is None else now
    run = writer(product, row.branch) if runs is None else runs.get(row.branch)
    sid, why = resumable(product, run, now, repo=repo, cfg=cfg, branch=row.branch)
    if not sid:
        return None, None, 0, why
    return run, sid, rnd, review_path


# ---- the findings, and the message that carries them (§2.4) -------------------

def _table(lines):
    """The contiguous block of ``|`` lines under the check header, verbatim — a reviewer's own
    evidence column is the finding, and re-rendering it would lose the ``\\|`` the template
    asks for. No check header → the first block of two or more ``|`` lines; none → ``''``."""
    def block(start):
        end = start
        while end < len(lines) and lines[end].lstrip().startswith('|'):
            end += 1
        return lines[start:end]
    for i, line in enumerate(lines):
        if _CHECK_HEADER_RE.match(line):
            return '\n'.join(l.rstrip() for l in block(i))
    for i, line in enumerate(lines):
        if line.lstrip().startswith('|') and (i == 0 or not lines[i - 1].lstrip().startswith('|')):
            b = block(i)
            if len(b) >= 2:
                return '\n'.join(l.rstrip() for l in b)
    return ''


def _items(lines):
    """Every ``C``/``I`` item as ``(letter, whole text, line number)``. An item runs from its
    marker through the lines that continue it — indented, or plain prose — and stops at the next
    item, a heading, a table row, a ``verdict:`` line, or a blank line not followed by an
    indented one: half of a C is not actionable."""
    out = []
    i = 0
    while i < len(lines):
        m = _ITEM_RE.match(lines[i])
        if not m:
            i += 1
            continue
        start, body = i, [lines[i].rstrip()]
        i += 1
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                nxt = lines[i + 1] if i + 1 < len(lines) else ''
                if nxt[:1] in (' ', '\t') and nxt.strip() and not _ITEM_RE.match(nxt):
                    body.append('')
                    i += 1
                    continue
                break
            if (_ITEM_RE.match(line) or _HEADING_RE.match(line) or line.lstrip().startswith('|')
                    or review_mod.VERDICT_LINE_RE.search(line)):
                break
            body.append(line.rstrip())
            i += 1
        out.append((m.group(1), '\n'.join(body), start))
    return out


def _verdict_word(text):
    """The review's own verdict, upper-cased (``CHANGES REQUESTED``) — its ``verdict:`` line's
    own words (:data:`evidence.review.VERDICT_LINE_RE`, the one parser's pattern, P8) first, else
    the first legacy word (:data:`evidence.review.LEGACY_WORD_RE`) anywhere in the text; ``''``
    when neither is there."""
    m = review_mod.VERDICT_LINE_RE.search(text)
    if m:
        return m.group('v').strip().strip('`*_ ').upper()
    m = review_mod.LEGACY_WORD_RE.search(text)
    return m.group(0).upper() if m else ''


def findings(text):
    """A review file → ``{'verdict', 'table', 'criticals', 'improvements', 'missed'}``.
    ``verdict`` is :func:`_verdict_word`'s (P8, the one parser's own patterns, reused rather than
    a second one); ``table`` is the contiguous block of ``|`` lines under the check header,
    verbatim; ``criticals`` and
    ``improvements`` are the ``C``/``I`` items in file order, each kept whole. ``missed`` is the
    ``### Missed in round N`` section's items — the ``I`` items among them are in
    ``improvements`` too (the template records a missed finding as an I) — or, when the section
    labels none, its non-empty lines."""
    text = text or ''
    lines = text.splitlines()
    items = _items(lines)
    missed = []
    at = next((i for i, l in enumerate(lines) if _MISSED_RE.match(l)), None)
    if at is not None:
        end = next((i for i in range(at + 1, len(lines)) if _HEADING_RE.match(lines[i])),
                   len(lines))
        missed = [t for _, t, n in items if at < n < end]
        if not missed:
            missed = [l.strip() for l in lines[at + 1:end] if l.strip()]
    return {'verdict': _verdict_word(text),
            'table': _table(lines),
            'criticals': [t for k, t, _ in items if k == 'C'],
            'improvements': [t for k, t, _ in items if k == 'I'],
            'missed': missed}


def prompt(product, run, row, round_n, review_path, text):
    """The continuation message. Never a brief: the session holds the card, the spec, the plan
    and its own diff already, so it carries the findings and the instruction — no preamble, no
    rules, no tail. ``text`` is the review file (``''`` when the branch carries none — a
    ``correct`` row then sends the harvest's ``row.correction`` under
    :data:`stall.CORRECTION_HEAD`, the red gate's own output being the finding). The whole
    message is capped at ``conventions.preamble_max_lines``: the table and the C list stay whole,
    the I list is trimmed from its end."""
    branch = (run or {}).get('branch') or row.branch
    item = row.item_id
    main = getattr(product, 'main', None) or 'main'
    f = findings(text) if text else None
    if f:
        head = (f'CORRECTION — round {round_n} of the review of {item} is in. '
                f'verdict: {f["verdict"].lower() or "not stated"}')
        closing_report = 'what it asked, where it is now closed'
        body = [f'Review: {review_path}'] if review_path else []
        if f['table']:
            body.append(f['table'])
        criticals = f['criticals']
        improvements = f['improvements'] + [m for m in f['missed'] if m not in f['improvements']]
    else:
        head = f'CORRECTION — {item} was held and is back with you.'
        closing_report = 'what failed, where it is now closed'
        correction = (getattr(row, 'correction', '') or '').rstrip()
        body = [(CORRECTION_HEAD + correction).strip('\n')] if correction else []
        criticals, improvements = [], []
    intro = (f'This is your own session, your own worktree and your own branch `{branch}`, '
             f'rebased onto origin/{main} before this message. Do not re-read what you wrote; '
             f'read the findings.')
    closing = ("Fix every C exactly as it specifies; apply the I's you agree with; never widen "
               "the scope — a change the review did not ask for buys another round. Re-run the "
               f"acceptance tests and the Gate. Commit with `git commit -s`, push `{branch}`, and "
               f"finish with the typed REPORT, one line per C: {closing_report}.")

    def render(kept):
        parts = [head, intro, *body]
        if criticals:
            parts.append('\n'.join(criticals))
        if kept:
            parts.append('\n'.join(improvements[:kept]))
        if kept < len(improvements):
            parts.append(f'({len(improvements) - kept} more I not sent — the review file has them.)')
        parts.append(closing)
        return '\n\n'.join(parts) + '\n'

    cap = preamble_mod.max_lines(product)
    kept = len(improvements)
    while kept and len(render(kept).splitlines()) > cap:
        kept -= 1
    return render(kept)


# ---- the launch that continues (§2.5) ------------------------------------------

def continue_run(product, row, run, session_id, round_n, review_path):
    """Continue ``run``'s session with ``row``'s findings. Writes the message to
    ``<state>/briefs/<writer job>.continue-r<n>.md`` (through :func:`spawn.briefs_dir`, so it
    sits beside a brief), rebuilds the ``Job`` :func:`spawn.spawn` would (PD8, PD6, PD10 — the
    writer's own worktree, account and id range, no new reservation) with ``resume=session_id``
    and the writer's own ``ASF_SESSION``, and calls :meth:`runtime.Runtime.continue_run`.

    ``None`` when the worktree cannot be prepared or the account's credentials cannot be read (a
    :class:`spawn.SpawnError`/:class:`runtime.AuthEnvError` is the fallback's trigger, not a
    crash) or the runtime declines — the caller falls back in the same tick (PD9). Otherwise the
    ledger record PD7 lists: the writer's ``job``, ``worktree``, ``branch``, ``id_range``,
    ``account``, ``product`` and ``session``; the row's own ``item``, ``feature`` and ``kind``;
    and this launch's own ``pid``, ``pgid``, ``started``, ``brief``, ``resumed`` and
    ``continued: 1``."""
    job = run['job']
    branch = run['branch']
    text = ''
    if review_path:
        try:
            with open(review_path, encoding='utf-8') as f:
                text = f.read()
        except OSError:
            text = ''
    message = prompt(product, run, row, round_n, review_path, text)
    brief_path = os.path.join(spawn_mod.briefs_dir(product), f'{job}.continue-r{round_n}.md')
    with open(brief_path, 'w', encoding='utf-8') as f:
        f.write(message)
    cfg = spawn_mod.load_cfg()
    wp = cfg.get('worker_pool') or {}
    account = {a.name: a for a in pool_mod.accounts_from_config(cfg)}.get(run.get('account'))
    product_auth_env = env_mod.product_auth_env(product)
    try:
        runtime_mod.auth_env_values(account, product_auth_env)
        worktree = spawn_mod.make_worktree(product, job, branch)
    except (spawn_mod.SpawnError, runtime_mod.AuthEnvError):
        return None
    add_dirs = [os.path.expanduser(d) for d in (product._get('job_grants') or [])]
    job_env = {**env_mod.worker_env(cfg, product),
               **githooks.item_env(getattr(product, 'conventions', None), row.item_id, branch),
               'BACKLOG_ID_RANGE': run.get('id_range'), 'ASF_SESSION': run['session']}
    runtime = runtime_mod.from_config(cfg)
    j = runtime_mod.Job(product.name, job, worktree, brief_path, run.get('model'),
                        account=account, add_dirs=add_dirs,
                        permission_mode=wp.get('permission_mode') or runtime_mod.DEFAULT_PERMISSION_MODE,
                        env=job_env, settings_file=spawn_mod.settings_file(wp),
                        hooks_dir=githooks.ensure(product), resume=session_id,
                        passthrough=env_mod.env_passthrough(cfg), product_auth_env=product_auth_env,
                        branch=branch, base=product.main)
    result = runtime.continue_run(j)
    if result is None:
        return None
    record = {'job': job, 'item': row.item_id, 'feature': row.feature_id, 'kind': row.brief_kind,
              'account': account.name if account else run.get('account'),
              'pid': result.pid, 'pgid': result.pid, 'worktree': worktree, 'branch': branch,
              'started': pool_mod.now_iso(), 'log': result.log_path, 'brief': brief_path,
              'id_range': run.get('id_range'), 'runtime': runtime.name, 'session': run['session'],
              'product': product.name, 'resumed': session_id, 'continued': 1}
    pool_mod.append_session(product, record)
    return record
