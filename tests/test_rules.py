import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from asf.rules import rules

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
FOLDERS = ['epics', 'features', 'stories', 'tasks', 'bugs', 'decisions', 'rules']

RULE_BODY = (
    "## Statement\n\n"
    "## Why\n\n"
    "## Check\n\n"
    "## Source\n\n"
    "## Children\n\n"
    "## Backlinks\n"
)


def make_repo():
    root = tempfile.mkdtemp(prefix='rules_test_')
    for f in FOLDERS:
        os.makedirs(os.path.join(root, f))
    os.makedirs(os.path.join(root, 'tools', 'checks'))
    return root


def write_rule(root, rid, title, typed_lines=()):
    lines = [f"id: {rid}", 'type: rule', f"title: {title}"]
    lines.extend(typed_lines)
    lines.append('# ---- machine ----')
    lines.append('state: New')
    lines.append('updated: 2026-09-21T00:00:00Z')
    header = '\n'.join(lines)
    path = os.path.join(root, 'rules', f"{rid}.md")
    with open(path, 'w', encoding='utf-8') as f:
        f.write(f"---\n{header}\n---\n{RULE_BODY}")
    return path


def write_check(root, name, script):
    path = os.path.join(root, 'tools', 'checks', name)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(script)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


def _parse_frontmatter(text):
    """A minimal parser for this module's flat `key: value` frontmatter blocks — just enough
    to build the index.json shape rules.py's load_rules() reads, without depending on the
    full backlog.py toolchain (out of scope for this port; see `reindex()` below)."""
    if not text.startswith('---\n'):
        return {}
    end = text.find('\n---', 4)
    if end == -1:
        return {}
    data = {}
    for line in text[4:end].splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, _, value = line.partition(':')
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        elif value == 'true':
            value = True
        elif value == 'false':
            value = False
        data[key] = value
    return data


def reindex(root):
    """Rebuild index.json straight from the fixture cards' frontmatter. The original test
    shelled out to `backlog.py index`; that CLI (and the record/frontmatter machinery behind
    it) is a separate module not ported alongside rules.py, so this reads the same fixture
    files rules.py itself will read and writes the index.json shape it expects."""
    items = {}
    for folder in FOLDERS:
        d = os.path.join(root, folder)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith('.md'):
                continue
            with open(os.path.join(d, name), encoding='utf-8') as f:
                entry = _parse_frontmatter(f.read())
            iid = entry.get('id') or name[:-3]
            items[iid] = entry
    with open(os.path.join(root, 'index.json'), 'w', encoding='utf-8') as f:
        json.dump({'items': items}, f, indent=2, sort_keys=True)


def run_rules(root, args):
    env = dict(os.environ)
    env['BACKLOG_ROOT'] = root
    env['PYTHONPATH'] = PROJECT_ROOT + os.pathsep + env.get('PYTHONPATH', '')
    return subprocess.run([sys.executable, '-m', 'asf.rules.rules'] + args, cwd=root, env=env,
                          capture_output=True, text=True)


