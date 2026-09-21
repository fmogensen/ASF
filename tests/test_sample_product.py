"""The dogfood-in-CI test: ASF runs a second product — ``sample/`` — end to end.

``sample/`` carries conventions of its own (``feature/`` and ``bugfix/`` branches, ``specs/`` and
``plans/`` at the repo root, no CI, no deploy, the fake worker runtime) and a record of ten cards.
The test copies it into a temp dir, makes bare origins for the repo and the record, writes a temp
``ASF_HOME``, then drives the real ``asf`` command line — ``init``, one ``tick``, ``next``,
``doctor`` — as a subprocess. No network, no account, no agent: a literal left over from the first
product shows up here as a wrong branch, a missing launch or a failed step.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, 'sample')


def _git(args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _publish(tree, origin):
    """``tree`` becomes a git repo whose ``main`` is pushed to a new bare ``origin``."""
    _git(['init', '-q', '--bare', '-b', 'main', origin], cwd=os.path.dirname(origin))
    _git(['init', '-q', '-b', 'main'], cwd=tree)
    _git(['config', 'user.email', 'sample@example.com'], cwd=tree)
    _git(['config', 'user.name', 'sample'], cwd=tree)
    _git(['add', '-A'], cwd=tree)
    _git(['commit', '-q', '-m', 'sample'], cwd=tree)
    _git(['remote', 'add', 'origin', origin], cwd=tree)
    _git(['push', '-q', '-u', 'origin', 'main'], cwd=tree)
    _git(['remote', 'set-head', 'origin', 'main'], cwd=tree)


def _fill(src, dest, **marks):
    with open(src, encoding='utf-8') as f:
        text = f.read()
    for k, v in marks.items():
        text = text.replace(f'@{k}@', v)
    with open(dest, 'w', encoding='utf-8') as f:
        f.write(text)


class SampleProductTest(unittest.TestCase):
    """One setUp for the whole class: init + one tick, then each test reads what it left."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = os.path.realpath(tempfile.mkdtemp(prefix='asf_sample_'))
        cls.sample = os.path.join(cls.tmp, 'sample')
        shutil.copytree(SAMPLE, cls.sample)
        cls.repo = os.path.join(cls.sample, 'repo')
        cls.backlog = os.path.join(cls.sample, 'backlog')
        _publish(cls.repo, os.path.join(cls.tmp, 'repo.git'))
        _publish(cls.backlog, os.path.join(cls.tmp, 'backlog.git'))

        cls.home = os.path.join(cls.tmp, 'asf-home')
        os.makedirs(os.path.join(cls.home, 'products'))
        _fill(os.path.join(cls.sample, 'product.yaml'),
              os.path.join(cls.home, 'products', 'sample.yaml'), REPO=cls.repo, BACKLOG=cls.backlog)
        _fill(os.path.join(cls.sample, 'config.yaml'), os.path.join(cls.home, 'config.yaml'),
              SAMPLE=cls.sample)

        cls.env = dict(os.environ, ASF_HOME=cls.home, HOME=cls.tmp, PYTHONPATH=ROOT,
                       GIT_AUTHOR_NAME='sample', GIT_AUTHOR_EMAIL='sample@example.com',
                       GIT_COMMITTER_NAME='sample', GIT_COMMITTER_EMAIL='sample@example.com',
                       GH_TOKEN='', PATH=cls._path_with_offline_gh(os.path.join(cls.tmp, 'bin')))
        cls.env.pop('ASF_PRODUCT', None)
        cls.init = cls.asf('init', '--product', 'sample')
        cls.before = cls.asf('next', '--product', 'sample', '--json')
        cls.tick = cls.asf('tick', '--product', 'sample')

    @staticmethod
    def _path_with_offline_gh(stub_dir):
        """PATH with a stub ``gh`` first: every call fails the way an offline ``gh`` does."""
        os.makedirs(stub_dir)
        stub = os.path.join(stub_dir, 'gh')
        with open(stub, 'w') as f:
            f.write('#!/bin/sh\necho "gh: offline in tests" >&2\nexit 1\n')
        os.chmod(stub, 0o755)
        return stub_dir + os.pathsep + os.environ.get('PATH', '')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def asf(cls, *argv, cwd=None):
        return subprocess.run([sys.executable, '-m', 'asf.cli', *argv], cwd=cwd or cls.tmp,
                              env=cls.env, capture_output=True, text=True, timeout=300)

    def sessions(self):
        path = os.path.join(self.home, 'state', 'sample', 'sessions.jsonl')
        out = {}
        with open(path, encoding='utf-8') as f:
            for line in f:
                rec = json.loads(line)
                out.setdefault(rec['job'], {}).update(rec)
        return out

    def record(self):
        return os.path.join(self.home, 'state', 'sample', 'record')

    # ---- init ----------------------------------------------------------------

    def test_init_adopts_the_ten_cards(self):
        self.assertEqual(self.init.returncode, 0, self.init.stdout + self.init.stderr)
        self.assertIn('init: adopted 10 items', self.init.stdout)

    # ---- the tick ------------------------------------------------------------

    def test_tick_runs_every_step(self):
        self.assertEqual(self.tick.returncode, 0, self.tick.stdout + self.tick.stderr)
        self.assertNotIn('FAILED', self.tick.stdout)

    def test_record_clone_has_one_tick_state_commit_with_the_step_log(self):
        subjects = _git(['log', '--format=%s', 'origin/main'], cwd=self.record()).splitlines()
        state = [s for s in subjects if s.startswith('tick: state')]
        self.assertEqual(len(state), 1, subjects)
        self.assertFalse([s for s in subjects if s.startswith('tick: steps')], subjects)
        # the tick line rides in the same commit as the derived state
        files = _git(['show', '--name-only', '--format=', 'origin/main'], cwd=self.record())
        self.assertIn('metrics/ticks/', files)
        self.assertIn('index.json', files)

    def test_before_the_tick_the_s1_bug_is_the_only_row(self):
        # an open S1 holds every other row back; its branch carries the product's fix prefix
        rows = json.loads(self.before.stdout)
        self.assertEqual([(r['kind'], r['item_id'], r['action'], r['branch'], r['brief_kind'])
                          for r in rows],
                         [('BUG → FIX', 'B-0001', 'would launch', 'bugfix/B-0001', 'fix-bug')])

    def test_s1_bug_launched_as_bug_fix_on_its_bugfix_branch(self):
        sessions = self.sessions()
        bug = [s for s in sessions.values() if s.get('item') == 'B-0001']
        self.assertEqual(len(bug), 1, sessions)
        s = bug[0]
        self.assertEqual(s['branch'], 'bugfix/B-0001')
        self.assertEqual(s['runtime'], 'fake')
        self.assertEqual(s['kind'], 'fix-bug')
        self.assertIn('launched fix-bug-b-0001', self.tick.stdout)
        # the worktree is on the product's own prefix, cut from the product's main
        head = _git(['rev-parse', '--abbrev-ref', 'HEAD'], cwd=s['worktree'])
        self.assertEqual(head, 'bugfix/B-0001')
        # one launch event in the record, for the Bug
        events_dir = os.path.join(self.record(), 'metrics', 'events')
        events = []
        for name in os.listdir(events_dir):
            with open(os.path.join(events_dir, name)) as f:
                events += [json.loads(ln) for ln in f if ln.strip()]
        self.assertEqual([e['item'] for e in events if e['kind'] == 'launch'], ['B-0001'])
        # ...and it was pushed in the tick's one commit
        files = _git(['show', '--name-only', '--format=', 'origin/main'], cwd=self.record())
        self.assertIn('metrics/events/', files)

    def test_the_bug_brief_names_its_item_first(self):
        s = next(s for s in self.sessions().values() if s.get('item') == 'B-0001')
        with open(s['brief'], encoding='utf-8') as f:
            text = f.read()
        # the workers put the fix-bug harvest header (kind + the named test) ahead of the brief
        # itself; what the brief builder wrote starts right after it, with its item line
        head, _, body = text.partition('\n\n')
        self.assertTrue(head.startswith('Kind: fix-bug — B-0001 (S1)'), head)
        self.assertTrue(body.startswith('Backlog item: B-0001 — The counter crashes on an empty file'),
                        body[:200])
        self.assertIn('Branch: `bugfix/B-0001`', body)

    def test_next_shows_the_bug_in_flight_and_a_feature_waiting(self):
        live = [s for s in self.sessions().values() if not s.get('ended')]
        inflight = os.path.join(self.tmp, 'inflight.json')
        with open(inflight, 'w') as f:
            json.dump(live, f)
        _git(['pull', '-q', '--ff-only'], cwd=self.backlog)
        # one slot more than the sample's one: the Bug holds its slot, the next row shows
        p = self.asf('next', '--product', 'sample', '--json', '--inflight', inflight,
                     '--capacity', '2')
        self.assertEqual(p.returncode, 0, p.stderr)
        rows = json.loads(p.stdout)
        self.assertNotIn('B-0001', [r['item_id'] for r in rows])  # in flight: no second session
        self.assertEqual([(r['kind'], r['item_id'], r['branch']) for r in rows],
                         [('CARD → SPEC', 'F-0001', 'spec/F-0001')])
        # at the sample's own capacity the Feature waits for the Bug's slot
        p = self.asf('next', '--product', 'sample', '--json', '--inflight', inflight)
        self.assertEqual(json.loads(p.stdout), [])

    def test_doctor_is_clean(self):
        p = self.asf('doctor', '--product', 'sample')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


if __name__ == '__main__':
    unittest.main()
