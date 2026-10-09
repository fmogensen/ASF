"""asf.dispatch — the dispatcher at ``~/.local/bin/asf``: one stable path, each product's own pin.

Every caller outside the factory's own processes names one path for the CLI: the worker
accounts' hook entries (``asf hook approvals``, the ``unpushed`` Stop gate), a product's tracked
``pre-push``, the session's ``asf-hook`` and a session's own ``asf land`` / ``asf set`` all run
``$HOME/.local/bin/asf`` (a session's isolated HOME links it, :func:`asf.workers.runtime.
link_factory_cli`). When that path is one shared install, every product runs whichever version
was installed last — a product pinned to an older sha still runs the newest code in its hooks,
and an upgrade that recreates the venv breaks a running session's hooks mid-call.

So the path is a POSIX ``sh`` script (no python before the ``exec``) that picks the product and
``exec``s the ``bin/asf`` of that product's pinned venv, read from ``state/<p>/install.json``:

1. the product: ``--product <p>`` / ``--product=<p>`` in the arguments, else ``$ASF_PRODUCT``,
   else the product whose ``repo_dir`` / ``backlog_dir`` / state directory holds the cwd
   (:func:`asf.env.product_of_dir`, the one python call, made only when neither names one);
2. that product's pin, when its venv's ``bin/asf`` is executable;
3. else the default product's pin (``config.yaml``'s ``default_product``, named at write time
   and read at run time — so a move of the default product needs no rewrite);
4. else the CLI baked in at write time (:func:`default_cli`);
5. else exit 127 with one line naming the product.

``install.json``'s ``venv`` is an absolute venv directory or a bare venv name under the pipx
venvs directory; its ``previous: {sha, venv}`` is never taken. ``ASF_DISPATCH_TRACE=1`` makes
the script print, on stderr, which product it resolved, from what, and which CLI it runs
(the hook smoke script reads that line).

:func:`install` writes the script — only where no file is, or over a file this module wrote (the
:data:`MARKER` line). A pipx link at the path is moved aside to ``<path>.pipx-link`` and
replaced (F-0283: while it held the path, every hook ran the shared install, never a product's
pin); its CLI is the last fallback the script bakes when no product is pinned. Any other file is
refused.

``python -m asf.dispatch --product-of-dir <dir>`` prints the product holding ``<dir>`` (step 1's
cwd lookup), or nothing.
"""
import json
import os
import shlex
import sys

from asf import env

#: The header line that marks the file as this module's: :func:`install` rewrites a file that
#: carries it and refuses any other.
MARKER = '# asf dispatcher — written by `asf hooks install`'

#: The dispatcher's place, relative to a HOME: where hooks, git hooks and scripts call the CLI.
REL_PATH = os.path.join('.local', 'bin', 'asf')

#: The suffix a shared install's link (pipx) at the dispatcher's path is kept under when
#: :func:`install` replaces it.
LINK_BACKUP = '.pipx-link'

#: A product's pin record, relative to its state directory (written by ``asf upgrade --product``).
INSTALL_RECORD = 'install.json'

_SCRIPT = r'''#!/bin/sh
{marker}; rewritten by every install.
# It runs the asf CLI of the product's pinned venv (<asf home>/state/<product>/install.json), so a
# hook, a git hook or a session's own command follows that product's pin (asf.dispatch).
ASF_DISPATCH_HOME={home}
ASF_DISPATCH_VENVS={venvs}
ASF_DISPATCH_DEFAULT_PRODUCT={default_product}
ASF_DISPATCH_DEFAULT_CLI={default_cli}
ASF_DISPATCH_PY={py}

asf_home=${{ASF_HOME:-$ASF_DISPATCH_HOME}}

# $1 = a product: prints its pinned venv's bin/asf when that is executable; "previous" is skipped
pin_cli() {{
    case "$1" in ''|*/*|.*) return 1 ;; esac
    f="$asf_home/state/$1/{record}"
    [ -r "$f" ] || return 1
    v=$(tr -d '\n' < "$f" | sed 's/"previous"[[:space:]]*:[[:space:]]*{{[^}}]*}}//' |
        sed -n 's/.*"venv"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
    [ -n "$v" ] || return 1
    case "$v" in /*) ;; *) v="$ASF_DISPATCH_VENVS/$v" ;; esac
    [ -x "$v/bin/asf" ] && [ ! -d "$v/bin/asf" ] || return 1
    printf '%s\n' "$v/bin/asf"
}}

p=""; from=""; prev=""
for a in "$@"; do
    case "$a" in --product=*) p=${{a#--product=}}; from=--product ;; esac
    if [ "$prev" = "--product" ]; then p=$a; from=--product; fi
    prev=$a
done
if [ -z "$p" ] && [ -n "${{ASF_PRODUCT:-}}" ]; then p=$ASF_PRODUCT; from='$ASF_PRODUCT'; fi
if [ -z "$p" ]; then
    py=$ASF_DISPATCH_PY
    d=$(pin_cli "$ASF_DISPATCH_DEFAULT_PRODUCT") && [ -x "${{d%/asf}}/python" ] && py="${{d%/asf}}/python"
    here=$(pwd)
    # from / so the cwd (a checkout of asf itself) never shadows the venv's own package
    p=$(cd / && ASF_HOME="$asf_home" "$py" -m asf.dispatch --product-of-dir "$here" 2>/dev/null) || p=""
    [ -n "$p" ] && from=cwd
fi

cli=""; via=""
if [ -n "$p" ]; then cli=$(pin_cli "$p") && via=pin; fi
if [ -z "$cli" ]; then cli=$(pin_cli "$ASF_DISPATCH_DEFAULT_PRODUCT") && via=default-pin; fi
if [ -z "$cli" ] && [ -x "$ASF_DISPATCH_DEFAULT_CLI" ]; then cli=$ASF_DISPATCH_DEFAULT_CLI; via=default-cli; fi
if [ -n "$cli" ] && [ "$cli" -ef "$0" ]; then cli=""; via=""; fi
if [ -n "${{ASF_DISPATCH_TRACE:-}}" ]; then
    echo "asf-dispatch: product=${{p:-none}} from=${{from:-none}} via=${{via:-none}} cli=${{cli:-none}}" >&2
fi
[ -n "$cli" ] && exec "$cli" "$@"
echo "asf: no asf to run for product '${{p:-?}}' — no pinned venv in $asf_home/state/${{p:-?}}/{record}, and the default ($ASF_DISPATCH_DEFAULT_CLI) is gone; asf upgrade --product ${{p:-<p>}} --to <sha>" >&2
exit 127
'''


