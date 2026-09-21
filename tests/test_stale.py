import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from asf import env as asf_env
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
    return root


def make_home(stage_limits=None):
    """A throwaway ASF_HOME whose default product carries `stage_limits` (none when None)."""
    home = tempfile.mkdtemp(prefix='stale_home_')
    os.makedirs(os.path.join(home, 'products'))
    with open(os.path.join(home, 'config.yaml'), 'w') as f:
        f.write('default_product: sample\n')
    lines = ['repo_slug: sample/product', 'main: main']
    if stage_limits:
        lines.append('stage_limits:')
        lines += [f'  {k}: {v}' for k, v in stage_limits.items()]
    with open(os.path.join(home, 'products', 'sample.yaml'), 'w') as f:
        f.write('\n'.join(lines) + '\n')
    return home


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


def run(args, cwd, home=None):
    env = dict(os.environ)
    env['PYTHONPATH'] = REPO_ROOT + os.pathsep + env.get('PYTHONPATH', '')
    env.pop('ASF_PRODUCT', None)
    if home:
        env['ASF_HOME'] = home
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

    def test_defaults_are_the_limits_json_values(self):
        # the defaults carry exactly what the per-product limits.json file used to
        with open(LIMITS_FIXTURE) as f:
            self.assertEqual(stale.DEFAULT_LIMITS, json.load(f))


class LoadLimitsTests(unittest.TestCase):
    def test_no_product_is_the_defaults(self):
        self.assertEqual(stale.load_limits(None), stale.DEFAULT_LIMITS)

    def test_product_stage_limits_override_per_key(self):
        product = asf_env.Product('sample', {'stage_limits': {'spec-draft': '48h', 'bug_S1': '2h'}})
        limits = stale.load_limits(product)
        self.assertEqual(limits['spec-draft'], '48h')
        self.assertEqual(limits['bug_S1'], '2h')
        self.assertEqual(limits['task_active'], '45m')

    def test_non_duration_values_are_left_out(self):
        product = asf_env.Product('sample', {'stage_limits': {'s1_hours': 2, 'spec-draft': 'soon'}})
        limits = stale.load_limits(product)
        self.assertNotIn('s1_hours', limits)
        self.assertEqual(limits['spec-draft'], '24h')


class StaleCommandTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.home = make_home()
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
        run(['index'], self.root, self.home)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.home, ignore_errors=True)

    def stale(self, *args, home=None):
        return run(['stale'] + list(args), self.root, home or self.home)

    def test_product_stage_limits_are_read(self):
        home = make_home({'spec-draft': '48h'})
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        r = self.stale(home=home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), '', r.stdout)

    def test_product_stage_limits_can_tighten(self):
        home = make_home({'spec-draft': '1h'})
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        ids = {l.split()[0] for l in self.stale(home=home).stdout.splitlines() if l.strip()}
        self.assertEqual(ids, {'F-0001'})
        home2 = make_home({'spec-draft': '30s'})
        self.addCleanup(shutil.rmtree, home2, ignore_errors=True)
        ids = {l.split()[0] for l in self.stale(home=home2).stdout.splitlines() if l.strip()}
        self.assertEqual(ids, {'F-0001', 'F-0002'})

    def test_a_limits_json_in_the_record_is_never_read(self):
        os.makedirs(os.path.join(self.root, 'tools'))
        with open(os.path.join(self.root, 'tools', 'limits.json'), 'w') as f:
            json.dump({'spec-draft': '1m'}, f)
        ids = {l.split()[0] for l in self.stale().stdout.splitlines() if l.strip()}
        self.assertEqual(ids, {'F-0001'})

    def test_only_the_stale_feature_is_reported(self):
        r = self.stale()
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        self.assertEqual(len(lines), 1, r.stdout)
        self.assertTrue(lines[0].startswith('F-0001 spec-draft'), lines[0])
        self.assertIn('> 24h', lines[0])

    def test_json_form(self):
        r = self.stale('--json')
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = json.loads(r.stdout)
        self.assertEqual([row['id'] for row in rows], ['F-0001'])
        self.assertEqual(rows[0]['limit'], '24h')

    def test_task_active_over_45m_is_flagged(self):
        write_item(self.root, 'T-0002', 'task', 'Slow task', parent='F-0001',
                  typed_lines=['decided: true'],
                  machine_lines=['state: Active', f'stage_since: {iso(self.old)}',
                                 f'updated: {iso(self.old)}'])
        run(['index'], self.root, self.home)
        r = self.stale()
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
        run(['index'], self.root, self.home)
        r = self.stale()
        found = [l for l in r.stdout.splitlines() if l.startswith('F-0003') and '> 3d' in l]
        self.assertEqual(len(found), 1, r.stdout)


if __name__ == '__main__':
    unittest.main()
