import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from asf.tick import stale

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
LIMITS_FIXTURE = os.path.join(HERE, 'fixtures', 'limits.json')
FOLDERS = ['epics', 'features', 'stories', 'tasks', 'bugs', 'decisions', 'rules']

DEFAULT_BODY = (
    "## Description\n\n## Acceptance\n- [ ] \n\n## Non-goals\n\n"
    "## History\n- 2026-09-01: created\n\n## Children\n\n## Backlinks\n"
)
FOLDER_OF = {'epic': 'epics', 'feature': 'features', 'story': 'stories', 'task': 'tasks',
             'bug': 'bugs', 'decision': 'decisions', 'rule': 'rules'}


def make_repo():
    root = tempfile.mkdtemp(prefix='stale_test_')
    for f in FOLDERS:
        os.makedirs(os.path.join(root, f))
    os.makedirs(os.path.join(root, 'tools'))
    shutil.copy(LIMITS_FIXTURE, os.path.join(root, 'tools', 'limits.json'))
    return root


def write_item(root, id_, type_, title, parent=None, typed_lines=(), machine_lines=(), body=None):
    lines = [f"id: {id_}", f"type: {type_}", f"title: {title}"]
    if parent:
        lines.append(f"parent: {parent}")
    lines.extend(typed_lines)
    lines.append('# ---- machine ----')
    lines.extend(machine_lines)
    header = '\n'.join(lines)
    path = os.path.join(root, FOLDER_OF[type_], f"{id_}.md")
    with open(path, 'w', encoding='utf-8') as f:
        f.write(f"---\n{header}\n---\n{body if body is not None else DEFAULT_BODY}")
    return path


def iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def run(args, cwd):
    env = dict(os.environ)
    env['PYTHONPATH'] = REPO_ROOT + os.pathsep + env.get('PYTHONPATH', '')
    return subprocess.run([sys.executable, '-m', 'asf.cli'] + args, cwd=cwd, env=env,
                           capture_output=True, text=True)


class DurationHelpersTests(unittest.TestCase):
    def test_limit_seconds(self):
        self.assertEqual(stale.limit_seconds('3d'), 3 * 86400)
        self.assertEqual(stale.limit_seconds('24h'), 24 * 3600)
        self.assertEqual(stale.limit_seconds('45m'), 45 * 60)

    def test_format_age(self):
        self.assertEqual(stale.format_age(90 * 60), '1h')
        self.assertEqual(stale.format_age(50 * 3600), '2d')
        self.assertEqual(stale.format_age(30), '0m')

    def test_limits_json_has_every_documented_key(self):
        with open(LIMITS_FIXTURE) as f:
            limits = json.load(f)
        for key in ('card_undecided', 'spec-draft', 'spec-review', 'plan-draft', 'plan-review',
                    'plan-approved', 'task_active', 'pr_approved_unbatched', 'bug_S1', 'bug_S2',
                    'undecided_close'):
            self.assertIn(key, limits)


class StaleCommandTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.now = datetime.datetime.now(datetime.timezone.utc)
        self.old = self.now - datetime.timedelta(hours=30)
        self.recent = self.now - datetime.timedelta(minutes=1)

        write_item(self.root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'],
                  machine_lines=['state: New', f'stage_since: {iso(self.recent)}',
                                 f'updated: {iso(self.recent)}'])
        # the one stale Feature: spec-draft for 30h, limit is 24h
        write_item(self.root, 'F-0001', 'feature', 'Free plan', parent='E-0009',
                  typed_lines=['decided: true'],
                  machine_lines=['state: Active', 'stage: spec-draft',
                                 f'stage_since: {iso(self.old)}', f'updated: {iso(self.old)}'])
        # a healthy Feature: recent spec-draft, well under the limit
        write_item(self.root, 'F-0002', 'feature', 'Paid plan', parent='E-0009',
                  typed_lines=['decided: true'],
                  machine_lines=['state: Active', 'stage: spec-draft',
                                 f'stage_since: {iso(self.recent)}', f'updated: {iso(self.recent)}'])
        # an Active Task well within the 45m limit
        write_item(self.root, 'T-0001', 'task', 'Wire it up', parent='F-0001',
                  typed_lines=['decided: true'],
                  machine_lines=['state: Active', f'stage_since: {iso(self.recent)}',
                                 f'updated: {iso(self.recent)}'])
        run(['index'], self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_only_the_stale_feature_is_reported(self):
        r = run(['stale'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        self.assertEqual(len(lines), 1, r.stdout)
        self.assertTrue(lines[0].startswith('F-0001 spec-draft'), lines[0])
        self.assertIn('> 24h', lines[0])

    def test_json_form(self):
        r = run(['stale', '--json'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = json.loads(r.stdout)
        self.assertEqual([row['id'] for row in rows], ['F-0001'])
        self.assertEqual(rows[0]['limit'], '24h')

    def test_task_active_over_45m_is_flagged(self):
        write_item(self.root, 'T-0002', 'task', 'Slow task', parent='F-0001',
                  typed_lines=['decided: true'],
                  machine_lines=['state: Active', f'stage_since: {iso(self.old)}',
                                 f'updated: {iso(self.old)}'])
        run(['index'], self.root)
        r = run(['stale'], self.root)
        ids = {l.split()[0] for l in r.stdout.splitlines() if l.strip()}
        self.assertIn('T-0002', ids)
        line = [l for l in r.stdout.splitlines() if l.startswith('T-0002')][0]
        self.assertIn('> 45m', line)

    def test_undecided_card_over_3_days(self):
        older = self.now - datetime.timedelta(days=4)
        write_item(self.root, 'F-0003', 'feature', 'Someday idea', parent='E-0009',
                  typed_lines=['decided: false'],
                  machine_lines=['state: New', f'stage_since: {iso(older)}',
                                 f'updated: {iso(older)}'])
        run(['index'], self.root)
        r = run(['stale'], self.root)
        found = [l for l in r.stdout.splitlines() if l.startswith('F-0003') and '> 3d' in l]
        self.assertEqual(len(found), 1, r.stdout)


if __name__ == '__main__':
    unittest.main()
