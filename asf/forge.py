"""asf.forge — the one door for the factory's forge-side clean-up actions.

A leftover the factory acts on (:mod:`asf.stale_act`) lives partly on the forge: a pull request
to close, a branch to archive or delete, a CI run to cancel. Each action goes through a
:class:`Forge` chosen per product by :func:`for_product`, so the policy that decides *what* to
do never knows *which* host does it:

* :class:`GitHubForge` — a product with a hosted ``owner/name`` slug: PRs are listed and closed
  with ``gh``, an archive commit and its ref are made through the host's API (no push, no
  product hook), a branch is deleted only while its tip is still the one read (a lease), and a
  run cancel is :func:`asf.run_cancel.cancel`.
* :class:`GitForge` — a plain-git product (no hosted slug): there are no PRs and no CI runs
  (``has_prs`` / ``has_runs`` false — a caller says "not applicable", never red); the archive and
  the delete are ref-only ``git push`` es to ``origin``.

Every call answers ``(ok, detail)`` and never raises, except a rate limit
(:class:`asf.gh_limit.RateLimited`), which stops the caller's pass. The trunk and a
``conventions.protected_refs`` ref are refused before any call (:mod:`asf.refguard`). A dry run
in progress (:mod:`asf.mutation_guard`) refuses the mutating ``gh`` calls inside
:mod:`asf.github` itself.
"""
from asf import github, gitops, gitpush, refguard

#: the ref namespace an archived tip is kept under (the retention sweep ages it out)
ARCHIVE_PREFIX = 'archive/'


class Forge:
    """The interface. ``slug`` is the hosted ``owner/name`` (None for plain git), ``repo`` the
    product's checkout, ``trunk`` its main branch, ``protected`` the extra refs never written."""
    name = 'none'
    has_prs = False
    has_runs = False

    def __init__(self, repo=None, trunk='main', protected=None, slug=None):
        self.repo, self.trunk, self.slug = repo, trunk, slug
        #: None: :data:`asf.refguard.DEFAULT_PROTECTED_REFS`
        self.protected = None if protected is None else tuple(protected)

    def guard(self, ref, what):
        return refguard.refusal(f'refs/heads/{ref}', what, self.trunk, self.protected,
                                out=lambda _s: None)

    # -- reads ------------------------------------------------------------------------------
    def open_prs(self):
        """``{number: {branch, head}}`` of the open PRs, ``{}`` when the forge has none, or None
        when the answer is Unknown (never read as "none open")."""
        return {}

    # -- actions ----------------------------------------------------------------------------
    def archive(self, name, tip, message):
        """Keep ``tip`` as ``archive/<name>``: one ``[skip ci]`` commit over it (its tree, ``tip``
        its one parent — so the retention sweep dates the archive from today). ``(ok, sha|why)``;
        an archive already standing over ``tip`` counts as made."""
        raise NotImplementedError

    def close_pr(self, number, reason):
        return False, 'no pull requests on this forge'

    def delete_branch(self, branch, head):
        """Delete ``branch`` while its tip is still ``head``. ``(ok, why)``; gone already is ok."""
        raise NotImplementedError

    def cancel_run(self, run_id, status=None):
        return False, 'no CI runs on this forge'


