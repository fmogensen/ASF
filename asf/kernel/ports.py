"""asf.kernel.ports — the three doors the kernel reaches the world through (ASF 0.2).

:class:`RecordPort` (the cards and the operator's files), :class:`GitHubPort` (the product's PRs)
and :class:`SessionPort` (the worker sessions) are protocols: a test hands the loop fakes, the
real tick hands it :func:`real_ports`. Each read returns the kernel's model values
(:mod:`asf.kernel.model`); each write is one :mod:`asf.kernel.actions` value's side effect.

The real implementations are thin, over the old floor's proven primitives: the record through
:func:`asf.record.core.load_items`, :mod:`asf.record.frontmatter` and :mod:`asf.record.writer`
(Stories through :func:`asf.record.ids.write_new_item`); GitHub through :func:`asf.github.gh`,
which refuses every mutating call while :func:`asf.mutation_guard.active` is on; sessions through
the ledger (:mod:`asf.workers.pool`, :mod:`asf.workers.lifecycle`) and
:func:`asf.workers.spawn.spawn` (whose runtime is the local one, or the cloud one —
:class:`asf.workers.remote.RemoteRuntime` — per the account's lane).

**The card's kernel fields** live in the existing machine block (below the ``# ---- machine ----``
marker), so every old writer carries them through byte for byte
(:data:`asf.record.frontmatter.DERIVABLE_KEYS` does not name them):

- ``kernel_state``: the item's :class:`~asf.kernel.model.State` value (``new`` … ``stuck``).
  A card without it reads as ``done`` when the old ``state`` is ``Closed``/``Resolved``, else
  ``new`` — the kernel then judges it afresh.
- ``kernel_stuck_reason`` / ``kernel_stuck_owner`` / ``kernel_stuck_next`` /
  ``kernel_stuck_since``: the :class:`~asf.kernel.model.Stuck` while ``kernel_state`` is
  ``stuck`` (``since`` is when it first got stuck on that reason, for the status table's age).
- ``kernel_fix_rounds``: red- or review-driven fix rounds already launched.
- ``kernel_attempts``: the reason of each failed attempt, oldest first.
- ``kernel_findings``: the review findings handed to the next fix round.
- ``kernel_answers``: the operator answers already applied; ``kernel_question``: the open one.
- ``kernel_reopened``: ``true`` when a Done item was reopened (a new PR is accepted).
- ``kernel_notes``: the questions a session asked while its work moved on anyway (a ``done``
  REPORT with a pushed head) — shown by ``asf kernel status``, holding nothing.
"""
import datetime
import json
import os
import re
import time
import typing

from asf.kernel import model as M
from asf.kernel import reports
from asf.record.core import ID_TOKEN_RE, as_list, canonicalize, is_retired, load_items

#: the machine-block keys the kernel owns (see the module docstring)
STATE, ATTEMPTS, FIX_ROUNDS = 'kernel_state', 'kernel_attempts', 'kernel_fix_rounds'
FINDINGS, ANSWERS, QUESTION = 'kernel_findings', 'kernel_answers', 'kernel_question'
STUCK_REASON, STUCK_OWNER = 'kernel_stuck_reason', 'kernel_stuck_owner'
STUCK_NEXT, STUCK_SINCE, REOPENED = 'kernel_stuck_next', 'kernel_stuck_since', 'kernel_reopened'
NOTES = 'kernel_notes'
KERNEL_KEYS = (STATE, STUCK_REASON, STUCK_OWNER, STUCK_NEXT, STUCK_SINCE, FIX_ROUNDS, ATTEMPTS,
               FINDINGS, ANSWERS, QUESTION, REOPENED, NOTES)

#: the card types the kernel judges (decisions and rules are never work)
WORK_TYPES = ('epic', 'feature', 'story', 'task', 'bug')

#: the old record's states that mean the item is finished
CLOSED_STATES = ('Closed', 'Resolved')

#: the old session kinds that are a build in the kernel's words
BUILD_KINDS = ('task', 'fix-bug', 'correct', 'adjudicate', 'build')

#: the review ledger a kernel review session appends its verdict to (``state/<product>/``)
REVIEWS_FILE = 'kernel-reviews.jsonl'

#: where an ended review session's review file is kept (``state/<product>/``) before its
#: worktree is freed: ``<job>.md``
REVIEW_COPIES_DIR = 'kernel-reviews'

