"""Work the feeder must never hide (a product's parity ranks 1–4, 2026-10-02).

* past the attempt limit an item is the adjudicate row, or a PARKED row once adjudicated on its
  card — never dropped with no row at all (T-0338, T-0349);
* a landing the lifecycle records is honoured by ``after:`` only when it is the item's own
  (#560: attributable, not a merge parent, not a document lane); one that is not is a NEEDS
  DECISION row, never a silent no-row (T-0091);
* an ``after:`` on a removed card reads its merge survivor, or is dropped when the card was
  removed outright; a survivor removed unlanded is a decision (T-0163 on T-0162);
* a delivery member only the console may edit, or one a cross-delivery cycle defers, never
  holds the delivery's code members (T-0027's delivery, held whole by T-0037).
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from asf.env import Product
from asf.feeder import rows
from asf.tick import step_wave
from asf.workers import landing
from asf.workers import lifecycle as lc


def product(**extra):
    conv = {'branch_prefixes': {'spec': 'spec', 'plan': 'plan', 'task': 'task'}}
    conv.update(extra.pop('conventions', {}))
    return Product('sample', dict({'conventions': conv}, **extra))


def feature(fid='F-0001', **over):
    f = {'id': fid, 'type': 'feature', 'decided': True, 'rank': 1, 'state': 'Active',
         'stage': 'plan-approved'}
    f.update(over)
    return f


def task(tid, **over):
    t = {'id': tid, 'type': 'task', 'parent': 'F-0001', 'state': 'New', 'decided': True,
         'writes': [f'src/{tid.lower()}.py']}
    t.update(over)
    return t


def index(*cards):
    out = {c['id']: c for c in cards}
    for c in cards:
        parent = out.get(c.get('parent'))
        if parent is not None and not c.get('removed'):
            parent.setdefault('children', []).append(c['id'])
    return {'items': out}


def row_of(out, iid):
    mine = [r for r in out if r.item_id == iid]
    return mine[0] if len(mine) == 1 else mine


class RemovedDependencyTest(unittest.TestCase):

    def test_a_chain_of_groom_merges_reads_through_to_the_landed_survivor(self):
        # T-0163 after T-0162; T-0162 merged into T-0159; T-0159 merged into T-0158 (Closed).
        # Neither removed card is done, so neither was in the live map nor in retired_done.
        idx = index(feature(),
                    task('T-0158', state='Closed', merged=['T-0159']),
                    task('T-0159', removed='merged into T-0158 (groom 2026-09-25)',
                         merged=['T-0160', 'T-0162']),
                    task('T-0162', removed='merged into T-0159 (groom 2026-09-25)'),
                    task('T-0163', after=['T-0162']))
        r = row_of(rows.candidates(idx, product(), []), 'T-0163')
        self.assertEqual((r.kind, r.action), (rows.PLAN_CODE, rows.LAUNCH))

    def test_the_removed_line_alone_names_the_survivor(self):
        idx = index(feature(), task('T-0002', state='Active'),
                    task('T-0001', removed='merged into T-0002 (groom 2026-09-25)'),
                    task('T-0003', after=['T-0001']))
        r = row_of(rows.candidates(idx, product(), []), 'T-0003')
        self.assertEqual((r.action, r.waits_on), ('WAITS ON T-0002', 'T-0002'))

    def test_a_card_removed_outright_is_a_dead_edge_and_dropped(self):
        idx = index(feature(), task('T-0001', removed='out of scope (groom 2026-09-25)'),
                    task('T-0002', after=['T-0001']))
        items = rows.items_of(idx)
        self.assertEqual(rows.after_of(items, items['T-0002']), [])
        self.assertEqual(rows.dead_after(items, items['T-0002']),
                         [('T-0001', 'out of scope (groom 2026-09-25)')])
        r = row_of(rows.candidates(idx, product(), []), 'T-0002')
        self.assertEqual((r.kind, r.action), (rows.PLAN_CODE, rows.LAUNCH))

    def test_a_survivor_removed_unlanded_is_a_decision_not_a_wait(self):
        idx = index(feature(),
                    task('T-0002', removed='already on main (groom 2026-09-26)', merged=['T-0001']),
                    task('T-0001', removed='merged into T-0002 (groom 2026-09-25)'),
                    task('T-0003', after=['T-0001']))
        r = row_of(rows.candidates(idx, product(), []), 'T-0003')
        self.assertEqual((r.action, r.waits_on, r.launches),
                         (rows.NEEDS_DECISION, 'decision', False))
        self.assertIn('T-0002 was removed unlanded', r.reason)


class UnverifiedLandingTest(unittest.TestCase):

    def test_an_open_task_landed_on_a_document_lane_is_a_decision_row(self):
        idx = index(feature(), task('T-0091', state='Active', evidence=['branch plan-T-0091']))
        out = rows.candidates(idx, product(), [], occupancy={'landed': {'T-0091': 'abc'}},
                              unverified_landed={'T-0091': 'a plan merged, not its work'})
        r = row_of(out, 'T-0091')
        self.assertEqual((r.action, r.waits_on, r.launches),
                         (rows.NEEDS_DECISION, 'decision', False))
        self.assertIn('a plan merged, not its work', r.reason)

    def test_another_row_speaking_for_it_wins(self):
        idx = index(feature(), task('T-0091', state='Active', evidence=['PR #5 CLOSED']))
        out = rows.candidates(idx, product(), [], unverified_landed={'T-0091': 'x'})
        self.assertEqual([r.kind for r in out if r.item_id == 'T-0091'], [rows.STALE])

    def test_a_verified_landing_releases_the_successors(self):
        idx = index(feature(), task('T-0091', state='Active', evidence=['branch task-T-0091']),
                    task('T-0094', after=['T-0091']))
        held = row_of(rows.candidates(idx, product(), []), 'T-0094')
        self.assertEqual(held.action, 'WAITS ON T-0091')
        free = row_of(rows.candidates(idx, product(), [],
                                      landed_shas={'T-0091': ('a' * 40, 'T-0091: x')}), 'T-0094')
        self.assertEqual(free.action, rows.LAUNCH)


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


class VerifyLandingsTest(unittest.TestCase):
    """#560 on a real repo: only a commit attributable to the item counts."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix='verify-landings-')
        origin, repo = os.path.join(cls.root, 'origin.git'), os.path.join(cls.root, 'repo')
        git(cls.root, 'init', '-q', '--bare', '-b', 'main', origin)
        git(cls.root, 'clone', '-q', origin, repo)
        for k, v in (('user.email', 't@t'), ('user.name', 't'), ('commit.gpgsign', 'false'),
                     ('core.hooksPath', '/dev/null')):
            git(repo, 'config', k, v)
        shas = {}
        for name, subject in (('a.py', 'T-0001: build a'), ('b.py', 'merge-queue: #966 (x)'),
                              ('c.md', 'plan(T-0003): split it')):
            with open(os.path.join(repo, name), 'w') as f:
                f.write(name)
            git(repo, 'add', name)
            git(repo, 'commit', '-q', '-m', subject)
            shas[name] = git(repo, 'rev-parse', 'HEAD')
        git(repo, 'push', '-q', 'origin', 'HEAD:main')
        git(repo, 'fetch', '-q', 'origin')
        cls.repo, cls.shas = repo, shas

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def verify(self, landed, on, cards):
        occ = {'landed': landed, 'landed_on': on}
        items = rows.items_of(index(feature(), *cards))
        return landing.verify_landings(product(), occ, items, path=None, repo=self.repo,
                                       main='main')

    def test_a_commit_naming_the_item_verifies(self):
        got, bad = self.verify({'T-0001': self.shas['a.py']}, {'T-0001': 'task-T-0001'},
                               [task('T-0001', state='Active')])
        self.assertEqual(list(got), ['T-0001'])
        self.assertEqual(got['T-0001'][1], 'T-0001: build a')
        self.assertEqual(bad, {})

    def test_another_prs_merge_queue_commit_does_not(self):
        got, bad = self.verify({'T-0001': self.shas['b.py']}, {'T-0001': 'task-T-0001'},
                               [task('T-0001', state='Active')])
        self.assertEqual(got, {})
        self.assertIn('is not its commit', bad['T-0001'])

    def test_a_document_lane_landing_never_does(self):
        got, bad = self.verify({'T-0003': self.shas['c.md']}, {'T-0003': 'plan/T-0003'},
                               [task('T-0003', state='Active')])
        self.assertEqual(got, {})
        self.assertIn('plan lane', bad['T-0003'])

    def test_a_closed_card_and_an_unfetched_sha_claim_nothing(self):
        got, bad = self.verify({'T-0001': self.shas['a.py'], 'T-0002': 'f' * 40},
                               {'T-0001': 'task-T-0001', 'T-0002': 'task-T-0002'},
                               [task('T-0001', state='Closed'), task('T-0002', state='Active')])
        self.assertEqual((got, bad), ({}, {}))


