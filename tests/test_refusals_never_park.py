"""tests.test_refusals_never_park — a refusal from the approvals hook never parks an item.

The hook still refuses exactly what it refused before, and records the hold for the audit
trail; but its message tells the session plainly that the action is not its to do and what to
do instead (never ``NEEDS OPERATOR``), the wave relaunches the item as normal, the relaunch's
brief names what was refused, and an item refused for one class on two relaunches in a row
becomes a question for the groom's adjudicator — not for the operator.
"""
import io
import json
import os
import shutil
import tempfile
import unittest

from asf import approvals, env
from asf.groom import groom as groom_mod
from asf.groom import policy as groom_policy
from asf.tick import step_wave

JOB = 'task-t-0118'
ITEM = 'T-0118'


class _Home(unittest.TestCase):
    """A temp ASF home with product ``demo`` and a session ledger the test writes runs into."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        os.makedirs(os.path.join(env.ASF_HOME, 'products'))
        os.makedirs(os.path.join(env.ASF_HOME, 'state', 'demo'))
        with open(env.product_path('demo'), 'w') as f:
            f.write('product: demo\nmain: main\n')
        self.repo = os.path.join(self.tmp, 'repo')
        os.makedirs(self.repo)
        self.n = 0

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def product(self):
        return env.load_product('demo')

    def run_(self, item=ITEM, job=JOB, refused=(), ended=True):
        """One launched run of ``item`` under ``job``, and each class in ``refused`` refused
        during it. Runs are a minute apart, oldest first."""
        self.n += 1
        started = f'2026-09-25T10:{self.n:02d}:00Z'
        with open(os.path.join(env.ASF_HOME, 'state', 'demo', 'sessions.jsonl'), 'a') as f:
            f.write(json.dumps({'job': job, 'item': item, 'branch': f'task/{item}',
                                'started': started, 'pid': 1}) + '\n')
            if ended:
                f.write(json.dumps({'job': job, 'ended': f'2026-09-25T10:{self.n:02d}:50Z'})
                        + '\n')
        for cls in refused:
            approvals.append('demo', {
                'event': 'refused', 'hold': f'{item}/{cls}', 'item': item, 'class': cls,
                'level': 'human-now', 'job': job, 'tool': 'Bash',
                'detail': 'git push origin HEAD:main', 'ts': f'2026-09-25T10:{self.n:02d}:30Z'})

    def hook(self, tool_name, tool_input, job=JOB):
        with open(os.path.join(env.ASF_HOME, 'state', 'demo', 'sessions.jsonl'), 'a') as f:
            f.write(json.dumps({'job': job, 'item': ITEM, 'branch': 'task/T-0118',
                                'started': '2026-09-25T09:00:00Z', 'pid': 1}) + '\n')
        call = json.dumps({'tool_name': tool_name, 'tool_input': tool_input, 'cwd': self.repo})
        out = io.StringIO()
        rc = approvals.run_hook(call, {'ASF_JOB': job, 'ASF_PRODUCT': 'demo'}, out=out)
        return rc, out.getvalue()


class RefusalMessageTest(_Home):
    def test_a_trunk_push_is_refused_with_what_to_do_instead(self):
        rc, out = self.hook('Bash', {'command': 'git push origin HEAD:main'})
        self.assertEqual(rc, 2, out)
        self.assertIn(f'REFUSED touch_production (human-now) on {ITEM}', out)
        self.assertIn("pushing to the trunk and deploying are the harvest's job", out)
        self.assertIn('push your own branch', out)
        self.assertNotIn('NEEDS OPERATOR:', out)
        self.assertNotIn('asf approvals resolve', out)
        self.assertEqual([h['hold'] for h in approvals.open_holds('demo')],
                         [f'{ITEM}/touch_production'])   # recorded for the audit trail

    def test_a_git_hook_edit_is_refused_as_not_the_sessions(self):
        rc, out = self.hook('Bash', {'command': 'git config core.hooksPath /dev/null'})
        self.assertEqual(rc, 2, out)
        self.assertIn("the repo's git hooks are not yours to edit", out)
        self.assertNotIn('NEEDS OPERATOR:', out)

    def test_every_hook_class_says_why_and_what_instead(self):
        for c in approvals.CLASSES:
            if 'hook' in c.read_by and c.name != 'touch_amendable_set':
                with self.subTest(cls=c.name):
                    self.assertTrue(c.why and c.instead, c.name)
                    self.assertFalse(c.parks, c.name)


class NoParkTest(_Home):
    def test_a_hook_hold_parks_nothing(self):
        for cls in ('touch_production', 'touch_security', 'spend_money', 'touch_legal',
                    'touch_customer_data', 'new_epic', 'file_bug'):
            approvals.refuse('demo', ITEM, cls, 'human-now', JOB, 'Bash', 'x')
        self.assertEqual(approvals.parked('demo'), {})

    def test_a_harvest_merge_hold_still_parks(self):
        approvals.refuse('demo', 'B-0001', 'merge_amendable_set', 'human-now', 'fix-b-0001',
                         'harvest', 'rules/r.md')
        self.assertEqual(approvals.parked('demo'),
                         {'B-0001': ('merge_amendable_set', 'human-now')})

    def test_a_hook_hold_does_not_keep_the_card_from_the_adjudicator(self):
        product = self.product()
        approvals.refuse(product, 'F-1111', 'spend_money', 'human-now', 'j', 'Bash', 'x')
        self.assertNotIn('F-1111', step_wave._operator_owned(product, {}))
        approvals.refuse(product, 'F-2222', 'merge_amendable_set', 'human-now', 'j', 'harvest',
                         'rules/r.md')
        self.assertIn('F-2222', step_wave._operator_owned(product, {}))


class RelaunchBriefTest(_Home):
    def test_the_relaunch_brief_names_the_last_runs_refusal(self):
        self.run_(refused=('touch_production',))
        text = approvals.refusal_text('demo', ITEM)
        self.assertIn('REFUSED LAST RUN', text)
        self.assertIn('touch_production', text)
        self.assertIn('git push origin HEAD:main', text)
        self.assertIn('push your own branch', text)
        self.assertNotIn('NEEDS OPERATOR:', text)

    def test_a_clean_last_run_says_nothing(self):
        self.run_(refused=('touch_production',))
        self.run_()
        self.assertEqual(approvals.refusal_text('demo', ITEM), '')
        self.assertEqual(approvals.refusal_text('demo', 'T-9999'), '')

    def test_the_brief_builder_appends_it(self):
        import importlib
        build_mod = importlib.import_module('asf.briefs.build')
        self.run_(refused=('touch_security',))
        self.assertIn('REFUSED LAST RUN', build_mod.refusal_section(self.product(), ITEM))
        self.assertEqual(build_mod.refusal_section(None, ITEM), '')


class EscalationTest(_Home):
    def test_two_refused_relaunches_in_a_row_go_to_the_adjudicator(self):
        self.run_(refused=('touch_production',))
        self.run_(refused=('touch_production',))       # relaunch 1, refused again
        self.assertEqual(approvals.escalations('demo'), {})
        self.run_(refused=('touch_production',))       # relaunch 2, refused again
        self.assertEqual(approvals.escalations('demo'), {ITEM: ('touch_production', 3)})

    def test_a_clean_run_breaks_the_streak(self):
        self.run_(refused=('touch_production',))
        self.run_(refused=('touch_production',))
        self.run_()
        self.run_(refused=('touch_production',))
        self.assertEqual(approvals.escalations('demo'), {})

    def test_a_different_class_is_its_own_streak(self):
        self.run_(refused=('touch_production',))
        self.run_(refused=('touch_security',))
        self.run_(refused=('touch_production',))
        self.assertEqual(approvals.escalations('demo'), {})

    def canonical(self, **meta):
        m = {'id': ITEM, 'type': 'task', 'title': 'push the fix', 'state': 'Active', **meta}
        return {ITEM: {'meta': m, 'body': '', 'path': '', 'relpath': ''}}

    def test_the_groom_asks_the_adjudicator_not_the_operator(self):
        for _ in range(3):
            self.run_(refused=('touch_production',))
        lines = groom_mod.groom_refused_section(self.canonical(), self.product())
        self.assertEqual(len(lines), 1, lines)
        line = lines[0]
        self.assertTrue(line.startswith(f'- [ ] {ITEM} push the fix — '), line)
        self.assertIn(groom_policy.REFUSED_MARK, line)
        self.assertIn('touch_production', line)
        self.assertIn('reshape:', line)
        self.assertIn('not a question for the operator', line)
        self.assertTrue(line.endswith('→ answer: ____'), line)
        self.assertEqual(groom_policy.open_questions(line), [(ITEM, line)])

    def test_money_may_go_to_the_operator(self):
        for _ in range(3):
            self.run_(refused=('spend_money',))
        line = groom_mod.groom_refused_section(self.canonical(), self.product())[0]
        self.assertIn('NEEDS OPERATOR', line)

    def test_a_closed_or_reshaped_card_is_not_asked(self):
        for _ in range(3):
            self.run_(refused=('touch_production',))
        self.assertEqual(groom_mod.groom_refused_section(
            self.canonical(reshape='split it'), self.product()), [])
        self.assertEqual(groom_mod.groom_refused_section(
            self.canonical(state='Closed'), self.product()), [])

    def test_the_question_is_not_spoken_for_by_the_relaunch(self):
        for _ in range(3):
            self.run_(refused=('touch_production',))
        line = groom_mod.groom_refused_section(self.canonical(), self.product())[0]
        inflight = [{'item': ITEM, 'kind': 'task', 'account': 'w', 'age': '1m'}]
        out, n = groom_policy.suppress({'refused': [line]}, {'items': {}}, inflight,
                                       self.product())
        self.assertEqual((out['refused'], n), ([line], 0))

    def test_reshape_is_an_answer(self):
        self.assertEqual(groom_mod._parse_answer('reshape: push a branch, not main — refused'),
                         ('reshape', 'push a branch, not main'))


if __name__ == '__main__':
    unittest.main()
