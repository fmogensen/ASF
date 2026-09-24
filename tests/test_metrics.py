import contextlib
import datetime as dt
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from asf import env
from asf.init import STREAM_FOLDERS
from asf.record import frontmatter
from asf.record import match
from asf.record.check import cmd_check
from asf.record.index import do_index
from asf.conventions import Conventions
from asf.metrics import import_sessions
from asf.metrics import metrics

DAY = '2026-09-21'

# The fixture-repo helpers below (make_repo/write_item/item_text) mirror asf.record's own test
# fixtures (see tests/test_match.py's `fixture()`, and the item-file shape asf.record.core.TYPES
# and asf.record.frontmatter define) rather than importing a `tests.test_backlog` — the source
# project's equivalent test helper module hasn't been ported here as its own module; asf.record's
# CLI is a set of `cmd_*` functions (new/check/index/ingest), not one script.
FOLDERS = ['epics', 'features', 'stories', 'tasks', 'bugs', 'decisions', 'rules']
DEFAULT_BODY = (
    "## Description\n\n"
    "## Acceptance\n"
    "- [ ] \n\n"
    "## Non-goals\n\n"
    "## History\n"
    "- 2026-01-01: created\n\n"
    "## Children\n\n"
    "## Backlinks\n"
)


def make_repo():
    root = tempfile.mkdtemp(prefix='metrics_test_')
    for f in FOLDERS:
        os.makedirs(os.path.join(root, f))
    return root


def item_text(id_, type_, title, parent=None, typed_lines=(),
              machine_lines=('schema_version: 1',
                             'state: New',
                              'stage_since: 2026-01-01T00:00:00Z',
                              'updated: 2026-01-01T00:00:00Z'),
              body=None):
    lines = [f"id: {id_}", f"type: {type_}", f"title: {title}"]
    if parent:
        lines.append(f"parent: {parent}")
    lines.extend(typed_lines)
    lines.append('# ---- machine ----')
    lines.extend(machine_lines)
    header = '\n'.join(lines)
    return f"---\n{header}\n---\n{body if body is not None else DEFAULT_BODY}"


def folder_of(type_):
    return {
        'epic': 'epics', 'feature': 'features', 'story': 'stories',
        'task': 'tasks', 'bug': 'bugs', 'decision': 'decisions', 'rule': 'rules',
    }[type_]


def write_item(root, id_, type_, title, **kw):
    path = os.path.join(root, folder_of(type_), f"{id_}.md")
    with open(path, 'w', encoding='utf-8') as f:
        f.write(item_text(id_, type_, title, **kw))
    return path


def build_repo():
    """E-0001 Billing (budget 100) > F-0001 Free plan (FREE-1, PR 601) > S-0001 > T-0002; F-0001 > T-0001 (PR 623,
    FREE-1/T3); E-0001 > F-0002 Other > B-0001; then `asf index`."""
    root = make_repo()
    for f in STREAM_FOLDERS:
        os.makedirs(os.path.join(root, f), exist_ok=True)
    write_item(root, 'E-0001', 'epic', 'Billing', typed_lines=('budget_usd: 100',))
    write_item(root, 'F-0001', 'feature', 'Free plan', parent='E-0001',
               typed_lines=('priority: need', 'legacy_id: FREE-1', 'links:', '  prs: [601]'))
    write_item(root, 'S-0001', 'story', 'Sign-up without a card', parent='F-0001')
    write_item(root, 'T-0001', 'task', 'Plan door copy', parent='F-0001',
               typed_lines=('legacy_id: FREE-1/T3', 'links:', '  prs: [623]', '  branches: [cloud/free-plan-t3]'))
    write_item(root, 'T-0002', 'task', 'Sign-up form', parent='S-0001', typed_lines=('stories: [S-0001]',))
    write_item(root, 'F-0002', 'feature', 'Other feature', parent='E-0001')
    write_item(root, 'B-0001', 'bug', 'Banner shows', parent='F-0002', typed_lines=('severity: S2',))
    reindex(root)
    return root


def reindex(root):
    rc = do_index(root)
    assert rc == 0, f"asf.record.index.do_index failed with rc={rc}"


def job(name, conclusion='success', minutes=5, runner='box1', step=None):
    return {'name': name, 'conclusion': conclusion, 'runner': runner, 'minutes': minutes, 'failed_step': step}


def ci_event(run, **kw):
    ev = {'run': run, 'workflow': 'ci', 'sha': 'a' * 40, 'branch': 'cloud/free-plan-t3', 'conclusion': 'success',
          'attempt': 1, 'minutes': 10, 'jobs': [job('lint', minutes=4), job('e2e', minutes=6)],
          'ts': f'{DAY}T10:00:00Z'}
    ev.update(kw)
    return ev