class LandedOnTest(unittest.TestCase):

    def test_occupancy_names_the_branch_a_landing_was_recorded_on(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, 'sessions.jsonl')
        with open(path, 'w') as f:
            for ln in ({'job': 'reshape-t-0091', 'pid': 1, 'started': 't1', 'item': 'T-0091',
                        'kind': 'reshape', 'branch': 'plan-T-0091'},
                       {'job': 'reshape-t-0091', 'ended': 't2', 'end_reason': 'finished',
                        'harvested': 'a' * 40}):
                f.write(json.dumps(ln) + '\n')
        out = lc.occupancy(path, alive=lambda _pid: False)
        self.assertEqual(out['landed'], {'T-0091': 'a' * 40})
        self.assertEqual(out['landed_on'], {'T-0091': 'plan-T-0091'})


class AdjudicationsTest(unittest.TestCase):

    def test_the_newest_adjudicate_run_on_the_card_as_it_stands(self):
        from unittest import mock
        runs = {'adjudicate-t-0338': [
            {'item': 'T-0338', 'kind': 'adjudicate', 'started': '2026-09-26T14:00:00Z',
             'ended': 'x', 'card_digest': 'old'},
            {'item': 'T-0338', 'kind': 'adjudicate', 'started': '2026-09-26T15:00:00Z',
             'ended': 'x', 'card_digest': 'now'}],
            'correct-t-0001': [{'item': 'T-0001', 'kind': 'adjudicate', 'ended': 'x',
                                'started': 't'}]}
        p = product()
        with mock.patch.object(step_wave.lifecycle, 'runs', return_value=runs), \
                mock.patch.object(step_wave.pool_mod, 'sessions_path', return_value='/x'), \
                mock.patch('asf.briefs.build.card_digest', return_value='now'):
            got = step_wave.adjudications(p, {'items': {}}, {'T-0338': 51, 'T-0001': 2})
        self.assertEqual(got, {'T-0338': {'runs': 2, 'at': '2026-09-26T15:00:00Z',
                                          'same_card': True}})
        with mock.patch.object(step_wave.lifecycle, 'runs', return_value=runs), \
                mock.patch.object(step_wave.pool_mod, 'sessions_path', return_value='/x'), \
                mock.patch('asf.briefs.build.card_digest', return_value='edited'):
            got = step_wave.adjudications(p, {'items': {}}, {'T-0338': 51})
        self.assertFalse(got['T-0338']['same_card'])


