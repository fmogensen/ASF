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
from asf.workers import lifecycle

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


def run(job, ended, reason, landed=False, usd=1.0, minutes=10.0, publish_refused='', worktree=''):
    return Run(job=job, kind='task', model='m', item=None, started=ended, ended=ended,
               minutes=minutes, landed=landed, end_reason=reason, usd=usd,
               publish_refused=publish_refused, worktree=worktree)


def gate(ts, signature='--- test_tick_steps: FAILED (rc 1)', conclusion='failure', scheme=None,
         branches=('fix/x',), seconds=60.0):
    g = {'ts': ts, 'branches': list(branches), 'seconds': seconds, 'conclusion': conclusion,
         'signature': signature}
    if scheme is not None:
        g['signature_scheme'] = scheme
    return g


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
        # correct + the bug's one session; the Task's first review is planned work, not repair
        self.assertEqual(r['repair_sessions'], 2)
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
        # correct (the first review is planned; fix-bug is first-time work)
        self.assertEqual(w['repair_sessions'], 1)
        self.assertEqual(w['ci_min'], 12.0)

    def test_the_headline_names_the_four_numbers(self):
        line = score.headline_line(score.headline(facts_fixture()), {'stale_prs': 3})
        self.assertEqual(line, '1 on prod / 1 landed (7 d) · lead 2.4 d (task 1.4 d) · $21.00/feature all-in · '
                               '1 repair sessions/feature · 3 stale PRs')

    def test_a_week_that_landed_nothing_says_what_it_spent(self):
        h = score.headline(facts_fixture(as_of='2026-09-20T00:00:00Z'))
        self.assertIn('$0.00 spent, nothing landed', score.headline_line(h))

    def test_failure_classes_fold_digits_and_detail(self):
        self.assertEqual(score.failure_class('failed: not pushed: 3 uncommitted files'), 'failed: not pushed')
        self.assertTrue(score.is_dead(run('j', '2026-09-01T00:00:00Z', 'dead pid')))
        self.assertFalse(score.is_dead(run('j', '2026-09-01T00:00:00Z', 'finished')))
        self.assertFalse(score.is_dead(run('j', '2026-09-01T00:00:00Z', 'failed', landed=True)))


class RepairPrefixTests(unittest.TestCase):
    """score.REPAIR_PREFIXES carries 'precheck' (F-0224 S-36505): a precheck session is repair
    load in its own right, and no other kind's numbers move because of it."""

    def test_precheck_is_repair(self):
        self.assertTrue(score.is_repair('precheck'))

    def test_a_precheck_job_name_reads_kind_precheck(self):
        self.assertEqual(score.session_kind({'task': 'precheck-t-0271', 'item': 'T-0271'}), 'precheck')

    def test_a_precheck_cause_can_cross_the_kind_share_gate(self):
        sessions = sessions_fixture() + [{'ts': '2026-09-06T10:00:00Z', 'task': 'precheck-t-0271',
                                          'item': 'T-0001', 'usd': 5.0, 'minutes': 5}]
        f = facts_fixture(sessions=sessions)
        found = {c.key for c in diagnose.causes(f, *diagnose.window(f.as_of, 7),
                                                 {'kind_share': 0.05, 'kind_min_usd': 1})}
        self.assertIn('kind:precheck', found)

    def test_kind_review_share_is_unmoved_by_precheck_joining_repair_prefixes(self):
        sessions = sessions_fixture() + [{'ts': '2026-09-06T10:00:00Z', 'task': 'precheck-t-0271',
                                          'item': 'T-0001', 'usd': 5.0, 'minutes': 5}]
        f = facts_fixture(sessions=sessions)
        w = diagnose.window(f.as_of, 7)
        with_precheck = diagnose.metric(f, 'kind:review', *w)
        without_prefixes = tuple(p for p in score.REPAIR_PREFIXES if p != 'precheck')
        with mock.patch.object(score, 'REPAIR_PREFIXES', without_prefixes):
            without_precheck = diagnose.metric(f, 'kind:review', *w)
        self.assertEqual(with_precheck, without_precheck)


class PlannedFirstReviewTests(unittest.TestCase):
    """Decision 2026-10-06 (release gate criterion 2): a Task's first review round is planned work,
    not repair; its second review on, and every correct / adjudicate / rebase / remerge /
    relaunch / bounce / revise / hotfix, is."""

    def s(self, ts, task, item, rnd=None):
        return {'ts': f'2026-09-0{ts}T10:00:00Z', 'task': task, 'item': item, 'round': rnd}

    def test_the_first_review_of_an_item_is_not_repair_the_second_is(self):
        ss = [self.s(2, 'review-t-0001', 'T-0001'), self.s(3, 'correct-t-0001', 'T-0001'),
              self.s(4, 'review-t-0001', 'T-0001'), self.s(4, 'review-t-0002', 'T-0002')]
        self.assertEqual(score.repair_flags(ss), [False, True, True, False])

    def test_order_is_by_time_not_by_position(self):
        ss = [self.s(4, 'review-t-0001', 'T-0001'), self.s(2, 'review-t-0001', 'T-0001')]
        self.assertEqual(score.repair_flags(ss), [True, False])

    def test_a_named_round_two_review_is_repair_even_alone(self):
        self.assertEqual(score.repair_flags([self.s(2, 'review-t-0001-r2', 'T-0001', rnd=2)]), [True])

    def test_every_rework_kind_is_repair(self):
        kinds = ('correct', 'adjudicate', 'rebase', 'remerge', 'relaunch', 'bounce', 'revise', 'hotfix')
        ss = [self.s(2, f'{k}-t-0001', 'T-0001') for k in kinds]
        self.assertEqual(score.repair_flags(ss), [True] * len(kinds))
        self.assertEqual(score.repair_flags([self.s(2, 'coder-t-0001', 'T-0001')]), [False])

    def test_the_window_counts_a_second_review_whose_first_fell_before_it(self):
        sessions = sessions_fixture() + [{'ts': '2026-09-04T09:00:00Z', 'task': 'review-t-0001',
                                          'item': 'T-0001', 'usd': 1.0, 'minutes': 10}]
        f = facts_fixture(sessions=sessions)
        w = score.window_row(f, facts.to_dt('2026-09-04T00:00:00Z'), facts.to_dt('2026-09-07T00:00:00Z'))
        self.assertEqual(w['repair_sessions'], 1)
        self.assertEqual(w['repair_by_kind'], {'review': 1})

    def test_the_scorecard_and_release_readiness_agree(self):
        from asf import env, release
        f = facts_fixture()
        h = score.headline(f)
        with mock.patch.object(release, 'gather', side_effect=lambda root, product, cfg, **kw: {
                'as_of': f.as_of, 'since': '', 'items': {}, 'hand_commits': [], 'installs': [],
                'repair': {'sessions': h['repair_sessions'], 'landed': h['landed'],
                           'per_feature': h['repair_per_feature'], 'by_kind': h['repair_by_kind']},
                'upgrade': {'upgrades': [], 'failed': [], 'torn': [], 'rollbacks': [], 'chain_breaks': []},
                'ci_runs': [], 'ci_steps': [], 'docs': {'headings': [], 'tag': None, 'changelog_section': False,
                                                        'changelog_notes': False}}):
            d = release.compute('/nowhere', env.Product('p', {}))
        rep = next(c for c in d['criteria'] if c['key'] == 'repair')
        self.assertIn(f"{h['repair_sessions']} repair sessions / {h['landed']} Features", rep['evidence'])
        self.assertIn('(correct 1)', rep['evidence'])


