"""``flags.queue_store``: the merge queue's requests and rebuilds through :mod:`asf.state.store`.

The two races this closes. A queue pass read the land requests, spent seconds on host reads per
PR, and wrote its copy back — an ``asf land`` request added meanwhile by another process
vanished. A rebuild request the CI start queue wrote while the pass cleared another batch's
vanished the same way. Under the flag every writer goes through one locked read-modify-write on
the file as it is *then*; the tests below make the second process write in exactly the window
the old code lost it in. The files keep their shape: the oldest pinned reader still loads them.
"""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from asf import merge_queue
from asf.harvest import harvest
from asf.state import store

from tests.test_merge_queue import QueueRepo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: a second process: ``argv[1]`` state dir, ``argv[2]`` what to do, ``argv[3:]`` its arguments
_OTHER = '''
import sys
from asf import merge_queue

class On:
    name = 'sample'
    def flag(self, name, default=None):
        return 'on' if name == merge_queue.STORE_FLAG else default

sd, verb, args = sys.argv[1], sys.argv[2], sys.argv[3:]
if verb == 'land':
    merge_queue.add_request(sd, int(args[0]), args[1], product=On())
elif verb == 'rebuild':
    assert merge_queue.request_rebuild(sd, args[0], 'STUCK — other process', product=On())
'''


