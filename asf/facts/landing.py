"""asf.facts.landing — did an item's work land on the trunk: ``Landed`` | ``NotLanded`` | ``Unknown``.

Fourteen readers answer this question each its own way (a run line's ``harvested:``, a commit
naming the item, a REPORT's sha that covers the item's ``writes:``, an open PR …) and each reads
a git or host that could not answer as "no". :func:`landed` is the one answer, in rule order:

0. **voided** — a sha the item's ledger voided (``asf reset``: the void's ``head``, a run whose
   lane PR a standing reset names, a commit that is the merge of a voided PR) is never a landing,
   whatever the host says;
1. **pr-merge** — a run line's ``harvested:`` (the lane's merge, its PR's merge commit),
   attributed by :func:`asf.workers.landing.attribution` (its name, its PR's merge);
2. **names** — a trunk commit whose subject or trailer names the item
   (:func:`asf.workers.landing.names`), since the item's last reset;
3. **trunkclose** — the newest ended run's REPORT says ``status: done`` and names a trunk
   commit that is not its own work (:func:`asf.workers.trunkclose.trunk_sha`), attributed by
   ``names`` or ``pr`` — the arm is part of the answer (``by: trunkclose/<arm>``).

A commit that only **covers** the item's ``writes:`` is never a landing: it fills
:attr:`NotLanded.hint_sha`. A landed commit a later trunk commit reverts (``This reverts commit
<sha>``) is ``NotLanded('reverted by …')``. A landing is ``NotLanded('unmerged work on <branch>')``
while a branch of the item's runs (one not itself landed) or an open PR naming it still carries
commits the trunk does not. Any question git could not answer, or the open PRs not read this
pass, is ``Unknown`` — never a "no".

The host is read only through :mod:`asf.facts.cache` (the pass's one ``open_prs`` read — the
tick primes it, :func:`prime`): the shadow adds no gh load (S-M13).

**Shadow** (W6-PR2). The workers' deciders — :func:`asf.workers.trunkclose.evidence`, the landed
half of :func:`asf.workers.relaunch.assess`, :func:`asf.workers.landing.verify_landings`,
:func:`asf.workers.lifecycle.landed` and :func:`~asf.workers.lifecycle.landed_earlier` — hand
their own answer to :func:`shadow` with the fact as the other side. It always returns the
decider's answer; under ``flags.facts: shadow`` (or ``new``: these deciders have not cut over yet)
it logs a disagreement to ``facts-disagree.jsonl`` once a day per ``(decider, key, old, new)``.
:func:`replay` (``asf facts replay``) runs the same comparison offline over the ledger.
"""
import datetime
import json
import os
import re

from asf import gh_limit, github, gitops
from asf.facts import cache, disagree
from asf.facts.types import AsOf, Landed, NotLanded, OpenPrs, Unknown, is_unknown

FACT = 'landed'
#: The deciders shadowed here (the ``decider`` of a disagreement record).
TRUNKCLOSE, RELAUNCH, VERIFY = 'trunkclose', 'relaunch', 'verify_landings'
LIFECYCLE, EARLIER = 'lifecycle.landed', 'lifecycle.landed_earlier'
DECIDERS = (TRUNKCLOSE, RELAUNCH, VERIFY, LIFECYCLE, EARLIER)

#: How far back the trunk is read for commits naming an item, and for reverts.
NAMES_SCAN = 50
REVERT_SCAN = 2000
#: An open-PR list this long may be cut short (:data:`asf.workers.landing.PR_LIMIT`).
PR_LIMIT = 300
REVERT_RE = re.compile(r'This reverts commit ([0-9a-f]{7,40})')
#: A disagreement already logged within this many hours is not logged again.
DEDUPE_HOURS = 24

#: The cache's key for the product the pass primed (:func:`bind`): the run-level shadow
#: (:func:`shadow_run`) only knows a run, never its product.
_BOUND = ('', 'facts.landing.bound', '', '')


# ---- the pass ----------------------------------------------------------------------------------

