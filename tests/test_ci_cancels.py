"""asf.ci_cancels — the nine causes and their groups, and the ledger this module reads (never
writes). `Claims` writes through the landed `ci_queue.claim_cancel` into a tempfile state dir and
reads back through `ci_cancels.claims()`; no `gh` and no network anywhere in this file."""
import datetime
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from asf import ci_cancels, ci_queue


def _hook_free_env():
    """The subprocess env for a throwaway fixture repo's own git calls: with the factory's
    ``core.hooksPath`` override (set via ``GIT_CONFIG_*`` env vars for every git process in this
    worktree, F-0230 PD11) stripped, so a fixture commit never runs the product's own commit
    hooks, plus a fixed identity so the fixture never depends on a local ``user.name``/
    ``user.email``."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_CONFIG')}
    env['GIT_AUTHOR_NAME'] = env['GIT_COMMITTER_NAME'] = 'ci-cancels-fixture'
    env['GIT_AUTHOR_EMAIL'] = env['GIT_COMMITTER_EMAIL'] = 'fixture@example.com'
    return env


def _git(args, cwd):
    p = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True,
                        env=_hook_free_env())
    if p.returncode != 0:
        raise RuntimeError(f'git {args} in {cwd} failed: {p.stderr}')
    return p.stdout.strip()


def _init_repo():
    """A tempdir holding one repo, ``main`` branch, one empty root commit."""
    repo = tempfile.mkdtemp()
    _git(['init', '-q', '-b', 'main'], repo)
    _git(['config', 'user.email', 'fixture@example.com'], repo)
    _git(['config', 'user.name', 'ci-cancels-fixture'], repo)
    _git(['commit', '-q', '--allow-empty', '-m', 'root'], repo)
    return repo


def _commit(repo, msg, fname, content):
    path = os.path.join(repo, fname)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(content + '\n')
    _git(['add', fname], repo)
    _git(['commit', '-q', '-m', msg], repo)
    return _git(['rev-parse', 'HEAD'], repo)


def _amend(repo, msg, extra=()):
    _git(['commit', '--amend', '-q', '-m', msg, *extra], repo)
    return _git(['rev-parse', 'HEAD'], repo)


def _literal_causes(path):
    """Every cause string a `claim_cancel(...)` call in `path` passes literally — the third
    positional argument, which may be a plain string or an `'a' if cond else 'b'` ternary. A call
    whose cause is a variable (the `def claim_cancel(...)` line itself) is not matched."""
    with open(path, encoding='utf-8') as f:
        text = f.read()
    found = set()
    for m in re.finditer(
            r"claim_cancel\(\s*[^,\n]+,\s*[^,\n]+,\s*"
            r"('[^']*'(?:\s+if\s+.+?\s+else\s+'[^']*')?)", text):
        found |= set(re.findall(r"'([^']+)'", m.group(1)))
    return found


class Causes(unittest.TestCase):
    def test_the_nine_causes_and_their_groups(self):
        self.assertEqual(ci_cancels.CAUSES,
                          ('relief', 'stall', 'dedupe', 'merged', 'timeout', 'rewrite', 'newhead',
                           'unresolved', 'unclaimed'))
        seen = set()
        for causes in ci_cancels.GROUPS.values():
            self.assertFalse(seen & set(causes), 'a cause named in two groups')
            seen |= set(causes)
        self.assertEqual(seen, set(ci_cancels.CAUSES), 'GROUPS does not partition CAUSES exactly')

    def test_every_landed_cause_string_is_honoured_or_reread(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        found = set()
        for rel in ('asf/ci_queue.py', 'asf/harvest/lane.py'):
            found |= _literal_causes(os.path.join(root, rel))
        self.assertTrue(found, 'no claim_cancel(...) cause literal was found — the regex needs a '
                         'second look, not a green test on an empty match')
        known = set(ci_cancels.HONOURED) | ci_cancels.REREAD
        self.assertEqual(found, known,
                          f'{found ^ known} — a cause string landed in asf/ci_queue.py or '
                          f'asf/harvest/lane.py that F-0230 PD2 has not decided the partition of')

    def test_a_claim_beats_every_inference(self):
        """A run also superseded by a content-free re-push and also holding a merged PR is
        `relief` when the ledger claims it `relief` — the claim is tried first (C4) and neither
        the merge nor the head pair (unresolved, `repo=None`) is ever reached."""
        run = {'id': 1, 'head_branch': 'worker/T-1', 'head_sha': 'aaa', 'event': 'pull_request',
               'pr': 42, 'created_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-01T01:00:00Z'}
        later = {'id': 2, 'head_branch': 'worker/T-1', 'head_sha': 'bbb',
                 'created_at': '2026-01-01T00:30:00Z', 'conclusion': 'success'}
        claims = {'1': {'cause': 'relief'}}
        prs = {42: '2026-01-01T00:45:00Z'}
        got, stray = ci_cancels.cause(run, claims, [run, later], {}, prs, None, 'main', False)
        self.assertEqual(got, 'relief')
        self.assertIsNone(stray)

    def test_a_job_at_its_limit_is_a_timeout_not_a_supersede(self):
        later = {'id': 2, 'head_branch': 'b', 'head_sha': 'zzz',
                 'created_at': '2026-01-01T00:30:00Z', 'conclusion': 'success'}
        run = {'id': 1, 'head_branch': 'b', 'head_sha': 'aaa', 'event': 'push',
               'created_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-01T06:00:00Z',
               'jobs': [{'name': 'build', 'started_at': '2026-01-01T00:00:00Z',
                         'completed_at': '2026-01-01T06:00:00Z'}]}
        got, stray = ci_cancels.cause(run, {}, [run, later], {}, {}, None, 'main', False)
        self.assertEqual(got, 'timeout')
        self.assertIsNone(stray)

        short = dict(run, jobs=[{'name': 'build', 'started_at': '2026-01-01T00:00:00Z',
                                  'completed_at': '2026-01-01T00:12:00Z'}])
        got2, _stray2 = ci_cancels.cause(short, {}, [short, later], {}, {}, None, 'main', False)
        self.assertNotEqual(got2, 'timeout')

    def test_an_inferred_merge_needs_the_merge_before_the_cancel(self):
        run = {'id': 1, 'head_branch': 'worker/T-1', 'head_sha': 'aaa', 'event': 'pull_request',
               'pr': 7, 'created_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-01T01:00:00Z'}
        got, _stray = ci_cancels.cause(run, {}, [run], {}, {7: '2026-01-01T00:59:00Z'}, None,
                                        'main', False)
        self.assertEqual(got, 'merged')

        got2, _stray2 = ci_cancels.cause(run, {}, [run], {}, {7: '2026-01-01T01:01:00Z'}, None,
                                          'main', False)
        self.assertNotEqual(got2, 'merged')

    def test_a_push_run_a_pr_run_covers_is_dedupe(self):
        push = {'id': 1, 'head_branch': 'worker/T-1', 'head_sha': 'sha1', 'event': 'push',
                'created_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-01T00:05:00Z'}
        pr_run = {'id': 2, 'head_branch': 'worker/T-1', 'head_sha': 'sha1',
                  'event': 'pull_request', 'created_at': '2026-01-01T00:00:00Z',
                  'conclusion': 'success'}
        got, _stray = ci_cancels.cause(push, {}, [push, pr_run], {}, {}, None, 'main', False)
        self.assertEqual(got, 'dedupe')

        got2, _stray2 = ci_cancels.cause(push, {}, [push], {}, {}, None, 'main', False)
        self.assertNotEqual(got2, 'dedupe')

    def test_the_superseding_run_must_have_started_before_the_cancel(self):
        run = {'id': 1, 'head_branch': 'worker/T-1', 'head_sha': 'aaa', 'event': 'pull_request',
               'created_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-01T00:10:00Z'}
        too_late = {'id': 2, 'head_branch': 'worker/T-1', 'head_sha': 'bbb',
                    'created_at': '2026-01-01T00:11:00Z', 'conclusion': 'success'}
        got, _stray = ci_cancels.cause(run, {}, [run, too_late], {}, {}, None, 'main', False)
        self.assertEqual(got, 'unclaimed')

    def test_nothing_explains_it_is_unclaimed_not_a_guess(self):
        run = {'id': 1, 'head_branch': 'worker/T-1', 'head_sha': 'aaa', 'event': 'pull_request',
               'created_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-01T00:10:00Z'}
        got, stray = ci_cancels.cause(run, {}, [run], {}, {}, None, 'main', False)
        self.assertEqual(got, 'unclaimed')
        self.assertIsNone(stray)

    def test_a_superseded_claim_is_re_read_through_the_head_pair(self):
        """F-0230 PD2: `superseded` is `explain_cancels`' own weak reading and is never honoured
        as a claim — it is re-classified from the head pair like any unclaimed run, and comes
        back `rewrite` here because the superseding head is a content-free rebase."""
        repo = _init_repo()
        try:
            _git(['checkout', '-q', '-b', 'feature'], repo)
            _commit(repo, 'F1', 'a.txt', 'x')
            old = _commit(repo, 'F2', 'b.txt', 'y')
            _git(['checkout', '-q', 'main'], repo)
            _commit(repo, 'M1', 'trunk.txt', 'm')
            # cause() calls head_kind with trunk=f'origin/{trunk}' (its own trunk is a bare
            # branch name); this fixture has no remote, so give it the ref head_kind expects.
            _git(['update-ref', 'refs/remotes/origin/main', 'main'], repo)
            _git(['checkout', '-q', 'feature'], repo)
            _git(['rebase', '-q', 'main'], repo)
            new = _git(['rev-parse', 'HEAD'], repo)

            run = {'id': 1, 'head_branch': 'feature', 'head_sha': old, 'event': 'push',
                   'created_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-01T00:10:00Z'}
            superseder = {'id': 2, 'head_branch': 'feature', 'head_sha': new,
                          'created_at': '2026-01-01T00:05:00Z', 'conclusion': 'success'}
            claims = {'1': {'cause': 'superseded', 'by': 2}}
            got, stray = ci_cancels.cause(run, claims, [run, superseder], {}, {}, repo, 'main',
                                           False)
            self.assertEqual(got, 'rewrite')
            self.assertIsNone(stray)
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_a_job_timeout_claim_resolves_to_timeout_without_limit_arithmetic(self):
        """F-0230 PD2: `job-timeout` is the host's own annotation and is honoured as a claim — it
        resolves to `timeout` straight from `HONOURED`, and this run carries no `jobs` at all, so
        a wrong answer here could only come from skipping the claim."""
        run = {'id': 1, 'head_branch': 'b', 'head_sha': 'aaa', 'event': 'push',
               'created_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-01T00:10:00Z'}
        got, stray = ci_cancels.cause(run, {'1': {'cause': 'job-timeout'}}, [run], {}, {}, None,
                                       'main', False)
        self.assertEqual(got, 'timeout')
        self.assertIsNone(stray)


class HeadKind(unittest.TestCase):
    def setUp(self):
        self.repo = _init_repo()

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_a_rebase_onto_a_moved_trunk_is_a_rewrite(self):
        _git(['checkout', '-q', '-b', 'feature'], self.repo)
        _commit(self.repo, 'F1', 'a.txt', 'x')
        old = _commit(self.repo, 'F2', 'b.txt', 'y')
        _git(['checkout', '-q', 'main'], self.repo)
        _commit(self.repo, 'M1', 'trunk.txt', 'm')
        _git(['checkout', '-q', 'feature'], self.repo)
        _git(['rebase', '-q', 'main'], self.repo)
        new = _git(['rev-parse', 'HEAD'], self.repo)
        self.assertEqual(
            ci_cancels.head_kind(self.repo, old, new, trunk='main', fetch=False), 'rewrite')

    def test_a_reword_and_a_signoff_are_rewrites(self):
        _git(['checkout', '-q', '-b', 'feature'], self.repo)
        _commit(self.repo, 'F1', 'a.txt', 'x')
        old = _commit(self.repo, 'F2 original', 'b.txt', 'y')
        reworded = _amend(self.repo, 'F2 reworded')
        self.assertEqual(
            ci_cancels.head_kind(self.repo, old, reworded, trunk='main', fetch=False), 'rewrite')
        signed = _amend(self.repo, 'F2 original', extra=('-s',))
        self.assertEqual(
            ci_cancels.head_kind(self.repo, old, signed, trunk='main', fetch=False), 'rewrite')

    def test_a_new_commit_on_top_is_a_newhead(self):
        _git(['checkout', '-q', '-b', 'feature'], self.repo)
        _commit(self.repo, 'F1', 'a.txt', 'x')
        old = _commit(self.repo, 'F2', 'b.txt', 'y')
        new = _commit(self.repo, 'F3', 'c.txt', 'z')
        self.assertEqual(
            ci_cancels.head_kind(self.repo, old, new, trunk='main', fetch=False), 'newhead')

    def test_a_rebase_carrying_new_work_is_a_newhead(self):
        _git(['checkout', '-q', '-b', 'feature'], self.repo)
        _commit(self.repo, 'F1', 'a.txt', 'x')
        old = _commit(self.repo, 'F2', 'b.txt', 'y')
        _git(['checkout', '-q', 'main'], self.repo)
        _commit(self.repo, 'M1', 'trunk.txt', 'm')
        _git(['checkout', '-q', 'feature'], self.repo)
        _git(['rebase', '-q', 'main'], self.repo)
        new = _commit(self.repo, 'F3', 'c.txt', 'z')
        self.assertEqual(
            ci_cancels.head_kind(self.repo, old, new, trunk='main', fetch=False), 'newhead')

    def test_a_dropped_commit_is_a_newhead(self):
        root = _git(['rev-parse', 'HEAD'], self.repo)
        _git(['checkout', '-q', '-b', 'feature'], self.repo)
        f1 = _commit(self.repo, 'F1', 'a.txt', 'x')
        old = _commit(self.repo, 'F2', 'b.txt', 'y')
        _git(['checkout', '-q', '-b', 'onlyf1', root], self.repo)
        _git(['cherry-pick', f1], self.repo)
        new = _git(['rev-parse', 'HEAD'], self.repo)
        self.assertEqual(
            ci_cancels.head_kind(self.repo, old, new, trunk='main', fetch=False), 'newhead')

    def test_an_unknown_sha_is_unresolved_and_never_raises(self):
        self.assertEqual(
            ci_cancels.head_kind(self.repo, 'a' * 40, 'b' * 40, trunk='main', fetch=False),
            'unresolved')
        nongit = tempfile.mkdtemp()
        try:
            self.assertEqual(
                ci_cancels.head_kind(nongit, 'a' * 40, 'b' * 40, trunk='main', fetch=False),
                'unresolved')
        finally:
            shutil.rmtree(nongit, ignore_errors=True)


class Claims(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_claim_round_trips_and_the_newest_line_per_run_wins(self):
        ci_queue.claim_cancel(self.tmp, 7, 'relief')
        ci_queue.claim_cancel(self.tmp, 7, 'stall')
        got = ci_cancels.claims(self.tmp)
        self.assertEqual(got['7']['cause'], 'stall')

    def test_the_ledger_is_bounded_by_age_not_line_count(self):
        """Replaces the spec's line-count fence (F-0230 PD4): the landed ledger prunes by
        ci_queue.CLAIM_TTL_S, not by a line count, so this asserts age-based pruning instead and
        that this module mints no MAX_CLAIM_LINES of its own."""
        self.assertFalse(hasattr(ci_cancels, 'MAX_CLAIM_LINES'))
        t0 = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        ci_queue.claim_cancel(self.tmp, 1, 'relief', t0)
        inside = t0 + datetime.timedelta(seconds=ci_queue.CLAIM_TTL_S - 60)
        ci_queue.claim_cancel(self.tmp, 2, 'stall', inside)
        past = t0 + datetime.timedelta(seconds=ci_queue.CLAIM_TTL_S + 60)
        ci_queue.claim_cancel(self.tmp, 3, 'relief', past)
        # run 1 is older than CLAIM_TTL_S as of the write at `past` and is dropped by it; run 2
        # is still inside the window at that same write and survives.
        self.assertEqual(set(ci_cancels.claims(self.tmp)), {'2', '3'})

    def test_an_unwritable_state_dir_never_raises_and_reads_back_empty(self):
        path = os.path.join(self.tmp, 'not-a-dir')
        open(path, 'w', encoding='utf-8').close()
        ci_queue.claim_cancel(path, 1, 'relief')
        self.assertEqual(ci_cancels.claims(os.path.join(self.tmp, 'missing')), {})

    def test_a_corrupt_line_is_dropped_not_fatal(self):
        """Rewritten to the landed format (F-0230 PD1): the store is one JSON object, not JSONL,
        so the fence's subject is a corrupt file and a corrupt entry, not a corrupt line."""
        path = os.path.join(self.tmp, ci_queue.CANCELS_FILE)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('{oops')
        self.assertEqual(ci_cancels.claims(self.tmp), {})
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'1': {'cause': 'relief', 'at': '2026-01-01T00:00:00Z'}, '2': 'not-a-dict'},
                      f)
        self.assertEqual(set(ci_cancels.claims(self.tmp)), {'1'})


