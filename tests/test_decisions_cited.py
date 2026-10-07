"""A plan cites its product's decisions as bare ``D<n>`` (``per D7``); the record writes them as
``D-0007``. :func:`asf.record.decisions.normalise` rewrites a bare ``D<n>`` the register holds
into the record's form on every card the plan-tasks and replan passes mint, and ``asf check``
accepts a bare ``D<n>`` that resolves to the product's own ``docs/decisions`` — it was flagging
every such citation as "a link gone bare" once a record D-card shared the number."""
import os
import shutil
import tempfile
import unittest

from asf.record import check, decisions, plan_tasks
from tests.test_backlog import make_repo, write_item
from tests.test_plan_tasks import read


class Normalise(unittest.TestCase):

    def test_a_bare_id_the_register_holds_is_written_in_the_records_form(self):
        known = {'D-0007', 'D-0012'}
        self.assertEqual(decisions.normalise('per D7 and D12; D99 stays', known),
                         'per D-0007 and D-0012; D99 stays')

    def test_code_spans_prefixed_ids_and_full_ids_are_left_alone(self):
        known = {'D-0007'}
        text = 'quoted `D7`, PF-D7, D-0007\n```\nD7\n```\n'
        self.assertEqual(decisions.normalise(text, known), text)

    def test_nothing_known_changes_nothing(self):
        self.assertEqual(decisions.normalise('per D7', ()), 'per D7')
        self.assertEqual(decisions.normalise('', {'D-0007'}), '')


class PlanTasksNormalise(unittest.TestCase):

    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'The reader', parent='E-0001',
                   typed_lines=('decided: true',))
        write_item(self.root, 'D-0007', 'decision', 'Read through the parser')

    def test_the_minted_task_cites_the_decision_in_the_records_form(self):
        plan = ('# Plan F-0001\n\n### Task 1: the reader, per D7\nwrites: a.py\n\n'
                'Built as D7 says; D99 is the product\'s own.\n')
        ev = {'features': {'f-0001': {'plan': 'origin/main:docs/plans/f-0001.md',
                                      'plan_on_main': True, 'spec_on_main': True}}}
        made = plan_tasks.mint_plan_tasks(self.root, None, ev, out=lambda *_: None,
                                          read_ref=lambda ref: plan)
        self.assertEqual(made, ['T-0001'])
        meta, body = read(self.root, 'task', 'T-0001')
        self.assertEqual(meta['title'], 'the reader, per D-0007')
        self.assertIn('Built as D-0007 says; D99 is', body)


class CheckAcceptsTheProductsRegister(unittest.TestCase):

    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        write_item(self.root, 'D-0007', 'decision', 'A record decision')
        path = os.path.join(self.root, 'epics', 'E-0001.md')
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        with open(path, encoding='utf-8') as f:
            text = f.read()
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text.replace('## Description\n', '## Description\nBuilt per D7.\n'))

    def bare(self, docs=()):
        findings, _w, _i = check.record_findings(self.root, layout=False, docs_decisions=docs)
        return [m for _p, _l, m in findings if 'bare decision' in m]

    def test_a_bare_id_the_record_holds_is_still_flagged(self):
        self.assertEqual(len(self.bare()), 1)

    def test_a_bare_id_the_products_docs_carry_is_a_citation_of_that_register(self):
        self.assertEqual(self.bare(docs={'D-0007'}), [])

    def test_the_docs_register_is_read_from_the_product_checkout(self):
        repo = tempfile.mkdtemp(prefix='decisions_repo_')
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        os.makedirs(os.path.join(repo, 'docs', 'decisions'))
        with open(os.path.join(repo, 'docs', 'decisions', '0007-parser.md'), 'w') as f:
            f.write('# parser\n')
        self.assertEqual(decisions.docs_ids(repo), {'D-0007'})


if __name__ == '__main__':
    unittest.main()
