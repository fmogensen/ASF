"""asf.tick.steps — the step manifest ``asf tick`` carries, and who owns each step.

The scheduler ran six steps: ``record``, ``health``, ``wave``, ``prs``, ``batch`` (its own job)
and daily ``daily``. Each resolves to an owner:

* ``asf``    — a python callable in this package (today only ``record``);
* a command  — ``legacy_steps: {health: "bash ~/x/health.sh --fix"}`` in the product yaml, run as a
  subprocess with a timeout, its output written to the tick log with a ``[legacy:<step>]`` prefix;
* ``off``    — ``legacy_steps: {batch: off}``: the operator says another job still runs it.

A step declared nowhere is a refusal, not a silent skip — the tick exits 2 before running anything.
``record`` defaults to ``asf`` (it is what ``asf`` has); every other step must be declared.
"""
import datetime
import os
import shlex
import signal
import subprocess

from asf import env

STEPS = ['record', 'health', 'wave', 'prs', 'batch', 'daily']

# the steps with an asf implementation (see asf.tick.tick.run_record_step)
ASF_STEPS = ('record',)

DEFAULT_LEGACY_TIMEOUT_S = 900


class StepError(Exception):
    """A manifest the tick refuses to run (exit 2)."""


def declared(product):
    return product._get('legacy_steps') or {}


def resolve(product, steps=None):
    """``[(step, owner, command)]`` for ``steps`` (default: all, in manifest order). ``owner`` is
    ``asf``, ``legacy``, ``off`` or ``undeclared``; ``command`` is the command line for a legacy
    step, else None. Does not run anything."""
    decl = declared(product)
    rows = []
    for step in (steps or STEPS):
        value = decl.get(step)
        text = str(value).strip() if value is not None else None
        if text is None or text == 'asf':
            # undeclared falls back to asf only for a step asf has (record); `asf` for a step
            # with no asf implementation is as ownerless as no declaration at all
            owner, command = ('asf' if step in ASF_STEPS else 'undeclared'), None
        elif text == 'off':
            owner, command = 'off', None
        else:
            owner, command = 'legacy', text
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
                f"tick: step {step} has no owner — declare it under legacy_steps in "
                f"products/{product.name}.yaml (asf | <command> | off)")


def manifest_table(rows):
    """The ``--manifest`` table: a header and one line per step, columns padded to their widest."""
    def cell(row):
        step, owner, command = row
        return (step, owner, command or ('asf.tick.tick:run_record_step' if owner == 'asf' else '-'))
    body = [('step', 'owner', 'command')] + [cell(r) for r in rows]
    w0 = max(len(r[0]) for r in body)
    w1 = max(len(r[1]) for r in body)
    return '\n'.join(f'{a:<{w0}}  {b:<{w1}}  {c}' for a, b, c in body) + '\n'


# ---- legacy steps -------------------------------------------------------------

def legacy_timeout():
    """Seconds a legacy step may run: config ``tick.legacy_timeout_s`` (default 900)."""
    v = (env.load_config().get('tick') or {}).get('legacy_timeout_s')
    return v if isinstance(v, (int, float)) and v > 0 else DEFAULT_LEGACY_TIMEOUT_S


def run_legacy(step, command, timeout, emit=print):
    """Run ``command`` (split shell-style, ``~`` expanded, no shell) and send each output line —
    stderr merged in — to ``emit`` as ``[legacy:<step>] <line>``. Returns the exit code; 124 if it
    outlived ``timeout`` (its whole process group is killed)."""
    prefix = f'[legacy:{step}] '
    argv = [os.path.expanduser(a) for a in shlex.split(command)]
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                start_new_session=True)
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