def prime(product, *, run=None):
    """Start a pass's shadow for ``product``: the one open-PR read (:func:`asf.facts.cache.prime`)
    and the binding the run-level shadow reads (:func:`bind`) — only when the product's mode is
    not ``old`` (an ``old`` product spends nothing). Never raises."""
    from asf import facts
    try:
        if facts.mode(product) == facts.OLD:
            return None
        got = cache.prime(product, run=run)
        bind(product)
        return got
    except Exception as e:  # noqa: BLE001 — the shadow's setup never takes a tick down
        print(f'FACTS: shadow not primed ({type(e).__name__}: {e})', flush=True)
        return None


def bind(product):
    """The product this pass shadows for: what :func:`shadow_run` reads."""
    cache.put(*_BOUND[:3], _BOUND[3], product)


def bound():
    """The product :func:`bind` named this pass, or None."""
    got = cache.get(*_BOUND[:3], _BOUND[3])
    return None if got is cache.MISS else got


# ---- the fact ----------------------------------------------------------------------------------

def _where(product, repo, main, path):
    repo = repo or getattr(product, 'repo_dir', None)
    main = main or getattr(product, 'main', None) \
        or getattr(getattr(product, 'conventions', None), 'main', None) or 'main'
    if path is None and getattr(product, 'name', None):
        from asf import env
        from asf.workers import pool as pool_mod
        try:
            path = pool_mod.sessions_path(
                product if isinstance(product, env.Product) else str(product.name))
        except Exception:  # noqa: BLE001 — no state dir: no ledger
            path = None
    return repo, main, path


def _memo(product, kind, key, head, fn):
    got = cache.get(product, kind, key, head)
    return got if got is not cache.MISS else cache.put(product, kind, key, head, fn())


def _tip(product, repo, main):
    """``origin/<main>``'s sha this pass, '' when there is none, None when git could not tell."""
    return _memo(product, 'landing.tip', f'{repo}\0{main}', '',
                 lambda: gitops.rev_parse(repo, f'refs/remotes/origin/{main}'))


def _reverts(product, repo, main, tip):
    """``{reverted sha: reverting sha}`` over the trunk's newest :data:`REVERT_SCAN` commits, or
    None when git could not read them."""
    def read():
        r = gitops.git(['log', f'-n{REVERT_SCAN}', '-F', '--grep=This reverts commit',
                        '--format=%H%x00%B%x1e', f'origin/{main}'], repo)
        if not r.ok:
            return None
        out = {}
        for chunk in r.data.split('\x1e'):
            sha, _, body = chunk.strip().partition('\x00')
            for target in REVERT_RE.findall(body):
                out.setdefault(target, sha)
        return out
    return _memo(product, 'landing.reverts', f'{repo}\0{main}', tip, read)


def _reverted_by(reverts, sha):
    for target, by in (reverts or {}).items():
        if len(sha) >= 7 and (target.startswith(sha) or sha.startswith(target)):
            return by
    return ''


def _named_commits(repo, main, item, since=''):
    """The trunk's newest :data:`NAMES_SCAN` commits whose message mentions ``item`` (newest
    first; :func:`asf.workers.landing.names` then decides), or None when git could not read."""
    args = ['log', f'-n{NAMES_SCAN}', '-F', '-i', f'--grep={item}', '--format=%H']
    if since:
        args.append(f'--since={since}')
    r = gitops.git([*args, f'origin/{main}'], repo)
    return [s for s in r.data.split() if s] if r.ok else None


def _folded(product, path):
    """The ledger folded once this pass (:func:`asf.workers.lifecycle._folded` reads the file)."""
    from asf.workers import lifecycle
    return _memo(product, 'landing.folded', path or '', '', lambda: lifecycle._folded(path))


def _voided(folded, item):
    """``{pr: reset}`` of the item's standing resets (a void among them): a PR a reset names is
    never the item's landing, nor is its merge commit (rule 0)."""
    return {str(v.get('pr')).lstrip('#'): v for v in folded.standing.get(item, ()) if v.get('pr')}


def _merge_of(repo, sha, by_pr):
    """The entry of ``by_pr`` (``{pr number: value}``) whose PR commit ``sha`` is the merge of
    (``… (#n)``, ``Merge pull request #n``, ``merge-queue: #n``), or None."""
    subject = gitops.log1(repo, sha, '%s') or ''
    for n, value in by_pr.items():
        if n.isdigit() and re.search(
                rf'(\(#{n}\)\s*$|^Merge pull request #{n}\b|^merge-queue: #{n}\b)', subject):
            return value
    return None


