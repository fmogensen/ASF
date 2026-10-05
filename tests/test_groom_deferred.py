"""tests.test_groom_deferred — a Task under a Story whose every acceptance line is deferred by a
decision is not asked about again: the decision already settled it (the groom re-decided such
Tasks' ``decided: false`` every day)."""
import datetime
import shutil
import unittest

from asf.groom import groom as groom_mod
from asf.record.core import canonicalize, load_items
from tests.test_inbox_shape import make_repo, write_item

OLD = ['schema_version: 1', 'state: New', 'stage_since: 2026-01-01T00:00:00Z',
       'updated: 2026-01-01T00:00:00Z']


def story_body(*lines):
    return ('## Description\n\nd\n\n## Acceptance\n\n' + ''.join(f'- [ ] {l}\n' for l in lines)
            + '\n## History\n\n- 2026-01-01 00:00 created\n')


class DeferredStoryTasksTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, True)
        write_item(self.root, 'E-0001', 'epic', 'Epic', typed_lines=['decided: true'])
        write_item(self.root, 'F-0001', 'feature', 'Feat', parent='E-0001',
                   typed_lines=['decided: true'])
        write_item(self.root, 'S-0001', 'story', 'All deferred', parent='F-0001',
                   typed_lines=['decided: true'],
                   body=story_body('one — deferred by [[D-0012]]', 'two — deferred by D-0013'))
        write_item(self.root, 'S-0002', 'story', 'Live', parent='F-0001',
                   typed_lines=['decided: true'],
                   body=story_body('one — deferred by [[D-0012]]', 'two is live'))
        write_item(self.root, 'T-0001', 'task', 'Under deferred', parent='S-0001',
                   typed_lines=['decided: false', 'writes: [a.py]'], machine_lines=OLD)
        write_item(self.root, 'T-0002', 'task', 'Named deferred', parent='F-0001',
                   typed_lines=['decided: false', 'writes: [b.py]', 'stories: [S-0001]'],
                   machine_lines=OLD)
        write_item(self.root, 'T-0003', 'task', 'Under live', parent='S-0002',
                   typed_lines=['decided: false', 'writes: [c.py]'], machine_lines=OLD)
        self.canonical = canonicalize(load_items(self.root)[0])[0]

    def ids(self, lines):
        return {l.split()[3] for l in lines}

    def test_the_undecided_sections_skip_tasks_of_a_fully_deferred_story(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        for lines in (groom_mod.groom_undecided_section(self.canonical, now, 3),
                      groom_mod.groom_undecided_section(self.canonical, now, 14),
                      groom_mod.groom_undecided_rest_section(self.canonical, {})):
            got = self.ids(lines)
            self.assertIn('T-0003', got)
            self.assertNotIn('T-0001', got)
            self.assertNotIn('T-0002', got)

    def test_the_rule(self):
        self.assertTrue(groom_mod.deferred_by_decision('T-0001', self.canonical))
        self.assertTrue(groom_mod.deferred_by_decision('T-0002', self.canonical))
        self.assertFalse(groom_mod.deferred_by_decision('T-0003', self.canonical))
        self.assertFalse(groom_mod.deferred_by_decision('S-0002', self.canonical))


if __name__ == '__main__':
    unittest.main()