#: the operator's answers ledger (``state/<product>/``), one JSON object per line
ANSWERS_FILE = 'operator-answers.jsonl'


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def item_of_branch(branch):
    """The item id a branch names (``<prefix>/<ID>[-slug]``, any case), else ``None``."""
    m = ID_TOKEN_RE.search((branch or '').upper())
    return m.group(0) if m else None


# ---- the protocols ------------------------------------------------------------------------------

class RecordPort(typing.Protocol):
    def items(self) -> dict: ...                       # id -> Item
    def card_fields(self, item_id) -> dict: ...         # the kernel's machine keys as stored
    def specs_landed(self) -> dict: ...                 # Feature id -> spec text
    def answers(self) -> list: ...                      # [Answer]
    def reviews(self) -> list: ...                      # [Review] from the kernel's ledger
    def record_review(self, item_id, pr, tree_sha, verdict, findings) -> None: ...  # append
    def paused(self) -> bool: ...
    def write_fields(self, item_id, fields) -> None: ...  # merge into the machine block
    def mint_story(self, feature_id, story_id, title, acceptance) -> None: ...


class GitHubPort(typing.Protocol):
    def prs(self) -> list: ...                          # [PR], open and recently merged
    def reviews(self, prs) -> list: ...                 # [Review] GitHub holds on PR heads
    def enable_auto_merge(self, pr) -> None: ...
    def update_branch(self, pr) -> None: ...
    def rerun(self, run_id) -> None: ...


class SessionPort(typing.Protocol):
    def sessions(self) -> list: ...                     # [Session], every run not yet ended
    def launch(self, kind, item_id, branch, brief, meta) -> str: ...  # the job id
    def end(self, session, free_worktree) -> None: ...


class PortError(Exception):
    """A write a port could not do; the message is the reason the applier records."""


# ---- the record ---------------------------------------------------------------------------------

def item_from_card(rec):
    """One :class:`~asf.kernel.model.Item` from a ``load_items`` record."""
    meta = rec['meta']
    from asf.record import frontmatter
    _typed, machine = frontmatter.split_machine(meta)
    raw = machine.get(STATE)
    try:
        state = M.State(raw) if raw else None
    except ValueError:
        state = None
    if state is None:
        state = M.State.DONE if machine.get('state') in CLOSED_STATES else M.State.NEW
    stuck = None
    if state is M.State.STUCK:
        stuck = M.Stuck(str(machine.get(STUCK_REASON) or 'stuck'),
                        str(machine.get(STUCK_OWNER) or 'operator'),
                        str(machine.get(STUCK_NEXT) or ''))
    rank = meta.get('rank')
    return M.Item(
        id=meta.get('id'), type=str(meta.get('type') or 'task'), title=str(meta.get('title') or ''),
        parent=meta.get('parent'), rank=rank if isinstance(rank, int) else None,
        priority=meta.get('priority'), after=[str(a) for a in as_list(meta.get('after'))],
        writes=[str(w) for w in as_list(meta.get('writes'))], body=rec.get('body') or '',
        state=state, stuck=stuck, attempts=[str(a) for a in as_list(machine.get(ATTEMPTS))],
        fix_rounds=int(machine.get(FIX_ROUNDS) or 0),
        findings=[str(f) for f in as_list(machine.get(FINDINGS))],
        answers=[str(a) for a in as_list(machine.get(ANSWERS))],
        question=machine.get(QUESTION) or None, reopened=bool(machine.get(REOPENED)),
        notes=[str(n) for n in as_list(machine.get(NOTES))])


def _jsonl(path):
    try:
        with open(path, encoding='utf-8') as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


