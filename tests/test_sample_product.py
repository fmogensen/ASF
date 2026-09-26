"""The dogfood-in-CI test: ASF runs a second product — ``sample/`` — end to end.

``sample/`` carries conventions of its own (``feature/`` and ``bugfix/`` branches, ``specs/`` and
``plans/`` at the repo root, no CI, no deploy, the fake worker runtime) and a record of ten cards.
The test copies it into a temp dir, makes bare origins for the repo and the record, writes a temp
``ASF_HOME``, then drives the real ``asf`` command line — ``init``, one ``tick``, ``next``,
``doctor`` — as a subprocess. No network, no account, no agent: a literal left over from the first
product shows up here as a wrong branch, a missing launch or a failed step.

The second half (F-0087) walks the failure paths through whole ticks on the same fake runtime:
a held branch → the correction row → landed (with the trunk moving under it), and a session that
ends "done" without pushing → held → the correction in the same worktree → landed. The test
plays the session between ticks and asserts the tick's printed lines.
"""
import json
import os
import shutil
import subprocess
import time
import sys
import tempfile
import unittest

from asf import env, hermetic, scheduler, tokens
from asf.metrics import metrics

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.test_sample_product` does not
    from test_scheduler import fake_clis
except ImportError:  # pragma: no cover - import shape only
    from tests.test_scheduler import fake_clis

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

        cls.env = hermetic.build(dict(os.environ, ASF_HOME=cls.home, PYTHONPATH=ROOT,
                                      GIT_AUTHOR_NAME='sample', GIT_AUTHOR_EMAIL='sample@example.com',
                                      GIT_COMMITTER_NAME='sample', GIT_COMMITTER_EMAIL='sample@example.com',
                                      GH_TOKEN='',
                                      PATH=cls._path_with_offline_gh(os.path.join(cls.tmp, 'bin'))),
                                 home=cls.tmp)
        cls.init = cls.asf('init', '--product', 'sample')
        # B-0131: a real install ends with the operator writing the console's own allow list —
        # this fixture stands in for that operator, so `doctor`'s console-permissions row is
        # green the same way a real, fully-installed product's is
        cls.console_permissions = cls.asf('console-permissions', 'install', '--product', 'sample',
                                          '--scope', 'user')
        cls.before = cls.asf('next', '--product', 'sample', '--json')
        cls.tick = cls.asf('tick', '--product', 'sample')

    @staticmethod
    def _path_with_offline_gh(stub_dir):
        """PATH with a stub ``gh`` first: every call fails the way an offline ``gh`` does. The
        other CLIs ``asf doctor`` probes answer at once (B-0071) instead of asking the network."""
        fake_clis(stub_dir)
        stub = os.path.join(stub_dir, 'gh')
        with open(stub, 'w') as f:
            f.write('#!/bin/sh\necho "gh: offline in tests" >&2\nexit 1\n')
        os.chmod(stub, 0o755)
        return stub_dir + os.pathsep + os.environ.get('PATH', '')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def asf(cls, *argv, cwd=None, env=None):
        return subprocess.run([sys.executable, '-m', 'asf.cli', *argv], cwd=cwd or cls.tmp,
                              env=env or cls.env, capture_output=True, text=True, timeout=300)

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

    def test_init_offers_the_console_permissions(self):
        # B-0131 round 1: `asf init` on its own — no `tools/install.sh` in between — still ends
        # with the offer, the same as the installer's step 6.
        self.assertIn('allow  Bash(asf:*)', self.init.stdout)
        self.assertIn('run one to write it: asf console-permissions install --product sample '
                      '--scope user|repo', self.init.stdout)

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
        # in flight: no second session, but still named — a WAITS row, never silent
        self.assertEqual([(r['item_id'], r['action']) for r in rows if r['item_id'] == 'B-0001'],
                         [('B-0001', 'WAITS ON session')])
        self.assertEqual([(r['kind'], r['item_id'], r['branch']) for r in rows
                          if r['action'].startswith('would launch')],
                         [('CARD → SPEC', 'F-0001', 'spec/F-0001')])
        # at the sample's own capacity the Feature waits for the Bug's slot
        p = self.asf('next', '--product', 'sample', '--json', '--inflight', inflight)
        self.assertEqual([(r['item_id'], r['action']) for r in json.loads(p.stdout)],
                         [('B-0001', 'WAITS ON session')])

    def test_doctor_is_clean(self):
        p = self.asf('doctor', '--product', 'sample')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_the_review_brief_cites_the_samples_dirs(self):
        # the review-kind brief is the one body that cites a path under every one of the sample's
        # own dirs at once: `state_lines` emits the review path only `if kind in REVIEW_KINDS`
        # (asf/briefs/preamble.py:348), so a spec-kind brief carries `specs/` but no `reviews/`.
        # The case is named for the kind it drives.
        p = self.asf('brief', '--product', 'sample', '--item', 'F-0001', '--kind', 'review')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('Specs live in `specs`, plans in `plans`, reviews in `reviews`.', p.stdout)
        self.assertIn('specs/f-0001.md', p.stdout)
        self.assertIn('reviews/f-0001-r1.md', p.stdout)
        self.assertNotIn('docs/specs', p.stdout)
        self.assertNotIn('docs/reviews', p.stdout)

    def test_evidence_sees_the_spec_under_specs(self):
        p = self.asf('evidence', '--product', 'sample', '--fresh')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        data = json.loads(p.stdout)
        # `serialise` writes the key as `spec`, holding `it['spec_doc'][0]` — a string, not the
        # tuple (asf/evidence/evidence.py:610)
        self.assertEqual(data['features']['f-0002']['spec'], 'origin/main:specs/f-0002.md')
        # the sample declares no matrix_path, briefs_dir or decisions file — nothing looked for them
        self.assertEqual(data['stories'], {})
        self.assertNotIn('briefs', data)
        self.assertNotIn('decisions', data)
        # and no literal of the first product's tree survives in what the pass emits
        for literal in ('superpowers', 'sdd-input', 'feature-matrix'):
            self.assertNotIn(literal, p.stdout)

    def test_the_evidence_cache_is_under_the_products_state(self):
        # a TMPDIR this case owns: the cache belongs under the product's state dir, and the old
        # `/tmp/backlog-evidence.json` must not come back beside it
        tmpdir = os.path.join(self.tmp, 'owned-tmp')
        os.makedirs(tmpdir, exist_ok=True)
        p = self.asf('evidence', '--product', 'sample', '--fresh',
                     env=dict(self.env, TMPDIR=tmpdir))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        cache = os.path.join(self.home, 'state', 'sample', 'cache-evidence.json')
        self.assertTrue(os.path.isfile(cache), cache)
        with open(cache, encoding='utf-8') as f:
            json.load(f)  # the cache the call just wrote, not a stale or foreign one
        self.assertEqual(sorted(os.listdir(tmpdir)), [], 'evidence wrote under TMPDIR')

    @unittest.skip("blocked: nothing reads conventions.intake_dir yet (Task 5 of F-0074 is "
                   "unlanded — the field exists at asf/conventions.py:100 and appears nowhere "
                   "under asf/groom, asf/record or asf/tick), and this fixture's `asf init` takes "
                   "the adopt branch (asf/init.py:275), so `lay_down` — the only caller that "
                   "creates the stream folders — never runs. Kept so F-0074 PD5's proof is a "
                   "known debt, not a silent loss.")
    def test_init_creates_the_intake_dir_not_inbox(self):
        folders = os.listdir(self.record())
        self.assertIn('cards', folders)
        self.assertNotIn('inbox', folders)


class SampleTokenCapsTest(unittest.TestCase):
    def test_sample_declares_token_caps(self):
        with open(os.path.join(SAMPLE, 'product.yaml')) as f:
            data = env.loads(f.read())
        p = env.Product('sample', data)
        tokens.caps(p)
        self.assertEqual(set(tokens.cap_for(p, 'spec')), set(tokens.DIMENSIONS))
        self.assertTrue(all(isinstance(v, int) for v in tokens.cap_for(p, 'spec').values()))


class SampleClocksTest(unittest.TestCase):
    """F-0083/S-7207: the sample product's own clocks: block, read straight off disk — no
    fixture, no subprocess, since scheduler.clocks() only needs the parsed product data."""

    def test_sample_declares_clocks(self):
        with open(os.path.join(SAMPLE, 'product.yaml'), encoding='utf-8') as f:
            product = env.Product('sample', env.loads(f.read()))
        clocks = scheduler.clocks(product)
        self.assertEqual([c.name for c in clocks], ['record', 'dispatch', 'daily', 'shadow'])


# ---- the failure paths, whole loop (F-0087) ----------------------------------------------

RED_TEST = ('import unittest\n\nfrom src.count import count\n\n\nclass EmptyTests(unittest.TestCase):\n'
            '    def test_empty(self):\n        self.assertEqual(count(None), 0)\n')
GREEN_COUNT = ('def count(text):\n    if not text:\n        return 0\n'
               '    return len(text.split())\n')


class FailurePathsBase(unittest.TestCase):
    """A fresh copy of ``sample/`` per class; the test plays the session between ticks (the fake
    runtime returns at once and touches no git), rewriting ``fake_script.json`` before each tick
    so every launch in that tick gets the scripted result. Every tick's printed lines are kept."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = os.path.realpath(tempfile.mkdtemp(prefix='asf_sample_fail_'))
        cls.sample = os.path.join(cls.tmp, 'sample')
        shutil.copytree(SAMPLE, cls.sample)
        cls.repo = os.path.join(cls.sample, 'repo')
        cls.backlog = os.path.join(cls.sample, 'backlog')
        cls.repo_origin = os.path.join(cls.tmp, 'repo.git')
        _publish(cls.repo, cls.repo_origin)
        _publish(cls.backlog, os.path.join(cls.tmp, 'backlog.git'))
        cls.home = os.path.join(cls.tmp, 'asf-home')
        os.makedirs(os.path.join(cls.home, 'products'))
        _fill(os.path.join(cls.sample, 'product.yaml'),
              os.path.join(cls.home, 'products', 'sample.yaml'), REPO=cls.repo, BACKLOG=cls.backlog)
        _fill(os.path.join(cls.sample, 'config.yaml'), os.path.join(cls.home, 'config.yaml'),
              SAMPLE=cls.sample)
        cls.env = hermetic.build(dict(os.environ, ASF_HOME=cls.home, PYTHONPATH=ROOT,
                                      GIT_AUTHOR_NAME='sample', GIT_AUTHOR_EMAIL='sample@example.com',
                                      GIT_COMMITTER_NAME='sample', GIT_COMMITTER_EMAIL='sample@example.com',
                                      GH_TOKEN='', PATH=SampleProductTest._path_with_offline_gh(
                                          os.path.join(cls.tmp, 'bin'))),
                                 home=cls.tmp)
        cls.ticks = []
        init = cls.asf('init', '--product', 'sample')
        assert init.returncode == 0, init.stdout + init.stderr

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def asf(cls, *argv):
        return subprocess.run([sys.executable, '-m', 'asf.cli', *argv], cwd=cls.tmp, env=cls.env,
                              capture_output=True, text=True, timeout=300)

    @classmethod
    def script(cls, *steps):
        with open(os.path.join(cls.sample, 'fake_script.json'), 'w') as f:
            json.dump(list(steps), f)

    @classmethod
    def tick(cls):
        p = cls.asf('tick', '--product', 'sample', '--fresh')  # ticks seconds apart: no evidence cache
        assert p.returncode == 0 and 'Traceback' not in p.stdout + p.stderr, p.stdout + p.stderr
        lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
        lines += cls.background_harvest(lines)
        cls.ticks.append(lines)
        return lines

    @classmethod
    def background_harvest(cls, lines):
        """The lines of the harvest this tick started in the background, once it has finished —
        the tick itself never waits on it; the scenario does, so each tick's landing is its own."""
        started = [ln for ln in lines if ln.startswith('harvest: started in the background (pid ')]
        if not started:
            return []
        pid = int(started[0].split('(pid ')[1].split(')')[0])
        path = os.path.join(cls.home, 'state', 'sample', 'harvest.json')
        deadline = time.monotonic() + 240
        while True:
            try:
                with open(path, encoding='utf-8') as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                rec = {}
            if rec.get('pid') == pid and rec.get('finished'):
                break
            assert time.monotonic() < deadline, f'background harvest {pid} never finished: {rec}'
            time.sleep(0.2)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(dict(rec, reported=True), f)
        return list(rec.get('lines') or [])

    @classmethod
    def sessions(cls):
        from asf.workers import lifecycle
        return lifecycle.latest(os.path.join(cls.home, 'state', 'sample', 'sessions.jsonl'))

    @staticmethod
    def session_commits(worktree, rel, text, subject):
        path = os.path.join(worktree, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(text)
        _git(['add', '-A'], cwd=worktree)
        _git(['commit', '-q', '-m', subject], cwd=worktree)

    @staticmethod
    def session_pushes(worktree):
        branch = _git(['rev-parse', '--abbrev-ref', 'HEAD'], cwd=worktree)
        _git(['push', '-q', 'origin', branch], cwd=worktree)
        return branch

    def origin_log(self):
        """(sha, subject) of the trunk, newest first, without the rollup's own changelog commits
        (a product with no deploy files each release's notes on its trunk)."""
        out = _git(['log', '--format=%H %s', 'main'], cwd=self.repo_origin).splitlines()
        return [tuple(ln.split(' ', 1)) for ln in out
                if not metrics.CHANGELOG_SUBJECT_RE.match(ln.split(' ', 1)[1])]

    def origin_subjects(self):
        return [s for _sha, s in self.origin_log()]

    @staticmethod
    def find(lines, prefix):
        hits = [ln for ln in lines if ln.startswith(prefix)]
        return hits[0] if hits else None


class HeldThenCorrectedThenLanded(FailurePathsBase):
    """held branch → correction row → landed, with the trunk moving under the correction."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.script({'ok': True, 'result': 'fixed B-0001, pushed'})
        cls.t1 = cls.tick()                                   # launches fix-bug-b-0001
        s = cls.sessions()['fix-bug-b-0001']
        cls.wt = s['worktree']
        # the session's work: a test that names the fix — and is red
        cls.session_commits(cls.wt, 'tests/test_empty.py', RED_TEST, 'fix(B-0001): add test_empty')
        cls.session_pushes(cls.wt)
        cls.t2 = cls.tick()                                   # health: finished; harvest: held
        cls.t3 = cls.tick()                                   # wave: the correction row launches
        s = cls.sessions()['correct-b-0001']
        cls.wt2 = s['worktree']
        cls.wt2_branch = _git(['rev-parse', '--abbrev-ref', 'HEAD'], cwd=cls.wt2)
        cls.session_commits(cls.wt2, 'src/count.py', GREEN_COUNT, 'fix(B-0001): return 0 on empty')
        cls.session_pushes(cls.wt2)
        # the trunk moves before harvest looks again: someone landed a README change
        other = os.path.join(cls.tmp, 'other')
        _git(['clone', '-q', cls.repo_origin, other], cwd=cls.tmp)
        _git(['config', 'user.email', 'o@example.com'], cwd=other)
        _git(['config', 'user.name', 'o'], cwd=other)
        with open(os.path.join(other, 'README'), 'a') as f:
            f.write('moved\n')
        _git(['commit', '-qam', 'docs: README moved the trunk'], cwd=other)
        _git(['push', '-q', 'origin', 'HEAD:main'], cwd=other)
        cls.t4 = cls.tick()                                   # health: finished; harvest: rebased, landed
        cls.t5 = cls.tick()                                   # health: reaped

    def test_tick_1_launches_the_fix(self):
        self.assertIsNotNone(self.find(self.t1, 'launched fix-bug-b-0001'), self.t1)

    def test_tick_2_holds_the_red_gate_and_sends_it_back(self):
        self.assertIn('ended     fix-bug-b-0001           finished', self.t2)
        held = self.find(self.t2, 'held bugfix/B-0001: ')
        self.assertIsNotNone(held, self.t2)
        self.assertTrue(held.endswith('— back to its session (round 1)'), held)
        self.assertIn('test_empty', held)

    def test_tick_3_launches_the_correction_on_the_same_branch_and_worktree(self):
        self.assertIsNotNone(self.find(self.t3, 'launched correct-b-0001'), self.t3)
        self.assertEqual(os.path.realpath(self.wt2), os.path.realpath(self.wt))
        self.assertEqual(self.wt2_branch, 'bugfix/B-0001')
        self.assertIsNone(self.find(self.t3, 'launched fix-bug-b-0001'), 'no second fix session')

    def test_tick_4_rebases_onto_the_moved_trunk_and_lands(self):
        self.assertIn('ended     correct-b-0001           finished', self.t4)
        landed = self.find(self.t4, 'landed bugfix/B-0001 → ')
        self.assertIsNotNone(landed, self.t4)
        sha = landed.split('→ ')[1].strip()
        self.assertEqual(self.origin_log()[0][0], sha)
        subjects = self.origin_subjects()
        self.assertEqual(subjects[:3], ['fix(B-0001): return 0 on empty', 'fix(B-0001): add test_empty',
                                        'docs: README moved the trunk'])
        self.assertEqual(_git(['branch', '--list', 'bugfix/B-0001'], cwd=self.repo_origin), '')
        self.assertEqual(self.sessions()['correct-b-0001']['harvested'], sha)

    def test_tick_5_reaps_the_landed_worktree(self):
        self.assertTrue(self.find(self.t5, 'reaped fix-bug-b-0001 (landed '), self.t5)
        self.assertFalse(os.path.exists(self.wt))

    def test_every_tick_committed_and_pushed_the_record(self):
        for i, lines in enumerate(self.ticks):
            self.assertTrue(self.find(lines, 'tick: state committed and pushed'), (i, lines))


class FinishedWithoutPushIsCorrected(FailurePathsBase):
    """a result that says done with nothing pushed → held with the unpushed-work correction →
    the correction runs in the same worktree → pushed → landed."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.script({'ok': True, 'result': 'done — waiting for the background suite'})
        cls.t1 = cls.tick()
        cls.wt = cls.sessions()['fix-bug-b-0001']['worktree']
        with open(os.path.join(cls.wt, 'src', 'count.py'), 'w') as f:   # half done, uncommitted
            f.write(GREEN_COUNT + '# ' + 'AKIA' + 'A' * 16 + '\n')  # B-0094: the redaction gate refuses the factory's commit
        cls.t2 = cls.tick()                                   # health: not pushed → held; wave: correct
        cls.wt2 = cls.sessions()['correct-b-0001']['worktree']
        with open(os.path.join(cls.wt2, 'src', 'count.py'), 'w') as f:   # the correction removes the line the gate refused
            f.write(GREEN_COUNT)
        cls.session_commits(cls.wt2, 'tests/test_empty.py', RED_TEST.replace('count("")', 'count("")'),
                            'fix(B-0001): return 0 on empty, with test_empty')
        cls.session_pushes(cls.wt2)
        cls.t3 = cls.tick()                                   # landed

    def test_tick_2_judges_not_pushed_holds_and_relaunches_in_the_same_worktree(self):
        self.assertIn('ended     fix-bug-b-0001           failed: not pushed: 1 uncommitted file(s), 0 unpushed commit(s)', self.t2)
        held = self.find(self.t2, 'held      fix-bug-b-0001           unpushed work: ')
        self.assertIsNotNone(held, self.t2)
        self.assertIn('commit and push what you have', held)
        self.assertIsNotNone(self.find(self.t2, 'launched correct-b-0001'), self.t2)
        self.assertEqual(os.path.realpath(self.wt2), os.path.realpath(self.wt))
        self.assertTrue(os.path.exists(os.path.join(self.wt2, 'src', 'count.py')))

    def test_the_correction_brief_carries_the_unpushed_work_reason(self):
        with open(self.sessions()['correct-b-0001']['brief'], encoding='utf-8') as f:
            text = f.read()
        self.assertIn('CORRECTION: the step failed with:\nunpushed work: not pushed: 1 uncommitted file(s)', text)

    def test_tick_3_lands_the_corrected_branch(self):
        self.assertIsNotNone(self.find(self.t3, 'landed bugfix/B-0001 → '), self.t3)
        self.assertEqual(self.origin_subjects()[0], 'fix(B-0001): return 0 on empty, with test_empty')


if __name__ == '__main__':
    unittest.main()
