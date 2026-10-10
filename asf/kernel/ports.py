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
- ``kernel_extra_rounds``: fix rounds granted beyond ``max_fix_rounds`` — one per operator
  answer to a Stuck at the fix-round cap (or a conflict its rebase session could not resolve);
  the item's cap is ``max_fix_rounds`` + this.
- ``kernel_attempts``: the reason of each failed attempt, oldest first.
- ``kernel_findings``: the review findings handed to the next fix round.
- ``kernel_answers``: the operator answers already applied; ``kernel_question``: the open one.
- ``kernel_reopened``: ``true`` when a Done item was reopened (a new PR is accepted).
- ``kernel_notes``: the questions a session asked while its work moved on anyway (a ``done``
  REPORT with a pushed head) — shown by ``asf kernel status``, holding nothing.
"""
import datetime
import hashlib
import json
import os
import re
import time
import typing

from asf.kernel import model as M
from asf.kernel import reports
from asf.record.core import ID_TOKEN_RE, as_list, canonicalize, is_retired, load_items
from asf.workers import cloudpid

#: the machine-block keys the kernel owns (see the module docstring)
STATE, ATTEMPTS, FIX_ROUNDS = 'kernel_state', 'kernel_attempts', 'kernel_fix_rounds'
FINDINGS, ANSWERS, QUESTION = 'kernel_findings', 'kernel_answers', 'kernel_question'
STUCK_REASON, STUCK_OWNER = 'kernel_stuck_reason', 'kernel_stuck_owner'
STUCK_NEXT, STUCK_SINCE, REOPENED = 'kernel_stuck_next', 'kernel_stuck_since', 'kernel_reopened'
NOTES, EXTRA_ROUNDS = 'kernel_notes', 'kernel_extra_rounds'
KERNEL_KEYS = (STATE, STUCK_REASON, STUCK_OWNER, STUCK_NEXT, STUCK_SINCE, FIX_ROUNDS, ATTEMPTS,
               FINDINGS, ANSWERS, QUESTION, REOPENED, NOTES, EXTRA_ROUNDS)

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
    def record_review(self, item_id, pr, tree_sha, verdict, findings,
                      change_id='') -> None: ...  # append
    def paused(self) -> bool: ...
    def write_fields(self, item_id, fields) -> None: ...  # merge into the machine block
    def mint_story(self, feature_id, story_id, title, acceptance) -> None: ...


class GitHubPort(typing.Protocol):
    def prs(self) -> list: ...                          # [PR], open and recently merged
    def reviews(self, prs) -> list: ...                 # [Review] GitHub holds on PR heads
    def enable_auto_merge(self, pr) -> None: ...
    def update_branch(self, pr) -> None: ...
    def rerun(self, run_id) -> None: ...
    def branches(self) -> list: ...                     # [Branch] under the work prefixes
    def open_pr(self, branch, base, title, body) -> int: ...  # the PR number (new or existing)


class SessionPort(typing.Protocol):
    def sessions(self) -> list: ...                     # [Session], every run not yet ended
    def launch(self, kind, item_id, branch, brief, meta) -> str: ...  # the job id
    def end(self, session, free_worktree) -> None: ...


class PortError(Exception):
    """A write a port could not do; the message is the reason the applier records."""


class NoSeat(PortError):
    """A launch with no seat on its lane (:meth:`RealSessions.lane`): ``max_sessions`` pools the
    local and cloud seats, so a local-only launch (a review, a rebase round) can meet a full local
    lane while cloud seats are free. It waits for a seat — the applier records no attempt."""


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
        extra_rounds=int(machine.get(EXTRA_ROUNDS) or 0),
        findings=[str(f) for f in as_list(machine.get(FINDINGS))],
        answers=[str(a) for a in as_list(machine.get(ANSWERS))],
        question=machine.get(QUESTION) or None, reopened=bool(machine.get(REOPENED)),
        notes=[str(n) for n in as_list(machine.get(NOTES))],
        stuck_since=(str(machine.get(STUCK_SINCE)) if stuck is not None and machine.get(STUCK_SINCE)
                     else None))


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
        return [M.Answer(str(r['item']), str(r['text']), str(r.get('at') or ''))
                for r in _jsonl(os.path.join(self.state_dir, ANSWERS_FILE))
                if r.get('item') and r.get('text')]

    def reviews(self):
        return [M.Review(str(r['item']), str(r.get('tree_sha') or r['tree']), str(r['verdict']),
                         [str(f) for f in as_list(r.get('findings'))],
                         str(r.get('change_id') or ''))
                for r in _jsonl(os.path.join(self.state_dir, REVIEWS_FILE))
                if r.get('item') and (r.get('tree_sha') or r.get('tree')) and r.get('verdict')]

    def record_review(self, item_id, pr, tree_sha, verdict, findings, change_id=''):
        """Append one verdict to the review ledger: ``{item, pr, tree_sha, change_id, verdict,
        findings, at}``. A row from before ``change_id`` was kept (or with it '') matches on its
        tree alone."""
        os.makedirs(self.state_dir, exist_ok=True)
        row = {'item': item_id, 'pr': pr, 'tree_sha': tree_sha, 'verdict': verdict,
               'findings': list(findings), 'at': now_iso()}
        if change_id:
            row['change_id'] = change_id
        line = json.dumps(row, sort_keys=True)
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
PR_FIELDS = ('number', 'headRefName', 'headRefOid', 'baseRefName', 'mergeable',
             'mergeStateStatus', 'autoMergeRequest', 'files', 'statusCheckRollup',
             'latestReviews')
_HUNK = re.compile(r'^@@[^@]*@@')


def change_id(files):
    """The PR's own change as one stable hash, from the compare API's ``files`` (merge base ->
    head): each file's name, old name, status and patch with the hunk headers' line numbers
    dropped (as a patch-id ignores them), in name order; a file with no patch (binary, too large)
    counts by its blob sha. A merge of trunk into the branch moves the merge base and the line
    numbers, never this; a new commit on the PR changes it. '' for no files."""
    rows = []
    for f in sorted(files or [], key=lambda f: str(f.get('filename') or '')):
        patch = f.get('patch')
        body = ('\n'.join(_HUNK.sub('@@', line) for line in patch.splitlines())
                if patch is not None else 'blob:%s' % (f.get('sha') or ''))
        rows.append('\0'.join((str(f.get('filename') or ''), str(f.get('previous_filename') or ''),
                                str(f.get('status') or ''), body)))
    if not rows:
        return ''
    return hashlib.sha256('\0\0'.join(rows).encode('utf-8')).hexdigest()


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


_LOG_STAMP = re.compile(r'^\ufeff?\d{4}-\d\d-\d\dT[\d:.]+Z ?')
_ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')

#: the lines of a failed log's tail a red's fix round carries
LOG_TAIL_LINES = 30


def failed_step(log, lines=LOG_TAIL_LINES):
    """``(step, tail)`` of a ``gh run view --log-failed`` ``log`` (``job<TAB>step<TAB>line``):
    the step of its last line and that step's last ``lines`` lines (timestamps and colour codes
    dropped); ``('', '')`` when nothing reads."""
    rows = [r.split('\t', 2) for r in str(log or '').splitlines()]
    rows = [r for r in rows if len(r) == 3]
    if not rows:
        return '', ''
    step = rows[-1][1].strip()
    body = [_ANSI.sub('', _LOG_STAMP.sub('', r[2])).rstrip() for r in rows if r[1].strip() == step]
    body = [b for b in body if b.strip()]
    return step, '\n'.join(body[-lines:])


def required_from_rules(rules):
    """The check names of every ``required_status_checks`` rule in a branch-rules listing."""
    out = []
    for r in rules or ():
        if not isinstance(r, dict) or r.get('type') != 'required_status_checks':
            continue
        for c in (r.get('parameters') or {}).get('required_status_checks') or ():
            name = c.get('context') if isinstance(c, dict) else None
            if name and name not in out:
                out.append(str(name))
    return tuple(out)


class RealGitHub:
    """The product's repo (``repo_slug``) through :func:`asf.github.gh` with its ``auth_env``."""

    #: merged PRs read per tick: enough to see every recent landing
    MERGED_LIMIT = 100
    #: red runs whose logs are read per tick (the rest keep ``failing_files`` empty)
    LOG_LIMIT = 10
    #: the compare API's file cap: a change listing this many files may be cut short
    COMPARE_FILES_CAP = 300

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
                  conflicting=(d.get('mergeable') == 'CONFLICTING'
                               or d.get('mergeStateStatus') == 'DIRTY'), files=files,
                  checks=[_check(c) for c in d.get('statusCheckRollup') or []],
                  auto_merge=bool(d.get('autoMergeRequest')))
        pr.tree_sha = self._tree(pr.head_sha)
        pr.change_id = self._change(d.get('baseRefName') or self.product.main, pr.head_sha)
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

    #: (repo, base, head sha) -> change id: a head's merge base with trunk never moves, so
    #: neither does its change (one compare call per new head, not per tick)
    _changes = {}

    def _change(self, base, head):
        """The PR's own change at ``head`` (:func:`change_id` over ``compare/<base>...<head>``);
        '' when unreadable or cut short (the verdict then keys by tree alone)."""
        if not (base and head):
            return ''
        key = (self.slug, base, head)
        if key in self._changes:
            return self._changes[key]
        r = self._gh(['api', 'repos/%s/compare/%s...%s' % (self.slug, base, head)])
        if not r.ok or not isinstance(r.data, dict):
            return ''
        files = r.data.get('files')
        if not isinstance(files, list) or len(files) >= self.COMPARE_FILES_CAP:
            return ''  # GitHub lists at most 300 files: a longer change cannot be hashed whole
        cid = change_id(files)
        if len(self._changes) >= 4096:
            self._changes.clear()
        self._changes[key] = cid
        return cid

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
            check.failed_step, check.log_tail = failed_step(log.data or '')

    def required_checks(self, branch=None):
        """The check names GitHub's rules require on ``branch`` (the trunk by default): every
        ``required_status_checks`` rule of ``rules/branches/<branch>``; ``()`` when unreadable."""
        branch = branch or self.product.main
        if not (self.slug and branch):
            return ()
        r = self._gh(['api', 'repos/%s/rules/branches/%s' % (self.slug, branch)])
        if not r.ok or not isinstance(r.data, list):
            return ()
        return required_from_rules(r.data)

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
                    out.append(M.Review(pr.item_id, pr.tree_sha, verdict, [body] if body else [],
                                        pr.change_id))
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

    def _prefixes(self):
        conv = self.product.conventions
        return sorted({p for p in (conv.prefix('code'), conv.prefix('fix')) if p})

    def branches(self):
        """Every branch on origin under the kernel's work prefixes (``code``, ``fix``) that names
        an item: ``git/matching-refs/heads/<prefix>`` — what ``ls-remote`` would list."""
        out = []
        for prefix in self._prefixes():
            r = self._gh(['api', 'repos/%s/git/matching-refs/heads/%s' % (self.slug, prefix)])
            if not r.ok or not isinstance(r.data, list):
                continue  # unknown is no pushed branch: nothing is opened on a guess
            for d in r.data or []:
                name = str((d or {}).get('ref') or '')[len('refs/heads/'):]
                iid = item_of_branch(name)
                if name and iid:
                    out.append(M.Branch(name, iid, str((d.get('object') or {}).get('sha') or '')))
        return out

    def _pr_of(self, branch):
        r = self._gh(['pr', 'list', '-R', self.slug, '--head', branch, '--state', 'open',
                      '--json', 'number'])
        return int(r.data[0]['number']) if r.ok and r.data else None

    def open_pr(self, branch, base, title, body):
        """Open the PR of ``branch`` against ``base`` (the trunk when empty); a PR already open
        for the branch is returned, not duplicated. Returns the PR number."""
        number = self._pr_of(branch)
        if number is not None:
            return number
        r = self._gh(['pr', 'create', '-R', self.slug, '--base', base or self.product.main,
                      '--head', branch, '--title', title, '--body', body], json=False)
        m = re.search(r'/pull/(\d+)', '%s\n%s' % (r.stdout or '', r.stderr or ''))
        if m and (r.ok or 'already exists' in (r.stderr or '')):
            return int(m.group(1))
        number = self._pr_of(branch)
        if number is not None:
            return number
        raise PortError('open PR %s: %s' % (branch, r.reason or 'no PR number in the reply'))


