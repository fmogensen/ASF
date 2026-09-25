"""asf.scorecard — the value loop: facts, the scorecard's numbers, the diagnosis, filing, verifying.

Every expected number is worked out by hand in a comment beside it.
"""
import datetime
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from asf import env
from asf.groom.inbox import parse_inbox_file
from asf.improve.measure import Run
from asf.scorecard import diagnose, facts, loop, score
from asf.scorecard.facts import Facts

UTC = datetime.timezone.utc


def card_md(iid, typ, title, state, stage, history, parent='E-0001', extra='', body=''):
    hist = '\n'.join(f'- {h}' for h in history)
    return (f"---\nid: {iid}\ntype: {typ}\ntitle: \"{title}\"\nparent: {parent}\n{extra}"
            f"# ---- machine ----\nschema_version: 1\nstate: {state}\nstage: {stage}\n"
            f"stage_since: 2026-09-10T12:00:00Z\nupdated: 2026-09-10T12:00:00Z\n---\n"
            f"## Description\n{body or title}\n\n## History\n{hist}\n\n## Children\n\n## Backlinks\n")


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    return path


def run(job, ended, reason, landed=False, usd=1.0, minutes=10.0):
    return Run(job=job, kind='task', model='m', item=None, started=ended, ended=ended,
               minutes=minutes, landed=landed, end_reason=reason, usd=usd)


def items_fixture():
    """F-0001 carded 09-01, landed 09-03 10:00, on prod (Closed) 09-04 10:00; one Task under it;
    B-0001 an S1 filed 09-05 naming F-0001; F-0002 carded and still building."""
    return {
        'E-0001': {'id': 'E-0001', 'type': 'epic', 'parent': None, 'title': 'E', 'text': '',
                   'created': '2026-09-01T00:00:00Z', 'landed': None, 'prod': None,
                   'send_backs': 0, 'reopens': 0},
        'F-0001': {'id': 'F-0001', 'type': 'feature', 'parent': 'E-0001', 'title': 'one', 'text': '',
                   'created': '2026-09-01T00:00:00Z', 'landed': '2026-09-03T10:00:00Z',
                   'prod': '2026-09-04T10:00:00Z', 'send_backs': 1, 'reopens': 0},
        'T-0001': {'id': 'T-0001', 'type': 'task', 'parent': 'F-0001', 'title': 't', 'text': '',
                   'created': '2026-09-02T00:00:00Z', 'landed': '2026-09-03T10:00:00Z', 'prod': None,
                   'send_backs': 0, 'reopens': 1},
        'B-0001': {'id': 'B-0001', 'type': 'bug', 'parent': 'E-0001', 'title': 'b', 'severity': 'S1',
                   'text': 'broke after F-0001 landed', 'created': '2026-09-05T00:00:00Z',
                   'landed': None, 'prod': None, 'send_backs': 0, 'reopens': 0},
        'F-0002': {'id': 'F-0002', 'type': 'feature', 'parent': 'E-0001', 'title': 'two', 'text': '',
                   'created': '2026-09-02T00:00:00Z', 'landed': None, 'prod': None,
                   'send_backs': 0, 'reopens': 0},
    }


def sessions_fixture():
    return [
        {'ts': '2026-09-02T10:00:00Z', 'task': 'spec-f-0001', 'item': 'F-0001', 'usd': 4.0,
         'tokens_input': 10, 'tokens_output': 20, 'tokens_cache_read': 0, 'tokens_cache_write': 0,
         'minutes': 30},
        {'ts': '2026-09-02T12:00:00Z', 'task': 'coder-t-0001', 'item': 'T-0001', 'usd': 6.0, 'minutes': 60,
         'in_tokens': 500},
        {'ts': '2026-09-03T08:00:00Z', 'task': 'correct-t-0001', 'item': 'T-0001', 'usd': 2.0, 'minutes': 20},
        {'ts': '2026-09-03T09:00:00Z', 'task': 'review-t-0001', 'item': 'T-0001', 'usd': 1.0, 'minutes': 10},
        {'ts': '2026-09-05T09:00:00Z', 'task': 'fix-bug-b-0001', 'item': 'B-0001', 'usd': 3.0, 'minutes': 15},
        {'ts': '2026-09-06T09:00:00Z', 'task': 'spec-f-0002', 'item': 'F-0002', 'usd': 5.0, 'minutes': 25},
    ]


