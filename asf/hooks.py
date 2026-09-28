"""asf.hooks — ``asf hooks install --product p`` and ``asf hook <name>``.

``hooks install`` merges Claude Code hook entries into the product repo's
``.claude/settings.json``: one entry per (event, hook) that a core rule card declares with
``hook: [PreToolUse, PostToolUse, Stop]`` (any subset). The hook's name is the rule id in its
check-script form (``R-0042`` → ``r0042``), and its command is ``<absolute asf> hook <name>
--product <p>`` with ``asf`` resolved from PATH — never a checkout. The merge is idempotent and
leaves every unrelated key alone; an entry that differs only in the ``asf`` path is replaced.

It also merges the built-in ``approvals`` hook, product-less, into every worker account's own
*user* settings file (:func:`account_settings_path`) — unconditionally, whether or not any rule
declares a hook (F-0031 §2.3, PD5): the approvals matrix binds every session, not just those in a
product repo with a rule card.

``install`` also writes the redaction gate's own *git* hooks (F-0075, T-0025): :func:`ensure_git_hooks`
puts a ``pre-commit`` and a ``pre-push`` into ``git rev-parse --git-path hooks`` of each of
``product.repo_dir`` and ``product.backlog_dir`` (when set), each a four-line script that
``exec``s ``asf redact --pre-commit|--pre-push --product <p>``. A hook file already there and
already asf's is left alone; one already there and not asf's is left untouched too, and turns the
whole call into a ``NEEDS OPERATOR`` refusal (D10) — no hook file asf did not write is ever
edited or overwritten — save one asf's record pre-commit that still runs ``asf check`` over the
whole record, which gains ``--staged`` (:func:`staged_check_upgrade`). That refusal is reported only after every other hook — the approvals hook
above all — has been written: one refusal never skips the others.

``asf hook <name>`` runs a hook built into ``asf`` when :data:`BUILTIN` names it (``approvals``,
:func:`asf.approvals.run_hook`), else ``tools/checks/<name>.sh`` (the record's, then the cwd's)
with the hook's stdin, exiting 0 when there is no such script.
"""
import json
import os
import re
import shutil
import subprocess
import sys

from asf import env
from asf.workers import pool

EVENTS = ('PreToolUse', 'PostToolUse', 'Stop')
RULES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'rules')

#: The runtime's own settings files, project-level — matched by `asf.approvals`'s
#: `touch_security` path recogniser (F-0031). This module is `tools/check_conventions.sh`'s one
#: exemption for the runtime settings path, so the path lives here, not in `asf/approvals.py`.
RUNTIME_SETTINGS_GLOBS = ('.claude/settings.json', '.claude/settings.local.json')

#: Where a product keeps its check scripts — the convention of `check_script`, named once for
#: the amendable set (F-0024 §2.1) as well.
CHECKS_DIR = 'tools/checks'

#: The git hooks a product versions in its repo — the redaction gate's convention (F-0075).
GIT_HOOK_GLOBS = ('.githooks/*',)

#: The runtime's own role-agent files, project-level — the runtime adapter's convention.
RUNTIME_AGENT_GLOBS = ('.claude/agents/*.md',)

#: The hooks built into ``asf`` — ``{name: the events it answers}`` — run by :func:`cmd_hook`
#: instead of a check script (F-0031 §2.3). A built-in name shadows a script of the same name.
BUILTIN = {'approvals': ('PreToolUse',)}

#: The two git hooks the redaction gate installs (F-0075, D10). Each name doubles as the
#: ``asf redact`` mode it execs (``--pre-commit`` / ``--pre-push``).
GIT_HOOK_NAMES = ('pre-commit', 'pre-push')

#: The real git hooks directory's own files — never tracked, so not part of the amendable set
#: (`GIT_HOOK_GLOBS`), but never a session's to write either: the hook asf installs, and the
#: `<hook>.local` behind it that runs on every commit (F-0121, D9).
REAL_GIT_HOOK_GLOBS = ('.git/hooks/*', '*/.git/hooks/*')

#: What a foreign hook is renamed to when `asf hooks install --chain` chains it (F-0121).
LOCAL_SUFFIX = '.local'

