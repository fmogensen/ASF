"""asf.facts — the facts core (W6-PR1): every fact carries ``as_of`` and may be Unknown; the
per-tick cache is keyed by head; ``shadow()`` runs old and new, logs a disagreement and returns
old — whatever ``new_fn`` raises; ``conventions.flags.facts`` picks old | shadow | new."""
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

from asf import env, facts, gh_limit
from asf.facts import cache, disagree
from asf.facts.types import AsOf, Alive, Dead, Landed, NotLanded, OpenPrs, Unknown, is_unknown

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.test_facts` does not
    import contracts
except ImportError:  # pragma: no cover - import shape only
    from tests import contracts

P = 'alpha'


class Prod:
    """The part of a product the facts core reads: a name, a slug and the flags."""

    def __init__(self, flags=None, name=P, slug='example/repo'):
        from asf.conventions import Conventions
        self.name, self.repo_slug = name, slug
        self.conventions = Conventions()
        self.conventions.extra['flags'] = dict(flags or {})

    def flag(self, name, default=None):
        return self.conventions.flag(name, default)


class Home(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='asf-facts-')
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        cache.clear()
        gh_limit.reset()
        self.err = io.StringIO()
        quiet = redirect_stderr(self.err)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def tearDown(self):
        env.ASF_HOME = self._home
        cache.clear()
        gh_limit.reset()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def records(self):
        return disagree.records(P)


class Types(unittest.TestCase):
    def test_every_fact_carries_as_of(self):
        at = AsOf('abc', '2026-10-04T00:00:00Z')
        for fact in (Unknown('rc 1', at), Landed('abc', 'pr-merge', at), NotLanded('open', at),
                     Alive(12, at), Dead('2026-10-04T00:00:00Z', at), OpenPrs((), at)):
            self.assertIs(fact.as_of, at)

    def test_unknown_is_never_a_falsy_none(self):
        u = Unknown('timeout', AsOf.now())
        self.assertTrue(is_unknown(u))
        self.assertFalse(is_unknown(NotLanded('no', AsOf.now())))
        self.assertTrue(AsOf.now().at.endswith('Z'))

    def test_open_prs_by_head(self):
        prs = OpenPrs(({'number': 1, 'headRefName': 'worker/T-0001'},), AsOf.now())
        self.assertEqual(prs.by_head('worker/T-0001')['number'], 1)
        self.assertIsNone(prs.by_head('worker/T-0002'))


class Modes(unittest.TestCase):
    def test_default_is_old(self):
        self.assertEqual(facts.mode(Prod()), 'old')
        self.assertEqual(facts.mode(None), 'old')

    def test_each_mode_read(self):
        for m in ('old', 'shadow', 'new'):
            self.assertEqual(facts.mode(Prod({'facts': m})), m)
        self.assertEqual(facts.mode(Prod({'facts': ' Shadow '})), 'shadow')

    def test_an_unknown_value_is_old(self):
        self.assertEqual(facts.mode(Prod({'facts': 'on'})), 'old')
        self.assertEqual(facts.mode(Prod({'facts': True})), 'old')

    def test_facts_is_a_known_flag(self):
        from asf import conventions
        self.assertIn('facts', conventions.KNOWN_FLAGS)


class Shadow(Home):
    def run_shadow(self, flags, old, new, **kw):
        calls = []

        def old_fn():
            calls.append('old')
            return old

        def new_fn():
            calls.append('new')
            if isinstance(new, BaseException):
                raise new
            return new

        got = facts.shadow(Prod(flags), 'landed', 'T-0001', old_fn, new_fn, **kw)
        return got, calls

    def test_old_runs_old_only(self):
        got, calls = self.run_shadow({}, True, False)
        self.assertEqual((got, calls), (True, ['old']))
        self.assertEqual(self.records(), [])

    def test_new_runs_new_only(self):
        got, calls = self.run_shadow({'facts': 'new'}, True, False)
        self.assertEqual((got, calls), (False, ['new']))
        self.assertEqual(self.records(), [])

    def test_shadow_returns_old_and_logs_a_disagreement(self):
        got, calls = self.run_shadow({'facts': 'shadow'}, True, False, decider='trunkclose')
        self.assertEqual((got, calls), (True, ['old', 'new']))
        [rec] = self.records()
        self.assertEqual((rec['fact'], rec['decider'], rec['key'], rec['old'], rec['new']),
                         ('landed', 'trunkclose', 'T-0001', True, False))
        self.assertTrue(rec['ts'].endswith('Z'))

    def test_shadow_agreeing_logs_nothing(self):
        got, _ = self.run_shadow({'facts': 'shadow'}, True, True)
        self.assertTrue(got)
        self.assertEqual(self.records(), [])

    def test_same_maps_a_fact_onto_the_old_answer(self):
        at = AsOf('abc', '2026-10-04T00:00:00Z')
        same = lambda old, new: old == isinstance(new, Landed)  # noqa: E731
        got, _ = self.run_shadow({'facts': 'shadow'}, True, Landed('abc', 'names', at), same=same)
        self.assertTrue(got)
        self.assertEqual(self.records(), [])
        self.run_shadow({'facts': 'shadow'}, True, NotLanded('open work', at), same=same)
        [rec] = self.records()
        self.assertEqual(rec['new'], {'fact': 'NotLanded', 'why': 'open work', 'hint_sha': '',
                                      'as_of': {'head': 'abc', 'at': '2026-10-04T00:00:00Z'}})

    def test_new_raising_never_reaches_the_old_decider(self):
        got, _ = self.run_shadow({'facts': 'shadow'}, True, KeyError('x'))
        self.assertTrue(got)
        [rec] = self.records()
        self.assertEqual(rec['new'], 'error:KeyError')

    def test_a_rate_limit_inside_new_is_logged_not_raised(self):
        got, _ = self.run_shadow({'facts': 'shadow'}, True, gh_limit.RateLimited('limit'))
        self.assertTrue(got)
        self.assertEqual(self.records()[0]['new'], 'error:RateLimited')

    def test_new_raising_in_new_mode_raises(self):
        with self.assertRaises(KeyError):
            self.run_shadow({'facts': 'new'}, True, KeyError('x'))

    def test_old_raising_raises_in_shadow(self):
        def boom():
            raise ValueError('old')
        with self.assertRaises(ValueError):
            facts.shadow(Prod({'facts': 'shadow'}), 'landed', 'k', boom, lambda: True)

    def test_an_unwritable_log_never_raises(self):
        with mock.patch.object(disagree.store, 'append', side_effect=OSError('disk full')):
            got, _ = self.run_shadow({'facts': 'shadow'}, True, False)
        self.assertTrue(got)
        self.assertIn('FACTS', self.err.getvalue())


class Log(Home):
    def test_log_shape_and_count(self):
        prod = Prod()
        disagree.log(prod, 'landed', 'T-1', True, False, decider='trunkclose')
        disagree.log(prod, 'landed', 'T-2', True, Unknown('rc 1', AsOf('', 'x')),
                     decider='trunkclose')
        disagree.log(prod, 'landed', 'T-3', False, True, decider='relaunch')
        path = os.path.join(self.tmp, 'state', P, 'facts-disagree.jsonl')
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(disagree.count(prod),
                         {('landed', 'trunkclose'): 2, ('landed', 'relaunch'): 1})
        self.assertEqual(self.records()[1]['new'],
                         {'fact': 'Unknown', 'reason': 'rc 1', 'as_of': {'head': '', 'at': 'x'}})

    def test_count_since(self):
        prod = Prod()
        with mock.patch.object(disagree, '_now', return_value='2026-10-01T00:00:00Z'):
            disagree.log(prod, 'landed', 'T-1', True, False)
        with mock.patch.object(disagree, '_now', return_value='2026-10-03T00:00:00Z'):
            disagree.log(prod, 'landed', 'T-2', True, False)
        self.assertEqual(disagree.count(prod, since='2026-10-02T00:00:00Z'), {('landed', ''): 1})
        self.assertEqual(disagree.count(prod), {('landed', ''): 2})

    def test_status_cell_only_when_there_is_one(self):
        prod = Prod()
        self.assertIsNone(disagree.status_cell(prod))
        disagree.log(prod, 'landed', 'T-1', True, False, decider='trunkclose')
        cell = disagree.status_cell(prod)
        self.assertIn('1 disagreement', cell)
        self.assertIn('landed/trunkclose 1', cell)

    def test_status_row(self):
        from asf.views import status
        prod = Prod()
        disagree.log(prod, 'landed', 'T-1', True, False, decider='trunkclose')
        self.assertIn('landed/trunkclose', status.facts_cell(prod))

    def test_scorecard_window_counts_the_log(self):
        from asf.scorecard import facts as sfacts, score
        prod = Prod()
        with mock.patch.object(disagree, '_now', return_value='2026-10-03T00:00:00Z'):
            disagree.log(prod, 'landed', 'T-1', True, False)
        f = sfacts.Facts(items={}, sessions=[], ci=[], gates=[], runs=[], clutter={},
                         as_of='2026-10-04T00:00:00Z', disagree=disagree.records(prod))
        self.assertEqual(score.headline(f)['facts_disagree'], 1)
        f.disagree = []
        self.assertEqual(score.headline(f)['facts_disagree'], 0)

    def test_facts_carries_landings_a_record_with_no_landings_file_reads_empty_no_diagnostic(self):
        # S-77507: Facts.landings is filled from the `landings` stream; a record with no
        # `landings/` day file at all reads as [] and raises no diagnostic.
        from asf.scorecard import facts as sfacts
        tmp = tempfile.mkdtemp(prefix='asf-facts-landings-')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        f = sfacts.load(tmp, None, as_of='2026-10-04T00:00:00Z')
        self.assertEqual(f.landings, [])
        self.assertEqual(f.diagnostics, [])


class Cache(Home):
    def test_keyed_by_head(self):
        cache.put(P, 'verdict', 'pr-1', 'aaa', 'approved')
        self.assertEqual(cache.get(P, 'verdict', 'pr-1', 'aaa'), 'approved')
        self.assertIs(cache.get(P, 'verdict', 'pr-1', 'bbb'), cache.MISS)
        self.assertIs(cache.get('beta', 'verdict', 'pr-1', 'aaa'), cache.MISS)

    def test_clear(self):
        cache.put(P, 'verdict', 'pr-1', 'aaa', 'approved')
        cache.clear()
        self.assertIs(cache.get(P, 'verdict', 'pr-1', 'aaa'), cache.MISS)

    def gh(self, rc=0, out='[]', err=''):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, rc, out, err)
        return run, calls

    def test_open_prs_one_read_a_tick(self):
        run, calls = self.gh(out='[{"number": 7, "headRefName": "worker/T-0001"}]')
        prod = Prod()
        got = cache.prime(prod, run=run)
        again = cache.prime(prod, run=run)
        self.assertEqual(len(calls), 1)
        self.assertIs(got, again)
        self.assertEqual(cache.open_prs(prod).by_head('worker/T-0001')['number'], 7)
        self.assertTrue(got.as_of.at)

    def test_open_prs_miss_is_unknown_never_a_call(self):
        run, calls = self.gh()
        with mock.patch('subprocess.run', run):
            got = cache.open_prs(Prod())
        self.assertTrue(is_unknown(got))
        self.assertEqual(calls, [])

    def test_a_refused_read_is_unknown_and_not_retried(self):
        run, calls = self.gh(rc=1, out='', err='HTTP 502')
        prod = Prod()
        got = cache.prime(prod, run=run)
        self.assertTrue(is_unknown(got))
        self.assertIn('502', got.reason)
        self.assertIs(cache.prime(prod, run=run), got)
        self.assertEqual(len(calls), 1)

    def test_a_rate_limit_is_unknown(self):
        fx = contracts.FixtureGh('rate-limit')
        rc, _out, err = fx.answers[next(iter(fx.answers))]
        run, _calls = self.gh(rc=rc, out='', err=err)
        got = cache.prime(Prod(), run=run)
        self.assertTrue(is_unknown(got))
        self.assertEqual(got.reason, 'rate limited')
        self.assertTrue(is_unknown(cache.prime(Prod(name='beta'), run=run)))  # latched: no spend

    def test_no_slug_is_unknown(self):
        self.assertTrue(is_unknown(cache.prime(Prod(slug=None))))

    def test_a_tick_clears_the_cache(self):
        run, calls = self.gh()
        prod = Prod()
        cache.prime(prod, run=run)
        from asf.tick import tick
        tick.Context(prod)
        self.assertTrue(is_unknown(cache.open_prs(prod)))


if __name__ == '__main__':
    unittest.main()
