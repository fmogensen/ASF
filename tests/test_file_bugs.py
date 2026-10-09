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
from asf.record.core import canonicalize, load_items, today
from asf.schema import SCHEMA_VERSION
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
    os.makedirs(os.path.join(root, 'metrics', 'sessions'))
    shutil.copy(LIMITS_FIXTURE, os.path.join(root, 'tools', 'limits.json'))
    return root


def write_item(root, id_, type_, title, parent=None, typed_lines=(), machine_lines=None, body=None):
    if machine_lines is None:
        machine_lines = ['state: New', 'stage_since: 2026-09-01T00:00:00Z',
                         'updated: 2026-09-01T00:00:00Z']
    lines = [f"id: {id_}", f"type: {type_}", f"title: {title}",
             f"schema_version: {SCHEMA_VERSION}"]
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


def write_session_line(root, day, obj):
    path = os.path.join(root, 'metrics', 'sessions', f"{day}.jsonl")
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(obj) + '\n')


def write_rule(root, rid, title, typed_lines=()):
    lines = [f"id: {rid}", 'type: rule', f"title: {title}",
             f"schema_version: {SCHEMA_VERSION}"]
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
            'run': 1, 'sha': 'a', 'branch': 'cloud/my-feature', 'ts': iso(self.now),
            'jobs': [{'name': 'gate', 'failed_step': 'boom', 'conclusion': 'failure'}],
        })
        sigs = file_bugs.ci_signatures(self.root, self.now)
        self.assertEqual(sigs, {})

    def test_one_failure_that_is_the_trunks_latest_run_is_a_signature(self):
        """The trunk is red now: the lane holds every PR red on that check until it is green,
        so its Bug is filed at once — never after a second red."""
        write_ci_line(self.root, '2026-09-21', {
            'run': 1, 'sha': 'a', 'branch': 'main', 'ts': iso(self.now),
            'jobs': [{'name': 'gate', 'failed_step': 'boom', 'conclusion': 'failure'}],
        })
        sigs = file_bugs.ci_signatures(self.root, self.now)
        self.assertEqual(list(sigs), ['gate: boom'])
        self.assertEqual(sigs['gate: boom']['severity'], 'S2')

    def test_one_trunk_failure_already_green_again_is_not_a_signature(self):
        write_ci_line(self.root, '2026-09-21', {
            'run': 1, 'sha': 'a', 'branch': 'main', 'ts': iso(self.now - datetime.timedelta(hours=2)),
            'jobs': [{'name': 'gate', 'failed_step': 'boom', 'conclusion': 'failure'}],
        })
        write_ci_line(self.root, '2026-09-21', {
            'run': 2, 'sha': 'b', 'branch': 'main', 'ts': iso(self.now - datetime.timedelta(hours=1)),
            'jobs': [{'name': 'gate', 'conclusion': 'success'}],
        })
        self.assertEqual(file_bugs.ci_signatures(self.root, self.now), {})

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
            'run': 1, 'sha': 'a', 'branch': 'cloud/my-feature', 'ts': iso(self.now - datetime.timedelta(hours=1)),
            'jobs': [{'name': 'gate', 'failed_step': 'boom', 'conclusion': 'failure'}],
        })
        write_ci_line(self.root, '2026-09-19', {
            'run': 2, 'sha': 'a', 'branch': 'cloud/my-feature',
            'ts': iso(self.now - datetime.timedelta(hours=30)),
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


class ReadmeStaleTests(unittest.TestCase):
    """`file_bugs.readme_signatures` — F-0030 §2.7, Task 6: `readme/stale`, filed once a red
    `asf readme --check` has sat past `README_STALE_DAYS`, bumped rather than duplicated."""

    SPAN = '<!--asf:n sessions-->9<!--/asf:n--> sessions run so far.\n'

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix='filebugs_readme_')
        self.now = datetime.datetime.now(datetime.timezone.utc)
        self.conv = Conventions()

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def _write(self, name, text):
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)

    def _facts(self, day, text='9'):
        self._write('docs/readme-numbers.json',
                    json.dumps({'day': day, 'numbers': {'sessions': {'text': text}}}))

    def test_eight_days_behind_and_red_files_one_bug(self):
        self._write('README.md', self.SPAN.replace('-->9<!--', '-->99<!--'))
        day = (self.now.date() - datetime.timedelta(days=8)).isoformat()
        self._facts(day)
        sigs = file_bugs.readme_signatures(self.repo, self.now, self.conv)
        self.assertEqual(list(sigs), ['readme/stale'])
        self.assertEqual(sigs['readme/stale']['severity'], 'S3')

    def test_a_second_run_bumps_not_duplicates(self):
        # the signature is deterministic per (page, facts): two calls on the same red, stale
        # state produce the identical signature, so `_file_or_bump_bug` bumps the one Bug already
        # filed rather than minting a second
        self._write('README.md', self.SPAN.replace('-->9<!--', '-->99<!--'))
        day = (self.now.date() - datetime.timedelta(days=8)).isoformat()
        self._facts(day)
        first = file_bugs.readme_signatures(self.repo, self.now, self.conv)
        second = file_bugs.readme_signatures(self.repo, self.now, self.conv)
        self.assertEqual(list(first), list(second), ['readme/stale'])

    def test_six_days_behind_files_nothing(self):
        self._write('README.md', self.SPAN.replace('-->9<!--', '-->99<!--'))
        day = (self.now.date() - datetime.timedelta(days=6)).isoformat()
        self._facts(day)
        self.assertEqual(file_bugs.readme_signatures(self.repo, self.now, self.conv), {})

    def test_red_with_no_facts_file_files_nothing(self):
        self._write('README.md', self.SPAN.replace('-->9<!--', '-->99<!--'))
        self.assertEqual(file_bugs.readme_signatures(self.repo, self.now, self.conv), {})

    def test_sound_page_files_nothing(self):
        self._write('README.md', self.SPAN)
        day = (self.now.date() - datetime.timedelta(days=8)).isoformat()
        self._facts(day)
        self.assertEqual(file_bugs.readme_signatures(self.repo, self.now, self.conv), {})

    def test_spanless_readme_files_nothing(self):
        self._write('README.md', 'Nothing to see here.\n')
        self.assertEqual(file_bugs.readme_signatures(self.repo, self.now, self.conv), {})


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


