"""asf.workers.refusals — the one owner of a refusal's kind, its text and its clause (F-0266
S-64354), and the record's own refusals read off it, never re-written (S-64356)."""
import json
import os
import shutil
import tempfile
import unittest

from asf import env
from asf.workers import lifecycle, pushlog, refusals


class TheRefusalModule(unittest.TestCase):
    """S-64354: the ledger (``path``/``env_for``/``clear``/``ledger``), ``recognise`` over raw
    text, ``last`` over the ledger and the record, and ``clause``."""

    def setUp(self):
        self._home = env.ASF_HOME
        env.ASF_HOME = tempfile.mkdtemp()
        self.addCleanup(self._restore)

    def _restore(self):
        shutil.rmtree(env.ASF_HOME, ignore_errors=True)
        env.ASF_HOME = self._home

    def write_ledger(self, product, job, *lines):
        p = refusals.path(product, job)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            for line in lines:
                f.write(line + '\n')

    # ---- path, env_for, clear ----------------------------------------------------

    def test_path_is_under_gates_named_for_the_job(self):
        p = refusals.path('sample', 'coder-t-1')
        self.assertEqual(p, os.path.join(env.state_dir('sample'), pushlog.PUSHES_DIR,
                                         'coder-t-1.refusals'))

    def test_a_job_of_dotdot_x_cannot_leave_gates(self):
        p = refusals.path('sample', '../x')
        self.assertEqual(os.path.dirname(p), os.path.join(env.state_dir('sample'),
                                                           pushlog.PUSHES_DIR))
        self.assertEqual(os.path.basename(p), 'x.refusals')

    def test_env_for_makes_the_directory_and_names_the_variable(self):
        out = refusals.env_for('sample', 'coder-t-1')
        self.assertEqual(out, {'ASF_REFUSAL_LOG': refusals.path('sample', 'coder-t-1')})
        self.assertTrue(os.path.isdir(os.path.dirname(out['ASF_REFUSAL_LOG'])))

    def test_clear_on_a_missing_file_does_not_raise(self):
        refusals.clear('sample', 'no-such-job')  # must not raise

    # ---- ledger -------------------------------------------------------------------

    def test_ledger_of_no_file_is_empty(self):
        self.assertEqual(refusals.ledger('sample', 'coder-t-1'), [])

    def test_ledger_drops_an_unparseable_line_and_keeps_the_rest(self):
        self.write_ledger('sample', 'coder-t-1',
                          json.dumps({'at': '2026-10-07T21:00:00Z', 'kind': 'push-allow',
                                     'where': 'shim', 'line': 'asf: push refused'}),
                          'not even json',
                          json.dumps({'kind': 'naming'}),  # no 'line': dropped too
                          json.dumps({'at': '2026-10-07T21:05:00Z', 'kind': 'naming',
                                     'where': 'shim', 'line': 'commits do not name T-1'}))
        got = refusals.ledger('sample', 'coder-t-1')
        self.assertEqual([(r.kind, r.line, r.at, r.where) for r in got],
                         [('push-allow', 'asf: push refused', '2026-10-07T21:00:00Z', 'ledger'),
                          ('naming', 'commits do not name T-1', '2026-10-07T21:05:00Z', 'ledger')])

    # ---- recognise ------------------------------------------------------------------

    def test_recognise_returns_the_last_match(self):
        blob = ('commits do not name T-0001: every commit subject on the branch names its item\n'
                'asf: push refused — an ASF session pushes only to factory branches, not: main')
        self.assertEqual(refusals.recognise(blob).kind, refusals.PUSH_ALLOW)

    def test_recognise_files_push_allow(self):
        line = 'asf: push refused — an ASF session pushes only to factory branches, not: main'
        got = refusals.recognise(line)
        self.assertEqual((got.kind, got.line, got.where), (refusals.PUSH_ALLOW, line, 'run log'))

    def test_recognise_files_refguard(self):
        line = ('REF GUARD: refused push → main: a protected ref of this repository (its trunk '
                'or conventions.protected_refs) — only a landing path (a door) advances it; push '
                'a branch and land it instead')
        self.assertEqual(refusals.recognise(line).kind, refusals.REFGUARD)

    def test_recognise_files_the_trunk_or_write_refguard_line_too(self):
        line = ('REF GUARD: refused publish main → main: a protected ref (the trunk or '
                'conventions.protected_refs) — a factory write never pushes, forces or deletes it')
        self.assertEqual(refusals.recognise(line).kind, refusals.REFGUARD)

    def test_a_refguard_warn_line_is_not_recognised(self):
        line = ('REF GUARD (warn): push → main: a protected ref of this repository (its trunk or '
                'conventions.protected_refs) — only a landing path (a door) advances it; push a '
                'branch and land it instead')
        self.assertIsNone(refusals.recognise(line))

    def test_recognise_files_pre_push(self):
        self.assertEqual(refusals.recognise('remote: pre-push hook declined').kind,
                         refusals.PRE_PUSH)

    def test_a_refguard_line_that_also_says_push_files_as_refguard_not_pre_push(self):
        line = ('REF GUARD: refused push → main: a protected ref of this repository (its trunk '
                'or conventions.protected_refs) — only a landing path (a door) advances it; push '
                'a branch and land it instead')
        self.assertEqual(refusals.recognise(line).kind, refusals.REFGUARD)

    def test_recognise_files_redaction_both_forms(self):
        self.assertEqual(refusals.recognise(
            'redact: a.py:12 names a worker account — replace with lane-N').kind,
            refusals.REDACTION)
        self.assertEqual(refusals.recognise(
            'asf: REDACTION REFUSED (name) — no asf and no checks/redact.sh here').kind,
            refusals.REDACTION)

    def test_recognise_of_nothing_is_none(self):
        self.assertIsNone(refusals.recognise(''))
        self.assertIsNone(refusals.recognise(None))
        self.assertIsNone(refusals.recognise('nothing refused here'))

    # ---- last -----------------------------------------------------------------------

    def test_last_prefers_the_newest_at(self):
        self.write_ledger('sample', 'coder-t-1',
                          json.dumps({'at': '2026-10-07T21:00:00Z', 'kind': 'push-allow',
                                     'line': 'older'}),
                          json.dumps({'at': '2026-10-07T21:05:00Z', 'kind': 'naming',
                                     'line': 'newer'}))
        got = refusals.last('sample', 'coder-t-1')
        self.assertEqual(got.line, 'newer')

    def test_last_sorts_a_refusal_with_no_at_oldest(self):
        run = {'started': '2026-10-07T20:00:00Z',
              'correction': {'kind': 'naming', 'text': 'from the record', 'at': ''}}
        self.write_ledger('sample', 'coder-t-1',
                          json.dumps({'at': '2026-10-07T21:00:00Z', 'kind': 'push-allow',
                                     'line': 'from the ledger'}))
        got = refusals.last('sample', 'coder-t-1', run)
        self.assertEqual(got.line, 'from the ledger')

    def test_last_reads_text_only_when_ledger_and_record_are_both_empty(self):
        self.assertIsNone(refusals.last('sample', 'coder-t-1', {}, None))
        got = refusals.last('sample', 'coder-t-1', {}, 'commits do not name T-1')
        self.assertEqual(got.kind, refusals.NAMING)
        # the ledger already answers: text is never consulted
        self.write_ledger('sample', 'coder-t-2',
                          json.dumps({'at': '2026-10-07T21:00:00Z', 'kind': 'push-allow',
                                     'line': 'from the ledger'}))
        got2 = refusals.last('sample', 'coder-t-2', {}, 'commits do not name T-1')
        self.assertEqual(got2.line, 'from the ledger')

    def test_last_filters_the_ledger_to_the_runs_own_window(self):
        # F-0266 PD3: a ledger line outside this run's started/ended window is not this run's
        self.write_ledger('sample', 'coder-t-1',
                          json.dumps({'at': '2026-10-07T19:00:00Z', 'kind': 'push-allow',
                                     'line': 'before this run started'}))
        run = {'started': '2026-10-07T20:00:00Z', 'ended': '2026-10-07T21:00:00Z'}
        self.assertIsNone(refusals.last('sample', 'coder-t-1', run))
        self.write_ledger('sample', 'coder-t-1',
                          json.dumps({'at': '2026-10-07T20:30:00Z', 'kind': 'push-allow',
                                     'line': 'inside the window'}))
        self.assertEqual(refusals.last('sample', 'coder-t-1', run).line, 'inside the window')

    # ---- clause -----------------------------------------------------------------------

    def test_clause_of_none_is_empty(self):
        self.assertEqual(refusals.clause(None), '')

    def test_clause_is_cut_to_clause_max(self):
        r = refusals.Refusal(kind='naming', line='x' * 500, at='2026-10-07T21:00:00Z')
        got = refusals.clause(r, now='2026-10-07T21:03:00Z')
        self.assertLessEqual(len(got), refusals.CLAUSE_MAX)
        self.assertTrue(got.startswith(' — last ASF refusal (naming, 3m before): '), got)

    def test_clause_omits_the_age_clause_when_there_is_no_at(self):
        r = refusals.Refusal(kind='naming', line='commits do not name T-1')
        self.assertEqual(refusals.clause(r, now='2026-10-07T21:03:00Z'),
                         ' — last ASF refusal (naming): commits do not name T-1')


