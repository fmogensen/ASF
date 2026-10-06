"""asf/evals/run.py — score the frozen eval set and report the reading.

A lever with fewer than the manifest's ``min_tasks`` tasks, or missing a required polarity, is
not scored: it is ``exempt``, and ``unrecorded`` on top of that when ``evals/exemptions.json``
carries no entry for it. A recorded gap is quiet; an unrecorded one is loud — ``asf evals run``
and ``asf evals check`` both exit non-zero on it, so a lever can never go silently unmeasured.

``score`` takes a ``record_root`` and a ``product`` because a product's own enforced rules add
``rule:<id>`` levers to the roster a later Task discovers from the record; here the parameters
exist and are threaded through, but the roster scored is the manifest's own.
"""
import dataclasses
import json
import os
import sys
import time

from asf.evals import adapters
from asf.evals import set as evals_set


@dataclasses.dataclass(frozen=True)
class Failure:
    task_id: str
    why: str
    expected: str
    got: str
    detail: str


@dataclasses.dataclass(frozen=True)
class LeverReading:
    lever: str              #: the lever id
    lever_hash: str
    passed: int
    total: int
    exempt: bool
    exempt_reason: str      #: '' when not exempt
    unrecorded: bool
    failures: tuple
    seconds: float


@dataclasses.dataclass(frozen=True)
class Reading:
    set_hash: str
    levers: tuple            #: LeverReading, in the scored roster's order


def _manifest_policy(eval_set):
    """``min_tasks`` and ``require_both_polarities`` live on the manifest, not on
    :class:`asf.evals.set.Set` — re-read from ``eval_set.root``, already validated once by
    ``load()``, so this never raises what that load did not."""
    path = os.path.join(eval_set.root, 'manifest.json')
    with open(path, encoding='utf-8') as f:
        manifest = json.load(f)
    return manifest.get('min_tasks', 2), manifest.get('require_both_polarities', True)


def _is_covered(lever, min_tasks, require_both_polarities):
    if len(lever.tasks) < min_tasks:
        return False
    if not require_both_polarities:
        return True
    expects = {t.expect for t in lever.tasks}
    return 'fire' in expects and 'no-fire' in expects


def _run_task(lever, task):
    """``(ok, got, detail)`` for one task: ``got`` is ``'fire'``, ``'no-fire'`` or ``'broken'``
    (a :class:`adapters.Broken`, or any other exception the adapter raised — both are a fail
    with ``got: 'broken'``, never excluded and never a no-fire, §1.4)."""
    try:
        fired, detail = adapters.fires(lever, task)
    except adapters.Broken as e:
        return False, 'broken', str(e)
    except Exception as e:  # the adapter's problem, not this module's to diagnose further
        msg = str(e) or repr(e)
        return False, 'broken', msg.splitlines()[0]
    got = 'fire' if fired else 'no-fire'
    ok = fired == (task.expect == 'fire')
    return ok, got, (detail or '')


def _score_lever(lever, min_tasks, require_both_polarities):
    if not _is_covered(lever, min_tasks, require_both_polarities):
        return LeverReading(
            lever=lever.id, lever_hash=None, passed=0, total=0,
            exempt=True, exempt_reason=lever.exempt_reason,
            unrecorded=not bool(lever.exempt_reason), failures=(), seconds=0.0)

    start = time.time()
    failures = []
    passed = 0
    for task in sorted(lever.tasks, key=lambda t: t.path):
        ok, got, detail = _run_task(lever, task)
        if ok:
            passed += 1
        else:
            failures.append(Failure(task_id=task.id, why=task.why, expected=task.expect,
                                     got=got, detail=detail))
    return LeverReading(
        lever=lever.id, lever_hash=evals_set.lever_hash(lever), passed=passed,
        total=len(lever.tasks), exempt=False, exempt_reason='', unrecorded=False,
        failures=tuple(failures), seconds=time.time() - start)