class FalseCloseTimelineTests(unittest.TestCase):
    def test_each_landing_and_reopening_is_stamped(self):
        body = ('## History\n'
                '- 2026-09-01 00:00 ingest: stage building 1/1 → landed (x)\n'
                '- 2026-09-02 00:00 ingest: stage landed → building 2/2 (y)\n'
                '- 2026-09-03 00:00 ingest: stage building 2/2 → landed (z)\n')
        t = facts.timeline({'state': 'Resolved', 'stage': 'landed'}, body)
        self.assertEqual(t['closes'], ['2026-09-01T00:00:00Z', '2026-09-03T00:00:00Z'])
        self.assertEqual(t['reopened'], ['2026-09-02T00:00:00Z'])
        self.assertEqual(t['reopens'], 1)


class TotalRowTests(unittest.TestCase):
    """score.total_row over literal stored weekly rows (F-0148) — no Facts, no file, no clock."""

    ROW_P = {'week': '2026-09-21', 'landed': 2, 'on_prod': 1, 'tasks_landed': 1, 'sessions': 10,
             'tokens': 500, 'usd': 20.0, 'ci_min': 5.0, 'repair_sessions': 3, 'send_backs': 1,
             'bugs': 1, 's1': 0, 'dead_sessions': 1, 'dead_usd': 2.0,
             'usd_per_feature': 10.0, 'own_usd_per_feature': 8.0,
             'clutter': {'open_prs': 5, 'stale_prs': 2, 'branches': 3}}
    ROW_Q = {'week': '2026-09-21', 'landed': 3, 'on_prod': 2, 'tasks_landed': 2, 'sessions': 15,
             'tokens': 700, 'usd': 90.0, 'ci_min': 8.0, 'repair_sessions': 4, 'send_backs': 0,
             'bugs': 2, 's1': 1, 'dead_sessions': 0, 'dead_usd': 0.0,
             'usd_per_feature': 30.0, 'own_usd_per_feature': 25.0,
             'clutter': {'open_prs': 3, 'stale_prs': 0, 'branches': 0}}

    def test_the_summing_keys_are_added(self):
        t = score.total_row([self.ROW_P, self.ROW_Q])
        self.assertEqual(t['landed'], 5)
        self.assertEqual(t['on_prod'], 3)
        self.assertEqual(t['tasks_landed'], 3)
        self.assertEqual(t['sessions'], 25)
        self.assertEqual(t['tokens'], 1200)
        self.assertEqual(t['usd'], 110.0)
        self.assertEqual(t['ci_min'], 13.0)
        self.assertEqual(t['repair_sessions'], 7)
        self.assertEqual(t['send_backs'], 1)
        self.assertEqual(t['bugs'], 3)
        self.assertEqual(t['s1'], 1)
        self.assertEqual(t['dead_sessions'], 1)
        self.assertEqual(t['dead_usd'], 2.0)
        self.assertEqual(t['products'], 2)

    def test_usd_per_feature_is_all_in_over_every_landing_not_a_mean_of_means(self):
        t = score.total_row([self.ROW_P, self.ROW_Q])
        # 20 + 90 all-in over 2 + 3 landed
        self.assertEqual(t['usd_per_feature'], 22.0)
        naive_mean = (self.ROW_P['usd_per_feature'] + self.ROW_Q['usd_per_feature']) / 2
        self.assertNotEqual(t['usd_per_feature'], naive_mean)

    def test_own_usd_per_feature_is_the_landed_weighted_mean_exact_against_its_spend(self):
        t = score.total_row([self.ROW_P, self.ROW_Q])
        own_spend = (self.ROW_P['own_usd_per_feature'] * self.ROW_P['landed']
                     + self.ROW_Q['own_usd_per_feature'] * self.ROW_Q['landed'])
        self.assertEqual(own_spend, 91.0)
        self.assertEqual(t['own_usd_per_feature'], 18.2)
        self.assertEqual(round(t['own_usd_per_feature'] * t['landed'], 2), own_spend)

    def test_the_three_medians_are_none(self):
        t = score.total_row([self.ROW_P, self.ROW_Q])
        for k in score.TOTAL_MEDIANS:
            self.assertIsNone(t[k])

    def test_clutter_sums_a_key_no_row_reads_stays_none_a_key_every_row_reads_as_zero_stays_zero(self):
        rows = [{'week': '2026-09-21', 'landed': 0, 'clutter': {'stale_prs': 0}},
                {'week': '2026-09-21', 'landed': 0, 'clutter': {'stale_prs': 0, 'branches': 5}}]
        t = score.total_row(rows)
        self.assertIsNone(t['clutter']['open_prs'])    # no row carries a reading
        self.assertEqual(t['clutter']['stale_prs'], 0)  # every row read it, all zero
        self.assertEqual(t['clutter']['branches'], 5)

    def test_a_week_where_nothing_landed_gives_none_ratios_and_no_division_by_zero(self):
        row = {'week': '2026-09-21', 'landed': 0, 'on_prod': 0, 'usd': 50.0,
               'own_usd_per_feature': 0, 'repair_sessions': 0}
        t = score.total_row([row])
        self.assertIsNone(t['usd_per_feature'])
        self.assertIsNone(t['own_usd_per_feature'])
        self.assertIsNone(t['repair_per_feature'])

    def test_an_empty_row_list_gives_a_total_that_says_it_covers_nothing(self):
        t = score.total_row([])
        self.assertEqual(t['products'], 0)
        self.assertEqual(t['landed'], 0)
        self.assertIsNone(t['week'])