# ---- sessions -----------------------------------------------------------------------------------

def _kernel_kind(kind):
    kind = str(kind or 'build')
    return 'build' if kind in BUILD_KINDS else kind


class RealSessions:
    """The product's session ledger (``state/<product>/sessions.jsonl``) and the launch."""

    def __init__(self, product, cfg=None, log=None):
        self.product = product
        self._cfg = cfg
        self.log = log or _quiet
        self._creates = 0   # cloud launches tried by this port (one tick)

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
            alive = lifecycle.pid_alive(run.get('pid'))  # a cloud token: the remote run's status
            result = None if alive else report_result(run.get('log'))
            in_cloud = cloudpid.is_token(run.get('pid'))
            pushed = not alive and pushlog.count(self.product, run['job']) > 0
            said = reports.read(result)
            unpushed, refused = '', ''
            if result is not None and kind != 'review' and not pushed \
                    and (said['status'] == reports.DONE or run.get('kernel_pr') is not None):
                unpushed, refused = unpushed_head(
                    run.get('worktree') or '', run.get('branch') or '', self.product.main,
                    refguard_listed(self.product))
            out.append(M.Session(
                job=run['job'], item_id=run['item'], kind=kind, pid=run.get('pid'), alive=alive,
                # a cloud run the remote status calls over has ended: it never reads as a dead pid
                ended=result is not None or (in_cloud and not alive),
                result=session_result(kind, pushed, said), cloud=in_cloud,
                question=said['question'] or None, last_line=said['last_line'],
                report=str((result or {}).get('result') or ''),
                pr=run.get('kernel_pr'), tree_sha=run.get('kernel_tree') or '',
                change_id=run.get('kernel_change') or '',
                worktree=run.get('worktree') or '', branch=run.get('branch') or '',
                status=said['status'],
                fields=said['fields'], api_error=said['api_error'],
                unpushed=unpushed, push_refused=refused))
            out[-1].started = run.get('started') or ''
        return out

    def stranded(self, item_id):
        """The last ended (non-review) session of ``item_id`` whose kept worktree still holds a
        HEAD origin lacks — the rebase a refused force-push left — as a :class:`M.Session` with
        ``unpushed``/``push_refused`` read (:func:`unpushed_head`), else None."""
        from asf.workers import lifecycle, pool
        rows = [r for r in pool.load_sessions(self.product).values()
                if r.get('item') == item_id and not lifecycle.is_live(r)
                and _kernel_kind(r.get('kind')) != 'review' and r.get('worktree')
                and os.path.isdir(r.get('worktree'))]
        if not rows:
            return None
        run = rows[-1]  # the ledger is in launch order: the last one ended
        unpushed, refused = unpushed_head(run['worktree'], run.get('branch') or '',
                                          self.product.main, refguard_listed(self.product))
        if not unpushed:
            return None
        return M.Session(job=run['job'], item_id=item_id, kind=_kernel_kind(run.get('kind')),
                         pid=run.get('pid'), alive=False, ended=True, result='none',
                         worktree=run['worktree'], branch=run.get('branch') or '',
                         pr=run.get('kernel_pr'), unpushed=unpushed, push_refused=refused)

    def _live(self):
        """``(local live, cloud live, {account: live})`` over the ledger's live runs."""
        from asf.workers import cloud, lifecycle, pool
        local = in_cloud = 0
        per = {}
        for run in pool.load_sessions(self.product).values():
            if lifecycle.is_live(run):
                per[run.get('account')] = per.get(run.get('account'), 0) + 1
                if cloud.is_cloud(run):
                    in_cloud += 1
                else:
                    local += 1
        return local, in_cloud, per

    def _account(self, lane_settings=None):
        """A local account with a free seat; with ``lane_settings``, the cloud lane's account
        (:func:`asf.workers.cloud.lane_accounts`) with the most free seats — a routine runs on
        its account and counts in that account's cap like any session."""
        from asf.workers import cloud, pool
        accounts = pool.accounts_from_config(self.cfg())
        live = self._live()[2]
        if lane_settings is not None:
            free = [(a.cap - live.get(a.name, 0), -i, a)
                    for i, a in enumerate(cloud.lane_accounts(accounts, lane_settings))
                    if live.get(a.name, 0) < a.cap]
            if not free:
                raise PortError('no cloud account with a free seat')
            return max(free, key=lambda t: t[:2])[2]
        for acct in accounts:
            if live.get(acct.name, 0) < acct.cap:
                return acct
        raise PortError('no account with a free seat')

    def lane(self, kind, meta=None):
        """``'cloud'`` or ``'local'``: the seat a ``kind`` launch takes. A launch that needs the
        host (``meta['host']``: a rebase round, which the host publishes from its worktree; a
        kind :func:`asf.workers.cloud.local_only` keeps here) takes a local seat; every other one
        a cloud seat — if its brief kind is in ``kernel.launch.cloud_kinds`` (a review would push a
        report commit and restart the PR's CI, so reviews stay local) — while the lane has one (``kernel.launch.cloud_max``), its creates this tick
        are under ``cloud.max_creates_per_tick`` and its fallback breaker has not tripped; else a
        local seat (``kernel.launch.local_max``). Raises :class:`NoSeat` when neither has one."""
        from asf.workers import cloud
        local_max, cloud_max = lane_seats(self.product, self.cfg())
        local, in_cloud, _ = self._live()
        s = cloud_lane(self.product, self.cfg()) if cloud_max else None
        host = bool((meta or {}).get('host')) or (
            s is not None and cloud.local_only(_KindRow(kind), s))
        allowed = self.product.kernel['launch']['cloud_kinds']
        if s is None or host or kind not in allowed:
            why = ('the cloud lane is off' if s is None else 'the launch needs the host' if host
                   else '%s is not in kernel.launch.cloud_kinds' % kind)
        elif in_cloud >= cloud_max:
            why = 'cloud seats %d/%d' % (in_cloud, cloud_max)
        elif s.max_creates_per_tick and self._creates >= s.max_creates_per_tick:
            why = 'cloud creates this tick %d/%d' % (self._creates, s.max_creates_per_tick)
        else:
            why = cloud.Breaker(self.product, s).tripped()
        if not why:
            return 'cloud'
        if local < local_max:
            return 'local'
        raise NoSeat('no free seat: local %d/%d, cloud %d/%d (%s)'
                        % (local, local_max, in_cloud, cloud_max, why))

    def launch(self, kind, item_id, branch, brief, meta=None):
        """Spawn one session with ``brief`` (an :class:`asf.briefs.build.Brief`: its text, model
        and grants) on the seat :meth:`lane` picks; ``meta`` (``pr``, ``tree``, ``change``) goes on
        the session's ledger row. A cloud launch goes through the lane's runtime (the old floor's
        remote trigger: :class:`asf.workers.remote.RemoteRuntime`); one that fails counts on the
        lane's breaker and falls back to a local seat when there is one."""
        from asf.workers import cloud
        job = '%s-%s-%d' % (kind, item_id.lower(), int(time.time()))
        bk = getattr(brief, 'kind', None)
        bk = bk.replace('_', '-') if isinstance(bk, str) and bk else (
            'coder' if kind == 'build' else kind)
        if self.lane(bk, meta) == 'cloud':
            s = cloud_lane(self.product, self.cfg())
            self._creates += 1
            try:
                self._spawn(kind, item_id, branch, brief, job, self._account(s),
                            cloud.lane_runtime(s, self.product))
                cloud.Breaker(self.product, s).ok()
            except PortError as e:
                cloud.Breaker(self.product, s).fail(str(e))
                if self._live()[0] >= lane_seats(self.product, self.cfg())[0]:
                    raise
                self.log('%s %s — cloud launch failed (%s): a local seat instead'
                         % (job, item_id, e))
                job += '-local'
                self._spawn(kind, item_id, branch, brief, job, self._account())
        else:
            acct = self._account()
            runtime = None
            if acct.role == 'cloud':  # the old floor's lane account: its runtime is the cloud one
                runtime = cloud.lane_runtime(cloud.settings(self.cfg(), self.product),
                                             self.product)
            self._spawn(kind, item_id, branch, brief, job, acct, runtime)
        if meta and any(meta.get(k) is not None for k in ('pr', 'tree', 'change')):
            from asf.workers import pool
            pool.update_session(self.product, job, kernel_pr=meta.get('pr'),
                                kernel_tree=meta.get('tree'), kernel_change=meta.get('change'))
        return job

    def _spawn(self, kind, item_id, branch, brief, job, acct, runtime=None):
        from asf.workers import pool, spawn
        row = pool.Row(job, item_id, kind='task' if kind == 'build' else kind, branch=branch,
                       title=item_id, model=getattr(brief, 'model', '') or '',
                       add_dirs=getattr(brief, 'add_dirs', ()) or (),
                       card_digest=getattr(brief, 'card_digest', '') or '')
        try:
            # no heartbeat: the kernel judges liveness by pid (a cloud run: its remote status) and
            # REPORT, and the beat loop's writes under the shared .git are what a sandboxed
            # session is refused (B-0098)
            spawn.spawn(self.product, row, acct, getattr(brief, 'text', brief), runtime=runtime,
                        cfg=self.cfg(), heartbeat=False)
        except spawn.SpawnError as e:
            raise PortError('launch: %s' % e) from None

    def sync(self, out=None):
        """Bring the product's live cloud runs up to date (:func:`asf.workers.cloud.sync`: the
        remote run's status, its report commit on the branch) before the tick reads them — a
        cloud session's liveness and REPORT come from there. Nothing is read while the ledger
        holds no live cloud run."""
        from asf.workers import cloud, lifecycle, pool
        if not any(lifecycle.is_live(r) and cloud.is_cloud(r)
                   for r in pool.load_sessions(self.product).values()):
            return []
        return cloud.sync(self.product, self.cfg(), out=out or self.log)

    def push_rebase(self, session, sha):
        """The safety net for a session that reported ``pushed: rebased <sha>``, or ended
        ``done`` with commits origin lacks (``Session.unpushed``), and stopped (B-82658, B-83312,
        T-0196): when its worktree still holds ``sha`` as HEAD, push it once to the session's
        branch with ``--force-with-lease=<branch>:<origin's head read just before>`` — only when
        that head is in the branch's own history (:func:`overwritable`; a push that would erase a
        commit the local history never held is refused) — and record the sha on the session's
        push log. Returns a note; raises :class:`PortError` when it cannot or will not push."""
        from asf import gitops, gitpush, refguard
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
        main, protected = self.product.main, refguard_listed(self.product)
        why = (refguard.refusal(branch, 'push branch %s' % branch, main, protected, out=_quiet)
               or overwritable(wt, full, expected, branch))
        if why:
            raise PortError('rebased %s: %s' % (sha, why))
        push = gitpush.push(['--force-with-lease=refs/heads/%s:%s' % (branch, expected), 'origin',
                             '%s:refs/heads/%s' % (full, branch)], wt,
                            guard=refguard.Guard(main, protected))
        if push.returncode != 0:
            tail = ((push.stderr or push.stdout or '').strip().splitlines() or [''])[-1]
            raise PortError('rebased %s: push branch failed: %s' % (sha, tail))
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
        extra = {}
        if cloudpid.is_token(session.pid):  # never a dead pid: the remote status ended it
            if reason == lifecycle.DEAD_PID:
                reason = lifecycle.NOT_PUSHED
            extra['cloud_why'] = cloudpid.why(session.pid) or None
        pool.update_session(self.product, session.job, ended=now_iso(), end_reason=reason, **extra)
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


