"""F-0260 PD5/PD6/P9/P11: ``mint_id``'s floor is origin's own tree, read through
``asf.gitops``, never the working tree a checkout can leave stale (``asf/record/ids.py``)."""
import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import env
from asf.record import idclaim, ids
from tests import gitfixture

FOLDERS = ['epics', 'features', 'stories', 'tasks', 'bugs', 'decisions', 'rules']


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True,
                          check=True, stdin=subprocess.DEVNULL).stdout.strip()


def _write_bug(root, n):
    path = os.path.join(root, 'bugs', f'B-{n:04d}.md')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(
            f'---\nid: B-{n:04d}\ntype: bug\ntitle: seed\n'
            '# ---- machine ----\nstate: New\nstage_since: 2026-09-01T00:00:00Z\n'
            'updated: 2026-09-01T00:00:00Z\n---\n## Description\n\n## History\n'
            '- 2026-09-01: created\n')


class OriginFixtures(unittest.TestCase):
    """A bare origin on ``main``, seeded with ``B-0001``..``<id>``; clones of it the tests read
    as checkouts, and a product registry a checkout can resolve to for ``flags``."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ids_origin_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.origin = os.path.join(self.tmp, 'origin.git')
        git(self.tmp, 'init', '-q', '--bare', '-b', 'main', self.origin)
        self.home = os.path.join(self.tmp, 'home')
        os.makedirs(os.path.join(self.home, 'products'))
        old_home = env.ASF_HOME
        env.ASF_HOME = self.home
        self.addCleanup(setattr, env, 'ASF_HOME', old_home)
        self._seeded = 0
        self._n = 0

    def _scratch_clone(self):
        self._n += 1
        path = os.path.join(self.tmp, f'scratch{self._n}')
        git(self.tmp, 'clone', '-q', self.origin, path)
        gitfixture.identity(path)
        return path

    def _seed_bugs_up_to(self, n):
        if n <= self._seeded:
            return
        scratch = self._scratch_clone()
        for f in FOLDERS:
            os.makedirs(os.path.join(scratch, f), exist_ok=True)
        for i in range(self._seeded + 1, n + 1):
            _write_bug(scratch, i)
        git(scratch, 'add', '-A')
        git(scratch, 'commit', '-qm', f'seed to B-{n:04d}')
        git(scratch, 'push', '-q', 'origin', 'HEAD:main')
        self._seeded = n

    def _register_product(self, checkout, flags):
        name = f'fixture{self._n}'
        lines = [f'product: {name}', f'backlog_dir: {checkout}', 'main: main']
        if flags:
            lines.append('conventions:')
            lines.append('  flags:')
            for k, v in flags.items():
                lines.append(f'    {k}: {v}')
        with open(os.path.join(self.home, 'products', f'{name}.yaml'), 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')

    def _clone(self, flags=None):
        self._n += 1
        path = os.path.join(self.tmp, f'checkout{self._n}')
        git(self.tmp, 'clone', '-q', self.origin, path)
        gitfixture.identity(path)
        if flags:
            self._register_product(path, flags)
        return path

    def checkout_cloned_at(self, id_, flags=None):
        """A checkout cloned from origin once it holds bugs up to ``id_`` ('B-0267')."""
        self._seed_bugs_up_to(int(id_.split('-')[1]))
        return self._clone(flags)

    def two_checkouts_cloned_at(self, id_):
        """Two independent clones, both at the same point."""
        self._seed_bugs_up_to(int(id_.split('-')[1]))
        return self._clone(), self._clone()

    def origin_pushes_bugs(self, *ns):
        """Origin moves ahead of every existing checkout: bugs ``ns`` land on ``main`` through a
        fresh clone, never through a checkout a test already holds."""
        scratch = self._scratch_clone()
        os.makedirs(os.path.join(scratch, 'bugs'), exist_ok=True)
        for n in ns:
            _write_bug(scratch, n)
        git(scratch, 'add', '-A')
        git(scratch, 'commit', '-qm', f'origin: {", ".join(f"B-{n:04d}" for n in ns)}')
        git(scratch, 'push', '-q', 'origin', 'HEAD:main')
        self._seeded = max(self._seeded, *ns)

    def fixture_record_without_origin(self, id_):
        """A plain local repo, no remote at all, seeded with bugs up to ``id_``."""
        top = int(id_.split('-')[1])
        self._n += 1
        root = os.path.join(self.tmp, f'noorigin{self._n}')
        os.makedirs(root)
        git(root, 'init', '-q', '-b', 'main')
        gitfixture.identity(root)
        for f in FOLDERS:
            os.makedirs(os.path.join(root, f), exist_ok=True)
        for i in range(1, top + 1):
            _write_bug(root, i)
        git(root, 'add', '-A')
        git(root, 'commit', '-qm', 'seed')
        return root


class RecordIdsOriginTests(OriginFixtures):
    def test_a_checkout_behind_origin_does_not_remint_origins_ids(self):
        """P9, verbatim: cloned at B-0267, origin then carrying B-0268..B-0270."""
        root = self.checkout_cloned_at('B-0267')
        self.origin_pushes_bugs(268, 269, 270)
        self.assertEqual(ids.record_top(root, 'B'), 267)       # the working tree is still behind
        self.assertEqual([ids.mint_id(root, {}, 'bug') for _ in range(3)],
                         ['B-0271', 'B-0272', 'B-0273'])

    def test_origin_top_reads_origins_tree_not_the_working_tree(self):
        root = self.checkout_cloned_at('B-0267')
        self.origin_pushes_bugs(268, 269, 270)
        self.assertEqual(ids.origin_top(root, 'B')[0], 270)
        self.assertEqual(ids.record_top(root, 'B'), 267)

    def test_the_grooms_intake_mint_claims_on_origin_like_asf_new(self):
        """D6: the gate was `claim or worktree`, and the intake passed neither (P10)."""
        root = self.checkout_cloned_at('B-0267')
        before = {c.ref for c in idclaim.claims(root)}
        minted = ids.mint_id(root, {}, 'bug')                  # claim=False, not a worktree
        self.assertEqual({c.ref for c in idclaim.claims(root)} - before,
                         {idclaim.ref_for('B', int(minted.split('-')[1]))})

    def test_two_writers_that_both_read_origins_top_do_not_mint_the_same_id(self):
        """What the floor fix alone would have left open (D6)."""
        a, b = self.two_checkouts_cloned_at('B-0267')
        self.assertNotEqual(ids.mint_id(a, {}, 'bug'), ids.mint_id(b, {}, 'bug'))

    def test_a_backlog_id_range_still_wins_and_claims_nothing_new(self):
        root = self.checkout_cloned_at('B-0267')
        with mock.patch.dict(os.environ, {'BACKLOG_ID_RANGE': 'B:0500-0549'}):
            self.assertEqual(ids.mint_id(root, {}, 'bug'), 'B-0500')

    def test_a_record_with_no_origin_keeps_the_local_path(self):
        root = self.fixture_record_without_origin('B-0267')
        self.assertEqual(ids.mint_id(root, {}, 'bug'), 'B-0268')

    def test_id_claim_off_keeps_the_local_path_but_still_clears_origins_floor(self):
        root = self.checkout_cloned_at('B-0267', flags={'id_claim': 'off'})
        self.origin_pushes_bugs(268, 269, 270)
        self.assertEqual(ids.mint_id(root, {}, 'bug'), 'B-0271')

    def test_an_unreachable_origin_mints_locally_but_still_refuses_asf_new(self):
        """PD6: the gate read origin and could not — a ``claim=False`` caller falls back to the
        local path over ``max_n``, with the line said, and ``asf new`` (``claim=True``) keeps
        today's refusal, because a console mint should not collide where an error is safer."""
        root = self.checkout_cloned_at('B-0267')
        git(root, 'remote', 'set-url', 'origin', os.path.join(self.tmp, 'no-such-origin.git'))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            minted = ids.mint_id(root, {}, 'bug')
        self.assertEqual(minted, 'B-0268')
        self.assertIn('ids: origin unreachable — B-id minted locally, may collide', out.getvalue())
        with self.assertRaises(SystemExit):
            ids.mint_id(root, {}, 'bug', claim=True)

    def test_the_local_fallback_refreshes_the_claims_mirror_before_stepping_over_it(self):
        """C1 (review-t-0817): a block another job claimed by push, no card written, must still
        be invisible to this checkout's stale local mirror of ``refs/asf/ids/*`` — ``origin_top``
        alone (267) is not enough; the local fallback must refresh the mirror too, or it mints
        B-0268 right on top of the other job's claim."""
        c1 = self.checkout_cloned_at('B-0267', flags={'id_claim': 'off'})
        c2 = self._clone()
        idclaim.claim_one(c2, 'B', 'other-job', floor=267)      # claims B-0268 on origin, no card
        self.assertEqual(ids.origin_top(c1, 'B')[0], 267)       # the floor alone still reads 267
        self.assertEqual(ids.mint_id(c1, {}, 'bug'), 'B-0269')  # but the mirror refresh steps over it


if __name__ == '__main__':
    unittest.main()
