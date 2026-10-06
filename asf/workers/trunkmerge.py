"""``asf trunk-check --pre-push`` — a session's push is checked the way CI's merge ref is.

A pull request's CI runs on its merge ref: the branch merged into the trunk, so the trunk's own
check list and scripts judge the branch. A session's pre-push ran the product's check in its own
worktree — the *branch's* copy of the scripts. A branch cut before the trunk added a step (a
product's ``gate-fast.sh`` gaining a forbidden-names step, 2026-10-02) passed its pre-push and
went red on that step in CI: a Task's plan branch, two visible red runs. Since launches stopped
rebasing (#329), every older branch carried that gap.

So after the product's own pre-push hook passes, the session's hook (:mod:`asf.workers.githooks`)
runs :func:`check` on each pushed branch head: a checkout of the head with ``origin/<trunk>``
merged into it (``git merge --no-commit --no-ff``) — what CI's merge ref holds — and the product's
``conventions.pre_push_check`` run there. A red check refuses the push, with the trunk's failing
step in its last lines; a merge that conflicts refuses it with "conflicts with trunk — rebase"
(the lane's rebuild, #570, rebases factory branches; a session rebases its own).

The checkout is cached per branch under ``<state>/trunk-merge/`` and reset between uses
(``reset --hard``, ``clean -fd``: ignored files — installed dependencies, build caches — stay),
so a second push of the branch pays for the check alone; a checkout unused for
:data:`STALE_S` is removed on the next call. Measured on a product's ``pnpm gate:fast``: 87 s on
a cold checkout (the package manager installs from its store, typecheck runs cold), 19 s warm.
"""
import fcntl
import os
import re
import shutil
import subprocess
import sys
import time

from asf import env, hermetic

#: the cached checkouts, under the product's state dir
HOLDER = 'trunk-merge'
#: a cached checkout unused this long is removed on the next call
STALE_S = 24 * 3600
#: the longest the check may run
TIMEOUT_S = 900
#: the last lines of a red check the refusal quotes
TAIL_LINES = 15
#: how long the trunk fetch may take before the check runs on the trunk ref the repo has
FETCH_TIMEOUT_S = 60
ZERO = re.compile(r'^0+$')


def _git(args, cwd, timeout=None):
    try:
        return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True,
                              env=hermetic.git_env(), timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(e.cmd, 124, '', 'timed out')


def _run(command, cwd, timeout):
    """``(returncode, output)`` of shell ``command`` in ``cwd``; None on a timeout."""
    p = subprocess.Popen(command, shell=True, cwd=cwd, env=hermetic.git_env(),
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out or ''
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, 9)
        except (ProcessLookupError, PermissionError):
            p.kill()
        out, _ = p.communicate()
        return None, out or ''


def slug(branch):
    return re.sub(r'[^A-Za-z0-9._-]+', '-', branch).strip('-') or 'head'


def prune(repo, holder, now=None, keep=()):
    """Removes each cached checkout under ``holder`` unused for :data:`STALE_S`."""
    now = time.time() if now is None else now
    try:
        names = os.listdir(holder)
    except OSError:
        return []
    gone = []
    for name in names:
        path = os.path.join(holder, name)
        if name in keep or not os.path.isdir(path):
            continue
        try:
            age = now - os.path.getmtime(path)
        except OSError:
            continue
        if age > STALE_S:
            _git(['worktree', 'remove', '--force', path], cwd=repo)
            shutil.rmtree(path, ignore_errors=True)
            gone.append(name)
    if gone:
        _git(['worktree', 'prune'], cwd=repo)
    return gone


def _checkout(repo, path, sha):
    """The cached checkout at ``path`` made a clean detached checkout of ``sha``; an error
    line, or ''."""
    if os.path.isdir(path) and _git(['rev-parse', '--git-dir'], cwd=path).returncode == 0:
        _git(['merge', '--abort'], cwd=path)
        steps = (['reset', '-q', '--hard'], ['clean', '-fdq'],
                 ['checkout', '-q', '--detach', '-f', sha], ['clean', '-fdq'])
        if all(_git(s, cwd=path).returncode == 0 for s in steps):
            return ''
        _git(['worktree', 'remove', '--force', path], cwd=repo)
    shutil.rmtree(path, ignore_errors=True)
    _git(['worktree', 'prune'], cwd=repo)
    add = _git(['worktree', 'add', '-q', '--detach', path, sha], cwd=repo)
    if add.returncode != 0:
        shutil.rmtree(path, ignore_errors=True)
        return (add.stderr or add.stdout).strip()
    return ''


