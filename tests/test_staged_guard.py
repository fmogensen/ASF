"""F-0282: a record commit from a stale tree never deletes a card another writer filed.

A bare origin and a record clone, both temp: the clone's index is made stale (a card the trunk
carries is removed from it) and the guard must refuse that deletion — naming the card — while a
card the branch made itself, a card the trunk already dropped, intake's own move to ``done/``
and a deliberate deletion with the escape variable all pass."""
import os
import subprocess
import sys
import tempfile
import unittest

from asf import hermetic
from asf.record import staged_guard

ID = ['-c', 'user.name=guard', '-c', 'user.email=guard@example.com']


def git(cwd, *args, check=True):
    return subprocess.run(['git', *ID, *args], cwd=cwd, capture_output=True, text=True,
                          check=check)


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


class StagedGuardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.origin = os.path.join(tmp.name, 'origin.git')
        self.repo = os.path.join(tmp.name, 'record')
        git(tmp.name, 'init', '-q', '--bare', '-b', 'main', self.origin)
        git(tmp.name, 'clone', '-q', self.origin, self.repo)
        git(self.repo, 'config', 'core.hooksPath', os.devnull)
        git(self.repo, 'checkout', '-q', '-b', 'main')
        write(self.repo, 'index.json', '{}\n')
        write(self.repo, 'features/F-10001.md', '---\nid: F-10001\n---\nfiled first\n')
        write(self.repo, 'inbox/s1-urgent.md', 'an urgent note\n')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'seed')
        git(self.repo, 'push', '-q', 'origin', 'main')
        self.env = {}

    def check(self):
        return staged_guard.check(self.repo, trunk='main', environ=self.env)

    def test_a_stale_index_deleting_a_filed_card_is_refused_naming_it(self):
        git(self.repo, 'rm', '-q', '--cached', 'inbox/s1-urgent.md')
        write(self.repo, 'stories/S-10002.md', 'a new story\n')
        git(self.repo, 'add', 'stories/S-10002.md')
        out = self.check()
        self.assertEqual(len(out), 1)
        self.assertIn('inbox/s1-urgent.md', out[0])
        self.assertIn(staged_guard.ALLOW_VAR, out[0])

    def test_a_deleted_card_is_refused(self):
        git(self.repo, 'rm', '-q', 'features/F-10001.md')
        self.assertIn('features/F-10001.md', ''.join(self.check()))

    def test_a_card_this_branch_created_may_be_deleted(self):
        other = self.repo + '-other'
        git(os.path.dirname(self.repo), 'clone', '-q', self.origin, other)
        git(other, 'config', 'core.hooksPath', os.devnull)
        write(other, 'features/F-10003.md', 'theirs\n')
        git(other, 'add', '-A')
        git(other, 'commit', '-q', '-m', 'theirs')
        git(other, 'push', '-q', 'origin', 'main')
        write(self.repo, 'features/F-10003.md', 'mine\n')  # this branch's own, made apart
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'mine')
        git(self.repo, 'fetch', '-q', 'origin')
        git(self.repo, 'rm', '-q', 'features/F-10003.md')
        self.assertEqual(self.check(), [])

    def test_a_card_the_trunk_already_dropped_may_go(self):
        other = self.repo + '-other'
        git(os.path.dirname(self.repo), 'clone', '-q', self.origin, other)
        git(other, 'config', 'core.hooksPath', os.devnull)
        git(other, 'rm', '-q', 'features/F-10001.md')
        git(other, 'commit', '-q', '-m', 'drop')
        git(other, 'push', '-q', 'origin', 'main')
        git(self.repo, 'fetch', '-q', 'origin')
        git(self.repo, 'rm', '-q', 'features/F-10001.md')
        self.assertEqual(self.check(), [])

    def test_intakes_move_to_done_passes(self):
        text = 'an urgent note\n'
        git(self.repo, 'rm', '-q', 'inbox/s1-urgent.md')
        write(self.repo, 'inbox/done/s1-urgent.md', f'→ B-10004\n\n{text}')
        write(self.repo, 'bugs/B-10004.md', 'the bug\n')
        git(self.repo, 'add', '-A')
        self.assertEqual(self.check(), [])

    def _answered(self):
        """The F-0305 sequence's first half: a filed note with a question, answered by groom —
        its header lines rewritten in place, so its HEAD text survives nowhere verbatim."""
        from asf.groom import inbox
        write(self.repo, 'inbox/s1-answered.md',
              '# S1: the guard refuses the move\n\nthe body\n\n## Question\nwhat type?\n')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'file')
        git(self.repo, 'push', '-q', 'origin', 'main')
        applied, _ = inbox.apply_answer(self.repo, 's1-answered.md', 'feature', '2026-10-08',
                                        'groom')
        self.assertTrue(applied)
        return inbox

    def test_groom_apply_then_move_to_done_passes(self):
        inbox = self._answered()
        with open(os.path.join(self.repo, 'inbox/s1-answered.md'), encoding='utf-8') as f:
            text = f.read()
        inbox.move_to_done(os.path.join(self.repo, 'inbox'), 's1-answered.md',
                           '→ closed (groom 2026-10-08, groom)', text)
        git(self.repo, 'add', '-A')
        self.assertEqual(self.check(), [])

    def test_groom_apply_then_a_mint_named_by_the_title_slug_passes(self):
        self._answered()
        path = os.path.join(self.repo, 'inbox/s1-answered.md')
        with open(path, encoding='utf-8') as f:
            text = f.read()
        write(self.repo, 'inbox/done/the-guard-refuses-the-move.md', f'→ B-10005\n\n{text}')
        write(self.repo, 'bugs/B-10005.md', 'the bug\n')
        os.remove(path)
        git(self.repo, 'add', '-A')
        self.assertEqual(self.check(), [])

    def test_an_intake_deletion_with_no_done_note_is_refused(self):
        self._answered()
        git(self.repo, 'rm', '-q', '-f', 'inbox/s1-answered.md')
        self.assertIn('inbox/s1-answered.md', ''.join(self.check()))

    def test_an_unrelated_done_note_does_not_pair_with_a_stale_deletion(self):
        git(self.repo, 'rm', '-q', '--cached', 'inbox/s1-urgent.md')
        write(self.repo, 'inbox/done/another-note.md', '→ B-10006\n\n# another note\n')
        git(self.repo, 'add', 'inbox/done/another-note.md')
        self.assertIn('inbox/s1-urgent.md', ''.join(self.check()))

    def test_a_card_deletion_is_refused_whatever_done_note_it_brings(self):
        git(self.repo, 'rm', '-q', 'features/F-10001.md')
        write(self.repo, 'inbox/done/F-10001.md', '→ closed\n\n---\nid: F-10001\n---\n')
        git(self.repo, 'add', '-A')
        self.assertIn('features/F-10001.md', ''.join(self.check()))

    def test_the_escape_variable_lets_a_meant_deletion_through(self):
        git(self.repo, 'rm', '-q', 'features/F-10001.md')
        self.env = {staged_guard.ALLOW_VAR: '1'}
        self.assertEqual(self.check(), [])

    def test_a_code_repo_is_never_judged(self):
        git(self.repo, 'rm', '-q', 'index.json')
        git(self.repo, 'commit', '-q', '-m', 'no longer a record')
        git(self.repo, 'rm', '-q', 'features/F-10001.md')
        self.assertEqual(self.check(), [])

    def test_no_origin_trunk_is_no_refusal(self):
        git(self.repo, 'remote', 'remove', 'origin')
        git(self.repo, 'rm', '-q', 'features/F-10001.md')
        self.assertEqual(self.check(), [])

    def test_the_pre_commit_mode_of_redact_refuses_the_commit(self):
        git(self.repo, 'rm', '-q', '--cached', 'inbox/s1-urgent.md')
        env = {k: v for k, v in os.environ.items() if k != staged_guard.ALLOW_VAR}
        env['PYTHONPATH'] = hermetic.package_parent()
        p = subprocess.run([sys.executable, '-m', 'asf.redact', '--pre-commit'], cwd=self.repo,
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn('inbox/s1-urgent.md', p.stderr)


if __name__ == '__main__':
    unittest.main()
