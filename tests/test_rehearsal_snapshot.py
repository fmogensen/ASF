"""tests.test_rehearsal_snapshot — asf.rehearsal's builder half: filler, build and
manifest_holds, against a synthetic source this module writes itself. No test here reaches a
real record or the network (S-79604's own acceptance says so)."""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import env, rehearsal
from asf.record import core, frontmatter, staged_guard
from tests import gitfixture

DESCRIPTION = """## Description
{body}

## Acceptance
- [ ] something passes

## History
- 2026-01-01: created
- 2026-01-02: groom: priority → P1, see {ref}
{timestamped}
{continuation}

## Children

## Backlinks
"""

#: a ``## History`` entry of the *timestamped* shape the record writes beside the template's own
#: ``- <date>: created`` — :func:`asf.record.reopen` stamps ``- <date> <HH:MM> <verb>: …``, and
#: ``asf set`` and groom write ``- <date> <verb>: …``. The clearer owes this shape the same
#: treatment as the colon shape: the date and the verb stay, the prose after them goes.
HISTORY_TIMESTAMPED_PROSE = 'the rehearsal found it filed by hand and still open'
HISTORY_TIMESTAMPED = f'- 2026-01-03 09:04 reopen: {HISTORY_TIMESTAMPED_PROSE} — state Done'

#: a ``## History`` line that does not open an entry: the wrapped tail of the one above it, and
#: free prose like the entry itself. It must not reach the snapshot as written.
HISTORY_CONTINUATION = '  wrapped on from the line above, free prose and not a new entry'

#: a ``done/`` header of the shape :func:`asf.groom.inbox.move_to_done` writes — free prose, with
#: an operator-ish name and a filesystem path in it. Neither may reach the snapshot; the ``→`` and
#: the id must. (Stand-in values: no real account name or machine path is spelled in this tree.)
PROSE_HEADER = '→ closed (groom 2026-02-03, lane-1 via /opt/elsewhere/notes.md)'

#: a ``## `` heading a person wrote, not one of the record template's own
#: (``asf/record/new.py:207-214``). The heading *line* is free prose like any other: a real
#: record's hand-written headings name a date, a pull request number and a product, so a heading
#: kept verbatim merely because it opens with ``## `` carries all three into the snapshot.
HAND_HEADING_PROSE = 'Source: the thread of 2026-02-05 (PR #1234), read by hand'
HAND_HEADING = f'## {HAND_HEADING_PROSE}'
HAND_SECTION_BODY = 'the prose filed under a hand-written heading, cleared like any other body'

#: a ``removed:`` reason of the shape :func:`asf.record.setfield._set_only` accepts. The prose must
#: not reach the snapshot; the task id :mod:`asf.record.ingest` reads back out of it must.
REMOVED_REASON = 'merged into T-00002 after lane-1 found the duplicate'

#: the prose-bearing typed fields a real card carries beyond its title — none of them on the
#: builder's structural allowlist, every one of them free text a person or a groom wrote:
#: a Task's ``reshape:`` (:data:`asf.tick.rejudge.RESHAPE_KIND`), a Decision's ``decided_by:``
#: and a Rule's ``reason:``/``check:`` (``asf/record/new.py:21-22``), plus an ``area:`` label.
RESHAPE = 'split T-00003 | T-00004 (groom 2026-02-04)'
DECIDED_BY = 'lane-1, after the second rehearsal of the week'
RULE_REASON = 'a snapshot that carries real prose is not a fixture but a leak'
RULE_CHECK = 'the tracked snapshot is scanned by this product own privacy scan'
AREA = 'the rehearsal lane'
LINK_LABEL = 'the thread this was agreed in filed by hand'

#: every prose value above, the shape a leak check reads: none of these may appear in the
#: snapshot, whole or in part.
PROSE_VALUES = (RESHAPE, DECIDED_BY, RULE_REASON, RULE_CHECK, AREA, LINK_LABEL, REMOVED_REASON,
                HISTORY_CONTINUATION.strip(), PROSE_HEADER, HISTORY_TIMESTAMPED_PROSE,
                HAND_HEADING_PROSE, HAND_SECTION_BODY)

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

