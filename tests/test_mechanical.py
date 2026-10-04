"""asf.harvest.mechanical — the causes code settles before a correction is written (W2-PR3a).

Scenarios on a fixture repo (:class:`tests.test_lane.LaneFixture`): under
``conventions.flags.mechanical: on`` a conflict git rebases clean is pushed by the lane, set
PUSHED with reason ``mechanical:conflict`` and the event on the run, no correction; a textual
conflict is a correction carrying what the lane tried; a footprint red is never pushed on — the
hold names the widening rule's verdict; with the flag off every row is today's.
"""
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace

from asf import env
from asf import redact as redact_mod
from asf.feeder import widen
from asf.harvest import harvest, lane, mechanical
from asf.workers import health as health_mod
from asf.workers import lifecycle
from asf.workers import pool as pool_mod
from tests import test_lane as TL
from tests import test_lifecycle as TLC
from tests import test_workers as TW

sh = TL.sh

ON = {'mechanical': 'on'}


class Flag(unittest.TestCase):

    def product(self, flags=None):
        conv = {'test_command': 'true'}
        if flags is not None:
            conv['flags'] = flags
        return env.Product('sample', {'repo_dir': '/nonexistent', 'main': 'main',
                                      'conventions': conv})

    def test_default_is_off(self):
        self.assertFalse(mechanical.enabled(self.product()))
        self.assertFalse(mechanical.enabled(None))

    def test_on_words(self):
        for v in ('on', 'true', True, 'yes', 'ON'):
            self.assertTrue(mechanical.enabled(self.product({'mechanical': v})), v)
        for v in ('off', False, 'no', ''):
            self.assertFalse(mechanical.enabled(self.product({'mechanical': v})), v)

    def test_the_flag_is_known(self):
        from asf import conventions
        self.assertIn(mechanical.FLAG, conventions.KNOWN_FLAGS)

    def test_handles_only_table_kinds(self):
        p = self.product(ON)
        self.assertTrue(mechanical.handles(p, 'conflict'))
        self.assertTrue(mechanical.handles(p, lifecycle.REBASE_CONFLICT))
        self.assertTrue(mechanical.handles(p, lifecycle.FOOTPRINT))
        self.assertFalse(mechanical.handles(p, 'review'))
        self.assertFalse(mechanical.handles(self.product(), 'conflict'))

    def test_a_dry_run_applies_nothing(self):
        ln = SimpleNamespace(product=self.product(ON), dry_run=True)
        self.assertIsNone(mechanical.apply(ln, {'branch': 'b'}, {'kind': 'conflict'}))


class Rebase(TL.LaneFixture):
    """conflict → rebase, through :func:`asf.harvest.lane.send_back` (the fixture's helpers are
    :class:`tests.test_lane.GitMechanicsNeverSpawnASession`'s, borrowed — not its tests)."""

    _G = TL.GitMechanicsNeverSpawnASession
    B, AUTHOR = _G.B, _G.AUTHOR
    tip, commit, own, sessions = _G.tip, _G.commit, _G.own, _G.sessions
    branch, archive, facts = _G.branch, _G.archive, _G.facts

    def setUp(self):
        super().setUp()
        self.lines = []

    def run_of(self, job='coder-t-0001'):
        return harvest.read_sessions(self.state_dir)[job]

    def test_a_clean_conflict_is_rebased_pushed_and_logged(self):
        old = self.branch()
        self.push_main({'y.txt': 'y\n'}, 'fix: y (#811)')
        trunk = self.origin_main()
        ln = lane.Lane(self.product(flags=ON), self.state_dir, out=self.lines.append)
        f = self.facts(old)
        got = lane.send_back(ln, f, 'conflict', 'PR #7 merge refused — not mergeable', [])
        self.assertEqual(got, 'mechanical')
        new = self.tip()
        self.assertNotEqual(new, old)
        self.assertEqual(sh(['git', 'rev-parse', f'{new}~1'], cwd=self.origin).stdout.strip(),
                         trunk)
        self.assertEqual(self.archive('rebase', old), old)
        rec = self.lane_of(self.B)
        self.assertEqual((rec['state'], rec['reason']), (lane.PUSHED, 'mechanical:conflict'))
        self.assertEqual((rec['event'], rec['mechanical'], rec['head']),
                         ('mechanical', 'conflict', new))
        ev = self.run_of()['mechanical']
        self.assertEqual((ev['event'], ev['kind'], ev['head'], ev['resolved']),
                         ('mechanical', 'conflict', new, True))
        self.assertEqual(lifecycle.corrections(self.sessions()), {})
        self.assertEqual(ln.results[self.B], 'mechanical')
        self.assertTrue(any(l.startswith(f'mechanical:conflict {self.B}: resolved')
                            for l in self.lines), self.lines)
        self.assertEqual(self.origin_main(), trunk)

    def test_a_pending_conflict_correction_is_cleared(self):
        old = self.branch()
        self.push_main({'y.txt': 'y\n'}, 'fix: y (#811)')
        ln = lane.Lane(self.product(flags=ON), self.state_dir, out=self.lines.append)
        f = dict(self.facts(old), correction={'kind': 'conflict', 'text': 'x'})
        self.assertEqual(lane.send_back(ln, f, 'conflict', 'x', []), 'mechanical')
        self.assertIsNone(f['correction'])
        self.assertIsNone(self.run_of().get('correction'))

    def test_a_textual_conflict_is_a_correction_carrying_why(self):
        self.push_main({'c.txt': 'base\n'}, 'chore: c')
        old = self.branch({'c.txt': 'branch\n'})
        self.push_main({'c.txt': 'trunk\n'}, 'fix: c on the trunk (#813)')
        ln = lane.Lane(self.product(flags=ON), self.state_dir, out=self.lines.append)
        got = lane.send_back(ln, self.facts(old), 'conflict', 'PR #7 merge refused', [])
        self.assertEqual(got, 'held')
        self.assertEqual(self.tip(), old)
        corr = lifecycle.corrections(self.sessions())['T-0001']
        self.assertEqual(corr['kind'], 'conflict')
        self.assertIn('git stops at', corr['text'])
        self.assertIn('conflicts in c.txt', corr['text'])
        self.assertEqual(self.lane_of(self.B)['state'], lane.BACK)
        ev = self.run_of()['mechanical']
        self.assertEqual((ev['kind'], ev['resolved'], ev['head']), ('conflict', False, old))
        self.assertTrue(any(l.startswith(f'mechanical:conflict {self.B}: residue')
                            for l in self.lines), self.lines)

    def test_a_red_pre_push_check_is_residue_and_pushes_nothing(self):
        old = self.branch()
        self.push_main({'y.txt': 'y\n'}, 'fix: y (#811)')
        ln = lane.Lane(self.product(flags=ON, pre_push_check='echo lint finding; exit 3'),
                       self.state_dir, out=self.lines.append)
        self.assertEqual(lane.send_back(ln, self.facts(old), 'conflict', 'x', []), 'held')
        self.assertEqual(self.tip(), old)
        corr = lifecycle.corrections(self.sessions())['T-0001']
        self.assertIn('pre-push check fails', corr['text'])
        self.assertIn('lint finding', corr['text'])

    def test_a_conflict_with_a_batch_ahead_is_never_rebased(self):
        old = self.branch()
        self.push_main({'y.txt': 'y\n'}, 'fix: y (#811)')
        ln = lane.Lane(self.product(flags=ON), self.state_dir, out=self.lines.append)
        got = lane.send_back(ln, self.facts(old), 'conflict', 'x', ['a.txt'], rebase=False)
        self.assertEqual(got, 'held')
        self.assertEqual(self.tip(), old)
        self.assertNotIn('mechanical', self.run_of())

    def test_flag_off_keeps_todays_rows(self):
        old = self.branch()
        self.push_main({'y.txt': 'y\n'}, 'fix: y (#811)')
        ln = lane.Lane(self.product(), self.state_dir, out=self.lines.append)
        got = lane.send_back(ln, self.facts(old), 'conflict', 'PR #7 merge refused', [])
        self.assertEqual(got, 'rebased')
        rec = self.lane_of(self.B)
        self.assertEqual((rec['state'], rec['reason']),
                         (lane.PUSHED, 'rebased onto main by the lane (no session)'))
        self.assertNotIn('event', rec)
        self.assertNotIn('mechanical', self.run_of())
        self.assertFalse(any(l.startswith('mechanical:') for l in self.lines), self.lines)