def default_path(home=None):
    """The dispatcher's path under ``home`` (default: ``$HOME``, read at call time — so a process
    run with a temp HOME resolves a path inside it, never the operator's)."""
    return os.path.join(os.path.expanduser(home or '~'), REL_PATH)


def home_redirected(asf_home=None):
    """True when ``asf_home`` (default :data:`asf.env.ASF_HOME`) is not the ASF home this
    process's environment names (``$ASF_HOME``, else ``$HOME/.ASF``): the home was moved
    in-process — a test pointing :data:`asf.env.ASF_HOME` at a temp dir while ``$HOME`` is still
    the operator's. A dispatcher written then would bake the temp home into the operator's
    ``~/.local/bin/asf``, so :func:`reassert` leaves the default path alone."""
    named = os.environ.get('ASF_HOME') or os.path.join(os.path.expanduser('~'), '.ASF')
    return os.path.realpath(asf_home or env.ASF_HOME) != os.path.realpath(named)


def pipx_venvs():
    """Where a bare venv name in ``install.json`` lives: ``$PIPX_HOME/venvs``, else pipx's
    default ``~/.local/pipx/venvs``."""
    base = os.environ.get('PIPX_HOME') or os.path.join(os.path.expanduser('~'), '.local', 'pipx')
    return os.path.join(base, 'venvs')


def record_path(product, asf_home=None):
    return os.path.join(asf_home or env.ASF_HOME, 'state', product, INSTALL_RECORD)


