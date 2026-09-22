"""The T10 gate scenario: one temp repo carrying every fixture the brief names — one stale
Feature, one uncovered Story, two overlapping Tasks, one repeated CI signature, one inbox file,
one answered groom file — run through stale/check/file-bugs/groom/index once, then run the whole
sequence a second time and require it to be a no-op.
"""
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from asf import hermetic

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
FOLDERS = ['epics', 'features', 'stories', 'tasks', 'bugs', 'decisions', 'rules']

# Originally `tools/limits.json`, colocated with the (unported) `backlog.py` and copied into
# the fixture repo by setUp(). The CLI orchestrator this scenario drives now lives at
# `asf.cli` (built out from record/tick/groom/harvest — a separate, larger port than
# evidence.py/rules.py), so this test invokes it via `python3 -m asf.cli` instead of a
# colocated script, and inlines the limits fixture rather than depending on a sibling file.
LIMITS = {
    "card_undecided": "3d",
    "spec-draft": "24h",
    "spec-review": "12h",
    "plan-draft": "24h",
    "plan-review": "12h",
    "plan-approved": "24h",
    "task_active": "45m",
    "pr_approved_unbatched": "10m",
    "bug_S1": "10m",
    "bug_S2": "24h",
    "undecided_close": "14d",
}

DEFAULT_BODY = (
    "## Description\n\n## Acceptance\n- [ ] \n\n## Non-goals\n\n"
    "## History\n- 2026-09-01: created\n\n## Children\n\n## Backlinks\n"
)
FOLDER_OF = {'epic': 'epics', 'feature': 'features', 'story': 'stories', 'task': 'tasks',
             'bug': 'bugs', 'decision': 'decisions', 'rule': 'rules'}


def write_item(root, id_, type_, title, parent=None, typed_lines=(), machine_lines=None, body=None):
    if machine_lines is None:
        machine_lines = ['state: New', 'stage_since: 2026-09-01T00:00:00Z',
                         'updated: 2026-09-01T00:00:00Z']
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


def run(args, cwd):
    env = hermetic.build()
    env['PYTHONPATH'] = PROJECT_ROOT + os.pathsep + env.get('PYTHONPATH', '')
    return subprocess.run([sys.executable, '-m', 'asf.cli'] + args, cwd=cwd, env=env,
                          capture_output=True, text=True)


def iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def snapshot(root):
    out = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            with open(path, 'rb') as f:
                out[rel] = hashlib.sha256(f.read()).hexdigest()
    return out


