"""asf.tick.steps — the step manifest ``asf tick`` carries, and who owns each step.

The scheduler runs eight steps: ``record``, ``health``, ``groom``, ``wave``, ``prs``, ``harvest``,
``batch`` (its own job) and daily ``daily``. Each resolves to an owner:

* ``asf``    — a python callable in this package (``record``, ``health``, ``groom``, ``wave``, ``prs``,
  ``harvest``, ``daily`` — :data:`ASF_CALLABLES`);
* a command  — ``steps: {health: "bash ~/x/health.sh --fix"}`` in the product yaml, run as a
  subprocess with a timeout, its output written to the tick log with a ``[command:<step>]`` prefix;
* ``off``    — ``steps: {batch: off}``: the operator says another job still runs it.

A step declared nowhere is a refusal, not a silent skip — the tick exits 2 before running anything.
A step ``asf`` implements defaults to ``asf``; ``batch`` (a product's own merge-queue script, from
its repo) has no ``asf`` implementation and must be declared.
"""
import datetime
import os
import re
import select
import shlex
import signal
import subprocess
import time

from asf import env

STEPS = ['record', 'health', 'groom', 'wave', 'prs', 'harvest', 'batch', 'daily']

# the steps with an asf implementation, and where each lives (the --manifest command column)
ASF_CALLABLES = {
    'record': 'asf.tick.tick:run_record_step',
    'health': 'asf.tick.step_health:run',
    'groom': 'asf.tick.step_groom:run',
    'wave': 'asf.tick.step_wave:run',
    'prs': 'asf.tick.step_prs:run',
    'harvest': 'asf.tick.step_harvest:run',
    'daily': 'asf.tick.step_daily:run',
}
ASF_STEPS = tuple(ASF_CALLABLES)

DEFAULT_LEGACY_TIMEOUT_S = 900


class StepError(Exception):
    """A manifest the tick refuses to run (exit 2)."""


def declared(product):
    return product._get('steps') or {}


def resolve(product, steps=None):
    """``[(step, owner, command)]`` for ``steps`` (default: all, in manifest order). ``owner`` is
    ``asf``, ``command``, ``off`` or ``undeclared``; ``command`` is the command line for a command
    step, else None. Does not run anything."""
    decl = declared(product)
    rows = []
    for step in (steps or STEPS):
        value = decl.get(step)
        text = str(value).strip() if value is not None else None
        if text is None or text == 'asf':
            # undeclared falls back to asf only for a step asf has; `asf` for a step
            # with no asf implementation is as ownerless as no declaration at all
            owner, command = ('asf' if step in ASF_STEPS else 'undeclared'), None
        elif text == 'off':
            owner, command = 'off', None
        else:
            owner, command = 'command', text
        rows.append((step, owner, command))
    return rows


def parse_steps(text):
    """``"record,health"`` → ``['record', 'health']`` in manifest order; unknown name → StepError."""
    wanted = [s.strip() for s in text.split(',') if s.strip()]
    for s in wanted:
        if s not in STEPS:
            raise StepError(f"tick: unknown step {s} — steps are {', '.join(STEPS)}")
    return [s for s in STEPS if s in wanted]


def check_owned(rows, product):
    """Raise StepError with the refusal line for the first step that has no owner."""
    for step, owner, _ in rows:
        if owner == 'undeclared':
            raise StepError(
                f"tick: step {step} has no owner — declare it under steps in "
                f"products/{product.name}.yaml (asf | <command> | off)")


def manifest_table(rows):
    """The ``--manifest`` table: a header and one line per step, columns padded to their widest."""
    def cell(row):
        step, owner, command = row
        return (step, owner, command or (ASF_CALLABLES[step] if owner == 'asf' else '-'))
    body = [('step', 'owner', 'command')] + [cell(r) for r in rows]
    w0 = max(len(r[0]) for r in body)
    w1 = max(len(r[1]) for r in body)
    return '\n'.join(f'{a:<{w0}}  {b:<{w1}}  {c}' for a, b, c in body) + '\n'


# ---- command steps -------------------------------------------------------------

def command_timeout():
    """Seconds a command step may run: config ``tick.step_timeout_s`` (default 900)."""
    v = (env.load_config().get('tick') or {}).get('step_timeout_s')
    return v if isinstance(v, (int, float)) and v > 0 else DEFAULT_LEGACY_TIMEOUT_S


