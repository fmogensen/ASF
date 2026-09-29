"""The amendable set (F-0024): named once, every glob derived from the module that owns it; the
tenth class that refuses a session's write to it and cannot be widened, granted away, or made to
park a wave.
"""
import importlib
import io
import json
import os
import shutil
import tempfile
import unittest

from asf import amendable, approvals, env, hooks
from asf.env import Product
from asf.record import core
from asf.tick import tick
from tests.test_tick import TickTestCase, _git
build = importlib.import_module('asf.briefs.build')  # `asf.briefs.build` the attribute is a function

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def product(**conventions):
    return Product('demo', {'main': 'main', 'conventions': conventions})


class SetTests(unittest.TestCase):
    def kinds(self, **conventions):
        return {k.name: k for k in amendable.kinds(product(**conventions))}

    def test_the_six_kinds_in_order(self):
        self.assertEqual([k.name for k in amendable.kinds(product())],
                         ['rule_cards', 'checks', 'hooks', 'role_agents', 'briefs', 'evals'])
        for k in amendable.kinds(product()):
            self.assertTrue(k.why)

    def test_every_glob_is_the_constant_it_came_from(self):
        rules = core.TYPES['rule'][0]
        k = self.kinds(briefs_dir='docs/briefs', evals_dir='qa')
        self.assertEqual(k['rule_cards'].globs, (f'{rules}/*.md', f'{rules}/index.json'))
        self.assertEqual(k['checks'].globs, (f'{rules}/*.sh', hooks.CHECKS_DIR + '/*'))
        self.assertEqual(k['hooks'].globs,
                         tuple(hooks.RUNTIME_SETTINGS_GLOBS) + tuple(hooks.GIT_HOOK_GLOBS))
        templates = os.path.relpath(build.TEMPLATES_DIR, REPO_ROOT)
        self.assertEqual(k['role_agents'].globs,
                         (templates + '/*.md',) + tuple(hooks.RUNTIME_AGENT_GLOBS))
        self.assertEqual(k['briefs'].globs, ('docs/briefs/*',))
        self.assertEqual(k['evals'].globs, ('qa/*',))

    def test_evals_default_and_no_briefs_glob_when_unset(self):
        k = self.kinds()
        self.assertEqual(k['evals'].globs, ('evals/*',))
        self.assertEqual(k['briefs'].globs, ())

    def test_paths_unset_is_the_defaults(self):
        p = product()
        self.assertEqual(amendable.source(p), 'default')
        self.assertEqual(amendable.paths(p),
                         tuple(g for k in amendable.kinds(p) for g in k.globs))

    def test_paths_a_list_is_that_list(self):
        p = product(amendable_paths=['docs/CONSTITUTION.md'])
        self.assertEqual(amendable.source(p), 'yaml')
        self.assertEqual(amendable.paths(p), ('docs/CONSTITUTION.md',))

    def test_paths_empty_list_is_empty(self):
        p = product(amendable_paths=[])
        self.assertEqual(amendable.source(p), 'yaml')
        self.assertEqual(amendable.paths(p), ())

    def test_kind_of_one_path_per_kind(self):
        p = product(briefs_dir='docs/briefs')
        for path, kind in (
            ('rules/R-0042.md', 'rule_cards'), ('rules/index.json', 'rule_cards'),
            ('rules/R-0042.sh', 'checks'), ('tools/checks/x.sh', 'checks'),
            ('.githooks/pre-commit', 'hooks'), ('.claude/settings.json', 'hooks'),
            ('asf/briefs/templates/coder.md', 'role_agents'),
            ('.claude/agents/coder.md', 'role_agents'),
            ('docs/briefs/coder.md', 'briefs'), ('evals/e1.yaml', 'evals'),
        ):
            self.assertEqual(amendable.kind_of(p, path).name, kind, path)

    def test_kind_of_none_outside_the_set(self):
        p = product(briefs_dir='docs/briefs')
        for path in ('docs/specs/f-0024.md', 'plugin/skills/status/SKILL.md', 'asf/approvals.py'):
            self.assertIsNone(amendable.kind_of(p, path), path)

    def test_reaches(self):
        p = product()
        self.assertEqual(amendable.reaches(p, ['rules/*']), 'rules/*')
        self.assertEqual(amendable.reaches(p, ['rules/R-0042.md']), 'rules/R-0042.md')
        self.assertIsNone(amendable.reaches(p, ['docs/**']))
        self.assertEqual(amendable.reaches(p, ['docs/**', 'rules/*']), 'rules/*')

    def test_partition_splits_inside_first_each_side_in_order(self):
        p = product()
        self.assertEqual(amendable.partition(p, ['rules/*']), (['rules/*'], []))
        self.assertEqual(amendable.partition(p, ['docs/**']), ([], ['docs/**']))
        self.assertEqual(amendable.partition(p, ['docs/**', 'rules/*']),
                          (['rules/*'], ['docs/**']))
        self.assertEqual(amendable.partition(p, []), ([], []))

    def test_an_excluded_glob_lands_in_outside(self):
        p = product(amendable_paths=['docs/process/*', '!docs/process/evidence/*'])
        self.assertEqual(amendable.partition(p, ['docs/process/evidence/x/*']),
                          ([], ['docs/process/evidence/x/*']))

    def test_reaches_is_partitions_first_inside(self):
        p = product()
        cases = [['rules/*'], ['docs/**'], ['docs/**', 'rules/*'], []]
        for globs in cases:
            inside, _outside = amendable.partition(p, globs)
            self.assertEqual(amendable.reaches(p, globs), (inside or [None])[0])
        pe = product(amendable_paths=['docs/process/*', '!docs/process/evidence/*'])
        globs = ['docs/process/evidence/x/*']
        inside, _outside = amendable.partition(pe, globs)
        self.assertEqual(amendable.reaches(pe, globs), (inside or [None])[0])

    def test_glob_set_is_the_pair_and_threads_the_same_answer(self):
        p = product()
        gs = amendable.glob_set(p)
        self.assertEqual(gs, (amendable.paths(p), amendable.excluded(p)))
        for globs in [['rules/*'], ['docs/**'], ['docs/**', 'rules/*'], []]:
            self.assertEqual(amendable.partition(p, globs, glob_set=gs),
                              amendable.partition(p, globs))
            self.assertEqual(amendable.reaches(p, globs, glob_set=gs),
                              amendable.reaches(p, globs))
        pe = product(amendable_paths=['docs/process/*', '!docs/process/evidence/*'])
        gse = amendable.glob_set(pe)
        globs = ['docs/process/evidence/x/*']
        self.assertEqual(amendable.partition(pe, globs, glob_set=gse),
                          amendable.partition(pe, globs))
        self.assertEqual(amendable.reaches(pe, globs, glob_set=gse),
                          amendable.reaches(pe, globs))

    def test_write_target(self):
        p = product()
        hit = amendable.write_target(p, 'Write', {'file_path': 'rules/R-0099.md'}, REPO_ROOT)
        self.assertEqual((hit[0], hit[1].name), ('rules/R-0099.md', 'rule_cards'))
        hit = amendable.write_target(
            p, 'Bash', {'command': 'echo x > .githooks/pre-commit'}, REPO_ROOT)
        self.assertEqual(hit[1].name, 'hooks')
        self.assertIsNone(
            amendable.write_target(p, 'Bash', {'command': 'cat rules/R-0042.md'}, REPO_ROOT))
        self.assertIsNone(amendable.write_target(
            p, 'Write', {'file_path': 'docs/specs/f-0024.md'}, REPO_ROOT))

    def test_write_target_a_listed_path_outside_every_kind_still_has_a_kind(self):
        # a product's own amendable_paths may name a path no built-in kind's globs cover
        # (a process doc): the hit carries the "listed" kind, never None — a None kind
        # crashed the approvals hook ("'NoneType' object has no attribute 'name'"), and the
        # crash read as a refusal of whatever the session was doing
        p = product(amendable_paths=['docs/process/*'])
        # a read with its errors sent to /dev/null writes nothing
        self.assertIsNone(amendable.write_target(
            p, 'Bash', {'command': 'ls docs/process/README.md 2>/dev/null'}, REPO_ROOT))
        self.assertIsNone(amendable.write_target(
            p, 'Bash', {'command': 'grep x docs/process/README.md 2>&1'}, REPO_ROOT))
        hit = amendable.write_target(
            p, 'Bash', {'command': 'echo x > docs/process/README.md'}, REPO_ROOT)
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], 'docs/process/README.md')
        self.assertEqual(hit[1].name, 'listed')
        self.assertTrue(hit[1].why)

    def test_a_bang_entry_excludes_a_subtree_from_the_set(self):
        # process rules are protected; a Task's evidence file under them is not
        from asf import approvals
        p = product(amendable_paths=['docs/process/*', '!docs/process/evidence/*'])
        self.assertNotIn('!docs/process/evidence/*', amendable.paths(p))
        self.assertIsNone(amendable.write_target(
            p, 'Write', {'file_path': 'docs/process/evidence/2026-09-21/t3.md'}, REPO_ROOT))
        self.assertIsNotNone(amendable.write_target(
            p, 'Write', {'file_path': 'docs/process/rules.md'}, REPO_ROOT))
        self.assertEqual(approvals.merge_class(p, ['docs/process/evidence/a/b.md']),
                         ('merge_routine_pr', None))
        self.assertEqual(approvals.merge_class(p, ['docs/process/rules.md'])[0],
                         'merge_amendable_set')
        self.assertIsNone(amendable.reaches(p, ['docs/process/evidence/x/*']))

    def test_format_set_has_a_row_per_kind(self):
        rows = amendable.format_set(product())
        self.assertEqual(len(rows), 6)
        self.assertTrue(rows[0].startswith('rule_cards'))


