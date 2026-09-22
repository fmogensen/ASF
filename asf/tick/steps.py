"""asf.tick.steps — the step manifest ``asf tick`` carries, and who owns each step.

The scheduler runs seven steps: ``record``, ``health``, ``wave``, ``prs``, ``harvest``,
``batch`` (its own job) and daily ``daily``. Each resolves to an owner:

* ``asf``    — a python callable in this package (``record``, ``health``, ``wave``, ``prs``,
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
import shlex
import signal
import subprocess

from asf import env

STEPS = ['record', 'health', 'wave', 'prs', 'harvest', 'batch', 'daily']

# the steps with an asf implementation, and where each lives (the --manifest command column)
ASF_CALLABLES = {
    'record': 'asf.tick.tick:run_record_step',
    'health': 'asf.tick.step_health:run',
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


def run_command(step, command, timeout, emit=print, cwd=None):
    """Run ``command`` (split shell-style, ``~`` expanded, no shell) in ``cwd`` — the product's
    ``repo_dir`` (B-0050: never the tick's own cwd) — and send each output line, stderr merged
    in, to ``emit`` as ``[command:<step>] <line>``. Returns the exit code; 124 if it outlived
    ``timeout`` (its whole process group is killed)."""
    prefix = f'[command:{step}] '
    argv = [os.path.expanduser(a) for a in shlex.split(command)]
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                start_new_session=True, cwd=cwd or None)
    except OSError as e:
        emit(f'{prefix}cannot start: {e}')
        return 127
    try:
        out, _ = proc.communicate(timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, _ = proc.communicate()
        out = (out or '') + f'timeout after {timeout}s — killed\n'
        rc = 124
    for line in (out or '').splitlines():
        emit(prefix + line)
    return rc


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
