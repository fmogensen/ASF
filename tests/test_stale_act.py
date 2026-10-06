"""asf.stale_act — stale means act: a lane-STALE PR is archived and closed, a stuck Task far
behind the trunk is re-planned, and a removed or closed item renders no row in ``asf next``.
Hermetic: a fake forge, a record folder and a lane registry in a temp ASF_HOME."""
import datetime as dt
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import env, forge as forge_mod, stale_act
from asf.env import Product
from asf.feeder import rows

try:
    from test_stale import make_home, make_repo, run as run_cli, write_item, iso
except ImportError:  # pragma: no cover - import shape only
    from tests.test_stale import make_home, make_repo, run as run_cli, write_item, iso

NOW = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.timezone.utc)


class FakeForge(forge_mod.Forge):
    name = 'fake'
    has_prs = True
    has_runs = True

    def __init__(self, prs=None, archive_ok=True):
        super().__init__(repo=None, trunk='main')
        self.prs, self.archive_ok, self.calls = dict(prs or {}), archive_ok, []

    def open_prs(self):
        return self.prs

    def archive(self, name, tip, message):
        self.calls.append(('archive', name, tip))
        return (True, 'f00d' * 10) if self.archive_ok else (False, 'refused')

    def close_pr(self, number, reason):
        self.calls.append(('close', number, reason))
        return True, ''

    def delete_branch(self, branch, head):
        self.calls.append(('delete', branch, head))
        return True, ''


def product(stale=None, slug='acme/sample'):
    conv = {'branch_prefixes': {'code': 'task/'}}
    if stale is not None:
        conv['stale'] = stale
    return Product('sample', {'repo_slug': slug, 'main': 'main', 'conventions': conv})


class Fixture(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='stale_act_home_')
        self.patch = mock.patch.object(env, 'ASF_HOME', self.home)
        self.patch.start()
        self.root = make_repo()
        self.state = os.path.join(self.home, 'state', 'sample')
        os.makedirs(self.state)
        old = iso(NOW - dt.timedelta(days=12))
        write_item(self.root, 'E-0001', 'epic', 'Factory', typed_lines=['decided: true'],
                   machine_lines=['state: Active', f'stage_since: {old}'])
        write_item(self.root, 'F-0001', 'feature', 'Floor', parent='E-0001',
                   typed_lines=['decided: true'],
                   machine_lines=['state: Active', 'stage: building 0/2', f'stage_since: {old}'])
        write_item(self.root, 'T-0001', 'task', 'Wire', parent='F-0001',
                   typed_lines=['decided: true', 'writes: [a.py]'],
                   machine_lines=['state: Active', f'stage_since: {old}'])

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.root, ignore_errors=True)

    def lane(self, item='T-0001', branch='task/T-0001', state='STALE', pr=36, days=5,
             head='abc123'):
        at = iso(NOW - dt.timedelta(days=days))
        lines = [
            {'job': f'task-{item.lower()}', 'item': item, 'kind': 'task', 'branch': branch,
             'started': at, 'ended': at, 'end_reason': 'finished'},
            {'job': f'task-{item.lower()}', 'branch': branch,
             'lane': {'state': state, 'head': head, 'pr': pr, 'at': at, 'head_at': at,
                      'reason': 'unmoved in PUSHED past lane.stale_after', 'item': item}}]
        with open(os.path.join(self.state, 'sessions.jsonl'), 'a') as f:
            for line in lines:
                f.write(json.dumps(line) + '\n')

    def ledger(self):
        path = os.path.join(self.state, stale_act.LEDGER)
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]

    def card(self, rel):
        with open(os.path.join(self.root, rel)) as f:
            return f.read()


