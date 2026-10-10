"""Small builders for the kernel's facts: each fills the fields a scenario does not care about.

``facts(items=[...])`` keys the items by id; every other builder returns one model value with
defaults that are neutral (no red, no behind, no verdict, a live session). The readers
``of(plan, cls)`` and ``state(plan, iid)`` pick a plan apart.
"""
from asf.kernel import actions as A
from asf.kernel import model as M

State = M.State

#: the document lanes' branch prefixes and paths in the test product (config, not kernel literals)
DOC_BRANCHES = ('spec/', 'plan/')
DOC_PATHS = ('docs/**',)


def config(**kw):
    kw.setdefault('doc_branches', DOC_BRANCHES)
    kw.setdefault('doc_paths', DOC_PATHS)
    kw.setdefault('work_branch', 'worker/')
    kw.setdefault('fix_branch', 'fix/')
    kw.setdefault('revert_branch', 'revert/')
    return M.Config(**kw)


def item(iid, **kw):
    if 'type' not in kw:
        kw['type'] = {'E': 'epic', 'F': 'feature', 'S': 'story', 'B': 'bug'}.get(iid[0], 'task')
    return M.Item(id=iid, **kw)


def task(iid, state=State.READY, rank=1, **kw):
    return item(iid, state=state, rank=rank, **kw)


def check(name='test', conclusion='success', status='completed', run_id=1, **kw):
    if status != 'completed':
        conclusion = None
    return M.Check(name=name, status=status, conclusion=conclusion, run_id=run_id, **kw)


def pr(number, item_id, branch=None, tree='tree-1', head='head-1', files=None, checks=None,
       **kw):
    return M.PR(number=number, branch=branch or 'worker/%s' % item_id, item_id=item_id,
                head_sha=head, tree_sha=tree, files=list(files or ['src/a.py']),
                checks=list(checks if checks is not None else [check()]), **kw)


def session(job, item_id, **kw):
    kw.setdefault('pid', 4242)
    kw.setdefault('worktree', '/work/%s' % job)
    return M.Session(job=job, item_id=item_id, **kw)


def review(item_id, tree='tree-1', verdict='approve', findings=None, change=''):
    return M.Review(item_id=item_id, tree_sha=tree, verdict=verdict, findings=list(findings or []),
                    change_id=change)


def answer(item_id, text):
    return M.Answer(item_id=item_id, text=text)


def facts(items=(), **kw):
    return M.Facts(items={i.id: i for i in items}, **kw)


def of(plan, cls):
    """Every action of type ``cls`` in ``plan``, in order."""
    return [a for a in plan.actions if isinstance(a, cls)]


def state(plan, iid):
    """``iid``'s :class:`State` in ``plan``."""
    return plan.states[iid][0]


def stuck(plan, iid):
    """``iid``'s :class:`asf.kernel.model.Stuck` in ``plan`` (``None`` unless Stuck)."""
    return plan.states[iid][1]


def launched(plan, kind=None):
    """The item ids ``plan`` launches (of ``kind``, when given)."""
    return [a.item_id for a in of(plan, A.Launch) if kind is None or a.kind == kind]