#: folder -> the parent id a card of that folder points at (``parent:`` is structural: kept).
_PARENTS = {
    'features': 'E-0001', 'stories': 'F-0001', 'tasks': 'S-00001', 'bugs': 'F-0001',
    'decisions': 'E-0001', 'rules': 'E-0001',
}

#: the per-folder card counts the committed ``tests/rehearsal/`` is built at — a record of real
#: shape (185 cards: a handful of Epics, many Tasks), kept here so the committed fixture is
#: regenerable from this module alone for as long as it stands on a synthetic stand-in source.
#: The operator refresh (``asf rehearse --build --from-product <p>``) replaces it with the real
#: record's own shape; nothing here pins the fixture's counts, only this generator's.
FIXTURE_COUNTS = {'epics': 6, 'features': 24, 'stories': 22, 'tasks': 64, 'bugs': 48,
                  'decisions': 10, 'rules': 11}


def _write_card(record, folder, iid, type_, title, body, typed='', machine='', state='New'):
    """One card file: ``typed`` lines go in the typed block (where a person's own fields live),
    ``machine`` lines after the marker."""
    os.makedirs(os.path.join(record, folder), exist_ok=True)
    text = (
        '---\n'
        f'id: {iid}\n'
        f'type: {type_}\n'
        f'title: {title}\n'
        f'{typed}'
        '# ---- machine ----\n'
        'schema_version: 1\n'
        f'state: {state}\n'
        f'{machine}'
        'stage_since: 2026-01-01T09:00:00Z\n'
        'updated: 2026-01-01T09:00:00Z\n'
        '---\n'
        f'{body}'
    )
    with open(os.path.join(record, folder, f'{iid}.md'), 'w', encoding='utf-8') as f:
        f.write(text)


def _card_fields(folder, index):
    """``(typed, machine)`` frontmatter lines for one card — a mix of the structural fields the
    snapshot keeps verbatim (``parent``, ``rank``, ``writes``, ``after``, ``stories``,
    ``priority``, ``severity``, ``lane``, ``size``, a Decision's ISO ``date``) and the prose
    fields it has to clear (``reshape``, ``decided_by``, a Rule's ``reason``/``check``,
    ``area``, ``links``, and ``removed``, whose id must survive its prose)."""
    typed = []
    machine = []
    if folder in _PARENTS:
        typed.append(f'parent: {_PARENTS[folder]}\n')
    typed.append(f'rank: {100 + index}\n')
    typed.append(f'priority: P{1 + index % 3}\n')
    if folder == 'tasks':
        typed.append('writes: [asf/rehearsal.py, tests/test_rehearsal_snapshot.py]\n')
        typed.append('after: [T-00002]\n')
        # the Task→Story edge `asf check` resolves and `ingest` reads back: structural, so it
        # is kept verbatim and not cleared to filler
        typed.append('stories: [S-00001, S-00002]\n')
        if index == 1:
            typed.append(f'reshape: {RESHAPE}\n')
            machine.append(f'removed: {REMOVED_REASON}\n')
    if folder == 'bugs':
        typed.append(f'severity: S{1 + index % 3}\n')
    if folder == 'features':
        typed.append('lane: full\nsize: m\n')
    if folder == 'decisions':
        typed.append(f'decided_by: {DECIDED_BY}\ndate: 2026-01-05\n')
    if folder == 'rules':
        typed.append(f'scope: repo\nenforced: true\nreason: {RULE_REASON}\ncheck: {RULE_CHECK}\n')
    if folder == 'stories':
        typed.append(f'area: {AREA}\n')
    if index == 1:
        typed.append(f'links: {{note: {LINK_LABEL}}}\n')
    return ''.join(typed), ''.join(machine)


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