def _why_void(v):
    return (f"voided by {v.get('by') or 'operator'} at {v.get('at') or '?'}: "
            f"{v.get('why') or 'reset'}")


def landed(product, item, *, sha='', repo=None, main=None, path=None, writes=None, prs=None,
           branches=(), open_prs=None):
    """The landing fact of ``item`` (see the module doc): :class:`Landed` ``(sha, by)``,
    :class:`NotLanded` ``(why, hint_sha)`` or :class:`Unknown`. ``sha``: a landing the caller
    recorded, judged first. ``open_prs``: the open PRs to read instead of the pass's cache (the
    offline :func:`replay`). Memoised for the pass."""
    repo, main, path = _where(product, repo, main, path)
    if not repo or not item:
        return Unknown('no repo' if not repo else 'no item', AsOf.now())
    tip = _tip(product, repo, main)
    if not tip:
        return Unknown(f'origin/{main} could not be read' if tip is None
                       else f'no origin/{main}', AsOf.now())
    key = json.dumps([item, sha, list(branches or ()), list(writes or ()) if writes else None,
                      open_prs is not None])
    return _memo(product, 'landing.landed', key, tip, lambda: _landed(
        product, item, sha, repo, main, path, writes, prs, branches, open_prs, tip))


def _landed(product, item, sha, repo, main, path, writes, prs, branches, open_prs, tip):
    from asf.evidence import evidence as ev
    from asf.workers import landing, lifecycle, relaunch, trunkclose
    as_of = AsOf.now(tip)
    folded = _folded(product, path)
    runs = folded.by_item().get(item, []) if path else []
    if writes is None:
        writes = landing.item_writes(product, item)
    if prs is None:
        prs = landing.run_prs(path, item)
    try:
        prefixes = ev.branch_prefixes(product)
    except Exception:  # noqa: BLE001 — no prefixes: no document lane known
        prefixes = {}
    voided_prs = _voided(folded, item)
    reverts = _reverts(product, repo, main, tip)
    seen, state = set(), {'unknown': '', 'voided': '', 'reverted': '', 'hint': ''}
    # a document lane's merges (a spec, a plan) land a document, never the item's work
    docs = {}
    for r in runs:
        if ev.lane_kind(r.get('branch') or '', prefixes):
            lane = r.get('lane') if isinstance(r.get('lane'), dict) else {}
            if lane.get('pr'):
                docs[str(lane['pr']).lstrip('#')] = r
            for h in (r.get('harvested'), lane.get('sha')):
                if h and h not in lifecycle.NOT_A_LANDING:
                    seen.add(str(h)[:7])

    def judge(cand, rule, run=None):
        """``(sha, by)`` when ``cand`` is the item's landing, else None (``state`` says why)."""
        if not cand or cand in lifecycle.NOT_A_LANDING or cand[:7] in seen:
            return None
        seen.add(cand[:7])
        void = (lifecycle.voided_run(path, run, cand, folded=folded) if run
                else lifecycle.voided_sha(path, item, cand))
        if not void and voided_prs:
            void = _merge_of(repo, cand, voided_prs)
        if rule == 'names':
            subject = gitops.log1(repo, cand, '%s')
            if subject is None:
                state['unknown'] = state['unknown'] or f'git could not read {cand[:9]}'
                return None
            if ev.lands_nothing(subject) or (docs and _merge_of(repo, cand, docs)):
                return None  # a report, spec, plan or review commit names, never lands
        if void:
            state['voided'] = state['voided'] or _why_void(void)
            return None
        lane = (run or {}).get('lane') if isinstance((run or {}).get('lane'), dict) else {}
        if lane.get('pr') and lane.get('state') == 'MERGED' \
                and lifecycle._sha_match(lane.get('sha') or cand, cand):
            # the lane recorded its PR merged at this sha: the PR's merge, whatever its subject
            # (an attested batch, a reworded squash) — once the trunk carries it
            trunk = landing.on_trunk(repo, main, cand)
            arm = None if trunk is None else ('pr' if trunk else '')
        else:
            arm = landing.attribution(repo, main, cand, item, writes, prs)
        if arm is None:
            state['unknown'] = state['unknown'] or f'git could not attribute {cand[:9]}'
            return None
        if arm == 'covers':
            state['hint'] = state['hint'] or trunkclose.full_sha(repo, cand) or cand
            return None
        if not arm:
            return None
        if reverts is None:
            state['unknown'] = state['unknown'] or 'the trunk\'s reverts could not be read'
            return None
        by_revert = _reverted_by(reverts, cand)
        if by_revert:
            state['reverted'] = state['reverted'] or f'reverted by {by_revert[:9]}'
            return None
        if rule == 'trunkclose':
            return cand, f'trunkclose/{arm}'
        return cand, 'pr-merge' if arm == 'pr' or (lane.get('pr') and arm == 'names') else arm

    def candidates():
        if sha:
            run = next((r for r in runs if lifecycle._sha_match(r.get('harvested'), sha)), None)
            yield sha, 'recorded', run
        for r in sorted(runs, key=lambda r: r.get('started') or '', reverse=True):
            h = str(r.get('harvested') or '')
            if h and not ev.lane_kind(r.get('branch') or '', prefixes):
                yield h, 'harvested', r
        since = (folded.resets.get(item) or {}).get('at') or ''
        named = _named_commits(repo, main, item, since)
        if named is None:
            state['unknown'] = state['unknown'] or 'git could not read the trunk\'s log'
        for c in named or ():
            yield c, 'names', None
        run = trunkclose.newest_ended(path, item) if path else None
        if run and not ev.lane_kind(run.get('branch') or '', prefixes):
            text = relaunch._result_text(run)
            if relaunch.terminal(text).startswith(trunkclose.DONE):
                c, _arm = trunkclose.trunk_sha(repo, main, run, text, item, writes, prs)
                if c:
                    yield c, 'trunkclose', None

    hit = None
    for cand, rule, run in candidates():
        hit = judge(cand, rule, run)
        if hit:
            break
    if not hit:
        if state['unknown']:
            return Unknown(state['unknown'], as_of)
        why = state['voided'] or state['reverted'] or 'no trunk commit attributable to it'
        return NotLanded(why, as_of, hint_sha=state['hint'])
    got, by = hit
    work = _open_work(product, repo, main, item, runs, branches, open_prs)
    if is_unknown(work):
        return Unknown(work.reason, as_of)
    if work:
        return NotLanded(f'unmerged work on {work}', as_of, hint_sha=got)
    return Landed(trunkclose.full_sha(repo, got) or got, by, as_of)


