"""R14 / I1, I2, I3, I10, I11 — the record step one writer at a time.

Each writer of the record clone (ingest, the plan-task minter, ``asf set`` and the widen pass
through ``set_typed``) runs through :mod:`asf.record.stage`: its change is captured, the record
invariants (:mod:`asf.invariants`) judge that change alone, and only the paths an invariant
refuses are put back — to what they held before *that* writer ran — while everything else it
wrote, and every other writer's output, stands. A bad write is refused before commit instead of
refusing every commit after it."""
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import invariants, redact
from asf.record import frontmatter, ingest, stage
from asf.record.core import load_items
from asf.record.index import do_index
from asf.record.setfield import set_typed

FOLDERS = ['epics', 'features', 'stories', 'tasks', 'bugs', 'decisions', 'rules']
FOLDER_OF = {'epic': 'epics', 'feature': 'features', 'story': 'stories', 'task': 'tasks',
             'bug': 'bugs'}
BODY = ("## Description\n\n## Acceptance\n- [ ] \n\n## Non-goals\n\n## History\n"
        "- 2026-01-01: created\n\n## Children\n\n## Backlinks\n")
EMPTY_EV = {'features': {}, 'stories': {}, 'prod_sha': None, 'dev_sha': None,
            'checked': set(), 'main_sha': None, 'merged': {}, 'branches': []}
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_NOSYSTEM": "1"}


def make_record():
    root = tempfile.mkdtemp(prefix='record_stage_')
    for f in FOLDERS:
        os.makedirs(os.path.join(root, f))
    return root


def write(root, id_, type_, title='t', parent=None, typed=(), machine=None, body=BODY):
    machine = list(machine if machine is not None else
                   ('schema_version: 1', 'state: New', 'stage_since: 2026-01-01T00:00:00Z',
                    'updated: 2026-01-01T00:00:00Z'))
    lines = [f'id: {id_}', f'type: {type_}', f'title: {title}']
    if parent:
        lines.append(f'parent: {parent}')
    lines += list(typed) + ['# ---- machine ----'] + machine
    rel = f'{FOLDER_OF[type_]}/{id_}.md'
    with open(os.path.join(root, rel), 'w', encoding='utf-8') as f:
        f.write('---\n' + '\n'.join(lines) + '\n---\n' + body)
    return rel


def read(root, rel):
    with open(os.path.join(root, rel), encoding='utf-8') as f:
        return f.read()


def meta(root, rel):
    return frontmatter.parse(read(root, rel), path=rel)[0]


def stamp_index(root, version=1):
    with open(os.path.join(root, 'index.json'), 'w', encoding='utf-8') as f:
        f.write('{"items": {}, "schema_version": %d}\n' % version)


class StageTestCase(unittest.TestCase):
    def setUp(self):
        self.root = make_record()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(stage.drain)
        # the redaction patterns are the test's own: no operator config leaks in
        p = mock.patch.object(redact, 'default_patterns', return_value=[])
        p.start()
        self.addCleanup(p.stop)


