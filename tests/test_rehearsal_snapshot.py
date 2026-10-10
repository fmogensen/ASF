"""tests.test_rehearsal_snapshot — asf.rehearsal's builder half: filler, build and
manifest_holds, against a synthetic source this module writes itself. No test here reaches a
real record or the network (S-79604's own acceptance says so)."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import env, rehearsal
from tests import gitfixture

DESCRIPTION = """## Description
{body}

## Acceptance
- [ ] something passes

## History
- 2026-01-01: created
- 2026-01-02: groom: priority → P1, see {ref}

## Children

## Backlinks
"""

#: a ``done/`` header of the shape :func:`asf.groom.inbox.move_to_done` writes — free prose, with
#: an operator-ish name and a filesystem path in it. Neither may reach the snapshot; the ``→`` and
#: the id must. (Stand-in values: no real account name or machine path is spelled in this tree.)
PROSE_HEADER = '→ closed (groom 2026-02-03, lane-1 via /opt/elsewhere/notes.md)'

#: a ``removed:`` reason of the shape :func:`asf.record.setfield._set_only` accepts. The prose must
#: not reach the snapshot; the task id :mod:`asf.record.ingest` reads back out of it must.
REMOVED_REASON = 'merged into T-00002 after lane-1 found the duplicate'

#: folder -> (type, prefix, digit width). Three prefixes five digits wide, four kept at four —
#: the same shape the plan's own sizing table reads off the real record.
_SHAPES = {
    'epics': ('epic', 'E', 4),
    'features': ('feature', 'F', 4),
    'stories': ('story', 'S', 5),
    'tasks': ('task', 'T', 5),
    'bugs': ('bug', 'B', 5),
    'decisions': ('decision', 'D', 4),
    'rules': ('rule', 'R', 4),
}


def _write_card(record, folder, iid, type_, title, body, extra=''):
    os.makedirs(os.path.join(record, folder), exist_ok=True)
    text = (
        '---\n'
        f'id: {iid}\n'
        f'type: {type_}\n'
        f'title: {title}\n'
        '# ---- machine ----\n'
        'schema_version: 1\n'
        'state: New\n'
        f'{extra}'
        'stage_since: 2026-01-01T09:00:00Z\n'
        'updated: 2026-01-01T09:00:00Z\n'
        '---\n'
        f'{body}'
    )
    with open(os.path.join(record, folder, f'{iid}.md'), 'w', encoding='utf-8') as f:
        f.write(text)


def _tree_digest(root, skip_git=False):
    """``{relpath: sha256-of-content}`` for every file under ``root`` — the shape a mutation
    check needs, since a path listing alone cannot see a file rewritten in place."""
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        if skip_git and '.git' in os.path.relpath(dirpath, root).split(os.sep):
            continue
        for name in files:
            path = os.path.join(dirpath, name)
            with open(path, 'rb') as f:
                out[os.path.relpath(path, root)] = hashlib.sha256(f.read()).hexdigest()
    return out


def _build_source(root, n_per_type=3, tags=True):
    """A from-scratch synthetic record + repo of real *shape*: every item type, a five-digit id
    for three prefixes, intake in its three states, two tags (one annotated, one a pre-release).
    ``tags=False`` leaves the repo tag-less, the shape that drives the builder's synthetic-ref
    fallback. Returns ``(record_dir, repo_dir, {folder: [ids]})``."""
    record = os.path.join(root, 'record')
    repo = os.path.join(root, 'repo')
    os.makedirs(record, exist_ok=True)
    os.makedirs(repo, exist_ok=True)

    ids = {}
    task_ref = f"T-{1:05d}"
    for folder, (type_, prefix, width) in _SHAPES.items():
        ids[folder] = []
        for i in range(1, n_per_type + 1):
            iid = f"{prefix}-{i:0{width}d}"
            ids[folder].append(iid)
            body = DESCRIPTION.format(body='a card body of some real length here ' * 3,
                                       ref=task_ref)
            # one card carries a free-text `removed:` reason — the shape setfield accepts and
            # ingest reads a task id back out of
            extra = f'removed: {REMOVED_REASON}\n' if (folder, i) == ('tasks', 1) else ''
            _write_card(record, folder, iid, type_, 'a representative title here', body,
                        extra=extra)

    os.makedirs(os.path.join(record, 'inbox', 'done'), exist_ok=True)
    with open(os.path.join(record, 'inbox', 'open-1.md'), 'w', encoding='utf-8') as f:
        f.write('an open note\nwith two lines of text')
    with open(os.path.join(record, 'inbox', 'done', 'done-1.md'), 'w', encoding='utf-8') as f:
        f.write(f"→ {task_ref}\n\na groomed note, already typed into a card")
    # the second done note's header is what `move_to_done` actually writes: free prose, with an
    # account name and a machine path in it (asf/groom/inbox.py)
    with open(os.path.join(record, 'inbox', 'done', 'done-2.md'), 'w', encoding='utf-8') as f:
        f.write(f"{PROSE_HEADER}\n\na second groomed note, closed by hand")

    subprocess.run(['git', 'init', '-q', repo], check=True)
    gitfixture.identity(repo, 'ci', 'ci@localhost')
    with open(os.path.join(repo, 'README.md'), 'w', encoding='utf-8') as f:
        f.write('placeholder\n')
    subprocess.run(['git', '-C', repo, 'add', '.'], check=True)
    subprocess.run(['git', '-C', repo, 'commit', '-q', '-m', 'first'], check=True)
    if tags:
        subprocess.run(['git', '-C', repo, 'tag', '-a', 'v0.1.0', '-m',
                        'release notes of some length'], check=True)
        subprocess.run(['git', '-C', repo, 'tag', 'v0.1.1-rc1'], check=True)
    return record, repo, ids


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMMITTED_SNAPSHOT = os.path.join(REPO_ROOT, 'tests', 'rehearsal')


class SnapshotCommittedAndComplete(unittest.TestCase):
    """Acceptance line 1: tests/rehearsal/ is committed with manifest.json, record/ and repo/,
    and the manifest carries its seven required keys — read off the actual committed fixture,
    not a tempfile build."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(COMMITTED_SNAPSHOT, 'manifest.json'), encoding='utf-8') as f:
            cls.manifest = json.load(f)

    def test_files_exist(self):
        self.assertTrue(os.path.isfile(os.path.join(COMMITTED_SNAPSHOT, 'manifest.json')))
        self.assertTrue(os.path.isdir(os.path.join(COMMITTED_SNAPSHOT, 'record')))
        self.assertTrue(os.path.isdir(os.path.join(COMMITTED_SNAPSHOT, 'repo')))

    def test_manifest_keys(self):
        for k in ('cards', 'widest_id', 'refs', 'intake', 'built_at', 'asf_version', 'source_sha'):
            self.assertIn(k, self.manifest, k)