class StalePrIsArchivedClosedAndReplanned(Fixture):
    """Acceptance 1: an open PR the lane marks STALE past its deadline is archived to
    ``archive/pr-<n>``, closed with a one-line reason, and its open item gets a replan."""

    def test_archive_close_delete_and_replan(self):
        self.lane(days=5)
        fake = FakeForge({36: {'branch': 'task/T-0001', 'head': 'abc123'}})
        done = stale_act.run(product({'act': True, 'task': False}), self.root, forge=fake,
                             now=NOW, behind=lambda b: 0)
        self.assertEqual(fake.calls[0], ('archive', 'pr-36', 'abc123'))
        close = [c for c in fake.calls if c[0] == 'close']
        self.assertEqual(len(close), 1)
        self.assertEqual(close[0][1], 36)
        self.assertIn('archive/pr-36', close[0][2])
        self.assertNotIn('\n', close[0][2])
        self.assertIn(('delete', 'task/T-0001', 'abc123'), fake.calls)
        # the replan is the Feature's reshape decision, written on its card
        self.assertIn('reshape:', self.card('features/F-0001.md'))
        [rec] = self.ledger()
        self.assertEqual((rec['kind'], rec['pr'], rec['item']), ('pr', 36, 'T-0001'))
        self.assertTrue(any('replan of F-0001 queued' in d for d in rec['did']))
        self.assertEqual(len(done), 1)

    def test_younger_than_its_deadline_is_left(self):
        self.lane(days=1)
        fake = FakeForge({36: {'branch': 'task/T-0001', 'head': 'abc123'}})
        self.assertEqual(stale_act.run(product({'act': True}), self.root, forge=fake, now=NOW,
                                       behind=lambda b: 0), [])
        self.assertEqual(fake.calls, [])

    def test_a_closed_pr_is_not_acted_on_again(self):
        self.lane(days=5)
        fake = FakeForge({})
        self.assertEqual(stale_act.run(product({'act': True}), self.root, forge=fake, now=NOW,
                                       behind=lambda b: 0), [])

    def test_a_closed_item_is_closed_but_not_replanned(self):
        write_item(self.root, 'T-0001', 'task', 'Wire', parent='F-0001',
                   typed_lines=['decided: true', 'writes: [a.py]'],
                   machine_lines=['state: Closed', f'stage_since: {iso(NOW)}'])
        self.lane(days=5)
        fake = FakeForge({36: {'branch': 'task/T-0001', 'head': 'abc123'}})
        stale_act.run(product({'act': True}), self.root, forge=fake, now=NOW, behind=lambda b: 0)
        self.assertTrue(any(c[0] == 'close' for c in fake.calls))
        self.assertNotIn('reshape:', self.card('features/F-0001.md'))

    def test_a_refused_archive_closes_nothing(self):
        self.lane(days=5)
        fake = FakeForge({36: {'branch': 'task/T-0001', 'head': 'abc123'}}, archive_ok=False)
        stale_act.run(product({'act': True}), self.root, forge=fake, now=NOW, behind=lambda b: 0)
        self.assertEqual([c[0] for c in fake.calls], ['archive'])
        self.assertIn('held', self.ledger()[0]['did'][0])

    def test_a_live_session_or_park_holds_it(self):
        self.lane(days=5)
        fake = FakeForge({36: {'branch': 'task/T-0001', 'head': 'abc123'}})
        stale_act.run(product({'act': True}), self.root, forge=fake, now=NOW,
                      behind=lambda b: 0, live={'T-0001'})
        stale_act.run(product({'act': True}), self.root, forge=fake, now=NOW,
                      behind=lambda b: 0, parked={'T-0001'})
        self.assertEqual(fake.calls, [])


