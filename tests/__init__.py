"""The test package (used by `discover -t .`, and by every `python -m unittest tests.<module>`):
the suite runs against its own operator home, never ~/.ASF (B-0043), never the caller's identity
(B-0055) and never the caller's git config (B-0114). `tests/test_00_home.py` does the same three
for `discover -s tests`, first. The two are twins and must stay in step — each defect above was
once fixed in one of them only; `tests/test_hermetic.py` now pins both entry points."""
import atexit
import os
import shutil
import tempfile

if not os.environ.get('ASF_TESTS_HOME') and not os.environ.get('ASF_HOME', '').startswith(tempfile.gettempdir()):
    os.environ['ASF_HOME'] = tempfile.mkdtemp(prefix='asf-tests-home-')
    atexit.register(shutil.rmtree, os.environ['ASF_HOME'], True)  # never left in $TMPDIR
elif os.environ.get('ASF_TESTS_HOME'):
    os.environ['ASF_HOME'] = os.environ['ASF_TESTS_HOME']
# The host-pressure guard reads a quiet host in the suite (asf.workers.host): a loaded developer
# machine must not hold every launch a test expects. Subprocesses inherit it.
os.environ['ASF_HOST_READING'] = '0 1 0'
# B-0114: a session that runs the suite carries its own core.hooksPath in GIT_CONFIG_*, and git
# applies it to every repo — a fixture repo this suite creates would answer with the caller's hook
# dir, and `asf hooks install` would call those hooks foreign. `tests/test_00_home.py` drops it for
# `discover -s tests`; this drops it for every other entry point, `python -m unittest tests.<mod>`
# first among them, since importing any test module imports this package before that one.
from asf import hermetic  # after the ASF_HOME lines above: asf.env reads ASF_HOME at import

hermetic.strip_git_config(os.environ)
# B-0055, the same miss in the other direction: the tick's ASF_PRODUCT and a session's job reach
# this entry point too, and a test that runs a record command with no --product then resolves a
# product the suite's temp home has never heard of. A test that needs a product names one.
for _var in hermetic.CALLER_IDENTITY:
    os.environ.pop(_var, None)
