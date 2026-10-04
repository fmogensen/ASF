"""The session registry's per-question cost (asf.workers.lifecycle._folded).

2026-10-04: a product's ``sessions.jsonl`` reached 21,827 lines (8.9 MB). Every per-item question
the wave asks (``item_runs``, ``rounds_of``, ``voided_sha``, ``item_park`` …) re-read the whole
file and compared its bytes with the cached copy, so a wave that asked a few hundred thousand of
them ran 23+ minutes at 100% CPU holding the tick lock. A cache hit is now one ``stat``."""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from asf.workers import lifecycle as lc

LINES = 25_000
QUESTIONS = 20_000
#: the hot path's ceiling: 20k questions took ~100 s against a 25k-line registry before the fix
#: (one 9 MB read and memcmp each); the stat-only hit answers them in well under a second
LIMIT_S = 10.0


def _ledger(path, lines=LINES, items=400):
    with open(path, 'w') as f:
        for i in range(lines):
            item = f'T-{i % items:04d}'
            job = f'task-{item.lower()}-{i // items}'
            if i % 3 == 0:
                rec = {'job': job, 'pid': 1000 + i, 'started': f'2026-10-0{1 + i % 4}T{i % 24:02d}:00:00Z',
                       'item': item, 'branch': f'feat/{item}-{i // items}', 'kind': 'task',
                       'log': f'/tmp/log-{i}.jsonl', 'pad': 'x' * 300}
            else:
                rec = {'job': job, 'ended': f'2026-10-0{1 + i % 4}T{i % 24:02d}:30:00Z',
                       'result': 'ok', 'lane': {'state': 'PUSHED', 'at': 't', 'head': 'a' * 40}}
            f.write(json.dumps(rec) + '\n')


class RegistryHitCostTests(unittest.TestCase):

    def setUp(self):
        d = tempfile.mkdtemp(prefix='asf-registry-perf-')
        self.addCleanup(lambda: __import__('shutil').rmtree(d, ignore_errors=True))
        self.path = os.path.join(d, 'sessions.jsonl')
        _ledger(self.path)
        lc._REGISTRY_CACHE.clear()
        self.addCleanup(lc._REGISTRY_CACHE.clear)

    def later(self, seconds=5):
        """The wall clock ``seconds`` on: the registry's stat is settled (past the racy window)."""
        real = time.time_ns
        return mock.patch.object(lc.time, 'time_ns', lambda: real() + seconds * 1_000_000_000)

    def test_per_item_questions_on_a_25k_line_registry_are_fast(self):
        self.assertGreater(os.path.getsize(self.path), 5_000_000)
        with self.later():
            lc.runs(self.path)          # the one parse
            t0 = time.perf_counter()
            for n in range(QUESTIONS):
                item = f'T-{n % 400:04d}'
                lc.rounds_of(self.path, item)
                lc.voided_sha(self.path, item, 'a' * 40)
                lc.item_park(self.path, item)
            took = time.perf_counter() - t0
        self.assertLess(took, LIMIT_S, f'{QUESTIONS * 3} registry questions took {took:.1f}s')

    def test_an_append_is_seen_after_a_settled_hit(self):
        with self.later():
            before = len(lc.item_runs(self.path, 'T-0001'))
            with open(self.path, 'a') as f:
                f.write(json.dumps({'job': 'task-new', 'pid': 1, 'started': 'z', 'item': 'T-0001'}) + '\n')
            self.assertEqual(len(lc.item_runs(self.path, 'T-0001')), before + 1)

    def test_a_same_size_rewrite_after_a_settled_hit_is_seen(self):
        small = self.path + '.small'
        with open(small, 'w') as f:
            f.write(json.dumps({'job': 'j', 'pid': 11, 'started': 't1', 'item': 'B-0001'}) + '\n')
        with self.later():
            self.assertEqual(lc.runs(small)['j'][0]['pid'], 11)
            st = os.stat(small)
            with open(small, 'w') as f:   # same size, mtime put back: ctime still moves
                f.write(json.dumps({'job': 'j', 'pid': 12, 'started': 't1', 'item': 'B-0001'}) + '\n')
            os.utime(small, ns=(st.st_atime_ns, st.st_mtime_ns))
            self.assertEqual(lc.runs(small)['j'][0]['pid'], 12)

    def test_a_fresh_registry_is_checked_against_its_bytes(self):
        # inside the racy window the stat alone is not trusted: the bytes are compared
        lc.runs(self.path)
        hit = next(iter(lc._REGISTRY_CACHE.values()))
        self.assertFalse(lc._settled(hit, lc._signature(os.stat(self.path))))
        with self.later():
            lc.runs(self.path)
            self.assertTrue(lc._settled(hit, lc._signature(os.stat(self.path))))


if __name__ == '__main__':
    unittest.main()