class TheRecordsOwnRefusals(unittest.TestCase):
    """S-64356: the factory's own refusals — ``publish_refused`` and a ``CORRECTION_KINDS``
    correction — are read off the record, never re-written."""

    def test_a_refguard_publish_refused_line_files_as_refguard(self):
        run = {'publish_refused': ('publish worker/T-0001 refused: REF GUARD: refused push → '
                                   'main: a protected ref of this repository (its trunk or '
                                   'conventions.protected_refs) — only a landing path (a door) '
                                   'advances it; push a branch and land it instead')}
        got = refusals.from_record(run)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].kind, refusals.REFGUARD)
        self.assertEqual(got[0].where, 'publish')

    def test_the_same_field_holding_the_repo_hook_output_files_as_pre_push(self):
        run = {'publish_refused': 'publish worker/T-0001 refused: remote: pre-push hook declined'}
        got = refusals.from_record(run)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].kind, refusals.PRE_PUSH)

    def test_a_redaction_publish_refused_line_files_as_redaction(self):
        run = {'publish_refused': ('publish worker/T-0001 refused: redact: a.py:12 names a '
                                   'worker account — replace with lane-N')}
        got = refusals.from_record(run)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].kind, refusals.REDACTION)

    def test_a_review_and_an_unpushed_correction_are_ignored(self):
        self.assertEqual(refusals.from_record(
            {'correction': {'kind': 'review', 'text': 'changes requested', 'at': 't'}}), [])
        self.assertEqual(refusals.from_record(
            {'correction': {'kind': lifecycle.UNPUSHED, 'text': 'x', 'at': 't'}}), [])
        self.assertEqual(refusals.from_record(
            {'correction': {'kind': 'empty', 'text': 'x', 'at': 't'}}), [])

    def test_a_gate_correction_files_under_its_own_kind(self):
        run = {'correction': {'kind': 'gate', 'at': '2026-10-07T21:00:00Z',
                              'text': 'the pre-push check fails on the transplant — nothing '
                                      'pushed, a correction round'}}
        got = refusals.from_record(run)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].kind, 'gate')
        self.assertEqual(got[0].at, '2026-10-07T21:00:00Z')
        self.assertEqual(got[0].where, 'correction')

    def test_no_publish_refused_and_no_correction_is_empty(self):
        self.assertEqual(refusals.from_record({}), [])
        self.assertEqual(refusals.from_record(None), [])


if __name__ == '__main__':
    unittest.main()
