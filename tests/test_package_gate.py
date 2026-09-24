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
        self.assertEqual(problems, ["R4: status 'maybe' is not one of test, pending, n/a"])
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


if __name__ == '__main__':
    unittest.main()
