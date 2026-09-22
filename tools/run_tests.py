#!/usr/bin/env python3
"""tools/run_tests.py — the suite, one test module per process, N at a time (B-0068).

``python3 -m unittest discover -s tests`` runs every module in one process, one after another:
835 tests in 324 s on a ten-core machine, most of it subprocess-bound fixtures (git, ``asf
tick``, ``asf init``) that leave the other cores idle. This runner discovers the same modules
(``tests/test_*.py``), orders them heaviest first (by the tests each carries) and runs each in
its own ``python -m unittest`` process, at most ``--shards`` at a time (default
``min(cpu_count, 8)``) — a process pool over modules balances itself, where a fixed split by
count would wait on whichever shard drew the slow fixtures.

Every process gets the hermetic environment (:func:`asf.hermetic.build`: no git-hook variable,
no caller identity, the default branch pinned, this checkout first on ``PYTHONPATH``) and its
own operator home (``ASF_TESTS_HOME``, one per process under one directory removed at the end),
and runs ``tests.test_00_home`` first, as ``discover`` would, so its environment is exactly the
serial suite's. A module that cannot run beside the others is named in :data:`SERIAL` and runs
after the pool, alone, in one final process.

A red module's output is printed the moment it finishes; ``-v`` prints every module's. The end
is the suite's own summary shape — ``Ran X tests in Ys`` and ``OK`` / ``FAILED (...)`` — and the
exit status is non-zero when any process was. ``--shards 1`` is the serial suite, one module at
a time. Python 3 stdlib only.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:  # the checkout under test, not an installed copy
    sys.path.insert(0, ROOT)

from asf import hermetic  # noqa: E402

#: The module that sets the hermetic home; ``discover`` loads it first, so every process does.
HOME_MODULE = 'test_00_home'
#: Modules that cannot run beside another (they take a machine-wide resource) — run after the
#: pool, alone, in one final process. None today; the name stays so the rule has a home.
SERIAL = ()
#: The most processes the default runs at once.
MAX_SHARDS = 8

RAN_RE = re.compile(r'^Ran (\d+) tests? in ([\d.]+)s', re.M)
VERDICT_RE = re.compile(r'^(OK|FAILED)(?: \((.*)\))?\s*$', re.M)
COUNT_RE = re.compile(r'(\w+)=(\d+)')
TEST_DEF_RE = re.compile(r'^\s+(?:async )?def test_', re.M)


def default_shards():
    return max(1, min(os.cpu_count() or 1, MAX_SHARDS))


def discover(tests_dir):
    """``{module: tests it carries}`` for every ``test_*.py`` under ``tests_dir``."""
    out = {}
    for name in sorted(os.listdir(tests_dir)):
        if not (name.startswith('test_') and name.endswith('.py')):
            continue
        with open(os.path.join(tests_dir, name), encoding='utf-8') as f:
            out[name[:-3]] = len(TEST_DEF_RE.findall(f.read()))
    return out


def plan(tests_dir, serial=SERIAL):
    """``(pool, serial)``: the modules to run in the pool, heaviest first, and the ones to run
    alone afterwards. The home module is in neither — every process runs it first."""
    weights = discover(tests_dir)
    pool = [m for m in weights if m != HOME_MODULE and m not in serial]
    pool.sort(key=lambda m: (-weights[m], m))
    tail = [m for m in weights if m in serial]
    return pool, tail


def child_env(root, home):
    env = hermetic.build(worktree=root)
    env['ASF_TESTS_HOME'] = home
    return env


def run_modules(modules, root, package, home, env_base=None):
    """One ``python -m unittest`` over ``modules`` (the home module first), as a Popen."""
    names = ([f'{package}.{HOME_MODULE}'] if os.path.isfile(
        os.path.join(root, package, HOME_MODULE + '.py')) else [])
    names += [f'{package}.{m}' for m in modules if m != HOME_MODULE]
    os.makedirs(home, exist_ok=True)
    return subprocess.Popen([sys.executable, '-m', 'unittest', *names], cwd=root,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env=child_env(root, home))


def parse(output):
    """``(tests, seconds, verdict, counts)`` from one process's output; ``verdict`` is None when
    the process never reached its summary (a crash, a collection error)."""
    ran = RAN_RE.search(output or '')
    verdict = VERDICT_RE.search(output or '')
    counts = dict((k, int(v)) for k, v in COUNT_RE.findall(verdict.group(2) or '')) if verdict else {}
    return (int(ran.group(1)) if ran else 0, float(ran.group(2)) if ran else 0.0,
            verdict.group(1) if verdict else None, counts)


def summary(results, seconds, shards):
    """The suite's own two lines over every process's result. ``results``: ``[(module, rc,
    output)]``."""
    tests = 0
    counts = {}
    red = []
    for module, rc, output in results:
        n, _s, verdict, c = parse(output)
        tests += n
        for k, v in c.items():
            counts[k] = counts.get(k, 0) + v
        if rc != 0 or verdict != 'OK':
            red.append(module)
            if verdict is None:
                counts['errors'] = counts.get('errors', 0) + 1
    failed = bool(red) or any(k in counts for k in ('failures', 'errors', 'unexpectedSuccesses'))
    lines = [f'Ran {tests} tests in {seconds:.3f}s ({len(results)} module(s), {shards} at a time)', '']
    detail = ', '.join(f'{k}={v}' for k, v in sorted(counts.items()))
    if failed:
        lines.append('FAILED' + (f' ({detail})' if detail else ''))
        lines.append('red: ' + ', '.join(red) if red else '')
    else:
        lines.append('OK' + (f' ({detail})' if detail else ''))
    return '\n'.join(l for l in lines if l is not None)


def run(tests_dir, shards=None, verbose=False, out=print, serial=SERIAL):
    """Run the suite; the exit status."""
    tests_dir = os.path.abspath(tests_dir)
    root, package = os.path.split(tests_dir)
    shards = shards or default_shards()
    pool, tail = plan(tests_dir, serial)
    homes = tempfile.mkdtemp(prefix='asf-tests-')
    results = []
    started = time.monotonic()

    def finish(module, proc):
        output = proc.communicate()[0]
        results.append((module, proc.returncode, output))
        n, s, verdict, _c = parse(output)
        if proc.returncode != 0 or verdict != 'OK':
            out(f'--- {module}: {verdict or "no summary"} (rc {proc.returncode})')
            out(output.rstrip())
        elif verbose:
            out(f'--- {module}: {verdict}, {n} test(s) in {s:.1f}s')

    try:
        running = []
        queue = list(pool)
        while queue or running:
            while queue and len(running) < shards:
                module = queue.pop(0)
                running.append((module, run_modules([module], root, package,
                                                    os.path.join(homes, module))))
            done = [(m, p) for m, p in running if p.poll() is not None]
            if not done:
                time.sleep(0.05)
                continue
            for m, p in done:
                running.remove((m, p))
                finish(m, p)
        if tail:
            finish('+'.join(tail), run_modules(tail, root, package, os.path.join(homes, 'serial')))
    finally:
        shutil.rmtree(homes, ignore_errors=True)
    out('')
    out(summary(results, time.monotonic() - started, shards))
    return 0 if all(rc == 0 and parse(o)[2] == 'OK' for _m, rc, o in results) else 1


def build_parser():
    p = argparse.ArgumentParser(prog='run_tests.py', description=__doc__.split('\n\n')[0])
    p.add_argument('--shards', type=int, default=None,
                   help=f'processes at a time (default min(cpu_count, {MAX_SHARDS}))')
    p.add_argument('-s', '--start-directory', default=os.path.join(ROOT, 'tests'),
                   help='the tests package (default: this checkout\'s tests/)')
    p.add_argument('-v', '--verbose', action='store_true', help='print every module\'s verdict')
    p.add_argument('--list', action='store_true', help='print the plan and exit')
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.list:
        pool, tail = plan(args.start_directory)
        print(f'shards: {args.shards or default_shards()}')
        for m in pool:
            print(f'pool    {m}')
        for m in tail:
            print(f'serial  {m}')
        return 0
    return run(args.start_directory, args.shards, args.verbose)


if __name__ == '__main__':
    sys.exit(main())