class _KindRow:
    """The one field :func:`asf.workers.cloud.local_only` reads off a launch: its kind."""

    def __init__(self, kind):
        self.kind = 'task' if kind == 'build' else kind


def _quiet(_line):
    """A sink for a refusal line the caller reports itself."""


def refguard_listed(product):
    """``product``'s protected refs (:func:`asf.refguard.listed`), None for the defaults."""
    from asf import refguard
    return refguard.listed(getattr(product, 'conventions', None))


def report_result(log_path):
    """The result record an ended session's REPORT is read off: the last run's ``result`` line
    (:func:`asf.workers.runtime.read_result`), unless it holds no REPORT and an earlier result of
    that same run does — a session that printed its REPORT and then answered a stale background
    notification (T-0432: "That monitor has served its purpose…") reported all the same. None
    while the run has not ended."""
    from asf.workers import report, runtime
    last = runtime.read_result(log_path)
    if last is None or report.parse(str(last.get('result') or '')):
        return last
    found = None
    with open(log_path, encoding='utf-8', errors='replace') as f:
        for line in f:
            try:
                rec = json.loads(line) if line.strip() else None
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            if runtime.launch_boundary(rec):
                found = None
            elif rec.get('type') == 'result' and report.parse(str(rec.get('result') or '')):
                found = rec
    return found or last