class RuleViolationFieldTests(unittest.TestCase):
    """§2.7: a violation line's two optional trailing fields — ``sev=`` is that place's own
    severity, ``sig=`` makes it its own Bug instead of folding into one Bug per rule."""

    def setUp(self):
        self.root = make_repo()
        self.state = tempfile.mkdtemp(prefix='filebugs_state_')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.state, ignore_errors=True)

    def _file_bugs(self, epic='E-0009'):
        import argparse
        import contextlib
        import io
        args = argparse.Namespace(default_bug_epic=epic, file_bug_level='auto',
                                  conventions=Conventions(), state_dir=self.state)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = file_bugs.cmd_file_bugs(args, self.root)
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_line_fields_splits_the_known_fields_off(self):
        text, sev, sig = file_bugs.line_fields(
            'R-0009 secret alert open github_pat acme#7 since 2026-01-01 sev=S1 sig=secret-acme-7')
        self.assertEqual(text, 'R-0009 secret alert open github_pat acme#7 since 2026-01-01')
        self.assertEqual(sev, 'S1')
        self.assertEqual(sig, 'secret-acme-7')

    def test_an_unknown_field_stays_in_the_text(self):
        text, sev, sig = file_bugs.line_fields('R-0001 merged red foo=bar')
        self.assertEqual(text, 'R-0001 merged red foo=bar')
        self.assertIsNone(sev)
        self.assertIsNone(sig)

    def test_an_invalid_severity_is_ignored_and_stays_in_the_text(self):
        text, sev, sig = file_bugs.line_fields('R-0001 merged red sev=S9')
        self.assertEqual(text, 'R-0001 merged red sev=S9')
        self.assertIsNone(sev)
        self.assertIsNone(sig)

    def test_a_line_with_neither_field_is_unchanged(self):
        line = 'R-0001 merged with no green run sha=abc1234 3d'
        text, sev, sig = file_bugs.line_fields(line)
        self.assertEqual(text, line)
        self.assertIsNone(sev)
        self.assertIsNone(sig)

    def test_two_sig_lines_file_two_bugs_each_its_own_severity_and_evidence(self):
        secret_line = ('R-0009 secret alert open github_pat acme#7 since 2026-01-01 '
                       'sev=S1 sig=secret-acme-7')
        dep_line = ('R-0009 dependency alert open high lodash (npm) acme#3 since 2026-01-02 '
                   'sev=S2 sig=dep-acme-3')
        data = {'violations': [{'rule': 'R-0009', 'line': secret_line},
                               {'rule': 'R-0009', 'line': dep_line}]}
        sigs = file_bugs.rule_violation_signatures(self.root, data)
        self.assertEqual(set(sigs), {'R-0009: secret-acme-7', 'R-0009: dep-acme-3'})
        self.assertEqual(sigs['R-0009: secret-acme-7']['severity'], 'S1')
        self.assertEqual(sigs['R-0009: secret-acme-7']['evidence'], [secret_line])
        self.assertEqual(sigs['R-0009: dep-acme-3']['severity'], 'S2')
        self.assertEqual(sigs['R-0009: dep-acme-3']['evidence'], [dep_line])

    def test_two_lines_with_neither_field_file_one_s2_bug_byte_for_byte(self):
        line1 = 'R-0001 merged with no green run sha=abc1234 3d'
        line2 = 'R-0001 merged with no green run sha=def5678 1d'
        write_rule(self.root, 'R-0001', 'Never merge red',
                  typed_lines=['scope: merge', 'check: tools/checks/r0001.sh'])
        write_check_script(self.root, 'r0001.sh',
                           f"#!/usr/bin/env bash\necho '{line1}'\necho '{line2}'\nexit 1\n")
        run(['index'], self.root)
        sigs = file_bugs.rule_violation_signatures(self.root)
        self.assertEqual(len(sigs), 1)
        sig = list(sigs)[0]
        self.assertEqual(sig, 'R-0001: rule violated')
        self.assertEqual(sigs[sig]['severity'], 'S2')
        self.assertEqual(sigs[sig]['places'], 2)
        self.assertEqual(sigs[sig]['evidence'], [line1, line2])

    def test_a_second_run_the_same_day_files_nothing_and_bumps_nothing(self):
        write_item(self.root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])
        run(['index'], self.root)
        data = {'violations': [{'rule': 'R-0009', 'line':
                                'R-0009 secret alert open github_pat acme#7 since 2026-01-01 '
                                'sev=S1 sig=secret-acme-7'}], 'broken': []}
        orig = file_bugs.rule_check_results
        file_bugs.rule_check_results = lambda root: data
        try:
            first = self._file_bugs()
            second = self._file_bugs()
        finally:
            file_bugs.rule_check_results = orig
        self.assertIn('1 filed, 0 bumped', first)
        self.assertIn('0 filed, 0 bumped', second)
        bugs = [n for n in os.listdir(os.path.join(self.root, 'bugs')) if n.endswith('.md')]
        self.assertEqual(len(bugs), 1)


