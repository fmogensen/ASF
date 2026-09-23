"""asf.workers.githooks — the per-product git hook dir a session runs under (F-0076, D3/D4/D13).

``ensure(product)`` writes ``state/<product>/githooks/asf-hook`` and one pass-through file per
client-side hook name (:data:`HOOKS`), and returns the directory — handed to the session as
``core.hooksPath`` through :func:`asf.workers.runtime.build_env`. Every hook in the dir ``exec``s
``asf-hook <name>``, which stamps an ``ASF-Session`` trailer on ``prepare-commit-msg`` and then
chains to the product repo's own hook of the same name, found at hook time (not frozen in at
spawn, since ``core.hooksPath`` can change without a respawn). A file is rewritten only when its
content differs, so a session that runs ``ensure`` on every launch never touches a file it
already wrote correctly. Nothing here is written into the product repo or its ``.git``.
"""
import os

from asf import env

#: The client-side hook names from githooks(5) — every one a shim passes through to the
#: product's own hook of the same name.
HOOKS = ('applypatch-msg', 'pre-applypatch', 'post-applypatch', 'pre-commit', 'pre-merge-commit',
         'prepare-commit-msg', 'commit-msg', 'post-commit', 'pre-rebase', 'post-checkout',
         'post-merge', 'pre-push', 'post-rewrite', 'reference-transaction', 'pre-auto-gc',
         'post-index-change', 'push-to-checkout', 'sendemail-validate')

#: The shim every hook name in the dir is: ``exec``s ``asf-hook`` with its own name.
_SHIM = '#!/bin/sh\nexec "$(dirname "$0")/asf-hook" {name} "$@"\n'

#: 1. the hook name, and stamp the ``ASF-Session`` trailer on a ``prepare-commit-msg``;
#: 2. find the product's own hooks dir at hook time (D4): ``core.hooksPath`` with
#:    ``GIT_CONFIG_COUNT`` unset, anchored at the worktree's toplevel if relative, else
#:    ``<git-common-dir>/hooks``;
#: 3. nothing to chain when that dir is this one;
#: 4. otherwise exec the product's own hook of the same name, stdin passed through by ``exec``.
ASF_HOOK = r'''#!/bin/sh
# written by asf.workers.githooks.ensure — the ASF-Session trailer and hook passthrough (F-0076)
name=$1
shift
here=$(cd "$(dirname "$0")" && pwd -P)

if [ "$name" = "prepare-commit-msg" ] && [ -n "$ASF_SESSION" ]; then
    git interpret-trailers --in-place --if-exists doNothing \
        --trailer "ASF-Session: $ASF_SESSION" "$1"
fi

own=$(env -u GIT_CONFIG_COUNT git config --get core.hooksPath 2>/dev/null)
if [ -n "$own" ]; then
    case "$own" in
        /*) : ;;
        *) top=$(git rev-parse --show-toplevel 2>/dev/null) && own="$top/$own" ;;
    esac
else
    common=$(git rev-parse --git-common-dir 2>/dev/null) && own="$common/hooks"
fi

if [ -n "$own" ] && [ -d "$own" ]; then
    resolved=$(cd "$own" && pwd -P)
    [ "$resolved" = "$here" ] && exit 0
fi

if [ -n "$own" ] && [ -x "$own/$name" ]; then
    exec "$own/$name" "$@"
fi

exit 0
'''


def _write_if_changed(path, text):
    data = text.encode('utf-8')
    if os.path.isfile(path):
        with open(path, 'rb') as f:
            if f.read() == data:
                return
    with open(path, 'wb') as f:
        f.write(data)
    os.chmod(path, 0o755)


def ensure(product):
    """Writes ``state/<product>/githooks/asf-hook`` and one pass-through file per :data:`HOOKS`
    name, each rewritten only when its content differs. Returns the directory."""
    d = os.path.join(env.state_dir(product), 'githooks')
    os.makedirs(d, exist_ok=True)
    _write_if_changed(os.path.join(d, 'asf-hook'), ASF_HOOK)
    for name in HOOKS:
        _write_if_changed(os.path.join(d, name), _SHIM.format(name=name))
    return d
