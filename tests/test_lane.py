"""The PR lane as one state machine (:mod:`asf.harvest.lane`).

The pure table first: one :func:`lane.next_state` test per transition of the package plan's §2
(T1–T14) and per §9 addition (R4 head moved, R5 the merge queue, R6 a reopened PR, R3/R8 the
MERGING intent) — no git, no ``gh``. Then the pieces that carry the machine: the lane state on
the run line (R1), the in-process pass that stops at GATE and the detached gate pass that
decides only the gate's outcomes (R2), the docs branch the gate refuses routed to a
STARVED → SPEC/PLAN correction (R7), the docs-only trunk move that is not gated again (R25), a
gate timeout that is unknown, never red (§12), the feeder reading the lane through the one
occupancy answer (R16), and the ``NAME=value`` prefix of a test command (§12).
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from asf import env
from asf.feeder import rows as feeder_rows
from asf.harvest import harvest, lane, rebuild_check
from asf.workers import host, lifecycle

NOW = 1_800_000_000.0
HEAD, NEW = 'a' * 40, 'b' * 40


def facts(**kw):
    """A finished, pushed code branch ahead of the trunk, fast-forward, no review wanted."""
    f = {'branch': 'worker/T-0001', 'item': 'T-0001', 'head': HEAD, 'ended': True,
         'landed': False, 'live': False, 'ahead': 1, 'mode': 'ff', 'host': True, 'now': NOW,
         'stale_after': 2 * 86400, 'review_required': False}
    f.update(kw)
    return f


def rec(state, **kw):
    r = {'state': state, 'head': HEAD, 'pr': None, 'at': '2027-01-15T08:00:00Z', 'reason': ''}
    r.update(kw)
    return r


APPROVED = {'round': 1, 'verdict': 'approved', 'text': 'approved', 'path': 'r/t-0001-r1.md',
            'current': True}
CHANGES = dict(APPROVED, verdict='changes', text='changes requested')


class Transitions(unittest.TestCase):
    """One test per row of the §2 table (T1–T14)."""

    def test_t1_a_finished_run_ahead_of_the_trunk_is_pushed(self):
        self.assertEqual(lane.next_state(None, facts())[0], lane.PUSHED)
        self.assertEqual(lane.next_state(None, facts(ended=False))[0], None)  # still running
        self.assertEqual(lane.next_state(None, facts(ahead=0))[0], None)      # nothing to land
        self.assertEqual(lane.next_state(None, facts(landed=True))[0], None)

    def test_t1_a_run_owing_a_correction_is_back_not_pushed(self):
        state, reason = lane.next_state(None, facts(correction={'kind': 'gate', 'text': 'x'}))
        self.assertEqual((state, reason), (lane.BACK, 'kind=gate'))

    def test_t2_fast_forward_the_branch_is_its_own_pr(self):
        state, reason = lane.next_state(rec(lane.PUSHED), facts())
        self.assertEqual(state, lane.PR_OPEN)
        self.assertIn('its own PR', reason)

    def test_t2_pull_request_adopts_an_open_pr_or_opens_one(self):
        f = facts(mode='pr', pr={'number': 7, 'state': 'OPEN', 'head': HEAD})
        self.assertEqual(lane.next_state(rec(lane.PUSHED), f), (lane.PR_OPEN, 'PR #7'))
        self.assertEqual(lane.next_state(rec(lane.PUSHED), facts(mode='pr')),
                         (lane.PR_OPEN, 'open a PR'))

    def test_t2_adoption_a_branch_no_run_holds(self):
        self.assertEqual(lane.next_state(None, facts(adopt=True)), (lane.PUSHED, 'adopted'))

    def test_t3_a_required_review_is_a_state_not_a_correction(self):
        state, reason = lane.next_state(rec(lane.PR_OPEN), facts(review_required=True))
        self.assertEqual(state, lane.REVIEW)
        self.assertEqual(reason, 'round 1 wanted: no ASF review yet')
        stale = dict(APPROVED, current=False)
        self.assertEqual(lane.next_state(rec(lane.PR_OPEN), facts(review_required=True,
                                                                   review=stale))[1],
                         'round 2 wanted: r/t-0001-r1.md predates the head')

    def test_t4_an_approved_review_of_the_head_or_no_policy_is_the_gate(self):
        self.assertEqual(lane.next_state(rec(lane.REVIEW), facts(review_required=True,
                                                                  review=APPROVED))[0], lane.GATE)
        self.assertEqual(lane.next_state(rec(lane.PR_OPEN), facts())[0], lane.GATE)

    def test_t5_changes_on_the_head_go_back(self):
        self.assertEqual(lane.next_state(rec(lane.REVIEW), facts(review_required=True,
                                                                  review=CHANGES)),
                         (lane.BACK, 'kind=review'))

    def test_t5b_changes_a_correction_answered_without_a_commit_want_a_fresh_review(self):
        """B-0149, a product's T-0360/T-0097: the lane's restack cleared the review's finding
        (a conflict with the trunk), so the correct session found nothing to change and pushed
        nothing. The review of the head still read changes, and BACK relaunched a correction
        every wave. A review a correction answered without a commit wants a fresh round."""
        state, reason = lane.next_state(rec(lane.PR_OPEN), facts(
            review_required=True, review=CHANGES, review_answered='correct-t-0001'))
        self.assertEqual(state, lane.REVIEW)
        self.assertEqual(reason, 'round 2 wanted: r/t-0001-r1.md was answered by '
                                 'correct-t-0001 without a commit')
        self.assertEqual(lane.next_state(rec(lane.REVIEW, reason=reason), facts(
            review_required=True, review=CHANGES, review_answered='correct-t-0001'))[0],
            lane.REVIEW)

    def test_t5n_an_approval_asking_nothing_a_correction_answered_goes_to_the_gate(self):
        """A product's T-0652 (2026-10-05): the review said ``verdict: approved`` with an empty C
        list, yet read changes; the correct session reported "nothing to correct" and the row
        parked. A review that asks nothing, answered on this head by a correction, lands."""
        rv = dict(CHANGES, text='approved', asks_nothing=True)
        state, reason = lane.next_state(rec(lane.REVIEW), facts(
            review_required=True, review=rv, nothing_to_correct='correct-t-0001',
            review_answered='correct-t-0001'))
        self.assertEqual(state, lane.GATE)
        self.assertIn('nothing to correct', reason)
        # a review that asks for something still goes back, answered or not
        self.assertEqual(lane.next_state(rec(lane.PR_OPEN), facts(
            review_required=True, review=dict(CHANGES, asks_nothing=False),
            nothing_to_correct='correct-t-0001')), (lane.BACK, 'kind=review'))
        # and one no correction has looked at yet goes back for one
        self.assertEqual(lane.next_state(rec(lane.PR_OPEN), facts(
            review_required=True, review=rv)), (lane.BACK, 'kind=review'))

    def test_t5a_changes_a_ruling_overruled_on_this_head_go_to_the_gate(self):
        state, reason = lane.next_state(rec(lane.PR_OPEN), facts(
            review_required=True, review=CHANGES, overruled='adjudicate-t-0001'))
        self.assertEqual(state, lane.GATE)
        self.assertIn("overruled by adjudicate-t-0001's ruling", reason)

    def test_t6_t7_t8_the_gate_states_are_the_gates_to_decide(self):
        for s in (lane.GATE, lane.WAITING_CI, lane.WAITING):
            with self.subTest(state=s):
                self.assertEqual(lane.next_state(rec(s, reason='r'), facts())[0], s)

    def test_t9_a_lane_refusal_goes_back(self):
        f = facts(refusal=('naming', 'commits do not name T-0001'))
        self.assertEqual(lane.next_state(rec(lane.PUSHED), f), (lane.BACK, 'kind=naming'))

    def test_t10_merging_then_the_host_says_merged_is_merged(self):
        f = facts(mode='pr', pr={'number': 7, 'state': 'MERGED', 'head': HEAD, 'merge_sha': NEW})
        self.assertEqual(lane.next_state(rec(lane.MERGING, method='squash'), f),
                         (lane.MERGED, 'method=squash'))

    def test_t11_found_merged_outside_the_lane(self):
        f = facts(mode='pr', pr={'number': 7, 'state': 'MERGED', 'head': HEAD, 'merge_sha': NEW})
        for s in (lane.PR_OPEN, lane.REVIEW, lane.GATE, lane.WAITING):
            with self.subTest(state=s):
                self.assertEqual(lane.next_state(rec(s), f), (lane.MERGED, 'method=external'))
        self.assertEqual(lane.next_state(rec(lane.GATE), facts(on_trunk=True)),
                         (lane.MERGED, 'method=on-trunk'))

    def test_t11_an_old_merged_pr_does_not_close_new_work(self):
        f = facts(mode='pr', pr={'number': 3, 'state': 'MERGED', 'head': 'c' * 40})
        self.assertNotEqual(lane.next_state(rec(lane.GATE), f)[0], lane.MERGED)

    def test_t12_stale_closed_pr_gone_branch_superseded_item_unmoved(self):
        closed = facts(mode='pr', pr={'number': 7, 'state': 'CLOSED', 'head': HEAD})
        self.assertEqual(lane.next_state(rec(lane.REVIEW), closed)[0], lane.STALE)
        self.assertEqual(lane.next_state(rec(lane.GATE), facts(head=None)),
                         (lane.STALE, 'branch gone'))
        self.assertEqual(lane.next_state(rec(lane.GATE), facts(closed='Closed'))[0], lane.STALE)
        old = rec(lane.PUSHED, at='2020-01-01T00:00:00Z')
        self.assertEqual(lane.next_state(old, facts(mode='pr'))[0], lane.STALE)

    def test_t13_back_is_left_when_the_correction_is_answered(self):
        self.assertEqual(lane.next_state(rec(lane.BACK), facts(correction={'kind': 'gate'}))[0],
                         lane.BACK)
        self.assertEqual(lane.next_state(rec(lane.BACK), facts()), (lane.PUSHED,
                                                                    'correction answered'))
        self.assertEqual(lane.next_state(rec(lane.BACK), facts(head=NEW))[0], lane.PUSHED)

    def test_t14_reaped_and_merged_are_terminal(self):
        for s in (lane.REAPED, lane.MERGED):
            with self.subTest(state=s):
                self.assertEqual(lane.next_state(rec(s), facts(head=NEW))[0], s)

    def test_a_live_session_holds_the_branch(self):
        self.assertEqual(lane.next_state(rec(lane.REVIEW, reason='x'), facts(live=True, head=NEW)),
                         (lane.REVIEW, 'x'))

    def test_every_state_is_a_lane_state(self):
        for s in lane.LANE_STATES:
            self.assertIn(lane.next_state(rec(s), facts())[0], lane.LANE_STATES + (None,))


class Overrides(unittest.TestCase):
    """§9's additions, each an R-line of §10."""

    def test_r4_a_moved_head_in_any_open_state_is_pushed_again(self):
        for s in lane.OPEN_STATES:
            if s == lane.PARKED:  # Tp': PARKED has its own resume rule, not R4's
                continue
            with self.subTest(state=s):
                state, reason = lane.next_state(rec(s), facts(head=NEW))
                self.assertEqual(state, lane.PUSHED)
                self.assertIn('head moved', reason)

    def test_r4_review_currency_follows_the_head(self):
        """A review approved at the old head is history: after the move the branch is reviewed
        again (T3), never gated on the old verdict."""
        f = facts(head=NEW, review_required=True, review=dict(APPROVED, current=False))
        pushed = lane.next_state(rec(lane.GATE), f)
        self.assertEqual(pushed[0], lane.PUSHED)
        opened = lane.next_state(rec(lane.PUSHED, head=NEW), f)
        self.assertEqual(lane.next_state(rec(opened[0], head=NEW), f)[0], lane.REVIEW)

    def test_r5_queued_merged_is_ours_rejected_waits(self):
        merged = facts(mode='pr', pr={'number': 7, 'state': 'MERGED', 'head': HEAD})
        self.assertEqual(lane.next_state(rec(lane.QUEUED), merged), (lane.MERGED, 'method=queue'))
        rejected = facts(mode='pr', pr={'number': 7, 'state': 'OPEN', 'queued': False})
        self.assertEqual(lane.next_state(rec(lane.QUEUED), rejected),
                         (lane.WAITING, 'queue-rejected'))
        still = facts(mode='pr', pr={'number': 7, 'state': 'OPEN', 'queued': True})
        self.assertEqual(lane.next_state(rec(lane.QUEUED, reason='q'), still), (lane.QUEUED, 'q'))

    def test_r5_in_queue_counts_toward_the_merge_budget(self):
        product = env.Product('p', {'repo_slug': 'o/p', 'conventions': {'landing': 'pull-request'},
                                    'capacity': {'batch': {'parallel': 2}}})
        host = lane.GitHubHost(product)
        host._queue = True
        host.in_queue = 2
        self.assertEqual(host.slots(), (0, 'capacity.batch.parallel'))

    def test_r6_a_reopened_pr_leaves_stale(self):
        f = facts(mode='pr', pr={'number': 7, 'state': 'OPEN', 'head': HEAD})
        self.assertEqual(lane.next_state(rec(lane.STALE), f), (lane.PR_OPEN, 'PR #7 reopened'))
        self.assertEqual(lane.next_state(rec(lane.STALE), dict(f, closed='removed'))[0],
                         lane.STALE)

    def test_r3_merging_intent_is_ours(self):
        """A merge seen after the lane wrote MERGING is the lane's own, never ``external``."""
        f = facts(mode='pr', pr={'number': 7, 'state': 'MERGED', 'head': HEAD, 'merge_sha': NEW})
        self.assertEqual(lane.next_state(rec(lane.MERGING, method='squash'), f)[1],
                         'method=squash')
        self.assertEqual(lane.next_state(rec(lane.GATE), f)[1], 'method=external')

    def test_r8_a_crash_after_the_push_is_closed_at_the_pushed_sha(self):
        """Fast-forward: MERGING names the sha it pushes; the next pass finds it on the trunk."""
        self.assertEqual(lane.next_state(rec(lane.MERGING, sha=NEW, method='ff'),
                                         facts(merging_landed=True)), (lane.MERGED, 'method=ff'))

    def test_r8_a_crash_before_the_merge_is_gated_again(self):
        self.assertEqual(lane.next_state(rec(lane.MERGING), facts())[0], lane.GATE)
        # …but not while the harvest that wrote it still holds the gate
        self.assertEqual(lane.next_state(rec(lane.MERGING, reason='m'),
                                         facts(harvest_running=True)), (lane.MERGING, 'm'))


class Draft(unittest.TestCase):
    """Tp/Tp': the PR's owner parked it as a draft — no merge, no review/correction/adjudicate,
    no reword or rebase — and unparks it by marking it ready again (B-0130)."""

    def draft_pr(self, number=817, **kw):
        pr = {'number': number, 'state': 'OPEN', 'head': HEAD, 'draft': True}
        pr.update(kw)
        return facts(mode='pr', pr=pr)

    def test_tp_any_open_state_but_merging_and_queued_parks(self):
        f = self.draft_pr()
        for s in (None, lane.PUSHED, lane.PR_OPEN, lane.REVIEW, lane.GATE, lane.WAITING,
                  lane.WAITING_CI, lane.BACK):
            with self.subTest(state=s):
                prev = rec(s) if s is not None else None
                state, reason = lane.next_state(prev, f)
                self.assertEqual(state, lane.PARKED)
                self.assertIn('draft', reason)
                self.assertIn('#817', reason)

    def test_tp_merging_and_queued_are_not_interrupted_mid_merge(self):
        merging = facts(mode='pr', pr={'number': 817, 'state': 'OPEN', 'head': HEAD,
                                       'draft': True}, harvest_running=True)
        self.assertEqual(lane.next_state(rec(lane.MERGING, reason='m'), merging),
                         (lane.MERGING, 'm'))
        queued = self.draft_pr(queued=True)
        self.assertEqual(lane.next_state(rec(lane.QUEUED, reason='q'), queued), (lane.QUEUED, 'q'))

    def test_tp_stays_parked_while_still_a_draft(self):
        parked = rec(lane.PARKED, reason='PR #817 is a draft — parked by its owner')
        self.assertEqual(lane.next_state(parked, self.draft_pr()), (lane.PARKED, parked['reason']))

    def test_tp_a_closed_or_stale_draft_resolves_first_not_parked(self):
        closed = facts(mode='pr', pr={'number': 817, 'state': 'CLOSED', 'head': HEAD,
                                      'draft': True})
        self.assertEqual(lane.next_state(rec(lane.REVIEW), closed)[0], lane.STALE)
        merged = facts(mode='pr', pr={'number': 817, 'state': 'MERGED', 'head': HEAD,
                                      'draft': True})
        self.assertEqual(lane.next_state(rec(lane.GATE), merged), (lane.MERGED, 'method=external'))

    def test_tpprime_ready_for_review_resumes_at_pr_open(self):
        parked = rec(lane.PARKED, reason='PR #817 is a draft — parked by its owner')
        ready = facts(mode='pr', pr={'number': 817, 'state': 'OPEN', 'head': HEAD,
                                     'draft': False})
        state, reason = lane.next_state(parked, ready)
        self.assertEqual(state, lane.PR_OPEN)
        self.assertIn('#817', reason)
        # normal progression resumes from there, as any PR_OPEN branch's would
        self.assertEqual(lane.next_state(rec(state, head=HEAD), ready)[0], lane.GATE)

    def test_prs_reads_isdraft(self):
        product = env.Product('p', {'repo_slug': 'o/p', 'conventions': {'landing': 'pull-request'}})
        h = lane.GitHubHost(product)
        data = [{'number': 817, 'headRefName': 'cloud/direct-F-0111', 'headRefOid': HEAD,
                 'state': 'OPEN', 'isDraft': True, 'baseRefName': 'main'}]
        with mock.patch.object(lane.H, 'gh_json', return_value=data) as m:
            out = h.prs()
        self.assertTrue(out['cloud/direct-F-0111']['draft'])
        self.assertTrue(any('isDraft' in a for a in m.call_args[0][0]))

    def test_prs_a_ready_pr_is_not_draft(self):
        product = env.Product('p', {'repo_slug': 'o/p', 'conventions': {'landing': 'pull-request'}})
        h = lane.GitHubHost(product)
        data = [{'number': 818, 'headRefName': 'cloud/direct-F-0113', 'headRefOid': HEAD,
                 'state': 'OPEN', 'isDraft': False}]
        with mock.patch.object(lane.H, 'gh_json', return_value=data):
            out = h.prs()
        self.assertFalse(out['cloud/direct-F-0113']['draft'])


class LandingClass(unittest.TestCase):
    def product(self, **conv):
        base = {'specs_dir': 'specs', 'plans_dir': 'plans', 'reviews_dir': 'reviews'}
        base.update(conv)
        return env.Product('p', {'conventions': base})

    def test_docs_roots_and_doc_paths(self):
        p = self.product(doc_paths=['README', 'docs/guide/*'])
        self.assertEqual(lane.landing_class(p, ['specs/f.md', 'reviews/x.md']), lane.DOCS)
        self.assertEqual(lane.landing_class(p, ['README', 'docs/guide/a.md']), lane.DOCS)
        self.assertEqual(lane.landing_class(p, ['specs/f.md', 'src/a.py']), lane.CODE)
        self.assertEqual(lane.landing_class(p, []), lane.CODE)
        self.assertEqual(lane.landing_class(self.product(), ['README']), lane.CODE)

    def test_r25_a_docs_only_trunk_move_does_not_regate_a_green_branch(self):
        p = self.product()
        prev = rec(lane.WAITING, green={'head': HEAD, 'trunk': 'c' * 40})
        self.assertTrue(lane.skip_regate(prev, HEAD, ['specs/f-0002.md'], p))
        self.assertTrue(lane.skip_regate(prev, HEAD, [], p))
        self.assertFalse(lane.skip_regate(prev, HEAD, ['src/a.py'], p))
        self.assertFalse(lane.skip_regate(prev, NEW, ['specs/f-0002.md'], p))
        self.assertFalse(lane.skip_regate(rec(lane.WAITING), HEAD, [], p))
        self.assertFalse(lane.skip_regate(prev, HEAD, None, p))


# ---- the machine on a real repo ----------------------------------------------------------

GREEN = ('import unittest\n\n\nclass T(unittest.TestCase):\n'
         '    def test_ok(self):\n        self.assertTrue(True)\n')
SLOW = ('import time\nimport unittest\n\n\nclass T(unittest.TestCase):\n'
        '    def test_slow(self):\n        time.sleep(30)\n')
GATE_TEST = ('import os\nimport unittest\n\n\nclass Docs(unittest.TestCase):\n'
             '    def test_no_retired_name(self):\n'
             '        for d in ("plans", "specs"):\n'
             '            for n in os.listdir(d) if os.path.isdir(d) else ():\n'
             '                with open(os.path.join(d, n)) as f:\n'
             '                    self.assertNotIn("retired_name", f.read())\n')


def sh(cmd, cwd=None, env_=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          env=dict(harvest.clean_env(), **(env_ or {})))


def _check_in_place(product, sha, k, state_dir=None):
    rebuild_check.run_job(product, sha, k, out=lambda _l: None, state_dir=state_dir)
    return 0