class Footprint(unittest.TestCase):
    """footprint → the widening rule's verdict: a red head is never pushed on."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix='mech_')
        self.addCleanup(shutil.rmtree, self.state, True)
        self.record = {'job': 'coder-t-0080', 'item': 'T-0080', 'branch': 'worker/T-0080'}
        self.lines = []
        with open(harvest.sessions_path(self.state), 'w', encoding='utf-8') as fh:
            fh.write(json.dumps(dict(self.record, kind='coder', started='2026-10-01T00:00:00Z',
                                     ended='2026-10-01T01:00:00Z', end_reason='finished'))
                     + '\n')
        self.items = {
            'F-0001': {'type': 'feature'},
            'T-0080': {'type': 'task', 'parent': 'F-0001', 'writes': ['src/a.py']},
        }

    def lane(self, flags=None, max_files=None):
        conv = {'test_command': 'true'}
        if flags is not None:
            conv['flags'] = flags
        if max_files is not None:
            conv['widen_max_files'] = max_files
        product = env.Product('sample', {'repo_dir': '/nonexistent', 'main': 'main',
                                         'conventions': conv})
        return SimpleNamespace(product=product, items=self.items, dry_run=False,
                               state_dir=self.state, path=harvest.sessions_path(self.state),
                               out=self.lines.append, results={}, trunk='main')

    def hold(self, ln):
        f = {'branch': 'worker/T-0080', 'item': 'T-0080', 'head': 'a' * 40, 'run': self.record}
        return lane.hold_with_correction(
            self.state, 'worker/T-0080', self.record, 'gate', 'FAIL: test_other', self.lines.append,
            ['tests/test_other.py'], ['src/a.py'], ['src/a.py'], own=True,
            read=lambda p: {'tests/test_other.py': 'from src.a import f\n'}.get(p), lane=ln, f=f)

    def run_of(self):
        return harvest.read_sessions(self.state)['coder-t-0080']

    def test_a_footprint_red_holds_naming_the_verdict(self):
        self.assertEqual(self.hold(self.lane(ON)), 'held')
        run = self.run_of()
        corr = run['correction']
        self.assertEqual((corr['kind'], corr['needs']), ('footprint', ['tests/test_other.py']))
        self.assertIn('widening rule says widen +tests/test_other.py', corr['text'])
        ev = run['mechanical']
        self.assertEqual((ev['kind'], ev['resolved']), ('footprint', False))
        self.assertNotIn('lane', run)  # never set PUSHED: the same head would gate red again
        self.assertTrue(any(l.startswith('mechanical:footprint worker/T-0080: residue')
                            for l in self.lines), self.lines)

    def test_over_the_cap_the_verdict_is_reshape(self):
        self.assertEqual(self.hold(self.lane(ON, max_files=0)), 'held')
        self.assertIn('widening rule says reshape', self.run_of()['correction']['text'])

    def test_flag_off_keeps_todays_footprint_hold(self):
        self.assertEqual(self.hold(self.lane()), 'held')
        run = self.run_of()
        self.assertEqual(run['correction']['kind'], 'footprint')
        self.assertNotIn('widening rule', run['correction']['text'])
        self.assertNotIn('mechanical', run)
        self.assertFalse(any(l.startswith('mechanical:') for l in self.lines), self.lines)

    def test_without_a_lane_the_hold_is_todays(self):
        lane.hold_with_correction(
            self.state, 'worker/T-0080', self.record, 'gate', 'FAIL: test_other', self.lines.append,
            ['tests/test_other.py'], ['src/a.py'], ['src/a.py'], own=True,
            read=lambda p: {'tests/test_other.py': 'from src.a import f\n'}.get(p))
        self.assertNotIn('mechanical', self.run_of())


class Widen(unittest.TestCase):
    """:func:`asf.feeder.widen.widen` — the one composition of the rule's facts."""

    ITEMS = {
        'F-1': {'type': 'feature'},
        'T-1': {'type': 'task', 'parent': 'F-1', 'writes': ['src/a.py']},
        'T-2': {'type': 'task', 'parent': 'F-1', 'writes': ['src/b.py']},
        'T-9': {'type': 'task', 'state': 'Resolved', 'writes': ['lib/done.py']},
    }

    def test_within_the_cap(self):
        v = widen.widen(self.ITEMS, 'T-1', ['tests/test_x.py'], limit=2)
        self.assertEqual((v.kind, v.paths), (widen.WIDEN, ('tests/test_x.py',)))

    def test_over_the_cap_is_reshape(self):
        v = widen.widen(self.ITEMS, 'T-1', ['x.py', 'y.py'], limit=1)
        self.assertEqual(v.kind, widen.RESHAPE)

    def test_inside_the_feature_passes_the_cap(self):
        v = widen.widen(self.ITEMS, 'T-1', ['src/b.py'], limit=0)
        self.assertEqual(v.kind, widen.WIDEN)

    def test_a_delivered_path_does_not_count(self):
        v = widen.widen(self.ITEMS, 'T-1', ['lib/done.py'], limit=0)
        self.assertEqual(v.kind, widen.WIDEN)

    def test_a_live_owner_makes_it_wait(self):
        v = widen.widen(self.ITEMS, 'T-1', ['src/b.py'], limit=5,
                        running=[('T-2', ['src/b.py'])])
        self.assertEqual((v.kind, v.detail), (widen.WAITS, 'T-2'))

    def test_a_second_widening_is_reshape(self):
        v = widen.widen(self.ITEMS, 'T-1', ['x.py'], limit=5, widened_before=1)
        self.assertEqual(v.kind, widen.RESHAPE)


