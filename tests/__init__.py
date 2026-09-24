"""The test package (used by `discover -t .`): the suite runs against its own operator home,
never ~/.ASF (B-0043). `tests/test_00_home.py` does the same for `discover -s tests`, first."""
import os
import tempfile

if not os.environ.get('ASF_TESTS_HOME') and not os.environ.get('ASF_HOME', '').startswith(tempfile.gettempdir()):
    os.environ['ASF_HOME'] = tempfile.mkdtemp(prefix='asf-tests-home-')
elif os.environ.get('ASF_TESTS_HOME'):
    os.environ['ASF_HOME'] = os.environ['ASF_TESTS_HOME']
# The host-pressure guard reads a quiet host in the suite (asf.workers.host): a loaded developer
# machine must not hold every launch a test expects. Subprocesses inherit it.
os.environ['ASF_HOST_READING'] = '0 1 0'
