"""asf.product_check — ``asf product check <file> --product <p>``: will the venv this product
runs accept this product file? The pre-save check.

A product pinned to an older sha (:mod:`asf.installs`) reads its product file with *that* sha's
loader. A key this checkout tolerates (an unknown key is a warning here, :func:`asf.env.
product_problems`) can still be a refusal there — and a refused product file is a clock that
ticks ``ConfigError`` until someone notices. So the check runs the file through the loader of the
venv the product actually runs, in a subprocess of that venv's own interpreter (no
``PYTHONPATH``, cwd ``/``: nothing of this checkout can shadow its ``asf``):

1. ``--venv <dir>`` when given (a candidate venv, before a move);
2. else the product's pin (``install.json``'s venv);
3. else the interpreter its clock plist runs (an unpinned product's shared install);
4. else this process's own interpreter, said so on the first line.

Under a reader that predates the errors/warnings split every finding is a refusal, as that
reader's :func:`load_product` treats it. Prints one line per finding and exits 1 when that
venv's loader would refuse the file, else 0. Writes nothing.
"""
import json
import os
import subprocess
import sys

from asf import env

#: Run by the target venv's interpreter: ``sys.argv[1]`` is the file. Prints one JSON line —
#: ``errors`` and ``warnings`` as that reader splits them.
_CHECK = r'''
import json, sys
import asf
from asf import env
text = open(sys.argv[1], encoding='utf-8').read()
split = getattr(env, 'product_problems', None)
if split is not None:
    errors, warnings = split(text)
else:
    errors, warnings = env.validate_product_text(text), []
print(json.dumps({'asf': asf.__file__, 'errors': [list(e) for e in errors],
                  'warnings': [list(w) for w in warnings]}))
'''


def target(product_name, venv=None, cfg=None):
    """``(interpreter, env_vars, cwd, label)`` — the loader the check runs under (see the module
    doc for the order). ``env_vars`` None means this process's environment without
    ``PYTHONPATH``."""
    from asf import doctor, installs
    if venv:
        venv = os.path.abspath(os.path.expanduser(venv))
        return installs.interpreter(venv), None, '/', f'venv {os.path.basename(venv)}'
    try:
        cfg = env.load_config() if cfg is None else cfg
    except env.ConfigError:
        cfg = {}
    found = doctor._load_target(product_name, cfg)
    if found is not None:
        return found
    return sys.executable, None, '/', f'this process ({sys.executable}) — {product_name} has ' \
                                      'no pin and no clock plist'


def run_check(path, interpreter, env_vars=None, cwd='/', timeout=120):
    """``(result, error)``: the target reader's ``{'errors', 'warnings', 'asf'}``, or ``None``
    and a phrase when it did not answer."""
    from asf import doctor
    child = doctor._clean_env() if env_vars is None else dict(
        {k: str(v) for k, v in env_vars.items()}, ASF_HOME=env.ASF_HOME)
    child.setdefault('PATH', '/usr/bin:/bin')
    try:
        p = subprocess.run([interpreter, '-c', _CHECK, os.path.abspath(path)],
                           capture_output=True, text=True, env=child, cwd=cwd, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return None, f'{interpreter} did not run ({e})'
    for line in reversed((p.stdout or '').strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and 'errors' in data:
            return data, ''
    err = (p.stderr or '').strip().splitlines()
    return None, err[-1] if err else f'exit {p.returncode}'


def _finding(kind, item):
    line, key, problem = (list(item) + ['', '', ''])[:3]
    at = f'line {line}' if line else 'file'
    return f'  {kind:<7} {at}: {key + " " if key else ""}{problem}'


def check(path, product_name, venv=None, out=print, cfg=None):
    """The command's body; returns the exit status."""
    if not os.path.isfile(path):
        out(f'product check: {path}: no such file')
        return 2
    interpreter, env_vars, cwd, label = target(product_name, venv, cfg)
    if not os.path.exists(interpreter):
        out(f'product check: {label}: {interpreter} is not on disk')
        return 2
    data, err = run_check(path, interpreter, env_vars, cwd)
    if data is None:
        out(f'product check: {path} under {label}: the loader did not answer ({err})')
        return 2
    from asf import installs
    sha = installs.commit_of(os.path.dirname(os.path.dirname(interpreter)))[:7]
    at = f' @ {sha}' if sha else ''
    errors, warnings = data.get('errors') or [], data.get('warnings') or []
    verdict = ('REFUSED' if errors else 'loads') + (
        f' ({len(warnings)} warning(s))' if warnings else '')
    out(f'product check: {path} under {label}{at}: {verdict}')
    for e in errors:
        out(_finding('error', e))
    for w in warnings:
        out(_finding('warning', w))
    return 1 if errors else 0


def cmd_product(args):
    if args.product_command == 'check':
        name = args.product or env.default_product_name()
        return check(args.file, name, venv=args.venv)
    return 2


def register(subparsers):
    p = subparsers.add_parser('product', help='the product file: check it before saving')
    sub = p.add_subparsers(dest='product_command', required=True)
    c = sub.add_parser('check', help="load <file> with the loader of the venv the product runs "
                                     '(its pin, else its clock) — the pre-save check')
    c.add_argument('file')
    env.add_product_arg(c)
    c.add_argument('--venv', help='check under this venv instead (a candidate, before a move)')
    c.set_defaults(run=cmd_product)
    return p