class HookTests(unittest.TestCase):
    """§3.2 — a session that writes to the set is refused by the hook, before the matrix is even
    read (D7): the five lines of §2.3 on stderr, naming the path and the kind, and one ``refused``
    ledger line carrying ``class``, ``kind``, ``path`` and ``patch``. A command that names no file
    (``asf new rule``, ``asf set R-nnnn``, ``asf hooks install``) is still refused, through the
    ordinary matrix — :func:`amendable.write_target` "answers 'which file', and a command that
    names no file has none" (F-0024 plan, Task 1 step 6). A read, a write outside the set, and
    every one of the above with no ``ASF_JOB`` all pass."""

    ITEM = 'F-0024'
    JOB = 'code-F-0024'

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        os.makedirs(os.path.join(env.ASF_HOME, 'products'))
        os.makedirs(os.path.join(env.ASF_HOME, 'state', 'demo'))
        self.repo = os.path.join(self.tmp, 'repo')
        os.makedirs(self.repo)
        self.write_product()
        with open(os.path.join(env.ASF_HOME, 'state', 'demo', 'sessions.jsonl'), 'w') as f:
            f.write(json.dumps({
                'job': self.JOB, 'item': self.ITEM, 'branch': 'worker/f-0024',
                'started': '2026-09-24T00:00:00Z', 'pid': 1,
            }) + '\n')

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_product(self, extra='conventions:\n  briefs_dir: docs/briefs\n'):
        with open(env.product_path('demo'), 'w') as f:
            f.write(f'product: demo\nmain: main\n{extra}')

    def call(self, tool_name, tool_input, job=JOB):
        environ = {'ASF_PRODUCT': 'demo'}
        if job:
            environ['ASF_JOB'] = job
        call = json.dumps({
            'tool_name': tool_name, 'tool_input': tool_input, 'cwd': self.repo})
        out = io.StringIO()
        rc = approvals.run_hook(call, environ, out=out)
        return rc, out.getvalue()

    def ledger(self):
        return approvals.read('demo')

    def test_a_write_to_the_set_is_refused_with_the_five_lines_the_kind_and_the_patch(self):
        cases = [
            ('Write', {'file_path': 'rules/R-0099.md', 'content': 'a new rule\n'},
             'rules/R-0099.md', 'rule_cards'),
            ('Edit', {'file_path': 'rules/R-0042.md', 'old_string': 'old', 'new_string': 'new'},
             'rules/R-0042.md', 'rule_cards'),
            ('MultiEdit', {'file_path': 'tools/checks/x.sh',
                            'edits': [{'old_string': 'a', 'new_string': 'b'}]},
             'tools/checks/x.sh', 'checks'),
            ('NotebookEdit', {'notebook_path': 'evals/analysis.ipynb', 'new_source': 'x = 1'},
             'evals/analysis.ipynb', 'evals'),
            ('Bash', {'command': 'sed -i s/x/y/ rules/R-0042.md'}, 'rules/R-0042.md',
             'rule_cards'),
            ('Bash', {'command': 'echo x > .githooks/pre-commit'}, '.githooks/pre-commit',
             'hooks'),
            ('Bash', {'command': 'cp /tmp/a docs/briefs/coder.md'}, 'docs/briefs/coder.md',
             'briefs'),
        ]
        for tool_name, tool_input, relpath, kind in cases:
            with self.subTest(tool=tool_name, path=relpath):
                rc, out = self.call(tool_name, tool_input)
                self.assertEqual(rc, 2, out)
                lines = out.splitlines()
                self.assertEqual(len(lines), 5, out)
                self.assertTrue(lines[0].startswith(
                    f'REFUSED touch_amendable_set (human-now) on {self.ITEM} — {relpath}'
                    f' ({kind}:'), lines[0])
                self.assertIn('No session edits the amendable set.', lines[1])
                self.assertEqual(
                    lines[2],
                    f'  Propose it: asf propose --from-hold {self.ITEM}/touch_amendable_set'
                    ' --why "<one line>"')
                self.assertIn(f'NEEDS OPERATOR: {self.ITEM} touch_amendable_set —', lines[3])
                self.assertEqual(
                    lines[4],
                    '  and carry on with every part of the job that does not depend on it.')

                rec = self.ledger()[-1]
                self.assertEqual(rec['event'], 'refused')
                self.assertEqual(rec['class'], 'touch_amendable_set')
                self.assertEqual(rec['kind'], kind)
                self.assertEqual(rec['detail'], relpath)
                self.assertTrue(rec['patch'])

    def test_a_command_naming_no_file_is_still_refused_through_the_ordinary_matrix(self):
        for command in ('asf new rule --title x', 'asf set R-0042 --level auto',
                         'asf hooks install'):
            with self.subTest(command=command):
                rc, out = self.call('Bash', {'command': command})
                self.assertEqual(rc, 2, out)
                self.assertIn(f'REFUSED touch_amendable_set (human-now) on {self.ITEM} —', out)
                rec = self.ledger()[-1]
                self.assertEqual(rec['class'], 'touch_amendable_set')
                self.assertNotIn('kind', rec)     # write_target saw no file (Task 1 step 6)

    def test_negatives_pass_through(self):
        cases = [
            ('Read', {'file_path': 'rules/R-0042.md'}),
            ('Write', {'file_path': 'docs/specs/f-0024.md', 'content': 'x'}),
            ('Bash', {'command': 'cat rules/R-0042.md'}),
        ]
        for tool_name, tool_input in cases:
            with self.subTest(tool=tool_name, input=tool_input):
                self.assertEqual(self.call(tool_name, tool_input), (0, ''))
        self.assertEqual(self.ledger(), [])

    def test_no_job_means_every_case_passes(self):
        for tool_name, tool_input in (
            ('Write', {'file_path': 'rules/R-0099.md', 'content': 'x'}),
            ('Bash', {'command': 'echo x > .githooks/pre-commit'}),
            ('Bash', {'command': 'asf new rule --title x'}),
        ):
            with self.subTest(tool=tool_name):
                self.assertEqual(self.call(tool_name, tool_input, job=None), (0, ''))
        self.assertEqual(self.ledger(), [])


    def test_a_read_only_command_that_names_the_set_is_not_held(self):
        """F-0042 (2026-09-27): a spec session's read-only grep was refused because its pattern
        text held ``asf new rule`` and ``rules/``. A command that only reads — grep, ls, cat,
        git diff/log/show — runs nothing of the set and writes nothing in it."""
        for command in (
            'grep -rln "rules/R-\\|asf new rule\\|rule card" docs/specs/ docs/plans/ | head -20',
            'grep -n "a>b" rules/R-0042.md',
            'ls rules/ && cat rules/README.md 2>&1 | head -5',
            'git log --oneline -- rules/ asf/briefs/templates/coder.md',
            'git diff origin/main -- rules/README.md; git show HEAD:rules/R-0042.md',
            'rg "asf hooks install" docs > /tmp/out.txt',
            # F-0064 (2026-09-27): the same misreading held a grep as touch_security
            'grep -n "secrets\\|gh secret\\|gh auth\\|auth_env" asf/approvals.py | head -30',
            # T-0301 (2026-09-27): a commit whose message names a template and a `<id>` was
            # refused as a write to the template
            'git commit -s -m "$(cat <<\'EOF\'\ntask(T-0301): parts\n\n'
            'asf/briefs/templates/reshape.md is amendable; `<id> (<writes>)`\nEOF\n)"',
            'git commit -s -m "task(T-0301): leave asf/briefs/templates/reshape.md, <id> -> x"',
        ):
            with self.subTest(command=command):
                self.assertEqual(self.call('Bash', {'command': command}), (0, ''))
        self.assertEqual(self.ledger(), [])

    def test_a_real_write_or_run_in_a_read_only_shaped_command_is_still_held(self):
        for command, kind in (
            ('cat /tmp/x > rules/README.md', 'rule_cards'),
            ('grep -v old rules/R-0042.md | tee rules/R-0042.md', 'rule_cards'),
            ('git show HEAD:x.md > .githooks/pre-commit', 'hooks'),
            ('ls && asf new rule --title x', None),
            ('bash -c "asf new rule --title x"', None),
        ):
            with self.subTest(command=command):
                rc, out = self.call('Bash', {'command': command})
                self.assertEqual(rc, 2, out)
                self.assertIn('REFUSED touch_amendable_set (human-now)', out)
                self.assertEqual(self.ledger()[-1].get('kind'), kind)

    def test_a_console_amend_row_is_announced_once(self):
        """The CONSOLE → AMEND row (the feeder's plan-time route) says one NEEDS OPERATOR line
        the first tick it is seen, and nothing on the next — no hold, nothing parked."""
        from asf.feeder import rows
        prod = env.load_product('demo')
        row = rows.Row(tier=2, kind=rows.CONSOLE_AMEND, item_id='T-0303', feature_id='F-0042',
                       action='WAITS ON console: amendable rules/README.md', brief_kind='task',
                       branch='worker/T-0303', reason='x', waits_on='console',
                       amend='rules/README.md')
        other = rows.Row(tier=2, kind=rows.PLAN_CODE, item_id='T-0001', feature_id='F-0042',
                         action=rows.LAUNCH, brief_kind='task', branch='worker/T-0001',
                         reason='y')
        lines = []
        approvals.announce_console_amends(prod, [row, other], lines.append)
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith('NEEDS OPERATOR: T-0303 writes rules/README.md'),
                        lines[0])
        lines.clear()
        approvals.announce_console_amends(prod, [row, other], lines.append)
        self.assertEqual(lines, [])
        self.assertEqual(approvals.open_holds(prod), [])
        self.assertEqual(approvals.parked(prod), {})


