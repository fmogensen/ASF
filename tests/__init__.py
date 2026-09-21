"""The suite never reads the operator's live ~/.ASF.

unittest imports this package before any test module imports ``asf.env`` (which
reads ``ASF_HOME`` at import), so the home is pinned here to a fresh temp dir.
``ASF_TESTS_HOME`` names one explicitly instead. Subprocess tests inherit it.
"""
import atexit
import os
import shutil
import tempfile

if os.environ.get('ASF_TESTS_HOME'):
    os.environ['ASF_HOME'] = os.environ['ASF_TESTS_HOME']
else:
    _home = tempfile.mkdtemp(prefix='asf-tests-home-')
    atexit.register(shutil.rmtree, _home, ignore_errors=True)
    os.environ['ASF_HOME'] = _home