class T10GateScenario(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='t10_gate_')
        for f in FOLDERS:
            os.makedirs(os.path.join(self.root, f))
        for d in ('inbox/done', 'groom', 'metrics/ci', 'metrics/ticks', 'metrics/sessions',
                 'metrics/daily', 'releases', 'tools/checks'):
            os.makedirs(os.path.join(self.root, d))
        with open(os.path.join(self.root, 'tools', 'limits.json'), 'w', encoding='utf-8') as f:
            json.dump(LIMITS, f)

        self.now = datetime.datetime.now(datetime.timezone.utc)
        self.today = self.now.strftime('%Y-%m-%d')
        old = self.now - datetime.timedelta(hours=30)
        recent = self.now - datetime.timedelta(minutes=1)

        # E-0009: the factory epic every auto-filed Bug and inbox Bug lands under
        write_item(self.root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'],
                  machine_lines=['state: New', f'stage_since: {iso(recent)}', f'updated: {iso(recent)}'])

        # one stale Feature: spec-draft for 30h, limit 24h
        write_item(self.root, 'F-0001', 'feature', 'Free plan', parent='E-0009',
                  typed_lines=['decided: true'],
                  machine_lines=['state: Active', 'stage: spec-draft',
                                 f'stage_since: {iso(old)}', f'updated: {iso(old)}'])

        # a Feature at "building" with one uncovered Story, and two overlapping Active Tasks
        write_item(self.root, 'F-0002', 'feature', 'Billing rework', parent='E-0009',
                  typed_lines=['decided: true'],
                  machine_lines=['state: Active', 'stage: building 0/1',
                                 f'stage_since: {iso(recent)}', f'updated: {iso(recent)}'])
        write_item(self.root, 'S-0001', 'story', 'Uncovered story', parent='F-0002',
                  typed_lines=['decided: true'],
                  machine_lines=['state: New', f'stage_since: {iso(recent)}', f'updated: {iso(recent)}'])
        write_item(self.root, 'T-0001', 'task', 'Task one', parent='F-0002',
                  typed_lines=['decided: true', "writes: ['apps/web/billing/**']"],
                  machine_lines=['state: Active', f'stage_since: {iso(recent)}', f'updated: {iso(recent)}'])
        write_item(self.root, 'T-0002', 'task', 'Task two', parent='F-0002',
                  typed_lines=['decided: true', "writes: ['apps/web/billing/page.tsx']"],
                  machine_lines=['state: Active', f'stage_since: {iso(recent)}', f'updated: {iso(recent)}'])

        # the item the pre-answered groom file will decide
        write_item(self.root, 'F-0003', 'feature', 'Old idea', parent='E-0009',
                  typed_lines=['decided: false'],
                  machine_lines=['state: New', 'stage: card',
                                 f'stage_since: {iso(recent)}', f'updated: {iso(recent)}'])

        # one repeated CI signature, on a batch branch (S2), within the 24h window
        ci_path = os.path.join(self.root, 'metrics', 'ci', f"{self.today}.jsonl")
        with open(ci_path, 'w', encoding='utf-8') as f:
            for i, ts in enumerate([self.now - datetime.timedelta(hours=1),
                                    self.now - datetime.timedelta(hours=2)]):
                f.write(json.dumps({
                    'run': 900 + i, 'sha': 'cafef00d', 'branch': 'worktree-m-batch-z', 'ts': iso(ts),
                    'jobs': [{'name': 'gate', 'failed_step': 'the gate is flaky', 'conclusion': 'failure'}],
                }) + '\n')

        # one inbox file. `parent:` is explicit here rather than relying on a hardcoded
        # default epic for unparented Bugs — asf.groom.inbox.process_inbox() takes that
        # default as a parameter (None unless an operator configures one), unlike the
        # original tool this scenario was ported from, which always assumed E-0009.
        with open(os.path.join(self.root, 'inbox', 'report.md'), 'w', encoding='utf-8') as f:
            f.write("# Checkout is broken\nparent: E-0009\nCustomers cannot pay right now.\n")

        # one answered groom file, from "yesterday"
        yesterday = (self.now - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
        with open(os.path.join(self.root, 'groom', f"{yesterday}.md"), 'w', encoding='utf-8') as f:
            f.write("# Groom " + yesterday + "\n\n## Undecided > 3 days\n\n"
                    "- [ ] F-0003 Old idea — undecided 4d → answer: yes\n")

        r = run(['index'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _pipeline(self):
        stale = run(['stale'], self.root)
        check = run(['check'], self.root)
        file_bugs = run(['file-bugs'], self.root)
        groom = run(['groom', '--date', self.today, '--apply'], self.root)
        index = run(['index'], self.root)
        return stale, check, file_bugs, groom, index

    def test_first_pass_hits_exactly_the_named_fixtures(self):
        stale, check, file_bugs, groom, index = self._pipeline()

        # one stale Feature
        stale_ids = {l.split()[0] for l in stale.stdout.splitlines() if l.strip()}
        self.assertEqual(stale_ids, {'F-0001'})

        # one uncovered Story, two overlapping Tasks — both from `check`
        self.assertIn('S-0001 has no Task listing it in stories:', check.stdout)
        overlap_lines = [l for l in check.stdout.splitlines() if 'intersects Active task' in l]
        self.assertEqual(len(overlap_lines), 1, check.stdout)

        # one repeated signature -> one Bug filed under E-0009
        self.assertIn('1 filed', file_bugs.stdout)
        bug_files = [n for n in os.listdir(os.path.join(self.root, 'bugs')) if n.endswith('.md')]
        # F-0003's inbox-report Bug is separate from the auto-filed CI Bug — two Bugs total,
        # one from the inbox card and one from file-bugs
        self.assertEqual(len(bug_files), 2, bug_files)

        # one inbox file -> one card, one answered groom file -> one applied answer
        self.assertIn('applied 1', groom.stdout)
        self.assertIn('inbox 1 card', groom.stdout)
        self.assertEqual(index.returncode, 0, index.stderr)

        with open(os.path.join(self.root, 'features', 'F-0003.md')) as f:
            self.assertIn('decided: true', f.read())
        self.assertEqual(os.listdir(os.path.join(self.root, 'inbox')), ['done'])

    def test_second_pass_is_a_no_op(self):
        self._pipeline()
        before = snapshot(self.root)

        stale2, check2, file_bugs2, groom2, index2 = self._pipeline()

        self.assertIn('0 filed, 0 bumped', file_bugs2.stdout)
        self.assertIn('applied 0', groom2.stdout)
        self.assertIn('inbox 0 card', groom2.stdout)

        after = snapshot(self.root)
        self.assertEqual(before, after)

        # `check` and `index` are themselves stable under a second run (README's own contract)
        r = run(['check'], self.root)
        # only the pre-existing overlap/coverage fixtures remain — no new violations appeared
        self.assertEqual(check2.stdout, r.stdout)


if __name__ == '__main__':
    unittest.main()