class LaneFixture(unittest.TestCase):
    """A bare origin, the product's checkout, and a worker clone that pushes lane branches."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='lane_')
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        # the rebuilt head's pre-push check, run in place of its background job: these tests
        # judge what the lane does with its result (tests/test_lane_off_tick.py: where it runs)
        patcher = mock.patch.object(rebuild_check, 'spawn', _check_in_place)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.origin = os.path.join(self.base, 'origin.git')
        self.repo = os.path.join(self.base, 'repo')
        self.worker = os.path.join(self.base, 'worker')
        self.state_dir = os.path.join(self.base, 'state')
        os.makedirs(self.state_dir)
        ident = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't',
                 'GIT_COMMITTER_EMAIL': 't@t'}
        self.ident = ident
        sh(['git', 'init', '-q', '--bare', '-b', 'main', self.origin])
        sh(['git', 'clone', '-q', self.origin, self.repo])
        self.write(self.repo, 'checks/test_fx.py', GREEN)
        self.write(self.repo, 'checks/test_docs.py', GATE_TEST)
        sh(['git', 'add', '-A'], cwd=self.repo)
        sh(['git', 'commit', '-qm', 'init'], cwd=self.repo, env_=ident)
        sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)
        sh(['git', 'clone', '-q', self.origin, self.worker])

    def write(self, root, rel, text):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)

    def product(self, **conventions):
        conv = {'test_command': f'{shlex.quote(sys.executable)} -m unittest discover -s checks -p test_*.py',
                'specs_dir': 'specs', 'plans_dir': 'plans', 'reviews_dir': 'reviews',
                'lane': {'review': {'code': 'none'}},
                'branch_prefixes': {'code': 'worker/', 'plan': 'plan/', 'spec': 'spec/'}}
        conv.update(conventions)
        return env.Product('sample', {'repo_dir': self.repo, 'main': 'main',
                                      'conventions': conv, 'steps': {'batch': 'off'}})

    def push_lane(self, branch, files, subject):
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'checkout', '-q', '-B', branch, 'origin/main'], cwd=self.worker)
        for rel, text in files.items():
            self.write(self.worker, rel, text)
        sh(['git', 'add', '-A'], cwd=self.worker)
        sh(['git', 'commit', '-qm', subject], cwd=self.worker, env_=self.ident)
        sh(['git', 'push', '-q', '-f', 'origin', branch], cwd=self.worker)

    def push_lane_empty(self, branch):
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'push', '-q', 'origin', 'origin/main:refs/heads/' + branch], cwd=self.worker)

    def push_lane_body(self, branch, files, subject, body='', new=True):
        # push_lane writes a subject only; a trailer belongs in the commit's body.
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        if new:
            sh(['git', 'checkout', '-q', '-B', branch, 'origin/main'], cwd=self.worker)
        else:
            sh(['git', 'checkout', '-q', branch], cwd=self.worker)
        for rel, text in files.items():
            self.write(self.worker, rel, text)
        sh(['git', 'add', '-A'], cwd=self.worker)
        args = ['git', 'commit', '-q', '-m', subject] + (['-m', body] if body else [])
        sh(args, cwd=self.worker, env_=self.ident)
        sh(['git', 'push', '-q', '-f', 'origin', branch], cwd=self.worker)

    def note(self, branch):
        # commits_note reads origin/<trunk>..origin/<branch>: the product checkout's remote
        # refs are otherwise stale after a push from the worker clone.
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        return lane.commits_note(self.repo, 'main', branch)

    def push_main(self, files, subject):
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'checkout', '-q', '-B', 'tmp-main', 'origin/main'], cwd=self.worker)
        for rel, text in files.items():
            self.write(self.worker, rel, text)
        sh(['git', 'add', '-A'], cwd=self.worker)
        sh(['git', 'commit', '-qm', subject], cwd=self.worker, env_=self.ident)
        sh(['git', 'push', '-q', 'origin', 'tmp-main:main'], cwd=self.worker)

    def session(self, job, item, branch, kind='coder'):
        p = subprocess.Popen(['true'])
        p.wait()
        with open(os.path.join(self.state_dir, 'sessions.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps({'job': job, 'item': item, 'branch': branch, 'kind': kind,
                                'pid': p.pid, 'started': '2026-09-21T00:00:00Z'}) + '\n')
            f.write(json.dumps({'job': job, 'ended': '2026-09-21T00:05:00Z',
                                'end_reason': 'finished', 'rc': 0}) + '\n')

    def lane_of(self, branch):
        return (lifecycle.by_branch(os.path.join(self.state_dir, 'sessions.jsonl'))
                .get(branch) or {}).get('lane') or {}

    def origin_main(self):
        return sh(['git', 'rev-parse', 'main'], cwd=self.origin).stdout.strip()


class OwnCommits(LaneFixture):
    """A1 (F-0191) — the lane writes down how many commits of its own the branch carried,
    at the moment it can still count them, and carries the count forward once it cannot."""

    def test_a_branch_with_a_commit_records_own_1_through_to_merged_ff(self):
        self.push_lane('worker/T-0001', {'a.txt': 'a\n'}, 'feat(T-0001): a')
        self.session('coder-t-0001', 'T-0001', 'worker/T-0001')
        lane.lane_pass(self.product(), self.state_dir, out=lambda *_: None)
        gated = self.lane_of('worker/T-0001')
        self.assertEqual(gated['state'], lane.GATE)
        self.assertEqual(gated['own'], 1)
        harvest.run_product_harvest(self.product(), self.state_dir, out=lambda *_: None,
                                    lane_pass=False)
        landed = self.lane_of('worker/T-0001')
        self.assertEqual(landed['state'], lane.MERGED)
        self.assertEqual(landed['method'], 'ff')
        self.assertEqual(landed['own'], 1)

    def test_a_branch_pushed_at_the_trunk_tip_records_own_0(self):
        # the item's commit is already on the trunk (PD6): the branch itself carries nothing
        self.push_main({'a.txt': 'a\n'}, 'feat(T-0001): a')
        self.push_lane_empty('worker/T-0001')
        self.session('coder-t-0001', 'T-0001', 'worker/T-0001')
        lane.lane_pass(self.product(), self.state_dir, out=lambda *_: None)
        landed = self.lane_of('worker/T-0001')
        self.assertEqual(landed['state'], lane.MERGED)
        self.assertEqual(landed['method'], lane.ON_TRUNK)
        self.assertEqual(landed['own'], 0)

    def test_a_branch_gone_from_origin_carries_the_previous_own_forward(self):
        self.push_lane('worker/T-0001', {'a.txt': 'a\n'}, 'feat(T-0001): a')
        self.session('coder-t-0001', 'T-0001', 'worker/T-0001')
        lane.lane_pass(self.product(), self.state_dir, out=lambda *_: None)
        gated = self.lane_of('worker/T-0001')
        self.assertEqual(gated['own'], 1)
        # the branch's own commit reaches the trunk another way, then the branch itself is gone
        sh(['git', 'push', '-q', 'origin', 'worker/T-0001:main'], cwd=self.worker)
        sh(['git', 'push', '-q', 'origin', '--delete', 'worker/T-0001'], cwd=self.worker)
        lane.lane_pass(self.product(), self.state_dir, out=lambda *_: None)
        landed = self.lane_of('worker/T-0001')
        self.assertEqual(landed['state'], lane.MERGED)
        self.assertEqual(landed['method'], lane.ON_TRUNK)
        self.assertEqual(landed['own'], 1, 'the last pass that could count it stands (PD7)')

    def test_a_record_with_neither_known_has_no_own_key_at_all(self):
        ln = lane.Lane(self.product(), self.state_dir, out=lambda *_: None)
        f = {'branch': 'worker/T-0001', 'item': 'T-0001', 'head': HEAD}
        ln.write(f, ln.record(f, lane.STALE, 'test: no ahead, no prior own'))
        rec_ = self.lane_of('worker/T-0001')
        self.assertEqual(rec_['state'], lane.STALE)
        self.assertNotIn('own', rec_)


class ProvesNote(LaneFixture):
    """F-0257 S-59256 — the pull request says which lines are not proved. ``lane.commits_note``
    builds a third block, ``## Not proved``, from the one ``git log`` it already runs."""

    WHOLE = 'Proves: S-0021 line 5 — tests/guard.test.ts::"the type guard"'
    QUALIFIED = ('Proves: S-0021 line 6 — tests/guard.test.ts '
                 '(type guard, size guard, EXIF strip only)')
    GAP = ('Not proved: S-0021 line 7 — the EXIF strip has no test yet; '
           'the fixture needs a real JPEG')

    def test_a_whole_claim_gives_items_and_proves_and_no_not_proved(self):
        self.push_lane_body('worker/T-0001', {'a.txt': 'a\n'}, 'task(T-0001): the type guard',
                            self.WHOLE)
        self.assertEqual(self.note('worker/T-0001'),
                         '\n## Items\n\n- T-0001\n'
                         '\n## Proves\n\n- S-0021 line 5 — tests/guard.test.ts::"the type guard"\n')

    def test_the_cards_qualified_claim_gives_not_proved_and_no_proves(self):
        self.push_lane_body('worker/T-0001', {'a.txt': 'a\n'}, 'task(T-0001): the exif strip',
                            self.QUALIFIED)
        self.assertEqual(self.note('worker/T-0001'),
                         '\n## Items\n\n- T-0001\n'
                         '\n## Not proved\n\n- S-0021 line 6 — partial claim: "only" '
                         '(tests/guard.test.ts (type guard, size guard, EXIF strip only))\n')

    def test_one_of_each_gives_both_blocks_proves_first(self):
        self.push_lane_body('worker/T-0001', {'a.txt': 'a\n'}, 'task(T-0001): guard work',
                            self.WHOLE + '\n' + self.QUALIFIED)
        self.assertEqual(self.note('worker/T-0001'),
                         '\n## Items\n\n- T-0001\n'
                         '\n## Proves\n\n- S-0021 line 5 — tests/guard.test.ts::"the type guard"\n'
                         '\n## Not proved\n\n- S-0021 line 6 — partial claim: "only" '
                         '(tests/guard.test.ts (type guard, size guard, EXIF strip only))\n')

    def test_a_not_proved_trailer_with_no_proves_anywhere_gives_not_proved_alone(self):
        self.push_lane_body('worker/T-0001', {'a.txt': 'a\n'}, 'task(T-0001): exif todo',
                            self.GAP)
        self.assertEqual(self.note('worker/T-0001'),
                         '\n## Items\n\n- T-0001\n'
                         '\n## Not proved\n\n- S-0021 line 7 — the EXIF strip has no test yet; '
                         'the fixture needs a real JPEG\n')

    def test_refused_claims_come_before_declared_gaps_whatever_order_they_were_written(self):
        self.push_lane_body('worker/T-0001', {'a.txt': 'a\n'}, 'task(T-0001): exif work',
                            self.GAP + '\n' + self.QUALIFIED)
        self.assertEqual(self.note('worker/T-0001'),
                         '\n## Items\n\n- T-0001\n'
                         '\n## Not proved\n\n- S-0021 line 6 — partial claim: "only" '
                         '(tests/guard.test.ts (type guard, size guard, EXIF strip only))\n'
                         '- S-0021 line 7 — the EXIF strip has no test yet; '
                         'the fixture needs a real JPEG\n')

    def test_a_branch_with_neither_and_an_unreadable_range_both_give_empty(self):
        self.push_lane_empty('worker/T-0001')
        self.assertEqual(self.note('worker/T-0001'), '')
        self.assertEqual(lane.commits_note(os.path.join(self.base, 'no-such-repo'), 'main',
                                           'worker/T-0001'), '')

    def test_two_commits_the_trailer_on_the_older_one_still_produce_the_block(self):
        self.push_lane_body('worker/T-0001', {'a.txt': 'a\n'}, 'task(T-0001): the exif strip',
                            self.QUALIFIED)
        self.push_lane_body('worker/T-0001', {'b.txt': 'b\n'}, 'task(T-0001): tidy', new=False)
        self.assertEqual(self.note('worker/T-0001'),
                         '\n## Items\n\n- T-0001\n'
                         '\n## Not proved\n\n- S-0021 line 6 — partial claim: "only" '
                         '(tests/guard.test.ts (type guard, size guard, EXIF strip only))\n')


class LaneRepo(LaneFixture):
    """The lane over a real origin: state on the run line, the two passes, the gate's outcomes."""

    def test_r1_lane_state_lives_on_the_run_line(self):
        self.push_lane('worker/T-0001', {'a.txt': 'a\n'}, 'feat(T-0001): a')
        self.session('coder-t-0001', 'T-0001', 'worker/T-0001')
        lines = []
        lane.lane_pass(self.product(), self.state_dir, out=lines.append)
        rec_ = self.lane_of('worker/T-0001')
        self.assertEqual(rec_['state'], lane.GATE)
        self.assertEqual(rec_['head'], sh(['git', 'rev-parse', 'worker/T-0001'],
                                          cwd=self.origin).stdout.strip())
        self.assertEqual([n for n in os.listdir(self.state_dir) if n.endswith('.jsonl')],
                         ['sessions.jsonl'])  # no lanes.jsonl, no ledger of its own
        with open(os.path.join(self.state_dir, 'sessions.jsonl'), encoding='utf-8') as f:
            written = [json.loads(ln) for ln in f if '"lane"' in ln]
        self.assertTrue(written and all(ln['job'] == 'coder-t-0001' for ln in written))

    def test_review_none_never_gates_a_code_head_its_own_review_says_changes_on(self):
        """2026-10-04, T-0571 #1058: a restored CODE branch walked STALE → PR_OPEN → GATE as
        ``review: none`` although the only review of its exact head said ``changes``. A waived
        (or not required) review never walks past a current changes verdict: the branch goes
        back as on any review hold; with no review, or an approving one, it gates as before."""
        for verdict, want in (('changes requested\n\n## C\n\n- C1 a.txt:1 is wrong\n', lane.BACK),
                              ('approved\n', lane.GATE), (None, lane.GATE)):
            with self.subTest(verdict=verdict):
                self.setUp()
                files = {'a.txt': 'a\n'}
                if verdict:
                    files['reviews/1-t-0001.md'] = f'verdict: {verdict}'
                self.push_lane('worker/T-0001', files, 'feat(T-0001): a')
                self.session('coder-t-0001', 'T-0001', 'worker/T-0001')
                lines = []
                lane.lane_pass(self.product(lane={'review': {'code': 'none'}}), self.state_dir,
                               out=lines.append)
                rec_ = self.lane_of('worker/T-0001')
                self.assertEqual(rec_['state'], want, (rec_, lines))
                if want == lane.BACK:
                    self.assertEqual(rec_['reason'], 'kind=review')
                else:
                    self.assertEqual(rec_['reason'], 'review: none')

    def test_an_open_pr_whose_item_is_no_card_is_never_adopted(self):
        # `worker/retro-2026-09-19` only looks like an id (RETRO-2026): with no card, no session
        # could answer a hold, so adopting it would park it in BACK for good
        self.push_lane('worker/retro-2026-09-19', {'r.txt': 'r\n'}, 'notes')
        self.push_lane('worker/T-0001', {'a.txt': 'a\n'}, 'feat(T-0001): a')
        items = {'T-0001': {'id': 'T-0001', 'type': 'task', 'state': 'Active'}}
        ln = lane.Lane(self.product(), self.state_dir, out=lambda *_: None, items=items)
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        heads = ln.remote_heads()
        ln.trunk_sha = heads['main']
        pr = {'number': 7, 'state': 'OPEN'}
        self.assertIsNone(ln.branch_facts('worker/retro-2026-09-19', None,
                                          heads['worker/retro-2026-09-19'], pr, True, False))
        f = ln.branch_facts('worker/T-0001', None, heads['worker/T-0001'], pr, True, False)
        self.assertTrue(f['adopt'])

    def test_branch_facts_diffs_the_branch_once(self):
        # the files a branch touched were asked twice per branch per pass (already_on_trunk,
        # then the landing class): one `git diff` each, the same answer both times
        self.push_lane('worker/T-0001', {'a.txt': 'a\n', 'b.txt': 'b\n'}, 'feat(T-0001): a')
        items = {'T-0001': {'id': 'T-0001', 'type': 'task', 'state': 'Active'}}
        ln = lane.Lane(self.product(), self.state_dir, out=lambda *_: None, items=items)
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        heads = ln.remote_heads()
        ln.trunk_sha = heads['main']
        pr = {'number': 7, 'state': 'OPEN'}
        real = lane.touched_files
        with mock.patch.object(lane, 'touched_files', side_effect=real) as touched:
            f = ln.branch_facts('worker/T-0001', None, heads['worker/T-0001'], pr, True, False)
        self.assertEqual(touched.call_count, 1)
        self.assertEqual(f['files'], ['a.txt', 'b.txt'])
        self.assertFalse(f['on_trunk'])
        # given the files, already_on_trunk answers exactly as it does reading them itself
        conv = ln.conv
        for b in ('worker/T-0001',):
            self.assertEqual(
                lane.already_on_trunk(self.repo, 'main', b, conv, 'T-0001'),
                lane.already_on_trunk(self.repo, 'main', b, conv, 'T-0001',
                                      files=real(self.repo, 'main', b)))

    def test_r2_the_in_process_pass_stops_at_the_gate_and_the_gate_pass_lands(self):
        self.push_lane('worker/T-0001', {'a.txt': 'a\n'}, 'feat(T-0001): a')
        self.session('coder-t-0001', 'T-0001', 'worker/T-0001')
        before = self.origin_main()
        lane.lane_pass(self.product(), self.state_dir, out=lambda *_: None)
        self.assertEqual(self.origin_main(), before, 'the in-process pass never gates')
        # a fresh finished branch the pass has not seen yet: the detached gate never moves it
        self.push_lane('worker/T-0002', {'b.txt': 'b\n'}, 'feat(T-0002): b')
        self.session('coder-t-0002', 'T-0002', 'worker/T-0002')
        results = harvest.run_product_harvest(self.product(), self.state_dir,
                                              out=lambda *_: None, lane_pass=False)
        self.assertEqual(results, {'worker/T-0001': 'landed'})
        self.assertEqual(self.lane_of('worker/T-0001')['state'], lane.MERGED)
        self.assertEqual(self.lane_of('worker/T-0002'), {})

    def test_r7_a_docs_branch_the_gate_refuses_goes_back_to_a_starved_plan_session(self):
        self.push_lane('plan/F-0001', {'plans/f-0001.md': 'uses retired_name\n'},
                       'plan(F-0001): the plan')
        self.session('plan-f-0001', 'F-0001', 'plan/F-0001', kind='plan')
        before = self.origin_main()
        lines = []
        results = harvest.run_product_harvest(self.product(), self.state_dir, out=lines.append)
        self.assertEqual(results, {'plan/F-0001': 'held'}, lines)
        self.assertEqual(self.origin_main(), before, 'a docs branch the gate refuses never lands')
        path = os.path.join(self.state_dir, 'sessions.jsonl')
        occ = lifecycle.occupancy(path)
        self.assertEqual(occ['corrections']['F-0001']['kind'], lane.LANDING_GATE)
        items = {'F-0001': {'id': 'F-0001', 'type': 'feature', 'decided': True,
                            'state': 'Active', 'stage': 'plan-draft'}}
        (row,) = [r for r in feeder_rows.candidates(items, self.product(), [], occupancy=occ)
                  if r.item_id == 'F-0001']
        self.assertEqual((row.kind, row.brief_kind, row.branch, row.launches),
                         (feeder_rows.STARVED_PLAN, 'plan', 'plan/F-0001', True))
        self.assertIn('retired_name', row.correction + '\n'.join(lines) or '')

    def _adjudicated(self, pushed_sha, status='done', blocked_on='none', review_commit=False,
                     launch_head=None, commits='none'):
        """A held branch whose round-1 review reads changes requested, then a finished
        adjudicate run whose REPORT says ``pushed: yes <pushed_sha>`` — a product's B-1377 loop.
        ``review_commit``: the review sits in its own commit on top of the code commit, as a
        review session commits it; ``pushed_sha='code'`` then names that code commit."""
        review = ('verdict: changes requested\n\n## C\n\n- a.txt:1 is wrong\n')
        if review_commit:
            self.push_lane('worker/T-0001', {'a.txt': 'a\n'}, 'feat(T-0001): a')
            self.write(self.worker, 'reviews/1-t-0001.md', review)
            sh(['git', 'add', '-A'], cwd=self.worker)
            sh(['git', 'commit', '-qm', 'review(T-0001): round 1 — changes requested'],
               cwd=self.worker, env_=self.ident)
            sh(['git', 'push', '-q', 'origin', 'worker/T-0001'], cwd=self.worker)
        else:
            self.push_lane('worker/T-0001', {'a.txt': 'a\n', 'reviews/1-t-0001.md': review},
                           'feat(T-0001): a')
        head = sh(['git', 'rev-parse', 'worker/T-0001'], cwd=self.origin).stdout.strip()
        if pushed_sha == 'code':
            pushed_sha = sh(['git', 'rev-parse', 'worker/T-0001~1'],
                            cwd=self.origin).stdout.strip()
        if launch_head == 'head':
            launch_head = head
        log = os.path.join(self.state_dir, 'adjudicate-t-0001.jsonl')
        report = (f'REPORT\nitem: T-0001\nkind: adjudicate\nstatus: {status}\n'
                  f'branch: worker/T-0001\npushed: yes {pushed_sha or head}\ncommits: {commits}\n'
                  f'ruling: the C list is false against this branch — a.txt:1 already reads a; '
                  f'overruled, do not reopen it.\nblocked_on: {blocked_on}\nwrites: a.txt\n'
                  f'superseded_by: none\n')
        with open(log, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                                'result': report}) + '\n')
        path = os.path.join(self.state_dir, 'sessions.jsonl')
        with open(path, 'a', encoding='utf-8') as f:
            for ln in ({'job': 'coder-t-0001', 'item': 'T-0001', 'branch': 'worker/T-0001',
                        'kind': 'coder', 'pid': 1, 'started': '2026-09-21T00:00:00Z'},
                       {'job': 'coder-t-0001', 'ended': '2026-09-21T00:05:00Z',
                        'end_reason': 'finished', 'rounds': 3,
                        'correction': {'kind': 'review', 'at': '2026-09-21T00:06:00Z',
                                       'at_cap': True, 'text': 'reviews/1-t-0001.md reads '
                                       'changes requested: answer its C list'}},
                       {'job': 'adjudicate-t-0001', 'item': 'T-0001', 'branch': 'worker/T-0001',
                        'kind': 'adjudicate', 'pid': 2, 'started': '2026-09-21T00:10:00Z',
                        'log': log, **({'launch_head': launch_head} if launch_head else {})},
                       {'job': 'adjudicate-t-0001', 'ended': '2026-09-21T00:15:00Z',
                        'end_reason': 'finished'}):
                f.write(json.dumps(ln) + '\n')
        return path, head

    def test_an_adjudicate_ruling_on_the_unchanged_head_overrules_the_review(self):
        """a product's B-1377 (2026-09-26, 14 adjudicate sessions): the adjudicator overruled the
        round-1 review's C list and pushed nothing. Its run is a new job on the branch, so the
        lane walked it - → PUSHED → PR_OPEN → BACK (kind=review) off the same stale review and
        wrote a *new* correction after the ruling — which B-0128's ``settled`` (an adjudicate run
        started after the hold) can never cover — and the feeder launched another adjudicate.
        A finished ruling on this very head answers the review: the lane gates it, no new hold."""
        path, head = self._adjudicated(None)
        product = self.product(lane={'review': {'code': 'required'}})
        lines = []
        lane.lane_pass(product, self.state_dir, out=lines.append)
        rec_ = self.lane_of('worker/T-0001')
        self.assertEqual(rec_['state'], lane.GATE, lines)
        self.assertIn('overruled', rec_['reason'])
        self.assertIn('adjudicate-t-0001', rec_['reason'])
        self.assertIsNone(lifecycle.pending_correction(lifecycle.latest(path)['adjudicate-t-0001'],
                                                       path))
        # the one hold the ruling answered stays settled (B-0128): a WAITS ON row, no session
        self.assertTrue(lifecycle.corrections(path)['T-0001']['settled'])
        lane.lane_pass(product, self.state_dir, out=lines.append)  # a second tick holds nothing
        self.assertTrue(lifecycle.corrections(path)['T-0001']['settled'], lines)

    def test_a_ruling_naming_the_code_commit_under_the_review_commit_overrules_it(self):
        """a product's B-1377 (2026-09-26, 18 adjudicate sessions): the review session commits its
        review file on top of the code, so the head is the review commit — and the adjudicator's
        REPORT named the code commit (``pushed: yes 99bcb623e``), the last one it could have
        changed. Nothing outside the reviews directory differs between that sha and the head:
        the ruling answered the review of this head all the same."""
        self._adjudicated('code', review_commit=True)
        product = self.product(lane={'review': {'code': 'required'}})
        lines = []
        lane.lane_pass(product, self.state_dir, out=lines.append)
        rec_ = self.lane_of('worker/T-0001')
        self.assertEqual(rec_['state'], lane.GATE, lines)
        self.assertIn('adjudicate-t-0001', rec_['reason'])

    def test_a_ruling_launched_on_the_head_it_left_alone_overrules_whatever_sha_it_names(self):
        """The session's ``pushed:`` sha is its own claim; spawn's ``launch_head`` is the fact. A
        ruling that committed nothing, launched on the head the branch still sits on, answered
        the review of that head whatever sha its REPORT names — B-1377's last rulings wrote
        ``pushed: yes 99bcb623ee0a…`` and ``99bcb623e5...`` for the commit 99bcb623e36e…, a sha
        that exists nowhere, while spawn recorded the head both were launched on."""
        self._adjudicated('e' * 40, launch_head='head')
        product = self.product(lane={'review': {'code': 'required'}})
        lines = []
        lane.lane_pass(product, self.state_dir, out=lines.append)
        self.assertEqual(self.lane_of('worker/T-0001')['state'], lane.GATE, lines)

    def test_a_ruling_that_names_commits_or_another_launch_head_does_not_overrule(self):
        for kw in ({'pushed_sha': 'e' * 40, 'launch_head': 'f' * 40},
                   {'pushed_sha': 'e' * 40, 'launch_head': 'head', 'commits': 'abc1234 fix: C1'}):
            with self.subTest(**kw):
                self.setUp()
                self._adjudicated(**kw)
                product = self.product(lane={'review': {'code': 'required'}})
                lane.lane_pass(product, self.state_dir, out=lambda *_: None)
                self.assertEqual(self.lane_of('worker/T-0001')['state'], lane.BACK)

    def test_a_ruling_on_another_head_or_not_done_does_not_overrule(self):
        """The ruling stands on the head it ruled on only: a report naming another sha, one that
        is not ``done``, or one ``blocked_on`` another item, leaves the review's hold alone."""
        for kw in ({'pushed_sha': 'c' * 40}, {'pushed_sha': None, 'status': 'partial'},
                   {'pushed_sha': None, 'blocked_on': 'T-0009'}):
            with self.subTest(**kw):
                self.setUp()
                path, _ = self._adjudicated(**kw)
                product = self.product(lane={'review': {'code': 'required'}})
                lane.lane_pass(product, self.state_dir, out=lambda *_: None)
                self.assertEqual(self.lane_of('worker/T-0001')['state'], lane.BACK)

    def test_a_ruling_stands_when_the_lane_rebases_the_branch_under_it(self):
        """F-0173: the lane itself rebases a branch that is waiting on a done ruling (same
        patches, new shas). The review is still current (B-0147) and the ruling still stands
        (this card): the branch goes REVIEW → GATE, and the feeder is never asked for a second
        adjudicate."""
        path, head = self._adjudicated(None)
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)  # the lane saw this head as the tip
        self.push_main({'t.txt': 't\n'}, 'chore: the trunk moves')
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'checkout', '-q', '-B', 'worker/T-0001', 'origin/worker/T-0001'],
           cwd=self.worker)
        sh(['git', 'rebase', '-q', 'origin/main'], cwd=self.worker)
        sh(['git', 'push', '-q', '-f', 'origin', 'worker/T-0001'], cwd=self.worker)
        new_head = sh(['git', 'rev-parse', 'origin/worker/T-0001'], cwd=self.worker).stdout.strip()
        self.assertNotEqual(new_head, head)
        self.assertEqual(
            sh(['git', 'cat-file', '-e', head + '^{commit}'], cwd=self.repo).returncode, 0)
        product = self.product(lane={'review': {'code': 'required'}})
        lines = []
        lane.lane_pass(product, self.state_dir, out=lines.append)
        rec_ = self.lane_of('worker/T-0001')
        self.assertEqual(rec_['state'], lane.GATE, lines)
        self.assertIn('overruled', rec_['reason'])
        self.assertIn('adjudicate-t-0001', rec_['reason'])
        self.assertIsNone(lifecycle.pending_correction(lifecycle.latest(path)['adjudicate-t-0001'],
                                                       path))
        self.assertTrue(lifecycle.corrections(path)['T-0001']['settled'])

    def test_a_rebase_that_carries_new_code_never_keeps_the_ruling(self):
        """The bound on the test above: the branch is rebased *and* a real new commit is pushed
        on top. The review is no longer current, the T5a arm is never reached, and the lane asks
        for round 2 — exactly what
        test_a_correct_session_that_pushes_new_commits_asks_for_a_new_review_round requires
        without a rebase."""
        path, head = self._adjudicated('d' * 40)
        self.push_main({'t.txt': 't\n'}, 'chore: the trunk moves')
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'checkout', '-q', '-B', 'worker/T-0001', 'origin/worker/T-0001'],
           cwd=self.worker)
        sh(['git', 'rebase', '-q', 'origin/main'], cwd=self.worker)
        sh(['git', 'push', '-q', '-f', 'origin', 'worker/T-0001'], cwd=self.worker)
        self.write(self.worker, 'a.txt', 'fixed\n')
        sh(['git', 'commit', '-qam', 'fix(T-0001): answer C1'], cwd=self.worker, env_=self.ident)
        sh(['git', 'push', '-q', 'origin', 'worker/T-0001'], cwd=self.worker)
        with open(path, 'a', encoding='utf-8') as f:
            for ln in ({'job': 'correct-t-0001', 'item': 'T-0001', 'branch': 'worker/T-0001',
                        'kind': 'correct', 'pid': 3, 'started': '2026-09-21T00:20:00Z'},
                       {'job': 'correct-t-0001', 'ended': '2026-09-21T00:25:00Z',
                        'end_reason': 'finished'}):
                f.write(json.dumps(ln) + '\n')
        product = self.product(lane={'review': {'code': 'required'}})
        lane.lane_pass(product, self.state_dir, out=lambda *_: None)
        rec_ = self.lane_of('worker/T-0001')
        self.assertEqual((rec_['state'], rec_.get('round')), (lane.REVIEW, 2), rec_)
        self.assertNotIn('T-0001', lifecycle.corrections(path))

    def test_a_correct_session_that_pushes_new_commits_asks_for_a_new_review_round(self):
        """After a session answering a review pushes new commits, the next lane state is a new
        REVIEW round on the new head — never another correction off the stale review file."""
        path, head = self._adjudicated('d' * 40)
        sh(['git', 'checkout', '-q', 'worker/T-0001'], cwd=self.worker)
        self.write(self.worker, 'a.txt', 'fixed\n')
        sh(['git', 'commit', '-qam', 'fix(T-0001): answer C1'], cwd=self.worker, env_=self.ident)
        sh(['git', 'push', '-q', 'origin', 'worker/T-0001'], cwd=self.worker)
        with open(path, 'a', encoding='utf-8') as f:
            for ln in ({'job': 'correct-t-0001', 'item': 'T-0001', 'branch': 'worker/T-0001',
                        'kind': 'correct', 'pid': 3, 'started': '2026-09-21T00:20:00Z'},
                       {'job': 'correct-t-0001', 'ended': '2026-09-21T00:25:00Z',
                        'end_reason': 'finished'}):
                f.write(json.dumps(ln) + '\n')
        product = self.product(lane={'review': {'code': 'required'}})
        lane.lane_pass(product, self.state_dir, out=lambda *_: None)
        rec_ = self.lane_of('worker/T-0001')
        self.assertEqual((rec_['state'], rec_.get('round')), (lane.REVIEW, 2), rec_)
        self.assertNotIn('T-0001', lifecycle.corrections(path))

    def test_red_trunk_waits_never_a_round(self):
        self.push_lane('worker/T-0001', {'a.txt': 'a\n'}, 'feat(T-0001): a')
        self.session('coder-t-0001', 'T-0001', 'worker/T-0001')
        self.push_main({'checks/test_red.py': GREEN.replace('True)', 'False)')}, 'red trunk')
        results = harvest.run_product_harvest(self.product(), self.state_dir,
                                              out=lambda *_: None)
        self.assertEqual(results, {'worker/T-0001': 'waiting'})
        run = lifecycle.by_branch(os.path.join(self.state_dir, 'sessions.jsonl'))['worker/T-0001']
        self.assertEqual((run['lane']['state'], run['lane']['reason']), (lane.WAITING, 'trunk-red'))
        self.assertFalse(run.get('correction'))
        self.assertFalse(run.get('rounds'))

    def test_gate_timeout_is_unknown_not_red(self):
        """§12: a gate that runs out of time bisects nothing and blames no one: the set waits
        (``gate-timeout``) and is retried; the second timeout in a row leaves one ``gate too
        slow`` line for the status and doctor rows."""
        product = self.product(harvest={'gate_timeout_s': 1})
        for n in (1, 2):
            self.push_lane(f'worker/T-000{n}', {f'checks/test_slow{n}.py': SLOW},
                           f'feat(T-000{n}): slow')
            self.session(f'coder-t-000{n}', f'T-000{n}', f'worker/T-000{n}')
        lines = []
        with mock.patch.object(env, 'state_dir', lambda *_a, **_k: self.state_dir):
            for tick in (1, 2):
                results = harvest.run_product_harvest(product, self.state_dir, out=lines.append)
                self.assertEqual(results, {'worker/T-0001': 'waiting', 'worker/T-0002': 'waiting'})
            slow = lane.gate_slow_line(product)
            from asf import doctor
            from asf.views import status
            self.assertEqual(doctor.check_gate_speed(product), slow)
            self.assertEqual(status.gate_cell(product), slow)
        self.assertFalse([ln for ln in lines if 'bisecting' in ln], lines)
        for b in ('worker/T-0001', 'worker/T-0002'):
            rec_ = self.lane_of(b)
            self.assertEqual((rec_['state'], rec_['reason'], rec_['timeouts']),
                             (lane.WAITING, 'gate-timeout', 2))
            run = lifecycle.by_branch(os.path.join(self.state_dir, 'sessions.jsonl'))[b]
            self.assertFalse(run.get('correction'))
        self.assertTrue(slow and slow.startswith('gate too slow: 1s vs gate_timeout_s'), slow)

    def test_host_pressure_holds_the_gate_and_starts_no_suite(self):
        """B-0109: the landing gate *is* a full test suite. On a host already at its guards it is
        never started — the set waits (``host-pressure``), nobody is blamed, and the next tick on a
        quiet host gates it and lands it. The incident was this suite beside a worker's own."""
        self.push_lane('worker/T-0001', {'a.txt': 'a\n'}, 'feat(T-0001): a')
        self.session('coder-t-0001', 'T-0001', 'worker/T-0001')
        before = self.origin_main()
        lines = []
        with mock.patch.dict(os.environ, {host.READING_ENV: '90 12 87'}), \
                mock.patch.object(harvest, 'product_gate',
                                  side_effect=AssertionError('a suite started under pressure')):
            results = harvest.run_product_harvest(self.product(), self.state_dir, out=lines.append)
        self.assertEqual(results, {'worker/T-0001': 'waiting'}, lines)
        self.assertEqual(self.origin_main(), before, 'nothing lands under host pressure')
        self.assertTrue([ln for ln in lines if 'host pressure load 90/cores 12, swap 87%' in ln],
                        lines)
        run = lifecycle.by_branch(os.path.join(self.state_dir, 'sessions.jsonl'))['worker/T-0001']
        self.assertEqual((run['lane']['state'], run['lane']['reason']),
                         (lane.WAITING, lane.HOST_PRESSURE))
        self.assertFalse(run.get('correction'))  # pressure is not a defect: no round, no blame
        self.assertFalse(run.get('rounds'))
        results = harvest.run_product_harvest(self.product(), self.state_dir, out=lines.append)
        self.assertEqual(results, {'worker/T-0001': 'landed'}, lines)

    def test_gate_command_env_prefix(self):
        """§12: ``NAME=value`` tokens leading ``ci.test_command`` are the command's environment,
        not a program to run."""
        self.assertEqual(harvest.command_env(['A=1', 'B_2=x y', 'python3', 'C=3']),
                         (['python3', 'C=3'], {'A': '1', 'B_2': 'x y'}))
        probe = ('import os, unittest\n\n\nclass E(unittest.TestCase):\n'
                 '    def test_env(self):\n'
                 '        self.assertEqual(os.environ.get("LANE_PROBE"), "on")\n')
        self.push_lane('worker/T-0001', {'checks/test_probe.py': probe}, 'feat(T-0001): probe')
        self.session('coder-t-0001', 'T-0001', 'worker/T-0001')
        cmd = f'LANE_PROBE=on {shlex.quote(sys.executable)} -m unittest discover -s checks -p test_*.py'
        results = harvest.run_product_harvest(self.product(test_command=cmd), self.state_dir,
                                              out=lambda *_: None)
        self.assertEqual(results, {'worker/T-0001': 'landed'})