class TotalLineTests(unittest.TestCase):
    """score.delta_row, score.delta and score.total_line (F-0148) — pure, over literal dicts."""

    def test_delta_row_skips_a_key_missing_or_none_on_either_side_never_reading_it_as_zero(self):
        now = {'on_prod': 7, 'landed': 11, 'usd': 422.40}
        prev = {'on_prod': 5, 'landed': None, 'usd': 382.40}
        d = score.delta_row(now, prev)
        self.assertEqual(d, {'on_prod': 2, 'usd': 40.0})
        self.assertNotIn('landed', d)   # None on prev, not read as zero
        self.assertEqual(score.delta_row(now, {}), {})
        self.assertEqual(score.delta_row(now, None), {})
        self.assertEqual(score.delta_row(None, prev), {})

    def test_delta_renders_the_five_shapes(self):
        self.assertEqual(score.delta(3), '+3')
        self.assertEqual(score.delta(-6.10, money=True), '-$6.10')
        self.assertEqual(score.delta(2.5, unit='d'), '+2.5 d')
        self.assertEqual(score.delta(0), '0')
        self.assertEqual(score.delta(None), '—')

    def test_the_line_carries_every_headline_number_with_its_delta_the_week_the_day_and_the_count(self):
        totals = {
            'day': 3, 'days': 7,
            'total': {'week': '2026-09-21', 'products': 3, 'on_prod': 7, 'landed': 11,
                      'usd_per_feature': 38.40, 'own_usd_per_feature': 21.00,
                      'repair_per_feature': 2.1, 'usd': 422.40},
            'delta': {'on_prod': 2, 'landed': 3, 'usd_per_feature': -6.10,
                      'own_usd_per_feature': -1.40, 'repair_per_feature': -0.4, 'usd': 40.00},
        }
        line = score.total_line(totals)
        self.assertEqual(line,
            'scorecard week 2026-09-21 (day 3/7, 3 products): 7 on prod (+2) · 11 landed (+3) · '
            '$38.40/feature all-in (-$6.10) · $21.00/feature own (-$1.40) · '
            '2.1 repair sessions/feature (-0.4) · $422.40 all-in (+$40.00)')
        self.assertNotIn('sample', line)
        self.assertNotIn('other', line)

    def test_the_line_reads_all_dashes_when_there_is_nothing_stored_to_compare_against(self):
        totals = {
            'day': 1, 'days': 7,
            'total': {'week': '2026-09-21', 'products': 2, 'on_prod': 0, 'landed': 1,
                      'usd_per_feature': 12.00, 'own_usd_per_feature': 12.00,
                      'repair_per_feature': 0.0, 'usd': 12.00},
            'delta': {},
        }
        line = score.total_line(totals)
        self.assertEqual(line,
            'scorecard week 2026-09-21 (day 1/7, 2 products): 0 on prod (—) · 1 landed (—) · '
            '$12.00/feature all-in (—) · $12.00/feature own (—) · '
            '0 repair sessions/feature (—) · $12.00 all-in (—)')

    def test_total_line_of_none_is_the_no_snapshot_sentence(self):
        self.assertEqual(score.total_line(None),
                         'scorecard: no weekly snapshot yet — the daily step writes the first one')

    def test_total_line_of_an_envelope_with_zero_products_is_the_same_sentence(self):
        totals = {'day': 1, 'days': 7, 'total': {'products': 0}, 'delta': {}}
        self.assertEqual(score.total_line(totals),
                         'scorecard: no weekly snapshot yet — the daily step writes the first one')


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

    def test_a_timeout_cancel_counts_red_under_the_same_key_with_a_timeout(self):
        ci = [{'ts': '2026-09-03T00:00:00Z', 'minutes': 360,
               'jobs': [{'name': 'tests', 'conclusion': 'cancelled', 'minutes': 360, 'cause': 'timeout'}]}]
        f = facts_fixture(ci=ci)
        r = diagnose.rank(f, *self.window(f))
        job = next(c for c in r['by_ci_job'] if c['name'] == 'ci-job:tests')
        self.assertEqual(job['red'], 1)
        self.assertEqual(job['timeouts'], 1)
        self.assertEqual(job['runner_losses'], 0)

    def test_a_runner_loss_cancel_counts_red_under_the_same_key_with_a_runner_loss(self):
        ci = [{'ts': '2026-09-03T00:00:00Z', 'minutes': 2,
               'jobs': [{'name': 'tests', 'conclusion': 'cancelled', 'minutes': 2, 'cause': 'runner-loss'}]}]
        f = facts_fixture(ci=ci)
        r = diagnose.rank(f, *self.window(f))
        job = next(c for c in r['by_ci_job'] if c['name'] == 'ci-job:tests')
        self.assertEqual(job['red'], 1)
        self.assertEqual(job['timeouts'], 0)
        self.assertEqual(job['runner_losses'], 1)

    def test_a_failure_cancel_and_an_unclassified_cancel_are_not_red(self):
        ci = [{'ts': '2026-09-03T00:00:00Z', 'minutes': 10,
               'jobs': [{'name': 'tests', 'conclusion': 'cancelled', 'minutes': 5, 'cause': 'failure'},
                        {'name': 'tests', 'conclusion': 'cancelled', 'minutes': 5}]}]
        f = facts_fixture(ci=ci)
        r = diagnose.rank(f, *self.window(f))
        job = next(c for c in r['by_ci_job'] if c['name'] == 'ci-job:tests')
        self.assertEqual(job['runs'], 2)
        self.assertEqual(job['red'], 0)
        self.assertEqual(job['timeouts'], 0)
        self.assertEqual(job['runner_losses'], 0)

    def test_metric_reads_the_timeout_red_count_off_the_same_key(self):
        ci = [{'ts': '2026-09-03T00:00:00Z', 'minutes': 360,
               'jobs': [{'name': 'tests', 'conclusion': 'cancelled', 'minutes': 360, 'cause': 'timeout'}]}]
        f = facts_fixture(ci=ci)
        self.assertEqual(diagnose.metric(f, 'ci-job:tests', *self.window(f)), 1.0)   # 1 red / 1 week

    def test_five_timed_out_runs_raise_a_product_cause_over_the_default_threshold(self):
        # 5 red runs over the 7-day (1-week) window; threshold 3.0/week
        ci = [{'ts': f'2026-09-0{i + 1}T00:00:00Z', 'minutes': 360,
               'jobs': [{'name': 'tests', 'conclusion': 'cancelled', 'minutes': 360, 'cause': 'timeout'}]}
              for i in range(5)]
        f = facts_fixture(ci=ci)
        found = {c.key: c for c in diagnose.causes(f, *self.window(f))}
        c = found['ci-job:tests']
        self.assertEqual(c.scope, diagnose.PRODUCT)
        self.assertEqual(c.value, 5.0)
        self.assertTrue(c.detail.endswith('5 ran to their limit.'))

    def test_a_gate_row_still_carries_the_two_new_counters_at_zero(self):
        f = facts_fixture()
        r = diagnose.rank(f, *self.window(f))
        for c in r['by_ci_job']:
            self.assertIn('timeouts', c)
            self.assertIn('runner_losses', c)
        gate_row = next(c for c in r['by_ci_job'] if c['name'] == 'gate:green')
        self.assertEqual(gate_row['timeouts'], 0)
        self.assertEqual(gate_row['runner_losses'], 0)


