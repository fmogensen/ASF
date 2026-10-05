"""A record commit racing another commit in the same checkout (round E #21).

``asf correct`` files the ruling on the card and commits it from a scratch index built off
``HEAD``. A tick's record push (or a second console command) committing meanwhile made the
commit fail on a lock — a traceback, the card edit left uncommitted — or, worse, land with the
other commit as its parent and the old tree, reverting it. The commit is now built again on the
new ``HEAD``, and the push rebases as before."""
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf.record import index as index_mod
from asf.record import publish


def g(*args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True)


def card(item, history=''):
    return (f'---\nid: {item}\ntype: task\nstate: New\ntitle: t {item}\n---\n\n# {item}\n\n'
            f'## History\n{history}')


class RaceTests(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.origin, self.root = os.path.join(self.d, 'o.git'), os.path.join(self.d, 'rec')
        g('init', '-q', '--bare', '-b', 'main', self.origin, cwd=self.d)
        g('init', '-q', '-b', 'main', self.root, cwd=self.d)
        for k, v in (('user.name', 't'), ('user.email', 't@t')):
            g('config', k, v, cwd=self.root)
        os.makedirs(os.path.join(self.root, 'tasks'))
        for item in ('T-0001', 'T-0002'):
            self.write(item, card(item))
        g('add', '-A', cwd=self.root)
        g('commit', '-qm', 'base', cwd=self.root)
        g('remote', 'add', 'origin', self.origin, cwd=self.root)
        g('push', '-q', 'origin', 'main', cwd=self.root)
        sleep = mock.patch('time.sleep', lambda *_a: None)
        sleep.start()
        self.addCleanup(sleep.stop)

    def write(self, item, text):
        with open(os.path.join(self.root, 'tasks', f'{item}.md'), 'w') as f:
            f.write(text)

    def show(self, rev, item):
        return g('show', f'{rev}:tasks/{item}.md', cwd=self.root).stdout

    def other_commit(self):
        self.write('T-0002', card('T-0002', '- the other process\n'))
        g('add', 'tasks/T-0002.md', cwd=self.root)
        self.assertEqual(g('commit', '-qm', 'other', cwd=self.root).returncode, 0)

    def test_a_commit_landing_while_it_is_built_is_kept_and_ours_goes_on_top(self):
        real, fired = index_mod.refresh, []

        def racing(*a, **k):
            if not fired:
                fired.append(1)
                self.other_commit()
            return real(*a, **k)

        self.write('T-0001', card('T-0001', '- the ruling\n'))
        with mock.patch.object(index_mod, 'refresh', racing):
            self.assertTrue(publish.publish(self.root, os.path.join(self.root, 'tasks', 'T-0001.md'),
                                            'record: T-0001 operator ruling'))
        self.assertIn('the other process', self.show('HEAD', 'T-0002'))
        self.assertIn('the ruling', self.show('HEAD', 'T-0001'))
        self.assertIn('the other process', self.show('origin/main', 'T-0002'))
        self.assertIn('the ruling', self.show('origin/main', 'T-0001'))
        self.assertEqual(g('status', '--porcelain', '--', 'tasks', cwd=self.root).stdout, '')

    def test_a_lock_held_by_another_commit_is_waited_out(self):
        lock = os.path.join(self.root, '.git', 'refs', 'heads', 'main.lock')
        open(lock, 'w').close()
        released = []

        def sleep(*_a):
            if os.path.exists(lock):
                os.remove(lock)
                released.append(1)

        self.write('T-0001', card('T-0001', '- the ruling\n'))
        import time
        with mock.patch.object(time, 'sleep', sleep):
            self.assertTrue(publish.commit_paths(self.root, ['tasks/T-0001.md'], 'ruling'))
        self.assertEqual(released, [1])
        self.assertIn('the ruling', self.show('HEAD', 'T-0001'))

    def test_a_checkout_that_never_settles_is_a_named_error_not_a_silent_revert(self):
        real = index_mod.refresh

        def always(*a, **k):
            self.other_commit_n = getattr(self, 'other_commit_n', 0) + 1
            self.write('T-0002', card('T-0002', f'- other {self.other_commit_n}\n'))
            g('add', 'tasks/T-0002.md', cwd=self.root)
            g('commit', '-qm', f'other {self.other_commit_n}', cwd=self.root)
            return real(*a, **k)

        self.write('T-0001', card('T-0001', '- the ruling\n'))
        with mock.patch.object(index_mod, 'refresh', always):
            with self.assertRaises(publish.ConcurrentCommit):
                publish.commit_paths(self.root, ['tasks/T-0001.md'], 'ruling')
        self.assertIn(f'- other {self.other_commit_n}', self.show('HEAD', 'T-0002'))


class CorrectNeverCrashesTests(unittest.TestCase):

    def test_a_failed_record_commit_is_a_reason_not_a_traceback(self):
        from types import SimpleNamespace
        from asf.workers import correct
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, 'tasks', 'T-0001.md')
        os.makedirs(os.path.dirname(path))
        with open(path, 'w') as f:
            f.write(card('T-0001'))
        product = SimpleNamespace(backlog_dir=d)
        from asf.evidence import rulings
        from asf.record import stage
        with mock.patch.object(rulings, '_card_file', lambda *_a: path), \
                mock.patch.object(stage, 'guarded', lambda *a, **k: (None, None, [])), \
                mock.patch.object(publish, 'publish',
                                  side_effect=publish.ConcurrentCommit('kept moving')):
            why = correct.file_history(product, 'T-0001', '- a line', 'msg')
        self.assertIn('record commit failed', why)
        self.assertIn('kept moving', why)


if __name__ == '__main__':
    unittest.main()
