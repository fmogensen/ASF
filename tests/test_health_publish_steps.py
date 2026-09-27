"""The health pass makes the publishes of independent runs at once (asf.workers.health.run_steps).

A factory publish is a push the product's pre-push hook gates; one after another they were most
of a tick's health step. Runs on different branches, worktrees and items publish side by side;
runs sharing any of them go strictly in the ledger's order, each seeing the one before it."""
import threading
import unittest

from asf.workers import health


def _pass(job, log):
    """A run's pass: note its evidence read, yield one publish, note the resume."""
    log.append(('gather', job))
    result = yield (job,)
    log.append(('resumed', job, result))


class RunStepsTest(unittest.TestCase):
    def test_independent_runs_publish_at_once(self):
        barrier = threading.Barrier(2, timeout=5)
        log = []

        def publish(job):
            barrier.wait()  # breaks (BrokenBarrierError) unless both publishes run together
            return f'pushed {job}'

        entries = [('a', {'branch': 'cloud/a', 'item': 'T-1', 'worktree': '/w/a'}, None),
                   ('b', {'branch': 'cloud/b', 'item': 'T-2', 'worktree': '/w/b'}, None)]
        entries = [(j, r, _pass(j, log)) for j, r, _ in entries]
        health.run_steps(entries, publish, workers=2)
        self.assertIn(('resumed', 'a', 'pushed a'), log)
        self.assertIn(('resumed', 'b', 'pushed b'), log)

    def test_runs_on_one_branch_go_one_after_another(self):
        log = []

        def publish(job):
            log.append(('publish', job))
            return job

        entries = [(j, {'branch': 'cloud/x', 'worktree': f'/w/{j}', 'item': f'T-{j}'},
                    _pass(j, log)) for j in ('a', 'b')]
        health.run_steps(entries, publish, workers=3)
        # the second run's evidence is read only after the first one's publish and resume
        self.assertEqual(log, [('gather', 'a'), ('publish', 'a'), ('resumed', 'a', 'a'),
                               ('gather', 'b'), ('publish', 'b'), ('resumed', 'b', 'b')])

    def test_runs_of_one_item_or_one_worktree_go_one_after_another(self):
        for shared in ({'item': 'T-9'}, {'worktree': '/w/same'}):
            log = []
            runs = [dict({'branch': f'cloud/{j}', 'item': f'T-{j}', 'worktree': f'/w/{j}'},
                         **shared) for j in ('a', 'b')]
            entries = [(j, r, _pass(j, log)) for j, r in zip(('a', 'b'), runs)]
            health.run_steps(entries, lambda job: job, workers=3)
            self.assertEqual([e[:2] for e in log],
                             [('gather', 'a'), ('resumed', 'a'), ('gather', 'b'), ('resumed', 'b')],
                             shared)

    def test_a_run_with_no_publish_and_a_run_with_two(self):
        log = []

        def none(job):
            log.append(('done', job))
            return
            yield  # noqa: unreachable — a generator with nothing to publish

        def two(job):
            first = yield (job + '1',)
            second = yield (job + '2',)
            log.append(('two', first, second))

        entries = [('n', {'branch': 'n'}, none('n')), ('t', {'branch': 't'}, two('t'))]
        health.run_steps(entries, lambda x: x.upper(), workers=2)
        self.assertEqual(log, [('done', 'n'), ('two', 'T1', 'T2')])

    def test_a_publish_that_raises_is_raised(self):
        def boom(job):
            raise OSError('push hung')

        entries = [(j, {'branch': j}, _pass(j, [])) for j in ('a', 'b')]
        with self.assertRaises(OSError):
            health.run_steps(entries, boom, workers=2)


if __name__ == '__main__':
    unittest.main()
