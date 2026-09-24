import contextlib
import io
import json
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from asf.record import check
from asf.record import frontmatter
from asf.record import ingest
from asf.record.core import today
from asf.record.index import do_index

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

EMPTY_EV = {
    'features': {}, 'stories': {}, 'prod_sha': None, 'dev_sha': None,
    'checked': set(), 'main_sha': None, 'merged': {}, 'branches': [],
}


def make_repo():
    root = tempfile.mkdtemp(prefix='ingest_test_')
    for f in FOLDERS:
        os.makedirs(os.path.join(root, f))
    return root


def write(root, id_, type_, title, folder, parent=None, typed_lines=(),
         machine_lines=('state: New', 'stage_since: 2026-01-01T00:00:00Z',
                        'updated: 2026-01-01T00:00:00Z'), body=None):
    lines = [f"id: {id_}", f"type: {type_}", f"title: {title}"]
    if parent:
        lines.append(f"parent: {parent}")
    lines.extend(typed_lines)
    lines.append('# ---- machine ----')
    lines.extend(machine_lines)
    header = '\n'.join(lines)
    text = f"---\n{header}\n---\n{body if body is not None else DEFAULT_BODY}"
    path = os.path.join(root, folder, f"{id_}.md")
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    return path


def read_meta(root, folder, id_):
    text = open(os.path.join(root, folder, f"{id_}.md"), encoding='utf-8').read()
    return frontmatter.parse(text, path=f"{folder}/{id_}.md")


class ProductionIsTheProductsOwn(unittest.TestCase):
    """B-0077/B-0078: what "in production" means is the product's own. A product that configures
    a deploy sha waits for the merge to reach it and for the operator's tick; a product that
    configures none — a package, a library, a tool — has its trunk as production, so a green
    trunk is the close. Without this no Feature of such a product could leave `Resolved`."""

    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def run_ingest(self, ev):
        with mock.patch.object(ingest.evidence, 'load', return_value=ev):
            return ingest.cmd_ingest(types.SimpleNamespace(fresh=False), self.root)

    def landed(self, iid, sha):
        return {iid: {'branches': [], 'open_prs': [], 'commit': sha, 'pr': None, 'green': True}}

    def feature_with_a_closed_task(self):
        write(self.root, 'F-0001', 'feature', 'The record', 'features')
        write(self.root, 'T-0001', 'task', 'Write it', 'tasks', parent='F-0001')

    def test_a_feature_whose_tasks_landed_closes_when_nothing_is_deployed(self):
        self.feature_with_a_closed_task()
        ev = dict(EMPTY_EV, ci=True, ids={**self.landed('T-0001', 'a' * 40),
                                          **self.landed('F-0001', 'b' * 40)})
        self.assertEqual(self.run_ingest(ev), 0)
        meta, _b = read_meta(self.root, 'features', 'F-0001')
        self.assertEqual(meta['state'], 'Closed')

    def test_a_deploy_still_gates_that_close(self):
        self.feature_with_a_closed_task()
        ev = dict(EMPTY_EV, ci=True, prod_sha='c' * 40,
                  ids={**self.landed('T-0001', 'a' * 40), **self.landed('F-0001', 'b' * 40)})
        self.assertEqual(self.run_ingest(ev), 0)
        meta, _b = read_meta(self.root, 'features', 'F-0001')
        self.assertEqual(meta['state'], 'Resolved')

    def test_a_feature_with_no_tasks_closes_on_a_green_trunk(self):
        # B-0078: ingest threw away the state it had just derived and hardcoded `Resolved`
        write(self.root, 'F-0002', 'feature', 'One property test per failure class', 'features')
        ev = dict(EMPTY_EV, ci=True, ids=self.landed('F-0002', 'd' * 40))
        self.assertEqual(self.run_ingest(ev), 0)
        meta, _b = read_meta(self.root, 'features', 'F-0002')
        self.assertEqual(meta['state'], 'Closed')
        self.assertEqual(meta['stage'], 'landed')

    def test_a_feature_with_no_tasks_waits_for_the_deploy_when_there_is_one(self):
        write(self.root, 'F-0003', 'feature', 'The checkout page', 'features')
        ev = dict(EMPTY_EV, ci=True, prod_sha='e' * 40, ids=self.landed('F-0003', 'd' * 40))
        self.assertEqual(self.run_ingest(ev), 0)
        meta, _b = read_meta(self.root, 'features', 'F-0003')
        self.assertEqual(meta['state'], 'Resolved')

    def test_the_ladder_says_landed_not_on_prod_when_nothing_is_deployed(self):
        # "on prod" names a deployment; a product that deploys nothing has none to name
        self.feature_with_a_closed_task()
        ev = dict(EMPTY_EV, ci=True, ids={**self.landed('T-0001', 'a' * 40),
                                          **self.landed('F-0001', 'b' * 40)})
        self.assertEqual(self.run_ingest(ev), 0)
        meta, _b = read_meta(self.root, 'features', 'F-0001')
        self.assertEqual(meta['stage'], 'landed')


class LandingOutranksThePlan(unittest.TestCase):
    """B-0074: a Task listed in its plan's table stayed Active although its commit was on main
    with a green run — the plan's `branch … exists` line won. A landing outranks every source."""

    def evidence(self, **ids):
        return {'features': {}, 'ids': ids}

    def task_card(self, tid, plan='p'):
        return {'id': tid, 'type': 'task', 'title': tid, 'links': {'plan': f'plans/{plan}.md'}}

    def test_a_landed_task_closes_even_when_its_plan_lists_the_branch(self):
        from asf.record import ingest
        ev = self.evidence(**{'T-0010': {'branches': ['task/T-0010'], 'commit': 'cada650' * 6,
                                         'green': True, 'open_prs': [], 'pr': None}})
        state, lines = ingest.match_ids('T-0010', ev)
        self.assertEqual(state, 'Closed')
        self.assertTrue(any('names T-0010' in l for l in lines), lines)

    def test_a_task_with_only_a_branch_is_active(self):
        from asf.record import ingest
        ev = self.evidence(**{'T-0011': {'branches': ['task/T-0011'], 'commit': None,
                                         'green': False, 'open_prs': [], 'pr': None}})
        state, _ = ingest.match_ids('T-0011', ev)
        self.assertEqual(state, 'Active')


