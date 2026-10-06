"""tests/test_evals.py — the set loads, or says exactly why (§3.1); the hash is the
instrument's identity (§3.2). Both classes build their own fixtures in a temporary directory,
because §3.2's "leaves the other levers' alone" needs a set of at least two, and the shipped
``evals/`` ships with none (PD5).

``evals/manifest.json``, ``evals/exemptions.json`` and ``evals/README.md`` are in the factory's
own amendable set (F-0024: no session edits it, a person lands it through ``asf propose``). The
tests that load the *shipped* set skip when it has not landed yet rather than failing on a hold
this module cannot resolve; see the T-0288 report's ``NEEDS OPERATOR`` line.
"""
import contextlib
import io
import json
import os
import re
import tempfile
import unittest

from asf import cli
from asf.evals import set as evals_set

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
SHIPPED_EVALS = os.path.join(PROJECT_ROOT, 'evals')
SHIPPED_MANIFEST = os.path.isfile(os.path.join(SHIPPED_EVALS, 'manifest.json'))

MATCHER_JUDGES = 'asf.record.match.match_event'


def write_json(path, obj, **dump_kwargs):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, **dump_kwargs)


def lever_entry(lever_id, adapter='matcher', judges=MATCHER_JUDGES,
                implements=('asf/record/match.py',), tasks=None):
    return {'id': lever_id, 'adapter': adapter, 'judges': judges,
            'implements': list(implements), 'tasks': tasks or lever_id}


def make_manifest(root, levers, min_tasks=2, require_both_polarities=True):
    write_json(os.path.join(root, 'manifest.json'), {
        'levers': levers, 'min_tasks': min_tasks,
        'require_both_polarities': require_both_polarities,
    })


def make_exemptions(root, exemptions=()):
    write_json(os.path.join(root, 'exemptions.json'), {'exemptions': list(exemptions)})


def fire_task(lever, name, want_ids=('F-0001',), branch='worker/f-0001-x'):
    return {
        'id': f'{lever}/{name}', 'lever': lever, 'expect': 'fire',
        'why': f'a branch naming F-0001 belongs to it',
        'given': {'items': {'F-0001': {'type': 'feature'}}, 'branch': branch},
        'want': {'ids': list(want_ids)},
    }


def no_fire_task(lever, name, branch='worker/unrelated-thing'):
    return {
        'id': f'{lever}/{name}', 'lever': lever, 'expect': 'no-fire',
        'why': 'an unrelated branch is not attributed to F-0001',
        'given': {'items': {'F-0001': {'type': 'feature'}}, 'branch': branch},
        'want': {'ids': ['F-0001']},
    }


def basic_two_lever_set(root):
    """``alpha`` and ``beta``, each with one must-fire and one must-not-fire task — the pair
    §3.2's "leaves the other levers' alone" needs."""
    make_manifest(root, [lever_entry('alpha'), lever_entry('beta')])
    make_exemptions(root)
    write_json(os.path.join(root, 'alpha', '001-fire.json'), fire_task('alpha', '001-fire'))
    write_json(os.path.join(root, 'alpha', '002-no-fire.json'), no_fire_task('alpha', '002-no-fire'))
    write_json(os.path.join(root, 'beta', '001-fire.json'), fire_task('beta', '001-fire'))
    write_json(os.path.join(root, 'beta', '002-no-fire.json'), no_fire_task('beta', '002-no-fire'))


