import contextlib
import io
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from asf.record import check
from asf.record import frontmatter
from asf.record import ingest
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
        self.assertEqual(meta['evidence'], ['branch worker/T-0002-wire'])

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
        self.assertEqual(len(meta['evidence']), 1)
        self.assertIn('no evidence found', meta['evidence'][0])
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
        write(self.root, 'E-0009', 'epic', 'Factory', 'epics', typed_lines=['legacy_id: GOAL 9'])
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


if __name__ == '__main__':
    unittest.main()
