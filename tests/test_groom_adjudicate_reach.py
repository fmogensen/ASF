"""tests.test_groom_adjudicate_reach — a groom day's open questions reach an adjudicate session on
the next plain tick, whoever wrote the file and whatever else the wave has to launch; a question
the operator owns never does; and ``asf groom`` / ``asf set`` publish what they write to the
record's origin, where the tick's clone reads it."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from asf import approvals, cli, env
from asf.feeder import rows as feeder_rows
from asf.record.ids import write_new_item
from asf.tick import step_wave
from tests.test_tick import _git
from tests.test_tick_steps import StepsTestCase

DATE = '2026-09-22'


def _items():
    """Three ranked Features with a Task each (three PLAN → CODE launches) and one unranked
    card from the inbox — the oldest open question, as on the first live product."""
    items = {}
    for n in (1, 2, 3):
        fid, tid = f'F-000{n}', f'T-000{n}'
        items[fid] = {'id': fid, 'type': 'feature', 'title': f'f{n}', 'folder': 'features',
                      'rank': n, 'stage': 'building 0/1', 'state': 'Active', 'decided': True,
                      'children': [tid]}
        items[tid] = {'id': tid, 'type': 'task', 'title': f't{n}', 'folder': 'tasks',
                      'parent': fid, 'rank': 1, 'state': 'New', 'writes': [f'f{n}.py'],
                      'decided': True}
    items['F-1111'] = {'id': 'F-1111', 'type': 'feature', 'title': 'from the inbox',
                       'folder': 'features', 'state': 'New', 'stage': 'card',
                       'decided': False, 'stage_since': '2026-09-22T00:00:00Z'}
    items['E-0001'] = {'id': 'E-0001', 'type': 'epic', 'title': 'an epic', 'folder': 'epics',
                       'state': 'New', 'decided': False}
    return items


GROOM = ('# Groom 2026-09-22\n\n'
         '- [ ] F-1111 from the inbox — from inbox, awaiting a decision → answer: ____\n'
         '- [ ] E-0001 an epic — undecided 3d → answer: ____\n')


class GroomOnOriginReachesThePlainTick(StepsTestCase):
    product_extra = 'steps:\n  batch: off\napprovals:\n  groom: auto\n'

    def setUp(self):
        super().setUp()
        seed = os.path.join(self.tmp, 'groom-seed')
        _git(['clone', '-q', self.origin, seed], self.tmp)
        _git(['config', 'user.email', 'o@example.com'], seed)
        _git(['config', 'user.name', 'o'], seed)
        with open(os.path.join(seed, 'index.json'), 'w') as f:
            json.dump({'generated': '', 'items': _items()}, f)
        os.makedirs(os.path.join(seed, 'groom'), exist_ok=True)
        with open(os.path.join(seed, 'groom', f'{DATE}.md'), 'w') as f:
            f.write(GROOM)
        _git(['add', '-A'], seed)
        _git(['commit', '-q', '-m', 'groom by hand'], seed)
        _git(['push', '-q', 'origin', 'HEAD:main'], seed)
        self.write_config('feeder:\n  capacity: 1\n')
        self.waved = []

        def build(product, row, index, inflight, repo_facts=None):
            return types.SimpleNamespace(kind=row.brief_kind, item_id=row.item_id,
                                         text='brief\n', model='Opus', add_dirs=[],
                                         id_ranges_needed=[])

        def wave(product, rows, n, brief_fn=None, out=print):
            self.waved += [r.job for r in rows]
            return [], []
        for name, fn in (('_build', build), ('_wave', wave)):
            p = mock.patch.object(step_wave, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def test_the_wave_launches_the_adjudicator_before_the_ranked_feature_work(self):
        # one free slot, three ranked Tasks ready: the day's one adjudicate session still goes
        # first — its oldest card is unranked, and ranking the row by it put it behind every
        # launch, where the cut never reached it
        step_wave.run(self.ctx(), out=self.lines.append)
        self.assertEqual(self.waved, [f'groom-{DATE}'])

    def test_a_new_epic_question_is_not_put_to_the_adjudicator(self):
        state = step_wave.groom_state(self.product, self.ctx().record_root())
        self.assertEqual(state['open'], ['F-1111'])
        self.assertTrue(all('E-0001' not in line for line in state['lines']))

    def test_a_card_the_hook_refused_goes_to_the_adjudicator(self):
        approvals.refuse(self.product, 'F-1111', 'spend_money', 'human-now', 'task-t-0001',
                         'Bash', 'raise a paid tier')
        state = step_wave.groom_state(self.product, self.ctx().record_root())
        self.assertEqual(state['open'], ['F-1111'])

    def test_a_card_on_an_open_harvest_hold_stays_with_the_operator(self):
        approvals.refuse(self.product, 'F-1111', 'merge_amendable_set', 'human-now',
                         'task-t-0001', 'harvest', 'rules/r1.md')
        state = step_wave.groom_state(self.product, self.ctx().record_root())
        self.assertEqual(state['open'], [])
        self.assertEqual([r for r in feeder_rows.candidates(
            {'items': _items()}, self.product, [], groom_state=state)
            if r.kind == feeder_rows.GROOM_ADJUDICATE], [])

    def test_a_granted_hold_gives_the_question_back(self):
        hold = approvals.refuse(self.product, 'F-1111', 'merge_amendable_set', 'human-now', 'j',
                                'harvest', 'x')
        approvals.resolve(self.product, hold, 'granted')
        state = step_wave.groom_state(self.product, self.ctx().record_root())
        self.assertEqual(state['open'], ['F-1111'])


def git(cwd, *a):
    return subprocess.run(['git', *a], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


class ConsoleWritersPublish(unittest.TestCase):
    """``asf groom`` (with or without ``--apply``) and ``asf set`` commit what they wrote and push
    it; what the operator had already changed by hand is left out of the commit."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='groompub_')
        self.origin = os.path.join(self.tmp, 'origin.git')
        self.root = os.path.join(self.tmp, 'record')
        git(self.tmp, 'init', '-q', '--bare', '-b', 'main', self.origin)
        git(self.tmp, 'clone', '-q', self.origin, self.root)
        for k, v in (('user.name', 'T'), ('user.email', 't@x')):
            git(self.root, 'config', k, v)
        write_new_item(self.root, {}, 'feature', 'F-0001', {'title': 'A feature'}, '',
                       '2026-01-01', 'seed')
        with open(os.path.join(self.root, 'notes.txt'), 'w') as f:
            f.write('seed\n')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-qm', 'seed')
        git(self.root, 'push', '-q', 'origin', 'HEAD:main')
        self.cwd = os.getcwd()
        os.chdir(self.root)
        env_patch = mock.patch.dict(os.environ, {'ASF_PRODUCT': ''})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def asf(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cli.main(list(argv))

    def origin_files(self):
        return git(self.origin, 'ls-tree', '-r', '--name-only', 'main').splitlines()

    def test_asf_groom_commits_and_pushes_its_groom_file(self):
        with open(os.path.join(self.root, 'notes.txt'), 'w') as f:
            f.write('a hand edit, not the groom\'s\n')
        before = int(git(self.origin, 'rev-list', '--count', 'main'))
        self.asf('groom', '--date', DATE)
        self.assertEqual(int(git(self.origin, 'rev-list', '--count', 'main')), before + 1)
        self.assertIn(f'groom/{DATE}.md', self.origin_files())
        self.assertTrue(git(self.origin, 'log', '-1', '--format=%s', 'main').startswith(
            f'groom: {DATE}'))
        # the operator's own edit is not swept into the groom's commit
        self.assertEqual(git(self.origin, 'show', 'main:notes.txt'), 'seed')
        self.assertIn('notes.txt', git(self.root, 'status', '--porcelain'))

    def test_asf_groom_apply_applies_todays_answers_and_pushes_the_card(self):
        self.asf('groom', '--date', DATE)
        path = os.path.join(self.root, 'groom', f'{DATE}.md')
        with open(path, encoding='utf-8') as f:
            text = f.read()
        line = next((ln for ln in text.splitlines() if ln.startswith('- [ ] F-0001')), None)
        if line is None:  # the fixture's card asks nothing today: write the operator's answer
            line = '- [ ] F-0001 A feature — undecided 3d → answer: ____'
            text += '\n' + line + '\n'
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text.replace(line, line.replace('____', 'yes')))
        self.asf('groom', '--date', DATE, '--apply')
        card = git(self.origin, 'show', 'main:features/F-0001.md')
        self.assertIn('decided: true', card)
        self.assertEqual(git(self.root, 'status', '--porcelain', '--', 'features'), '')

    def test_asf_set_commits_and_pushes_the_card(self):
        self.assertEqual(self.asf('set', 'F-0001', 'priority=P1'), 0)
        self.assertIn('priority: P1', git(self.origin, 'show', 'main:features/F-0001.md'))
        self.assertEqual(git(self.origin, 'log', '-1', '--format=%s', 'main'),
                         'record: set F-0001')


if __name__ == '__main__':
    unittest.main()