def run_cli(root, *argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = metrics.main(['--root', root, *argv])
    return rc, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.root = build_repo()
        self.items = match.load_index(self.root)
        self.addCleanup(shutil.rmtree, self.root, True)

    def read(self, rel):
        with open(os.path.join(self.root, rel), encoding='utf-8') as f:
            return f.read()

    def put(self, stream, ev):
        return metrics.append_event(self.root, stream, metrics.validate(stream, ev, self.items))


class Schema(Base):
    def bad(self, stream, ev, needle):
        with self.assertRaises(metrics.SchemaError) as cm:
            metrics.validate(stream, ev, self.items)
        self.assertIn(needle, str(cm.exception))

    def test_required_keys_and_types(self):
        ev = ci_event(1)
        for k in ('run', 'workflow', 'sha', 'branch', 'conclusion', 'attempt', 'minutes', 'jobs'):
            self.bad('ci', {a: b for a, b in ev.items() if a != k}, f"missing required key '{k}'")
        self.bad('ci', dict(ci_event(1), run='7'), "'run' must be int")
        self.bad('ci', ci_event(1, minutes=True), "'minutes' must be num")
        self.bad('ci', ci_event(1, attempt=0), 'attempt')
        self.bad('ci', ci_event(1, jobs=[{'name': 'x'}]), 'ci.jobs[0]: missing required key')
        self.bad('ci', ci_event(1, wat=1), 'unknown key(s) wat')
        self.bad('sessions', {'task': 't', 'account': 'accta'}, "missing required key 'result'")
        self.bad('sessions', {'task': 't', 'account': 'accta', 'result': 'done', 'kind': 'nope'}, "'kind' must be one of")
        self.bad('ticks', {'tick': 1, 'launches': 0}, "missing required key 'merges'")
        self.bad('ticks', {'tick': 1, 'launches': 0, 'merges': 0, 'stalls': 0, 'refusals': 0, 'relaunches': 0,
                           'quota': {'accta': {'h5': 1}}}, 'quota')

    def test_ts_must_be_utc_iso(self):
        self.bad('ci', ci_event(1, ts='2026-09-21 10:00'), "'ts' must be a UTC ISO timestamp")
        self.bad('ci', ci_event(1, ts='2026-09-21T10:00:00+02:00'), "'ts' must be a UTC ISO timestamp")

    def test_an_id_is_never_invented(self):
        self.bad('ci', ci_event(1, items=['T-9999']), 'T-9999 is not in index.json')
        self.bad('sessions', {'task': 't', 'account': 'a', 'result': 'done', 'item': 'F-7777'}, 'F-7777')
        self.bad('ci', ci_event(1, items=['nope']), 'not an item id')

    def test_ts_and_defaults_are_filled(self):
        now = dt.datetime(2026, 9, 21, 6, 40, 1, tzinfo=dt.timezone.utc)
        ev = metrics.validate('sessions', {'task': 'fix-free-plan-t3-r2', 'account': 'accta', 'result': 'done'},
                              self.items, now=now)
        self.assertEqual(ev['ts'], '2026-09-21T06:40:01Z')
        self.assertEqual((ev['kind'], ev['round'], ev['usd'], ev['pushes'], ev['model']), ('fix', 2, None, None, None))
        self.assertEqual(ev['item'], 'T-0001')

    def test_kind_from_the_task_prefix(self):
        want = {'spec-free-plan': 'spec', 'review-x-r1': 'review', 'fix-x-r3': 'fix', 'p2-t1': 'code', 'plan-x': 'plan',
                'preflight-x': 'preflight', 'probe-x': 'probe', 'rebase-x': 'rebase', 'relaunch-x': 'relaunch',
                'launch-accta': 'launch', 'tick-0001': 'tick', 'adjudicate-x': 'other'}
        for task, kind in want.items():
            self.assertEqual(metrics.derive_kind(task), kind, task)
        self.assertIsNone(metrics.derive_round('p2-t1'))
        self.assertEqual(metrics.derive_round('fix-x-r12'), 12)

    def test_ci_matcher_fills_items_or_says_why_null(self):
        ev = metrics.validate('ci', ci_event(1), self.items)
        self.assertEqual((ev['items'], ev['item_reason']), (['T-0001'], None))
        ev = metrics.validate('ci', ci_event(2, branch='cloud/unrelated'), self.items)
        self.assertIsNone(ev['items'])
        self.assertIn('no rule matched', ev['item_reason'])
        ev = metrics.validate('ci', ci_event(3, branch='worktree-m-batch-20260921-0101', batch='worktree-m-batch-20260921-0101'),
                              self.items)
        self.assertEqual((ev['items'], ev['item_reason']), (None, 'batch run without a PR list'))
        hints = {'prs': [601, 623]}
        ev = metrics.validate('ci', dict(ci_event(4, branch='worktree-m-batch-20260921-0101',
                                                  batch='worktree-m-batch-20260921-0101'), match_hints=hints), self.items)
        self.assertEqual(ev['items'], ['F-0001', 'T-0001'])
        self.assertNotIn('match_hints', ev)

    def test_session_with_two_matches_picks_the_deepest_and_says_so(self):
        ev = metrics.validate('sessions', {'task': 'x', 'account': 'a', 'result': 'done',
                                           'branch': 'cloud/y', 'match_hints': {'title': 'S-0001 and T-0002'}}, self.items)
        self.assertEqual(ev['item'], 'T-0002')
        self.assertEqual(ev['item_reason'], 'ambiguous: S-0001, T-0002')
        ev = metrics.validate('sessions', {'task': 'zzz', 'account': 'a', 'result': 'done'}, self.items)
        self.assertIsNone(ev['item'])
        self.assertIn('no rule matched', ev['item_reason'])


class Append(Base):
    def test_cli_appends_and_is_idempotent(self):
        payload = json.dumps(ci_event(77))
        rc, out, _ = run_cli(self.root, 'append', 'ci', payload)
        self.assertEqual(rc, 0)
        self.assertIn('appended to metrics/ci/2026-09-21.jsonl', out)
        rc, out, _ = run_cli(self.root, 'append', 'ci', payload)
        self.assertEqual(rc, 0)
        self.assertIn('no-op: already in metrics/ci/2026-09-21.jsonl', out)
        text = self.read('metrics/ci/2026-09-21.jsonl')
        self.assertEqual(len(text.splitlines()), 1)
        # a second attempt of the same run is a different event
        run_cli(self.root, 'append', 'ci', json.dumps(ci_event(77, attempt=2)))
        self.assertEqual(len(self.read('metrics/ci/2026-09-21.jsonl').splitlines()), 2)

    def test_line_format_sorted_keys_no_trailing_space(self):
        run_cli(self.root, 'append', 'ci', json.dumps(ci_event(5)))
        line = self.read('metrics/ci/2026-09-21.jsonl')
        self.assertTrue(line.endswith('\n'))
        for l in line.splitlines():
            self.assertEqual(l, l.rstrip())
            obj = json.loads(l)
            self.assertEqual(list(obj), sorted(obj))
            self.assertEqual(list(obj['jobs'][0]), sorted(obj['jobs'][0]))

    def test_the_file_is_chosen_by_ts_and_created(self):
        run_cli(self.root, 'append', 'ci', json.dumps(ci_event(5, ts='2026-09-19T23:59:59Z')))
        self.assertTrue(os.path.isfile(os.path.join(self.root, 'metrics/ci/2026-09-19.jsonl')))

    def test_natural_keys(self):
        s = {'task': 'fix-x-r1', 'account': 'accta', 'result': 'done', 'ts': f'{DAY}T01:00:00Z'}
        t = {'tick': 12, 'launches': 1, 'merges': 0, 'stalls': 0, 'refusals': 0, 'relaunches': 0}
        self.assertEqual(self.put('sessions', s)[0], 'appended')
        self.assertEqual(self.put('sessions', s)[0], 'exists')
        self.assertEqual(self.put('sessions', dict(s, ts=f'{DAY}T02:00:00Z'))[0], 'appended')
        self.assertEqual(self.put('ticks', dict(t, ts=f'{DAY}T01:00:00Z'))[0], 'appended')
        self.assertEqual(self.put('ticks', dict(t, ts=f'{DAY}T01:00:00Z', launches=9))[0], 'exists')

    def test_schema_error_is_exit_2_with_the_reason(self):
        rc, out, err = run_cli(self.root, 'append', 'ci', json.dumps({'run': 1}))
        self.assertEqual(rc, 2)
        self.assertIn("missing required key", err)
        self.assertFalse(os.listdir(os.path.join(self.root, 'metrics', 'ci')) if os.path.isdir(os.path.join(self.root, 'metrics', 'ci')) else False)
        rc, _o, err = run_cli(self.root, 'append', 'ci', 'not json')
        self.assertEqual(rc, 2)
        self.assertIn('not JSON', err)

    def test_stdin_appends_every_line_and_reports_bad_ones(self):
        data = '\n'.join([json.dumps(ci_event(1)), json.dumps({'run': 2}), json.dumps(ci_event(3))]) + '\n'
        with mock.patch('sys.stdin', io.StringIO(data)):
            rc, out, err = run_cli(self.root, 'append', 'ci', '--stdin')
        self.assertEqual(rc, 2)
        self.assertIn('line 2:', err)
        self.assertEqual(len(self.read('metrics/ci/2026-09-21.jsonl').splitlines()), 2)


def fixture_streams(test):
    """A day of events: two ci runs (one red with a cancelled job), three sessions, two ticks."""
    test.put('ci', ci_event(1, ts=f'{DAY}T09:00:00Z', minutes=10, conclusion='failure',
                            jobs=[job('lint', minutes=4, runner='box1'), job('e2e', 'failure', 6, 'box2', 'Run playwright tests')]))
    test.put('ci', ci_event(2, ts=f'{DAY}T10:00:00Z', branch='worktree-m-batch-20260921-0900',
                            batch='worktree-m-batch-20260921-0900', conclusion='cancelled', minutes=20, cancelled_minutes=12,
                            superseded=True, items=['F-0001', 'B-0001'],
                            jobs=[job('e2e', 'cancelled', 12, 'box2'), job('lint', minutes=8, runner='box1')]))
    test.put('ci', ci_event(3, ts=f'{DAY}T11:00:00Z', attempt=2, branch='main', minutes=10, items=None, item_reason='nothing to match on',
                            jobs=[job('lint', minutes=10)]))
    test.put('sessions', {'task': 'spec-free-plan', 'account': 'accta', 'result': 'done', 'ts': f'{DAY}T08:00:00Z', 'usd': 2.5})
    test.put('sessions', {'task': 'review-spec-free-plan-r2', 'account': 'acctb', 'result': 'done', 'ts': f'{DAY}T08:30:00Z',
                          'branch': 'cloud/spec-free-plan'})
    test.put('sessions', {'task': 'fix-free-plan-t3-r1', 'account': 'accta', 'result': 'done', 'ts': f'{DAY}T09:30:00Z', 'usd': 1.25})
    test.put('sessions', {'task': 'mystery-x', 'account': 'acctc', 'result': 'failed', 'ts': f'{DAY}T09:45:00Z'})
    test.put('ticks', {'tick': 1, 'ts': f'{DAY}T08:00:00Z', 'launches': 3, 'merges': 1, 'stalls': 0, 'refusals': 1,
                       'relaunches': 1, 'quota': {'accta': {'h5': 10, 'd7': 20}, 'acctb': {'h5': 5, 'd7': 6}},
                       'refused_files': {'apps/web/x.ts': 1}})
    test.put('ticks', {'tick': 2, 'ts': f'{DAY}T09:00:00Z', 'launches': 3, 'merges': 2, 'stalls': 1, 'refusals': 0,
                       'relaunches': 0, 'quota': {'accta': {'h5': 12, 'd7': 21}}})


EXPECTED_DAILY = """# Factory scorecard 2026-09-21

generated: 2026-09-21 — from metrics/ci (3), metrics/sessions (4), metrics/ticks (2)

## Waste

| Metric | Value | Note |
|---|---|---|
| ci runs | 3 — 1 green, 1 red, 1 reruns | red rate 33 % |
| runner-minutes | 40 (0 h) — useful 70 % | cancelled: batch 12 (30 %) — 30 % of the minutes |
| red job: e2e | 1/2 (50 %) | |
| red signature | 1× | e2e: Run playwright tests |
| batches | 1 cut, 1 refused | refused on: apps/web/x.ts ×1 |
| agents | 6 launches in 2 ticks, 1 relaunches | 3 PRs merged → 2.0 launches per merged PR |
| tokens | in — · out — · cache rd — · cache wr — | 4 sessions, 0 capped |
| spec quality | 1 specs reviewed, 0 in round 1 | mean 2.0 rounds, median 2 |
| bandwidth | cux 5h/7d: accta 12/21 %, acctb 5/6 % | 2 runners seen, 40 runner-minutes |

## Cost per Feature (7 days)

2026-09-15 … 2026-09-21; a Feature's row sums its Tasks, Stories and Bugs; a CI run's minutes are split over the items it names; tokens are four separate numbers and are never added together.

| Feature | Title | Sessions | Fix rounds | Runner-min | USD | In | Out | Cache rd | Cache wr |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|
| F-0001 | Free plan | 3 | 1 | 20 | 3.75 | — | — | — | — |
| F-0002 | Other feature | 0 | 0 | 10 | — | — | — | — | — |
| (no Feature) | unattributed or outside a Feature | 1 | 0 | 10 | — | — | — | — | — |
| **All** | | 4 | 1 | 40 | 3.75 | — | — | — | — |
"""


def _at(minute):
    return f"{DAY}T{9 + minute // 60:02d}:{minute % 60:02d}:00Z"


def intake_events():
    """Five cards filed at 09:00; A-D decided 10/20/30/40 min later; A, B, D launched 4/8/12 min after
    that, C never; a launch of D before it was decided must not count."""
    evs = []
    for i in 'ABCDE':
        evs.append({'kind': 'intake', 'item': f'T-{i}', 'filed': _at(0), 'ts': _at(3)})
    for i, m in zip('ABCD', (10, 20, 30, 40)):
        evs.append({'kind': 'groom_answer', 'item': f'T-{i}', 'field': 'decided', 'value': 'true', 'ts': _at(m)})
    evs.append({'kind': 'groom_answer', 'item': 'T-E', 'field': 'priority', 'value': 'true', 'ts': _at(15)})
    evs.append({'kind': 'launch', 'item': 'T-D', 'ts': _at(35)})
    for i, m in zip('ABD', (14, 28, 52)):
        evs.append({'kind': 'launch', 'item': f'T-{i}', 'ts': _at(m)})
    return evs


class IntakeLatencyTests(Base):
    def test_median_p90_and_pair_counts(self):
        (_, a, na), (_, b, nb) = metrics.intake_latency_rows(intake_events())
        self.assertEqual(a, 'median 25 min, p90 40 min')
        self.assertEqual(na, '4 of 5 cards measured (1 not decided by a groom answer)')
        self.assertEqual(b, 'median 8 min, p90 12 min')
        self.assertEqual(nb, '3 of 4 decided cards launched')

    def test_empty_stream_divides_by_nothing(self):
        rows = metrics.intake_latency_rows([])
        self.assertEqual([r[1] for r in rows], ['no pairs measured'] * 2)

    def test_render_daily_renders_both_rows(self):
        os.makedirs(os.path.join(self.root, 'metrics', 'events'), exist_ok=True)
        with open(os.path.join(self.root, 'metrics', 'events', f'{DAY}.jsonl'), 'w', encoding='utf-8') as f:
            for ev in intake_events():
                f.write(json.dumps(ev) + '\n')
        md = metrics.render_daily(self.root, DAY, self.items)
        self.assertIn('| intake → decided | median 25 min, p90 40 min | 4 of 5 cards measured', md)
        self.assertIn('| decided → first session | median 8 min, p90 12 min | 3 of 4 decided cards launched |', md)

    def test_no_events_argument_changes_nothing(self):
        fixture_streams(self)
        args = (metrics.read_stream(self.root, 'ci'), metrics.read_stream(self.root, 'sessions'),
                metrics.read_stream(self.root, 'ticks'))
        self.assertEqual(metrics.scorecard_rows(*args), metrics.scorecard_rows(*args, events=()))
        self.assertFalse(any(r[0].startswith('intake') for r in metrics.scorecard_rows(*args)))


class Rollup(Base):
    def test_daily_tables_match_the_expected_md(self):
        fixture_streams(self)
        self.assertEqual(metrics.render_daily(self.root, DAY, self.items), EXPECTED_DAILY)

    def test_empty_day_renders(self):
        md = metrics.render_daily(self.root, '2026-01-01', self.items)
        self.assertIn('| ci runs | 0 — 0 green, 0 red, 0 reruns | red rate 0 % |', md)
        self.assertIn('| **All** | | 0 | 0 | 0 | — |', md)

    def test_cost_sums_through_children(self):
        costs = metrics.compute_costs(
            [{'minutes': 30, 'items': ['T-0002', 'F-0001']}, {'minutes': 9, 'items': ['B-0001']}, {'minutes': 5, 'items': None}],
            [{'item': 'T-0002', 'kind': 'fix', 'usd': 1.5}, {'item': 'S-0001', 'kind': 'code', 'usd': None},
             {'item': 'F-0001', 'kind': 'spec', 'usd': 2.0}, {'item': None, 'kind': 'code', 'usd': 9.0}])
        f1 = metrics.subtree_cost(self.items, costs, 'F-0001')
        self.assertEqual((f1['sessions'], f1['fix_rounds'], f1['runner_min'], f1['usd']), (3, 1, 30.0, 3.5))
        f2 = metrics.subtree_cost(self.items, costs, 'F-0002')
        self.assertEqual((f2['sessions'], f2['runner_min'], f2['usd']), (0, 9.0, None))
        e = metrics.subtree_cost(self.items, costs, 'E-0001')
        self.assertEqual((e['sessions'], e['runner_min'], e['usd']), (3, 39.0, 3.5))

    def test_item_cost_and_epic_spend_are_written(self):
        fixture_streams(self)
        changed, spend = metrics.write_costs(self.root, self.items, metrics.read_stream(self.root, 'ci'),
                                             metrics.read_stream(self.root, 'sessions'),
                                             now=dt.datetime(2026, 9, 21, 12, 0, 0, tzinfo=dt.timezone.utc))
        self.assertEqual(sorted(changed), ['B-0001', 'E-0001', 'F-0001', 'T-0001'])
        idx = self.reindexed()
        self.assertEqual(idx['T-0001']['cost'], {'sessions': 1, 'fix_rounds': 1, 'runner_min': 10, 'usd': 1.25})
        self.assertEqual(idx['F-0001']['cost'], {'sessions': 2, 'fix_rounds': 0, 'runner_min': 10, 'usd': 2.5})
        self.assertEqual(idx['B-0001']['cost'], {'sessions': 0, 'fix_rounds': 0, 'runner_min': 10, 'usd': None})
        self.assertEqual(idx['F-0001']['updated'], '2026-09-21T12:00:00Z')
        self.assertEqual(idx['E-0001']['spend_usd'], 3.75)
        self.assertEqual(idx['E-0001']['budget_usd'], 100)
        self.assertEqual(spend['E-0001'], (3.75, 100))
        self.assertNotIn('cost', idx['F-0002'])

    def reindexed(self):
        reindex(self.root)
        return match.load_index(self.root)

    def test_write_machine_leaves_typed_lines_byte_identical(self):
        fixture_streams(self)
        paths = {i: os.path.join(self.root, 'features' if i.startswith('F') else 'tasks', f'{i}.md') for i in ('F-0001', 'T-0001')}

        def typed(p):
            with open(p, encoding='utf-8') as f:
                text = f.read()
            head, _sep, _rest = text.partition(frontmatter.MARKER)
            return head.encode()

        before = {i: typed(p) for i, p in paths.items()}
        metrics.write_costs(self.root, self.items, metrics.read_stream(self.root, 'ci'),
                            metrics.read_stream(self.root, 'sessions'))
        for i, p in paths.items():
            self.assertEqual(typed(p), before[i], i)
            with open(p, encoding='utf-8') as f:
                self.assertIn('cost: {sessions:', f.read())
        with open(paths['T-0001'], encoding='utf-8') as f:
            self.assertIn('cost: {sessions: 1, fix_rounds: 1, runner_min: 10, usd: 1.25}', f.read())
        with open(os.path.join(self.root, 'features', 'F-0002.md'), encoding='utf-8') as f:
            self.assertNotIn('cost:', f.read())

    def test_null_usd_round_trips(self):
        fixture_streams(self)
        metrics.write_costs(self.root, self.items, metrics.read_stream(self.root, 'ci'), metrics.read_stream(self.root, 'sessions'))
        with open(os.path.join(self.root, 'bugs', 'B-0001.md'), encoding='utf-8') as f:
            text = f.read()
        self.assertIn('cost: {sessions: 0, fix_rounds: 0, runner_min: 10, usd: null}', text)
        meta, _b = frontmatter.parse(text)
        self.assertIsNone(meta['cost']['usd'])

    def test_rollup_twice_changes_nothing_and_check_passes(self):
        fixture_streams(self)
        with mock.patch.object(metrics, 'deploy_runs', return_value=[]):
            rc, out, _ = run_cli(self.root, 'rollup', DAY)
            self.assertEqual(rc, 0, out)
            snap = self.snapshot()
            rc, out, _ = run_cli(self.root, 'rollup', DAY)
        self.assertEqual(rc, 0)
        self.assertIn('unchanged metrics/daily/2026-09-21.md', out)
        self.assertIn('cost: 0 item(s) updated', out)
        self.assertEqual(self.snapshot(), snap)
        self.assertIn('budget: E-0001 Billing: spend $3.75 of budget $100 (4 %)', out)
        check_out = io.StringIO()
        with contextlib.redirect_stdout(check_out):
            check_rc = cmd_check(types.SimpleNamespace(paths=None), self.root)
        self.assertEqual(check_rc, 0, check_out.getvalue())

    def snapshot(self):
        snap = {}
        for base, _d, files in os.walk(self.root):
            for f in files:
                p = os.path.join(base, f)
                with open(p, 'rb') as fh:
                    snap[os.path.relpath(p, self.root)] = fh.read()
        return snap


DEPLOYS = [{'headSha': 'b' * 40, 'conclusion': 'success', 'updatedAt': '2026-09-21T05:00:00Z'},
           {'headSha': 'c' * 40, 'conclusion': 'failure', 'updatedAt': '2026-09-20T05:00:00Z'},
           {'headSha': 'd' * 40, 'conclusion': 'success', 'updatedAt': '2026-09-19T05:00:00Z'}]
PRS = {601: {'merged': True, 'merge_commit_sha': '1' * 40, 'body': 'Adds it\n\nTry it: open /billing as a free user\n', 'title': 'x'},
       623: {'merged': True, 'merge_commit_sha': '2' * 40, 'body': 'no try line', 'title': 'y'}}


class Releases(Base):
    def run_release(self, anc, deploys=DEPLOYS, prs=PRS):
        with mock.patch.object(metrics, 'deploy_runs', return_value=deploys), \
                mock.patch.object(metrics, 'pr_info', side_effect=lambda n, *a, **kw: prs.get(n)), \
                mock.patch.object(metrics, 'is_ancestor', side_effect=lambda repo, c, of: (c, of) in anc):
            return metrics.write_release(self.root, DAY, self.items, repo='/nonexistent')

    def test_items_merged_since_the_previous_deploy_are_listed_by_type(self):
        anc = {('1' * 40, 'b' * 40), ('2' * 40, 'b' * 40), ('2' * 40, 'd' * 40)}   # 623 was already in the older deploy
        path = self.run_release(anc)
        self.assertEqual(os.path.basename(path), f'{DAY}-bbbbbbb.md')
        text = self.read(f'releases/{DAY}-bbbbbbb.md')
        self.assertIn('# Release 2026-09-21 · bbbbbbb', text)
        self.assertIn('Everything merged since `ddddddd`.', text)   # the failed deploy c… is not a baseline
        self.assertIn('## Features\n\n- [F-0001](../features/F-0001.md) Free plan — #601\n  - Try it: open /billing as a free user', text)
        self.assertNotIn('T-0001', text)

    def test_grouped_by_type_and_missing_try_it(self):
        anc = {('1' * 40, 'b' * 40), ('2' * 40, 'b' * 40)}
        self.run_release(anc)
        text = self.read(f'releases/{DAY}-bbbbbbb.md')
        self.assertLess(text.index('## Features'), text.index('## Tasks'))
        self.assertIn('- [T-0001](../tasks/T-0001.md) Plan door copy — #623\n', text)
        self.assertNotIn('Try it: no try', text)

    def test_existing_release_for_the_sha_is_left_alone(self):
        with open(os.path.join(self.root, 'releases', '2026-09-20-bbbbbbb.md'), 'w') as f:
            f.write('x')
        self.assertIsNone(self.run_release(set()))
        self.assertEqual(os.listdir(os.path.join(self.root, 'releases')), ['2026-09-20-bbbbbbb.md'])

    def test_previous_release_file_is_the_baseline(self):
        with open(os.path.join(self.root, 'releases', '2026-09-19-ddddddd.md'), 'w') as f:
            f.write('x')
        anc = {('1' * 40, 'b' * 40), ('2' * 40, 'b' * 40), ('1' * 40, 'ddddddd')}
        self.run_release(anc)
        text = self.read(f'releases/{DAY}-bbbbbbb.md')
        self.assertNotIn('F-0001', text)
        self.assertIn('T-0001', text)

    def test_empty_list_says_so(self):
        self.run_release(set())
        self.assertIn('No item reached prod in this release.', self.read(f'releases/{DAY}-bbbbbbb.md'))

    def test_unmerged_prs_and_no_successful_deploy(self):
        prs = {601: dict(PRS[601], merged=False), 623: None}
        self.run_release({('1' * 40, 'b' * 40)}, prs=prs)
        self.assertIn('No item reached prod', self.read(f'releases/{DAY}-bbbbbbb.md'))
        self.assertIsNone(self.run_release(set(), deploys=[DEPLOYS[1]]))

    def test_no_release_while_no_item_has_a_pr(self):
        no_prs = {i: {k: v for k, v in it.items() if k != 'links'} for i, it in self.items.items()}
        with mock.patch.object(metrics, 'deploy_runs', return_value=DEPLOYS):
            self.assertIsNone(metrics.write_release(self.root, DAY, no_prs, repo='/nonexistent'))
        self.assertEqual(os.listdir(os.path.join(self.root, 'releases')), [])

    def test_rollup_writes_the_release_once(self):
        anc = {('1' * 40, 'b' * 40)}
        with mock.patch.object(metrics, 'deploy_runs', return_value=DEPLOYS), \
                mock.patch.object(metrics, 'pr_info', side_effect=lambda n, *a, **kw: PRS.get(n)), \
                mock.patch.object(metrics, 'is_ancestor', side_effect=lambda repo, c, of: (c, of) in anc), \
                mock.patch.dict(os.environ, {'BACKLOG_PRODUCT_REPO': '/nonexistent'}):
            _rc, out, _ = run_cli(self.root, 'rollup', DAY)
            self.assertIn('release: releases/2026-09-21-bbbbbbb.md', out)
            _rc, out, _ = run_cli(self.root, 'rollup', DAY)
            self.assertIn('release: none new', out)


def _git(cwd, *args):
    return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.com',
                           '-c', 'core.hooksPath=/dev/null', *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class TrunkReleases(Base):
    """A product that deploys nothing ships its green trunk (B-0077): the rollup cuts the release
    itself — the note in the record, a `v<major>.<minor>.<patch>` tag on the product repo, the
    version's notes in its changelog."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix='trunk_release_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = os.path.join(self.tmp, 'origin.git')
        self.repo = os.path.join(self.tmp, 'work')
        _git(self.tmp, 'init', '-q', '--bare', '-b', 'main', self.origin)
        _git(self.tmp, 'clone', '-q', self.origin, self.repo)
        os.makedirs(os.path.join(self.repo, 'pkg'))
        self.commit('pkg/__init__.py', '__version__ = "0.1.0"\n', 'init: the package')
        self.commit('pkg/a.py', 'a = 1\n', 'task(T-0001): plan door copy')
        self.product = env.Product('trunky', {
            'repo_dir': self.repo, 'repo_slug': 'x/y', 'main': 'main', 'ci': 'none',
            'conventions': {'version_file': 'pkg/__init__.py'}})
        # the rollup's clock: each release() runs later than the last, past the default interval
        self.now = metrics.now_utc()
        clock = mock.patch.object(metrics, 'now_utc', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def commit(self, path, text, msg):
        if _git(self.origin, 'for-each-ref', 'refs/heads/main'):
            _git(self.repo, 'pull', '-q', '--rebase', 'origin', 'main')    # the rollup's changelog commits
        with open(os.path.join(self.repo, path), 'w') as f:
            f.write(text)
        _git(self.repo, 'add', path)
        _git(self.repo, 'commit', '-q', '-m', msg)
        _git(self.repo, 'push', '-q', 'origin', 'HEAD:main')
        return _git(self.repo, 'rev-parse', 'HEAD')

    def release(self, day=DAY, later=dt.timedelta(hours=2)):
        self.now += later
        with contextlib.redirect_stdout(io.StringIO()):
            return metrics.write_release(self.root, day, self.items, product=self.product)

    def origin_tags(self):
        out = _git(self.origin, 'for-each-ref', '--format=%(refname:short) %(*objectname)', 'refs/tags')
        return dict(line.split() for line in out.splitlines())

    def changelog(self):
        return _git(self.origin, 'show', 'main:CHANGELOG.md')

    def set_epics(self, *epics):
        """(id, title, state, rank) Epics in the index, in place of the fixture's one."""
        for iid, title, state, rank in epics:
            self.items[iid] = {'id': iid, 'type': 'epic', 'title': title, 'state': state, 'rank': rank,
                               'folder': 'epics'}

    def legacy_release(self, day, sha):
        """A release as the rollup cut it before versions: a `release-*` tag and a note naming it."""
        name = f'release-{day}-{sha[:7]}'
        _git(self.repo, 'tag', '-a', name, sha, '-m', f'Release {day} · {sha[:7]}')
        _git(self.repo, 'push', '-q', 'origin', f'refs/tags/{name}')
        with open(os.path.join(self.root, 'releases', f'{day}-{sha[:7]}.md'), 'w') as f:
            f.write(f'# Release {day} · {sha[:7]}\n\nTag `{name}`.\n')
        return name

    # ---- the rule -------------------------------------------------------------

    def test_an_epic_title_carries_a_version_or_none(self):
        self.assertEqual(metrics.epic_version('Product 0.1 — a working factory'), (0, 1))
        self.assertEqual(metrics.epic_version('Thing 12.3: faster'), (12, 3))
        self.assertIsNone(metrics.epic_version("Control plane — the factory's dashboard"))
        self.assertIsNone(metrics.epic_version('v2 dashboard'))

    def test_the_line_is_the_lowest_ranked_open_versioned_epic(self):
        self.set_epics(('E-0001', 'Billing', 'Active', 0),                 # unversioned: skipped
                       ('E-0002', 'Product 0.1 — a working factory', 'Closed', 1),
                       ('E-0003', 'Product 0.3 — autonomous', 'New', 3),
                       ('E-0004', 'Product 0.2 — self-improvement', 'Active', 2))
        self.assertEqual(metrics.roadmap_line(self.items), (0, 2))
        self.items['E-0004']['removed'] = 'not now'
        self.assertEqual(metrics.roadmap_line(self.items), (0, 3))
        self.assertIsNone(metrics.roadmap_line({'E-0001': self.items['E-0001']}))

    def test_the_next_patch_and_the_first_release_of_a_line(self):
        self.assertEqual(metrics.next_version(set(), (0, 1)), (0, 1, 0))
        self.assertEqual(metrics.next_version({(0, 1, 0), (0, 1, 1)}, (0, 1)), (0, 1, 2))
        self.assertEqual(metrics.next_version({(0, 1, 0), (0, 1, 7)}, (0, 2)), (0, 2, 0))
        self.assertEqual(metrics.next_version({(0, 2, 0)}, (0, 1)), (0, 2, 1))     # never backwards

    # ---- the release ----------------------------------------------------------

    def test_a_product_without_a_deploy_cuts_a_tagged_release_of_its_trunk(self):
        head = _git(self.repo, 'rev-parse', 'HEAD')
        path = self.release()
        self.assertEqual(os.path.basename(path or ''), f'{DAY}-{head[:7]}.md')
        text = self.read(f'releases/{DAY}-{head[:7]}.md')
        self.assertIn('- [T-0001](../tasks/T-0001.md) Plan door copy', text)
        self.assertIn('\nversion: v0.1.0\n', text)          # no versioned Epic: version_file's 0.1
        self.assertIn('Tag `v0.1.0`', text)
        self.assertEqual(self.origin_tags(), {'v0.1.0': head})     # pushed, at the released sha

    def test_none_within_the_interval_and_several_a_day_past_it(self):
        first = self.release()
        self.assertIsNotNone(first)
        head = self.commit('pkg/b.py', 'b = 1\n', 'fix(B-0001): banner')
        self.assertIsNone(self.release(later=dt.timedelta(minutes=30)))     # 30m after: too soon
        self.assertIsNone(self.release(later=dt.timedelta(minutes=29)))     # 59m after: still too soon
        second = self.release(later=dt.timedelta(minutes=2))                # 61m after, the same day
        self.assertEqual(os.path.basename(second or ''), f'{DAY}-{head[:7]}.md')
        self.assertIn('\nversion: v0.1.1\n', self.read(os.path.relpath(second, self.root)))
        self.assertEqual(self.origin_tags()['v0.1.1'], head)
        log = self.changelog()
        self.assertLess(log.index(f'## v0.1.1 — {DAY}'), log.index(f'## v0.1.0 — {DAY}'))
        # a third the same day: its notes start after the second, whatever the sha7s sort as
        third = self.commit('pkg/c.py', 'c = 1\n', 'task(T-0002): sign-up form')
        path = self.release()
        text = self.read(os.path.relpath(path, self.root))
        self.assertIn('\nversion: v0.1.2\n', text)
        self.assertNotIn('B-0001', text)
        self.assertEqual(self.origin_tags()['v0.1.2'], third)
        self.assertEqual(len(os.listdir(os.path.join(self.root, 'releases'))), 3)
        self.assertIsNone(self.release())                           # nothing landed since

    def test_the_interval_is_a_convention(self):
        self.product.conventions.extra['release_min_interval'] = '3h'
        self.assertEqual(metrics.release_min_interval_s(self.product), 3 * 3600)
        self.release()
        self.commit('pkg/b.py', 'b = 1\n', 'fix(B-0001): banner')
        self.assertIsNone(self.release())                           # 2h < 3h
        self.assertIsNotNone(self.release())
        for value, seconds in ((None, 3600), ('15m', 900), ('30s', 30), ('1d', 86400), (5, 300),
                               ('0m', 0), ('soon', 3600)):
            self.product.conventions.extra['release_min_interval'] = value
            self.assertEqual(metrics.release_min_interval_s(self.product), seconds, value)

    def test_no_release_for_a_day_older_than_the_newest(self):
        self.release()
        self.commit('pkg/b.py', 'b = 1\n', 'fix(B-0001): banner')
        self.assertIsNone(self.release('2026-09-20'))

    def test_the_next_release_is_the_next_patch(self):
        self.release()
        head = self.commit('pkg/b.py', 'b = 1\n', 'fix(B-0001): banner')
        path = self.release('2026-09-22')
        text = self.read(os.path.relpath(path, self.root))
        self.assertIn('- [B-0001](../bugs/B-0001.md) Banner shows', text)
        self.assertNotIn('T-0001', text)                            # already in the first release
        self.assertIn('\nversion: v0.1.1\n', text)
        self.assertEqual(self.origin_tags()['v0.1.1'], head)
        self.assertIsNone(self.release('2026-09-23'))               # nothing landed since

    def test_a_new_active_epic_rolls_the_minor_over(self):
        self.set_epics(('E-0002', 'Product 0.1 — first', 'Active', 1), ('E-0003', 'Product 0.2 — next', 'New', 2))
        self.release()
        self.commit('pkg/b.py', 'b = 1\n', 'fix(B-0001): banner')
        self.release('2026-09-22')
        self.items['E-0002']['state'] = 'Closed'
        head = self.commit('pkg/c.py', 'c = 1\n', 'task(T-0002): sign-up form')
        self.release('2026-09-23')
        self.assertEqual(self.origin_tags()['v0.2.0'], head)
        self.assertIn('v0.1.1', self.origin_tags())

    def test_release_notes_group_features_bugs_and_item_less_commits(self):
        notes = metrics.render_notes(self.items, {'T-0001': [], 'S-0001': [], 'B-0001': [], 'E-0001': []},
                                     ['hotfix(install): one installer'], 'install it @v1.2.3')
        self.assertEqual(notes, '### Features landed\n\n- F-0001 Free plan\n\n'
                                '### Bugs fixed\n\n- B-0001 Banner shows\n\n'
                                '### Improvements and hotfixes\n\n- hotfix(install): one installer\n\n'
                                '### Upgrade\n\n`install it @v1.2.3`\n')
        many = metrics.render_notes(self.items, {}, [f'c{i}' for i in range(metrics.NOTES_MAX_COMMITS + 3)])
        self.assertIn('- … and 3 more', many)

    def test_the_notes_reach_the_note_the_tag_and_the_changelog(self):
        self.commit('pkg/h.py', 'h = 1\n', 'hotfix(pkg): a quick one')
        path = self.release()
        text = self.read(os.path.relpath(path, self.root))
        notes = metrics.notes_of(text)
        self.assertIn('- F-0001 Free plan', notes)
        self.assertIn('- hotfix(pkg): a quick one', notes)
        self.assertIn('pipx install --force "git+https://github.com/x/y.git@v0.1.0"', notes)
        self.assertIn('### Features landed', _git(self.origin, 'tag', '-l', '--format=%(contents)', 'v0.1.0'))
        self.assertTrue(self.changelog().startswith('# Changelog'))
        self.assertIn(f'## v0.1.0 — {DAY}\n\n{notes}', self.changelog() + '\n')

    def test_the_changelog_is_newest_first_and_idempotent(self):
        self.release()
        self.commit('pkg/b.py', 'b = 1\n', 'fix(B-0001): banner')
        self.release('2026-09-22')
        log = self.changelog()
        self.assertLess(log.index('## v0.1.1'), log.index('## v0.1.0'))
        before = _git(self.origin, 'rev-parse', 'main')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(metrics.sync_changelog(self.root, self.product))
        self.assertEqual(_git(self.origin, 'rev-parse', 'main'), before)          # nothing to add
        self.assertEqual(metrics.merge_changelog(log + '\n', {'v0.1.0': 'x'}), log + '\n')
        merged = metrics.merge_changelog('# Log\n\n## v0.1.0 — d\n\nold\n', {'v0.1.2': '## v0.1.2 — e\n\nnew\n'})
        self.assertEqual(merged, '# Log\n\n## v0.1.2 — e\n\nnew\n\n## v0.1.0 — d\n\nold\n')
        # the changelog commit is not an improvement of the next release
        subject = _git(self.origin, 'log', '-1', '--format=%s', 'main')
        self.assertTrue(metrics.CHANGELOG_SUBJECT_RE.match(subject), subject)
        self.assertEqual(metrics.improvement_commits(self.repo, self.items, 'origin/main', 'origin/main~1'), [])

    # ---- the migration --------------------------------------------------------

    def test_a_legacy_release_tag_is_adopted_as_the_next_patch_once(self):
        first = _git(self.repo, 'rev-parse', 'HEAD')
        _git(self.repo, 'tag', '-a', 'v0.1.0', first, '-m', 'Release')
        _git(self.repo, 'push', '-q', 'origin', 'refs/tags/v0.1.0')
        with open(os.path.join(self.root, 'releases', f'2026-09-20-{first[:7]}.md'), 'w') as f:
            f.write(f'# Release 2026-09-20 · {first[:7]}\n\nTag `v0.1.0`.\n')
        head = self.commit('pkg/b.py', 'b = 1\n', 'fix(B-0001): banner')
        legacy = self.legacy_release(DAY, head)
        with contextlib.redirect_stdout(io.StringIO()):
            done = metrics.migrate_releases(self.root, self.items, self.product)
        self.assertEqual([t for _p, t in done], ['v0.1.0', 'v0.1.1'])
        tags = self.origin_tags()
        self.assertEqual((tags['v0.1.1'], tags[legacy]), (head, head))         # the old tag is kept
        text = self.read(f'releases/{DAY}-{head[:7]}.md')
        self.assertIn('\nversion: v0.1.1\n', text)
        self.assertIn(f'adopted from `{legacy}`', text)
        self.assertIn('- B-0001 Banner shows', metrics.notes_of(text))
        self.assertIn('\nversion: v0.1.0\n', self.read(f'releases/2026-09-20-{first[:7]}.md'))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(metrics.migrate_releases(self.root, self.items, self.product), [])   # idempotent
        self.assertEqual(sorted(self.origin_tags()), sorted(['v0.1.0', 'v0.1.1', legacy]))
        # the rollup runs it, then files both versions' notes, then cuts the next patch
        nxt = self.commit('pkg/c.py', 'c = 1\n', 'task(T-0002): sign-up form')
        self.release('2026-09-22')
        self.assertEqual(self.origin_tags()['v0.1.2'], nxt)
        log = self.changelog()
        self.assertLess(log.index('## v0.1.2'), log.index('## v0.1.1'))
        self.assertLess(log.index('## v0.1.1'), log.index('## v0.1.0'))

    # ---- the GitHub Release ---------------------------------------------------

    def test_a_github_release_is_skipped_quietly_without_gh(self):
        hosted = env.Product('hosted', {'repo_dir': self.repo, 'repo_slug': 'x/y', 'main': 'main'})
        err = io.StringIO()
        with mock.patch.object(metrics.shutil, 'which', return_value=None), \
                mock.patch.object(metrics, 'gh') as gh, contextlib.redirect_stderr(err):
            self.assertFalse(metrics.publish_github_release(hosted, 'v0.1.0', 'notes'))
        gh.assert_not_called()
        self.assertIn('gh not found; no GitHub release for v0.1.0', err.getvalue())
        with mock.patch.object(metrics, 'gh') as gh:                       # no hosted repo: not asked
            self.assertFalse(metrics.publish_github_release(self.product, 'v0.1.0', 'notes'))
        gh.assert_not_called()

    def test_a_github_release_is_created_once_and_offline_is_logged(self):
        hosted = env.Product('hosted', {'repo_dir': self.repo, 'repo_slug': 'x/y', 'main': 'main'})
        calls = []

        def fake_gh(args, *a, **kw):
            calls.append(args[:2])
            return None if args[1] == 'view' else ''
        with mock.patch.object(metrics.shutil, 'which', return_value='/bin/gh'), \
                mock.patch.object(metrics, 'gh', side_effect=fake_gh):
            self.assertTrue(metrics.publish_github_release(hosted, 'v0.1.0', 'notes'))
        self.assertEqual(calls, [['release', 'view'], ['release', 'create']])
        err = io.StringIO()
        with mock.patch.object(metrics.shutil, 'which', return_value='/bin/gh'), \
                mock.patch.object(metrics, 'gh', return_value=None), contextlib.redirect_stderr(err):
            self.assertFalse(metrics.publish_github_release(hosted, 'v0.1.0', 'notes'))
        self.assertIn('no GitHub release for v0.1.0', err.getvalue())


LOG = """23:50 MERGE: batch A → main abc1234
00:05 tick-0100 start
00:06 LAND: batch B worktree-m-batch-20260921-0006 cut from main (#11 #12 #13)
00:07 WAVE 0005: 2 launches — accta 1; acctb 1; quota after: accta 12%/34%; acctb 5%/6%;
00:08 REFUSED batch C on: apps/web/x.ts
00:09 RELAUNCH: fix-a-r1 → fix-a-r2
00:10 MERGE: batch B → main deadbee
00:10 MERGE: #580 no service containers → main
00:11 LAUNCH: fix-free-plan-t3-r1 on accta
06:00 tick-0101 start
06:03 WAVE 0600: nothing ready
"""


SAMPLE_LOG = """03:21 REFUSED-WRITE: session fix-spec-api-docs-r2: UNIQUE constraint failed: sessions.id
03:21 WAVE 0308: 25 launches — accta 8; acctb 12; acctc 5;  quota after: acctc 7%/64%; accta 0%/34%; acctb 34%/27%; acctd 0%/68%;
03:25 LAUNCH OK acctd launch-fix-eu-compliance-t7-gate-acctd: 1/1 running
03:35 WAVE 0332: revise cap (3/wave) — published-limits waits
03:35 WAVE 0332: revise cap (3/wave) — routine-heartbeat waits
03:36 LAUNCH OK acctc launch-wave0332-acctc: 1/1 running
03:37 LAUNCH OK acctd launch-wave0332-acctd: 2/2 running
03:37 REFUSED-WRITE: I9: a working review session is already on cloud/azure-eu-routes
03:40 LAUNCH OK acctb launch-wave0332-acctb: 9/9 running
03:40 REFUSED-WRITE: I9: a working review session is already on cloud/vendorx-lane-docs
03:41 REFUSED-WRITE: I9: a working fix session is already on cloud/plan-published-limits
03:41 WAVE 0332: 12 launches — acctd 2; acctb 9; acctc 1;  quota after: acctc 11%/64%; accta 5%/35%; acctb 39%/28%; acctd 4%/69%;
03:42 LAUNCH OK acctc launch-ci-16-site-deploy-acctc: 1/1 running
03:47 LAUNCH OK acctc launch-rebase-allowance-v3-acctc: 1/1 running
03:54 LAND: batch A0354 (683 680 679 677 676 675 674 673) REFUSED — merge-queue: refusing the batch — #674 conflicts with #673 on: .github/workflows/ci.yml
03:55 LAND: batch A0354 (683 680 679 677 676 675 673) REFUSED — merge-queue: refusing the batch — #683 conflicts with the batch so far on: docs/superpowers/plans/2026-09-19-transparent-meter.md
03:56 WAVE 0352: revise cap (3/wave) — starter-bots waits
03:57 LAUNCH OK acctd launch-wave0352-acctd: 2/2 running
03:57 REFUSED-WRITE: I9: a working review session is already on cloud/derived-register-t5
04:02 LAUNCH OK accta launch-wave0352-accta: 11/11 running
04:03 REFUSED-WRITE: I9: a working fix session is already on cloud/plan-published-limits
04:04 REFUSED-WRITE: session fix-spec-api-docs-r2: UNIQUE constraint failed: sessions.id
04:04 WAVE 0352: 25 launches — acctd 2; accta 11; acctb 12;  quota after: acctc 22%/65%; accta 10%/36%; acctb 49%/30%; acctd 7%/70%;
"""


class SessionEventTests(Base):
    RECORD = {'job': 'p2-t1', 'account': 'accta', 'started': '2026-09-21T06:00:00Z',
              'ended': '2026-09-21T06:10:00Z', 'end_reason': 'done'}

    def event(self, result):
        return metrics.session_event(self.RECORD, result, self.items)

    def test_input_tokens_are_the_sum_of_the_three(self):
        ev = self.event({'type': 'result', 'num_turns': 9,
                         'usage': {'input_tokens': 10, 'cache_creation_input_tokens': 5,
                                   'cache_read_input_tokens': 100, 'output_tokens': 7}})
        self.assertEqual((ev['in_tokens'], ev['turns']), (115, 9))

    def test_no_usage_is_null_not_zero(self):
        ev = self.event({'type': 'result'})
        self.assertEqual((ev['in_tokens'], ev['turns']), (None, None))
        self.assertEqual((self.event(None)['in_tokens'], self.event(None)['turns']), (None, None))

    def test_the_schema_takes_them(self):
        ev = metrics.validate('sessions', dict(self.event({'num_turns': 3, 'usage': {'input_tokens': 4}}),
                                               item=None), {})
        self.assertEqual((ev['in_tokens'], ev['turns']), (4, 3))
        old = metrics.validate('sessions', {'task': 't', 'account': 'a', 'result': 'done'}, {})
        self.assertEqual((old['in_tokens'], old['turns']), (None, None))


class Backfill(Base):
    NOW = dt.datetime(2026, 9, 21, 6, 30).astimezone()

    def recs(self):
        return import_sessions.parse_log(LOG.splitlines(), now=self.NOW)

    def test_log_days_are_inferred_from_the_clock_wrapping(self):
        recs = self.recs()
        self.assertEqual(recs[0][0].date().isoformat(), '2026-09-20')      # 23:50 came before midnight
        self.assertEqual(recs[1][0].date().isoformat(), '2026-09-21')
        self.assertEqual(recs[-1][0].strftime('%Y-%m-%d %H:%M'), '2026-09-21 06:03')
        # a log whose last line is later than now ended yesterday
        late = import_sessions.parse_log(['22:00 a', '23:00 b'], now=dt.datetime(2026, 9, 21, 6, 30).astimezone())
        self.assertEqual(late[-1][0].date().isoformat(), '2026-09-20')

    def test_batch_pr_lists_come_from_the_cut_lines(self):
        self.assertEqual(import_sessions.batch_prs_from_log(self.recs()), {'worktree-m-batch-20260921-0006': [11, 12, 13]})

    def test_ticks_from_the_log(self):
        ticks = import_sessions.ticks_from_log(self.recs())
        self.assertEqual([t['tick'] for t in ticks], [5, 600])
        t = ticks[0]
        self.assertEqual((t['launches'], t['merges'], t['refusals'], t['relaunches'], t['stalls']), (2, 4, 1, 1, 0))
        self.assertEqual(t['refused_files'], {'apps/web/x.ts': 1})
        self.assertEqual(t['quota'], {'accta': {'h5': 12, 'd7': 34}, 'acctb': {'h5': 5, 'd7': 6}})
        self.assertEqual(t['duration_s'], 360)
        self.assertEqual(ticks[1]['launches'], 0)
        for ev in ticks:
            metrics.validate('ticks', ev, self.items)

    def test_one_tick_per_wave_on_the_real_log_shapes(self):
        lines = SAMPLE_LOG.splitlines()
        recs = import_sessions.parse_log(lines, now=dt.datetime(2026, 9, 21, 4, 30).astimezone())
        ticks = import_sessions.ticks_from_log(recs)
        self.assertEqual(len([l for l in lines if re.search(r'WAVE \d{4}: \d+ launches', l)]), 3)
        self.assertEqual([t['tick'] for t in ticks], [308, 332, 352])
        by = {t['tick']: t for t in ticks}
        self.assertEqual((by[308]['launches'], by[332]['launches'], by[352]['launches']), (25, 12, 25))
        # 03:21 is 13 min from 0308 and 11 from 0332: neither window takes it
        self.assertEqual(by[308]['refusals'], 0)
        self.assertEqual(by[332]['refusals'], 0)            # REFUSED-WRITE is a store write, not a refusal
        self.assertEqual(by[352]['refusals'], 2)            # the two 03:54/03:55 batch refusals
        self.assertEqual(by[352]['refused_files'], {'.github/workflows/ci.yml': 1,
                                                     'docs/superpowers/plans/2026-09-19-transparent-meter.md': 1})
        self.assertEqual(by[332]['quota']['acctb'], {'h5': 39, 'd7': 28})
        self.assertEqual(by[352]['quota']['acctc'], {'h5': 22, 'd7': 65})
        for ev in ticks:
            metrics.validate('ticks', ev, self.items)

    def test_stalls_and_windows(self):
        lines = ['01:06 TICK: tick-0038 job hung since 00:40 (no log writes) — killed; tick-0106 spawned',
                 '01:07 RELAUNCH: fix-a-r1 → fix-a-r2', '01:08 WAVE 0100: 3 launches — accta 3; ',
                 '01:20 RELAUNCH: fix-b-r1 → fix-b-r2', '01:21 WAVE 0120: nothing ready']
        ticks = {t['tick']: t for t in import_sessions.ticks_from_log(
            import_sessions.parse_log(lines, now=dt.datetime(2026, 9, 21, 2, 0).astimezone()))}
        self.assertEqual((ticks[100]['stalls'], ticks[100]['relaunches'], ticks[100]['launches']), (1, 1, 3))
        self.assertEqual((ticks[120]['stalls'], ticks[120]['relaunches'], ticks[120]['launches']), (0, 1, 0))

    def test_backfill_ticks_are_idempotent_on_tick(self):
        recs = import_sessions.parse_log(SAMPLE_LOG.splitlines(), now=dt.datetime(2026, 9, 21, 4, 30).astimezone())
        for _ in range(2):
            for ev in import_sessions.ticks_from_log(recs):
                metrics.append_event(self.root, 'ticks', metrics.validate('ticks', ev, self.items))
        self.assertEqual(len(metrics.read_stream(self.root, 'ticks')), 3)

    def write_results(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, 'accta'))
        with open(os.path.join(d, 'accta', 'dispatched.json'), 'w') as f:
            json.dump([{'id': 'fix-free-plan-t3-r1', 'model': 'claude-sonnet-5', 'status': 'running'}], f)
        res = os.path.join(d, 'results.jsonl')
        with open(res, 'w') as f:
            f.write(json.dumps({'task': 'fix-free-plan-t3-r1', 'account': 'accta', 'branch': 'cloud/free-plan-t3',
                                'result': 'failed', 'reason': 'x', 'sha': 'abc', 'ended_at': '2026-09-21T06:20:00+0200'}) + '\n')
            f.write(json.dumps({'task': 'fix-free-plan-t3-r1', 'account': 'accta', 'branch': 'cloud/free-plan-t3',
                                'result': 'done', 'reason': None, 'sha': 'abc', 'ended_at': '2026-09-21T06:20:00+0200'}) + '\n')
            f.write('garbage\n')
            f.write(json.dumps({'task': 'old', 'account': 'accta', 'result': 'done', 'ended_at': '2026-01-01T00:00:00+0100'}) + '\n')
        return d, res

    def write_registry(self, home):
        """A session registry and a job log, exactly as asf.workers.pool/runtime write them."""
        state = os.path.join(home, 'state', 'sample')
        logs = os.path.join(home, 'logs', 'jobs', 'sample')
        os.makedirs(state)
        os.makedirs(logs)
        with open(os.path.join(state, 'sessions.jsonl'), 'w') as f:
            f.write(json.dumps({'job': 'fix-free-plan-t3-r1', 'item': 'T-0001', 'kind': 'fix-bug',
                                'account': 'acct-a', 'model': 'claude-sonnet-5',
                                'branch': 'feature/free-plan-t3',
                                'started': '2026-09-21T04:16:00Z'}) + '\n')
            f.write(json.dumps({'job': 'fix-free-plan-t3-r1', 'ended': '2026-09-21T04:20:00Z',
                                'end_reason': 'finished'}) + '\n')
            f.write(json.dumps({'job': 'still-running', 'item': 'T-0002', 'kind': 'task',
                                'account': 'acct-a', 'started': '2026-09-21T04:16:00Z'}) + '\n')
        with open(os.path.join(logs, 'fix-free-plan-t3-r1.jsonl'), 'w') as f:
            f.write(json.dumps({'type': 'system', 'subtype': 'init'}) + '\n')
            f.write(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                                'result': 'fixed', 'total_cost_usd': 0.42,
                                'duration_ms': 240000}) + '\n')
        return state, logs

    def test_sessions_come_from_the_registry_and_the_job_log(self):
        d, _res = self.write_results()
        home = os.path.join(d, 'asf-home2')
        self.write_registry(home)
        product = env.Product('sample', {})
        with mock.patch.object(env, 'ASF_HOME', home):
            evs = metrics.sessions_from_registry(product, since_day='2026-09-20', items=self.items)
        self.assertEqual(len(evs), 1)                  # the session still running is not an event
        ev = evs[0]
        # the record may be public: the account is an index into the pool, never a name (B-0023)
        self.assertEqual((ev['task'], ev['result'], ev['kind'], ev['item'], ev['account']),
                         ('fix-free-plan-t3-r1', 'finished', 'fix', 'T-0001', 'a?'))
        self.assertNotIn('acct-a', json.dumps(ev))
        self.assertEqual((ev['minutes'], ev['usd'], ev['reason']), (4.0, 0.42, 'fixed'))
        self.assertEqual(ev['ts'], '2026-09-21T04:20:00Z')
        metrics.validate('sessions', ev, self.items)   # it is a valid event as it stands

    def test_account_index_and_capped_reason(self):
        with mock.patch.object(env, 'load_config',
                               lambda: {'worker_pool': {'accounts': [{'name': 'x1'}, {'name': 'x2'}]}}):
            self.assertEqual(metrics.account_index('x2'), 'a2')
            self.assertEqual(metrics.account_index('nobody'), 'a?')
            self.assertEqual(metrics.account_index(None), 'a?')
        d, _res = self.write_results()
        home = os.path.join(d, 'asf-home3')
        state, logs = self.write_registry(home)
        with open(os.path.join(logs, 'fix-free-plan-t3-r1.jsonl'), 'w') as f:
            f.write(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                                'result': 'first line ' + 'x' * 400 + '\nsecond line with a path /home/someone',
                                'total_cost_usd': 0.1, 'duration_ms': 1000}) + '\n')
        product = env.Product('sample', {})
        with mock.patch.object(env, 'ASF_HOME', home):
            evs = metrics.sessions_from_registry(product, since_day='2026-09-20', items=self.items)
        self.assertEqual(len(evs[0]['reason']), 200)
        self.assertNotIn('second line', evs[0]['reason'])

    def test_import_sessions_reads_a_legacy_results_file(self):
        d, res = self.write_results()
        log = os.path.join(d, 'runner.log')
        with open(log, 'w') as f:
            f.write(LOG)
        counts = import_sessions.import_file(self.root, res, 'legacy-results', log=log,
                                             launch_dir=d, since_day='2026-09-20',
                                             now=self.NOW)
        self.assertEqual(counts['sessions appended'], 1)
        ev = json.loads(self.read('metrics/sessions/2026-09-21.jsonl').strip())
        self.assertEqual((ev['task'], ev['result'], ev['model'], ev['item']),
                         ('fix-free-plan-t3-r1', 'done', 'claude-sonnet-5', 'T-0001'))
        # a second import of the same file changes nothing
        again = import_sessions.import_file(self.root, res, 'legacy-results', log=log,
                                            launch_dir=d, since_day='2026-09-20', now=self.NOW)
        self.assertEqual(again['sessions exists'], 1)

    def test_import_sessions_reads_a_legacy_runner_log(self):
        d, _res = self.write_results()
        log = os.path.join(d, 'runner.log')
        with open(log, 'w') as f:
            f.write(LOG)
        counts = import_sessions.import_file(self.root, log, 'legacy-log', since_day='2026-09-20',
                                             now=self.NOW)
        self.assertEqual(counts['ticks appended'], 2)

    def test_sessions_from_results(self):
        d, res = self.write_results()
        recs = self.recs()
        evs = import_sessions.sessions_from_results(res, d, recs, self.items, '2026-09-20')
        self.assertEqual(len(evs), 1)                       # last line per task wins; the January one is out of the window
        ev = evs[0]
        self.assertEqual((ev['task'], ev['result'], ev['model'], ev['ts']),
                         ('fix-free-plan-t3-r1', 'done', 'claude-sonnet-5', '2026-09-21T04:20:00Z'))
        self.assertEqual(ev['minutes'], round((dt.datetime(2026, 9, 21, 4, 20, tzinfo=dt.timezone.utc)
                                               - recs[-3][0]).total_seconds() / 60, 1))
        out = metrics.validate('sessions', ev, self.items)
        self.assertEqual((out['kind'], out['round'], out['item']), ('fix', 1, 'T-0001'))

    def fake_gh_lines(self, args, timeout=300):
        joined = ' '.join(args)
        if '/jobs' in joined:
            run = int(joined.split('/runs/')[1].split('/')[0])
            if run == 1:
                return [{'name': 'lint', 'conclusion': 'success', 'runner_name': 'box1', 'started_at': '2026-09-21T01:00:00Z',
                         'completed_at': '2026-09-21T01:04:30Z', 'failed': []},
                        {'name': 'e2e', 'conclusion': 'failure', 'runner_name': 'box2', 'started_at': '2026-09-21T01:00:00Z',
                         'completed_at': '2026-09-21T01:06:00Z', 'failed': ['Run tests']}]
            return [{'name': 'lint', 'conclusion': 'cancelled', 'runner_name': 'box1', 'started_at': '2026-09-21T02:00:00Z',
                     'completed_at': '2026-09-21T02:03:00Z', 'failed': []}]
        return [{'id': 1, 'name': 'ci', 'head_branch': 'cloud/free-plan-t3', 'head_sha': 'e' * 40, 'conclusion': 'failure',
                 'created_at': '2026-09-21T01:00:00Z', 'updated_at': '2026-09-21T01:07:00Z', 'run_attempt': 1, 'pr': [623]},
                {'id': 2, 'name': 'ci', 'head_branch': 'worktree-m-batch-20260921-0006', 'head_sha': 'f' * 40,
                 'conclusion': 'cancelled', 'created_at': '2026-09-21T02:00:00Z', 'updated_at': '2026-09-21T02:03:00Z',
                 'run_attempt': 1, 'pr': []},
                {'id': 3, 'name': 'dco', 'head_branch': 'x', 'head_sha': 'e' * 40, 'conclusion': 'success',
                 'created_at': '2026-09-21T01:00:00Z', 'updated_at': '2026-09-21T01:01:00Z', 'run_attempt': 1, 'pr': []}]

    def test_ci_backfill_and_idempotence(self):
        d, _res = self.write_results()
        argv = ['backfill', '--days', '2']
        prs = {623: {'merged': True, 'merge_commit_sha': '2' * 40, 'body': '', 'title': 'x'},
               11: {'title': 'T-0002 form', 'body': '', 'merged': True, 'merge_commit_sha': None},
               12: None, 13: None}
        # no ~/.ASF is present in CI: stub the product lookup so ci_from_api's repo-slug resolution
        # (used only to build the (mocked) gh_lines request, never a real API call) doesn't need one.
        # the merge-batch lane is a branch prefix this product happens to have, not a literal
        stub_product = env.Product('sample', {
            'repo_slug': 'sample/sample',
            'conventions': {'branch_prefixes': {'code': 'feature/', 'batch': 'worktree-m-batch-'}}})
        home = os.path.join(d, 'asf-home')
        self.write_registry(home)
        with mock.patch.object(metrics, 'gh_lines', side_effect=self.fake_gh_lines), \
                mock.patch.object(metrics, 'pr_info', side_effect=lambda n, *a, **kw: prs.get(n)), \
                mock.patch.object(metrics, 'today', return_value='2026-09-21'), \
                mock.patch.object(env, 'ASF_HOME', home), \
                mock.patch.object(env, 'load_product', return_value=stub_product):
            rc, out, _ = run_cli(self.root, *argv)
            self.assertEqual(rc, 0)
            self.assertIn('ci appended: 2', out)
            self.assertIn('sessions appended: 1', out)
            snap = {p: self.read(p) for p in ('metrics/ci/2026-09-21.jsonl',)}
            rc, out, _ = run_cli(self.root, *argv)
            self.assertIn('ci exists: 2', out)
            self.assertNotIn('appended', out)
            sessions = [json.loads(l) for l in self.read('metrics/sessions/2026-09-21.jsonl').splitlines()]
            self.assertEqual([(e['task'], e['result'], e['kind'], e['usd'], e['minutes']) for e in sessions],
                             [('fix-free-plan-t3-r1', 'finished', 'fix', 0.42, 4.0)])
            self.assertEqual(sessions[0]['item'], 'T-0001')
        for p, text in snap.items():
            self.assertEqual(self.read(p), text)
        ci = [json.loads(l) for l in snap['metrics/ci/2026-09-21.jsonl'].splitlines()]
        red = next(e for e in ci if e['run'] == 1)
        self.assertEqual((red['minutes'], red['pr'], red['items'], red['conclusion']), (10, 623, ['T-0001'], 'failure'))
        self.assertEqual([j['failed_step'] for j in red['jobs']], [None, 'Run tests'])
        batch = next(e for e in ci if e['run'] == 2)
        # the branch is recognised as the batch lane by the product's `batch` prefix; with no PR
        # list for it (that came from the previous runner's log, now an import) it matches no item
        self.assertEqual((batch['batch'], batch['cancelled_minutes'], batch['items'], batch['item_reason']),
                         ('worktree-m-batch-20260921-0006', 3, None, 'batch run without a PR list'))
        self.assertFalse(batch['superseded'])           # no later run on that branch


