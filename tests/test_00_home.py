"""Loaded first by `unittest discover -s tests` (modules import in name order): the suite runs
against its own operator home, never the operator's ~/.ASF (B-0043). Both the environment (for
every subprocess a test starts) and `asf.env.ASF_HOME` (the constant the package reads, which
tests patch per case) point at it. ASF_TESTS_HOME names a directory to use instead."""
import os
import tempfile
import unittest


def hermetic_home():
    chosen = os.environ.get('ASF_TESTS_HOME')
    if not chosen:
        current = os.environ.get('ASF_HOME', '')
        chosen = current if current.startswith(tempfile.gettempdir()) else tempfile.mkdtemp(prefix='asf-tests-home-')
    os.environ['ASF_HOME'] = chosen
    from asf import env
    env.ASF_HOME = chosen
    return chosen


hermetic_home()


class HomeIsHermetic(unittest.TestCase):
    def test_the_suite_never_reads_the_operators_home(self):
        from asf import env
        self.assertNotEqual(os.path.realpath(env.ASF_HOME), os.path.realpath(os.path.expanduser('~/.ASF')))
        self.assertEqual(os.environ.get('ASF_HOME'), env.ASF_HOME)  # subprocesses inherit it
