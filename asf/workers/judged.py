"""A correction round is briefed on the branch as it is, not as its review judged it.

A hold records the head it was written on (``judged_head``, :func:`asf.workers.lifecycle.hold`).
By the time the correct session launches the branch may have moved past it — another session
pushed, an earlier round's push landed after the review was filed — and a session briefed only
with the old review re-answers points the branch already answers, or answers them on the wrong
code (production, 2026-10-05). At launch (:func:`asf.workers.spawn.spawn`, after the worktree's
fetch) :func:`launch_section` re-reads ``origin/<branch>``: when it carries commits the judged
head lacks, the brief gains a ``BRANCH MOVED`` section naming the real head and the commits and
files since, so the session judges each point of the correction against the current head.

A rebase onto a newer trunk alone is no move: commits patch-equal to the judged ones are left
out (``--cherry-pick``), and so is everything the trunk holds.
"""
from asf import gitops

#: The row kinds whose brief is a correction of a judged head.
KINDS = ('correct',)
#: The most commits the section lists.
MAX_COMMITS = 20


def _git(repo, *args):
    r = gitops.git(list(args), repo, timeout=60)
    return r.data if r.ok else None


def _same(a, b):
    a, b = (a or '').lower(), (b or '').lower()
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


def moved(repo, branch, judged_head, main):
    """``(head, commits, stat)`` when ``origin/<branch>`` carries work ``judged_head`` lacks —
    ``commits`` the new ``"<sha> <subject>"`` lines (None when the judged head is unknown here:
    history was rewritten past it), ``stat`` the diff stat since it ('' when the head is no
    descendant). None when the branch is where the review left it, or cannot be read."""
    if not repo or not branch or not judged_head:
        return None
    head = _git(repo, 'rev-parse', '--verify', '-q', f'refs/remotes/origin/{branch}')
    if not head or _same(head, judged_head):
        return None
    if _git(repo, 'cat-file', '-e', f'{judged_head}^{{commit}}') is None:
        return head, None, ''
    trunk = f'refs/remotes/origin/{main}' if main else None
    args = ['log', '--format=%h %s', '--right-only', '--cherry-pick', '--no-merges',
            f'{judged_head}...{head}']
    if trunk and _git(repo, 'rev-parse', '--verify', '-q', trunk):
        args += ['--not', trunk]
    out = _git(repo, *args)
    if out is None:
        return None
    commits = [ln for ln in out.splitlines() if ln.strip()]
    if not commits:
        return None  # a rebase of the judged work onto a newer trunk: nothing new to judge
    stat = ''
    if _git(repo, 'merge-base', '--is-ancestor', judged_head, head) is not None:
        stat = _git(repo, 'diff', '--stat', judged_head, head) or ''
    return head, commits, stat


def section(repo, branch, judged_head, main):
    """The brief's ``BRANCH MOVED`` section, or '' when the branch has not moved."""
    got = moved(repo, branch, judged_head, main)
    if not got:
        return ''
    head, commits, stat = got
    lines = [f'\n\nBRANCH MOVED SINCE THE JUDGMENT: the correction above was written on '
             f'{judged_head[:9]}, but origin/{branch} is now at {head[:9]}.']
    if commits is None:
        lines.append(f'{judged_head[:9]} is no longer in the history (the branch was rewritten): '
                     f'read every point of the correction against {head[:9]} itself.')
    else:
        shown = commits[:tunable('MAX_COMMITS')]
        lines.append(f'Commits since ({len(commits)}):')
        lines += [f'  - {c}' for c in shown]
        if len(commits) > len(shown):
            lines.append(f'  - … {len(commits) - len(shown)} more '
                         f'(git log {judged_head[:9]}..{head[:9]})')
        if stat:
            lines += ['Diff since:', stat]
    lines.append(f'Your worktree starts on {head[:9]}. Judge each point of the correction against '
                 f'that head: a point the new commits already answer needs no change — say so in '
                 f'your report; answer only what still stands, on top of {head[:9]}.')
    return '\n'.join(lines)


def launch_section(path, repo, main, kind, item, branch):
    """:func:`section` for a launch: a ``correct`` row's item's pending correction carries the
    head it judged (``judged_head``); '' for any other row, or when nothing moved. Never raises —
    a brief is never lost to this read."""
    if kind not in KINDS or not item:
        return ''
    try:
        from asf.workers import lifecycle
        corr = lifecycle.correction_of(path, item) or {}
        judged_head = str(corr.get('judged_head') or '')
        if not judged_head:
            return ''
        return section(repo, corr.get('branch') or branch, judged_head, main)
    except Exception:  # noqa: BLE001 — the launch goes on with the brief it has
        return ''


# ---- tunables ---------------------------------------------------------------

#: The config key (``~/.ASF/config.yaml``) over each constant above; the constant is its default.
TUNABLES = {
    'MAX_COMMITS': 'brief.judged_commits_max',
}


def tunable(name):
    """The constant ``name`` of :data:`TUNABLES` with its config key over it."""
    from asf import config_keys
    return config_keys.value(TUNABLES[name], globals()[name])