class NotPushedCauseTests(unittest.TestCase):
    """T-0525: the frozen string, class, key and title do not move; the cause's detail gains one
    line naming the sub-causes a reopen would need."""

    def window(self, f, days=7):
        return diagnose.window(f.as_of, days)

    def test_the_frozen_string_class_key_and_title_are_unchanged(self):
        text = lifecycle.push_gap(lifecycle.Evidence(uncommitted=3, unpushed=0))
        self.assertEqual(text, 'not pushed: 3 uncommitted file(s), 0 unpushed commit(s)')
        self.assertEqual(score.failure_class(f'failed: {text}'), 'failed: not pushed')
        runs = [run(f'j{i}', '2026-09-05T00:00:00Z', f'failed: {text}') for i in range(6)]
        f = facts_fixture(runs=runs)
        s, e = self.window(f)
        found = {c.key: c for c in diagnose.causes(f, s, e)}
        self.assertIn('failure:failed: not pushed', found)
        c = found['failure:failed: not pushed']
        self.assertEqual(c.title, "Sessions die with 'failed: not pushed' 6 times a week")
        weeks = (e - s).total_seconds() / (7 * 86400)
        self.assertEqual(diagnose.metric(f, 'failure:failed: not pushed', s, e),
                          round(6 / weeks, 2))

    def test_sub_cause_returns_the_right_name_for_each_precedence_case(self):
        wt = '/tmp/wt'
        uncommitted = run('u', 'x', f"failed: {lifecycle.push_gap(lifecycle.Evidence(uncommitted=3, unpushed=0))}", worktree=wt)
        unpushed = run('u', 'x', f"failed: {lifecycle.push_gap(lifecycle.Evidence(uncommitted=0, unpushed=2))}", worktree=wt)
        never_pushed = run('u', 'x', f"failed: {lifecycle.push_gap(lifecycle.Evidence(uncommitted=0, unpushed=0))}", worktree=wt)
        no_worktree = run('u', 'x', 'failed: not pushed: n/a', worktree='')
        other = run('u', 'x', 'failed: not pushed: something odd', worktree=wt)
        hook_refused = run('u', 'x', '', worktree=wt, publish_refused='refused by the repo pre-push hook')
        rebase = run('u', 'x', '', worktree=wt, publish_refused='rebase conflicts in: foo.py')
        stale = run('u', 'x', '', worktree=wt, publish_refused='would lose 3 commits from the stale head')
        network = run('u', 'x', '', worktree=wt, publish_refused='connection reset by peer')
        generic_refused = run('u', 'x', '', worktree=wt, publish_refused='a signal killed the push midway')
        # publish_refused wins even over a missing worktree: the factory tried before it lost it
        refused_beats_no_worktree = run('u', 'x', '', worktree='', publish_refused='refused by the repo pre-push hook')

        self.assertEqual(diagnose.sub_cause(uncommitted), 'uncommitted')
        self.assertEqual(diagnose.sub_cause(unpushed), 'unpushed')
        self.assertEqual(diagnose.sub_cause(never_pushed), 'never pushed')
        self.assertEqual(diagnose.sub_cause(no_worktree), 'no worktree')
        self.assertEqual(diagnose.sub_cause(other), 'other')
        self.assertEqual(diagnose.sub_cause(hook_refused), 'hook refused')
        self.assertEqual(diagnose.sub_cause(rebase), 'rebase conflict')
        self.assertEqual(diagnose.sub_cause(stale), 'stale head')
        self.assertEqual(diagnose.sub_cause(network), 'network')
        self.assertEqual(diagnose.sub_cause(generic_refused), 'refused')
        self.assertEqual(diagnose.sub_cause(refused_beats_no_worktree), 'hook refused')

    def test_sub_cause_line_omits_empty_buckets_and_sums_to_the_total(self):
        subs = {'uncommitted': 9, 'unpushed': 6, 'hook refused': 4, 'rebase conflict': 1, 'no worktree': 3}
        line = diagnose.sub_cause_line(subs, 23)
        self.assertEqual(line,
            "of the 23 runs: 9 left files uncommitted, 6 committed and never pushed, "
            "5 the factory could not publish (4 hook refused, 1 rebase conflict), "
            "3 had no worktree left to publish.")
        self.assertNotIn('never pushed the branch', line)   # zero runs in that bucket: absent
        self.assertNotIn('stale head', line)                # zero runs in that refusal split: absent
        self.assertEqual(diagnose.sub_cause_line({}, 0), '')
        self.assertEqual(diagnose.sub_cause_line({'uncommitted': 1}, 0), '')

    def test_the_causes_detail_carries_the_sub_cause_line_that_sums_to_its_own_count(self):
        wt = '/tmp/wt'
        runs = [
            run('u1', '2026-09-05T00:00:00Z',
                f"failed: {lifecycle.push_gap(lifecycle.Evidence(uncommitted=3, unpushed=0))}", worktree=wt),
            run('u2', '2026-09-05T00:00:00Z',
                f"failed: {lifecycle.push_gap(lifecycle.Evidence(uncommitted=0, unpushed=2))}", worktree=wt),
            run('u3', '2026-09-05T00:00:00Z',
                f"failed: {lifecycle.push_gap(lifecycle.Evidence(uncommitted=0, unpushed=0))}", worktree=wt),
            run('u4', '2026-09-05T00:00:00Z', 'failed: not pushed: n/a', worktree=wt,
                publish_refused='refused by the repo pre-push hook'),
            run('u5', '2026-09-05T00:00:00Z', 'failed: not pushed: n/a', worktree=wt,
                publish_refused='rebase conflicts in: foo.py'),
            run('u6', '2026-09-05T00:00:00Z', 'failed: not pushed: n/a', worktree=''),
            run('u7', '2026-09-05T00:00:00Z', 'failed: not pushed: something odd', worktree=wt),
        ]
        f = facts_fixture(runs=runs)
        found = {c.key: c for c in diagnose.causes(f, *self.window(f))}
        c = found['failure:failed: not pushed']
        self.assertEqual(c.title, "Sessions die with 'failed: not pushed' 7 times a week")   # unchanged shape
        expected_line = (
            "of the 7 runs: 1 left files uncommitted, 1 committed and never pushed, "
            "1 never pushed the branch at all, 2 the factory could not publish "
            "(1 hook refused, 1 rebase conflict), 1 had no worktree left to publish, "
            "1 ended for a reason this line cannot read.")
        self.assertTrue(c.detail.endswith(expected_line), c.detail)


