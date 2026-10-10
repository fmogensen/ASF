"""asf.kernel.mainline — the main safety net (ASF 0.2).

With "require branches to be up to date" off (``Facts.strict`` False) GitHub merges a green PR
behind its base, so two changes green on their own can land red together. CI runs on every push
to the trunk; :func:`judge` reads the newest commits there (``Facts.main``, newest first) and
plans what a red trunk needs. Pure: facts and config in, actions and one record out.

- A commit's verdict on its required checks (``config.required_checks``; empty: every check):
  ``red`` once a required check completed red, ``green`` once every required check completed
  green (success, neutral or skipped), else ``pending`` (running, cancelled, not reported).
- The newest commit with a verdict decides. Green: nothing. Red: the commits from it back to
  (not including) the last green commit are the candidates — the red is new since that green.
- A red whose every red required check reads as infra or flaky (a runner-loss signature on its
  log, :data:`asf.flake.INFRA_SIGNATURES`; or failing files that are all known and all outside
  every candidate's files: off the change, as a PR's off-PR red) is rerun once first
  (:class:`~asf.kernel.actions.Rerun`); still red on the rerun, it is the change's.
- Exactly one candidate, a merged PR of an item on the record, and ``config.main_red_revert``:
  :class:`~asf.kernel.actions.RevertPR` — ``git revert`` of its squash commit on
  ``revert/<item>``, auto-merge on, the item back to Ready with a note (its relaunch carries the
  red as a finding). A candidate already reverted (``Item.reverted``) waits for its revert.
- Otherwise (several candidates, a direct push, no green in sight, the knob off):
  :class:`~asf.kernel.actions.FileBug` — one Bug card for a fix session on main's red naming the
  candidates, filed once per red spell (:func:`bug_key`); while it is open, main waits on it.

Every red tick has a record ``{'sha', 'action'}`` (``Plan.main``), logged
``MAIN RED <sha> -> <action>``.
"""
from asf.kernel import actions as A
from asf.kernel.model import RED_CONCLUSIONS, State

#: a completed required check's conclusions that count as green
GREEN_CONCLUSIONS = ('success', 'neutral', 'skipped')

#: the marker line a main-red Bug's body carries: ``main-red: <last green>..<red>``
BUG_MARKER = 'main-red: '

#: the most characters of a red's log tail a Bug or finding carries
TAIL_MAX = 1500


def _required(name, config):
    return not config.required_checks or name in config.required_checks


def verdict(commit, config):
    """``'red'``, ``'green'`` or ``'pending'`` of ``commit`` on its required checks."""
    req = [c for c in commit.checks if _required(c.name, config)]
    if any(c.status == 'completed' and c.conclusion in RED_CONCLUSIONS for c in req):
        return 'red'
    names = {c.name for c in req}
    if not req or any(n not in names for n in config.required_checks):
        return 'pending'
    if all(c.status == 'completed' and c.conclusion in GREEN_CONCLUSIONS for c in req):
        return 'green'
    return 'pending'


def red_checks(commit, config):
    """The required checks completed red on ``commit``."""
    return [c for c in commit.checks if _required(c.name, config)
            and c.status == 'completed' and c.conclusion in RED_CONCLUSIONS]


def infra_or_flaky(check, files):
    """Whether red ``check`` says nothing of the change: its log names a runner loss, or its
    failing files are known and none is among ``files`` (every candidate's)."""
    from asf.flake import INFRA_SIGNATURES
    tail = str(check.log_tail or '').lower()
    if not check.failing_files and tail and any(sig in tail for sig in INFRA_SIGNATURES):
        return True
    return bool(check.failing_files) and not (set(check.failing_files) & set(files))


def red_spell(facts, config):
    """``(red commit, candidates, last green sha)`` of the trunk, or None when its newest commit
    with a verdict is green (or none has one). ``last green`` is '' when no green commit is in
    sight: the candidates are then every commit read from the red one back."""
    commits = list(facts.main or [])
    for i, c in enumerate(commits):
        v = verdict(c, config)
        if v == 'pending':
            continue
        if v == 'green':
            return None
        for j in range(i + 1, len(commits)):
            if verdict(commits[j], config) == 'green':
                return c, commits[i:j], commits[j].sha
        return c, commits[i:], ''
    return None


def bug_key(red, green):
    """The marker of one red spell: ``main-red: <last green>..<red>``."""
    return '%s%s..%s' % (BUG_MARKER, green or 'none', red.sha)


def open_bug(facts, green):
    """The id of an open (not Done) Bug filed for the red spell since ``green``, else None."""
    head = '%s%s..' % (BUG_MARKER, green or 'none')
    for iid in sorted(facts.items):
        it = facts.items[iid]
        if it.type == 'bug' and head in (it.body or '') and it.state is not State.DONE:
            return iid
    return None


