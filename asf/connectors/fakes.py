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