class MatchFeatureTests(unittest.TestCase):
    def _meta_from(self, typed_lines):
        lines = ['id: F-0001', 'type: feature', 'title: Free plan'] + list(typed_lines) + \
                ['# ---- machine ----', 'state: New']
        text = '---\n' + '\n'.join(lines) + '\n---\nbody\n'
        meta, _body = frontmatter.parse(text, path='F-0001.md')
        return meta

    def test_matches_by_spec_path(self):
        meta = self._meta_from(['links:', '  spec: docs/superpowers/specs/x.md'])
        ev = dict(EMPTY_EV, features={'free-plan': {
            'alias': None, 'spec': 'origin/main:docs/superpowers/specs/x.md',
            'spec_branch': None, 'plan': None, 'plan_branch': None, 'tasks': {}, 'prs': [],
        }})
        slug, fev = ingest.match_feature(meta, ev)
        self.assertEqual(slug, 'free-plan')
        self.assertIs(fev, ev['features']['free-plan'])

    def test_b0059_matches_by_own_id_as_the_slug(self):
        # the lane's own convention: spec/F-0001 lands docs/specs/f-0001.md; no typed link needed
        meta = self._meta_from([])
        ev = dict(EMPTY_EV, features={'f-0001': {
            'alias': None, 'spec': 'origin/main:docs/specs/f-0001.md',
            'spec_branch': None, 'plan': None, 'plan_branch': None, 'tasks': {}, 'prs': [],
        }})
        slug, fev = ingest.match_feature(meta, ev)
        self.assertEqual(slug, 'f-0001')
        self.assertIs(fev, ev['features']['f-0001'])

    def test_matches_by_legacy_id_alias(self):
        meta = self._meta_from(['legacy_id: FREE-1'])
        ev = dict(EMPTY_EV, features={'free-plan': {
            'alias': 'FREE-1', 'spec': None, 'spec_branch': None, 'plan': None,
            'plan_branch': None, 'tasks': {}, 'prs': [],
        }})
        slug, fev = ingest.match_feature(meta, ev)
        self.assertEqual(slug, 'free-plan')

    def test_matches_by_branches(self):
        meta = self._meta_from(['links:', '  branches: [cloud/spec-free-plan]'])
        ev = dict(EMPTY_EV, features={'free-plan': {
            'alias': None, 'spec': None, 'spec_branch': 'cloud/spec-free-plan',
            'plan_branch': None, 'plan': None, 'tasks': {}, 'prs': [],
        }})
        slug, _fev = ingest.match_feature(meta, ev)
        self.assertEqual(slug, 'free-plan')

    def test_matches_by_prs(self):
        meta = self._meta_from(['links:', '  prs: [601]'])
        ev = dict(EMPTY_EV, features={'free-plan': {
            'alias': None, 'spec': None, 'spec_branch': None, 'plan_branch': None,
            'plan': None, 'tasks': {}, 'prs': [601, 623],
        }})
        slug, _fev = ingest.match_feature(meta, ev)
        self.assertEqual(slug, 'free-plan')

    def test_no_match_returns_none(self):
        meta = self._meta_from(['legacy_id: SOMETHING-ELSE'])
        slug, fev = ingest.match_feature(meta, EMPTY_EV)
        self.assertIsNone(slug)
        self.assertIsNone(fev)


class MatchTaskTests(unittest.TestCase):
    def _meta_from(self, typed_lines, id_='T-0001'):
        lines = [f'id: {id_}', 'type: task', 'title: Wire it'] + list(typed_lines) + \
                ['# ---- machine ----', 'state: New']
        text = '---\n' + '\n'.join(lines) + '\n---\nbody\n'
        meta, _body = frontmatter.parse(text, path=f'{id_}.md')
        return meta

    def test_matches_by_legacy_plan_task_token(self):
        meta = self._meta_from(['legacy_id: FREE-1/T3'])
        ev = dict(EMPTY_EV, features={'free-plan': {
            'alias': 'FREE-1', 'tasks': {'T3': {'branch': 'cloud/free-plan-t3', 'pr': None,
                                                'pr_state': None, 'merged_sha': None}},
        }})
        slug, tid, tev = ingest.match_task(meta, ev)
        self.assertEqual((slug, tid), ('free-plan', 'T3'))
        self.assertEqual(tev['branch'], 'cloud/free-plan-t3')

    def test_matches_by_own_id_in_branch_name(self):
        meta = self._meta_from([], id_='T-0913')
        ev = dict(EMPTY_EV, features={'free-plan': {
            'alias': None, 'tasks': {'T1': {'branch': 'cloud/hotfix-t-0913', 'pr': None,
                                            'pr_state': None, 'merged_sha': None}},
        }})
        slug, tid, tev = ingest.match_task(meta, ev)
        self.assertEqual((slug, tid), ('free-plan', 'T1'))

    def test_matches_by_links_branches(self):
        meta = self._meta_from(['links:', '  branches: [cloud/free-plan-t1]'])
        ev = dict(EMPTY_EV, features={'free-plan': {
            'alias': None, 'tasks': {'T1': {'branch': 'cloud/free-plan-t1', 'pr': None,
                                            'pr_state': None, 'merged_sha': None}},
        }})
        slug, tid, tev = ingest.match_task(meta, ev)
        self.assertEqual((slug, tid), ('free-plan', 'T1'))

    def test_no_match(self):
        meta = self._meta_from([])
        slug, tid, tev = ingest.match_task(meta, EMPTY_EV)
        self.assertIsNone(slug)
        self.assertIsNone(tid)
        self.assertIsNone(tev)


class MatchStoryTests(unittest.TestCase):
    def test_matches_by_legacy_id(self):
        lines = ['id: S-0001', 'type: story', 'title: Sign up', 'legacy_id: F-ID-1',
                 '# ---- machine ----', 'state: New']
        text = '---\n' + '\n'.join(lines) + '\n---\nbody\n'
        meta, _body = frontmatter.parse(text, path='S-0001.md')
        ev = dict(EMPTY_EV, stories={'F-ID-1': {'status': 'todo'}})
        legacy, sev = ingest.match_story(meta, ev)
        self.assertEqual(legacy, 'F-ID-1')
        self.assertEqual(sev['status'], 'todo')