#: how many entries of a branch's reflog :func:`overwritable` reads for its pre-rebase history
REFLOG_DEPTH = 500


def overwritable(wt, head, origin, branch):
    """'' when pushing ``head`` over ``origin`` (origin's tip of ``branch``) loses nothing the
    worktree's own history never held, else why not: ``origin`` is an ancestor of ``head`` (a
    fast-forward), or of an entry of ``branch``'s reflog (the pre-rebase history the session
    rewrote), or every commit it has that ``head`` lacks is patch-equivalent to one in ``head``
    (``git cherry``). A tip that cannot be read even after a fetch is never overwritten."""
    from asf import gitops
    if not origin or origin == head:
        return ''

    def has(sha):
        return gitops.git(['cat-file', '-e', '%s^{commit}' % sha], wt, timeout=60).ok

    def ancestor(of):
        return gitops.git(['merge-base', '--is-ancestor', origin, of], wt, timeout=60).ok

    if not has(origin):
        gitops.git(['fetch', '-q', 'origin', branch], wt, timeout=300)
        if not has(origin):
            return 'cannot read origin/%s at %s' % (branch, origin[:12])
    if ancestor(head):
        return ''
    log = gitops.git(['log', '-g', '-n', str(REFLOG_DEPTH), '--format=%H',
                      'refs/heads/%s' % branch], wt, timeout=60)
    seen = set()
    for sha in (log.data.split() if log.ok else []):
        if sha not in seen:
            seen.add(sha)
            if ancestor(sha):
                return ''
    cherry = gitops.git(['cherry', head, origin], wt, timeout=120)
    if not cherry.ok:
        return 'cannot compare origin/%s with this branch: %s' % (branch, cherry.reason)
    lost = [ln.split()[1][:9] for ln in cherry.stdout.splitlines() if ln.startswith('+')]
    if not lost:
        return ''
    return ('origin/%s holds %d commit(s) this branch never had (%s) — fetch it and carry them '
            'by hand, then push' % (branch, len(lost), ', '.join(lost[:5])))


