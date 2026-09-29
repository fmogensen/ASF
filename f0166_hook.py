"""F-0166 reproduction (scratch, not committed): the pre-commit's stale-Backlinks refusal."""
import io
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from asf.record import check  # noqa: E402


def git(repo, *args):
    return subprocess.run(['git', '-C', repo] + list(args), capture_output=True, text=True)


def scratch_record():
    tmp = tempfile.mkdtemp(prefix='f0166-hook-')
    repo = os.path.join(tmp, 'repo')
    shutil.copytree(os.path.join(ROOT, 'sample', 'backlog'), os.path.join(repo, 'backlog'))
    git(repo, 'init', '-q')
    git(repo, 'config', 'user.email', 'a@b.c')
    git(repo, 'config', 'user.name', 'a')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-qm', 'base')
    return tmp, repo


def run_staged(repo):
    out = io.StringIO()
    cwd = os.getcwd()
    os.chdir(os.path.join(repo, 'backlog'))
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = check.cmd_check_staged(os.path.join(repo, 'backlog'))
    finally:
        os.chdir(cwd)
    return rc, out.getvalue()


def case(name, mutate):
    tmp, repo = scratch_record()
    card = os.path.join(repo, 'backlog', 'features', 'F-0001.md')
    with open(card, encoding='utf-8') as f:
        text = f.read()
    with open(card, 'w', encoding='utf-8') as f:
        f.write(mutate(text))
    git(repo, 'add', '-A')
    rc, out = run_staged(repo)
    print(f'=== {name}\n    rc={rc} ({"REFUSED" if rc else "passes"})')
    for line in out.strip().splitlines():
        print('    ' + line)
    with open(card, encoding='utf-8') as f:
        now = f.read()
    print(f'    card note still present after the hook: '
          f'{"yes" if "hand-appended" in now or "MANUAL" in now else "n/a"}')
    shutil.rmtree(tmp)
    print()


case('a bare note appended at the end of the card (today)',
     lambda t: t + '\nNote 2026-09-26: a hand-appended note.\n')

case('a card whose ## Backlinks list is stale, index.json correct',
     lambda t: t.replace('- [T-0001](../tasks/T-0001.md) Split on whitespace\n', ''))

case('a card whose ## Children list is stale, index.json correct',
     lambda t: t.replace('- [S-0002](../stories/S-0002.md) Empty input counts zero — New\n', ''))

case('a staged mention that makes index.json stale too (the 69380aa path)',
     lambda t: t.replace('## Description\n', '## Description\nsee B-0001 and S-0003 for this\n'))