class _Rounds(TLC._RebaseShape):
    """A lane branch on origin and a session's worktree of it, for the ``unpushed`` entry."""

    REVIEW = 'reviews/1-t-0001.md'

    def write(self, name, text, msg, repo=None):
        repo = repo or self.repo
        os.makedirs(os.path.dirname(os.path.join(repo, name)) or repo, exist_ok=True)
        return self.commit(name, text, msg, repo=repo)

    def review_round_on_origin(self, text='round 1 — approved\n'):
        """A reviewer's round committed to origin/<branch> from another clone."""
        other = os.path.join(self.base, 'reviewer')
        if not os.path.isdir(other):
            self.sh(['clone', '-q', '-b', self.branch, self.origin, other], self.base)
            self.identity(other, 'reviewer@example.com')
        else:
            self.sh(['pull', '-q', '--rebase', 'origin', self.branch], other)
        sha = self.write(self.REVIEW, text, 'review(T-0001): round 1', repo=other)
        self.sh(['push', '-q', 'origin', self.branch], other)
        return sha

    def remote(self):
        return self.sh(['ls-remote', '--heads', 'origin', self.branch], self.repo).split()[0]

    def on_origin(self, ref):
        out = self.sh(['ls-remote', '--heads', 'origin', ref], self.repo)
        return out.split()[0] if out else ''

    def head(self):
        return self.sh(['rev-parse', 'HEAD'], self.repo)


def _droppable(path):
    return path.startswith('reviews/')


class DroppedRounds(_Rounds):
    """W2-PR3b (2): origin holds review rounds the session's head lacks; the session wrote the
    same review file (an adjudication), so neither a rebase onto origin nor a carry applies —
    today a refusal every pass ("rebase conflicts", "would lose N commits"). Under the flag the
    rounds are archived and the head published over them; any other commit still refuses."""

    def setUp(self):
        super().setUp()
        self.commit('a', 'own a\n', 'task(T-0001): own a')
        self.push_branch()

    def test_on_its_base_a_review_only_conflict_refuses_without_the_flag(self):
        self.review_round_on_origin()
        self.write(self.REVIEW, 'adjudicated\n', 'review(T-0001): adjudicated')
        self.commit('c', 'own c\n', 'task(T-0001): own c')
        remote = self.remote()
        ok, line = lifecycle.publish(self.repo, self.branch, remote, main='main')
        self.assertFalse(ok, line)
        self.assertTrue(lifecycle.rebase_conflict(line), line)
        self.assertEqual(self.remote(), remote)

    def test_on_its_base_review_rounds_are_archived_and_the_head_published(self):
        rnd = self.review_round_on_origin()
        self.write(self.REVIEW, 'adjudicated\n', 'review(T-0001): adjudicated')
        head = self.commit('c', 'own c\n', 'task(T-0001): own c')
        ok, line = lifecycle.publish(self.repo, self.branch, rnd, main='main',
                                     droppable=_droppable)
        self.assertTrue(ok, line)
        self.assertIn('dropped 1 review/notes round(s)', line)
        self.assertEqual(self.remote(), head)
        self.assertEqual(self.on_origin(lifecycle.copies_archive(self.branch, rnd)), rnd)

    def test_past_the_remote_on_the_trunk_review_rounds_are_archived_and_published(self):
        rnd = self.review_round_on_origin()
        self.land_on_trunk(('t', 'trunk\n', 'chore: the trunk moves'))
        self.sh(['rebase', '-q', 'origin/main'], self.repo)
        self.write(self.REVIEW, 'adjudicated\n', 'review(T-0001): adjudicated')
        head = self.head()
        ok, line = lifecycle.publish(self.repo, self.branch, rnd, main='main')
        self.assertFalse(ok, line)
        self.assertIn('would lose 1 commit', line)
        ok, line = lifecycle.publish(self.repo, self.branch, rnd, main='main',
                                     droppable=_droppable)
        self.assertTrue(ok, line)
        self.assertIn('dropped 1 review/notes round(s)', line)
        self.assertEqual(self.remote(), head)
        self.assertEqual(self.on_origin(lifecycle.copies_archive(self.branch, rnd)), rnd)

    def test_a_code_commit_on_origin_still_refuses(self):
        self.review_round_on_origin()
        other = os.path.join(self.base, 'reviewer')
        self.commit('c', 'their c\n', 'task(T-0001): their c', repo=other)
        self.sh(['push', '-q', 'origin', self.branch], other)
        remote = self.remote()
        self.write(self.REVIEW, 'adjudicated\n', 'review(T-0001): adjudicated')
        self.commit('c', 'own c\n', 'task(T-0001): own c')
        ok, line = lifecycle.publish(self.repo, self.branch, remote, main='main',
                                     droppable=_droppable)
        self.assertFalse(ok, line)
        self.assertEqual(self.remote(), remote)
        self.assertEqual(self.on_origin(lifecycle.copies_archive(self.branch, remote)), '')


