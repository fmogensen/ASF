"""tests.test_move — ``asf move`` (F-0120 §5): the selection, the all-or-nothing round-trip,
``--remove``, ``--to`` and the deduped import into the target's inbox."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from asf import cli, env
from asf.record import frontmatter
from asf.record.core import is_retired

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.x` does not
    from tests import gitfixture
except ImportError:  # pragma: no cover - import shape only
    import gitfixture

FOLDERS = ['epics', 'features', 'stories', 'tasks', 'bugs', 'decisions', 'rules']
FOLDER_OF = {'epic': 'epics', 'feature': 'features', 'story': 'stories', 'task': 'tasks',
             'bug': 'bugs'}


def _body(description='x', acceptance=('y',)):
    acc = ''.join(f"- [ ] {a}\n" for a in acceptance) if acceptance else "- [ ] \n"
    return (f"## Description\n{description}\n\n## Acceptance\n{acc}\n## Non-goals\n\n"
           "## History\n- 2026-09-01: created\n\n## Children\n\n## Backlinks\n")


def write_item(root, id_, type_, title, parent=None, typed=(), body=None):
    lines = [f'id: {id_}', f'type: {type_}', f'title: {title}']
    if parent:
        lines.append(f'parent: {parent}')
    lines += list(typed)
    lines += ['# ---- machine ----', 'state: New', 'stage_since: 2026-09-01T00:00:00Z',
             'updated: 2026-09-01T00:00:00Z']
    path = os.path.join(root, FOLDER_OF[type_], f'{id_}.md')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('---\n' + '\n'.join(lines) + '\n---\n' + (body if body is not None else _body()))
    return path


def meta_of(root, type_, id_):
    path = os.path.join(root, FOLDER_OF[type_], f'{id_}.md')
    with open(path, encoding='utf-8') as f:
        return frontmatter.parse(f.read(), path=f'{FOLDER_OF[type_]}/{id_}.md')[0]


def make_repo():
    root = tempfile.mkdtemp(prefix='move_test_')
    for f in FOLDERS:
        os.makedirs(os.path.join(root, f))
    with open(os.path.join(root, 'index.json'), 'w', encoding='utf-8') as f:
        json.dump({'generated': '', 'items': {}}, f)
    return root


def git_log_count(root):
    out = subprocess.run(['git', '-C', root, 'rev-list', '--count', 'HEAD'],
                         capture_output=True, text=True, check=True)
    return int(out.stdout.strip())


class _CLIBase(unittest.TestCase):
    """Shared ``ASF_HOME`` + product-registration plumbing, ``cli.main`` run in-process."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='move_cli_')
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        self._orig_cwd = os.getcwd()
        os.makedirs(os.path.join(self.tmp, 'products'))
        with open(env.config_path(), 'w', encoding='utf-8') as f:
            f.write('scheduler:\n  kind: none\n')

    def tearDown(self):
        os.chdir(self._orig_cwd)
        env.ASF_HOME = self._orig_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def register(self, name, backlog_dir):
        with open(env.product_path(name), 'w', encoding='utf-8') as f:
            f.write(f'product: {name}\nbacklog_dir: {backlog_dir}\n')

    def run_cli(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = cli.main(argv)
        return rc, out.getvalue()


class BulkRemoveTests(_CLIBase):
    """The card's first named test: a bulk remove writes each card and makes one commit."""

    def setUp(self):
        super().setUp()
        tree = make_repo()
        write_item(tree, 'E-0001', 'epic', 'the epic')
        write_item(tree, 'F-0001', 'feature', 'card one', parent='E-0001')
        write_item(tree, 'F-0002', 'feature', 'card two', parent='E-0001')
        write_item(tree, 'F-0003', 'feature', 'card three', parent='E-0001')
        gitfixture.publish(tree, os.path.join(self.tmp, 'origin.git'))
        self.record = tree
        self.register('sample', self.record)

    def test_bulk_remove_writes_each_card_and_makes_one_commit(self):
        before = git_log_count(self.record)
        rc, out = self.run_cli(['move', 'F-0001', 'F-0002', '--remove', 'not ours',
                                '--product', 'sample'])
        self.assertEqual(rc, 0, out)
        for iid in ('F-0001', 'F-0002'):
            meta = meta_of(self.record, 'feature', iid)
            self.assertEqual(meta['removed'], 'not ours')
            self.assertIsNone(meta.get('belongs_to'))
            self.assertTrue(is_retired(meta))
        meta3 = meta_of(self.record, 'feature', 'F-0003')
        self.assertFalse(is_retired(meta3))
        self.assertEqual(git_log_count(self.record) - before, 1, out)

    def test_a_second_identical_run_writes_nothing_and_makes_no_commit(self):
        rc, out = self.run_cli(['move', 'F-0001', 'F-0002', '--remove', 'not ours',
                                '--product', 'sample'])
        self.assertEqual(rc, 0, out)
        before = git_log_count(self.record)
        rc2, out2 = self.run_cli(['move', 'F-0001', 'F-0002', '--remove', 'not ours',
                                  '--product', 'sample'])
        self.assertEqual(rc2, 2, out2)  # both already retired: an empty selection refuses
        self.assertEqual(git_log_count(self.record), before, out2)


class SelectionTests(_CLIBase):
    def setUp(self):
        super().setUp()
        self.record = make_repo()
        write_item(self.record, 'E-0001', 'epic', 'the epic')
        write_item(self.record, 'F-0001', 'feature', 'card one', parent='E-0001',
                  typed=['belongs_to: acme'])
        write_item(self.record, 'F-0002', 'feature', 'card two', parent='E-0001',
                  typed=['belongs_to: acme'])
        write_item(self.record, 'F-0003', 'feature', 'unrelated card', parent='E-0001')
        self.register('sample', self.record)

    def test_ids_and_query_together_refuse(self):
        rc, out = self.run_cli(['move', 'F-0001', '--query', 'belongs_to=acme', '--remove', 'x',
                                '--product', 'sample'])
        self.assertEqual(rc, 2, out)
        self.assertIn('never both', out)
        meta = meta_of(self.record, 'feature', 'F-0001')
        self.assertFalse(is_retired(meta))

    def test_an_unknown_id_refuses(self):
        rc, out = self.run_cli(['move', 'F-9999', '--remove', 'x', '--product', 'sample'])
        self.assertEqual(rc, 2, out)
        self.assertIn("no item 'F-9999'", out)

    def test_an_empty_query_result_refuses_with_the_spec_message(self):
        rc, out = self.run_cli(['move', '--query', 'belongs_to=nobody', '--remove', 'x',
                                '--product', 'sample'])
        self.assertEqual(rc, 2, out)
        self.assertIn('--query belongs_to=nobody selects no card', out)

    def test_two_query_pairs_and(self):
        rc, out = self.run_cli(['move', '--query', 'belongs_to=acme', '--query', 'type=feature',
                                '--remove', 'x', '--dry-run', '--product', 'sample'])
        self.assertEqual(rc, 0, out)
        self.assertIn('F-0001', out)
        self.assertIn('F-0002', out)
        self.assertNotIn('F-0003', out)

    def test_a_card_that_would_not_round_trip_refuses_the_whole_selection(self):
        rc, out = self.run_cli(['move', 'F-0001', 'F-0002', '--remove', 'bad\nreason',
                                '--product', 'sample'])
        self.assertEqual(rc, 2, out)
        for iid in ('F-0001', 'F-0002'):
            meta = meta_of(self.record, 'feature', iid)
            self.assertFalse(is_retired(meta))
            self.assertEqual(meta.get('belongs_to'), 'acme')


class DryRunTests(_CLIBase):
    def setUp(self):
        super().setUp()
        self.record = make_repo()
        write_item(self.record, 'E-0001', 'epic', 'the epic')
        write_item(self.record, 'F-0001', 'feature', 'dunning retries', parent='E-0001')
        self.register('sample', self.record)
        self.target = make_repo()
        self.register('acme', self.target)

    def test_dry_run_remove_prints_the_table_and_writes_nothing(self):
        rc, out = self.run_cli(['move', 'F-0001', '--remove', 'not ours', '--dry-run',
                                '--product', 'sample'])
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.splitlines()[0], f'record: {self.record}', out)
        self.assertIn('removed', out)
        self.assertFalse(is_retired(meta_of(self.record, 'feature', 'F-0001')))

    def test_dry_run_to_prints_the_table_and_writes_nothing_in_either_record(self):
        rc, out = self.run_cli(['move', 'F-0001', '--to', 'acme', '--dry-run',
                                '--product', 'sample'])
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.splitlines()[0], f'record: {self.record}', out)
        self.assertIn('moved to acme', out)
        meta = meta_of(self.record, 'feature', 'F-0001')
        self.assertFalse(is_retired(meta))
        inbox = os.path.join(self.target, 'inbox')
        self.assertEqual([] if not os.path.isdir(inbox) else os.listdir(inbox), [])


