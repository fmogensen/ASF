"""asf.gitpush — a factory push of a ref that carries no new code skips the product's pre-push
hook (``--no-verify``), and every factory push is killed after ``git.push_timeout_s``: the ref is
logged and left as it was, and the caller goes on (2026-09-26: a product's archive push sat 8+
minutes in its hook inside the tick's health step)."""
import ast
import glob
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from asf import gitpush, hermetic, refguard
from asf.conventions import DEFAULT_PUSH_TIMEOUT_S, DEFAULT_REF_PUSH_TIMEOUT_S, Conventions
from asf.env import Product
from asf.workers import lifecycle as lc
from tests import test_lifecycle as tl


def git(args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def hook(repo, body):
    """``repo``'s pre-push hook (under ``core.hooksPath``, as a product sets it)."""
    hooks = os.path.join(repo, '.hooks')
    os.makedirs(hooks, exist_ok=True)
    path = os.path.join(hooks, 'pre-push')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('#!/bin/sh\n' + body)
    os.chmod(path, 0o755)
    git(['config', 'core.hooksPath', hooks], repo)


def receive_hook(origin, body):
    """``origin``'s ``pre-receive`` hook — the server side, which ``--no-verify`` does not skip,
    unlike the client's own ``pre-push`` (:func:`hook`). What hangs a ref-only push in a test."""
    path = os.path.join(origin, 'hooks', 'pre-receive')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('#!/bin/sh\n' + body)
    os.chmod(path, 0o755)


#: the guard an ordinary branch push passes: the defaults, refusing
G = refguard.Guard()


class PushTest(unittest.TestCase):

    def setUp(self):
        base = tempfile.mkdtemp(prefix='gitpush_')
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.origin, self.repo = os.path.join(base, 'origin.git'), os.path.join(base, 'repo')
        git(['init', '-q', '--bare', '-b', 'main', self.origin], base)
        git(['clone', '-q', self.origin, self.repo], base)
        for k, v in (('user.name', 'T'), ('user.email', 't@example.com'),
                     ('commit.gpgsign', 'false')):
            git(['config', k, v], self.repo)
        git(['commit', '-q', '--allow-empty', '-m', 'seed'], self.repo)
        git(['push', '-q', 'origin', 'HEAD:main'], self.repo)
        self.sha = git(['rev-parse', 'HEAD'], self.repo)

    def heads(self):
        return git(['for-each-ref', '--format=%(refname:short)', 'refs/heads'], self.origin).split()

    def test_a_ref_only_push_skips_the_products_hook_and_a_content_push_runs_it(self):
        hook(self.repo, 'echo "lint: red" >&2\nexit 1\n')
        r = gitpush.push(['-q', 'origin', f'{self.sha}:refs/heads/archive/x'], self.repo,
                         refs_only=True, guard=G)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('--no-verify', r.args)
        self.assertIn('archive/x', self.heads())
        r = gitpush.push(['-q', 'origin', ':refs/heads/archive/x'], self.repo, refs_only=True,
                         guard=G)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('archive/x', self.heads())
        r = gitpush.push(['-q', 'origin', f'{self.sha}:refs/heads/work'], self.repo, guard=G)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn('--no-verify', r.args)
        self.assertIn('lint: red', r.stderr)

    def test_a_hanging_push_times_out_names_the_ref_and_leaves_it(self):
        hook(self.repo, 'sleep 30\nexit 0\n')
        lines = []
        started = time.monotonic()
        r = gitpush.push(['-q', 'origin', f'{self.sha}:refs/heads/work'], self.repo, timeout=1,
                         log=lines.append, guard=G)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(r.returncode, gitpush.TIMED_OUT)
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith('push timed out after 1s:'), lines)
        self.assertIn('refs/heads/work', lines[0])
        self.assertIn(lines[0], r.stderr)
        self.assertNotIn('work', self.heads())

    def test_the_child_env_is_hermetic_git_env(self):
        # F-0013 D4: gitpush no longer builds its own hook-variable list — one remover, shared
        # with the harvest gate and everything else that spawns a git child.
        base = dict(os.environ, GIT_DIR='/elsewhere/.git', GIT_QUARANTINE_PATH='/elsewhere/q',
                    FAKE_VAR='keep')
        captured = {}

        class FakePopen:
            def __init__(self, *args, **kwargs):
                captured['env'] = kwargs['env']
                self.pid = 1
                self.returncode = 0

            def communicate(self, timeout=None):
                return '', ''

        with mock.patch.object(gitpush.subprocess, 'Popen', FakePopen):
            gitpush.push(['-q', 'origin', f'{self.sha}:refs/heads/x'], self.repo, env=base,
                         guard=G)
        self.assertEqual(captured['env'], hermetic.git_env(base))


