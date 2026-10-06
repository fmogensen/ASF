"""tests.test_scorecard_denials — G2 ask 4: a denials-per-session line on the scorecard, read
from the job logs (``env.log_dir()/jobs/<product>/<job>.jsonl``), never the record. The parser
(:func:`asf.scorecard.facts.parse_job_denials`) and the file reader
(:func:`asf.scorecard.facts.job_denials`) are tested against literal transcript fixtures; the
window aggregation and the printed line (:mod:`asf.scorecard.score`) against
``tests.test_scorecard``'s own ``facts_fixture``.
"""
import datetime
import json
import os
import tempfile
import unittest

from asf import env
from asf.env import Product
from asf.improve.measure import Run
from asf.scorecard import facts, score
from asf.workers import runtime as runtime_mod
from tests.test_scorecard import facts_fixture, run

UTC = datetime.timezone.utc


def _line(**kw):
    return json.dumps(kw)


#: A session with three denials: one whose refusal naming a command is bucketed past it, one a
#: shell-safety refusal (no command to trim), and one with no matching tool_result at all — the
#: fallback reads its own tool_name/command.
JOB_LOG = [
    _line(type='system'),
    _line(type='assistant', message={'content': [
        {'type': 'tool_use', 'id': 'toolu_1', 'name': 'Bash',
         'input': {'command': 'bash tools/check_conventions.sh'}}]}),
    _line(type='user', message={'content': [
        {'type': 'tool_result', 'tool_use_id': 'toolu_1', 'is_error': True,
         'content': 'This command requires approval: bash tools/check_conventions.sh'}]}),
    _line(type='user', message={'content': [
        {'type': 'tool_result', 'tool_use_id': 'toolu_2', 'is_error': True,
         'content': [{'type': 'text', 'text': 'Contains simple_expansion'}]}]}),
    _line(type='result', subtype='success', result='done', permission_denials=[
        {'tool_name': 'Bash', 'tool_use_id': 'toolu_1',
         'tool_input': {'command': 'bash tools/check_conventions.sh'}},
        {'tool_name': 'Bash', 'tool_use_id': 'toolu_2',
         'tool_input': {'command': 'git merge-base --is-ancestor HEAD HEAD; echo $?'}},
        {'tool_name': 'Write', 'tool_use_id': 'toolu_3', 'tool_input': {'file_path': 'x.py'}},
    ]),
]


class ParseJobDenialsTests(unittest.TestCase):
    def test_every_denial_is_counted_and_bucketed(self):
        count, reasons = facts.parse_job_denials(JOB_LOG)
        self.assertEqual(count, 3)
        self.assertEqual(reasons, [
            'This command requires approval: bash tools/check_conventions.sh',
            'Contains simple_expansion',
            'Write',
        ])

    def test_a_command_naming_refusal_is_trimmed_past_the_command(self):
        self.assertEqual(
            facts.bucket_denial_reason(
                'This command requires approval: bash tools/check_x.sh --fix now'),
            'This command requires approval: bash tools/check_x.sh')

    def test_a_refusal_with_no_command_is_kept_whole(self):
        self.assertEqual(facts.bucket_denial_reason('Contains simple_expansion'),
                         'Contains simple_expansion')

    def test_no_result_line_means_no_denials(self):
        self.assertEqual(facts.parse_job_denials([_line(type='system'), _line(type='assistant')]),
                         (0, []))

    def test_malformed_lines_are_skipped_not_raised(self):
        count, reasons = facts.parse_job_denials(['not json', '', '{"type": "result"}',
                                                   '{"type": "result", "permission_denials": 3}'])
        self.assertEqual((count, reasons), (0, []))

    def test_an_empty_log_is_zero(self):
        self.assertEqual(facts.parse_job_denials([]), (0, []))