class DryRunAndDefaults(Fixture):
    def test_dry_run_changes_nothing_and_says_what_it_would(self):
        self.lane(days=5)
        fake = FakeForge({36: {'branch': 'task/T-0001', 'head': 'abc123'}})
        lines = []
        done = stale_act.run(product({'act': True}), self.root, forge=fake, now=NOW,
                             behind=lambda b: 0, dry_run=True, out=lines.append)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.ledger(), [])
        self.assertTrue(any('would archive' in ln and 'archive/pr-36' in ln for ln in lines))
        self.assertEqual(done[0]['did'], ['dry run'])

    def test_another_product_is_a_dry_run_until_it_opts_in(self):
        self.lane(days=5)
        fake = FakeForge({36: {'branch': 'task/T-0001', 'head': 'abc123'}})
        stale_act.run(product(), self.root, forge=fake, now=NOW, behind=lambda b: 0,
                      out=lambda _l: None)
        self.assertEqual(fake.calls, [])
        stale_act.run(product(), self.root, forge=fake, now=NOW, behind=lambda b: 0,
                      act_now=True, out=lambda _l: None)  # asf stale --act
        self.assertTrue(fake.calls)

    def test_asf_own_repo_acts_by_default(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(stale_act.__file__)))
        own = Product('asf', {'repo_dir': here})
        self.assertTrue(stale_act.acts(own))
        self.assertFalse(stale_act.acts(product()))
        self.assertFalse(stale_act.acts(Product('asf', {'repo_dir': here,
                                                        'conventions': {'stale': {'act': False}}})))

    def test_every_kind_can_be_disabled(self):
        self.lane(days=5)
        fake = FakeForge({36: {'branch': 'task/T-0001', 'head': 'abc123'}})
        self.assertEqual(stale_act.run(product({'act': True, 'pr': False, 'task': False}),
                                       self.root, forge=fake, now=NOW,
                                       behind=lambda b: 10 ** 6), [])
        self.assertEqual(fake.calls, [])

    def test_small_repo_defaults(self):
        cfg = stale_act.settings(product())
        self.assertEqual((cfg['pr_after'], cfg['task_factor'], cfg['pr'], cfg['task']),
                         ('3d', 3, True, True))
        bad = stale_act.settings(product({'pr_after': 'soon', 'task_behind': -1, 'pr': 'yes'}))
        self.assertEqual((bad['pr_after'], bad['task_behind'], bad['pr']),
                         ('3d', stale_act.DEFAULTS['task_behind'], True))

    def test_a_plain_git_product_skips_prs_as_not_applicable(self):
        git = forge_mod.for_product(product(slug=''))
        self.assertIsInstance(git, forge_mod.GitForge)
        self.assertFalse(git.has_prs)
        self.assertIsInstance(forge_mod.for_product(product()), forge_mod.GitHubForge)
        self.lane(days=5)
        lines = []
        stale_act.plan(product({'task': False}, slug=''), self.root, forge=git, now=NOW,
                       out=lines.append, live=set(), parked=set())
        self.assertTrue(any('not applicable' in ln for ln in lines))

    def test_github_forge_archives_over_the_tip_then_deletes_under_a_lease(self):
        from asf import github
        refs, calls = {'task/T-0001': 'abc123'}, []

        def gh(args, **_kw):
            calls.append(args)
            text = ' '.join(args)
            if '/git/ref/heads/' in text:
                sha = refs.get(text.split('/git/ref/heads/')[1].split()[0], '')
                return github.Result(bool(sha), sha, 0 if sha else 1, sha)
            if text.endswith('.parents[].sha'):
                return github.Result(True, 'abc123\n', 0, 'abc123\n')
            if text.endswith('.tree.sha'):
                return github.Result(True, 'tree1', 0, 'tree1')
            if '/git/commits' in text and '-X POST' in text:
                return github.Result(True, 'arch01', 0, 'arch01')
            if '/git/refs' in text and '-X POST' in text:
                refs['archive/pr-36'] = 'arch01'
                return github.Result(True, '', 0, '')
            if '-X DELETE' in text:
                refs.pop('task/T-0001', None)
                return github.Result(True, '', 0, '')
            return github.Result(False, None, 1, '', '', '', 'unexpected')
        f = forge_mod.GitHubForge(slug='acme/sample', trunk='main')
        with mock.patch.object(github, 'gh', gh):
            self.assertEqual(f.archive('pr-36', 'abc123', 'm'), (True, 'arch01'))
            self.assertEqual(f.archive('pr-36', 'abc123', 'm'), (True, 'arch01'))  # made before
            self.assertEqual(f.delete_branch('task/T-0001', 'moved9'),
                             (False, 'tip moved (abc123 is not moved9) — kept'))
            self.assertTrue(f.delete_branch('task/T-0001', 'abc123')[0])
        self.assertIn('parents[]=abc123', [a for c in calls for a in c])

    def test_the_forge_refuses_the_trunk(self):
        f = forge_mod.GitHubForge(slug='acme/sample', trunk='main')
        ok, why = f.delete_branch('main', 'abc')
        self.assertFalse(ok)
        self.assertIn('REF GUARD', why)