def _open_work(product, repo, main, item, runs, branches, open_prs):
    """The first branch still holding ``item``'s unmerged work — of ``branches`` or its runs (a
    branch a run landed aside: its commits are on the trunk under the merge), or the head of an
    open PR naming it — '' when none, :class:`Unknown` when git or the pass's PR list could not
    tell."""
    from asf.evidence import evidence as ev
    from asf.workers import lifecycle
    landed_on = {r.get('branch') for r in runs
                 if r.get('harvested') and r.get('harvested') not in lifecycle.NOT_A_LANDING}
    for b in dict.fromkeys([*(branches or ()), *(r.get('branch') for r in runs)]):
        if not b or b == main or b in landed_on:
            continue
        ref = gitops.rev_parse(repo, f'refs/remotes/origin/{b}')
        if ref is None:
            return Unknown(f'git could not read origin/{b}', AsOf.now())
        if not ref:
            continue
        n = gitops.rev_list_count(repo, f'origin/{main}', f'origin/{b}')
        if n is None:
            return Unknown(f'git could not compare origin/{b}', AsOf.now())
        if n:
            return b
    got = open_prs if open_prs is not None else cache.open_prs(product)
    if is_unknown(got):
        return Unknown(f'open PRs: {got.reason}', got.as_of)
    limit = github.pr_list_limit(PR_LIMIT)
    if len(got.prs) >= limit:
        return Unknown(f'open PRs: {limit} listed, the list may be cut short', got.as_of)
    want = str(item).upper()
    for pr in got.prs:
        head = pr.get('headRefName') or ''
        if want not in ev.naming_ids(pr.get('title') or '') and want not in ev.branch_ids(head):
            continue
        oid = pr.get('headRefOid') or ''
        if not oid or gitops.is_ancestor(repo, oid, f'origin/{main}') is not True:
            return head or f"#{pr.get('number')}"  # not fetched counts: its commits are unknown
    return ''