class RealRecord:
    """The product's record (``backlog_dir``), its trunk's specs (``repo_dir``/``specs_dir``)
    and its state directory's ledgers (answers, kernel reviews, the launch pause)."""

    def __init__(self, product, state_dir=None):
        from asf import env
        self.product = product
        self.root = product.backlog_dir
        self.state_dir = state_dir or os.path.join(env.ASF_HOME, 'state', product.name)
        self._cards = None

    def _load(self):
        if self._cards is None:
            by_id, _errors = load_items(self.root)
            self._cards, _dupes = canonicalize(by_id)
        return self._cards

    def items(self):
        out = {}
        for iid, rec in self._load().items():
            if rec['meta'].get('type') not in WORK_TYPES:
                continue
            it = item_from_card(rec)
            if is_retired(rec['meta']):  # on the record (never re-minted), invisible, finished
                it.state, it.stuck, it.priority = M.State.DONE, None, 'later'
            out[iid] = it
        return out

    def card_fields(self, item_id):
        rec = self._load().get(item_id)
        return {k: rec['meta'].get(k) for k in KERNEL_KEYS if k in rec['meta']} if rec else {}

    def specs_landed(self):
        """Every Feature spec on the trunk checkout: ``<specs_dir>/f-<n>.md`` -> ``F-<n>``."""
        conv = self.product.conventions
        d = os.path.join(self.product.repo_dir or '', conv.specs_dir)
        out = {}
        try:
            names = sorted(os.listdir(d))
        except OSError:
            return out
        for name in names:
            iid = item_of_branch(name[:-3]) if name.endswith('.md') else None
            if not iid or not self._is_feature(iid):
                continue
            try:
                with open(os.path.join(d, name), encoding='utf-8') as f:
                    out[iid] = f.read()
            except OSError:
                continue
        return out

    def _is_feature(self, iid):
        rec = self._load().get(iid)
        return rec is not None and rec['meta'].get('type') == 'feature'

    def answers(self):
        return [M.Answer(str(r['item']), str(r['text']))
                for r in _jsonl(os.path.join(self.state_dir, ANSWERS_FILE))
                if r.get('item') and r.get('text')]

    def reviews(self):
        return [M.Review(str(r['item']), str(r.get('tree_sha') or r['tree']), str(r['verdict']),
                         [str(f) for f in as_list(r.get('findings'))])
                for r in _jsonl(os.path.join(self.state_dir, REVIEWS_FILE))
                if r.get('item') and (r.get('tree_sha') or r.get('tree')) and r.get('verdict')]

    def record_review(self, item_id, pr, tree_sha, verdict, findings):
        """Append one verdict to the review ledger: ``{item, pr, tree_sha, verdict, findings,
        at}``."""
        os.makedirs(self.state_dir, exist_ok=True)
        line = json.dumps({'item': item_id, 'pr': pr, 'tree_sha': tree_sha, 'verdict': verdict,
                           'findings': list(findings), 'at': now_iso()}, sort_keys=True)
        with open(os.path.join(self.state_dir, REVIEWS_FILE), 'a', encoding='utf-8') as f:
            f.write(line + '\n')

    def paused(self):
        from asf import pause
        return pause.read(self.product.name) is not None

    def write_fields(self, item_id, fields):
        """Merge ``fields`` into the card's machine block (``None`` removes a key), in one
        whole-card write; the card's other lines stay as they were."""
        from asf.record import frontmatter, writer
        rec = self._load().get(item_id)
        if rec is None:
            raise PortError('%s: no card on the record' % item_id)
        with open(rec['path'], encoding='utf-8') as f:
            text = f.read()
        meta, body = frontmatter.parse(text, path=rec['relpath'])
        keys = set(getattr(meta, 'machine_keys', set()))
        for k, v in fields.items():
            if v is None or v == []:
                meta.pop(k, None)
                keys.discard(k)
            else:
                meta[k] = v
                keys.add(k)
        meta['updated'] = now_iso()
        keys.add('updated')
        meta.machine_keys = keys
        new_text = frontmatter.render(meta, body)
        writer.write_card(rec['path'], new_text)
        rec['meta'], rec['text'] = meta, new_text

    def mint_story(self, feature_id, story_id, title, acceptance):
        from asf.record.core import today
        from asf.record.ids import write_new_item
        cards = self._load()
        if story_id in cards:
            return
        if feature_id not in cards:
            raise PortError('%s: parent %s is not on the record' % (story_id, feature_id))
        write_new_item(self.root, cards, 'story', story_id, {'title': title, 'parent': feature_id},
                       '', today(), 'kernel: declared by the landed spec',
                       acceptance=list(acceptance), shape=('parent-feature', 'story'))

    def publish(self, message):
        """Commit and push what this tick wrote, through the record's one push."""
        from asf.record import publish
        publish.publish_changes(self.root, self._before, message)

    def snapshot(self):
        from asf.record import publish
        self._before = publish.snapshot(self.root)


# ---- GitHub -------------------------------------------------------------------------------------

#: the open-PR fields one ``gh pr list`` reads
PR_FIELDS = ('number', 'headRefName', 'headRefOid', 'mergeable', 'mergeStateStatus',
             'autoMergeRequest', 'files', 'statusCheckRollup', 'latestReviews')
