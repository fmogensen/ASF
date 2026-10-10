"""asf.prepush — the matcher, the runner and the ledger behind ``asf pre-push``.

A product declares ``conventions.pre_push_checks``, an ordered list of ``{paths, run, why}``
rules (:func:`asf.conventions.Conventions.pre_push_checks`). ``asf pre-push`` (:mod:`asf.cli`)
reads the hook's ref lines on stdin, asks git which paths each pushed head adds over
``origin/<trunk>``, runs each rule whose ``paths`` any added path hits, in declaration order, and
refuses the push on the first red one — with the rule's own ``why`` and the command's last lines.
Before any rule runs, a path outside the session's ``ASF_WRITES`` footprint refuses the push too
(F-0301 S-77505).

Pure first — ``rules``, ``matched``, ``reaching``, ``outside``, ``added_paths``, ``run_rule``,
``refusal``, ``boundary_env``, ``boundary`` know nothing of briefs or metrics — then the two
impure edges: ``added_paths`` (reads the repo) and ``run_rule`` (runs a command). ``TAIL_LINES``
and ``TIMEOUT_S`` are :mod:`asf.workers.trunkmerge`'s own answers for how much of a red check to
quote and how long one rule may run (C13); declared here, not imported, since this module has no
other reason to import that one.

The ledger, for the ``caught`` count (F-0301 S-77507): one JSON object per refusal, appended to
``<state>/<p>/gates/<job>.prepush`` — shaped exactly as :mod:`asf.workers.pushlog` and
:mod:`asf.workers.refusals`' own ledgers (``path``/``env_for``/``clear``, ``os.path.basename(job)``
so no value walks out of ``gates/``). Every write is ``try``/``except OSError`` and never changes
the exit status the push already decided (C13, pinned by
``tests/test_session_push_guard.py::ARefusedPushIsWrittenDown::test_the_exit_status_never_depends_on_the_write``).
"""
import datetime
import json
import os
import subprocess

from asf import env, gitops, hermetic
from asf.feeder import footprint, widen
from asf import customer_content

#: the paths a refusal names before the rest are counted
MAX_SHOWN = 10
#: trunkmerge's own answer for how much of a red check a refusal quotes (C13)
TAIL_LINES = 15
#: one rule's own clock, trunkmerge.TIMEOUT_S's peer
TIMEOUT_S = 900
#: the ledger directory under env.state_dir(product) — pushlog's and refusals' own
GATES_DIR = 'gates'


def rules(conv):
    """``conventions.pre_push_checks`` of ``conv`` — ``[]`` for a product with none."""
    return conv.pre_push_checks() if conv is not None else []


def matched(rules_, paths):
    """``[(rule, [path])]``: for each rule, in declaration order, the ``paths`` it hits
    (:func:`asf.customer_content.matches`); a rule with no hit is left out."""
    out = []
    for rule in rules_ or ():
        hits = [p for p in paths or () if customer_content.matches(rule.get('paths') or (), p)]
        if hits:
            out.append((rule, hits))
    return out


def reaching(rules_, writes):
    """``[rule]`` — the rules whose ``paths`` globs overlap ``writes`` (:func:`globs_overlap`):
    what a brief's rubric prints before any diff exists, since the rule and the Task's own
    ``writes:`` are both just glob lists."""
    writes = list(writes or ())
    out = []
    for rule in rules_ or ():
        if any(footprint.globs_overlap(p, w) for p in rule.get('paths') or () for w in writes):
            out.append(rule)
    return out


def outside(paths, boundary):
    """``paths`` not inside ``boundary`` — delegates to :func:`asf.feeder.widen.outside` so the
    door and the tick answer with one function (C5). An empty ``boundary`` (unset ``ASF_WRITES``)
    tests nothing: ``[]``, never every path."""
    if not boundary:
        return []
    return widen.outside(paths, boundary)


