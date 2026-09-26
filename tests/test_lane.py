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
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from asf import env
from asf.feeder import rows as feeder_rows
from asf.harvest import harvest, lane
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


class LaneFixture(unittest.TestCase):
    """A bare origin, the product's checkout, and a worker clone that pushes lane branches."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='lane_')
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
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
        conv = {'test_command': f'{sys.executable} -m unittest discover -s checks -p test_*.py',
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
        cmd = f'LANE_PROBE=on {sys.executable} -m unittest discover -s checks -p test_*.py'
        results = harvest.run_product_harvest(self.product(test_command=cmd), self.state_dir,
                                              out=lambda *_: None)
        self.assertEqual(results, {'worker/T-0001': 'landed'})


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


class RefPushes(LaneFixture):
    """The lane's ref-only pushes (archive, branch delete) leave from a clean checkout detached
    at origin/<trunk>, so the product's pre-push hook runs in full against the trunk's tree —
    never against a product checkout a person left behind or edited. A refused one is loud: a
    line in the tick's output and a row in status and doctor, and the delete is owed and retried."""

    def setUp(self):
        super().setUp()
        self.marker = os.path.join(self.base, 'refuse-delete')
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

    def test_a_refused_archive_holds_the_branch_and_says_why(self):
        self.push_main({'.githooks/pre-push': '#!/bin/sh\necho "claims: bad" >&2\nexit 1\n'},
                       'a hook that refuses all')
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
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
        self.assertIn(f'reworded 2 subjects on {self.B} (naming) — no session', lines)
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
        self.assertEqual(self.log('%s', self.B).splitlines()[0], 'feat(T-0001): later')
        self.assertTrue(any(l.startswith(f'reword {self.B} (naming) push refused') for l in lines),
                        lines)
        held = [l for l in lines if l.startswith(f'held {self.B}: commits do not name T-0001')]
        self.assertTrue(held and held[0].endswith('(naming, no round)'), lines)
        corr = lifecycle.corrections(self.sessions())['T-0001']
        self.assertEqual((corr['kind'], corr['rounds']), (lifecycle.NAMING, 0))
        self.assertEqual(self.lane_of(self.B)['reason'], 'kind=naming')

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
        self.assertIn(f'reworded 1 subjects on {self.B} (naming) — no session', lines)
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
        self.assertIn(f'reworded 1 subjects on {self.B} (naming) — no session', self.lines)
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
        red = [{'name': 'gate', 'bucket': 'pass'}, {'name': 'gate-tests', 'bucket': 'cancel'},
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


if __name__ == '__main__':
    unittest.main()