#: the one shape a generated intake note name may have (``asf/rehearsal.py:_build_intake``).
INTAKE_NAME_RE = re.compile(r'^(open|done)-note-\d+\.md$')


def _uncleared(text):
    """The characters of ``text`` that no clearing could have left behind: filler is ASCII
    lowercase words and spaces, an id token is kept verbatim, and
    :func:`asf.rehearsal._clear_gap` keeps the run of non-word characters at a gap's edges. An
    uppercase letter, a digit or an underscore outside an id token is prose copied through."""
    rest = core.ID_TOKEN_RE.sub('', text)
    return [c for c in rest
            if (c.isalnum() or c == '_') and not (c.isascii() and c.isalpha() and c.islower())]


def _strings(value):
    """Every string inside a frontmatter value — the scalar itself, or a list's or dict's."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [x for v in value for x in _strings(v)]
    if isinstance(value, dict):
        return [x for v in value.values() for x in _strings(v)]
    return []


def _history_lines(body):
    """The ``## History`` lines of ``body`` that do not open an entry and are not blank — the
    continuation lines, free prose the clearer owes the same treatment as the entry above."""
    _preamble, sections = core.parse_sections(body)
    out = []
    for heading, content in sections:
        if heading.strip() != '## History':
            continue
        for line in content.split('\n'):
            if line.strip() and not line.lstrip().startswith('- '):
                out.append(line)
    return out


def _build_source(root, n_per_type=3, tags=True, counts=None):
    """A from-scratch synthetic record + repo of real *shape*: every item type, a five-digit id
    for three prefixes, each card carrying both structural and prose-bearing typed fields and a
    ``## History`` with both entry shapes and a continuation line, one card with a hand-written
    section heading, intake in its three states, three tags (two annotated, one a lightweight
    pre-release). ``counts`` gives a per-folder card count where
    the flat ``n_per_type`` will not do (:data:`FIXTURE_COUNTS`); ``tags=False`` leaves the repo
    tag-less, the shape that drives the builder's synthetic-ref fallback. Returns
    ``(record_dir, repo_dir, {folder: [ids]})``.

    Nothing real is in here: every prose value is a stand-in this module spells out, and no
    account name or machine path of any host is among them."""
    record = os.path.join(root, 'record')
    repo = os.path.join(root, 'repo')
    os.makedirs(record, exist_ok=True)
    os.makedirs(repo, exist_ok=True)

    ids = {}
    task_ref = f"T-{1:05d}"
    for folder, (type_, prefix, width) in _SHAPES.items():
        ids[folder] = []
        how_many = (counts or {}).get(folder, n_per_type)
        for i in range(1, how_many + 1):
            iid = f"{prefix}-{i:0{width}d}"
            ids[folder].append(iid)
            # body and title lengths vary with the index: the filler is seeded by the length of
            # what it replaces, so a record of one length everywhere proves nothing
            body = DESCRIPTION.format(body='a card body of some real length here ' * (2 + i % 3),
                                       ref=task_ref, timestamped=HISTORY_TIMESTAMPED,
                                       continuation=HISTORY_CONTINUATION)
            if folder == 'tasks' and i == 2:
                # one card carrying a section heading a person typed, which is not one of the
                # template's own and may not be copied through
                body += f"\n{HAND_HEADING}\n\n{HAND_SECTION_BODY}\n"
            typed, machine = _card_fields(folder, i)
            _write_card(record, folder, iid, type_, 'a representative title here' + ' x' * (i % 4),
                        body, typed=typed, machine=machine,
                        state=('New', 'Active', 'Done')[i % 3])

    os.makedirs(os.path.join(record, 'inbox', 'done'), exist_ok=True)
    with open(os.path.join(record, 'inbox', 'open-1.md'), 'w', encoding='utf-8') as f:
        f.write('an open note\nwith two lines of text')
    with open(os.path.join(record, 'inbox', 'a-second-filed-note.md'), 'w',
              encoding='utf-8') as f:
        f.write('a second open note, filed under a name made from its own title')
    with open(os.path.join(record, 'inbox', 'done', 'done-1.md'), 'w', encoding='utf-8') as f:
        f.write(f"→ {task_ref}\n\na groomed note, already typed into a card")
    # the second done note's header is what `move_to_done` actually writes: free prose, with an
    # account name and a machine path in it (asf/groom/inbox.py)
    with open(os.path.join(record, 'inbox', 'done', 'done-2.md'), 'w', encoding='utf-8') as f:
        f.write(f"{PROSE_HEADER}\n\na second groomed note, closed by hand")
    with open(os.path.join(record, 'inbox', 'done', 'done-3.md'), 'w', encoding='utf-8') as f:
        f.write(f"→ {task_ref} (a third note, done)\n\na third groomed note, longer than the "
                 "two above it so the cleared copies differ in length too")

    subprocess.run(['git', 'init', '-q', repo], check=True)
    gitfixture.identity(repo, 'ci', 'ci@localhost')
    with open(os.path.join(repo, 'README.md'), 'w', encoding='utf-8') as f:
        f.write('placeholder\n')
    subprocess.run(['git', '-C', repo, 'add', '.'], check=True)
    subprocess.run(['git', '-C', repo, 'commit', '-q', '-m', 'first'], check=True)
    if tags:
        # two annotated tags, so flipping one `annotated` flag leaves the other standing — the
        # shape the annotated *count* claim is there to catch
        subprocess.run(['git', '-C', repo, 'tag', '-a', 'v0.1.0', '-m',
                        'release notes of some length'], check=True)
        subprocess.run(['git', '-C', repo, 'tag', '-a', 'v0.1.1', '-m',
                        'a second release, with notes of its own'], check=True)
        subprocess.run(['git', '-C', repo, 'tag', 'v0.1.2-rc1'], check=True)
    return record, repo, ids


def build_fixture_source(root):
    """The synthetic stand-in the committed ``tests/rehearsal/`` stands on: this module's own
    generator at the fixture's card counts. Public on purpose — it is what a refresh of the
    committed fixture runs against until the operator console rebuilds it from a real product
    (see ``tests/rehearsal/README.md``). Those two lines are::

        record, repo, _ids = build_fixture_source(tempfile.mkdtemp())
        rehearsal.build(record, repo, 'tests/rehearsal')
    """
    return _build_source(root, counts=FIXTURE_COUNTS)


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
        # the third state is a *pair*: an open note and the done/ copy written under its stem
        self.assertIn(intake['mismatch']['note'], intake['open'])
        self.assertIn(intake['mismatch']['done'], intake['done'])

    def _note(self, *parts):
        path = os.path.join(COMMITTED_SNAPSHOT, 'record', self.manifest['intake_dir'], *parts)
        with open(path, encoding='utf-8') as f:
            return f.read()

    def test_the_mismatch_done_copy_is_not_a_superstring_and_still_pairs_by_name(self):
        intake_dir = self.manifest['intake_dir']
        mismatch = self.manifest['intake']['mismatch']
        note_text = self._note(mismatch['note'])
        done_text = self._note('done', mismatch['done'])
        # the state acceptance line 5 names: a done/ copy that is not a byte-for-byte superstring
        # of the note, so asf.record.staged_guard.check's containment arm (`text in t`) cannot
        # cover the move...
        self.assertTrue(note_text.strip(), mismatch)
        self.assertNotIn(note_text.strip(), done_text)
        # ...which leaves _paired_done, the guard's one escape (F-0305), as what does — and that
        # pairs by name, which is why the copy carries the note's own stem.
        note_rel = f"{intake_dir}/{mismatch['note']}"
        done_rel = f"{intake_dir}/done/{mismatch['done']}"
        self.assertTrue(
            staged_guard._paired_done(note_rel, note_text, [done_rel], {done_rel: done_text},
                                       intake_dir),
            (note_rel, done_rel))

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
        # the entry's own prose is gone — read off the History section, since `priority:` is a
        # structural frontmatter key the same card keeps verbatim
        _preamble, sections = core.parse_sections(text.split('---\n', 2)[2])
        history = dict((h.strip(), c) for h, c in sections)['## History']
        self.assertNotIn('priority', history)
        self.assertNotIn('P1', history)

    def test_history_id_reference_kept(self):
        iid = self.ids['tasks'][0]
        path = os.path.join(self.out, 'record', 'tasks', f'{iid}.md')
        with open(path, encoding='utf-8') as f:
            text = f.read()
        self.assertIn(iid, text)

    def test_manifest_holds_on_the_just_built_snapshot(self):
        self.assertEqual(rehearsal.manifest_holds(self.out), [])

    def test_a_done_headers_free_prose_is_cleared_and_its_arrow_and_id_kept(self):
        path = os.path.join(self.out, 'record', 'inbox', 'done', 'done-note-2.md')
        with open(path, encoding='utf-8') as f:
            header = f.read().split('\n\n', 1)[0]
        self.assertNotEqual(header, PROSE_HEADER)
        self.assertEqual(len(header), len(PROSE_HEADER))
        self.assertTrue(header.startswith('→ '), header)
        for leaked in ('closed', 'groom', '2026-02-03', 'lane-1', '/opt/elsewhere/notes.md'):
            self.assertNotIn(leaked, header, header)

    def _card(self, folder, which=0):
        iid = self.ids[folder][which]
        with open(os.path.join(self.out, 'record', folder, f'{iid}.md'), encoding='utf-8') as f:
            return frontmatter.parse(f.read(), f'{iid}.md')

    def test_no_prose_value_of_the_source_reaches_the_snapshot(self):
        # every typed field but the structural allowlist is prose until proven otherwise: a
        # Task's `reshape:`, a Decision's `decided_by:`, a Rule's `reason:`/`check:`, an
        # `area:`, a `links:` label — none of them may be copied through (C4).
        for dirpath, _dirs, files in os.walk(self.out):
            for name in files:
                with open(os.path.join(dirpath, name), encoding='utf-8') as f:
                    text = f.read()
                for value in PROSE_VALUES:
                    self.assertNotIn(value, text, f'{name}: {value!r}')

    def test_a_prose_typed_field_is_cleared_to_its_own_length(self):
        reshape = self._card('tasks')[0]['reshape']
        self.assertEqual(len(reshape), len(RESHAPE))
        self.assertEqual(_uncleared(reshape), [], reshape)
        rule = self._card('rules')[0]
        self.assertEqual(len(rule['reason']), len(RULE_REASON))
        self.assertEqual(len(rule['check']), len(RULE_CHECK))
        decision = self._card('decisions')[0]
        self.assertEqual(len(decision['decided_by']), len(DECIDED_BY))
        self.assertEqual(_uncleared(decision['decided_by']), [], decision['decided_by'])

    def test_every_structural_field_is_kept_verbatim(self):
        task = self._card('tasks')[0]
        self.assertEqual(task['parent'], 'S-00001')
        self.assertEqual(task['rank'], 101)
        self.assertEqual(task['priority'], 'P2')
        self.assertEqual(task['writes'], ['asf/rehearsal.py', 'tests/test_rehearsal_snapshot.py'])
        self.assertEqual(task['after'], ['T-00002'])
        self.assertEqual(task['stories'], ['S-00001', 'S-00002'])
        self.assertEqual(task['state'], 'Active')
        self.assertEqual(task['stage_since'], '2026-01-01T09:00:00Z')
        self.assertEqual(self._card('decisions')[0]['date'], '2026-01-05')   # a date is kept
        self.assertEqual(self._card('bugs')[0]['severity'], 'S2')
        feature = self._card('features')[0]
        self.assertEqual((feature['lane'], feature['size']), ('full', 'm'))
        self.assertIs(self._card('rules')[0]['enforced'], True)

    def test_a_history_continuation_line_is_cleared_too(self):
        _meta, body = self._card('tasks')
        lines = _history_lines(body)
        self.assertEqual(len(lines), 1, lines)
        line = lines[0]
        self.assertEqual(len(line), len(HISTORY_CONTINUATION))
        self.assertTrue(line.startswith('  '), repr(line))   # the shape of the line survives
        self.assertEqual(_uncleared(line), [], line)
        self.assertNotIn('wrapped on from the line above', line)

    def test_intake_notes_are_written_under_generated_names(self):
        # a note's own file name is a slug of its title (asf/groom/inbox.py), so it is prose:
        # the snapshot names its notes itself and the manifest claims those names.
        intake = os.path.join(self.out, 'record', 'inbox')
        open_names = sorted(n for n in os.listdir(intake) if n.endswith('.md'))
        done_names = sorted(os.listdir(os.path.join(intake, 'done')))
        self.assertEqual(open_names, ['open-note-1.md', 'open-note-2.md'])
        # 'open-note-1.md' under done/ is the mismatch copy: the note's own stem, which is what
        # the staged guard pairs by (asf/rehearsal.py:_build_intake)
        self.assertEqual(done_names, ['done-note-1.md', 'done-note-2.md', 'done-note-3.md',
                                       'open-note-1.md'])
        self.assertEqual(sorted(self.manifest['intake']['open']), open_names)
        self.assertEqual(sorted(self.manifest['intake']['done']), done_names)
        for source_name in ('open-1.md', 'a-second-filed-note.md', 'done-1.md', 'done-2.md',
                            'done-3.md'):
            self.assertNotIn(source_name, open_names + done_names)

    def test_a_timestamped_history_entry_keeps_its_stamp_and_verb_and_loses_its_prose(self):
        # the shape asf.record.reopen writes. Before the entry regex read it, the whole line took
        # the fallback and the stamp went out with the prose.
        _meta, body = self._card('tasks')
        history = dict((h.strip(), c) for h, c in core.parse_sections(body)[1])['## History']
        line = next(l for l in history.split('\n') if l.startswith('- 2026-01-03'))
        self.assertEqual(len(line), len(HISTORY_TIMESTAMPED))
        self.assertTrue(line.startswith('- 2026-01-03 09:04 reopen:'), line)
        self.assertNotIn(HISTORY_TIMESTAMPED_PROSE, line)
        self.assertEqual(_uncleared(line[len('- 2026-01-03 09:04 reopen:'):]), [], line)

    def test_a_hand_written_section_heading_is_cleared_and_the_templates_are_kept(self):
        _meta, body = self._card('tasks', 1)   # the card _build_source gives a hand-typed heading
        headings = [h for h, _c in core.parse_sections(body)[1]]
        for kept in ('## Description', '## Acceptance', '## History', '## Children',
                      '## Backlinks'):
            self.assertIn(kept, headings, headings)
        self.assertNotIn(HAND_HEADING, headings)
        hand = [h for h in headings if h not in rehearsal._TEMPLATE_HEADINGS]
        self.assertEqual(len(hand), 1, headings)
        # the heading's shape survives (the '## ' and the line's own length) and nothing it said
        self.assertTrue(hand[0].startswith('## '), hand[0])
        self.assertEqual(len(hand[0]), len(HAND_HEADING))
        self.assertEqual(_uncleared(hand[0]), [], hand[0])

    def test_an_epics_features_heading_is_kept_verbatim(self):
        # `asf check` reads an Epic's Features list by this heading (asf/record/check.py:394)
        # and asf.groom.shape names it as an Epic's shape, so it is structure like the
        # template's own: cleared, the snapshot's own check reports every Epic as spanning
        # fewer than two Features. The list under it stays prose and is cleared.
        self.assertEqual(rehearsal._clear_heading('## Features'), '## Features')

    def test_the_mismatch_done_copy_pairs_by_name_and_is_a_truncation(self):
        intake = self.manifest['intake']
        mismatch = intake['mismatch']
        self.assertEqual(mismatch['done'], mismatch['note'])
        base = os.path.join(self.out, 'record', 'inbox')
        with open(os.path.join(base, mismatch['note']), encoding='utf-8') as f:
            note_text = f.read()
        with open(os.path.join(base, 'done', mismatch['done']), encoding='utf-8') as f:
            done_text = f.read()
        self.assertTrue(note_text.strip())
        self.assertNotIn(note_text.strip(), done_text)
        self.assertIn(done_text.strip(), note_text)   # a prefix of it, and shorter
        self.assertTrue(staged_guard._paired_done(
            f"inbox/{mismatch['note']}", note_text, [f"inbox/done/{mismatch['done']}"],
            {f"inbox/done/{mismatch['done']}": done_text}, 'inbox'))

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


class CommittedSnapshotIsCleared(unittest.TestCase):
    """The committed ``tests/rehearsal/`` carries no prose the builder owes a clearing — read
    off the fixture itself, as a property, so it holds for the synthetic stand-in this fixture
    stands on *and* for the operator refresh that will rebuild it from the real record."""

    @classmethod
    def setUpClass(cls):
        cls.cards = []
        for folder in core.ITEM_FOLDERS:
            d = os.path.join(COMMITTED_SNAPSHOT, 'record', folder)
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                if not name.endswith('.md'):
                    continue
                path = os.path.join(d, name)
                with open(path, encoding='utf-8') as f:
                    cls.cards.append((os.path.join(folder, name),
                                       frontmatter.parse(f.read(), path)))
        with open(os.path.join(COMMITTED_SNAPSHOT, 'manifest.json'), encoding='utf-8') as f:
            cls.manifest = json.load(f)

    def test_there_are_cards_to_check(self):
        self.assertTrue(self.cards)

    def test_every_non_structural_frontmatter_value_is_cleared(self):
        for relpath, (meta, _body) in self.cards:
            for key, value in meta.items():
                if key in rehearsal._STRUCTURAL_KEYS:
                    continue
                for text in _strings(value):
                    if rehearsal._ISO_RE.match(text):
                        continue
                    self.assertEqual(_uncleared(text), [], f'{relpath}: {key}: {text!r}')

    def test_every_history_continuation_line_is_cleared(self):
        for relpath, (_meta, body) in self.cards:
            for line in _history_lines(body):
                self.assertEqual(_uncleared(line), [], f'{relpath}: {line!r}')

    def test_every_intake_note_carries_a_generated_name(self):
        intake = os.path.join(COMMITTED_SNAPSHOT, 'record', self.manifest['intake_dir'])
        names = [n for n in os.listdir(intake) if n.endswith('.md')]
        names += os.listdir(os.path.join(intake, 'done'))
        self.assertTrue(names)
        for name in names:
            self.assertRegex(name, INTAKE_NAME_RE)


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

    def test_a_mismatch_copy_that_swallows_its_note_whole_is_a_red_line(self):
        # the claim is non-containment, checked against the note's own text on disk — so writing
        # the note back into its done/ copy breaks it, and no edit of manifest.json can hide that
        with open(os.path.join(self.out, 'manifest.json'), encoding='utf-8') as f:
            mismatch = json.load(f)['intake']['mismatch']
        base = os.path.join(self.out, 'record', 'inbox')
        with open(os.path.join(base, mismatch['note']), encoding='utf-8') as f:
            note_text = f.read()
        with open(os.path.join(base, 'done', mismatch['done']), 'w', encoding='utf-8') as f:
            f.write(note_text + '\nand a line more\n')
        problems = rehearsal.manifest_holds(self.out)
        self.assertTrue(any('intake.mismatch' in p for p in problems), problems)

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