def other_process(state_dir, *args):
    path = os.pathsep.join(filter(None, [ROOT, os.environ.get('PYTHONPATH')]))
    env = dict(os.environ, PYTHONPATH=path)
    proc = subprocess.run([sys.executable, '-c', _OTHER, state_dir, *args], env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr


class Flagged(QueueRepo):
    flags = {merge_queue.STORE_FLAG: 'on'}

    def product(self, **conventions_):
        return super().product(**dict({'flags': dict(self.flags)}, **conventions_))


class RequestsSurviveAPass(Flagged):
    """``asf land`` from a second process while :func:`requested_ready` waits on the host."""

    def setUp(self):
        super().setUp()
        self.setUpBacks()
        # #5's branch left origin: the pass asks the host, and the host says merged — dropped
        merge_queue.add_request(self.state_dir, 5, 'hotfix/gone', product=self.product())
        self.gh.pr_state[5] = 'MERGED'
        fake, self.fired = self.gh, []

        def slow_host(args):
            if args[:3] == ['pr', 'view', '5'] and not self.fired:
                self.fired.append(True)
                other_process(self.state_dir, 'land', '9', 'hotfix/new')   # lands mid-pass
            return fake(args)
        patch = mock.patch.object(harvest, '_gh', side_effect=slow_host)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_request_added_while_the_pass_waits_on_the_host_is_not_lost(self):
        ln = self.lane()
        self.assertEqual(merge_queue.requested_ready(ln, self.heads(), self.heads()['main']), [])
        self.assertTrue(self.fired)
        reqs = merge_queue.load_requests(self.state_dir)
        self.assertEqual(sorted(reqs), ['9'])                 # #5 dropped, #9 kept
        self.assertEqual(reqs['9']['branch'], 'hotfix/new')
        self.assertTrue(any('PR #5 is merged — request dropped' in l for l in self.lines),
                        self.lines)

    def test_a_red_is_written_onto_the_request_as_the_file_holds_it_then(self):
        # #7 red at its head (a conflict, its author's) while #9 is added mid-pass
        self.push_lane('hotfix/seven', {'s.txt': 's\n'}, 'hotfix: s')
        merge_queue.add_request(self.state_dir, 7, 'hotfix/seven', product=self.product())
        self.gh.mergeable[7] = 'CONFLICTING'
        ln = self.lane()
        merge_queue.requested_ready(ln, self.heads(), self.heads()['main'])
        reqs = merge_queue.load_requests(self.state_dir)
        self.assertEqual(sorted(reqs), ['7', '9'])
        self.assertEqual(reqs['7']['red']['kind'], 'conflict')
        self.assertEqual(reqs['7']['red']['head'], self.heads()['hotfix/seven'])


class RebuildSurvivesAClear(Flagged):
    """A rebuild request the CI start queue writes while the pass clears another batch's."""

    def setUp(self):
        super().setUp()
        self.chain = [{'ref': f'batch/{x}', 'sha': x * 40, 'base': 'b' * 40, 'members': []}
                      for x in 'ac']
        merge_queue.save(self.state_dir, {'batches': self.chain}, self.product())
        merge_queue.request_rebuild(self.state_dir, 'batch/a', 'STUCK — a', self.product())

    def test_the_other_request_survives(self):
        real, fired = merge_queue.load_rebuilds, []

        def racing(state_dir):
            got = real(state_dir)
            if not fired:   # the clear has read the file: the start queue writes now
                fired.append(True)
                other_process(self.state_dir, 'rebuild', 'batch/c')
            return got
        with mock.patch.object(merge_queue, 'load_rebuilds', side_effect=racing):
            merge_queue._clear_rebuild(self.state_dir, 'batch/a', self.product())
        self.assertTrue(fired)
        got = merge_queue.load_rebuilds(self.state_dir)
        self.assertEqual(sorted(got), ['batch/c'])
        self.assertEqual(got['batch/c']['why'], 'STUCK — other process')


class CorruptIsNeverWiped(Flagged):
    def test_a_torn_requests_file_is_kept_and_said(self):
        with open(merge_queue.requests_path(self.state_dir), 'w') as f:
            f.write('{"requests": {"7": ')
        with mock.patch.object(store, '_warn'):
            with self.assertRaises(store.StoreCorrupt):
                merge_queue.add_request(self.state_dir, 9, 'hotfix/x', product=self.product())
            ln = self.lane()
            merge_queue._send_back(ln, {'requested': True, 'branch': 'hotfix/x', 'head': 'h',
                                        'pr': {'number': 9}}, 'gate', 'red', [])
        self.assertTrue(any('land-requests.json not written' in l for l in self.lines), self.lines)
        with open(merge_queue.requests_path(self.state_dir)) as f:
            self.assertEqual(f.read(), '{"requests": {"7": ')


class FlagOff(QueueRepo):
    """Off (the default) writes as before: the fixed temp name, no lock file."""

    def test_no_lock_file_and_the_same_bytes(self):
        merge_queue.add_request(self.state_dir, 9, 'hotfix/x', product=self.product())
        self.assertFalse(merge_queue.store_on(self.product()))
        self.assertFalse(os.path.exists(merge_queue.requests_path(self.state_dir) + '.lock'))
        with open(merge_queue.requests_path(self.state_dir)) as f:
            self.assertEqual(json.load(f)['requests']['9']['branch'], 'hotfix/x')


class PinnedReaderReadsTheStoreFiles(Flagged):
    """The oldest pinned venv (``tools/pinned-readers.txt``) reads what the store wrote."""

    def test_pinned_loaders_read_requests_chain_and_rebuilds(self):
        from tests import pinned
        p = self.product()
        chain = [{'ref': 'batch/a', 'sha': 'a' * 40, 'base': 'b' * 40,
                  'members': [{'branch': 'hotfix/x', 'head': 'c' * 40}]}]
        merge_queue.save(self.state_dir, {'batches': chain}, p)
        merge_queue.add_request(self.state_dir, 9, 'hotfix/x', by='me', priority=True, product=p)
        merge_queue.add_request(self.state_dir, 11, 'hotfix/y', product=p)
        merge_queue.drop_request(self.state_dir, 11, p)
        self.assertTrue(merge_queue.request_rebuild(self.state_dir, 'batch/a', 'STUCK', p))
        self.assertTrue(os.path.exists(merge_queue.requests_path(self.state_dir) + '.lock'))
        code = (f'import json; from asf import merge_queue as m; sd = {self.state_dir!r}; '
                'print(json.dumps({"reqs": m.load_requests(sd), "chain": m.load(sd), '
                '"rebuilds": m.load_rebuilds(sd)}))')
        for sha in pinned.pinned_shas():
            with self.subTest(sha=sha):
                proc = pinned.run_pinned(sha, code)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                out = json.loads(proc.stdout.strip().splitlines()[-1])
                self.assertEqual(out['reqs'], merge_queue.load_requests(self.state_dir))
                self.assertEqual(sorted(out['reqs']), ['9'])
                self.assertTrue(out['reqs']['9']['priority'])
                self.assertEqual(out['chain'], {'batches': chain})
                self.assertEqual(out['rebuilds']['batch/a']['why'], 'STUCK')


if __name__ == '__main__':
    unittest.main()