class SetLoadTests(unittest.TestCase):

    @unittest.skipUnless(SHIPPED_MANIFEST, 'evals/manifest.json has not landed — it is in the'
                                           ' amendable set (F-0024); see NEEDS OPERATOR')
    def test_shipped_set_loads(self):
        eval_set = evals_set.load()
        self.assertIsInstance(eval_set, evals_set.Set)
        for lever in eval_set.levers:
            for task in lever.tasks:
                self.assertEqual(task.lever, lever.id)
                self.assertTrue(task.why)
                self.assertTrue(task.given)
                self.assertTrue(task.want)

    def test_a_well_formed_set_loads(self):
        with tempfile.TemporaryDirectory() as root:
            basic_two_lever_set(root)
            eval_set = evals_set.load(root)
        self.assertEqual({l.id for l in eval_set.levers}, {'alpha', 'beta'})
        for lever in eval_set.levers:
            self.assertEqual(len(lever.tasks), 2)
            self.assertEqual({t.expect for t in lever.tasks}, {'fire', 'no-fire'})
            for task in lever.tasks:
                self.assertEqual(task.lever, lever.id)

    def test_lever_field_disagreeing_with_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            basic_two_lever_set(root)
            write_json(os.path.join(root, 'alpha', '001-fire.json'),
                       fire_task('beta', '001-fire'))  # lever says beta, directory says alpha
            with self.assertRaises(evals_set.EvalError) as cm:
                evals_set.load(root)
        self.assertIn('001-fire.json', str(cm.exception))

    def test_unknown_adapter_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            make_manifest(root, [lever_entry('alpha', adapter='not-a-real-adapter')])
            make_exemptions(root)
            with self.assertRaises(evals_set.EvalError) as cm:
                evals_set.load(root)
        self.assertIn('manifest.json', str(cm.exception))

    def test_bad_expect_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            basic_two_lever_set(root)
            task = fire_task('alpha', '001-fire')
            task['expect'] = 'maybe'
            write_json(os.path.join(root, 'alpha', '001-fire.json'), task)
            with self.assertRaises(evals_set.EvalError) as cm:
                evals_set.load(root)
        self.assertIn('001-fire.json', str(cm.exception))

    def test_missing_why_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            basic_two_lever_set(root)
            task = fire_task('alpha', '001-fire')
            del task['why']
            write_json(os.path.join(root, 'alpha', '001-fire.json'), task)
            with self.assertRaises(evals_set.EvalError) as cm:
                evals_set.load(root)
        self.assertIn('why', str(cm.exception))

    def test_missing_given_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            basic_two_lever_set(root)
            task = fire_task('alpha', '001-fire')
            task['given'] = {}
            write_json(os.path.join(root, 'alpha', '001-fire.json'), task)
            with self.assertRaises(evals_set.EvalError) as cm:
                evals_set.load(root)
        self.assertIn('given', str(cm.exception))

    def test_missing_want_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            basic_two_lever_set(root)
            task = fire_task('alpha', '001-fire')
            task['want'] = {}
            write_json(os.path.join(root, 'alpha', '001-fire.json'), task)
            with self.assertRaises(evals_set.EvalError) as cm:
                evals_set.load(root)
        self.assertIn('want', str(cm.exception))

    def test_a_given_key_the_judged_function_does_not_take_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            basic_two_lever_set(root)
            task = fire_task('alpha', '001-fire')
            task['given']['not_a_match_event_kwarg'] = 'x'
            write_json(os.path.join(root, 'alpha', '001-fire.json'), task)
            with self.assertRaises(evals_set.EvalError) as cm:
                evals_set.load(root)
        self.assertIn('not_a_match_event_kwarg', str(cm.exception))

    def test_duplicate_task_id_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            basic_two_lever_set(root)
            write_json(os.path.join(root, 'alpha', '003-dup.json'),
                       fire_task('alpha', '001-fire'))  # same id as 001-fire.json
            with self.assertRaises(evals_set.EvalError) as cm:
                evals_set.load(root)
        self.assertIn('duplicate', str(cm.exception).lower())

    def test_a_stray_json_under_no_levers_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            basic_two_lever_set(root)
            write_json(os.path.join(root, 'not-a-lever', '001-x.json'), {'x': 1})
            with self.assertRaises(evals_set.EvalError) as cm:
                evals_set.load(root)
        self.assertIn('not-a-lever', str(cm.exception))

    def test_exemption_for_a_lever_that_has_a_pair_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            basic_two_lever_set(root)
            make_exemptions(root, [{'lever': 'alpha', 'reason': 'filed', 'card': 'B-0001'}])
            with self.assertRaises(evals_set.EvalError) as cm:
                evals_set.load(root)
        self.assertIn('alpha', str(cm.exception))

    def test_exemption_missing_reason_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            make_manifest(root, [lever_entry('alpha')])
            write_json(os.path.join(root, 'alpha', '001-fire.json'), fire_task('alpha', '001-fire'))
            make_exemptions(root, [{'lever': 'alpha', 'reason': '', 'card': 'B-0001'}])
            with self.assertRaises(evals_set.EvalError) as cm:
                evals_set.load(root)
        self.assertIn('reason', str(cm.exception))

    def test_exemption_missing_card_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            make_manifest(root, [lever_entry('alpha')])
            write_json(os.path.join(root, 'alpha', '001-fire.json'), fire_task('alpha', '001-fire'))
            make_exemptions(root, [{'lever': 'alpha', 'reason': 'no pair yet', 'card': ''}])
            with self.assertRaises(evals_set.EvalError) as cm:
                evals_set.load(root)
        self.assertIn('card', str(cm.exception))

    def test_a_lever_with_no_pair_and_a_recorded_exemption_loads(self):
        with tempfile.TemporaryDirectory() as root:
            make_manifest(root, [lever_entry('alpha')])
            write_json(os.path.join(root, 'alpha', '001-fire.json'), fire_task('alpha', '001-fire'))
            make_exemptions(root, [{'lever': 'alpha', 'reason': 'no pair yet', 'card': 'B-0001'}])
            eval_set = evals_set.load(root)
        lever = eval_set.levers[0]
        self.assertEqual(lever.exempt_reason, 'no pair yet')
        self.assertEqual(lever.exempt_card, 'B-0001')


