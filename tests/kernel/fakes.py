"""In-memory ports for the kernel's loop: each records the writes it was asked for, and a write
named in ``fail`` raises instead (so a test can break one action and watch the rest go on)."""
import copy

from asf.kernel import ports as P
from asf.kernel import model as M


class FakeRecord:

    def __init__(self, items, specs=None, answers=(), reviews=(), paused=False, fail=()):
        self._items = {i.id: i for i in items}
        self.fields = {i.id: {} for i in items}
        self._specs = dict(specs or {})
        self._answers, self._reviews = list(answers), list(reviews)
        self._paused = paused
        self.fail = set(fail)
        self.writes, self.minted = [], []

    def items(self):
        out = {}
        for iid, it in self._items.items():
            it = copy.deepcopy(it)
            f = self.fields[iid]
            if P.STATE in f:
                it.state = M.State(f[P.STATE])
            if it.state is M.State.STUCK and f.get(P.STUCK_REASON):
                it.stuck = M.Stuck(f[P.STUCK_REASON], f[P.STUCK_OWNER], f.get(P.STUCK_NEXT, ''))
            it.attempts = list(f.get(P.ATTEMPTS, it.attempts))
            it.fix_rounds = f.get(P.FIX_ROUNDS, it.fix_rounds)
            it.answers = list(f.get(P.ANSWERS, it.answers))
            out[iid] = it
        return out

    def card_fields(self, item_id):
        return dict(self.fields.get(item_id, {}))

    def specs_landed(self):
        return dict(self._specs)

    def answers(self):
        return list(self._answers)

    def reviews(self):
        return list(self._reviews)

    def paused(self):
        return self._paused

    def write_fields(self, item_id, fields):
        if ('write', item_id) in self.fail:
            raise P.PortError('card %s unwritable' % item_id)
        self.writes.append((item_id, dict(fields)))
        for k, v in fields.items():
            if v is None or v == []:
                self.fields[item_id].pop(k, None)
            else:
                self.fields[item_id][k] = v

    def mint_story(self, feature_id, story_id, title, acceptance):
        self.minted.append((feature_id, story_id, title, list(acceptance)))
        self._items[story_id] = M.Item(id=story_id, type='story', title=title, parent=feature_id)
        self.fields[story_id] = {}


class FakeGitHub:

    def __init__(self, prs=(), reviews=(), fail=()):
        self._prs, self._reviews = list(prs), list(reviews)
        self.fail = set(fail)
        self.calls = []

    def prs(self):
        return copy.deepcopy(self._prs)

    def reviews(self, prs):
        return list(self._reviews)

    def _do(self, what, arg):
        if (what, arg) in self.fail:
            raise P.PortError('%s %s refused' % (what, arg))
        self.calls.append((what, arg))

    def enable_auto_merge(self, pr):
        self._do('auto_merge', pr)

    def update_branch(self, pr):
        self._do('update_branch', pr)

    def rerun(self, run_id):
        self._do('rerun', run_id)


class FakeSessions:

    def __init__(self, sessions=(), fail=()):
        self._sessions = list(sessions)
        self.fail = set(fail)
        self.launched, self.ended = [], []

    def sessions(self):
        return copy.deepcopy(self._sessions)

    def launch(self, kind, item_id, branch, brief):
        if ('launch', item_id) in self.fail:
            raise P.PortError('no account with a free seat')
        job = '%s-%s' % (kind, item_id.lower())
        self.launched.append((kind, item_id, branch, brief))
        self._sessions.append(M.Session(job=job, item_id=item_id, kind=kind))
        return job

    def end(self, session, free_worktree):
        self.ended.append((session.job, free_worktree))
        self._sessions = [s for s in self._sessions if s.job != session.job]


def ports(record=None, github=None, sessions=None):
    return P.Ports(record or FakeRecord([]), github or FakeGitHub(), sessions or FakeSessions())