class ReplayOwn(_Rounds):
    """W2-PR3b (3): origin was rebuilt under the worktree (the lane's rebase, another session's
    resolution) — a rebase onto it replays the old tip over its rewritten copy and conflicts.
    The factory replays only the session's own commits, past the fork point, and publishes."""

    def setUp(self):
        super().setUp()
        self.commit('a', 'own a\n', 'task(T-0001): own a')
        self.old = self.push_branch()
        other = os.path.join(self.base, 'rebuilder')
        self.sh(['clone', '-q', '-b', self.branch, self.origin, other], self.base)
        self.identity(other, 'lane@example.com')
        self.sh(['reset', '-q', '--hard', 'origin/main'], other)
        self.rebuilt = self.commit('a', 'own a, resolved\n', 'task(T-0001): own a', repo=other)
        self.sh(['push', '-q', '-f', 'origin', self.branch], other)
        self.sh(['fetch', '-q', 'origin'], self.repo)   # the tracking ref's reflog sees it
        self.own = self.commit('c', 'own c\n', 'task(T-0001): own c')

    def product(self, flags=ON):
        return env.Product('sample', {'repo_dir': self.repo, 'main': 'main',
                                      'conventions': {'flags': flags, 'reviews_dir': 'reviews'}})

    def test_without_the_entry_the_rebase_conflicts(self):
        ok, line = lifecycle.publish(self.repo, self.branch, self.rebuilt, main='main')
        self.assertFalse(ok, line)
        self.assertTrue(lifecycle.rebase_conflict(line), line)

    def test_the_sessions_own_commits_are_replayed_and_published(self):
        ok, line, out = mechanical.publish_worktree(self.product(), self.repo, self.branch,
                                                    self.rebuilt, main='main')
        self.assertTrue(ok, line)
        self.assertIn('replayed 1 own commit(s)', line)
        remote = self.remote()
        self.assertEqual(self.sh(['rev-parse', f'{remote}~1'], self.repo), self.rebuilt)
        self.assertEqual(self.sh(['log', '-1', '--format=%s', remote], self.repo),
                         'task(T-0001): own c')
        self.assertEqual((out.kind, out.resolved, out.head), (lifecycle.UNPUSHED, True, remote))

    def test_an_own_commit_that_conflicts_leaves_the_worktree_as_it_was(self):
        self.commit('a', 'own a, the session\'s\n', 'task(T-0001): own a again')
        head = self.head()
        ok, line, out = mechanical.publish_worktree(self.product(), self.repo, self.branch,
                                                    self.rebuilt, main='main')
        self.assertFalse(ok, line)
        self.assertTrue(lifecycle.rebase_conflict(line), line)
        self.assertIn('conflict with the rewritten', line)
        self.assertEqual(self.head(), head)
        self.assertEqual(self.remote(), self.rebuilt)
        self.assertFalse(out.resolved)


class DeclaredTransplant(_Rounds):
    """A correct/transplant run cuts its branch fresh from the trunk and carries the approved
    content over (a product's T-0349): origin/<branch> holds code commits the new head does not,
    so its push is not a fast-forward, the session may not force, and the brief tells it to write
    ``pushed: rebased <sha> — the factory publishes``. Under the flag the factory does publish it
    — the old tip archived, the head pushed under the lease — when the report declares that exact
    head and origin's tip is the one the worktree itself held. Anything else still refuses."""

    def setUp(self):
        super().setUp()
        self.commit('a', 'own a\n', 'task(T-0001): own a')
        self.push_branch()
        self.push_from_elsewhere('f', 'fix f\n', 'fix(T-0001): a reviewer fix')
        self.sh(['fetch', '-q', 'origin'], self.repo)   # the session saw origin's tip
        self.land_on_trunk(('t', 'trunk\n', 'chore: the trunk moves'))
        self.sh(['checkout', '-q', '-B', self.branch, 'origin/main'], self.repo)
        with open(os.path.join(self.repo, 'a'), 'w') as fh:
            fh.write('own a, carried\n')
        self.head_sha = self.commit('f', 'fix f, carried\n',
                                    'task(T-0001): transplanted fresh from main')
        self.tip = self.remote()

    def product(self, flags=ON):
        return env.Product('sample', {'repo_dir': self.repo, 'main': 'main',
                                      'conventions': {'flags': flags, 'reviews_dir': 'reviews'}})

    def test_undeclared_it_refuses_as_today(self):
        ok, line, out = mechanical.publish_worktree(self.product(), self.repo, self.branch,
                                                    self.tip, main='main')
        self.assertFalse(ok, line)
        self.assertIn('would lose', line)
        self.assertEqual(self.remote(), self.tip)
        self.assertFalse(out.resolved)

    def test_a_declared_transplant_is_published_and_the_old_tip_archived(self):
        ok, line, out = mechanical.publish_worktree(self.product(), self.repo, self.branch,
                                                    self.tip, main='main',
                                                    declared=self.head_sha[:9])
        self.assertTrue(ok, line)
        self.assertIn('transplant', line)
        self.assertEqual(self.remote(), self.head_sha)
        self.assertEqual(self.on_origin(lifecycle.copies_archive(self.branch, self.tip)),
                         self.tip)
        self.assertTrue(out.resolved)

    def test_a_declared_sha_that_is_not_the_head_refuses(self):
        ok, line, _out = mechanical.publish_worktree(self.product(), self.repo, self.branch,
                                                     self.tip, main='main', declared='0badc0de1')
        self.assertFalse(ok, line)
        self.assertEqual(self.remote(), self.tip)

    def test_a_commit_pushed_after_the_session_looked_still_refuses(self):
        newer = self.push_from_elsewhere('p', 'a person\'s newer commit\n', 'fix: newer')
        ok, line, _out = mechanical.publish_worktree(self.product(), self.repo, self.branch,
                                                     newer, main='main',
                                                     declared=self.head_sha[:9])
        self.assertFalse(ok, line)
        self.assertEqual(self.remote(), newer)
        self.assertEqual(self.on_origin(lifecycle.copies_archive(self.branch, newer)), '')


