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
- ``kernel_rebuilds``: the times the kernel archived the item's branch, closed its PR and built
  it afresh (:class:`~asf.kernel.actions.ArchiveAndReset`): at most once per item.
- ``kernel_attempts``: the reason of each failed attempt, oldest first.
- ``kernel_findings``: the review findings handed to the next fix round.
- ``kernel_answers``: the operator answers already applied; ``kernel_question``: the open one.
- ``kernel_reopened``: ``true`` when a Done item was reopened (a new PR is accepted).
- ``kernel_reverted``: the merged PR numbers the kernel reverted off a red trunk
  (:class:`~asf.kernel.actions.RevertPR`): they no longer make the item Done.
- ``kernel_notes``: the questions a session asked while its work moved on anyway (a ``done``
  REPORT with a pushed head) — shown by ``asf kernel status``, holding nothing.
- ``kernel_dor_fills``: the groom-fill sessions launched for a card the Definition of Ready
  holds (:mod:`asf.kernel.dor`).

A groom-fill verdict (:func:`asf.kernel.dor.parse_verdict`) is applied through the record's own
writer (:func:`asf.record.setfield.set_typed`: the parser round-trip and the record stage):
:meth:`RealRecord.fill_card` writes ``writes:`` (and ``creates:``) and the ``## Acceptance``
lines; :meth:`RealRecord.supersede` retires the card with ``removed:`` and ``superseded_by:``
(and ``supersedes:`` on the successor), or ``landed:`` for a sha.
"""
import datetime
import hashlib
import json
import os
import re
import time
import typing

from asf.kernel import dor as dor_mod
from asf.kernel import intake as intake_mod
from asf.kernel import model as M
from asf.kernel import reports
from asf.kernel.decide import READ_ONLY
from asf.record.core import ID_TOKEN_RE, as_list, canonicalize, is_retired, load_items
from asf.workers import cloudpid

#: the machine-block keys the kernel owns (see the module docstring)
STATE, ATTEMPTS, FIX_ROUNDS = 'kernel_state', 'kernel_attempts', 'kernel_fix_rounds'
FINDINGS, ANSWERS, QUESTION = 'kernel_findings', 'kernel_answers', 'kernel_question'
STUCK_REASON, STUCK_OWNER = 'kernel_stuck_reason', 'kernel_stuck_owner'
STUCK_NEXT, STUCK_SINCE, REOPENED = 'kernel_stuck_next', 'kernel_stuck_since', 'kernel_reopened'
NOTES, EXTRA_ROUNDS, REBUILDS = 'kernel_notes', 'kernel_extra_rounds', 'kernel_rebuilds'
#: the card's declared wait edges — a human key the kernel may add to
#: (:class:`~asf.kernel.actions.WaitOn`), written in the typed block, never the machine block
AFTER = 'after'
HUMAN_KEYS = (AFTER,)
REVERTED, DOR_FILLS = 'kernel_reverted', 'kernel_dor_fills'
#: the kernel version that counted the fills: fills made under another version do not count
DOR_FILLS_VER = 'kernel_dor_fills_ver'
#: the machine-block keys that hold a Stuck (cleared together)
STUCK_KEYS = (STUCK_REASON, STUCK_OWNER, STUCK_NEXT, STUCK_SINCE)
KERNEL_KEYS = (STATE, STUCK_REASON, STUCK_OWNER, STUCK_NEXT, STUCK_SINCE, FIX_ROUNDS, ATTEMPTS,
               FINDINGS, ANSWERS, QUESTION, REOPENED, NOTES, EXTRA_ROUNDS, REBUILDS, REVERTED,
               DOR_FILLS, DOR_FILLS_VER)

#: the branch prefix a rebuilt item's old head is pushed under (:class:`ArchiveAndReset`)
ARCHIVE_PREFIX = 'archive/'

#: the card types the kernel judges (decisions and rules are never work)
WORK_TYPES = ('epic', 'feature', 'story', 'task', 'bug')

#: the old record's states that mean the item is finished
CLOSED_STATES = ('Closed', 'Resolved')

#: the old session kinds that are a build in the kernel's words
#: the state file of the intake-decide sessions launched per key (``state/<product>/``)
INTAKE_TRIES_FILE = 'kernel-intake-tries.json'

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
    def widen_writes(self, item_id, paths) -> None: ...  # add to writes: (the record's writer)
    def mint_story(self, feature_id, story_id, title, acceptance) -> None: ...


class GitHubPort(typing.Protocol):
    def prs(self) -> list: ...                          # [PR], open and recently merged
    def reviews(self, prs) -> list: ...                 # [Review] GitHub holds on PR heads
    def enable_auto_merge(self, pr) -> None: ...
    def update_branch(self, pr) -> None: ...
    def rerun(self, run_id, cancel=False) -> None: ...
    def branches(self) -> list: ...                     # [Branch] under the work prefixes
    def open_pr(self, branch, base, title, body) -> int: ...  # the PR number (new or existing)
    def archive_and_reset(self, pr, branch, head_sha, comment) -> str: ...  # the archive branch
    def close_pr(self, pr, comment) -> None: ...


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

def _kernel_version():
    from asf import __version__
    return str(__version__)


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
        writes=[str(w) for w in as_list(meta.get('writes'))], stories=declared_stories_of(rec),
        body=rec.get('body') or '',
        state=state, stuck=stuck, attempts=[str(a) for a in as_list(machine.get(ATTEMPTS))],
        fix_rounds=int(machine.get(FIX_ROUNDS) or 0),
        extra_rounds=int(machine.get(EXTRA_ROUNDS) or 0),
        rebuilds=int(machine.get(REBUILDS) or 0),
        findings=[str(f) for f in as_list(machine.get(FINDINGS))],
        answers=[str(a) for a in as_list(machine.get(ANSWERS))],
        question=machine.get(QUESTION) or None, reopened=bool(machine.get(REOPENED)),
        notes=[str(n) for n in as_list(machine.get(NOTES))],
        reverted=[int(n) for n in as_list(machine.get(REVERTED)) if str(n).isdigit()],
        creates=[str(c) for c in as_list(meta.get('creates'))],
        dor_fills=(int(machine.get(DOR_FILLS) or 0)
                   if str(machine.get(DOR_FILLS_VER) or '') == _kernel_version() else 0),
        stuck_since=(str(machine.get(STUCK_SINCE)) if stuck is not None and machine.get(STUCK_SINCE)
                     else None),
        plan=_plan_link(meta), created=_created(meta, machine, rec.get('body') or ''),
        signature=str(meta.get('signature') or ''),
        decided=meta.get('decided') is True or is_retired(meta),
        severity=str(meta.get('severity') or ''))


#: a History line that dates the card (``- 2026-09-24: created (inbox) …``)
CREATED_RE = re.compile(r'^\s*-\s*(\d{4}-\d{2}-\d{2})\b[^\n]*\bcreated\b', re.M)


def _plan_link(meta):
    links = meta.get('links')
    return str(links.get('plan') or '') if isinstance(links, dict) else ''


def _created(meta, machine, body):
    """The card's creation date: ``created:``, else its History's first ``created`` line, else
    ``stage_since`` ('' when none)."""
    if meta.get('created'):
        return str(meta.get('created'))
    m = CREATED_RE.search(body)
    if m:
        return m.group(1)
    return str(machine.get('stage_since') or '')


def declared_stories_of(rec):
    """The Story ids a Task card declares it covers: its ``stories:`` field, else the ``stories:``
    line of its body (the plan's Task shape), in order; [] for any other card."""
    from asf.record.plan_tasks import STORIES_LINE_RE, STORY_ID_RE
    meta = rec['meta']
    if meta.get('type') != 'task':
        return []
    named = [str(s) for s in as_list(meta.get('stories')) if s]
    if not named:
        m = STORIES_LINE_RE.search(rec.get('body') or '')
        named = STORY_ID_RE.findall(m.group(1)) if m else []
    return list(dict.fromkeys(named))


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


class PublishRefused(RuntimeError):
    """The record's pre-commit refused the tick's commit; the message is one log line."""


def refusal_line(err):
    """One line from a refused record commit: the files and the finding classes it named."""
    text = '%s\n%s' % (getattr(err, 'stdout', '') or '', getattr(err, 'stderr', '') or '')
    found = re.findall(r'^(\S+?):\d+: (\w+) \(([^)]*)\)', text, re.M)
    if not found:
        return 'record commit refused (exit %s)' % getattr(err, 'returncode', '?')
    files = sorted({f for f, _k, _s in found})
    kinds = sorted({'%s (%s)' % (k, src) for _f, k, src in found})
    return 'record commit refused: %s — %s' % (', '.join(files), ', '.join(kinds))


def scrub_value(value, fn):
    """``value`` with ``fn`` applied to every string inside it (lists, tuples, dicts)."""
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, dict):
        return {k: scrub_value(v, fn) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(scrub_value(v, fn) for v in value)
    return value


class RealRecord:
    """The product's record (``backlog_dir``), its trunk's specs (``repo_dir``/``specs_dir``)
    and its state directory's ledgers (answers, kernel reviews, the launch pause)."""

    def __init__(self, product, state_dir=None):
        from asf import env
        self.product = product
        self.root = product.backlog_dir
        self.state_dir = state_dir or os.path.join(env.ASF_HOME, 'state', product.name)
        self._cards = None
        self._errors = []

    def _scrub(self, value):
        """``value`` with every string in it passed through :func:`asf.redact.scrub_roles` — the
        kernel copies text from session reports, launch failures and CI logs into cards, and a
        worker account's name (or a home path naming one) there makes the record's pre-commit
        refuse the whole tick's commit."""
        from asf import redact
        pats = redact.default_patterns(self.root)
        return scrub_value(value, lambda t: redact.scrub_roles(t, pats))

    def _load(self):
        if self._cards is None:
            by_id, self._errors = load_items(self.root)
            self._cards, _dupes = canonicalize(by_id)
        return self._cards

    def unreadable(self):
        """``{item id: why}``: every card on the record the parser refused (its id read off the
        file name) — on the record, yet absent from :meth:`items`."""
        self._load()
        out = {}
        for err in self._errors or []:
            path = str(err[0] if isinstance(err, (list, tuple)) and err else err)
            iid = item_of_branch(os.path.basename(path).rsplit('.', 1)[0])
            if iid and iid not in out:
                why = ' '.join(str(err[-1] if isinstance(err, (list, tuple)) else err).split())
                out[iid] = '%s: %s' % (path, why[:120])
        return out

    def items(self):
        out = {}
        for iid, rec in self._load().items():
            if rec['meta'].get('type') not in WORK_TYPES:
                continue
            it = item_from_card(rec)
            if is_retired(rec['meta']):  # on the record (never re-minted), invisible, finished
                it.stale_stuck = it.state is M.State.STUCK or any(
                    rec['meta'].get(k) is not None for k in STUCK_KEYS)
                it.state, it.stuck, it.priority = M.State.DONE, None, 'later'
            out[iid] = it
        return out

    def card_fields(self, item_id):
        rec = self._load().get(item_id)
        return {k: rec['meta'].get(k) for k in KERNEL_KEYS if k in rec['meta']} if rec else {}

    def _trunk(self):
        return 'origin/%s' % (getattr(self.product, 'main', None) or 'main')

    def refresh_trunk(self):
        """Fetch ``origin/<main>`` into the product's repo, once a tick, so every trunk read this
        tick (landed specs and plans, :meth:`trunk_files`) sees what has landed — the checkout's
        working tree is never read for them, nor touched (a checkout 131 commits behind
        hid every recent spec and plan). A dry run fetches nothing; a failed fetch is a note, and
        the last fetched ``origin/<main>`` is read."""
        from asf import gitops, mutation_guard
        repo = getattr(self.product, 'repo_dir', None)
        if not repo or not os.path.isdir(repo) or mutation_guard.is_active():
            return None
        main = getattr(self.product, 'main', None) or 'main'
        r = gitops.git(['fetch', '-q', '--no-tags', 'origin',
                        '+refs/heads/%s:refs/remotes/origin/%s' % (main, main)], repo, timeout=120)
        return None if r.ok else 'fetch origin/%s failed: %s' % (
            main, (r.reason or r.data or '').strip()[:160])

    def _trunk_docs(self, folder):
        """``{Feature id: (path, text)}``: every ``<folder>/f-<n>.md`` on ``origin/<main>`` whose
        id is a Feature on the record; None when ``origin/<main>`` cannot be read (no repo, no
        origin) — the caller then reads the checkout."""
        from asf import gitops
        from asf.evidence import evidence
        repo = getattr(self.product, 'repo_dir', None)
        if not repo or not os.path.isdir(repo):
            return None
        rev = self._trunk()
        if not gitops.git(['rev-parse', '--verify', '-q', rev + '^{commit}'], repo,
                          timeout=60).ok:
            return None
        r = gitops.git(['ls-tree', '--name-only', '%s:%s' % (rev, folder)], repo, timeout=60)
        if not r.ok:
            return {}
        paths = {}
        for name in sorted((r.data or '').splitlines()):
            iid = item_of_branch(name[:-3]) if name.endswith('.md') else None
            if iid and self._is_feature(iid) and iid not in paths:
                paths[iid] = '%s/%s' % (folder.rstrip('/'), name)
        texts = evidence.read_refs(['%s:%s' % (rev, p) for p in paths.values()],
                                   product=self.product)
        return {iid: (p, texts.get('%s:%s' % (rev, p))) for iid, p in paths.items()
                if texts.get('%s:%s' % (rev, p)) is not None}

    def mint_plan_tasks(self, out=print, claim_cited=False):
        """The Task cards of every plan landed on ``origin/<main>`` (``<plans_dir>/f-<n>.md``)
        whose Feature has none yet, through the record's own minter
        (:func:`asf.record.plan_tasks.mint_plan_tasks`: parent, writes, stories and the plan's
        order; its guards — decisions, id claims, duplicates, already on the trunk — included).
        Idempotent: a Feature with a Task child is left alone. Returns the new ids.

        A plan the minter refuses whole is kept for :meth:`plan_refusals` (the kernel re-plans
        it). With ``claim_cited`` a refusal whose only cause is ids no claim covers, none of them
        on the record nor inside any claim, is resolved in code first: each id is claimed on the
        record's origin as a block of one, its claimant naming every Feature that cites it
        (:func:`asf.record.idclaim.claim_exact`), and the plans are minted again."""
        self._plan_refusals = {}
        refusals = {}
        made = self._mint_plans(out, refusals)
        if claim_cited and refusals:
            claimed = self._claim_cited(refusals, out)
            if claimed:
                refusals = {}
                made += self._mint_plans(out, refusals)
        self._plan_refusals = refusals
        return made

    def plan_refusals(self):
        """``{Feature id: why}`` of every landed plan the last :meth:`mint_plan_tasks` refused."""
        return {fid: r['reason'] for fid, r in (getattr(self, '_plan_refusals', None)
                                                 or {}).items()}

    def _claim_cited(self, refusals, out=print):
        """Claim the ids the refused plans cite that no claim covers, when that is the plans'
        only fault and no record item nor claim holds any of them (:mod:`asf.record.idcheck`).
        Returns the ids claimed; a failure is one line."""
        from asf.record import idclaim, idcheck
        cite = {}
        for fid, r in sorted(refusals.items()):
            ids = r.get('ids') or {}
            if ids and all(k == idcheck.UNCOVERED for k in ids.values()):
                for iid in ids:
                    cite.setdefault(iid, []).append(fid)
        if not cite or not idclaim.has_origin(self.root):
            return []
        try:
            idclaim.fetch(self.root)
            held = idclaim.claims(self.root)
        except idclaim.ClaimError as e:
            out('plan-tasks: cited ids not claimed — %s' % e)
            return []
        items = self._load()
        free = [i for i in cite if i not in items and idclaim.covers(held, i) is None]
        whole = {fid for fid, r in refusals.items()
                 if r.get('ids') and all(i in free for i in r['ids'])}
        done = []
        for iid in sorted(free):
            if not any(fid in whole for fid in cite[iid]):
                continue
            who = 'asf-kernel: cited by %s' % ' '.join(cite[iid])
            try:
                ok = idclaim.claim_exact(self.root, iid, who)
            except idclaim.ClaimError as e:
                ok = False
                out('plan-tasks: claim %s failed — %s' % (iid, e))
            if ok:
                done.append(iid)
        if done:
            out('plan-tasks: claimed cited id(s) %s (on no record item, in no claim) for %s' % (
                ', '.join(done), ', '.join(sorted(whole))))
        return done

    def _mint_plans(self, out, refusals):
        from asf.record import plan_tasks
        conv = self.product.conventions
        plans = self._trunk_docs(conv.plans_dir) or {}
        if not plans:
            return []
        specs = self._trunk_docs(conv.specs_dir) or {}
        rev = self._trunk()
        features, lane, texts = {}, {}, {}
        for fid, (path, text) in plans.items():
            f = {'alias': None, 'plan': '%s:%s' % (rev, path), 'plan_on_main': True,
                 'spec': None, 'spec_on_main': False, 'prs': [], 'tasks': {}}
            texts[f['plan']] = text
            if fid in specs:
                spath, stext = specs[fid]
                f['spec'], f['spec_on_main'] = '%s:%s' % (rev, spath), True
                texts[f['spec']] = stext
            features[fid.lower()] = f
            lane[fid.upper()] = {'plan': [path]}
        made = plan_tasks.mint_plan_tasks(self.root, self.product,
                                          {'features': features, 'lane_docs': lane},
                                          out=out, read_ref=texts.get, refusals=refusals)
        self._cards = None
        return list(made or [])

    def specs_landed(self):
        """Every Feature spec on ``origin/<main>``: ``<specs_dir>/f-<n>.md`` -> ``F-<n>`` (the
        checkout's working tree only for a repo with no ``origin/<main>``)."""
        conv = self.product.conventions
        got = self._trunk_docs(conv.specs_dir)
        if got is not None:
            return {iid: text for iid, (_path, text) in got.items()}
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
        return [M.Answer(str(r['item']), str(r['text']), str(r.get('at') or ''),
                         str(r.get('job') or ''))
                for r in _jsonl(os.path.join(self.state_dir, ANSWERS_FILE))
                if r.get('item') and r.get('text')]

    def id_claims(self, ids):
        """``{id: (ref, sha)}`` of the claim on the record repo's origin
        (:func:`asf.workers.spawn.claim_repo`, ``refs/asf/ids/*`` fetched first) covering each of
        ``ids``, ``''`` when none does; ``{}`` when the claims cannot be read from origin (no
        record repo, no origin, a failed fetch) — the question then stays with the operator."""
        from asf import gitops
        from asf.record import idclaim
        from asf.workers import spawn
        from asf import mutation_guard
        repo = spawn.claim_repo(self.product)
        if not repo:
            return {}
        try:
            if not mutation_guard.is_active():  # a dry run reads the local mirror, fetches nothing
                idclaim.fetch(repo)
        except idclaim.ClaimError:
            return {}
        cl = idclaim.claims(repo)
        out, shas = {}, {}
        for iid in ids:
            c = idclaim.covers(cl, iid)
            if c is None:
                out[iid] = ''
                continue
            if c.ref not in shas:
                r = gitops.git(['rev-parse', '--verify', '-q', c.ref], repo)
                shas[c.ref] = (r.stdout or '').strip() if r.ok else ''
            out[iid] = (c.ref, shas[c.ref])
        return out

    def _intake(self):
        return getattr(getattr(self.product, 'conventions', None), 'intake_dir', None) or 'inbox'

    def inbox_filed(self, titles):
        """``{title: path}``: the intake card (``<intake_dir>/<slug>.md``, or under ``done/``
        once groomed) each title would be filed as, relative to the record; ``''`` when the record
        holds none (:func:`asf.groom.inbox.file_card`'s name)."""
        out = {}
        for title in titles:
            name = inbox_name(self._scrub(title))
            found = ''
            for rel in (name, 'done/' + name):
                if os.path.exists(os.path.join(self.root, self._intake(), rel)):
                    found = '%s/%s' % (self._intake().rstrip('/'), rel)
                    break
            out[title] = found
        return out

    def file_inbox(self, title, body):
        """File one untyped card into the intake dir through :func:`asf.groom.inbox.file_card`
        (it publishes); one already there is not filed again. The path, relative to the
        record."""
        from asf.groom import inbox
        title = self._scrub(title)
        have = self.inbox_filed([title])[title]
        if have:
            return have
        path = inbox.file_card(self.root, self._intake(), title, '\n' + self._scrub(body))
        return os.path.relpath(path, self.root)

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
        for k, v in self._scrub(dict(fields)).items():
            if v is None or v == []:
                meta.pop(k, None)
                keys.discard(k)
            else:
                meta[k] = v
                if k not in HUMAN_KEYS:
                    keys.add(k)
        meta['updated'] = now_iso()
        keys.add('updated')
        meta.machine_keys = keys
        new_text = frontmatter.render(meta, body)
        writer.write_card(rec['path'], new_text)
        rec['meta'], rec['text'] = meta, new_text

    def file_bug(self, key, title, body, rank=None):
        """Write a Bug card titled ``title`` with ``body`` (which carries ``key``) at ``rank``,
        unless a card already carries ``key``; returns its id."""
        from asf.record.core import today
        from asf.record.ids import mint_id, write_new_item
        cards = self._load()
        for iid, rec in cards.items():
            if key in (rec.get('body') or rec.get('text') or ''):
                return iid
        bug_id = mint_id(self.root, cards, 'bug', claimant='asf-kernel')
        fields = {'title': self._scrub(title)}
        if isinstance(rank, int):
            fields['rank'] = rank
        write_new_item(self.root, cards, 'bug', bug_id, fields, self._scrub(body), today(),
                       'kernel: main is red')
        return bug_id

    def trunk_files(self):
        """The paths on ``origin/<main>`` in the product's repo (``git ls-tree``, no fetch: the
        launches fetch it), as a frozenset; None when unreadable."""
        from asf import gitops
        repo = getattr(self.product, 'repo_dir', None)
        if not repo or not os.path.isdir(repo):
            return None
        r = gitops.git(['ls-tree', '-r', '--name-only', 'origin/%s' % self.product.main], repo,
                       timeout=60)
        if not r.ok:
            return None
        return frozenset(line for line in (r.data or '').splitlines() if line)

    def _rec(self, item_id):
        rec = self._load().get(item_id)
        if rec is None:
            raise PortError('%s: no card on the record' % item_id)
        return rec

    def fill_card(self, item_id, acceptance, writes, creates=(), why=''):
        """A groom-fill's ``fill``/``reshape``: ``writes:`` (and ``creates:``, the paths it
        declares new) through :func:`asf.record.setfield.set_typed`, then the ``## Acceptance``
        section replaced by ``acceptance`` (one ``- [ ]`` line each) with a History line."""
        from asf.record import frontmatter, writer
        from asf.record.core import parse_sections, render_sections, today
        from asf.record.setfield import set_typed
        rec = self._rec(item_id)
        updates = {'writes': self._scrub(list(writes))}
        if creates or rec['meta'].get('creates'):
            updates['creates'] = self._scrub(list(creates)) or None
        err = set_typed(rec, updates, writer='groom-fill')
        if err:
            raise PortError(err)
        meta, body = frontmatter.parse(rec['text'], path=rec['relpath'])
        pre, sections = parse_sections(body)
        lines = ''.join('- [ ] %s\n' % a for a in self._scrub(list(acceptance)))
        found = False
        for sec in sections:
            if sec[0].strip() == '## Acceptance':
                sec[1], found = '\n' + lines + '\n', True
        if not found:
            sections.insert(0, ['## Acceptance', '\n' + lines + '\n'])
        from asf.record.ingest import append_history_lines
        body = append_history_lines(render_sections(pre, sections), [
            '- %s groom-fill: acceptance and writes filled%s' % (today(), ' — ' + why if why
                                                                 else '')])
        text = frontmatter.render(meta, body)
        writer.write_card(rec['path'], text)
        rec['meta'], rec['text'], rec['body'] = meta, text, body

    def widen_writes(self, item_id, paths):
        """A ``needs-writes`` grant (:mod:`asf.kernel.resolvers`): ``paths`` not on the card's
        ``writes:`` yet are appended to it through :func:`asf.record.setfield.set_typed` (the
        parser round-trip and the record stage) with a History line; a refusal raises
        :class:`PortError` and the card is unchanged."""
        from asf.record.core import today
        from asf.record.setfield import set_typed
        rec = self._rec(item_id)
        have = [str(w) for w in as_list(rec['meta'].get('writes'))]
        added = [p for p in self._scrub(list(paths)) if p not in have]
        if not added:
            return
        err = set_typed(rec, {'writes': have + added}, writer='kernel', product=self.product,
                        history=['- %s kernel: writes widened +%s (needs-writes)'
                                 % (today(), ' '.join(added))])
        if err:
            raise PortError(err)
        from asf.record import frontmatter
        rec['meta'] = frontmatter.parse(rec['text'], path=rec['relpath'])[0]

    def supersede(self, item_id, by, why):
        """A groom-fill's ``superseded``: retire ``item_id`` (``removed:``) with
        ``superseded_by: <by>`` and ``supersedes:`` on ``by`` when it is a card; a sha is
        ``landed:`` instead."""
        from asf.record.core import today
        from asf.record.retire import landing_ref, removal
        from asf.record.setfield import set_typed
        rec = self._rec(item_id)
        kind, ref = landing_ref(by)
        cards = self._load()
        updates = {'removed': removal('superseded by %s — %s' % (by, self._scrub(why)),
                                      [(kind, ref)] if kind == 'sha' else [])}
        if kind == 'sha':
            updates['landed'] = ref
        elif by in cards:
            updates['superseded_by'] = by
        else:
            raise PortError('superseded_by %s is not on the record' % by)
        err = set_typed(rec, updates, writer='groom-fill', history=[
            '- %s groom-fill: retired, superseded by %s' % (today(), by)])
        if err:
            raise PortError(err)
        if kind != 'sha':
            win = cards[by]
            have = [str(x) for x in as_list(win['meta'].get('supersedes'))]
            if item_id not in have:
                err = set_typed(win, {'supersedes': sorted(set(have) | {item_id})},
                                writer='groom-fill', history=[
                                    '- %s groom-fill: supersedes %s' % (today(), item_id)])
                if err:
                    raise PortError(err)

    def mint_story(self, feature_id, story_id, title, acceptance):
        from asf.record.core import today
        from asf.record.ids import write_new_item
        cards = self._load()
        if story_id in cards:
            return
        if feature_id not in cards:
            raise PortError('%s: parent %s is not on the record' % (story_id, feature_id))
        write_new_item(self.root, cards, 'story', story_id,
                       {'title': self._scrub(title), 'parent': feature_id},
                       '', today(), 'kernel: declared by the landed spec',
                       acceptance=self._scrub(list(acceptance)), shape=('parent-feature', 'story'))

    # ---- intake (:mod:`asf.kernel.intake`) ------------------------------------------------------

    def _bug_epic(self):
        conv = getattr(self.product, 'conventions', None)
        try:
            return conv.get('default_bug_epic') if conv is not None else None
        except AttributeError:
            return None

    def mint_inbox(self):
        """The groom's own minting path (:func:`asf.groom.inbox.process_inbox`): every inbox note
        the shape rules read becomes a card (``decided: false``), the rest get their
        ``## Question``. Returns the new ids; the cards are read afresh after."""
        from asf.groom import inbox
        from asf.record.core import today
        if not os.path.isdir(os.path.join(self.root or '', self._intake())):
            return []
        created = inbox.process_inbox(self.root, self._load(), today(),
                                      default_bug_parent=self._bug_epic(),
                                      intake_dir=self._intake())
        self._cards = None
        return created

    def inbox_count(self):
        """How many notes sit in the intake dir (read only: a dry run's count)."""
        d = os.path.join(self.root or '', self._intake())
        try:
            return sum(1 for n in os.listdir(d) if n.endswith('.md')
                       and os.path.isfile(os.path.join(d, n)))
        except OSError:
            return 0

    def notes(self):
        """``{key: Item}``: every inbox note that carries a ``## Question`` — type ``note``, its
        title, its text and the question (:mod:`asf.kernel.intake`)."""
        from asf.groom import inbox
        from asf.kernel import intake
        d = os.path.join(self.root or '', self._intake())
        out = {}
        try:
            names = sorted(os.listdir(d))
        except OSError:
            return out
        for name in names:
            path = os.path.join(d, name)
            if not name.endswith('.md') or not os.path.isfile(path):
                continue
            try:
                with open(path, encoding='utf-8') as f:
                    text = f.read()
            except OSError:
                continue
            if not inbox._has_question(text):
                continue
            body, question = inbox._split_question(text)
            title = inbox.parse_inbox_file(body).title or name[:-3]
            key = intake.note_key(name)
            out[key] = M.Item(id=key, type=intake.NOTE, title=title, body=body.strip(),
                              question=' '.join(str(question or '').split()) or None)
        return out

    def _tries_path(self):
        return os.path.join(self.state_dir, INTAKE_TRIES_FILE)

    def intake_tries(self):
        """``{key: n}``: the intake-decide sessions launched per card or note."""
        try:
            with open(self._tries_path(), encoding='utf-8') as f:
                got = json.load(f)
        except (OSError, ValueError):
            return {}
        return {str(k): int(v) for k, v in got.items() if str(v).isdigit()} \
            if isinstance(got, dict) else {}

    def count_intake(self, key):
        """One more intake-decide session for ``key``."""
        tries = self.intake_tries()
        tries[key] = tries.get(key, 0) + 1
        os.makedirs(self.state_dir, exist_ok=True)
        tmp = self._tries_path() + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(tries, f, indent=1, sort_keys=True)
        os.replace(tmp, self._tries_path())

    def decide_intake(self, key, v):
        """Apply the intake verdict ``v`` (:class:`asf.kernel.intake.Verdict`) to ``key``: a
        card through the groom's answer grammar (:func:`asf.groom.groom.apply_groom_answers`) and
        its ``priority:``; a note through the inbox's (:func:`asf.groom.inbox.apply_answer`),
        then minted, and the new card decided the same way. Returns a note."""
        from asf.kernel import intake
        if intake.is_note(key):
            return self._decide_note(key, v)
        rec = self._rec(key)
        words = intake.answer_words(item_from_card(rec), v)
        self._groom_answer(key, rec, words, v)
        self._reread(rec)
        pri = intake.priority_of(v)
        if pri and str(rec['meta'].get('priority') or '') != pri:
            from asf.record.core import today
            from asf.record.setfield import set_typed
            err = set_typed(rec, {'priority': pri}, writer='kernel', product=self.product,
                            history=['- %s intake: priority → %s (%s)' % (today(), pri, v.by)])
            self._reread(rec)
            if err:
                raise PortError(err)
        return 'answered %s%s' % ('; '.join(words), ', priority %s' % pri if pri else '')

    @staticmethod
    def _reread(rec):
        """Bring one cached card up to date with its file after a writer outside this port."""
        from asf.record import frontmatter
        with open(rec['path'], encoding='utf-8') as f:
            text = f.read()
        rec['meta'], rec['body'] = frontmatter.parse(text, path=rec['relpath'])
        rec['text'] = text

    def _groom_answer(self, key, rec, words, v):
        import tempfile
        from asf.groom import groom
        from asf.record.core import today
        title = ' '.join(str(rec['meta'].get('title') or '').split())
        fd, path = tempfile.mkstemp(suffix='.answers')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                for w in words:
                    f.write('- [ ] %s %s — intake → answer: adjudicator: %s\n'
                            % (key, title, ' '.join(self._scrub(w).split())))
            groom.apply_groom_answers(self.root, self._load(), path, today(),
                                      adjudicator_job=v.by, product=self.product)
        finally:
            os.remove(path)

    def _decide_note(self, key, v):
        from asf.groom import inbox
        from asf.kernel import intake
        from asf.record.core import today
        notes = self.notes()
        note = notes.get(key)
        if note is None:
            raise PortError('%s: no such note with a question in the inbox' % key)
        clauses = intake.note_clauses(note, v)
        ok, why = inbox.apply_answer(self.root, intake.note_name(key), clauses, today(),
                                     'intake %s' % v.by, intake_dir=self._intake())
        if not ok:
            raise PortError('%s: %s' % (key, why or 'the answer was not applied'))
        if v.decision == 'close':
            return 'note closed'
        created = self.mint_inbox()
        cards = self._load()
        mine = [i for i in created if ' '.join(str(cards[i]['meta'].get('title') or '').split())
                == ' '.join(str(note.title or '').split())]
        if not mine:
            again = self.notes().get(key)
            return 'answered %s; not minted: %s' % (
                clauses, again.question if again is not None else 'no card')
        import dataclasses
        out = self.decide_intake(mine[0], dataclasses.replace(v, parent=''))
        return 'answered %s; minted %s, %s' % (clauses, mine[0], out)

    def publish(self, message):
        """Commit and push what this tick wrote, through the record's one push. Every card the
        tick changed is scrubbed on disk first (:meth:`scrub_changed`), so a stray name is
        replaced, not refused; a commit the pre-commit still refuses is raised as
        :class:`PublishRefused` (one line: the files and the finding classes) for the tick to
        log — the refused paths are put back, so the next tick writes them again."""
        import subprocess
        from asf.record import publish
        self.scrub_changed()
        try:
            publish.publish_changes(self.root, self._before, message)
        except subprocess.CalledProcessError as e:
            raise PublishRefused(refusal_line(e)) from None

    def scrub_changed(self):
        """Pass every card file changed since :meth:`snapshot` through the scrubber in place;
        the paths it changed."""
        from asf import redact
        from asf.record import publish
        before = getattr(self, '_before', None)
        if before is None:
            return []
        pats = redact.default_patterns(self.root)
        fixed = []
        for rel, content in sorted(publish._dirty(self.root).items()):
            if content is None or not rel.endswith('.md') or before.get(rel) == content:
                continue
            text = content.decode('utf-8', errors='replace')
            clean = redact.scrub_roles(text, pats)
            if clean != text:
                with open(os.path.join(self.root, rel), 'w', encoding='utf-8') as f:
                    f.write(clean)
                fixed.append(rel)
        return fixed

    def snapshot(self):
        from asf.record import publish
        self._before = publish.snapshot(self.root)


# ---- GitHub -------------------------------------------------------------------------------------

#: the open-PR fields one ``gh pr list`` reads
PR_FIELDS = ('number', 'headRefName', 'headRefOid', 'baseRefName', 'mergeable',
             'mergeStateStatus', 'autoMergeRequest', 'files', 'statusCheckRollup',
             'latestReviews', 'additions', 'deletions')
_HUNK = re.compile(r'^@@[^@]*@@')


def _lines(d):
    """A PR's additions plus deletions off its listing (0 when unread)."""
    try:
        return int(d.get('additions') or 0) + int(d.get('deletions') or 0)
    except (TypeError, ValueError):
        return 0


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


def job_keys(rollup):
    """``{(check name, run id): details URL}`` of each check's newest job in the rollup — a job
    (one attempt of a run) is finished once, so its URL keys its facts across ticks."""
    best = {}
    for c in rollup or []:
        check = _check(c)
        url = str(c.get('detailsUrl') or '')
        if not (check.run_id and '/job/' in url):
            continue
        key, order = (check.name, check.run_id), _run_order(c, check)
        if key not in best or best[key][0] <= order:
            best[key] = (order, url)
    return {k: v[1] for k, v in best.items()}


def _run_order(c, check):
    """The sort key that puts the newest run of a check last: its run id, then its latest time
    (completed, else started, else created — an unfinished rerun is newer than its red)."""
    times = [str(c.get(k) or '') for k in ('completedAt', 'startedAt', 'createdAt')]
    times = [t for t in times if t and not t.startswith('0001-')]
    return (check.run_id or 0, max(times) if times else '')


def newest_checks(rollup):
    """The rollup's checks, one per name: when several runs carry one check name, only the
    newest (:func:`_run_order`) decides red or green — an older skipped or red run never hides
    a newer one. Order is the rollup's first appearance of each name."""
    best, order = {}, []
    for c in rollup or []:
        check = _check(c)
        key = _run_order(c, check)
        if check.name not in best:
            order.append(check.name)
        elif best[check.name][0] > key:
            continue
        best[check.name] = (key, check)
    return [best[n][1] for n in order]


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


def strict_from_rules(rules):
    """Whether a branch-rules listing requires branches to be up to date: any
    ``required_status_checks`` rule with ``strict_required_status_checks_policy`` on (none such:
    False — nothing requires it)."""
    return any(isinstance(r, dict) and r.get('type') == 'required_status_checks'
               and bool((r.get('parameters') or {}).get('strict_required_status_checks_policy'))
               for r in rules or ())


#: the trunk's newest commits the main safety net reads, with their checks and merged PRs (one
#: GraphQL call a tick)
MAIN_QUERY = '''query($owner:String!,$name:String!,$ref:String!,$n:Int!){
 repository(owner:$owner,name:$name){ref(qualifiedName:$ref){target{... on Commit{
  history(first:$n){nodes{oid messageHeadline
   associatedPullRequests(first:3){nodes{number headRefName merged additions deletions
    files(first:100){nodes{path}}}}
   statusCheckRollup{contexts(first:100){nodes{__typename
    ... on CheckRun{name status conclusion detailsUrl startedAt completedAt}
    ... on StatusContext{context state targetUrl createdAt}}}}}}}}}}}'''


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


#: a failed ``gh`` read GitHub may answer on the next try: a 5xx, a timeout, a dropped
#: connection, a secondary rate limit (:func:`transient`)
TRANSIENT_RE = re.compile(
    r'HTTP 5\d\d|\b50[0234]\b|gateway|service unavailable|timed? ?out|timeout|'
    r'connection (reset|refused)|\bEOF\b|TLS handshake|could not resolve host|'
    r'something went wrong|secondary rate limit|abuse detection', re.IGNORECASE)


def transient(result):
    """Whether a failed :class:`asf.github.Result` is a transient GitHub failure."""
    return not result.ok and bool(TRANSIENT_RE.search('%s\n%s' % (result.reason or '',
                                                                   result.stderr or '')))


def _secondary(limit):
    """Whether a :class:`asf.gh_limit.RateLimited` is GitHub's secondary (burst) limit, which a
    short wait clears — not the hourly quota, which latches the process."""
    return bool(re.search(r'secondary rate limit|abuse detection', str(limit), re.IGNORECASE))


#: the ``X-RateLimit-*`` headers of a ``gh api -i`` reply
_RATE_HDR = re.compile(r'^x-ratelimit-(limit|remaining|reset):\s*(\d+)\s*$', re.I | re.M)

#: the file of immutable GitHub facts kept across ticks (``state/<product>/``)
GH_CACHE_FILE = 'kernel-gh-cache.json'


def rate_headers(text):
    """``(remaining, limit, reset)`` off a ``gh api -i`` reply's ``X-RateLimit-*`` headers, or
    None when they are not there."""
    got = {k.lower(): int(v) for k, v in _RATE_HDR.findall(str(text or ''))}
    if 'remaining' not in got or 'limit' not in got:
        return None
    return got['remaining'], got['limit'], got.get('reset', 0)


class GhCache:
    """Facts GitHub never changes, kept across ticks in ``path`` (JSON; None: this process only):
    ``trees`` (commit sha -> tree sha), ``changes`` (``base...head`` -> change id) and ``jobs``
    (a finished red job's details URL -> its run attempt and failed-log reading). Each section
    keeps its newest :data:`MAX` entries. An unreadable file is an empty cache; a dry run
    (:func:`asf.mutation_guard.is_active`) reads it and writes nothing."""

    SECTIONS = ('trees', 'changes', 'jobs')
    MAX = 2000

    def __init__(self, path=None):
        self.path, self.dirty = path, False
        data = {}
        if path:
            try:
                with open(path, encoding='utf-8') as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = {}
        if not isinstance(data, dict):
            data = {}
        self.data = {k: dict(data.get(k) or {}) if isinstance(data.get(k), dict) else {}
                     for k in self.SECTIONS}

    def get(self, section, key):
        return self.data[section].get(key)

    def put(self, section, key, value):
        sec = self.data[section]
        if sec.get(key) == value:
            return
        sec.pop(key, None)
        sec[key] = value
        while len(sec) > self.MAX:
            sec.pop(next(iter(sec)))
        self.dirty = True

    def save(self):
        from asf import mutation_guard
        if not (self.path and self.dirty) or mutation_guard.is_active():
            return
        try:
            os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
            with open(self.path + '.tmp', 'w', encoding='utf-8') as f:
                json.dump(self.data, f, sort_keys=True)
            os.replace(self.path + '.tmp', self.path)
            self.dirty = False
        except OSError:
            pass  # a cache never fails the tick: the next one reads again


class RealGitHub:
    """The product's repo (``repo_slug``) through :func:`asf.github.gh` with its ``auth_env``.

    A read that fails transiently (:func:`transient`; a secondary rate limit too) is tried again
    after each of ``kernel.github.retry_delays_s`` (default 2, 5, 10 s). Once a read has spent
    them, GitHub counts as down for this port's tick: later reads are tried once. A write is
    never retried here — the next tick decides it again on fresh facts.

    The budget (2026-10-10: the core limit ran out at 60 open PRs): a fact that never changes —
    a commit's tree, a head's change, a finished red job's attempt and failed log — is read once
    and kept in :class:`GhCache` across ticks (``cache_path``); the pushed branches come from
    ``git ls-remote`` (no API cost). Once per tick (:meth:`read_budget`) the core calls left are
    read off a real call's ``X-RateLimit-*`` headers (``gh api rate_limit`` was seen answering
    5000 left while every call was refused); under ``kernel.github.slow_below`` of the limit the
    tick is ``slow``: it reads no new change (a verdict keys by tree alone) and no failed log, and
    logs one line."""

    #: merged PRs read per tick: enough to see every recent landing
    MERGED_LIMIT = 100
    #: red runs whose logs are read per tick (the rest keep ``failing_files`` empty)
    LOG_LIMIT = 10
    #: the compare API's file cap: a change listing this many files may be cut short
    COMPARE_FILES_CAP = 300

    def __init__(self, product, run=None, sleep=None, cache_path=None, git=None, log=None):
        from asf import ci_pool
        self.product = product
        self.cache = GhCache(cache_path)
        self._git = git
        self.log = log or _quiet
        self.budget, self.slow, self._budget_read = None, False, False
        self.open_heads = None  # every open PR's number -> head sha, once the PRs are read
        try:
            self._slow_below = float(product.kernel['github']['slow_below'])
        except (AttributeError, KeyError, TypeError, ValueError):
            self._slow_below = 0.15
        self.slug = product.repo_slug
        self._env = ci_pool._gh_env(product)
        self._run = run
        self._sleep = sleep or time.sleep
        self._logs = 0
        try:
            self._delays = tuple(product.kernel['github']['retry_delays_s'])
        except (AttributeError, KeyError, TypeError):
            self._delays = (2, 5, 10)
        self._down = False

    def _gh(self, args, json=True, retry=True):
        from asf import gh_limit, github
        delays = self._delays if retry and not self._down else ()
        for i in range(len(delays) + 1):
            try:
                r = github.gh(args, json=json, env=self._env, run=self._run)
            except gh_limit.RateLimited as e:
                if i >= len(delays) or not _secondary(e):
                    raise
                gh_limit.unlatch()  # a burst limit: the backoff below is the wait it asks for
                r = None
            if r is not None and not transient(r):
                return r
            if i < len(delays):
                self._sleep(delays[i])
        if retry:
            self._down = True
        return r

    def prs(self):
        """The open PRs and the recently merged ones; :class:`PortError` when either listing is
        unreadable (a missing merged PR would read as an item with none: no partial facts)."""
        self.read_budget()
        try:
            return self._prs()
        finally:
            self.cache.save()

    def read_budget(self):
        """Once per port (one tick): the core calls left (:attr:`budget`: ``(remaining, limit,
        reset)``, None when unreadable) off ``gh api -i repos/<slug>``'s headers, and
        :attr:`slow` when they are under ``kernel.github.slow_below`` of the limit (one log line
        says so). A rate-limited probe raises :class:`asf.gh_limit.RateLimited`: the tick is
        blind before it spends anything else."""
        if self._budget_read:
            return self.budget
        self._budget_read = True
        r = self._gh(['api', '-i', 'repos/%s' % self.slug], json=False, retry=False)
        self.budget = rate_headers('%s\n%s' % (r.stdout or '', r.stderr or ''))
        if self.budget is None:
            return None
        left, limit, reset = self.budget
        self.slow = limit > 0 and left < limit * self._slow_below
        if self.slow:
            at = time.strftime('%H:%MZ', time.gmtime(reset)) if reset else '?'
            self.log('kernel tick: GitHub core calls low — %d/%d left (under %.0f%%): reads '
                     'slowed (no new change or failed-log reads) until the reset at %s'
                     % (left, limit, 100 * self._slow_below, at))
        return self.budget

    def _prs(self):
        r = self._gh(['pr', 'list', '-R', self.slug, '--state', 'open', '--limit', '200',
                      '--json', ','.join(PR_FIELDS)])
        if not r.ok:
            raise PortError('open PRs unreadable: %s' % r.reason)
        out = []
        self.open_heads = {d['number']: d.get('headRefOid') or '' for d in r.data or []
                           if isinstance(d, dict) and d.get('number')}
        for d in r.data or []:
            if self._revert_branch(d.get('headRefName')):
                continue  # the kernel's revert of a red trunk: never its item's own PR
            pr = self._open_pr(d)
            if pr is not None:
                out.append(pr)
        r = self._gh(['pr', 'list', '-R', self.slug, '--state', 'merged', '--limit',
                      str(self.MERGED_LIMIT), '--json', 'number,headRefName,headRefOid'])
        if not r.ok:
            raise PortError('merged PRs unreadable: %s' % r.reason)
        for d in r.data or []:
            iid = item_of_branch(d.get('headRefName'))
            if iid and not self._revert_branch(d.get('headRefName')):
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
                  checks=newest_checks(d.get('statusCheckRollup') or []),
                  auto_merge=bool(d.get('autoMergeRequest')),
                  clean=d.get('mergeStateStatus') == 'CLEAN',
                  auto_merge_at=str((d.get('autoMergeRequest') or {}).get('enabledAt') or ''),
                  lines=_lines(d))
        pr.tree_sha = self._tree(pr.head_sha)
        pr.change_id = self._change(d.get('baseRefName') or self.product.main, pr.head_sha)
        pr.latest_reviews = d.get('latestReviews') or []
        jobs = job_keys(d.get('statusCheckRollup') or [])
        for c in pr.checks:
            if c.status == 'completed' and c.conclusion in M.RED_CONCLUSIONS and c.run_id:
                self._red_detail(c, files, jobs.get((c.name, c.run_id)) or '')
        return pr

    def _tree(self, sha):
        """The commit's tree (a commit never changes its tree: read once, kept in the cache)."""
        if not sha:
            return ''
        got = self.cache.get('trees', sha)
        if got:
            return got
        r = self._gh(['api', 'repos/%s/git/commits/%s' % (self.slug, sha)])
        tree = ((r.data or {}).get('tree') or {}).get('sha', '') if r.ok else ''
        if tree:
            self.cache.put('trees', sha, tree)
        return tree

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
        got = self.cache.get('changes', '%s...%s' % (base, head))
        if got is not None:
            return got
        if self.slow:
            return ''  # a slowed tick reads no new change: the verdict keys by tree alone
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
        if cid:
            self.cache.put('changes', '%s...%s' % (base, head), cid)
        return cid

    def _red_detail(self, check, files, job=''):
        """A finished red check's run attempt and failed-log reading; a finished job never
        changes, so with its details URL (``job``) both are read once and kept in the cache."""
        seen = dict(self.cache.get('jobs', job) or {}) if job else {}
        if 'attempt' in seen:
            check.attempt = int(seen['attempt'] or 1)
        else:
            r = self._gh(['api', 'repos/%s/actions/runs/%d' % (self.slug, check.run_id)])
            if r.ok and isinstance(r.data, dict):
                check.attempt = int(r.data.get('run_attempt') or 1)
                seen['attempt'] = check.attempt
        if 'step' in seen:
            check.failing_files = list(seen.get('files') or [])
            check.failed_step, check.log_tail = seen['step'], seen.get('tail') or ''
        elif self._logs < self.LOG_LIMIT and not self.slow:
            self._logs += 1
            log = self._gh(['run', 'view', str(check.run_id), '-R', self.slug, '--log-failed'],
                           json=False)
            if log.ok:
                check.failing_files = failing_files(log.data or '', files)
                check.failed_step, check.log_tail = failed_step(log.data or '')
                seen.update(files=check.failing_files, step=check.failed_step,
                            tail=check.log_tail)
        if job and seen:
            self.cache.put('jobs', job, seen)

    def _rules(self, branch=None):
        """``rules/branches/<branch>`` (the trunk by default) as a list, or None when
        unreadable — read once per port (one tick) and shared by :meth:`required_checks` and
        :meth:`strict`."""
        branch = branch or self.product.main
        if not (self.slug and branch):
            return None
        memo = self.__dict__.setdefault('_rules_memo', {})
        if branch not in memo:
            r = self._gh(['api', 'repos/%s/rules/branches/%s' % (self.slug, branch)])
            memo[branch] = r.data if r.ok and isinstance(r.data, list) else None
        return memo[branch]

    def required_checks(self, branch=None):
        """The check names GitHub's rules require on ``branch`` (the trunk by default): every
        ``required_status_checks`` rule of ``rules/branches/<branch>``; ``()`` when unreadable."""
        return required_from_rules(self._rules(branch) or ())

    def strict(self, branch=None):
        """Whether the trunk's ruleset requires branches to be up to date
        (:func:`strict_from_rules`); True when the rules are unreadable (keep the merge train)."""
        rules = self._rules(branch)
        return True if rules is None else strict_from_rules(rules)

    def _revert_branch(self, branch):
        prefix = self.product.conventions.prefix('revert')
        return bool(prefix) and str(branch or '').startswith(prefix)

    def main_commits(self, limit=15):
        """The trunk's newest ``limit`` commits (:class:`~asf.kernel.model.MainCommit`, newest
        first) with the newest run of each check and the merged PR each squashes; the red checks
        of the newest red commit get their attempt and failed log (:meth:`_red_detail`). ``[]``
        when unreadable (the safety net then does nothing)."""
        owner, _, name = str(self.slug or '').partition('/')
        if not (owner and name):
            return []
        r = self._gh(['api', 'graphql', '-f', 'owner=%s' % owner, '-f', 'name=%s' % name,
                      '-f', 'ref=refs/heads/%s' % self.product.main, '-F', 'n=%d' % limit,
                      '-f', 'query=%s' % MAIN_QUERY])
        try:
            nodes = r.data['data']['repository']['ref']['target']['history']['nodes']
        except (AttributeError, KeyError, TypeError):
            return []
        if not r.ok or not isinstance(nodes, list):
            return []
        out, detailed = [], False
        for n in nodes:
            rollup = ((n.get('statusCheckRollup') or {}).get('contexts') or {}).get('nodes') or []
            prs = [p for p in ((n.get('associatedPullRequests') or {}).get('nodes') or [])
                   if p.get('merged')]
            p = prs[0] if prs else {}
            branch = str(p.get('headRefName') or '')
            c = M.MainCommit(
                sha=str(n.get('oid') or ''), headline=str(n.get('messageHeadline') or ''),
                pr=p.get('number'), branch=branch,
                item_id='' if self._revert_branch(branch) else (item_of_branch(branch) or ''),
                files=[f.get('path') for f in ((p.get('files') or {}).get('nodes') or [])
                       if f.get('path')],
                checks=newest_checks(rollup), lines=_lines(p))
            reds = [k for k in c.checks if k.status == 'completed'
                    and k.conclusion in M.RED_CONCLUSIONS and k.run_id]
            if reds and not detailed:
                detailed = True
                jobs = job_keys(rollup)
                for k in reds:
                    self._red_detail(k, c.files, jobs.get((k.name, k.run_id)) or '')
            out.append(c)
        self.cache.save()
        return out

    def revert_pr(self, sha, branch, title, body):
        """Open the PR of ``git revert <sha>`` (a squash commit on the trunk) on ``branch``,
        through the API alone: a commit with the tree of ``sha``'s parent on top of ``sha`` is
        merged into a branch at the trunk's head (GitHub's three-way merge; a fast-forward when
        ``sha`` is the head). An open PR on ``branch`` is reused; a branch already pushed only
        gets its PR. Returns the PR number; :class:`PortError` on a conflicting revert."""
        number = self._pr_of(branch)
        if number is not None:
            return number
        main = self.product.main
        r = self._gh(['api', 'repos/%s/git/ref/heads/%s' % (self.slug, branch)], retry=False)
        if not (r.ok and isinstance(r.data, dict)):
            got = self._gh(['api', 'repos/%s/commits/%s' % (self.slug, sha)])
            parents = (got.data or {}).get('parents') if got.ok and isinstance(got.data, dict) \
                else None
            if not parents:
                raise PortError('revert %s: its parent is unreadable' % sha)
            tree = self._gh(['api', 'repos/%s/git/commits/%s' % (self.slug, parents[0]['sha'])])
            tree_sha = ((tree.data or {}).get('tree') or {}).get('sha') if tree.ok else None
            head = self._gh(['api', 'repos/%s/git/ref/heads/%s' % (self.slug, main)])
            head_sha = ((head.data or {}).get('object') or {}).get('sha') if head.ok else None
            if not (tree_sha and head_sha):
                raise PortError('revert %s: the trunk or the parent tree is unreadable' % sha)
            msg = 'Revert %s\n\nThis reverts commit %s.' % (title, sha)
            rc = self._gh(['api', '-X', 'POST', 'repos/%s/git/commits' % self.slug,
                           '-f', 'message=%s' % msg, '-f', 'tree=%s' % tree_sha,
                           '-f', 'parents[]=%s' % sha], retry=False)
            undo = (rc.data or {}).get('sha') if rc.ok and isinstance(rc.data, dict) else None
            if not undo:
                raise PortError('revert %s: %s' % (sha, rc.reason))
            self._write(['api', '-X', 'POST', 'repos/%s/git/refs' % self.slug,
                         '-f', 'ref=refs/heads/%s' % branch,
                         '-f', 'sha=%s' % (undo if head_sha == sha else head_sha)],
                        'push %s' % branch)
            if head_sha != sha:
                m = self._gh(['api', '-X', 'POST', 'repos/%s/merges' % self.slug,
                              '-f', 'base=%s' % branch, '-f', 'head=%s' % undo,
                              '-f', 'commit_message=%s' % msg], retry=False)
                if not m.ok:
                    self._gh(['api', '-X', 'DELETE', 'repos/%s/git/refs/heads/%s'
                              % (self.slug, branch)], json=False, retry=False)
                    raise PortError('revert %s conflicts with the trunk: %s' % (sha, m.reason))
        return self.open_pr(branch, main, title, body)

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
        r = self._gh(args, json=False, retry=False)
        if not r.ok:
            raise PortError('%s: %s' % (what, r.reason))

    def enable_auto_merge(self, pr):
        merge = self.product.conventions.get('merge')
        method = (merge.get('method') if isinstance(merge, dict) else None) or 'squash'
        self._write(['pr', 'merge', str(pr), '-R', self.slug, '--auto', '--' + method],
                    'auto-merge #%d' % pr)

    def merge(self, pr, head_sha):
        """Merge ``pr`` now with the product's method, only while its head is ``head_sha``."""
        merge = self.product.conventions.get('merge')
        method = (merge.get('method') if isinstance(merge, dict) else None) or 'squash'
        self._forget(pr)
        self._write(['pr', 'merge', str(pr), '-R', self.slug, '--' + method,
                     '--match-head-commit', head_sha], 'merge #%d' % pr)

    def _forget(self, pr):
        """A write to PR ``pr`` drops the per-tick view of it (its head sha read before)."""
        if self.open_heads is not None:
            self.open_heads.pop(pr, None)

    def update_branch(self, pr):
        self._forget(pr)
        self._write(['api', '-X', 'PUT', 'repos/%s/pulls/%d/update-branch' % (self.slug, pr)],
                    'update-branch #%d' % pr)

    def rerun(self, run_id, cancel=False):
        if cancel:  # stalled past its bound: cancel now, the cancelled check is rerun next tick
            self._write(['run', 'cancel', str(run_id), '-R', self.slug], 'cancel %d' % run_id)
            return
        self._write(['run', 'rerun', str(run_id), '-R', self.slug, '--failed'],
                    'rerun %d' % run_id)

    #: the run statuses :meth:`active_runs` lists
    ACTIVE_RUN_STATUSES = ('queued', 'in_progress')

    def active_runs(self):
        """The repo's queued and running workflow runs (:class:`~asf.kernel.model.CIRun`, at most
        100 of each status): two reads, none while the budget is :attr:`slow`. Read before
        :meth:`prs`, so a run whose PR was open then and is missing from the open listing after
        is one whose PR closed."""
        self.read_budget()
        if self.slow:
            return []
        out = []
        for status in self.ACTIVE_RUN_STATUSES:
            r = self._gh(['api', 'repos/%s/actions/runs?status=%s&per_page=100'
                          % (self.slug, status)])
            if not r.ok:
                raise PortError('%s runs unreadable: %s' % (status, r.reason))
            for d in (r.data or {}).get('workflow_runs') or []:
                out.append(M.CIRun(
                    run_id=d.get('id'), head_sha=str(d.get('head_sha') or ''),
                    branch=str(d.get('head_branch') or ''), event=str(d.get('event') or ''),
                    prs=[p.get('number') for p in d.get('pull_requests') or []
                         if isinstance(p, dict) and p.get('number')]))
        return out

    def cancel_run(self, run_id):
        self._write(['run', 'cancel', str(run_id), '-R', self.slug], 'cancel %s' % run_id)

    def _prefixes(self):
        conv = self.product.conventions
        return sorted({p for p in (conv.prefix('code'), conv.prefix('fix')) if p})

    def branches(self):
        """Every branch on origin under the kernel's work prefixes (``code``, ``fix``) that names
        an item: ``git ls-remote origin refs/heads/<prefix>*`` in the product's checkout (no API
        cost), else ``git/matching-refs/heads/<prefix>``."""
        out = []
        for prefix in self._prefixes():
            listed = self._ls_remote(prefix)
            if listed is not None:
                for sha, name in listed:
                    iid = item_of_branch(name)
                    if iid:
                        out.append(M.Branch(name, iid, sha))
                continue
            r = self._gh(['api', 'repos/%s/git/matching-refs/heads/%s' % (self.slug, prefix)])
            if not r.ok or not isinstance(r.data, list):
                continue  # unknown is no pushed branch: nothing is opened on a guess
            for d in r.data or []:
                name = str((d or {}).get('ref') or '')[len('refs/heads/'):]
                iid = item_of_branch(name)
                if name and iid:
                    out.append(M.Branch(name, iid, str((d.get('object') or {}).get('sha') or '')))
        return out

    def _ls_remote(self, prefix):
        """``[(sha, branch)]`` under ``refs/heads/<prefix>`` on the checkout's origin, or None
        when there is no checkout or git could not answer (the API is read instead)."""
        repo = getattr(self.product, 'repo_dir', None)
        if not (repo and os.path.isdir(repo)):
            return None
        from asf import gitops
        r = (self._git or gitops.git)(['ls-remote', 'origin', 'refs/heads/%s*' % prefix], repo,
                                      timeout=60)
        if not r.ok:
            return None
        out = []
        for line in str(r.data or '').splitlines():
            sha, _, ref = line.partition('\t')
            if ref.startswith('refs/heads/') and sha:
                out.append((sha.strip(), ref.strip()[len('refs/heads/'):]))
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
                      '--head', branch, '--title', title, '--body', body], json=False,
                     retry=False)
        m = re.search(r'/pull/(\d+)', '%s\n%s' % (r.stdout or '', r.stderr or ''))
        if m and (r.ok or 'already exists' in (r.stderr or '')):
            return int(m.group(1))
        number = self._pr_of(branch)
        if number is not None:
            return number
        raise PortError('open PR %s: %s' % (branch, r.reason or 'no PR number in the reply'))

    def archive_and_reset(self, pr, branch, head_sha, comment):
        """Keep ``branch``'s head ``head_sha`` as ``archive/<branch>`` (created, or moved there),
        close PR ``pr`` with ``comment`` and delete ``branch`` on origin. Returns the archive
        branch. The archive is written first: a failure before the delete loses nothing."""
        if not head_sha:
            raise PortError('archive %s: no head sha' % branch)
        archive = ARCHIVE_PREFIX + branch
        self._forget(pr)
        r = self._gh(['api', '-X', 'POST', 'repos/%s/git/refs' % self.slug,
                      '-f', 'ref=refs/heads/%s' % archive, '-f', 'sha=%s' % head_sha],
                     retry=False)
        if not r.ok:
            self._write(['api', '-X', 'PATCH', 'repos/%s/git/refs/heads/%s' % (self.slug, archive),
                         '-f', 'sha=%s' % head_sha, '-F', 'force=true'], 'archive %s' % archive)
        self._write(['pr', 'close', str(pr), '-R', self.slug, '--comment', comment],
                    'close #%d' % pr)
        self._write(['api', '-X', 'DELETE', 'repos/%s/git/refs/heads/%s' % (self.slug, branch)],
                    'delete %s' % branch)
        return archive

    def close_pr(self, pr, comment):
        """Close PR ``pr`` with ``comment`` (its branch is kept)."""
        self._forget(pr)
        self._write(['pr', 'close', str(pr), '-R', self.slug, '--comment', comment],
                    'close #%d' % pr)


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
            if result is not None and kind in (dor_mod.GROOM_FILL, intake_mod.KIND):
                result = dict(result, result=run_text(run.get('log')))
            in_cloud = cloudpid.is_token(run.get('pid'))
            pushed = not alive and pushlog.count(self.product, run['job']) > 0
            said = reports.read(result)
            unpushed, refused = '', ''
            if result is not None and kind not in READ_ONLY and not pushed \
                    and (said['status'] == reports.DONE or run.get('kernel_pr') is not None):
                unpushed, refused = unpushed_head(
                    run.get('worktree') or '', run.get('branch') or '', self.product.main,
                    refguard_listed(self.product))
            landed = False
            if result is not None and kind not in READ_ONLY and not pushed \
                    and said['status'] == reports.DONE:
                landed = claim_landed(getattr(self.product, 'repo_dir', None),
                                      run.get('branch') or '', (said['fields'] or {}).get('pushed'))
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
                unpushed=unpushed, push_refused=refused, claim_landed=landed))
            out[-1].started = run.get('started') or ''
        return out

    def last_jobs(self):
        """``{item id: its last job}`` over the session ledger (launch order, ended runs too)."""
        from asf.workers import pool
        return {r['item']: r['job'] for r in pool.load_sessions(self.product).values()
                if r.get('item') and r.get('job')}

    def stranded(self, item_id):
        """The last ended (non-review) session of ``item_id`` whose kept worktree still holds a
        HEAD origin lacks — the rebase a refused force-push left — as a :class:`M.Session` with
        ``unpushed``/``push_refused`` read (:func:`unpushed_head`), else None."""
        from asf.workers import lifecycle, pool
        rows = [r for r in pool.load_sessions(self.product).values()
                if r.get('item') == item_id and not lifecycle.is_live(r)
                and _kernel_kind(r.get('kind')) not in READ_ONLY and r.get('worktree')
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
        """``(local live, cloud live, {account: live})`` over the ledger's runs that hold a seat:
        no ``ended`` line yet *and* the pid answers (a cloud run: its remote status is working).
        A dead run the tick has not ended yet holds no seat — ``decide`` counts only alive
        sessions, so a dead row counted here lost its seat twice (2026-10-10 10:24Z: two dead
        local rows on lane accounts read capacity 17 of 18; 10:36Z: the host lane read 10/10
        while 8 ran)."""
        from asf.workers import cloud, lifecycle, pool
        local = in_cloud = 0
        per = {}
        for run in pool.load_sessions(self.product).values():
            if lifecycle.is_live(run) and lifecycle.pid_alive(run.get('pid')):
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

    def capacity(self):
        """The seats a launch can take now, live ones included (``Facts.seats``):
        ``kernel.launch.local_max`` plus the cloud seats the lane can really fill — its live cloud
        runs plus the free seats of its accounts (:func:`asf.workers.cloud.lane_accounts`), at
        most ``cloud_max``; only the live cloud runs while the lane is off or its breaker tripped
        — and never more than the accounts' caps together. Measured 2026-10-10 09:44Z: 22
        configured seats (10 local + 12 cloud) on accounts holding 18, the breaker tripped: 16
        real, so 6 "free" seats only failed their launches."""
        from asf.workers import cloud, pool
        local_max, cloud_max = lane_seats(self.product, self.cfg())
        _local, in_cloud, live = self._live()
        accounts = pool.accounts_from_config(self.cfg())
        s = cloud_lane(self.product, self.cfg()) if cloud_max else None
        if s is None or cloud.Breaker(self.product, s).tripped():
            cloud_seats = in_cloud
        else:
            free = sum(max(0, a.cap - live.get(a.name, 0))
                       for a in cloud.lane_accounts(accounts, s))
            cloud_seats = min(cloud_max, in_cloud + free)
        seats = local_max + cloud_seats
        if accounts:
            seats = min(seats, sum(a.cap for a in accounts))
        return seats

    def seat_note(self):
        """One line on how :meth:`capacity` came out (a dry run prints it)."""
        from asf.workers import cloud, pool
        local_max, cloud_max = lane_seats(self.product, self.cfg())
        local, in_cloud, live = self._live()
        accounts = pool.accounts_from_config(self.cfg())
        s = cloud_lane(self.product, self.cfg()) if cloud_max else None
        tripped = cloud.Breaker(self.product, s).tripped() if s is not None else ''
        free = sum(max(0, a.cap - live.get(a.name, 0))
                   for a in cloud.lane_accounts(accounts, s)) if s is not None else 0
        return ('local %d/%d, cloud %d/%d (lane accounts free %d%s), accounts %s of cap %d '
                '-> capacity %d' % (local, local_max, in_cloud, cloud_max, free,
                                    '; breaker: %s' % tripped if tripped else '',
                                    ' '.join('%s %d/%d' % (a.name, live.get(a.name, 0), a.cap)
                                             for a in accounts),
                                    sum(a.cap for a in accounts), self.capacity()))

    def lane(self, kind, meta=None):
        """``'cloud'`` or ``'local'``: the seat a ``kind`` launch takes. A launch that needs the
        host (``meta['host']``: a rebase round, which the host publishes from its worktree; a
        kind :func:`asf.workers.cloud.local_only` keeps here) takes a local seat; every other one
        a cloud seat — if its brief kind is in ``kernel.launch.cloud_kinds`` (a cloud review
        reports on its own ``asf-reviews/<job>`` branch, never the PR's, so it restarts no CI) —
        while the lane has one (``kernel.launch.cloud_max``), its creates this tick
        are under ``cloud.max_creates_per_tick`` and its fallback breaker has not tripped; else a
        local seat (``kernel.launch.local_max``). Raises :class:`NoSeat` when neither has one."""
        from asf.workers import cloud
        local_max, cloud_max = lane_seats(self.product, self.cfg())
        local, in_cloud, _ = self._live()
        s = cloud_lane(self.product, self.cfg()) if cloud_max else None
        if (meta or {}).get('local_first') and local < local_max:
            return 'local'  # a breach relaunch: this host first, then the cloud lane
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
        if kind == 'review' and self._cloud_review_lost(item_id):
            # its last cloud review ended without a verdict: this one runs on the host
            meta = dict(meta or {}, host=True)
        if self.lane(bk, meta) == 'cloud':
            s = cloud_lane(self.product, self.cfg())
            try:  # every lane account full is a want of a seat, never a create error
                acct = self._account(s)
            except PortError as e:
                if self._live()[0] >= lane_seats(self.product, self.cfg())[0]:
                    raise NoSeat('no free seat: %s, and every local seat is taken' % e)
                acct = None
            if acct is None:
                job += '-local'
                self._spawn(kind, item_id, branch, brief, job, self._account())
                return self._meta(job, meta)
            self._creates += 1
            try:
                self._spawn(kind, item_id, branch, brief, job, acct,
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
        return self._meta(job, meta)

    def _meta(self, job, meta):
        """Put ``meta``'s ``pr``/``tree``/``change`` on ``job``'s ledger row; returns ``job``."""
        if meta and any(meta.get(k) is not None for k in ('pr', 'tree', 'change')):
            from asf.workers import pool
            pool.update_session(self.product, job, kernel_pr=meta.get('pr'),
                                kernel_tree=meta.get('tree'), kernel_change=meta.get('change'))
        return job

    def _cloud_review_lost(self, item_id):
        """True when ``item_id``'s last ended review was a cloud run that ended without its
        verdict (its ``cloud_why`` is not :func:`asf.workers.cloud.classify`'s finished line):
        the next review takes a local seat, so one lost cloud review is run again once on the
        host instead of dying the same way again and again (2026-10-10: 23 items sat in Review
        behind cloud reviews whose report never arrived)."""
        from asf.workers import pool
        last = None
        for run in pool.load_sessions(self.product).values():
            if (run.get('item') == item_id and _kernel_kind(run.get('kind')) == 'review'
                    and run.get('ended')):
                last = run
        if last is None or not cloudpid.is_token(last.get('pid')):
            return False
        return not str(last.get('cloud_why') or '').startswith('report commit')

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
        """End ``session`` on the ledger; a live one (past its max age) is stopped first —
        its process group, or its cloud run — and a stop that fails leaves it running."""
        from asf import env
        from asf.workers import lifecycle, pool, trash
        if session.alive:
            run = pool.load_sessions(self.product).get(session.job)
            if run is not None:
                ok, why = lifecycle.stop(pool.sessions_path(self.product), run)
                if not ok:
                    raise PortError('session %s not stopped: %s' % (session.job, why))
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


def report_result(log_path, holds=None):
    """The result record an ended session's REPORT is read off: the last run's ``result`` line
    (:func:`asf.workers.runtime.read_result`), unless it holds no REPORT and an earlier result of
    that same run does — a session that printed its REPORT and then answered a stale background
    notification (T-0432: "That monitor has served its purpose…") reported all the same. None
    while the run has not ended. ``holds``: what a result must hold instead of a REPORT (a
    callable on its text; a groom-fill's verdict block: its stop may have been refused and its
    last word be about that, live 2026-10-10)."""
    from asf.workers import report, runtime
    holds = holds or (lambda text: bool(report.parse(text)))
    last = runtime.read_result(log_path)
    if last is None or holds(str(last.get('result') or '')):
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
            elif rec.get('type') == 'result' and holds(str(rec.get('result') or '')):
                found = rec
    return found or last


def run_text(log_path):
    """Every text the last run of a session's log said — its assistant messages and results, in
    order — as one string: a groom-fill's verdict block is read off the last one it printed,
    wherever it printed it (live 2026-10-10: the pinned venv's stop gate refused the stop and
    the session's later messages were about that; its block sat in an earlier message)."""
    from asf.workers import runtime
    texts = []
    try:
        with open(log_path, encoding='utf-8', errors='replace') as f:
            for line in f:
                try:
                    rec = json.loads(line) if line.strip() else None
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if runtime.launch_boundary(rec):
                    texts = []
                elif rec.get('type') == 'result':
                    texts.append(str(rec.get('result') or ''))
                elif rec.get('type') == 'assistant':
                    for c in ((rec.get('message') or {}).get('content') or []):
                        if isinstance(c, dict) and c.get('type') == 'text':
                            texts.append(str(c.get('text') or ''))
    except (OSError, TypeError):
        return ''
    return '\n'.join(texts)


#: how many entries of a branch's reflog :func:`overwritable` reads for its pre-rebase history
REFLOG_DEPTH = 500


def overwritable(wt, head, origin, branch):
    """'' when pushing ``head`` over ``origin`` (origin's tip of ``branch``) loses nothing the
    worktree's own history never held, else why not: ``origin`` is an ancestor of ``head`` (a
    fast-forward), or of an entry of ``branch``'s reflog (the pre-rebase history the session
    rewrote), or every commit it has that ``head`` lacks is patch-equivalent to one in ``head``
    (``git cherry``). A tip that cannot be read even after a fetch is never overwritten."""
    from asf import gitops, mutation_guard
    if not origin or origin == head:
        return ''

    def has(sha):
        return gitops.git(['cat-file', '-e', '%s^{commit}' % sha], wt, timeout=60).ok

    def ancestor(of):
        return gitops.git(['merge-base', '--is-ancestor', origin, of], wt, timeout=60).ok

    if not has(origin):
        if not mutation_guard.is_active():  # a dry run fetches nothing
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


def claim_landed(repo, branch, value):
    """Whether a REPORT's ``pushed:`` ``value`` claims a push that origin holds now: the branch's
    head is read fresh (``git fetch`` of that one branch, never a cached listing) and the sha the
    claim names is that head or an ancestor of it (the session's report commit may sit on top of
    the pushed sha). A claim naming no sha counts when the branch is on origin."""
    from asf import gitops
    from asf.kernel import reports
    claimed, sha = reports.pushed_claim(value)
    if not (claimed and branch and repo and os.path.isdir(repo)):
        return False
    git = gitops.git
    ref = 'refs/heads/%s' % branch
    got = git(['fetch', '--quiet', '--no-tags', '--no-write-fetch-head', 'origin', ref], repo,
              timeout=120)
    if not got.ok:
        return False
    head = git(['ls-remote', 'origin', ref], repo, timeout=60)
    top = str(head.data or '').split('\t')[0].strip() if head.ok else ''
    if not top:
        return False
    if not sha:
        return True
    full = git(['rev-parse', '--verify', '--quiet', '%s^{commit}' % sha], repo, timeout=60)
    if not full.ok or not full.data:
        return False
    return git(['merge-base', '--is-ancestor', full.data, top], repo, timeout=60).ok


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
    if kind in READ_ONLY:
        return 'report'
    if said['question']:
        return 'question'
    return 'report' if said['fields'] else 'none'


def _under(path, parent):
    path, parent = os.path.realpath(path), os.path.realpath(parent)
    return path.startswith(parent.rstrip(os.sep) + os.sep)


# ---- the product's ports and knobs --------------------------------------------------------------

class Ports:
    """The three ports one tick uses, the brief maker ``brief(item, launch, findings, pr)``
    a launch hands its session (:class:`asf.kernel.briefs.Briefer` on the real ports), and the
    trunk probe that answers fact-checkable questions (:class:`asf.kernel.trunk.TrunkProbe`;
    None: none is probed)."""

    def __init__(self, record, github, sessions, brief=None, trunk=None):
        self.record, self.github, self.sessions, self.brief = record, github, sessions, brief
        self.trunk = trunk


def inbox_name(title):
    """The file name :func:`asf.groom.inbox.file_card` gives a card titled ``title``."""
    return (re.sub(r'[^a-z0-9]+', '-', str(title).lower()).strip('-') or 'card') + '.md'


def real_ports(product):
    from asf.kernel.briefs import Briefer
    from asf.kernel.trunk import TrunkProbe
    from asf import env
    state = os.path.join(env.ASF_HOME, 'state', product.name)
    cache = os.path.join(state, GH_CACHE_FILE)
    return Ports(RealRecord(product), RealGitHub(product, cache_path=cache),
                 RealSessions(product), Briefer(product), TrunkProbe.for_product(product, state))


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
    ``update_parallel``, and the Stuck escalation (``kernel.stuck``) from its ``kernel:``
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
        update_parallel=int(k['landing']['update_parallel']),
        landing_max_wait_h=float(k['landing']['max_wait_h']),
        escalate_after_h=float(k['stuck']['escalate_after_h']),
        rebuild_after_h=float(k['stuck']['rebuild_after_h']),
        strong_model=str(k['stuck']['strong_model']),
        id_claim_answer=bool(k['stuck']['id_claim_answer']),
        id_claim_prefixes=tuple(str(p) for p in k['stuck']['id_claim_prefixes']),
        resolve_trunk_tests=bool(k['resolve']['trunk_tests']),
        resolve_symbols=bool(k['resolve']['symbols']),
        resolve_gates=bool(k['resolve']['gates']),
        resolve_inbox_bugs=bool(k['resolve']['inbox_bugs']),
        resolve_needs_writes=bool(k['resolve']['needs_writes']),
        resolve_plan_ids=bool(k['resolve']['plan_ids']),
        replan_refused=bool(k['resolve']['replan']),
        wait_targets=dict(k['waits']['targets']) if k['waits']['breach'] else {},
        max_session_age_h=(k['waits']['max_session_age'] / 3600 if k['waits']['breach']
                           and k['waits']['max_session_age'] else None),
        max_review_age_h=(k['waits']['max_review_session_age'] / 3600 if k['waits']['breach']
                          and k['waits']['max_review_session_age'] else None),
        max_ci_age_h=(k['waits']['max_ci_age'] / 3600 if k['waits']['breach']
                      and k['waits']['max_ci_age'] else None),
        close_floor=bool(k['floor']['close_orphan_prs']),
        cancel_stale_ci=bool(k['floor']['cancel_stale_ci']),
        needed_satisfied=bool(k['gate']['satisfied']),
        main_red_revert=bool(k['landing']['main_red_revert']),
        revert_branch=conv.prefix('revert'),
        max_open_prs=int(k['launch']['max_open_prs']) or None,
        dor=bool(k['dor']['enabled']), dor_fill_per_tick=int(k['dor']['fill_per_tick']),
        dor_max_fills=int(k['dor']['max_fills']),
        dor_max_concurrent=int(k['dor']['max_concurrent']), groom_branch=conv.prefix('groom-fill'),
        intake=bool(k['intake']['enabled']),
        intake_decide_per_tick=int(k['intake']['decide_per_tick']),
        intake_max_tries=int(k['intake']['max_tries']),
        intake_branch=conv.prefix(intake_mod.KIND),
        risk_high=tuple(k['risk']['high']), risk_large_lines=int(k['risk']['large_lines']),
        plan_on_approve=bool(k['launch']['plan_on_approve']))



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