class ManifestClaimsFiveDigitIdAndAnnotatedTag(unittest.TestCase):
    """Acceptance line 2, read off the committed fixture's own manifest.json."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(COMMITTED_SNAPSHOT, 'manifest.json'), encoding='utf-8') as f:
            cls.manifest = json.load(f)

    def test_widest_id_has_a_five_digit_prefix_by_digit_run(self):
        # PD10: the spec's own `len(str(v)) >= 5` fence is weak (a four-digit id's string is
        # already 6 characters, "T-0001"), so the real claim is checked on the digit run here.
        digit_runs = [len(v.split('-', 1)[1]) for v in self.manifest['widest_id'].values()]
        self.assertGreaterEqual(max(digit_runs), 5, self.manifest['widest_id'])

    def test_at_least_one_annotated_ref(self):
        self.assertTrue(any(r.get('annotated') for r in self.manifest['refs']), self.manifest['refs'])

    def test_a_prerelease_ref_is_carried(self):
        self.assertTrue(any(not r.get('annotated') for r in self.manifest['refs']),
                         self.manifest['refs'])


class IntakeThreeStates(unittest.TestCase):
    """Acceptance line 5, read off the committed fixture."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(COMMITTED_SNAPSHOT, 'manifest.json'), encoding='utf-8') as f:
            cls.manifest = json.load(f)

    def test_intake_three_states_present(self):
        intake = self.manifest['intake']
        self.assertTrue(intake['open'])
        self.assertGreaterEqual(len(intake['done']), 2)
        self.assertIn('mismatch', intake)
        self.assertIn(intake['mismatch']['name'], intake['done'])

    def test_intake_done_header_kept_text_cleared(self):
        name = self.manifest['intake']['done'][0]
        intake_dir = self.manifest['intake_dir']
        path = os.path.join(COMMITTED_SNAPSHOT, 'record', intake_dir, 'done', name)
        with open(path, encoding='utf-8') as f:
            text = f.read()
        self.assertTrue(text.startswith('→ '))
        self.assertNotIn('groomed note', text)


