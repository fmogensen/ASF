"""The landing fact (asf.facts.landing, W6-PR2): its rules on a real git repo, the shadow seam
the workers' deciders call (always the old answer; a disagreement logged once a day), the
run-level shadow of ``lifecycle.landed``/``landed_earlier``, the tick's one prime per pass, and
the offline replay. The scenario rows are ``tests/scenarios/test_facts_shadow.py``."""
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from asf import env, gh_limit
from asf.facts import cache, disagree, landing as fl
from asf.facts.types import AsOf, Landed, NotLanded, OpenPrs, Unknown
from asf.workers import landing, lifecycle

ITEM = 'T-0007'


class Prod:
    def __init__(self, repo, facts='shadow', name='p'):
        from asf.conventions import Conventions
        self.name, self.repo_dir, self.main, self.repo_slug = name, repo, 'main', 'o/p'
        self.conventions = Conventions()
        self.conventions.extra['flags'] = {'facts': facts} if facts else {}
        self.backlog_dir = None

    def flag(self, name, default=None):
        return self.conventions.flag(name, default)


class _Repo(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='asf-facts-landing-')
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self._home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.d, 'home')
        self.addCleanup(setattr, env, 'ASF_HOME', self._home)
        cache.clear()
        self.addCleanup(cache.clear)
        gh_limit.reset()
        self.repo = os.path.join(self.d, 'repo')
        os.makedirs(self.repo)
        self.git('init', '-q')
        self.base = self.commit('chore: start')
        self.trunk()
        self.prod = Prod(self.repo)
        self.path = os.path.join(env.state_dir(self.prod.name), 'sessions.jsonl')
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        open(self.path, 'w').close()

    def git(self, *a):
        return subprocess.run(['git', *a], cwd=self.repo, check=True, capture_output=True,
                              text=True).stdout.strip()

    def commit(self, msg, path=None):
        if path:
            with open(os.path.join(self.repo, path), 'a') as f:
                f.write(msg + '\n')
            self.git('add', path)
        self.git('-c', 'user.email=a@b', '-c', 'user.name=a', 'commit', '-q', '--allow-empty',
                 '-m', msg)
        return self.git('rev-parse', 'HEAD')

    def trunk(self):
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')

    def write(self, *lines):
        with open(self.path, 'a') as f:
            for ln in lines:
                f.write(json.dumps(ln) + '\n')

    def fact(self, **kw):
        kw.setdefault('open_prs', OpenPrs((), AsOf.now()))
        kw.setdefault('writes', [])
        return fl.landed(self.prod, ITEM, path=self.path, **kw)

    def log(self):
        return [r for r in disagree.records(self.prod) if r.get('fact') == fl.FACT]