def pinned_cli(product, asf_home=None, venvs=None):
    """The python twin of the script's ``pin_cli``: the product's pinned venv's ``bin/asf`` when
    ``install.json`` names a venv whose CLI is executable, else None. Never raises."""
    if not product or '/' in product or product.startswith('.'):
        return None
    try:
        with open(record_path(product, asf_home), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    venv = data.get('venv') if isinstance(data, dict) else None
    if not venv or not isinstance(venv, str):
        return None
    if not os.path.isabs(venv):
        venv = os.path.join(venvs or pipx_venvs(), venv)
    cli = os.path.join(venv, 'bin', 'asf')
    return cli if os.path.isfile(cli) and os.access(cli, os.X_OK) else None


def is_ours(path):
    """The file at ``path`` is a dispatcher this module wrote (its :data:`MARKER` line)."""
    if os.path.islink(path) or not os.path.isfile(path):
        return False
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return MARKER in f.read(4096)
    except OSError:
        return False


def baked(path, name):
    """The value a written dispatcher at ``path`` bakes for ``ASF_DISPATCH_<name>``, or None."""
    if not is_ours(path):
        return None
    with open(path, encoding='utf-8') as f:
        for line in f:
            if line.startswith(f'ASF_DISPATCH_{name}='):
                words = shlex.split(line.split('=', 1)[1])
                return words[0] if words else ''
    return None


def _executable(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def default_cli(path, default_product, asf_home=None, venvs=None):
    """The CLI baked in as the last fallback: the default product's pin, else the one the
    dispatcher already at ``path`` bakes (still executable), else the ``bin/asf`` of the venv
    this process runs from. Never the dispatcher itself. None when there is none."""
    candidates = [pinned_cli(default_product, asf_home, venvs), baked(path, 'DEFAULT_CLI')]
    if sys.prefix != sys.base_prefix:
        candidates.append(os.path.join(sys.prefix, 'bin', 'asf'))
    for c in candidates:
        if _executable(c) and not (os.path.exists(path) and os.path.samefile(c, path)) \
                and not is_ours(c):
            return os.path.abspath(c)
    return None


def render(asf_home, venvs, default_product, default_cli_path, py):
    """The script's text, every baked value shell-quoted."""
    q = shlex.quote
    return _SCRIPT.format(marker=MARKER, home=q(asf_home), venvs=q(venvs),
                          default_product=q(default_product or ''),
                          default_cli=q(default_cli_path or ''), py=q(py), record=INSTALL_RECORD)


def install(path=None, asf_home=None, venvs=None, default_product=None, cli=None):
    """Write the dispatcher at ``path`` (default :func:`default_path`). Returns ``(rc, detail)``:
    rc 0 when it is written or already current — a pipx link at the path is moved aside to
    ``<path>``:data:`LINK_BACKUP` first; rc 2 with a ``NEEDS OPERATOR`` line when the
    path holds a file this module did not write, or when there is no CLI to fall back to."""
    path = path or default_path()
    asf_home = os.path.abspath(asf_home or env.ASF_HOME)
    venvs = venvs or pipx_venvs()
    if default_product is None:
        try:
            default_product = env.load_config().get('default_product') or ''
        except (env.ConfigError, OSError, ValueError):
            default_product = ''
    backup = None
    if os.path.islink(path):
        if os.path.exists(path):
            # the shared install's link (pipx): every hook naming this path ran that install,
            # never a product's pin (F-0283). It is moved aside — kept, so it can be put back —
            # and its CLI stays the last fallback when nothing else is pinned
            target = os.path.realpath(path)
            backup = path + LINK_BACKUP
            if os.path.lexists(backup):
                os.remove(backup)
            os.replace(path, backup)
            cli = cli or default_cli(path, default_product, asf_home, venvs) or (
                target if _executable(target) and not is_ours(target) else None)
            if not cli:
                os.replace(backup, path)
        else:
            os.remove(path)                   # a dangling link: the install it named is gone
    elif os.path.exists(path) and not is_ours(path):
        return 2, (f'NEEDS OPERATOR: {path} is not asf\'s dispatcher — move it away, then '
                   'asf hooks install again')
    cli = cli or default_cli(path, default_product, asf_home, venvs)
    if not cli:
        return 2, (f'NEEDS OPERATOR: no asf to fall back to for the dispatcher at {path} — '
                   f'pin the default product ({default_product or "config.yaml default_product"}) '
                   'with asf upgrade --product <p> --to <sha>')
    py = os.path.join(os.path.dirname(cli), 'python')
    if not _executable(py):
        py = sys.executable
    text = render(asf_home, venvs, default_product, cli, py)
    try:
        with open(path, encoding='utf-8') as f:
            if f.read() == text:
                return 0, f'dispatcher: {path} current (default {cli})'
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.tmp-{os.getpid()}'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.chmod(tmp, 0o755)
    os.replace(tmp, path)                     # a hook mid-call reads the old or the new, never half
    kept = f'; the shared install\'s link kept as {backup}' if backup else ''
    return 0, f'dispatcher: {path} written (default {cli}){kept}'


def reassert(path=None):
    """Put the dispatcher back at ``path`` when something took it: ``(changed, detail)``.

    ``(False, None)`` — nothing to do — when the path does not exist (a host with no factory CLI
    is not this function's business), when it is a link to a dispatcher (an agent home's link to
    the operator's path, :func:`asf.workers.runtime.link_factory_cli`), or when the dispatcher
    there is already what :func:`render` writes. Otherwise :func:`install` runs: a pipx link that
    took the path back is moved aside and replaced (``pipx install --force`` recreates it, and
    nothing else in the factory notices — F-0283), and an out-of-date dispatcher is rewritten.
    ``(False, detail)`` with the ``NEEDS OPERATOR`` line for a foreign file, which is never
    touched. Never raises.

    Two more no-ops keep a pass from writing a dispatcher that is not its own: a default-resolved
    call while :func:`home_redirected` (a test's in-process ASF home under the operator's HOME),
    and a dispatcher at ``path`` that bakes a different ASF home than :data:`asf.env.ASF_HOME`
    (another home's dispatcher is that home's install to rewrite, ``asf hooks install``)."""
    if path is None and home_redirected():
        return False, None
    path = path or default_path()
    try:
        if not os.path.lexists(path):
            return False, None
        if os.path.islink(path) and is_ours(os.path.realpath(path)):
            return False, None
        home = baked(path, 'HOME')
        if home and os.path.realpath(home) != os.path.realpath(env.ASF_HOME):
            return False, None
        try:
            with open(path, 'rb') as f:
                before = f.read()
        except OSError:
            before = None
        rc, detail = install(path)
        if rc != 0:
            return False, detail
        try:
            with open(path, 'rb') as f:
                after = f.read()
        except OSError:
            after = None
        changed = after != before
        return changed, detail if changed else None
    except OSError as e:
        return False, f'NEEDS OPERATOR: the dispatcher at {path} could not be written ({e})'


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) == 2 and argv[0] == '--product-of-dir':
        name = env.product_of_dir(argv[1])
        if name:
            print(name)
        return 0
    print('usage: python -m asf.dispatch --product-of-dir <dir>', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