class GitHubForge(Forge):
    name = 'github'
    has_prs = True
    has_runs = True

    def open_prs(self):
        r = github.open_prs(self.slug)
        if not r.ok or not isinstance(r.data or [], list):
            return None
        return {int(p['number']): {'branch': p.get('headRefName') or '',
                                   'head': p.get('headRefOid') or ''}
                for p in r.data or () if isinstance(p, dict) and p.get('number')}

    def _ref_sha(self, ref):
        r = github.gh(['api', f'repos/{self.slug}/git/ref/heads/{ref}', '--jq', '.object.sha'])
        return (r.stdout or '').strip() if r.ok else ''

    def archive(self, name, tip, message):
        ref = f'{ARCHIVE_PREFIX}{name}'
        guard = self.guard(ref, 'archive')
        if guard:
            return False, guard
        cur = self._ref_sha(ref)
        if cur:
            if cur == tip:
                return True, cur
            parents = github.gh(['api', f'repos/{self.slug}/git/commits/{cur}', '--jq',
                                 '.parents[].sha'])
            if parents.ok and tip in (parents.stdout or '').split():
                return True, cur
            return False, f'{ref} already stands at {cur[:9]}, not over {tip[:9]}'
        tree = github.gh(['api', f'repos/{self.slug}/git/commits/{tip}', '--jq', '.tree.sha'])
        if not tree.ok or not (tree.stdout or '').strip():
            return False, f'tip {tip[:9]} unreadable ({tree.reason or "no tree"})'
        made = github.gh(['api', '-X', 'POST', f'repos/{self.slug}/git/commits',
                          '-f', f'message={message}', '-f', f'tree={tree.stdout.strip()}',
                          '-f', f'parents[]={tip}', '--jq', '.sha'])
        sha = (made.stdout or '').strip() if made.ok else ''
        if not sha:
            return False, f'archive commit not made ({made.reason})'
        put = github.gh(['api', '-X', 'POST', f'repos/{self.slug}/git/refs',
                         '-f', f'ref=refs/heads/{ref}', '-f', f'sha={sha}'])
        if put.ok or self._ref_sha(ref) == sha:
            return True, sha
        return False, f'{ref} not created ({put.reason})'

    def close_pr(self, number, reason):
        r = github.gh(['pr', 'close', str(number), '-R', self.slug, '--comment', reason])
        return r.ok, r.reason

    def delete_branch(self, branch, head):
        guard = self.guard(branch, 'delete')
        if guard:
            return False, guard
        cur = self._ref_sha(branch)
        if not cur:
            return True, 'already gone'
        if head and cur != head:
            return False, f'tip moved ({cur[:9]} is not {head[:9]}) — kept'
        r = github.gh(['api', '-X', 'DELETE', f'repos/{self.slug}/git/refs/heads/{branch}'])
        return r.ok, r.reason

    def cancel_run(self, run_id, status=None):
        from asf import run_cancel

        def call(args):
            r = github.gh(args)
            return r.ok, r.stdout
        c = run_cancel.cancel(call, self.slug, run_id, status=status)
        return bool(c), c.detail


class GitForge(Forge):
    """Plain git: ref-only pushes to ``origin`` from the product's checkout."""
    name = 'git'

    def _remote_sha(self, ref):
        r = gitops.git(['ls-remote', '--heads', 'origin', gitops.head_ref(ref)], self.repo)
        return gitops.head_sha(r.stdout, ref) if r.ok else None

    def _push(self, args):
        """One ref-only push through the push door (:func:`asf.gitpush.push`); ``(ok, why)``."""
        p = gitpush.push(args, self.repo, refs_only=True,
                         guard=refguard.Guard(self.trunk, self.protected))
        why = ((p.stderr or '').strip().splitlines() or [f'git exit {p.returncode}'])[0]
        return p.returncode == 0, '' if p.returncode == 0 else why

    def archive(self, name, tip, message):
        ref = f'{ARCHIVE_PREFIX}{name}'
        guard = self.guard(ref, 'archive')
        if guard:
            return False, guard
        if not self.repo:
            return False, 'no checkout'
        made = gitops.git(['commit-tree', f'{tip}^{{tree}}', '-p', tip, '-m', message], self.repo)
        if not made.ok or not made.data:
            return False, f'archive commit not made ({made.reason})'
        push = self._push(['-q', 'origin', f'{made.data}:refs/heads/{ref}'])
        return (True, made.data) if push[0] else (False, f'{ref} not pushed ({push[1]})')

    def delete_branch(self, branch, head):
        guard = self.guard(branch, 'delete')
        if guard:
            return False, guard
        if not self.repo:
            return False, 'no checkout'
        cur = self._remote_sha(branch)
        if cur is None:
            return False, 'origin unreadable'
        if not cur:
            return True, 'already gone'
        lease = [f'--force-with-lease=refs/heads/{branch}:{head}'] if head else []
        return self._push(['-q', *lease, 'origin', f':refs/heads/{branch}'])


def for_product(product):
    """The product's :class:`Forge`: GitHub when it names a hosted ``repo_slug``, else plain
    git over its checkout."""
    conv = product.conventions
    protected = refguard.listed(conv)
    kw = dict(repo=product.repo_dir, trunk=conv.main, protected=protected)
    slug = (product.repo_slug or '').strip()
    if slug and '/' in slug:
        return GitHubForge(slug=slug, **kw)
    return GitForge(**kw)
