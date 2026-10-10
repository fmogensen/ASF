"""asf.evidence.review_store — where a review lives: off the branch it reviews.

A review session once committed its review file onto the PR branch and pushed it. Every such
push moved the PR head: it cancelled and restarted the PR's whole CI (the heaviest run the
product has, a dozen times a day) and moved the head a merge watcher was pinned to — for a file
no check reads. So the review never touches the branch now.

**The writer.** The session writes the review in its worktree, at the path the brief names
(``conventions.review_path``), and commits nothing. When the run ends, the health pass files it
here (:func:`take`) — before anything could commit it as a leftover — and removes it from the
worktree, so the branch the session found is the branch it leaves. A session that committed the
review anyway, and nothing else, has that commit filed the same way and dropped unpushed.

**The binding.** An entry is keyed by the item, the branch, and the head the session reviewed:
the worktree's HEAD when the review was filed — the session commits nothing, so that is exactly
the code it read. A later push to the branch leaves the entry naming the old head, and the lane's
:func:`asf.evidence.review.is_current` reads it as history, as it read a branch file's ``head:``
line before.

**The readers** (:func:`asf.evidence.review.review_at`, the evidence scan, the brief facts)
look here first and still read the review files a branch carries — the reviews committed before
this store, and those a cloud session (whose only way back is the branch) commits. The newest
entry here wins unless the branch holds a strictly higher round.

Layout: ``<state dir>/reviews/<slug>/<branch key>/<stamp>-r<n>-<head>.md``. The stamp orders
the entries of one branch by filing time, so a branch recut after its PR closed (its rounds
reset to 1) reads its new review, not the old branch's higher round.
"""
import datetime
import os
import re
import subprocess

#: The store's directory under a product's state dir.
DIRNAME = 'reviews'
#: An entry's file name.
NAME_RE = re.compile(r'^(?P<stamp>\d{8}T\d{12}Z)-r(?P<n>\d+)-(?P<head>[0-9a-f]{7,40})\.md$')
#: How much of an entry is read.
READ_CHARS = 20000


def root(product):
    """The store's directory for ``product`` (``<state dir>/reviews``), or None."""
    if product is None:
        return None
    from asf import env
    try:
        return os.path.join(env.state_dir(product), DIRNAME)
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def branch_key(branch):
    """A branch name as one directory name: ``/`` and anything unsafe become ``__``."""
    b = str(branch or '')
    if b.startswith('origin/'):
        b = b[len('origin/'):]
    return re.sub(r'[^A-Za-z0-9._-]+', '__', b.strip('/')) or '_'


def _dir(store, slug, branch):
    return os.path.join(store, str(slug).lower(), branch_key(branch))


def _stamp(now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.strftime('%Y%m%dT%H%M%S%fZ')


def put(store, slug, branch, n, head, text, now=None):
    """File one review: ``text`` as round ``n`` of ``slug`` on ``branch``, bound to ``head``.
    Written whole or not at all (a temp file, then a rename). Returns the entry's path."""
    head = str(head or '').lower()
    if not store or not slug or not branch or not re.fullmatch(r'[0-9a-f]{7,40}', head):
        raise ValueError(f'review entry needs a store, a slug, a branch and a head sha '
                         f'(got {slug!r}, {branch!r}, {head!r})')
    d = _dir(store, slug, branch)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f'{_stamp(now)}-r{int(n)}-{head}.md')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text or '')
    os.replace(tmp, path)
    return path


def entries(store, slug, branch):
    """Every review of ``slug`` filed for ``branch``, oldest first:
    ``[{'round', 'head', 'file'}]``."""
    if not store or not slug or not branch:
        return []
    d = _dir(store, slug, branch)
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    out = []
    for name in names:
        m = NAME_RE.match(name)
        if m:
            out.append({'round': int(m.group('n')), 'head': m.group('head'),
                        'file': os.path.join(d, name)})
    return out