class OneJobTwoBranches(LaneFixture):
    """A product's F-0011: its spec and plan PRs (#1023, #1037) were each corrected under the one
    job name ``correct-f-0011`` — the spec run first, the plan run later. Every lane write went
    to the job's latest run, so the spec branch's record landed on the plan run: each tick the
    plan read the spec's head as its own ("head moved a1cfbfc → 21807f8"), the spec never saw
    its own record move ("head moved 3024d40 → a1cfbfc"), and both re-entered PUSHED → PR_OPEN
    → REVIEW/GATE for good — a fresh review round asked for on every pass."""

    def launch(self, job, branch, kind, started):
        p = subprocess.Popen(['true'])
        p.wait()
        with open(os.path.join(self.state_dir, 'sessions.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps({'job': job, 'item': 'F-0011', 'feature': 'F-0011',
                                'branch': branch, 'kind': kind, 'pid': p.pid,
                                'started': started}) + '\n')
            f.write(json.dumps({'job': job, 'ended': started.replace('00Z', '05Z'),
                                'end_reason': 'finished', 'rc': 0}) + '\n')

    def test_a_spec_and_plan_pair_under_one_job_name_converge_in_one_pass_and_stay(self):
        self.push_lane('spec/F-0011', {'specs/f-0011.md': 'the spec\n'}, 'spec(F-0011): spec')
        self.push_lane('plan/F-0011', {'plans/f-0011.md': 'the plan\n'}, 'plan(F-0011): plan')
        self.launch('correct-f-0011', 'spec/F-0011', 'correct', '2026-09-27T05:39:00Z')
        self.launch('correct-f-0011', 'plan/F-0011', 'correct', '2026-09-27T08:42:00Z')
        heads = {b: sh(['git', 'rev-parse', b], cwd=self.origin).stdout.strip()
                 for b in ('spec/F-0011', 'plan/F-0011')}
        product = self.product()
        lane.lane_pass(product, self.state_dir, out=lambda *_: None)
        for b, sha in heads.items():
            self.assertEqual(self.lane_of(b).get('head'), sha, f'{b} holds its own head')
        before = {b: self.lane_of(b) for b in heads}
        for _ in range(2):
            lines = []
            lane.lane_pass(product, self.state_dir, out=lines.append)
            self.assertEqual([ln for ln in lines if 'head moved' in ln or ' → ' in ln], [],
                             'a pass on unmoved heads moves neither branch')
            for b, sha in heads.items():
                self.assertEqual(self.lane_of(b).get('head'), sha)
                self.assertEqual(self.lane_of(b).get('state'), before[b].get('state'))


class DeliveryLaneTest(LaneFixture):
    """A delivery's branch (F-0102): a lead whose ``delivers:`` names its members, one commit per
    member — accepted; a member no commit names is reported on the landing line, not refused."""
    BRANCH = 'worker/F-0097'
    LEAD = 'F-0097'

    def items(self, *members):
        return {self.LEAD: {'delivers': list(members)}}

    def push_commits(self, branch, commits):
        """``commits``: ``[(subject, {rel: text})]`` — one commit per pair, oldest first."""
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'checkout', '-q', '-B', branch, 'origin/main'], cwd=self.worker)
        for subject, files in commits:
            for rel, text in files.items():
                self.write(self.worker, rel, text)
            sh(['git', 'add', '-A'], cwd=self.worker)
            sh(['git', 'commit', '-qm', subject], cwd=self.worker, env_=self.ident)
        sh(['git', 'push', '-q', '-f', 'origin', branch], cwd=self.worker)

    def refusal(self, commits, members):
        self.push_commits(self.BRANCH, commits)
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        return lane.lane_refusal(self.repo, 'main', self.BRANCH, self.LEAD, members=members)

    def land(self, commits, members, **conventions):
        self.push_commits(self.BRANCH, commits)
        self.session('coder-f-0097', self.LEAD, self.BRANCH)
        lines = []
        results = harvest.run_product_harvest(self.product(**conventions), self.state_dir,
                                              out=lines.append, items=self.items(*members))
        return results, [ln for ln in lines if ln.startswith(('landed', 'held'))]

    def test_commits_naming_different_members_are_accepted(self):
        self.assertIsNone(self.refusal([('feat(F-0097): the feature', {'a.txt': 'a\n'}),
                                        ('fix(B-0034): the bug', {'b.txt': 'b\n'})],
                                       ('F-0097', 'B-0034')))

    def test_a_commit_naming_no_member_is_refused(self):
        kind, text = self.refusal([('feat(F-0097): the feature', {'a.txt': 'a\n'}),
                                   ('fix(B-0034): the bug', {'b.txt': 'b\n'}),
                                   ('fix(B-0099): not a member', {'c.txt': 'c\n'})],
                                  ('F-0097', 'B-0034'))
        self.assertEqual(kind, 'naming')
        self.assertIn('commits do not name F-0097 or one of F-0097, B-0034', text)

    def test_a_missing_member_is_reported_not_refused(self):
        commits = [('feat(F-0097): the feature', {'a.txt': 'a\n'}),
                   ('fix(B-0034): the bug', {'b.txt': 'b\n'})]
        members = ('F-0097', 'B-0034', 'S-0055')
        self.assertIsNone(self.refusal(commits, members))
        self.assertEqual(lane.members_named(self.repo, 'main', self.BRANCH, members),
                         (['F-0097', 'B-0034'], ['S-0055']))

    def missing_member_lands(self, gate):
        results, lines = self.land([('feat(F-0097): the feature', {'a.txt': 'a\n'}),
                                    ('fix(B-0034): the bug', {'b.txt': 'b\n'})],
                                   ('F-0097', 'B-0034', 'S-0055'), harvest={'gate': gate})
        self.assertEqual(results, {self.BRANCH: 'landed'})
        sha = sh(['git', 'rev-parse', 'main'], cwd=self.origin).stdout.strip()
        self.assertEqual(lines, [f'landed {self.BRANCH} → {sha}: delivered F-0097, B-0034 '
                                 '— no commit for S-0055'])

    def test_the_landing_line_names_the_missing_member_combined(self):
        self.missing_member_lands('combined')

    def test_the_landing_line_names_the_missing_member_per_branch(self):
        self.missing_member_lands('per-branch')

    def test_a_delivery_with_every_member_named_lands_with_no_missing_clause(self):
        results, lines = self.land([('feat(F-0097): the feature', {'a.txt': 'a\n'}),
                                    ('fix(B-0034): the bug', {'b.txt': 'b\n'})],
                                   ('F-0097', 'B-0034'))
        self.assertEqual(results, {self.BRANCH: 'landed'})
        self.assertTrue(lines[0].endswith(': delivered F-0097, B-0034'), lines)

    def test_merge_commit_still_refused(self):
        self.push_commits(self.BRANCH, [('feat(F-0097): the feature', {'a.txt': 'a\n'})])
        sh(['git', 'checkout', '-q', 'main'], cwd=self.worker)
        self.write(self.worker, 'm.txt', 'm\n')
        sh(['git', 'add', '-A'], cwd=self.worker)
        sh(['git', 'commit', '-qm', 'trunk moves'], cwd=self.worker, env_=self.ident)
        sh(['git', 'push', '-q', 'origin', 'main'], cwd=self.worker)
        sh(['git', 'checkout', '-q', self.BRANCH], cwd=self.worker)
        sh(['git', 'merge', '-q', '--no-edit', 'main', '-m', 'fix(B-0034): merge main'],
           cwd=self.worker, env_=self.ident)
        sh(['git', 'push', '-q', 'origin', self.BRANCH], cwd=self.worker)
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        kind, text = lane.lane_refusal(self.repo, 'main', self.BRANCH, self.LEAD,
                                       members=('F-0097', 'B-0034'))
        self.assertEqual(kind, 'merge')
        self.assertIn('merge commit on a lane branch', text)

    def session_at(self, job, item, branch, started, end_reason='failed: dead pid'):
        """A run that ended without a report — a crash, a run cap, a spent window."""
        p = subprocess.Popen(['true'])
        p.wait()
        with open(os.path.join(self.state_dir, 'sessions.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps({'job': job, 'item': item, 'branch': branch, 'kind': 'delivery-code',
                                'pid': p.pid, 'started': started}) + '\n')
            f.write(json.dumps({'job': job, 'ended': started, 'end_reason': end_reason, 'rc': 1})
                    + '\n')

    def test_a_feature_delivery_not_whole_is_held_and_lands_once_whole(self):
        """T9i (``delivery: feature``): a Task-led delivery whose run died with one member's
        commit missing is held ``incomplete`` — no PR, the branch kept — and the next push that
        completes it lands with every member."""
        branch, lead = 'worker/T-0001', 'T-0001'
        items = {'T-0001': {'type': 'task', 'delivers': ['T-0001', 'T-0002']},
                 'T-0002': {'type': 'task', 'delivered_by': 'T-0001'}}
        self.push_commits(branch, [('feat(T-0001): one', {'a.txt': 'a\n'})])
        self.session_at('delivery-code-t-0001', lead, branch, '2026-09-21T00:00:00Z')
        before = self.origin_main()
        lines = []
        results = harvest.run_product_harvest(self.product(), self.state_dir, out=lines.append,
                                              items=items)
        self.assertEqual(results, {branch: 'held'}, lines)
        run = lifecycle.by_branch(os.path.join(self.state_dir, 'sessions.jsonl'))[branch]
        self.assertEqual(run['lane']['state'], lane.BACK)
        self.assertEqual(run['correction']['kind'], lifecycle.INCOMPLETE)
        self.assertIn('no commit on origin/worker/T-0001 names T-0002', run['correction']['text'])
        self.assertIn('the run ended without a report', run['correction']['text'])
        self.assertEqual(run['correction']['finding'], ['T-0002'])
        self.assertEqual(self.origin_main(), before, 'nothing lands while the delivery is partial')
        self.assertTrue(any('held worker/T-0001: delivery incomplete' in ln for ln in lines), lines)
        # the session answers the hold from the branch's head: the second member's commit lands both
        self.push_commits(branch, [('feat(T-0001): one', {'a.txt': 'a\n'}),
                                   ('feat(T-0002): two', {'b.txt': 'b\n'})])
        later = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + 60))
        self.session('delivery-code-t-0001', lead, branch)
        with open(os.path.join(self.state_dir, 'sessions.jsonl'), encoding='utf-8') as f:
            text = f.read().replace('"started": "2026-09-21T00:00:00Z"', f'"started": "{later}"', 1)
        # the fixture's session helper stamps a fixed start: the answering run must start after
        # the hold, so its launch line is restamped (the last launch line is the answer's)
        head, _sep, tail = text.rpartition('"started": "2026-09-21T00:00:00Z"')
        text = head + f'"started": "{later}"' + tail if _sep else text
        with open(os.path.join(self.state_dir, 'sessions.jsonl'), 'w', encoding='utf-8') as f:
            f.write(text)
        results = harvest.run_product_harvest(self.product(), self.state_dir, out=lines.append,
                                              items=items)
        self.assertEqual(results, {branch: 'landed'}, lines)
        self.assertTrue(any(ln.startswith('landed worker/T-0001') and ln.endswith(': delivered T-0001, T-0002')
                            for ln in lines), lines)

    def test_item_footprint_is_the_union_of_the_lead_and_its_members(self):
        items = {'F-0097': {'delivers': ['F-0097', 'B-0034'], 'writes': ['a/**']},
                 'B-0034': {'writes': ['a/**', 'b.py']}, 'B-0001': {'writes': ['z']}}
        self.assertEqual(lane.item_footprint(items, 'F-0097'), ['a/**', 'b.py'])
        self.assertEqual(lane.item_footprint(items, 'B-0001'), ['z'])
        self.assertEqual(lane.item_footprint(None, 'B-0001'), [])


class FakePRHost(lane.FastForwardHost):
    """Fast-forward underneath, with a PR list and a ``close`` that records what it said."""

    def __init__(self, product, ln, prs):
        super().__init__(product, ln)
        self._prs, self.closed = prs, []

    def prs(self):
        return self._prs

    def close(self, number, comment):
        self.closed.append((number, comment))
        return True