class Prod:
    def __init__(self, name='p', improve=None, repo_dir=None, epic='E-0001'):
        self.name, self.improve, self.repo_dir, self.repo_slug, self.main = name, improve or {}, repo_dir, None, 'main'
        self.conventions = mock.Mock(intake_dir='inbox', default_bug_epic=epic)


def cause(key='failure:dead pid', scope=diagnose.FACTORY, value=8.0, scheme=None):
    return diagnose.Cause(key, scope, value, 5.0, 'runs/week', 'Sessions die', 'detail.', scheme=scheme)


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


class SchemeChangeTests(unittest.TestCase):
    """T-0542: a `gate:` cause key whose after-window speaks a newer signature scheme than it was
    filed under is not read as a win — it is `scheme changed`, with no after-number and no reopen."""

    KEY = 'gate:--- test_tick_steps: FAILED (rc N)'

    def setUp(self):
        self.q = []
        self.enq = lambda target, e: self.q.append((target, e))

    def entry(self, scheme=1, **kw):
        e = {'n': 1, 'marker': f'scorecard-cause: {self.KEY} #1', 'target': 'p', 'scope': 'product',
             'title': 'CI is red', 'filed': '2026-09-06', 'baseline': 3.5, 'unit': 'red runs/week',
             'threshold': 3.0, 'card': None, 'verdict': None, 'log': [], 'scheme': scheme}
        e.update(kw)
        return e

    def verify(self, gates, entry, landed='2026-09-08T00:00:00Z', as_of='2026-09-23T00:00:00Z',
               card='F-0009', marker=None):
        f = facts_fixture(gates=gates, as_of=as_of)
        state = {self.KEY: entry}
        cards = {card: {'id': card, 'text': f"x\n{marker or entry['marker']}\n", 'landed': landed,
                        'removed': False}}
        p = Prod()
        done = loop.verify(p, f, loop.settings(p), state, lambda t: cards, as_of, enqueue_fn=self.enq,
                           epic_fn=lambda t, c, pr: 'E-0001')
        return done, state

    def test_a_scheme_change_in_the_after_window_is_recorded_with_no_after_number_and_no_reopen(self):
        # before (08-25 .. 09-08): 1 red / 2 weeks = 0.5/week
        gates = [gate('2026-09-01T00:00:00Z'), gate('2026-09-10T00:00:00Z', scheme=2)]
        done, state = self.verify(gates, self.entry(scheme=1))
        self.assertEqual(done, [(self.KEY, 'scheme changed')])
        e = state[self.KEY]
        self.assertEqual(e['verdict'], 'scheme changed')
        self.assertIsNone(e['after'])
        self.assertEqual(e['before'], 0.5)
        self.assertNotIn('reopens', e)                       # C9: no reopen
        (target, entry), = self.q
        self.assertEqual((target, entry['kind'], entry['card']), ('p', 'history', 'F-0009'))
        self.assertIn('scheme changed', entry['line'])
        self.assertIn('0.5 before', entry['line'])
        self.assertIn('no comparable reading after', entry['line'])
        self.assertIn('from 1 to 2', entry['line'])
        self.assertEqual(len(self.q), 1)                     # no inbox enqueue

    def test_the_scheme_still_spoken_in_the_after_window_is_judged_the_ordinary_way(self):
        # after window still carries a scheme-1 red gate alongside no scheme-2 one: not a change
        gates = [gate('2026-09-01T00:00:00Z'), gate('2026-09-10T00:00:00Z')]
        done, state = self.verify(gates, self.entry(scheme=1))
        self.assertEqual(done, [(self.KEY, "didn't move")])   # 0.5 before, 0.5 after — reopened, not scheme changed
        self.assertEqual(state[self.KEY]['baseline'], 0.5)
        self.assertEqual(state[self.KEY]['reopens'], 'F-0009')

    def test_an_entry_with_no_scheme_key_is_judged_the_ordinary_way(self):
        no_scheme = self.entry()
        del no_scheme['scheme']
        gates = [gate('2026-09-01T00:00:00Z'), gate('2026-09-10T00:00:00Z', scheme=2)]
        done, state = self.verify(gates, no_scheme)
        self.assertEqual(done, [(self.KEY, "didn't move")])   # 0.5 before, 0.5 after — scheme unread
        self.assertEqual(state[self.KEY]['baseline'], 0.5)
        self.assertEqual(state[self.KEY]['reopens'], 'F-0009')

    def test_an_after_window_with_no_red_gate_at_all_still_reads_a_genuine_fall_as_moved(self):
        gates = [gate('2026-09-01T00:00:00Z')]                # before only: 0.5/week, after: nothing red
        done, state = self.verify(gates, self.entry(scheme=1))
        self.assertEqual(done, [(self.KEY, 'moved')])
        self.assertEqual(state[self.KEY]['after'], 0.0)

    def test_file_causes_writes_scheme_for_a_gate_cause_and_none_for_others(self):
        state = {}
        found = [cause('gate:x', diagnose.PRODUCT, scheme=2), cause('kind:y', diagnose.FACTORY)]
        p = Prod()
        loop.file_causes(p, found, loop.settings(p), '2026-09-06T00:00:00Z', state, factory='fac',
                         enqueue_fn=lambda t, e: None, epic_fn=lambda t, c, pr: 'E-0001')
        self.assertEqual(state['gate:x']['scheme'], 2)
        self.assertIsNone(state['kind:y']['scheme'])

    def test_a_reopened_gate_cause_carries_its_scheme_so_a_later_verify_can_still_catch_a_change(self):
        # round 1: didn't move (0.5 before, 0.5 after, both scheme 1) — reopened
        gates1 = [gate('2026-09-01T00:00:00Z'), gate('2026-09-10T00:00:00Z')]
        done1, state = self.verify(gates1, self.entry(scheme=1))
        self.assertEqual(done1, [(self.KEY, "didn't move")])
        reopened = state[self.KEY]
        self.assertEqual(reopened['reopens'], 'F-0009')
        self.assertEqual(reopened['scheme'], 1)               # the after-window it was just read over
        self.assertEqual(reopened['marker'], f'scorecard-cause: {self.KEY} #2')

        # round 2: the reopened entry's after-window now speaks only scheme 2 — caught, not closed
        gates2 = [gate('2026-10-15T00:00:00Z', scheme=2)]
        done2, state2 = self.verify(gates2, reopened, landed='2026-10-08T00:00:00Z',
                                    as_of='2026-10-23T00:00:00Z', card='F-0010',
                                    marker=reopened['marker'])
        self.assertEqual(done2, [(self.KEY, 'scheme changed')])
        self.assertIsNone(state2[self.KEY]['after'])

    def test_a_non_gate_causes_reopened_entry_carries_no_scheme(self):
        state = {'failure:dead pid': {'n': 1, 'marker': 'scorecard-cause: failure:dead pid #1',
                                      'target': 'p', 'scope': 'factory', 'title': 'Sessions die',
                                      'filed': '2026-09-06', 'baseline': 8.0, 'unit': 'runs/week',
                                      'threshold': 5.0, 'card': None, 'verdict': None, 'log': [],
                                      'scheme': None}}
        cards = {'F-0009': {'id': 'F-0009', 'text': 'x\nscorecard-cause: failure:dead pid #1\n',
                            'landed': '2026-09-08T00:00:00Z', 'removed': False}}
        runs = [run(f'j{i}', f'2026-09-{d:02d}T01:00:00Z', 'dead pid') for i, d in enumerate((1, 2, 10, 11, 12))]
        f = facts_fixture(runs=runs, as_of='2026-09-23T00:00:00Z')
        p = Prod()
        done = loop.verify(p, f, loop.settings(p), state, lambda t: cards, '2026-09-23T00:00:00Z',
                           enqueue_fn=self.enq, epic_fn=lambda t, c, pr: 'E-0001')
        self.assertEqual(done, [('failure:dead pid', "didn't move")])
        self.assertIsNone(state['failure:dead pid']['scheme'])


