"""tests/test_readme_first_user.py — the README-only first-user run: exactly the commands under
the README's ``## Quick start`` heading, in order, the way a stranger copies them — through to a
first landed Task, on the stub runtime (``sample/first_task.json``), with no account, network,
agent or ``gh``.

The only substitutions are the ones a test cannot avoid: the clone URL becomes a local bare
mirror of this checkout with the same basename (so ``cd <name>`` still works), and ``<product>``
in the second block becomes the sample product the first block made. Everything runs under a
temp ``HOME`` (:func:`asf.hermetic.build`), with an ``asf`` on ``PATH`` that is the clone's own
code — what a git hook or a session shells out to.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from asf import hermetic

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.<module>` does not
    from test_scheduler import fake_clis
except ImportError:  # pragma: no cover - import shape only
    from tests.test_scheduler import fake_clis

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(ROOT, 'README.md')


def quick_start_blocks(text):
    """The fenced ``bash`` blocks under ``## Quick start``, up to the next ``## `` heading."""
    m = re.search(r'^## Quick start\s*$(.*?)(?=^## )', text, re.M | re.S)
    if not m:
        return []
    return [b.strip('\n') for b in re.findall(r'^```(?:bash|sh)\n(.*?)^```', m.group(1), re.M | re.S)]


def commands(block):
    """One block's command lines: comments and blank lines dropped, trailing comments cut."""
    out = []
    for line in block.splitlines():
        line = re.sub(r'\s+#.*$', '', line).strip()
        if line and not line.startswith('#'):
            out.append(line)
    return out


class QuickStartBlocks(unittest.TestCase):
    def test_the_readme_has_a_quick_start_with_a_clone_and_the_script(self):
        with open(README, encoding='utf-8') as f:
            blocks = quick_start_blocks(f.read())
        self.assertGreaterEqual(len(blocks), 1, 'no bash block under ## Quick start')
        first = commands(blocks[0])
        self.assertTrue(any(c.startswith('git clone ') for c in first), first)
        self.assertTrue(any('tools/quickstart.sh' in c for c in first), first)

    def test_the_parser_reads_only_the_quick_start_section(self):
        text = ('## Install\n```bash\nnot this\n```\n## Quick start\nprose\n```bash\n'
                'one  # a comment\n# only a comment\ntwo\n```\n## Configuration\n```bash\nnor this\n```\n')
        self.assertEqual([commands(b) for b in quick_start_blocks(text)], [['one', 'two']])


class ReadmeFirstUserRun(unittest.TestCase):
    """One run of the README's Quick start, start to a landed Task; each test reads what it left."""

    @classmethod
    def setUpClass(cls):
        with open(README, encoding='utf-8') as f:
            cls.blocks = [commands(b) for b in quick_start_blocks(f.read())]
        cls.tmp = os.path.realpath(tempfile.mkdtemp(prefix='asf_readme_'))
        work, scratch = os.path.join(cls.tmp, 'work'), os.path.join(cls.tmp, 'scratch')
        os.makedirs(work)
        os.makedirs(scratch)

        url = re.search(r'git clone (\S+)', ' '.join(cls.blocks[0])).group(1)
        name = os.path.basename(url.rstrip('/'))
        mirror = os.path.join(cls.tmp, 'origin', name if name.endswith('.git') else name + '.git')
        subprocess.run(['git', 'clone', '-q', '--bare', ROOT, mirror], check=True,
                       capture_output=True)
        cls.clone = os.path.join(work, name[:-4] if name.endswith('.git') else name)

        stub_dir = os.path.join(cls.tmp, 'bin')
        fake_clis(stub_dir)
        with open(os.path.join(stub_dir, 'gh'), 'w') as f:
            f.write('#!/bin/sh\necho "gh: offline in tests" >&2\nexit 1\n')
        with open(os.path.join(stub_dir, 'asf'), 'w') as f:
            f.write(f'#!/bin/sh\nPYTHONPATH="{cls.clone}" exec {sys.executable} -m asf.cli "$@"\n')
        for n in ('gh', 'asf'):
            os.chmod(os.path.join(stub_dir, n), 0o755)
        path = stub_dir + os.pathsep + os.environ.get('PATH', '')
        cls.env = hermetic.build(dict(os.environ, GH_TOKEN='', PATH=path, TMPDIR=scratch),
                                 worktree=cls.clone, home=os.path.join(cls.tmp, 'home'))
        os.makedirs(cls.env['HOME'], exist_ok=True)
        for k in ('ASF_HOME', 'ASF_PRODUCT'):
            cls.env.pop(k, None)

        script = '\n'.join(['set -e'] + [c.replace(url, mirror) for c in cls.blocks[0]])
        cls.first = subprocess.run(['bash', '-c', script], cwd=work, env=cls.env,
                                   capture_output=True, text=True, timeout=1500)
        lines = cls.first.stdout.splitlines()
        cls.dir = lines[0] if lines else ''
        cls.second = []
        if cls.first.returncode == 0 and len(cls.blocks) > 1:
            env = dict(cls.env, ASF_HOME=os.path.join(cls.dir, '.ASF'),
                       HOME=os.path.join(cls.dir, 'home'))
            for c in cls.blocks[1]:
                cmd = c.replace('<product>', 'sample')
                cls.second.append((cmd, subprocess.run(['bash', '-c', cmd], cwd=cls.clone, env=env,
                                                       capture_output=True, text=True, timeout=600)))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_first_block_runs_clean(self):
        self.assertEqual(self.first.returncode, 0, self.first.stdout[-4000:] + self.first.stderr[-4000:])

    def test_a_first_task_landed(self):
        m = re.search(r'^quickstart: first Task landed: (T-\d{4})$', self.first.stdout, re.M)
        self.assertIsNotNone(m, self.first.stdout[-4000:])
        with open(os.path.join(self.dir, 'sample', 'backlog', 'index.json'), encoding='utf-8') as f:
            self.assertEqual(json.load(f)['items'][m.group(1)]['state'], 'Closed')

    def test_every_command_of_the_second_block_exits_0(self):
        self.assertTrue(self.second, 'the second block never ran')
        for cmd, p in self.second:
            self.assertEqual(p.returncode, 0, f'{cmd}\n{p.stdout[-3000:]}\n{p.stderr[-3000:]}')


if __name__ == '__main__':
    unittest.main()