class Orphans(LaneRepo):
    """A lane branch no run holds — the pre-record lane's ``<prefix><plan>-t3``, a card's second
    branch — is never left open for good: it lands, is closed with a comment and archived, or
    is picked back up by the lane for its card."""

    LATER = 3 * 86400  # past the default lane.stale_after (2d)

    def run_lane(self, items, prs=None, later=True):
        ln = lane.Lane(self.product(), self.state_dir, out=lambda *_: None, items=items,
                       now=time.time() + (self.LATER if later else 0))
        ln.host = FakePRHost(ln.product, ln, prs or {})
        lane.lane_pass(ln.product, self.state_dir, items=items, lane=ln)
        return ln

    def heads(self):
        return sh(['git', 'for-each-ref', '--format=%(refname:short)', 'refs/heads'],
                  cwd=self.origin).stdout.split()

    def test_a_done_card_closes_the_pr_with_a_comment_and_archives_the_branch(self):
        self.push_lane('worker/free-plan-t1', {'a.txt': 'a\n'}, 'feat: a')
        items = {'T-0007': {'id': 'T-0007', 'type': 'task', 'state': 'Closed',
                            'legacy_id': 'FREE-1/T1', 'parent': 'F-0001'},
                 'F-0001': {'id': 'F-0001', 'type': 'feature', 'state': 'Active',
                            'legacy_id': 'free-plan', 'children': ['T-0007']}}
        prs = {'worker/free-plan-t1': {'number': 70, 'state': 'OPEN', 'title': 'a'}}
        ln = self.run_lane(items, prs, later=False)  # a done card needs no waiting
        self.assertEqual([n for n, _ in ln.host.closed], [70])
        self.assertIn('T-0007 is Closed in the record', ln.host.closed[0][1])
        self.assertIn('archive/worker/free-plan-t1', ln.host.closed[0][1])
        self.assertNotIn('worker/free-plan-t1', self.heads())
        self.assertIn('archive/worker/free-plan-t1', self.heads())
        self.assertEqual(self.lane_of('worker/free-plan-t1')['state'], lane.STALE)

    def test_no_card_closes_once_past_stale_after_never_before(self):
        self.push_lane('worker/factory-retro-2026-09-19', {'r.txt': 'r\n'}, 'notes')
        prs = {'worker/factory-retro-2026-09-19': {'number': 48, 'state': 'OPEN'}}
        ln = self.run_lane({}, prs, later=False)
        self.assertEqual(ln.host.closed, [])
        self.assertIn('worker/factory-retro-2026-09-19', self.heads())
        ln = self.run_lane({}, prs)
        self.assertEqual([n for n, _ in ln.host.closed], [48])
        self.assertIn('no card in the record claims it', ln.host.closed[0][1])
        self.assertNotIn('worker/factory-retro-2026-09-19', self.heads())

    def test_an_unclaimed_legacy_branch_is_deleted_not_archived(self):
        """Retention deletes archives after days anyway: a superseded branch under a legacy
        prefix no card claims is deleted outright, with one line saying so."""
        self.push_lane('worker/factory-retro-2026-09-19', {'r.txt': 'r\n'}, 'notes')
        prs = {'worker/factory-retro-2026-09-19': {'number': 48, 'state': 'OPEN'}}
        lines = []
        product = self.product(branch_retention={'legacy_prefixes': ['worker/factory-']})
        ln = lane.Lane(product, self.state_dir, out=lines.append, items={},
                       now=time.time() + self.LATER)
        ln.host = FakePRHost(product, ln, prs)
        lane.lane_pass(product, self.state_dir, items={}, lane=ln)
        self.assertEqual([n for n, _ in ln.host.closed], [48])
        self.assertIn('deleted, not archived', ln.host.closed[0][1])
        self.assertNotIn('worker/factory-retro-2026-09-19', self.heads())
        self.assertNotIn('archive/worker/factory-retro-2026-09-19', self.heads())
        said = [x for x in lines if x.startswith('superseded worker/factory-retro-2026-09-19')]
        self.assertEqual(len(said), 1, lines)
        self.assertIn('deleted, not archived (legacy prefix worker/factory-', said[0])
        self.assertEqual(self.lane_of('worker/factory-retro-2026-09-19')['state'], lane.STALE)

    def test_a_hosted_origin_archives_and_deletes_through_the_api_never_a_push(self):
        self.push_lane('worker/factory-retro-2026-09-19', {'r.txt': 'r\n'}, 'notes')
        b = 'worker/factory-retro-2026-09-19'
        head = sh(['git', 'rev-parse', b], cwd=self.origin).stdout.strip()
        calls = []

        def fake(args):
            calls.append(args)
            if args[:3] == ['api', '-X', 'POST'] and args[3].endswith('/git/commits'):
                return 0, 'c' * 40 + '\n', ''
            if args[0] == 'api' and args[1] == f'repos/o/p/git/ref/heads/{b}':
                return 0, head + '\n', ''
            return 0, '', ''
        lines = []
        with mock.patch.object(lane, 'repo_slug', return_value='o/p'), \
                mock.patch('asf.init.slug_from_url', return_value='o/p'), \
                mock.patch.object(harvest, '_gh', side_effect=fake):
            ln = lane.Lane(self.product(), self.state_dir, out=lines.append, items={},
                           now=time.time() + self.LATER)
            ln.host = FakePRHost(ln.product, ln, {})
            lane.lane_pass(ln.product, self.state_dir, items={}, lane=ln)
        self.assertEqual(calls[1], ['api', '-X', 'POST', 'repos/o/p/git/refs', '-f',
                                    f'ref=refs/heads/archive/{b}', '-f', 'sha=' + 'c' * 40])
        self.assertIn('parents[]=' + head, calls[0])
        self.assertEqual(calls[-1], ['api', '-X', 'DELETE', f'repos/o/p/git/refs/heads/{b}'])
        self.assertIn(b, self.heads())  # nothing went by git: the fake host kept it
        self.assertNotIn(f'archive/{b}', self.heads())
        timed = [x for x in lines if x.startswith(f'lane: push {b} ')]
        self.assertEqual(len(timed), 2, lines)
        self.assertRegex(timed[0], r'\(archive, api\)$')
        self.assertRegex(timed[1], r'\(delete, api\)$')
        self.assertFalse(os.path.isdir(os.path.join(self.state_dir, lane.REF_PUSH_DIR))
                         and os.listdir(os.path.join(self.state_dir, lane.REF_PUSH_DIR)))

    def test_an_open_card_it_alone_answers_for_is_picked_back_up(self):
        self.push_lane('worker/free-plan-t3', {'c.txt': 'c\n'}, 'feat: the door')
        items = {'T-0009': {'id': 'T-0009', 'type': 'task', 'state': 'Active',
                            'legacy_id': 'FREE-1/T3', 'parent': 'F-0001'},
                 'F-0001': {'id': 'F-0001', 'type': 'feature', 'state': 'Active',
                            'legacy_id': 'free-plan', 'children': ['T-0009']}}
        prs = {'worker/free-plan-t3': {'number': 723, 'state': 'OPEN'}}
        ln = self.run_lane(items, prs, later=False)
        self.assertEqual(ln.host.closed, [])
        rec_ = self.lane_of('worker/free-plan-t3')
        # adopted for its card; its pre-record commits name no item, so the lane rewords them
        # itself — no correction session — and moves it on
        self.assertEqual((rec_['item'], rec_['state']), ('T-0009', lane.GATE))
        path = os.path.join(self.state_dir, 'sessions.jsonl')
        self.assertEqual(lifecycle.by_branch(path)['worker/free-plan-t3']['item'], 'T-0009')
        self.assertNotIn('T-0009', lifecycle.corrections(path))
        self.assertEqual(sh(['git', 'log', '-1', '--format=%s', 'worker/free-plan-t3'],
                            cwd=self.origin).stdout.strip(), 'feat(T-0009): the door')

    def test_a_card_another_branch_answers_for_supersedes_the_orphan(self):
        self.push_lane('worker/T-0009', {'c.txt': 'new\n'}, 'feat(T-0009): the door')
        self.session('coder-t-0009', 'T-0009', 'worker/T-0009')
        self.push_lane('worker/free-plan-t3', {'c.txt': 'old\n'}, 'feat: the door')
        items = {'T-0009': {'id': 'T-0009', 'type': 'task', 'state': 'Active',
                            'links': {'prs': [723]}}}
        prs = {'worker/free-plan-t3': {'number': 723, 'state': 'OPEN'}}
        ln = self.run_lane(items, prs)
        self.assertEqual([n for n, _ in ln.host.closed], [723])
        self.assertIn('T-0009 is answered by worker/T-0009', ln.host.closed[0][1])
        self.assertIn('worker/T-0009', self.heads())

    def test_too_far_behind_to_rebase_is_closed_and_its_card_goes_back_to_the_feeder(self):
        self.push_lane('worker/free-plan-t3', {'c.txt': 'branch\n'}, 'feat: the door')
        self.push_main({'c.txt': 'trunk\n'}, 'trunk moved')
        items = {'T-0009': {'id': 'T-0009', 'type': 'task', 'state': 'Active',
                            'links': {'branches': ['worker/free-plan-t3']}}}
        prs = {'worker/free-plan-t3': {'number': 723, 'state': 'OPEN'}}
        ln = self.run_lane(items, prs)
        self.assertEqual([n for n, _ in ln.host.closed], [723])
        self.assertIn('too far behind main to rebase', ln.host.closed[0][1])
        occ = lifecycle.occupancy(os.path.join(self.state_dir, 'sessions.jsonl'), alive=lambda *_: False)
        self.assertNotIn('T-0009', occ['busy'])
        self.assertNotIn('T-0009', occ['waiting_landing'])

    def test_a_diff_already_on_the_trunk_lands_and_closes_the_pr(self):
        self.push_lane('worker/fact-t3', {'d.txt': 'd\n'}, 'feat: d')
        self.push_main({'d.txt': 'd\n'}, 'd landed another way')
        prs = {'worker/fact-t3': {'number': 566, 'state': 'OPEN'}}
        ln = self.run_lane({}, prs, later=False)
        self.assertEqual([n for n, _ in ln.host.closed], [566])
        self.assertIn('already on main', ln.host.closed[0][1])
        self.assertNotIn('worker/fact-t3', self.heads())
        self.assertEqual(self.lane_of('worker/fact-t3')['state'], lane.MERGED)

    def lane_with_lines(self, prs):
        lines = []
        ln = lane.Lane(self.product(), self.state_dir, out=lines.append, items=None,
                       now=time.time())
        ln.host = FakePRHost(ln.product, ln, prs)
        lane.lane_pass(ln.product, self.state_dir, lane=ln)
        return ln, lines

    def test_a_branch_reset_to_the_trunk_tip_with_an_open_pr_is_not_landed(self):
        # a repair archived a corrupted branch and reset it to the trunk; its work is pushed
        # again seconds later. Its tip equals the trunk, but none of its commits is there.
        self.push_lane('worker/T-0001', {'a.txt': 'a\n'}, 'feat(T-0001): a')
        self.session('coder-t-0001', 'T-0001', 'worker/T-0001')
        prs = {'worker/T-0001': {'number': 9, 'state': 'OPEN'}}
        self.lane_with_lines(prs)
        before = self.lane_of('worker/T-0001')
        self.assertTrue(before.get('state'))
        sh(['git', 'push', '-q', '-f', 'origin', 'origin/main:refs/heads/worker/T-0001'],
           cwd=self.worker)
        ln, lines = self.lane_with_lines(prs)
        self.assertIn('worker/T-0001', self.heads(), 'an empty branch is never deleted')
        self.assertEqual(ln.host.closed, [])
        self.assertEqual(self.lane_of('worker/T-0001'), before, 'its record is not advanced')
        self.assertEqual(ln.results.get('worker/T-0001'), 'waiting')
        self.assertTrue([l for l in lines if l.startswith('empty branch — waits: worker/T-0001')
                         and 'PR #9' in l], lines)
        self.assertFalse([l for l in lines if 'already on main' in l], lines)
        # the same reset with no PR at all: no T-0001 commit is on the trunk, so still not landed
        ln, lines = self.lane_with_lines({})
        self.assertIn('worker/T-0001', self.heads())
        self.assertEqual(self.lane_of('worker/T-0001'), before)

    def test_a_branch_whose_item_commit_reached_the_trunk_lands(self):
        self.push_lane('worker/T-0001', {'a.txt': 'a\n'}, 'feat(T-0001): a')
        self.session('coder-t-0001', 'T-0001', 'worker/T-0001')
        self.lane_with_lines({})
        sh(['git', 'push', '-q', 'origin', 'worker/T-0001:main'], cwd=self.worker)
        ln, lines = self.lane_with_lines({})
        self.assertEqual(self.lane_of('worker/T-0001')['state'], lane.MERGED)
        self.assertEqual(ln.results.get('worker/T-0001'), 'landed')
        self.assertNotIn('worker/T-0001', self.heads())

    def test_one_pass_takes_up_a_bounded_number_of_orphans(self):
        for i in range(3):
            self.push_lane(f'worker/old-{i}', {f'o{i}.txt': 'o\n'}, 'old')
        with mock.patch.object(lane, 'ORPHANS_PER_PASS', 2):
            self.run_lane({})
            self.assertEqual(sum(b.startswith('worker/old-') for b in self.heads()), 1)
            self.run_lane({})
        self.assertEqual(sum(b.startswith('worker/old-') for b in self.heads()), 0)


HOOK = """#!/bin/sh
# a product's pre-push hook: it lints the checkout it runs in, so a checkout off the trunk fails
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || { echo "lint: red tree" >&2; exit 1; }
if [ -f "{marker}" ]; then
  while read -r _l lsha _r _s; do
    [ "$lsha" = 0000000000000000000000000000000000000000 ] && { echo "no deletes today" >&2; exit 1; }
  done
fi
exit 0
"""

#: origin's own gate (a host's branch rule): it refuses a delete while the marker stands, and
#: every ref under ``archive/`` while the archive marker does — ``--no-verify`` never skips it.
RECEIVE = """#!/bin/sh
while read -r _old new ref; do
  if [ -f "{marker}" ] && [ "$new" = 0000000000000000000000000000000000000000 ]; then
    echo "no deletes today" >&2; exit 1
  fi
  case "$ref" in refs/heads/archive/*)
    [ -f "{marker}-archive" ] && { echo "claims: bad" >&2; exit 1; } ;;
  esac
done
exit 0
"""


class RefPushes(LaneFixture):
    """The lane's ref-only pushes (archive, branch delete) leave from a clean checkout detached
    at origin/<trunk> — never a product checkout a person left behind or edited — and carry no
    new code, so the product's pre-push hook is skipped (``--no-verify``, 2026-09-26: an archive
    push sat 8+ min in a product's hook). A refusal by origin is loud: a line in the tick's output
    and a row in status and doctor, and the delete is owed and retried."""

    def setUp(self):
        super().setUp()
        self.marker = os.path.join(self.base, 'refuse-delete')
        receive = os.path.join(self.origin, 'hooks', 'pre-receive')
        with open(receive, 'w', encoding='utf-8') as f:
            f.write(RECEIVE.replace('{marker}', self.marker))
        os.chmod(receive, 0o755)
        self.write(self.repo, '.githooks/pre-push', HOOK.replace('{marker}', self.marker))
        os.chmod(os.path.join(self.repo, '.githooks/pre-push'), 0o755)
        sh(['git', 'add', '-A'], cwd=self.repo)
        sh(['git', 'commit', '-qm', 'hook'], cwd=self.repo, env_=self.ident)
        sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)
        sh(['git', 'config', 'core.hooksPath', '.githooks'], cwd=self.repo)
        # the product checkout: a local commit ahead, the trunk moved on, the hook edited by hand
        self.write(self.repo, 'local.txt', 'mine\n')
        sh(['git', 'add', '-A'], cwd=self.repo)
        sh(['git', 'commit', '-qm', 'local work'], cwd=self.repo, env_=self.ident)
        self.push_main({'moved.txt': 'm\n'}, 'trunk moved')
        with open(os.path.join(self.repo, '.githooks/pre-push'), 'a', encoding='utf-8') as f:
            f.write('exit 1\n')
        self.checkout_head = sh(['git', 'rev-parse', 'HEAD'], cwd=self.repo).stdout.strip()
        self.push_lane('worker/free-plan-t1', {'a.txt': 'a\n'}, 'feat: a')
        self.items = {'T-0007': {'id': 'T-0007', 'type': 'task', 'state': 'Closed',
                                 'legacy_id': 'FREE-1/T1', 'parent': 'F-0001'},
                      'F-0001': {'id': 'F-0001', 'type': 'feature', 'state': 'Active',
                                 'legacy_id': 'free-plan', 'children': ['T-0007']}}
        self.prs = {'worker/free-plan-t1': {'number': 70, 'state': 'OPEN', 'title': 'a'}}

    def run_lane(self):
        lines = []
        ln = lane.Lane(self.product(), self.state_dir, out=lines.append, items=self.items)
        ln.host = FakePRHost(ln.product, ln, self.prs)
        lane.lane_pass(ln.product, self.state_dir, items=self.items, lane=ln)
        return ln, lines

    def heads(self):
        return sh(['git', 'for-each-ref', '--format=%(refname:short)', 'refs/heads'],
                  cwd=self.origin).stdout.split()

    def rows(self, product):
        from asf import doctor
        from asf.views import status
        with mock.patch.object(env, 'state_dir', lambda *_a, **_k: self.state_dir):
            return (lane.ref_push_line(product), doctor.check_ref_pushes(product),
                    status.lane_push_cell(product))

    def test_the_pushes_leave_from_a_clean_trunk_checkout_and_the_hook_passes_there(self):
        # the product checkout's own hook refuses everything: the old route failed every push
        refused = sh(['git', 'push', '-q', 'origin', ':refs/heads/worker/free-plan-t1'],
                     cwd=self.repo)
        self.assertNotEqual(refused.returncode, 0)
        ln, lines = self.run_lane()
        self.assertEqual([n for n, _ in ln.host.closed], [70])
        self.assertNotIn('worker/free-plan-t1', self.heads())
        self.assertIn('archive/worker/free-plan-t1', self.heads())
        self.assertFalse([x for x in lines if 'ref push failed' in x], lines)
        self.assertEqual(self.rows(ln.product), (None, None, None))
        # the product checkout is untouched, and the disposable checkout is gone
        self.assertEqual(sh(['git', 'rev-parse', 'HEAD'], cwd=self.repo).stdout.strip(),
                         self.checkout_head)
        self.assertEqual(len(sh(['git', 'worktree', 'list'], cwd=self.repo).stdout.splitlines()), 1)
        self.assertEqual(os.listdir(os.path.join(self.state_dir, lane.REF_PUSH_DIR)), [])

    def test_a_refused_delete_is_loud_owed_and_retried(self):
        open(self.marker, 'w').close()
        ln, lines = self.run_lane()
        self.assertIn('archive/worker/free-plan-t1', self.heads())
        self.assertIn('worker/free-plan-t1', self.heads())
        failed = [x for x in lines if x.startswith('ref push failed: delete worker/free-plan-t1')]
        self.assertTrue(failed and 'no deletes today' in failed[0], lines)
        self.assertTrue([x for x in lines if '1 ref push(es) failed this pass' in x], lines)
        rec_ = self.lane_of('worker/free-plan-t1')
        self.assertEqual((rec_['state'], rec_['delete']), (lane.STALE, lane.DELETE_OWED))
        line, doc, cell = self.rows(ln.product)
        self.assertTrue(line and line.startswith('ref pushes failing: 1'), line)
        self.assertIn('no deletes today', line)
        self.assertEqual(doc, line)
        self.assertEqual(cell, line)
        os.remove(self.marker)  # the hook lets it through: the next pass pays the delete owed
        ln, lines = self.run_lane()
        self.assertNotIn('worker/free-plan-t1', self.heads())
        self.assertIn('deleted worker/free-plan-t1 (a delete an earlier pass could not push)',
                      lines)
        self.assertNotIn('delete', self.lane_of('worker/free-plan-t1'))
        self.assertEqual(self.rows(ln.product), (None, None, None))

    def test_a_product_hook_that_refuses_all_never_holds_an_archive_or_a_delete(self):
        # the trunk's own pre-push hook refuses everything and would take minutes: a ref push
        # carries no new code, so it never runs
        self.push_main({'.githooks/pre-push': '#!/bin/sh\nsleep 30\necho "lint: red" >&2\n'
                                              'exit 1\n'}, 'a hook that refuses all')
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        started = time.monotonic()
        ln, lines = self.run_lane()
        self.assertLess(time.monotonic() - started, 25)
        self.assertEqual([n for n, _ in ln.host.closed], [70])
        self.assertNotIn('worker/free-plan-t1', self.heads())
        self.assertIn('archive/worker/free-plan-t1', self.heads())
        self.assertFalse([x for x in lines if 'ref push failed' in x], lines)

    def test_a_hanging_ref_push_times_out_is_logged_and_the_pass_goes_on(self):
        receive = os.path.join(self.origin, 'hooks', 'pre-receive')
        with open(receive, 'w', encoding='utf-8') as f:
            f.write('#!/bin/sh\nsleep 30\n')
        started = time.monotonic()
        lines = []
        ln = lane.Lane(self.product(git={'push_timeout_s': 1}), self.state_dir,
                       out=lines.append, items=self.items)
        ln.host = FakePRHost(ln.product, ln, self.prs)
        lane.lane_pass(ln.product, self.state_dir, items=self.items, lane=ln)
        self.assertLess(time.monotonic() - started, 25)
        self.assertIn('worker/free-plan-t1', self.heads())
        self.assertNotIn('archive/worker/free-plan-t1', self.heads())
        self.assertTrue([x for x in lines if x.startswith('push timed out after 1s:')
                         and 'archive/worker/free-plan-t1' in x], lines)
        self.assertIn('held worker/free-plan-t1: archive could not be pushed', lines)

    def test_a_refused_archive_holds_the_branch_and_says_why(self):
        open(self.marker + '-archive', 'w').close()
        ln, lines = self.run_lane()
        self.assertEqual(ln.host.closed, [])
        self.assertIn('worker/free-plan-t1', self.heads())
        self.assertNotIn('archive/worker/free-plan-t1', self.heads())
        self.assertIn('held worker/free-plan-t1: archive could not be pushed', lines)
        self.assertTrue([x for x in lines if x.startswith('ref push failed: archive')
                         and 'claims: bad' in x], lines)
        self.assertTrue(self.rows(ln.product)[0])


    def run_deferring(self):
        lines = []
        ln = lane.Lane(self.product(), self.state_dir, out=lines.append, items=self.items)
        ln.host = FakePRHost(ln.product, ln, self.prs)
        lane.lane_pass(ln.product, self.state_dir, items=self.items, lane=ln, defer_pushes=True)
        return ln, lines

    def test_a_deferring_pass_pushes_nothing_until_push_deferred_then_the_same(self):
        # the wave's pass: no ref push (no pre-push hook) before the launches; after them the
        # same archive, record, PR close and delete a pass that pushes at once makes
        before = self.lane_of('worker/free-plan-t1')
        ln, lines = self.run_deferring()
        self.assertEqual([k for k, _f, _w in ln.deferred], ['archive'])
        self.assertIn('worker/free-plan-t1', self.heads())
        self.assertNotIn('archive/worker/free-plan-t1', self.heads())
        self.assertEqual(ln.host.closed, [])
        self.assertEqual(self.lane_of('worker/free-plan-t1'), before)
        self.assertFalse([x for x in lines if x.startswith('lane: push ')], lines)
        lane.push_deferred(ln)
        self.assertEqual(ln.deferred, [])
        self.assertEqual([n for n, _ in ln.host.closed], [70])
        self.assertNotIn('worker/free-plan-t1', self.heads())
        self.assertIn('archive/worker/free-plan-t1', self.heads())
        self.assertEqual(self.lane_of('worker/free-plan-t1')['state'], lane.STALE)
        timed = [x for x in lines if x.startswith('lane: push worker/free-plan-t1 ')]
        self.assertEqual(len(timed), 2, lines)   # the archive, then the delete, each timed
        for x, kind in zip(timed, ('archive', 'delete')):
            self.assertRegex(x, rf'^lane: push worker/free-plan-t1 \d+\.\ds \({kind}\)$')
        self.assertEqual(os.listdir(os.path.join(self.state_dir, lane.REF_PUSH_DIR)), [])

    def test_a_deferred_archive_waits_when_a_session_was_launched_on_its_branch(self):
        ln, lines = self.run_deferring()
        with open(os.path.join(self.state_dir, 'sessions.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps({'job': 'fix-t-0007', 'item': 'T-0007', 'kind': 'coder',
                                'branch': 'worker/free-plan-t1', 'pid': os.getpid(),
                                'started': '2026-09-25T00:00:00Z'}) + '\n')
        lane.push_deferred(ln)
        self.assertIn('worker/free-plan-t1', self.heads())
        self.assertNotIn('archive/worker/free-plan-t1', self.heads())
        self.assertIn('lane: archive of worker/free-plan-t1 waits — a session was launched on '
                      'it this tick', lines)

    def test_a_deferred_delete_after_its_record_is_owed_when_refused(self):
        # a delete queued behind its record keeps its semantics: refused → owed, retried
        open(self.marker, 'w').close()
        ln, lines = self.run_deferring()
        lane.push_deferred(ln)
        rec_ = self.lane_of('worker/free-plan-t1')
        self.assertEqual((rec_['state'], rec_['delete']), (lane.STALE, lane.DELETE_OWED))
        os.remove(self.marker)
        ln, lines = self.run_deferring()
        self.assertEqual([k for k, _f, _w in ln.deferred], ['delete'])
        self.assertIn('worker/free-plan-t1', self.heads())
        lane.push_deferred(ln)
        self.assertNotIn('worker/free-plan-t1', self.heads())
        self.assertNotIn('delete', self.lane_of('worker/free-plan-t1'))


class UnparkResetsLoopGuard(unittest.TestCase):
    """A product's T-0338, 2026-09-26 17:28: ``asf unpark`` cleared a copies park and the very
    next tick parked it again — the loop guard recounted the three adjudicate launches from
    before the unpark, and a copies hold routes to a correct session, never adjudicate. The
    guard counts only launches after the item's latest unpark, of the kind the hold routes to."""

    H = 'e6f42c14b' + 'e' * 31
    B = 'cloud/T-0338'

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.path = os.path.join(self.d, 'sessions.jsonl')

    def write(self, *lines):
        with open(self.path, 'a', encoding='utf-8') as f:
            for ln in lines:
                f.write(json.dumps(ln) + '\n')

    def launch(self, n, kind, minute):
        job = f'{kind}-t-0338'
        self.write({'job': job, 'item': 'T-0338', 'branch': self.B, 'kind': kind, 'pid': n,
                    'started': f'2026-09-26T{minute}:00Z', 'launch_head': self.H},
                   {'job': job, 'ended': f'2026-09-26T{minute}:30Z',
                    'end_reason': 'failed: not pushed: 0 uncommitted file(s), 1 unpushed commit(s)'})
        return lifecycle.latest(self.path)[job]

    def copies_hold(self, run):
        fields, line = lifecycle.hold(self.path, run, lifecycle.COPIES,
                                      'trunk history under the branch', '2026-09-26T18:00:00Z',
                                      head=self.H)
        self.write(dict(fields, job=run['job']))
        return fields['correction'], line

    def parked_then_unparked(self):
        """Three adjudicate launches on one head, parked, then ``asf unpark`` at 17:28."""
        for n, minute in enumerate(('15:00', '16:00', '17:00'), 1):
            run = self.launch(n, 'adjudicate', minute)
        self.write({'job': run['job'], 'correction': {'kind': lifecycle.COPIES, 'text': 'x',
                    'at': '2026-09-26T17:10:00Z', 'parked': True, 'reason': 'loop'},
                    'operator_flagged': 1})
        self.write({'job': run['job'], 'correction': None, 'unparked': '2026-09-26T17:28:00Z',
                    'unpark_why': 'fix'})
        return lifecycle.latest(self.path)[run['job']]

    def test_unpark_then_the_same_head_is_not_reparked_and_a_correct_row_follows(self):
        run = self.parked_then_unparked()
        corr, line = self.copies_hold(run)
        self.assertNotIn('parked', corr, line)
        self.assertTrue(line.endswith('(copies, no round)'), line)
        items = {'T-0338': {'id': 'T-0338', 'type': 'task', 'state': 'Active'}}
        product = env.Product('p', {'conventions': {}})
        rows, _ = feeder_rows.correction_rows(items, product, set(),
                                              lifecycle.corrections(self.path))
        self.assertEqual([(r.item_id, r.kind) for r in rows],
                         [('T-0338', feeder_rows.FIX_CORRECT)])
        self.assertTrue(rows[0].launches)

    def test_adjudicate_launches_never_park_a_copies_hold(self):
        for n, minute in enumerate(('15:00', '16:00', '17:00'), 1):
            run = self.launch(n, 'adjudicate', minute)
        corr, line = self.copies_hold(run)
        self.assertNotIn('parked', corr, line)

    def test_three_correct_launches_after_the_unpark_on_an_unmoved_head_park_again(self):
        self.parked_then_unparked()
        for n, minute in enumerate(('17:40', '17:50', '17:55'), 4):
            run = self.launch(n, 'correct', minute)
        corr, line = self.copies_hold(run)
        self.assertIs(corr['parked'], True)
        self.assertIn('correct launched 3 times on e6f42c14b', corr['reason'])
        self.assertTrue(line.startswith(f'parked {self.B}: '), line)

    def test_launches_before_the_unpark_never_count_for_any_hold(self):
        run = self.parked_then_unparked()
        fields, _ = lifecycle.hold(self.path, run, 'review', 'x', '2026-09-26T18:00:00Z',
                                   head=self.H)
        self.assertNotIn('parked', fields['correction'])