def facts_fixture(as_of='2026-09-06T23:00:00Z', **kw):
    base = dict(items=items_fixture(), sessions=sessions_fixture(),
                ci=[{'ts': '2026-09-03T09:30:00Z', 'items': ['T-0001', 'F-0002'], 'minutes': 10,
                     'jobs': [{'name': 'e2e', 'conclusion': 'failure', 'minutes': 10}]}],
                gates=[{'ts': '2026-09-03T09:40:00Z', 'branches': ['task/T-0001'], 'seconds': 120,
                        'conclusion': 'success'}],
                runs=[], clutter={'open_prs': 4, 'stale_prs': 1, 'branches': 2}, as_of=as_of)
    base.update(kw)
    return Facts(**base)


class TimelineTests(unittest.TestCase):
    def test_created_landed_and_prod_come_from_the_history(self):
        body = ("## History\n- 2026-09-01: created (inbox)\n"
                "- 2026-09-02 09:00 ingest: stage card → spec-review r1 (x)\n"
                "- 2026-09-02 10:00 ingest: stage spec-review r1 → spec-review r2 (x)\n"
                "- 2026-09-03 10:00 ingest: stage building 2/3 → landed (commit abc)\n"
                "- 2026-09-03 10:00 ingest: state Active → Resolved (x)\n"
                "- 2026-09-04 11:30 ingest: state Resolved → Closed (x)\n")
        t = facts.timeline({'state': 'Closed', 'stage': 'landed'}, body)
        self.assertEqual(t['created'], '2026-09-01T00:00:00Z')
        self.assertEqual(t['landed'], '2026-09-03T10:00:00Z')
        self.assertEqual(t['prod'], '2026-09-04T11:30:00Z')
        self.assertEqual(t['send_backs'], 1)        # r1 → r2
        self.assertEqual(t['reopens'], 0)

    def test_a_reopened_card_lands_when_it_lands_again(self):
        body = ("## History\n- 2026-09-01: created\n"
                "- 2026-09-02 10:00 ingest: state Active → Resolved (x)\n"
                "- 2026-09-02 12:00 ingest: state Resolved → Active (x)\n"
                "- 2026-09-05 08:00 ingest: state Active → Resolved (x)\n")
        t = facts.timeline({'state': 'Resolved', 'stage': 'landed'}, body)
        self.assertEqual(t['landed'], '2026-09-05T08:00:00Z')
        self.assertEqual(t['reopens'], 1)
        self.assertIsNone(t['prod'])

    def test_a_card_open_now_has_not_landed_whatever_its_history_says(self):
        body = "## History\n- 2026-09-01: created\n- 2026-09-02 10:00 ingest: state New → Resolved (x)\n"
        self.assertIsNone(facts.timeline({'state': 'Active', 'stage': 'building 1/2'}, body)['landed'])

    def test_a_landed_card_with_no_dated_landing_falls_back_to_stage_since(self):
        t = facts.timeline({'state': 'Resolved', 'stage': 'landed',
                            'stage_since': '2026-09-09T01:02:03Z'}, "## History\n- 2026-09-01: created\n")
        self.assertEqual(t['landed'], '2026-09-09T01:02:03Z')

    def test_ids_in_reads_branch_names(self):
        self.assertEqual(facts.ids_in('fix/b-0101 task/T-0002 fix/B-0101'), ['B-0101', 'T-0002'])


