"""The merge queue's pre-cut check and duplicate-row blame (:mod:`asf.merge_queue`).

An append-only register merged by union (``merge=union``) merges clean in git: two rows booking
the same number pass the cut, and only the product's rules check sees them — once on a heavy
batch run, red. ``merge_queue.precut_check`` runs that check on the batch tree as each member merges,
before the push, and drops the member that turns it red, naming the PR. When such a red still
reaches CI, the failing log names the register and the rows, never a ``path:line``: blame maps the
rows to the member whose diff added them.
"""
import unittest

from asf import merge_queue

from tests.test_lane import sh
from tests.test_merge_queue import QueueRepo, job_run, check_run

REG = 'docs/reg.md'
#: a register check: the first column is a booking, booked once
CHECK = ("dups=$(awk '{print $1}' docs/reg.md | sort | uniq -d); "
         "if [ -n \"$dups\" ]; then echo \"reg-check: docs/reg.md: id $dups claimed twice\"; "
         "exit 1; fi; echo 'reg-check: ok'")


class RegisterRepo(QueueRepo):
    """A product with a union-merged register on main, booking 0100."""

    def setUp(self):
        super().setUp()
        self.setUpBacks()
        self.write(self.repo, '.gitattributes', f'{REG} merge=union\n')
        self.write(self.repo, REG, '0100 main-row\n')
        sh(['git', 'add', '-A'], cwd=self.repo)
        sh(['git', 'commit', '-qm', 'register'], cwd=self.repo, env_=self.ident)
        sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)

    def book(self, branch, row):
        self.push_lane(branch, {REG: f'0100 main-row\n{row}\n'}, f'book {row}')



class CutCheck(RegisterRepo):
    def product_checked(self, check=CHECK):
        return self.product(merge_queue={'ref_prefix': 'batch/', 'batch_size': 3,
                                         'precut_check': check})

    def test_a_member_whose_rows_main_already_booked_is_dropped_from_the_cut_by_name(self):
        self.book('worker/T-0001', '0200 one')
        self.book('worker/T-0002', '0100 stale')      # main booked 0100 already
        self.book('worker/T-0003', '0300 three')
        self.queue_pass(self.lane(self.product_checked()),
                        [self.entry('worker/T-0001', 1, 'T-0001', files=(REG,)),
                         self.entry('worker/T-0002', 2, 'T-0002', files=(REG,)),
                         self.entry('worker/T-0003', 3, 'T-0003', files=(REG,))])
        (batch,) = self.batches()
        self.assertEqual([m['branch'] for m in batch['members']],
                         ['worker/T-0001', 'worker/T-0003'])
        self.assertEqual([(b, k) for b, k, _t, _f in self.backs], [('worker/T-0002', 'gate')])
        text = self.backs[0][2]
        self.assertIn('PR #2', text)
        self.assertIn('id 0100 claimed twice', text)
        self.assertEqual(self.backs[0][3], [REG])
        self.assertTrue(any('PR #2' in l and 'dropped from the cut' in l for l in self.lines),
                        self.lines)
        # the pushed batch tree passes the check: no duplicate row reached the ref
        tree = sh(['git', 'show', f"{batch['sha']}:{REG}"], cwd=self.origin).stdout
        self.assertEqual(tree.count('0100'), 1, tree)

    def test_a_member_booking_what_an_earlier_member_booked_is_dropped_naming_it(self):
        self.book('worker/T-0001', '0200 one')
        self.book('worker/T-0002', '0200 two')
        self.queue_pass(self.lane(self.product_checked()),
                        [self.entry('worker/T-0001', 1, 'T-0001', files=(REG,)),
                         self.entry('worker/T-0002', 2, 'T-0002', files=(REG,))])
        (batch,) = self.batches()
        self.assertEqual([m['branch'] for m in batch['members']], ['worker/T-0001'])
        (back,) = self.backs
        self.assertEqual(back[0], 'worker/T-0002')
        self.assertIn('#1 merged', back[2])

    def test_a_base_already_red_on_the_check_blames_nobody(self):
        self.push_main({REG: '0100 main-row\n0100 dup-on-main\n'}, 'main red')
        self.book('worker/T-0001', '0200 one')
        self.queue_pass(self.lane(self.product_checked()),
                        [self.entry('worker/T-0001', 1, 'T-0001', files=(REG,))])
        (batch,) = self.batches()
        self.assertEqual([m['branch'] for m in batch['members']], ['worker/T-0001'])
        self.assertEqual(self.backs, [])
        self.assertTrue(any('itself' in l for l in self.lines), self.lines)

    def test_no_precut_check_set_cuts_as_before(self):
        self.book('worker/T-0001', '0100 stale')
        self.queue_pass(self.lane(), [self.entry('worker/T-0001', 1, 'T-0001', files=(REG,))])
        (batch,) = self.batches()
        self.assertEqual([m['branch'] for m in batch['members']], ['worker/T-0001'])
        self.assertEqual(self.backs, [])

    def test_settings_reads_one_command_or_a_list(self):
        conv = type('C', (), {'map_of': lambda self, k: {'precut_check': 'node x.mjs'}})()
        self.assertEqual(merge_queue.settings(conv)['precut_check'], ('node x.mjs',))
        conv = type('C', (), {'map_of': lambda self, k: {'precut_check': ['a', '', 'b']}})()
        self.assertEqual(merge_queue.settings(conv)['precut_check'], ('a', 'b'))


def register_log(*findings):
    stamp = '2026-10-05T06:37:57.1639137Z '
    lines = (['##[group]Run ./scripts/gate-fast.sh --all', '##[endgroup]']
             + [f'dup-ids: {f}' for f in findings]
             + [f'dup-ids: {len(findings)} duplicate booking(s) found',
                '##[error]Process completed with exit code 1.'])
    return '\n'.join(stamp + l for l in lines) + '\n'


class RegisterBlame(RegisterRepo):
    """A duplicate-id red reached CI (no pre-cut check set): the log names the file and the rows."""

    def red(self, sha, job, finding):
        self.gh.checks[sha] = [job_run('rules', job), check_run('gate', 'skipped'),
                               check_run('gate-tests', 'skipped')]
        self.gh.logs[job] = register_log(finding)
        self.gh.steps[job] = 'gate:fast (registers)'

    def test_the_member_whose_diff_added_the_named_rows_is_blamed(self):
        self.book('worker/T-0001', '| migration | 0200-0209 | plan-alpha | reserved |')
        self.book('worker/T-0002', '| migration | 0100-0109 | plan-bravo | reserved |')
        self.book('worker/T-0003', '| migration | 0300-0309 | plan-charlie | reserved |')
        self.queue_pass(self.lane(), [self.entry(f'worker/T-000{i}', i, f'T-000{i}', files=(REG,))
                                      for i in (1, 2, 3)])
        (batch,) = self.batches()
        finding = (f'{REG}: migration 0100–0109 (plan-bravo) overlaps migration 0100 '
                   f'(main-row) — the same id claimed twice')
        self.red(batch['sha'], '201', finding)
        self.queue_pass(self.lane(), [])            # flake triage re-runs it first
        self.red(batch['sha'], '202', finding)
        self.queue_pass(self.lane(), [])
        self.assertEqual([(b, k) for b, k, _t, _f in self.backs], [('worker/T-0002', 'gate')])
        self.assertEqual(self.backs[0][3], [REG])
        (again,) = self.batches()
        self.assertEqual([m['branch'] for m in again['members']],
                         ['worker/T-0001', 'worker/T-0003'])


if __name__ == '__main__':
    unittest.main()