class NamingRepair(LaneFixture):
    """A branch whose subjects do not name its item is reworded by the lane itself — no session,
    no round, never a STALEMATE → ADJUDICATE row; a reword it cannot push goes back to the
    writer's session as a correction that spends no round."""

    B = 'worker/T-0001'
    AUTHOR = {'GIT_AUTHOR_NAME': 'Ada', 'GIT_AUTHOR_EMAIL': 'ada@x',
              'GIT_AUTHOR_DATE': '1700000000 +0200', 'GIT_COMMITTER_NAME': 'Cy',
              'GIT_COMMITTER_EMAIL': 'cy@x', 'GIT_COMMITTER_DATE': '1700000100 +0200'}

    def push_commits(self, commits):
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'checkout', '-q', '-B', self.B, 'origin/main'], cwd=self.worker)
        for subject, files in commits:
            for rel, text in files.items():
                self.write(self.worker, rel, text)
            sh(['git', 'add', '-A'], cwd=self.worker)
            sh(['git', 'commit', '-qm', subject + '\n\nthe body stays'], cwd=self.worker,
               env_=self.AUTHOR)
        sh(['git', 'push', '-q', '-f', 'origin', self.B], cwd=self.worker)
        return self.tip()

    def tip(self):
        return sh(['git', 'rev-parse', self.B], cwd=self.origin).stdout.strip()

    def log(self, fmt, rev):
        return sh(['git', 'log', f'--format={fmt}', f'main..{rev}'], cwd=self.origin).stdout

    def sessions(self):
        return os.path.join(self.state_dir, 'sessions.jsonl')

    def test_the_lane_rewords_the_subjects_and_pushes_them_with_no_session(self):
        old = self.push_commits([('feat(T-0001): the door', {'a.txt': 'a\n'}),
                                 ('tidy up', {'b.txt': 'b\n'}),
                                 ('fix: the hinge', {'c.txt': 'c\n'})])
        self.session('coder-t-0001', 'T-0001', self.B)
        lines = []
        lane.lane_pass(self.product(), self.state_dir, out=lines.append)
        self.assertIn(f'reword {self.B}: 2 subjects, trees identical — pushed', lines)
        new = self.tip()
        self.assertNotEqual(new, old)
        self.assertEqual(self.log('%s', new).splitlines(),
                         ['fix(T-0001): the hinge', 'task(T-0001): tidy up',
                          'feat(T-0001): the door'])
        # trees, authors, committers and dates identical; the body untouched
        self.assertEqual(self.log('%T %an %ae %ad %cn %ce %cd', new),
                         self.log('%T %an %ae %ad %cn %ce %cd', old))
        self.assertEqual(self.log('%b', new), self.log('%b', old))
        rec_ = self.lane_of(self.B)
        self.assertEqual((rec_['state'], rec_['head']), (lane.GATE, new))
        self.assertEqual(lifecycle.corrections(self.sessions()), {})
        self.assertFalse(any(l.startswith('held ') for l in lines), lines)

    def test_the_push_goes_over_a_lease_on_the_old_tip(self):
        self.push_commits([('tidy up', {'a.txt': 'a\n'})])
        self.session('coder-t-0001', 'T-0001', self.B)
        real = lane.reword_branch

        def racing(*a, **kw):  # the writer pushes again between the read and the reword push
            out = real(*a, **kw)
            sh(['git', 'checkout', '-q', self.B], cwd=self.worker)
            self.write(self.worker, 'z.txt', 'z\n')
            sh(['git', 'add', '-A'], cwd=self.worker)
            sh(['git', 'commit', '-qm', 'feat(T-0001): later'], cwd=self.worker, env_=self.ident)
            sh(['git', 'push', '-q', 'origin', self.B], cwd=self.worker)
            return out
        lines = []
        with mock.patch.object(lane, 'reword_branch', side_effect=racing):
            lane.lane_pass(self.product(), self.state_dir, out=lines.append)
        # the lease refused the push over the writer's commit; the lane read it again and
        # reworded the new tip — the writer's commit kept, no session
        self.assertEqual(self.log('%s', self.B).splitlines(),
                         ['feat(T-0001): later', 'task(T-0001): tidy up'])
        self.assertTrue(any(l.startswith(f'reword {self.B}: the branch moved') for l in lines),
                        lines)
        self.assertFalse(any(l.startswith('held ') for l in lines), lines)
        self.assertEqual(lifecycle.corrections(self.sessions()), {})

    def test_a_branch_already_held_for_naming_is_reworded_and_its_correction_cleared(self):
        old = self.push_commits([('tidy up', {'a.txt': 'a\n'})])
        self.session('coder-t-0001', 'T-0001', self.B)
        with open(self.sessions(), 'a', encoding='utf-8') as f:
            f.write(json.dumps({'job': 'coder-t-0001', 'rounds': 3,
                                'lane': {'state': lane.BACK, 'head': old, 'pr': None,
                                         'at': '2026-09-21T00:06:00Z', 'reason': 'kind=naming',
                                         'item': 'T-0001'},
                                'correction': {'kind': 'naming', 'text': 'commits do not name',
                                               'at': '2026-09-21T00:06:00Z'}}) + '\n')
        self.assertIn('T-0001', lifecycle.corrections(self.sessions()))
        lines = []
        lane.lane_pass(self.product(), self.state_dir, out=lines.append)
        self.assertIn(f'reword {self.B}: 1 subjects, trees identical — pushed', lines)
        self.assertEqual(lifecycle.corrections(self.sessions()), {})
        self.assertEqual(self.lane_of(self.B)['state'], lane.GATE)

    def test_naming_never_reaches_adjudicate(self):
        product = env.Product('p', {'conventions': {}})
        items = {'T-0001': {'id': 'T-0001', 'type': 'task', 'state': 'Active'}}
        c = {'kind': lifecycle.NAMING, 'text': 'commits do not name T-0001', 'rounds': 7,
             'branch': self.B}
        rows, _ids = feeder_rows.correction_rows(items, product, set(), {'T-0001': c})
        self.assertEqual([r.kind for r in rows], [feeder_rows.FIX_CORRECT])
        self.assertEqual(feeder_rows.NAMING, lifecycle.NAMING)
        # the hold spends no round and is never at the cap, whatever the item's rounds
        run = {'job': 'coder-t-0001', 'item': 'T-0001', 'branch': self.B, 'rounds': 9}
        fields, line = lifecycle.hold(self.sessions(), run, lifecycle.NAMING, 'x', 'now')
        self.assertNotIn('rounds', fields)
        self.assertNotIn('at_cap', fields['correction'])
        self.assertTrue(line.endswith('(naming, no round)'), line)
        state = lifecycle.derive(dict(run, correction=fields['correction']),
                                 lifecycle.Evidence())
        self.assertEqual(state.name, lifecycle.HELD)

    def test_a_copies_conflict_never_reaches_adjudicate(self):
        # a product's T-0338 at its round cap: the rebuild's conflict went to an adjudicate
        # session, which rules on disputes and rebases nothing — then was held again
        product = env.Product('p', {'conventions': {}})
        items = {'T-0001': {'id': 'T-0001', 'type': 'task', 'state': 'Active'}}
        c = {'kind': lane.COPIES, 'text': 'trunk history under the branch', 'rounds': 7,
             'branch': self.B}
        rows, _ids = feeder_rows.correction_rows(items, product, set(), {'T-0001': c})
        self.assertEqual([r.kind for r in rows], [feeder_rows.FIX_CORRECT])
        self.assertEqual((feeder_rows.COPIES, lane.COPIES), (lifecycle.COPIES,) * 2)
        run = {'job': 'coder-t-0001', 'item': 'T-0001', 'branch': self.B, 'rounds': 9}
        fields, line = lifecycle.hold(self.sessions(), run, lifecycle.COPIES, 'x', 'now')
        self.assertNotIn('rounds', fields)
        self.assertNotIn('at_cap', fields['correction'])
        self.assertTrue(line.endswith('(copies, no round)'), line)
        state = lifecycle.derive(dict(run, correction=fields['correction']),
                                 lifecycle.Evidence())
        self.assertEqual(state.name, lifecycle.HELD)


class SignoffRepair(LaneFixture):
    """A PR whose DCO check is red: the lane signs each unsigned commit of a factory branch off
    itself (trees unchanged, pushed over a lease) — and never rewrites a foreign branch."""

    B, AUTHOR = NamingRepair.B, NamingRepair.AUTHOR
    push_commits, tip, log = NamingRepair.push_commits, NamingRepair.tip, NamingRepair.log

    def runner(self):
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        return lane.Lane(self.product(), self.state_dir, out=self.lines.append)

    def setUp(self):
        super().setUp()
        self.lines = []

    def test_the_lane_signs_off_a_factory_branch(self):
        old = self.push_commits([('feat(T-0001): the door', {'a.txt': 'a\n'}),
                                 ('feat(T-0001): the hinge\n\nSigned-off-by: Ada <ada@x>',
                                  {'b.txt': 'b\n'})])
        self.session('coder-t-0001', 'T-0001', self.B)
        ln = self.runner()
        f = {'branch': self.B, 'kind': 'code', 'item': 'T-0001', 'head': old,
             'prev': rec(lane.GATE, head=old, pr=7), 'run': {'job': 'coder-t-0001'}}
        got = ln.repair_signoff(f, 'DCO sign-off')
        self.assertIsNotNone(got)
        self.assertIn(f'signed off 1 commits on {self.B} (DCO sign-off) — no session', self.lines)
        new = self.tip()
        self.assertNotEqual(new, old)
        self.assertEqual((got['state'], got['head']), (lane.PUSHED, new))
        bodies = self.log('%B%x00', new).split('\x00')[:2]
        self.assertEqual([b.count('Signed-off-by: Ada <ada@x>') for b in bodies], [1, 1], bodies)
        self.assertTrue(bodies[1].rstrip().endswith('Signed-off-by: Ada <ada@x>'), bodies)
        self.assertEqual(self.log('%T %an %ae %ad %cn %ce %cd %s', new),
                         self.log('%T %an %ae %ad %cn %ce %cd %s', old))
        # signed already: nothing to do, nothing pushed
        f['head'] = new
        self.assertIsNone(ln.repair_signoff(f, 'DCO'))
        self.assertEqual(self.tip(), new)

    def test_a_foreign_branch_is_never_rewritten(self):
        old = self.push_commits([('feat(T-0001): the door', {'a.txt': 'a\n'})])
        ln = self.runner()
        f = {'branch': self.B, 'kind': None, 'item': 'T-0001', 'head': old,
             'prev': rec(lane.GATE, head=old, pr=7), 'run': None}
        self.assertIsNone(ln.repair_signoff(f, 'DCO'))
        self.assertEqual(self.tip(), old)
        self.assertTrue(any('under no factory prefix' in l for l in self.lines), self.lines)

    def test_a_red_dco_check_triggers_the_repair_and_no_hold(self):
        product = env.Product('p', {'repo_slug': 'o/p', 'conventions': {
            'landing': 'pull-request', 'landing_checks': ['DCO sign-off', 'ci']}})
        runner = lane.Lane.__new__(lane.Lane)
        runner.product, runner.conv, runner.out, runner.dry_run = product, product.conventions, \
            self.lines.append, False
        runner.results, runner.now, runner.state_dir = {}, NOW, self.state_dir
        runner.repo = None
        host = lane.GitHubHost(product, None)
        host.lane = runner
        f = {'branch': 'worker/T-0001', 'prev': rec(lane.GATE, pr=7), 'class': lane.CODE,
             'head': HEAD, 'kind': 'code'}
        checks = [{'name': 'ci', 'bucket': 'pass'}, {'name': 'DCO sign-off', 'bucket': 'fail'}]
        with mock.patch.object(harvest, '_gh', return_value=(0, json.dumps(checks), '')), \
                mock.patch.object(lane.Lane, 'repair_signoff', return_value={'state': 'x'}) as rs, \
                mock.patch.object(lane, 'send_back') as sb:
            self.assertIsNone(host.check_gate(f, 7, ['src/a.py']))
        rs.assert_called_once_with(f, 'DCO sign-off')
        sb.assert_not_called()
        # a product naming its own check: only that name counts
        product = env.Product('p', {'repo_slug': 'o/p', 'conventions': {
            'commit': {'signoff_check': 'signed'}}})
        self.assertFalse(product.conventions.is_signoff_check('DCO'))
        self.assertTrue(product.conventions.is_signoff_check('commits-signed'))


class NamingRewordNeverCostsASession(LaneFixture):
    """2026-09-27: a product's tick log carried 437 ``reword <b> (naming) push refused`` lines in a
    day, every one the product's own pre-push hook (its redaction gate re-scans each rewritten
    commit's diff: new shas, the same content origin already has) — and each sent the branch back
    to a session. A message-only rewrite (same tree per commit, checked before the push) goes out
    with ``--no-verify`` over a lease on the tip the lane read; a lease race is re-read and
    retried once; a live session's branch waits; only a rewrite that cannot be done holds."""

    B, AUTHOR = NamingRepair.B, NamingRepair.AUTHOR
    tip, push_commits, log, sessions = (NamingRepair.tip, NamingRepair.push_commits,
                                        NamingRepair.log, NamingRepair.sessions)

    def setUp(self):
        super().setUp()
        self.lines = []

    def naming(self, old, **kw):
        f = {'branch': self.B, 'kind': 'code', 'item': 'T-0001', 'head': old,
             'refusal': (lifecycle.NAMING, 'commits do not name T-0001'),
             'prev': rec(lane.GATE, head=old), 'run': {'job': 'coder-t-0001'}}
        f.update(kw)
        return f

    def runner(self):
        return lane.Lane(self.product(), self.state_dir, out=self.lines.append)

    def refusing_hook(self):
        """The product's pre-push hook refuses everything — as a product's redaction gate refused
        every rewritten commit whose content it had already let through."""
        hook = os.path.join(self.repo, '.git', 'hooks', 'pre-push')
        with open(hook, 'w', encoding='utf-8') as fh:
            fh.write('#!/bin/sh\necho "redact: refused — 2 finding(s)"\nexit 1\n')
        os.chmod(hook, 0o755)

    def writer_pushes(self, subject, rel):
        sh(['git', 'checkout', '-q', self.B], cwd=self.worker)
        self.write(self.worker, rel, rel + '\n')
        sh(['git', 'add', '-A'], cwd=self.worker)
        sh(['git', 'commit', '-qm', subject], cwd=self.worker, env_=self.ident)
        sh(['git', 'push', '-q', 'origin', self.B], cwd=self.worker)

    def test_a_refusing_pre_push_hook_does_not_stop_a_message_only_reword(self):
        self.refusing_hook()
        old = self.push_commits([('tidy up', {'a.txt': 'a\n'}), ('fix: x', {'b.txt': 'b\n'})])
        self.session('coder-t-0001', 'T-0001', self.B)
        lane.lane_pass(self.product(), self.state_dir, out=self.lines.append)
        self.assertIn(f'reword {self.B}: 2 subjects, trees identical — pushed', self.lines)
        new = self.tip()
        self.assertNotEqual(new, old)
        self.assertEqual(self.log('%s', new).splitlines(),
                         ['fix(T-0001): x', 'task(T-0001): tidy up'])
        self.assertEqual(self.log('%T', new), self.log('%T', old))
        self.assertFalse(any(l.startswith('held ') for l in self.lines), self.lines)
        self.assertEqual(lifecycle.corrections(self.sessions()), {})

    def test_the_hook_is_skipped_only_when_every_tree_is_identical(self):
        self.refusing_hook()
        old = self.push_commits([('tidy up', {'a.txt': 'a\n'})])
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        # a "reword" that changed content too: another tree under the rewritten commit
        other = sh(['git', 'commit-tree', sh(['git', 'rev-parse', 'origin/main^{tree}'],
                                              cwd=self.repo).stdout.strip(),
                    '-p', 'origin/main', '-m', 'task(T-0001): tidy up'],
                   cwd=self.repo, env_=self.ident).stdout.strip()
        f = self.naming(old)
        with mock.patch.object(lane, 'reword_branch', return_value=(other, 1)):
            self.assertIsNone(self.runner().repair_naming(f))
        self.assertEqual(self.tip(), old)
        line = [l for l in self.lines if l.startswith(f'reword {self.B}:')]
        self.assertTrue(line and 'trees differ' in line[0] and 'nothing pushed' in line[0],
                        self.lines)
        self.assertIn('The lane could not because:', f['refusal'][1])
        self.assertIn('trees differ', f['refusal'][1])

    def test_the_lease_is_on_the_tip_the_lane_read(self):
        old = self.push_commits([('tidy up', {'a.txt': 'a\n'})])
        self.writer_pushes('feat(T-0001): later', 'z.txt')  # the facts' head is behind origin
        f = self.naming(old)
        self.assertIsNotNone(self.runner().repair_naming(f))
        self.assertEqual(self.log('%s', self.tip()).splitlines(),
                         ['feat(T-0001): later', 'task(T-0001): tidy up'])
        self.assertEqual(f['head'], self.tip())
        self.assertIn(f'reword {self.B}: 1 subjects, trees identical — pushed', self.lines)

    def test_a_lease_race_is_re_read_and_retried_once(self):
        self.push_commits([('tidy up', {'a.txt': 'a\n'})])
        self.session('coder-t-0001', 'T-0001', self.B)
        real, calls = lane.reword_branch, []

        def racing(*a, **kw):  # the writer pushes between the lane's read and its first push
            out = real(*a, **kw)
            calls.append(out)
            if len(calls) == 1:
                self.writer_pushes('feat(T-0001): later', 'z.txt')
            return out
        with mock.patch.object(lane, 'reword_branch', side_effect=racing):
            lane.lane_pass(self.product(), self.state_dir, out=self.lines.append)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.log('%s', self.B).splitlines(),
                         ['feat(T-0001): later', 'task(T-0001): tidy up'])
        self.assertTrue(any(l.startswith(f'reword {self.B}: the branch moved') and 'retried' in l
                            for l in self.lines), self.lines)
        self.assertIn(f'reword {self.B}: 1 subjects, trees identical — pushed', self.lines)
        self.assertFalse(any(l.startswith('held ') for l in self.lines), self.lines)
        self.assertEqual(lifecycle.corrections(self.sessions()), {})

    def test_a_second_lease_race_defers_with_no_session(self):
        self.push_commits([('tidy up', {'a.txt': 'a\n'})])
        self.session('coder-t-0001', 'T-0001', self.B)
        real, calls = lane.reword_branch, []

        def racing(*a, **kw):  # the writer pushes after every read
            out = real(*a, **kw)
            calls.append(out)
            self.writer_pushes(f'feat(T-0001): later {len(calls)}', f'z{len(calls)}.txt')
            return out
        with mock.patch.object(lane, 'reword_branch', side_effect=racing):
            lane.lane_pass(self.product(), self.state_dir, out=self.lines.append)
        self.assertEqual(len(calls), 2)  # one retry, then the next pass
        self.assertTrue(any(l.startswith(f'reword {self.B}: deferred') and 'moved' in l
                            for l in self.lines), self.lines)
        self.assertFalse(any(l.startswith('held ') for l in self.lines), self.lines)
        self.assertEqual(lifecycle.corrections(self.sessions()), {})
        self.assertNotEqual(self.lane_of(self.B).get('reason'), 'kind=naming')

    def test_a_live_session_holding_the_branch_defers_the_reword(self):
        old = self.push_commits([('tidy up', {'a.txt': 'a\n'})])
        f = self.naming(old, live=True)
        with mock.patch.object(lane, 'reword_branch') as rw:
            self.assertEqual(self.runner().repair_naming(f), lane.DEFERRED)
        rw.assert_not_called()
        self.assertEqual(self.tip(), old)
        self.assertTrue(any(l.startswith(f'reword {self.B}: deferred — a live session holds the '
                                         f'branch') for l in self.lines), self.lines)
        self.assertEqual(lifecycle.corrections(self.sessions()), {})

    def test_a_deferred_reword_never_holds_in_the_pass(self):
        old = self.push_commits([('tidy up', {'a.txt': 'a\n'})])
        self.session('coder-t-0001', 'T-0001', self.B)
        with mock.patch.object(lane.Lane, 'repair_naming', return_value=lane.DEFERRED) as rp:
            lane.lane_pass(self.product(), self.state_dir, out=self.lines.append)
        self.assertEqual(rp.call_count, 1)  # one attempt a pass, never two
        self.assertFalse(any(l.startswith('held ') for l in self.lines), self.lines)
        self.assertEqual(lifecycle.corrections(self.sessions()), {})
        self.assertEqual(self.tip(), old)

    def test_a_remote_refusal_defers_with_its_reason(self):
        hook = os.path.join(self.origin, 'hooks', 'pre-receive')
        old = self.push_commits([('tidy up', {'a.txt': 'a\n'})])
        with open(hook, 'w', encoding='utf-8') as fh:
            fh.write('#!/bin/sh\necho "protected branch hook declined" >&2\nexit 1\n')
        os.chmod(hook, 0o755)
        f = self.naming(old)
        self.assertEqual(self.runner().repair_naming(f), lane.DEFERRED)
        self.assertEqual(self.tip(), old)
        line = [l for l in self.lines if l.startswith(f'reword {self.B}: deferred')]
        self.assertTrue(line and 'protected branch hook declined' in line[0], self.lines)

    def test_a_branch_under_no_factory_prefix_still_goes_back_to_its_session(self):
        old = self.push_commits([('tidy up', {'a.txt': 'a\n'})])
        f = self.naming(old, kind=None)
        self.assertIsNone(self.runner().repair_naming(f))
        self.assertEqual(self.tip(), old)
        self.assertTrue(any('under no factory prefix' in l for l in self.lines), self.lines)


class TrunkCommitsNeverReworded(LaneFixture):
    """2026-09-26: the naming repair reworded trunk commits sitting under a factory branch (the
    branch rebased onto a trunk its tracking ref did not know yet) into ``task(<item>): …`` copies
    and pushed them. A repair rewrites only the branch's own commits — never one reachable from
    the trunk, never a merge, never a copy of a trunk commit — and a rebuilt branch that is not
    the old one's commits and diff is never pushed."""

    B, AUTHOR = NamingRepair.B, NamingRepair.AUTHOR
    tip = NamingRepair.tip

    def setUp(self):
        super().setUp()
        self.lines = []

    def commit(self, subject, files):
        for rel, text in files.items():
            self.write(self.worker, rel, text)
        sh(['git', 'add', '-A'], cwd=self.worker)
        sh(['git', 'commit', '-qm', subject], cwd=self.worker, env_=self.AUTHOR)

    def naming(self, old):
        return {'branch': self.B, 'kind': 'code', 'item': 'T-0001', 'head': old,
                'refusal': (lifecycle.NAMING, 'commits do not name T-0001'),
                'prev': rec(lane.GATE, head=old), 'run': {'job': 'coder-t-0001'}}

    def test_a_branch_rebased_onto_a_trunk_the_tracking_ref_does_not_know(self):
        # the product checkout's plain fetch never updates its origin/main: it stays at init
        sh(['git', 'config', 'remote.origin.fetch',
            '+refs/heads/worker/*:refs/remotes/origin/worker/*'], cwd=self.repo)
        self.push_main({'x.txt': 'x\n'}, 'fix(ci): home-clock (#812)')
        self.push_main({'y.txt': 'y\n'}, 'T-0341 — the thing (#814)')
        trunk = self.origin_main()
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'checkout', '-q', '-B', self.B, 'origin/main'], cwd=self.worker)
        self.commit('tidy up', {'a.txt': 'a\n'})
        sh(['git', 'push', '-q', '-f', 'origin', self.B], cwd=self.worker)
        old = self.tip()
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        stale = sh(['git', 'rev-parse', 'origin/main'], cwd=self.repo).stdout.strip()
        self.assertNotEqual(stale, trunk)  # the bug's precondition
        ln = lane.Lane(self.product(), self.state_dir, out=self.lines.append)
        self.assertIsNotNone(ln.repair_naming(self.naming(old)))
        self.assertIn(f'reword {self.B}: 1 subjects, trees identical — pushed', self.lines)
        new = self.tip()
        self.assertEqual(sh(['git', 'log', '--format=%s', f'main..{new}'],
                            cwd=self.origin).stdout.splitlines(), ['task(T-0001): tidy up'])
        self.assertEqual(sh(['git', 'merge-base', '--is-ancestor', trunk, new],
                            cwd=self.origin).returncode, 0)
        subjects = sh(['git', 'log', '--format=%s', new], cwd=self.origin).stdout.splitlines()
        self.assertFalse([s for s in subjects if s.startswith('task(') and '(#81' in s])
        self.assertEqual(self.origin_main(), trunk)

    def test_a_branch_with_the_trunk_merged_in_is_not_reworded(self):
        sh(['git', 'checkout', '-q', '-B', self.B, 'origin/main'], cwd=self.worker)
        self.commit('tidy up', {'a.txt': 'a\n'})
        self.push_main({'x.txt': 'x\n'}, 'fix(ci): home-clock (#812)')
        sh(['git', 'checkout', '-q', self.B], cwd=self.worker)
        sh(['git', 'merge', '-q', '--no-edit', 'origin/main'], cwd=self.worker, env_=self.AUTHOR)
        sh(['git', 'push', '-q', '-f', 'origin', self.B], cwd=self.worker)
        old = self.tip()
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        why = []
        self.assertEqual(lane.reword_branch(self.repo, 'main', self.B, 'T-0001', 'task', why),
                         (None, 0))
        self.assertIn('merge commit', why[0])
        ln = lane.Lane(self.product(), self.state_dir, out=self.lines.append)
        self.assertIsNone(ln.repair_naming(self.naming(old)))
        self.assertEqual(self.tip(), old)
        self.assertTrue(any('merge commit' in l and 'back to its session' in l
                            for l in self.lines), self.lines)

    def test_a_copy_of_a_trunk_commit_under_the_branch_is_not_reworded(self):
        sh(['git', 'checkout', '-q', '-B', self.B, 'origin/main'], cwd=self.worker)
        self.commit('fix(ci): home-clock', {'x.txt': 'x\n'})
        self.commit('tidy up', {'a.txt': 'a\n'})
        sh(['git', 'push', '-q', '-f', 'origin', self.B], cwd=self.worker)
        self.push_main({'x.txt': 'x\n'}, 'fix(ci): home-clock (#812)')
        old = self.tip()
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        ln = lane.Lane(self.product(), self.state_dir, out=self.lines.append)
        f = self.naming(old)
        self.assertIsNone(ln.repair_naming(f))
        self.assertEqual(self.tip(), old)
        self.assertTrue(any('copies of origin/main commits' in l and 'back to its session' in l
                            for l in self.lines), self.lines)
        # a product's T-0338: the session is told the cause, not only "reword them"
        self.assertEqual(f['refusal'][0], lifecycle.NAMING)
        self.assertIn('The lane could not because: 1 commit(s) on it are copies of origin/main',
                      f['refusal'][1])
        self.assertIn('rebase onto origin/main', f['refusal'][1])

    def test_a_rebuilt_branch_that_fails_the_guard_is_never_pushed(self):
        sh(['git', 'checkout', '-q', '-B', self.B, 'origin/main'], cwd=self.worker)
        self.commit('tidy up', {'a.txt': 'a\n'})
        sh(['git', 'push', '-q', '-f', 'origin', self.B], cwd=self.worker)
        old = self.tip()
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        diffs = iter(['the old diff', 'another diff'])
        ln = lane.Lane(self.product(), self.state_dir, out=self.lines.append)
        with mock.patch.object(lane, '_diff', side_effect=lambda *a: next(diffs)):
            self.assertIsNone(ln.repair_naming(self.naming(old)))
        self.assertEqual(self.tip(), old)
        self.assertTrue(any('rewrite guard' in l for l in self.lines), self.lines)