def landed_run(product, run, path=None):
    """Did ``run``'s own landing (its ``harvested:``) hold: :class:`Landed` ``(sha, 'harvested')``,
    or :class:`NotLanded` — no landing, an archive mark (``superseded``), a voided one, one a trunk
    commit reverted — or :class:`Unknown` (the trunk's reverts unreadable). Cheap: one folded
    ledger and one revert scan per pass, whatever the number of runs."""
    from asf.workers import lifecycle
    run = run or {}
    sha = str(run.get('harvested') or '')
    repo, main, path = _where(product, None, None, path)
    if not sha:
        return NotLanded('not harvested', AsOf.now())
    if sha in lifecycle.NOT_A_LANDING:
        return NotLanded(f'{sha}: archived, nothing reached the trunk', AsOf.now())
    void = lifecycle.voided_run(path, run, folded=_folded(product, path))
    if void:
        return NotLanded(_why_void(void), AsOf.now())
    tip = _tip(product, repo, main) if repo else None
    if not tip:
        return Unknown(f'origin/{main} could not be read', AsOf.now())
    reverts = _reverts(product, repo, main, tip)
    if reverts is None:
        return Unknown('the trunk\'s reverts could not be read', AsOf.now(tip))
    by = _reverted_by(reverts, sha)
    if by:
        return NotLanded(f'reverted by {by[:9]}', AsOf.now(tip))
    return Landed(sha, 'harvested', AsOf.now(tip))


def landed_earlier_run(product, path, run):
    """:func:`asf.workers.lifecycle.landed_earlier` by the fact: the sha of the newest earlier run
    on ``run``'s branch whose landing holds (:func:`landed_run`), None, or :class:`Unknown`."""
    branch, started = (run or {}).get('branch'), (run or {}).get('started') or ''
    if not path or not branch:
        return None
    for rs in _folded(product, path).view.values():
        for r in rs:
            if r.get('branch') != branch or not r.get('harvested') \
                    or (r.get('started') or '') >= started:
                continue
            got = landed_run(product, r, path)
            if isinstance(got, Landed):
                return got.sha
            if is_unknown(got):
                return got
    return None


# ---- the shadow --------------------------------------------------------------------------------

def outcome(value):
    """A decider's or the fact's answer as ``landed`` / ``not`` / ``unknown``."""
    if isinstance(value, Landed):
        return 'landed'
    if isinstance(value, NotLanded):
        return 'not'
    if is_unknown(value) or value == 'unknown' or (getattr(value, 'why', None) is not None
                                                   and not value):
        return 'unknown'  # a decider's own falsy Unknown (asf.workers.trunkclose.Unknown) too
    return 'landed' if value else 'not'


def agree_tri(old, new):
    """Three-valued: the decider's ``landed``/``not``/``unknown`` is the fact's."""
    return outcome(old) == outcome(new)


def agree_closes(old, new):
    """Two-valued: the decider closes exactly when the fact says Landed (an Unknown closes
    nothing, like a "no")."""
    return (outcome(old) == 'landed') == (outcome(new) == 'landed')


def _sig(value):
    """What a disagreement is about, without its clock."""
    if isinstance(value, Landed):
        return ['Landed', value.sha[:9], value.by]
    if isinstance(value, NotLanded):
        return ['NotLanded', value.why, value.hint_sha[:9]]
    if isinstance(value, Unknown):
        return ['Unknown', value.reason]
    return disagree.render(value)


def _seen(product):
    def read():
        since = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(hours=DEDUPE_HOURS)).strftime('%Y-%m-%dT%H:%M:%SZ')
        out = set()
        for r in disagree.records(product, since):
            if r.get('fact') == FACT:
                out.add(_key(r.get('decider'), r.get('key'), r.get('old'), r.get('new'),
                             rendered=True))
        return out
    return _memo(product, 'landing.seen', '', '', read)


