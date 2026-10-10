"""asf.kernel.needed_probe — the host's reads behind the still-needed gate (ASF 0.2).

:func:`read` fills ``Facts.needed`` for :mod:`asf.kernel.needed`: for each fresh launch candidate
(:func:`asf.kernel.needed.candidates`, in launch order) that names its proving tests
(:func:`asf.kernel.needed.test_ids`):

- its **baseline**: the trunk commit that first added its plan document (``links: {plan: …}``),
  else the newest trunk commit before its creation date; none -> not probed;
- its **new ids**: the named ids absent at the baseline (the module file missing, or the class /
  method not defined in it: :func:`asf.kernel.trunk.symbol_in`) — a test that already existed
  proves nothing about the item. Every new id must be present at ``origin/<main>`` (else nothing
  is run: the item is not done);
- the **run**: the new ids through :meth:`asf.kernel.trunk.TrunkProbe.probe` (``trunk-tests``: a
  fresh detached worktree of origin's trunk, the per-tick cap and cache the question resolvers
  share; nothing runs in a dry run, a cached result is read).

``{iid: {ids, baseline, tests}}`` (``tests`` the run's result, absent until it ran); nothing
while the trunk probe's ``trunk-tests`` class is off. :meth:`NeededProbe.landed` reads which shas
a done REPORT names the trunk holds (``Facts.landed_shas``). Baselines and
new ids are kept by item in ``state/<product>/kernel-needed.json``. Every read is a local git read
of the product checkout; a failure leaves the item unprobed (it launches as before).
"""
import json
import os
import time

from asf.kernel import needed as N
from asf.kernel import resolvers as R
from asf.kernel import trunk as T

CACHE_FILE = 'kernel-needed.json'

#: a cached baseline older than this is read again (seconds)
CACHE_TTL_S = 14 * 86400


class NeededProbe:
    """The gate's reads on ``trunk``'s repository (:class:`asf.kernel.trunk.TrunkProbe`).
    ``git(args)`` returns ``(ok, stdout)``; the default runs :func:`asf.gitops.git` in the repo."""

    def __init__(self, trunk, git=None):
        self.trunk = trunk
        self._git = git

    def git(self, args):
        if self._git is not None:
            return self._git(args)
        from asf import gitops
        r = gitops.git(args, self.trunk.repo)
        return bool(r.ok), (r.data or '') if r.ok else ''

    # ---- the cache ----------------------------------------------------------------------------

    def _path(self):
        return os.path.join(self.trunk.state_dir, CACHE_FILE)

    def _load(self):
        try:
            with open(self._path(), encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        now = time.time()
        return {k: v for k, v in data.items() if isinstance(v, dict)
                and now - float(v.get('at') or 0) < CACHE_TTL_S} if isinstance(data, dict) else {}

    def _save(self, cache):
        from asf import mutation_guard
        if mutation_guard.is_active():
            return
        os.makedirs(self.trunk.state_dir, exist_ok=True)
        with open(self._path() + '.tmp', 'w', encoding='utf-8') as f:
            json.dump(cache, f, indent=1, sort_keys=True)
        os.replace(self._path() + '.tmp', self._path())

    # ---- git reads -----------------------------------------------------------------------------

    def baseline(self, item, main):
        """The trunk commit ``item`` was planned against, or ''."""
        if item.plan:
            ok, out = self.git(['log', main, '--diff-filter=A', '--format=%H', '--reverse', '--',
                                item.plan])
            first = out.split()[0] if ok and out.split() else ''
            if first:
                return first
        day = str(item.created or '')[:10]
        if len(day) == 10:
            ok, out = self.git(['rev-list', '-1', '--before=%sT00:00:00Z' % day, main])
            return out.strip() if ok else ''
        return ''

    def _show(self, sha, path):
        ok, out = self.git(['show', '%s:%s' % (sha, path)])
        return out if ok else None

    def defined(self, sha, tid):
        """Whether test id ``tid`` (a module, ``module.Class``, ``module.Class.method``) is
        defined at ``sha``."""
        parts = tid.split('.')
        for n in range(len(parts), 0, -1):
            path = '/'.join(parts[:n]) + '.py'
            src = self._show(sha, path)
            if src is not None:
                if n == len(parts):
                    return True
                return bool(T.symbol_in(src, path, parts[n:]).get('exists'))
        return False

    # ---- the read ---------------------------------------------------------------------------

    def landed(self, sessions):
        """``Facts.landed_shas``: each sha an ended ``done`` build REPORT names -> its full sha
        when ``origin/<main>`` holds it, else ''."""
        main, out = 'origin/%s' % self.trunk.main, {}
        for s in sessions:
            if s.alive or not s.ended or s.kind == 'review' or s.status != 'done':
                continue
            for sha in N.report_shas(s)[:5]:
                if sha in out:
                    continue
                ok, full = self.git(['rev-parse', '--verify', '-q', sha + '^{commit}'])
                full = full.strip() if ok else ''
                if full:
                    ok, _ = self.git(['merge-base', '--is-ancestor', full, main])
                out[sha] = full if ok and full else ''
        return out

    def read(self, items, prs=(), sessions=(), inherit=True):
        """``Facts.needed`` (see the module doc)."""
        main = 'origin/%s' % self.trunk.main
        cache, out, probes = self._load(), {}, []
        if not self.trunk.enabled.get(R.TRUNK_TESTS):
            return out
        for iid in N.candidates(items, prs, sessions, inherit):
            it = items[iid]
            ids = N.test_ids(it, items)
            if not ids:
                continue
            key = '%s|%s|%s|%s' % (iid, it.plan, it.created, ','.join(ids))
            got = cache.get(key)
            if got is None:
                base = self.baseline(it, main)
                got = {'at': time.time(), 'baseline': base,
                       'new': [i for i in ids if not self.defined(base, i)] if base else []}
                cache[key] = got
            new = list(got.get('new') or [])
            if not got.get('baseline') or not new:
                continue
            if not all(self.defined(main, i) for i in new):
                continue
            out[iid] = {'ids': new, 'baseline': got['baseline']}
            probes.append((iid, R.Probe(R.TRUNK_TESTS, tuple(new))))
        self._save(cache)
        if probes:
            results = self.trunk.probe([p for _iid, p in probes], more=True) or {}
            for iid, p in probes:
                if p.key in results:
                    out[iid]['tests'] = results[p.key]
        return out