class UnpushedEntry(unittest.TestCase):

    def test_the_table_holds_unpushed(self):
        p = env.Product('sample', {'repo_dir': '/nonexistent', 'main': 'main',
                                   'conventions': {'flags': ON}})
        self.assertTrue(mechanical.handles(p, lifecycle.UNPUSHED))
        self.assertIn(lifecycle.UNPUSHED, mechanical.CLEARS)

    def test_round_file_reads_the_review_and_reports_dirs(self):
        conv = env.Product('sample', {'repo_dir': '/nonexistent', 'main': 'main', 'conventions': {
            'reviews_dir': '.sdd/reviews', 'reports_dir': 'notes'}}).conventions
        f = mechanical.round_file(conv)
        self.assertTrue(f('.sdd/reviews/1-x.md'))
        self.assertTrue(f('notes/run.md'))
        self.assertFalse(f('src/app.ts'))
        self.assertFalse(f('.sdd/reviews-old.md'))

    def test_no_worktree_is_a_residue(self):
        ln = SimpleNamespace(product=None, trunk='main')
        out = mechanical.unpushed(ln, {'branch': 'b', 'head': 'abc', 'run': {}}, {})
        self.assertFalse(out.resolved)


class UnpushedHealth(TW.Home):
    """W2-PR3b through the health step: a run ended ``not pushed`` with an adjudication in its
    worktree while origin gained a review round on the same file. Flag off: held, as today.
    Flag on: published, the round archived, the run finished, the event on the run."""

    spawn, commit = TW.TestHealth.spawn, TW.TestHealth.commit

    def flagged(self, on):
        conv = {'reviews_dir': 'reviews'}
        if on:
            conv['flags'] = ON
        self.product = env.Product('sample', {'repo_dir': self.repo, 'main': 'main',
                                              'job_grants': [self.grant],
                                              'stage_limits': {'silent_min': 30},
                                              'conventions': conv})

    def shape(self):
        rec = self.spawn('adj', {'ok': True})
        wt, branch = rec['worktree'], rec['branch']
        self.commit(wt, 'own')
        TW.git('push', '-q', 'origin', branch, cwd=wt)
        other = tempfile.mkdtemp(prefix='reviewer_')
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        TW.git('clone', '-q', '-b', branch, TW.git('remote', 'get-url', 'origin', cwd=wt), other,
               cwd=wt)
        os.makedirs(os.path.join(other, 'reviews'))
        self.commit(other, 'reviews/1-x.md')
        TW.git('push', '-q', 'origin', branch, cwd=other)
        rnd = TW.git('rev-parse', 'HEAD', cwd=other)
        os.makedirs(os.path.join(wt, 'reviews'))
        with open(os.path.join(wt, 'reviews', '1-x.md'), 'w') as f:
            f.write('adjudicated')
        TW.git('add', '-A', cwd=wt)
        TW.git('commit', '-qm', 'adjudicated', cwd=wt)
        reason = 'failed: not pushed: 0 uncommitted file(s), 2 unpushed commit(s)'
        pool_mod.update_session(self.product, 'adj', ended='2026-10-03T07:00:45Z',
                                end_reason=reason, rc=1)
        return wt, branch, rnd

    def test_flag_off_the_run_is_held(self):
        self.flagged(False)
        wt, branch, rnd = self.shape()
        health_mod.health(self.product, fix=True, alive=lambda pid: False, out=lambda s: None)
        run = pool_mod.load_sessions(self.product)['adj']
        self.assertNotEqual(run['end_reason'], 'finished')
        self.assertIn('rebase conflicts', run.get('publish_refused') or '')
        self.assertNotIn('mechanical', run)
        self.assertEqual(TW.git('ls-remote', '--heads', 'origin', branch, cwd=wt).split()[0], rnd)

    def test_flag_on_the_rounds_are_archived_and_the_run_published(self):
        self.flagged(True)
        wt, branch, rnd = self.shape()
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False,
                                  out=lambda s: None)
        run = pool_mod.load_sessions(self.product)['adj']
        self.assertTrue(any(j == 'adj' and w == 'published' for j, w, _d in found), found)
        self.assertEqual(run['end_reason'], 'finished')
        self.assertEqual(TW.git('ls-remote', '--heads', 'origin', branch, cwd=wt).split()[0],
                         TW.git('rev-parse', 'HEAD', cwd=wt))
        archive = lifecycle.copies_archive(branch, rnd)
        self.assertEqual(TW.git('ls-remote', '--heads', 'origin', archive, cwd=wt).split()[0], rnd)
        ev = run['mechanical']
        self.assertEqual((ev['event'], ev['kind'], ev['resolved']),
                         ('mechanical', lifecycle.UNPUSHED, True))