class ScoreTests(unittest.TestCase):
    def test_a_feature_row_sums_its_subtree_and_its_bugs(self):
        rows = score.feature_rows(facts_fixture())
        self.assertEqual([r['id'] for r in rows], ['F-0001'])   # F-0002 has not landed
        r = rows[0]
        self.assertEqual(r['usd'], 13.0)          # 4 spec + 6 coder + 2 correct + 1 review
        self.assertEqual(r['bug_usd'], 3.0)       # the fix-bug session on B-0001
        self.assertEqual(r['sessions'], 4)
        self.assertEqual(r['tokens'], 530)        # 30 on the spec + in_tokens 500 on the coder
        self.assertEqual(r['ci_min'], 7.0)        # half of the 10-minute run + the 2-minute gate
        self.assertEqual(r['repair_sessions'], 3)  # correct + review + the bug's one session
        self.assertEqual(r['corrections'], 1)
        self.assertEqual(r['send_backs'], 1)
        self.assertEqual(r['reopens'], 1)         # the Task's
        self.assertEqual((r['bugs'], r['s1']), (1, 1))
        self.assertEqual(r['lead_days'], 2.4)     # 09-01 00:00 → 09-03 10:00
        self.assertEqual(r['prod_days'], 3.4)

    def test_the_week_is_all_in(self):
        w = score.weekly(facts_fixture(), 1)[0]   # the ISO week of 2026-08-31 … 09-06
        self.assertEqual(w['week'], '2026-08-31')
        self.assertEqual((w['landed'], w['on_prod']), (1, 1))
        self.assertEqual(w['usd'], 21.0)          # every session in the week
        self.assertEqual(w['usd_per_feature'], 21.0)
        self.assertEqual(w['own_usd_per_feature'], 13.0)
        self.assertEqual(w['repair_sessions'], 2)  # correct + review (fix-bug is first-time work)
        self.assertEqual(w['ci_min'], 12.0)

    def test_the_headline_names_the_four_numbers(self):
        line = score.headline_line(score.headline(facts_fixture()), {'stale_prs': 3})
        self.assertEqual(line, '1 on prod / 1 landed (7 d) · lead 2.4 d (task 1.4 d) · $21.00/feature all-in · '
                               '2 repair sessions/feature · 3 stale PRs')

    def test_a_week_that_landed_nothing_says_what_it_spent(self):
        h = score.headline(facts_fixture(as_of='2026-09-20T00:00:00Z'))
        self.assertIn('$0.00 spent, nothing landed', score.headline_line(h))

    def test_failure_classes_fold_digits_and_detail(self):
        self.assertEqual(score.failure_class('failed: not pushed: 3 uncommitted files'), 'failed: not pushed')
        self.assertTrue(score.is_dead(run('j', '2026-09-01T00:00:00Z', 'dead pid')))
        self.assertFalse(score.is_dead(run('j', '2026-09-01T00:00:00Z', 'finished')))
        self.assertFalse(score.is_dead(run('j', '2026-09-01T00:00:00Z', 'failed', landed=True)))


class DiagnoseTests(unittest.TestCase):
    def window(self, f, days=7):
        return diagnose.window(f.as_of, days)

    def test_rank_orders_kinds_by_spend(self):
        f = facts_fixture()
        r = diagnose.rank(f, *self.window(f))
        self.assertEqual(r['by_kind'][0]['name'], 'spec')      # $4 + $5 over coder's $6
        self.assertEqual(r['by_ci_job'][0]['name'], 'ci-job:e2e')
        self.assertEqual(r['by_feature'][0]['name'], 'F-0001')

    def test_a_repair_kind_over_its_share_is_a_cause(self):
        f = facts_fixture()
        # correct: $2 of $21 = 9.5 %; set the floor under it
        found = diagnose.causes(f, *self.window(f), {'kind_share': 0.05, 'kind_min_usd': 1})
        kinds = [c for c in found if c.key.startswith('kind:')]
        self.assertEqual([c.key for c in kinds], ['kind:correct'])   # review: $1 = 4.8 %, under
        self.assertEqual(kinds[0].scope, diagnose.FACTORY)
        self.assertIn('$2.00 of $21.00', kinds[0].detail)
        self.assertEqual(diagnose.causes(f, *self.window(f)), [])   # defaults: nothing over

    def test_failures_ci_and_clutter_cross_their_thresholds(self):
        runs = [run(f'j{i}', '2026-09-05T00:00:00Z', 'failed: not pushed: 2 files') for i in range(6)]
        f = facts_fixture(runs=runs, clutter={'open_prs': 20, 'stale_prs': 12, 'branches': 1})
        found = {c.key: c for c in diagnose.causes(f, *self.window(f), {'ci_red_per_week': 0.5})}
        self.assertEqual(found['failure:failed: not pushed'].value, 6.0)
        self.assertEqual(found['ci-job:e2e'].scope, diagnose.PRODUCT)
        self.assertIn('clutter:stale-prs', found)

    def test_metric_rereads_a_cause_over_any_window(self):
        runs = [run('a', '2026-09-02T00:00:00Z', 'dead pid'), run('b', '2026-09-10T00:00:00Z', 'dead pid'),
                run('c', '2026-09-11T00:00:00Z', 'dead pid')]
        f = facts_fixture(runs=runs)
        s = datetime.datetime(2026, 9, 1, tzinfo=UTC)
        wk = datetime.timedelta(weeks=1)
        self.assertEqual(diagnose.metric(f, 'failure:dead pid', s, s + wk), 1.0)
        self.assertEqual(diagnose.metric(f, 'failure:dead pid', s + wk, s + 2 * wk), 2.0)
        self.assertEqual(diagnose.metric(f, 'lead-time', s, s + wk), 2.4)
        with self.assertRaises(KeyError):
            diagnose.metric(f, 'nonsense', s, s + wk)