def unpushed_head(wt, branch, main='main', protected=None):
    """``(head, refused)`` of an ended session's worktree ``wt`` on ``branch`` (its checked-out
    branch when empty): ``head`` is the HEAD sha when it holds commits no origin ref has and
    origin's ``branch`` is not already at or past it, else ''; ``refused`` is why that head is not
    pushed (:func:`overwritable`), else ''. The trunk or a protected ref is never a target."""
    from asf import gitops, refguard
    from asf.harvest import harvest
    if not wt or not os.path.isdir(wt):
        return '', ''
    got = gitops.git(['rev-parse', 'HEAD'], wt, timeout=60)
    if not got.ok or not got.data:
        return '', ''
    head = got.data
    if not branch:
        ref = gitops.git(['rev-parse', '--abbrev-ref', 'HEAD'], wt, timeout=60)
        branch = ref.data if ref.ok and ref.data != 'HEAD' else ''
    if not branch or refguard.refusal(branch, 'push', main, protected, out=_quiet):
        return '', ''
    ahead = gitops.git(['rev-list', '--count', head, '--not', '--remotes=origin'], wt,
                       timeout=60)
    if not ahead.ok or not ahead.data.isdigit() or int(ahead.data) == 0:
        return '', ''
    origin = harvest.remote_head(wt, branch)
    if origin == head:
        return '', ''
    if origin and gitops.git(['cat-file', '-e', '%s^{commit}' % origin], wt, timeout=60).ok and \
            gitops.git(['merge-base', '--is-ancestor', head, origin], wt, timeout=60).ok:
        return '', ''  # origin is at or past it
    return head, overwritable(wt, head, origin, branch)


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