#: The comment line every hook `_git_hook_body` writes carries — what `asf hooks uninstall` knows
#: this installer's own file by, and `asf init`'s (`asf.init.INIT_MARKER`) or the operator's
#: hand-merged one by its absence (D6).
HOOK_MARKER = 'written by asf hooks install'

#: The git hooks git feeds on stdin, whose chained body buffers it once so both halves read the
#: same bytes (D2). `pre-commit` is not one: git does not give it ref lines and does not reliably
#: close its stdin, and a `cat` there would hang every commit in the repo.
STDIN_HOOKS = ('pre-push',)


def git_hooks_dir(repo):
    """``git -C <repo> rev-parse --git-path hooks``, made absolute — the real hooks directory of
    ``repo`` whether or not ``core.hooksPath`` is set, and shared by every worktree of ``repo``
    (git resolves it against the common ``.git`` dir, not the worktree's own). ``None`` when
    ``repo`` is not a directory, or not a git repo — a caller reports that, it never raises."""
    if not repo or not os.path.isdir(repo):
        return None
    # a GIT_DIR / GIT_WORK_TREE / GIT_INDEX_FILE inherited from a git hook (a suite run from a
    # pre-push, a hook of the caller's own commit) names another repo than ``repo``: git would
    # answer with *that* repo's hooks directory, and a hook meant for ``repo`` lands there
    from asf import hermetic
    clean = {k: v for k, v in os.environ.items() if k not in hermetic.GIT_HOOK}
    p = subprocess.run(['git', '-C', repo, 'rev-parse', '--git-path', 'hooks'],
                       capture_output=True, text=True, env=clean)
    if p.returncode != 0:
        return None
    out = p.stdout.strip()
    return out if os.path.isabs(out) else os.path.join(repo, out)


def _git_hook_body(name, asf_path, product_name, chained=False):
    """The hook script ``asf hooks install`` writes for ``name`` (F-0121). ``chained=False`` is
    today's three lines: a shebang, the :data:`HOOK_MARKER` comment, and an ``exec`` of the gate.
    ``chained=True`` is the body written when a foreign hook was renamed to ``<name>.local``
    (:data:`LOCAL_SUFFIX`) behind it: the gate runs first, then the renamed hook, found at run
    time as ``"$(dirname "$0")/<name>.local"`` (D3) and guarded by ``[ -x … ]`` so one git was
    already ignoring stays ignored. A hook in :data:`STDIN_HOOKS` buffers stdin once into a
    ``mktemp`` file so both halves read the same bytes (D2, D3); every other hook passes stdin
    through untouched. Both forward ``"$@"`` and propagate whichever half's exit status is
    nonzero first."""
    if not chained:
        return ('#!/bin/sh\n'
                '# written by asf hooks install — the redaction gate (F-0075)\n'
                f'exec "{asf_path}" redact --{name} --product {product_name}\n')
    local = name + LOCAL_SUFFIX
    if name in STDIN_HOOKS:
        return ('#!/bin/sh\n'
                f'# {HOOK_MARKER} — the redaction gate (F-0075), chaining {local}\n'
                f'# Your own {name} was renamed to {local} and runs below, with the same arguments and\n'
                '# the same refs. `asf hooks uninstall --product <p>` puts it back.\n'
                '# git writes the pushed refs on stdin and both halves need them: buffered once, read twice.\n'
                'refs="$(mktemp "${TMPDIR:-/tmp}/asf-pre-push.XXXXXX")" || exit 1\n'
                'trap \'rm -f "$refs"\' EXIT HUP INT TERM\n'
                'cat > "$refs"\n'
                f'"{asf_path}" redact --{name} --product {product_name} < "$refs" || exit $?\n'
                f'own="$(dirname "$0")/{local}"\n'
                '[ -x "$own" ] || exit 0\n'
                '"$own" "$@" < "$refs"\n'
                'exit $?\n')
    return ('#!/bin/sh\n'
            f'# {HOOK_MARKER} — the redaction gate (F-0075), chaining {local}\n'
            f'# Your own {name} was renamed to {local} and runs below, with the same arguments\n'
            '# and the same stdin. `asf hooks uninstall --product <p>` puts it back.\n'
            f'"{asf_path}" redact --{name} --product {product_name} || exit $?\n'
            f'own="$(dirname "$0")/{local}"\n'
            '[ -x "$own" ] || exit 0\n'
            'exec "$own" "$@"\n')