class RulesCheckTests(unittest.TestCase):
    """Three fixture rules: one whose check passes, one whose check fails,
    one that is honestly unenforced."""

    def setUp(self):
        self.root = make_repo()
        write_rule(self.root, 'R-0001', 'Main is frozen while a batch is in CI',
                   typed_lines=['scope: merge', 'check: tools/checks/r0001.sh'])
        write_check(self.root, 'r0001.sh', "#!/usr/bin/env bash\nexit 0\n")
        write_rule(self.root, 'R-0002', 'Never merge without a gate on that sha',
                   typed_lines=['scope: ci', 'check: tools/checks/r0002.sh'])
        write_check(self.root, 'r0002.sh',
                    "#!/usr/bin/env bash\n"
                    "echo 'R-0002 merge without a gate sha=abc1234 2026-09-21T06:00:00Z'\n"
                    "exit 1\n")
        write_rule(self.root, 'R-0003', 'One question at a time',
                   typed_lines=['scope: console', 'enforced: false',
                                'reason: "a check cannot read the console"'])
        reindex(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_header_counts_and_violation_line(self):
        proc = run_rules(self.root, ['check'])
        self.assertEqual(proc.returncode, 1, proc.stderr)
        lines = proc.stdout.strip().split('\n')
        self.assertEqual(lines[0], '== RULES 2 checked, 1 violations, 1 unenforced')
        self.assertEqual(
            lines[1],
            'R-0002 merge without a gate sha=abc1234 2026-09-21T06:00:00Z')
        self.assertEqual(len(lines), 2)

    def test_verbose_lists_the_unenforced_with_reasons(self):
        proc = run_rules(self.root, ['check', '--verbose'])
        self.assertIn('-- unenforced (1)', proc.stdout)
        self.assertIn('R-0003 a check cannot read the console', proc.stdout)

    def test_json_shape_for_the_bug_filer(self):
        proc = run_rules(self.root, ['check', '--json'])
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload['violations'], [{
            'rule': 'R-0002',
            'line': 'R-0002 merge without a gate sha=abc1234 2026-09-21T06:00:00Z',
        }])
        self.assertEqual(payload['unenforced'], [{
            'rule': 'R-0003',
            'reason': 'a check cannot read the console',
        }])

    def test_exit_zero_when_nothing_is_violated(self):
        os.remove(os.path.join(self.root, 'rules', 'R-0002.md'))
        reindex(self.root)
        proc = run_rules(self.root, ['check'])
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(proc.stdout.strip(),
                         '== RULES 1 checked, 0 violations, 1 unenforced')

    def test_runs_from_an_unrelated_cwd(self):
        """factory-health.sh calls rules.py by absolute path from elsewhere; the packaged
        equivalent is invoking the module from an unrelated cwd with BACKLOG_ROOT set."""
        env = dict(os.environ)
        env['BACKLOG_ROOT'] = self.root
        env['PYTHONPATH'] = PROJECT_ROOT + os.pathsep + env.get('PYTHONPATH', '')
        proc = subprocess.run([sys.executable, '-m', 'asf.rules.rules', 'check'],
                              cwd=tempfile.gettempdir(), env=env,
                              capture_output=True, text=True)
        self.assertIn('== RULES 2 checked, 1 violations, 1 unenforced',
                      proc.stdout)


class CheckFailureModeTests(unittest.TestCase):
    """An unrunnable check is a violation of the check, not a pass."""

    def setUp(self):
        self.root = make_repo()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _one_rule(self, script=None, check='tools/checks/r0009.sh'):
        write_rule(self.root, 'R-0009', 'A rule with a check',
                   typed_lines=['scope: tick', f"check: {check}"])
        if script is not None:
            write_check(self.root, 'r0009.sh', script)
        reindex(self.root)
        return rules.load_rules(self.root)[0]

    def test_timeout_is_reported_as_a_violation(self):
        rule = self._one_rule("#!/usr/bin/env bash\nsleep 30\n")
        old = rules.TIMEOUT
        rules.TIMEOUT = 1
        try:
            lines = rules.run_check(self.root, rule)
        finally:
            rules.TIMEOUT = old
        self.assertEqual(len(lines), 1)
        self.assertIn('check timed out after 1s', lines[0])
        self.assertTrue(lines[0].startswith('R-0009 '))

    def test_missing_script_is_a_violation(self):
        rule = self._one_rule(script=None)
        self.assertEqual(rules.run_check(self.root, rule),
                         ['R-0009 check script missing tools/checks/r0009.sh'])

    def test_crash_is_a_violation_carrying_the_first_stderr_line(self):
        rule = self._one_rule("#!/usr/bin/env bash\necho boom >&2\nexit 2\n")
        lines = rules.run_check(self.root, rule)
        self.assertEqual(
            lines, ['R-0009 check failed exit 2 tools/checks/r0009.sh boom'])

    def test_exit_one_with_no_output_is_a_violation(self):
        rule = self._one_rule("#!/usr/bin/env bash\nexit 1\n")
        lines = rules.run_check(self.root, rule)
        self.assertEqual(
            lines,
            ['R-0009 check exited 1 with no violation line tools/checks/r0009.sh'])

    def test_violation_lines_are_prefixed_with_the_rule_id_when_missing(self):
        rule = self._one_rule("#!/usr/bin/env bash\necho 'main moved x y'\nexit 1\n")
        self.assertEqual(rules.run_check(self.root, rule),
                         ['R-0009 main moved x y'])

    def test_one_line_per_violation(self):
        rule = self._one_rule(
            "#!/usr/bin/env bash\necho 'R-0009 a'\necho 'R-0009 b'\nexit 1\n")
        self.assertEqual(rules.run_check(self.root, rule),
                         ['R-0009 a', 'R-0009 b'])

    def test_a_card_with_neither_check_nor_enforced_false_is_a_violation(self):
        write_rule(self.root, 'R-0010', 'A rule nobody finished',
                   typed_lines=['scope: tick'])
        reindex(self.root)
        proc = run_rules(self.root, ['check'])
        self.assertEqual(proc.returncode, 1)
        self.assertIn(
            'R-0010 rule card has neither check: nor enforced: false',
            proc.stdout)


