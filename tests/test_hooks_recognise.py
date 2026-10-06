"""``asf/hooks.py`` — both recognisers read back every form asf can write (F-0111 S-32651): a
suffixed basename (a pipx ``--suffix`` install, F-0111 P3) and the module forms, so a second
``hooks install`` from the same install changes nothing and never appends a second settings entry,
and the doctor's ``approvals-hook`` row stays green."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from asf import env, hooks
from tests.gitfixture import executable_asf


def _git(args, cwd):
    subprocess.run(['git'] + args, cwd=cwd, check=True, capture_output=True, text=True)


def executable_asf_suffixed(bin_dir, suffix):
    """A do-nothing ``asf<suffix>`` executable beside :func:`tests.gitfixture.executable_asf` —
    the pipx ``--suffix`` form (F-0111 P3)."""
    os.makedirs(bin_dir, exist_ok=True)
    path = os.path.join(bin_dir, f'asf{suffix}')
    with open(path, 'w') as f:
        f.write('#!/bin/sh\nexit 0\n')
    os.chmod(path, 0o755)
    return path


# ---- the form table ---------------------------------------------------------

#: Every form ``_git_hook_body`` (or an operator's own ``-m`` invocation) can write, and the near
#: misses that must never be read back as ours (F-0111 §"reading back what asf writes", the table
#: at ``docs/specs/f-0111.md:168-174``). ``{p}`` fills in the ``exec "<path>/asf"`` style path,
#: ``{name}`` the hook name under test. ``entry`` is what :func:`hooks.hook_entry` must answer;
#: ``None`` for a form :data:`hooks.hook_entry` never resolves (a bare name, or not ours at all).
GIT_HOOK_FORMS = [
    ('bare', 'exec asf redact --{name}', None, True),
    ('quoted absolute', 'exec "/opt/p/bin/asf" redact --{name} --product p', '/opt/p/bin/asf', True),
    ('unquoted absolute', 'exec /opt/p/bin/asf redact --{name} --product p', '/opt/p/bin/asf', True),
    ('quoted suffixed', 'exec "/opt/p/bin/asf-live" redact --{name} --product p', '/opt/p/bin/asf-live', True),
    ('unquoted suffixed', 'exec /opt/p/bin/asf-live redact --{name} --product p', '/opt/p/bin/asf-live', True),
    ('module: asf.redact', 'exec "/v/bin/python3" -m asf.redact --{name}', '/v/bin/python3', True),
    ('module: asf.cli redact', 'exec "/v/bin/python3" -m asf.cli redact --{name}', '/v/bin/python3', True),
    ('a different program that starts with asf', 'exec /opt/p/bin/asfmt redact-all --{name}', None, False),
    ('inside a string', 'echo "asf redact --{name}"', None, False),
    ('a # comment', '# exec asf redact --{name}', None, False),
]

HOOK_NAMES = ('pre-commit', 'pre-push')


class IsGitHookOursFormTests(unittest.TestCase):
    def test_every_written_form_is_recognised_and_every_near_miss_is_not(self):
        for name in HOOK_NAMES:
            for label, template, entry, ours in GIT_HOOK_FORMS:
                text = template.format(name=name)
                with self.subTest(name=name, form=label):
                    self.assertIs(hooks.is_git_hook_ours(text, name), ours)
                    self.assertEqual(hooks.hook_entry(text, name), entry)

    def test_the_other_hooks_line_is_not_this_hooks(self):
        text = 'exec asf redact --pre-commit'
        self.assertFalse(hooks.is_git_hook_ours(text, 'pre-push'))
        self.assertIsNone(hooks.hook_entry(text, 'pre-push'))

    def test_a_longer_command_name_never_matches_a_shorter_one(self):
        # the left anchor (F-0111 §"reading back what asf writes"): a hook naming some other
        # command that merely ends in "asf" is never read as asf's own
        text = 'exec notasf redact --pre-push'
        self.assertFalse(hooks.is_git_hook_ours(text, 'pre-push'))
        self.assertIsNone(hooks.hook_entry(text, 'pre-push'))


#: The settings table (F-0111 §"reading back what asf writes"): every command form
#: ``hook_command`` can write, and the near misses. ``{p}`` product tail appended when ``product``
#: is given.
SETTINGS_FORMS = [
    ('quoted-directory path', '/dir/asf hook {name}{tail}', True),
    ('suffixed path', '/dir/asf-live hook {name}{tail}', True),
    ('bare', 'asf hook {name}{tail}', True),
    ('module: asf.cli', '/v/bin/python3 -m asf.cli hook {name}{tail}', True),
]


class IsOursSettingsFormTests(unittest.TestCase):
    def test_every_written_form_is_recognised(self):
        for name in ('approvals', 'r0001'):
            for product in (None, 'p'):
                tail = f' --product {product}' if product is not None else ''
                for label, template, ours in SETTINGS_FORMS:
                    command = template.format(name=name, tail=tail)
                    with self.subTest(name=name, product=product, form=label):
                        self.assertIs(hooks._is_ours(command, name, product), ours)

    def test_a_command_naming_a_different_program_is_not_ours(self):
        self.assertFalse(hooks._is_ours('/dir/asfmt hook approvals', 'approvals', None))

    def test_a_product_mismatch_is_not_ours(self):
        self.assertFalse(hooks._is_ours('asf hook approvals --product q', 'approvals', 'p'))

    def test_trailing_text_is_not_ours(self):
        self.assertFalse(hooks._is_ours('asf hook approvals extra', 'approvals', None))


# ---- reading back a whole second install -------------------------------------

class SecondInstallChangesNothingTests(unittest.TestCase):
    """A second (and third) ``hooks install`` from the same suffixed install changes no file and
    appends no settings entry (F-0111: P4, P5, P6, P7)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hooks_recognise_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        os.makedirs(os.path.join(env.ASF_HOME, 'products'))
        self.addCleanup(self._restore_home)

        self.repo = os.path.join(self.tmp, 'repo')
        _git(['init', '-q', self.repo], self.tmp)
        self.rules = os.path.join(self.tmp, 'rules')
        os.makedirs(self.rules)
        with open(os.path.join(self.rules, 'R-0001.md'), 'w') as f:
            f.write('---\nid: R-0001\ntype: rule\ntitle: guard\nhook: [PreToolUse, Stop]\n---\n')
        # the repo's .claude/settings.json would be a new file in its git status — gated by the
        # matrix (F-0109); these cases are about reading back what was written, so: auto
        self.product = env.Product('sample', {'repo_dir': self.repo,
                                              'approvals': {'touch_security': 'auto'}})
        self.settings = os.path.join(self.repo, '.claude', 'settings.json')

        self.asf_live = executable_asf_suffixed(os.path.join(self.tmp, 'bin'), '-live')
        self.which = lambda n: self.asf_live
        self.account_dir = os.path.join(self.tmp, 'account')
        self.account_settings = os.path.join(self.account_dir, 'settings.json')
        self.cfg = {'worker_pool': {'accounts': [{'name': 'w1', 'config_dir': self.account_dir}]}}

    def _restore_home(self):
        env.ASF_HOME = self._orig_home

    def install(self, which=None):
        rc, msg = hooks.install(self.product, rules_dir=self.rules, which=which or self.which, cfg=self.cfg)
        self.assertEqual(rc, 0, msg)

    def _bytes(self, *paths):
        out = {}
        for p in paths:
            if os.path.isfile(p):
                with open(p, 'rb') as f:
                    out[p] = f.read()
        return out

    def test_a_second_and_third_install_change_nothing(self):
        self.install()
        pre_commit = os.path.join(self.repo, '.git', 'hooks', 'pre-commit')
        pre_push = os.path.join(self.repo, '.git', 'hooks', 'pre-push')
        before = self._bytes(pre_commit, pre_push, self.settings, self.account_settings)
        self.assertEqual(set(before), {pre_commit, pre_push, self.settings, self.account_settings})

        self.install()
        after = self._bytes(pre_commit, pre_push, self.settings, self.account_settings)
        self.assertEqual(after, before)
        self._assert_one_entry_each()

        # a third run, the entry point resolved the same way asf-live's install would resolve it
        self.install(which=lambda n: self.asf_live)
        self.assertEqual(self._bytes(pre_commit, pre_push, self.settings, self.account_settings), before)
        self._assert_one_entry_each()

    def _assert_one_entry_each(self):
        with open(self.settings) as f:
            repo_settings = json.load(f)
        self.assertEqual(len(repo_settings['hooks']['PreToolUse']), 1)
        self.assertEqual(len(repo_settings['hooks']['PreToolUse'][0]['hooks']), 1)
        with open(self.account_settings) as f:
            account_settings = json.load(f)
        self.assertEqual(len(account_settings['hooks']['PreToolUse']), 1)
        self.assertEqual(len(account_settings['hooks']['PreToolUse'][0]['hooks']), 1)