if __name__ == '__main__':
    unittest.main()


TOKEN_KEYS = ('tokens_input', 'tokens_output', 'tokens_cache_read', 'tokens_cache_write')


def tok_session(item, **dims):
    ev = {'ts': f'{DAY}T09:00:00Z', 'task': 't', 'account': 'a1', 'kind': 'code', 'result': 'finished', 'item': item}
    ev.update({f'tokens_{d}': n for d, n in dims.items()})
    return ev


class SessionTokensTest(Base):
    def registry_events(self, result_record, extra_lines=()):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        home = os.path.join(d, 'asf-home2')
        _state, logs = Backfill.write_registry(self, home)
        with open(os.path.join(logs, 'fix-free-plan-t3-r1.jsonl'), 'w') as f:
            for rec in (*extra_lines, result_record):
                f.write(json.dumps(rec) + '\n')
        with mock.patch.object(env, 'ASF_HOME', home):
            return metrics.sessions_from_registry(env.Product('sample', {}), since_day='2026-09-20', items=self.items)

    def test_every_session_line_carries_four_dimensions(self):
        [ev] = self.registry_events({'type': 'result', 'result': 'fixed', 'total_cost_usd': 0.42,
                                     'usage': {'input_tokens': 10, 'output_tokens': 20,
                                               'cache_read_input_tokens': 300, 'cache_creation_input_tokens': 40}})
        self.assertEqual([ev[k] for k in TOKEN_KEYS], [10, 20, 300, 40])
        metrics.validate('sessions', ev, self.items)

    def test_a_session_with_no_usage_carries_four_nulls(self):
        [ev] = self.registry_events({'type': 'result', 'result': 'fixed', 'total_cost_usd': 0.42})
        self.assertEqual([ev[k] for k in TOKEN_KEYS], [None] * 4)
        self.assertEqual(ev['usd'], 0.42)

    def test_a_capped_session_has_tokens_and_no_money(self):
        from asf import tokens
        capped = tokens.cap_result('code', 'output', 1100, 1000,
                                   {'input': 5, 'output': 1100, 'cache_read': None, 'cache_write': 7}, None)
        [ev] = self.registry_events(capped)
        self.assertIsNone(ev['usd'])
        self.assertEqual([ev[k] for k in TOKEN_KEYS], [5, 1100, None, 7])
        self.assertTrue(ev['reason'].startswith('token cap:'), ev['reason'])

    def test_the_event_has_no_total(self):
        [ev] = self.registry_events({'type': 'result', 'usage': {'input_tokens': 1}})
        self.assertFalse([k for k in ev if re.search(r'^tokens$|total_tokens|tokens_total', k)])
        with self.assertRaises(metrics.SchemaError):
            metrics.validate('sessions', dict(ev, tokens=1), self.items)

    def test_an_old_line_still_reads(self):
        old = {'ts': f'{DAY}T09:00:00Z', 'task': 't', 'account': 'a1', 'kind': 'code', 'result': 'finished',
               'item': 'T-0001', 'usd': 1.0}
        path = os.path.join(self.root, 'metrics', 'sessions', f'{DAY}.jsonl')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(json.dumps(old) + '\n')
        evs = metrics.read_stream(self.root, 'sessions', [DAY])
        self.assertEqual(len(evs), 1)
        c = metrics.compute_costs([], evs)['T-0001']
        self.assertEqual(c['tokens'], {'input': None, 'output': None, 'cache_read': None, 'cache_write': None})


