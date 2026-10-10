"""asf.workers.refusals — the one owner of a refusal's kind, its text and the clause a dead
reason carries (F-0266 S-64354, S-64356). Hermetic: a temp ASF_HOME, no git, no gh."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import env, refguard
from asf.workers import lifecycle
from asf.workers import refusals

NAMING_LINE = ('commits do not name T-44931: every commit subject on the branch names its item '
               '— the lane could not reword them: x')
PUSH_ALLOW_LINE = 'asf: push refused — an ASF session pushes only to factory branches, not: main'


def _quiet(fn, *a, **kw):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **kw)


class _Home(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='refusals_')
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        p = mock.patch.object(env, 'ASF_HOME', self.home)
        p.start()
        self.addCleanup(p.stop)

    def write(self, job, *recs, raw=()):
        path = refusals.env_for('p', job)['ASF_REFUSAL_LOG']
        with open(path, 'a', encoding='utf-8') as f:
            for r in recs:
                f.write(json.dumps(r) + '\n')
            for line in raw:
                f.write(line + '\n')
        return path


class TheRefusalModule(_Home):
    def test_the_kinds_alias_the_constants_that_own_their_strings(self):
        self.assertEqual(refusals.NAMING, lifecycle.NAMING)
        self.assertEqual(refusals.PRE_PUSH, lifecycle.HOOK_REFUSED)
        self.assertEqual(refusals.KINDS,
                         ('naming', 'hook refused', 'refguard', 'push-allow', 'redaction',
                          'non-fast-forward'))
        self.assertEqual(refusals.CLAUSE_MAX, 200)

    def test_the_path_stays_under_gates(self):
        p = refusals.path('p', 'coder-t-1')
        self.assertEqual(p, os.path.join(env.state_dir('p'), 'gates', 'coder-t-1.refusals'))
        self.assertEqual(os.path.dirname(refusals.path('p', '../x')),
                         os.path.join(env.state_dir('p'), 'gates'))
        self.assertEqual(refusals.env_for('p', 'j'), {'ASF_REFUSAL_LOG': refusals.path('p', 'j')})

    def test_clear_on_a_missing_file_does_not_raise(self):
        refusals.clear('p', 'nothing-here')
        path = self.write('j', {'kind': 'push-allow', 'line': 'x', 'at': ''})
        refusals.clear('p', 'j')
        self.assertFalse(os.path.exists(path))

    def test_the_ledger_drops_an_unparseable_line_and_keeps_the_rest(self):
        self.write('j', {'at': '2026-10-07T10:00:00Z', 'kind': 'push-allow', 'where': 'shim',
                         'line': PUSH_ALLOW_LINE}, raw=('{not json', '"a string"'))
        self.write('j', {'at': '2026-10-07T10:01:00Z', 'kind': 'hook refused',
                         'line': 'pre-push: tests failed\nmore'})
        got = refusals.ledger('p', 'j')
        self.assertEqual([(r.kind, r.line, r.where) for r in got],
                         [('push-allow', PUSH_ALLOW_LINE, 'ledger'),
                          ('hook refused', 'pre-push: tests failed', 'ledger')])
        self.assertEqual(refusals.ledger('p', 'none'), [])

    def test_from_record_reads_publish_and_a_refusal_correction_only(self):
        run = {'ended': '2026-10-07T10:00:00Z',
               'publish_refused': 'publish worker/T-1 refused: REF GUARD: refused publish main → '
                                  'main: a protected ref',
               'correction': {'kind': 'naming', 'text': NAMING_LINE, 'at': '2026-10-07T10:05:00Z'}}
        got = refusals.from_record(run)
        self.assertEqual([(r.kind, r.where, r.at) for r in got],
                         [('refguard', 'publish', '2026-10-07T10:00:00Z'),
                          ('naming', 'correction', '2026-10-07T10:05:00Z')])
        for kind in ('review', lifecycle.UNPUSHED):
            self.assertEqual(refusals.from_record({'correction': {'kind': kind, 'text': 'x'}}), [])

    def test_recognise_files_each_sites_string_and_returns_the_last(self):
        cases = [
            (PUSH_ALLOW_LINE, 'push-allow'),
            (_quiet(refguard.refusal, 'main', 'publish main', main='main'), 'refguard'),
            (refguard.Guard('main').refusal('refs/heads/main'), 'refguard'),
            (NAMING_LINE, 'naming'),
            ('redact: a/b.py:12 a secret', 'redaction'),
            ('asf: REDACTION REFUSED (pre-commit)', 'redaction'),
            ('error: failed to push some refs: pre-push hook declined', 'hook refused'),
        ]
        for text, kind in cases:
            got = refusals.recognise('noise\n' + text + '\nmore noise')
            self.assertIsNotNone(got, text)
            self.assertEqual((got.kind, got.where), (kind, 'run log'), text)
        self.assertIn('push', refguard.Guard('main').refusal('refs/heads/main'))
        both = f'{NAMING_LINE}\n{PUSH_ALLOW_LINE}\nbye'
        self.assertEqual(refusals.recognise(both).kind, 'push-allow')
        self.assertIsNone(refusals.recognise('all good\nnothing refused'))
        self.assertIsNone(refusals.recognise(''))

    def test_last_prefers_the_newest_at_and_sorts_no_at_oldest(self):
        self.write('j', {'at': '2026-10-07T10:00:00Z', 'kind': 'push-allow', 'line': 'a'},
                   {'kind': 'hook refused', 'line': 'pre-push: b'})
        run = {'job': 'j', 'correction': {'kind': 'naming', 'text': NAMING_LINE,
                                          'at': '2026-10-07T09:00:00Z'}}
        self.assertEqual(refusals.last('p', 'j', run).kind, 'push-allow')
        run['correction']['at'] = '2026-10-07T11:00:00Z'
        self.assertEqual(refusals.last('p', 'j', run).kind, 'naming')

    def test_last_reads_text_only_when_ledger_and_record_are_empty(self):
        self.assertEqual(refusals.last('p', 'j', {}, text=PUSH_ALLOW_LINE).kind, 'push-allow')
        self.assertIsNone(refusals.last('p', 'j', {}))
        self.write('j', {'at': '2026-10-07T10:00:00Z', 'kind': 'hook refused', 'line': 'pre-push x'})
        self.assertEqual(refusals.last('p', 'j', {}, text=PUSH_ALLOW_LINE).kind, 'hook refused')

    def test_the_clause(self):
        self.assertEqual(refusals.clause(None), '')
        r = refusals.Refusal('naming', NAMING_LINE + '\nsecond line', '2026-10-07T10:00:00Z')
        c = refusals.clause(r, now='2026-10-07T10:03:30Z')
        self.assertTrue(c.startswith(' — last ASF refusal (naming, 3m before): commits do not '
                                     'name T-44931'), c)
        self.assertNotIn('second line', c)
        self.assertLessEqual(len(c), refusals.CLAUSE_MAX)
        long = refusals.clause(refusals.Refusal('naming', 'x' * 500), now=0)
        self.assertEqual(len(long), refusals.CLAUSE_MAX)
        self.assertEqual(refusals.clause(refusals.Refusal('naming', 'y')),
                         ' — last ASF refusal (naming): y')


class TheRecordsOwnRefusals(_Home):
    def test_a_refguard_publish_refusal_files_as_refguard(self):
        line = 'publish worker/T-1 refused: ' + refguard.Guard('main').refusal('refs/heads/main')
        [r] = refusals.from_record({'publish_refused': line})
        self.assertEqual((r.kind, r.where), ('refguard', 'publish'))

    def test_the_repos_hook_output_files_as_pre_push(self):
        [r] = refusals.from_record({'publish_refused': 'publish worker/T-1 refused: pre-push hook '
                                                       'declined: lint failed'})
        self.assertEqual(r.kind, 'hook refused')

    def test_a_redaction_correction_files_as_redaction(self):
        [r] = refusals.from_record({'correction': {'kind': lifecycle.HOOK_REFUSED,
                                                   'text': 'redact: src/a.py:3 a token',
                                                   'at': '2026-10-07T10:00:00Z'}})
        self.assertEqual((r.kind, r.where), ('redaction', 'correction'))

    def test_a_refused_publish_writes_no_ledger(self):
        # C5: publish refusals and corrections are read off the record, never re-written
        run = {'job': 'j', 'publish_refused': 'publish b refused: pre-push hook declined'}
        self.assertEqual(refusals.last('p', 'j', run).where, 'publish')
        self.assertFalse(os.path.exists(refusals.path('p', 'j')))
        for mod in ('asf/workers/lifecycle.py', 'asf/harvest/lane.py'):
            with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), mod),
                      encoding='utf-8') as f:
                self.assertNotIn('ASF_REFUSAL_LOG', f.read())

    def test_a_naming_hold_is_found_after_pending_correction_stops_answering(self):
        path = os.path.join(env.state_dir('p'), 'sessions.jsonl')
        run = {'job': 'coder-t-44931', 'item': 'T-44931', 'kind': 'coder',
               'branch': 'worker/T-44931', 'started': '2026-10-07T09:00:00Z'}
        fields, _line = lifecycle.hold(path, run, lifecycle.NAMING, NAMING_LINE,
                                       '2026-10-07T10:00:00Z')
        held = dict(run, **fields)
        [r] = refusals.from_record(held)
        self.assertEqual((r.kind, r.at), ('naming', '2026-10-07T10:00:00Z'))
        self.assertTrue(r.line.startswith('commits do not name T-44931'))


class DeadReason(_Home):
    def test_dead_reason_composes_the_stored_why_with_a_later_refusal(self):
        run = {'job': 'j', 'ended': '2026-10-07T10:00:00Z',
               'dead_why': 'run 500 ended succeeded without the report commit'}
        self.assertEqual(refusals.dead_reason(run, 'p'), run['dead_why'])
        run['correction'] = {'kind': 'naming', 'text': NAMING_LINE, 'at': '2026-10-07T10:02:00Z'}
        got = refusals.dead_reason(run, 'p')
        self.assertTrue(got.startswith(run['dead_why'] + ' — last ASF refusal (naming'), got)
        self.assertIn('commits do not name T-44931', got)
        self.assertEqual(refusals.dead_reason({'job': 'j'}, 'p'), '')
        self.assertEqual(refusals.dead_reason(dict(run, dead_why=got), 'p'), got)  # not twice


if __name__ == '__main__':
    unittest.main()