class DeliveryLeftOutTest(unittest.TestCase):
    """T-0027's delivery: T-0027, T-0030, T-0032, T-0037 — only T-0037 (the close-out, after
    T-0032, writing a path in the amendable set) needs the console."""

    def cards(self, **extra):
        lead = task('T-0027', stage='plan-approved', state='Active',
                    delivers=['T-0027', 'T-0030', 'T-0032', 'T-0037'])
        return [feature(), lead,
                task('T-0030', delivered_by='T-0027', after=['T-0027']),
                task('T-0032', delivered_by='T-0027', after=['T-0030']),
                task('T-0037', delivered_by='T-0027', after=['T-0032'],
                     writes=['asf/briefs/templates/groom.md'])]

    def test_the_console_member_is_left_out_and_waits_on_its_predecessor(self):
        out = rows.candidates(index(*self.cards()), product(), [])
        lead, close = row_of(out, 'T-0027'), row_of(out, 'T-0037')
        self.assertEqual((lead.kind, lead.action), (rows.DELIVERY_CODE, rows.LAUNCH))
        self.assertIn('3 Tasks', lead.reason)
        self.assertEqual((close.kind, close.action, close.launches, close.amend),
                         (rows.CONSOLE_AMEND, 'WAITS ON T-0032', False, ''))
        items = rows.items_of(index(*self.cards()))
        self.assertNotIn('asf/briefs/templates/groom.md',
                         rows._delivery_union(items, items['T-0027'], ['T-0037']))

    def test_once_its_predecessor_landed_it_is_the_consoles_edit(self):
        cards = self.cards()
        for c in cards:
            if c['id'] in ('T-0030', 'T-0032'):
                c['state'] = 'Closed'
        r = row_of(rows.candidates(index(*cards), product(), []), 'T-0037')
        self.assertEqual((r.kind, r.waits_on), (rows.CONSOLE_AMEND, 'console'))
        self.assertTrue(r.amend)

    def test_a_waiting_console_member_is_not_announced(self):
        from asf import approvals
        from unittest import mock
        out = rows.candidates(index(*self.cards()), product(), [])
        said = []
        with mock.patch.object(approvals, 'read', return_value=[]), \
                mock.patch.object(approvals, 'append'):
            got = approvals.announce_console_amends(product(), out, said.append)
        self.assertEqual((got, said), ([], []))

    def test_a_cross_delivery_cycle_defers_the_member_not_the_delivery(self):
        # T-0030 after T-0456; T-0456 (another delivery's lead) after T-0027 — the lead of this
        # one. Held whole, neither delivery could ever start.
        cards = self.cards()
        cards[2]['after'] = ['T-0027', 'T-0456']
        cards.append(task('T-0456', after=['T-0027'], delivers=['T-0456'],
                          stage='plan-approved'))
        out = rows.candidates(index(*cards), product(), [])
        self.assertEqual(row_of(out, 'T-0027').action, rows.LAUNCH)
        self.assertIn('1 Tasks', row_of(out, 'T-0027').reason)
        self.assertEqual((row_of(out, 'T-0030').kind, row_of(out, 'T-0030').action),
                         (rows.PLAN_CODE, 'WAITS ON T-0027'))
        self.assertEqual(row_of(out, 'T-0032').action, 'WAITS ON T-0030')
        items = rows.items_of(index(*cards))
        self.assertEqual(rows.left_out(product(), items, 'T-0027'),
                         (['T-0037'], ['T-0030', 'T-0032']))

    def test_a_held_delivery_claims_no_footprint(self):
        # a delivery an unlanded after: holds must not hold, by its union, a Task beside it
        cards = self.cards()
        cards[1]['after'] = ['T-0900']
        cards.append(task('T-0900', state='Active', writes=['z.py'],
                          evidence=['PR #9 OPEN']))
        cards.append(task('T-0200', writes=['src/t-0030.py']))
        out = rows.candidates(index(*cards), product(), [])
        self.assertEqual(row_of(out, 'T-0027').action, 'WAITS ON T-0900')
        self.assertEqual(row_of(out, 'T-0200').action, rows.LAUNCH)


