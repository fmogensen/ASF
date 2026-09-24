"""asf.tick.tick — the live tick works in its own clone and pushes (B-0013); the step manifest."""
import argparse
import contextlib
import hashlib
import io
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from asf import env
from asf.tick import shadow, steps, summary, tick

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


TIMING_RE = re.compile(r'^(\[(?:step|record):[a-z-]+\]|tick: total) \d+\.\ds$')


def untimed(out):
    """``out`` without the per-step and per-record-part timing lines and the tick's total."""
    return ''.join(l for l in out.splitlines(True) if not TIMING_RE.match(l.rstrip('\n')))


def steps_only(out):
    """A tick's stdout without the two summary blocks and the per-step timing lines — for the
    assertions whose subject is the step log (F-0078)."""
    return untimed(out.split('\n\nIN FLIGHT')[0] + '\n')


def _tree_digest(path):
    """Every file under ``path`` (the .git dir included), name and bytes, hashed."""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            h.update(os.path.relpath(full, path).encode())
            with open(full, 'rb') as f:
                h.update(f.read())
    return h.hexdigest()


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

    def test_operator_checkout_is_untouched(self):
        before = _tree_digest(self.operator)
        self.run_tick(steps='record')
        self.run_tick(steps='record')
        self.assertEqual(_tree_digest(self.operator), before)

    def test_no_backlog_dir_is_reported_not_raised(self):
        with open(env.product_path('sample'), 'w') as f:
            f.write('repo_slug: x/y\n')
        rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 1)
        self.assertIn('tick: record failed', out)


class GroomStepOrderTests(TickTestCase):
    """The tick's third step: after ``health``, before ``wave``, in the tick's own record clone."""

    product_yaml = 'steps:\n  batch: off\n  daily: off\n  prs: off\n  harvest: off\n'

    def test_the_groom_runs_between_health_and_wave_and_is_logged(self):
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
        self.assertEqual(order, ['health', 'groom', 'wave'])
        self.assertIn('groom 2026-01-01: applied 0, inbox 1 card(s)', out)
        self.assertEqual(seen, [(True, None, self.record_path())])
        import json
        day = time.strftime('%Y-%m-%d', time.gmtime())
        log = _git(['show', f'main:metrics/ticks/{day}.jsonl'], self.origin)
        steps_run = json.loads(log.splitlines()[-1])['steps']
        self.assertIn(('groom', True), [(s['step'], s['ok']) for s in steps_run])
        self.assertEqual([s['step'] for s in steps_run][:4], ['record', 'health', 'groom', 'wave'])

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
            ('batch', 'undeclared', None),
            ('daily', 'asf', None),
        ])

    def test_asf_declared_for_a_step_asf_lacks_is_undeclared(self):
        self.write_product('steps:\n  batch: asf\n  health: asf\n')
        rows = dict((s, o) for s, o, _ in steps.resolve(env.load_product('sample')))
        self.assertEqual(rows['batch'], 'undeclared')
        self.assertEqual(rows['health'], 'asf')

    def test_manifest_table_golden(self):
        rc, out = self.run_tick(manifest=True)
        self.assertEqual(rc, 0)
        self.assertEqual(out, (
            'step     owner       command\n'
            'record   asf         asf.tick.tick:run_record_step\n'
            'health   command     bash ~/x/health.sh --fix\n'
            'groom    asf         asf.tick.step_groom:run\n'
            'wave     off         -\n'
            'prs      asf         asf.tick.step_prs:run\n'
            'harvest  asf         asf.tick.step_harvest:run\n'
            'batch    undeclared  -\n'
            'daily    asf         asf.tick.step_daily:run\n'))

    def test_manifest_golden_all_asf_and_a_batch_command(self):
        self.write_product('steps:\n  batch: bash ~/q/merge-queue.sh --once\n')
        rc, out = self.run_tick(manifest=True)
        self.assertEqual(rc, 0)
        self.assertEqual(out, (
            'step     owner    command\n'
            'record   asf      asf.tick.tick:run_record_step\n'
            'health   asf      asf.tick.step_health:run\n'
            'groom    asf      asf.tick.step_groom:run\n'
            'wave     asf      asf.tick.step_wave:run\n'
            'prs      asf      asf.tick.step_prs:run\n'
            'harvest  asf      asf.tick.step_harvest:run\n'
            'batch    command  bash ~/q/merge-queue.sh --once\n'
            'daily    asf      asf.tick.step_daily:run\n'))

    def test_undeclared_batch_exits_2(self):
        self.write_product('')
        rc, out = self.run_tick()
        self.assertEqual(rc, 2)
        self.assertEqual(out, 'tick: step batch has no owner — declare it under steps in '
                              'products/sample.yaml (asf | <command> | off)\n')

    def test_undeclared_step_refuses_before_running_anything(self):
        marker = os.path.join(self.tmp, 'ran')
        self.write_product(f'steps:\n  health: touch {marker}\n  wave: off\n')
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
        self.assertEqual(steps_only(out).splitlines(), ['[command:health] bad', 'tick: step health exited 3',
                                            '[command:wave] after'])

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


class SummaryTests(TickTestCase):
    def test_every_tick_ends_with_the_two_tables(self):
        rc, out = self.run_tick(steps='record')
        self.assertEqual(rc, 0)
        lines = untimed(out).rstrip('\n').split('\n')
        self.assertEqual(lines[0], f'tick: state committed and pushed ({self.record_path()})')
        self.assertEqual(lines[1:3], ['', 'IN FLIGHT — none'])
        self.assertTrue(lines[-1].startswith('DONE since '), lines[-1])
        self.assertTrue(lines[-1].endswith('— none (first tick on this clock)'), lines[-1])
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
        self.assertEqual(tick.step_timing_line('wave', 3.14159), '[step:wave] 3.1s')
        self.assertEqual(tick.total_line(12), 'tick: total 12.0s')


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
            tick.run_step0('/nowhere', product)
        return calls

    def test_backfill_reads_the_products_workflow_and_no_launcher_dir(self):
        (a,) = self.step0(env.Product('p', {'ci': {'provider': 'gh-actions', 'workflow': 'build'}}))
        self.assertEqual((a.workflow, a.launch_dir, a.sessions, a.log), ('build', None, None, None))
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
        self.assertEqual([ln.split()[0] for ln in lines],
                         ['[record:backfill]', '[record:ingest]', '[record:file-bugs]',
                          '[record:rollup]', '[record:index]'])
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