def _key(decider, key, old, new, rendered=False):
    if rendered:
        n = new
        if isinstance(n, dict) and n.get('fact') in ('Landed', 'NotLanded', 'Unknown'):
            n = {'Landed': lambda d: ['Landed', str(d.get('sha', ''))[:9], d.get('by')],
                 'NotLanded': lambda d: ['NotLanded', d.get('why'), str(d.get('hint_sha', ''))[:9]],
                 'Unknown': lambda d: ['Unknown', d.get('reason')]}[n['fact']](n)
        return json.dumps([decider or '', str(key), old, n], sort_keys=True, default=str)
    return json.dumps([decider or '', str(key), disagree.render(old), _sig(new)],
                      sort_keys=True, default=str)


def shadow(product, decider, key, old, new_fn, *, agree=agree_tri, view=None):
    """``old`` — always. Under ``flags.facts`` ``shadow`` (or ``new``: the workers' deciders
    have not cut over) the fact ``new_fn()`` runs beside it; whatever it raises is logged as
    ``error:<Type>``, and a disagreement (``agree(old, new)`` false) is logged to
    ``facts-disagree.jsonl`` — once a day per ``(decider, key, old, new)``. ``view(old)`` is
    what the log shows of the decider's answer."""
    from asf import facts
    if product is None or facts.mode(product) == facts.OLD:
        return old
    shown = view(old) if view else old
    try:
        new = new_fn()
    except (Exception, gh_limit.RateLimited) as e:  # noqa: BLE001 — never raises into old
        new = f'error:{type(e).__name__}'
    else:
        try:
            if agree(old, new):
                return old
        except Exception as e:  # noqa: BLE001
            new = f'error:{type(e).__name__} (agree)'
    try:
        sig, seen = _key(decider, key, shown, new), _seen(product)
        if sig in seen:
            return old
        seen.add(sig)
    except Exception:  # noqa: BLE001 — a dedupe that cannot run logs
        pass
    disagree.log(product, FACT, key, shown, new, decider=decider)
    return old


def shadow_run(run, old):
    """:func:`asf.workers.lifecycle.landed`'s shadow: ``old`` — always; the run's own landing
    (:func:`landed_run`) compared beside it when this pass bound a product (:func:`bind`) whose
    ledger carries the run. A run with no landing mark, or an archive mark (``superseded`` — the
    harvest's "done with it", which the decider means), is not asked."""
    from asf.workers import lifecycle
    product = bound()
    sha = (run or {}).get('harvested')
    if product is None or not old or not sha or sha in lifecycle.NOT_A_LANDING:
        return old
    path = _where(product, None, None, None)[2]
    try:
        if (run or {}).get('job') not in _folded(product, path).view:
            return old  # another product's run
    except Exception:  # noqa: BLE001
        return old
    key = f"{run.get('job')}@{str(sha)[:9]}"
    return shadow(product, LIFECYCLE, key, old,
                  lambda: _memo(product, 'landing.run', key, '',
                                lambda: landed_run(product, run, path)),
                  agree=agree_closes)


def shadow_earlier(path, run, old):
    """:func:`asf.workers.lifecycle.landed_earlier`'s shadow: ``old`` — always; the fact's
    earlier landing on the run's branch (:func:`landed_earlier_run`) compared beside it."""
    product = bound()
    if product is None or not path:
        return old
    try:
        mine = _where(product, None, None, None)[2]
        if not mine or os.path.abspath(mine) != os.path.abspath(path):
            return old
    except Exception:  # noqa: BLE001
        return old
    key = f"{(run or {}).get('job')}@{(run or {}).get('started') or ''}"
    return shadow(product, EARLIER, key, old,
                  lambda: landed_earlier_run(product, path, run),
                  agree=lambda o, n: (o or None) == (None if is_unknown(n) else n)
                  or (is_unknown(n) and not o))


# ---- the offline replay ------------------------------------------------------------------------