class FactRules(_Repo):

    def test_a_commit_naming_the_item_lands_it(self):
        sha = self.commit(f'task({ITEM}): the work')
        self.trunk()
        got = self.fact()
        self.assertIsInstance(got, Landed)
        self.assertEqual((got.sha, got.by, got.as_of.head), (sha, 'names', sha))

    def test_a_commit_only_covering_the_writes_is_a_hint(self):
        sha = self.commit('chore: shared helpers', path='lines.py')
        self.trunk()
        got = self.fact(writes=['lines.py'], sha=sha)  # a recorded landing that only covers
        self.assertIsInstance(got, NotLanded)
        self.assertEqual(got.hint_sha, sha)

    def test_a_reverted_landing_is_not_landed(self):
        sha = self.commit(f'task({ITEM}): the work')
        rev = self.commit(f'Revert "task({ITEM}): the work"\n\nThis reverts commit {sha}.')
        self.trunk()
        got = self.fact()
        self.assertIsInstance(got, NotLanded)
        self.assertIn(f'reverted by {rev[:9]}', got.why)

    def test_a_voided_landing_is_never_landed(self):
        sha = self.commit(f'task({ITEM}): the work')
        self.trunk()
        self.write({'job': 'coder-t-0007', 'item': ITEM, 'branch': 'w/t-0007',
                    'started': '2026-01-01T00:00:00Z', 'ended': '2026-01-01T01:00:00Z',
                    'harvested': sha})
        lifecycle.note_reset(self.path, ITEM, {'pr': None, 'head': sha, 'why': 'not its work'},
                             now='2026-01-02T00:00:00Z', alive=lambda *_a, **_k: False)
        got = self.fact(sha=sha)
        self.assertIsInstance(got, NotLanded)
        self.assertIn('voided', got.why)

    def test_unmerged_work_on_a_run_branch_is_not_landed(self):
        self.commit(f'task({ITEM}): part one')
        self.trunk()
        self.git('checkout', '-q', '-b', 'work')
        self.commit(f'task({ITEM}): part two, unmerged')
        self.git('update-ref', 'refs/remotes/origin/w/t-0007', 'HEAD')
        self.write({'job': 'coder-t-0007', 'item': ITEM, 'branch': 'w/t-0007',
                    'started': '2026-01-01T00:00:00Z'})
        got = self.fact()
        self.assertIsInstance(got, NotLanded)
        self.assertEqual(got.why, 'unmerged work on w/t-0007')

    def test_an_open_pr_naming_the_item_is_unmerged_work(self):
        self.commit(f'task({ITEM}): the work')
        self.trunk()
        prs = OpenPrs(({'number': 3, 'title': f'{ITEM} — more', 'headRefName': 'x',
                        'headRefOid': 'f' * 40},), AsOf.now())
        got = self.fact(open_prs=prs)
        self.assertIsInstance(got, NotLanded)
        self.assertIn('unmerged work on x', got.why)

    def test_the_open_prs_not_read_this_pass_are_unknown_never_a_call(self):
        self.commit(f'task({ITEM}): the work')
        self.trunk()
        with mock.patch('asf.github.gh', side_effect=AssertionError('no gh in a fact')):
            got = fl.landed(self.prod, ITEM, path=self.path, writes=[])
        self.assertIsInstance(got, Unknown)
        self.assertIn('not read this tick', got.reason)

    def test_no_trunk_is_unknown(self):
        self.git('update-ref', '-d', 'refs/remotes/origin/main')
        self.assertIsInstance(self.fact(), Unknown)


class Shadow(_Repo):

    def test_old_mode_never_runs_the_fact(self):
        prod = Prod(self.repo, facts='old')
        got = fl.shadow(prod, fl.TRUNKCLOSE, ITEM, 'old answer',
                        lambda: self.fail('the fact ran under old'))
        self.assertEqual(got, 'old answer')
        self.assertEqual(disagree.records(prod), [])

    def test_shadow_returns_old_and_logs_a_disagreement_once(self):
        new = NotLanded('no trunk commit attributable to it', AsOf.now('abc'))
        for _ in range(3):
            self.assertIs(fl.shadow(self.prod, fl.VERIFY, ITEM, True, lambda: new), True)
            cache.clear()  # a new pass: the log itself is read back
        log = self.log()
        self.assertEqual(len(log), 1, log)
        self.assertEqual((log[0]['decider'], log[0]['key'], log[0]['old']),
                         (fl.VERIFY, ITEM, True))
        self.assertEqual(log[0]['new']['fact'], 'NotLanded')

    def test_agreement_logs_nothing(self):
        new = Landed('a' * 40, 'names', AsOf.now())
        fl.shadow(self.prod, fl.VERIFY, ITEM, True, lambda: new)
        self.assertEqual(self.log(), [])

    def test_a_fact_that_raises_is_logged_never_raised(self):
        def boom():
            raise RuntimeError('x')
        self.assertEqual(fl.shadow(self.prod, fl.RELAUNCH, ITEM, '', boom), '')
        self.assertEqual(self.log()[0]['new'], 'error:RuntimeError')

    def test_new_mode_still_answers_old_until_the_cutover(self):
        prod = Prod(self.repo, facts='new')
        got = fl.shadow(prod, fl.VERIFY, ITEM, False,
                        lambda: Landed('a' * 40, 'names', AsOf.now()))
        self.assertIs(got, False)
        self.assertEqual(len(disagree.records(prod)), 1)

    def test_verify_landings_is_shadowed(self):
        sha = self.commit('chore: not its commit')
        self.trunk()
        occ = {'landed': {ITEM: sha}, 'landed_on': {ITEM: 'w/t-0007'}}
        cache.put(self.prod, cache.OPEN_PRS, '', '', OpenPrs((), AsOf.now()))
        # the decider calls it unverified; the fact agrees (not its commit): nothing logged
        got = landing.verify_landings(self.prod, occ, {ITEM: {'type': 'task'}}, path=self.path)
        self.assertEqual(got[0], {})
        self.assertIn(ITEM, got[1])
        self.assertEqual(self.log(), [])