class ScorecardTokensTest(Base):
    def costs_table(self, sessions):
        return metrics.cost_table(self.items, [], sessions)

    def test_cost_table_shows_four_columns_beside_usd(self):
        lines = self.costs_table([
            dict(tok_session('T-0001', input=1500, output=20, cache_read=2_500_000, cache_write=7), usd=1.0),
            dict(tok_session('T-0002', input=500, output=5, cache_read=500_000, cache_write=3), usd=2.0)])
        self.assertTrue(lines[0].endswith('| USD | In | Out | Cache rd | Cache wr |'))
        row = next(l for l in lines if l.startswith('| F-0001'))
        self.assertTrue(row.endswith('| 3.00 | 2.0 k | 25 | 3.0 M | 10 |'), row)

    def test_a_dimension_no_session_carried_is_a_dash(self):
        lines = self.costs_table([tok_session('T-0001', input=42)])
        row = next(l for l in lines if l.startswith('| F-0001'))
        self.assertTrue(row.endswith('| 42 | — | — | — |'), row)

    def test_the_all_row_never_adds_across_dimensions(self):
        lines = self.costs_table([tok_session('T-0001', input=1000, output=2000, cache_read=3000, cache_write=4000),
                                  tok_session(None, input=1000, output=2000, cache_read=3000, cache_write=4000)])
        row = next(l for l in lines if l.startswith('| **All**'))
        self.assertTrue(row.endswith('| 2.0 k | 4.0 k | 6.0 k | 8.0 k |'), row)
        self.assertNotIn('20.0 k', row)

    def test_the_waste_table_itemises_the_day(self):
        sessions = [tok_session('T-0001', input=1000, output=2000, cache_read=3000, cache_write=4000),
                    dict(tok_session('T-0002', input=1), result='failed: token cap')]
        rows = {r[0]: r for r in metrics.scorecard_rows([], sessions, [])}
        self.assertEqual(rows['tokens'][1], 'in 1.0 k · out 2.0 k · cache rd 3.0 k · cache wr 4.0 k')
        self.assertEqual(rows['tokens'][2], '2 sessions, 1 capped')

    def test_the_rendered_scorecard_has_no_total(self):
        fixture_streams(self)
        md = metrics.render_daily(self.root, DAY, self.items)
        self.assertIsNone(re.search(r'total tokens|tokens total', md, re.I))