class DirectionTests(unittest.TestCase):
    """The third verdict, pure — no fixture."""

    def test_direction_pins_the_eight_cases(self):
        self.assertEqual(loop.direction(10, 8, 0.2), 'moved')
        self.assertEqual(loop.direction(10, 12, 0.2), 'got worse')
        self.assertEqual(loop.direction(10, 11, 0.2), "didn't move")
        self.assertEqual(loop.direction(10, 8.5, 0.2), "didn't move")
        self.assertEqual(loop.direction(0, 0, 0.2), 'moved')
        self.assertEqual(loop.direction(0, 1, 0.2), 'got worse')
        self.assertIsNone(loop.direction(None, 1, 0.2))
        self.assertIsNone(loop.direction(1, None, 0.2))

    def test_moved_still_answers_as_it_does_today(self):
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
        self.t21 = commit('feat(T-0021): after the deploy')

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
        self.assertEqual(names, ['stale', 'rollup', 'scorecard', 'deciders'])

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



FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'scorecard')


def _prun(job, started, *, kind='coder', model='light-model', item='T-0001', head='aaaaaaa',
          ended=None, reason='finished', harvested=None, usd=1.0, causes=()):
    from asf.scorecard.facts import to_dt  # noqa: F401
    r = {'_job': job, 'started': started, 'kind': kind, 'model': model, 'item': item,
         '_head': head, 'ended': ended or started, 'end_reason': reason, '_usd': usd,
         '_causes': sorted(causes)}
    if harvested:
        r['harvested'] = harvested
    return r