class DeclaredTransplantHealth(UnpushedHealth):
    """The T-0349 shape through the health step: the run cut its branch fresh from the trunk,
    origin/<branch> holds a code commit the head replaces, and the report says ``pushed: rebased
    <sha> — the factory publishes``. Flag on: published, the old tip archived, the run finished.
    Flag off: held, as today — never parked."""

    def shape(self):
        rec = self.spawn('adj', {'ok': True})
        wt, branch = rec['worktree'], rec['branch']
        self.commit(wt, 'own')
        TW.git('push', '-q', 'origin', branch, cwd=wt)
        other = tempfile.mkdtemp(prefix='reviewer_')
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        TW.git('clone', '-q', '-b', branch, TW.git('remote', 'get-url', 'origin', cwd=wt), other,
               cwd=wt)
        self.commit(other, 'fix')
        TW.git('push', '-q', 'origin', branch, cwd=other)
        tip = TW.git('rev-parse', 'HEAD', cwd=other)
        TW.git('fetch', '-q', 'origin', cwd=wt)
        main = TW.git('rev-parse', 'origin/main', cwd=wt)
        TW.git('reset', '-q', '--hard', main, cwd=wt)          # cut fresh from the trunk
        for name in ('own', 'fix'):
            with open(os.path.join(wt, name), 'w') as f:
                f.write(name + ', carried')
        TW.git('add', '-A', cwd=wt)
        TW.git('commit', '-qm', 'task: transplanted', cwd=wt)
        head = TW.git('rev-parse', 'HEAD', cwd=wt)
        run = pool_mod.load_sessions(self.product)['adj']
        with open(run['log'], 'a', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                                'result': f'REPORT\nitem: x\nkind: correct\nstatus: done\n'
                                          f'pushed: rebased {head[:9]} — the factory publishes\n'
                                          f'NEEDS OPERATOR: run asf land for it\n'}) + '\n')
        reason = 'failed: not pushed: 0 uncommitted file(s), 1 unpushed commit(s)'
        pool_mod.update_session(self.product, 'adj', ended='2026-10-03T07:00:45Z',
                                end_reason=reason, rc=1)
        return wt, branch, tip

    def test_flag_off_the_run_is_held(self):
        self.flagged(False)
        wt, branch, tip = self.shape()
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False,
                                  out=lambda s: None, items={})
        run = pool_mod.load_sessions(self.product)['adj']
        self.assertNotEqual(run['end_reason'], 'finished')
        self.assertFalse([f for f in found if f[1] == 'parked'], found)
        self.assertEqual(TW.git('ls-remote', '--heads', 'origin', branch, cwd=wt).split()[0], tip)

    def test_flag_on_the_declared_transplant_is_published_and_the_old_tip_archived(self):
        self.flagged(True)
        wt, branch, tip = self.shape()
        found = health_mod.health(self.product, fix=True, alive=lambda pid: False,
                                  out=lambda s: None, items={})
        run = pool_mod.load_sessions(self.product)['adj']
        self.assertTrue(any(j == 'adj' and w == 'published' for j, w, _d in found), found)
        self.assertEqual(run['end_reason'], 'finished')
        self.assertEqual(TW.git('ls-remote', '--heads', 'origin', branch, cwd=wt).split()[0],
                         TW.git('rev-parse', 'HEAD', cwd=wt))
        archive = lifecycle.copies_archive(branch, tip)
        self.assertEqual(TW.git('ls-remote', '--heads', 'origin', archive, cwd=wt).split()[0], tip)
        self.assertFalse([f for f in found if f[1] == 'parked'], found)


if __name__ == '__main__':
    unittest.main()


# ---- W2-PR3c: copies / hook refused / naming; mechanical-cause review and adjudicate rows ----

class Table3c(unittest.TestCase):

    def test_the_table_holds_copies_naming_merge_and_hook_refused(self):
        p = env.Product('sample', {'repo_dir': '/nonexistent', 'main': 'main',
                                   'conventions': {'flags': ON}})
        for kind in (lifecycle.COPIES, lifecycle.NAMING, 'merge', lifecycle.HOOK_REFUSED):
            self.assertTrue(mechanical.handles(p, kind), kind)
        self.assertIn(lifecycle.HOOK_REFUSED, mechanical.CLEARS)

    def test_a_pending_correction_is_retried_once_per_head_and_hour(self):
        run = {'mechanical': {'kind': 'copies', 'head': 'abc', 'resolved': False,
                              'at': '2026-10-04T10:00:00Z'}}
        at = mechanical.to_dt('2026-10-04T10:30:00Z')
        self.assertTrue(mechanical.tried(run, 'copies', 'abc', at))
        self.assertFalse(mechanical.tried(run, 'copies', 'def', at))      # a new head
        self.assertFalse(mechanical.tried(run, 'conflict', 'abc', at))    # another cause
        later = mechanical.to_dt('2026-10-04T11:30:00Z')
        self.assertFalse(mechanical.tried(run, 'copies', 'abc', later))   # the trunk had time
        self.assertFalse(mechanical.tried({}, 'copies', 'abc', at))
        run['mechanical']['trunk'] = 't1'
        self.assertTrue(mechanical.tried(run, 'copies', 'abc', later, trunk='t1'))  # unmoved
        self.assertFalse(mechanical.tried(run, 'copies', 'abc', later, trunk='t2'))

    def test_a_ruled_or_parked_correction_is_never_retried(self):
        ln = SimpleNamespace(product=env.Product('sample', {
            'repo_dir': '/nonexistent', 'main': 'main', 'conventions': {'flags': ON}}),
            dry_run=False)
        for extra in ({'ruled': True}, {'parked': True}):
            f = {'branch': 'b', 'kind': 'code', 'head': 'abc', 'run': {},
                 'correction': dict({'kind': 'conflict', 'text': 'x'}, **extra)}
            self.assertIsNone(mechanical.retry_pending(ln, f), extra)


