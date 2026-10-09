"""asf.security — the security pass in two parts, all off ``conventions.security``: which of the
product's own named classes a diff's files fall under (:mod:`asf.security.paths`, read by the
precheck pass), and the host's own secret- and dependency-scanning feeds read as ASF's own Bugs
(:mod:`asf.security.alerts`). Unset, nothing is sensitive and no feed is read.

No line this package prints, holds or files ever carries a machine address or an alert value
(D11, P20) — only what a check found: counts, classes, an age.
"""
import os

from asf import env


def gh_env(product):
    """The environment ``gh`` runs with: this process's, with the product's ``auth_env`` files
    read over it — the behaviour :func:`asf.ci_pool._gh_env` documents. An unreadable file leaves
    the ambient login in place. It opens the files named in the product's config and spells no
    path of its own."""
    out = dict(os.environ)
    for var, path in env.product_auth_env(product).items():
        try:
            with open(path, encoding='utf-8') as f:
                value = f.read().strip()
        except OSError:
            continue
        if value:
            out[var] = value
    return out


from asf.security.doctor import doctor_findings  # noqa: E402 — after gh_env, so the import
# back into this half-initialized package (asf.security.alerts' own ``from asf.security import
# gh_env``) finds it already defined; no cycle, no optional dependency.