class CostBlockTokensTest(Base):
    NOW = dt.datetime(2026, 9, 21, 12, 0, 0, tzinfo=dt.timezone.utc)

    def card(self, iid):
        path = os.path.join(self.root, 'tasks', f'{iid}.md')
        meta, _body = frontmatter.parse(self.read(f'tasks/{iid}.md'), path=path)
        return meta

    def test_cost_block_carries_the_four_keys(self):
        sessions = [tok_session('T-0001', input=1, output=2, cache_read=3, cache_write=4)]
        metrics.write_costs(self.root, self.items, [], sessions, now=self.NOW)
        cost = self.card('T-0001')['cost']
        self.assertEqual((cost['tokens_input'], cost['tokens_output'], cost['tokens_cache_read'],
                          cost['tokens_cache_write']), (1, 2, 3, 4))
        self.assertIn('usd', cost)
        self.assertFalse([v for v in cost.values() if isinstance(v, dict)])

    def test_a_null_dimension_is_omitted_not_zero(self):
        metrics.write_costs(self.root, self.items, [], [tok_session('T-0001', output=9)], now=self.NOW)
        cost = self.card('T-0001')['cost']
        self.assertEqual(cost['tokens_output'], 9)
        for k in ('tokens_input', 'tokens_cache_read', 'tokens_cache_write'):
            self.assertNotIn(k, cost)

    def test_an_epic_subtree_adds_per_dimension(self):
        costs = metrics.compute_costs([], [tok_session('T-0001', input=1, output=10),
                                           tok_session('B-0001', input=2, cache_read=5)])
        e = metrics.subtree_cost(self.items, costs, 'E-0001')
        self.assertEqual(e['tokens'], {'input': 3, 'output': 10, 'cache_read': 5, 'cache_write': None})
        self.assertIsNone(e['usd'])