@unittest.skipUnless(
    os.path.isfile(os.path.join(PROJECT_ROOT, 'index.json')),
    "no index.json at the repo root yet — this package's own rules/ library "
    "(README: 'rules/ — the core rule cards and their checks') has not been authored "
    "in this skeleton; this class exercises it once it exists.")
class RuleCardTests(unittest.TestCase):
    """The repository's own rule cards: every one is honest about whether it
    is enforced, and every `check:` points at a runnable script."""

    ROOT = PROJECT_ROOT

    def cards(self):
        return rules.load_rules(self.ROOT)

    def test_there_are_rule_cards(self):
        self.assertGreater(len(self.cards()), 50)

    def test_every_card_is_a_check_or_an_honest_reason(self):
        for rule in self.cards():
            with self.subTest(rule=rule['id']):
                if rule.get('enforced') is False:
                    self.assertTrue((rule.get('reason') or '').strip(),
                                    'enforced: false needs a reason')
                else:
                    self.assertTrue(rule.get('check'),
                                    'a card is a check: or an enforced: false')

    def test_every_check_script_exists_and_is_executable(self):
        for rule in self.cards():
            script = rule.get('check')
            if not script:
                continue
            with self.subTest(rule=rule['id']):
                self.assertEqual(
                    script, f"tools/checks/{rule['id'].replace('-', '').lower()}.sh",
                    'a check script is named after its rule')
                path = os.path.join(self.ROOT, script)
                self.assertTrue(os.path.isfile(path), f"{script} is missing")
                self.assertTrue(os.access(path, os.X_OK), f"{script} is not executable")

    def test_every_check_script_parses(self):
        for rule in self.cards():
            script = rule.get('check')
            if not script:
                continue
            with self.subTest(rule=rule['id']):
                proc = subprocess.run(
                    ['bash', '-n', os.path.join(self.ROOT, script)],
                    capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_every_card_carries_a_scope_and_a_source(self):
        scopes = {'tick', 'merge', 'launch', 'ci', 'session', 'docs',
                  'console', 'quota', 'infra'}
        for rule in self.cards():
            with self.subTest(rule=rule['id']):
                self.assertIn(rule.get('scope'), scopes)
                self.assertGreater(len(rule.get('source') or ''), 20)

    def test_supersedes_points_at_a_rule_that_exists(self):
        ids = {r['id'] for r in self.cards()}
        for rule in self.cards():
            for other in rule.get('supersedes') or []:
                with self.subTest(rule=rule['id']):
                    self.assertIn(other, ids)

    def test_no_check_script_calls_a_mutating_command(self):
        """Read-only by rule: exit 0 or 1, and never a command that changes
        a repository, a runner, a session or a CI run."""
        banned = [
            'git push', 'git fetch', 'git commit', 'git merge ', 'git checkout',
            'git worktree add', 'git branch -', 'gh run cancel', 'gh pr merge',
            'gh pr close', 'gh api -X', 'gh api --method', 'cux switch',
            'docker compose', 'docker rm', 'docker stop', 'kill -9', 'rm -rf',
        ]
        d = os.path.join(self.ROOT, 'tools', 'checks')
        for name in sorted(os.listdir(d)):
            if not name.endswith('.sh'):
                continue
            with open(os.path.join(d, name), encoding='utf-8') as f:
                text = f.read()
            for token in banned:
                with self.subTest(script=name, token=token):
                    self.assertNotIn(token, text)


class LoadRulesTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_missing_index_is_an_error_not_a_pass(self):
        proc = run_rules(self.root, ['check'])
        self.assertEqual(proc.returncode, 2)
        self.assertIn('index.json is missing', proc.stderr)

    def test_only_rule_items_are_loaded(self):
        write_rule(self.root, 'R-0001', 'A rule',
                   typed_lines=['enforced: false', 'reason: "no data yet"'])
        with open(os.path.join(self.root, 'epics', 'E-0009.md'), 'w',
                  encoding='utf-8') as f:
            f.write("---\nid: E-0009\ntype: epic\ntitle: The factory\n"
                    "# ---- machine ----\nstate: New\n---\n## Description\n")
        reindex(self.root)
        loaded = rules.load_rules(self.root)
        self.assertEqual([r['id'] for r in loaded], ['R-0001'])

    def test_removed_or_moved_rules_are_not_loaded(self):
        write_rule(self.root, 'R-0001', 'Live', typed_lines=['check: tools/checks/r1.sh'])
        write_rule(self.root, 'R-0002', 'Moved', typed_lines=[
            'check: tools/checks/r2.sh', 'moved_to: other:R-0002', 'removed: moved'])
        write_rule(self.root, 'R-0003', 'Removed', typed_lines=[
            'check: tools/checks/r3.sh', 'removed: retired'])
        reindex(self.root)
        self.assertEqual([r['id'] for r in rules.load_rules(self.root)], ['R-0001'])

    def test_checks_run_in_parallel(self):
        """Ten one-second checks finish well inside a serial ten seconds."""
        import time
        for n in range(1, 11):
            rid = f"R-{n:04d}"
            write_rule(self.root, rid, f"Rule {n}",
                       typed_lines=[f"check: tools/checks/r{n:04d}.sh"])
            write_check(self.root, f"r{n:04d}.sh",
                        "#!/usr/bin/env bash\nsleep 1\nexit 0\n")
        reindex(self.root)
        loaded = rules.load_rules(self.root)
        started = time.monotonic()
        self.assertEqual(rules.run_all(self.root, loaded), [])
        self.assertLess(time.monotonic() - started, 6.0)


if __name__ == '__main__':
    unittest.main()


class CoreRulesTests(unittest.TestCase):
    """B-0021: the core check scripts ship in the ASF repo's `rules/`; a card's `check:` resolves
    against the product first, then the core set; a card whose script is in neither is a violation."""

    def setUp(self):
        self.root = make_repo()
        self.core = tempfile.mkdtemp(prefix='core_rules_')
        self.home = tempfile.mkdtemp(prefix='asf_home_')
        write_rule(self.root, 'R-0001', 'A core rule',
                   typed_lines=['scope: tick', 'check: tools/checks/r0001.sh'])
        reindex(self.root)

    def tearDown(self):
        for d in (self.root, self.core, self.home):
            shutil.rmtree(d, ignore_errors=True)

    def _core_script(self, name, script):
        path = os.path.join(self.core, name)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(script)

    def _run(self, args, root=None):
        env = dict(os.environ)
        env['BACKLOG_ROOT'] = root or self.root
        env['ASF_HOME'] = self.home
        env['ASF_CORE_RULES_DIR'] = self.core
        env['PYTHONPATH'] = PROJECT_ROOT + os.pathsep + env.get('PYTHONPATH', '')
        return subprocess.run([sys.executable, '-m', 'asf.rules.rules'] + args, cwd=self.root,
                              env=env, capture_output=True, text=True)

    def test_core_check_script_runs_when_the_product_has_none(self):
        self._core_script('r0001.sh', "echo 'core rule broken here since today'\nexit 1\n")
        proc = self._run(['check'])
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip().split('\n'),
                         ['== RULES 1 checked, 1 violations, 0 unenforced',
                          'R-0001 core rule broken here since today'])

    def test_a_rule_with_no_script_anywhere_is_a_violation(self):
        proc = self._run(['check'])
        self.assertEqual(proc.returncode, 1)
        self.assertIn('R-0001 check script missing tools/checks/r0001.sh', proc.stdout)

    def test_the_product_script_wins_over_the_core_one(self):
        self._core_script('r0001.sh', "echo broken\nexit 1\n")
        write_check(self.root, 'r0001.sh', "#!/usr/bin/env bash\nexit 0\n")
        proc = self._run(['check'])
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_check_takes_a_product(self):
        self._core_script('r0001.sh', "exit 0\n")
        os.makedirs(os.path.join(self.home, 'products'))
        with open(os.path.join(self.home, 'products', 'p.yaml'), 'w') as f:
            f.write(f"product: p\nbacklog_dir: {self.root}\n")
        proc = self._run(['check', '--product', 'p'], root='/nonexistent')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), '== RULES 1 checked, 0 violations, 0 unenforced')