class SecretAlertTests(unittest.TestCase):
    """T-0363's surviving acceptance ("a planted test secret in a branch is filed within one
    tick"), hermetic per PD10: no subprocess, no ``gh``."""

    def setUp(self):
        self.root = make_repo()
        self.state = tempfile.mkdtemp(prefix='filebugs_state_')
        write_item(self.root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])
        run(['index'], self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.state, ignore_errors=True)

    def test_a_planted_secret_alert_is_filed_as_an_s1_bug_in_one_tick(self):
        from asf import env
        from asf.security import alerts

        class FakeHost(alerts.Host):
            def secrets(self):
                return [{'number': 7, 'state': 'open', 'kind': 'github_pat',
                        'url': 'https://x/7', 'created_at': '2026-01-01T00:00:00Z',
                        'locations_count': 1}]

            def dependencies(self):
                return []

        product = env.Product('p', {'repo_slug': 'acme/widgets'})
        lines = alerts.violations(product, host=FakeHost())
        self.assertEqual(len(lines), 1)
        data = {'violations': [{'rule': 'R-0009', 'line': lines[0]}], 'broken': []}

        import argparse
        import contextlib
        import io
        args = argparse.Namespace(default_bug_epic='E-0009', file_bug_level='auto',
                                  conventions=Conventions(), state_dir=self.state)
        orig = file_bugs.rule_check_results
        file_bugs.rule_check_results = lambda root: data
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = file_bugs.cmd_file_bugs(args, self.root)
        finally:
            file_bugs.rule_check_results = orig
        self.assertEqual(rc, 0)
        bugs = [n for n in os.listdir(os.path.join(self.root, 'bugs')) if n.endswith('.md')]
        self.assertEqual(len(bugs), 1)
        with open(os.path.join(self.root, 'bugs', bugs[0])) as f:
            meta, _body = frontmatter.parse(f.read(), path=f'bugs/{bugs[0]}')
        self.assertEqual(meta['severity'], 'S1')
        self.assertEqual(meta['signature'], 'R-0009: secret-acme/widgets-7')
        self.assertEqual(meta['parent'], 'E-0009')


BARE_REF_BODY = (
    "## Description\nas decided in D1, the thing\n\n## Acceptance\n- [ ] \n\n## Non-goals\n\n"
    "## History\n- 2026-09-01: created\n\n## Children\n\n## Backlinks\n"
)