class TrunkCopiesDropped(LaneFixture):
    """2026-09-26: lane branches carried copies of trunk commits (a product's T-0338: 22 of 31)
    and looped — "rebase onto origin/main" went back to sessions whose briefs forbid a force.
    The lane rebuilds a factory branch as the trunk plus its own commits itself: the old tip
    archived, a conflict held back with its files and nothing pushed, never while a live session
    holds the branch, never the trunk or a protected ref."""

    B, AUTHOR = NamingRepair.B, NamingRepair.AUTHOR
    tip = NamingRepair.tip
    commit = TrunkCommitsNeverReworded.commit

    def setUp(self):
        super().setUp()
        self.lines = []

    def sessions(self):
        return os.path.join(self.state_dir, 'sessions.jsonl')

    def own(self, rev):
        return sh(['git', 'log', '--format=%s', f'main..{rev}'], cwd=self.origin).stdout.split('\n')[:-1]

    def copied_branch(self, own_files=None):
        """A branch holding a copy of trunk commit ``x.txt`` (the trunk's own, landed later
        under another sha) under two of its own commits."""
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'checkout', '-q', '-B', self.B, 'origin/main'], cwd=self.worker)
        self.commit('feat(T-0001): the door', {'a.txt': 'a\n'})
        self.commit('fix(ci): home-clock', {'x.txt': 'x\n'})
        self.commit('fix(T-0001): the hinge', own_files or {'b.txt': 'b\n'})
        sh(['git', 'push', '-q', '-f', 'origin', self.B], cwd=self.worker)
        self.push_main({'x.txt': 'x\n'}, 'fix(ci): home-clock (#812)')
        return self.tip()

    def archive(self, old):
        return sh(['git', 'rev-parse', '--verify', '-q',
                   f'refs/heads/archive/{self.B}-copies-{old[:9]}'],
                  cwd=self.origin).stdout.strip()

    def test_copies_are_dropped_and_own_commits_kept_with_the_old_tip_archived(self):
        old = self.copied_branch()
        self.session('coder-t-0001', 'T-0001', self.B)
        trunk = self.origin_main()
        lane.lane_pass(self.product(), self.state_dir, out=self.lines.append)
        new = self.tip()
        self.assertNotEqual(new, old)
        self.assertEqual(sh(['git', 'rev-parse', f'{new}~2'], cwd=self.origin).stdout.strip(),
                         trunk)
        self.assertEqual(self.own(new), ['fix(T-0001): the hinge', 'feat(T-0001): the door'])
        # authors kept, the change the same
        self.assertEqual(sh(['git', 'log', '--format=%an %ae %ad', f'main..{new}'],
                            cwd=self.origin).stdout.splitlines(), ['Ada ada@x ' + sh(
                                ['git', 'log', '-1', '--format=%ad', old],
                                cwd=self.origin).stdout.strip()] * 2)
        self.assertEqual(sh(['git', 'diff', f'main', new, '--stat'], cwd=self.origin).stdout,
                         sh(['git', 'diff', 'main', old, '--stat'], cwd=self.origin).stdout)
        self.assertEqual(self.archive(old), old)
        done = [l for l in self.lines if l.startswith(f'dropped 1 trunk copies from {self.B}')]
        self.assertEqual(len(done), 1, self.lines)
        self.assertIn(f'{old[:9]} → {new[:9]}, 2 own commit(s)', done[0])
        rec_ = self.lane_of(self.B)
        self.assertEqual((rec_['state'], rec_['head']), (lane.GATE, new))
        self.assertEqual(lifecycle.corrections(self.sessions()), {})
        self.assertFalse(any('back to its session' in l for l in self.lines), self.lines)
        self.assertEqual(self.origin_main(), trunk)

    def test_a_naming_hold_caused_by_copies_is_cleared_not_sent_back(self):
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'checkout', '-q', '-B', self.B, 'origin/main'], cwd=self.worker)
        self.commit('fix(ci): home-clock', {'x.txt': 'x\n'})
        self.commit('tidy up', {'a.txt': 'a\n'})
        sh(['git', 'push', '-q', '-f', 'origin', self.B], cwd=self.worker)
        self.push_main({'x.txt': 'x\n'}, 'fix(ci): home-clock (#812)')
        self.session('coder-t-0001', 'T-0001', self.B)
        lane.lane_pass(self.product(), self.state_dir, out=self.lines.append)
        self.assertEqual(self.own(self.tip()), ['task(T-0001): tidy up'])
        self.assertEqual(lifecycle.corrections(self.sessions()), {})
        self.assertFalse(any('back to its session' in l for l in self.lines), self.lines)

    def test_a_merge_beside_the_copies_is_dropped_too(self):
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'checkout', '-q', '-B', self.B, 'origin/main'], cwd=self.worker)
        self.commit('fix(ci): home-clock', {'x.txt': 'x\n'})
        self.commit('feat(T-0001): the door', {'a.txt': 'a\n'})
        self.push_main({'y.txt': 'y\n'}, 'fix: y (#811)')
        sh(['git', 'checkout', '-q', self.B], cwd=self.worker)
        sh(['git', 'merge', '-q', '--no-edit', 'origin/main'], cwd=self.worker, env_=self.AUTHOR)
        sh(['git', 'push', '-q', '-f', 'origin', self.B], cwd=self.worker)
        self.push_main({'x.txt': 'x\n'}, 'fix(ci): home-clock (#812)')  # the copy's original
        self.session('coder-t-0001', 'T-0001', self.B)
        lane.lane_pass(self.product(), self.state_dir, out=self.lines.append)
        new = self.tip()
        self.assertEqual(self.own(new), ['feat(T-0001): the door'])
        self.assertEqual(sh(['git', 'rev-list', '--merges', f'main..{new}'],
                            cwd=self.origin).stdout, '')
        self.assertEqual(self.lane_of(self.B)['state'], lane.GATE)

    def test_a_conflict_pushes_nothing_and_holds_with_the_files(self):
        self.push_main({'c.txt': 'base\n'}, 'chore: c')
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'checkout', '-q', '-B', self.B, 'origin/main'], cwd=self.worker)
        self.commit('fix(ci): home-clock', {'x.txt': 'x\n'})
        self.commit('fix(T-0001): the hinge', {'c.txt': 'branch\n'})
        sh(['git', 'push', '-q', '-f', 'origin', self.B], cwd=self.worker)
        self.push_main({'x.txt': 'x\n'}, 'fix(ci): home-clock (#812)')
        self.push_main({'c.txt': 'trunk\n'}, 'fix: c on the trunk (#813)')
        old = self.tip()
        self.session('coder-t-0001', 'T-0001', self.B)
        lane.lane_pass(self.product(), self.state_dir, out=self.lines.append)
        self.assertEqual(self.tip(), old)
        self.assertEqual(self.archive(old), '')
        self.assertTrue(any(l.startswith(f'drop copies {self.B}:') and 'c.txt' in l
                            for l in self.lines), self.lines)
        corr = lifecycle.corrections(self.sessions())['T-0001']
        self.assertEqual((corr['kind'], corr['rounds']), (lane.COPIES, 0))
        self.assertTrue(any(l.startswith(f'held {self.B}: trunk history')
                            and l.endswith('(copies, no round)') for l in self.lines), self.lines)
        self.assertIn('conflicts in c.txt', corr['text'])
        self.assertIn('the factory publishes the rebased branch', corr['text'])
        self.assertEqual(self.lane_of(self.B)['state'], lane.BACK)
        # the next pass on the same head leaves the hold alone: no second hold, no push
        more = []
        lane.lane_pass(self.product(), self.state_dir, out=more.append)
        self.assertEqual(self.tip(), old)
        self.assertFalse(any(l.startswith('held ') for l in more), more)

    def test_a_live_session_defers_the_rebuild(self):
        old = self.copied_branch()
        p = subprocess.Popen(['true'])
        p.wait()
        with open(self.sessions(), 'a', encoding='utf-8') as fh:
            fh.write(json.dumps({'job': 'coder-t-0001', 'item': 'T-0001', 'branch': self.B,
                                 'kind': 'coder', 'pid': p.pid,
                                 'started': '2026-09-21T00:00:00Z'}) + '\n')
        lane.lane_pass(self.product(), self.state_dir, out=self.lines.append)
        self.assertEqual(self.tip(), old)
        self.assertEqual(self.archive(old), '')
        self.assertTrue(any('a live session holds it' in l for l in self.lines), self.lines)

    def test_a_branch_under_no_factory_prefix_is_never_rebuilt(self):
        old = self.copied_branch()
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        f = {'branch': self.B, 'kind': None, 'item': 'T-0001', 'head': old,
             'prev': rec(lane.PUSHED, head=old), 'run': {'job': 'x'}}
        with mock.patch.object(lane, 'drop_trunk_copies') as dc:
            self.assertIsNone(lane.Lane(self.product(), self.state_dir,
                                        out=self.lines.append).drop_copies(f))
        dc.assert_not_called()

    def test_the_rebuild_guard_refuses_a_dropped_copy_the_trunk_reverted(self):
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'checkout', '-q', '-B', self.B, 'origin/main'], cwd=self.worker)
        self.commit('fix(ci): home-clock', {'x.txt': 'x\n'})
        self.commit('feat(T-0001): the door', {'a.txt': 'a\n'})
        sh(['git', 'push', '-q', '-f', 'origin', self.B], cwd=self.worker)
        self.push_main({'x.txt': 'x\n'}, 'fix(ci): home-clock (#812)')
        sh(['git', 'rm', '-q', 'x.txt'], cwd=self.worker)
        sh(['git', 'commit', '-qm', 'revert home-clock'], cwd=self.worker, env_=self.ident)
        sh(['git', 'push', '-q', 'origin', 'tmp-main:main'], cwd=self.worker)
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        res = lane.drop_trunk_copies(self.repo, 'main', self.B)
        self.assertIsNone(res['new'])
        self.assertIn('rebuild guard', res['why'])


class GitMechanicsNeverSpawnASession(LaneFixture):
    """2026-09-30: pure git mechanics are the lane's, never a session's. A person or an agent
    merged the trunk into a lane branch by hand (a merge alone: B-0056's hold sent a session to
    rebase it); a branch the host calls conflicting went back to a session even when git
    rebases it clean. The lane rebases it itself — the product's pre-push check run on the
    result first, one push over a lease, the old tip archived — and only a textual conflict
    (its files named) or a red check reaches a session."""

    B, AUTHOR = NamingRepair.B, NamingRepair.AUTHOR
    tip = NamingRepair.tip
    commit = TrunkCommitsNeverReworded.commit
    own = TrunkCopiesDropped.own
    sessions = TrunkCopiesDropped.sessions

    def setUp(self):
        super().setUp()
        self.lines = []

    def branch(self, files=None):
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'checkout', '-q', '-B', self.B, 'origin/main'], cwd=self.worker)
        self.commit('feat(T-0001): the door', files or {'a.txt': 'a\n'})
        sh(['git', 'push', '-q', '-f', 'origin', self.B], cwd=self.worker)
        return self.tip()

    def archive(self, tag, old):
        return sh(['git', 'rev-parse', '--verify', '-q',
                   f'refs/heads/archive/{self.B}-{tag}-{old[:9]}'],
                  cwd=self.origin).stdout.strip()

    def facts(self, old):
        self.session('coder-t-0001', 'T-0001', self.B)
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        return {'branch': self.B, 'kind': 'code', 'class': lane.CODE, 'item': 'T-0001',
                'head': old, 'prev': rec(lane.GATE, head=old, pr=7), 'correction': None,
                'run': {'job': 'coder-t-0001', 'item': 'T-0001', 'branch': self.B,
                        'kind': 'coder'}}

    def test_a_trunk_merged_in_by_hand_is_dropped_by_the_lane_not_a_session(self):
        self.branch()
        self.push_main({'y.txt': 'y\n'}, 'fix: y (#811)')
        sh(['git', 'checkout', '-q', self.B], cwd=self.worker)
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'merge', '-q', '--no-edit', 'origin/main'], cwd=self.worker, env_=self.AUTHOR)
        sh(['git', 'push', '-q', '-f', 'origin', self.B], cwd=self.worker)
        old = self.tip()
        self.session('coder-t-0001', 'T-0001', self.B)
        trunk = self.origin_main()
        lane.lane_pass(self.product(), self.state_dir, out=self.lines.append)
        new = self.tip()
        self.assertNotEqual(new, old)
        self.assertEqual(self.own(new), ['feat(T-0001): the door'])
        self.assertEqual(sh(['git', 'rev-list', '--merges', f'main..{new}'],
                            cwd=self.origin).stdout, '')
        self.assertEqual(sh(['git', 'rev-parse', f'{new}~1'], cwd=self.origin).stdout.strip(),
                         trunk)
        self.assertEqual(self.archive('copies', old), old)
        self.assertEqual(lifecycle.corrections(self.sessions()), {})
        self.assertFalse(any('back to its session' in l for l in self.lines), self.lines)
        self.assertEqual(self.lane_of(self.B)['state'], lane.GATE)
        self.assertEqual(self.origin_main(), trunk)

    def test_a_conflict_git_rebases_clean_is_pushed_by_the_lane(self):
        old = self.branch()
        self.push_main({'y.txt': 'y\n'}, 'fix: y (#811)')
        trunk = self.origin_main()
        ln = lane.Lane(self.product(), self.state_dir, out=self.lines.append)
        f = self.facts(old)
        got = lane.send_back(ln, f, 'conflict', 'PR #7 merge refused — not mergeable', [])
        self.assertEqual(got, 'rebased')
        new = self.tip()
        self.assertEqual(sh(['git', 'rev-parse', f'{new}~1'], cwd=self.origin).stdout.strip(),
                         trunk)
        self.assertEqual(self.own(new), ['feat(T-0001): the door'])
        self.assertEqual(self.archive('rebase', old), old)
        self.assertEqual(f['head'], new)
        self.assertEqual(self.lane_of(self.B)['state'], lane.PUSHED)
        self.assertEqual(lifecycle.corrections(self.sessions()), {})
        self.assertTrue(any(l.startswith(f'rebased {self.B} onto origin/main') and 'no session'
                            in l for l in self.lines), self.lines)
        self.assertEqual(self.origin_main(), trunk)

    def test_a_textual_conflict_goes_to_a_session_naming_the_files(self):
        self.push_main({'c.txt': 'base\n'}, 'chore: c')
        old = self.branch({'c.txt': 'branch\n'})
        self.push_main({'c.txt': 'trunk\n'}, 'fix: c on the trunk (#813)')
        ln = lane.Lane(self.product(), self.state_dir, out=self.lines.append)
        f = self.facts(old)
        got = lane.send_back(ln, f, 'conflict', 'PR #7 merge refused — not mergeable', [])
        self.assertEqual(got, 'held')
        self.assertEqual(self.tip(), old)
        self.assertEqual(self.archive('rebase', old), '')
        corr = lifecycle.corrections(self.sessions())['T-0001']
        self.assertEqual(corr['kind'], 'conflict')
        self.assertIn('git stops at', corr['text'])
        self.assertIn('conflicts in c.txt', corr['text'])
        self.assertEqual(self.lane_of(self.B)['state'], lane.BACK)

    def test_the_pre_push_check_runs_before_the_mechanical_push(self):
        old = self.branch()
        self.push_main({'y.txt': 'y\n'}, 'fix: y (#811)')
        marker = os.path.join(self.base, "checked")
        check = f'test -f y.txt && test -f a.txt && echo ok > {marker}'
        ln = lane.Lane(self.product(pre_push_check=check), self.state_dir,
                       out=self.lines.append)
        self.assertEqual(lane.send_back(ln, self.facts(old), 'conflict', 'x', []), 'rebased')
        self.assertTrue(os.path.exists(marker))  # run on the rebased head: both files there
        self.assertNotEqual(self.tip(), old)

    def test_a_red_pre_push_check_is_a_session_naming_the_failure_and_pushes_nothing(self):
        old = self.branch()
        self.push_main({'y.txt': 'y\n'}, 'fix: y (#811)')
        ln = lane.Lane(self.product(pre_push_check='echo R-1101 changeset names a private '
                                                   'package; exit 3'),
                       self.state_dir, out=self.lines.append)
        got = lane.send_back(ln, self.facts(old), 'conflict', 'PR #7 merge refused', [])
        self.assertEqual(got, 'held')
        self.assertEqual(self.tip(), old)
        self.assertEqual(self.archive('rebase', old), '')
        corr = lifecycle.corrections(self.sessions())['T-0001']
        self.assertIn('pre-push check fails', corr['text'])
        self.assertIn('exit 3', corr['text'])
        self.assertIn('R-1101 changeset names a private package', corr['text'])

    def test_a_hand_merge_whose_rebuild_fails_the_pre_push_check_is_held_unpushed(self):
        self.branch()
        self.push_main({'y.txt': 'y\n'}, 'fix: y (#811)')
        sh(['git', 'checkout', '-q', self.B], cwd=self.worker)
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'merge', '-q', '--no-edit', 'origin/main'], cwd=self.worker, env_=self.AUTHOR)
        sh(['git', 'push', '-q', '-f', 'origin', self.B], cwd=self.worker)
        old = self.tip()
        self.session('coder-t-0001', 'T-0001', self.B)
        lane.lane_pass(self.product(pre_push_check='echo lint: 2 findings; exit 1'),
                       self.state_dir, out=self.lines.append)
        self.assertEqual(self.tip(), old)
        self.assertEqual(self.archive('copies', old), '')
        corr = lifecycle.corrections(self.sessions())['T-0001']
        self.assertEqual((corr['kind'], corr['rounds']), (lane.COPIES, 0))
        self.assertIn('lint: 2 findings', corr['text'])

    def test_a_live_session_or_a_foreign_branch_is_never_rebased_by_the_lane(self):
        old = self.branch()
        self.push_main({'y.txt': 'y\n'}, 'fix: y (#811)')
        ln = lane.Lane(self.product(), self.state_dir, out=self.lines.append)
        for extra in ({'live': True}, {'foreign': True}, {'kind': None}):
            f = dict(self.facts(old), **extra)
            self.assertIsNone(ln.rebase_onto_trunk(f))
        self.assertEqual(self.tip(), old)

    def test_a_merge_queue_conflict_with_a_batch_ahead_is_not_rebased(self):
        ln = lane.Lane(self.product(), self.state_dir, out=self.lines.append)
        f = self.facts(self.branch())
        with mock.patch.object(lane.Lane, 'rebase_onto_trunk') as rb:
            lane.send_back(ln, f, 'conflict', 'x', ['a.txt'], rebase=False)
        rb.assert_not_called()


class RefGuard(LaneFixture):
    """The product's host may protect nothing: every factory ref write refuses the trunk and a
    ``conventions.protected_refs`` ref itself, with one loud line."""

    def setUp(self):
        super().setUp()
        self.lines = []
        self.trunk = self.origin_main()
        sh(['git', 'push', '-q', 'origin', 'HEAD:refs/heads/release/1'], cwd=self.repo)
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)

    def runner(self, **conventions):
        return lane.Lane(self.product(**conventions), self.state_dir, out=self.lines.append)

    def loud(self):
        self.assertTrue(any(l.startswith('REF GUARD: refused') for l in self.lines), self.lines)

    def test_the_naming_repair_never_rewrites_the_trunk(self):
        f = {'branch': 'main', 'kind': 'code', 'item': 'T-0001', 'head': self.trunk,
             'refusal': (lifecycle.NAMING, 'x'), 'prev': rec(lane.GATE), 'run': None}
        with mock.patch.object(lane, 'reword_branch') as rw:
            self.assertIsNone(self.runner().repair_naming(f))
        rw.assert_not_called()
        self.loud()
        self.assertEqual(self.origin_main(), self.trunk)

    def test_the_copies_rebuild_never_rewrites_a_protected_ref(self):
        for b in ('main', 'release/1'):
            f = {'branch': b, 'kind': 'code', 'item': 'T-0001', 'head': self.trunk,
                 'prev': rec(lane.PUSHED), 'run': None}
            with mock.patch.object(lane, 'trunk_history', return_value=(['c' * 40], [])), \
                    mock.patch.object(lane, 'drop_trunk_copies') as dc:
                self.assertIsNone(self.runner().drop_copies(f))
            dc.assert_not_called()
        self.loud()
        self.assertEqual(self.origin_main(), self.trunk)

    def test_the_signoff_repair_never_rewrites_a_protected_ref(self):
        f = {'branch': 'release/1', 'kind': 'code', 'head': self.trunk, 'prev': rec(lane.GATE),
             'run': None}
        with mock.patch.object(lane, 'signoff_branch') as so:
            self.assertIsNone(self.runner().repair_signoff(f, 'DCO'))
        so.assert_not_called()
        self.loud()

    def test_a_ref_push_or_delete_never_reaches_the_trunk(self):
        ln = self.runner()
        self.assertFalse(ln.ref_push(':refs/heads/main', 'delete main'))
        self.assertFalse(ln.ref_push(f'{self.trunk}:refs/heads/release/1', 'archive release/1'))
        self.assertEqual(len(ln.ref_failures), 2)
        self.loud()
        self.assertEqual(self.origin_main(), self.trunk)
        with mock.patch.object(harvest, '_gh') as gh:
            ok, why = lane.api_ref('o/p', ':refs/heads/main', main='main')
        gh.assert_not_called()
        self.assertFalse(ok)
        self.assertIn('REF GUARD', why)

    def test_publish_retention_and_branch_push_refuse_the_trunk(self):
        from asf.workers import retention
        ok, line = lifecycle.publish(self.repo, 'release/1', '', main='trunk')
        self.assertFalse(ok)
        self.assertIn('REF GUARD', line)
        ok, why = retention.delete(self.repo, 'main', self.trunk, main='main')
        self.assertFalse(ok)
        self.assertIn('REF GUARD', why)
        ok, why = harvest.push_branch(self.repo, self.trunk, 'master', '')
        self.assertFalse(ok)
        self.assertIn('REF GUARD', why)
        self.assertEqual(self.origin_main(), self.trunk)

    def test_protected_refs_is_the_products_list_and_the_trunk_always(self):
        from asf import refguard
        ln = self.runner(protected_refs=['prod/*'])
        self.assertTrue(ln.guarded('refs/heads/prod/eu', 'x'))
        self.assertTrue(ln.guarded('main', 'x'))
        self.assertFalse(ln.guarded('release/1', 'x'))
        self.assertFalse(ln.guarded('worker/T-0001', 'x'))
        self.assertTrue(refguard.refusal('release/3', 'x', out=lambda _l: None))


