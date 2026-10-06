"""tests.test_readonly — G2: the read-only grant (``asf/readonly.py``), hermetic (a tmp git repo
as the one allowed root). ``GrantTests`` is every shape the hook must answer *allow* for without
a declared command; ``DeclaredTests`` is the product's own check/test commands; ``RefuseTests``
is everything the grammar must leave to the runtime's own rules — a mutation, an escape from the
root, or shell the grammar does not read at all."""
import os
import subprocess
import tempfile
import unittest

from asf import readonly
from asf.env import Product


def _git(args, cwd):
    subprocess.run(['git'] + args, cwd=cwd, check=True, capture_output=True, text=True)


class ReadonlyTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='readonly_')
        self.addCleanup(self._rmtree)
        _git(['init', '-q'], self.root)
        _git(['config', 'user.email', 'x@example.com'], self.root)
        _git(['config', 'user.name', 'x'], self.root)
        os.makedirs(os.path.join(self.root, 'tools'), exist_ok=True)
        with open(os.path.join(self.root, 'README.md'), 'w', encoding='utf-8') as f:
            f.write('line one\nline two\nline three\n')
        script = os.path.join(self.root, 'tools', 'check_x.sh')
        with open(script, 'w', encoding='utf-8') as f:
            f.write('#!/bin/bash\necho ok\n')
        os.chmod(script, 0o755)
        _git(['add', '-A'], self.root)
        _git(['commit', '-q', '-m', 'first'], self.root)

    def _rmtree(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def grant(self, command, declared=()):
        return readonly.grant(command, self.root, [self.root], declared)


class GrantTests(ReadonlyTestCase):
    """Read-only shapes the hook must allow outright, with no declared command."""

    def test_plain_read_programs(self):
        for cmd in ('ls -la', 'cat README.md', 'head README.md', 'tail -n3 README.md',
                    'grep -rn line .', 'rg line .', 'wc -l README.md', 'pwd', 'echo hi',
                    'find . -name "*.md"'):
            with self.subTest(cmd=cmd):
                self.assertTrue(self.grant(cmd), cmd)

    def test_git_reads(self):
        for cmd in ('git log -3', 'git show HEAD', 'git status', 'git diff', 'git blame README.md',
                    'git ls-files', 'git rev-parse HEAD', 'git merge-base --is-ancestor HEAD HEAD',
                    'git branch', 'git branch --list', 'git tag', 'git tag --list',
                    'git remote -v', 'git config --get user.name', 'git reflog show',
                    'git worktree list', 'git fetch origin'):
            with self.subTest(cmd=cmd):
                self.assertTrue(self.grant(cmd), cmd)

    def test_exit_code_after_a_git_read(self):
        self.assertTrue(self.grant('git merge-base --is-ancestor HEAD HEAD; echo $?'))
        self.assertTrue(self.grant('git merge-base --is-ancestor HEAD HEAD ; echo "rc=$?"'))

    def test_cd_into_the_root_then_a_read(self):
        self.assertTrue(self.grant(f'cd {self.root} && git log'))
        self.assertTrue(self.grant('cd tools && cat check_x.sh'))
        self.assertTrue(self.grant('cd tools && ls && cd .. && git status'))

    def test_process_substitution_of_two_reads(self):
        self.assertTrue(self.grant('diff <(git show HEAD:README.md) <(sed -n 1,2p README.md)'))

    def test_pipeline_and_subshell_of_reads(self):
        self.assertTrue(self.grant('git log | head -3'))
        self.assertTrue(self.grant('(cd tools && ls)'))

    def test_redirection_to_dev_null_is_a_read(self):
        self.assertTrue(self.grant('git status > /dev/null 2>&1'))

    def test_fd_duplication_is_a_read(self):
        self.assertTrue(self.grant('git log 2>&1 | tail -3'))

    def test_safe_display_assignment_and_wrappers(self):
        self.assertTrue(self.grant('GIT_PAGER=cat git log'))
        self.assertTrue(self.grant('timeout 5 git status'))
        self.assertTrue(self.grant('command git status'))
        self.assertTrue(self.grant('env -u GIT_PAGER git log'))

    def test_newline_separated_reads(self):
        self.assertTrue(self.grant('git status\ngit log -1\n'))


class DeclaredTests(ReadonlyTestCase):
    """The product's own declared check and test commands."""

    def test_declared_check_command_with_tail_and_exit_code(self):
        declared = [(self.root, ['bash', 'tools/check_x.sh'])]
        self.assertTrue(self.grant('bash tools/check_x.sh 2>&1 | tail -3; echo $?', declared))

    def test_declared_command_with_extra_arguments(self):
        declared = [(self.root, ['bash', 'tools/check_x.sh'])]
        self.assertTrue(self.grant('bash tools/check_x.sh --fix; echo "rc=$?"', declared))

    def test_declared_command_matched_by_any_equivalent_path_form(self):
        declared = [(self.root, ['bash', 'tools/check_x.sh'])]
        script = os.path.join(self.root, 'tools', 'check_x.sh')
        for cmd in (f'bash {script}', 'sh ./tools/check_x.sh', 'tools/check_x.sh'):
            with self.subTest(cmd=cmd):
                self.assertTrue(self.grant(cmd, declared), cmd)

    def test_grant_for_reads_the_products_declared_commands(self):
        product = Product('demo', {'conventions': {
            'check_commands': ['bash tools/check_x.sh'],
        }})
        cmd = 'bash tools/check_x.sh 2>&1 | tail -3; echo $?'
        self.assertTrue(readonly.grant_for(product, cmd, self.root, {}))

    def test_grant_for_honours_read_only_allow_false(self):
        product = Product('demo', {'conventions': {
            'check_commands': ['bash tools/check_x.sh'],
            'read_only_allow': False,
        }})
        self.assertFalse(readonly.grant_for(
            product, 'bash tools/check_x.sh', self.root, {}))
        self.assertFalse(readonly.grant_for(product, 'git log', self.root, {}))

    def test_grant_for_reads_asf_read_roots(self):
        other = tempfile.mkdtemp(prefix='readonly_other_')
        self.addCleanup(lambda: __import__('shutil').rmtree(other, ignore_errors=True))
        with open(os.path.join(other, 'notes.txt'), 'w', encoding='utf-8') as f:
            f.write('x\n')
        product = Product('demo', {})
        self.assertFalse(readonly.grant_for(
            product, f'cat {other}/notes.txt', self.root, {}))
        self.assertTrue(readonly.grant_for(
            product, f'cat {other}/notes.txt', self.root, {'ASF_READ_ROOTS': other}))


class RefuseTests(ReadonlyTestCase):
    """Everything that must stay governed as today."""

    def test_mutating_git_subcommands(self):
        for cmd in ('git push', 'git commit -m x', 'git push origin main',
                    'git branch newname', 'git branch -d old', 'git tag -d v1',
                    'git config user.name y', 'git stash', 'git reset --hard'):
            with self.subTest(cmd=cmd):
                self.assertFalse(self.grant(cmd), cmd)

    def test_git_with_a_global_flag_is_not_judged(self):
        for cmd in ('git -c core.fsmonitor=x status', 'git --git-dir=/tmp/x log',
                    'git --exec-path=/x log'):
            with self.subTest(cmd=cmd):
                self.assertFalse(self.grant(cmd), cmd)

    def test_filesystem_mutations(self):
        for cmd in ('rm -rf tools', "sed -i 's/a/b/' README.md", 'find . -delete',
                    'echo hi > README.md', 'echo hi >> README.md'):
            with self.subTest(cmd=cmd):
                self.assertFalse(self.grant(cmd), cmd)

    def test_paths_outside_the_root(self):
        for cmd in ('cat ~/.ssh/id_rsa', 'cd / && ls', 'cat ../../etc/passwd',
                    'ls /etc'):
            with self.subTest(cmd=cmd):
                self.assertFalse(self.grant(cmd), cmd)

    def test_unsupported_shell_is_never_granted(self):
        for cmd in ('echo $HOME', 'echo `date`', 'cat <<EOF\nhi\nEOF',
                    'for f in a b; do echo "$f"; done', 'echo hi &',
                    'GIT_EXTERNAL_DIFF=rm git diff', 'T=$(mktemp -d); echo "$T"'):
            with self.subTest(cmd=cmd):
                self.assertFalse(self.grant(cmd), cmd)

    def test_docker_and_vm_tools_are_never_granted(self):
        declared = [(self.root, ['docker', 'ps'])]
        for cmd in ('docker ps', 'colima start', 'multipass launch', 'limactl start',
                    'open -a Docker'):
            with self.subTest(cmd=cmd):
                self.assertFalse(self.grant(cmd, declared), cmd)

    def test_empty_or_blank_command(self):
        self.assertFalse(self.grant(''))
        self.assertFalse(self.grant('   '))

    def test_a_mutation_after_a_declared_check_is_never_granted(self):
        declared = [(self.root, ['bash', 'tools/check_x.sh'])]
        self.assertFalse(self.grant('bash tools/check_x.sh && rm -rf /', declared))


if __name__ == '__main__':
    unittest.main()
