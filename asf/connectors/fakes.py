"""asf.connectors.fakes — in-tree fakes, selectable as ``connectors.<kind>: fake``: no service is
touched, every call is recorded in ``calls``, and answers come from ``answers`` (``{operation:
data}``, or ``{operation: callable(*args, **kw)}``); an operation with no answer is Unknown."""
from asf import github


class FakeConnector:
    name = 'fake'

    def __init__(self, cfg=None, answers=None):
        self.cfg = cfg
        self.answers = dict(answers or {})
        self.calls = []

    def _answer(self, op, *args, **kw):
        self.calls.append((op, args, kw))
        if op not in self.answers:
            return github.unknown(f'fake: no answer for {op}')
        got = self.answers[op]
        data = got(*args, **kw) if callable(got) else got
        if isinstance(data, github.Result):
            return data
        return github.Result(True, data, 0, '', '', github.now_iso(), '')


class FakeForge(FakeConnector):
    def pr(self, slug, number, fields, **kw):
        return self._answer('pr', slug, number, fields, **kw)

    def prs(self, slug, **kw):
        return self._answer('prs', slug, **kw)

    def open_prs(self, slug, **kw):
        return self._answer('open_prs', slug, **kw)

    def merge_commit(self, slug, number, **kw):
        return self._answer('merge_commit', slug, number, **kw)

    def checks(self, slug, sha, **kw):
        return self._answer('checks', slug, sha, **kw)

    def close_pr(self, slug, number, comment='', **kw):
        return self._answer('close_pr', slug, number, comment=comment, **kw)

    def reopen_pr(self, slug, number, **kw):
        return self._answer('reopen_pr', slug, number, **kw)

    def api(self, path, method='GET', fields=None, **kw):
        return self._answer('api', path, method=method, fields=fields, **kw)

    def auth_status(self, **kw):
        return self._answer('auth_status', **kw)


class FakeCI(FakeConnector):
    def runs(self, slug, **kw):
        return self._answer('runs', slug, **kw)

    def run(self, slug, run_id, **kw):
        return self._answer('run', slug, run_id, **kw)

    def runs_for_sha(self, slug, sha, **kw):
        return self._answer('runs_for_sha', slug, sha, **kw)

    def run_log(self, slug, run_id, **kw):
        return self._answer('run_log', slug, run_id, **kw)

    def rerun(self, slug, run_id, failed=True, **kw):
        return self._answer('rerun', slug, run_id, failed=failed, **kw)

    def cancel(self, slug, run_id, **kw):
        return self._answer('cancel', slug, run_id, **kw)

    def call(self, args, **kw):
        return self._answer('call', list(args), **kw)

    def source(self, product, run=None):
        from asf import ci_queue
        return ci_queue.Source()

    def backend(self, product, run=None):
        from asf import ci_pool
        return ci_pool.Backend()