class MatchBugTests(unittest.TestCase):
    def _meta_from(self, typed_lines):
        lines = ['id: B-0001', 'type: bug', 'title: Something broke'] + list(typed_lines) + \
                ['# ---- machine ----', 'state: New']
        text = '---\n' + '\n'.join(lines) + '\n---\nbody\n'
        meta, _body = frontmatter.parse(text, path='B-0001.md')
        return meta

    def test_no_links_no_match(self):
        meta = self._meta_from([])
        self.assertIsNone(ingest.match_bug(meta, EMPTY_EV))

    def test_open_pr_is_fixer_evidence_no_merge(self):
        meta = self._meta_from(['links:', '  prs: [701]'])
        ev = dict(EMPTY_EV, merged={})
        bev = ingest.match_bug(meta, ev)
        self.assertTrue(bev['has_fixer'])
        self.assertIsNone(bev['merged_sha'])

    def test_merged_pr_carries_sha(self):
        meta = self._meta_from(['links:', '  prs: [701]'])
        ev = dict(EMPTY_EV, merged={701: 'deadbeef'})
        bev = ingest.match_bug(meta, ev)
        self.assertEqual(bev['merged_sha'], 'deadbeef')


class IngestFieldsTests(unittest.TestCase):
    def test_no_change_is_a_true_no_op(self):
        machine = {'state': 'New', 'stage_since': '2026-01-01T00:00:00Z',
                  'updated': '2026-01-01T00:00:00Z'}
        ordered, history = ingest._ingest_fields(machine, 'New', None, None, (False, []),
                                                  '2026-01-01T00:00:00Z')
        self.assertIsNone(ordered)
        self.assertEqual(history, [])

    def test_state_change_bumps_stage_since_and_updated_and_writes_history(self):
        machine = {'state': 'New', 'stage_since': '2026-01-01T00:00:00Z',
                  'updated': '2026-01-01T00:00:00Z'}
        ordered, history = ingest._ingest_fields(
            machine, 'Active', None, ['branch cloud/x exists'], (False, []),
            '2026-02-02T00:00:00Z')
        self.assertEqual(ordered['state'], 'Active')
        self.assertEqual(ordered['stage_since'], '2026-02-02T00:00:00Z')
        self.assertEqual(ordered['updated'], '2026-02-02T00:00:00Z')
        self.assertEqual(len(history), 1)
        self.assertIn('state New → Active', history[0])
        self.assertIn('branch cloud/x exists', history[0])

    def test_blocked_true_adds_keys_blocked_false_omits_them(self):
        machine = {'state': 'New', 'stage_since': 'x', 'updated': 'x'}
        ordered, _h = ingest._ingest_fields(machine, 'New', None, None, (True, ['F-0051']), 'x')
        self.assertEqual(ordered['blocked'], True)
        self.assertEqual(ordered['blocked_by_open'], ['F-0051'])

        machine2 = {'state': 'New', 'stage_since': 'x', 'updated': 'x', 'blocked': True,
                   'blocked_by_open': ['F-0051']}
        ordered2, _h2 = ingest._ingest_fields(machine2, 'New', None, None, (False, []), 'y')
        self.assertNotIn('blocked', ordered2)
        self.assertNotIn('blocked_by_open', ordered2)

    def test_cost_is_preserved_untouched(self):
        machine = {'state': 'New', 'stage_since': 'x', 'updated': 'x',
                  'cost': {'sessions': 3, 'usd': 12.5}}
        ordered, _h = ingest._ingest_fields(machine, 'Active', None, ['x'], (False, []), 'y')
        self.assertEqual(ordered['cost'], {'sessions': 3, 'usd': 12.5})

    def test_stage_change_writes_its_own_history_line(self):
        machine = {'state': 'Active', 'stage': 'spec-draft', 'stage_since': 'x', 'updated': 'x'}
        ordered, history = ingest._ingest_fields(
            machine, 'Active', 'spec-approved', ['spec approved r1'], (False, []), 'y')
        self.assertEqual(ordered['stage'], 'spec-approved')
        self.assertEqual(len(history), 1)
        self.assertIn('stage spec-draft → spec-approved', history[0])


class AppendHistoryLinesTests(unittest.TestCase):
    def test_appends_after_existing_lines(self):
        body = ("## Description\n\n## History\n- 2026-01-01: created\n\n## Children\n\n"
                "## Backlinks\n")
        new_body = ingest.append_history_lines(body, ['- 2026-02-02 09:00 ingest: state New → Active (x)'])
        self.assertIn('- 2026-01-01: created\n- 2026-02-02 09:00 ingest:', new_body)
        self.assertIn('## Children', new_body)

    def test_no_lines_is_identity(self):
        body = "## History\n- x\n\n## Children\n"
        self.assertEqual(ingest.append_history_lines(body, []), body)


class CmdIngestEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def run_ingest(self, ev):
        with mock.patch.object(ingest.evidence, 'load', return_value=ev):
            return ingest.cmd_ingest(types.SimpleNamespace(fresh=False), self.root)

    def test_id_token_fills_an_unmatched_task(self):
        write(self.root, 'T-0002', 'task', 'Wire it', 'tasks')
        ev = dict(EMPTY_EV, ids={'T-0002': {'branches': ['worker/T-0002-wire'], 'open_prs': [],
                                            'commit': None, 'pr': None, 'green': False}})
        self.assertEqual(self.run_ingest(ev), 0)
        meta, _body = read_meta(self.root, 'tasks', 'T-0002')
        self.assertEqual(meta['state'], 'Active')
        # the rule that decided is always the last line (§2.3)
        self.assertEqual(meta['evidence'], ['branch worker/T-0002-wire', 'rule: in-flight'])

    def test_a_landing_outranks_a_legacy_plan_match(self):
        # B-0074 (was: `id_token_never_overrides_a_legacy_match`): the plan's task table says what
        # was intended, the commit says what landed. A commit naming the item on the trunk, with a
        # green run where there is CI, is the strongest evidence there is — for every type. The
        # plan's line stays as context so the reader still sees where the Task belongs.
        write(self.root, 'T-0001', 'task', 'Wire it', 'tasks', typed_lines=['legacy_id: FREE-1/T3'])
        ev = dict(EMPTY_EV, features={'free-plan': {
            'alias': 'FREE-1', 'spec': None, 'spec_branch': None, 'plan': None,
            'plan_branch': None, 'prs': [],
            'tasks': {'T3': {'branch': None, 'pr': None, 'pr_state': None, 'review': None,
                             'merged_sha': None, 'landed_no_branch': False}},
        }}, ids={'T-0001': {'branches': [], 'open_prs': [], 'commit': 'abcdef0123',
                            'pr': None, 'green': True}}, ci=None)
        self.assertEqual(self.run_ingest(ev), 0)
        meta, _body = read_meta(self.root, 'tasks', 'T-0001')
        self.assertEqual(meta['state'], 'Closed')
        self.assertEqual(meta['evidence'][0], 'commit abcdef0 names T-0001')
        self.assertIn('in plan free-plan (T3)', meta['evidence'])

    def test_unmatched_feature_gets_no_evidence_line(self):
        write(self.root, 'E-0001', 'epic', 'Factory', 'epics')
        write(self.root, 'F-0001', 'feature', 'Free plan', 'features', parent='E-0001')
        self.assertEqual(self.run_ingest(EMPTY_EV), 0)
        meta, _body = read_meta(self.root, 'features', 'F-0001')
        self.assertEqual(len(meta['evidence']), 2)
        self.assertIn('no evidence found', meta['evidence'][0])
        self.assertEqual(meta['evidence'][-1], 'rule: card')
        self.assertEqual(meta['state'], 'New')

    def test_feature_matches_by_legacy_id_and_goes_active(self):
        write(self.root, 'E-0001', 'epic', 'Factory', 'epics')
        write(self.root, 'F-0001', 'feature', 'Free plan', 'features', parent='E-0001',
              typed_lines=['legacy_id: FREE-1'])
        ev = dict(EMPTY_EV, features={'free-plan': {
            'alias': 'FREE-1', 'spec': 'origin/main:docs/superpowers/specs/x.md',
            'spec_branch': None, 'spec_on_main': True, 'spec_review': None,
            'plan': None, 'plan_branch': None, 'plan_on_main': False, 'plan_review': None,
            'tasks': {}, 'prs': [],
        }})
        self.assertEqual(self.run_ingest(ev), 0)
        meta, body = read_meta(self.root, 'features', 'F-0001')
        self.assertEqual(meta['state'], 'Active')
        self.assertEqual(meta['stage'], 'spec-approved')  # B-0059: landed on the trunk = approved
        self.assertIn('spec on origin/main', meta['evidence'])
        self.assertIn('ingest: state New → Active', body)

    def test_b0059_a_spec_landed_by_the_lane_is_approved_and_the_plan_lands_the_same_way(self):
        write(self.root, 'E-0001', 'epic', 'Factory', 'epics')
        write(self.root, 'F-0001', 'feature', 'Free plan', 'features', parent='E-0001')
        fev = {'alias': None, 'spec': 'origin/main:docs/specs/f-0001.md', 'spec_branch': None,
               'spec_on_main': True, 'spec_review': None, 'plan': None, 'plan_branch': None,
               'plan_on_main': False, 'plan_review': None, 'tasks': {}, 'prs': []}
        self.assertEqual(self.run_ingest(dict(EMPTY_EV, features={'f-0001': fev})), 0)
        meta, _body = read_meta(self.root, 'features', 'F-0001')
        self.assertEqual((meta['state'], meta['stage']), ('Active', 'spec-approved'))
        fev.update(plan='origin/main:docs/plans/f-0001.md', plan_on_main=True)
        self.assertEqual(self.run_ingest(dict(EMPTY_EV, features={'f-0001': fev})), 0)
        meta, _body = read_meta(self.root, 'features', 'F-0001')
        self.assertEqual((meta['state'], meta['stage']), ('Active', 'plan-approved'))

    def test_b0059_a_matched_feature_with_no_tasks_lands_on_a_code_commit_naming_it(self):
        write(self.root, 'E-0001', 'epic', 'Factory', 'epics')
        write(self.root, 'F-0001', 'feature', 'Free plan', 'features', parent='E-0001')
        fev = {'alias': None, 'spec': 'origin/main:docs/specs/f-0001.md', 'spec_branch': None,
               'spec_on_main': True, 'spec_review': None, 'plan': None, 'plan_branch': None,
               'plan_on_main': False, 'plan_review': None, 'tasks': {}, 'prs': []}
        ev = dict(EMPTY_EV, features={'f-0001': fev},
                  ids={'F-0001': {'branches': [], 'open_prs': [], 'commit': 'abc1234def', 'pr': None,
                                  'green': False}})
        self.assertEqual(self.run_ingest(ev), 0)
        meta, _body = read_meta(self.root, 'features', 'F-0001')
        self.assertEqual((meta['state'], meta['stage']), ('Resolved', 'landed'))

    def test_second_run_with_same_evidence_is_a_byte_no_op(self):
        write(self.root, 'E-0001', 'epic', 'Factory', 'epics')
        write(self.root, 'F-0001', 'feature', 'Free plan', 'features', parent='E-0001',
              typed_lines=['legacy_id: FREE-1'])
        ev = dict(EMPTY_EV, features={'free-plan': {
            'alias': 'FREE-1', 'spec': 'origin/main:docs/superpowers/specs/x.md',
            'spec_branch': None, 'spec_on_main': True, 'spec_review': None,
            'plan': None, 'plan_branch': None, 'plan_on_main': False, 'plan_review': None,
            'tasks': {}, 'prs': [],
        }})
        self.run_ingest(ev)
        snapshot = {}
        for f in FOLDERS:
            d = os.path.join(self.root, f)
            for name in os.listdir(d):
                snapshot[name] = open(os.path.join(d, name), encoding='utf-8').read()
        idx1 = open(os.path.join(self.root, 'index.json'), encoding='utf-8').read()
        self.run_ingest(ev)
        idx2 = open(os.path.join(self.root, 'index.json'), encoding='utf-8').read()
        for f in FOLDERS:
            d = os.path.join(self.root, f)
            for name in os.listdir(d):
                self.assertEqual(snapshot[name], open(os.path.join(d, name), encoding='utf-8').read(), name)
        self.assertEqual(idx1.split('"generated"')[1], idx2.split('"generated"')[1])

    def test_epic_with_no_children_and_no_upstream_change_is_untouched(self):
        write(self.root, 'E-0009', 'epic', 'Factory', 'epics', typed_lines=['legacy_id: GOAL 9'],
              machine_lines=('schema_version: 1', 'state: New', 'stage_since: 2026-01-01T00:00:00Z',
                             'updated: 2026-01-01T00:00:00Z'))
        before = open(os.path.join(self.root, 'epics', 'E-0009.md'), encoding='utf-8').read()
        self.run_ingest(EMPTY_EV)
        after = open(os.path.join(self.root, 'epics', 'E-0009.md'), encoding='utf-8').read()
        self.assertEqual(before, after)

    def test_epic_goes_active_when_child_feature_active(self):
        write(self.root, 'E-0001', 'epic', 'Factory', 'epics')
        write(self.root, 'F-0001', 'feature', 'Free plan', 'features', parent='E-0001',
              typed_lines=['legacy_id: FREE-1'])
        ev = dict(EMPTY_EV, features={'free-plan': {
            'alias': 'FREE-1', 'spec': 'origin/main:docs/superpowers/specs/x.md',
            'spec_branch': None, 'spec_on_main': True, 'spec_review': None,
            'plan': None, 'plan_branch': None, 'plan_on_main': False, 'plan_review': None,
            'tasks': {}, 'prs': [],
        }})
        self.run_ingest(ev)
        meta, _body = read_meta(self.root, 'epics', 'E-0001')
        self.assertEqual(meta['state'], 'Active')

    def test_task_closes_on_merge_via_legacy_plan_token(self):
        write(self.root, 'E-0001', 'epic', 'Factory', 'epics')
        write(self.root, 'F-0001', 'feature', 'Free plan', 'features', parent='E-0001',
              typed_lines=['legacy_id: FREE-1'])
        write(self.root, 'T-0001', 'task', 'Wire the model', 'tasks', parent='F-0001',
              typed_lines=['legacy_id: FREE-1/T1'])
        ev = dict(EMPTY_EV, merged={601: 'deadbeefcafefeed0000000000000000000000'})
        ev['features'] = {'free-plan': {
            'alias': 'FREE-1', 'spec': None, 'spec_branch': None, 'spec_on_main': False,
            'spec_review': None, 'plan': None, 'plan_branch': None, 'plan_on_main': False,
            'plan_review': None,
            'tasks': {'T1': {'branch': 'cloud/free-plan-t1', 'pr': 601, 'pr_state': 'MERGED',
                             'merged_sha': 'deadbeefcafefeed0000000000000000000000'}},
            'prs': [601],
        }}
        self.assertEqual(self.run_ingest(ev), 0)
        meta, _body = read_meta(self.root, 'tasks', 'T-0001')
        self.assertEqual(meta['state'], 'Closed')

    def test_story_active_when_a_task_lists_it(self):
        write(self.root, 'E-0001', 'epic', 'Factory', 'epics')
        write(self.root, 'F-0001', 'feature', 'Free plan', 'features', parent='E-0001')
        write(self.root, 'S-0001', 'story', 'Sign up', 'stories', parent='F-0001',
              typed_lines=['legacy_id: F-ID-1'])
        write(self.root, 'T-0001', 'task', 'Wire it', 'tasks', parent='F-0001',
              typed_lines=['stories: [S-0001]', 'links:', '  branches: [cloud/free-plan-t1]'])
        ev = dict(EMPTY_EV, branches=['cloud/free-plan-t1'])
        ev['stories'] = {'F-ID-1': {'status': 'todo', 'impl': [], 'test': [], 'area': 'billing',
                                    'milestone': 'M2'}}
        ev['features'] = {'free-plan': {
            'alias': None, 'spec': None, 'spec_branch': None, 'spec_on_main': False,
            'spec_review': None, 'plan': None, 'plan_branch': None, 'plan_on_main': False,
            'plan_review': None,
            'tasks': {'T1': {'branch': 'cloud/free-plan-t1', 'pr': None, 'pr_state': None,
                             'merged_sha': None}},
            'prs': [],
        }}
        self.assertEqual(self.run_ingest(ev), 0)
        tmeta, _ = read_meta(self.root, 'tasks', 'T-0001')
        self.assertEqual(tmeta['state'], 'Active')
        smeta, _ = read_meta(self.root, 'stories', 'S-0001')
        self.assertEqual(smeta['state'], 'Active')

    def test_blocked_by_open_item(self):
        write(self.root, 'E-0001', 'epic', 'Factory', 'epics')
        write(self.root, 'F-0001', 'feature', 'Free plan', 'features', parent='E-0001',
              typed_lines=['legacy_id: FREE-1'])
        write(self.root, 'F-0002', 'feature', 'Add-on', 'features', parent='E-0001',
              typed_lines=['blockedBy: [F-0001]'])
        ev = dict(EMPTY_EV, features={'free-plan': {
            'alias': 'FREE-1', 'spec': 'origin/main:docs/x.md', 'spec_branch': None,
            'spec_on_main': True, 'spec_review': None, 'plan': None, 'plan_branch': None,
            'plan_on_main': False, 'plan_review': None, 'tasks': {}, 'prs': [],
        }})
        self.run_ingest(ev)
        meta, _body = read_meta(self.root, 'features', 'F-0002')
        self.assertEqual(meta['blocked'], True)
        self.assertEqual(meta['blocked_by_open'], ['F-0001'])

    def test_human_blocker_always_open(self):
        write(self.root, 'E-0001', 'epic', 'Factory', 'epics')
        write(self.root, 'F-0001', 'feature', 'Free plan', 'features', parent='E-0001',
              typed_lines=['blockedBy: ["Ops: key"]'])
        self.run_ingest(EMPTY_EV)
        meta, _body = read_meta(self.root, 'features', 'F-0001')
        self.assertEqual(meta['blocked'], True)
        self.assertEqual(meta['blocked_by_open'], ['Ops: key'])