class Stage(StageTestCase):
    def test_r14_stage_captures_one_writers_paths_and_their_content_before_it(self):
        a = write(self.root, 'F-0001', 'feature')
        before = read(self.root, a)

        def writer(root):
            with open(os.path.join(root, a), 'a', encoding='utf-8') as f:
                f.write('more\n')
            write(root, 'F-0002', 'feature')

        s = stage.stage(self.root, 'ingest', writer)
        self.assertEqual(s.paths, ('features/F-0001.md', 'features/F-0002.md'))
        self.assertEqual(s.before, {a: before, 'features/F-0002.md': None})

    def test_r14_one_writer_refused_the_other_writers_output_stands(self):
        """Soft failure: the writer that strips a machine key (I1) has only that card put back —
        to what it held before *it* ran, so the earlier writer's good output on the same card
        survives — and its other, sound, write stands; nothing raises."""
        a = write(self.root, 'F-0001', 'feature', machine=(
            'schema_version: 1', 'state: New', 'cost: {usd: 1.5}',
            'stage_since: 2026-01-01T00:00:00Z', 'updated: 2026-01-01T00:00:00Z'))

        def good(root):
            frontmatter.merge_machine(os.path.join(root, a), {'state': 'Active'})
            write(root, 'F-0002', 'feature')

        def strips(root):
            path = os.path.join(root, a)
            m = frontmatter.split_machine(meta(root, a))[1]
            m.pop('cost')
            frontmatter.write_machine(path, m)
            write(root, 'F-0003', 'feature')

        staged, findings = stage.run_writers(self.root, [('ingest', good), ('widen', strips)])
        self.assertEqual([(f.invariant, f.paths) for f in findings], [('I1', (a,))])
        self.assertIn('cost', meta(self.root, a))
        self.assertEqual(meta(self.root, a)['state'], 'Active')  # the first writer's write stands
        self.assertTrue(os.path.exists(os.path.join(self.root, 'features', 'F-0003.md')))
        self.assertEqual(staged[1].refused, (a,))
        self.assertEqual(staged[1].paths, ('features/F-0003.md',))

    def test_r14_guarded_prints_and_keeps_the_finding_for_the_bug_filer(self):
        a = write(self.root, 'F-0001', 'feature')
        lines = []

        def strips(root):
            frontmatter.write_machine(os.path.join(root, a), {'state': 'New'})

        _r, s, findings = stage.guarded(self.root, 'set', strips, out=lines.append)
        self.assertEqual(s.refused, (a,))
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith('INVARIANT I1: set refused features/F-0001.md'), lines)
        self.assertEqual(stage.drain(), findings)
        self.assertEqual(stage.drain(), [])

    def test_r14_a_created_path_refused_is_removed(self):
        stamp_index(self.root)

        def mints(root):
            write(root, 'T-0001', 'task', machine=('state: New',))  # no schema_version: I2

        _r, s, findings = stage.guarded(self.root, 'plan-tasks', mints, out=lambda *_: None)
        self.assertEqual([f.invariant for f in findings], ['I2'])
        self.assertFalse(os.path.exists(os.path.join(self.root, 'tasks', 'T-0001.md')))


class I1NeverStripMachineKeys(StageTestCase):
    def test_i1_derivable_keys_may_go_others_may_not(self):
        a = write(self.root, 'F-0001', 'feature', machine=(
            'schema_version: 1', 'state: Active', 'stage: card', 'evidence: [x]',
            'spend_usd: 2.5', 'stage_since: 2026-01-01T00:00:00Z', 'updated: 2026-01-01T00:00:00Z'))

        def drops_derivable(root):
            frontmatter.merge_machine(os.path.join(root, a), {}, drop=('stage', 'evidence'))

        _r, _s, findings = stage.guarded(self.root, 'ingest', drops_derivable)
        self.assertEqual(findings, [])
        with self.assertRaises(ValueError):
            frontmatter.merge_machine(os.path.join(self.root, a), {}, drop=('spend_usd',))

    def test_i1_ingest_merges_the_machine_block_and_never_rebuilds_it(self):
        """Every key ingest does not derive keeps its own line byte for byte — an unknown key, a
        quoted value, a trailing comment — while the keys it derives change in place."""
        odd = ('schema_version: 1', 'state: New', 'x_future_key: "kept, as written"  # a note',
               'cost: {usd: 1.25, sessions: 3}', 'stage_since: 2026-01-01T00:00:00Z',
               'updated: 2026-01-01T00:00:00Z')
        rel = write(self.root, 'E-0001', 'epic', typed=('closed: true',), machine=odd)
        stamp_index(self.root)
        with mock.patch.object(ingest.evidence, 'load', return_value=dict(EMPTY_EV)):
            self.assertEqual(ingest.ingest_into(self.root, dict(EMPTY_EV)), 0)
        text = read(self.root, rel)
        self.assertEqual(meta(self.root, rel)['state'], 'Closed')
        for line in odd[:1] + odd[2:4]:
            self.assertIn('\n' + line + '\n', text)


