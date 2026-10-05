"""A groom ``no:`` on a Task that leads a delivery (``delivers: [<itself>, …]``).

Removing the lead left ``delivers:`` naming a removed item — itself — and the record's commit
hook refused the write. With no live member besides itself, the delivery goes with the card;
with live members, the answer is refused with the reason.
"""
import os
import shutil
import unittest

from asf.record import frontmatter

from tests.test_groom import make_repo, run, write_item


class NoOnADeliveryLead(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        write_item(self.root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])
        write_item(self.root, 'F-0001', 'feature', 'Some idea', parent='E-0009',
                   typed_lines=['decided: true'])

    def answer(self, line):
        run(['index'], self.root)
        with open(os.path.join(self.root, 'groom', '2026-09-20.md'), 'w', encoding='utf-8') as f:
            f.write(f"# Groom 2026-09-20\n\n## Undecided > 3 days\n\n{line}\n")
        return run(['groom', '--date', '2026-09-21', '--apply'], self.root)

    def card(self, iid):
        with open(os.path.join(self.root, 'tasks', f'{iid}.md'), encoding='utf-8') as f:
            return frontmatter.parse(f.read(), path=f'tasks/{iid}.md')

    def test_a_lead_delivering_only_itself_is_removed_with_its_delivery(self):
        write_item(self.root, 'T-0001', 'task', 'Lead', parent='F-0001',
                   typed_lines=['decided: true', 'delivers: [T-0001]'])
        r = self.answer('- [ ] T-0001 Lead — stale → answer: no: superseded')
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        meta, body = self.card('T-0001')
        self.assertIn('superseded', str(meta.get('removed')))
        self.assertNotIn('delivers', meta)
        self.assertIn('delivers:, goes with it', body)
        run(['index'], self.root)
        check = run(['check'], self.root)
        self.assertNotIn('delivers references removed item', check.stdout + check.stderr)

    def test_a_lead_with_a_live_member_is_refused_with_the_reason(self):
        write_item(self.root, 'T-0001', 'task', 'Lead', parent='F-0001',
                   typed_lines=['decided: true', 'delivers: [T-0001, T-0002]'])
        write_item(self.root, 'T-0002', 'task', 'Member', parent='F-0001',
                   typed_lines=['decided: true', 'delivered_by: T-0001'])
        r = self.answer('- [ ] T-0001 Lead — stale → answer: no')
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        meta, _body = self.card('T-0001')
        self.assertFalse(meta.get('removed'))
        self.assertEqual(meta.get('delivers'), ['T-0001', 'T-0002'])
        self.assertIn('leads the delivery of T-0002', r.stdout)


if __name__ == '__main__':
    unittest.main()