_RUN_ID = re.compile(r'/actions/runs/(\d+)')
_TEST_MODULE = re.compile(r'^(?:FAIL|ERROR): \S+ \(([\w.]+)\)', re.M)


def _check(c):
    """One rollup entry -> :class:`~asf.kernel.model.Check` (a commit status reads as a check)."""
    status = str(c.get('status') or c.get('state') or 'completed').lower()
    conclusion = c.get('conclusion')
    if c.get('__typename') == 'StatusContext':  # a commit status: state is its conclusion
        conclusion, status = status, ('completed' if status not in ('pending', 'expected')
                                      else 'in_progress')
    m = _RUN_ID.search(c.get('detailsUrl') or c.get('targetUrl') or '')
    return M.Check(name=str(c.get('name') or c.get('context') or '?'), status=status,
                   conclusion=str(conclusion).lower() if conclusion else None,
                   run_id=int(m.group(1)) if m else None)


def failing_files(log, files):
    """The PR ``files`` a failed run's ``log`` names, plus each failing unittest's module file
    (``FAIL: test_x (pkg.test_mod.Case)`` -> ``pkg/test_mod.py``) — empty when nothing reads."""
    out = [f for f in files if f and f in log]
    for dotted in _TEST_MODULE.findall(log):
        parts = dotted.split('.')
        for n in range(len(parts), 0, -1):
            path = '/'.join(parts[:n]) + '.py'
            if path in files or n == len(parts) - 1:
                out.append(path)
                break
    return sorted(set(out))