class R11I2AfterRestamp(StageTestCase):
    def test_r11_i2_after_restamp(self):
        """A migrated record whose card an older ingest stripped of ``schema_version``: the
        guarded ingest restamps it before I2 reads it — the migrated record never blocks."""
        rel = write(self.root, 'E-0001', 'epic', machine=('state: New',
                                                          'stage_since: 2026-01-01T00:00:00Z',
                                                          'updated: 2026-01-01T00:00:00Z'))
        stamp_index(self.root)
        with mock.patch.object(ingest.evidence, 'load', return_value=dict(EMPTY_EV)):
            rc = ingest.cmd_ingest(mock.Mock(product=None, fresh=False), self.root)
        self.assertEqual(rc, 0)
        self.assertEqual(stage.drain(), [])
        self.assertEqual(meta(self.root, rel)['schema_version'], 1)

    def test_i2_a_writer_that_drops_the_stamp_is_refused(self):
        rel = write(self.root, 'E-0001', 'epic')
        stamp_index(self.root)

        def lowers(root):
            frontmatter.merge_machine(os.path.join(root, rel), {'schema_version': 0})

        _r, _s, findings = stage.guarded(self.root, 'set', lowers, out=lambda *_: None)
        self.assertEqual([f.invariant for f in findings], ['I2'])
        self.assertEqual(meta(self.root, rel)['schema_version'], 1)

    def test_i2_an_unstamped_record_is_the_migrations_to_stamp(self):
        rel = write(self.root, 'E-0001', 'epic', machine=('state: New',))

        def touches(root):
            frontmatter.merge_machine(os.path.join(root, rel), {'state': 'Active'})

        self.assertEqual(stage.guarded(self.root, 'set', touches)[2], [])


class I3NeverIntersectingWrites(StageTestCase):
    def setUp(self):
        super().setUp()
        subprocess.run(['git', 'init', '-q', self.root], check=True, env=dict(os.environ, **GIT_ENV))
        self.owner = write(self.root, 'T-0001', 'task', parent='F-0001',
                           typed=('writes: [src/a.py]',),
                           machine=('schema_version: 1', 'state: Active',
                                    'stage_since: 2026-01-01T00:00:00Z',
                                    'updated: 2026-01-01T00:00:00Z'))
        self.task = write(self.root, 'T-0002', 'task', parent='F-0001',
                          typed=('writes: [src/b.py]',),
                          machine=('schema_version: 1', 'state: Active',
                                   'stage_since: 2026-01-01T00:00:00Z',
                                   'updated: 2026-01-01T00:00:00Z'))
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'seed')

    def git(self, *args):
        return subprocess.run(['git', '-C', self.root, *args], check=True, capture_output=True,
                              text=True, env=dict(os.environ, **GIT_ENV)).stdout

    def rec(self, iid):
        return load_items(self.root)[0][iid][0]

    def test_i3_set_refuses_an_intersecting_writes_before_it_is_written(self):
        err = set_typed(self.rec('T-0002'), {'writes': ['src/b.py', 'src/*.py']})
        self.assertIn('I3', err)
        self.assertEqual(meta(self.root, self.task)['writes'], ['src/b.py'])
        # a footprint that does not intersect is written
        self.assertIsNone(set_typed(self.rec('T-0002'), {'writes': ['src/b.py', 'src/c.py']}))
        self.assertEqual(meta(self.root, self.task)['writes'], ['src/b.py', 'src/c.py'])

    def test_i3_the_widen_pass_is_refused_before_its_commit(self):
        from asf.tick import widen_footprint
        head = self.git('rev-parse', 'HEAD')
        err = widen_footprint.write_card(self.root, 'T-0002', {'writes': ['src/b.py', 'src/a.py']},
                                         '2026-09-24 10:00', 'footprint widened: +src/a.py (test)')
        self.assertIn('I3', err)
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)  # nothing committed
        self.assertEqual(self.git('status', '--porcelain'), '')  # nothing left written
        self.assertIsNone(widen_footprint.write_card(
            self.root, 'T-0002', {'writes': ['src/b.py', 'src/d.py']}, '2026-09-24 10:00',
            'footprint widened: +src/d.py (test)'))
        self.assertNotEqual(self.git('rev-parse', 'HEAD'), head)

    def test_i3_an_intersection_already_in_the_record_is_not_this_writers(self):
        write(self.root, 'T-0003', 'task', parent='F-0001', typed=('writes: [src/a.py]',),
              machine=('schema_version: 1', 'state: Active'))
        self.assertIsNone(set_typed(self.rec('T-0003'), {'title': 'renamed'}))