class PushBudgetTests(unittest.TestCase):
    """Two budgets, and each default is the one F-0165 names: a hooked push — a session's branch
    published, the trunk, a lane rewrite — is bounded by the product's own lint, typecheck and
    build; a hookless one — an archive head, a delete, a tag — has only the network in it. One
    number for both meant 120 s, and 120 s killed real publishes on a loaded host."""

    setUp, heads = PushTest.setUp, PushTest.heads

    def test_the_defaults_by_value(self):
        self.assertEqual(DEFAULT_PUSH_TIMEOUT_S, 900)
        self.assertEqual(DEFAULT_REF_PUSH_TIMEOUT_S, 120)

    def test_push_timeout_picks_the_default_by_kind(self):
        self.assertEqual(gitpush.push_timeout(), DEFAULT_PUSH_TIMEOUT_S)
        self.assertEqual(gitpush.push_timeout(refs_only=True), DEFAULT_REF_PUSH_TIMEOUT_S)

    def test_the_split_on_one_conventions_object(self):
        conv = Conventions.from_mapping({'git': {'push_timeout_s': 900,
                                                  'ref_push_timeout_s': 30}})
        self.assertEqual(gitpush.push_timeout(conv), 900)
        self.assertEqual(gitpush.push_timeout(conv, refs_only=True), 30)
        # the operator's hand fix: only the hooked key is set — it no longer widens a ref push
        hand_fix = Conventions.from_mapping({'git': {'push_timeout_s': 900}})
        self.assertEqual(gitpush.push_timeout(hand_fix), 900)
        self.assertEqual(gitpush.push_timeout(hand_fix, refs_only=True),
                         DEFAULT_REF_PUSH_TIMEOUT_S)

    def test_both_keys_survive_from_mapping_and_neither_lands_in_extra(self):
        conv = Conventions.from_mapping({'git': {'push_timeout_s': 30, 'ref_push_timeout_s': 60,
                                                  'later': 'x'}})
        self.assertEqual(conv.push_timeout_s, 30)
        self.assertEqual(conv.ref_push_timeout_s, 60)
        self.assertEqual(conv.extra, {'git': {'later': 'x'}})
        self.assertEqual(Conventions.from_mapping({'git': {}}), Conventions())

    def test_a_junk_value_falls_back_to_its_own_default(self):
        junk = Conventions.from_mapping(
            {'git': {'push_timeout_s': 'soon', 'ref_push_timeout_s': 'now'}})
        self.assertEqual(gitpush.push_timeout(junk), DEFAULT_PUSH_TIMEOUT_S)
        self.assertEqual(gitpush.push_timeout(junk, refs_only=True), DEFAULT_REF_PUSH_TIMEOUT_S)

    def test_the_kind_of_push_picks_the_clock(self):
        # the origin's pre-receive hook is what --no-verify cannot skip (PD8): it hangs both
        # kinds of push alike, so only the two keys decide which one survives
        receive_hook(self.origin, 'sleep 2\nexit 0\n')
        conv = Conventions.from_mapping({'git': {'push_timeout_s': 1, 'ref_push_timeout_s': 60}})
        r = gitpush.push(['-q', 'origin', f'{self.sha}:refs/heads/work'], self.repo, conv=conv,
                         guard=G)
        self.assertEqual(r.returncode, gitpush.TIMED_OUT, r.stderr)
        r = gitpush.push(['-q', 'origin', f'{self.sha}:refs/heads/archive/a'], self.repo,
                         refs_only=True, conv=conv, guard=G)
        self.assertEqual(r.returncode, 0, r.stderr)
        conv2 = Conventions.from_mapping({'git': {'push_timeout_s': 60,
                                                   'ref_push_timeout_s': 1}})
        r = gitpush.push(['-q', 'origin', f'{self.sha}:refs/heads/work2'], self.repo, conv=conv2,
                         guard=G)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = gitpush.push(['-q', 'origin', f'{self.sha}:refs/heads/archive/b'], self.repo,
                         refs_only=True, conv=conv2, guard=G)
        self.assertEqual(r.returncode, gitpush.TIMED_OUT, r.stderr)

    def test_the_timed_out_line_names_the_key_to_raise(self):
        receive_hook(self.origin, 'sleep 30\nexit 0\n')
        lines = []
        r = gitpush.push(['-q', 'origin', f'{self.sha}:refs/heads/work'], self.repo, timeout=1,
                         log=lines.append, guard=G)
        self.assertEqual(r.returncode, gitpush.TIMED_OUT)
        self.assertTrue(lines[0].startswith('push timed out after 1s:'), lines)
        self.assertTrue(lines[0].endswith('(raise git.push_timeout_s)'), lines)
        lines2 = []
        r = gitpush.push(['-q', 'origin', f'{self.sha}:refs/heads/archive/x'], self.repo,
                         refs_only=True, timeout=1, log=lines2.append, guard=G)
        self.assertEqual(r.returncode, gitpush.TIMED_OUT)
        self.assertTrue(lines2[0].startswith('push timed out after 1s:'), lines2)
        self.assertTrue(lines2[0].endswith('(raise git.ref_push_timeout_s)'), lines2)


