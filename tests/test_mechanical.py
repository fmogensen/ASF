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


if __name__ == '__main__':
    unittest.main()
