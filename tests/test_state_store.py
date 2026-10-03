"""asf.state.store: whole files, versions, a per-file lock, quarantine, shared refusal, the reaper.

The two races the store exists for: a land request added by one process while another rewrote
the requests file vanished, and a rebuild request did the same. The concurrency test runs two
real processes doing 100 interleaved ``update``s each on one file and requires all 200 to be
there; the jsonl twin requires 200 whole lines.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import redirect_stderr
from unittest import mock

from asf import env
from asf.state import registry
from asf.state import store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = 'alpha'
REQ = 'land-requests.json'


class StoreHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='asf-store-')
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        self.err = io.StringIO()
        quiet = redirect_stderr(self.err)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sdir(self, product=P):
        return os.path.join(self.tmp, 'state', product)

    def product(self, name, flags=None):
        os.makedirs(os.path.join(self.tmp, 'products'), exist_ok=True)
        text = f'repo_dir: {self.tmp}/repo-{name}\nmain: main\n'
        if flags:
            text += 'conventions:\n  flags:\n' + ''.join(f'    {k}: {v}\n' for k, v in flags.items())
        with open(os.path.join(self.tmp, 'products', f'{name}.yaml'), 'w', encoding='utf-8') as f:
            f.write(text)


class Paths(StoreHome):
    def test_product_file_lives_in_its_state_dir(self):
        self.assertEqual(store.path(P, REQ), os.path.join(self.sdir(), REQ))

    def test_shared_file_lives_one_level_up(self):
        self.assertEqual(store.path(P, 'seats.json'), os.path.join(self.tmp, 'state', 'seats.json'))

    def test_unregistered_name_is_refused(self):
        with self.assertRaises(store.StoreUnregistered):
            store.path(P, 'pr-hygiene-123.json')
        with self.assertRaises(store.StoreUnregistered):
            store.read(P, '../escape.json')


class Atomic(StoreHome):
    def test_crash_before_replace_keeps_the_old_file_and_no_temp(self):
        store.write(P, REQ, {'n': 1})
        with mock.patch('os.replace', side_effect=OSError('disk gone')):
            with self.assertRaises(OSError):
                store.write(P, REQ, {'n': 2})
        self.assertEqual(store.read(P, REQ).data, {'n': 1})
        self.assertEqual([f for f in os.listdir(self.sdir()) if f.endswith('.tmp')], [])

    def test_crash_mid_write_keeps_the_old_file(self):
        target = store.path(P, REQ)
        store.atomic_write_json(target, {'n': 1})
        with mock.patch('json.dumps', side_effect=TypeError('unserialisable')):
            with self.assertRaises(TypeError):
                store.atomic_write_json(target, {'n': 2})
        with open(target, encoding='utf-8') as f:
            self.assertEqual(json.load(f), {'n': 1})

    def test_no_fixed_temp_name(self):
        target = store.path(P, REQ)
        seen = []
        real = os.replace
        with mock.patch('os.replace', side_effect=lambda a, b: (seen.append(a), real(a, b))):
            store.atomic_write_json(target, {})
            store.atomic_write_json(target, {})
        self.assertEqual(len(set(seen)), 2)
        self.assertTrue(all(os.path.dirname(s) == self.sdir() for s in seen))


class ReadStates(StoreHome):
    def test_absent_takes_the_default_as_a_copy(self):
        default = {'requests': []}
        got = store.read(P, REQ, default=default)
        self.assertEqual(got, (default, 0, store.ABSENT))
        got.data['requests'].append(1)
        self.assertEqual(default, {'requests': []})

    def test_ok_carries_a_version(self):
        v = store.write(P, REQ, {'a': 1})
        data, version, state = store.read(P, REQ)
        self.assertEqual((data, version, state), ({'a': 1}, v, store.OK))
        self.assertGreater(v, 0)

    def test_corrupt_is_not_absent_and_says_so(self):
        os.makedirs(self.sdir())
        with open(os.path.join(self.sdir(), REQ), 'w') as f:
            f.write('{"half": ')
        data, version, state = store.read(P, REQ, default={})
        self.assertEqual((data, state), ({}, store.CORRUPT))
        self.assertGreater(version, 0)
        self.assertIn('STATE CORRUPT', self.err.getvalue())

    def test_versions_strictly_increase(self):
        versions = [store.write(P, REQ, {'i': i}) for i in range(20)]
        self.assertEqual(versions, sorted(set(versions)))

    def test_jsonl_reads_records_and_skips_a_torn_line(self):
        store.append(P, 'gates.jsonl', {'a': 1})
        with open(store.path(P, 'gates.jsonl'), 'a') as f:
            f.write('{"torn')
        store.append(P, 'gates.jsonl', {'b': 2})
        self.assertEqual(store.read(P, 'gates.jsonl').data, [{'a': 1}, {'b': 2}])

    def test_a_lock_has_nothing_to_read(self):
        with self.assertRaises(store.StoreError):
            store.read(P, 'tick.lock')


class Writes(StoreHome):
    def test_expect_conflict(self):
        v1 = store.write(P, REQ, {'a': 1})
        store.write(P, REQ, {'a': 2})
        with self.assertRaises(store.StoreConflict):
            store.write(P, REQ, {'a': 3}, expect=v1)
        self.assertEqual(store.read(P, REQ).data, {'a': 2})

    def test_expect_zero_means_absent(self):
        store.write(P, REQ, {'a': 1}, expect=0)
        with self.assertRaises(store.StoreConflict):
            store.write(P, REQ, {'a': 2}, expect=0)

    def test_update_from_default_and_in_place(self):
        store.update(P, REQ, lambda d: d.setdefault('q', []).append('x') and None, default={})
        out = store.update(P, REQ, lambda d: dict(d, n=1))
        self.assertEqual(out, {'q': ['x'], 'n': 1})
        self.assertEqual(store.read(P, REQ).data, {'q': ['x'], 'n': 1})

    def test_update_on_corrupt_quarantines_and_refuses(self):
        os.makedirs(self.sdir())
        target = os.path.join(self.sdir(), REQ)
        with open(target, 'w') as f:
            f.write('not json')
        for _ in range(2):  # a second refusal does not copy again
            with self.assertRaises(store.StoreCorrupt) as cm:
                store.update(P, REQ, lambda d: {'wiped': True}, default={})
        with open(target) as f:
            self.assertEqual(f.read(), 'not json')  # never overwritten
        copies = [n for n in os.listdir(self.sdir()) if '.corrupt-' in n]
        self.assertEqual(len(copies), 1)
        self.assertEqual(cm.exception.quarantine, os.path.join(self.sdir(), copies[0]))
        with open(cm.exception.quarantine) as f:
            self.assertEqual(f.read(), 'not json')

    def test_write_over_corrupt_keeps_a_copy(self):
        os.makedirs(self.sdir())
        with open(os.path.join(self.sdir(), REQ), 'w') as f:
            f.write('garbage')
        store.write(P, REQ, {'ok': True})
        self.assertEqual(store.read(P, REQ).data, {'ok': True})
        self.assertEqual(len([n for n in os.listdir(self.sdir()) if '.corrupt-' in n]), 1)

    def test_kind_is_enforced(self):
        with self.assertRaises(store.StoreError):
            store.update(P, 'gates.jsonl', lambda d: d)
        with self.assertRaises(store.StoreError):
            store.append(P, REQ, {})

    def test_busy_lock_times_out_loudly(self):
        target = store.path(P, REQ)
        with store._locked(target):
            t0 = time.monotonic()
            with self.assertRaises(store.StoreBusy):
                store.update(P, REQ, lambda d: d, default={}, timeout_s=0.2)
            self.assertLess(time.monotonic() - t0, 5)
        self.assertIn('STATE BUSY', self.err.getvalue())
        store.update(P, REQ, lambda d: d, default={}, timeout_s=0.2)  # free again

    def test_schema_sidecar_records_the_registry_schema(self):
        self.assertIsNone(store.schema(P, REQ))
        store.write(P, REQ, {})
        self.assertEqual(store.schema(P, REQ), registry.spec(REQ).schema)
        with open(store.path(P, REQ)) as f:
            self.assertEqual(json.load(f), {})  # the file's own shape is untouched
        bumped = dict(registry.REGISTRY, **{REQ: registry.spec(REQ)._replace(schema=2)})
        with mock.patch.object(registry, 'REGISTRY', bumped):
            store.update(P, REQ, lambda d: d)
            self.assertEqual(store.schema(P, REQ), 2)


class Shared(StoreHome):
    def test_mutating_a_shared_file_is_refused_until_every_product_is_on(self):
        self.assertEqual(store.shared_ready(), (False, []))
        self.product('one', {'store_shared': 'on'})
        self.product('two')
        with self.assertRaises(store.StoreRefused) as cm:
            store.update(P, 'seats.json', lambda d: d, default=[])
        self.assertIn('two', str(cm.exception))
        with self.assertRaises(store.StoreRefused):
            store.write(P, 'run-costs.json', {})
        self.assertFalse(os.path.exists(store.path(P, 'seats.json')))
        self.product('two', {'store_shared': 'on'})
        self.assertEqual(store.shared_ready(), (True, []))
        store.update(P, 'seats.json', lambda d: d + [{'job': 'j'}], default=[])
        self.assertEqual(store.read(P, 'seats.json').data, [{'job': 'j'}])

    def test_reading_a_shared_file_is_always_allowed(self):
        os.makedirs(os.path.join(self.tmp, 'state'))
        with open(os.path.join(self.tmp, 'state', 'cloud-sessions.json'), 'w') as f:
            json.dump({'t': 1}, f)
        self.assertEqual(store.read(P, 'cloud-sessions.json').data, {'t': 1})

    def test_quota_names_are_shared(self):
        self.assertTrue(registry.is_shared('quota-limits.json'))
        self.assertTrue(registry.is_shared('quota-samples.jsonl'))
        self.assertFalse(registry.is_shared(REQ))


class Reaper(StoreHome):
    def touch(self, name, age_days=0, now=None):
        os.makedirs(self.sdir(), exist_ok=True)
        full = os.path.join(self.sdir(), name)
        with open(full, 'w') as f:
            f.write('{}')
        t = (now or time.time()) - age_days * 86400
        os.utime(full, (t, t))
        return full

    def test_dry_run_lists_orphans_and_expired_and_keeps_the_rest(self):
        now = time.time()
        self.touch(REQ, 400, now)                     # registered, no TTL: kept however old
        self.touch('pr-hygiene-42.json', 0, now)     # nobody registered it
        self.touch('summary-daily.stamp', 8, now)    # TTL 7 d, outlived
        self.touch('summary-wave.stamp', 1, now)     # within its TTL
        self.touch('.land-requests.json.abc.tmp', 2, now)
        self.touch('.land-requests.json.new.tmp', 0, now)  # a writer may be mid-write
        os.makedirs(os.path.join(self.sdir(), 'worktrees'))
        found = dict(store.reap(P, now=now))
        self.assertEqual(set(found), {'pr-hygiene-42.json', 'summary-daily.stamp',
                                      '.land-requests.json.abc.tmp'})
        self.assertEqual(found['pr-hygiene-42.json'], 'unregistered')
        self.assertTrue(os.path.exists(os.path.join(self.sdir(), 'pr-hygiene-42.json')))

    def test_apply_moves_to_dated_trash_and_purges_old_trash(self):
        now = time.time()
        self.touch('pr-hygiene-1.json', 0, now)
        old = os.path.join(self.sdir(), registry.TRASH, '2000-01-01')
        os.makedirs(old)
        found = store.reap(P, apply=True, now=now)
        day = time.strftime('%Y-%m-%d', time.gmtime(now))
        self.assertTrue(os.path.isfile(os.path.join(self.sdir(), registry.TRASH, day, 'pr-hygiene-1.json')))
        self.assertFalse(os.path.exists(os.path.join(self.sdir(), 'pr-hygiene-1.json')))
        self.assertFalse(os.path.exists(old))
        self.assertIn(('.trash/2000-01-01', 'purged'), found)
        self.assertEqual(store.reap(P, now=now), [])  # the trash is not reaped again

    def test_store_written_files_are_never_orphans(self):
        store.write(P, REQ, {})
        store.append(P, 'gates.jsonl', {})
        self.assertEqual(store.reap(P), [])


_CHILD = textwrap.dedent('''
    import sys, time
    from asf import env
    from asf.state import store
    env.ASF_HOME = sys.argv[1]
    who, n, go = sys.argv[2], int(sys.argv[3]), sys.argv[4]
    while not __import__('os').path.exists(go):
        time.sleep(0.005)
    def add(i):
        def fn(d):
            d['count'] = d.get('count', 0) + 1
            d.setdefault('requests', []).append(f'{who}-{i}')
        return fn
    for i in range(n):
        store.update('alpha', 'land-requests.json', add(i), default={}, timeout_s=60)
        store.append('alpha', 'gates.jsonl', {'by': who, 'i': i, 'pad': 'x' * 512}, timeout_s=60)
''')


class Concurrency(StoreHome):
    def test_two_processes_200_interleaved_updates_lose_none(self):
        go = os.path.join(self.tmp, 'go')
        environ = dict(os.environ, ASF_HOME=self.tmp,
                       PYTHONPATH=os.pathsep.join(filter(None, [ROOT, os.environ.get('PYTHONPATH')])))
        procs = [subprocess.Popen([sys.executable, '-c', _CHILD, self.tmp, who, '100', go],
                                  env=environ, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True) for who in ('a', 'b')]
        open(go, 'w').close()
        for p in procs:
            out, err = p.communicate(timeout=180)
            self.assertEqual(p.returncode, 0, err)
        data = store.read(P, REQ).data
        self.assertEqual(data['count'], 200)
        self.assertEqual(sorted(data['requests']),
                         sorted(f'{w}-{i}' for w in 'ab' for i in range(100)))
        for w in 'ab':  # each process's own order is kept
            mine = [r for r in data['requests'] if r.startswith(w)]
            self.assertEqual(mine, [f'{w}-{i}' for i in range(100)])
        with open(store.path(P, 'gates.jsonl')) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 200)
        records = [json.loads(line) for line in lines]
        self.assertEqual(sorted((r['by'], r['i']) for r in records),
                         sorted((w, i) for w in 'ab' for i in range(100)))


class HealthKeepsToItsOwnProduct(StoreHome):
    """The seat ledger and the run registries are read across products; health writes only its
    own. (Its ``settle_ended`` walks the sessions it is handed — the product's own registry.)"""

    def test_health_never_frees_another_products_seat(self):
        from asf.workers import cloud
        from asf.workers import cloudpid
        from asf.workers import health as health_mod
        from asf.workers import lifecycle
        from asf.workers import pool as pool_mod
        from asf.workers import seats

        mine, theirs = env.Product('one', {'repo_dir': self.tmp, 'main': 'main'}), \
            env.Product('two', {'repo_dir': self.tmp, 'main': 'main'})
        ended = '2026-09-26T12:55:43Z'
        for product, job, trig in ((mine, 'coder-a', 'trig_A'), (theirs, 'coder-b', 'trig_B')):
            tok = f'remote:{trig}'
            pool_mod.append_session(product, {
                'job': job, 'item': 'T-1', 'kind': 'coder', 'account': 'acct-a', 'pid': tok,
                'runtime': 'claude-remote', 'runtime_lane': 'cloud', 'started': '2026-09-26T12:00:00Z'})
            cloudpid.record(tok, cloudpid.WORKING, 'in_progress', product=product.name, job=job)
        pool_mod.update_session(mine, 'coder-a', ended=ended, end_reason='failed: quota-exhausted')
        # their run's ended line is not written yet (their health has not run): still a seat
        with seats.held() as ledger:
            ledger.add({'account': 'acct-a', 'product': 'two', 'job': 'coder-c', 'pid': os.getpid(),
                        'at': seats._stamp()})
        theirs_registry = pool_mod.sessions_path(theirs)
        with open(theirs_registry, 'rb') as f:
            before = f.read()

        found = health_mod.settle_ended(mine, pool_mod.load_sessions(mine),
                                        now=cloud._parse_ts(ended) + 60, retire=lambda run: True)

        self.assertEqual([(j, w) for j, w, _ in found], [('coder-a', 'settled')])
        self.assertEqual(cloudpid.status('remote:trig_B'), cloudpid.WORKING)
        with open(theirs_registry, 'rb') as f:
            self.assertEqual(f.read(), before)
        live = {(r['product'], r['job']) for r in lifecycle.live_all(os.path.join(self.tmp, 'state'))}
        self.assertIn(('two', 'coder-b'), live)
        self.assertNotIn(('one', 'coder-a'), live)
        claims, _why = seats.read()
        self.assertEqual([(c['product'], c['job']) for c in claims], [('two', 'coder-c')])


if __name__ == '__main__':
    unittest.main()