class HookRefusedDrop(unittest.TestCase):
    """The f-0086 loop (S-2: 95 of 118 hook-refused endings since 09-25 were redaction refusals
    naming a worker account): the session's worktree carries trunk history (it merged
    origin/main in), so the publish's lane-N rewrite — a plain chain only — cannot run, and the
    hook refuses every run. Under ``flags.mechanical`` the trunk history is dropped (the own
    commits rebased onto origin/<main>) and the publish runs again: the rewrite clears it. A
    finding in the session's own content still refuses, the worktree as it was."""

    _P = TLC.PublishRedactionTests
    sh, write_commit = _P.sh, _P.write_commit
    B = 'fix/B-9997'

    def setUp(self):
        self._P.setUp(self)
        self.write_commit('plan.md', f'a plan naming {self.account}\n', 'task(B-9997): a plan')
        # the trunk moves, and the session merges it in
        self.sh(['checkout', '-q', '-B', 'tmp', 'origin/main'], self.repo)
        self.write_commit('trunk.txt', 'trunk\n', 'fix: the trunk moved (#9)')
        self.sh(['push', '-q', 'origin', 'tmp:main'], self.repo)
        self.sh(['checkout', '-q', self.B], self.repo)
        self.sh(['fetch', '-q', 'origin'], self.repo)
        self.sh(['merge', '-q', '--no-edit', 'origin/main'], self.repo)
        self.merged = self.sh(['rev-parse', 'HEAD'], self.repo)

    def product(self, flags=ON):
        return env.Product('sample', {'repo_dir': self.repo, 'main': 'main',
                                      'conventions': {'flags': flags}})

    def test_without_the_entry_the_publish_is_refused_every_run(self):
        ok, line = lifecycle.publish(self.repo, self.B, '', main='main')
        self.assertFalse(ok, line)
        self.assertEqual(lifecycle.push_failure(line), lifecycle.HOOK_REFUSED)
        self.assertIn('names a worker account', line)

    def test_the_trunk_history_is_dropped_and_the_names_rewritten(self):
        ok, line, out = mechanical.publish_worktree(self.product(), self.repo, self.B, '',
                                                    main='main')
        self.assertTrue(ok, line)
        self.assertIn('dropped the trunk history', line)
        self.assertIn('worker-account names rewritten to lane-N', line)
        remote = self.sh(['rev-parse', f'origin/{self.B}'], self.repo)
        self.assertEqual(self.sh(['rev-list', '--merges', f'origin/main..{remote}'], self.repo),
                         '')
        self.assertEqual(self.sh(['rev-parse', f'{remote}~1'], self.repo),
                         self.sh(['rev-parse', 'origin/main'], self.repo))
        self.assertNotIn(self.account,
                         self.sh(['log', '-p', '--format=%B', f'origin/main..{remote}'], self.repo))
        self.assertEqual((out.kind, out.resolved, out.head),
                         (lifecycle.HOOK_REFUSED, True, remote))

    def test_a_finding_of_the_sessions_own_still_refuses_the_worktree_as_it_was(self):
        with open(os.path.join(self.home, 'redact-names.txt'), 'w', encoding='utf-8') as f:
            f.write('operator-' + 'private\n')
        redact_mod._DEFAULT_CACHE.clear()
        self.write_commit('own.md', 'naming operator-' + 'private\n', 'task(B-9997): own')
        head = self.sh(['rev-parse', 'HEAD'], self.repo)
        ok, line, out = mechanical.publish_worktree(self.product(), self.repo, self.B, '',
                                                    main='main')
        self.assertFalse(ok, line)
        self.assertIn('names a protected name', line)
        self.assertEqual(self.sh(['rev-parse', 'HEAD'], self.repo), head)
        self.assertEqual(self.sh(['ls-remote', '--heads', 'origin', self.B], self.repo), '')
        self.assertFalse(out.resolved)

    def test_a_worktree_with_no_trunk_history_is_not_rebased(self):
        self.sh(['reset', '-q', '--hard', 'HEAD~1'], self.repo)   # the merge undone
        self.sh(['commit', '-q', '--allow-empty', '-m', 'merge placeholder'], self.repo)
        with open(os.path.join(self.home, 'redact-names.txt'), 'w', encoding='utf-8') as f:
            f.write('operator-' + 'private\n')
        redact_mod._DEFAULT_CACHE.clear()
        self.write_commit('own.md', 'naming operator-' + 'private\n', 'task(B-9997): own')
        ok, line, _out = mechanical.publish_worktree(self.product(), self.repo, self.B, '',
                                                     main='main')
        self.assertFalse(ok, line)
        self.assertNotIn('dropped the trunk history', line)
        self.assertEqual(self.sh(['rev-list', '--count', 'origin/main..HEAD'], self.repo), '3')


class _Held(TL.LaneFixture):
    """A lane branch with a pending correction the lane's table may settle on a later pass."""

    _G = TL.GitMechanicsNeverSpawnASession
    B, AUTHOR = _G.B, _G.AUTHOR
    tip, commit, own, sessions = _G.tip, _G.commit, _G.own, _G.sessions
    branch, archive = _G.branch, _G.archive

    def setUp(self):
        super().setUp()
        self.lines = []

    def corrections(self):
        return lifecycle.corrections(self.sessions())

    def hold(self, kind, text, **extra):
        harvest.mark_session(self.state_dir, 'coder-t-0001',
                             correction=dict({'kind': kind, 'text': text,
                                              'at': '2026-09-21T00:06:00Z'}, **extra))


class CopiesRetried(_Held):
    """A copies hold — the lane's rebuild conflicted — waited for a session even after the
    trunk moved so the rebuild applies (a product's T-0338 / T-0349: adjudicated for a pick
    git now makes). Under the flag the table retries it once the trunk moved: rebuilt, pushed,
    the hold cleared, no session; never twice on one trunk."""

    def copies_hold(self, flags=ON):
        self.push_main({'c.txt': 'base\n'}, 'chore: c')
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'checkout', '-q', '-B', self.B, 'origin/main'], cwd=self.worker)
        self.commit('fix(ci): home-clock', {'x.txt': 'x\n'})
        self.commit('fix(T-0001): the hinge', {'c.txt': 'branch\n'})
        sh(['git', 'push', '-q', '-f', 'origin', self.B], cwd=self.worker)
        self.push_main({'x.txt': 'x\n'}, 'fix(ci): home-clock (#812)')
        self.push_main({'c.txt': 'trunk\n'}, 'fix: c on the trunk (#813)')
        old = self.tip()
        self.session('coder-t-0001', 'T-0001', self.B)
        lane.lane_pass(self.product(flags=flags), self.state_dir, out=self.lines.append)
        self.assertEqual(self.corrections()['T-0001']['kind'], lane.COPIES)
        return old

    def test_the_first_pass_tries_the_rebuild_once(self):
        old = self.copies_hold()
        self.assertEqual(sum(1 for l in self.lines if l.startswith(f'drop copies {self.B}:')),
                         1, self.lines)
        self.assertFalse(any(l.startswith('mechanical:copies') and 'resolved' in l
                             for l in self.lines), self.lines)
        ev = harvest.read_sessions(self.state_dir)['coder-t-0001']['mechanical']
        self.assertEqual((ev['kind'], ev['head'], ev['resolved']), (lane.COPIES, old, False))
        more = []
        lane.lane_pass(self.product(flags=ON), self.state_dir, out=more.append)
        self.assertEqual(self.tip(), old)
        self.assertFalse(any(l.startswith(('mechanical:', 'held ', 'rebased ')) for l in more),
                         more)

    def test_once_the_trunk_lets_it_the_rebuild_is_pushed_and_the_hold_cleared(self):
        old = self.copies_hold()
        self.push_main({'c.txt': 'base\n'}, 'revert: c on the trunk (#814)')
        harvest.mark_session(self.state_dir, 'coder-t-0001', mechanical=dict(
            harvest.read_sessions(self.state_dir)['coder-t-0001']['mechanical'],
            at='2026-09-21T00:00:00Z'))
        trunk = self.origin_main()
        more = []
        lane.lane_pass(self.product(flags=ON), self.state_dir, out=more.append)
        new = self.tip()
        self.assertNotEqual(new, old)
        self.assertEqual(self.own(new), ['fix(T-0001): the hinge'])
        self.assertEqual(sh(['git', 'rev-parse', f'{new}~1'], cwd=self.origin).stdout.strip(),
                         trunk)
        self.assertEqual(self.corrections(), {})
        self.assertTrue(any(l.startswith(f'mechanical:copies {self.B}: resolved')
                            for l in more), more)
        self.assertEqual(self.origin_main(), trunk)

    def test_flag_off_the_hold_waits_for_its_session(self):
        old = self.copies_hold(flags={})
        self.push_main({'c.txt': 'base\n'}, 'revert: c on the trunk (#814)')
        lane.lane_pass(self.product(), self.state_dir, out=self.lines.append)
        self.assertEqual(self.tip(), old)
        self.assertEqual(self.corrections()['T-0001']['kind'], lane.COPIES)
        self.assertNotIn('mechanical', harvest.read_sessions(self.state_dir)['coder-t-0001'])