class SetHashTests(unittest.TestCase):

    def test_hash_is_stable_across_two_loads(self):
        with tempfile.TemporaryDirectory() as root:
            basic_two_lever_set(root)
            first = evals_set.load(root)
            second = evals_set.load(root)
        self.assertEqual(first.set_hash, second.set_hash)
        first_by_id = {l.id: evals_set.lever_hash(l) for l in first.levers}
        second_by_id = {l.id: evals_set.lever_hash(l) for l in second.levers}
        self.assertEqual(first_by_id, second_by_id)

    def test_hash_is_stable_across_a_reformat_that_changes_no_value(self):
        with tempfile.TemporaryDirectory() as root:
            basic_two_lever_set(root)
            before = evals_set.load(root)
            task = fire_task('alpha', '001-fire')
            write_json(os.path.join(root, 'alpha', '001-fire.json'), task, indent=4, sort_keys=False)
            after = evals_set.load(root)
        self.assertEqual(before.set_hash, after.set_hash)

    def test_changing_a_tasks_want_changes_its_lever_and_the_set_hash(self):
        with tempfile.TemporaryDirectory() as root:
            basic_two_lever_set(root)
            before = evals_set.load(root)
            task = fire_task('alpha', '001-fire', want_ids=('F-0002',))
            write_json(os.path.join(root, 'alpha', '001-fire.json'), task)
            after = evals_set.load(root)
        before_by_id = {l.id: evals_set.lever_hash(l) for l in before.levers}
        after_by_id = {l.id: evals_set.lever_hash(l) for l in after.levers}
        self.assertNotEqual(before_by_id['alpha'], after_by_id['alpha'])
        self.assertEqual(before_by_id['beta'], after_by_id['beta'])
        self.assertNotEqual(before.set_hash, after.set_hash)

    def test_adding_a_task_changes_its_lever_and_the_set_hash_and_leaves_the_other_alone(self):
        with tempfile.TemporaryDirectory() as root:
            basic_two_lever_set(root)
            before = evals_set.load(root)
            write_json(os.path.join(root, 'alpha', '003-fire.json'),
                       fire_task('alpha', '003-fire', want_ids=('F-0099',)))
            after = evals_set.load(root)
        before_by_id = {l.id: evals_set.lever_hash(l) for l in before.levers}
        after_by_id = {l.id: evals_set.lever_hash(l) for l in after.levers}
        self.assertNotEqual(before_by_id['alpha'], after_by_id['alpha'])
        self.assertEqual(before_by_id['beta'], after_by_id['beta'])
        self.assertNotEqual(before.set_hash, after.set_hash)

    def test_deleting_a_task_changes_its_lever_and_the_set_hash(self):
        with tempfile.TemporaryDirectory() as root:
            basic_two_lever_set(root)
            # 'alpha' needs to keep its pair after the delete, so add a third task first.
            write_json(os.path.join(root, 'alpha', '003-fire.json'),
                       fire_task('alpha', '003-fire', want_ids=('F-0099',)))
            before = evals_set.load(root)
            os.remove(os.path.join(root, 'alpha', '003-fire.json'))
            after = evals_set.load(root)
        before_by_id = {l.id: evals_set.lever_hash(l) for l in before.levers}
        after_by_id = {l.id: evals_set.lever_hash(l) for l in after.levers}
        self.assertNotEqual(before_by_id['alpha'], after_by_id['alpha'])
        self.assertEqual(before_by_id['beta'], after_by_id['beta'])
        self.assertNotEqual(before.set_hash, after.set_hash)

    def test_hash_is_twelve_hex_characters(self):
        with tempfile.TemporaryDirectory() as root:
            basic_two_lever_set(root)
            eval_set = evals_set.load(root)
        self.assertRegex(eval_set.set_hash, r'^[0-9a-f]{12}$')
        for lever in eval_set.levers:
            self.assertRegex(evals_set.lever_hash(lever), r'^[0-9a-f]{12}$')

    def test_hash_is_recorded_in_no_file(self):
        with tempfile.TemporaryDirectory() as root:
            basic_two_lever_set(root)
            eval_set = evals_set.load(root)
        needle = eval_set.set_hash
        for tree in (SHIPPED_EVALS, os.path.join(PROJECT_ROOT, 'asf')):
            if not os.path.isdir(tree):
                continue
            for dirpath, _dirnames, filenames in os.walk(tree):
                for name in filenames:
                    path = os.path.join(dirpath, name)
                    try:
                        with open(path, encoding='utf-8') as f:
                            contents = f.read()
                    except (UnicodeDecodeError, OSError):
                        continue
                    self.assertNotIn(needle, contents, path)


