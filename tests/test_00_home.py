"""Loaded first by `unittest discover -s tests` (modules import in name order): the suite runs
against its own operator home, never the operator's ~/.ASF (B-0043). Both the environment (for
every subprocess a test starts) and `asf.env.ASF_HOME` (the constant the package reads, which
tests patch per case) point at it. ASF_TESTS_HOME names a directory to use instead. The caller's
identity (`asf.hermetic.CALLER_IDENTITY`: the tick's ASF_PRODUCT, a session's job and mint range)
is dropped the same way (B-0055): a test that needs a product names one."""
import os
import tempfile
import unittest


def hermetic_home():
    chosen = os.environ.get('ASF_TESTS_HOME')
    if not chosen:
        current = os.environ.get('ASF_HOME', '')
        chosen = current if current.startswith(tempfile.gettempdir()) else tempfile.mkdtemp(prefix='asf-tests-home-')
    os.environ['ASF_HOME'] = chosen
    from asf import env, hermetic
    env.ASF_HOME = chosen
    for var in hermetic.CALLER_IDENTITY:
        os.environ.pop(var, None)
    return chosen


hermetic_home()


class HomeIsHermetic(unittest.TestCase):
    def test_the_suite_never_reads_the_operators_home(self):
        from asf import env
        self.assertNotEqual(os.path.realpath(env.ASF_HOME), os.path.realpath(os.path.expanduser('~/.ASF')))
        self.assertEqual(os.environ.get('ASF_HOME'), env.ASF_HOME)  # subprocesses inherit it

    def test_the_suite_never_inherits_the_callers_identity(self):
        # B-0055: CI's hermetic step exports ASF_PRODUCT the way the tick does; a test that runs a
        # record command with no --product then resolves a product the suite's home never has
        from asf import hermetic
        for var in hermetic.CALLER_IDENTITY:
            self.assertNotIn(var, os.environ, f'{var} reached the suite from its caller')
