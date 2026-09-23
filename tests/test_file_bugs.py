import datetime
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from asf.conventions import Conventions
from asf.record import frontmatter
from asf.record.core import today
from asf.tick import file_bugs

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
    root = tempfile.mkdtemp(prefix='filebugs_test_')
    for f in FOLDERS:
        os.makedirs(os.path.join(root, f))
    os.makedirs(os.path.join(root, 'tools', 'checks'))
    os.makedirs(os.path.join(root, 'metrics', 'ci'))
    os.makedirs(os.path.join(root, 'metrics', 'ticks'))
    shutil.copy(LIMITS_FIXTURE, os.path.join(root, 'tools', 'limits.json'))
    return root


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


def write_ci_line(root, day, obj):
    path = os.path.join(root, 'metrics', 'ci', f"{day}.jsonl")
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(obj) + '\n')


def write_tick_line(root, day, obj):
    path = os.path.join(root, 'metrics', 'ticks', f"{day}.jsonl")
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(obj) + '\n')


def write_rule(root, rid, title, typed_lines=()):
    lines = [f"id: {rid}", 'type: rule', f"title: {title}"]
    lines.extend(typed_lines)
    lines.append('# ---- machine ----')
    lines.append('state: New')
    lines.append('updated: 2026-09-21T00:00:00Z')
    header = '\n'.join(lines)
    body = "## Statement\n\n## Why\n\n## Check\n\n## Source\n\n## Children\n\n## Backlinks\n"
    with open(os.path.join(root, 'rules', f"{rid}.md"), 'w', encoding='utf-8') as f:
        f.write(f"---\n{header}\n---\n{body}")


def write_check_script(root, name, script):
    path = os.path.join(root, 'tools', 'checks', name)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(script)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


def run(args, cwd):
    env = dict(os.environ)
    env.pop('BACKLOG_ID_RANGE', None)  # a job's range must not leak into the fixture's own mints (B-0012)
    env['PYTHONPATH'] = REPO_ROOT + os.pathsep + env.get('PYTHONPATH', '')
    # no operator config: the run reads asf.conventions' documented defaults, not this
    # machine's ~/.ASF, so the test asserts the same thing everywhere it runs
    env['ASF_HOME'] = os.path.join(cwd, 'no-such-asf-home')
    return subprocess.run([sys.executable, '-m', 'asf.cli'] + args, cwd=cwd, env=env,
                           capture_output=True, text=True)


def iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


class CiSignatureCollectionTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.now = datetime.datetime.now(datetime.timezone.utc)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_below_threshold_is_not_a_signature(self):
        write_ci_line(self.root, '2026-09-21', {
            'run': 1, 'sha': 'a', 'branch': 'main', 'ts': iso(self.now),
            'jobs': [{'name': 'gate', 'failed_step': 'boom', 'conclusion': 'failure'}],
        })
        sigs = file_bugs.ci_signatures(self.root, self.now)
        self.assertEqual(sigs, {})

    def test_two_occurrences_in_24h_on_main_is_s2(self):
        for i, ts in enumerate([self.now - datetime.timedelta(hours=1),
                                self.now - datetime.timedelta(hours=2)]):
            write_ci_line(self.root, '2026-09-21', {
                'run': i + 1, 'sha': 'a' * 9, 'branch': 'main', 'ts': iso(ts),
                'jobs': [{'name': 'gate', 'failed_step': 'boom', 'conclusion': 'failure'}],
            })
        sigs = file_bugs.ci_signatures(self.root, self.now)
        self.assertEqual(list(sigs), ['gate: boom'])
        self.assertEqual(sigs['gate: boom']['severity'], 'S2')
        self.assertEqual(sigs['gate: boom']['runs'], [1, 2])

    def test_feature_branch_reds_are_s3(self):
        for i, ts in enumerate([self.now - datetime.timedelta(hours=1),
                                self.now - datetime.timedelta(hours=2)]):
            write_ci_line(self.root, '2026-09-21', {
                'run': i + 1, 'sha': 'a', 'branch': 'cloud/my-feature', 'ts': iso(ts),
                'jobs': [{'name': 'gate', 'failed_step': 'boom', 'conclusion': 'failure'}],
            })
        sigs = file_bugs.ci_signatures(self.root, self.now)
        self.assertEqual(sigs['gate: boom']['severity'], 'S3')

    def test_outside_the_24h_window_does_not_count(self):
        write_ci_line(self.root, '2026-09-20', {
            'run': 1, 'sha': 'a', 'branch': 'main', 'ts': iso(self.now - datetime.timedelta(hours=1)),
            'jobs': [{'name': 'gate', 'failed_step': 'boom', 'conclusion': 'failure'}],
        })
        write_ci_line(self.root, '2026-09-19', {
            'run': 2, 'sha': 'a', 'branch': 'main', 'ts': iso(self.now - datetime.timedelta(hours=30)),
            'jobs': [{'name': 'gate', 'failed_step': 'boom', 'conclusion': 'failure'}],
        })
        sigs = file_bugs.ci_signatures(self.root, self.now)
        self.assertEqual(sigs, {})


class RefusalSignatureTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.now = datetime.datetime.now(datetime.timezone.utc)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_file_refused_twice_in_one_tick(self):
        write_tick_line(self.root, '2026-09-21', {
            'tick': 1, 'ts': iso(self.now - datetime.timedelta(hours=1)),
            'refused_files': {'apps/web/foo.ts': 2},
        })
        sigs = file_bugs.refusal_signatures(self.root, self.now)
        self.assertEqual(list(sigs), ['refusal: apps/web/foo.ts'])
        self.assertEqual(sigs['refusal: apps/web/foo.ts']['severity'], 'S3')

    def test_file_refused_once_each_across_two_ticks_still_counts(self):
        write_tick_line(self.root, '2026-09-21', {
            'tick': 1, 'ts': iso(self.now - datetime.timedelta(hours=3)),
            'refused_files': {'apps/web/foo.ts': 1},
        })
        write_tick_line(self.root, '2026-09-21', {
            'tick': 2, 'ts': iso(self.now - datetime.timedelta(hours=1)),
            'refused_files': {'apps/web/foo.ts': 1},
        })
        sigs = file_bugs.refusal_signatures(self.root, self.now)
        self.assertIn('refusal: apps/web/foo.ts', sigs)

    def test_single_refusal_does_not_qualify(self):
        write_tick_line(self.root, '2026-09-21', {
            'tick': 1, 'ts': iso(self.now), 'refused_files': {'apps/web/foo.ts': 1},
        })
        self.assertEqual(file_bugs.refusal_signatures(self.root, self.now), {})


class RuleViolationSignatureTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_violation_becomes_one_s2_signature(self):
        write_rule(self.root, 'R-0001', 'Never merge red',
                  typed_lines=['scope: merge', 'check: tools/checks/r0001.sh'])
        write_check_script(self.root, 'r0001.sh',
                           "#!/usr/bin/env bash\necho 'R-0001 merged with no green run sha=abc1234 3d'\nexit 1\n")
        run(['index'], self.root)
        sigs = file_bugs.rule_violation_signatures(self.root)
        self.assertEqual(len(sigs), 1)
        sig = list(sigs)[0]
        self.assertEqual(sig, 'R-0001: rule violated')   # one Bug per rule, places as evidence
        self.assertEqual(sigs[sig]['severity'], 'S2')
        self.assertEqual(sigs[sig]['places'], 1)
        self.assertIn('Never merge red', sigs[sig]['title'])

    def test_no_violations_is_empty(self):
        write_rule(self.root, 'R-0001', 'Always fine',
                  typed_lines=['scope: merge', 'check: tools/checks/r0001.sh'])
        write_check_script(self.root, 'r0001.sh', "#!/usr/bin/env bash\nexit 0\n")
        run(['index'], self.root)
        self.assertEqual(file_bugs.rule_violation_signatures(self.root), {})


class FileBugsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        write_item(self.root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])
        run(['index'], self.root)
        self.now = datetime.datetime.now(datetime.timezone.utc)
        for i, ts in enumerate([self.now - datetime.timedelta(hours=1),
                                self.now - datetime.timedelta(hours=2)]):
            write_ci_line(self.root, today(), {
                'run': 100 + i, 'sha': 'deadbee', 'branch': 'main', 'ts': iso(ts),
                'jobs': [{'name': 'gate', 'failed_step': 'flaky', 'conclusion': 'failure'}],
            })

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_first_run_files_exactly_one_bug(self):
        r = run(['file-bugs', '--default-bug-epic', 'E-0009'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('1 filed', r.stdout)
        bugs = [n for n in os.listdir(os.path.join(self.root, 'bugs')) if n.endswith('.md')]
        self.assertEqual(len(bugs), 1)
        with open(os.path.join(self.root, 'bugs', bugs[0])) as f:
            meta, _body = frontmatter.parse(f.read(), path=f'bugs/{bugs[0]}')
        self.assertEqual(meta['severity'], 'S2')
        self.assertEqual(meta['found_in'], 'ci')
        self.assertEqual(meta['parent'], 'E-0009')
        self.assertEqual(meta['count'], 1)
        self.assertEqual(meta['links']['runs'], [100, 101])

    def test_an_s1_s2_bug_is_filed_decided_so_bug_fix_need_not_wait_for_the_groom(self):
        # B-0089: the severity is the decision for an S1/S2 — no 24h wait for the daily groom.
        r = run(['file-bugs', '--default-bug-epic', 'E-0009'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        name = [n for n in os.listdir(os.path.join(self.root, 'bugs')) if n.endswith('.md')][0]
        with open(os.path.join(self.root, 'bugs', name)) as f:
            meta, _body = frontmatter.parse(f.read(), path=f'bugs/{name}')
        self.assertEqual(meta['severity'], 'S2')
        self.assertIs(meta['decided'], True)

    def test_with_no_default_bug_epic_configured_bug_is_still_filed_unparented(self):
        # file-bugs never blocks on a missing convention the way groom's inbox intake does — a
        # signature-keyed Bug with no parent is a `check` finding for a human, not a stall.
        r = run(['file-bugs'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('1 filed', r.stdout)
        bugs = [n for n in os.listdir(os.path.join(self.root, 'bugs')) if n.endswith('.md')]
        self.assertEqual(len(bugs), 1)
        with open(os.path.join(self.root, 'bugs', bugs[0])) as f:
            meta, _body = frontmatter.parse(f.read(), path=f'bugs/{bugs[0]}')
        self.assertNotIn('parent', meta)


class DeterministicBugIdTests(unittest.TestCase):
    """The legacy tool merges ci_signatures, then refusal_signatures, then
    rule_violation_signatures into one dict and files/bumps in `sorted(signatures)` order — so a
    run's new ids depend only on the sorted signature strings, never on which source found them
    first. Chosen so that source (insertion) order and sorted order disagree: a naive dict-order
    iteration would mint B-0001 for the CI signature; the sorted order must mint it for the rule
    violation instead.
    """

    GOLDEN = {
        'R-0001: rule violated': 'B-0001',
        'refusal: apps/web/foo.ts': 'B-0002',
        'zzz-job: boom': 'B-0003',
    }

    def _build_fixture(self):
        root = make_repo()
        write_item(root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])
        write_rule(root, 'R-0001', 'Never merge red',
                  typed_lines=['scope: merge', 'check: tools/checks/r0001.sh'])
        write_check_script(root, 'r0001.sh',
                           "#!/usr/bin/env bash\necho 'R-0001 merged with no green run sha=abc1234 3d'\nexit 1\n")
        run(['index'], root)

        now = datetime.datetime.now(datetime.timezone.utc)
        for i, ts in enumerate([now - datetime.timedelta(hours=1), now - datetime.timedelta(hours=2)]):
            write_ci_line(root, today(), {
                'run': 100 + i, 'sha': 'deadbee', 'branch': 'main', 'ts': iso(ts),
                'jobs': [{'name': 'zzz-job', 'failed_step': 'boom', 'conclusion': 'failure'}],
            })
        write_tick_line(root, today(), {
            'tick': 1, 'ts': iso(now - datetime.timedelta(hours=1)),
            'refused_files': {'apps/web/foo.ts': 2},
        })
        return root

    def _run_and_collect(self):
        root = self._build_fixture()
        try:
            r = run(['file-bugs', '--default-bug-epic', 'E-0009'], root)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn('3 filed', r.stdout)
            by_sig = {}
            for name in os.listdir(os.path.join(root, 'bugs')):
                if not name.endswith('.md'):
                    continue
                with open(os.path.join(root, 'bugs', name)) as f:
                    meta, _body = frontmatter.parse(f.read(), path=f'bugs/{name}')
                by_sig[meta['signature']] = meta['id']
            return by_sig
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_ids_match_the_sorted_signature_golden(self):
        self.assertEqual(self._run_and_collect(), self.GOLDEN)

    def test_two_runs_over_the_same_fixture_mint_the_same_ids(self):
        first = self._run_and_collect()
        second = self._run_and_collect()
        self.assertEqual(first, second)
        self.assertEqual(first, self.GOLDEN)


if __name__ == '__main__':
    unittest.main()


class BranchSeverityTests(unittest.TestCase):
    """A CI failure's severity comes from the branch it happened on — and which branches are the
    trunk and the merge-batch lane is the product's convention, never a literal here."""

    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.now = datetime.datetime.now(datetime.timezone.utc)

    def write_two_reds(self, branch):
        for i, ts in enumerate([self.now - datetime.timedelta(hours=1),
                                self.now - datetime.timedelta(hours=2)]):
            write_ci_line(self.root, today(), {
                'run': 200 + i, 'sha': 'deadbee', 'branch': branch, 'ts': iso(ts),
                'jobs': [{'name': 'gate', 'failed_step': 'flaky', 'conclusion': 'failure'}],
            })

    def sig(self, conv):
        sigs = file_bugs.ci_signatures(self.root, self.now, conv)
        return sigs['gate: flaky']['severity']

    def test_a_product_branch_is_one_severity_lower_than_the_trunk(self):
        self.write_two_reds('feature/add-login')
        self.assertEqual(self.sig(Conventions(main='trunk')), 'S3')

    def test_the_products_own_trunk_name_is_read_from_the_conventions(self):
        self.write_two_reds('trunk')
        self.assertEqual(self.sig(Conventions(main='trunk')), 'S2')
        self.assertEqual(self.sig(Conventions(main='main')), 'S3')

    def test_the_batch_lane_is_a_branch_prefix_not_a_literal(self):
        self.write_two_reds('merge-queue/2026-09-21')
        conv = Conventions.from_mapping({'branch_prefixes': {'batch': 'merge-queue/'}})
        self.assertEqual(self.sig(conv), 'S2')
        self.assertEqual(self.sig(Conventions()), 'S3')   # no batch lane configured