class RealGitHub:
    """The product's repo (``repo_slug``) through :func:`asf.github.gh` with its ``auth_env``."""

    #: merged PRs read per tick: enough to see every recent landing
    MERGED_LIMIT = 100
    #: red runs whose logs are read per tick (the rest keep ``failing_files`` empty)
    LOG_LIMIT = 10

    def __init__(self, product, run=None):
        from asf import ci_pool
        self.product = product
        self.slug = product.repo_slug
        self._env = ci_pool._gh_env(product)
        self._run = run
        self._logs = 0

    def _gh(self, args, json=True):
        from asf import github
        return github.gh(args, json=json, env=self._env, run=self._run)

    def prs(self):
        r = self._gh(['pr', 'list', '-R', self.slug, '--state', 'open', '--limit', '200',
                      '--json', ','.join(PR_FIELDS)])
        if not r.ok:
            raise PortError('open PRs unreadable: %s' % r.reason)
        out = []
        for d in r.data or []:
            pr = self._open_pr(d)
            if pr is not None:
                out.append(pr)
        r = self._gh(['pr', 'list', '-R', self.slug, '--state', 'merged', '--limit',
                      str(self.MERGED_LIMIT), '--json', 'number,headRefName,headRefOid'])
        for d in (r.data or []) if r.ok else []:
            iid = item_of_branch(d.get('headRefName'))
            if iid:
                out.append(M.PR(number=d['number'], branch=d['headRefName'], item_id=iid,
                                head_sha=d.get('headRefOid') or '', merged=True))
        return out

    def _open_pr(self, d):
        iid = item_of_branch(d.get('headRefName'))
        if not iid:
            return None
        files = [f.get('path') for f in d.get('files') or [] if f.get('path')]
        pr = M.PR(number=d['number'], branch=d['headRefName'], item_id=iid,
                  head_sha=d.get('headRefOid') or '',
                  behind=d.get('mergeStateStatus') == 'BEHIND',
                  conflicting=d.get('mergeable') == 'CONFLICTING', files=files,
                  checks=[_check(c) for c in d.get('statusCheckRollup') or []],
                  auto_merge=bool(d.get('autoMergeRequest')))
        pr.tree_sha = self._tree(pr.head_sha)
        pr.latest_reviews = d.get('latestReviews') or []
        for c in pr.checks:
            if c.status == 'completed' and c.conclusion in M.RED_CONCLUSIONS and c.run_id:
                self._red_detail(c, files)
        return pr

    def _tree(self, sha):
        if not sha:
            return ''
        r = self._gh(['api', 'repos/%s/git/commits/%s' % (self.slug, sha)])
        return ((r.data or {}).get('tree') or {}).get('sha', '') if r.ok else ''

    def _red_detail(self, check, files):
        r = self._gh(['api', 'repos/%s/actions/runs/%d' % (self.slug, check.run_id)])
        if r.ok and isinstance(r.data, dict):
            check.attempt = int(r.data.get('run_attempt') or 1)
        if self._logs >= self.LOG_LIMIT:
            return
        self._logs += 1
        log = self._gh(['run', 'view', str(check.run_id), '-R', self.slug, '--log-failed'],
                       json=False)
        if log.ok:
            check.failing_files = failing_files(log.data or '', files)

    def reviews(self, prs):
        """GitHub's own reviews on each open PR's current head: ``APPROVED`` / ``CHANGES_REQUESTED``
        on the head commit is a verdict on the head's tree; plus the old floor's standing
        approvals (:meth:`floor_approvals`)."""
        out = self.floor_approvals(prs)
        for pr in prs:
            for rv in getattr(pr, 'latest_reviews', None) or []:
                verdict = {'APPROVED': 'approve', 'CHANGES_REQUESTED': 'changes'}.get(rv.get('state'))
                oid = (rv.get('commit') or {}).get('oid')
                if verdict and pr.tree_sha and oid == pr.head_sha:
                    body = (rv.get('body') or '').strip()
                    out.append(M.Review(pr.item_id, pr.tree_sha, verdict, [body] if body else []))
        return out

    def floor_approvals(self, prs):
        """The old floor's approvals still standing: the newest review of the PR's item on its
        branch or in the review store (:func:`asf.evidence.review.review_at`, read-only on the
        product's checkout) that is approved and names the PR's head counts as an ``approve`` on
        the head's tree. Its ``changes`` verdicts are not imported."""
        from asf.evidence import review, review_store
        repo = getattr(self.product, 'repo_dir', None)
        if not repo or not os.path.isdir(repo):
            return []
        store, out = review_store.root(self.product), []
        for pr in prs:
            if not (pr.head_sha and pr.tree_sha):
                continue
            try:
                rv = review.review_at(repo, self.product.conventions, 'origin/' + pr.branch,
                                      pr.item_id, store=store)
            except Exception:  # noqa: BLE001 — an unreadable old review is no verdict
                continue
            head = str((rv or {}).get('head') or '').lower()
            if not (rv and rv.get('verdict') == review.APPROVED and len(head) >= 7):
                continue
            if pr.head_sha.lower().startswith(head) or \
                    (review._git(repo, 'rev-parse', head + '^{tree}') or '').strip() == pr.tree_sha:
                out.append(M.Review(pr.item_id, pr.tree_sha, 'approve', []))
        return out

    def _write(self, args, what):
        r = self._gh(args, json=False)
        if not r.ok:
            raise PortError('%s: %s' % (what, r.reason))

    def enable_auto_merge(self, pr):
        merge = self.product.conventions.get('merge')
        method = (merge.get('method') if isinstance(merge, dict) else None) or 'squash'
        self._write(['pr', 'merge', str(pr), '-R', self.slug, '--auto', '--' + method],
                    'auto-merge #%d' % pr)

    def update_branch(self, pr):
        self._write(['api', '-X', 'PUT', 'repos/%s/pulls/%d/update-branch' % (self.slug, pr)],
                    'update-branch #%d' % pr)

    def rerun(self, run_id):
        self._write(['run', 'rerun', str(run_id), '-R', self.slug, '--failed'],
                    'rerun %d' % run_id)


# ---- sessions -----------------------------------------------------------------------------------

def _kernel_kind(kind):
    kind = str(kind or 'build')
    return 'build' if kind in BUILD_KINDS else kind