def required_checks_for(product, github=None):
    """The checks that gate ``product``'s landing: ``conventions.landing_checks``, else what
    ``github`` (a port with ``required_checks``) reads off the base branch's rules, else ``()``
    (every check counts)."""
    named = product.conventions.get('landing_checks')
    names = [str(named)] if isinstance(named, str) else [str(n) for n in named or ()]
    if names:
        return tuple(names)
    reader = getattr(github, 'required_checks', None)
    if reader is None:
        return ()
    try:
        return tuple(reader() or ())
    except Exception:  # noqa: BLE001 — an unreadable rule set means every check counts
        return ()


def doc_globs(product):
    """The path globs of ``product``'s document trees (specs, plans, reviews)."""
    conv = product.conventions
    return tuple('%s/**' % d.rstrip('/') for d in (conv.specs_dir, conv.plans_dir,
                                                   conv.reviews_dir))


def config_for(product, cfg=None, github=None):
    """The :class:`~asf.kernel.model.Config` of ``product``: branch prefixes, document roots and
    the required checks (:func:`required_checks_for`, asking ``github`` when the conventions name
    none) from its conventions; ``max_sessions`` (:func:`lane_seats`: local + cloud), ``rank``, the idle alarm and the merge train's
    ``update_parallel`` from its ``kernel:``
    block (:mod:`asf.kernel.settings`, each with its documented default)."""
    conv = product.conventions
    k = product.kernel
    return M.Config(
        required_checks=required_checks_for(product, github),
        doc_branches=(conv.prefix('spec'), conv.prefix('plan')),
        doc_paths=doc_globs(product),
        work_branch=conv.prefix('code'), fix_branch=conv.prefix('fix'),
        max_sessions=sum(lane_seats(product, cfg)), rank=k['launch']['rank'],
        idle_alarm=bool(k['idle_alarm']['enabled']),
        idle_min_free=int(k['idle_alarm']['min_free_seats']),
        update_parallel=int(k['landing']['update_parallel']))



def cloud_lane(product, cfg=None):
    """The product's cloud lane (:func:`asf.workers.cloud.settings`) when the kernel may launch on
    it: ``kernel.launch.cloud_max`` above 0, ``cloud.enabled``, not ``cloud.mode: off``, and a
    runtime the lane runs; else None. ``cfg`` is ``config.yaml`` (read when None)."""
    if not product.kernel['launch']['cloud_max']:
        return None
    from asf.workers import cloud, spawn
    s = cloud.settings(spawn.load_cfg() if cfg is None else cfg, product)
    if not s.enabled or s.mode == cloud.MODE_OFF or s.runtime not in cloud.RUNTIMES:
        return None
    return s


def lane_seats(product, cfg=None):
    """``(local seats, cloud seats)``: ``kernel.launch.local_max`` (default ``max_sessions``) and
    ``kernel.launch.cloud_max`` — 0 while the cloud lane is off (:func:`cloud_lane`)."""
    from asf.kernel import settings
    local, cloud_max = settings.seats(product.kernel)
    return int(local), (int(cloud_max) if cloud_lane(product, cfg) is not None else 0)