class Prod:
    def __init__(self, name='p', improve=None, repo_dir=None, epic='E-0001'):
        self.name, self.improve, self.repo_dir, self.repo_slug, self.main = name, improve or {}, repo_dir, None, 'main'
        self.conventions = mock.Mock(intake_dir='inbox', default_bug_epic=epic)


def cause(key='failure:dead pid', scope=diagnose.FACTORY, value=8.0):
    return diagnose.Cause(key, scope, value, 5.0, 'runs/week', 'Sessions die', 'detail.')


class FilingTests(unittest.TestCase):
    def setUp(self):
        self.q = []
        self.enq = lambda target, e: self.q.append((target, e))

    def file(self, found, state, product=None, cfg=None, factory='fac'):
        product = product or Prod()
        cfg = cfg or loop.settings(product)
        return loop.file_causes(product, found, cfg, '2026-09-06T00:00:00Z', state, factory=factory,
                                enqueue_fn=self.enq, epic_fn=lambda t, c, p: 'E-0001')

    def test_a_cause_is_filed_once(self):
        state = {}
        self.assertEqual(self.file([cause()], state), ['failure:dead pid'])
        self.assertEqual(self.file([cause()], state), [])
        self.assertEqual(len(self.q), 1)
        self.assertEqual(state['failure:dead pid']['baseline'], 8.0)

    def test_factory_causes_go_to_the_factory_product_and_product_causes_stay_home(self):
        self.file([cause(), cause('ci-job:e2e', diagnose.PRODUCT)], {})
        self.assertEqual([t for t, _ in self.q], ['fac', 'p'])
        self.q.clear()
        self.file([cause('kind:x')], {}, factory=None)
        self.assertEqual([t for t, _ in self.q], ['p'])
        self.q.clear()
        p = Prod(improve={'scorecard': {'file_to': 'elsewhere'}})
        self.file([cause('kind:y')], {}, product=p)
        self.assertEqual([t for t, _ in self.q], ['elsewhere'])

    def test_a_day_files_at_most_max_per_run(self):
        state = {}
        self.file([cause(f'kind:{i}') for i in range(5)], state)
        self.assertEqual(len(state), 3)

    def test_the_card_carries_numbers_marker_and_parent_through_the_inbox_parser(self):
        self.file([cause()], {})
        text = self.q[0][1]['text']
        c = parse_inbox_file(text)
        self.assertEqual(c.title, 'Sessions die')
        self.assertEqual(c.headers.get('parent'), 'E-0001')
        self.assertIn('scorecard-cause: failure:dead pid #1', c.description)
        self.assertIn('8 runs/week (threshold 5)', c.description)
        self.assertTrue(c.acceptance)


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.q = []
        self.enq = lambda target, e: self.q.append((target, e))

    def verify(self, runs, landed='2026-09-08T00:00:00Z', as_of='2026-09-23T00:00:00Z'):
        f = facts_fixture(runs=runs, as_of=as_of)
        state = {'failure:dead pid': {'n': 1, 'marker': 'scorecard-cause: failure:dead pid #1',
                                      'target': 'p', 'scope': 'factory', 'title': 'Sessions die',
                                      'filed': '2026-09-06', 'baseline': 8.0, 'unit': 'runs/week',
                                      'threshold': 5.0, 'card': None, 'verdict': None, 'log': []}}
        cards = {'F-0009': {'id': 'F-0009', 'text': 'x\nscorecard-cause: failure:dead pid #1\n',
                            'landed': landed, 'removed': False}}
        p = Prod()
        done = loop.verify(p, f, loop.settings(p), state, lambda t: cards, as_of, enqueue_fn=self.enq,
                           epic_fn=lambda t, c, pr: 'E-0001')
        return done, state

    def dead(self, *days):
        return [run(f'j{i}', f'2026-09-{d:02d}T01:00:00Z', 'dead pid') for i, d in enumerate(days)]

    def test_a_card_whose_number_fell_moved(self):
        # before (08-25 … 09-08): 4 runs over 2 weeks = 2/week; after (09-08 … 09-22): 1 → 0.5/week
        done, state = self.verify(self.dead(1, 2, 3, 4, 10))
        self.assertEqual(done, [('failure:dead pid', 'moved')])
        self.assertEqual(state['failure:dead pid']['card'], 'F-0009')
        (target, e), = self.q
        self.assertEqual((target, e['kind'], e['card']), ('p', 'history', 'F-0009'))
        self.assertIn("failure:dead pid moved — 2 before, 0.5 after", e['line'])

    def test_a_card_that_did_not_move_is_reopened_with_the_numbers(self):
        done, state = self.verify(self.dead(1, 2, 10, 11, 12))    # 1/week before, 1.5/week after
        self.assertEqual(done, [('failure:dead pid', "didn't move")])
        kinds = [e['kind'] for _, e in self.q]
        self.assertEqual(kinds, ['history', 'inbox'])
        reopen = self.q[1][1]
        self.assertIn('Reopen F-0009', reopen['text'])
        self.assertIn('1 before, 1.5 after', reopen['text'])
        self.assertEqual(reopen['marker'], 'scorecard-cause: failure:dead pid #2')
        self.assertEqual(state['failure:dead pid']['n'], 2)
        self.assertIsNone(state['failure:dead pid']['verdict'])

    def test_nothing_is_judged_before_the_weeks_have_passed(self):
        done, state = self.verify(self.dead(1, 10), as_of='2026-09-15T00:00:00Z')
        self.assertEqual(done, [])
        self.assertEqual(state['failure:dead pid']['card'], 'F-0009')
        self.assertEqual(self.q, [])

    def test_moved_needs_a_fifth(self):
        self.assertTrue(loop.moved(10, 8, 0.2))
        self.assertFalse(loop.moved(10, 8.5, 0.2))
        self.assertTrue(loop.moved(0, 0, 0.2))
        self.assertIsNone(loop.moved(None, 1, 0.2))