def chained_local(text, name):
    """``<name>.local`` when ``text`` is a chained hook this installer wrote for ``name`` — it
    carries :data:`HOOK_MARKER` and names ``<name><LOCAL_SUFFIX>`` — else None. The name only: the
    file sits beside the hook, in the directory the caller already has."""
    text = text or ''
    local = name + LOCAL_SUFFIX
    if HOOK_MARKER in text and local in text:
        return local
    return None


def _write_hook(path, body):
    """The ``open`` / ``write`` / ``chmod 0o755`` triple every hook file this installer writes
    goes through, so the three cannot drift between the two call sites."""
    with open(path, 'w', encoding='utf-8') as f:
        f.write(body)
    os.chmod(path, 0o755)


#: A pipx ``--suffix`` appended to the declared console-script name: empty, or starting with a
#: digit or one of ``._+-`` — ``-live``, ``2``, ``.old`` and the like. Never a bare letter: a
#: command that merely starts with ``asf`` (``asfmt``) is a different program, not a suffixed
#: install, and this is what keeps it from being consumed as one below.
_SUFFIX = r'''(?:[0-9._+-][A-Za-z0-9._+-]*)?'''

#: The shape of an entry point asf may write: the declared console-script name (F-0111 P3) with
#: :data:`_SUFFIX` appended — ``asf``, ``asf-live``, ``asf2`` and the like. Used, path prefix and
#: quoting aside, by both recognisers below (F-0111 "reading back what asf writes").
_ENTRY_TOKEN = rf'''(?:[^\s"'\n]*/)?\basf{_SUFFIX}'''


def _hook_line_re(name):
    """Every form :func:`_git_hook_body` or an operator's ``-m`` invocation can write for hook
    ``name`` (F-0111 §"reading back what asf writes"): an entry-point basename — bare, quoted or
    path-qualified — followed by ``redact --<name>``, or ``-m asf.redact --<name>``, or ``-m
    asf.cli redact --<name>``. Each branch is anchored at a word boundary on the left, so a longer
    command name that merely starts with ``asf`` (``asfmt``) is never matched by accident."""
    n = re.escape(name)
    return re.compile(
        rf'''(?:(?P<q>["']){_ENTRY_TOKEN}(?P=q)|(?<!["']){_ENTRY_TOKEN})\s+redact\s+--{n}\b'''
        rf'''|-m\s+asf\.redact\s+--{n}\b'''
        rf'''|-m\s+asf\.cli\s+redact\s+--{n}\b''')


def is_git_hook_ours(text, name):
    """A hook file is *installed* when a line that is not a ``#`` comment matches
    :func:`_hook_line_re` (§2.4, F-0111) — the quoted or unquoted command form
    :func:`_git_hook_body` writes, a suffixed entry point (a pipx ``--suffix`` install, F-0111
    P3), or the module form (``python3 -m asf.redact`` or ``-m asf.cli redact``) an operator or
    another product might write instead. A match inside a quoted string that is not itself the
    whole command (``echo "asf redact --pre-push"``) is not ours — the closing quote around the
    entry point, when there is one, must land right after it, not at the end of the line."""
    pat = _hook_line_re(name)
    return any(pat.search(line) for line in (text or '').splitlines() if not line.strip().startswith('#'))