class RemovedTaskTests(unittest.TestCase):
    """A Task marked `removed` (B-0067's `merged into <survivor>`) leaves its Feature's ladder
    and stops covering the Stories it used to list — T-0051."""

    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def run_ingest(self, ev):
        with mock.patch.object(ingest.evidence, 'load', return_value=ev):
            return ingest.cmd_ingest(types.SimpleNamespace(fresh=False), self.root)

    def test_removed_task_does_not_hold_the_feature(self):
        write(self.root, 'E-0001', 'epic', 'Factory', 'epics')
        write(self.root, 'F-0001', 'feature', 'Free plan', 'features', parent='E-0001')
        write(self.root, 'T-0001', 'task', 'Do it', 'tasks', parent='F-0001')
        write(self.root, 'T-0002', 'task', 'Also do it', 'tasks', parent='F-0001',
              typed_lines=['removed: merged into T-0001 (groom 2026-01-02)'])
        fev = {'alias': None, 'spec': 'origin/main:docs/specs/f-0001.md', 'spec_branch': None,
               'spec_on_main': True, 'spec_review': None, 'plan': None, 'plan_branch': None,
               'plan_on_main': False, 'plan_review': None, 'tasks': {}, 'prs': []}
        ev = dict(EMPTY_EV, features={'f-0001': fev}, ids={
            'T-0001': {'branches': [], 'open_prs': [], 'commit': 'abc1234def', 'pr': None,
                       'green': True},
            'T-0002': {'branches': ['worker/T-0002-x'], 'open_prs': [], 'commit': None,
                       'pr': None, 'green': False},
        })
        self.assertEqual(self.run_ingest(ev), 0)
        meta, _body = read_meta(self.root, 'features', 'F-0001')
        self.assertEqual(meta['stage'], 'landed')

    def test_story_on_a_removed_task_only_is_uncovered(self):
        write(self.root, 'E-0001', 'epic', 'Factory', 'epics')
        write(self.root, 'F-0001', 'feature', 'Free plan', 'features', parent='E-0001',
              machine_lines=['state: Active', 'stage: building 0/1',
                             'stage_since: 2026-01-01T00:00:00Z', 'updated: 2026-01-01T00:00:00Z'])
        write(self.root, 'S-0001', 'story', 'Uncovered', 'stories', parent='F-0001')
        write(self.root, 'T-0001', 'task', 'Removed task', 'tasks', parent='F-0001',
              typed_lines=['stories: [S-0001]',
                          'removed: merged into T-0002 (groom 2026-01-02)'])
        self.assertEqual(do_index(self.root), 0)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            check.cmd_check(types.SimpleNamespace(paths=None), self.root)
        self.assertIn('S-0001 has no Task listing it in stories:', buf.getvalue())