class Occupancy(unittest.TestCase):
    """R16: the lane and the feeder are one stream — the feeder reads the lane's states through
    the one occupancy answer, and the rows follow them."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='occ_')
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, 'sessions.jsonl')

    def line(self, **rec_):
        with open(self.path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec_) + '\n')

    def run_(self, job, item, branch, state, **lane_kw):
        self.line(job=job, item=item, branch=branch, kind='coder', pid=None,
                  started='2026-09-21T00:00:00Z', ended='2026-09-21T00:05:00Z',
                  end_reason='finished', lane=dict(rec(state), item=item, **lane_kw))

    def test_r16_the_feeder_reads_the_lane(self):
        self.run_('coder-t-0001', 'T-0001', 'worker/T-0001', lane.REVIEW, round=2, pr=7)
        self.run_('coder-t-0002', 'T-0002', 'worker/T-0002', lane.GATE)
        self.run_('coder-t-0003', 'T-0003', 'worker/T-0003', lane.MERGED)
        occ = lifecycle.occupancy(self.path)
        self.assertEqual(set(occ['waiting_landing']), {'T-0001', 'T-0002'})
        self.assertEqual(occ['review']['T-0001']['round'], 2)
        self.assertEqual(occ['landing']['T-0002']['state'], lane.GATE)
        feature = {'id': 'F-0001', 'type': 'feature', 'decided': True, 'state': 'Active',
                   'stage': 'plan-approved'}
        items = {'F-0001': feature}
        for n in (1, 2, 3):
            items[f'T-000{n}'] = {'id': f'T-000{n}', 'type': 'task', 'state': 'Active',
                                  'parent': 'F-0001', 'writes': [f'src/{n}.py']}
        product = env.Product('p', {'conventions': {}})
        got = [(r.kind, r.item_id, r.launches, r.review_round)
               for r in feeder_rows.candidates(items, product, [], occupancy=occ)
               if r.item_id.startswith('T-')]
        self.assertEqual(got, [(feeder_rows.PUSHED_REVIEW, 'T-0001', True, 2),
                               (feeder_rows.PUSHED_LAND, 'T-0002', False, 0)])

    def test_a_live_run_is_busy_and_never_waiting(self):
        self.line(job='coder-t-0001', item='T-0001', branch='worker/T-0001', kind='coder',
                  pid=os.getpid(), started='2026-09-21T00:00:00Z')
        occ = lifecycle.occupancy(self.path, alive=lambda pid: True)
        self.assertIn('T-0001', occ['busy'])
        self.assertNotIn('T-0001', occ['waiting_landing'])

    def test_back_is_the_corrections(self):
        self.run_('coder-t-0001', 'T-0001', 'worker/T-0001', lane.BACK)
        self.line(job='coder-t-0001', correction={'kind': 'gate', 'text': 'red', 'at': 'x'},
                  rounds=1)
        occ = lifecycle.occupancy(self.path)
        self.assertNotIn('T-0001', occ['waiting_landing'])
        self.assertEqual(occ['corrections']['T-0001']['kind'], 'gate')

    def test_a_pre_lane_finished_run_waits_to_land(self):
        self.line(job='spec-f-0001', item='F-0001', branch='spec/F-0001', kind='spec', pid=None,
                  started='2026-09-21T00:00:00Z', ended='2026-09-21T00:05:00Z',
                  end_reason='finished')
        occ = lifecycle.occupancy(self.path)
        self.assertEqual(occ['docs']['F-0001']['spec'], lifecycle.PUSHED_WAIT)
        self.assertIn('spec/F-0001', occ['branches'])

    def test_a_parked_draft_pr_launches_nothing_and_shows_a_waits_row(self):
        """B-0130: PARKED is busy (no new session) and lands in ``landing``, never ``review`` —
        the feeder shows a WAITS row naming the draft, never PUSHED → REVIEW nor an adjudicate."""
        self.run_('coder-t-0004', 'T-0004', 'worker/T-0004', lane.PARKED, pr=817,
                  reason='PR #817 is a draft — parked by its owner')
        self.line(job='coder-t-0004', correction={'kind': 'gate', 'text': 'red', 'at': 'x'},
                  rounds=1)
        occ = lifecycle.occupancy(self.path)
        self.assertIn('T-0004', occ['waiting_landing'])
        self.assertNotIn('T-0004', occ['review'])
        self.assertIn('draft', occ['landing']['T-0004']['why'])
        item = {'T-0004': {'id': 'T-0004', 'type': 'task', 'state': 'Active', 'parent': 'F-0001'}}
        product = env.Product('p', {'conventions': {}})
        rows = feeder_rows.candidates(item, product, [], occupancy=occ)
        self.assertEqual([r.kind for r in rows], [feeder_rows.PUSHED_LAND])
        row = rows[0]
        self.assertFalse(row.launches)
        self.assertIn('draft', row.action + row.reason)
        self.assertNotIn(feeder_rows.PUSHED_REVIEW, [r.kind for r in rows])
        self.assertNotIn(feeder_rows.STALEMATE, [r.kind for r in rows])


class FeederAndOutcomes(unittest.TestCase):
    """The feeder half of the stream (R16): footprint, outcome classes, the DONE table."""

    def test_shared_paths_are_no_footprint_overlap(self):
        from asf.feeder import footprint
        running = [('T-0001', ['src/a.py', 'uv.lock'])]
        self.assertEqual(footprint.first_conflict(['uv.lock', 'src/b.py'], running), 'T-0001')
        self.assertIsNone(footprint.first_conflict(['uv.lock', 'src/b.py'], running,
                                                   shared=['uv.lock']))
        self.assertEqual(footprint.first_conflict(['src/a.py'], running, shared=['uv.lock']),
                         'T-0001')
        product = env.Product('p', {'conventions': {'shared_paths': ['uv.lock']}})
        items = {'F-0001': {'id': 'F-0001', 'type': 'feature', 'decided': True,
                            'state': 'Active', 'stage': 'plan-approved',
                            'children': ['T-0001', 'T-0002']},
                 'T-0001': {'id': 'T-0001', 'type': 'task', 'state': 'Active',
                            'parent': 'F-0001', 'writes': ['src/a.py', 'uv.lock']},
                 'T-0002': {'id': 'T-0002', 'type': 'task', 'state': 'New', 'parent': 'F-0001',
                            'writes': ['src/b.py', 'uv.lock']}}
        inflight = [{'item': 'T-0001', 'kind': 'task', 'job': 'task-t-0001'}]
        (row,) = [r for r in feeder_rows.candidates(items, product, inflight)
                  if r.item_id == 'T-0002']
        self.assertTrue(row.launches, row.action)

    def test_hook_refusal_and_network_error_are_classes_of_their_own(self):
        self.assertEqual(lifecycle.outcome_class('failed: hook refused: pre-push: red'),
                         lifecycle.HOOK_REFUSED)
        self.assertEqual(lifecycle.outcome_class('failed: network error: early EOF'),
                         lifecycle.NETWORK_ERROR)
        self.assertEqual(lifecycle.push_failure('fatal: unable to access: Could not resolve host'),
                         lifecycle.NETWORK_ERROR)
        self.assertEqual(lifecycle.push_failure('error: failed to push some refs (pre-push hook '
                                                'declined)'), lifecycle.HOOK_REFUSED)
        self.assertIsNone(lifecycle.push_failure('the script does not push'))

    def test_b0097_a_network_blip_is_retried_and_a_hook_refusal_carries_its_output(self):
        from asf.workers import health
        ev = lifecycle.Evidence(result={'result': 'REPORT\npushed: no — the push was refused: '
                                                  'pre-push: the trunk is red\n'})
        cls, detail = health.push_retry(ev, 'failed: unpushed work', None)
        self.assertEqual(cls, lifecycle.HOOK_REFUSED)
        self.assertIn('the trunk is red', detail)
        net = health.push_retry(lifecycle.Evidence(result={'result': 'done'}), 'failed: not pushed: 1',
                                'publish w/x refused: fatal: unable to access: Connection reset')
        self.assertEqual(net[0], lifecycle.NETWORK_ERROR)
        self.assertIsNone(health.push_retry(ev, 'finished', None))
        # a commit its own hook refused is still a hold (B-0094), not a push class
        self.assertIsNone(health.push_retry(lifecycle.Evidence(result={'result': 'x'}),
                                            'failed: not pushed: 1', 'commit w/x refused: refused'))

    def test_the_done_table_credits_only_a_run_with_its_own_commits(self):
        from asf.tick import summary
        own = {'job': 'a', 'ended': 'x', 'end_reason': 'finished', 'harvested': 'abc1234'}
        empty = dict(own, end_reason='failed: empty branch: nothing to land')
        adopted = dict(own, adopted=True)
        archived = dict(own, harvested='superseded')
        self.assertTrue(summary.credits_landing(own))
        for run in (empty, adopted, archived):
            with self.subTest(run=run):
                self.assertFalse(summary.credits_landing(run))
        text = summary.render([], [own, empty], {}, 's', 'n', False)
        self.assertEqual(text.count('landed abc1234'), 1)


class ApiRef(unittest.TestCase):
    """A hosted origin's ref-only pushes are host API calls (no pre-push hook)."""

    def run_(self, refspec, answers, lease=None):
        calls = []

        def fake(args):
            calls.append(args)
            return answers.pop(0)
        with mock.patch.object(harvest, '_gh', side_effect=fake):
            return lane.api_ref('o/r', refspec, lease) + (calls,)

    def test_create_and_one_already_there_at_the_sha(self):
        ok, why, calls = self.run_('a' * 40 + ':refs/heads/archive/x', [(0, '', '')])
        self.assertTrue(ok, why)
        self.assertEqual(calls, [['api', '-X', 'POST', 'repos/o/r/git/refs', '-f',
                                  'ref=refs/heads/archive/x', '-f', 'sha=' + 'a' * 40]])
        ok, why, _ = self.run_('a' * 40 + ':refs/heads/archive/x',
                               [(1, '', 'Reference already exists'), (0, 'a' * 40 + '\n', '')])
        self.assertTrue(ok, why)
        ok, why, _ = self.run_('a' * 40 + ':refs/heads/archive/x',
                               [(1, '', 'Reference already exists'), (0, 'b' * 40 + '\n', '')])
        self.assertFalse(ok)
        self.assertIn('already exists', why)

    def test_delete_only_while_the_tip_is_the_judged_sha(self):
        lease = 'refs/heads/x:' + 'a' * 40
        ok, why, calls = self.run_(':refs/heads/x', [(0, 'a' * 40 + '\n', ''), (0, '', '')], lease)
        self.assertTrue(ok, why)
        self.assertEqual(calls[1], ['api', '-X', 'DELETE', 'repos/o/r/git/refs/heads/x'])
        ok, why, calls = self.run_(':refs/heads/x', [(0, 'b' * 40 + '\n', '')], lease)
        self.assertFalse(ok)
        self.assertIn('tip moved', why)
        self.assertEqual(len(calls), 1)


class SkippedRequiredCheck(unittest.TestCase):
    """2026-09-26 incident: a PR merged on its one landing check while the product's deploy-
    required suites were still running (then cancelled) and one had failed. A PR merges on its
    checks alone only when every required check concluded ``success`` on its head: skipped,
    missing, pending, cancelled or failed never merges, and the held line names them."""
    def _host(self, conv=None, deploy_sha=None):
        cfg = {'repo_slug': 'o/p', 'conventions': dict({
            'landing': 'pull-request', 'landing_checks': ['ci', 'docs'],
            'landing_checks_missing': 'wait'}, **(conv or {}))}
        if deploy_sha is not None:
            cfg['deploy_sha'] = deploy_sha
        product = env.Product('p', cfg)
        tmp = tempfile.mkdtemp(prefix='skip_')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        runner = lane.Lane.__new__(lane.Lane)
        self.lines = []
        runner.product, runner.conv, runner.out, runner.dry_run = product, product.conventions, \
            self.lines.append, False
        runner.results, runner.now, runner.state_dir = {}, NOW, tmp
        runner.repo = None
        host = lane.GitHubHost(product, None)
        host.lane = runner
        self.runner = runner
        return host

    def _gate(self, host, checks, cls=lane.CODE, prev=None):
        f = {'branch': 'worker/T-0001', 'prev': prev or rec(lane.GATE, pr=7), 'class': cls,
             'head': HEAD}
        with mock.patch.object(harvest, '_gh', return_value=(0, json.dumps(checks), '')), \
                mock.patch.object(lane.Lane, 'set', lambda self, f, s, r, result=None, **kw:
                                  self.results.update({f['branch']: (s, r)})), \
                mock.patch.object(lane, 'send_back', lambda ln, f, *a, **k:
                                  ln.results.update({f['branch']: ('back', a)})):
            return host.check_gate(f, 7, ['src/a.py']), self.runner.results.get(f['branch'])

    def test_a_skipped_required_check_never_merges(self):
        host = self._host()
        checks = [{'name': 'ci', 'bucket': 'pass'}, {'name': 'docs', 'bucket': 'skipping'}]
        how, got = self._gate(host, checks)
        self.assertIsNone(how)
        self.assertEqual(got[0], lane.WAITING_CI)
        self.assertIn('docs', got[1])
        self.assertIn('skipped', got[1])
        # and the missing-check clock running out never turns a skip into a local gate
        old = rec(lane.WAITING_CI, pr=7, since=NOW - 10 * 86400)
        how, got = self._gate(host, checks, prev=old)
        self.assertIsNone(how)
        self.assertEqual(got[0], lane.WAITING_CI)

    def test_every_required_check_success_merges_on_ci(self):
        host = self._host()
        checks = [{'name': 'ci', 'bucket': 'pass'}, {'name': 'docs', 'bucket': 'pass'},
                  {'name': 'lint', 'bucket': 'skipping'}]
        self.assertEqual(self._gate(host, checks)[0], 'ci')

    def test_a_missing_required_check_waits(self):
        host = self._host()
        how, got = self._gate(host, [{'name': 'ci', 'bucket': 'pass'}])
        self.assertIsNone(how)
        self.assertEqual(got[0], lane.WAITING_CI)
        self.assertIn('docs', got[1])

    def test_the_deploy_required_jobs_are_required_to_merge_code(self):
        # the incident shape: the landing check green, the deploy-required suites unfinished,
        # cancelled or failed — never a merge
        host = self._host(conv={'landing_checks': ['gate']},
                          deploy_sha={'prod': {'mode': 'manual', 'workflow': 'd.yml',
                                               'required_jobs': ['gate', 'gate-tests', 'e2e']}})
        pending = [{'name': 'gate', 'bucket': 'pass'}, {'name': 'gate-tests', 'bucket': 'pending'},
                   {'name': 'e2e', 'bucket': 'pass'}]
        how, got = self._gate(host, pending)
        self.assertIsNone(how)
        self.assertEqual(got[0], lane.WAITING_CI)
        self.assertIn('gate-tests', got[1])
        red = [{'name': 'gate', 'bucket': 'pass'}, {'name': 'gate-tests', 'bucket': 'pass'},
               {'name': 'e2e', 'bucket': 'fail'}]
        how, got = self._gate(host, red)
        self.assertIsNone(how)
        self.assertNotEqual(got and got[0], 'ci')
        missing = [{'name': 'gate', 'bucket': 'pass'}, {'name': 'e2e', 'bucket': 'pass'}]
        how, got = self._gate(host, missing)
        self.assertIsNone(how)
        self.assertIn('gate-tests', got[1])
        green = [{'name': 'gate', 'bucket': 'pass'}, {'name': 'gate-tests', 'bucket': 'pass'},
                 {'name': 'e2e (shard 1)', 'bucket': 'pass'}, {'name': 'soak', 'bucket': 'fail'}]
        self.assertEqual(self._gate(host, green)[0], 'ci')
        # a matrix leg of a required job that failed is red
        leg = green[:2] + [{'name': 'e2e (shard 1)', 'bucket': 'pass'},
                           {'name': 'e2e (shard 2)', 'bucket': 'fail'}]
        self.assertIsNone(self._gate(host, leg)[0])

    def test_a_docs_pr_needs_only_its_landing_checks(self):
        host = self._host(conv={'landing_checks': ['gate']},
                          deploy_sha={'prod': {'mode': 'manual', 'workflow': 'd.yml',
                                               'required_jobs': ['gate', 'gate-tests']}})
        checks = [{'name': 'gate', 'bucket': 'pass'}, {'name': 'gate-tests', 'bucket': 'skipping'}]
        self.assertEqual(self._gate(host, checks, cls=lane.DOCS)[0], 'ci')


class PathFilteredRequiredCheck(unittest.TestCase):
    """A PR whose CI path-filters suites (a job ``if:`` false reports skipped; a job the workflow
    never created is missing) merges under ``conventions.merge_skipped: path-filtered`` (the
    default) once every workflow run on its head completed, a required check concluded success
    and none is red. Anything short of that waits; ``never`` keeps skip-is-not-green."""
    REQUIRED = ['gate', 'gate-tests', 'm8-e2e', 'p1-e2e']

    def _host(self, conv=None):
        cfg = {'repo_slug': 'o/p', 'conventions': dict({
            'landing': 'pull-request', 'landing_checks': self.REQUIRED,
            'landing_checks_missing': 'wait'}, **(conv or {}))}
        product = env.Product('p', cfg)
        tmp = tempfile.mkdtemp(prefix='pathf_')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        runner = lane.Lane.__new__(lane.Lane)
        self.lines = []
        runner.product, runner.conv, runner.out, runner.dry_run = product, product.conventions, \
            self.lines.append, False
        runner.results, runner.now, runner.state_dir = {}, NOW, tmp
        runner.repo = None
        host = lane.GitHubHost(product, None)
        host.lane = runner
        self.runner = runner
        return host

    def _gh(self, checks, runs, jobs):
        self.calls = []

        def gh(args):
            self.calls.append(args)
            if args[:2] == ['pr', 'checks']:
                return 0, json.dumps(checks), ''
            if args[0] == 'api' and '/actions/runs?head_sha=' in args[1]:
                return 0, json.dumps({'workflow_runs': runs}), ''
            if args[0] == 'api' and args[1].split('?')[0].endswith('/jobs'):
                return 0, json.dumps({'jobs': jobs}), ''
            return 1, '', 'unexpected'
        return gh

    def _gate(self, host, checks, runs, jobs, prev=None):
        f = {'branch': 'worker/T-0001', 'prev': prev or rec(lane.GATE, pr=7), 'class': lane.CODE,
             'head': HEAD}
        with mock.patch.object(harvest, '_gh', side_effect=self._gh(checks, runs, jobs)), \
                mock.patch.object(lane.Lane, 'set', lambda self, f, s, r, result=None, **kw:
                                  self.results.update({f['branch']: (s, r)})), \
                mock.patch.object(lane, 'send_back', lambda ln, f, *a, **k:
                                  ln.results.update({f['branch']: ('back', a)})):
            how = host.check_gate(f, 7, ['src/a.py'])
            return how, self.runner.results.get(f['branch']), f

    CHECKS = [{'name': 'gate', 'bucket': 'pass'}, {'name': 'gate-tests', 'bucket': 'pass'},
              {'name': 'm8-e2e', 'bucket': 'skipping'}]
    JOBS = [{'name': 'changes', 'conclusion': 'success'},
            {'name': 'gate', 'conclusion': 'success'},
            {'name': 'gate-tests', 'conclusion': 'success'},
            {'name': 'm8-e2e', 'conclusion': 'skipped'}]
    DONE = [{'id': 11, 'name': 'ci', 'status': 'completed', 'conclusion': 'success'}]

    def _checks(self, gate='pass'):
        return [dict(c, bucket=gate) if c['name'] == 'gate' else c for c in self.CHECKS]

    def test_completed_run_skipped_and_missing_with_a_success_merges(self):
        host = self._host()
        how, _got, f = self._gate(host, self._checks(), self.DONE, self.JOBS)
        self.assertEqual(how, 'ci')
        text = '\n'.join(self.lines)
        self.assertIn('m8-e2e skipped by the workflow — satisfied (run completed, gate, '
                      'gate-tests success)', text)
        self.assertIn('p1-e2e not created by the workflow — satisfied', text)
        self.assertTrue(f.get('on_checks'))

    def test_a_run_still_in_progress_waits_and_never_reaches_the_local_gate(self):
        host = self._host()
        runs = self.DONE + [{'id': 12, 'name': 'e2e', 'status': 'in_progress'}]
        old = rec(lane.WAITING_CI, pr=7, since=NOW - 10 * 86400)
        how, got, _f = self._gate(host, self._checks(), runs, self.JOBS, prev=old)
        self.assertIsNone(how)
        self.assertEqual(got[0], lane.WAITING_CI)
        self.assertIn('not completed', got[1])
        self.assertIn('e2e', got[1])

    def test_a_skip_beside_a_failed_required_check_is_red(self):
        host = self._host()
        how, got, _f = self._gate(host, self._checks('fail'), self.DONE, self.JOBS)
        self.assertIsNone(how)
        self.assertEqual(got[0], 'back')

    def test_all_required_skipped_or_missing_with_no_success_waits(self):
        host = self._host()
        checks = [{'name': 'gate', 'bucket': 'skipping'},
                  {'name': 'gate-tests', 'bucket': 'skipping'},
                  {'name': 'm8-e2e', 'bucket': 'skipping'}]
        jobs = [{'name': c['name'], 'conclusion': 'skipped'} for c in checks]
        how, got, _f = self._gate(host, checks, self.DONE, jobs)
        self.assertIsNone(how)
        self.assertEqual(got[0], lane.WAITING_CI)
        self.assertIn('skipped', got[1])

    def test_a_neutral_check_is_not_a_workflow_skip(self):
        # gh buckets NEUTRAL with SKIPPED; only a job that concluded `skipped` is the workflow's
        host = self._host()
        jobs = [dict(j, conclusion='neutral') if j['name'] == 'm8-e2e' else j for j in self.JOBS]
        how, got, _f = self._gate(host, self._checks(), self.DONE, jobs)
        self.assertIsNone(how)
        self.assertIn('m8-e2e', got[1])

    def test_never_keeps_skipped_not_green(self):
        host = self._host(conv={'merge_skipped': 'never'})
        how, got, _f = self._gate(host, self._checks(), self.DONE, self.JOBS)
        self.assertIsNone(how)
        self.assertEqual(got[0], lane.WAITING_CI)
        self.assertIn('m8-e2e', got[1])
        self.assertFalse(any('/actions/runs' in ' '.join(a) for a in self.calls))

    def test_unreadable_runs_hold(self):
        host = self._host()
        how, got, _f = self._gate(host, self._checks(), [], self.JOBS)
        self.assertIsNone(how)
        self.assertIn('m8-e2e', got[1])

    def test_recheck_before_merging_still_applies(self):
        host = self._host()
        f = {'branch': 'worker/T-0001', 'class': lane.CODE, 'head': HEAD, 'how': 'ci',
             'on_checks': True}
        with mock.patch.object(harvest, '_gh', side_effect=self._gh(
                self._checks(), self.DONE, self.JOBS)):
            self.assertIsNone(host.recheck(f, 7))
        # a required check went red since the pass read them
        with mock.patch.object(harvest, '_gh', side_effect=self._gh(
                self._checks('fail'), self.DONE, self.JOBS)):
            self.assertIn('red', host.recheck(f, 7))
        # a new run started on the head: the skip is no longer the workflow's final word
        runs = self.DONE + [{'id': 13, 'name': 'ci', 'status': 'queued'}]
        with mock.patch.object(harvest, '_gh', side_effect=self._gh(
                self._checks(), runs, self.JOBS)):
            self.assertIn('not completed', host.recheck(f, 7))
        # merge_skipped flipped to never between the gate and the merge
        host = self._host(conv={'merge_skipped': 'never'})
        with mock.patch.object(harvest, '_gh', side_effect=self._gh(
                self._checks(), self.DONE, self.JOBS)):
            self.assertIn('m8-e2e', host.recheck(f, 7))


class AConflictingBranchNeverWaitsForCI(LaneFixture):
    """A product's #1037, #1023 and #1017: PRs opened on branches that already conflicted with
    the trunk. GitHub runs no ``pull_request`` workflow on a conflicting PR, so none of them ever
    got a CI run — while the lane asked review rounds of them, pass after pass. A branch the
    trunk conflicts with goes BACK to be rebased before a PR is opened, and an open PR found
    conflicting goes back before any review or CI wait: every PR the factory opens gets its run."""

    CONFLICT = ['a.txt']

    def test_pushed_goes_back_instead_of_opening_a_pr(self):
        f = facts(mode='pr', conflict=self.CONFLICT)
        self.assertEqual(lane.next_state(rec(lane.PUSHED), f), (lane.BACK, 'kind=conflict'))
        f = facts(mode='pr', conflict=self.CONFLICT, pr={'number': 7, 'state': 'OPEN',
                                                         'head': HEAD})
        self.assertEqual(lane.next_state(rec(lane.PUSHED), f), (lane.BACK, 'kind=conflict'))
        # a clean branch opens as before
        self.assertEqual(lane.next_state(rec(lane.PUSHED), facts(mode='pr', conflict=[])),
                         (lane.PR_OPEN, 'open a PR'))

    def test_an_open_pr_goes_back_before_any_review(self):
        f = facts(mode='pr', conflict=self.CONFLICT, review_required=True,
                  pr={'number': 7, 'state': 'OPEN', 'head': HEAD})
        for s in (lane.PR_OPEN, lane.REVIEW):
            self.assertEqual(lane.next_state(rec(s, pr=7), f), (lane.BACK, 'kind=conflict'))

    def test_the_branch_facts_read_the_conflict(self):
        self.push_main({'a.txt': 'trunk\n'}, 'chore: a on main')
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'checkout', '-q', '-B', 'worker/T-0001', 'origin/main~1'], cwd=self.worker)
        self.write(self.worker, 'a.txt', 'branch\n')
        sh(['git', 'add', '-A'], cwd=self.worker)
        sh(['git', 'commit', '-qm', 'feat(T-0001): a'], cwd=self.worker, env_=self.ident)
        sh(['git', 'push', '-q', '-f', 'origin', 'worker/T-0001'], cwd=self.worker)
        self.push_lane('worker/T-0002', {'b.txt': 'b\n'}, 'feat(T-0002): b')
        items = {t: {'id': t, 'type': 'task', 'state': 'Active'} for t in ('T-0001', 'T-0002')}
        ln = lane.Lane(self.product(), self.state_dir, out=lambda *_: None, items=items)
        ln.mode, ln.slug = 'pr', 'o/p'
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        heads = ln.remote_heads()
        ln.trunk_sha = heads['main']
        f = ln.branch_facts('worker/T-0001', None, heads['worker/T-0001'], None, True, False)
        self.assertEqual(f['conflict'], ['a.txt'])
        f = ln.branch_facts('worker/T-0002', None, heads['worker/T-0002'], None, True, False)
        self.assertFalse(f.get('conflict'))

    def test_back_sends_it_to_be_rebased(self):
        runner = lane.Lane.__new__(lane.Lane)
        lines = []
        runner.out, runner.dry_run, runner.results, runner.trunk = lines.append, False, {}, 'main'
        f = {'branch': 'cloud/spec-x', 'item': 'F-0001', 'head': HEAD, 'prev': rec(lane.REVIEW),
             'pr': {'number': 1023, 'state': 'OPEN'}, 'conflict': ['docs/a.md'], 'run': {}}
        with mock.patch.object(lane, 'send_back') as sb:
            runner.enter_back(f, 'kind=conflict')
        sb.assert_called_once()
        _ln, _f, kind, text, files = sb.call_args[0]
        self.assertEqual(kind, 'conflict')
        self.assertIn('#1023', text)
        self.assertIn('no pull_request workflow', text)
        self.assertEqual(list(files), ['docs/a.md'])