class ProgramTests(unittest.TestCase):
    """asf.scorecard.program — the improvement program's row and ``--check``."""

    def setUp(self):
        from asf.scorecard import program
        self.P = program
        self.start = facts.to_dt('2026-10-01T00:00:00Z')
        self.end = facts.to_dt('2026-10-02T00:00:00Z')

    def test_a_three_hour_launch_gap_is_three_idle_hours_and_a_short_one_is_none(self):
        runs = [_prun('a-t-0001', '2026-10-01T01:00:00Z'), _prun('b-t-0002', '2026-10-01T02:00:00Z'),
                _prun('c-t-0003', '2026-10-01T05:00:00Z')]   # gaps 1 h (not idle), 3 h (idle)
        self.assertEqual(self.P.row(runs, self.start, self.end)['idle_hours'], 3.0)

    def test_another_products_launch_splits_a_gap_under_all(self):
        runs = [_prun('a-t-0001', '2026-10-01T01:00:00Z'), _prun('c-t-0003', '2026-10-01T05:00:00Z')]
        row = self.P.row(runs, self.start, self.end, extra_starts=['2026-10-01T03:00:00Z'])
        self.assertEqual(row['idle_hours'], 0.0)   # 2 h + 2 h: neither over 2 h

    def test_four_offline_lines_are_four_offline_ticks(self):
        text = '\n'.join(['DONE since 2026-10-01T03:00:00Z — none'] + [
            'tick: record failed (fatal: Could not resolve host)',
            'tick: record failed — offline; nothing else ran'] * 4 + ['tick: record ok'])
        lines = self.P.offline_lines([text])
        self.assertEqual(len(lines), 4)
        self.assertEqual(self.P.row([], self.start, self.end, offline=lines)['offline_ticks'], 4)

    def test_an_offline_line_dated_before_the_window_is_not_counted(self):
        text = ('DONE since 2026-09-20T03:00:00Z\ntick: record failed — offline; nothing else ran\n')
        row = self.P.row([], self.start, self.end, offline=self.P.offline_lines([text]))
        self.assertEqual(row['offline_ticks'], 0)
        self.assertIsNone(self.P.row([], self.start, self.end)['offline_ticks'])  # not read

    def test_each_run_is_one_waste_class_in_order(self):
        runs = [_prun('a-t-0001', '2026-10-01T01:00:00Z', reason='failed: not pushed'),
                _prun('b-t-0002', '2026-10-01T01:10:00Z', reason='failed: empty branch: nothing to land'),
                _prun('c-t-0003', '2026-10-01T01:20:00Z', reason='superseded by T-0009')]
        runs += [_prun('d-t-0004', f'2026-10-01T02:0{i}:00Z', head='bbbbbbb') for i in range(5)]
        w = self.P.row(runs, self.start, self.end)['waste_by_class']
        # the 4th and 5th run of d on one head are a loop; an empty branch is nothing, not failed
        self.assertEqual({k: v['runs'] for k, v in w.items()},
                         {'failed': 1, 'loop': 2, 'superseded': 1, 'nothing': 1})

    def test_a_correct_run_is_mechanical_only_when_every_cause_is(self):
        runs = [_prun('correct-t-0001', '2026-10-01T01:00:00Z', kind='correct', causes=['footprint']),
                _prun('correct-t-0002', '2026-10-01T01:00:00Z', kind='correct',
                      causes=['footprint', 'review'], usd=5.0)]
        m = self.P.row(runs, self.start, self.end)['mechanical_only_corrects']
        self.assertEqual((m['runs'], m['usd']), (1, 1.0))

    def test_heavy_share_and_cardless_heavy_reviews(self):
        runs = [_prun('review-pr-0001', '2026-10-01T01:00:00Z', kind='review', item='PR-0001',
                      model='heavy-model', usd=3.0),
                _prun('coder-t-0002', '2026-10-01T01:00:00Z', usd=1.0)]
        row = self.P.row(runs, self.start, self.end, heavy={'heavy-model'})
        self.assertEqual((row['heavy_share'], row['cardless_heavy_reviews']), (0.75, 1))

    def test_roots_follow_each_wait_to_the_item_it_ends_on(self):
        board = [{'item_id': 'T-0002', 'waits_on': 'T-0001', 'action': 'WAITS ON T-0001', 'brief_kind': ''},
                 {'item_id': 'T-0003', 'waits_on': 'delivery', 'brief_kind': 'review',
                  'action': 'WAITS ON delivery T-0002: …'},
                 {'item_id': 'T-0001', 'waits_on': 'operator', 'action': 'PARKED', 'brief_kind': ''}]
        row = self.P.row([], self.start, self.end, board=board)
        self.assertEqual(row['rows_waiting_on_item'], 2)
        self.assertEqual(row['top_roots'], [('T-0001', 2)])
        self.assertEqual(row['reviews_held_after'], 1)

    def test_pending_keys_read_null_until_their_producer_registers(self):
        row = self.P.row([], self.start, self.end)
        for key in self.P.PENDING:
            self.assertIn(key, row)
            self.assertIsNone(row[key])
        with mock.patch.dict(self.P._PRODUCERS, {'refguard_warns': lambda ctx: 7}):
            self.assertEqual(self.P.row([], self.start, self.end)['refguard_warns'], 7)

    def test_the_fixture_ledger_reproduces_the_rebaseline_since_10_01(self):
        runs = self.P.ledger_runs(os.path.join(FIXTURES, 'ledger-sample.jsonl'))
        row = self.P.row(runs, facts.to_dt('2026-10-01T12:00:00Z'),
                         facts.to_dt('2026-10-03T16:00:00Z'), heavy={'heavy-model'})
        # the re-baseline's since-10-01 column: ~$292, max 2 runs of a job on a head (0 over 3),
        # 4 mechanical-only corrects for $5.74, 1 cardless review on heavy, 2 reshape runs
        self.assertEqual((row['runs'], row['usd']), (101, 292.1))
        self.assertEqual(row['max_runs_job_head']['runs'], 2)
        self.assertEqual(row['job_heads_over_3'], 0)
        m = row['mechanical_only_corrects']
        self.assertEqual((m['runs'], m['usd']), (4, 5.74))
        self.assertEqual(row['cardless_heavy_reviews'], 1)
        self.assertEqual(row['reshape']['runs'], 2)
        self.assertEqual(row['waste_by_class']['loop']['runs'], 0)
        self.assertEqual(row['infra_ended']['runs'], 0)

    def test_window_specs(self):
        now = facts.to_dt('2026-10-03T00:00:00Z')
        s, e = self.P.window('7d', now)
        self.assertEqual(facts.iso(s), '2026-09-26T00:00:01Z')
        s, _e = self.P.window('since=2026-10-01T12:00Z', now)
        self.assertEqual(facts.iso(s), '2026-10-01T12:00:00Z')
        with self.assertRaises(ValueError):
            self.P.window('a week', now)


