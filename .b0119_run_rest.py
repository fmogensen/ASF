import sys
sys.path.insert(0, '.')
from tools import run_tests

with open('.b0119_modules.txt') as f:
    only = f.read().split()

rc = run_tests.run('tests', only=only)
sys.exit(rc)