def hook_entry(text, name):
    """The command the line :func:`is_git_hook_ours` matches for hook ``name`` names, unquoted
    (F-0111): the entry-point path for the basename form, the interpreter for either module form,
    ``None`` when no line matches. Also ``None`` for a bare entry-point name — a command with no
    ``/`` in it (PD5): ``asf init``'s hooks resolve it at hook-run time by design and are never a
    repair candidate."""
    n = re.escape(name)
    entry_pat = re.compile(
        rf'''(?:(?P<q>["'])(?P<qpath>{_ENTRY_TOKEN})(?P=q)|(?<!["'])(?P<upath>{_ENTRY_TOKEN}))'''
        rf'''\s+redact\s+--{n}\b''')
    module_pat = re.compile(
        rf'''(?:(?P<mq>["'])(?P<qi>[^\s"'\n]+)(?P=mq)|(?<!["'])(?P<ui>[^\s"'\n]+))'''
        rf'''\s+-m\s+asf\.(?:redact|cli\s+redact)\s+--{n}\b''')
    for line in (text or '').splitlines():
        if line.strip().startswith('#'):
            continue
        m = module_pat.search(line)  # tried first: the entry pattern's shape also fits
        if m:                        # ``asf.cli``/``asf.redact`` as a (wrong) bare entry name
            return m.group('qi') or m.group('ui')
        m = entry_pat.search(line)
        if m:
            path = m.group('qpath') or m.group('upath')
            return path if '/' in path else None
    return None


#: A record pre-commit line that runs ``asf check`` over the whole record — the invocation
#: before ``--staged``. Only a real command: ``asf`` (bare, or a path ending ``/asf``, quoted or
#: not; ``exec`` before it allowed) as the line's first word, then the ``check`` word, then
#: nothing but ``--product`` flags up to the line's end or its ``;`` / ``&&`` / ``||`` / ``|``.
#: Never ``check-foo``, a quoted string (``echo "asf check"``), a comment, ``--deep`` /
#: ``--invariants`` / ``--staged``, or explicit paths. Group ``head`` ends at ``check``, where
#: the flag is inserted.
WHOLE_RECORD_CHECK_RE = re.compile(
    r"""(?m)^(?P<head>[ \t]*(?:exec[ \t]+)?"""
    r"""(?:asf|"[^"\n]*/asf"|'[^'\n]*/asf'|[^\s"';&|#]*/asf)[ \t]+check)"""
    r"""(?=(?:[ \t]+--product(?:=|[ \t]+)[^\s;&|]+)*[ \t]*(?:$|[;&|]))""")

#: The check line an older ``asf init`` wrote into the record's pre-commit: the staged paths
#: passed to ``asf check`` through ``xargs`` — a deleted card never checked, its absence never
#: judged. :func:`init_hook_upgrade` swaps this one line for :data:`STAGED_CHECK_LINE`.
OLD_INIT_CHECK_LINE = "echo \"$staged\" | tr '\\n' '\\0' | xargs -0 asf check || exit 1"
STAGED_CHECK_LINE = 'asf check --staged || exit 1'


def add_staged_flag(text):
    """``text`` with ``--staged`` after each whole-record ``asf check`` line's ``check`` word (and
    an older ``asf init``'s ``xargs`` check line swapped for :data:`STAGED_CHECK_LINE`) — every
    other line and flag kept as it stands."""
    lines = text.split('\n')
    lines = [STAGED_CHECK_LINE if line.strip() == OLD_INIT_CHECK_LINE else line for line in lines]
    return WHOLE_RECORD_CHECK_RE.sub(lambda m: m.group('head') + ' --staged', '\n'.join(lines))


def init_hook_upgrade(text, name):
    """The text an ``asf init``-written hook file should now have, when ``text`` is one written
    by an older ``asf init`` (its :data:`asf.init.INIT_MARKER`) that lacks the redaction gate or
    the pre-commit's ``--staged`` check — ASF's own file, so it is brought up to date rather than
    called foreign: a missing gate gets the whole current hook; a gated one only its check line
    edited (:func:`add_staged_flag`), so a line or flag the operator added stays. None otherwise."""
    from asf import init  # local: init imports this module
    text = text or ''
    if init.INIT_MARKER not in text:
        return None
    if is_git_hook_ours(text, name):
        if name != 'pre-commit':
            return None
        new = add_staged_flag(text)
        return None if new == text else new
    return {'pre-commit': init.PRE_COMMIT, 'pre-push': init.PRE_PUSH}.get(name)


