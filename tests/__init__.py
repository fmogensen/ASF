"""The test package (used by `discover -t .`, and by every `python -m unittest tests.<module>`):
the suite runs against its own operator home, never ~/.ASF (B-0043), never the caller's identity
(B-0055) and never the caller's git config (B-0114). `tests/test_00_home.py` does the same three
for `discover -s tests`, first. The two are twins and must stay in step — each defect above was
once fixed in one of them only; `tests/test_hermetic.py` now pins both entry points."""
import atexit
import os
import shutil
import tempfile

# The operator's own files (~/.ASF's config and product files, the runtime's settings in every
# worker account the config names) are snapshotted now and checked at exit: a process that wrote
# a path of its own into one exits non-zero, whatever its tests said. First, so its exit check runs
# after the temp home's cleanup below. Twin: tests/test_00_home.py.
from asf import hermetic  # imports nothing of asf.env: ASF_HOME below is still read after it

hermetic.guard_operator_files()

if not os.environ.get('ASF_TESTS_HOME') and not os.environ.get('ASF_HOME', '').startswith(tempfile.gettempdir()):
    os.environ['ASF_HOME'] = tempfile.mkdtemp(prefix='asf-tests-home-')
    atexit.register(shutil.rmtree, os.environ['ASF_HOME'], True)  # never left in $TMPDIR
elif os.environ.get('ASF_TESTS_HOME'):
    os.environ['ASF_HOME'] = os.environ['ASF_TESTS_HOME']
# The host-pressure guard reads a quiet host in the suite (asf.workers.host): a loaded developer
# machine must not hold every launch a test expects. Subprocesses inherit it.
os.environ['ASF_HOST_READING'] = '0 1 0'
# The runtime's config dir is the caller's (a worker session's is its account's, and a runtime CLI
# a test reaches writes its settings there): the suite's is a dir under its own temp home.
os.environ[hermetic.RUNTIME_CONFIG_DIR] = os.path.join(os.environ['ASF_HOME'], 'runtime-config')
# B-0114: a session that runs the suite carries its own core.hooksPath in GIT_CONFIG_*, and git
# applies it to every repo — a fixture repo this suite creates would answer with the caller's hook
# dir, and `asf hooks install` would call those hooks foreign. `tests/test_00_home.py` drops it for
# `discover -s tests`; this drops it for every other entry point, `python -m unittest tests.<mod>`
# first among them, since importing any test module imports this package before that one.
hermetic.strip_git_config(os.environ)
# The same leak through a hook's own variables: a suite run from a pre-commit / pre-push hook
# inherits GIT_DIR / GIT_WORK_TREE / GIT_INDEX_FILE, and every `git -C <fixture>` would then act on
# the caller's repo — a fixture's hook (review-b-0111's /x/asf) written into a real checkout.
for _var in hermetic.GIT_HOOK:
    os.environ.pop(_var, None)
# B-0055, the same miss in the other direction: the tick's ASF_PRODUCT and a session's job reach
# this entry point too, and a test that runs a record command with no --product then resolves a
# product the suite's temp home has never heard of. A test that needs a product names one.
for _var in hermetic.CALLER_IDENTITY:
    os.environ.pop(_var, None)
# F-0281: git's global config is a file the suite owns, holding the suite's identity — a fixture
# never needs a `git config user.*` of its own, and the caller's global config is never read.
hermetic.suite_git_identity(os.environ['ASF_HOME'])
