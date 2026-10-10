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
        self.writes, self.minted, self.recorded, self.changes = [], [], [], []

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
            it.extra_rounds = f.get(P.EXTRA_ROUNDS, it.extra_rounds)
            it.rebuilds = f.get(P.REBUILDS, it.rebuilds)
            if P.STUCK_SINCE in f:
                it.stuck_since = f[P.STUCK_SINCE]
            it.answers = list(f.get(P.ANSWERS, it.answers))
            it.findings = list(f.get(P.FINDINGS, it.findings))
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

    def record_review(self, item_id, pr, tree_sha, verdict, findings, change_id=''):
        self.recorded.append((item_id, pr, tree_sha, verdict, list(findings)))
        self.changes.append(change_id)
        self._reviews.append(M.Review(item_id, tree_sha, verdict, list(findings), change_id))

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

    def __init__(self, prs=(), reviews=(), fail=(), branches=()):
        self._prs, self._reviews = list(prs), list(reviews)
        self._branches = list(branches)
        self.fail = set(fail)
        self.calls, self.opened = [], []

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

    def rerun(self, run_id, cancel=False):
        self._do('cancel' if cancel else 'rerun', run_id)

    def branches(self):
        return copy.deepcopy(self._branches)

    def archive_and_reset(self, pr, branch, head_sha, comment):
        self._do('archive_and_reset', pr)
        self.archived = getattr(self, 'archived', []) + [(pr, branch, head_sha, comment)]
        self._prs = [p for p in self._prs if p.number != pr]
        self._branches = [b for b in self._branches if b.name != branch]
        return P.ARCHIVE_PREFIX + branch

    def close_pr(self, pr, comment):
        self._do('close_pr', pr)
        self.closed = getattr(self, 'closed', []) + [(pr, comment)]
        self._prs = [p for p in self._prs if p.number != pr]

    def open_pr(self, branch, base, title, body):
        self._do('open_pr', branch)
        self.opened.append((branch, base, title, body))
        number = 900 + len(self.opened)
        self._prs.append(M.PR(number=number, branch=branch, item_id=P.item_of_branch(branch),
                              head_sha='head-%d' % number, tree_sha='tree-%d' % number,
                              files=['src/a.py']))
        return number


class FakeSessions:

    def __init__(self, sessions=(), fail=(), stranded=(), last_jobs=None):
        self._sessions = list(sessions)
        self._last_jobs = dict(last_jobs or {})
        self.fail = set(fail)
        self._stranded = {s.item_id: s for s in stranded}
        self.launched, self.ended, self.meta, self.pushed = [], [], [], []

    def sessions(self):
        return copy.deepcopy(self._sessions)

    def last_jobs(self):
        out = dict(self._last_jobs)
        out.update({s.item_id: s.job for s in self._sessions})
        return out

    def stranded(self, item_id):
        return copy.deepcopy(self._stranded.get(item_id))

    def launch(self, kind, item_id, branch, brief, meta=None):
        if ('launch', item_id) in self.fail:
            raise P.PortError('no account with a free seat')
        job = '%s-%s' % (kind, item_id.lower())
        self.launched.append((kind, item_id, branch, getattr(brief, 'text', brief)))
        self.meta.append(dict(meta or {}))
        self._sessions.append(M.Session(job=job, item_id=item_id, kind=kind))
        return job

    def push_rebase(self, session, sha):
        if ('push_rebase', session.job) in self.fail:
            raise P.PortError('rebased %s: push refused' % sha)
        self.pushed.append((session.job, session.branch, sha))
        return 'pushed rebased %s' % sha

    def end(self, session, free_worktree):
        self.ended.append((session.job, free_worktree))
        self._sessions = [s for s in self._sessions if s.job != session.job]


def brief(item, launch, findings=(), pr=None):
    """A stand-in brief maker: the kind, the item and the findings, one per line."""
    return '\n'.join(['%s %s on %s' % (launch.kind, item.id, launch.branch)] + list(findings))


def ports(record=None, github=None, sessions=None, briefer=brief):
    return P.Ports(record or FakeRecord([]), github or FakeGitHub(), sessions or FakeSessions(),
                   briefer)
