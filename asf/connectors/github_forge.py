"""asf.connectors.github_forge — the default ``forge`` connector: GitHub, through ``gh``.

A thin, named layer over :mod:`asf.github` (the ``gh`` transport: rate-limit latch, dry-run
guard, timeouts, Unknown on failure). Each method calls its :mod:`asf.github` reader by module
attribute at call time, so the behaviour is exactly that reader's.
"""
from asf import github


class GitHubForge:
    name = 'github'

    def __init__(self, cfg=None):
        self.cfg = cfg

    def pr(self, slug, number, fields, **kw):
        return github.pr(slug, number, fields, **kw)

    def prs(self, slug, **kw):
        return github.prs(slug, **kw)

    def open_prs(self, slug, **kw):
        return github.open_prs(slug, **kw)

    def merge_commit(self, slug, number, **kw):
        return github.merge_commit(slug, number, **kw)

    def checks(self, slug, sha, **kw):
        return github.checks(slug, sha, **kw)

    def close_pr(self, slug, number, comment='', **kw):
        args = ['pr', 'close', str(number), '-R', slug]
        if comment:
            args += ['--comment', comment]
        return github.gh(args, **kw)

    def reopen_pr(self, slug, number, **kw):
        return github.gh(['pr', 'reopen', str(number), '-R', slug], **kw)

    def api(self, path, *args, **kw):
        return github.api(path, *args, **kw)

    def auth_status(self, **kw):
        return github.gh(['auth', 'status'], **kw)