def landed_ids(iid, sha, green=True):
    return {iid: {'branches': [], 'open_prs': [], 'commit': sha, 'pr': None, 'green': green}}


def matched_feature(**over):
    fev = {'alias': None, 'spec': 'origin/main:docs/specs/f-0001.md', 'spec_branch': None,
           'spec_on_main': True, 'spec_review': None, 'plan': None, 'plan_branch': None,
           'plan_on_main': False, 'plan_review': None, 'tasks': {}, 'prs': []}
    fev.update(over)
    return {'f-0001': fev}


class IngestTestCase(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def run_ingest(self, ev):
        with mock.patch.object(ingest.evidence, 'load', return_value=ev):
            return ingest.cmd_ingest(types.SimpleNamespace(fresh=False), self.root)

    def meta(self, folder, iid):
        return read_meta(self.root, folder, iid)[0]

    def snapshot(self):
        out = {}
        for f in FOLDERS:
            d = os.path.join(self.root, f)
            for name in sorted(os.listdir(d)):
                with open(os.path.join(d, name), encoding='utf-8') as fh:
                    out[name] = fh.read()
        return out


class DescentTests(IngestTestCase):
    """§2.4: a Feature that closed closes the children beneath it that have no evidence of their
    own — and only those."""

    def tree(self):
        write(self.root, 'E-0001', 'epic', 'Factory', 'epics')
        write(self.root, 'F-0001', 'feature', 'Free plan', 'features', parent='E-0001')
        write(self.root, 'S-0001', 'story', 'No Task at all', 'stories', parent='F-0001')
        write(self.root, 'S-0002', 'story', 'A Task is under way', 'stories', parent='F-0001')
        write(self.root, 'T-0001', 'task', 'Under the childless Story', 'tasks', parent='S-0001')
        write(self.root, 'T-0002', 'task', 'Under way', 'tasks', parent='S-0002',
              typed_lines=['stories: [S-0002]'])
        write(self.root, 'B-0001', 'bug', 'A defect under it', 'bugs', parent='F-0001')

    def evidence(self, feature_green=True):
        return dict(EMPTY_EV, ci=True, features=matched_feature(),
                    ids={**landed_ids('F-0001', 'b' * 40, green=feature_green),
                         'T-0002': {'branches': ['worker/T-0002-x'], 'open_prs': [], 'commit': None,
                                    'pr': None, 'green': False}})

    def test_a_closed_feature_closes_the_story_with_no_task_and_names_itself_and_its_sha(self):
        self.tree()
        self.assertEqual(self.run_ingest(self.evidence()), 0)
        self.assertEqual(self.meta('features', 'F-0001')['state'], 'Closed')
        story = self.meta('stories', 'S-0001')
        self.assertEqual(story['state'], 'Closed')
        self.assertEqual(story['evidence'][-1], 'rule: parent-closed')
        self.assertIn('F-0001 Closed (commit ' + 'b' * 7 + ')', story['evidence'])

    def test_a_story_whose_task_is_active_does_not_move(self):
        self.tree()
        self.run_ingest(self.evidence())
        story = self.meta('stories', 'S-0002')
        self.assertEqual(story['state'], 'Active')
        self.assertEqual(story['evidence'][-1], 'rule: task-active')
        self.assertEqual(self.meta('tasks', 'T-0002')['state'], 'Active')

    def test_a_task_under_the_childless_story_closes_too(self):
        self.tree()
        self.run_ingest(self.evidence())
        task = self.meta('tasks', 'T-0001')
        self.assertEqual(task['state'], 'Closed')
        self.assertEqual(task['evidence'][-1], 'rule: parent-closed')
        self.assertIn('S-0001 Closed (commit ' + 'b' * 7 + ')', task['evidence'])

    def test_a_bug_under_the_closed_feature_stays_open(self):
        self.tree()
        self.run_ingest(self.evidence())
        self.assertEqual(self.meta('bugs', 'B-0001')['state'], 'New')

    def test_descent_writes_nothing_when_the_feature_is_merely_resolved(self):
        self.tree()
        self.run_ingest(self.evidence(feature_green=False))
        self.assertEqual(self.meta('features', 'F-0001')['state'], 'Resolved')
        self.assertEqual(self.meta('stories', 'S-0001')['state'], 'New')
        self.assertEqual(self.meta('tasks', 'T-0001')['state'], 'New')

    def test_a_second_ingest_over_the_same_evidence_writes_nothing(self):
        self.tree()
        self.run_ingest(self.evidence())
        before = self.snapshot()
        self.run_ingest(self.evidence())
        self.assertEqual(self.snapshot(), before)

    def test_the_epic_closes_once_every_child_has(self):
        write(self.root, 'E-0001', 'epic', 'Factory', 'epics')
        write(self.root, 'F-0001', 'feature', 'Free plan', 'features', parent='E-0001')
        self.run_ingest(dict(EMPTY_EV, ci=True, features=matched_feature(),
                             ids=landed_ids('F-0001', 'b' * 40)))
        epic = self.meta('epics', 'E-0001')
        self.assertEqual(epic['state'], 'Closed')
        self.assertEqual(epic['evidence'], ['rule: children-closed'])


class RuleLineTests(IngestTestCase):
    """§2.3: the rule that decided is the last `evidence:` entry, and History quotes it."""

    def test_a_closed_task_ends_with_its_rule_and_history_quotes_it(self):
        write(self.root, 'T-0001', 'task', 'Wire it', 'tasks')
        self.run_ingest(dict(EMPTY_EV, ci=True, ids=landed_ids('T-0001', 'abcdef0123')))
        meta, body = read_meta(self.root, 'tasks', 'T-0001')
        self.assertEqual(meta['state'], 'Closed')
        self.assertEqual(meta['evidence'], ['commit abcdef0 names T-0001',
                                            'CI green on main at or after it', 'rule: landed-green'])
        self.assertIn('ingest: state New → Closed (rule: landed-green; commit abcdef0 names T-0001; '
                      'CI green on main at or after it)', body)

    def test_every_derived_item_ends_with_a_rule_line(self):
        write(self.root, 'F-0001', 'feature', 'Free plan', 'features')
        write(self.root, 'S-0001', 'story', 'A story', 'stories', parent='F-0001')
        write(self.root, 'T-0001', 'task', 'A task', 'tasks', parent='F-0001')
        write(self.root, 'B-0001', 'bug', 'A bug', 'bugs')
        self.run_ingest(EMPTY_EV)
        for folder, iid, rule in (('features', 'F-0001', 'card'), ('stories', 'S-0001', 'no-rule'),
                                  ('tasks', 'T-0001', 'planned'), ('bugs', 'B-0001', 'filed')):
            self.assertEqual(self.meta(folder, iid)['evidence'][-1], 'rule: ' + rule, iid)

    def test_a_shapeless_story_is_named_residue_and_keeps_its_state(self):
        write(self.root, 'S-0001', 'story', 'Nothing sees it', 'stories',
              machine_lines=('state: Active', 'stage_since: 2026-01-01T00:00:00Z',
                             'updated: 2026-01-01T00:00:00Z'))
        self.run_ingest(EMPTY_EV)
        story = self.meta('stories', 'S-0001')
        self.assertEqual(story['state'], 'Active')
        self.assertEqual(story['evidence'][-1], 'rule: no-rule')

    def test_a_closed_item_stays_closed_when_its_evidence_goes(self):
        write(self.root, 'T-0001', 'task', 'Wire it', 'tasks',
              machine_lines=('state: Closed', 'stage_since: 2026-01-01T00:00:00Z',
                             'updated: 2026-01-01T00:00:00Z'))
        self.run_ingest(EMPTY_EV)
        task = self.meta('tasks', 'T-0001')
        self.assertEqual(task['state'], 'Closed')
        self.assertEqual(task['evidence'][-2:], ['held Closed; planned would say New',
                                                 'rule: closed-terminal'])

    def test_a_story_whose_tasks_all_landed_closes(self):
        write(self.root, 'F-0001', 'feature', 'Free plan', 'features')
        write(self.root, 'S-0001', 'story', 'A story', 'stories', parent='F-0001')
        write(self.root, 'T-0001', 'task', 'A task', 'tasks', parent='F-0001',
              typed_lines=['stories: [S-0001]'])
        self.run_ingest(dict(EMPTY_EV, ci=True, ids=landed_ids('T-0001', 'abcdef0123')))
        story = self.meta('stories', 'S-0001')
        self.assertEqual((story['state'], story['evidence'][-1]), ('Closed', 'rule: tasks-closed'))


class BugQuietTests(IngestTestCase):
    """§1.4: a Bug closes on green CI once its signature has been unseen past `bug_quiet`."""

    def bug(self, *typed):
        write(self.root, 'B-0001', 'bug', 'A defect', 'bugs',
              typed_lines=['links:', '  prs: [701]', *typed])
        return dict(EMPTY_EV, merged={701: 'deadbeef' * 5})

    def test_a_merged_fix_with_a_quiet_signature_closes(self):
        self.run_ingest(self.bug('signature: ci:red', 'last_filed: 2020-01-01'))
        bug = self.meta('bugs', 'B-0001')
        self.assertEqual((bug['state'], bug['evidence'][-1]), ('Closed', 'rule: quiet'))

    def test_a_signature_seen_inside_the_limit_stays_resolved(self):
        self.run_ingest(self.bug('signature: ci:red', 'last_filed: ' + today()))
        bug = self.meta('bugs', 'B-0001')
        self.assertEqual((bug['state'], bug['evidence'][-1]), ('Resolved', 'rule: fixed'))

    def test_a_bug_with_no_signature_closes_on_green_alone(self):
        self.run_ingest(self.bug())
        self.assertEqual(self.meta('bugs', 'B-0001')['state'], 'Closed')


class IngestDerivesNothing(unittest.TestCase):
    """§3.2: `asf.evidence.closing` chooses every state. A `task_state`-style call growing back
    into ingest is a sixth definition of done, and this is what stops it."""

    STATE_CHOICES = {'task_state', 'story_state', 'feature_state', 'bug_state', 'epic_state'}

    def called(self, source):
        import ast
        return sorted(n.func.attr for n in ast.walk(ast.parse(source))
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                      and n.func.attr in self.STATE_CHOICES)

    def test_ingest_calls_no_evidence_state_function(self):
        with open(ingest.__file__, encoding='utf-8') as f:
            self.assertEqual(self.called(f.read()), [])

    def test_the_walk_would_see_one(self):
        self.assertEqual(self.called("evidence.task_state(True, None, None, None)"), ['task_state'])


class NoCoderBeforeTheSpecLands(unittest.TestCase):
    """A plan on the trunk is not a plan-approved Feature while its spec is not approved: on a
    migrated record the spec can still sit on a pre-lane branch, and a coder launched against it
    finds no spec on the trunk. The Feature stays on the spec ladder, with a row that lands the
    spec from the branch it is on (or writes one when there is none), and no PLAN → CODE row."""

    SPEC_TRUNK = 'origin/main:docs/specs/f-0001.md'
    PLAN_TRUNK = 'origin/main:docs/plans/f-0001.md'

    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        write(self.root, 'F-0001', 'feature', 'Widgets', 'features', typed_lines=['decided: true'])
        write(self.root, 'T-0001', 'task', 'Build the widget', 'tasks', parent='F-0001',
              typed_lines=['writes: [src/widget.py]'])

    def fev(self, **kw):
        out = {'alias': None, 'spec': None, 'spec_branch': None, 'spec_on_main': False,
               'spec_review': None, 'plan': self.PLAN_TRUNK, 'plan_branch': None,
               'plan_on_main': True, 'plan_review': None, 'tasks': {}, 'prs': []}
        out.update(kw)
        return out

    def rows_after(self, fev):
        from asf.env import Product
        from asf.feeder import rows
        with mock.patch.object(ingest.evidence, 'load',
                               return_value=dict(EMPTY_EV, features={'f-0001': fev})):
            self.assertEqual(ingest.cmd_ingest(types.SimpleNamespace(fresh=False), self.root), 0)
        meta, _b = read_meta(self.root, 'features', 'F-0001')
        with open(os.path.join(self.root, 'index.json'), encoding='utf-8') as f:
            index = json.load(f)
        return meta, rows.candidates(index, Product('sample', {}), [])

    def test_plan_on_trunk_and_spec_only_on_a_branch_gets_a_spec_row_not_code_rows(self):
        from asf.feeder import rows
        meta, out = self.rows_after(self.fev(
            spec='origin/spec/F-0001:docs/specs/f-0001.md', spec_branch='spec/F-0001'))
        self.assertEqual(meta['stage'], 'spec-draft')
        self.assertIn('spec on spec/F-0001', meta['evidence'])
        self.assertEqual([(r.kind, r.item_id) for r in out], [(rows.STARVED_SPEC, 'F-0001')])
        self.assertEqual(out[0].branch, 'spec/F-0001')
        self.assertIn("land the existing spec", out[0].reason)
        self.assertIn("don't rewrite it", out[0].reason)

    def test_a_spec_on_a_pre_lane_branch_is_landed_from_that_branch(self):
        from asf.feeder import rows
        meta, out = self.rows_after(self.fev(spec='origin/old/widgets:docs/specs/widgets.md'))
        self.assertEqual(meta['stage'], 'spec-draft')
        self.assertEqual([(r.kind, r.branch) for r in out], [(rows.STARVED_SPEC, 'old/widgets')])

    def test_plan_on_trunk_and_no_spec_anywhere_is_a_card(self):
        from asf.feeder import rows
        meta, out = self.rows_after(self.fev())
        self.assertEqual(meta['stage'], 'card')
        self.assertEqual([(r.kind, r.item_id) for r in out], [(rows.CARD_SPEC, 'F-0001')])

    def test_plan_and_spec_both_on_trunk_get_code_rows(self):
        from asf.feeder import rows
        meta, out = self.rows_after(self.fev(spec=self.SPEC_TRUNK, spec_on_main=True))
        self.assertEqual(meta['stage'], 'plan-approved')
        self.assertEqual([(r.kind, r.item_id) for r in out], [(rows.PLAN_CODE, 'T-0001')])

    def test_an_approved_spec_review_off_the_trunk_is_landed_not_coded(self):
        from asf.feeder import rows
        meta, out = self.rows_after(self.fev(
            spec='origin/spec/F-0001:docs/specs/f-0001.md', spec_branch='spec/F-0001',
            spec_review=(1, 'APPROVED', 'f-0001-spec-review-r1.md')))
        self.assertEqual(meta['stage'], 'spec-approved')
        self.assertEqual([(r.kind, r.branch, r.launches) for r in out],
                         [(rows.APPROVED_LAND, 'spec/F-0001', False)])

    def test_an_approved_spec_waiting_on_its_open_pr_is_pushed_and_waiting(self):
        from asf.env import Product
        from asf.feeder import rows
        self.rows_after(self.fev(
            spec='origin/spec/F-0001:docs/specs/f-0001.md', spec_branch='spec/F-0001',
            spec_review=(1, 'APPROVED', 'f-0001-spec-review-r1.md')))
        with open(os.path.join(self.root, 'index.json'), encoding='utf-8') as f:
            index = json.load(f)
        out = rows.candidates(index, Product('sample', {}), [], open_branches={'spec/F-0001'})
        self.assertEqual([(r.kind, r.launches) for r in out], [(rows.PUSHED_LAND, False)])

    def test_an_approved_spec_that_cannot_land_as_is_gets_a_spec_session(self):
        from asf.env import Product
        from asf.feeder import rows
        self.rows_after(self.fev(
            spec='origin/spec/F-0001:docs/specs/f-0001.md', spec_branch='spec/F-0001',
            spec_review=(1, 'APPROVED', 'f-0001-spec-review-r1.md')))
        with open(os.path.join(self.root, 'index.json'), encoding='utf-8') as f:
            index = json.load(f)
        text = "Land the existing approved spec — don't rewrite it."
        corr = {'F-0001': {'kind': rows.LAND_SPEC, 'text': text, 'rounds': 0,
                           'branch': 'spec/F-0001'}}
        out = rows.candidates(index, Product('sample', {}), [], corrections=corr)
        self.assertEqual([(r.kind, r.branch, r.brief_kind, r.launches) for r in out],
                         [(rows.STARVED_SPEC, 'spec/F-0001', 'spec', True)])
        self.assertEqual(out[0].reason, text)

    def test_a_spec_on_the_trunk_is_what_lets_tasks_run(self):
        evidence = ingest.evidence
        spec = {'exists': True, 'approved': False, 'review': None, 'on_trunk': False}
        plan = {'exists': True, 'approved': True, 'review': None}
        self.assertEqual(evidence.feature_stage(spec, plan, ['Active', 'New'], False), 'spec-draft')
        approved = dict(spec, approved=True)
        self.assertEqual(evidence.feature_stage(approved, plan, ['Active', 'New'], False),
                         'spec-approved')
        self.assertEqual(evidence.feature_stage(approved, plan, [], False), 'spec-approved')
        on_trunk = dict(approved, on_trunk=True)
        self.assertEqual(evidence.feature_stage(on_trunk, plan, ['Active', 'New'], False),
                         'building 0/2')
        self.assertEqual(evidence.feature_stage(on_trunk, plan, [], False), 'plan-approved')
        self.assertEqual(evidence.feature_stage(spec, plan, ['Closed'], False), 'landed')

    def test_a_linked_spec_is_found_on_the_trunk_or_on_a_remote_branch(self):
        meta = {'id': 'F-0001', 'type': 'feature', 'links': {'spec': 'docs/design/widgets.md'}}
        fev = self.fev()
        product = types.SimpleNamespace(main='main')
        ev = dict(EMPTY_EV, branches=['main', 'old/widgets'])
        with mock.patch.object(ingest.evidence, 'resolve',
                               side_effect=lambda refs, product=None: {
                                   r: ('abc' if r.startswith('origin/old/widgets:') else None)
                                   for r in refs}):
            self.assertEqual(ingest._spec_home(meta, fev, ev, product), (False, 'old/widgets'))
        with mock.patch.object(ingest.evidence, 'resolve',
                               side_effect=lambda refs, product=None: {
                                   r: ('abc' if r.startswith('origin/main:') else None)
                                   for r in refs}):
            self.assertEqual(ingest._spec_home(meta, fev, ev, product), (True, ''))
        self.assertEqual(ingest._spec_home(meta, fev, ev, None), (False, ''))


if __name__ == '__main__':
    unittest.main()
