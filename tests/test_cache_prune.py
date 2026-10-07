"""The shared build cache is bounded by rule (:mod:`asf.workers.caches`).

One fixture: an ASF_HOME whose product state dir holds a cache directory of entries with known
sizes and ages."""
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from asf import conventions, env
from asf.workers import caches

DAY = 86400
MB = 1024 * 1024


class CacheFixture(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        home = tempfile.mkdtemp(prefix='caches_home_')
        self.addCleanup(shutil.rmtree, home, True)
        p = mock.patch.object(env, 'ASF_HOME', home)
        p.start()
        self.addCleanup(p.stop)
        self.product = env.Product('sample', {})
        self.cache = os.path.join(env.state_dir(self.product), 'turbo-cache')
        os.makedirs(self.cache)
        self.cfg = {'worker_pool': {'env': {'TURBO_CACHE_DIR': self.cache}}}

    def conv(self, **over):
        return conventions.Conventions.from_mapping({'cache_prune': over})

    def entry(self, stem, mb, age_days, meta=True):
        """One cache entry: an archive of ``mb`` MiB and its sidecar, last used ``age_days`` ago."""
        paths = [os.path.join(self.cache, f'{stem}.tar.zst')]
        if meta:
            paths.append(os.path.join(self.cache, f'{stem}-meta.json'))
        with open(paths[0], 'wb') as f:
            f.write(b'\0' * (mb * MB))
        if meta:
            with open(paths[1], 'w') as f:
                json.dump({'hash': stem}, f)
        when = self.now - age_days * DAY
        for path in paths:
            os.utime(path, (when, when))
        return paths

    def prune(self, fix=True, **over):
        lines = []
        state = caches.prune(self.product, cfg=self.cfg, conv=self.conv(**over), fix=fix,
                             out=lines.append, now=self.now)
        return state, lines


class PrunePass(CacheFixture):
    def test_an_entry_past_the_age_goes_whole_and_a_young_one_stays(self):
        old = self.entry('aaa', 4, age_days=10)
        young = self.entry('bbb', 4, age_days=1)
        state, lines = self.prune()
        for path in old:                    # the archive and its sidecar, together (C5)
            self.assertFalse(os.path.exists(path), path)
        for path in young:
            self.assertTrue(os.path.exists(path), path)
        d = state['dirs'][0]
        self.assertEqual((d['pruned'], d['by_reason']), (1, {'age': 1}))
        self.assertEqual(len(lines), 1)     # one line per pass (C10)
        self.assertIn('pruned 1', lines[0])
        self.assertIn('1 over 7d', lines[0])

    def test_the_cap_takes_the_oldest_first_until_it_fits(self):
        for i in range(6):
            self.entry(f'e{i}', 1, age_days=i + 1)   # e5 oldest, e0 newest
        state, lines = self.prune(max_age_days=0, max_size_gb=4 * MB / caches.GB)
        gone = [f'e{i}' for i in range(6)
                if not os.path.exists(os.path.join(self.cache, f'e{i}.tar.zst'))]
        self.assertEqual(sorted(gone), ['e4', 'e5'])
        self.assertEqual(state['dirs'][0]['by_reason'], {'cap': 2})
        self.assertIn('over the 0.0 GB cap', lines[0])

    def test_an_entry_used_inside_the_floor_is_never_taken(self):
        fresh = self.entry('hot', 8, age_days=0)     # written now: a live session's own
        state, lines = self.prune(max_age_days=0, max_size_gb=0.000001, min_age_min=60)
        for path in fresh:
            self.assertTrue(os.path.exists(path), path)
        self.assertEqual(state['dirs'][0]['pruned'], 0)
        self.assertIn('nothing to prune', lines[0])

    def test_a_read_today_keeps_an_entry_written_a_fortnight_ago(self):
        paths = self.entry('hit', 2, age_days=14)
        for path in paths:                            # read now, not rewritten
            os.utime(path, (self.now, self.now - 14 * DAY))
        state, _lines = self.prune()
        self.assertEqual(state['dirs'][0]['pruned'], 0)
        for path in paths:
            self.assertTrue(os.path.exists(path), path)

    def test_a_cache_outside_the_state_dir_is_refused_and_untouched(self):
        outside = tempfile.mkdtemp(prefix='elsewhere_')
        self.addCleanup(shutil.rmtree, outside, True)
        victim = os.path.join(outside, 'zzz.tar.zst')
        with open(victim, 'wb') as f:
            f.write(b'\0' * MB)
        os.utime(victim, (self.now - 99 * DAY, self.now - 99 * DAY))
        self.cfg = {'worker_pool': {'env': {'TURBO_CACHE_DIR': outside}}}
        state, lines = self.prune()
        self.assertTrue(os.path.exists(victim))       # 99 days old and still there
        self.assertIn('outside the product', state['dirs'][0]['problem'])
        self.assertIn('is not pruned', lines[0])

    def test_a_value_that_is_not_an_absolute_path_is_refused(self):
        self.cfg = {'worker_pool': {'env': {'TURBO_CACHE_DIR': '~/turbo-cache'}}}
        state, lines = self.prune()
        self.assertIn('not an absolute path', state['dirs'][0]['problem'])
        self.assertEqual(state['dirs'][0]['pruned'], 0)
        self.assertIn('is not pruned', lines[0])

    def test_no_cache_configured_is_no_line_no_state_no_rows(self):
        self.cfg = {}
        state, lines = self.prune()
        self.assertEqual(lines, [])
        self.assertIsNone(state)
        self.assertIsNone(caches.read_state(self.product))
        self.assertIsNone(caches.doctor_line(self.product))
        self.assertIsNone(caches.status_line(self.product))

    def test_a_dry_pass_measures_and_removes_nothing(self):
        paths = self.entry('old', 3, age_days=30)
        state, _lines = self.prune(fix=False)
        for path in paths:
            self.assertTrue(os.path.exists(path), path)
        self.assertEqual(state['dirs'][0]['entries'], 1)
        self.assertGreaterEqual(state['dirs'][0]['bytes'], 3 * MB)

    def test_per_pass_bounds_one_pass_and_the_rest_is_left(self):
        for i in range(5):
            self.entry(f'x{i}', 1, age_days=30 + i)
        state, lines = self.prune(per_pass=2)
        self.assertEqual((state['dirs'][0]['pruned'], state['dirs'][0]['left']), (2, 3))
        self.assertIn('3 left for the next pass', lines[0])

    def test_a_directory_child_is_counted_and_never_removed(self):
        nested = os.path.join(self.cache, 'sub')
        os.makedirs(nested)
        inner = os.path.join(nested, 'keep')
        with open(inner, 'w') as f:
            f.write('x')
        os.utime(nested, (self.now - 99 * DAY, self.now - 99 * DAY))
        state, _lines = self.prune()
        self.assertTrue(os.path.exists(inner))
        self.assertEqual(state['dirs'][0]['skipped_dirs'], 1)


class CachePruneConventions(unittest.TestCase):
    def test_defaults(self):
        conv = conventions.Conventions.from_mapping({})
        self.assertEqual(conv.pruning('max_age_days'), 7)
        self.assertEqual(conv.pruning('max_size_gb'), 10)
        self.assertEqual(conv.pruning('min_age_min'), 60)
        self.assertEqual(conv.pruning('env_vars'), ('TURBO_CACHE_DIR',))
        self.assertEqual(conv.pruning('summary_glob'), conventions.DEFAULT_RUN_SUMMARY_GLOB)

    def test_an_override_wins_and_zero_turns_a_half_off(self):
        conv = conventions.Conventions.from_mapping(
            {'cache_prune': {'max_size_gb': 0.5, 'max_age_days': 0}})
        self.assertEqual(conv.pruning('max_size_gb'), 0.5)
        self.assertEqual(conv.pruning('max_age_days'), 0)
        self.assertEqual(conv.pruning('min_age_min'), 60)   # untouched keys keep their default

    def test_a_bad_key_and_a_bad_value_are_named(self):
        problems = dict(conventions.validate_mapping(
            {'cache_prune': {'max_age_dayz': 7, 'max_size_gb': -1}}))
        self.assertIn('cache_prune.max_age_dayz', problems)
        self.assertIn('cache_prune.max_size_gb', problems)

    def test_a_misshapen_block_is_a_shape_finding_not_a_silent_default(self):
        conv = conventions.Conventions.from_mapping({'cache_prune': 'off'})
        self.assertEqual(conv.pruning('max_age_days'), 7)
        self.assertTrue(any('cache_prune' in str(f) for f in conv.shape_findings()))
