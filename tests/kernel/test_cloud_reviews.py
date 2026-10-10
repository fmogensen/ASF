"""Cloud reviews report on ``refs/asf/reviews/<job>``, never on the PR branch: a push there moves
the PR head and restarts its whole CI, which is why reviews were local-only. The CLOUD block of a
review brief sends the verdict commit to its own ref; the host reads it when the run ends (the
verdict lands on the run's log as its result), deletes the ref, and the kernel records the
verdict in the review ledger as for a local review — an approve goes to Landing. Local reviews
keep the log path unchanged, and reviews are in the default ``kernel.launch.cloud_kinds``."""
import os
import tempfile
import unittest
from unittest import mock

from asf import env
from asf.kernel import loop
from asf.kernel import ports as P
from asf.kernel import settings
from asf.kernel.decide import decide
from asf.kernel.facts import read_facts
from asf.workers import cloud
from asf.workers import pool as pool_mod
from asf.workers import quota as quota_mod
from asf.workers import remote
from asf.workers import runtime as runtime_mod
from asf.workers import wave as wave_mod

try:
    from kernel import builders as B
    from kernel import fakes as F
    from kernel.test_go_live import LedgerRecord
    from test_remote import ON, FakeHelper, acct, job as make_job
    from test_workers import Home, feature_row, git
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel import fakes as F
    from tests.kernel.test_go_live import LedgerRecord
    from tests.test_remote import ON, FakeHelper, acct, job as make_job
    from tests.test_workers import Home, feature_row, git

State = B.State

VERDICT_MSG = ('asf: report spec-1\n\nREPORT\nitem: T-0001\nkind: review\nstatus: done\n'
               'pushed: n/a — review reported on its ref\n\nVERDICT: %s\nFINDINGS: %s\n')


class ReviewBrief(unittest.TestCase):

    def test_a_cloud_review_pushes_its_verdict_to_its_own_ref_never_the_branch(self):
        j = make_job(name='review-t-0001-1', kind='review')
        text = cloud.cloud_brief('the brief', j, setting=remote.setting_lines(j))
        self.assertIn('git push origin HEAD:refs/asf/reviews/review-t-0001-1', text)
        self.assertIn('never commit to, push or force-push `task/t-0001`', text)
        self.assertNotIn('Commit and push on `task/t-0001` only', text)
        self.assertNotIn('push `task/t-0001`. The factory', text)
        self.assertIn('VERDICT:', text)

    def test_a_light_review_too_and_a_build_keeps_its_branch_push(self):
        j = make_job(name='review-t-0001-2', kind='light-review')
        self.assertIn('HEAD:refs/asf/reviews/review-t-0001-2', cloud.cloud_brief('b', j))
        build = cloud.cloud_brief('b', make_job(kind='task'), setting=remote.setting_lines(
            make_job()))
        self.assertIn('Commit and push on `task/t-0001` only', build)
        self.assertIn('push `task/t-0001`. The factory', build)
        self.assertNotIn('refs/asf/reviews', build)

    def test_reviews_are_in_the_default_cloud_kinds(self):
        kinds = settings.read(None)['launch']['cloud_kinds']
        self.assertIn('review', kinds)
        self.assertIn('light-review', kinds)