class PolicyTests(TickTestCase):
    """§3.3 — the policy cannot be widened or granted away: ``matrix`` refuses any level but
    ``human-now`` for ``touch_amendable_set`` (F-0024's own message), the hook refuses even when
    the rest of the matrix is invalid or the hold has been marked ``granted`` straight in the
    ledger, ``resolve(…, 'granted')`` itself raises, ``resolve(…, 'proposed')`` closes the hold,
    and ``raise_holds`` names the open hold and parks the item (a relaunch only buys the same
    refusal) — while a ``touch_security`` hold in the same ledger parks nothing (any other hook
    refusal never parks)."""

    ITEM = 'F-0024'
    JOB = 'code-F-0024'

    @classmethod
    def build_repos(cls, tmp):
        super().build_repos(tmp)
        seed = os.path.join(tmp, 'seed')
        with open(os.path.join(seed, 'index.json'), 'w') as f:
            json.dump({'generated': '', 'items': {}}, f)
        _git(['add', '-A'], seed)
        _git(['commit', '-q', '-m', 'index'], seed)
        _git(['push', '-q', 'origin', 'HEAD:main'], seed)

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(env.ASF_HOME, 'state', 'sample'), exist_ok=True)
        with open(os.path.join(env.ASF_HOME, 'state', 'sample', 'sessions.jsonl'), 'w') as f:
            f.write(json.dumps({
                'job': self.JOB, 'item': self.ITEM, 'branch': 'worker/f-0024',
                'started': '2026-09-24T00:00:00Z', 'pid': 1,
            }) + '\n')
        self.repo = os.path.join(self.tmp, 'repo')
        os.makedirs(self.repo)

    def call(self, tool_name, tool_input, job=JOB):
        environ = {'ASF_PRODUCT': 'sample'}
        if job:
            environ['ASF_JOB'] = job
        call = json.dumps({'tool_name': tool_name, 'tool_input': tool_input, 'cwd': self.repo})
        out = io.StringIO()
        rc = approvals.run_hook(call, environ, out=out)
        return rc, out.getvalue()

    def test_matrix_rejects_a_widened_level_and_loads_at_human_now(self):
        for level in ('auto', 'groom'):
            with self.subTest(level=level):
                self.write_product(f'approvals:\n  touch_amendable_set: {level}\n')
                with self.assertRaises(env.ConfigError) as ctx:
                    approvals.matrix(env.load_product('sample'))
                self.assertIn('F-0024', str(ctx.exception))
                self.assertIn('touch_amendable_set', str(ctx.exception))
        self.write_product('approvals:\n  touch_amendable_set: human-now\n')
        m = approvals.matrix(env.load_product('sample'))
        self.assertEqual(m['touch_amendable_set'], ('human-now', 'yaml'))

    def test_the_hook_refuses_even_with_an_invalid_matrix(self):
        self.write_product('approvals:\n  touch_production: maybe\n')
        rc, out = self.call('Write', {'file_path': 'rules/R-0042.md', 'content': 'x'})
        self.assertEqual(rc, 2, out)
        self.assertIn('REFUSED touch_amendable_set (human-now)', out)
        self.assertNotIn('matrix is invalid', out)   # never reached: the amendable check is first

    def test_the_hook_refuses_even_when_the_hold_is_granted(self):
        self.write_product('')
        hold = f'{self.ITEM}/touch_amendable_set'
        approvals.append('sample', {
            'event': 'resolved', 'hold': hold, 'resolution': 'granted',
            'ts': '2026-09-24T00:00:00Z'})
        self.assertTrue(approvals.is_granted('sample', hold))
        rc, out = self.call('Write', {'file_path': 'rules/R-0042.md', 'content': 'x'})
        self.assertEqual(rc, 2, out)

    def test_resolve_granted_raises_and_proposed_closes_the_hold(self):
        self.write_product('')
        hold = approvals.refuse(
            'sample', self.ITEM, 'touch_amendable_set', 'human-now', self.JOB, 'Write',
            'rules/R-0042.md', kind='rule_cards', patch='x')
        with self.assertRaises(ValueError):
            approvals.resolve('sample', hold, 'granted')
        self.assertIn(hold, [h['hold'] for h in approvals.open_holds('sample')])
        approvals.resolve('sample', hold, 'proposed')
        self.assertNotIn(hold, [h['hold'] for h in approvals.open_holds('sample')])
        self.assertEqual(approvals.holds('sample')[hold]['resolution'], 'proposed')

    def test_raise_holds_names_it_and_parks_it(self):
        self.write_product('')
        approvals.refuse('sample', self.ITEM, 'touch_amendable_set', 'human-now', self.JOB,
                          'Write', 'rules/R-0042.md', kind='rule_cards', patch='x')
        approvals.refuse('sample', 'B-0002', 'touch_security', 'human-now', 'code-B-0002',
                          'Write', '.env')
        lines = []
        ctx = tick.Context(env.load_product('sample'))
        held = approvals.raise_holds(ctx, lines.append)
        self.assertTrue(any(
            l.startswith(f'NEEDS OPERATOR: held touch_amendable_set on {self.ITEM}')
            for l in lines), lines)
        # parked: no session edits the amendable set, so a relaunch spends a slot on a refusal
        # a touch_security refusal parks nothing: the session was told to finish another way
        self.assertEqual(held, {self.ITEM: ('touch_amendable_set', 'human-now')})


if __name__ == '__main__':
    unittest.main()