def replay(product, since='', out=print):
    """Old vs new over the ledger, offline (``asf facts replay``): every item with a run started
    at or after ``since`` — the trunk check (:func:`asf.workers.trunkclose.evidence`, the host
    unasked), the landing verification (:func:`asf.workers.landing.attributable` on the
    lifecycle's recorded landing) — and every run's ``harvested:`` (:func:`landed_run`) against
    the fact. The host is not read on either side (an open PR is invisible to both: the live
    shadow is the evidence for that half). Prints one line per disagreement and a summary;
    returns ``{decider: [(key, old, new)]}``."""
    from asf.workers import landing, lifecycle, trunkclose
    repo, main, path = _where(product, None, None, None)
    found = {d: [] for d in (TRUNKCLOSE, VERIFY, LIFECYCLE)}
    if not repo or not path:
        out(f'facts replay: {getattr(product, "name", product)} has no repo or no ledger')
        return found
    none_open = OpenPrs((), AsOf.now())
    folded = _folded(product, path)
    items = {}
    for rs in folded.view.values():
        for r in rs:
            if r.get('item') and isinstance(r.get('item'), str) \
                    and (r.get('started') or '') >= (since or ''):
                items.setdefault(r['item'], []).append(r)
    occ = lifecycle.occupancy(path, alive=lambda *_a, **_k: False)
    recorded = occ.get('landed') or {}
    for item in sorted(items):
        writes = landing.item_writes(product, item)
        prs = landing.run_prs(path, item)

        def fact(sha='', branches=()):
            return landed(product, item, sha=sha, repo=repo, main=main, path=path,
                          writes=writes, prs=prs, branches=branches, open_prs=none_open)
        try:
            run = trunkclose.newest_ended(path, item)
            if run is not None and not run.get('trunk_closed'):
                old = trunkclose._evidence(path, item, repo, main, writes, ask_gh=False)
                new = fact(branches=[run.get('branch')])
                if not agree_tri(old, new):
                    found[TRUNKCLOSE].append((item, view_evidence(old), new))
            sha = recorded.get(item)
            if sha and landing.on_trunk(repo, main, sha):
                old = landing.attributable(repo, main, sha, item, writes, prs)
                new = fact(sha=sha)
                old = 'unknown' if old is None else bool(old)
                if not agree_tri(old, new):
                    found[VERIFY].append((item, old, new))
        except Exception as e:  # noqa: BLE001 — one item's error is a line, not the replay's end
            out(f'facts replay: {item}: {type(e).__name__}: {e}')
        for r in items[item]:
            h = r.get('harvested')
            if not h or h in lifecycle.NOT_A_LANDING:
                continue
            new = landed_run(product, r, path)
            if not agree_closes(True, new):
                found[LIFECYCLE].append((f"{r.get('job')}@{str(h)[:9]}", True, new))
    total = 0
    for decider, rows in found.items():
        for key, old, new in rows:
            total += 1
            out(f'{decider:<18} {key:<24} old={json.dumps(disagree.render(old))} '
                f'new={json.dumps(_sig(new))}')
    per = ', '.join(f'{d} {len(r)}' for d, r in found.items())
    out(f'facts replay: {len(items)} item(s) since {since or "the start"}: {total} '
        f'disagreement(s) ({per})')
    return found


def view_evidence(old):
    """:func:`asf.workers.trunkclose.evidence`'s answer as the log shows it."""
    if isinstance(old, tuple) and old:
        run = old[1] if len(old) > 1 and isinstance(old[1], dict) else {}
        return {'sha': old[0], 'arm': run.get('trunk_arm', ''), 'job': run.get('job', '')}
    if old is not None and not old and hasattr(old, 'why'):
        return f'unknown: {old.why}'
    return old


def cmd_facts(args):
    """``asf facts replay --product P [--since ISO]``."""
    from asf import env
    product = env.load_product(getattr(args, 'product', None))
    found = replay(product, getattr(args, 'since', '') or '')
    return 0 if found is not None else 1


def register(sub):
    p = sub.add_parser('facts', help='compare the landing fact with the old deciders, offline')
    p.add_argument('action', choices=('replay',))
    p.add_argument('--since', default='', help='replay the runs started at or after this ISO '
                                               'time (default: the whole ledger)')
    from asf import env
    env.add_product_arg(p)
    p.set_defaults(func=cmd_facts)
    return p