class RecordTests(unittest.TestCase):
    """The loop against a record on disk: load, drain, snapshot, the daily part."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        write(self.root, 'epics/E-0001.md', card_md('E-0001', 'epic', 'E', 'Active', 'card',
                                                     ['2026-09-01: created'], parent='null'))
        write(self.root, 'features/F-0001.md', card_md(
            'F-0001', 'feature', 'one', 'Closed', 'landed',
            ['2026-09-01: created', '2026-09-03 10:00 ingest: stage building 1/1 → landed (x)',
             '2026-09-04 10:00 ingest: state Resolved → Closed (x)']))
        write(self.root, 'metrics/sessions/2026-09-02.jsonl',
              json.dumps({'ts': '2026-09-02T10:00:00Z', 'task': 'coder-f-0001', 'item': 'F-0001',
                          'usd': 4.0}) + '\n')
        self.state = tempfile.TemporaryDirectory()
        self.addCleanup(self.state.cleanup)
        patcher = mock.patch.object(env, 'state_dir', lambda product=None: self.state.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_load_reads_cards_and_streams(self):
        f = facts.load(self.root, None, as_of='2026-09-06T00:00:00Z')
        self.assertEqual(f.items['F-0001']['landed'], '2026-09-03T10:00:00Z')
        self.assertEqual(f.items['F-0001']['prod'], '2026-09-04T10:00:00Z')
        self.assertEqual(len(f.sessions), 1)

    def test_drain_writes_the_card_once_and_the_history_line_when_the_card_exists(self):
        p = Prod()
        loop.enqueue('p', {'kind': 'inbox', 'name': 'x.md', 'text': '# X\n\nscorecard-cause: k #1\n',
                           'marker': 'scorecard-cause: k #1'}, state_dir=self.state.name)
        loop.enqueue('p', {'kind': 'inbox', 'name': 'x-again.md', 'text': '# X\n\nscorecard-cause: k #1\n',
                           'marker': 'scorecard-cause: k #1'}, state_dir=self.state.name)
        loop.enqueue('p', {'kind': 'history', 'card': 'F-0001', 'line': '- 2026-09-20 scorecard: k moved'},
                     state_dir=self.state.name)
        loop.enqueue('p', {'kind': 'history', 'marker': 'scorecard-cause: nope #1', 'line': '- later'},
                     state_dir=self.state.name)
        self.assertEqual(loop.drain(p, self.root, out=lambda s: None, state_dir=self.state.name), (1, 1))
        self.assertTrue(os.path.isfile(os.path.join(self.root, 'inbox', 'x.md')))
        self.assertFalse(os.path.exists(os.path.join(self.root, 'inbox', 'x-again.md')))
        with open(os.path.join(self.root, 'features', 'F-0001.md'), encoding='utf-8') as f:
            self.assertIn('- 2026-09-20 scorecard: k moved', f.read())
        left = loop._read_lines(os.path.join(self.state.name, loop.QUEUE))
        self.assertEqual([e['line'] for e in left], ['- later'])      # its card is not minted yet

    def test_snapshot_upserts_the_week(self):
        p = Prod()
        f = facts.load(self.root, None, as_of='2026-09-06T00:00:00Z')
        loop.snapshot(p, f)
        loop.snapshot(p, f)
        rows = loop.snapshots(p)
        self.assertEqual([r['week'] for r in rows], ['2026-08-31'])
        self.assertEqual(rows[0]['landed'], 1)

    def test_the_daily_part_measures_files_and_drains_into_its_own_record(self):
        p = Prod(improve={'scorecard': {'thresholds': {'usd_per_feature': 1}}})
        f = facts.load(self.root, None, as_of='2026-09-06T00:00:00Z')
        lines = []
        with mock.patch.object(loop, 'epic_of', lambda t, c=None, pr=None: 'E-0001'):
            self.assertEqual(loop.daily(p, self.root, out=lines.append, facts=f, factory=''), 0)
        self.assertTrue(lines[-1].startswith('scorecard: 1 on prod / 1 landed'))
        self.assertIn('1 filed', lines[-1])
        inbox = os.listdir(os.path.join(self.root, 'inbox'))
        self.assertEqual(inbox, ['scorecard-cost-per-feature-1.md'])
        with mock.patch.object(loop, 'epic_of', lambda t, c=None, pr=None: 'E-0001'):
            loop.daily(p, self.root, out=lines.append, facts=f, factory='')
        self.assertIn('0 filed', lines[-1])                               # once per cause

    def test_the_status_row_and_the_command_read_the_record(self):
        from asf.views import scorecard as view
        from asf.views import status
        with mock.patch.object(facts, 'now_iso', lambda: '2026-09-06T00:00:00Z'):
            self.assertEqual(status.value_cell(self.root, Prod()),
                             '1 on prod / 1 landed (7 d) · lead 2.4 d (task —) · $4.00/feature all-in · '
                             '0 repair sessions/feature')
            with mock.patch.object(env, 'load_product', lambda name=None: Prod()), \
                    mock.patch.object(facts, 'forge_clutter', lambda p: {}), \
                    mock.patch('sys.stdout', new_callable=io.StringIO) as out:
                view.cmd_scorecard(mock.Mock(product='p', weeks=2, json=False), self.root)
        text = out.getvalue()
        self.assertIn('**SCORECARD p**', text)
        self.assertIn('| 2026-08-31 | 1 | 1 |', text)
        self.assertIn('F-0001 one', text)


class ProdAttributionTests(unittest.TestCase):
    """A landed Feature is on prod when every commit that landed it is an ancestor of the sha
    prod runs — the Prod row's own source — not only once the record says on-prod/Closed."""

    def _items(self):
        meta = lambda **kw: dict({'state': 'Resolved', 'stage': 'landed',  # noqa: E731
                                  'stage_since': '2026-09-24T15:37:00Z'}, **kw)
        return {
            'F-0001': facts.card(meta(id='F-0001', type='feature', evidence=[
                'merge c272091 of groom/x lands F-0001', 'CI green on main at or after it']), ''),
            'F-0002': facts.card(meta(id='F-0002', type='feature', evidence=['2/2 tasks Closed'],
                                      stage_since='2026-09-25T18:32:00Z'), ''),
            'T-0001': facts.card(meta(id='T-0001', type='task', parent='F-0002', state='Closed',
                                      evidence=['merge 74df364 of cloud/T-0001 lands T-0001 (PR #7)']), ''),
            'T-0002': facts.card(meta(id='T-0002', type='task', parent='F-0002', state='Closed',
                                      evidence=['merge 79240b1 of cloud/T-0002 lands T-0002']), ''),
            'T-0003': facts.card(meta(id='T-0003', type='task', parent='F-0002', removed='merged',
                                      evidence=['commit 1111111 names T-0003']), ''),
        }

    def test_landing_shas_read_from_the_evidence(self):
        self.assertEqual(self._items()['F-0001']['landing_shas'], ['c272091'])
        self.assertEqual(self._items()['T-0003']['landing_shas'], ['1111111'])

    def test_ancestry_against_the_deployed_sha_decides(self):
        items = self._items()
        in_prod = {'c272091'}
        marked = facts.attribute_prod(items, 'de8e980', '2026-09-25T17:13:25Z',
                                      lambda s, base: base == 'de8e980' and s in in_prod)
        self.assertEqual(marked, ['F-0001'])
        self.assertEqual(items['F-0001']['prod'], '2026-09-25T17:13:25Z')  # the deploy, after landing
        self.assertIsNone(items['F-0002']['prod'])  # its tasks merged after the deploy
        items = self._items()
        in_prod |= {'74df364', '79240b1'}  # the removed task's commit is not asked
        facts.attribute_prod(items, 'de8e980', 1789949910391, lambda s, b: s in in_prod)
        self.assertEqual(items['F-0002']['prod'], '2026-09-25T18:32:00Z')  # landed after the deploy stamp

    def test_the_value_row_counts_it(self):
        items = self._items()
        facts.attribute_prod(items, 'de8e980', '2026-09-25T17:13:25Z', lambda s, b: s == 'c272091')
        f = Facts(items=items, sessions=[], ci=[], gates=[], runs=[], clutter={},
                  as_of='2026-09-25T20:35:00Z')
        self.assertTrue(score.headline_line(score.headline(f)).startswith('1 on prod / 2 landed'))

    def test_no_prod_sha_marks_nothing(self):
        items = self._items()
        self.assertEqual(facts.attribute_prod(items, None, None, lambda s, b: True), [])

    def test_load_reads_the_prod_rows_source(self):
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(facts, 'load_cards', return_value=self._items()), \
                mock.patch.object(facts, 'prod_deployment',
                                  return_value=('de8e980', '2026-09-25T17:13:25Z')) as pd, \
                mock.patch.object(facts, '_git_ancestor', return_value=lambda s, b: s == 'c272091'):
            f = facts.load(root, Prod(repo_dir=root), registry=False, forge=False)
        pd.assert_called_once()
        self.assertEqual(f.items['F-0001']['prod'], '2026-09-25T17:13:25Z')