class MoveToTargetTests(_CLIBase):
    """The card's second named test: a move imports to the target inbox deduped."""

    def setUp(self):
        super().setUp()
        src_tree = make_repo()
        write_item(src_tree, 'E-0001', 'epic', 'the epic')
        write_item(src_tree, 'F-0001', 'feature', 'billing retries', parent='E-0001',
                  typed=['belongs_to: acme'],
                  body=_body(description='Retry billing on failure.',
                            acceptance=('retries stop at the fourth attempt',)))
        write_item(src_tree, 'F-0002', 'feature', 'billing refunds', parent='E-0001',
                  typed=['belongs_to: acme'])
        gitfixture.publish(src_tree, os.path.join(self.tmp, 'source.git'))
        self.source = src_tree
        self.register('source', self.source)

        tgt_tree = make_repo()
        write_item(tgt_tree, 'E-0100', 'epic', 'billing platform')
        gitfixture.publish(tgt_tree, os.path.join(self.tmp, 'target.git'))
        self.target = tgt_tree
        self.register('acme', self.target)

    def test_move_to_writes_moved_to_and_files_deduped_intake(self):
        src_before = git_log_count(self.source)
        tgt_before = git_log_count(self.target)
        rc, out = self.run_cli(['move', '--query', 'belongs_to=acme', '--to', 'acme',
                                '--product', 'source'])
        self.assertEqual(rc, 0, out)
        for iid in ('F-0001', 'F-0002'):
            meta = meta_of(self.source, 'feature', iid)
            self.assertEqual(meta['moved_to'], 'acme')
            self.assertIsNone(meta.get('belongs_to'))
            self.assertTrue(is_retired(meta))
        self.assertEqual(git_log_count(self.source) - src_before, 1, out)
        self.assertEqual(git_log_count(self.target) - tgt_before, 1, out)

        inbox = os.path.join(self.target, 'inbox')
        files = sorted(f for f in os.listdir(inbox) if f.endswith('.md'))
        self.assertEqual(len(files), 2, files)
        found = False
        for name in files:
            with open(os.path.join(inbox, name), encoding='utf-8') as f:
                text = f.read()
            self.assertNotIn('parent:', text)
            if 'moved_from: source:F-0001' in text:
                found = True
                self.assertIn('Retry billing on failure.', text)
                self.assertIn('retries stop at the fourth attempt', text)
        self.assertTrue(found, files)

        # a second identical run imports nothing more: both source cards are now retired, so
        # the same selector picks nothing
        rc2, out2 = self.run_cli(['move', '--query', 'belongs_to=acme', '--to', 'acme',
                                  '--product', 'source'])
        self.assertEqual(rc2, 2, out2)
        self.assertEqual(sorted(f for f in os.listdir(inbox) if f.endswith('.md')), files)

    def test_an_unregistered_to_is_one_needs_operator_line(self):
        rc, out = self.run_cli(['move', '--query', 'belongs_to=acme', '--to', 'nowhere',
                                '--product', 'source'])
        self.assertEqual(rc, 2, out)
        lines = out.strip().splitlines()
        self.assertEqual(lines[0], f'record: {self.source}', out)
        self.assertTrue(lines[-1].startswith('NEEDS OPERATOR: '), out)

    def test_to_refuses_when_the_resolved_source_product_is_not_this_record(self):
        # a record that is nobody's registered backlog_dir, with no --product and no
        # $ASF_PRODUCT: `env.resolve_product` falls back to config.yaml's default_product,
        # which names a different record entirely (D16)
        unregistered = make_repo()
        write_item(unregistered, 'E-0001', 'epic', 'the epic')
        other = make_repo()
        self.register('other', other)
        with open(env.config_path(), 'w', encoding='utf-8') as f:
            f.write('scheduler:\n  kind: none\ndefault_product: other\n')
        os.chdir(unregistered)
        rc, out = self.run_cli(['move', 'E-0001', '--to', 'acme'])
        self.assertEqual(rc, 2, out)
        self.assertIn("needs the source product's own record", out)


