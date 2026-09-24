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
edited or overwritten. That refusal is reported only after every other hook — the approvals hook
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


def git_hooks_dir(repo):
    """``git -C <repo> rev-parse --git-path hooks``, made absolute — the real hooks directory of
    ``repo`` whether or not ``core.hooksPath`` is set, and shared by every worktree of ``repo``
    (git resolves it against the common ``.git`` dir, not the worktree's own). ``None`` when
    ``repo`` is not a directory, or not a git repo — a caller reports that, it never raises."""
    if not repo or not os.path.isdir(repo):
        return None
    p = subprocess.run(['git', '-C', repo, 'rev-parse', '--git-path', 'hooks'],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return None
    out = p.stdout.strip()
    return out if os.path.isabs(out) else os.path.join(repo, out)


def _git_hook_body(name, asf_path, product_name):
    return ('#!/bin/sh\n'
            '# written by asf hooks install — the redaction gate (F-0075)\n'
            f'exec "{asf_path}" redact --{name} --product {product_name}\n')


def is_git_hook_ours(text, name):
    """A hook file is *installed* when it contains ``asf redact --<name>`` or
    ``asf.redact --<name>`` in any form (§2.4) — the quoted command form :func:`_git_hook_body`
    writes (``"<path>/asf" redact ...``), unquoted (``asf`` on ``PATH``), or the module form
    (``python3 -m asf.redact``) an operator or another product might write instead."""
    return bool(re.search(rf'''asf['" .]*redact\s+--{re.escape(name)}\b''', text or ''))


def ensure_git_hooks(product, which=shutil.which):
    """Returns ``(ok, detail)`` (D10, §2.4). Writes the redaction gate's ``pre-commit`` and
    ``pre-push`` into :func:`git_hooks_dir` of each of ``product.repo_dir`` and
    ``product.backlog_dir`` that is set. A hook file already there and already asf's
    (:func:`is_git_hook_ours`) is left alone — running this twice changes nothing. One already
    there and not asf's is left untouched too, and the call refuses with the ``NEEDS OPERATOR``
    line naming the one line the operator adds; every other missing hook in the same call is
    still written."""
    asf_path = which('asf')
    if not asf_path:
        return False, 'NEEDS OPERATOR: asf is not on PATH — pipx install asf-factory'
    asf_path = os.path.abspath(asf_path)
    repos = [r for r in (product.repo_dir, product.backlog_dir) if r]
    if not repos:
        return True, 'no repo_dir or backlog_dir configured'
    refusals = []
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
                if not is_git_hook_ours(text, name):
                    refusals.append(f'NEEDS OPERATOR: {path} is not asf\'s — add the line: '
                                    f'"{asf_path}" redact --{name} --product {product.name}')
                continue
            os.makedirs(hooks_dir, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(_git_hook_body(name, asf_path, product.name))
            os.chmod(path, 0o755)
    if refusals:
        return False, '\n'.join(refusals)
    return True, f'pre-commit, pre-push in {len(repos)} repos'


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
    if product is None:
        return bool(re.search(rf'(^|/)asf hook {re.escape(name)}$', command or ''))
    return bool(re.search(rf'(^|/)asf hook {re.escape(name)} --product {re.escape(product)}$', command or ''))


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
    under it; otherwise it is the account's own ``home``, else ``home`` or the operator's."""
    if account.config_dir:
        return os.path.join(os.path.expanduser(account.config_dir), 'settings.json')
    base = account.home or home or os.path.expanduser('~')
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
    asf_path = which('asf')
    if not asf_path:
        return 2, 'NEEDS OPERATOR: asf is not on PATH — pipx install asf-factory'
    asf_path = os.path.abspath(asf_path)
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