class BuilderDoesNotMutateSource(unittest.TestCase):
    """Acceptance line 4: asf.rehearsal.build reads record_dir and repo_dir and never writes to
    either."""

    def test_build_never_writes_to_the_source_record_or_repo(self):
        # content digests, not a path listing: a source file rewritten in place leaves the set
        # of paths untouched, so only {relpath: sha256} can see it.
        root = tempfile.mkdtemp(prefix='rehearsal_test_')
        self.addCleanup(shutil.rmtree, root, True)
        record, repo, _ids = _build_source(root)
        record_before = _tree_digest(record)
        repo_before = _tree_digest(repo, skip_git=True)
        rehearsal.build(record, repo, os.path.join(root, 'snapshot'))
        self.assertEqual(record_before, _tree_digest(record))
        self.assertEqual(repo_before, _tree_digest(repo, skip_git=True))


class BuildWritesTheSnapshot(unittest.TestCase):
    """Builder behavior not named by the card's own Acceptance list — still proved here, against
    a synthetic tempfile build, alongside the acceptance-named classes above."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='rehearsal_test_')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.record, self.repo, self.ids = _build_source(self.root)
        self.out = os.path.join(self.root, 'snapshot')
        self.manifest = rehearsal.build(self.record, self.repo, self.out)

    def test_ids_kept_verbatim(self):
        for folder, ids in self.ids.items():
            for iid in ids:
                path = os.path.join(self.out, 'record', folder, f'{iid}.md')
                self.assertTrue(os.path.isfile(path), path)
                with open(path, encoding='utf-8') as f:
                    text = f.read()
                self.assertIn(f'id: {iid}', text)

    def test_title_is_cleared(self):
        iid = self.ids['tasks'][0]
        path = os.path.join(self.out, 'record', 'tasks', f'{iid}.md')
        with open(path, encoding='utf-8') as f:
            text = f.read()
        self.assertNotIn('a representative title here', text)

    def test_description_body_is_cleared(self):
        iid = self.ids['tasks'][0]
        path = os.path.join(self.out, 'record', 'tasks', f'{iid}.md')
        with open(path, encoding='utf-8') as f:
            text = f.read()
        self.assertNotIn('a card body of some real length here', text)

    def test_history_date_and_verb_kept_prose_cleared(self):
        iid = self.ids['tasks'][0]
        path = os.path.join(self.out, 'record', 'tasks', f'{iid}.md')
        with open(path, encoding='utf-8') as f:
            text = f.read()
        self.assertIn('- 2026-01-01: created', text)
        self.assertIn('- 2026-01-02: groom:', text)
        self.assertNotIn('priority', text)

    def test_history_id_reference_kept(self):
        iid = self.ids['tasks'][0]
        path = os.path.join(self.out, 'record', 'tasks', f'{iid}.md')
        with open(path, encoding='utf-8') as f:
            text = f.read()
        self.assertIn(iid, text)

    def test_manifest_holds_on_the_just_built_snapshot(self):
        self.assertEqual(rehearsal.manifest_holds(self.out), [])

    def test_a_done_headers_free_prose_is_cleared_and_its_arrow_and_id_kept(self):
        path = os.path.join(self.out, 'record', 'inbox', 'done', 'done-2.md')
        with open(path, encoding='utf-8') as f:
            header = f.read().split('\n\n', 1)[0]
        self.assertNotEqual(header, PROSE_HEADER)
        self.assertEqual(len(header), len(PROSE_HEADER))
        self.assertTrue(header.startswith('→ '), header)
        for leaked in ('closed', 'groom', '2026-02-03', 'lane-1', '/opt/elsewhere/notes.md'):
            self.assertNotIn(leaked, header, header)

    def test_a_removed_reasons_prose_is_cleared_and_its_id_kept(self):
        iid = self.ids['tasks'][0]
        path = os.path.join(self.out, 'record', 'tasks', f'{iid}.md')
        with open(path, encoding='utf-8') as f:
            text = f.read()
        line = next(l for l in text.split('\n') if l.startswith('removed:'))
        value = line[len('removed:'):].strip()
        self.assertEqual(len(value), len(REMOVED_REASON))
        self.assertIn('T-00002', value)           # ingest still reads its id back out
        for leaked in ('merged into', 'duplicate', 'lane-1'):
            self.assertNotIn(leaked, value, value)


class CommittedSnapshotIsScannedClean(unittest.TestCase):
    """The real, tracked ``tests/rehearsal/`` as committed on this branch — scanned over the
    actual repo root, the same way the Task's Gate runs it (S-79604's sixth line, P17)."""

    def _run(self, script):
        path = os.path.join(REPO_ROOT, 'tools', script)
        if not os.path.exists(path):
            self.skipTest(f'{script} not present on this checkout')
        return subprocess.run(['bash', path], cwd=REPO_ROOT, capture_output=True, text=True)

    def test_check_generic_is_clean(self):
        r = self._run('check_generic.sh')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_check_privacy_is_clean(self):
        r = self._run('check_privacy.sh')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class ManifestHoldsDetectsBreakage(unittest.TestCase):
    """Acceptance line 3: manifest_holds returns [] on the committed snapshot, and one line per
    broken claim when a card is removed, an id is narrowed, or a tag's annotated flag flips."""

    def test_manifest_holds_on_the_committed_snapshot(self):
        self.assertEqual(rehearsal.manifest_holds(COMMITTED_SNAPSHOT), [])

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='rehearsal_test_')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.record, self.repo, self.ids = _build_source(self.root)
        self.out = os.path.join(self.root, 'snapshot')
        rehearsal.build(self.record, self.repo, self.out)

    def test_a_card_removed_is_a_red_line(self):
        iid = self.ids['tasks'][0]
        os.remove(os.path.join(self.out, 'record', 'tasks', f'{iid}.md'))
        problems = rehearsal.manifest_holds(self.out)
        self.assertTrue(any('cards' in p for p in problems), problems)

    def test_an_id_narrowed_to_four_digits_is_a_red_line(self):
        iid = self.ids['tasks'][0]  # a five-digit task id
        path = os.path.join(self.out, 'record', 'tasks', f'{iid}.md')
        with open(path, encoding='utf-8') as f:
            text = f.read()
        narrowed = iid[:-1]  # drop one digit: five digits -> four
        text = text.replace(f'id: {iid}', f'id: {narrowed}', 1)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        problems = rehearsal.manifest_holds(self.out)
        self.assertTrue(any('widest_id' in p for p in problems), problems)

    def test_a_tag_annotated_flag_flipped_is_a_red_line(self):
        manifest_path = os.path.join(self.out, 'manifest.json')
        with open(manifest_path, encoding='utf-8') as f:
            manifest = json.load(f)
        flipped = False
        for ref in manifest['refs']:
            if ref['annotated']:
                ref['annotated'] = False
                flipped = True
                break
        self.assertTrue(flipped, manifest['refs'])
        with open(manifest_path, 'w', encoding='utf-8') as f:
            json.dump(manifest, f)
        problems = rehearsal.manifest_holds(self.out)
        self.assertTrue(any('refs' in p for p in problems), problems)

    def test_annotated_flag_flipped_in_both_files_is_still_a_red_line(self):
        # the same flip, made consistently in manifest.json's `refs` list and the independent
        # repo/refs.json it is cross-checked against — the `refs` comparison alone (C8) can't
        # catch this, since the two lists still agree with each other; only the separate
        # refs_total/refs_annotated scalar claims, recorded at build time, catch it.
        manifest_path = os.path.join(self.out, 'manifest.json')
        refs_path = os.path.join(self.out, 'repo', 'refs.json')
        with open(manifest_path, encoding='utf-8') as f:
            manifest = json.load(f)
        with open(refs_path, encoding='utf-8') as f:
            independent_refs = json.load(f)
        flipped = False
        for ref, iref in zip(manifest['refs'], independent_refs):
            if ref['annotated']:
                ref['annotated'] = False
                iref['annotated'] = False
                flipped = True
                break
        self.assertTrue(flipped, manifest['refs'])
        with open(manifest_path, 'w', encoding='utf-8') as f:
            json.dump(manifest, f)
        with open(refs_path, 'w', encoding='utf-8') as f:
            json.dump(independent_refs, f)
        problems = rehearsal.manifest_holds(self.out)
        self.assertTrue(any('refs_annotated' in p for p in problems), problems)