def staged_check_upgrade(text, name):
    """A record's pre-commit that is asf's (:func:`is_git_hook_ours`) but runs ``asf check``
    over the whole record refuses every commit while any untouched card carries an error. The
    text with ``--staged`` added to each such ``asf check`` line, or None when there is none —
    so a second run changes nothing."""
    if name != 'pre-commit' or not is_git_hook_ours(text, name):
        return None
    new = add_staged_flag(text)
    return None if new == text else new


def runnable_asf(which=shutil.which):
    """``(path, refusal)``: the absolute ``asf`` a hook or settings entry names, or the
    ``NEEDS OPERATOR`` line when there is none to name — not on PATH, or the ``which`` result not
    an executable file. A hook naming an ``asf`` that is not there refuses every commit or push
    of whoever runs it (review-b-0111's "hook refused" on a test fixture's ``/x/asf``), so none
    is ever written."""
    asf_path = which('asf')
    if not asf_path:
        return None, 'NEEDS OPERATOR: asf is not on PATH — pipx install asf-factory'
    asf_path = os.path.abspath(asf_path)
    if not os.path.isfile(asf_path) or not os.access(asf_path, os.X_OK):
        return None, (f'NEEDS OPERATOR: {asf_path} is not an executable asf — no hook is written '
                      'naming it; pipx install asf-factory')
    return asf_path, None


def ensure_git_hooks(product, which=shutil.which, chain=False):
    """Returns ``(ok, detail)`` (D10, §2.4). Writes the redaction gate's ``pre-commit`` and
    ``pre-push`` into :func:`git_hooks_dir` of each of ``product.repo_dir`` and
    ``product.backlog_dir`` that is set. A hook file already there and already asf's
    (:func:`is_git_hook_ours`) is left alone — running this twice changes nothing. One already
    there and not asf's is, with ``chain`` false, left untouched and the call refuses with the
    ``NEEDS OPERATOR`` line naming the one line the operator adds; every other missing hook in the
    same call is still written. With ``chain`` true (F-0121) it is instead renamed to
    ``<name>.local`` and ASF's chained body — the gate, then the renamed hook — is written in its
    place, unless a ``<name>.local`` is already there, which refuses rather than losing it. An
    ``asf`` that does not exist or is not executable (:func:`runnable_asf`) refuses before any
    hook is touched."""
    asf_path, refusal = runnable_asf(which)
    if refusal:
        return False, refusal
    repos = [r for r in (product.repo_dir, product.backlog_dir) if r]
    if not repos:
        return True, 'no repo_dir or backlog_dir configured'
    refusals = []
    moved = []
    for repo in repos:
        hooks_dir = git_hooks_dir(repo)
        if hooks_dir is None:
            refusals.append(f'NEEDS OPERATOR: {repo} is not a git repo — asf hooks install '
                            'cannot place its hooks there')
            continue  # one refusal never skips the other repo's hooks
        for name in GIT_HOOK_NAMES:
            path = os.path.join(hooks_dir, name)
            if os.path.isfile(path):
                with open(path, encoding='utf-8') as f:
                    text = f.read()
                upgrade = init_hook_upgrade(text, name) or staged_check_upgrade(text, name)
                if upgrade is not None:  # ASF's own record hook, from before the gate or --staged
                    _write_hook(path, upgrade)
                    continue
                if not is_git_hook_ours(text, name):
                    if not chain:
                        refusals.append(f'NEEDS OPERATOR: {path} is not asf\'s — add the line: '
                                        f'"{asf_path}" redact --{name} --product {product.name}')
                        continue
                    local = path + LOCAL_SUFFIX
                    if os.path.exists(local):
                        refusals.append(f'NEEDS OPERATOR: {local} is already there — asf hooks '
                                        f'install cannot chain {path} without losing it; move it '
                                        f'aside or merge the two')
                        continue
                    os.rename(path, local)
                    moved.append((path, local))
                    _write_hook(path, _git_hook_body(name, asf_path, product.name, chained=True))
                continue
            os.makedirs(hooks_dir, exist_ok=True)
            _write_hook(path, _git_hook_body(name, asf_path, product.name))
    if refusals:
        return False, '\n'.join(refusals)
    detail = f'pre-commit, pre-push in {len(repos)} repos'
    if moved:
        detail += '; ' + '; '.join(f'chained {path} → {local}' for path, local in moved)
    return True, detail


