"""Two more recurring ``NEEDS OPERATOR:`` classes code answers (:mod:`asf.kernel.resolvers`): a
repo gate script a cloud runtime refused to run ("confirm on the host, read-only: `bash
tools/check_conventions.sh`") is run on a fresh detached worktree of the session's branch head —
only a script the product's ``pre_push_check`` names — and answered with its last line and the
sha (``gate``); a Bug card a cloud session could not mint ("no record is mounted in this
container … `asf new bug --title …`") is filed through the record's inbox and answered "filed as
inbox/<file>; do not mint reserved ids" (``inbox-bug``). The fixtures are the questions the console
answered by hand on 2026-10-10 (``state/asf/operator-answers.jsonl``), word for word."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from asf.kernel import actions as A
from asf.kernel import decide as D
from asf.kernel import facts as K
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

#: plan-f-0318-1791621201, answered by hand 08:56:01Z
Q_0318 = ("`bash tools/check_conventions.sh` was not run directly in this session — the cloud "
          "runtime refused the scanning invocation; its scan was reproduced clean over its own 31 "
          "patterns and 272 files, and this branch changes only `docs/plans/f-0318.md`, which that "
          "check does not read, so the result cannot differ from the base commit's. Confirm on the "
          "host, read-only: `bash tools/check_conventions.sh`")
#: plan-f-0314-1791621559, answered by hand 08:56:24Z
TITLE = ('tests.test_workers TestSpawn: five failures on git 2.43.0 in cloud containers, green on '
         'the factory host')
Q_0314_MINT = (
    "mint the Bug card the operator already approved for the five pre-existing tests.test_workers "
    "TestSpawn failures (git 2.43.0, cloud containers only; green on the factory host). This "
    "session cannot: it is a record write and no record is mounted in this container. The five "
    "cases are test_a_review_on_a_branch_behind_main_pushes_nothing, "
    "test_b0046_correct_row_spawns_on_the_held_branch_not_rebased_when_it_merges_clean, "
    "test_b0048_adjudicate_row_spawns_on_the_held_branch, "
    "test_b0051_ended_sessions_worktree_is_reused_not_refused and "
    "test_trunk_rebase_needed_names_only_what_a_rebase_clears - `asf new bug --title \"%s\"`"
    % TITLE)
GATE = 'bash tools/check_conventions.sh'
SHA = '6bf692a53e0f1a2b3c4d5e6f708192a3b4c5d6e7'
SLUG = 'inbox/tests-test-workers-testspawn-five-failures-on-git-2-43-0-in-cloud-containers-green-on-the-factory-host.md'


def asked(iid, q, branch):
    return B.session('plan-%s-1' % iid.lower(), iid, kind='plan', alive=False, ended=True,
                     result='question', status='done', question=q, fields={'status': 'done'},
                     branch=branch)


def feature(iid):
    return B.task(iid, state=State.BUILDING, type='feature')


class Match(unittest.TestCase):

    def test_the_refused_gate_script_runs_on_the_branch_head(self):
        self.assertEqual(R.match(Q_0318, branch='plan/F-0318'),
                         R.Probe(R.GATE, (GATE, 'plan/F-0318')))
        self.assertIsNone(R.match(Q_0318), 'no branch, nothing to run it on')

    def test_the_unmintable_bug_is_filed(self):
        self.assertEqual(R.match(Q_0314_MINT), R.Probe(R.INBOX_BUG, (TITLE,)))
        self.assertEqual(R.match('done: NEEDS OPERATOR: ' + Q_0314_MINT),
                         R.Probe(R.INBOX_BUG, (TITLE,)))

    def test_near_misses_stay_with_the_console(self):
        for q in ('mint the Bug card for the five failures', 'please run `bash x.sh`',
                  "mint a Bug card — `asf new bug --title \"x\"`",
                  '`bash a.sh` and `bash b.sh` were not run here; confirm on the host'):
            self.assertIsNone(R.match(q, branch='plan/F-1'), q)

    def test_the_inbox_body_carries_the_question_and_its_cases(self):
        title, body = R.inbox_card(R.match(Q_0314_MINT), Q_0314_MINT, 'F-0314')
        self.assertEqual(title, TITLE)
        self.assertIn('F-0314', body)
        self.assertIn('no record is mounted in this container', body)
        self.assertIn('Cases: tests.test_workers.TestSpawn.test_a_review_on_a_branch_behind_main'
                      '_pushes_nothing', body)


class Answer(unittest.TestCase):

    def test_a_clean_gate(self):
        text = R.answer(R.Probe(R.GATE, (GATE, 'plan/F-0318')),
                        {'sha': SHA, 'rc': 0, 'last': 'check_conventions: clean'})
        self.assertEqual(text, 'Confirmed on the factory host: `bash tools/check_conventions.sh` '
                               'on plan/F-0318 at 6bf692a -> check_conventions: clean. Proceed.')

    def test_a_red_gate(self):
        text = R.answer(R.Probe(R.GATE, (GATE, 'plan/F-0318')),
                        {'sha': SHA, 'rc': 1, 'last': 'check_conventions: 2 finding(s)'})
        self.assertIn('red (rc 1) -> check_conventions: 2 finding(s)', text)
        self.assertIn('fix it before the push', text)

    def test_a_script_off_the_allowlist_is_no_answer(self):
        self.assertIsNone(R.answer(R.Probe(R.GATE, ('bash evil.sh', 'b')),
                                   {'sha': SHA, 'error': 'not in pre_push_check'}))

    def test_a_filed_bug(self):
        self.assertEqual(R.answer(R.Probe(R.INBOX_BUG, (TITLE,)), {'filed': SLUG}),
                         'Filed by the kernel through the inbox as %s (groom assigns its id); '
                         'do not mint reserved ids. Proceed.' % SLUG)
        self.assertIsNone(R.answer(R.Probe(R.INBOX_BUG, (TITLE,)), {'filed': ''}))


class Decide(unittest.TestCase):

    def test_a_clean_gate_answers_the_session(self):
        p = R.Probe(R.GATE, (GATE, 'plan/F-0318'))
        f = B.facts([feature('F-0318')], sessions=[asked('F-0318', Q_0318, 'plan/F-0318')],
                    resolved={p.key: {'sha': SHA, 'rc': 0, 'last': 'check_conventions: clean'}})
        a = B.of(D.decide(f, B.config()), A.ApplyAnswer)
        self.assertEqual([x.by for x in a], [R.GATE])
        self.assertIn('6bf692a -> check_conventions: clean', a[0].text)

    def test_an_unfiled_bug_is_filed_then_answered(self):
        p = R.match(Q_0314_MINT)
        f = B.facts([feature('F-0314')], sessions=[asked('F-0314', Q_0314_MINT, 'plan/F-0314')],
                    resolved={p.key: {'filed': ''}})
        plan = D.decide(f, B.config())
        filed = B.of(plan, A.FileInbox)
        self.assertEqual([(x.item_id, x.title) for x in filed], [('F-0314', TITLE)])
        self.assertEqual(B.of(plan, A.ApplyAnswer), [], 'answered once the card is on the record')
        self.assertEqual(B.stuck(plan, 'F-0314').owner, 'operator')
        f.resolved = {p.key: {'filed': SLUG}}
        plan = D.decide(f, B.config())
        self.assertEqual(B.of(plan, A.FileInbox), [])
        self.assertEqual([x.by for x in B.of(plan, A.ApplyAnswer)], [R.INBOX_BUG])

    def test_the_knobs_off_leave_the_console(self):
        p = R.match(Q_0314_MINT)
        f = B.facts([feature('F-0314')], sessions=[asked('F-0314', Q_0314_MINT, 'plan/F-0314')],
                    resolved={p.key: {'filed': ''}})
        plan = D.decide(f, B.config(resolve_inbox_bugs=False))
        self.assertEqual((B.of(plan, A.FileInbox), B.of(plan, A.ApplyAnswer)), ([], []))
        g = R.Probe(R.GATE, (GATE, 'plan/F-0318'))
        f = B.facts([feature('F-0318')], sessions=[asked('F-0318', Q_0318, 'plan/F-0318')],
                    resolved={g.key: {'sha': SHA, 'rc': 0, 'last': 'clean'}})
        self.assertEqual(B.of(D.decide(f, B.config(resolve_gates=False)), A.ApplyAnswer), [])


class Facts(unittest.TestCase):

    def test_gates_go_to_the_trunk_probe_and_bugs_to_the_record(self):
        seen = {}

        class Trunk:
            def probe(self, probes):
                seen['trunk'] = list(probes)
                return {p.key: {'sha': SHA, 'rc': 0, 'last': 'ok'} for p in probes}

        class Record(F.FakeRecord):
            def inbox_filed(self, titles):
                seen['record'] = list(titles)
                return {t: '' for t in titles}

        ports = F.ports(Record([feature('F-0318'), feature('F-0314')]),
                        sessions=F.FakeSessions([asked('F-0318', Q_0318, 'plan/F-0318'),
                                                 asked('F-0314', Q_0314_MINT, 'plan/F-0314')]))
        ports.trunk = Trunk()
        f = K.read_facts(ports)
        self.assertEqual(seen, {'trunk': [R.Probe(R.GATE, (GATE, 'plan/F-0318'))],
                                'record': [TITLE]})
        self.assertEqual(f.resolved[R.match(Q_0314_MINT).key], {'filed': ''})


def _git(cwd, *args):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True)


class Host(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        origin = os.path.join(self.tmp, 'origin.git')
        self.repo = os.path.join(self.tmp, 'repo')
        _git(self.tmp, 'init', '-q', '--bare', '-b', 'main', origin)
        _git(self.tmp, 'clone', '-q', origin, self.repo)
        for k, v in (('user.email', 't@example.com'), ('user.name', 't')):
            _git(self.repo, 'config', k, v)
        os.makedirs(os.path.join(self.repo, 'tools'))
        with open(os.path.join(self.repo, 'tools', 'check.sh'), 'w') as f:
            f.write('echo scanning\necho "check: clean on $(git rev-parse --short HEAD)"\n')
        _git(self.repo, 'add', '-A')
        _git(self.repo, 'commit', '-q', '-m', 'trunk')
        _git(self.repo, 'push', '-q', 'origin', 'HEAD:main')
        with open(os.path.join(self.repo, 'tools', 'check.sh'), 'w') as f:
            f.write('echo "check: 1 finding"\nexit 3\n')
        _git(self.repo, 'commit', '-q', '-am', 'branch')
        _git(self.repo, 'push', '-q', 'origin', 'HEAD:plan/F-1')
        _git(self.repo, 'reset', '-q', '--hard', 'origin/main')
        self.state = os.path.join(self.tmp, 'state')
        self.probe = T.TrunkProbe(self.repo, 'main', self.state, python=sys.executable,
                                  timeout_s=60, gates=('bash tools/check.sh',))

    def test_the_allowed_gate_runs_on_the_branch_head_and_leaves_no_worktree(self):
        p = R.Probe(R.GATE, ('bash tools/check.sh', 'main'))
        out = self.probe.probe([p])[p.key]
        self.assertEqual((out['rc'], out['last'][:13]), (0, 'check: clean '))
        q = R.Probe(R.GATE, ('bash tools/check.sh', 'plan/F-1'))
        out = self.probe.probe([q])[q.key]
        self.assertEqual((out['rc'], out['last']), (3, 'check: 1 finding'))
        wts = subprocess.run(['git', 'worktree', 'list'], cwd=self.repo, capture_output=True,
                             text=True).stdout.splitlines()
        self.assertEqual(len(wts), 1)

    def test_a_script_off_the_allowlist_is_never_run(self):
        p = R.Probe(R.GATE, ('bash tools/other.sh', 'main'))
        self.assertIn('error', self.probe.probe([p])[p.key])

    def test_the_allowlist_is_the_pre_push_check_steps(self):
        self.assertEqual(T.gate_steps('bash a.sh && bash  b.sh --x; python3 -m unittest -q t'),
                         ('bash a.sh', 'bash b.sh --x', 'python3 -m unittest -q t'))
        self.assertEqual(T.gate_steps({'code': 'make lint'}), ('make lint',))
        self.assertEqual(T.gate_steps(None), ())


class Record(unittest.TestCase):

    def test_the_intake_name_is_file_cards_and_a_groomed_card_counts(self):
        from asf.kernel import ports as P
        self.assertEqual('inbox/' + P.inbox_name(TITLE), SLUG)
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        rec = P.RealRecord.__new__(P.RealRecord)
        rec.root, rec.product = root, None
        rec._scrub = lambda v: v
        self.assertEqual(rec.inbox_filed([TITLE]), {TITLE: ''})
        os.makedirs(os.path.join(root, 'inbox', 'done'))
        open(os.path.join(root, 'inbox', 'done', P.inbox_name(TITLE)), 'w').close()
        self.assertEqual(rec.inbox_filed([TITLE]),
                         {TITLE: 'inbox/done/' + P.inbox_name(TITLE)})
        self.assertEqual(rec.file_inbox(TITLE, 'body'), 'inbox/done/' + P.inbox_name(TITLE),
                         'never filed twice')


class Settings(unittest.TestCase):

    def test_defaults(self):
        s = settings.read(None)['resolve']
        self.assertIs(s['gates'], True)
        self.assertIs(s['inbox_bugs'], True)


if __name__ == '__main__':
    unittest.main()