def _landed(state='Closed', hour=12, **kw):
    return dict({'state': state, 'stage_since': f'{DAY}T{hour:02d}:00:00Z'}, **kw)


def _sess(item, hour=8, usd=None, result='finished'):
    ev = {'ts': f'{DAY}T{hour:02d}:00:00Z', 'task': 't', 'account': 'a1', 'kind': 'code', 'result': result, 'item': item}
    if usd is not None:
        ev['usd'] = usd
    return ev


class DeliveredVsSingleTest(Base):
    def delivery(self):
        items = {'F-0100': _landed(delivers=['F-0100', 'B-0100', 'B-0101']),
                 'B-0100': _landed(delivered_by='F-0100'), 'B-0101': _landed(delivered_by='F-0100'),
                 'T-0100': _landed(), 'T-0101': _landed()}
        sessions = [_sess('F-0100', usd=1.5), _sess('F-0100', usd=1.5)]
        sessions += [_sess(i) for i in ('T-0100', 'T-0101') for _ in range(3)]
        return items, sessions

    def test_table_divides_a_delivery_over_its_items(self):
        rows = metrics.delivered_vs_single(*self.delivery(), DAY)
        d, s = rows['delivered'], rows['single']
        self.assertEqual((d['landed'], d['sessions'], round(d['sessions_per_item'], 2), d['usd'], d['usd_per_item']),
                         (3, 2, 0.67, 3.0, 1.0))
        self.assertEqual((s['landed'], s['sessions'], s['sessions_per_item'], s['usd']), (2, 6, 3.0, None))
        md = '\n'.join(metrics.delivered_table(rows))
        self.assertIn('| delivered | 3 | 2 | 0.67 | 3.00 | 1.00 |', md)
        self.assertIn('| single | 2 | 6 | 3.00 | — |', md)

    def test_hours_to_land_uses_stage_since(self):
        items = {'F-0100': _landed(hour=12, delivers=['F-0100', 'B-0100']),
                 'B-0100': _landed(hour=12, delivered_by='F-0100')}
        rows = metrics.delivered_vs_single(items, [_sess('F-0100', hour=8)], DAY)
        self.assertEqual(rows['delivered']['median_hours'], 4.0)
        self.assertIn('| 4.0 |', '\n'.join(metrics.delivered_table(rows)))

    def test_failed_share(self):
        items = {'T-0100': _landed()}
        sessions = [_sess('T-0100', result='failed: gate red')] + [_sess('T-0100') for _ in range(3)]
        rows = metrics.delivered_vs_single(items, sessions, DAY)
        self.assertIn('25 %', '\n'.join(metrics.delivered_table(rows)))

    def test_section_absent_when_nothing_landed(self):
        self.assertNotIn('## Delivered vs single', metrics.render_daily(self.root, DAY, self.items))
        rows = metrics.delivered_vs_single({'T-0100': _landed()}, [], DAY)
        self.assertEqual(rows['single']['landed'], 1)
        self.assertIsNone(rows['single']['median_hours'])
        self.assertIsNone(rows['delivered']['median_hours'])