# ---- the doctor's approvals-hook row -------------------------------------

class ApprovalsMissingReadsEveryFormTests(unittest.TestCase):
    """``approvals_missing`` (the doctor's ``approvals-hook`` row, F-0111 P7) sees the hook
    through every form :func:`hooks._is_ours` now accepts."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='approvals_missing_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _account(self, command):
        from asf.workers.pool import Account
        config_dir = os.path.join(self.tmp, command.replace('/', '_').replace(' ', '_'))
        os.makedirs(config_dir, exist_ok=True)
        settings = {'hooks': {'PreToolUse': [
            {'matcher': '*', 'hooks': [{'type': 'command', 'command': command}]}]}}
        with open(os.path.join(config_dir, 'settings.json'), 'w') as f:
            json.dump(settings, f)
        return Account('w', config_dir=config_dir)

    def test_every_recognised_form_reads_as_present(self):
        for label, template, ours in SETTINGS_FORMS:
            command = template.format(name='approvals', tail='')
            with self.subTest(form=label):
                account = self._account(command)
                self.assertEqual(hooks.approvals_missing([account]), [])

    def test_a_near_miss_still_answers_the_account(self):
        account = self._account('/dir/asfmt hook approvals')
        self.assertEqual(hooks.approvals_missing([account]), [account])

    def test_no_settings_file_at_all_answers_the_account(self):
        from asf.workers.pool import Account
        account = Account('w', config_dir=os.path.join(self.tmp, 'none'))
        self.assertEqual(hooks.approvals_missing([account]), [account])


if __name__ == '__main__':
    unittest.main()
