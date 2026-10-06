"""asf.connectors.ci_github — the default ``ci`` connector: GitHub Actions, through ``gh``.

A thin, named layer over :mod:`asf.github` (the ``gh`` transport) and over the two GitHub
Actions adapters that already exist: :class:`asf.ci_queue.GitHubSource` (what the CI start queue
reads and does) and :class:`asf.ci_pool.GitHubBackend` (the self-hosted runner pool). Each is
looked up by module attribute at call time, so the behaviour is exactly that code's.
"""
from asf import github


class GitHubActionsCI:
    name = 'github-actions'

    def __init__(self, cfg=None):
        self.cfg = cfg

    def runs(self, slug, **kw):
        return github.runs(slug, **kw)

    def run_log(self, slug, run_id, **kw):
        return github.run_log(slug, run_id, **kw)

    def run(self, slug, run_id, **kw):
        return github.api(f'repos/{slug}/actions/runs/{run_id}', **kw)

    def runs_for_sha(self, slug, sha, **kw):
        return github.api(f'repos/{slug}/actions/runs?head_sha={sha}&per_page=100', **kw)

    def rerun(self, slug, run_id, failed=True, **kw):
        args = ['run', 'rerun', str(run_id)]
        if failed:
            args.append('--failed')
        return github.gh(args + ['-R', slug], **kw)

    def cancel(self, slug, run_id, **kw):
        return github.gh(['run', 'cancel', str(run_id), '-R', slug], **kw)

    def call(self, args, **kw):
        return github.gh(args, **kw)

    def source(self, product, run=None):
        from asf import ci_queue
        return ci_queue.GitHubSource(product, run=run) if run is not None \
            else ci_queue.GitHubSource(product)

    def backend(self, product, run=None):
        from asf import ci_pool
        return ci_pool.GitHubBackend(product, run=run) if run is not None \
            else ci_pool.GitHubBackend(product)