class TargetsTests(unittest.TestCase):
    """asf.scorecard.targets and ``asf scorecard --check``."""

    ROW = {'cardless_heavy_reviews': 0, 'heavy_share': 0.4, 'rows_waiting_on_item': None,
           'waste_by_class': {'loop': {'runs': 0, 'usd': 0.0}}}

    def setUp(self):
        from asf.scorecard import targets
        self.T = targets

    def test_each_op_and_a_dotted_key(self):
        tg = self.T.parse({'targets': [
            {'key': 'cardless_heavy_reviews', 'op': '==', 'value': 0},
            {'key': 'heavy_share', 'op': '<', 'value': 0.30},
            {'key': 'waste_by_class.loop.usd', 'op': '<=', 'value': 0},
            {'key': 'rows_waiting_on_item', 'op': 'measured'},
            {'key': 'no_such_key', 'op': 'measured'},
            {'key': 'rows_waiting_on_item', 'op': '<=', 'value': 10}]})
        oks = [ok for _t, _v, ok in self.T.check(self.ROW, tg)]
        # heavy 0.4 misses < 0.30; an absent key is not measured; an unread number misses
        self.assertEqual(oks, [True, False, True, True, False, False])

    def test_a_malformed_file_is_refused(self):
        for bad in ({}, {'targets': [{'op': '=='}]}, {'targets': [{'key': 'x', 'op': '~'}]},
                    {'targets': [{'key': 'x', 'op': '<'}]}):
            with self.assertRaises(self.T.TargetsError):
                self.T.parse(bad)

    def _check(self, text, row=None):
        from asf.views import scorecard as view
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'targets.yaml')
            with open(path, 'w') as f:
                f.write(text)
            args = mock.Mock(check=path, window='7d', all=False)
            product = mock.Mock()
            product.name = 'p'
            row = dict(row or self.ROW, start='s', end='e')
            out = io.StringIO()
            with mock.patch('asf.scorecard.program.load', return_value=row), \
                    mock.patch('sys.stdout', out):
                rc = view.cmd_check(args, product)
        return rc, out.getvalue()

    def test_check_exits_0_when_every_target_is_met(self):
        rc, out = self._check('targets:\n  - {key: cardless_heavy_reviews, op: "==", value: 0}\n')
        self.assertEqual(rc, 0)
        self.assertIn('1/1 targets met', out)

    def test_check_exits_1_listing_each_missed_target(self):
        rc, out = self._check('targets:\n  - {key: heavy_share, op: "<", value: 0.3}\n'
                              '  - {key: cardless_heavy_reviews, op: measured}\n')
        self.assertEqual(rc, 1)
        self.assertIn('MISS heavy_share = 0.4 (want < 0.3)', out)
        self.assertIn('1/2 targets met', out)

    def test_check_exits_2_on_a_malformed_file(self):
        rc, out = self._check('wave: A\n')
        self.assertEqual(rc, 2)

    def test_the_repo_targets_file_parses_and_its_wave_a_targets_are_measured(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'docs', 'program', 'targets.yaml')
        tg = self.T.parse(env.load_file(path))
        self.assertTrue(tg)
        from asf.scorecard import program
        row = program.row([], facts.to_dt('2026-10-01T00:00:00Z'), facts.to_dt('2026-10-02T00:00:00Z'),
                          board=[])
        self.assertTrue(all(ok for _t, _v, ok in self.T.check(row, tg)))


if __name__ == '__main__':
    unittest.main()