def newest(store, slug, branch):
    """The newest review of ``slug`` filed for ``branch``: ``{'round', 'head', 'file', 'text'}``,
    or None."""
    found = entries(store, slug, branch)
    if not found:
        return None
    hit = dict(found[-1])
    try:
        with open(hit['file'], encoding='utf-8', errors='replace') as f:
            hit['text'] = f.read(READ_CHARS)
    except OSError:
        return None
    return hit


def newest_of(store, slugs, branch):
    """:func:`newest` over several candidate slugs (an item and its aliases): the latest filed."""
    best = None
    for slug in dict.fromkeys(s for s in slugs or () if s):
        hit = newest(store, slug, branch)
        if hit and (best is None or os.path.basename(hit['file']) > os.path.basename(best['file'])):
            best = hit
    return best


def prefer(stored, branch_round):
    """True when a stored entry is the review to read over the branch's own newest file of
    round ``branch_round`` (None: the branch carries none): the branch wins only with a strictly
    higher round."""
    if not stored:
        return False
    return branch_round is None or int(stored['round']) >= int(branch_round)


# ---- the writer's side: filing a finished session's review -------------------------------------

class NotACheckout(ValueError):
    """The worktree a review is filed from is no git checkout of its own: nothing it holds can
    be read against a head, so the filing is refused out loud, never an empty ``[]``."""


def is_checkout(wt):
    """True when ``wt`` is the top of a git checkout — not a plain directory, and not a
    directory inside some other checkout (whose HEAD would be read as the worktree's)."""
    p = _git(wt, 'rev-parse', '--show-toplevel')
    return p.returncode == 0 and bool(p.stdout.strip()) \
        and os.path.realpath(p.stdout.strip()) == os.path.realpath(wt)


def _git(wt, *args):
    return subprocess.run(['git', '-C', wt, *args], capture_output=True, text=True, timeout=120)


def _round_of(conv, path, slug):
    from asf.evidence import review
    m = review.pattern_rx(conv, slug).fullmatch(path)
    return int(m.group('n')) if m else None


def _reviews_dir(conv):
    from asf.evidence import review
    return review._dir_of(conv)


def take(store, conv, wt, branch, item, remote_sha=''):
    """File the review a finished session left in its worktree ``wt`` on ``branch`` and take it
    out of the worktree. Returns ``[(round, entry path)]`` — ``[]`` when there was none.

    * an uncommitted file at ``conventions.review_path`` of ``item`` (any round) is filed under
      the worktree's HEAD and deleted (unstaged first when it was staged);
    * commits above ``remote_sha`` (origin's head of ``branch``) that touch nothing but the
      reviews directory — a session that committed its review out of habit — are filed under
      ``remote_sha`` (the code they sit on) and dropped (``git reset --keep``), never pushed.
      A commit touching anything else leaves the commits alone: that is work, not a review.

    A ``wt`` that is a directory but no git checkout raises :class:`NotACheckout` (a
    ``ValueError``): the health pass logs ``review not filed: …``, never a silent nothing.
    """
    if not store or not wt or not os.path.isdir(wt) or not branch or not item:
        return []
    if not is_checkout(wt):
        # a leftover directory with no checkout in it (a product's T-0091): the review the
        # session wrote cannot be bound to a head — say so, the run is not "no review"
        raise NotACheckout(f'{wt} is not a git checkout: the review of {item} on {branch} '
                           f'cannot be filed from it')
    slug = str(item).lower()
    rdir = _reviews_dir(conv) + '/'
    filed = []
    remote_sha = (remote_sha or '').strip()
    head = _git(wt, 'rev-parse', 'HEAD').stdout.strip()
    if remote_sha and head and head != remote_sha \
            and _git(wt, 'merge-base', '--is-ancestor', remote_sha, 'HEAD').returncode == 0:
        changed = [p for p in _git(wt, 'diff', '--name-only', remote_sha, 'HEAD').stdout.splitlines()
                   if p.strip()]
        ours = [p for p in changed if _round_of(conv, p, slug) is not None]
        if ours and all(p.startswith(rdir) for p in changed):
            texts = [(p, _git(wt, 'show', f'HEAD:{p}')) for p in ours]
            if all(t.returncode == 0 for _p, t in texts) \
                    and _git(wt, 'reset', '-q', '--keep', remote_sha).returncode == 0:
                for p, t in sorted(texts, key=lambda pt: _round_of(conv, pt[0], slug)):
                    n = _round_of(conv, p, slug)
                    filed.append((n, put(store, slug, branch, n, remote_sha, t.stdout)))
                head = remote_sha
    st = _git(wt, 'status', '--porcelain', '--untracked-files=all', '-z', '--', rdir)
    if st.returncode != 0 or not head:
        return filed
    for entry in st.stdout.split('\0'):
        if len(entry) < 4 or entry[:2] not in ('??', 'A ', 'AM'):
            continue
        path = entry[3:]
        n = _round_of(conv, path, slug)
        if n is None:
            continue
        full = os.path.join(wt, path)
        try:
            with open(full, encoding='utf-8', errors='replace') as f:
                text = f.read()
        except OSError:
            continue
        filed.append((n, put(store, slug, branch, n, head, text)))
        if entry[:2] != '??':
            _git(wt, 'rm', '-q', '--cached', '--', path)
        try:
            os.remove(full)
        except OSError:
            pass
    return filed


