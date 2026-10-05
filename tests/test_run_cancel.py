"""asf.run_cancel — a queued run is force-cancelled (a plain cancel there does nothing), and every
cancel is read back until the run reads completed."""
import unittest

from asf import run_cancel

SLUG = 'o/r'


class Host:
    """A run on a fake host: a plain cancel ends it only once it is in progress; a force-cancel
    ends it whatever its status."""

    def __init__(self, status, plain_works=None, readable=True):
        self.status, self.readable, self.calls = status, readable, []
        self.plain_works = (status == 'in_progress') if plain_works is None else plain_works

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ['run', 'cancel']:
            if self.plain_works:
                self.status = 'completed'
            return True, ''
        if args[:3] == ['api', '-X', 'POST'] and args[3].endswith('/force-cancel'):
            self.status = 'completed'
            return True, ''
        if args[0] == 'api' and args[-2:] == ['--jq', '.status']:
            return (True, self.status + '\n') if self.readable else (False, '')
        return False, ''

    def verbs(self):
        return [('force' if c[:2] == ['api', '-X'] else 'plain' if c[:2] == ['run', 'cancel']
                 else 'read') for c in self.calls]


class Cancel(unittest.TestCase):
    def setUp(self):
        self.sleeps = []

    def cancel(self, host, status=None):
        return run_cancel.cancel(host, SLUG, 7, status=status, sleep=self.sleeps.append)

    def test_a_queued_run_is_force_cancelled_never_plain_cancelled(self):
        host = Host('queued')
        c = self.cancel(host, status='queued')
        self.assertTrue(c and c.forced and c.confirmed)
        self.assertEqual(host.verbs(), ['force', 'read'])
        self.assertIn(['api', '-X', 'POST', 'repos/o/r/actions/runs/7/force-cancel'], host.calls)
        self.assertNotIn(['run', 'cancel', '7', '-R', SLUG], host.calls)

    def test_a_run_in_progress_gets_the_plain_cancel_and_reads_completed(self):
        host = Host('in_progress')
        c = self.cancel(host, status='in_progress')
        self.assertEqual(host.verbs(), ['plain', 'read'])
        self.assertTrue(c.confirmed and not c.forced)

    def test_a_plain_cancel_the_run_still_reads_queued_after_is_forced(self):
        host = Host('queued', plain_works=False)       # the listing said in progress: stale
        c = self.cancel(host, status='in_progress')
        self.assertEqual(host.verbs(), ['plain', 'read', 'force', 'read'])
        self.assertTrue(c.forced and c.confirmed and c.status == 'completed')

    def test_the_status_is_read_when_the_caller_did_not_list_it(self):
        host = Host('queued')
        self.assertTrue(self.cancel(host).forced)
        self.assertEqual(host.verbs(), ['read', 'force', 'read'])

    def test_a_run_that_never_reads_completed_is_reported_not_confirmed(self):
        host = Host('queued')
        host.__class__ = type('Stuck', (Host,), {'__call__': lambda s, a: (
            s.calls.append(list(a)) or ((True, 'queued') if a[0] == 'api' and '--jq' in a
                                        else (True, '')))})
        c = self.cancel(host, status='queued')
        self.assertTrue(c.ok)
        self.assertFalse(c.confirmed)
        self.assertIn('still reads queued', run_cancel.unconfirmed(c))
        self.assertEqual(len(self.sleeps), run_cancel.CONFIRM_READS - 1)

    def test_an_unreadable_status_is_not_waited_on(self):
        host = Host('queued', readable=False)
        c = self.cancel(host, status='queued')
        self.assertTrue(c.ok)
        self.assertEqual((self.sleeps, run_cancel.unconfirmed(c)), ([], ''))

    def test_a_refused_cancel_is_falsy(self):
        c = run_cancel.cancel(lambda a: (False, ''), SLUG, 7, status='queued',
                              sleep=self.sleeps.append)
        self.assertFalse(c)
        self.assertIn('force-cancel refused', c.detail)

    def test_an_already_completed_run_is_left(self):
        host = Host('completed')
        self.assertTrue(self.cancel(host, status='completed'))
        self.assertEqual(host.calls, [])


if __name__ == '__main__':
    unittest.main()