class AdjudicateSkipped(_Held):
    """F-M4: an adjudicate row raised by a mechanical cause only — the same conflict held three
    times, ``at_cap`` — is skipped when git settles it: the table rebases the branch before the
    ruling is launched, and the hold is cleared."""

    def test_an_at_cap_conflict_git_now_rebases_is_cleared(self):
        old = self.branch()
        self.push_main({'y.txt': 'y\n'}, 'fix: y (#811)')
        self.session('coder-t-0001', 'T-0001', self.B)
        self.hold('conflict', 'PR #7 conflicts with origin/main in a.txt', at_cap=True, same=3)
        self.assertTrue(self.corrections()['T-0001'].get('at_cap'))
        lane.lane_pass(self.product(flags=ON), self.state_dir, out=self.lines.append)
        self.assertNotEqual(self.tip(), old)
        self.assertEqual(self.corrections(), {})
        self.assertTrue(any(l.startswith(f'mechanical:conflict {self.B}: resolved')
                            for l in self.lines), self.lines)

    def test_flag_off_the_adjudication_stays_pending(self):
        old = self.branch()
        self.push_main({'y.txt': 'y\n'}, 'fix: y (#811)')
        self.session('coder-t-0001', 'T-0001', self.B)
        self.hold('conflict', 'PR #7 conflicts with origin/main in a.txt', at_cap=True, same=3)
        lane.lane_pass(self.product(), self.state_dir, out=self.lines.append)
        self.assertEqual(self.tip(), old)
        self.assertTrue(self.corrections()['T-0001'].get('at_cap'))


class ReviewCarried(_Held):
    """F-M4: a review round raised by a mechanical cause only. The lane rebased an approved
    branch onto a trunk that changed a line beside its own (a clean pick, so the same change —
    but the patch's context differs, so ``same_code`` no longer recognises the reviewed head):
    a fresh review round was wanted for code no session touched. Under the flag the approval
    the table's own move left behind is carried to the head it made; the gate still runs."""

    LINES = ''.join(f'l{n}\n' for n in range(1, 8))

    def setUp(self):
        super().setUp()
        self.push_main({'a.txt': self.LINES}, 'chore: a')
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'checkout', '-q', '-B', self.B, 'origin/main'], cwd=self.worker)
        self.commit('feat(T-0001): the door', {'a.txt': self.LINES.replace('l4', 'L4')})
        code = sh(['git', 'rev-parse', 'HEAD'], cwd=self.worker).stdout.strip()
        self.commit('review(T-0001): round 1 — approved', {
            'reviews/1-t-0001.md': f'verdict: approved\nhead: {code}\n'})
        sh(['git', 'push', '-q', '-f', 'origin', self.B], cwd=self.worker)
        self.push_main({'a.txt': self.LINES.replace('l1', 'L1')}, 'fix: l1 (#815)')
        self.session('coder-t-0001', 'T-0001', self.B)
        self.hold('conflict', 'PR #7 conflicts with origin/main')

    def product(self, **conv):
        return super().product(lane={'review': {'code': 'required'}}, **conv)

    def test_the_approval_is_carried_to_the_rebuilt_head(self):
        old = self.tip()
        lane.lane_pass(self.product(flags=ON), self.state_dir, out=self.lines.append)
        new = self.tip()
        self.assertNotEqual(new, old)
        rec_ = self.lane_of(self.B)
        self.assertNotEqual(rec_['state'], lane.REVIEW, self.lines)
        self.assertEqual(rec_.get('mechanical_from'), old)
        more = []
        lane.lane_pass(self.product(flags=ON), self.state_dir, out=more.append)
        self.assertNotEqual(self.lane_of(self.B)['state'], lane.REVIEW, more)
        self.assertFalse(any('round 2 wanted' in l for l in self.lines + more), more)

    def test_a_later_pass_carries_it_from_the_lane_record(self):
        harvest.mark_session(self.state_dir, 'coder-t-0001', correction=None)
        old = self.tip()
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        ln = lane.Lane(self.product(flags=ON), self.state_dir, out=self.lines.append)
        f = ln.gather(prs=False)[self.B]
        self.assertEqual(lane.send_back(ln, f, 'conflict', 'PR #7 conflicts', []), 'mechanical')
        rec_ = self.lane_of(self.B)
        self.assertEqual((rec_['state'], rec_['mechanical_from']), (lane.PUSHED, old))
        more = []
        lane.lane_pass(self.product(flags=ON), self.state_dir, out=more.append)
        self.assertNotEqual(self.lane_of(self.B)['state'], lane.REVIEW, more)
        self.assertFalse(any('round 2 wanted' in l for l in more), more)