class I10FeatureLandsOnlyOnClosedTasks(StageTestCase):
    def test_i10_a_feature_resolved_over_an_open_task_is_refused(self):
        f = write(self.root, 'F-0001', 'feature')
        write(self.root, 'T-0001', 'task', parent='F-0001', machine=('schema_version: 1',
                                                                      'state: Closed'))
        write(self.root, 'T-0002', 'task', parent='F-0001', machine=('schema_version: 1',
                                                                      'state: Active'))

        def lands(root):
            frontmatter.merge_machine(os.path.join(root, f), {'state': 'Resolved',
                                                              'stage': 'landed'})

        _r, _s, findings = stage.guarded(self.root, 'ingest', lands, out=lambda *_: None)
        self.assertEqual([(x.invariant, x.subject) for x in findings], [('I10', 'F-0001')])
        self.assertIn('T-0002', findings[0].message)
        self.assertEqual(meta(self.root, f)['state'], 'New')

    def test_i10_every_task_closed_lands(self):
        f = write(self.root, 'F-0001', 'feature')
        write(self.root, 'T-0001', 'task', parent='F-0001', machine=('schema_version: 1',
                                                                      'state: Closed'))

        def lands(root):
            frontmatter.merge_machine(os.path.join(root, f), {'state': 'Resolved'})

        self.assertEqual(stage.guarded(self.root, 'ingest', lands)[2], [])


class I11DerivedTextIsScrubbed(StageTestCase):
    PATS = [redact.Pattern('name', 'test', __import__('re').compile(r'\bzorblax\b', 2))]

    def test_i11_derived_backlinks_never_copy_a_protected_name(self):
        """derived-backlinks-copy-a-protected-name: a title holding a protected name is quoted in
        other cards' Backlinks/Children as a neutral token, never the name."""
        write(self.root, 'E-0001', 'epic', title='Billing')
        write(self.root, 'F-0001', 'feature', title='Pay for Zorblax account', parent='E-0001')
        write(self.root, 'F-0002', 'feature', title='See F-0001', parent='E-0001')
        with mock.patch.object(redact, 'default_patterns', return_value=self.PATS):
            _r, _s, findings = stage.guarded(self.root, 'index', lambda root: do_index(root))
            self.assertEqual(findings, [])
            children = read(self.root, 'epics/E-0001.md').split('## Children')[1]
            self.assertIn('[F-0001](../features/F-0001.md) Pay for [redacted] account', children)
            self.assertNotIn('Zorblax', children)
            # the card that holds the name keeps it (asf check flags it there); nothing copies it
            self.assertIn('title: Pay for Zorblax account', read(self.root, 'features/F-0001.md'))

    def test_i11_a_writer_copying_the_name_into_derived_text_is_refused(self):
        rel = write(self.root, 'F-0002', 'feature', title='x')

        def copies(root):
            with open(os.path.join(root, rel), 'a', encoding='utf-8') as f:
                f.write('- [F-0001](../features/F-0001.md) Pay for zorblax\n')

        with mock.patch.object(redact, 'default_patterns', return_value=self.PATS):
            _r, _s, findings = stage.guarded(self.root, 'index', copies, out=lambda *_: None)
        self.assertEqual([f.invariant for f in findings], ['I11'])
        self.assertNotIn('zorblax', read(self.root, rel))

    def test_a_removed_or_moved_card_has_no_derived_sections(self):
        write(self.root, 'E-0001', 'epic', title='Billing')
        write(self.root, 'F-0001', 'feature', title='gone', parent='E-0001',
              typed=('removed: "superseded"',))
        write(self.root, 'F-0002', 'feature', title='elsewhere', parent='E-0001',
              typed=('moved_to: other:F-0009',))
        write(self.root, 'T-0001', 'task', title='under', parent='F-0001')
        write(self.root, 'F-0003', 'feature', title='names F-0001 and F-0002', parent='E-0001')
        do_index(self.root)
        for rel in ('features/F-0001.md', 'features/F-0002.md'):
            body = read(self.root, rel)
            self.assertEqual(body.split('## Children')[1], '\n\n## Backlinks\n', rel)


class Registry(unittest.TestCase):
    def test_the_record_invariants_are_registered(self):
        ids = [i.id for i in invariants.INVARIANTS if i.scope == 'record']
        for want in ('I1', 'I2', 'I3', 'I10', 'I11'):
            self.assertIn(want, ids)


if __name__ == '__main__':
    unittest.main()
