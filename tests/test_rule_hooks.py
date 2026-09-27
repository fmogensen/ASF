"""F-0061: the core rules' hooks — the contract every hook shares (§2.4).

Hermetic: a tmpdir ``ASF_HOME`` and a fixture product; no network, no real settings file.
"""
import io
import json
import os
import shutil
import tempfile
import unittest

from asf import approvals, env, hooks
from asf.rules import hookcall

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ContractTests(unittest.TestCase):
    """T4 — ``hookcall.run``: ``ASF_JOB`` unset returns 0 and writes nothing whatever ``decide``
    would have said; None returns 0 and writes nothing; lines return 2, print them and append
    exactly one ``refused-rule`` row with no ``hold`` key; a raising ``decide`` returns 2, prints
    ``HOOK_ERROR_LINE`` naming the rule and appends one ``rule-hook-error`` row — and the ledger
    is valid JSONL after all of them."""

    RULE = 'R-0001'

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        os.makedirs(os.path.join(env.ASF_HOME, 'products'))
        os.makedirs(os.path.join(env.ASF_HOME, 'logs'))
        self.repo = os.path.join(self.tmp, 'repo')
        os.makedirs(self.repo)
        with open(env.product_path('demo'), 'w') as f:
            f.write(f'product: demo\nmain: trunk\nrepo_dir: {self.repo}\n')
        self.calls = []

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def environ(self, job='coder-t-0001'):
        e = {'ASF_PRODUCT': 'demo', 'ASF_ITEM': 'T-0001'}
        if job:
            e['ASF_JOB'] = job
        return e

    def run_hook(self, decide, job='coder-t-0001', command='git add -A'):
        call = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': command},
                           'cwd': self.repo, 'hook_event_name': 'PreToolUse'})
        out = io.StringIO()
        rc = hookcall.run(self.RULE, decide, stdin_text=call, environ=self.environ(job),
                          out=out)
        return rc, out.getvalue()

    def ledger_lines(self):
        path = approvals.ledger_path('demo')
        if not os.path.exists(path):
            return []
        with open(path, encoding='utf-8') as f:
            return f.read().splitlines()

    def refuse(self, call, ctx):
        self.calls.append((call, ctx))
        return hookcall.refusal('git add -A', 'a stage names its files', self.RULE,
                                'a blanket stage commits whatever else is in the tree.',
                                'instead: git add <the paths you changed>')

    def test_no_job_returns_0_and_writes_nothing_whatever_decide_says(self):
        for decide in (self.refuse, lambda call, ctx: 1 / 0):
            with self.subTest(decide=decide):
                self.assertEqual(self.run_hook(decide, job=None), (0, ''))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.ledger_lines(), [])

    def test_none_returns_0_silent_and_writes_nothing(self):
        self.assertEqual(self.run_hook(lambda call, ctx: None), (0, ''))
        self.assertEqual(self.ledger_lines(), [])

    def test_lines_return_2_print_them_and_append_one_refused_rule_row(self):
        rc, out = self.run_hook(self.refuse)
        self.assertEqual(rc, 2)
        self.assertEqual(out.splitlines(), self.refuse({}, None))
        rows = [json.loads(l) for l in self.ledger_lines()]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual({k: row[k] for k in ('event', 'rule', 'item', 'job', 'tool', 'detail')},
                         {'event': 'refused-rule', 'rule': self.RULE, 'item': 'T-0001',
                          'job': 'coder-t-0001', 'tool': 'Bash', 'detail': 'git add -A'})
        self.assertNotIn('hold', row)
        self.assertTrue(row['ts'])
        self.assertEqual(approvals.open_holds(env.load_product('demo')), [])

    def test_a_raising_decide_returns_2_names_the_rule_and_appends_a_rule_hook_error(self):
        def broken(call, ctx):
            raise RuntimeError('boom')
        rc, out = self.run_hook(broken)
        self.assertEqual(rc, 2)
        self.assertEqual(out.strip(), hookcall.HOOK_ERROR_LINE.format(rule=self.RULE, why='boom'))
        rows = [json.loads(l) for l in self.ledger_lines()]
        self.assertEqual([(r['event'], r['rule'], r['job']) for r in rows],
                         [('rule-hook-error', self.RULE, 'coder-t-0001')])
        self.assertNotIn('hook-error', {r['event'] for r in rows})   # not the approvals guard

    def test_the_ledger_is_valid_jsonl_after_all_four(self):
        self.run_hook(self.refuse, job=None)
        self.run_hook(lambda call, ctx: None)
        self.run_hook(self.refuse)
        self.run_hook(lambda call, ctx: 1 / 0)
        rows = [json.loads(l) for l in self.ledger_lines()]
        self.assertEqual([r['event'] for r in rows], ['refused-rule', 'rule-hook-error'])

    def test_the_context_carries_what_every_rule_needs(self):
        rc, _ = self.run_hook(self.refuse)
        self.assertEqual(rc, 2)
        _call, ctx = self.calls[0]
        self.assertEqual((ctx.product.name, ctx.job, ctx.item, ctx.cwd),
                         ('demo', 'coder-t-0001', 'T-0001', self.repo))
        self.assertEqual((ctx.in_worktree, ctx.in_repo, ctx.in_backlog), (False, True, False))

    def test_refusal_renders_the_four_parts_and_the_needs_operator_line(self):
        lines = self.refuse({}, None)
        self.assertEqual(lines[0], 'REFUSED git add -A — a stage names its files (R-0001)')
        self.assertTrue(lines[2].startswith('  instead: '))
        self.assertEqual(lines[-1], '  This is not a question for a person: do not print '
                                    'NEEDS OPERATOR for it.')

    def test_the_readme_declares_no_hook(self):
        # rules/README.md is prose: declared_hooks still returns [] with it in place (PD2)
        self.assertTrue(os.path.isfile(os.path.join(REPO_ROOT, 'rules', 'README.md')))
        self.assertEqual(hooks.declared_hooks(os.path.join(REPO_ROOT, 'rules')), [])


if __name__ == '__main__':
    unittest.main()
