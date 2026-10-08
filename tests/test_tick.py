"""asf.tick.tick — the live tick works in its own clone and pushes (B-0013); the step manifest."""
import argparse
import contextlib
import datetime
import glob
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from asf import capacity, ci_queue, env
from asf.tick import shadow, steps, summary, tick
from asf.views import status

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.test_tick` does not
    from gitfixture import Template
except ImportError:  # pragma: no cover - import shape only
    from tests.gitfixture import Template


def _git(args, cwd=None):
    return subprocess.run(['git'] + args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _args(**kw):
    base = dict(product='sample', shadow=False, fresh=False, steps=None, manifest=False, daily=False)
    base.update(kw)
    return argparse.Namespace(**base)


#: an *ended* step, a record part, or the tick's total. No longer `$`-anchored: F-0142's end line
#: carries owner/pid/at after its seconds, and this is the one place that shape is matched.
TIMING_RE = re.compile(r'^(\[(?:step|record):[a-z-]+\]|tick: total|tick: wave latency) \d+\.\ds')
#: F-0142's start line — a per-step log line too, so `untimed` strips it with the rest
STEP_START_RE = re.compile(r'^\[step:[a-z-]+\] start ')


def untimed(out):
    """``out`` without the per-step and per-record-part timing lines and the tick's total."""
    return ''.join(l for l in out.splitlines(True)
                   if not (TIMING_RE.match(l.rstrip('\n')) or STEP_START_RE.match(l)))


def steps_only(out):
    """A tick's stdout without the two summary blocks and the per-step timing lines — for the
    assertions whose subject is the step log (F-0078)."""
    return untimed(out.split('\n\nIN FLIGHT')[0] + '\n')


def _explode_in_the_wave():
    raise ValueError('the wave broke')


def _fake_step0(root, product, fresh=False):
    """Stands in for the real metrics/ingest/rollup pass (it needs CI and session evidence): the
    one derived-state file a rollup would write, with fixed content so a re-run changes nothing."""
    os.makedirs(os.path.join(root, 'state'), exist_ok=True)
    with open(os.path.join(root, 'state', 'rollup.md'), 'w') as f:
        f.write('derived\n')


class TickTestCase(unittest.TestCase):
    """A temp ASF home, a bare origin seeded with a minimal record, the operator's own checkout."""

    product_yaml = ''
    #: One :class:`Template` per test class (a subclass extends :meth:`build_repos`), built the
    #: first time a test of the class runs and copied per test (B-0071).
    _templates = {}

    @classmethod
    def build_repos(cls, tmp):
        """Lay the repos under ``tmp``: the record's bare origin, seeded through a clone, and
        the operator's own checkout."""
        origin = os.path.join(tmp, 'origin.git')
        seed = os.path.join(tmp, 'seed')
        _git(['init', '-q', '--bare', '-b', 'main', origin])
        _git(['clone', '-q', origin, seed])
        _git(['config', 'user.email', 'seed@example.com'], seed)
        _git(['config', 'user.name', 'seed'], seed)
        os.makedirs(os.path.join(seed, 'features'))
        with open(os.path.join(seed, 'features', 'F-0001.md'), 'w') as f:
            f.write('---\nid: F-0001\ntitle: sample\n---\n')
        _git(['add', '-A'], seed)
        _git(['commit', '-q', '-m', 'seed'], seed)
        _git(['push', '-q', 'origin', 'HEAD:main'], seed)
        _git(['clone', '-q', origin, os.path.join(tmp, 'operator')])

    def setUp(self):
        template = self._templates.get(type(self))
        if template is None:
            template = self._templates[type(self)] = Template(self.build_repos, prefix='tick_test_')
        self.tmp = template.fresh()
        self.origin = os.path.join(self.tmp, 'origin.git')
        self.operator = os.path.join(self.tmp, 'operator')

        self._orig_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        os.makedirs(os.path.join(env.ASF_HOME, 'products'))
        self.write_product(self.product_yaml)
        self.write_config('')

        patcher = mock.patch.object(tick, 'run_step0', _fake_step0)
        patcher.start()
        self.addCleanup(patcher.stop)
        # the record's tail (backfill, rollup, ...) needs CI and session evidence too
        patcher = mock.patch.object(tick, 'run_record_tail_step', lambda ctx: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_product(self, extra):
        with open(env.product_path('sample'), 'w') as f:
            f.write(f'repo_slug: x/y\nbacklog_dir: {self.operator}\n{extra}')

    def write_config(self, text):
        with open(env.config_path(), 'w') as f:
            f.write(text)

    def run_tick(self, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = tick.cmd_tick(_args(**kw))
        return rc, out.getvalue()

    def origin_commits(self):
        return int(_git(['rev-list', '--count', 'main'], self.origin))

    def record_path(self):
        return os.path.join(env.ASF_HOME, 'state', 'sample', 'record')


class RecordStepTests(TickTestCase):
    """The record step's own commit and push; the tick's step-log commit is TickLineTests'."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(tick, 'write_tick_line', lambda ctx, ran: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_run_produces_one_pushed_commit(self):
        rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 0)
        self.assertEqual(steps_only(out), f'tick: state committed and pushed ({self.record_path()})\n')
        self.assertEqual(self.origin_commits(), 2)
        self.assertEqual(_git(['show', 'main:state/rollup.md'], self.origin), 'derived')
        self.assertEqual(_git(['log', '-1', '--format=%an <%ae>', 'main'], self.origin), 'ASF <asf@localhost>')

    def test_configured_identity_is_the_commit_author(self):
        self.write_config('factory:\n  git_identity: Bot Name <bot@example.com>\n')
        self.run_tick(steps='record')
        self.assertEqual(_git(['log', '-1', '--format=%an <%ae>', 'main'], self.origin),
                         'Bot Name <bot@example.com>')

    def test_second_run_with_no_change_makes_no_commit(self):
        self.run_tick(steps='record')
        rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 0)
        self.assertEqual(steps_only(out), f'tick: no change ({self.record_path()})\n')
        self.assertEqual(self.origin_commits(), 2)

    def test_refused_push_exits_1_then_next_run_resets_the_stray_commit(self):
        hook = os.path.join(self.origin, 'hooks', 'pre-receive')
        with open(hook, 'w') as f:
            f.write('#!/bin/sh\nexit 1\n')
        os.chmod(hook, 0o755)

        rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 1)
        self.assertEqual(steps_only(out),
                         f'tick: state committed, push refused — re-derived next run ({self.record_path()})\n')
        self.assertEqual(self.origin_commits(), 1)
        self.assertEqual(_git(['rev-list', '--count', 'HEAD'], self.record_path()), '2')  # seed + the stray

        os.remove(hook)
        rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 0)
        self.assertIn('committed and pushed', out)
        self.assertEqual(self.origin_commits(), 2)
        self.assertEqual(_git(['rev-list', '--count', 'HEAD'], self.record_path()), '2')  # not stacked on it
        self.assertEqual(_git(['rev-parse', 'HEAD'], self.record_path()), _git(['rev-parse', 'main'], self.origin))

    def test_origin_moved_during_the_tick_is_rebased_onto_and_pushed(self):
        """B-0030: a card pushed to the record while the tick ran made its push a non-fast-forward
        — and the commit, with the events the steps appended, was thrown away. Now the clone
        rebases onto the moved origin once and pushes again; both commits land."""
        real_commit = shadow.commit_local

        def commit_then_origin_moves(path, message):
            committed = real_commit(path, message)
            with open(os.path.join(self.operator, 'bugs.md'), 'w') as f:  # someone else's card
                f.write('filed by hand\n')
            _git(['add', '-A'], self.operator)
            _git(['commit', '-q', '-m', 'bug(B-0099): by hand'], self.operator)
            _git(['push', '-q', 'origin', 'HEAD:main'], self.operator)
            return committed
        with mock.patch.object(shadow, 'commit_local', commit_then_origin_moves):
            rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 0)
        self.assertEqual(steps_only(out),
                         'tick: origin moved during the tick — rebased onto origin/main and pushed\n'
                         f'tick: state committed and pushed ({self.record_path()})\n')
        self.assertEqual(self.origin_commits(), 3)
        self.assertEqual(_git(['show', 'main:state/rollup.md'], self.origin), 'derived')
        self.assertEqual(_git(['show', 'main:bugs.md'], self.origin), 'filed by hand')
        self.assertEqual(_git(['rev-parse', 'HEAD'], self.record_path()), _git(['rev-parse', 'main'], self.origin))

    def clone_and_operator(self):
        product = env.load_product('sample')
        path = shadow.ensure_clone(product, shadow.record_dir(product))
        _git(['config', 'user.email', 'hand@example.com'], self.operator)
        _git(['config', 'user.name', 'hand'], self.operator)
        return path

    def operator_commits_and_pushes(self, rel, text, message):
        full = os.path.join(self.operator, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w') as f:
            f.write(text)
        _git(['add', '-A'], self.operator)
        _git(['commit', '-q', '-m', message], self.operator)
        _git(['push', '-q', 'origin', 'HEAD:main'], self.operator)

    def test_origin_moved_during_the_tick_is_rebased_and_both_commits_land(self):
        """B-0030, the rule: non-fast-forward → fetch, rebase onto origin/<default> once → push.
        The tick's appended stream and the hand-pushed card both reach origin."""
        path = self.clone_and_operator()
        os.makedirs(os.path.join(path, 'metrics', 'ticks'))
        line = '{"tick": 1200}'
        with open(os.path.join(path, 'metrics', 'ticks', 'x.jsonl'), 'w') as f:
            f.write(line + '\n')
        self.assertTrue(shadow.commit_local(path, 'tick: state t'))
        self.operator_commits_and_pushes('bugs/B-0001.md', '---\nid: B-0001\n---\n', 'bug(B-0001): by hand')
        lines = []
        self.assertTrue(shadow.push(path, out=lines.append))
        self.assertEqual(self.origin_commits(), 3)
        self.assertEqual(_git(['show', 'main:metrics/ticks/x.jsonl'], self.origin), line)
        self.assertEqual(_git(['show', 'main:bugs/B-0001.md'], self.origin), '---\nid: B-0001\n---')
        self.assertEqual(lines, ['tick: origin moved during the tick — rebased onto origin/main and pushed'])

    def moving_origin(self, times):
        """A ``_push_once`` that moves origin — a hand commit pushed from the operator's clone —
        right before each of its first ``times`` attempts: the race between the push's own
        fetch/rebase and its push, ``times`` times over."""
        real = shadow._push_once
        calls = []

        def push_once(path, branch):
            n = len(calls)
            calls.append(branch)
            if n < times:
                self.operator_commits_and_pushes(f'bugs/B-{n + 1:04d}.md', f'---\nid: B-{n + 1:04d}\n---\n',
                                                 f'bug(B-{n + 1:04d}): by hand, mid-push')
            return real(path, branch)
        return push_once, calls

    def test_f0087_origin_moving_between_the_fetch_and_the_push_is_the_next_round(self):
        # F-0087, class "push races": origin moves in the window between push()'s own fetch
        # and its push — twice — and both the tick's line and every hand commit land
        path = self.clone_and_operator()
        os.makedirs(os.path.join(path, 'metrics', 'ticks'))
        with open(os.path.join(path, 'metrics', 'ticks', 'x.jsonl'), 'w') as f:
            f.write('{"tick": 1}\n')
        self.assertTrue(shadow.commit_local(path, 'tick: state t'))
        push_once, calls = self.moving_origin(times=2)
        lines = []
        with mock.patch.object(shadow, '_push_once', push_once):
            self.assertTrue(shadow.push(path, out=lines.append))
        self.assertEqual(len(calls), 3)   # refused, refused, pushed
        self.assertEqual(self.origin_commits(), 4)
        subjects = _git(['log', '--format=%s', 'main'], self.origin).splitlines()
        self.assertEqual(subjects[0], 'tick: state t')
        self.assertEqual(sorted(subjects[1:3]), ['bug(B-0001): by hand, mid-push', 'bug(B-0002): by hand, mid-push'])
        self.assertEqual(_git(['show', 'main:metrics/ticks/x.jsonl'], self.origin), '{"tick": 1}')
        self.assertEqual(lines, ['tick: origin moved during the tick — rebased onto origin/main and pushed'])
        for d in ('rebase-merge', 'rebase-apply'):
            self.assertFalse(os.path.isdir(os.path.join(path, '.git', d)), d)

    def test_f0087_origin_moving_past_the_retry_cap_is_refused_and_the_clone_is_clean(self):
        path = self.clone_and_operator()
        with open(os.path.join(path, 'state.md'), 'w') as f:
            f.write('derived\n')
        self.assertTrue(shadow.commit_local(path, 'tick: state t'))
        push_once, calls = self.moving_origin(times=shadow.PUSH_RETRIES + 1)
        with mock.patch.object(shadow, '_push_once', push_once):
            self.assertFalse(shadow.push(path))
        self.assertEqual(len(calls), shadow.PUSH_RETRIES + 1)
        for d in ('rebase-merge', 'rebase-apply'):
            self.assertFalse(os.path.isdir(os.path.join(path, '.git', d)), d)
        # the next record run resets and re-derives, as always
        rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 0)
        self.assertIn('committed and pushed', out)

    def test_f0087_typed_field_and_machine_block_on_one_card_resolve_by_ownership_under_the_race(self):
        # B-0044 under the race: the hand commit lands mid-push and touches the card the tick
        # re-derived; the typed line is the hand's, the machine block the tick's, nothing is lost
        from asf.record import frontmatter
        marker = frontmatter.MARKER
        path = self.clone_and_operator()
        with open(os.path.join(path, 'features', 'F-0001.md'), 'w') as f:
            f.write(f'---\nid: F-0001\ntitle: sample\n{marker}\nstate: Active\n---\n')
        self.assertTrue(shadow.commit_local(path, 'tick: state t'))
        real = shadow._push_once
        calls = []

        def push_once(p, branch):
            calls.append(branch)
            if len(calls) == 1:
                self.operator_commits_and_pushes('features/F-0001.md',
                                                 '---\nid: F-0001\ntitle: sample\ndecided: true\n---\n',
                                                 'feature(F-0001): decided by hand')
            return real(p, branch)
        lines = []
        with mock.patch.object(shadow, '_push_once', push_once):
            self.assertTrue(shadow.push(path, out=lines.append))
        self.assertEqual(lines, ['tick: origin moved — re-derived onto origin/main and pushed'])
        pushed = _git(['show', 'main:features/F-0001.md'], self.origin)
        self.assertIn('decided: true', pushed)
        self.assertNotIn('<<<<<<<', pushed)

    def test_conflicting_hand_commit_leaves_the_clone_clean_and_reports_refused(self):
        """B-0030, the conflict case (a body both sides edited, B-0044): the rebase is aborted, push() is False, no rebase is left
        in progress, and the next record run resets and re-derives as today."""
        path = self.clone_and_operator()
        with open(os.path.join(path, 'features', 'F-0001.md'), 'w') as f:
            f.write('---\nid: F-0001\ntitle: sample\n---\nbody from the clone\n')
        self.assertTrue(shadow.commit_local(path, 'tick: state t'))
        self.operator_commits_and_pushes('features/F-0001.md', '---\nid: F-0001\ntitle: sample\n---\nbody by hand\n',
                                         'feature(F-0001): by hand')
        self.assertFalse(shadow.push(path))
        for d in ('rebase-merge', 'rebase-apply'):
            self.assertFalse(os.path.isdir(os.path.join(path, '.git', d)), d)
        self.assertEqual(self.origin_commits(), 2)
        rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 0)
        self.assertIn('committed and pushed', out)

    def test_conflict_on_machine_owned_card_content_is_re_derived_and_pushed(self):
        """B-0044: the tick rewrote a card's machine block and a hand commit edited the same card's
        typed lines — the rebase conflicted, was aborted, and the tick's push was refused. Now the
        hand side wins the conflict, the derivation re-runs, the rebase continues and pushes."""
        from asf.record import frontmatter
        marker = frontmatter.MARKER
        path = self.clone_and_operator()
        card = os.path.join(path, 'features', 'F-0001.md')
        with open(card, 'w') as f:
            f.write(f'---\nid: F-0001\ntitle: sample\n{marker}\nstate: Active\n---\n')
        self.assertTrue(shadow.commit_local(path, 'tick: state t'))
        self.operator_commits_and_pushes('features/F-0001.md', '---\nid: F-0001\ntitle: by hand\n---\n',
                                         'feature(F-0001): groomed by hand')
        lines = []
        self.assertTrue(shadow.push(path, out=lines.append))
        self.assertEqual(lines, ['tick: origin moved — re-derived onto origin/main and pushed'])
        self.assertEqual(self.origin_commits(), 3)
        pushed = _git(['show', 'main:features/F-0001.md'], self.origin)
        self.assertIn('title: by hand', pushed)
        self.assertNotIn('<<<<<<<', pushed)
        for d in ('rebase-merge', 'rebase-apply'):
            self.assertFalse(os.path.isdir(os.path.join(path, '.git', d)), d)

    def test_operator_checkout_is_only_fast_forwarded(self):
        # the read views read the operator's checkout, so the tick brings it up to origin — by
        # fast-forward alone: what it held before is an ancestor of what it holds after
        before = _git(['rev-parse', 'HEAD'], self.operator).strip()
        self.run_tick(steps='record')
        self.run_tick(steps='record')
        after = _git(['rev-parse', 'HEAD'], self.operator).strip()
        self.assertEqual(after, _git(['rev-parse', 'main'], self.origin).strip())
        subprocess.run(['git', 'merge-base', '--is-ancestor', before, after], cwd=self.operator,
                       check=True)
        self.assertEqual(_git(['status', '--porcelain', '--untracked-files=no'], self.operator), '')

    def test_no_backlog_dir_is_reported_not_raised(self):
        with open(env.product_path('sample'), 'w') as f:
            f.write('repo_slug: x/y\n')
        rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 1)
        self.assertIn('tick: record failed', out)


class RecordHealthStaleStatusTests(TickTestCase):
    """B-0124: every tick fails at record while the host is offline — nothing pushes, so
    ``metrics/ticks`` never carries a line about it (:mod:`asf.tick.record_health` is the local
    stamp that survives instead), and ``asf status`` reads it back as a STALE row naming since
    when, why (the raw git error reduced to "offline") and how many ticks running."""

    def _offline_step0(self, root, product, fresh=False):
        raise subprocess.CalledProcessError(
            1, ['git', 'fetch'],
            stderr="fatal: unable to access 'https://github.com/x/y.git/': "
                   "Could not resolve host: github.com\n")

    def test_a_failed_record_tick_makes_status_print_the_stale_row_with_since_time_and_count(self):
        from asf.tick import record_health
        from asf.views import index_reader as ix
        from asf.views import status

        stamps = iter(['2026-09-25T03:01:00Z', '2026-09-25T03:06:00Z'])
        with mock.patch.object(tick, 'run_step0', self._offline_step0), \
                mock.patch.object(record_health, '_stamp', lambda: next(stamps)):
            rc1, out1 = self.run_tick(steps='record')
            rc2, out2 = self.run_tick(steps='record')

        self.assertEqual((rc1, rc2), (1, 1))
        self.assertIn('RECORD STALE — offline', out1)
        self.assertIn('RECORD STALE — offline', out2)

        product = env.load_product('sample')
        since = ix.local_stamp('2026-09-25T03:01:00Z', '%H:%M')
        self.assertEqual(status.stale_cell(self.operator, product),
                         f'STALE since {since} — record failed: offline (2 ticks)')

    def test_a_landed_tick_after_the_streak_clears_the_row(self):
        from asf.tick import record_health
        from asf.views import status

        with mock.patch.object(tick, 'run_step0', self._offline_step0):
            self.run_tick(steps='record')
        self.run_tick(steps='record')  # setUp's own _fake_step0: lands cleanly

        product = env.load_product('sample')
        self.assertIsNone(record_health.line(product))
        self.assertIsNone(status.stale_cell(self.operator, product))


def _write_index(root, generated):
    """``index.json`` with no live items — only ``generated`` matters to :func:`stale_cell`."""
    with open(os.path.join(root, 'index.json'), 'w', encoding='utf-8') as f:
        json.dump({'items': {}, 'generated': generated}, f)


def _append_tick_line(root, day, **fields):
    """One ``metrics/ticks/<day>.jsonl`` line, the stream's own shape plus ``fields``."""
    path = os.path.join(root, 'metrics', 'ticks', f'{day}.jsonl')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(fields, sort_keys=True, ensure_ascii=False) + '\n')


def _record_step(ok=True, seconds=1.0):
    return {'step': 'record', 'ok': ok, 'seconds': seconds}


class StaleRowCadenceTests(TickTestCase):
    """F-0221/S-38100: the Stale row's threshold is one full measured cadence, twice over — and
    S-38103's floor, that a real stall still fires and a checkout with no readable cadence keeps
    exactly today's ``2 x`` the clock."""

    product_yaml = 'clocks:\n  record:\n    steps: [record]\n    every: 5m\n'

    def test_duration_term_contributes_nothing_for_none_zero_negative_and_non_numeric(self):
        from asf.views import status
        product = env.load_product('sample')
        for duration_s in (None, 0, -5, 'x', True):
            with self.subTest(duration_s=duration_s):
                self.assertEqual(status._stale_after_s(product, duration_s), 600.0)
        self.assertEqual(status._stale_after_s(product, 798), 2196.0)

    def test_the_threshold_moves_with_the_clocks_block(self):
        from asf.views import status
        self.write_product('clocks:\n  record:\n    steps: [record]\n    every: 10m\n')
        product = env.load_product('sample')
        self.assertEqual(status._stale_after_s(product, 798), 2796.0)

    def test_no_clocks_block_falls_back_to_the_default_record_clock(self):
        from asf.views import status
        self.write_product('')
        product = env.load_product('sample')
        self.assertEqual(status._record_period_s(product), status.DEFAULT_RECORD_CLOCK_S)
        self.assertEqual(status._stale_after_s(product, 798), 2 * (300 + 798))

    def test_a_13_3m_tick_silences_the_row_through_20m_and_it_still_stales_at_40m(self):
        """Today both 20m and 40m are ``STALE`` (``2 x _record_period_s`` alone is 10m) — the
        false positive this Feature removes, and the real stall it keeps (S-38100, S-38103)."""
        from asf.views import index_reader as ix
        from asf.views import status
        product = env.load_product('sample')

        def at(age_minutes):
            now = datetime.datetime.now(datetime.timezone.utc)
            ts = now - datetime.timedelta(minutes=age_minutes)
            ts_str = ts.strftime('%Y-%m-%dT%H:%M:%SZ')
            day = ts.strftime('%Y-%m-%d')
            for p in glob.glob(os.path.join(self.operator, 'metrics', 'ticks', '*.jsonl')):
                os.remove(p)
            _write_index(self.operator, ts_str)
            _append_tick_line(self.operator, day, ts=ts_str, duration_s=798.0, product='sample',
                               steps=[_record_step()])
            return status.stale_cell(self.operator, product), ts_str

        result, _ = at(20)
        self.assertIsNone(result)
        result, ts_str = at(40)
        self.assertEqual(result, f"STALE since {ix.local_stamp(ts_str, '%H:%M')} — no fresh record in 40m")

    def test_a_real_stall_fires(self):
        """S-38103: nothing having finished a record for an hour, with a 13-minute last tick."""
        from asf.views import index_reader as ix
        from asf.views import status
        product = env.load_product('sample')
        now = datetime.datetime.now(datetime.timezone.utc)
        tick_at = now - datetime.timedelta(hours=1, minutes=3)
        ts_str = tick_at.strftime('%Y-%m-%dT%H:%M:%SZ')
        day = tick_at.strftime('%Y-%m-%d')
        _write_index(self.operator, ts_str)
        _append_tick_line(self.operator, day, ts=ts_str, duration_s=780.0, product='sample',
                           steps=[_record_step(seconds=780.0)])
        age_s = (datetime.datetime.now(datetime.timezone.utc) - tick_at).total_seconds()
        self.assertEqual(status.stale_cell(self.operator, product),
                          f"STALE since {ix.local_stamp(ts_str, '%H:%M')} — no fresh record in {ix.span(age_s)}")


class StaleRowRecordSignalTests(TickTestCase):
    """F-0221/S-38101: "a tick finished a record" is read from the newest ``metrics/ticks`` line
    whose ``steps`` carry a landed ``record``, falling back to ``index.json``'s ``generated`` —
    and the row takes whichever of the two is fresher, never the older (D2, D10)."""

    product_yaml = 'clocks:\n  record:\n    steps: [record]\n    every: 5m\n'

    def _now(self):
        return datetime.datetime.now(datetime.timezone.utc)

    def test_the_newer_signal_wins_when_the_tick_line_is_fresher(self):
        from asf.views import status
        product = env.load_product('sample')
        now = self._now()
        tick_ts = now - datetime.timedelta(minutes=4)
        generated_ts = now - datetime.timedelta(minutes=17)
        _write_index(self.operator, generated_ts.strftime('%Y-%m-%dT%H:%M:%SZ'))
        _append_tick_line(self.operator, tick_ts.strftime('%Y-%m-%d'),
                           ts=tick_ts.strftime('%Y-%m-%dT%H:%M:%SZ'), duration_s=60.0,
                           product='sample', steps=[_record_step()])
        self.assertIsNone(status.stale_cell(self.operator, product))

    def test_the_newer_signal_wins_when_generated_is_fresher_and_since_names_it(self):
        from asf.views import index_reader as ix
        from asf.views import status
        product = env.load_product('sample')
        now = self._now()
        tick_ts = now - datetime.timedelta(minutes=50)
        generated_ts = now - datetime.timedelta(minutes=15)
        generated_str = generated_ts.strftime('%Y-%m-%dT%H:%M:%SZ')
        _write_index(self.operator, generated_str)
        _append_tick_line(self.operator, tick_ts.strftime('%Y-%m-%d'),
                           ts=tick_ts.strftime('%Y-%m-%dT%H:%M:%SZ'), duration_s=60.0,
                           product='sample', steps=[_record_step()])
        result = status.stale_cell(self.operator, product)
        self.assertEqual(result,
                          f"STALE since {ix.local_stamp(generated_str, '%H:%M')} — no fresh record in 15m")

    def test_a_tick_with_no_record_step_or_a_failed_one_does_not_count(self):
        from asf.views import status
        product = env.load_product('sample')
        now = self._now()
        day = now.strftime('%Y-%m-%d')
        no_record = now - datetime.timedelta(minutes=1)
        failed_record = now - datetime.timedelta(minutes=2)
        _append_tick_line(self.operator, day, ts=no_record.strftime('%Y-%m-%dT%H:%M:%SZ'),
                           duration_s=798.0, product='sample',
                           steps=[{'step': 'health', 'ok': True, 'seconds': 1.0},
                                  {'step': 'wave', 'ok': True, 'seconds': 1.0}])
        _append_tick_line(self.operator, day, ts=failed_record.strftime('%Y-%m-%dT%H:%M:%SZ'),
                           duration_s=798.0, product='sample', steps=[_record_step(ok=False)])
        self.assertIsNone(status._last_record_tick(self.operator, product))
        # neither line counts, so the duration term is 0 and today's `2 x` the clock is the floor
        generated_ts = now - datetime.timedelta(minutes=11)
        _write_index(self.operator, generated_ts.strftime('%Y-%m-%dT%H:%M:%SZ'))
        self.assertIsNotNone(status.stale_cell(self.operator, product))

    def test_another_products_line_does_not_count_and_an_absent_product_does(self):
        from asf.views import status
        product = env.load_product('sample')
        now = self._now()
        day = now.strftime('%Y-%m-%d')
        other_ts = now - datetime.timedelta(minutes=1)      # fresher, but not this product's
        ours_ts = now - datetime.timedelta(minutes=50)       # older, but ours (product absent)
        _append_tick_line(self.operator, day, ts=other_ts.strftime('%Y-%m-%dT%H:%M:%SZ'),
                           duration_s=798.0, product='other', steps=[_record_step()])
        _append_tick_line(self.operator, day, ts=ours_ts.strftime('%Y-%m-%dT%H:%M:%SZ'),
                           duration_s=798.0, steps=[_record_step()])  # product absent: ours
        _write_index(self.operator, (now - datetime.timedelta(minutes=55)).strftime('%Y-%m-%dT%H:%M:%SZ'))
        last = status._last_record_tick(self.operator, product)
        self.assertEqual(last[0], ours_ts.strftime('%Y-%m-%dT%H:%M:%SZ'))
        # ours is 50m old against a 36.6m threshold (2 x (300 + 798)): past it
        self.assertIsNotNone(status.stale_cell(self.operator, product))

    def test_yesterdays_file_is_read_when_todays_does_not_exist_and_a_third_day_back_is_not(self):
        from asf.views import status
        product = env.load_product('sample')
        now = self._now()
        # today's file never gets created, so three day files sit on disk: day-3, day-2 and
        # day-1 (yesterday) — STALE_TICK_DAYS=2 must keep only the newest two of those three
        day3, day2, day1 = ((now - datetime.timedelta(days=3)).strftime('%Y-%m-%d'),
                             (now - datetime.timedelta(days=2)).strftime('%Y-%m-%d'),
                             (now - datetime.timedelta(days=1)).strftime('%Y-%m-%d'))
        day0 = now.strftime('%Y-%m-%d')
        # the third day back carries a line that would win if it were read at all: very fresh,
        # and a huge duration that would silence the row outright
        cheat_ts = now - datetime.timedelta(seconds=1)
        _append_tick_line(self.operator, day3, ts=cheat_ts.strftime('%Y-%m-%dT%H:%M:%SZ'),
                           duration_s=999999.0, product='sample', steps=[_record_step()])
        # the middle day (two days back) has no landed record of its own
        _append_tick_line(self.operator, day2, ts=(now - datetime.timedelta(hours=44)).strftime('%Y-%m-%dT%H:%M:%SZ'),
                           duration_s=60.0, product='sample',
                           steps=[{'step': 'health', 'ok': True, 'seconds': 1.0}])
        # yesterday's real, landed tick — this is the one the row should read; today's file
        # (day0) never gets created
        legit_ts = now - datetime.timedelta(hours=2)
        _append_tick_line(self.operator, day1, ts=legit_ts.strftime('%Y-%m-%dT%H:%M:%SZ'),
                           duration_s=60.0, product='sample', steps=[_record_step()])
        self.assertFalse(os.path.exists(os.path.join(self.operator, 'metrics', 'ticks', f'{day0}.jsonl')))
        _write_index(self.operator, (now - datetime.timedelta(hours=3)).strftime('%Y-%m-%dT%H:%M:%SZ'))
        last = status._last_record_tick(self.operator, product)
        self.assertEqual(last, (legit_ts.strftime('%Y-%m-%dT%H:%M:%SZ'), 60.0))
        # if the cheat line had been read, the row would be silent (it is ~0s old); it is not
        self.assertIsNotNone(status.stale_cell(self.operator, product))

    def test_an_absent_metrics_directory_reads_as_no_line(self):
        from asf.views import status
        product = env.load_product('sample')
        self.assertFalse(os.path.exists(os.path.join(self.operator, 'metrics')))
        self.assertIsNone(status._last_record_tick(self.operator, product))

    def test_an_unreadable_day_file_reads_as_no_line(self):
        from asf.views import status
        product = env.load_product('sample')
        day = self._now().strftime('%Y-%m-%d')
        path = os.path.join(self.operator, 'metrics', 'ticks', f'{day}.jsonl')
        os.makedirs(path)  # a directory where a file is expected: open() raises OSError
        self.assertIsNone(status._last_record_tick(self.operator, product))

    def test_a_non_json_line_is_skipped_and_leaves_todays_behaviour(self):
        from asf.views import status
        product = env.load_product('sample')
        day = self._now().strftime('%Y-%m-%d')
        path = os.path.join(self.operator, 'metrics', 'ticks', f'{day}.jsonl')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('not json at all\n')
        self.assertIsNone(status._last_record_tick(self.operator, product))

    def test_a_torn_last_line_leaves_the_fresh_line_above_it_readable(self):
        """PD7: ``_last_record_tick`` guards each line of its own, so a kill mid-append that
        tears the last line never costs the valid lines around it."""
        from asf.views import status
        product = env.load_product('sample')
        now = self._now()
        fresh_ts = now - datetime.timedelta(minutes=5)
        day = now.strftime('%Y-%m-%d')
        path = os.path.join(self.operator, 'metrics', 'ticks', f'{day}.jsonl')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fresh_line = json.dumps(dict(ts=fresh_ts.strftime('%Y-%m-%dT%H:%M:%SZ'), duration_s=798.0,
                                      product='sample', steps=[_record_step()]), sort_keys=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(fresh_line + '\n')
            f.write('{"ts": "2026-09-27T23:18:00Z", "product": "sam')  # truncated, no newline
        last = status._last_record_tick(self.operator, product)
        self.assertEqual(last, (fresh_ts.strftime('%Y-%m-%dT%H:%M:%SZ'), 798.0))

    def test_a_line_with_no_ts_does_not_count(self):
        from asf.views import status
        product = env.load_product('sample')
        day = self._now().strftime('%Y-%m-%d')
        _append_tick_line(self.operator, day, duration_s=798.0, product='sample',
                           steps=[_record_step()])  # no `ts` key at all
        self.assertIsNone(status._last_record_tick(self.operator, product))


class StaleRowUpgradeHoldTests(TickTestCase):
    """F-0221/S-38102: while an upgrade marker holds this product's ticks, past the threshold,
    the row names the hold instead of calling it a stall — except for the marker's own owner,
    whose ticks go on by design (D7), and except while the age is still inside the threshold,
    where the row says nothing at all (D8)."""

    product_yaml = 'clocks:\n  record:\n    steps: [record]\n    every: 5m\n'

    def setUp(self):
        super().setUp()
        from asf import upgrade
        self.upgrade = upgrade
        now = datetime.datetime.now(datetime.timezone.utc)
        tick_ts = now - datetime.timedelta(minutes=15)
        _write_index(self.operator, (now - datetime.timedelta(minutes=20)).strftime('%Y-%m-%dT%H:%M:%SZ'))
        _append_tick_line(self.operator, tick_ts.strftime('%Y-%m-%d'),
                           ts=tick_ts.strftime('%Y-%m-%dT%H:%M:%SZ'), duration_s=1.0,
                           product='sample', steps=[_record_step()])
        self.product = env.load_product('sample')

    def _write_marker(self, **data):
        path = self.upgrade.pending_path('sample')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def test_a_marker_owned_by_another_product_names_the_hold(self):
        from asf.views import status
        at = time.time() - 300  # 5m old: inside PENDING_TTL_S
        self._write_marker(sha='f5aa236abcdef', owner='other', at=at)
        result = status.stale_cell(self.operator, self.product)
        expected = f"{self.upgrade.held_label({'sha': 'f5aa236abcdef', 'at': at, 'owner': 'other'})} — no fresh record in 15m"
        self.assertEqual(result, expected)
        self.assertNotIn('STALE', result)

    def test_the_markers_own_owner_still_reads_stale(self):
        from asf.views import status
        self._write_marker(sha='f5aa236abcdef', owner='sample', at=time.time() - 300)
        result = status.stale_cell(self.operator, self.product)
        self.assertIn('STALE', result)

    def test_a_future_at_reads_stale(self):
        from asf.views import status
        self._write_marker(sha='f5aa236abcdef', owner='other', at=time.time() + 3600)
        self.assertIn('STALE', status.stale_cell(self.operator, self.product))

    def test_an_at_past_the_ttl_reads_stale(self):
        from asf.views import status
        self._write_marker(sha='f5aa236abcdef', owner='other',
                            at=time.time() - self.upgrade.PENDING_TTL_S - 60)
        self.assertIn('STALE', status.stale_cell(self.operator, self.product))

    def test_a_dead_operators_wait_reads_stale(self):
        from asf.views import status
        self._write_marker(sha='f5aa236abcdef', owner=None, at=time.time() - 300, pid=999999999)
        self.assertIn('STALE', status.stale_cell(self.operator, self.product))

    def test_a_pending_marker_inside_the_threshold_produces_no_row_at_all(self):
        from asf.views import status
        now = datetime.datetime.now(datetime.timezone.utc)
        tick_ts = now - datetime.timedelta(minutes=5)      # inside the 10m threshold
        day = tick_ts.strftime('%Y-%m-%d')
        path = os.path.join(self.operator, 'metrics', 'ticks', f'{day}.jsonl')
        if os.path.exists(path):
            os.remove(path)
        _write_index(self.operator, (now - datetime.timedelta(minutes=6)).strftime('%Y-%m-%dT%H:%M:%SZ'))
        _append_tick_line(self.operator, day, ts=tick_ts.strftime('%Y-%m-%dT%H:%M:%SZ'),
                           duration_s=1.0, product='sample', steps=[_record_step()])
        self._write_marker(sha='f5aa236abcdef', owner='other', at=time.time() - 60)
        self.assertIsNone(status.stale_cell(self.operator, self.product))


class StaleRow20260927ReplayTests(TickTestCase):
    """F-0221/S-38104: the day of 2026-09-27 replays — every healthy stretch silent (including
    the two moments the spec itself expected to name the upgrade hold, which this Feature's own
    rules make silent instead — PD1) — and the one real stall of the day still fires (PD2)."""

    product_yaml = 'clocks:\n  record:\n    steps: [record]\n    every: 5m\n'

    FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'ticks', '2026-09-27.jsonl')

    @classmethod
    def _fixture_rows(cls):
        rows = []
        with open(cls.FIXTURE, encoding='utf-8') as f:
            for raw in f:
                raw = raw.strip()
                if raw:
                    rows.append(json.loads(raw))
        return rows

    @staticmethod
    def _landed(row):
        return any(isinstance(s, dict) and s.get('step') == 'record' and s.get('ok') is True
                   for s in row.get('steps', []))

    def _replay(self, moment):
        """Write every fixture row at or before ``moment`` into ``self.operator``, ``ts`` shifted
        onto the real clock by one constant ``delta`` (PD3(b)); ``index.json``'s ``generated`` is
        the last landed record tick's start, shifted the same way. Returns that tick's original
        (unshifted) ``ts`` string, for building the expected ``since``."""
        from asf.views import index_reader as ix
        shutil.rmtree(os.path.join(self.operator, 'metrics', 'ticks'), ignore_errors=True)
        now = datetime.datetime.now(datetime.timezone.utc)
        delta = now - moment
        due = [r for r in self._fixture_rows() if ix.parse_ts(r['ts']) <= moment]
        for row in due:
            ts = ix.parse_ts(row['ts']) + delta
            shifted = dict(row, ts=ts.strftime('%Y-%m-%dT%H:%M:%SZ'))
            _append_tick_line(self.operator, shifted['ts'][:10], **shifted)
        landed = [r for r in due if self._landed(r)]
        last = max(landed, key=lambda r: ix.parse_ts(r['ts']))
        start = ix.parse_ts(last['ts']) - datetime.timedelta(seconds=last['duration_s'])
        _write_index(self.operator, (start + delta).strftime('%Y-%m-%dT%H:%M:%SZ'))
        return last['ts']

    def _pre_f0221_was_stale(self, moment, last_ts):
        """The expression this Feature replaces: ``generated`` alone against ``2 x`` the clock —
        measured on the *unshifted* day, since the shift preserves every interval exactly."""
        from asf.views import index_reader as ix
        from asf.views import status
        product = env.load_product('sample')
        rows = {r['ts']: r for r in self._fixture_rows()}
        last_row = rows[last_ts]
        start = ix.parse_ts(last_ts) - datetime.timedelta(seconds=last_row['duration_s'])
        age_s = (moment - start).total_seconds()
        self.assertGreater(age_s, 2 * status._record_period_s(product))

    def test_every_healthy_stretch_and_both_upgrade_wait_moments_are_silent(self):
        """S-38104, PD1: 13:24, 14:14 and 23:26 are the healthy stretches this Feature was built
        to silence; 23:31 and 23:36 are the moments the spec expected to name the upgrade hold —
        this Feature's own rules (D1, D8) leave them silent instead, for reasons PD1 measures."""
        from asf.views import status
        product = env.load_product('sample')
        for hh, mm in ((13, 24), (14, 14), (23, 26), (23, 31), (23, 36)):
            with self.subTest(moment=f'{hh:02d}:{mm:02d}'):
                moment = datetime.datetime(2026, 9, 27, hh, mm, tzinfo=datetime.timezone.utc)
                last_ts = self._replay(moment)
                self.assertIsNone(status.stale_cell(self.operator, product))
                self._pre_f0221_was_stale(moment, last_ts)

    def test_the_one_real_stall_of_the_day_still_fires(self):
        """PD2: the stall moment is 00:10 the next day — 12s short of the threshold at 00:05 (the
        spec's own figure), and four minutes clear of it here."""
        from asf.views import index_reader as ix
        from asf.views import status
        product = env.load_product('sample')
        moment = datetime.datetime(2026, 9, 28, 0, 10, tzinfo=datetime.timezone.utc)
        self._replay(moment)
        # the "since" the row prints is whatever stamp the checkout actually holds — the shifted
        # tick line's own ts, read back through the same function stale_cell calls (PD13: no
        # wall-clock string is hardcoded)
        shifted_last = status._last_record_tick(self.operator, product)
        result = status.stale_cell(self.operator, product)
        self.assertEqual(result,
                          f"STALE since {ix.local_stamp(shifted_last[0], '%H:%M')} — no fresh record in 1h")


class GroomStepOrderTests(TickTestCase):
    """The tick's third step: after ``health``, before ``wave``, in the tick's own record clone."""

    product_yaml = 'steps:\n  batch: off\n  daily: off\n  prs: off\n  harvest: off\n'

    def test_the_wave_runs_first_then_health_then_the_groom_and_is_logged(self):
        from asf.tick import step_groom, step_health, step_wave
        seen = []

        def fake_groom(args, root):
            seen.append((args.apply, args.answers_file, root))
            print('groom 2026-01-01: applied 0, inbox 1 card(s)')
            return 0

        def stub(name):
            def run(ctx, out=print):
                out(f'{name}: ran')
                return 0
            return run
        with mock.patch.object(step_health, 'run', stub('health')), \
                mock.patch.object(step_wave, 'run', stub('wave')), \
                mock.patch('asf.groom.groom.cmd_groom', fake_groom):
            rc, out = self.run_tick()
        self.assertEqual(rc, 0)
        lines = steps_only(out).splitlines()
        order = [ln.split()[0].rstrip(':') for ln in lines
                 if ln.split() and ln.split()[0].rstrip(':') in ('health', 'groom', 'wave')]
        self.assertEqual(order, ['wave', 'health', 'groom'])
        self.assertIn('groom 2026-01-01: applied 0, inbox 1 card(s)', out)
        self.assertEqual(seen, [(True, None, self.record_path())])
        import json
        day = time.strftime('%Y-%m-%d', time.gmtime())
        log = _git(['show', f'main:metrics/ticks/{day}.jsonl'], self.origin)
        steps_run = json.loads(log.splitlines()[-1])['steps']
        self.assertIn(('groom', True), [(s['step'], s['ok']) for s in steps_run])
        self.assertEqual([s['step'] for s in steps_run][:4], ['record', 'wave', 'health', 'groom'])

    def test_a_failing_groom_is_a_failed_step_and_the_wave_still_runs(self):
        from asf.tick import step_health, step_wave
        ran = []
        with mock.patch.object(step_health, 'run', lambda ctx: 0), \
                mock.patch.object(step_wave, 'run', lambda ctx: ran.append('wave') or 0), \
                mock.patch('asf.groom.groom.cmd_groom', lambda args, root: 2):
            rc, out = self.run_tick()
        self.assertEqual(rc, 1)
        self.assertIn('[step:groom] FAILED groom exited 2', out)
        self.assertEqual(ran, ['wave'])


class ManifestTests(TickTestCase):
    product_yaml = ('steps:\n'
                    '  health: bash ~/x/health.sh --fix\n'
                    '  wave: off\n')

    def test_resolution_asf_command_off_undeclared(self):
        rows = steps.resolve(env.load_product('sample'))
        self.assertEqual(rows, [
            ('record', 'asf', None),
            ('health', 'command', 'bash ~/x/health.sh --fix'),
            ('groom', 'asf', None),
            ('wave', 'off', None),
            ('prs', 'asf', None),
            ('harvest', 'asf', None),
            ('batch', 'off', None),
            ('watchdog', 'asf', None),
            ('daily', 'asf', None),
        ])

    def test_an_absent_batch_is_off_and_every_step_runs(self):
        rc, out = self.run_tick(manifest=True)
        self.assertEqual(rc, 0)
        self.assertNotIn('has no owner', out)
        self.assertIn('batch     off      -\n', out)

    def test_asf_declared_for_a_step_asf_lacks_is_undeclared(self):
        self.write_product('steps:\n  batch: asf\n  health: asf\n')
        rows = dict((s, o) for s, o, _ in steps.resolve(env.load_product('sample')))
        self.assertEqual(rows['batch'], 'undeclared')
        self.assertEqual(rows['health'], 'asf')

    def test_manifest_table_golden(self):
        rc, out = self.run_tick(manifest=True)
        self.assertEqual(rc, 0)
        self.assertEqual(out, (
            'step      owner    command\n'
            'record    asf      asf.tick.tick:run_record_step\n'
            'health    command  bash ~/x/health.sh --fix\n'
            'groom     asf      asf.tick.step_groom:run\n'
            'wave      off      -\n'
            'prs       asf      asf.tick.step_prs:run\n'
            'harvest   asf      asf.tick.step_harvest:run\n'
            'batch     off      -\n'
            'watchdog  asf      asf.tick.step_watchdog:run\n'
            'daily     asf      asf.tick.step_daily:run\n'))

    def test_manifest_golden_all_asf_and_a_batch_command(self):
        self.write_product('steps:\n  batch: bash ~/q/merge-queue.sh --once\n')
        rc, out = self.run_tick(manifest=True)
        self.assertEqual(rc, 0)
        self.assertEqual(out, (
            'step      owner    command\n'
            'record    asf      asf.tick.tick:run_record_step\n'
            'health    asf      asf.tick.step_health:run\n'
            'groom     asf      asf.tick.step_groom:run\n'
            'wave      asf      asf.tick.step_wave:run\n'
            'prs       asf      asf.tick.step_prs:run\n'
            'harvest   asf      asf.tick.step_harvest:run\n'
            'batch     command  bash ~/q/merge-queue.sh --once\n'
            'watchdog  asf      asf.tick.step_watchdog:run\n'
            'daily     asf      asf.tick.step_daily:run\n'))

    def test_undeclared_batch_exits_2(self):
        self.write_product('steps:\n  batch: asf\n')
        rc, out = self.run_tick()
        self.assertEqual(rc, 2)
        self.assertEqual(out, 'tick: step batch has no owner — declare it under steps in '
                              'products/sample.yaml (asf | <command> | off)\n')

    def test_undeclared_step_refuses_before_running_anything(self):
        marker = os.path.join(self.tmp, 'ran')
        self.write_product(f'steps:\n  health: touch {marker}\n  wave: off\n  batch: asf\n')
        rc, out = self.run_tick()
        self.assertEqual(rc, 2)
        self.assertEqual(out, 'tick: step batch has no owner — declare it under steps in '
                              'products/sample.yaml (asf | <command> | off)\n')
        self.assertFalse(os.path.exists(marker))
        self.assertFalse(os.path.exists(self.record_path()))
        self.assertEqual(self.origin_commits(), 1)

    def test_steps_subset_only_needs_its_own_steps_declared(self):
        rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 0)
        self.assertNotIn('has no owner', out)

    def test_unknown_step_name_is_refused(self):
        rc, out = self.run_tick(steps='record,nope')
        self.assertEqual(rc, 2)
        self.assertIn('unknown step nope', out)


class LegacyStepTests(TickTestCase):
    product_yaml = ('steps:\n'
                    '  health: python3 -c \'print("hi")\'\n'
                    '  wave: off\n'
                    '  prs: off\n'
                    '  batch: off\n'
                    '  daily: python3 -c \'print("daily ran")\'\n')

    def test_command_output_is_prefixed_in_the_log(self):
        rc, out = self.run_tick(steps='health')
        self.assertEqual(rc, 0)
        self.assertEqual(steps_only(out), '[command:health] hi\n')

    def test_steps_subset_runs_only_those_and_in_manifest_order(self):
        rc, out = self.run_tick(steps='health,record')
        self.assertEqual(rc, 0)
        self.assertEqual(untimed(out).splitlines()[0], '[command:health] hi')
        # the one commit comes last: after every step, over the state and the tick line together
        self.assertEqual(steps_only(out).splitlines()[-1], f'tick: state committed and pushed ({self.record_path()})')
        self.assertNotIn('daily', out)

    def test_off_step_is_reported_not_run(self):
        rc, out = self.run_tick(steps='batch')
        self.assertEqual(rc, 0)
        self.assertEqual(steps_only(out), 'tick: step batch off (another job runs it)\n')

    def test_failing_command_step_exits_1_and_later_steps_still_run(self):
        self.write_product('steps:\n  health: python3 -c \'import sys; print("bad"); sys.exit(3)\'\n'
                           '  wave: python3 -c \'print("after")\'\n')
        rc, out = self.run_tick(steps='health,wave')
        self.assertEqual(rc, 1)
        self.assertEqual(steps_only(out).splitlines(), ['[command:wave] after', '[command:health] bad',
                                            'tick: step health exited 3'])

    def test_timeout_kills_a_sleep(self):
        self.write_config('tick:\n  step_timeout_s: 1\n')
        self.write_product('steps:\n  health: sleep 30\n')
        t0 = time.monotonic()
        rc, out = self.run_tick(steps='health')
        self.assertLess(time.monotonic() - t0, 15)
        self.assertEqual(rc, 1)
        self.assertIn('[command:health] timeout after 1s — killed', out)
        self.assertIn('tick: step health exited 124', out)

    def test_timeout_kills_the_whole_process_group(self):
        lines = []
        rc = steps.run_command('health', "sh -c 'sleep 30 & sleep 30'", 1, emit=lines.append)
        self.assertEqual(rc, 124)

    def test_b0119_output_is_emitted_as_the_command_runs_not_buffered_to_the_end(self):
        """A long command step (a factory-cron.sh, a factory-batch.sh) wrote nothing to the tick
        log until it exited: the whole point of a per-line log is to show a live step apart from
        a hung one, and a step that logs only at the end shows neither while it runs."""
        times = []
        rc = steps.run_command(
            'batch', "sh -c 'echo one; sleep 1; echo two'", 5,
            emit=lambda line: times.append((time.monotonic(), line)))
        self.assertEqual(rc, 0)
        self.assertEqual([line for _, line in times], ['[command:batch] one', '[command:batch] two'])
        self.assertGreater(times[1][0] - times[0][0], 0.5)

    def test_shadow_never_runs_a_command_step(self):
        marker = os.path.join(self.tmp, 'ran')
        self.write_product(f'steps:\n  health: touch {marker}\n')
        with mock.patch.object(tick, 'render_tables', return_value={}):
            rc, out = self.run_tick(shadow=True)
        self.assertEqual(rc, 0)
        self.assertIn('tick --shadow:', out)
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(self.origin_commits(), 1)  # and never pushes

    def test_daily_runs_once_a_day(self):
        rc, out = self.run_tick(steps='daily')
        self.assertEqual(steps_only(out), '[command:daily] daily ran\n')
        with open(steps.stamp_path(env.load_product('sample'))) as f:
            self.assertEqual(f.read().strip(), steps._today())

        rc, out = self.run_tick(steps='daily')
        self.assertEqual(steps_only(out), 'tick: step daily already ran today\n')

        rc, out = self.run_tick(steps='daily', daily=True)
        self.assertEqual(steps_only(out), '[command:daily] daily ran\n')

    def test_daily_runs_when_the_stamp_is_from_another_day(self):
        product = env.load_product('sample')
        with open(steps.stamp_path(product), 'w') as f:
            f.write('2001-01-01\n')
        rc, out = self.run_tick(steps='daily')
        self.assertEqual(steps_only(out), '[command:daily] daily ran\n')

    def test_failed_daily_is_not_stamped(self):
        self.write_product('steps:\n  daily: python3 -c \'raise SystemExit(1)\'\n')
        self.run_tick(steps='daily')
        self.assertFalse(os.path.exists(steps.stamp_path(env.load_product('sample'))))


class DrainingMoveTests(TickTestCase):
    """While a move drains (``asf upgrade --product``), the tick runs every step: a drain holds
    only new merge-queue cuts, never a launch (#32)."""
    product_yaml = ('steps:\n'
                    '  health: python3 -c \'print("health ran")\'\n'
                    '  wave: python3 -c \'print("wave launched")\'\n'
                    '  prs: off\n'
                    '  batch: off\n')

    def test_a_draining_move_keeps_every_step_and_the_launches(self):
        from asf import upgrade
        upgrade._write_json(upgrade.draining_path('sample'),
                            {'sha': 'f' * 40, 'pid': os.getpid(), 'at': time.time()})
        self.addCleanup(upgrade.clear_draining, 'sample')
        rc, out = self.run_tick(steps='health,wave')
        self.assertEqual(rc, 0, out)
        self.assertIn('health ran', out)
        self.assertIn('wave launched', out)


class DailyCatchUpTests(TickTestCase):
    """B-0123: a daily that missed its own clock catches up on the next regular tick, once,
    instead of waiting for tomorrow's daily clock to fire again."""

    product_yaml = ('steps:\n  health: off\n  wave: off\n  prs: off\n  harvest: off\n  batch: off\n'
                     'clocks:\n'
                     '  record:\n'
                     '    steps: [record]\n'
                     '    every: 5m\n'
                     '  dispatch:\n'
                     '    steps: [health, wave, prs, harvest, batch]\n'
                     '    every: 10m\n'
                     '  daily:\n'
                     '    steps: [daily]\n'
                     '    at: "00:00"\n')  # always already past by the time a test runs

    def test_a_failed_daily_catches_up_once_on_the_next_regular_tick(self):
        from asf.tick import step_daily
        with mock.patch.object(step_daily, 'run',
                                side_effect=[RuntimeError('daily parts failed: rollup'), 0]):
            rc, out = self.run_tick(steps='daily')  # the 06:00 (here: 00:00) clock's own tick
            self.assertEqual(rc, 1)
            self.assertFalse(os.path.exists(steps.stamp_path(env.load_product('sample'))))

            rc, out = self.run_tick(steps='health,wave,prs,harvest,batch')  # the next dispatch tick
            self.assertEqual(rc, 0)
            self.assertIn('daily: catching up — 00:00 run failed: daily parts failed: rollup', out)
            with open(steps.stamp_path(env.load_product('sample'))) as f:
                self.assertEqual(f.read().strip(), steps._today())

    def test_a_successful_daily_is_not_run_again_by_a_later_regular_tick(self):
        from asf.tick import step_daily
        with mock.patch.object(step_daily, 'run', return_value=0) as m:
            rc, out = self.run_tick(steps='daily')
            self.assertEqual(rc, 0)

            rc, out = self.run_tick(steps='health,wave,prs,harvest,batch')
            self.assertEqual(rc, 0)
            self.assertNotIn('daily', out)
        self.assertEqual(m.call_count, 1)


class SummaryTests(TickTestCase):
    def test_every_tick_ends_with_the_two_tables(self):
        rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 0)
        lines = untimed(out).rstrip('\n').split('\n')
        self.assertEqual(lines[0], f'tick: state committed and pushed ({self.record_path()})')
        self.assertEqual(lines[1:3], ['', 'IN FLIGHT — none'])
        self.assertEqual(lines[-2:], ['TICK — record ok', 'nothing launched, merged or stalled'])
        self.assertTrue(lines[-4].startswith('DONE since '), lines[-4])
        self.assertTrue(lines[-4].endswith('— none (first tick on this clock)'), lines[-4])
        self.assertTrue(os.path.exists(summary.stamp_path(env.load_product('sample'), 'record')))

    def test_a_tick_with_every_step_off_still_prints_the_tables(self):
        self.write_product('steps:\n  batch: off\n')
        rc, out = self.run_tick(steps='batch')
        self.assertEqual(rc, 0)
        self.assertEqual(out.split('\n')[0], 'tick: step batch off (another job runs it)')
        self.assertIn('\nIN FLIGHT — none\n', out)
        self.assertIn('\nDONE since ', out)

    def test_shadow_and_manifest_print_no_tables(self):
        with mock.patch.object(tick, 'render_tables', return_value={}):
            _, out = self.run_tick(shadow=True)
        self.assertNotIn('IN FLIGHT', out)
        _, out = self.run_tick(manifest=True)
        self.assertNotIn('IN FLIGHT', out)

    def test_a_summary_failure_never_changes_the_rc(self):
        with mock.patch.object(summary, 'render', side_effect=ValueError('boom')):
            rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 0)
        self.assertEqual(out.rstrip('\n').split('\n')[-1], 'tick: summary not rendered (boom)')

    def test_a_refused_push_is_named_failed_in_the_tick_line_not_ok(self):
        """B-0146: the tick exited 1 on a refused push while its TICK line still read
        ``record ok`` — the failing step named nowhere a person reads. The line and the exit
        code must agree, and the reason (the push's own line) rides with it."""
        hook = os.path.join(self.origin, 'hooks', 'pre-receive')
        with open(hook, 'w') as f:
            f.write('#!/bin/sh\nexit 1\n')
        os.chmod(hook, 0o755)
        rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 1)
        tick_line = [l for l in out.splitlines() if l.startswith('TICK — ')][0]
        self.assertIn('record ok', tick_line)
        self.assertIn('commit FAILED (push refused)', tick_line)


class StatusSnapshotTests(TickTestCase):
    """F-0118: the tick's last act is a snapshot of the FACTORY STATUS table, written atomically,
    for every session's status line (``asf status --line``)."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(tick, 'write_tick_line', lambda ctx, ran: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def snapshot_path(self):
        return status.snapshot_path(env.load_product('sample'))

    def test_tick_writes_a_parseable_snapshot_with_one_line_per_row(self):
        rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 0)
        path = self.snapshot_path()
        self.assertTrue(os.path.exists(path))
        with open(path, encoding='utf-8') as f:
            lines = f.read().splitlines()
        self.assertRegex(lines[0], r'^# asf status sample ts=\S+ stale_after_s=\d+$')
        expected = status.rows(self.record_path(), env.load_product('sample'))
        self.assertEqual(len(lines) - 1, len(expected))

    def test_write_snapshot_goes_through_os_replace_a_failed_one_leaves_only_a_tmp(self):
        """``status.write_snapshot`` called directly (not through a tick): a failed ``os.replace``
        must never leave a partial ``status.txt`` — only the temp file it wrote first (PD8)."""
        product = env.load_product('sample')
        with mock.patch('os.replace', side_effect=OSError('disk gone')):
            with self.assertRaises(OSError):
                status.write_snapshot(self.operator, product)
        path = status.snapshot_path(product)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(len(glob.glob(path + '.*.tmp')), 1)

    def test_a_second_tick_replaces_the_snapshot_in_place_with_a_fresh_ts(self):
        self.run_tick(steps='record')
        path = self.snapshot_path()
        with open(path, encoding='utf-8') as f:
            first = f.read().splitlines()[0]
        time.sleep(1.1)
        self.run_tick(steps='record')
        with open(path, encoding='utf-8') as f:
            second = f.read().splitlines()[0]
        self.assertNotEqual(first, second)

    def test_a_render_that_raises_prints_one_line_leaves_the_rc_and_no_tmp_behind(self):
        with mock.patch.object(status, 'write_snapshot', side_effect=ValueError('boom')):
            rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 0)
        self.assertEqual(untimed(out).rstrip('\n').split('\n')[-1],
                         'tick: status snapshot not written (ValueError: boom)')
        product = env.load_product('sample')
        self.assertEqual(glob.glob(status.snapshot_path(product) + '.*.tmp'), [])

    def test_no_record_and_no_backlog_dir_leaves_an_existing_snapshot_untouched(self):
        product = env.load_product('sample')
        path = status.snapshot_path(product)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('existing content\n')

        class FakeProduct:
            name = 'sample'
            backlog_dir = None

        class FakeCtx:
            has_record = False
            product = FakeProduct()

            def record_root(self):
                raise AssertionError('must not clone the record just to draw a status line')

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            tick.write_status_snapshot(FakeCtx())
        with open(path, encoding='utf-8') as f:
            after = f.read()
        self.assertEqual(after, 'existing content\n')
        self.assertIn('tick: status snapshot not written (no record to render)', out.getvalue())


class StepTimingTests(TickTestCase):
    """Each step names its seconds as it ends, and the tick its total: a slow step is visible in
    the log while the tick still runs, not inferred after."""

    def test_each_step_prints_its_seconds_at_its_end_and_the_tick_its_total(self):
        self.write_product("steps:\n  health: python3 -c 'print(1)'\n  batch: off\n")
        rc, out = self.run_tick(steps='record,health')
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        timings = [l for l in lines if TIMING_RE.match(l) and not l.startswith('[record:')]
        self.assertEqual([l.split(' ')[0] for l in timings], ['[step:record]', '[step:health]', 'tick:'])
        self.assertLess(lines.index(timings[0]), lines.index('[command:health] 1'))
        self.assertLess(lines.index('[command:health] 1'), lines.index(timings[1]))
        self.assertLess(lines.index(timings[2]), lines.index('IN FLIGHT — none'))

    def test_failed_step_logs_traceback(self):
        """§12: a step that raises is one ``FAILED`` line and then its full traceback in the
        tick log — the line names the step, the traceback the code that broke."""
        from asf.tick import step_wave

        def wave(ctx):
            return _explode_in_the_wave()
        with mock.patch.object(step_wave, 'run', wave):
            rc, out = self.run_tick(steps='record,wave')
        self.assertEqual(rc, 1)
        self.assertIn('[step:wave] FAILED the wave broke', out)
        tail = out[out.index('[step:wave] FAILED'):]
        self.assertIn('Traceback (most recent call last):', tail)
        self.assertIn('_explode_in_the_wave', tail)
        self.assertIn('ValueError: the wave broke', tail)

    def test_the_line_shape(self):
        self.assertEqual(
            tick.step_end_line('wave', 3.14159, ok=True, owner='asf', pid=7,
                                at='2026-09-28T15:03:58Z'),
            '[step:wave] 3.1s ok=yes owner=asf pid=7 at=2026-09-28T15:03:58Z')
        self.assertEqual(
            tick.step_end_line('wave', 3.14159, ok=False, owner='asf', pid=7,
                                at='2026-09-28T15:03:58Z'),
            '[step:wave] 3.1s ok=no owner=asf pid=7 at=2026-09-28T15:03:58Z')
        self.assertEqual(
            tick.step_start_line('wave', 'command', pid=7, at='2026-09-28T15:03:58Z'),
            '[step:wave] start owner=command pid=7 at=2026-09-28T15:03:58Z')
        self.assertTrue(
            tick.step_end_line('wave', 3.14159, ok=True, owner='asf', pid=7,
                                at='2026-09-28T15:03:58Z').startswith('[step:wave] 3.1s'))
        self.assertEqual(tick.total_line(12), 'tick: total 12.0s')


class StepStartEndLines(TickTestCase):
    """F-0142: a start line before each step the tick actually runs, and the grown end line after
    it — printed around exactly the steps the loop runs, never around one it skips."""

    START_RE = re.compile(r'^\[step:([a-z-]+)\] start owner=(\S+) pid=(\d+) at=(\S+)$')
    END_RE = re.compile(r'^\[step:([a-z-]+)\] (\d+\.\d)s ok=(yes|no) owner=(\S+) pid=(\d+) at=(\S+)$')

    def _start_fields(self, line):
        m = self.START_RE.match(line)
        self.assertIsNotNone(m, line)
        step, owner, pid, at = m.groups()
        return {'step': step, 'owner': owner, 'pid': int(pid), 'at': at}

    def _end_fields(self, line):
        m = self.END_RE.match(line)
        self.assertIsNotNone(m, line)
        step, seconds, ok, owner, pid, at = m.groups()
        return {'step': step, 'seconds': seconds, 'ok': ok, 'owner': owner, 'pid': int(pid),
                'at': at}

    def test_the_pair_around_an_asf_step_and_a_command_step(self):
        self.write_product("steps:\n  health: python3 -c 'print(1)'\n  batch: off\n")
        rc, out = self.run_tick(steps='record,health')
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        starts = [l for l in lines if self.START_RE.match(l)]
        ends = [l for l in lines if self.END_RE.match(l)]
        self.assertEqual([self._start_fields(l)['step'] for l in starts], ['record', 'health'])
        self.assertEqual([self._end_fields(l)['step'] for l in ends], ['record', 'health'])
        record_start, health_start = starts
        record_end, health_end = ends

        def first(prefix):
            return next(i for i, l in enumerate(lines) if l.startswith(prefix))

        self.assertLess(lines.index(record_start), first('[record:clone]'))
        self.assertLess(first('[record:clone]'), lines.index(record_end))
        self.assertLess(lines.index(record_end), lines.index(health_start))
        self.assertLess(lines.index(health_start), first('[command:health] 1'))
        self.assertLess(first('[command:health] 1'), lines.index(health_end))

        rf, re_ = self._start_fields(record_start), self._end_fields(record_end)
        hf, he = self._start_fields(health_start), self._end_fields(health_end)
        self.assertEqual((rf['owner'], re_['owner'], re_['ok']), ('asf', 'asf', 'yes'))
        self.assertEqual((hf['owner'], he['owner'], he['ok']), ('command', 'command', 'yes'))
        for fields in (rf, re_, hf, he):
            self.assertEqual(fields['pid'], os.getpid())
            datetime.datetime.strptime(fields['at'], '%Y-%m-%dT%H:%M:%SZ')

    def test_a_step_that_raises_ends_ok_no(self):
        from asf.tick import step_wave

        def wave(ctx):
            return _explode_in_the_wave()
        with mock.patch.object(step_wave, 'run', wave):
            rc, out = self.run_tick(steps='record,wave')
        self.assertEqual(rc, 1)
        lines = out.splitlines()
        start_i = next(i for i, l in enumerate(lines) if l.startswith('[step:wave] start '))
        failed_i = lines.index('[step:wave] FAILED the wave broke')
        traceback_i = next(i for i, l in enumerate(lines)
                            if l.startswith('Traceback (most recent call last):'))
        end_i = next(i for i, l in enumerate(lines)
                     if l.startswith('[step:wave] ') and self.END_RE.match(l))
        self.assertTrue(start_i < failed_i < traceback_i < end_i)
        self.assertEqual(self._end_fields(lines[end_i])['ok'], 'no')

    def test_no_start_or_end_line_for_a_step_that_is_off(self):
        self.write_product('steps:\n  batch: off\n')
        rc, out = self.run_tick(steps='batch')
        self.assertIn('tick: step batch off (another job runs it)', out)
        self.assertNotIn('[step:batch]', out)

    def test_no_start_or_end_line_when_daily_already_ran_today(self):
        self.write_product("steps:\n  daily: python3 -c 'print(1)'\n")
        self.run_tick(steps='daily')
        rc, out = self.run_tick(steps='daily')
        self.assertIn('tick: step daily already ran today', out)
        self.assertNotIn('[step:daily]', out)

    def test_no_start_line_but_an_ok_end_line_when_batch_is_at_ci_capacity(self):
        self.write_product("steps:\n  batch: python3 -c 'print(1)'\n")
        resolved = capacity.Resolved(sessions=4, sessions_bound='default', ci=2, ci_bound='product',
                                      ci_inflight=3, batch={}, reserve={})
        with mock.patch.object(capacity, 'resolve', return_value=resolved):
            rc, out = self.run_tick(steps='batch')
        self.assertIn('waits    batch — at ci capacity (3/2)', out)
        self.assertNotIn('[step:batch] start', out)
        end_lines = [l for l in out.splitlines()
                     if l.startswith('[step:batch] ') and self.END_RE.match(l)]
        self.assertEqual(len(end_lines), 1)
        self.assertEqual(self._end_fields(end_lines[0])['ok'], 'yes')

    def test_no_start_line_but_an_ok_end_line_when_the_ci_queue_holds_batch(self):
        self.write_product("steps:\n  batch: python3 -c 'print(1)'\n")
        resolved = capacity.Resolved(sessions=4, sessions_bound='default', ci=2, ci_bound='product',
                                      ci_inflight=1, batch={}, reserve={})
        with mock.patch.object(capacity, 'resolve', return_value=resolved), \
                mock.patch.object(tick.ci_queue, 'admit',
                                   return_value=ci_queue.Decision(False, 'held')):
            rc, out = self.run_tick(steps='batch')
        self.assertNotIn('[step:batch] start', out)
        end_lines = [l for l in out.splitlines()
                     if l.startswith('[step:batch] ') and self.END_RE.match(l)]
        self.assertEqual(len(end_lines), 1)
        self.assertEqual(self._end_fields(end_lines[0])['ok'], 'yes')

    def test_no_start_or_end_line_when_the_steps_own_lock_is_held(self):
        self.write_product("steps:\n  batch: python3 -c 'print(1)'\n")
        held = tick.acquire_step_lock(env.load_product('sample'), 'batch')
        self.addCleanup(held.close)
        rc, out = self.run_tick(steps='batch')
        self.assertIn('tick: step batch is already running — skipped', out)
        self.assertNotIn('[step:batch]', out)

    def test_the_start_line_is_in_the_log_while_the_step_still_runs(self):
        log_path = os.path.join(self.tmp, 'tick.log')
        script_path = os.path.join(self.tmp, 'count_start.py')
        with open(script_path, 'w') as f:
            f.write("import sys\n"
                     "print(open(sys.argv[1]).read().count('[step:health] start '))\n")
        self.write_product(f"steps:\n  health: python3 {script_path} {log_path}\n  batch: off\n")
        with open(log_path, 'w') as f, contextlib.redirect_stdout(f):
            tick.cmd_tick(_args(steps='health'))
        with open(log_path) as f:
            out = f.read()
        self.assertIn('[command:health] 1', out)


class TickLockTests(TickTestCase):
    """One tick per product at a time: two jobs of one product (the 10m clock and the daily one)
    share the record clone, and a concurrent fetch/push there failed with ``cannot lock ref``."""

    def hold_lock(self):
        import fcntl
        f = open(tick.lock_path(env.load_product('sample')), 'a')
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(f.close)
        return f

    def test_a_tick_while_another_holds_the_product_skips_and_touches_nothing(self):
        self.hold_lock()
        rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 0)
        self.assertEqual(out, 'tick: another tick of sample is running — skipped\n')
        self.assertEqual(self.origin_commits(), 1)
        self.assertFalse(os.path.exists(self.record_path()))

    def test_the_daily_waits_for_the_running_tick(self):
        held = self.hold_lock()
        waited = []

        def sleep(s):  # the running tick ends while the daily waits
            waited.append(s)
            held.close()
        with mock.patch.object(tick.time, 'sleep', sleep):
            rc, out = self.run_tick(steps='record,daily')
        self.assertTrue(waited)
        self.assertNotIn('skipped', out)
        self.assertEqual(self.origin_commits(), 2)

    def test_the_lock_is_released_after_the_tick(self):
        self.run_tick(steps='record')
        rc, out = self.run_tick(steps='record')
        self.assertNotIn('skipped', out)


class CommandClockLockTests(TickTestCase):
    """A clock of command steps only runs its commands outside the product lock, each under a
    lock of its own: a legacy script running for half an hour no longer skips the product's
    main clock, and never overlaps itself."""

    product_yaml = "steps:\n  batch: python3 -c 'print(\"batch ran\")'\n"

    def nested(self, inner, **kw):
        """Run ``steps=inner`` as a second tick while the first one's command step runs."""
        seen = {}
        real = steps.run_command

        def run_command(step, command, timeout, **k):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                seen['rc'] = tick.cmd_tick(_args(steps=inner))
            seen['out'] = buf.getvalue()
            return real(step, command, timeout, **k)
        with mock.patch.object(steps, 'run_command', run_command):
            rc, out = self.run_tick(**kw)
        return rc, out, seen

    def test_an_asf_tick_is_not_skipped_while_a_command_clock_runs(self):
        rc, out, seen = self.nested('record', steps='batch')
        self.assertEqual(rc, 0)
        self.assertIn('[command:batch] batch ran', out)
        self.assertNotIn('skipped', seen['out'])
        self.assertEqual(seen['rc'], 0)
        self.assertEqual(self.origin_commits(), 2)  # the inner tick pushed its state

    def test_the_same_command_step_never_overlaps_itself(self):
        rc, out, seen = self.nested('batch', steps='batch')
        self.assertEqual(seen['rc'], 0)
        self.assertIn('tick: step batch is already running — skipped', seen['out'])
        self.assertNotIn('[command:batch]', seen['out'])
        self.assertIn('[command:batch] batch ran', out)

    def test_a_held_step_lock_skips_only_that_step(self):
        held = tick.acquire_step_lock(env.load_product('sample'), 'batch')
        self.addCleanup(held.close)
        rc, out = self.run_tick(steps='batch')
        self.assertEqual(rc, 0)
        self.assertIn('tick: step batch is already running — skipped', out)
        self.assertNotIn('[command:batch]', out)

    def test_a_command_clock_runs_while_the_product_lock_is_held(self):
        import fcntl
        f = open(tick.lock_path(env.load_product('sample')), 'a')
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(f.close)
        rc, out = self.run_tick(steps='batch')
        self.assertEqual(rc, 0)
        self.assertIn('[command:batch] batch ran', out)
        self.assertNotIn('is running — skipped', out)

    def test_two_asf_ticks_still_exclude_each_other(self):
        seen = {}

        def step0(root, product, fresh=False):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                seen['rc'] = tick.cmd_tick(_args(steps='record'))
            seen['out'] = buf.getvalue()
            return _fake_step0(root, product, fresh)
        with mock.patch.object(tick, 'run_step0', step0):
            rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 0)
        self.assertEqual(seen, {'rc': 0, 'out': 'tick: another tick of sample is running — skipped\n'})
        self.assertEqual(self.origin_commits(), 2)


class TickLineTests(unittest.TestCase):
    """What `tick_line` writes survives `metrics append ticks`: no unknown key, no missing one."""

    def test_the_line_is_a_valid_ticks_event(self):
        from asf.metrics import metrics
        ctx = tick.Context(env.Product('sample', {}))
        ran = [{'step': 'record', 'ok': True, 'seconds': 1.5}, {'step': 'wave', 'ok': False, 'seconds': 2.0}]
        line = tick.tick_line(ctx, ran)
        self.assertEqual(set(line) - set(metrics.SCHEMAS['ticks']), set())
        required = {k for k, (_kinds, default) in metrics.SCHEMAS['ticks'].items() if default is metrics.REQ}
        self.assertEqual(required - set(line), set())
        ev = metrics.validate('ticks', line, {})
        self.assertEqual(ev['steps'], ran)
        self.assertEqual(ev['product'], 'sample')


class Step0Tests(unittest.TestCase):
    """What step 0 hands the backfill: CI runs only, of the product's workflow; no launcher dir."""

    def step0(self, product):
        from asf.metrics import metrics
        from asf.record import ingest
        from asf.tick import file_bugs
        calls = []
        with mock.patch.object(metrics, 'cmd_backfill', lambda a, r: calls.append(a)), \
                mock.patch.object(metrics, 'cmd_rollup', lambda a, r: 0), \
                mock.patch.object(ingest, 'cmd_ingest', lambda a, r: 0), \
                mock.patch.object(file_bugs, 'cmd_file_bugs', lambda a, r: 0), \
                mock.patch.object(tick, 'do_index', lambda r: 0), \
                mock.patch.dict(os.environ):
            tick.run_record('/nowhere', product)
        return calls

    def test_backfill_reads_the_products_workflow_and_no_launcher_dir(self):
        (a,) = self.step0(env.Product('p', {'ci': {'provider': 'gh-actions', 'workflow': 'build'}}))
        self.assertEqual((a.workflow, a.launch_dir, a.sessions, a.log), ('build', None, None, None))
        self.assertIsNone(a.days)
        (a,) = self.step0(env.Product('p', {}))
        self.assertEqual(a.workflow, 'ci')

    def test_no_ci_no_backfill(self):
        self.assertEqual(self.step0(env.Product('p', {'ci': {'provider': 'none'}})), [])
        self.assertEqual(self.step0(env.Product('p', {'ci': 'none'})), [])

    def test_each_part_prints_its_seconds(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.step0(env.Product('p', {}))
        lines = out.getvalue().splitlines()
        # the wave's parts first (step 0), then the tail and the index again
        self.assertEqual([ln.split()[0] for ln in lines],
                         ['[record:ingest]', '[record:index]', '[record:backfill]',
                          '[record:file-bugs]', '[record:rollup]', '[record:index]'])
        self.assertTrue(all(TIMING_RE.match(ln) for ln in lines), lines)

    def test_a_part_that_raises_still_prints_its_seconds(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(RuntimeError):
            with tick.timed('ingest'):
                raise RuntimeError('boom')
        self.assertRegex(out.getvalue(), r'^\[record:ingest\] \d+\.\ds\n$')


class RecordStepTimingTests(TickTestCase):
    def test_the_record_step_times_the_clone_only(self):
        rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 0)
        parts = [ln.split()[0] for ln in out.splitlines() if ln.startswith('[record:')]
        self.assertEqual(parts, ['[record:clone]'])


if __name__ == '__main__':
    unittest.main()


class CommandStepHostPressure(unittest.TestCase):
    """A command step is not started while the host is over its guard; a quiet host runs it."""

    def test_loaded_host_skips_the_command(self):
        from asf.tick import steps
        lines = []
        with mock.patch.dict(os.environ, {'ASF_HOST_READING': '99 10 50'}):
            rc = steps.run_command('batch', "sh -c 'echo ran'", 5, emit=lines.append)
        self.assertEqual(rc, 0)
        self.assertEqual(len(lines), 1)
        self.assertIn('[command:batch] held: host pressure', lines[0])
        self.assertNotIn('ran', lines[0])

    def test_quiet_host_runs_the_command(self):
        from asf.tick import steps
        lines = []
        with mock.patch.dict(os.environ, {'ASF_HOST_READING': '0 10 0'}):
            rc = steps.run_command('batch', "sh -c 'echo ran'", 5, emit=lines.append)
        self.assertEqual((rc, lines), (0, ['[command:batch] ran']))


class MinimalProductTick(TickTestCase):
    """F-0247: a product with no product-specific config at all — a file with only its repo and
    record, an empty config.yaml — ticks on every default: each tunable at its built-in value, no
    product's test-runner knob in a worker's environment, no config row."""

    def test_a_minimal_product_ticks_on_the_defaults(self):
        from asf import config_keys, doctor, gitops, github
        from asf.tick import watchdog
        from asf.workers import headroom, lifecycle
        cfg = env.load_config()
        self.assertEqual(cfg, {})
        self.assertEqual(config_keys.problems(cfg), [])
        self.assertEqual(doctor.check_config_keys(cfg), [])
        self.assertEqual(env.worker_env(cfg, env.load_product('sample')), {})
        self.assertEqual((gitops.timeout_s(), gitops.fetch_timeout_s()), (120, 120))
        self.assertEqual((github.json_timeout_s(), github.cmd_timeout_s(),
                          github.pr_list_limit()), (60, 30, 300))
        self.assertEqual((lifecycle.round_cap(), lifecycle.loop_cap(), lifecycle.empty_ends_cap(),
                          lifecycle.incomplete_cap(), lifecycle.hook_refusal_cap()), (3, 3, 2, 2, 2))
        self.assertEqual(headroom.default_cost(), headroom.DEFAULT_COST)
        self.assertEqual(watchdog.seconds_for(None, ['wave'], interval_s=600), 3600)
        self.assertEqual(ci_queue.tunable('STUCK_RETRY_S'), 1800)
        rc, out = self.run_tick(steps='record,prs,harvest')   # the steps that need no host
        self.assertEqual(rc, 0, out)
        self.assertIn('committed and pushed', out)