def score(eval_set, product=None, record_root=None):
    """Score every lever the manifest declares, in manifest order — plus, once a later Task
    discovers them, one ``rule:<id>`` lever per the record's own enforced rule. ``product`` and
    ``record_root`` are accepted now so that discovery has a parameter to arrive on; neither
    widens the roster here."""
    min_tasks, require_both_polarities = _manifest_policy(eval_set)
    levers = tuple(_score_lever(lever, min_tasks, require_both_polarities)
                   for lever in eval_set.levers)
    return Reading(set_hash=eval_set.set_hash, levers=levers)


def print_reading(reading, out=print):
    """§2.4's printed shape: a header with the overall rate and the exempt/unrecorded counts,
    then one line per lever — a rate and its hash when scored, ``exempt — <reason>`` when not
    and recorded, ``UNRECORDED`` when not and not recorded — with a lever's failures printed
    under it, each with the ``why`` a human reads the failure against."""
    scored = [lr for lr in reading.levers if not lr.exempt]
    passed = sum(lr.passed for lr in scored)
    total = sum(lr.total for lr in scored)
    exempt_n = sum(1 for lr in reading.levers if lr.exempt)
    unrecorded_n = sum(1 for lr in reading.levers if lr.unrecorded)
    out(f"== EVALS set {reading.set_hash} — {passed}/{total} passed, "
        f"{exempt_n} lever{'' if exempt_n == 1 else 's'} exempt, {unrecorded_n} unrecorded")
    for lr in reading.levers:
        if lr.unrecorded:
            out(f"{lr.lever:<14} UNRECORDED — no eval task, no exemption")
        elif lr.exempt:
            out(f"{lr.lever:<14} exempt — {lr.exempt_reason} (0/0)")
        else:
            out(f"{lr.lever:<14} {lr.passed}/{lr.total}   {lr.lever_hash}")
            for failure in lr.failures:
                out(f"  FAIL {failure.task_id}  expected {failure.expected}, got {failure.got}")
                out(f"       why: {failure.why}")


def _unrecorded_lever_ids(eval_set):
    min_tasks, require_both_polarities = _manifest_policy(eval_set)
    return [lever.id for lever in eval_set.levers
            if not _is_covered(lever, min_tasks, require_both_polarities)
            and not lever.exempt_reason]


def _record_root(args):
    product = getattr(args, 'product', None)
    if not product:
        return None
    from asf import env
    return env.load_product(product).backlog_dir


def cmd_evals(args, out=None):
    """``run`` scores and prints, and exits 1 when any task failed or any lever is unrecorded —
    the reading doubles as a gate. ``check`` scores nothing: it applies the coverage rule and
    exits 1 on an unrecorded lever, naming it and nothing else. ``hash`` prints the set hash and
    each lever's. A missing eval set (PD10) is not a crash and not a pass: ``run``/``hash`` print
    the loader's message and exit 2, ``check`` prints it and exits 0 — nothing to report a
    coverage failure against."""
    out = out if out is not None else sys.stdout

    if args.evals_command is None:
        print('usage: asf evals run|check|hash', file=sys.stderr)
        return 2

    try:
        eval_set = evals_set.load()
    except evals_set.EvalError as e:
        print(str(e), file=out)
        return 0 if args.evals_command == 'check' else 2

    if args.evals_command == 'hash':
        print(eval_set.set_hash, file=out)
        for lever in eval_set.levers:
            print(f'{lever.id}  {evals_set.lever_hash(lever)}', file=out)
        return 0

    if args.evals_command == 'check':
        unrecorded = _unrecorded_lever_ids(eval_set)
        for lever_id in unrecorded:
            print(f'UNRECORDED {lever_id} — no eval task, no exemption', file=out)
        return 1 if unrecorded else 0

    reading = score(eval_set, product=getattr(args, 'product', None),
                    record_root=_record_root(args))
    if getattr(args, 'json', False):
        print(json.dumps(dataclasses.asdict(reading), indent=2, sort_keys=True), file=out)
    else:
        print_reading(reading, out=lambda line: print(line, file=out))
    failed = any(lr.failures for lr in reading.levers)
    unrecorded = any(lr.unrecorded for lr in reading.levers)
    return 1 if failed or unrecorded else 0