def filed(facts, key):
    """Whether a Bug carrying ``key`` is on the record (any state)."""
    return any(it.type == 'bug' and key in (it.body or '') for it in facts.items.values())


def _short(sha):
    return str(sha or '')[:9]


def _check_lines(reds):
    out = []
    for c in reds:
        line = '- %s' % c.name
        if c.failed_step:
            line += ' — step %r failed' % c.failed_step
        if c.run_id:
            line += ' (run %s)' % c.run_id
        out.append(line)
        tail = str(c.log_tail or '').strip()
        if tail:
            if len(tail) > TAIL_MAX:
                tail = '…' + tail[-TAIL_MAX:]
            out.append('\n```\n%s\n```' % tail)
    return out


def _candidate_line(c):
    who = ('PR #%d (%s%s)' % (c.pr, c.branch, ', ' + c.item_id if c.item_id else '')
           if c.pr else 'a direct push')
    return '- %s %s — %s' % (_short(c.sha), who, c.headline)


def revert_action(red, culprit, it, reds, config):
    """The :class:`RevertPR` of ``culprit`` (the one candidate, ``it``'s merged PR)."""
    names = ', '.join(c.name for c in reds)
    from asf.conventions import DEFAULT_BRANCH_PREFIXES
    branch = (config.revert_branch or DEFAULT_BRANCH_PREFIXES['revert']) + it.id
    title = 'Revert %s — main red at %s (%s)' % (it.id, _short(red.sha), names)
    body = '\n'.join(
        ['Main went red on %s at %s; the only change since the last green commit is %s '
         '(PR #%d). The kernel reverts it so main is green again; %s goes back to Ready.'
         % (names, red.sha, culprit.sha, culprit.pr, it.id), '', 'Red checks:']
        + _check_lines(reds))
    note = 'PR #%d reverted off main (red at %s: %s) — back to Ready' % (
        culprit.pr, _short(red.sha), names)
    finding = ('PR #%d landed and turned main red (%s at %s); it was reverted. Build it again '
               'so the required checks pass on main.' % (culprit.pr, names, _short(red.sha)))
    tails = [str(c.log_tail).strip() for c in reds if str(c.log_tail or '').strip()]
    if tails:
        finding += '\n' + tails[0][-TAIL_MAX:]
    return A.RevertPR(it.id, culprit.pr, culprit.sha, branch, title, body, note, finding)


def bug_action(red, candidates, green, reds, facts):
    """The :class:`FileBug` of a red spell (its candidates and red checks in the body)."""
    names = ', '.join(c.name for c in reds)
    key = bug_key(red, green)
    title = 'main red at %s: %s — %d candidate change(s)' % (_short(red.sha), names,
                                                            len(candidates))
    body = '\n'.join(
        ['Main is red on %s at %s, since the last green commit %s. Find the change that broke '
         'it among the candidates, fix main (or revert the culprit) so the required checks '
         'pass, and push the fix.' % (names, red.sha, green or '(none in sight)'), '',
         'Candidates (newest first):'] + [_candidate_line(c) for c in candidates]
        + ['', 'Red checks:'] + _check_lines(reds) + ['', key])
    ranks = [it.rank for it in facts.items.values() if isinstance(it.rank, int)]
    return A.FileBug(key, title, body, min(ranks + [1]) - 1)


def judge(facts, config):
    """``(actions, record)`` of the main safety net on ``facts``: see the module doc. ``record``
    is None while the trunk is green or unread."""
    spell = red_spell(facts, config)
    if spell is None:
        return [], None
    red, candidates, green = spell
    reds = red_checks(red, config)
    files = sorted({f for c in candidates for f in c.files})
    if reds and all(infra_or_flaky(c, files) for c in reds):
        runs = sorted({c.run_id for c in reds if c.run_id and c.attempt <= 1})
        if runs:
            return ([A.Rerun(r) for r in runs],
                    {'sha': red.sha, 'action': 'rerun %s (infra or flaky)'
                     % ', '.join(str(r) for r in runs)})
    if len(candidates) == 1 and config.main_red_revert:
        c = candidates[0]
        mine = c.pr and c.item_id and not (config.revert_branch
                                             and c.branch.startswith(config.revert_branch))
        it = facts.items.get(c.item_id) if mine else None  # a revert is never reverted
        if it is not None:
            if c.pr in it.reverted:
                return [], {'sha': red.sha, 'action': 'waits on the revert of #%d (%s)'
                            % (c.pr, it.id)}
            a = revert_action(red, c, it, reds, config)
            return [a], {'sha': red.sha, 'action': A.describe(a)}
    bug = open_bug(facts, green)
    if bug is not None:
        return [], {'sha': red.sha, 'action': 'waits on fix session %s' % bug}
    a = bug_action(red, candidates, green, reds, facts)
    if filed(facts, a.key):
        return [], {'sha': red.sha, 'action': 'its Bug is done; waits for main to rerun'}
    return [a], {'sha': red.sha, 'action': A.describe(a)}