def check(repo, holder, branch, sha, trunk, command, timeout=None, fetch=True):
    """``(ok, line)``: ``command`` run on ``sha`` with ``origin/<trunk>`` merged into it, in the
    cached checkout of ``branch`` under ``holder``. ``ok`` True when it passed; False when the
    merge conflicts or the command failed (``line`` says which, with the command's last
    :data:`TAIL_LINES` lines); None when no checkout could be made (nothing was judged)."""
    timeout = tunable('TIMEOUT_S') if timeout is None else timeout
    os.makedirs(holder, exist_ok=True)
    name = slug(branch)
    path = os.path.join(holder, name)
    with open(os.path.join(holder, f'.{name}.lock'), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if fetch:
            _git(['fetch', '-q', 'origin', f'+refs/heads/{trunk}:refs/remotes/origin/{trunk}'],
                 cwd=repo, timeout=tunable('FETCH_TIMEOUT_S'))
        if _git(['rev-parse', '-q', '--verify', f'origin/{trunk}^{{commit}}'],
                cwd=repo).returncode != 0:
            return None, f'origin/{trunk} does not resolve: nothing to merge'
        err = _checkout(repo, path, sha)
        if err:
            return None, f'no checkout of {sha[:9]} to merge origin/{trunk} into: {err}'
        try:
            os.utime(path)
            merge = _git(['-c', 'user.name=asf', '-c', 'user.email=asf@localhost', 'merge', '-q',
                          '--no-commit', '--no-ff', f'origin/{trunk}'], cwd=path)
            if merge.returncode != 0:
                files = _git(['diff', '--name-only', '--diff-filter=U'], cwd=path).stdout.split()
                tail = (merge.stderr or merge.stdout or '?').strip().splitlines() or ['?']
                why = f'conflicts in: {", ".join(files)}' if files else tail[-1]
                return False, (f'{branch} conflicts with trunk — rebase onto origin/{trunk} '
                               f'before pushing ({why})')
            rc, out = _run(command, path, timeout)
            if rc == 0:
                return True, ''
            state = f'timed out after {timeout}s' if rc is None else f'exit {rc}'
            lines = [ln for ln in out.splitlines() if ln.strip()][-TAIL_LINES:]
            return False, (f'`{command}` failed on {branch} merged with origin/{trunk} '
                           f'(what CI\'s merge ref runs: the trunk\'s check list and scripts): '
                           f'{state}' + (':\n' + '\n'.join(lines) if lines else ''))
        finally:
            _git(['merge', '--abort'], cwd=path)
            _git(['reset', '-q', '--hard'], cwd=path)
            prune(repo, holder, keep=(name,))


def pushed_heads(stdin_text, trunk):
    """``[(branch, sha)]`` of the branch heads a pre-push's ref lines push — deletes, tags and
    the trunk itself left out."""
    out = []
    for line in (stdin_text or '').splitlines():
        parts = line.split()
        if len(parts) < 4 or ZERO.match(parts[1]) or not parts[2].startswith('refs/heads/'):
            continue
        branch = parts[2][len('refs/heads/'):]
        if branch != trunk and (branch, parts[1]) not in out:
            out.append((branch, parts[1]))
    return out


def cmd_trunk_check(args, stdin=None, out=None):
    from asf import approvals
    out = out or sys.stderr
    name = getattr(args, 'product', None) or os.environ.get('ASF_PRODUCT')
    try:
        product = env.load_product(name)
    except env.ConfigError as e:
        print(f'asf trunk-check: no product ({e}) — not checked', file=out)
        return 0
    command = approvals.pre_push_check(product)
    if not command:
        return 0
    text = (stdin if stdin is not None else sys.stdin).read() if args.pre_push else ''
    trunk = product.main
    repo = _git(['rev-parse', '--show-toplevel'], cwd=os.getcwd()).stdout.strip() or os.getcwd()
    holder = os.path.join(env.state_dir(product), HOLDER)
    heads = pushed_heads(text, trunk) if args.pre_push else []
    if not args.pre_push:
        branch = _git(['rev-parse', '--abbrev-ref', 'HEAD'], cwd=repo).stdout.strip()
        sha = _git(['rev-parse', 'HEAD'], cwd=repo).stdout.strip()
        heads = [(branch if branch and branch != 'HEAD' else 'head', sha)] if sha else []
    for branch, sha in heads:
        started = time.time()
        ok, line = check(repo, holder, branch, sha, trunk, command)
        took = time.time() - started
        if ok is False:
            print(f'asf: push refused — {line}', file=out)
            return 1
        if ok is None:
            print(f'asf trunk-check: {line} — not checked', file=out)
        else:
            print(f'asf trunk-check: {branch} merged with origin/{trunk} passes `{command}` '
                  f'({took:.0f}s)', file=out)
    return 0


def register(sub):
    """``asf trunk-check [--pre-push] [--product P]``."""
    p = sub.add_parser('trunk-check', help='run the pre-push check on the branch merged with '
                                           'origin/<trunk>, as CI\'s merge ref runs it')
    p.add_argument('--pre-push', action='store_true',
                   help='read the pre-push hook\'s ref lines on stdin and check each pushed head')
    env.add_product_arg(p)
    p.set_defaults(func=cmd_trunk_check)


# ---- tunables ---------------------------------------------------------------

#: The config key (``~/.ASF/config.yaml``) over each constant above; the constant is its default.
TUNABLES = {
    'TIMEOUT_S': 'merge.timeout_s',
    'FETCH_TIMEOUT_S': 'merge.fetch_timeout_s',
}


def tunable(name):
    """The constant ``name`` of :data:`TUNABLES` with its config key over it."""
    from asf import config_keys
    return config_keys.value(TUNABLES[name], globals()[name])