class RealSessions:
    """The product's session ledger (``state/<product>/sessions.jsonl``) and the launch."""

    def __init__(self, product, cfg=None):
        self.product = product
        self._cfg = cfg

    def cfg(self):
        if self._cfg is None:
            from asf.workers import spawn
            self._cfg = spawn.load_cfg()
        return self._cfg

    def sessions(self):
        from asf.workers import lifecycle, pool, pushlog, runtime
        out = []
        for run in pool.load_sessions(self.product).values():
            if not lifecycle.is_live(run) or not run.get('item'):
                continue
            kind = _kernel_kind(run.get('kind'))
            alive = lifecycle.pid_alive(run.get('pid'))
            result = None if alive else runtime.read_result(run.get('log'))
            pushed = not alive and pushlog.count(self.product, run['job']) > 0
            said = reports.read(result)
            out.append(M.Session(
                job=run['job'], item_id=run['item'], kind=kind, pid=run.get('pid'), alive=alive,
                ended=result is not None, result=session_result(kind, pushed, said),
                question=said['question'] or None, last_line=said['last_line'],
                report=str((result or {}).get('result') or ''),
                pr=run.get('kernel_pr'), tree_sha=run.get('kernel_tree') or '',
                worktree=run.get('worktree') or '', branch=run.get('branch') or '',
                status=said['status'],
                fields=said['fields'], api_error=said['api_error']))
            out[-1].started = run.get('started') or ''
        return out

    def _account(self):
        from asf.workers import lifecycle, pool
        accounts = pool.accounts_from_config(self.cfg())
        live = {}
        for run in pool.load_sessions(self.product).values():
            if lifecycle.is_live(run):
                live[run.get('account')] = live.get(run.get('account'), 0) + 1
        for acct in accounts:
            if live.get(acct.name, 0) < acct.cap:
                return acct
        raise PortError('no account with a free seat')

    def launch(self, kind, item_id, branch, brief, meta=None):
        """Spawn one session with ``brief`` (an :class:`asf.briefs.build.Brief`: its text, model
        and grants); ``meta`` (``pr``, ``tree``) goes on the session's ledger row."""
        from asf.workers import pool, spawn
        acct = self._account()
        job = '%s-%s-%d' % (kind, item_id.lower(), int(time.time()))
        row = pool.Row(job, item_id, kind='task' if kind == 'build' else kind, branch=branch,
                       title=item_id, model=getattr(brief, 'model', '') or '',
                       add_dirs=getattr(brief, 'add_dirs', ()) or (),
                       card_digest=getattr(brief, 'card_digest', '') or '')
        runtime = None
        if acct.role == 'cloud':
            from asf.workers import cloud  # the cloud runtime: remote.RemoteRuntime or actions
            runtime = cloud.lane_runtime(cloud.settings(self.cfg(), self.product), self.product)
        try:
            # no heartbeat: the kernel judges liveness by pid and REPORT, and the beat loop's
            # writes under the shared .git are what a sandboxed session is refused (B-0098)
            spawn.spawn(self.product, row, acct, getattr(brief, 'text', brief), runtime=runtime,
                        cfg=self.cfg(), heartbeat=False)
        except spawn.SpawnError as e:
            raise PortError('launch: %s' % e) from None
        if meta:
            pool.update_session(self.product, job, kernel_pr=meta.get('pr'),
                                kernel_tree=meta.get('tree'))
        return job

    def push_rebase(self, session, sha):
        """The safety net for a session that reported ``pushed: rebased <sha>`` and stopped
        (B-82658, B-83312): when its worktree still holds ``sha`` as HEAD, push it once to the
        session's branch with ``--force-with-lease`` over origin's head read just before (a push
        that would erase a commit origin holds is refused), and record the sha on the session's
        push log. Returns a note; raises :class:`PortError` when it cannot or will not push."""
        from asf import gitops, refguard
        from asf.harvest import harvest
        from asf.workers import pushlog
        wt, branch = session.worktree, session.branch
        if not wt or not os.path.isdir(wt):
            raise PortError('rebased %s: no worktree to push from' % sha)
        head = gitops.git(['rev-parse', 'HEAD'], wt, timeout=60)
        if not head.ok or not head.data.startswith(sha.lower()):
            raise PortError('rebased %s: the worktree HEAD is %s' % (
                sha, (head.data[:12] if head.ok else 'unreadable')))
        full = head.data
        if not branch:
            got = gitops.git(['rev-parse', '--abbrev-ref', 'HEAD'], wt, timeout=60)
            branch = got.data if got.ok and got.data != 'HEAD' else ''
        if not branch:
            raise PortError('rebased %s: no branch to push to' % sha)
        if full in pushlog.shas(self.product, session.job):
            return 'rebased %s already pushed' % full[:12]
        expected = harvest.remote_head(wt, branch)
        if expected == full:
            return 'origin/%s already at %s' % (branch, full[:12])
        ok, why = harvest.push_branch(wt, full, branch, expected, self.product.main,
                                      refguard.listed(self.product.conventions))
        if not ok:
            raise PortError('rebased %s: %s' % (sha, why))
        log = pushlog.path(self.product, session.job)
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, 'a', encoding='utf-8') as f:
            f.write(full + '\n')
        return 'pushed rebased %s to %s (--force-with-lease)' % (full[:12], branch)

    def end(self, session, free_worktree):
        from asf import env
        from asf.workers import lifecycle, pool, trash
        reason = (lifecycle.FINISHED if session.result in ('pushed', 'report')
                  else lifecycle.DEAD_PID if not session.ended else lifecycle.NOT_PUSHED)
        pool.update_session(self.product, session.job, ended=now_iso(), end_reason=reason)
        wt = session.worktree
        state = env.state_dir(self.product)
        if free_worktree and wt and os.path.isdir(wt) and _under(wt, state):
            clean = True
            if session.kind == 'review':
                clean = not keep_review_files(wt, self.product.conventions.reviews_dir,
                                              os.path.join(state, REVIEW_COPIES_DIR), session.job)
            ok, why = trash.discard(self.product.repo_dir, state, wt, check_clean=clean)
            if not ok:
                raise PortError('worktree kept: %s' % why)