class DedupeSurvivesMintingTests(_CLIBase):
    def setUp(self):
        super().setUp()
        src_tree = make_repo()
        write_item(src_tree, 'E-0001', 'epic', 'the epic')
        write_item(src_tree, 'F-0001', 'feature', 'billing retries', parent='E-0001',
                  typed=['belongs_to: acme'])
        gitfixture.publish(src_tree, os.path.join(self.tmp, 'source.git'))
        self.source = src_tree
        self.register('source', self.source)

        tgt_tree = make_repo()
        write_item(tgt_tree, 'E-0100', 'epic', 'billing platform')
        gitfixture.publish(tgt_tree, os.path.join(self.tmp, 'target.git'))
        self.target = tgt_tree
        self.register('acme', self.target)

    def test_dedupe_survives_the_targets_groom_minting_the_card(self):
        rc, out = self.run_cli(['move', 'F-0001', '--to', 'acme', '--product', 'source'])
        self.assertEqual(rc, 0, out)
        inbox = os.path.join(self.target, 'inbox')
        self.assertEqual(len([f for f in os.listdir(inbox) if f.endswith('.md')]), 1)

        rc2, out2 = self.run_cli(['groom', '--product', 'acme'])
        self.assertEqual(rc2, 0, out2)
        self.assertEqual([f for f in os.listdir(inbox) if f.endswith('.md')], [])
        minted = [f for f in os.listdir(os.path.join(self.target, 'features')) if f.endswith('.md')]
        self.assertEqual(len(minted), 1, minted)
        with open(os.path.join(self.target, 'features', minted[0]), encoding='utf-8') as f:
            meta, _body = frontmatter.parse(f.read(), path='features/' + minted[0])
        self.assertEqual(meta.get('moved_from'), 'source:F-0001')

        from asf.record.move import dedupe
        self.assertEqual(dedupe(self.target, 'inbox', 'source:F-0001'), meta['id'])

        # the same `asf move` run again imports nothing more: the intake file is gone, but the
        # ref now lives on the minted card and in the target's index.json (D14)
        rc3, out3 = self.run_cli(['move', 'F-0001', '--to', 'acme', '--product', 'source'])
        self.assertEqual(rc3, 2, out3)  # F-0001 is already retired: the source-side no-op
        self.assertEqual([f for f in os.listdir(inbox) if f.endswith('.md')], [])


if __name__ == '__main__':
    unittest.main()