def run_command(step, command, timeout, emit=print, cwd=None, extra_env=None):
    """Run ``command`` (split shell-style, ``~`` expanded, no shell) in ``cwd`` — the product's
    ``repo_dir`` (B-0050: never the tick's own cwd) — and send each output line, stderr merged
    in, to ``emit`` as ``[command:<step>] <line>`` as the line is produced (B-0119: a step that
    only logged at the end left the tick log empty for however long the step ran, so a live step
    looked the same as a hung one). Returns the exit code; 124 if it outlived ``timeout`` (its
    whole process group is killed). ``extra_env``, when given, is laid over the tick's own
    environment for the child process (the capacity overlay)."""
    prefix = f'[command:{step}] '
    # a loaded host starts no new command step either — the same guard the wave launches under
    # (asf.workers.host, config.yaml host_guards): a product's own script (a batch, a merge
    # queue) running its suites beside the sessions is load the guard exists for
    from asf.workers import host as host_mod
    held, why, _reading = host_mod.pressure(env.load_config())
    if held:
        emit(f'{prefix}held: {why} — not started this tick')
        return 0
    argv = [os.path.expanduser(a) for a in shlex.split(command)]
    popen_env = {**os.environ, **extra_env} if extra_env else None
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                start_new_session=True, cwd=cwd or None, env=popen_env)
    except OSError as e:
        emit(f'{prefix}cannot start: {e}')
        return 127
    deadline = time.monotonic() + timeout
    timed_out = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        ready, _, _ = select.select([proc.stdout], [], [], remaining)
        if not ready:
            continue
        line = proc.stdout.readline()
        if line == '':
            break  # EOF: the child closed its end
        emit(prefix + line.rstrip('\n'))
    if timed_out:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for line in (proc.stdout.read() or '').splitlines():
            emit(prefix + line)
        emit(f'{prefix}timeout after {timeout}s — killed')
        proc.stdout.close()
        proc.wait()
        return 124
    proc.stdout.close()
    return proc.wait()


# ---- the daily stamp ----------------------------------------------------------

def stamp_path(product):
    return os.path.join(env.state_dir(product), 'daily.stamp')


def _today():
    return datetime.date.today().isoformat()


def daily_due(product, force=False, today=None):
    """True when ``--daily`` was passed or the stamp is not today's date."""
    if force:
        return True
    try:
        with open(stamp_path(product), encoding='utf-8') as f:
            return f.read().strip() != (today or _today())
    except OSError:
        return True


def write_daily_stamp(product, today=None):
    with open(stamp_path(product), 'w', encoding='utf-8') as f:
        f.write((today or _today()) + '\n')


# ---- catching up a missed daily (B-0123) ---------------------------------------
#
# The daily clock (``clocks: {daily: {steps: [daily], at: "06:00"}}``) fires once a day; a tick
# that misses its own window (the host was offline, the run failed before it stamped) is not
# retried by that clock again until tomorrow. Every other clock's own tick — ``record``,
# ``dispatch`` — never carries ``daily`` in its ``--steps`` at all, so nothing else even looks at
# it. A regular tick run after the daily clock's own time, on a day the stamp is still stale,
# runs it once as a catch-up instead of leaving it for tomorrow.

_AT_RE = re.compile(r'^([01]?\d|2[0-3]):([0-5]\d)$')


def _daily_clock_time(product):
    """``(hour, minute)`` of the clock whose steps carry ``daily``, or None when no clock does
    (a product with no ``clocks:`` block, or one that runs ``daily`` some other way)."""
    for entry in (product._get('clocks') or {}).values():
        if not isinstance(entry, dict):
            continue
        clock_steps = entry.get('steps')
        clock_steps = clock_steps if isinstance(clock_steps, list) else ([clock_steps] if clock_steps else [])
        m = _AT_RE.match(str(entry.get('at') or '').strip())
        if m and 'daily' in clock_steps:
            return int(m.group(1)), int(m.group(2))
    return None


def daily_catch_up_due(product, now=None):
    """True when the daily clock's own time has passed today and today's run still has not
    succeeded — this regular tick should run ``daily`` itself rather than wait for tomorrow."""
    at = _daily_clock_time(product)
    if at is None:
        return False
    now = now or datetime.datetime.now()
    return (now.hour, now.minute) >= at and daily_due(product, today=now.date().isoformat())


def daily_catch_up_message(product):
    hour, minute = _daily_clock_time(product)
    return f"daily: catching up — {hour:02d}:{minute:02d} run has not succeeded today"