class CloudReviewRef(Home):
    """A claude-remote review run: launched, its verdict pushed to its ref, synced, landed."""

    def setUp(self):
        super().setUp()
        self.product = env.Product('sample', dict(self.product._data, repo_slug='o/r'))
        self.cfg = dict(self.cfg, cloud=dict(ON))
        self.cfg['worker_pool'] = dict(self.cfg['worker_pool'], accounts=[
            {'name': 'acct-a', 'role': 'local', 'cap': 1},
            {'name': 'acct-c', 'role': 'worker', 'cap': 1}])
        self.fake = FakeHelper()
        self.origin = os.path.join(self.tmp, 'origin.git')

    def client(self, account=None):
        return remote.TriggerClient(account or acct(), binary='claude', run=self.fake,
                                    cwd=self.tmp)

    def launch(self):
        accounts = pool_mod.accounts_from_config(self.cfg)
        pool = pool_mod.Pool(accounts, quota_source=quota_mod.FakeQuotaSource({}))
        rt = remote.RemoteRuntime(cloud.settings(self.cfg, self.product), self.product,
                                  client=self.client)
        launched, _waits = wave_mod.wave(
            self.product, [feature_row('spec-1')], 5, pool=pool,
            runtime=runtime_mod.FakeRuntime([{'running': True}] * 5), cfg=self.cfg,
            out=lambda _l: None, local_hold='host pressure', cloud_runtime=rt)
        (_row, rec), = launched
        # the kernel's review: its ledger row's kind, PR and the change it reviews
        pool_mod.update_session(self.product, 'spec-1', kind='review', item='T-0001',
                                kernel_pr=7, kernel_tree='t1', kernel_change='c1')
        return dict(rec, kind='review')

    def sync(self):
        return cloud.sync(self.product, self.cfg, out=lambda _l: None,
                          remote_client=self.client())

    def push_verdict(self, rec, message, files=None):
        other = os.path.join(self.tmp, 'cloud-review')
        git('clone', '-q', '-b', rec['branch'], self.origin, other, cwd=self.tmp)
        for k, v in (('user.email', 'c@example.com'), ('user.name', 'c')):
            git('config', k, v, cwd=other)
        git('checkout', '-q', '--detach', cwd=other)
        for path, text in (files or {}).items():
            os.makedirs(os.path.dirname(os.path.join(other, path)), exist_ok=True)
            with open(os.path.join(other, path), 'w') as f:
                f.write(text)
            git('add', '-f', path, cwd=other)
        git('commit', '-q', '--allow-empty', '--cleanup=whitespace', '-m', message,
            '--trailer', f"ASF-Session: {rec['session']}", '--trailer', 'ASF-Report: spec-1',
            cwd=other)
        git('push', '-q', 'origin', 'HEAD:refs/asf/reviews/spec-1', cwd=other)

    def branch_head(self, rec):
        return git('rev-parse', f"refs/heads/{rec['branch']}", cwd=self.origin)

    def test_the_verdict_on_its_ref_finishes_the_run_and_lands_the_item(self):
        rec = self.launch()
        before = self.branch_head(rec)
        self.assertEqual(self.sync()[0][1], cloud.WORKING)
        self.push_verdict(rec, VERDICT_MSG % ('approve', 'none'))
        (_job, status, why), = self.sync()
        self.assertEqual(status, cloud.FINISHED, why)
        # read, then deleted: no review (or brief) ref outlives the run
        self.assertEqual(git('for-each-ref', 'refs/asf/', cwd=self.origin), '')
        self.assertEqual(git('for-each-ref', 'refs/asf/', cwd=rec['worktree']), '')
        # the PR branch was never pushed by the review
        self.assertEqual(self.branch_head(rec), before)
        self.assertIn('VERDICT: approve', runtime_mod.read_result(rec['log'])['result'])

        # the kernel reads the ended review off the ledger, as for a local one
        sessions = P.RealSessions(self.product, cfg=self.cfg, log=lambda *_: None).sessions()
        (s,), = [[x for x in sessions if x.job == 'spec-1']]
        self.assertEqual((s.kind, s.ended, s.alive, s.result, s.cloud),
                         ('review', True, False, 'report', True))
        self.assertEqual((s.pr, s.tree_sha, s.change_id), (7, 't1', 'c1'))

        state = tempfile.mkdtemp()
        rec_port = LedgerRecord([B.task('T-0001', state=State.REVIEW)], state)
        gh = F.FakeGitHub(prs=[B.pr(7, 'T-0001', tree='t1', change_id='c1')])
        ports = F.ports(record=rec_port, github=gh, sessions=F.FakeSessions([s]))
        loop.tick(env.Product('sample', {}), ports=ports, config=B.config(), state_dir=state,
                  out=lambda *_: None)
        facts = read_facts(ports)
        self.assertEqual([(r.item_id, r.tree_sha, r.change_id, r.verdict)
                          for r in facts.reviews], [('T-0001', 't1', 'c1', 'approve')])
        self.assertEqual(B.state(decide(facts, B.config()), 'T-0001'), State.LANDING)

    def test_the_verdict_in_the_review_file_is_read_too(self):
        rec = self.launch()
        self.push_verdict(rec, 'asf: report spec-1\n\nREPORT\nstatus: done\n',
                          files={'docs/reviews/T-0001.md':
                                 '# review\n\nVERDICT: changes\nFINDINGS: a.py:3 — fix it\n'})
        (_job, status, why), = self.sync()
        self.assertEqual(status, cloud.FINISHED, why)
        result = runtime_mod.read_result(rec['log'])['result']
        from asf.kernel.briefs import parse_verdict
        self.assertEqual(parse_verdict(result), ('changes', ['a.py:3 — fix it']))
        self.assertEqual(git('for-each-ref', 'refs/asf/', cwd=self.origin), '')

    def test_a_review_pushed_to_the_branch_does_not_count(self):
        rec = self.launch()
        other = os.path.join(self.tmp, 'wrong')
        git('clone', '-q', '-b', rec['branch'], self.origin, other, cwd=self.tmp)
        for k, v in (('user.email', 'c@example.com'), ('user.name', 'c')):
            git('config', k, v, cwd=other)
        git('commit', '-q', '--allow-empty', '-m', VERDICT_MSG % ('approve', 'none'),
            '--trailer', f"ASF-Session: {rec['session']}", '--trailer', 'ASF-Report: spec-1',
            cwd=other)
        git('push', '-q', 'origin', rec['branch'], cwd=other)
        self.assertEqual(self.sync()[0][1], cloud.WORKING)

    def test_an_unreadable_origin_is_never_no_report(self):
        rec = self.launch()
        failed = mock.Mock(returncode=128, stdout='', stderr='network down')
        with mock.patch.object(cloud, '_git', return_value=failed):
            with self.assertRaises(cloud.ReportUnreadable):
                cloud.review_report(rec['worktree'], 'spec-1')


class LocalReviewsUnchanged(unittest.TestCase):

    def test_a_local_review_is_not_a_cloud_run_and_reads_its_log(self):
        self.assertFalse(cloud.is_cloud({'pid': 4242, 'kind': 'review'}))
        # a non-review run still reads its report commit off its branch
        with mock.patch.object(cloud, 'report_commit', return_value='branch') as rc, \
                mock.patch.object(cloud, 'review_report') as rr:
            self.assertEqual(cloud.run_report({'kind': 'task', 'worktree': '/w',
                                               'branch': 'b', 'session': 's'}), 'branch')
        rc.assert_called_once_with('/w', 'b', 's')
        rr.assert_not_called()


if __name__ == '__main__':
    unittest.main()
