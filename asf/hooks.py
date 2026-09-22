"""asf.hooks — ``asf hooks install --product p`` and ``asf hook <name>``.

``hooks install`` merges Claude Code hook entries into the product repo's
``.claude/settings.json``: one entry per (event, hook) that a core rule card declares with
``hook: [PreToolUse, PostToolUse, Stop]`` (any subset). The hook's name is the rule id in its
check-script form (``R-0042`` → ``r0042``), and its command is ``<absolute asf> hook <name>
--product <p>`` with ``asf`` resolved from PATH — never a checkout. The merge is idempotent and
leaves every unrelated key alone; an entry that differs only in the ``asf`` path is replaced.

``asf hook <name>`` runs ``tools/checks/<name>.sh`` (the record's, then the cwd's) with the hook's
stdin, and exits 0 when there is no such script.
"""
import json
import os
import re
import shutil
import subprocess
import sys

from asf import env

EVENTS = ('PreToolUse', 'PostToolUse', 'Stop')
RULES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'rules')

#: The runtime's own settings files, project-level — matched by `asf.approvals`'s
#: `touch_security` path recogniser (F-0031). This module is `tools/check_conventions.sh`'s one
#: exemption for the runtime settings path, so the path lives here, not in `asf/approvals.py`.
RUNTIME_SETTINGS_GLOBS = ('.claude/settings.json', '.claude/settings.local.json')


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
    return f'{asf_path} hook {name} --product {product}'


def _is_ours(command, name, product):
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


def install(product, rules_dir=RULES_DIR, which=shutil.which):
    """Returns ``(rc, message)``."""
    hooks = declared_hooks(rules_dir)
    if not hooks:
        return 0, 'hooks: 0 rules declare a hook'
    asf_path = which('asf')
    if not asf_path:
        return 2, 'NEEDS OPERATOR: asf is not on PATH — pipx install asf-factory'
    if not product.repo_dir:
        return 2, f'NEEDS OPERATOR: product {product.name} has no repo_dir — set it in products/{product.name}.yaml'
    path = os.path.join(product.repo_dir, '.claude', 'settings.json')
    current = {}
    if os.path.isfile(path):
        with open(path, encoding='utf-8') as f:
            current = json.load(f)
    merged = merge(current, hooks, os.path.abspath(asf_path), product.name)
    if merged == current:
        return 0, f'hooks: {len(hooks)} already installed in {path}'
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(json.dumps(merged, indent=2) + '\n')
    return 0, f'hooks: {len(hooks)} installed in {path}'


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
        path = os.path.join(root, 'tools', 'checks', f'{name}.sh')
        if os.path.isfile(path):
            return path
    return None


def cmd_hook(args):
    script = check_script(args.name, args.product)
    if not script:
        return 0
    return subprocess.run(['bash', script]).returncode


def register(subparsers):
    p = subparsers.add_parser('hooks', help="write the product repo's Claude Code hook entries")
    p.add_argument('hooks_command', choices=['install'])
    p.add_argument('--product')
    p.set_defaults(run=cmd_hooks)
    p = subparsers.add_parser('hook', help='run one hook: tools/checks/<name>.sh, if present')
    p.add_argument('name')
    p.add_argument('--product')
    p.set_defaults(run=cmd_hook)
    return p