def declared_hooks(rules_dir=RULES_DIR):
    """``[(event, name)]`` from every rule card with a ``hook:`` line; empty if no ``rules/``."""
    from asf.record import frontmatter
    out = []
    if not os.path.isdir(rules_dir):
        return out
    for f in sorted(os.listdir(rules_dir)):
        if not f.endswith('.md'):
            continue
        path = os.path.join(rules_dir, f)
        with open(path, encoding='utf-8') as fh:
            meta = frontmatter.parse(fh.read(), path)[0]
        events = meta.get('hook')
        if not events:
            continue
        events = events if isinstance(events, list) else [events]
        name = str(meta.get('id') or f[:-3]).replace('-', '').lower()
        out += [(e, name) for e in events if e in EVENTS]
    return out


def hook_command(asf_path, name, product):
    if product is None:
        return f'{asf_path} hook {name}'
    return f'{asf_path} hook {name} --product {product}'


def _is_ours(command, name, product):
    """A settings ``command`` is asf's own hook entry for ``name`` (F-0111 §"reading back what
    asf writes") when it is an entry-point basename — bare or after a ``/``, any pipx
    ``--suffix`` included (:data:`_SUFFIX`, so a different program that merely starts with
    ``asf`` — ``asfmt`` — cannot satisfy this) — or ``-m asf.cli``, followed by ``hook <name>``
    and, when ``product`` is given, its ``--product <p>`` tail; the whole command, not a prefix
    of a longer one (the ``$`` anchor is unchanged)."""
    n = re.escape(name)
    tail = rf' --product {re.escape(product)}' if product is not None else ''
    return bool(re.search(rf'(?:(^|/)asf{_SUFFIX}|-m asf\.cli) hook {n}{tail}$', command or ''))


def merge(settings, hooks, asf_path, product):
    """``settings`` with every ``(event, name)`` in ``hooks`` present exactly once."""
    settings = dict(settings)
    all_hooks = dict(settings.get('hooks') or {})
    for event, name in hooks:
        want = hook_command(asf_path, name, product)
        groups = [dict(g, hooks=list(g.get('hooks') or [])) for g in all_hooks.get(event) or []]
        found = False
        for g in groups:
            for i, h in enumerate(g['hooks']):
                if _is_ours(h.get('command'), name, product):
                    g['hooks'][i] = dict(h, type='command', command=want)
                    found = True
        if not found:
            entry = {'hooks': [{'type': 'command', 'command': want}]}
            if event != 'Stop':
                entry = {'matcher': '*', **entry}
            groups.append(entry)
        all_hooks[event] = groups
    if all_hooks:
        settings['hooks'] = all_hooks
    return settings


def account_settings_path(account, home=None):
    """The runtime's *user* settings file an account's sessions read (PD4): ``CLAUDE_CONFIG_DIR``
    replaces the ``~/.claude`` directory itself, so a ``config_dir`` account's file sits directly
    under it; otherwise it is under the HOME its sessions run under
    (:func:`asf.workers.runtime.session_home` — its ``home:`` or its isolated one), else
    ``home`` or the operator's."""
    if account.config_dir:
        return os.path.join(os.path.expanduser(account.config_dir), 'settings.json')
    from asf.workers import runtime  # local: runtime is the session's side, hooks the install's
    base = runtime.session_home(account) or home or os.path.expanduser('~')
    return os.path.join(os.path.expanduser(base), '.claude', 'settings.json')


def _write_merged(path, hooks, asf_path, product):
    current = {}
    if os.path.isfile(path):
        with open(path, encoding='utf-8') as f:
            current = json.load(f)
    merged = merge(current, hooks, asf_path, product)
    if merged == current:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(json.dumps(merged, indent=2) + '\n')