class RecordErrorSignatureTests(unittest.TestCase):
    """B-0132: the record pre-commit no longer refuses a commit over an error in a card nobody
    touched, so the standing debt needs an owner — one Bug per error CLASS, never one per card."""

    def setUp(self):
        self.root = make_repo()
        write_item(self.root, 'D-0001', 'decision', 'A decision')
        write_item(self.root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])
        write_item(self.root, 'F-0001', 'feature', 'A feature', parent='E-0009')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_one_bug_per_error_class_not_one_per_card(self):
        for n in (279, 331, 332):
            write_item(self.root, f'T-{n:04d}', 'task', f'Task {n}', parent='F-0001',
                       body=BARE_REF_BODY)
        run(['index'], self.root)
        sigs = file_bugs.record_error_signatures(self.root)
        self.assertEqual(len(sigs), 1, sigs)
        sig = list(sigs)[0]
        self.assertEqual(sig, 'record error: bare decision reference …; write it as [[D-nnnn]]')
        self.assertEqual(sigs[sig]['places'], 3)          # three cards, one Bug
        self.assertEqual(sigs[sig]['severity'], 'S3')
        self.assertIn('bare decision reference', sigs[sig]['title'])
        evidence = '\n'.join(sigs[sig]['evidence'])
        for n in (279, 331, 332):
            self.assertIn(f'tasks/T-{n:04d}.md', evidence)
        self.assertIn('asf check', sigs[sig]['acceptance'][0])

    def test_two_error_classes_are_two_bugs(self):
        write_item(self.root, 'T-0279', 'task', 'Task', parent='F-0001', body=BARE_REF_BODY)
        write_item(self.root, 'B-0900', 'bug', 'No severity here', parent='F-0001')
        run(['index'], self.root)
        sigs = file_bugs.record_error_signatures(self.root)
        self.assertEqual(len(sigs), 2, sigs)
        self.assertIn('record error: bare decision reference …; write it as [[D-nnnn]]', sigs)
        self.assertIn('record error: …: bug without severity', sigs)

    def test_a_possessive_before_a_specific_does_not_split_the_class(self):
        # B-0132: the message's `T-0001's` possessive must not pair with the second glob's
        # opening quote — two overlaps on different globs are still one class, one Bug
        msg1 = "writes: 'asf/a.py' intersects Active task T-0001's 'asf/b.py'"
        msg2 = "writes: 'asf/c.py' intersects Active task T-0002's 'docs/x.md'"
        self.assertEqual(file_bugs.error_class(msg1), file_bugs.error_class(msg2))

    def test_the_self_healed_i3_overlap_class_is_never_filed(self):
        # B-0138: `asf.tick.widen_footprint.serialize_overlaps` orders this away every tick — a
        # Bug filed for it could never land a fix naming it and would never go quiet, so this
        # source must never file or bump one for it, however many Active Tasks overlap
        active = ['state: Active', 'stage_since: 2026-09-01T00:00:00Z',
                  'updated: 2026-09-01T00:00:00Z']
        for n in (500, 501, 502):
            write_item(self.root, f'T-{n:04d}', 'task', f'Task {n}', parent='F-0001',
                       typed_lines=['writes: [asf/a.py]'], machine_lines=active)
        run(['index'], self.root)
        self.assertIn('intersects Active task', run(['check'], self.root).stdout)
        self.assertEqual(file_bugs.record_error_signatures(self.root), {})

    def test_a_clean_record_files_nothing(self):
        write_item(self.root, 'T-0279', 'task', 'Task', parent='F-0001')
        run(['index'], self.root)
        self.assertEqual(file_bugs.record_error_signatures(self.root), {})

    def test_the_evidence_never_carries_the_defect_it_reports(self):
        # a Bug whose body quotes `D1` bare would itself be a bare decision reference — the
        # error class would then never be able to reach zero
        write_item(self.root, 'T-0279', 'task', 'Task', parent='F-0001', body=BARE_REF_BODY)
        run(['index'], self.root)
        r = run(['file-bugs', '--default-bug-epic', 'E-0009'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        run(['index'], self.root)
        check = run(['check'], self.root)
        self.assertNotIn('bugs/', check.stdout)

    def test_file_bugs_files_the_standing_record_error(self):
        write_item(self.root, 'T-0279', 'task', 'Task', parent='F-0001', body=BARE_REF_BODY)
        run(['index'], self.root)
        r = run(['file-bugs', '--default-bug-epic', 'E-0009'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('1 filed', r.stdout)
        filed = [n for n in os.listdir(os.path.join(self.root, 'bugs')) if n.endswith('.md')]
        self.assertEqual(len(filed), 1, filed)
        with open(os.path.join(self.root, 'bugs', filed[0])) as f:
            meta, body = frontmatter.parse(f.read(), path=f'bugs/{filed[0]}')
        self.assertEqual(meta['signature'],
                         'record error: bare decision reference …; write it as [[D-nnnn]]')
        self.assertEqual(meta['severity'], 'S3')
        self.assertIn('tasks/T-0279.md', body)
        # a second run the same day bumps nothing
        again = run(['file-bugs', '--default-bug-epic', 'E-0009'], self.root)
        self.assertIn('0 filed', again.stdout)

    def test_the_bug_this_tool_filed_is_not_itself_an_error_class(self):
        # with no usable Epic the filed Bug is unparented — a `check` finding a human fixes once,
        # never a class for the next run to file a Bug about (or every run files one more)
        write_item(self.root, 'T-0279', 'task', 'Task', parent='F-0001', body=BARE_REF_BODY)
        run(['index'], self.root)
        first = run(['file-bugs'], self.root)
        self.assertIn('1 filed', first.stdout)
        run(['index'], self.root)
        sigs = file_bugs.record_error_signatures(self.root)
        self.assertEqual(list(sigs),
                         ['record error: bare decision reference …; write it as [[D-nnnn]]'])
        second = run(['file-bugs'], self.root)
        self.assertIn('0 filed', second.stdout)


class OutcomeRateTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.now = datetime.datetime.now(datetime.timezone.utc)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    NOT_PUSHED_RESULT = 'failed: not pushed: 2 uncommitted file(s), 1 unpushed commit(s)'

    def _write_events(self, n_total, n_failing, result=NOT_PUSHED_RESULT):
        for i in range(n_total):
            write_session_line(self.root, today(), {
                'task': f'coder-t-{i:04d}', 'account': 'accta', 'item': f'T-{i:04d}',
                'branch': f'worker/T-{i:04d}',
                'result': result if i < n_failing else 'finished',
                'ts': iso(self.now - datetime.timedelta(minutes=i)),
            })

    def test_outcome_class_over_share_files_one_bug(self):
        self._write_events(49, 7)
        sigs = file_bugs.outcome_signatures(self.root, self.now)
        self.assertEqual(list(sigs), ['outcome: not pushed'])
        d = sigs['outcome: not pushed']
        self.assertEqual(d['severity'], 'S2')
        self.assertEqual(d['found_in'], 'dev')
        self.assertEqual(d['runs'], [])
        self.assertIn('14 %', d['title'])
        self.assertIn('threshold 10 %', d['title'])
        self.assertIn('7 of 49', d['title'])
        self.assertLessEqual(len(d['evidence']), 20)

    def test_four_of_fortynine_is_under_the_share(self):
        self._write_events(49, 4)
        self.assertEqual(file_bugs.outcome_signatures(self.root, self.now), {})

    def test_seventy_percent_under_the_session_floor_files_nothing(self):
        self._write_events(10, 7)
        self.assertEqual(file_bugs.outcome_signatures(self.root, self.now), {})

    def test_event_older_than_the_window_counts_in_neither_numerator_nor_denominator(self):
        self._write_events(49, 7)
        write_session_line(self.root, today(), {
            'task': 'coder-t-9999', 'account': 'accta', 'item': 'T-9999',
            'branch': 'worker/T-9999', 'result': self.NOT_PUSHED_RESULT,
            'ts': iso(self.now - datetime.timedelta(hours=30)),
        })
        sigs = file_bugs.outcome_signatures(self.root, self.now)
        self.assertIn('7 of 49', sigs['outcome: not pushed']['title'])

    def test_running_lines_move_no_percentage(self):
        self._write_events(49, 7)
        for i in range(5):
            write_session_line(self.root, today(), {
                'task': f'coder-running-{i}', 'account': 'accta', 'result': 'running',
                'ts': iso(self.now),
            })
        sigs = file_bugs.outcome_signatures(self.root, self.now)
        self.assertIn('7 of 49', sigs['outcome: not pushed']['title'])

    def test_second_run_same_day_is_a_no_op_then_the_next_day_bumps(self):
        self._write_events(49, 7)
        write_item(self.root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])
        run(['index'], self.root)
        info = file_bugs.outcome_signatures(self.root, self.now)['outcome: not pushed']

        by_id, _errors = load_items(self.root)
        canonical, _dupes = canonicalize(by_id)
        self.assertEqual(
            file_bugs._file_or_bump_bug(self.root, canonical, 'outcome: not pushed', info, today()),
            'filed')
        by_id, _errors = load_items(self.root)
        canonical, _dupes = canonicalize(by_id)
        rec = file_bugs._find_bug_by_signature(canonical, 'outcome: not pushed')
        with open(rec['path'], encoding='utf-8') as f:
            meta, _body = frontmatter.parse(f.read(), path=rec['relpath'])
        self.assertEqual(meta['found_in'], 'dev')
        self.assertEqual(meta['severity'], 'S2')
        first_title = meta['title']

        self.assertEqual(
            file_bugs._file_or_bump_bug(self.root, canonical, 'outcome: not pushed', info, today()),
            'skipped')

        tomorrow = (datetime.date.fromisoformat(today()) + datetime.timedelta(days=1)).isoformat()
        by_id, _errors = load_items(self.root)
        canonical, _dupes = canonicalize(by_id)
        self.assertEqual(
            file_bugs._file_or_bump_bug(self.root, canonical, 'outcome: not pushed', info, tomorrow),
            'bumped')
        by_id, _errors = load_items(self.root)
        canonical, _dupes = canonicalize(by_id)
        rec2 = file_bugs._find_bug_by_signature(canonical, 'outcome: not pushed')
        with open(rec2['path'], encoding='utf-8') as f:
            meta2, body2 = frontmatter.parse(f.read(), path=rec2['relpath'])
        self.assertEqual(meta2['count'], 2)
        self.assertEqual(meta2['title'], first_title)
        history = body2.split('## History\n', 1)[1].split('\n## ', 1)[0]
        history_lines = [l for l in history.splitlines() if l.strip()]
        self.assertEqual(len(history_lines), 2)


class RepeatFailureTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.now = datetime.datetime.now(datetime.timezone.utc)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_two_dead_pid_events_for_the_same_task_file_one_bug(self):
        for i in range(2):
            write_session_line(self.root, today(), {
                'task': f'coder-t-0091-r{i}', 'account': 'accta', 'item': 'T-0091',
                'branch': 'worker/T-0091', 'result': 'dead pid',
                'ts': iso(self.now - datetime.timedelta(hours=i)),
            })
        sigs = file_bugs.repeat_signatures(self.root, self.now)
        self.assertEqual(list(sigs), ['repeat: T-0091: dead pid'])
        d = sigs['repeat: T-0091: dead pid']
        self.assertEqual(d['severity'], 'S3')
        self.assertEqual(d['found_in'], 'dev')
        self.assertEqual(d['runs'], [])
        self.assertNotIn('links', d)
        self.assertIn('T-0091', d['title'])
        self.assertIn('dead pid', d['title'])
        self.assertEqual(len(d['evidence']), 2)

    def test_one_event_files_nothing(self):
        write_session_line(self.root, today(), {
            'task': 'coder-t-0091-r1', 'account': 'accta', 'item': 'T-0091',
            'branch': 'worker/T-0091', 'result': 'dead pid', 'ts': iso(self.now),
        })
        self.assertEqual(file_bugs.repeat_signatures(self.root, self.now), {})

    def test_two_different_failing_classes_on_the_same_task_do_not_combine(self):
        write_session_line(self.root, today(), {
            'task': 'coder-t-0091-r1', 'account': 'accta', 'item': 'T-0091',
            'branch': 'worker/T-0091', 'result': 'dead pid',
            'ts': iso(self.now - datetime.timedelta(hours=2)),
        })
        write_session_line(self.root, today(), {
            'task': 'coder-t-0091-r2', 'account': 'accta', 'item': 'T-0091',
            'branch': 'worker/T-0091',
            'result': 'failed: not pushed: 1 uncommitted file(s), 0 unpushed commit(s)',
            'ts': iso(self.now - datetime.timedelta(hours=1)),
        })
        self.assertEqual(file_bugs.repeat_signatures(self.root, self.now), {})

    def test_event_with_no_item_groups_on_and_is_named_by_its_task(self):
        for i in range(2):
            write_session_line(self.root, today(), {
                'task': 'spec-f-0096', 'account': 'accta', 'result': 'dead pid',
                'ts': iso(self.now - datetime.timedelta(hours=i)),
            })
        sigs = file_bugs.repeat_signatures(self.root, self.now)
        self.assertEqual(list(sigs), ['repeat: spec-f-0096: dead pid'])
        self.assertIn('spec-f-0096', sigs['repeat: spec-f-0096: dead pid']['title'])

    def test_raising_repeat_failure_n_turns_the_two_event_case_into_nothing_filed(self):
        for i in range(2):
            write_session_line(self.root, today(), {
                'task': f'coder-t-0091-r{i}', 'account': 'accta', 'item': 'T-0091',
                'branch': 'worker/T-0091', 'result': 'dead pid',
                'ts': iso(self.now - datetime.timedelta(hours=i)),
            })
        conv = Conventions.from_mapping({'repeat_failure_n': 3})
        self.assertEqual(file_bugs.repeat_signatures(self.root, self.now, conv), {})


class RefileWindowTests(unittest.TestCase):
    def test_too_soon_matrix(self):
        base = datetime.date(2026, 9, 30)
        cases = [
            (0, 1, True), (0, 7, True),
            (1, 1, False), (1, 7, True),
            (6, 7, True), (7, 7, False), (7, 1, False),
        ]
        for back, refile_days, expected in cases:
            last_filed = (base - datetime.timedelta(days=back)).isoformat()
            with self.subTest(back=back, refile_days=refile_days):
                self.assertIs(
                    file_bugs._too_soon(last_filed, base.isoformat(), refile_days), expected)

    def test_missing_last_filed_is_never_too_soon(self):
        self.assertFalse(file_bugs._too_soon(None, today(), 1))
        self.assertFalse(file_bugs._too_soon('', today(), 7))

    def test_unparseable_dates_fall_back_to_string_equality(self):
        self.assertTrue(file_bugs._too_soon('not-a-date', 'not-a-date', 1))
        self.assertFalse(file_bugs._too_soon('not-a-date', 'today', 1))
        self.assertFalse(file_bugs._too_soon('2026-09-29', 'not-a-date', 1))

    def test_default_refile_days_is_the_old_same_day_guard(self):
        root = make_repo()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        write_item(root, 'B-1000', 'bug', 'CI red: gate: flaky', typed_lines=[
            'severity: S2', 'found_in: ci', 'signature: gate: flaky', 'count: 1',
            'last_filed: 2026-09-29',
        ])
        by_id, _errors = load_items(root)
        canonical, _dupes = canonicalize(by_id)
        info = {'title': 'CI red: gate: flaky', 'severity': 'S2', 'evidence': ['seen again'],
                'runs': []}
        # a CI-failure Bug filed yesterday still bumps today under the default refile_days of 1
        self.assertEqual(
            file_bugs._file_or_bump_bug(root, canonical, 'gate: flaky', info, '2026-09-30'),
            'bumped')
        rec = canonical['B-1000']
        with open(rec['path'], encoding='utf-8') as f:
            meta, _body = frontmatter.parse(f.read(), path=rec['relpath'])
        self.assertEqual(meta['count'], 2)
        self.assertEqual(meta['last_filed'], '2026-09-30')
        self.assertEqual(meta['title'], 'CI red: gate: flaky')

        # one filed today is still unchanged
        write_item(root, 'B-1001', 'bug', 'CI red: gate: other', typed_lines=[
            'severity: S2', 'found_in: ci', 'signature: gate: other', 'count: 1',
            'last_filed: 2026-09-30',
        ])
        by_id, _errors = load_items(root)
        canonical, _dupes = canonicalize(by_id)
        self.assertEqual(
            file_bugs._file_or_bump_bug(root, canonical, 'gate: other', info, '2026-09-30'),
            'skipped')

    def test_refile_days_seven_skips_through_the_week_then_bumps(self):
        root = make_repo()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        write_item(root, 'B-2000', 'bug', 'ci waste: red rate', typed_lines=[
            'severity: S3', 'found_in: ci', 'signature: ci waste: red rate', 'count: 1',
            'last_filed: 2026-09-24',
        ])
        info = {'title': 'ci waste: red rate', 'severity': 'S3',
                'evidence': ['window over target'], 'runs': [], 'refile_days': 7}

        # six days back (2026-09-24 -> 2026-09-30): still inside the window
        by_id, _errors = load_items(root)
        canonical, _dupes = canonicalize(by_id)
        self.assertEqual(
            file_bugs._file_or_bump_bug(root, canonical, 'ci waste: red rate', info,
                                        '2026-09-30'),
            'skipped')

        # seven days back (2026-09-24 -> 2026-10-01): the window has passed
        by_id, _errors = load_items(root)
        canonical, _dupes = canonicalize(by_id)
        outcome = file_bugs._file_or_bump_bug(root, canonical, 'ci waste: red rate', info,
                                              '2026-10-01')
        self.assertEqual(outcome, 'bumped')
        rec = canonical['B-2000']
        with open(rec['path'], encoding='utf-8') as f:
            text = f.read()
        meta, body = frontmatter.parse(text, path=rec['relpath'])
        self.assertEqual(meta['count'], 2)
        self.assertEqual(meta['last_filed'], '2026-10-01')
        self.assertEqual(meta['title'], 'ci waste: red rate')
        history = body.split('## History\n', 1)[1].split('\n## ', 1)[0]
        history_lines = [l for l in history.splitlines() if l.strip()]
        self.assertEqual(len(history_lines), 2)
        self.assertIn('count 1 → 2', history_lines[-1])


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

    def test_the_filed_bug_states_what_is_wrong_and_carries_a_real_acceptance(self):
        # B-0101: the Description opens with a statement of the defect (not a bare evidence
        # list) and the Acceptance is a real, checkable line, never the empty `- [ ]`.
        r = run(['file-bugs', '--default-bug-epic', 'E-0009'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        name = [n for n in os.listdir(os.path.join(self.root, 'bugs')) if n.endswith('.md')][0]
        with open(os.path.join(self.root, 'bugs', name)) as f:
            meta, body = frontmatter.parse(f.read(), path=f'bugs/{name}')
        description = body.split('## Description\n', 1)[1].split('\n## ', 1)[0].strip()
        self.assertFalse(description.startswith('- '), description)
        self.assertIn(meta['title'], description.splitlines()[0])
        self.assertIn('run 100', description)
        acceptance = body.split('## Acceptance\n', 1)[1].split('\n## ', 1)[0]
        self.assertNotEqual(acceptance.strip(), '- [ ]')

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


class OneBugPerCauseTests(unittest.TestCase):
    """W4-PR3c: an invariant refusal files one Bug per ``(invariant, cause)``, not per path —
    the same I10 cause refused on several Features is one Bug naming every path."""

    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        write_item(self.root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])
        run(['index'], self.root)

    @staticmethod
    def i10(fid, tid):
        from asf.invariants import Finding
        return Finding('I10', 'record', fid, f'Resolved with open Task(s) {tid}',
                       paths=(f'features/{fid}.md',))

    def bugs(self):
        out = []
        for n in sorted(os.listdir(os.path.join(self.root, 'bugs'))):
            if n.endswith('.md'):
                with open(os.path.join(self.root, 'bugs', n), encoding='utf-8') as f:
                    out.append(frontmatter.parse(f.read(), path=f'bugs/{n}'))
        return out

    def test_two_findings_of_one_cause_are_one_signature(self):
        sigs = file_bugs.invariant_signatures([self.i10('F-0001', 'T-0001'), self.i10('F-0002', 'T-0007')])
        self.assertEqual(list(sigs), ['invariant I10: Resolved with open Task(s) …'])
        d = sigs['invariant I10: Resolved with open Task(s) …']
        self.assertEqual(d['places'], 2)
        self.assertTrue(any(e.startswith('features/F-0001.md') for e in d['evidence']))
        self.assertTrue(any(e.startswith('features/F-0002.md') for e in d['evidence']))

    def test_a_different_cause_is_a_different_bug(self):
        from asf.invariants import Finding
        other = Finding('I10', 'record', 'F-0003', 'Closed with no landing', paths=('features/F-0003.md',))
        sigs = file_bugs.invariant_signatures([self.i10('F-0001', 'T-0001'), other])
        self.assertEqual(len(sigs), 2)

    def test_the_cause_drops_the_paths_the_message_names(self):
        from asf.invariants import Finding
        f = Finding('I3', 'record', 'T-0002', "tasks/T-0002.md writes src/a.py, overlapping T-0001",
                    paths=('tasks/T-0002.md',))
        self.assertEqual(file_bugs.cause_key(f), '… writes src/a.py, overlapping …')

    def test_one_bug_filed_and_a_later_path_of_the_same_cause_is_a_history_line(self):
        out = []
        got = file_bugs.file_invariant_bugs(self.root, [self.i10('F-0001', 'T-0001'),
                                                        self.i10('F-0002', 'T-0007')], out=out.append)
        self.assertEqual(list(got.values()), ['filed'])
        bugs = self.bugs()
        self.assertEqual(len(bugs), 1)
        meta, body = bugs[0]
        self.assertEqual(meta['signature'], 'invariant I10: Resolved with open Task(s) …')
        self.assertEqual(meta['places'], 2)
        self.assertIn('features/F-0001.md', body)
        self.assertIn('features/F-0002.md', body)
        # the same day, the same cause on a third Feature: no second Bug, a History line
        got = file_bugs.file_invariant_bugs(self.root, [self.i10('F-0003', 'T-0009')], out=out.append)
        self.assertEqual(list(got.values()), ['bumped'])
        bugs = self.bugs()
        self.assertEqual(len(bugs), 1)
        history = bugs[0][1].split('## History', 1)[1]
        self.assertIn('also features/F-0003.md', history)
        # and the same path again adds nothing
        got = file_bugs.file_invariant_bugs(self.root, [self.i10('F-0003', 'T-0009')], out=out.append)
        self.assertEqual(list(got.values()), ['skipped'])

    def test_findings_naming_a_different_number_of_tasks_are_one_bug_and_a_history_line(self):
        out = []
        got = file_bugs.file_invariant_bugs(self.root, [self.i10('F-0001', 'T-0001, T-0002')],
                                            out=out.append)
        self.assertEqual(list(got.values()), ['filed'])
        self.assertEqual(self.bugs()[0][0]['signature'],
                         'invariant I10: Resolved with open Task(s) …')
        got = file_bugs.file_invariant_bugs(self.root, [self.i10('F-0002', 'T-0007')],
                                            out=out.append)
        self.assertEqual(list(got.values()), ['bumped'])
        bugs = self.bugs()
        self.assertEqual(len(bugs), 1)
        self.assertIn('also features/F-0002.md', bugs[0][1].split('## History', 1)[1])


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


class RuleCheckFailureTests(unittest.TestCase):
    """A rule check that times out is a check failure, not a violation: no product Bug, one
    ``rule check timed out`` line per run, one factory-side line once it persists. A real
    violation files a Bug with an Acceptance; a removed default Epic parents nothing."""

    def setUp(self):
        self.root = make_repo()
        self.state = tempfile.mkdtemp(prefix='filebugs_state_')
        write_item(self.root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.state, ignore_errors=True)

    def _file_bugs(self, epic='E-0009'):
        import argparse
        import contextlib
        import io
        args = argparse.Namespace(default_bug_epic=epic, file_bug_level='auto',
                                  conventions=Conventions(), state_dir=self.state)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = file_bugs.cmd_file_bugs(args, self.root)
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def _bugs(self):
        out = []
        for n in sorted(os.listdir(os.path.join(self.root, 'bugs'))):
            with open(os.path.join(self.root, 'bugs', n), encoding='utf-8') as f:
                out.append(frontmatter.parse(f.read(), path=f'bugs/{n}'))
        return out

    def _slow_rule(self):
        write_rule(self.root, 'R-0131', 'Docker stays off',
                   typed_lines=['scope: tick', 'check: tools/checks/r0131.sh'])
        write_check_script(self.root, 'r0131.sh', "#!/usr/bin/env bash\nsleep 30\n")

    def _violated_rule(self):
        write_rule(self.root, 'R-0001', 'Never merge red',
                   typed_lines=['scope: merge', 'check: tools/checks/r0001.sh'])
        write_check_script(self.root, 'r0001.sh',
                           "#!/usr/bin/env bash\necho 'R-0001 merged red sha=abc1234'\nexit 1\n")

    def _with_timeout(self, fn):
        # the check runs in a subprocess (`asf.rules.rules`): shorten its timeout there
        old = os.environ.get('ASF_RULE_CHECK_TIMEOUT')
        os.environ['ASF_RULE_CHECK_TIMEOUT'] = '1'
        try:
            return fn()
        finally:
            if old is None:
                os.environ.pop('ASF_RULE_CHECK_TIMEOUT', None)
            else:
                os.environ['ASF_RULE_CHECK_TIMEOUT'] = old

    def test_a_timed_out_check_files_no_bug_and_says_so(self):
        self._slow_rule()
        run(['index'], self.root)
        out = self._with_timeout(self._file_bugs)
        self.assertEqual(self._bugs(), [])
        self.assertIn('rule check timed out: R-0131', out)
        self.assertIn('0 filed', out)

    def test_a_check_that_keeps_timing_out_surfaces_once_as_a_factory_problem(self):
        self._slow_rule()
        run(['index'], self.root)
        outs = [self._with_timeout(self._file_bugs)
                for _ in range(file_bugs.CHECK_FAILURE_RUNS_TO_SURFACE + 1)]
        needs = [o for o in outs if 'NEEDS OPERATOR: rule check timed out: R-0131' in o]
        self.assertEqual(len(needs), 1)
        self.assertEqual(self._bugs(), [])
        from asf import doctor
        ok, detail = doctor.check_rule_checks(
            None, path=os.path.join(self.state, file_bugs.LEDGER_NAME))
        self.assertFalse(ok)
        self.assertIn('rule check timed out: R-0131', detail)

    def test_a_violation_files_a_bug_whose_acceptance_names_the_check(self):
        self._violated_rule()
        run(['index'], self.root)
        self._file_bugs()
        bugs = self._bugs()
        self.assertEqual(len(bugs), 1)
        meta, body = bugs[0]
        self.assertEqual(meta['signature'], 'R-0001: rule violated')
        self.assertEqual(meta['parent'], 'E-0009')
        acceptance = body.split('## Acceptance\n', 1)[1].split('\n## ', 1)[0]
        self.assertNotEqual(acceptance.strip(), '- [ ]')
        self.assertIn('R-0001', acceptance)
        self.assertIn('bash tools/checks/r0001.sh', acceptance)

    def test_a_rule_retired_since_the_last_index_files_no_bug(self):
        self._violated_rule()
        run(['index'], self.root)
        card = os.path.join(self.root, 'rules', 'R-0001.md')
        with open(card, encoding='utf-8') as f:
            text = f.read()
        with open(card, 'w', encoding='utf-8') as f:
            f.write(text.replace('# ---- machine ----', 'removed: retired\n# ---- machine ----', 1))
        self._file_bugs()
        self.assertEqual(self._bugs(), [])

    def test_a_rule_card_deleted_since_the_last_index_files_no_bug(self):
        self._violated_rule()
        run(['index'], self.root)
        os.remove(os.path.join(self.root, 'rules', 'R-0001.md'))
        self._file_bugs()
        self.assertEqual(self._bugs(), [])

    def test_a_removed_default_epic_parents_nothing_and_is_logged_once(self):
        write_item(self.root, 'E-0009', 'epic', 'Factory',
                   typed_lines=['decided: true', 'removed: "moved elsewhere"'])
        self._violated_rule()
        run(['index'], self.root)
        first = self._file_bugs()
        (meta, _body), = self._bugs()
        self.assertNotIn('parent', meta)
        self.assertIn('default_bug_epic E-0009 is removed', first)
        second = self._file_bugs()
        self.assertNotIn('default_bug_epic E-0009 is removed', second)

    def test_a_closed_default_epic_parents_nothing(self):
        write_item(self.root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'],
                   machine_lines=['state: Closed', 'stage_since: 2026-09-01T00:00:00Z',
                                  'updated: 2026-09-01T00:00:00Z'])
        self._violated_rule()
        run(['index'], self.root)
        self._file_bugs()
        (meta, _body), = self._bugs()
        self.assertNotIn('parent', meta)