class CmdRehearseBuildFromProduct(unittest.TestCase):
    """``asf rehearse --build --from-product <p>`` — S-79604's fourth line, driven through the
    actual CLI entry point rather than calling :func:`asf.rehearsal.build` directly."""

    def test_writes_the_snapshot_and_leaves_the_source_record_unchanged(self):
        root = tempfile.mkdtemp(prefix='rehearsal_test_')
        self.addCleanup(shutil.rmtree, root, True)
        record, repo, _ids = _build_source(root)
        before = sorted(os.path.join(dp, n) for dp, _d, fs in os.walk(record) for n in fs)
        product = env.Product('demo', {'backlog_dir': record, 'repo_dir': repo})
        args = argparse.Namespace(snapshot=os.path.join(root, 'snap'), build=True,
                                   from_product='demo')
        with mock.patch('asf.env.load_product', return_value=product):
            rc = rehearsal.cmd_rehearse(args, root)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(os.path.join(args.snapshot, 'manifest.json')))
        after = sorted(os.path.join(dp, n) for dp, _d, fs in os.walk(record) for n in fs)
        self.assertEqual(before, after)


class BuilderIsDeterministic(unittest.TestCase):
    def _assert_identical(self, m1, m2, out1, out2):
        # built_at is the one key that moves between two builds of the same source
        self.assertEqual(dict(m1, built_at=None), dict(m2, built_at=None))
        d1 = _tree_digest(out1)
        d2 = _tree_digest(out2)
        d1.pop('manifest.json', None)   # built_at differs; covered by the dict compare above
        d2.pop('manifest.json', None)
        self.assertEqual(d1, d2)

    def test_two_builds_from_one_source_are_byte_identical(self):
        root = tempfile.mkdtemp(prefix='rehearsal_test_')
        self.addCleanup(shutil.rmtree, root, True)
        record, repo, _ids = _build_source(root)
        out1 = os.path.join(root, 'out1')
        out2 = os.path.join(root, 'out2')
        self._assert_identical(rehearsal.build(record, repo, out1),
                                rehearsal.build(record, repo, out2), out1, out2)

    def test_two_builds_from_a_tag_less_source_are_byte_identical(self):
        # a tag-less source takes the synthetic-ref fallback. That entry's date must not be
        # today(), or a rebuild of the same source on a later day writes different bytes — so
        # the two builds here run under two different today() values and must still agree.
        root = tempfile.mkdtemp(prefix='rehearsal_test_')
        self.addCleanup(shutil.rmtree, root, True)
        record, repo, _ids = _build_source(root, tags=False)
        out1 = os.path.join(root, 'out1')
        out2 = os.path.join(root, 'out2')
        with mock.patch('asf.record.core.today', return_value='2026-01-01'):
            m1 = rehearsal.build(record, repo, out1)
        with mock.patch('asf.record.core.today', return_value='2031-12-31'):
            m2 = rehearsal.build(record, repo, out2)
        self.assertEqual([r['date'] for r in m1['refs']], [''], m1['refs'])
        self._assert_identical(m1, m2, out1, out2)


if __name__ == '__main__':
    unittest.main()