if __name__ == '__main__':
    unittest.main()


class DeadAfterHistoryTest(unittest.TestCase):
    """Ingest records, once per Task, why it stopped waiting on a removed card."""

    def record(self, cards):
        root = tempfile.mkdtemp(prefix='dead-after-')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        os.makedirs(os.path.join(root, 'tasks'))
        for c in cards:
            fm = '\n'.join(f'{k}: {json.dumps(v)}' for k, v in c.items())
            with open(os.path.join(root, 'tasks', f"{c['id']}.md"), 'w') as f:
                f.write(f"---\n{fm}\n---\n\n# {c['id']}\n\n## History\n\n- 2026-09-01 made\n")
        return root

    def canonical(self, root):
        from asf.record import core
        return core.canonicalize(core.load_items(root)[0])[0]

    def test_a_chain_and_a_drop_are_each_said_once(self):
        from asf.record import ingest
        root = self.record([
            {'id': 'T-0158', 'type': 'task', 'title': 'a', 'state': 'Closed', 'merged': ['T-0159']},
            {'id': 'T-0159', 'type': 'task', 'title': 'b',
             'removed': 'merged into T-0158 (groom 2026-09-25)', 'merged': ['T-0162']},
            {'id': 'T-0162', 'type': 'task', 'title': 'c',
             'removed': 'merged into T-0159 (groom 2026-09-25)'},
            {'id': 'T-0170', 'type': 'task', 'title': 'd', 'removed': 'out of scope (groom)'},
            {'id': 'T-0163', 'type': 'task', 'title': 'e', 'after': ['T-0162', 'T-0170']}])
        lines = ingest.dead_after_lines(self.canonical(root))
        self.assertEqual(lines, {'T-0163': [
            'after: T-0162 removed (merged into T-0159 (groom 2026-09-25)) — read as T-0158 '
            '(via T-0159)',
            'after: T-0170 removed (out of scope (groom)) — dropped, its work is not coming']})
        for _ in range(2):
            ingest.note_dead_after(self.canonical(root), '2026-10-02T10:00:00Z')
        with open(os.path.join(root, 'tasks', 'T-0163.md')) as f:
            body = f.read()
        self.assertEqual(body.count('ingest: after: T-0162 removed'), 1)
        self.assertEqual(body.count('ingest: after: T-0170 removed'), 1)
        self.assertIn('- 2026-10-02 10:00 ingest: after: T-0170', body)
