"""The ``landing: {sha, as_of, by}`` stamp (W4-PR3a): every closing rule that closes a Task, Bug
or Story writes which commit closed it, when, and by which path; the trunk's revert of that
commit rewrites it; ``asf reopen`` clears it; ``asf check`` holds its shape; the pinned reader
still loads a stamped card."""
import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from asf.evidence import evidence
from asf.groom import policy
from asf.record import check, frontmatter, ingest
from asf.record.index import do_index
from asf.record.reopen import cmd_reopen

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.x` does not
    import pinned
except ImportError:  # pragma: no cover - import shape only
    from tests import pinned

FOLDERS = ['epics', 'features', 'stories', 'tasks', 'bugs', 'decisions', 'rules']
FOLDER_OF = {'epic': 'epics', 'feature': 'features', 'story': 'stories', 'task': 'tasks',
             'bug': 'bugs'}
BODY = ("## Description\n\n## Acceptance\n- [ ] \n\n## Non-goals\n\n## History\n"
        "- 2026-01-01: created\n\n## Children\n\n## Backlinks\n")
EMPTY_EV = {'features': {}, 'stories': {}, 'prod_sha': None, 'dev_sha': None,
            'checked': set(), 'main_sha': None, 'merged': {}, 'branches': [], 'ids': {}}
A, B, C = 'a' * 40, 'b' * 40, 'c' * 40
NEW = ('schema_version: 1', 'state: New', 'stage_since: 2026-01-01T00:00:00Z',
       'updated: 2026-01-01T00:00:00Z')


def make_record():
    root = tempfile.mkdtemp(prefix='landing_test_')
    for f in FOLDERS:
        os.makedirs(os.path.join(root, f))
    return root


def write(root, id_, type_, parent=None, typed=(), machine=NEW):
    lines = [f'id: {id_}', f'type: {type_}', f'title: {id_} title']
    if parent:
        lines.append(f'parent: {parent}')
    lines += list(typed) + ['# ---- machine ----'] + list(machine)
    rel = f'{FOLDER_OF[type_]}/{id_}.md'
    with open(os.path.join(root, rel), 'w', encoding='utf-8') as f:
        f.write('---\n' + '\n'.join(lines) + '\n---\n' + BODY)
    return rel


def ids(iid, sha, **extra):
    return {iid: dict({'branches': [], 'open_prs': [], 'commit': sha, 'pr': None,
                       'green': True}, **extra)}


class _Record(unittest.TestCase):
    def setUp(self):
        self.root = make_record()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def ingest(self, ev):
        with mock.patch.object(ingest.evidence, 'load', return_value=ev), \
                contextlib.redirect_stdout(io.StringIO()):
            return ingest.cmd_ingest(types.SimpleNamespace(fresh=False, registry=False),
                                     self.root)

    def meta(self, rel):
        with open(os.path.join(self.root, rel), encoding='utf-8') as f:
            return frontmatter.parse(f.read(), path=rel)[0]

    def machine(self, rel):
        return frontmatter.split_machine(self.meta(rel))[1]

    def text(self, rel):
        with open(os.path.join(self.root, rel), encoding='utf-8') as f:
            return f.read()


class EachClosingRuleStamps(_Record):
    """A Task, Bug or Story closing now carries ``landing:`` naming its rule's evidence."""

    def test_a_task_closed_by_a_commit_naming_it_is_stamped_names(self):
        rel = write(self.root, 'T-0001', 'task')
        self.assertEqual(self.ingest(dict(EMPTY_EV, ci=True, ids=ids('T-0001', A))), 0)
        m = self.machine(rel)
        self.assertEqual(m['state'], 'Closed')
        self.assertEqual(m['landing']['sha'], A)
        self.assertEqual(m['landing']['by'], 'names')
        self.assertRegex(m['landing']['as_of'], r'^\d{4}-\d{2}-\d{2}T')

    def test_a_task_closed_by_the_lanes_merge_fact_is_stamped_pr_merge(self):
        rel = write(self.root, 'T-0001', 'task')
        self.ingest(dict(EMPTY_EV, ci=True, ids=ids('T-0001', A, merge='worker/T-0001', pr=7)))
        self.assertEqual(self.machine(rel)['landing']['by'], 'pr-merge')

    def test_a_task_closed_by_its_plan_rows_merged_pr_is_stamped_pr_merge(self):
        rel = write(self.root, 'T-0001', 'task')
        with mock.patch.object(ingest, 'match_task', return_value=(
                'f-0001', 'T1', {'merged_sha': B, 'pr': 9, 'branch': 'worker/T-0001'})):
            self.ingest(dict(EMPTY_EV, ci=True))
        m = self.machine(rel)
        self.assertEqual((m['state'], m['landing']['sha'], m['landing']['by']),
                         ('Closed', B, 'pr-merge'))

    def test_a_run_closed_on_trunk_evidence_is_stamped_trunkclose_with_its_arm(self):
        rel = write(self.root, 'T-0001', 'task')
        self.ingest(dict(EMPTY_EV, ci=True, ids=ids(
            'T-0001', A, merge='worker/T-0001', trunk_closed='already on origin/main',
            trunk_arm='names')))
        self.assertEqual(self.machine(rel)['landing']['by'], 'trunkclose/names')

    def test_a_trunk_close_naming_no_arm_is_held_to_the_covers_proof(self):
        rel = write(self.root, 'T-0001', 'task')
        self.ingest(dict(EMPTY_EV, ci=True, ids=ids(
            'T-0001', A, merge='worker/T-0001', trunk_closed='already on origin/main')))
        self.assertEqual(self.machine(rel)['landing']['by'], 'trunkclose/covers')

    def test_a_landing_the_groom_accepted_is_stamped_groom(self):
        rel = write(self.root, 'T-0001', 'task')
        self.ingest(dict(EMPTY_EV, ci=True, ids=ids(
            'T-0001', A, merge='worker/T-0001',
            trunk_closed=f'{policy.COVERS_ACCEPT}: aaaaaaa covers its writes:')))
        self.assertEqual(self.machine(rel)['landing']['by'], 'groom')

    def test_a_bug_resolved_by_a_commit_is_stamped(self):
        rel = write(self.root, 'B-0001', 'bug')
        self.ingest(dict(EMPTY_EV, ci=True, ids=ids('B-0001', C, green=False)))
        m = self.machine(rel)
        self.assertEqual((m['state'], m['landing']['sha'], m['landing']['by']),
                         ('Resolved', C, 'names'))

    def test_a_story_closed_by_its_tasks_carries_the_newest_tasks_sha(self):
        write(self.root, 'F-0001', 'feature')
        rel = write(self.root, 'S-0001', 'story', parent='F-0001')
        write(self.root, 'T-0001', 'task', parent='F-0001', typed=('stories: [S-0001]',))
        self.ingest(dict(EMPTY_EV, ci=True, ids=ids('T-0001', A)))
        m = self.machine(rel)
        self.assertIn(m['state'], ('Resolved', 'Closed'))
        self.assertEqual((m['landing']['sha'], m['landing']['by']), (A, 'children'))

    def test_a_child_descent_closed_carries_its_parents_sha(self):
        # S6: an open Story keeps its Feature from deriving Closed, so descent meets an
        # evidence-free Story only under a Feature an earlier pass already closed
        write(self.root, 'F-0001', 'feature',
              machine=tuple(l.replace('state: New', 'state: Closed') for l in NEW))
        rel = write(self.root, 'S-0001', 'story', parent='F-0001')
        fev = {'alias': None, 'spec': 'origin/main:docs/specs/f-0001.md', 'spec_branch': None,
               'spec_on_main': True, 'spec_review': None, 'plan': None, 'plan_branch': None,
               'plan_on_main': False, 'plan_review': None, 'tasks': {}, 'prs': []}
        self.ingest(dict(EMPTY_EV, ci=True, features={'f-0001': fev}, ids=ids('F-0001', B)))
        m = self.machine(rel)
        self.assertEqual(m['state'], 'Closed')
        self.assertEqual((m['landing']['sha'], m['landing']['by']), (B, 'descent'))

    def test_a_feature_carries_no_stamp(self):
        rel = write(self.root, 'F-0001', 'feature')
        self.ingest(dict(EMPTY_EV, ci=True, ids=ids('F-0001', B)))
        self.assertNotIn('landing', self.machine(rel))

    def test_a_card_already_closed_is_not_stamped_by_ingest_and_a_stamp_is_kept(self):
        closed = write(self.root, 'T-0001', 'task', machine=(
            'schema_version: 1', 'state: Closed', 'stage_since: 2026-01-01T00:00:00Z',
            'updated: 2026-01-01T00:00:00Z'))
        self.ingest(dict(EMPTY_EV, ci=True, ids=ids('T-0001', A)))
        self.assertNotIn('landing', self.machine(closed))   # the migration's job, not ingest's
        stamped = write(self.root, 'T-0002', 'task', machine=(
            'schema_version: 1', 'state: Closed',
            f'landing: {{sha: {B}, as_of: 2026-01-02T00:00:00Z, by: pr-merge}}',
            'stage_since: 2026-01-01T00:00:00Z', 'updated: 2026-01-01T00:00:00Z'))
        self.ingest(dict(EMPTY_EV, ci=True, ids=ids('T-0002', A)))
        self.assertEqual(self.machine(stamped)['landing']['sha'], B)

    def test_ingest_twice_is_no_diff(self):
        rel = write(self.root, 'T-0001', 'task')
        ev = dict(EMPTY_EV, ci=True, ids=ids('T-0001', A))
        self.ingest(ev)
        before = self.text(rel)
        self.ingest(ev)
        self.assertEqual(self.text(rel), before)

    def test_a_resolved_task_that_derives_open_again_loses_its_stamp(self):
        rel = write(self.root, 'T-0001', 'task')
        self.ingest(dict(EMPTY_EV, ci=True, ids=ids('T-0001', A, green=False)))
        self.assertEqual(self.machine(rel)['state'], 'Resolved')
        self.assertIn('landing', self.machine(rel))
        self.ingest(dict(EMPTY_EV, ci=True, ids={'T-0001': {
            'branches': ['worker/T-0001'], 'open_prs': [], 'commit': None, 'pr': None,
            'green': False}}))
        self.assertEqual(self.machine(rel)['state'], 'Active')
        self.assertNotIn('landing', self.machine(rel))


class RevertedLanding(_Record):
    """A landing commit the trunk reverted (``This reverts commit <sha>``) is no landing."""

    def test_a_stamped_card_whose_sha_is_reverted_is_rewritten(self):
        rel = write(self.root, 'T-0001', 'task')
        self.ingest(dict(EMPTY_EV, ci=True, ids=ids('T-0001', A)))
        self.ingest(dict(EMPTY_EV, ci=True, ids=ids('T-0001', A), reverts={A: C}))
        landing = self.machine(rel)['landing']
        self.assertEqual((landing['sha'], landing['by'], landing['reverts']), ('', 'reverted', A))
        self.assertIn(f'landing {A[:9]} reverted on the trunk', self.text(rel))
        before = self.text(rel)
        self.ingest(dict(EMPTY_EV, ci=True, ids=ids('T-0001', A), reverts={A: C}))
        self.assertEqual(self.text(rel), before)

    def test_a_close_on_a_reverted_sha_is_stamped_reverted(self):
        rel = write(self.root, 'T-0001', 'task')
        self.ingest(dict(EMPTY_EV, ci=True, ids=ids('T-0001', A), reverts={A: C}))
        self.assertEqual(self.machine(rel)['landing']['by'], 'reverted')

    def test_trunk_reverts_reads_the_revert_message_and_a_reverted_revert_restores(self):
        repo = tempfile.mkdtemp(prefix='landing_reverts_')
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)

        def git(*a):
            return subprocess.run(['git', *a], cwd=repo, capture_output=True, text=True,
                                  check=True).stdout.strip()
        git('init', '-q', '-b', 'main')
        git('config', 'user.email', 't@example.com')
        git('config', 'user.name', 't')
        shas = []
        for i in range(2):
            with open(os.path.join(repo, f'f{i}'), 'w') as f:
                f.write(str(i))
            git('add', '.')
            git('commit', '-q', '-m', f'feat: T-000{i + 1}')
            shas.append(git('rev-parse', 'HEAD'))
        git('revert', '--no-edit', shas[0])
        r1 = git('rev-parse', 'HEAD')
        git('revert', '--no-edit', shas[1])
        r2 = git('rev-parse', 'HEAD')
        git('revert', '--no-edit', r2)          # the second revert reverted: shas[1] stands again
        git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        product = types.SimpleNamespace(repo_dir=repo, main='main')
        reverts = evidence.trunk_reverts(product)
        self.assertEqual(reverts.get(shas[0]), r1)
        self.assertNotIn(shas[1], reverts)       # its revert was reverted: it stands
        self.assertEqual(reverts.get(r2), git('rev-parse', 'HEAD'))
        self.assertEqual(evidence.full_shas(product, [shas[0][:9], 'f' * 9]),
                         {shas[0][:9]: shas[0]})


class TypedLandedReconciles(_Record):
    """A typed ``landed:`` sha (§2.5) the trunk carries closes the Task by ``reconciled`` and is
    stamped ``console`` (F-0080, F-0106 C6/C7); one the trunk does not carry closes nothing."""

    def setUp(self):
        super().setUp()
        repo = tempfile.mkdtemp(prefix='landing_typed_')
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        # fixed dates: the same shas every run, so a sha's spelling never varies the outcome
        env = dict(os.environ, GIT_AUTHOR_DATE='2026-01-01T00:00:00Z',
                   GIT_COMMITTER_DATE='2026-01-01T00:00:00Z')

        def git(*a):
            return subprocess.run(['git', *a], cwd=repo, capture_output=True, text=True,
                                  check=True, env=env).stdout.strip()
        git('init', '-q', '-b', 'main')
        git('config', 'user.email', 't@example.com')
        git('config', 'user.name', 't')
        git('config', 'commit.gpgsign', 'false')
        git('commit', '-q', '--allow-empty', '-m', 'feat: the work, naming no id')
        self.on_trunk = git('rev-parse', 'HEAD')
        git('checkout', '-q', '-b', 'side')
        git('commit', '-q', '--allow-empty', '-m', 'wip: never merged')
        self.off_trunk = git('rev-parse', 'HEAD')
        git('checkout', '-q', 'main')
        self.product = types.SimpleNamespace(repo_dir=repo, main='main', conventions={})

    def run_ingest(self, ev):
        with contextlib.redirect_stdout(io.StringIO()):
            return ingest.ingest_into(self.root, ev, self.product)

    def test_a_typed_landed_sha_on_the_trunk_closes_reconciled_stamped_console(self):
        short = self.on_trunk[:9]
        rel = write(self.root, 'T-0001', 'task', typed=(f'landed: {short}',))
        self.assertTrue(check.LANDED_SHA_RE.fullmatch(short))
        self.assertEqual(self.run_ingest(dict(EMPTY_EV, main_sha=self.on_trunk)), 0)
        m = self.machine(rel)
        self.assertEqual(m['state'], 'Closed')
        self.assertIn('rule: reconciled', m['evidence'])
        self.assertEqual((m['landing']['sha'], m['landing']['by']), (self.on_trunk, 'console'))

    def test_a_typed_landed_sha_not_on_the_trunk_closes_nothing(self):
        rel = write(self.root, 'T-0001', 'task', typed=(f'landed: {self.off_trunk}',))
        self.assertEqual(self.run_ingest(dict(EMPTY_EV, main_sha=self.on_trunk)), 0)
        m = self.machine(rel)
        self.assertEqual(m['state'], 'New')
        self.assertNotIn('landing', m)
        self.assertIn(f'typed landed {self.off_trunk[:9]} is not on the trunk', m['evidence'])

    def test_a_typed_landed_sha_with_no_green_ci_after_it_closes_nothing(self):
        rel = write(self.root, 'T-0001', 'task', typed=(f'landed: {self.on_trunk}',))
        with mock.patch.object(ingest.evidence, 'ci_green_runs', return_value=[]):
            self.run_ingest(dict(EMPTY_EV, main_sha=self.on_trunk, ci=True))
        self.assertEqual(self.machine(rel)['state'], 'New')

    def test_with_no_trunk_read_a_typed_landed_sha_closes_nothing(self):
        rel = write(self.root, 'T-0001', 'task', typed=(f'landed: {self.on_trunk}',))
        self.run_ingest(dict(EMPTY_EV))
        self.assertEqual(self.machine(rel)['state'], 'New')


class ReopenClears(_Record):
    def test_reopen_clears_the_landing(self):
        rel = write(self.root, 'T-0001', 'task', machine=(
            'schema_version: 1', 'state: Closed',
            f'landing: {{sha: {A}, as_of: 2026-01-02T00:00:00Z, by: names}}',
            'evidence: [stale, "rule: landed-green"]',
            'stage_since: 2026-01-01T00:00:00Z', 'updated: 2026-01-01T00:00:00Z'))
        args = types.SimpleNamespace(id='T-0001', reason='a wrong close', product=None)
        with mock.patch.object(ingest.evidence, 'load', return_value=EMPTY_EV), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cmd_reopen(args, self.root), 0)
        m = self.machine(rel)
        self.assertEqual(m['state'], 'New')
        self.assertNotIn('landing', m)
        self.assertTrue(m['reopened'])


class CheckHoldsTheShape(_Record):
    GOOD = f'landing: {{sha: {A}, as_of: 2026-01-02T00:00:00Z, by: trunkclose/pr}}'

    def check(self, *machine, typed=()):
        """The findings ``asf check``'s card passes give one closed Task carrying ``machine``."""
        from asf.record.core import canonicalize, load_items
        write(self.root, 'T-0001', 'task', typed=typed, machine=(
            'schema_version: 1', 'state: Closed', *machine,
            'stage_since: 2026-01-01T00:00:00Z', 'updated: 2026-01-01T00:00:00Z'))
        by_id, errors = load_items(self.root)
        self.assertEqual(errors, [])
        canonical, _d = canonicalize(by_id)
        found = []
        add = lambda _rec, _line, msg: found.append(msg)   # noqa: E731
        check.check_residue(canonical, add, lambda _rec, _key: 1)
        check.check_landing(canonical, add, lambda _rec, _key: 1)
        return found

    def test_a_good_stamp_passes(self):
        self.assertEqual(self.check(self.GOOD), [])

    def test_a_migration_stamp_with_no_sha_passes(self):
        self.assertEqual(self.check('landing: {sha: "", as_of: 2026-01-02T00:00:00Z, '
                                    'by: migration, note: pre-I14}'), [])

    def test_each_bad_field_is_named(self):
        for line, want in (
                ('landing: {sha: abc1234, as_of: 2026-01-02T00:00:00Z, by: names}', 'landing.sha'),
                (f'landing: {{sha: {A}, as_of: yesterday, by: names}}', 'landing.as_of'),
                (f'landing: {{sha: {A}, as_of: 2026-01-02T00:00:00Z, by: hand}}', 'landing.by'),
                ('landing: {sha: "", as_of: 2026-01-02T00:00:00Z, by: reverted}',
                 'landing.reverts'),
                ('landing: abc', 'not a {sha, as_of, by} mapping')):
            with self.subTest(line=line):
                found = self.check(line)
                self.assertEqual(len(found), 1, found)
                self.assertIn(want, found[0])

    def test_a_bad_typed_landed_is_still_refused_beside_a_good_stamp(self):
        found = self.check(self.GOOD, typed=('landed: nope',))
        self.assertEqual(len(found), 1, found)
        self.assertIn("landed: 'nope' is not a 7-40 character hex sha", found[0])

    def test_cmd_check_runs_the_landing_pass(self):
        self.check('landing: {sha: abc1234, as_of: 2026-01-02T00:00:00Z, by: names}')
        do_index(self.root)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            check.cmd_check(types.SimpleNamespace(paths=None), self.root)
        self.assertIn("landing.sha 'abc1234'", buf.getvalue())

    def test_landing_by_set_is_the_ingests(self):
        for by in ingest.LANDING_BY:
            self.assertEqual(check.landing_problems(
                {'sha': A, 'as_of': '2026-01-02T00:00:00Z', 'by': by, 'reverts': A}), [])


class LandingBy(unittest.TestCase):
    def test_the_groom_covers_rule_and_each_arm(self):
        self.assertEqual(policy.landing_by(''), '')
        self.assertEqual(policy.landing_by(f'{policy.COVERS_ACCEPT}: x'), 'groom')
        for arm in ('names', 'pr', 'covers'):
            self.assertEqual(policy.landing_by('verified', arm), f'trunkclose/{arm}')
        self.assertEqual(policy.landing_by('verified', ''), 'trunkclose/covers')
        self.assertEqual(policy.landing_by('verified', 'bogus'), 'trunkclose/covers')


class ApprovalsCloseLandedNamesTheStamp(unittest.TestCase):
    def test_the_resolution_carries_the_landing(self):
        from asf import approvals
        written, lines = [], []
        stamp = {'sha': A, 'as_of': '2026-01-02T00:00:00Z', 'by': 'pr-merge'}
        with mock.patch.object(approvals, 'open_holds',
                               return_value=[{'item': 'T-0001', 'class': 'touch_amendable_set'}]), \
                mock.patch.object(approvals, 'append', lambda _p, e: written.append(e)):
            done = approvals.close_landed(None, {'T-0001': {'state': 'Closed', 'landing': stamp}},
                                          lines.append)
        self.assertEqual(done, ['T-0001/touch_amendable_set'])
        self.assertEqual(written[0]['landing'], stamp)
        self.assertIn(f'(landing {A[:9]}, by pr-merge)', lines[0])


class PinnedReaderLoadsAStampedCard(unittest.TestCase):
    """The pinned venv's loader parses a stamped card and its ``check_residue`` ignores the key."""

    CODE = r'''
import os, sys, tempfile
from asf.record import frontmatter, check
from asf.record.core import load_items, canonicalize
root = tempfile.mkdtemp()
for d in ("epics", "features", "stories", "tasks", "bugs", "decisions", "rules"):
    os.makedirs(os.path.join(root, d))
with open(os.path.join(root, "tasks", "T-0001.md"), "w") as f:
    f.write(sys.stdin.read())
by_id, errors = load_items(root)
assert not errors, errors
canonical, _d = canonicalize(by_id)
landing = frontmatter.split_machine(canonical["T-0001"]["meta"])[1]["landing"]
found = []
check.check_residue(canonical, lambda rec, line, msg: found.append(msg), lambda rec, key: 1)
print("OK", landing["sha"], landing["by"], len(found))
'''

    def test_the_pinned_reader(self):
        root = make_record()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        rel = write(root, 'T-0001', 'task', machine=(
            'schema_version: 1', 'state: Closed',
            f'landing: {{sha: {A}, as_of: 2026-01-02T00:00:00Z, by: trunkclose/covers}}',
            'evidence: ["commit aaaaaaa names T-0001", "rule: landed-green"]',
            'stage_since: 2026-01-01T00:00:00Z', 'updated: 2026-01-01T00:00:00Z'))
        with open(os.path.join(root, rel), encoding='utf-8') as f:
            text = f.read()
        for sha in pinned.pinned_shas():
            with self.subTest(sha=sha):
                r = pinned.run_pinned(sha, self.CODE, stdin=text)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout.split(), ['OK', A, 'trunkclose/covers', '0'])


if __name__ == '__main__':
    unittest.main()