class CoverageTests(unittest.TestCase):
    """§3.5: every lever this repo's manifest declares has a pair or a recorded exemption, or
    ``asf evals check`` names it and refuses. Every case runs through ``$ASF_EVALS_DIR`` the way
    ``cli.main(['evals', 'check'])`` itself resolves the set, which is what makes this the same
    assertion as the fence's own ``asf evals check`` line."""

    def setUp(self):
        self._orig_evals_dir = os.environ.get('ASF_EVALS_DIR')

    def tearDown(self):
        if self._orig_evals_dir is None:
            os.environ.pop('ASF_EVALS_DIR', None)
        else:
            os.environ['ASF_EVALS_DIR'] = self._orig_evals_dir

    def _main(self, root, argv):
        os.environ['ASF_EVALS_DIR'] = root
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(argv)
        return rc, out.getvalue()

    @unittest.skipUnless(SHIPPED_MANIFEST, 'evals/manifest.json has not landed — it is in the'
                                           ' amendable set (F-0024); see NEEDS OPERATOR')
    def test_shipped_rosters_levers_each_have_a_pair_or_a_recorded_exemption(self):
        eval_set = evals_set.load()
        for lever in eval_set.levers:
            expects = {t.expect for t in lever.tasks}
            has_pair = 'fire' in expects and 'no-fire' in expects
            self.assertTrue(has_pair or lever.exempt_reason,
                            f'{lever.id} has neither a pair nor a recorded exemption')

    def test_a_lever_with_only_one_polarity_is_a_coverage_failure_not_a_pass(self):
        with tempfile.TemporaryDirectory() as root:
            make_manifest(root, [lever_entry('alpha')])
            make_exemptions(root)
            write_json(os.path.join(root, 'alpha', '001-fire.json'), fire_task('alpha', '001-fire'))
            write_json(os.path.join(root, 'alpha', '002-fire-too.json'),
                       fire_task('alpha', '002-fire-too', want_ids=('F-0002',)))
            rc, out = self._main(root, ['evals', 'check'])
        self.assertEqual(rc, 1, out)
        self.assertIn('alpha', out)

    def test_an_undeclared_unexempted_lever_makes_check_exit_1_naming_it(self):
        with tempfile.TemporaryDirectory() as root:
            make_manifest(root, [lever_entry('alpha')])
            make_exemptions(root)
            rc, out = self._main(root, ['evals', 'check'])
        self.assertEqual(rc, 1, out)
        self.assertIn('alpha', out)

    def test_the_same_lever_with_an_entry_exits_0_and_is_reported_exempt_and_counted(self):
        with tempfile.TemporaryDirectory() as root:
            make_manifest(root, [lever_entry('alpha')])
            write_json(os.path.join(root, 'alpha', '001-fire.json'), fire_task('alpha', '001-fire'))
            make_exemptions(root, [{'lever': 'alpha', 'reason': 'no pair yet', 'card': 'B-0001'}])
            rc_check, out_check = self._main(root, ['evals', 'check'])
            rc_run, out_run = self._main(root, ['evals', 'run'])
        self.assertEqual(rc_check, 0, out_check)
        self.assertEqual(rc_run, 0, out_run)
        self.assertIn('exempt', out_run)
        self.assertIn('1 lever exempt', out_run)


if __name__ == '__main__':
    unittest.main()