def added_paths(repo, sha, base):
    """``git diff --name-only <base>...<sha>`` in ``repo`` — the paths the push adds over the
    trunk; ``None`` when git cannot resolve the diff (an unreadable base), never raising."""
    r = gitops.git(['diff', '--name-only', f'{base}...{sha}'], repo)
    if not r.ok:
        return None
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


def run_rule(rule, cwd, timeout=None):
    """``(ok, tail)``: ``rule['run']`` through the shell in ``cwd``, :data:`TIMEOUT_S` by
    default, output captured and merged. ``ok`` True when it exits 0; on red, ``tail`` is the
    command's last :data:`TAIL_LINES` non-empty lines; on the clock, ``(False, 'timed out after
    Ns')`` and the whole process group is killed (``start_new_session=True`` + ``os.killpg``)."""
    timeout = TIMEOUT_S if timeout is None else timeout
    p = subprocess.Popen(rule.get('run'), shell=True, cwd=cwd, env=hermetic.git_env(),
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, 9)
        except (ProcessLookupError, PermissionError):
            p.kill()
        p.communicate()
        return False, f'timed out after {timeout}s'
    if p.returncode == 0:
        return True, ''
    lines = [ln for ln in (out or '').splitlines() if ln.strip()][-TAIL_LINES:]
    return False, '\n'.join(lines)


def refusal(rule, tail):
    """The text a red rule's refusal carries: its own ``why``, the command and its exit, and
    the command's tail — the same ``why`` string the brief's rubric row prints (C13)."""
    lines = [rule.get('why') or '', f"`{rule.get('run')}` exited 1"]
    if tail:
        lines.append(tail)
    return '\n'.join(l for l in lines if l)


def boundary_env(globs):
    """``{'ASF_WRITES': ' '.join(globs)}`` — :func:`asf.workers.pushlog.env_for`'s shape."""
    return {'ASF_WRITES': ' '.join(globs or [])}


def boundary(value):
    """The ``ASF_WRITES`` env string back to a glob list; ``[]`` for unset or empty."""
    return [g for g in str(value or '').split() if g]


# ---- the ledger (S-77507's `caught`) -----------------------------------------------------------

def path(product, job):
    """``<state>/<p>/gates/<job>.prepush``; ``job`` through :func:`os.path.basename`, as
    :func:`asf.workers.pushlog.path`, so no value can walk out of :data:`GATES_DIR`."""
    return os.path.join(env.state_dir(product), GATES_DIR,
                        f'{os.path.basename(job or "")}.prepush')


def env_for(product, job):
    """``{'ASF_PREPUSH_LOG': path(...)}`` — the session environment ``asf pre-push`` appends
    to, set beside ``ASF_PUSH_LOG`` and ``ASF_REFUSAL_LOG``."""
    p = path(product, job)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return {'ASF_PREPUSH_LOG': p}


def clear(product, job):
    """Remove ``job``'s ledger at launch, so the file holds one run's refusals. Never raises."""
    try:
        os.remove(path(product, job))
    except OSError:
        pass


def record(kind, rule, paths):
    """Append one JSON object — ``{at, kind, rule, paths}`` — to ``$ASF_PREPUSH_LOG``. ``kind``
    is ``'footprint'`` or ``'rule'``; ``rule`` is the matched rule (or ``None`` for a footprint
    refusal) and only its ``run`` is kept. Never raises, and never changes the exit status: a
    write that fails is silently skipped (C13)."""
    p = os.environ.get('ASF_PREPUSH_LOG')
    if not p:
        return
    at = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    line = json.dumps({'at': at, 'kind': kind, 'rule': (rule or {}).get('run'),
                       'paths': list(paths or [])})
    try:
        with open(p, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass


def caught(product, job):
    """The number of refusals ``job``'s ledger holds — ``0`` for no file. Never raises."""
    try:
        with open(path(product, job), encoding='utf-8', errors='replace') as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0