class JobDenialsFileTests(unittest.TestCase):
    """:func:`asf.scorecard.facts.job_denials` — real files under ``env.log_dir()``."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='scorecard_denials_')
        self.addCleanup(__import__('shutil').rmtree, self.tmp, ignore_errors=True)
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        self.product = Product('sample', {})

    def tearDown(self):
        env.ASF_HOME = self._orig_home

    def write_log(self, job, lines):
        path = runtime_mod.job_log_path('sample', job)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')

    def test_reads_the_runs_own_transcript_by_job_name(self):
        self.write_log('code-t-0001', JOB_LOG)
        runs = [run('code-t-0001', '2026-09-03T10:00:00Z', 'finished', landed=True)]
        got = facts.job_denials(self.product, runs)
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0]['job'], got[0]['ended'], got[0]['count']),
                         ('code-t-0001', '2026-09-03T10:00:00Z', 3))
        self.assertEqual(len(got[0]['reasons']), 3)

    def test_a_run_with_no_transcript_counts_zero_not_an_error(self):
        runs = [run('code-ghost', '2026-09-03T10:00:00Z', 'finished')]
        got = facts.job_denials(self.product, runs)
        self.assertEqual(got, [{'job': 'code-ghost', 'ended': '2026-09-03T10:00:00Z',
                               'count': 0, 'reasons': []}])

    def test_a_quiet_transcript_is_zero_denials(self):
        self.write_log('code-quiet', [_line(type='result', subtype='success', result='ok')])
        runs = [run('code-quiet', '2026-09-03T10:00:00Z', 'finished', landed=True)]
        got = facts.job_denials(self.product, runs)
        self.assertEqual(got[0]['count'], 0)

    def test_no_product_reads_nothing(self):
        self.assertEqual(facts.job_denials(None, [run('x', '2026-09-03T10:00:00Z', 'finished')]), [])


class DenialsRowTests(unittest.TestCase):
    """:func:`asf.scorecard.score.denials_row` and :func:`denials_line` — pure, over
    ``Facts.denials``."""

    def _facts(self, denials):
        return facts_fixture(denials=denials)

    def test_mean_and_share_over_the_window(self):
        f = self._facts([
            {'job': 'a', 'ended': '2026-09-02T00:00:00Z', 'count': 3, 'reasons': ['r1', 'r1', 'r2']},
            {'job': 'b', 'ended': '2026-09-03T00:00:00Z', 'count': 0, 'reasons': []},
            {'job': 'c', 'ended': '2026-09-05T00:00:00Z', 'count': 1, 'reasons': ['r1']},
        ])
        start = datetime.datetime(2026, 9, 1, tzinfo=UTC)
        end = datetime.datetime(2026, 9, 7, tzinfo=UTC)
        row = score.denials_row(f, start, end)
        self.assertEqual(row['sessions'], 3)
        self.assertEqual(row['total'], 4)
        self.assertEqual(row['mean'], round(4 / 3, 2))
        self.assertEqual(row['share_pct'], round(100 * 2 / 3, 1))
        self.assertEqual(row['top'], [('r1', 3), ('r2', 1)])

    def test_outside_the_window_is_excluded(self):
        f = self._facts([{'job': 'a', 'ended': '2026-08-01T00:00:00Z', 'count': 5,
                         'reasons': ['r1']}])
        start = datetime.datetime(2026, 9, 1, tzinfo=UTC)
        end = datetime.datetime(2026, 9, 7, tzinfo=UTC)
        self.assertEqual(score.denials_row(f, start, end)['sessions'], 0)

    def test_no_sessions_is_none_not_zero(self):
        f = self._facts([])
        start = datetime.datetime(2026, 9, 1, tzinfo=UTC)
        end = datetime.datetime(2026, 9, 7, tzinfo=UTC)
        row = score.denials_row(f, start, end)
        self.assertEqual(row['sessions'], 0)
        self.assertIsNone(row['mean'])
        self.assertIsNone(row['share_pct'])

    def test_the_line_names_no_ended_sessions(self):
        self.assertEqual(score.denials_line({'denials': {'sessions': 0}}),
                         'Denials/session: — (no ended sessions in the window)')

    def test_the_line_names_the_mean_share_and_top_reasons(self):
        row = {'denials': {'sessions': 3, 'total': 4, 'mean': 1.33, 'share_pct': 66.7,
                           'top': [('r1', 2), ('r2', 1)]}}
        self.assertEqual(score.denials_line(row),
                         'Denials/session: 1.33 (66.7% of 3 sessions ≥1) — top: r1 (2); r2 (1)')

    def test_window_row_carries_the_denials_key(self):
        f = facts_fixture(denials=[{'job': 'spec-f-0001', 'ended': '2026-09-02T10:00:00Z',
                                   'count': 2, 'reasons': ['r1', 'r1']}])
        row = score.headline(f, days=7)
        self.assertEqual(row['denials']['sessions'], 1)
        self.assertEqual(row['denials']['mean'], 2.0)


if __name__ == '__main__':
    unittest.main()