def _git(repo, *args):
    import subprocess
    return subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True, check=True,
                          env=dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@e',
                                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@e')).stdout.strip()


class ChildrenResolvedProdTests(unittest.TestCase):
    """A Feature landed by ``rule: children-resolved`` has no landing sha of its own: its landing
    commit is the newest of its Stories'/Tasks' — their evidence sha, else a trunk commit whose
    subject names them — and one with none is a diagnostic line, never silently dropped."""

    LANDED = ['2026-09-20: created', '2026-09-24 01:21 ingest: stage building 1/1 → landed (x)']

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo, self.root = os.path.join(tmp.name, 'repo'), os.path.join(tmp.name, 'record')
        os.makedirs(self.repo)
        _git(self.repo, 'init', '-q', '-b', 'main')

        def commit(msg):
            _git(self.repo, 'commit', '-q', '--allow-empty', '-m', msg)
            return _git(self.repo, 'rev-parse', 'HEAD')
        commit('init')
        self.t11 = commit('feat: part one (T-0011)')      # no sha in T-0011's evidence: named on the trunk
        commit('plan(T-0011): a later document commit')   # a document lane never lands it
        self.t12 = commit('feat: part two')               # T-0012's merged PR, by its evidence
        self.f40 = commit('feat: whole (F-0040)')
        self.prod = self.f40
        self.t21 = commit('feat: after the deploy (T-0021)')

        def ev(*lines):
            return 'evidence:\n' + ''.join(f'  - "{line}"\n' for line in lines)
        write(self.root, 'epics/E-0001.md', card_md('E-0001', 'epic', 'E', 'Active', 'card',
                                                     ['2026-09-01: created'], parent='null'))
        for fid, extra in (('F-0010', ev('1/1 children Closed', 'rule: children-resolved')),
                           ('F-0020', ev('1/1 children Closed', 'rule: children-resolved')),
                           ('F-0030', ev('1/1 children Closed', 'rule: children-resolved')),
                           ('F-0040', ev(f'commit {self.f40[:7]} names F-0040', 'rule: landed'))):
            write(self.root, f'features/{fid}.md', card_md(fid, 'feature', f'feature {fid}', 'Resolved',
                                                           'landed', self.LANDED, extra=extra))
        cards = (('S-0010', 'story', 'F-0010', ev('rule: tasks-resolved')),
                 ('T-0011', 'task', 'F-0010', 'stories: [S-0010]\n' + ev('F-0010 Closed', 'rule: parent-closed')),
                 ('T-0012', 'task', 'F-0010', 'stories: [S-0010]\n'
                  + ev(f'PR #9 merged ({self.t12[:9]})', 'rule: landed')),
                 ('T-0021', 'task', 'F-0020', ev('rule: parent-closed')),
                 ('S-0030', 'story', 'F-0030', ev('rule: tasks-resolved')))
        for iid, typ, parent, extra in cards:
            write(self.root, f'{typ}s/{iid}.md', card_md(iid, typ, iid, 'Closed', 'landed', self.LANDED,
                                                          parent=parent, extra=extra))

    def load(self):
        with mock.patch.object(facts, 'prod_deployment', return_value=(self.prod, '2026-09-25T17:13:25Z')):
            return facts.load(self.root, Prod(repo_dir=self.repo), registry=False, forge=False,
                              as_of='2026-09-25T20:00:00Z')

    def test_the_landing_commit_comes_from_the_descendants(self):
        f = self.load()
        self.assertEqual(f.items['F-0010']['prod'], '2026-09-25T17:13:25Z')
        self.assertEqual(f.items['F-0010']['landing_sha'], self.t12[:9])  # the newer of the two
        self.assertEqual(f.items['F-0040']['prod'], '2026-09-25T17:13:25Z')   # its own sha
        self.assertIsNone(f.items['F-0020']['prod'])  # its Task's commit came after the deploy
        self.assertEqual(f.items['F-0020']['landing_sha'], self.t21)

    def test_a_feature_with_no_traceable_commit_is_a_diagnostic_once(self):
        f = self.load()
        self.assertIsNone(f.items['F-0030']['prod'])
        self.assertEqual(f.diagnostics, ['F-0030 landed without a traceable commit (feature F-0030)'])
        from asf.views import scorecard as view
        d = {'product': 'p', 'as_of': f.as_of, 'headline': score.headline(f), 'clutter': {},
             'weeks': [], 'features': [], 'window_days': 14, 'causes': [], 'loop': {},
             'rank': {'usd': 0, 'hours': 0, 'by_kind': [], 'by_failure': [], 'by_ci_job': [],
                      'by_feature': []}, 'diagnostics': f.diagnostics}
        self.assertEqual(view.render(d).count('F-0030 landed without a traceable commit'), 1)

    def test_the_value_row_counts_them(self):
        line = score.headline_line(score.headline(self.load()))
        self.assertTrue(line.startswith('2 on prod / 4 landed'), line)


