"""A session's ``NEEDS OPERATOR:`` question of a recurring, fact-checkable class is answered by the
kernel from a probe of the trunk — never by the console's judgement ("code driven, not LLM
guesswork"): whether named tests are red on the factory host's trunk or only in a cloud sandbox
(``trunk-tests``: the ids run on a fresh detached worktree of origin's trunk), and whether a
symbol a plan names exists at trunk (``symbol``). The fixtures are the questions seen live on
2026-10-10 (F-0279, F-0313, F-0314), word for word. Anything unmatched, unprobed or unsure stays an
operator Stuck, and each tick counts what code resolved against what went to the console."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from asf.kernel import actions as A
from asf.kernel import decide as D
from asf.kernel import facts as K
from asf.kernel import loop as L
from asf.kernel import resolvers as R
from asf.kernel import settings
from asf.kernel import trunk as T

try:
    from kernel import builders as B
    from kernel import fakes as F
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel import fakes as F

State = B.State

METHODS = ('test_a_review_on_a_branch_behind_main_pushes_nothing',
           'test_b0046_correct_row_spawns_on_the_held_branch_not_rebased_when_it_merges_clean',
           'test_b0048_adjudicate_row_spawns_on_the_held_branch',
           'test_b0051_ended_sessions_worktree_is_reused_not_refused',
           'test_trunk_rebase_needed_names_only_what_a_rebase_clears')

#: plan-f-0279-1791614944 (report 7c65a6b1e)
Q_0279 = ("whether the five tests.test_workers.TestSpawn failures recorded in the plan's PD5 (%s) "
          "are red on the factory host too or only in a cloud sandbox — this session cannot tell "
          "the two apart, and Task 2's Gate treats them as the trunk's — `python3 -m unittest -q "
          "tests.test_workers.TestSpawn`" % ', '.join(METHODS))
#: the same, as the kernel recorded it on the card (cut at 200 characters)
R_0279 = ("done: NEEDS OPERATOR: whether the five tests.test_workers.TestSpawn failures recorded in "
          "the plan's PD5 (test_a_review_on_a_branch_behind_main_pushes_nothing, "
          "test_b0046_correct_row_spawns_on_the_hel…")
#: plan-f-0313-1791619475 (report 45ae5b513)
Q_0313_HOST = ("whether tests.test_workers.TestSpawn's five failures are red on the factory host\n"
               " too or only in a cloud container. This is the spec's own first NEEDS OPERATOR, "
               "still unanswered,\n and this session sharpened it: all five are red here on a "
               "tree whose asf/ and tests/ are\n byte-identical to the trunk, so they are the "
               "product's or the container's, never a session's -\n and every cloud session's "
               "pre-push gate runs that module, so each one must either know to skip\n these five "
               "by name or will read them as its own. It blocks no Task of this plan -\n "
               "`python3 -m unittest tests.test_workers.TestSpawn 2>&1 | tail -3; git --version`")
#: plan-f-0313-1791614981 (report ecbd8286e)
Q_0313_SYM = ("whether `tests.test_harvest.HarvestLane` was meant to be an existing class the "
              "approved spec's S-84459 fence could name — it does not exist in "
              "tests/test_harvest.py, so the plan has Task 4 create it (PD3); if instead the fence "
              "should have named ProductHarvestTests, the spec's fence needs amending and the plan "
              "with it — `python3 -m unittest tests.test_harvest.HarvestLane; grep -n '^class ' "
              "tests/test_harvest.py`")
R_0313_HOST = ("done: NEEDS OPERATOR: whether tests.test_workers.TestSpawn's five failures are red "
               "on the factory host too or only in a cloud container. This is the spec's own "
               "first NEEDS OPERATOR, still unans…")
R_0313_SYM = ("done: NEEDS OPERATOR: whether `tests.test_harvest.HarvestLane` was meant to be an "
              "existing class the approved spec's S-84459 fence could name — it does not exist in "
              "tests/test_harvest.py, so the plan…")
#: plan-f-0314-1791615020 (report 3a3b80794)
Q_0314 = ("whether tests.test_workers' 5 pre-existing TestSpawn failures on git 2.43.0 (cloud "
          "containers) deserve a Bug card — they are red before any change, were already recorded "
          "in docs/plans/f-0289.md PD3, are owned by no card, and will stop every coder whose Gate "
          "names that module; this plan's Gates drop it (PD8) rather than fix it, since no Task "
          "here touches asf/workers/ — reproduce read-only with `cd /work/ASF && git checkout "
          "origin/main -- . && python3 -m unittest tests.test_workers 2>&1 | grep -E "
          "'^(FAIL|ERROR):|^Ran |^FAILED'`")
#: plan-f-0314-1791617923 (report 4de61cd1c): an order to mint, not a fact to check
Q_0314_MINT = ("mint the Bug card the operator already approved for the five pre-existing "
               "tests.test_workers TestSpawn failures (git 2.43.0, cloud containers only; green on "
               "the factory host). This session cannot: it is a record write and no record is "
               "mounted in this container. The five cases are %s - `asf new bug --title \"x\"`"
               % ', '.join(METHODS))

SHA = '55c6b71a0e0f1a2b3c4d5e6f708192a3b4c5d6e7'
SPAWN = 'tests.test_workers.TestSpawn'


def tests_probe(*ids):
    return R.Probe(R.TRUNK_TESTS, tuple(ids))


GREEN = {'sha': SHA, 'ran': 5, 'failed': [], 'ok': True}
RED = {'sha': SHA, 'ran': 5, 'failed': [SPAWN + '.' + METHODS[0]], 'ok': False}
ABSENT = {'sha': SHA, 'exists': False, 'path': 'tests/test_harvest.py',
          'defined': ['ProductHarvestTests', 'HarvestRecord']}


class Match(unittest.TestCase):

    def test_the_factory_host_or_cloud_questions_run_the_named_tests(self):
        self.assertEqual(R.match(Q_0279), tests_probe(*('%s.%s' % (SPAWN, m) for m in METHODS)))
        self.assertEqual(R.match(Q_0313_HOST), tests_probe(SPAWN))
        self.assertEqual(R.match(Q_0314), tests_probe(SPAWN), 'the bare class narrows the module')

    def test_a_recorded_reason_cut_before_the_sandbox_stays_with_the_console(self):
        self.assertIsNone(R.match(R_0279), 'it no longer says host or cloud: not narrow enough')

    def test_a_recorded_reason_cut_mid_name_runs_the_class(self):
        cut = R_0279.replace('the plan\'s PD5', 'a cloud sandbox')
        self.assertEqual(R.match(cut), tests_probe(SPAWN))
        self.assertEqual(R.match(R_0313_HOST), tests_probe(SPAWN))

    def test_the_missing_symbol_question_checks_the_symbol(self):
        want = R.Probe(R.SYMBOL, ('tests.test_harvest.HarvestLane',))
        self.assertEqual(R.match(Q_0313_SYM), want)
        self.assertEqual(R.match(R_0313_SYM), want)

    def test_other_questions_stay_with_the_console(self):
        self.assertEqual(R.match(Q_0314_MINT).cls, R.INBOX_BUG, 'its own class, not trunk-tests')
        for q in ('json or yaml?', 'whether tests.test_x fails?',
                  'whether the cloud is red?', 'whether `a.b` and `c.d` exist — `a.b` does not exist',
                  'whether the id claim covers S-1; or re-mint', ''):
            self.assertIsNone(R.match(q), q)


class Answer(unittest.TestCase):

    def test_green_on_trunk_is_cloud_only(self):
        text = R.answer(tests_probe(SPAWN), GREEN)
        self.assertIn('green on trunk at 55c6b71 (5 tests OK): cloud-sandbox only, not a trunk '
                      'red; proceed', text)

    def test_red_on_trunk_names_the_failures(self):
        text = R.answer(tests_probe(SPAWN), RED)
        self.assertIn('red on trunk at 55c6b71: %s.%s; treat as a trunk red' % (SPAWN, METHODS[0]),
                      text)

    def test_an_absent_symbol_lets_the_plan_proposal_stand(self):
        text = R.answer(R.Probe(R.SYMBOL, ('tests.test_harvest.HarvestLane',)), ABSENT)
        self.assertIn('`tests.test_harvest.HarvestLane` does not exist at trunk 55c6b71', text)
        self.assertIn('ProductHarvestTests', text)
        self.assertIn("the plan's own proposal stands", text)

    def test_a_present_symbol_says_where(self):
        text = R.answer(R.Probe(R.SYMBOL, ('tests.test_harvest.HarvestLane',)),
                        dict(ABSENT, exists=True, where='tests/test_harvest.py:40'))
        self.assertIn('exists at trunk 55c6b71 (tests/test_harvest.py:40)', text)

    def test_an_unsure_probe_is_no_answer(self):
        for result in (None, {}, dict(GREEN, error='timeout after 600s'), dict(GREEN, ran=0),
                       dict(RED, failed=[])):
            self.assertIsNone(R.answer(tests_probe(SPAWN), result), result)
        for result in (None, {}, dict(ABSENT, error='no module at trunk')):
            self.assertIsNone(R.answer(R.Probe(R.SYMBOL, ('a.b',)), result), result)


def feature(iid='F-0279'):
    return B.task(iid, state=State.BUILDING, type='feature')


def asked(iid='F-0279', q=Q_0279):
    return B.session('plan-%s-1' % iid.lower(), iid, kind='plan', alive=False, ended=True,
                     result='question', status='done', question=q, fields={'status': 'done'})


class Decide(unittest.TestCase):

    def plan(self, q=Q_0279, resolved=None, iid='F-0279', **cfg):
        probe = R.match(q)
        res = {probe.key: GREEN} if resolved is None and probe else (resolved or {})
        f = B.facts([feature(iid)], sessions=[asked(iid, q)], resolved=res)
        return D.decide(f, B.config(**cfg))

    def test_a_green_trunk_answers_the_ended_session(self):
        plan = self.plan()
        answers = B.of(plan, A.ApplyAnswer)
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0].by, R.TRUNK_TESTS)
        self.assertIn('cloud-sandbox only', answers[0].text)
        self.assertEqual(B.of(plan, A.MarkStuck), [])
        self.assertNotEqual(B.state(plan, 'F-0279'), State.STUCK)

    def test_the_missing_symbol_is_answered(self):
        plan = self.plan(q=Q_0313_SYM, iid='F-0313',
                         resolved={R.match(Q_0313_SYM).key: ABSENT})
        self.assertEqual([a.by for a in B.of(plan, A.ApplyAnswer)], [R.SYMBOL])

    def test_no_probe_result_leaves_the_operator_stuck(self):
        plan = self.plan(resolved={})
        self.assertEqual(B.of(plan, A.ApplyAnswer), [])
        self.assertEqual(B.stuck(plan, 'F-0279').owner, 'operator')

    def test_the_knob_off_leaves_the_operator_stuck(self):
        plan = self.plan(resolve_trunk_tests=False)
        self.assertEqual(B.of(plan, A.ApplyAnswer), [])
        self.assertEqual(B.stuck(plan, 'F-0279').owner, 'operator')
        plan = self.plan(q=Q_0313_SYM, iid='F-0313', resolve_symbols=False,
                         resolved={R.match(Q_0313_SYM).key: ABSENT})
        self.assertEqual(B.of(plan, A.ApplyAnswer), [])

    def test_a_recorded_operator_stuck_is_answered(self):
        it = B.task('F-0279', type='feature', state=State.STUCK,
                    stuck=B.M.Stuck(R_0313_HOST, 'operator'))
        plan = D.decide(B.facts([it], resolved={R.match(R_0313_HOST).key: RED}), B.config())
        answers = B.of(plan, A.ApplyAnswer)
        self.assertEqual([a.by for a in answers], [R.TRUNK_TESTS])
        self.assertIn('treat as a trunk red', answers[0].text)
        self.assertNotEqual(B.state(plan, 'F-0279'), State.STUCK)

    def test_an_operator_answer_is_not_tagged(self):
        f = B.facts([B.task('F-0279', type='feature', state=State.STUCK, question='json?',
                            stuck=B.M.Stuck('NEEDS OPERATOR: json?', 'operator'))],
                    answers=[B.M.Answer('F-0279', 'yaml')])
        self.assertEqual([a.by for a in B.of(D.decide(f, B.config()), A.ApplyAnswer)], [''])


class Facts(unittest.TestCase):

    def test_the_reader_probes_only_matched_questions(self):
        asked_for = []

        class Trunk:
            def probe(self, probes):
                asked_for.append(list(probes))
                return {p.key: GREEN for p in probes}

        ports = F.ports(F.FakeRecord([feature()]), sessions=F.FakeSessions([asked()]))
        ports.trunk = Trunk()
        f = K.read_facts(ports)
        self.assertEqual(asked_for, [[R.match(Q_0279)]])
        self.assertEqual(f.resolved, {R.match(Q_0279).key: GREEN})
        asked_for.clear()
        ports = F.ports(F.FakeRecord([feature()]), sessions=F.FakeSessions([asked(q='json?')]))
        ports.trunk = Trunk()
        self.assertEqual(K.read_facts(ports).resolved, {})
        self.assertEqual(asked_for, [], 'no matched question, no probe')

    def test_ports_without_a_trunk_probe_read_nothing(self):
        f = K.read_facts(F.ports(F.FakeRecord([feature()]), sessions=F.FakeSessions([asked()])))
        self.assertEqual(f.resolved, {})


class Tally(unittest.TestCase):

    def test_resolved_is_logged_and_counted_against_the_console(self):
        a = A.ApplyAnswer('F-0279', R.answer(tests_probe(SPAWN), GREEN), by=R.TRUNK_TESTS)
        self.assertTrue(A.describe(a).startswith('RESOLVED F-0279 trunk-tests -> green on trunk'))
        self.assertEqual(A.describe(A.ApplyAnswer('F-1', 'yaml')), 'answer F-1')
        it = B.task('F-0313', type='feature', state=State.STUCK,
                    stuck=B.M.Stuck('NEEDS OPERATOR: json or yaml?', 'operator'))
        f = B.facts([feature(), it], sessions=[asked()],
                    resolved={R.match(Q_0279).key: GREEN})
        plan = D.decide(f, B.config())
        s = L.summarize(plan, f)
        self.assertEqual((s['resolved'], s['escalated']), (1, 1))
        lines = []
        L.print_summary(s, lines.append)
        self.assertIn('questions: resolved by code 1, to the console 1 (50% by code)', lines)


def _git(cwd, *args):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True)


class Probe(unittest.TestCase):
    """The host side on a real git repo: a bare origin with a trunk the probe fetches."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        origin = os.path.join(self.tmp, 'origin.git')
        self.repo = os.path.join(self.tmp, 'repo')
        _git(self.tmp, 'init', '-q', '--bare', '-b', 'main', origin)
        _git(self.tmp, 'clone', '-q', origin, self.repo)
        for k, v in (('user.email', 't@example.com'), ('user.name', 't')):
            _git(self.repo, 'config', k, v)
        os.makedirs(os.path.join(self.repo, 'tests'))
        with open(os.path.join(self.repo, 'tests', '__init__.py'), 'w') as f:
            f.write('')
        with open(os.path.join(self.repo, 'tests', 'test_x.py'), 'w') as f:
            f.write('import unittest\n\n\nclass Green(unittest.TestCase):\n'
                    '    def test_a(self):\n        pass\n\n    def test_b(self):\n        pass\n\n\n'
                    'class Red(unittest.TestCase):\n    def test_c(self):\n'
                    '        self.fail("no")\n')
        _git(self.repo, 'add', '-A')
        _git(self.repo, 'commit', '-q', '-m', 'trunk')
        _git(self.repo, 'push', '-q', 'origin', 'HEAD:main')
        self.state = os.path.join(self.tmp, 'state')
        self.probe = T.TrunkProbe(self.repo, 'main', self.state, python=sys.executable,
                                  timeout_s=60)

    def worktrees(self):
        out = subprocess.run(['git', 'worktree', 'list', '--porcelain'], cwd=self.repo,
                             capture_output=True, text=True).stdout
        return [ln for ln in out.splitlines() if ln.startswith('worktree ')]

    def test_green_and_red_trunk_runs_and_a_clean_floor(self):
        green, red = tests_probe('tests.test_x.Green'), tests_probe('tests.test_x.Red.test_c')
        out = self.probe.probe([green])
        self.assertEqual(out[green.key]['ran'], 2)
        self.assertTrue(out[green.key]['ok'])
        self.assertEqual(len(out[green.key]['sha']), 40)
        out = self.probe.probe([red])
        self.assertEqual(out[red.key]['failed'], ['tests.test_x.Red.test_c'])
        self.assertEqual(len(self.worktrees()), 1, 'the probe worktree is removed')
        self.assertEqual(os.listdir(os.path.join(self.state, T.TMP_DIR)), [])

    def test_a_missing_test_id_is_no_answer(self):
        p = tests_probe('tests.test_x.Nope')
        self.assertIsNone(R.answer(p, self.probe.probe([p])[p.key]))

    def test_one_test_run_per_tick_and_results_are_cached(self):
        a, b = tests_probe('tests.test_x.Green.test_a'), tests_probe('tests.test_x.Green.test_b')
        self.assertEqual(list(self.probe.probe([a, b])), [a.key])
        self.assertEqual(sorted(self.probe.probe([a, b])), sorted([a.key, b.key]))

    def test_symbols_at_trunk(self):
        there, gone = (R.Probe(R.SYMBOL, ('tests.test_x.Red.test_c',)),
                       R.Probe(R.SYMBOL, ('tests.test_x.HarvestLane',)))
        out = self.probe.probe([there, gone])
        self.assertTrue(out[there.key]['exists'])
        self.assertEqual(out[there.key]['where'], 'tests/test_x.py:13')
        self.assertFalse(out[gone.key]['exists'])
        self.assertEqual(out[gone.key]['defined'], ['Green', 'Red'])
        nowhere = R.Probe(R.SYMBOL, ('nopkg.mod.X',))
        self.assertIsNone(R.answer(nowhere, self.probe.probe([nowhere])[nowhere.key]))

    def test_a_disabled_class_is_not_probed(self):
        probe = T.TrunkProbe(self.repo, 'main', self.state, trunk_tests=False, symbols=False)
        self.assertEqual(probe.probe([tests_probe('tests.test_x.Green'),
                                      R.Probe(R.SYMBOL, ('tests.test_x.Red',))]), {})


class Settings(unittest.TestCase):

    def test_defaults(self):
        s = settings.read(None)['resolve']
        self.assertEqual(s, {'trunk_tests': True, 'symbols': True, 'test_timeout_s': 600,
                             'python': 'python3', 'test_runs_per_tick': 1, 'gates': True,
                             'inbox_bugs': True})


if __name__ == '__main__':
    unittest.main()