class AHostDirtyButGitCleanBranchIsRebasedNotParked(LaneFixture):
    """#44: a register file's ``.gitattributes merge=union`` driver GitHub's own merge cannot
    apply, so GitHub reads such a PR ``mergeable: CONFLICTING`` while ``git merge-tree
    --write-tree`` (:func:`lane.conflict_files`) merges it onto the trunk clean. Reading
    ``f['conflict']`` (git's own verdict) alone left a PR like this PUSHED or PR_OPEN forever:
    GitHub runs no ``pull_request`` workflow on a PR it calls dirty, so it got no CI and no row.
    ``host_dirty`` carries GitHub's verdict (off the same per-tick PR snapshot the check-run read
    already pays for, :func:`lane.host_reads_dirty`), so the branch still goes BACK and the lane
    rebases it with the repo's own merge drivers — never a session, never silence."""

    def test_pushed_with_a_github_dirty_git_clean_pr_goes_back_too(self):
        f = facts(mode='pr', conflict=[], host_dirty=True)
        self.assertEqual(lane.next_state(rec(lane.PUSHED), f), (lane.BACK, 'kind=conflict'))
        # git-clean and GitHub-clean: opens as before
        self.assertEqual(lane.next_state(rec(lane.PUSHED), facts(mode='pr', conflict=[],
                                                                  host_dirty=False)),
                         (lane.PR_OPEN, 'open a PR'))

    def test_an_open_pr_host_dirty_goes_back_before_any_review(self):
        f = facts(mode='pr', conflict=[], host_dirty=True, review_required=True,
                  pr={'number': 7, 'state': 'OPEN', 'head': HEAD})
        for s in (lane.PR_OPEN, lane.REVIEW):
            self.assertEqual(lane.next_state(rec(s, pr=7), f), (lane.BACK, 'kind=conflict'))

    def test_host_reads_dirty_trusts_the_snapshot_only_at_the_exact_head(self):
        snap = {7: {'head': HEAD, 'mergeable': 'CONFLICTING'}}
        with mock.patch.object(lane.pr_graph, 'snapshot', return_value=snap):
            self.assertTrue(lane.host_reads_dirty('o/p', 7, HEAD))
            self.assertFalse(lane.host_reads_dirty('o/p', 7, NEW))     # head moved since
        with mock.patch.object(lane.pr_graph, 'snapshot',
                               return_value={7: {'head': HEAD, 'mergeable': 'MERGEABLE'}}):
            self.assertFalse(lane.host_reads_dirty('o/p', 7, HEAD))
        with mock.patch.object(lane.pr_graph, 'snapshot', return_value=None):
            self.assertFalse(lane.host_reads_dirty('o/p', 7, HEAD))
        self.assertFalse(lane.host_reads_dirty('o/p', None, HEAD))
        self.assertFalse(lane.host_reads_dirty('', 7, HEAD))

    def test_back_rebases_a_host_dirty_branch_with_no_file_list(self):
        runner = lane.Lane.__new__(lane.Lane)
        lines = []
        runner.out, runner.dry_run, runner.results, runner.trunk = lines.append, False, {}, 'main'
        f = {'branch': 'cloud/T-0099', 'item': 'T-0099', 'head': HEAD, 'prev': rec(lane.PUSHED),
             'pr': {'number': 1099, 'state': 'OPEN'}, 'conflict': [], 'host_dirty': True,
             'run': {}}
        with mock.patch.object(lane, 'send_back') as sb:
            runner.enter_back(f, 'kind=conflict')
        sb.assert_called_once()
        _ln, _f, kind, text, files = sb.call_args[0]
        self.assertEqual(kind, 'conflict')
        self.assertIn('#1099', text)
        self.assertIn('mergeable: CONFLICTING', text)
        self.assertIn('git merges it onto', text)
        self.assertEqual(list(files), [])

    def test_the_branch_facts_read_the_host_dirty_pr_when_git_is_clean(self):
        self.push_main({'a.txt': 'trunk\n'}, 'chore: a on main')
        self.push_lane('worker/T-0002', {'b.txt': 'b\n'}, 'feat(T-0002): b')
        items = {'T-0002': {'id': 'T-0002', 'type': 'task', 'state': 'Active'}}
        ln = lane.Lane(self.product(), self.state_dir, out=lambda *_: None, items=items)
        ln.mode, ln.slug = 'pr', 'o/p'
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        heads = ln.remote_heads()
        ln.trunk_sha = heads['main']
        snap = {42: {'head': heads['worker/T-0002'], 'mergeable': 'CONFLICTING'}}
        with mock.patch.object(lane.pr_graph, 'snapshot', return_value=snap):
            f = ln.branch_facts('worker/T-0002', None, heads['worker/T-0002'],
                                {'number': 42, 'state': 'OPEN'}, True, False)
        self.assertEqual(f['conflict'], [])
        self.assertTrue(f.get('host_dirty'))


class AMergeConflictGoesBack(unittest.TestCase):
    """2026-09-27: a product's two spec-lane PRs and one code PR passed GATE, and the host
    refused each merge for conflicts with a trunk that moved under them. The lane recorded
    WAITING (``merge refused``) with its green kept, and the next pass merged — and was refused —
    again: the code PR went WAITING → MERGING → WAITING on the conflict fifteen times in 21
    hours. A conflict is no one's clock: the branch goes BACK to be rebased, the gate's own
    conflict path (docs: a landing-gate correction; code: its session's round)."""

    CONFLICT = 'GraphQL: Pull Request has merge conflicts (mergePullRequest)'

    def _merge(self, how, cls, conflicting=False, files=('docs/decisions/bands.md',)):
        runner = lane.Lane.__new__(lane.Lane)
        lines = []
        runner.out, runner.dry_run, runner.results, runner.repo = lines.append, False, {}, None
        runner.trunk = 'main'
        fake = mock.Mock(in_queue=0, merged=0)
        fake.slots.return_value = (None, '')
        fake.recheck.return_value = None
        fake.merge.return_value = (None, how)
        fake.conflicting.return_value = conflicting
        runner.host = fake
        f = {'branch': 'cloud/spec-x', 'class': cls, 'kind': 'spec', 'item': 'F-0001',
             'head': HEAD, 'prev': rec(lane.GATE, pr=842), 'green': {'head': HEAD, 'trunk': NEW}}
        with mock.patch.object(lane.Lane, 'ci_admits', return_value=True), \
                mock.patch.object(lane.Lane, 'set'), \
                mock.patch.object(lane, 'conflict_files', return_value=list(files)), \
                mock.patch.object(lane, 'send_back') as sb, \
                mock.patch.object(lane, 'wait') as wt:
            lane.merge_prs(runner, [f])
        return sb, wt, lines

    def test_a_docs_lane_refused_for_conflicts_goes_back(self):
        sb, wt, _lines = self._merge(self.CONFLICT, lane.DOCS)
        wt.assert_not_called()
        sb.assert_called_once()
        _ln, f, kind, text, files = sb.call_args[0]
        self.assertEqual((f['branch'], kind), ('cloud/spec-x', 'conflict'))
        self.assertIn('#842', text)
        self.assertIn('merge conflicts', text)
        self.assertIn('git rebase origin/main', text)
        self.assertEqual(list(files), ['docs/decisions/bands.md'])

    def test_a_code_lane_refused_for_conflicts_goes_back(self):
        sb, wt, _lines = self._merge(self.CONFLICT, lane.CODE)
        wt.assert_not_called()
        self.assertEqual(sb.call_args[0][2], 'conflict')

    def test_a_refusal_whose_text_hides_the_conflict_asks_the_host(self):
        # gh prints ``is not mergeable: the merge commit cannot be cleanly created`` and then
        # the ``--auto`` hint; the refusal keeps only the last line, so the host's own
        # ``mergeable`` (CONFLICTING) is what says it is a conflict
        auto = ('To have the pull request merged after all the requirements have been met, add '
                'the `--auto` flag.')
        sb, wt, _lines = self._merge(auto, lane.DOCS, conflicting=True)
        wt.assert_not_called()
        self.assertEqual(sb.call_args[0][2], 'conflict')

    def test_the_host_reads_conflicting_off_the_pr(self):
        host = lane.GitHubHost.__new__(lane.GitHubHost)
        host.slug = 'o/p'
        for state, want in (('CONFLICTING', True), ('MERGEABLE', False), ('UNKNOWN', False)):
            with mock.patch.object(harvest, '_gh',
                                   return_value=(0, json.dumps({'mergeable': state}), '')):
                self.assertIs(host.conflicting(842), want)
        with mock.patch.object(harvest, '_gh', return_value=(1, '', 'boom')):
            self.assertIs(host.conflicting(842), False)

    def test_any_other_refusal_still_waits(self):
        # a policy refusal on a branch git merges clean: no conflict anywhere, it waits
        sb, wt, _lines = self._merge('To have the merge queue ... base branch policy', lane.DOCS,
                                     files=())
        sb.assert_not_called()
        wt.assert_called_once()
        self.assertIn('merge refused', wt.call_args[0][2])


class CheckCommandsFact(LaneFixture):
    """G2 ask 3: ``branch_facts`` kicks off the product's own ``check_commands`` on the head it
    just read (:meth:`lane.Lane.ensure_check_commands`) — a fact gathered, like any other, that
    no transition reads; the lane never waits on it and the result reaches a brief independently,
    by sha (:mod:`asf.harvest.product_checks`, :func:`asf.briefs.build.checks_section`)."""

    def test_a_product_with_check_commands_starts_one_on_the_head(self):
        p = self.product(check_commands=['echo ok'])
        lane_ = lane.Lane(p, state_dir=self.state_dir, out=lambda *_a: None)
        lane_.trunk_sha = HEAD
        with mock.patch('asf.harvest.product_checks.ensure') as ensure:
            lane_.branch_facts('worker/t', None, HEAD, None, True, False)
        ensure.assert_called_once_with(p, lane_.repo, lane_.state_dir, HEAD, ['echo ok'],
                                       None, dry_run=False)

    def test_a_product_with_no_check_commands_starts_nothing(self):
        p = self.product()
        lane_ = lane.Lane(p, state_dir=self.state_dir, out=lambda *_a: None)
        lane_.trunk_sha = HEAD
        with mock.patch('asf.harvest.product_checks.ensure') as ensure:
            lane_.branch_facts('worker/t', None, HEAD, None, True, False)
        ensure.assert_not_called()

    def test_a_branch_with_no_head_starts_nothing(self):
        p = self.product(check_commands=['echo ok'])
        lane_ = lane.Lane(p, state_dir=self.state_dir, out=lambda *_a: None)
        lane_.trunk_sha = HEAD
        with mock.patch('asf.harvest.product_checks.ensure') as ensure:
            lane_.branch_facts('worker/t', None, None, None, True, False)
        ensure.assert_not_called()


class ReportCommitsAreNoCopies(LaneFixture):
    """F-0278 (a product's T-0659, 2026-10-07): ``git cherry`` matched the branch's EMPTY report
    commits to the empty report commits on the trunk, so the copies check fired on every branch
    carrying one, the lane's naming reword bailed, and each new session's report moved the head
    and dropped the batch. An empty or report commit is never a trunk copy, and never refused
    for naming, in either subject form."""

    B, AUTHOR = NamingRepair.B, NamingRepair.AUTHOR
    tip = NamingRepair.tip
    commit = TrunkCommitsNeverReworded.commit

    def setUp(self):
        super().setUp()
        self.lines = []

    def branch(self, subject):
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'checkout', '-q', '-B', self.B, 'origin/main'], cwd=self.worker)
        self.commit(subject, {'a.txt': 'a\n'})
        for s in ('asf: report coder-t-0001', 'asf(T-0001): report coder-t-0001'):
            sh(['git', 'commit', '-q', '--allow-empty', '-m', s], cwd=self.worker,
               env_=self.AUTHOR)
        sh(['git', 'push', '-q', '-f', 'origin', self.B], cwd=self.worker)
        # meanwhile the trunk landed another session's empty report commit
        sh(['git', 'checkout', '-q', '-B', 'tmp-main', 'origin/main'], cwd=self.worker)
        sh(['git', 'commit', '-q', '--allow-empty', '-m', 'asf: report coder-t-0000'],
           cwd=self.worker, env_=self.AUTHOR)
        sh(['git', 'push', '-q', 'origin', 'tmp-main:main'], cwd=self.worker)
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        return self.tip()

    def test_one_fix_and_two_empty_reports_carry_no_copies(self):
        self.branch('fix(T-0001): the hinge')
        cherry = sh(['git', 'cherry', 'origin/main', f'origin/{self.B}'], cwd=self.repo).stdout
        self.assertEqual(cherry.count('- '), 2, cherry)  # what git itself says: two "copies"
        _base, shas, why = lane.own_commits(self.repo, 'main', self.B)
        self.assertEqual((len(shas or []), why), (3, ''))
        self.assertEqual(lane.trunk_history(self.repo, 'main', self.B), ([], []))
        self.assertEqual(lane.drop_trunk_copies(self.repo, 'main', self.B)['copies'], [])

    def test_the_naming_check_passes_report_commits_in_either_form(self):
        self.branch('fix(T-0001): the hinge')
        self.assertTrue(lane.commits_name_item(self.repo, 'main', self.B, 'T-0001'))
        self.assertIsNone(lane.lane_refusal(self.repo, 'main', self.B, 'T-0001'))
        self.assertFalse(lane.commits_name_item(self.repo, 'main', self.B, 'T-0002'))

    def test_an_unnamed_commit_beside_empty_reports_is_reworded_not_sent_back(self):
        old = self.branch('tidy up')
        self.session('coder-t-0001', 'T-0001', self.B)
        lane.lane_pass(self.product(), self.state_dir, out=self.lines.append)
        new = self.tip()
        self.assertNotEqual(new, old)
        self.assertEqual(sh(['git', 'log', '--format=%s', f'main..{new}'],
                            cwd=self.origin).stdout.splitlines(),
                         ['asf(T-0001): report coder-t-0001'] * 2 + ['task(T-0001): tidy up'])
        self.assertEqual(lifecycle.corrections(os.path.join(self.state_dir, 'sessions.jsonl')),
                         {})
        self.assertFalse(any('back to its session' in l for l in self.lines), self.lines)


class RelaunchOnlyWhatChanged(LaneFixture):
    """F-0275 — a naming or copies refusal is the lane's own mechanics: it never turns a landing
    wait BACK, and once a session answered it on a head and moved nothing, the same head is
    never handed to another session for it."""

    H1 = 'a' * 40

    def registry(self, *lines):
        path = os.path.join(self.state_dir, 'sessions.jsonl')
        with open(path, 'a', encoding='utf-8') as fh:
            for line in lines:
                fh.write(json.dumps(line) + '\n')
        return path

    def test_a_refusal_correction_never_turns_a_landing_wait_back(self):
        wait = rec(lane.GATE, head_at='2027-01-15T08:00:00Z')
        for kind in lane.REFUSAL_KINDS:
            corr = {'kind': kind, 'text': 'x', 'at': '2027-01-15T09:00:00Z'}
            self.assertFalse(lane.correction_turns_back(wait, corr), kind)
        self.assertTrue(lane.correction_turns_back(
            wait, {'kind': 'gate', 'text': 'x', 'at': '2027-01-15T09:00:00Z'}))
        ruling = {'kind': lane.COPIES, 'text': 'x', 'at': '2027-01-15T09:00:00Z',
                  'operator_ruling': True}
        self.assertTrue(lane.correction_turns_back(wait, ruling))

    def held_then_answered(self, judged):
        return self.registry(
            {'job': 'coder-t-0001', 'item': 'T-0001', 'branch': 'worker/T-0001', 'pid': 1,
             'kind': 'coder', 'started': '2026-09-21T00:00:00Z'},
            {'job': 'coder-t-0001', 'ended': '2026-09-21T00:05:00Z', 'end_reason': 'finished',
             'correction': {'kind': 'naming', 'text': 'commits do not name T-0001',
                            'at': '2026-09-21T00:06:00Z', 'judged_head': judged}},
            {'job': 'correct-t-0001', 'item': 'T-0001', 'branch': 'worker/T-0001', 'pid': 2,
             'kind': 'correct', 'started': '2026-09-21T00:10:00Z'},
            {'job': 'correct-t-0001', 'ended': '2026-09-21T00:15:00Z', 'end_reason': 'finished'})

    def test_refused_on_head_reads_an_answered_correction_on_the_same_head(self):
        path = self.held_then_answered(self.H1)
        self.assertTrue(lane.refused_on_head(path, 'T-0001', 'naming', self.H1))
        self.assertFalse(lane.refused_on_head(path, 'T-0001', 'naming', 'b' * 40))
        self.assertFalse(lane.refused_on_head(path, 'T-0001', 'copies', self.H1))
        self.assertFalse(lane.refused_on_head(path, 'T-0002', 'naming', self.H1))

    def test_an_unanswered_correction_is_not_a_repeat(self):
        path = self.registry(
            {'job': 'coder-t-0001', 'item': 'T-0001', 'branch': 'worker/T-0001', 'pid': 1,
             'kind': 'coder', 'started': '2026-09-21T00:00:00Z'},
            {'job': 'coder-t-0001', 'ended': '2026-09-21T00:05:00Z', 'end_reason': 'finished',
             'correction': {'kind': 'naming', 'text': 'x', 'at': '2026-09-21T00:06:00Z',
                            'judged_head': self.H1}})
        self.assertFalse(lane.refused_on_head(path, 'T-0001', 'naming', self.H1))

    def test_enter_back_holds_no_second_session_on_an_unchanged_head(self):
        path = self.held_then_answered(self.H1)
        lines = []
        ln = lane.Lane(self.product(), self.state_dir, out=lines.append, items={})
        run = lifecycle.by_branch(path)['worker/T-0001']
        f = {'branch': 'worker/T-0001', 'item': 'T-0001', 'head': self.H1, 'run': run,
             'prev': rec(lane.PUSHED, head=self.H1), 'kind': 'code', 'correction': None,
             'refusal': ('naming', 'commits do not name T-0001: …')}
        with mock.patch.object(lane, 'hold_with_correction',
                               side_effect=AssertionError('no second session')):
            self.assertIsNone(ln.enter_back(f, 'kind=naming'))
        self.assertEqual(ln.results['worker/T-0001'], 'waiting')
        self.assertTrue(any('not sent back again' in l for l in lines), lines)
        self.assertEqual(self.lane_of('worker/T-0001')['state'], lane.PUSHED)
        # the next pass says nothing new
        lines.clear()
        f['prev'] = self.lane_of('worker/T-0001')
        ln.enter_back(f, 'kind=naming')
        self.assertFalse(any('not sent back again' in l for l in lines), lines)


class ProvesRefusalTests(LaneFixture):
    """F-0040 S-56306: the landing refuses a Task branch that proves no acceptance line of a
    Story it lists."""

    ITEM = 'T-0123'
    STORY = 'S-18750'

    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix='record_')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.makedirs(os.path.join(self.root, 'stories'))
        with open(os.path.join(self.root, 'stories', f'{self.STORY}.md'), 'w',
                  encoding='utf-8') as f:
            f.write('## Acceptance\n- [ ] one\n- [ ] two\n')
        self.story_item = {'type': 'story', 'folder': 'stories', 'id': self.STORY}
        self.task_item = {'type': 'task', 'stories': [self.STORY]}
        self.items = {self.STORY: self.story_item, self.ITEM: self.task_item}

    def push_commits(self, branch, commits):
        """``commits``: ``[(subject, {rel: text})]`` — one commit per pair, oldest first."""
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'checkout', '-q', '-B', branch, 'origin/main'], cwd=self.worker)
        for subject, files in commits:
            for rel, text in files.items():
                self.write(self.worker, rel, text)
            sh(['git', 'add', '-A'], cwd=self.worker)
            sh(['git', 'commit', '-qm', subject], cwd=self.worker, env_=self.ident)
        sh(['git', 'push', '-q', '-f', 'origin', branch], cwd=self.worker)

    def refusal(self, commits, item=None, items=None, root=None):
        branch = f'worker/{item or self.ITEM}'
        self.push_commits(branch, commits)
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        return lane.lane_refusal(self.repo, 'main', branch, item or self.ITEM,
                                 items=self.items if items is None else items,
                                 root=self.root if root is None else root)

    def test_a_branch_with_no_trailer_is_refused(self):
        kind, text = self.refusal([('feat(T-0123): a', {'a.txt': 'a\n'})])
        self.assertEqual(kind, 'proves')
        self.assertIn('no claim', text)
        self.assertIn(self.STORY, text)
        self.assertIn('1 one', text)
        self.assertIn('2 two', text)
        self.assertIn('Proves: <S-id> line <n>', text)

    def test_a_valid_trailer_is_not_refused(self):
        self.assertIsNone(self.refusal(
            [('feat(T-0123): a', {'a.txt': 'a\n'}),
             ('fix(T-0123): b\n\nProves: S-18750 line 1 — tests/test_a.py::T::test_x',
              {'tests/test_a.py': 'x\n'})]))

    def test_a_claim_naming_a_story_the_task_does_not_list_is_refused(self):
        kind, text = self.refusal(
            [('feat(T-0123): a\n\nProves: S-9999 line 1 — tests/test_a.py::T::test_x',
              {'tests/test_a.py': 'x\n'})])
        self.assertEqual(kind, 'proves')
        self.assertIn('unknown story', text)

    def test_a_line_past_the_end_of_the_list_is_refused(self):
        kind, text = self.refusal(
            [('feat(T-0123): a\n\nProves: S-18750 line 7 — tests/test_a.py::T::test_x',
              {'tests/test_a.py': 'x\n'})])
        self.assertEqual(kind, 'proves')
        self.assertIn('no such line', text)

    def test_a_test_path_absent_from_the_tree_is_refused(self):
        kind, text = self.refusal(
            [('feat(T-0123): a\n\nProves: S-18750 line 1 — tests/nope.py::T::test_x',
              {'a.txt': 'a\n'})])
        self.assertEqual(kind, 'proves')
        self.assertIn('no such test', text)

    def test_an_item_that_is_not_a_task_is_not_refused(self):
        self.items[self.ITEM] = {'type': 'bug'}
        self.assertIsNone(self.refusal([('fix(T-0123): a', {'a.txt': 'a\n'})]))

    def test_a_task_listing_no_story_is_not_refused(self):
        self.items[self.ITEM] = {'type': 'task'}
        self.assertIsNone(self.refusal([('feat(T-0123): a', {'a.txt': 'a\n'})]))

    def test_a_call_with_no_items_is_not_refused(self):
        self.assertIsNone(self.refusal([('feat(T-0123): a', {'a.txt': 'a\n'})], items={}))

    def test_a_call_with_no_root_is_not_refused(self):
        self.assertIsNone(self.refusal([('feat(T-0123): a', {'a.txt': 'a\n'})], root=''))

    def test_the_merge_refusal_still_fires_first(self):
        self.push_commits('worker/T-0123', [('feat(T-0123): a', {'a.txt': 'a\n'})])
        sh(['git', 'checkout', '-q', 'main'], cwd=self.worker)
        self.write(self.worker, 'm.txt', 'm\n')
        sh(['git', 'add', '-A'], cwd=self.worker)
        sh(['git', 'commit', '-qm', 'trunk moves'], cwd=self.worker, env_=self.ident)
        sh(['git', 'push', '-q', 'origin', 'main'], cwd=self.worker)
        sh(['git', 'checkout', '-q', 'worker/T-0123'], cwd=self.worker)
        sh(['git', 'merge', '-q', '--no-edit', 'main', '-m', 'fix(T-0123): merge main'],
           cwd=self.worker, env_=self.ident)
        sh(['git', 'push', '-q', '-f', 'origin', 'worker/T-0123'], cwd=self.worker)
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        kind, _text = lane.lane_refusal(self.repo, 'main', 'worker/T-0123', self.ITEM,
                                        items=self.items, root=self.root)
        self.assertEqual(kind, 'merge')

    def test_the_naming_refusal_still_fires_first(self):
        kind, _text = self.refusal([('fix(B-0099): not this item', {'a.txt': 'a\n'})])
        self.assertEqual(kind, lifecycle.NAMING)

    def test_an_existing_four_argument_call_still_works(self):
        self.push_commits('worker/T-0123', [('feat(T-0123): a', {'a.txt': 'a\n'})])
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        self.assertIsNone(lane.lane_refusal(self.repo, 'main', 'worker/T-0123', self.ITEM))


if __name__ == '__main__':
    unittest.main()