class WiringTests(unittest.TestCase):
    def test_the_daily_step_runs_the_loop(self):
        from asf.tick import step_daily
        names = [n for n, _ in step_daily.parts(Prod(), '/nonexistent')]
        self.assertEqual(names, ['stale', 'rollup', 'scorecard'])

    def test_the_product_file_accepts_the_scorecard_block(self):
        text = ('repo_slug: x/y\nrepo_dir: /tmp\nimprove:\n  scorecard:\n    verify_weeks: 3\n'
                '    thresholds: {lead_days: 4}\n')
        self.assertEqual(env.validate_product_text(text), [])
        p = env.Product('p', env.loads(text))
        self.assertEqual(loop.settings(p)['verify_weeks'], 3)
        self.assertEqual(diagnose.thresholds(loop.settings(p)['thresholds'])['lead_days'], 4)

    def test_the_factory_product_is_the_one_whose_repo_is_the_source(self):
        prods = {'a': Prod('a', repo_dir='/a'), 'b': Prod('b', repo_dir='/b')}
        self.assertEqual(loop.factory_product(['a', 'b'], prods.__getitem__, lambda d: d == '/b'), 'b')
        self.assertIsNone(loop.factory_product(['a'], prods.__getitem__, lambda d: False))

    def test_the_cli_knows_the_command(self):
        from asf import cli
        args = cli.build_parser().parse_args(['scorecard', '--product', 'p', '--weeks', '6'])
        self.assertEqual((args.command, args.weeks), ('scorecard', 6))


if __name__ == '__main__':
    unittest.main()