class StaleActCommand(unittest.TestCase):
    def test_asf_stale_act_dry_run(self):
        root, home = make_repo(), make_home()
        try:
            run_cli(['index'], root, home)
            p = run_cli(['stale', '--act', '--dry-run'], root, home)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn('stale: nothing past its deadline', p.stdout)
            p = run_cli(['stale', '--dry-run'], root, home)
            self.assertEqual(p.returncode, 2)
        finally:
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(home, ignore_errors=True)


class StuckTaskIsReplanned(Fixture):
    """Acceptance 2: a Task in one stage past 3x its limit, its branch far behind the trunk, is
    re-planned from the current trunk instead of corrected."""

    def test_far_behind_and_stuck_is_replanned(self):
        self.lane(state='BACK', pr=40, days=10, head='beef01')
        fake = FakeForge({40: {'branch': 'task/T-0001', 'head': 'beef01'}})
        done = stale_act.run(product({'act': True, 'pr': False}), self.root, forge=fake,
                             now=NOW, behind=lambda b: 1100)
        self.assertEqual(fake.calls[0], ('archive', 'task/T-0001', 'beef01'))
        self.assertIn(('close', 40), [c[:2] for c in fake.calls])
        self.assertIn(('delete', 'task/T-0001', 'beef01'), fake.calls)
        self.assertIn('reshape:', self.card('features/F-0001.md'))
        self.assertEqual(done[0]['kind'], 'task')
        self.assertIn('1100 commits behind', done[0]['why'])

    def test_a_document_branch_is_never_taken_for_the_tasks_code(self):
        self.lane(branch='plan/T-0001', state='STALE', pr=None, days=10)
        seen = []
        prod = Product('sample', {'repo_slug': 'acme/sample', 'conventions': {
            'branch_prefixes': {'code': 'task/', 'plan': 'plan/'}, 'stale': {'pr': False}}})
        stale_act.plan(prod, self.root, forge=FakeForge({}), now=NOW, live=set(), parked=set(),
                       behind=lambda b: seen.append(b))
        self.assertEqual(seen, ['task/T-0001'])

    def test_close_to_the_trunk_is_left(self):
        self.lane(state='BACK', pr=40, days=10)
        fake = FakeForge({})
        self.assertEqual(stale_act.run(product({'act': True, 'pr': False}), self.root,
                                       forge=fake, now=NOW, behind=lambda b: 5), [])

    def test_under_three_times_its_limit_is_left(self):
        recent = iso(NOW - dt.timedelta(minutes=90))  # task_active 45m: 2x, not 3x
        write_item(self.root, 'T-0001', 'task', 'Wire', parent='F-0001',
                   typed_lines=['decided: true', 'writes: [a.py]'],
                   machine_lines=['state: Active', f'stage_since: {recent}'])
        self.assertEqual(stale_act.run(product({'act': True, 'pr': False}), self.root,
                                       forge=FakeForge({}), now=NOW, behind=lambda b: 5000), [])

    def test_a_branch_that_moved_lately_is_left(self):
        self.lane(state='REVIEW', pr=40, days=0)
        self.assertEqual(stale_act.run(product({'act': True, 'pr': False}), self.root,
                                       forge=FakeForge({}), now=NOW, behind=lambda b: 5000), [])

    def test_a_merge_in_progress_is_left(self):
        self.lane(state='QUEUED', pr=40, days=10)
        self.assertEqual(stale_act.run(product({'act': True, 'pr': False}), self.root,
                                       forge=FakeForge({}), now=NOW, behind=lambda b: 5000), [])

    def test_one_replan_per_feature_a_pass(self):
        write_item(self.root, 'T-0002', 'task', 'More', parent='F-0001',
                   typed_lines=['decided: true', 'writes: [b.py]'],
                   machine_lines=['state: Active', f'stage_since: {iso(NOW - dt.timedelta(days=9))}'])
        self.lane(state='BACK', pr=40, days=10)
        self.lane(item='T-0002', branch='task/T-0002', state='BACK', pr=41, days=10)
        writes = []

        def write(root, target, updates, stamp, note, product=None):
            writes.append(target)
        prod = product({'act': True, 'pr': False})
        items = stale_act.record_items(self.root)
        actions = stale_act.plan(prod, self.root, forge=FakeForge({}), now=NOW, items=items,
                                 behind=lambda b: 999, live=set(), parked=set())
        self.assertEqual(sorted(a['item'] for a in actions), ['T-0001', 'T-0002'])
        stale_act.act(prod, self.root, actions, forge=FakeForge({}), items=items, now=NOW,
                      write=write, out=lambda _l: None)
        self.assertEqual(writes, ['F-0001'])

    def test_a_task_with_no_feature_is_reshaped_itself(self):
        items = {'T-0009': {'id': 'T-0009', 'type': 'task', 'state': 'Active'}}
        self.assertEqual(stale_act.replan_target(items, 'T-0009'), ('T-0009', ''))
        self.assertEqual(stale_act.replan_target({'B-0001': {'type': 'bug', 'state': 'New'}},
                                                 'B-0001')[0], None)