class Table(unittest.TestCase):
    def test_every_cause_is_a_row_even_at_zero(self):
        classification = {'rows': [{'run': 1, 'branch': 'b', 'sha': 'a', 'cause': 'rewrite',
                                     'group': 'wasted', 'minutes': 10},
                                    {'run': 2, 'branch': 'b', 'sha': 'a', 'cause': 'relief',
                                     'group': 'traded', 'minutes': 5}],
                           'strays': 0}
        table = ci_cancels.rows(classification)
        self.assertEqual(tuple(r['cause'] for r in table), ci_cancels.CAUSES)
        by_cause = {r['cause']: r for r in table}
        self.assertEqual(by_cause['rewrite']['runs'], 1)
        self.assertEqual(by_cause['relief']['runs'], 1)
        for c in ci_cancels.CAUSES:
            if c not in ('rewrite', 'relief'):
                self.assertEqual(by_cause[c]['runs'], 0)
                self.assertEqual(by_cause[c]['minutes'], 0)

    def test_the_share_is_of_minutes_and_the_footer_is_red_past_the_threshold(self):
        # rewrite carries WASTED_ALERT_PCT exactly (20 of 100 minutes); the rest is sound.
        classification = {'rows': [{'run': 1, 'branch': 'b', 'sha': 'a', 'cause': 'rewrite',
                                     'group': 'wasted', 'minutes': 20},
                                    {'run': 2, 'branch': 'b', 'sha': 'a', 'cause': 'newhead',
                                     'group': 'sound', 'minutes': 80}],
                           'strays': 0}
        table = ci_cancels.rows(classification)
        self.assertEqual(sum(r['share'] for r in table), 100)
        by_cause = {r['cause']: r for r in table}
        self.assertEqual(by_cause['rewrite']['share'], ci_cancels.WASTED_ALERT_PCT)
        rendered = ci_cancels.render('p', 'clause', table, classification['strays'])
        self.assertIn('RED', rendered.splitlines()[-1])
        self.assertIn('F-0203', rendered)
        self.assertIn('unknown 0 %', rendered)

        # one point below the threshold: OK, and the unknown share is still named.
        under = {'rows': [{'run': 1, 'branch': 'b', 'sha': 'a', 'cause': 'rewrite',
                            'group': 'wasted', 'minutes': 19},
                           {'run': 2, 'branch': 'b', 'sha': 'a', 'cause': 'unresolved',
                            'group': 'unknown', 'minutes': 81}],
                 'strays': 0}
        under_table = ci_cancels.rows(under)
        under_rendered = ci_cancels.render('p', 'clause', under_table, under['strays'])
        self.assertIn('OK', under_rendered.splitlines()[-1])
        self.assertIn('unknown 81 %', under_rendered)

    def test_the_title_line_is_the_registry_s(self):
        from asf.views import header
        table = ci_cancels.rows({'rows': [], 'strays': 0})
        rendered = ci_cancels.render('acme', '2 days, 0 of 0 runs cancelled, 0 runner-minutes '
                                              'thrown away', table, 0)
        self.assertEqual(rendered.splitlines()[0],
                          header.head('ci cancels', 'acme', '2 days, 0 of 0 runs cancelled, 0 '
                                                             'runner-minutes thrown away'))
