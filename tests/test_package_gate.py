"""tools/package_gate.py — the R-line table is read, and every gap it leaves is named (§11)."""
import importlib.util
import os
import shutil
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location('package_gate',
                                               os.path.join(ROOT, 'tools', 'package_gate.py'))
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def table(rows):
    lines = ['| R-line | status | tests | why |', '| --- | --- | --- | --- |']
    lines += [f'| {r} | {s} | {t} | {w} |' for r, s, t, w in rows]
    return '\n'.join(lines) + '\n'


class PackageGateTable(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='gate_')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def read(self, rows):
        path = os.path.join(self.tmp, 't.md')
        with open(path, 'w') as f:
            f.write(table(rows))
        return gate.read_table(path)

    def full(self, **over):
        rows = {r: (r, 'n/a', '', 'a process step') for r in gate.R_LINES}
        rows.update(over)
        return list(rows.values())

    def test_the_checked_in_table_names_every_r_line_once(self):
        rows, problems = gate.read_table()
        self.assertEqual(problems, [])
        self.assertEqual(gate.table_gaps(rows), [])
        self.assertEqual(sorted(rows, key=lambda r: int(r[1:])), list(gate.R_LINES))

    def test_a_test_id_is_dotted(self):
        self.assertEqual(gate.dotted('tests/test_x.py::Case::test_y'), 'tests.test_x.Case.test_y')
        self.assertEqual(gate.dotted('tests.test_x.Case.test_y'), 'tests.test_x.Case.test_y')

    def test_a_missing_line_a_bare_na_and_a_testless_line_are_gaps(self):
        rows, problems = self.read(self.full(R2=('R2', 'n/a', '', ''), R3=('R3', 'test', '', 'x'),
                                             R4=('R4', 'maybe', '', 'x'))[1:])
        self.assertEqual(problems, ["R4: status 'maybe' is not one of test, pending, check, "
                                    "pending-live, n/a"])
        self.assertEqual(gate.table_gaps(rows), ['R1: not in the table',
                                                 'R2: n/a needs a reason and no test',
                                                 'R3: test names no test', 'R4: not in the table'])

    def test_a_mapped_test_must_pass_in_this_run(self):
        t1, t2 = 'tests/test_a.py::A::test_one', 'tests/test_a.py::A::test_two'
        rows, _p = self.read(self.full(R9=('R9', 'test', f'`{t1}`, `{t2}`', 'W4'),
                                       R1=('R1', 'pending', f'`{t1}`', 'lane')))
        outcomes = {'tests.test_a.A.test_one': gate.PASSED,
                    'tests.test_a.A.test_two': 'expected failure'}
        gaps, notes = gate.line_gaps(rows, outcomes)
        self.assertEqual(gaps, ['R9: tests.test_a.A.test_two expected failure'])
        self.assertEqual(notes, ['R1: pending, but every mapped test passes — mark it test'])
        gaps, _n = gate.line_gaps(rows, {})
        self.assertEqual(gaps[0], 'R1: pending (lane) — tests.test_a.A.test_one did not run')


def fake_git(ancestors=(), chain='', trunk=''):
    """A ``git(args) -> (rc, stdout)`` for the history checks: ``ancestors`` are the shas
    ``merge-base --is-ancestor`` accepts; ``chain``/``trunk`` the two rev-lists."""
    def git(args):
        if args[:2] == ['merge-base', '--is-ancestor']:
            return (0 if args[2] in ancestors else 1), ''
        if args[:2] == ['rev-list', '--first-parent']:
            return 0, chain
        if args[:1] == ['rev-list']:
            return 0, trunk
        return 1, ''
    return git


class PackageGateChecksAndLive(PackageGateTable):
    def smoke(self, text):
        path = os.path.join(self.tmp, 'smoke.txt')
        with open(path, 'w') as f:
            f.write(text)
        return path

    def test_r21_pending_live_fails_until_a_recorded_pass_names_a_sha_of_this_branch(self):
        rows, _p = self.read(self.full(R21=('R21', 'pending-live', '', 'the live smoke')))
        self.assertEqual(gate.table_gaps(rows), [])
        git = fake_git(ancestors=('abc123',))
        missing = os.path.join(self.tmp, 'none.txt')
        self.assertIn('no recorded live smoke', gate.live_gaps(rows, missing, git)[0])
        failed = self.smoke('date: d\naccount: a\nsha: abc123\nresult: FAIL\n')
        self.assertIn('result: FAIL', gate.live_gaps(rows, failed, git)[0])
        partial = self.smoke('date: d\nsha: abc123\nresult: PASS\n')
        self.assertIn('lacks account', gate.live_gaps(rows, partial, git)[0])
        foreign = self.smoke('date: d\naccount: a\nsha: fff999\nresult: PASS\n')
        self.assertIn('does not contain', gate.live_gaps(rows, foreign, git)[0])
        good = self.smoke('# c\ndate: d\naccount: a\nsha: abc123\nresult: PASS\n')
        self.assertEqual(gate.live_gaps(rows, good, git), [])

    def test_a_pending_live_line_names_no_test_and_gives_a_reason(self):
        rows, _p = self.read(self.full(R21=('R21', 'pending-live', '`tests/t.py::A::b`', 'x')))
        self.assertEqual(gate.table_gaps(rows), ['R21: pending-live needs a reason and no test'])

    def test_r15_r18_history_checks(self):
        rows, _p = self.read(self.full(R15=('R15', 'check', '`ancestor:e284ee7`', 'x'),
                                       R18=('R18', 'check', '`merged-not-rebased:74497d9`', 'x'),
                                       R19=('R19', 'check', '`vibes:1`', 'x')))
        self.assertEqual(gate.table_gaps(rows), ["R19: unknown check 'vibes:1'"])
        del rows['R19']
        merged = fake_git(ancestors=('e284ee7',), chain='s1\ns2\nm1\n', trunk='t1\nt2\n')
        self.assertEqual(gate.check_gaps(rows, merged), [])
        rebased = fake_git(chain='s1\nt2\nt1\n', trunk='t1\nt2\n')
        gaps = gate.check_gaps(rows, rebased)
        self.assertEqual([g.split(' — ')[0] for g in gaps],
                         ['R15: ancestor:e284ee7', 'R18: merged-not-rebased:74497d9'])
        self.assertIn('2 commit(s) of origin/main on the first-parent chain', gaps[1])

    def test_the_checked_in_r21_is_pending_live_and_r15_r18_hold_here(self):
        rows, _p = gate.read_table()
        self.assertEqual(rows['R21']['status'], 'pending-live')
        self.assertEqual({rows[r]['status'] for r in ('R15', 'R18')}, {'check'})


if __name__ == '__main__':
    unittest.main()