class GoneItemsRenderNoRows(unittest.TestCase):
    """Acceptance 3: a removed or closed item never renders a row in ``asf next`` — a branch
    park on a removed Task still showed PARKED (a product's T-0335)."""

    def index(self):
        return {'items': {
            'F-0001': {'id': 'F-0001', 'type': 'feature', 'stage': 'building 0/3', 'decided': True,
                       'state': 'Active', 'children': ['T-0001', 'T-0002', 'T-0003']},
            'T-0001': {'id': 'T-0001', 'type': 'task', 'parent': 'F-0001', 'rank': 1,
                       'decided': True, 'state': 'New', 'writes': ['a.py'],
                       'removed': 'dropped by the replan'},
            'T-0002': {'id': 'T-0002', 'type': 'task', 'parent': 'F-0001', 'rank': 2,
                       'decided': True, 'state': 'Closed', 'writes': ['b.py']},
            'T-0003': {'id': 'T-0003', 'type': 'task', 'parent': 'F-0001', 'rank': 3,
                       'decided': True, 'state': 'New', 'writes': ['c.py']}}}

    def test_parks_and_corrections_on_gone_items_render_nothing(self):
        occ = {'parks': [
            {'item': 'T-0001', 'scope': 'branch', 'branch': 'task/T-0001', 'reason': 'looping'},
            {'item': 'T-0002', 'scope': 'job', 'on_job': 'task-t-0002', 'reason': 'wait'}],
            'corrections': {'T-0001': {'kind': 'review', 'text': 'fix it', 'parked': True,
                                       'reason': 'wrote nothing twice'}}}
        got = rows.candidates(self.index(), product(), [], occupancy=occ)
        ids = {r.item_id for r in got}
        self.assertNotIn('T-0001', ids)
        self.assertNotIn('T-0002', ids)
        self.assertIn('T-0003', ids)

    def test_drop_gone_keeps_cards_the_record_does_not_hold(self):
        items = rows.items_of(self.index())
        keep = rows.Row(tier=2, kind=rows.PUSHED_LAND, item_id='PR-77', feature_id='',
                        action='WAITS ON landing', brief_kind='review', branch='x', reason='')
        gone = rows.Row(tier=2, kind=rows.FIX_CORRECT, item_id='T-0001', feature_id='F-0001',
                        action='PARKED x', brief_kind='correct', branch='y', reason='')
        closed = rows.Row(tier=2, kind=rows.FIX_CORRECT, item_id='T-0002', feature_id='F-0001',
                          action='PARKED x', brief_kind='correct', branch='y', reason='')
        self.assertEqual(rows.drop_gone([keep, gone, closed], items), [keep])


if __name__ == '__main__':
    unittest.main()