class OneResolverTests(unittest.TestCase):
    """The ``ast`` fence (C10, P14): ``push_timeout`` is ``gitpush``'s own, and no call site in
    ``asf/`` may newly resolve its own budget — the flag that skips the hook is the flag that
    sizes the clock, and a caller that computes a timeout can hand a hooked number to a ref push,
    which is F-0165's bug. Two sites are still owed that cleanup and are named below: the fence
    pins them exactly, so they can only shrink."""

    def test_no_call_site_resolves_its_own_push_budget(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        resolvers, explicit = [], []
        for path in sorted(glob.glob(os.path.join(root, 'asf', '**', '*.py'), recursive=True)):
            # Closed: an unclosed handle per file under asf/ is 140 kB of ResourceWarning, and
            # tools/run_tests.py reads a part's pipe only once that process exits — past the
            # pipe's 64 kB the child blocks on the write and the part hangs until CI kills it.
            with open(path, encoding='utf-8') as f:
                tree = ast.parse(f.read(), filename=path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, 'attr', None) or getattr(node.func, 'id', None)
                relpath = os.path.relpath(path, root).replace(os.sep, '/')
                if name == 'push_timeout' and relpath != 'asf/gitpush.py':
                    resolvers.append(f'{relpath}:{node.lineno}')
                if name == 'push' and getattr(getattr(node.func, 'value', None), 'id', '') == \
                        'gitpush':
                    explicit += [f'{relpath}:{node.lineno}' for k in node.keywords
                                if k.arg == 'timeout']
        # Two sites remain, one each: both compute the hooked budget only to hand it to
        # mechanical.publish_worktree's documented push_timeout_s, which takes no conv= to pass
        # instead. Closing them is one keyword on publish_worktree in asf/harvest/mechanical.py,
        # outside this item's writes: — see the session report's needs writes. A third site, or
        # one in any other file, is F-0165's bug coming back and fails here.
        self.assertEqual(sorted({r.split(':')[0] for r in resolvers}),
                         ['asf/harvest/mechanical.py', 'asf/workers/health.py'],
                         'push_timeout is gitpush\'s own: pass conv= instead')
        self.assertEqual(len(resolvers), 2, resolvers)
        # publish's documented push_timeout_s, and only it: _push_retrying forwards that one
        # number at both of its pushes — the first try and the retry after a network blip.
        self.assertEqual(len(explicit), 2, explicit)
        self.assertTrue(all(e.startswith('asf/workers/lifecycle.py') for e in explicit), explicit)


class RefGuardDoorTest(unittest.TestCase):
    """W4-PR1: every push passes a guard keyed on the repository it pushes. A target that is
    that repository's trunk or a protected ref is refused (``refuse``, before any git runs) or
    warned about (``warn``, the push proceeds); a landing path's door and the record pass."""

    setUp, heads = PushTest.setUp, PushTest.heads

    def push(self, target, guard, *pre):
        lines = []
        r = gitpush.push(['-q', *pre, 'origin', target], self.repo, refs_only=True,
                         guard=guard, log=lines.append)
        return r, lines

    def test_a_push_to_the_trunk_is_refused_without_a_subprocess(self):
        with mock.patch.object(gitpush.subprocess, 'Popen') as popen:
            r, lines = self.push(f'{self.sha}:refs/heads/main', refguard.Guard('main'))
        popen.assert_not_called()
        self.assertEqual(r.returncode, 1)
        self.assertNotEqual(r.returncode, gitpush.TIMED_OUT)
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith('REF GUARD: refused push → main'), lines)
        self.assertEqual(r.stderr, lines[0])

    def test_warn_says_so_and_the_push_proceeds(self):
        git(['commit', '-q', '--allow-empty', '-m', 'two'], self.repo)
        two = git(['rev-parse', 'HEAD'], self.repo)
        r, lines = self.push(f'{two}:refs/heads/main', refguard.Guard('main', mode='warn'))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith('REF GUARD (warn): push → main'), lines)
        self.assertEqual(git(['rev-parse', 'main'], self.origin), two)

    def test_off_is_silent(self):
        self.assertEqual(refguard.Guard('main', mode='off').refusal('x:refs/heads/main'), '')

    def test_every_spelling_of_a_protected_target(self):
        g = refguard.Guard('main')
        for target in (':main', ':refs/heads/main', f'+{self.sha}:release/1', 'HEAD:main',
                       'refs/heads/master'):
            self.assertTrue(g.refusal(target), target)
        for target in (f'{self.sha}:refs/heads/worker/x', 'refs/tags/v1.0.0', ':archive/x',
                       f'+{self.sha}:refs/asf/briefs/j'):
            self.assertEqual(g.refusal(target), '', target)
        # --delete <name>: the name is the target
        self.assertEqual(gitpush.targets(['-q', '--force-with-lease=refs/heads/main:x',
                                          'origin', '--delete', 'main']), ['main'])
        with mock.patch.object(gitpush.subprocess, 'Popen') as popen:
            r, _ = self.push('main', g, '--delete')
        popen.assert_not_called()
        self.assertEqual(r.returncode, 1)

    def test_a_worker_branch_is_allowed(self):
        r, lines = self.push(f'{self.sha}:refs/heads/worker/x', refguard.Guard('main'))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(lines, [])
        self.assertIn('worker/x', self.heads())

    def test_a_door_advances_the_trunk(self):
        git(['commit', '-q', '--allow-empty', '-m', 'two'], self.repo)
        two = git(['rev-parse', 'HEAD'], self.repo)
        r, lines = self.push(f'{two}:refs/heads/main', refguard.Guard('main', door=True))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(lines, [])
        self.assertEqual(git(['rev-parse', 'main'], self.origin), two)

    def test_protected_refs_and_a_develop_trunk(self):
        conv = Conventions.from_mapping({'protected_refs': ['stable/*'],
                                         'flags': {'refguard': 'refuse'}})
        g = refguard.guard_from('develop', conv)
        self.assertEqual(g.mode, 'refuse')
        self.assertTrue(g.refusal('x:refs/heads/develop'))
        self.assertTrue(g.refusal('x:refs/heads/stable/2'))
        self.assertEqual(g.refusal('x:refs/heads/main'), '')     # listed refs replace defaults
        self.assertEqual(g.refusal('x:refs/heads/release/1'), '')
        with mock.patch.object(gitpush.subprocess, 'Popen') as popen:
            r, _ = self.push(f'{self.sha}:refs/heads/develop', g)
        popen.assert_not_called()
        self.assertEqual(r.returncode, 1)

    def test_the_mode_is_the_flag_and_defaults_off(self):
        product = Product('p', {'repo_dir': self.repo, 'main': 'main', 'conventions': {}})
        self.assertEqual(refguard.guard_for(product).mode, 'off')
        self.assertEqual(refguard.DEFAULT_MODE, 'off')
        for value, mode in (('warn', 'warn'), ('refuse', 'refuse'), ('REFUSE', 'refuse'),
                            ('nonsense', 'off'), (True, 'off')):
            product = Product('p', {'repo_dir': self.repo, 'main': 'main',
                                    'conventions': {'flags': {'refguard': value}}})
            self.assertEqual(refguard.guard_for(product, self.repo).mode, mode, value)
        self.assertTrue(refguard.guard_for(product, self.repo, door=True).door)

    def test_the_record_guard_protects_nothing(self):
        git(['commit', '-q', '--allow-empty', '-m', 'record'], self.repo)
        two = git(['rev-parse', 'HEAD'], self.repo)
        r, lines = self.push('HEAD:main', refguard.RECORD)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(lines, [])
        self.assertEqual(git(['rev-parse', 'main'], self.origin), two)

    def test_the_door_is_declared_for_the_clients_lint(self):
        self.assertIs(gitpush.__gitpush_door__, True)
        import inspect
        param = inspect.signature(gitpush.push).parameters['guard']
        self.assertEqual(param.kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIs(param.default, inspect.Parameter.empty)


class PublishArchiveSkipsTheHookTest(unittest.TestCase):
    """The health step's publish of a session's rebased branch archives the old tip first
    (``archive/<b>-copies-<sha9>``): that push skips the product's hook; the branch's own push
    runs it, and neither may hold the step past its timeout."""

    sh, commit = tl.PublishACopiesRebaseTest.sh, tl.PublishACopiesRebaseTest.commit

    def setUp(self):
        tl.PublishACopiesRebaseTest.setUp(self)  # a rebased head whose old tip needs archiving
        self.ran = os.path.join(os.path.dirname(self.repo), 'hook-ran')
        # the product's hook: it refuses any archive ref and records every push it judged
        hook(self.repo, f'while read -r _l _s ref _o; do echo "$ref" >> "{self.ran}"; '
                        'case "$ref" in refs/heads/archive/*) echo "lint: 8 minutes" >&2; '
                        'exit 1;; esac; done\nexit 0\n')

    def test_the_archive_push_skips_the_hook_and_the_branch_push_runs_it(self):
        ok, line = lc.publish(self.repo, 'fix/B-7777', self.remote_sha, main='main')
        self.assertTrue(ok, line)
        archive = f'archive/fix/B-7777-copies-{self.remote_sha[:9]}'
        self.assertEqual(self.sh(['ls-remote', '--heads', 'origin', archive],
                                 self.repo).split()[0], self.remote_sha)
        with open(self.ran, encoding='utf-8') as f:
            self.assertEqual(f.read().split(), ['refs/heads/fix/B-7777'])

    def test_a_hanging_hook_times_out_and_publish_returns(self):
        hook(self.repo, 'sleep 30\nexit 0\n')
        started = time.monotonic()
        ok, line = lc.publish(self.repo, 'fix/B-7777', self.remote_sha, main='main',
                              push_timeout_s=1)
        self.assertLess(time.monotonic() - started, 15)
        self.assertFalse(ok, line)
        self.assertIn('push timed out after 1s', line)
        self.assertEqual(self.sh(['ls-remote', '--heads', 'origin', 'fix/B-7777'],
                                 self.repo).split()[0], self.remote_sha)
        # the archive push is hookless (refs_only) — the hanging hook never touches it, and it
        # goes through at the short budget while the branch push in front of it times out (C5)
        archive = f'archive/fix/B-7777-copies-{self.remote_sha[:9]}'
        self.assertEqual(self.sh(['ls-remote', '--heads', 'origin', archive],
                                 self.repo).split()[0], self.remote_sha)


class PublishSecondsTests(unittest.TestCase):
    """The hook's seconds are on the publish line (F-0165): ``gitpush.push`` reports how long a
    push took, timed out or not, and ``publish`` puts the branch push's seconds — never the
    archive push's — on its line, so the next operator who raises the budget raises it from a
    measurement rather than a guess."""

    sh, commit = tl.PublishACopiesRebaseTest.sh, tl.PublishACopiesRebaseTest.commit

    def setUp(self):
        tl.PublishACopiesRebaseTest.setUp(self)  # a rebased head whose old tip needs archiving
        hook(self.repo, 'sleep 2\nexit 0\n')  # the branch push runs this; the archive one skips it

    def test_seconds_is_a_float_on_a_push_that_went_through_and_one_that_timed_out(self):
        r = gitpush.push(['-q', 'origin', f'{self.remote_sha}:refs/heads/archive/seconds'],
                         self.repo, refs_only=True, guard=G)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsInstance(r.seconds, float)
        r = gitpush.push(['-q', 'origin', f'{self.remote_sha}:refs/heads/fix/seconds'],
                         self.repo, timeout=1, guard=G)
        self.assertEqual(r.returncode, gitpush.TIMED_OUT)
        self.assertIsInstance(r.seconds, float)
        self.assertGreaterEqual(r.seconds, 1)

    def test_publishs_line_carries_the_branch_pushs_seconds_not_the_archives(self):
        ok, line = lc.publish(self.repo, 'fix/B-7777', self.remote_sha, main='main')
        self.assertTrue(ok, line)
        self.assertRegex(line, r'published \S+ at [0-9a-f]+ in \d+\.\d+s')
        seconds = float(re.search(r'in (\d+\.\d+)s', line).group(1))
        self.assertGreaterEqual(seconds, 1.5)  # the hook's ~2s, not the archive push's instant

    def test_the_rebased_variant_keeps_its_clause(self):
        ok, line = lc.publish(self.repo, 'fix/B-7777', self.remote_sha, main='main')
        self.assertTrue(ok, line)
        self.assertTrue(line.startswith('rebased off trunk copies'), line)
        self.assertRegex(line, r'and pushed: published fix/B-7777 at [0-9a-f]+ in \d+\.\d+s$')


if __name__ == '__main__':
    unittest.main()