def install(product, rules_dir=RULES_DIR, which=shutil.which, cfg=None):
    """Returns ``(rc, message)``. Rule hooks go to the product repo's own settings when any rule
    declares one (PD5); the approvals hook always goes into every worker account's settings
    (§2.3), product-less, regardless; :func:`ensure_git_hooks` writes every missing git hook.

    Every hook that *can* be written is written before anything is refused: a foreign git hook
    or a missing ``repo_dir`` must never leave worker sessions without the approvals hook (the
    guard that refuses human-now actions). Each refusal is then one ``NEEDS OPERATOR`` line after
    the summary, and rc is 2 (§2.4)."""
    rule_hooks = declared_hooks(rules_dir)
    asf_path, refusal = runnable_asf(which)
    if refusal:
        return 2, refusal
    refusals = []

    accounts = pool.accounts_from_config(cfg or env.load_config())
    for account in accounts:
        _write_merged(account_settings_path(account), [('PreToolUse', 'approvals')], asf_path, None)

    git_ok, git_detail = ensure_git_hooks(product, which=which)
    if not git_ok:
        refusals.append(git_detail)
        git_detail = 'git hooks: NEEDS OPERATOR (below)'

    repo_settings = os.path.join(product.repo_dir or '(no repo_dir)', '.claude', 'settings.json')
    written = 0
    if rule_hooks and not product.repo_dir:
        refusals.append(f'NEEDS OPERATOR: product {product.name} has no repo_dir — '
                        f'set it in products/{product.name}.yaml')
    elif rule_hooks:
        _write_merged(repo_settings, rule_hooks, asf_path, product.name)
        written = len(rule_hooks)

    summary = (f'hooks: {written} rule hooks in {repo_settings}; '
               f'approvals in {len(accounts)} worker accounts; {git_detail}')
    if refusals:
        return 2, '\n'.join([summary] + refusals)
    return 0, summary


def approvals_missing(accounts, repo_dir=None):
    """The worker accounts whose sessions would run with no ``approvals`` PreToolUse hook: its
    command is in neither the account's own settings file (:func:`account_settings_path`) nor,
    when ``repo_dir`` is set, the product repo's ``.claude/settings.json``. Read-only — the
    doctor's ``approvals-hook`` row; an unreadable file counts as one without the hook."""
    def has(path):
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            return False
        groups = ((data if isinstance(data, dict) else {}).get('hooks') or {}).get('PreToolUse') or []
        return any(_is_ours(h.get('command'), 'approvals', None)
                   for g in groups if isinstance(g, dict)
                   for h in (g.get('hooks') or []) if isinstance(h, dict))
    in_repo = bool(repo_dir) and has(os.path.join(repo_dir, '.claude', 'settings.json'))
    return [a for a in accounts if not in_repo and not has(account_settings_path(a))]


def cmd_hooks(args):
    rc, msg = install(env.load_product(args.product))
    print(msg, file=sys.stderr if rc else sys.stdout)
    return rc


def check_script(name, product_name=None, cwd=None):
    """The first ``tools/checks/<name>.sh`` that exists: the record's, then the cwd's."""
    if not re.match(r'^[A-Za-z0-9_.-]+$', name):
        return None
    roots = []
    if product_name:
        try:
            backlog = env.load_product(product_name).backlog_dir
            if backlog:
                roots.append(backlog)
        except env.ConfigError:
            pass
    roots.append(cwd or os.getcwd())
    for root in roots:
        path = os.path.join(root, *CHECKS_DIR.split('/'), f'{name}.sh')
        if os.path.isfile(path):
            return path
    return None


def cmd_hook(args):
    if args.name in BUILTIN:
        from asf import approvals  # local: asf.approvals reads this module's runtime globs
        return approvals.run_hook(sys.stdin.read(), os.environ, product=args.product)
    script = check_script(args.name, args.product)
    if not script:
        return 0
    return subprocess.run(['bash', script]).returncode


def register(subparsers):
    p = subparsers.add_parser('hooks', help="write the product repo's Claude Code hook entries")
    p.add_argument('hooks_command', choices=['install'])
    p.add_argument('--product')
    p.set_defaults(run=cmd_hooks)
    p = subparsers.add_parser(
        'hook', help='run one hook: a built-in (approvals), else tools/checks/<name>.sh')
    p.add_argument('name')
    p.add_argument('--product')
    p.set_defaults(run=cmd_hook)
    return p