class RunShadow(_Repo):

    def run_line(self, sha, **kw):
        line = {'job': 'coder-t-0007', 'item': ITEM, 'branch': 'w/t-0007',
                'started': '2026-01-01T00:00:00Z', 'ended': '2026-01-01T01:00:00Z',
                'harvested': sha, **kw}
        self.write(line)
        return line

    def test_unbound_costs_nothing(self):
        run = self.run_line('a' * 40)
        with mock.patch.object(fl, 'landed_run', side_effect=AssertionError('asked')):
            self.assertTrue(lifecycle.landed(run))

    def test_a_reverted_harvest_is_logged_and_still_landed(self):
        sha = self.commit(f'task({ITEM}): the work')
        self.commit(f'Revert "x"\n\nThis reverts commit {sha}.')
        self.trunk()
        run = self.run_line(sha)
        fl.bind(self.prod)
        self.assertTrue(lifecycle.landed(run))
        self.assertTrue(lifecycle.landed(run))
        log = self.log()
        self.assertEqual(len(log), 1, log)
        self.assertEqual(log[0]['decider'], fl.LIFECYCLE)
        self.assertIn('reverted by', log[0]['new']['why'])

    def test_a_superseded_mark_is_not_asked(self):
        run = self.run_line('superseded')
        fl.bind(self.prod)
        self.assertTrue(lifecycle.landed(run))
        self.assertEqual(self.log(), [])

    def test_landed_earlier_is_shadowed(self):
        sha = self.commit(f'task({ITEM}): the work')
        self.commit(f'Revert "x"\n\nThis reverts commit {sha}.')
        self.trunk()
        self.run_line(sha)
        later = {'job': 'coder-t-0007-2', 'item': ITEM, 'branch': 'w/t-0007',
                 'started': '2026-01-03T00:00:00Z'}
        fl.bind(self.prod)
        self.assertEqual(lifecycle.landed_earlier(self.path, later), sha)
        self.assertEqual([r['decider'] for r in self.log()], [fl.EARLIER])


class Prime(_Repo):

    def test_the_tick_primes_only_a_shadowed_product(self):
        from asf.tick import tick
        calls = []

        def fake(slug, **kw):
            calls.append((slug, kw.get('fields')))
            from asf.github import Result
            return Result(True, data=[], as_of='2026-01-01T00:00:00+00:00')
        with mock.patch('asf.github.open_prs', fake):
            tick.Context(Prod(self.repo, facts='old'))
            self.assertEqual(calls, [])
            self.assertIsNone(fl.bound())
            tick.Context(self.prod)
        self.assertEqual(len(calls), 1)
        self.assertIn('title', calls[0][1])
        self.assertIs(fl.bound(), self.prod)
        self.assertEqual(cache.open_prs(self.prod).prs, ())


class Replay(_Repo):

    def test_replay_names_the_covers_only_close(self):
        cover = self.commit('chore: shared helpers', path='lines.py')
        self.trunk()
        log = os.path.join(self.d, 'run.log.jsonl')
        text = ('```\nREPORT\nitem: T-0007\nkind: coder\nstatus: done\nbranch: w/t-0007\n'
                f'pushed: none\ncommits: none\ntests: none\nleft out: on origin/main under '
                f'{cover[:9]}\nneeds writes: none\nproves: none\n```')
        with open(log, 'w') as f:
            f.write(json.dumps({'type': 'result', 'subtype': 'success', 'result': text}) + '\n')
        self.write({'job': 'coder-t-0007', 'item': ITEM, 'kind': 'coder', 'branch': 'w/t-0007',
                    'pid': 1, 'log': log, 'started': '2026-01-01T00:00:00Z',
                    'launch_head': self.base},
                   {'job': 'coder-t-0007', 'ended': '2026-01-01T01:00:00Z',
                    'end_reason': 'finished'})
        out = []
        with mock.patch.object(landing, 'item_writes', lambda _p, _i: ['lines.py']):
            found = fl.replay(self.prod, '2026-01-01T00:00:00Z', out=out.append)
        self.assertEqual([k for k, *_ in found[fl.TRUNKCLOSE]], [ITEM])
        self.assertIn('1 disagreement(s)', out[-1])

    def test_the_command_is_registered(self):
        from asf import cli
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit):
            cli.main(['facts', '--help'])
        self.assertIn('replay', buf.getvalue())


if __name__ == '__main__':
    unittest.main()