def dirty_paths(porcelain):
    """The paths ``git status --porcelain`` output names (both sides of a rename)."""
    out = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        for path in line[3:].split(' -> '):
            path = path.strip()
            if len(path) >= 2 and path[0] == path[-1] == '"':
                path = path[1:-1]
            out.append(path)
    return out


def keep_review_files(worktree, reviews_dir, dest_dir, job):
    """An ended review session's worktree whose only uncommitted or untracked files are under
    ``reviews_dir``: copy each of them into ``dest_dir`` (``<job>.md``, then ``<job>-2.md`` …) and
    return True — the worktree may be freed. False (nothing copied) when the tree is clean, is
    not a git worktree, or holds any other dirty file: it is kept to the clean check."""
    import shutil
    from asf import gitops
    st = gitops.git(['status', '--porcelain', '--untracked-files=all'], worktree, timeout=60)
    if not st.ok:
        return False
    paths = dirty_paths(st.stdout)
    root = str(reviews_dir or '').strip('/') + '/'
    if root == '/' or not paths or not all(p.startswith(root) for p in paths):
        return False
    os.makedirs(dest_dir, exist_ok=True)
    n = 0
    for path in sorted(set(paths)):
        src = os.path.join(worktree, path)
        if not os.path.isfile(src):
            continue  # a deleted review file: nothing to keep
        n += 1
        shutil.copyfile(src, os.path.join(dest_dir, '%s%s.md' % (job, '' if n == 1 else '-%d' % n)))
    return True


def session_result(kind, pushed, said):
    """How a session ended (:data:`asf.kernel.model.RESULTS`) from its push and what it said
    (:func:`asf.kernel.reports.read`): a push wins; a review reports; then a question, a REPORT,
    or none."""
    if pushed:
        return 'pushed'
    if kind == 'review':
        return 'report'
    if said['question']:
        return 'question'
    return 'report' if said['fields'] else 'none'


def _under(path, parent):
    path, parent = os.path.realpath(path), os.path.realpath(parent)
    return path.startswith(parent.rstrip(os.sep) + os.sep)


# ---- the product's ports and knobs --------------------------------------------------------------

class Ports:
    """The three ports one tick uses, and the brief maker ``brief(item, launch, findings, pr)``
    a launch hands its session (:class:`asf.kernel.briefs.Briefer` on the real ports)."""

    def __init__(self, record, github, sessions, brief=None):
        self.record, self.github, self.sessions, self.brief = record, github, sessions, brief


def real_ports(product):
    from asf.kernel.briefs import Briefer
    return Ports(RealRecord(product), RealGitHub(product), RealSessions(product),
                 Briefer(product))


def config_for(product, cfg=None):
    """The :class:`~asf.kernel.model.Config` of ``product``: branch prefixes and document roots
    from its conventions; ``max_sessions``, ``rank`` and the idle alarm from its ``kernel:``
    block (:mod:`asf.kernel.settings`, each with its documented default)."""
    conv = product.conventions
    k = product.kernel
    return M.Config(
        doc_branches=(conv.prefix('spec'), conv.prefix('plan')),
        doc_paths=tuple('%s/**' % d.rstrip('/') for d in (conv.specs_dir, conv.plans_dir,
                                                          conv.reviews_dir)),
        work_branch=conv.prefix('code'), fix_branch=conv.prefix('fix'),
        max_sessions=int(k['launch']['max_sessions']), rank=k['launch']['rank'],
        idle_alarm=bool(k['idle_alarm']['enabled']),
        idle_min_free=int(k['idle_alarm']['min_free_seats']))