#: Where a :func:`recover`-ed entry's text came from.
FROM_WORKTREE, FROM_REPORT = 'the review file in its worktree', 'the session REPORT'


def _resolved_head(repo, sha):
    sha = (sha or '').strip().lower()
    if not re.fullmatch(r'[0-9a-f]{7,40}', sha):
        return None
    if _git(repo, 'rev-parse', '--verify', '--quiet', f'{sha}^{{commit}}').returncode == 0:
        return sha
    return None


def recover(store, conv, repo, wt, branch, item, pr_head='', report='', launch_head=''):
    """File the review :func:`take` refused — a worktree ``wt`` that is no git checkout, so
    nothing in it can be bound to a head the usual way. Reads the review **without git**: the
    file at ``conventions.review_path`` in ``wt`` if one is there
    (:func:`asf.evidence.review.worktree_review`, :data:`FROM_WORKTREE`, the newest round in the
    directory wins), or — no worktree, no directory, no file — the verdict block of the finished
    session's ``report`` (:data:`FROM_REPORT`, :func:`asf.evidence.review.recovered_review`).

    The head is never read from ``wt`` — not one git question can be answered there. It is
    resolved against ``repo`` (the product's own checkout) in one order and one order only: the
    review text's own verdict block head, then its ``head:`` line, then ``launch_head``, then
    ``pr_head`` — the first of the four ``repo`` can verify as a real commit. None resolvable, or
    nothing to read in the first place, returns None having written nothing: the review's own
    head wins so a recovery can never bind an approval to code the session did not read.

    Files through :func:`put` exactly as :func:`take` does — the same layout, the same stamped
    name, indistinguishable from an entry ``take`` filed. Never writes into ``wt`` and never
    deletes from it, so a read-only worktree recovers the same way a writable one does. Returns
    ``(round, entry path, source)``, or None.
    """
    from asf.evidence import review
    hit = review.worktree_review(conv, wt, item)
    if hit is not None:
        n, _path, text = hit
        source = FROM_WORKTREE
    else:
        n, text, source = 1, report or '', FROM_REPORT

    v = review.verdict_block(text)
    head = None
    for candidate in (v.head if v else None, review.head_of(text), launch_head, pr_head):
        head = _resolved_head(repo, candidate)
        if head:
            break
    if head is None:
        return None

    if source == FROM_REPORT:
        text = review.recovered_review(report, head, item=item, n=n, source=FROM_REPORT)
        if not text:
            return None

    entry = put(store, str(item).lower(), branch, n, head, text)
    return n, entry, source
