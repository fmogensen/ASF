"""asf.console_perms — B-0131: the operator console's own allow list.

Under Claude Code's default (non-``--dangerously-skip-permissions``) auto mode, the operator's
own orchestrator session hits the safety classifier's prompt on every one of the factory's own
maintenance commands — ``bash tools/install.sh``, ``asf approvals resolve``, a ``launchctl``
pause/resume of a clock, a lane branch push, ``git worktree`` cleanup — and an approval given in
chat does not persist: the next session asks again, and the session cannot add its own allow
rule (that is refused as self-modification). ``tools/install.sh`` ends by offering the operator a
reviewed allow list (:func:`offer_text`) the operator writes, once and by hand
(``asf console-permissions install``), into either their own user-level Claude Code settings or
the product repo's ``.claude/settings.json`` — never both silently, never without being shown in
full first. ``asf doctor``'s ``console permissions`` row (:func:`check_doctor`) is red while
neither carries every rule.

Five rules are fixed, verbatim from the card; two are the product's own, never a literal branch
name: a push of each of its lane branch prefixes
(:func:`asf.conventions.Conventions.all_prefixes`) is allowed, and a force-push of its trunk
(``product.main``) is denied.
"""
import json
import os
import sys

#: The five rules every product gets, unchanged — the console's own commands, never a product
#: one (B-0131's card, verbatim).
FIXED_ALLOW = (
    'Bash(asf:*)',
    'Bash(bash tools/install.sh:*)',
    'Bash(launchctl bootout gui/*/asf.*)',
    'Bash(launchctl bootstrap gui/*)',
    'Bash(git worktree:*)',
)


def allow_rules(product):
    """:data:`FIXED_ALLOW`, plus one ``git push origin <prefix>*`` per lane branch prefix the
    product's own conventions declare — never a bare ``git push:*``, which would also allow a
    push straight to the trunk."""
    prefixes = product.conventions.all_prefixes() if product is not None else ()
    return FIXED_ALLOW + tuple(f'Bash(git push origin {p}*)' for p in prefixes)


def deny_rules(product):
    """One rule: a force-push of the product's own trunk (``product.main``) is refused outright
    — the card's "a deny rule for git push --force* on the trunk"."""
    main = product.main if product is not None else 'main'
    return (f'Bash(git push --force* origin {main})',)


def _read(path):
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def missing(settings, product):
    """``(missing_allow, missing_deny)`` — the rules of :func:`allow_rules`/:func:`deny_rules`
    not already in ``settings``'s ``permissions.allow``/``permissions.deny``."""
    perms = (settings or {}).get('permissions') or {}
    have_allow, have_deny = set(perms.get('allow') or []), set(perms.get('deny') or [])
    return ([r for r in allow_rules(product) if r not in have_allow],
            [r for r in deny_rules(product) if r not in have_deny])


def merge(settings, product):
    """``settings`` with every rule of :func:`allow_rules`/:func:`deny_rules` present exactly
    once — existing entries and order kept, every unrelated key untouched."""
    settings = dict(settings or {})
    perms = dict(settings.get('permissions') or {})
    allow, deny = list(perms.get('allow') or []), list(perms.get('deny') or [])
    for r in allow_rules(product):
        if r not in allow:
            allow.append(r)
    for r in deny_rules(product):
        if r not in deny:
            deny.append(r)
    perms['allow'], perms['deny'] = allow, deny
    settings['permissions'] = perms
    return settings


def offer_text(product):
    """What ``tools/install.sh`` and ``asf console-permissions offer`` show the operator before
    anything is written — every rule, in full, named once, so the confirmation that follows is
    of the actual list, never a description of it."""
    lines = ["console-permissions: the console's own allow list — nothing is written until you "
             "run `asf console-permissions install`:"]
    lines += [f'  allow  {r}' for r in allow_rules(product)]
    lines += [f'  deny   {r}' for r in deny_rules(product)]
    return '\n'.join(lines)


def settings_paths(product, home=None):
    """The two places :func:`merge` can be written, in the order :func:`check_doctor` reads them
    — the operator's own user-level settings, then the product repo's."""
    home = home if home is not None else os.path.expanduser('~')
    paths = [os.path.join(home, '.claude', 'settings.json')]
    if product is not None and product.repo_dir:
        paths.append(os.path.join(product.repo_dir, '.claude', 'settings.json'))
    return paths


def check_doctor(product, home=None):
    """``(ok, detail)`` — the doctor's ``console permissions`` row (B-0131). Claude Code merges
    user and project settings, so a rule present in *either* file counts; red names, by name,
    each rule missing from both."""
    paths = settings_paths(product, home)
    have_allow, have_deny = set(), set()
    for path in paths:
        perms = (_read(path) or {}).get('permissions') or {}
        have_allow.update(perms.get('allow') or [])
        have_deny.update(perms.get('deny') or [])
    missing_allow = [r for r in allow_rules(product) if r not in have_allow]
    missing_deny = [r for r in deny_rules(product) if r not in have_deny]
    if missing_allow or missing_deny:
        names = ', '.join(missing_allow + missing_deny)
        cmd = 'asf console-permissions install --product ' + (product.name if product else '<p>')
        return False, f'missing {names} — {cmd} --scope user|repo'
    return True, f'{len(paths)} settings file(s) checked, every rule present'


def cmd_console_permissions(args):
    from asf import env
    product = env.load_product(args.product) if getattr(args, 'product', None) else None
    if args.console_permissions_command == 'offer':
        print(offer_text(product))
        return 0
    paths = settings_paths(product)
    if args.scope == 'repo':
        if len(paths) < 2:
            print('console-permissions: NEEDS OPERATOR: --scope repo needs a product with a '
                  'repo_dir', file=sys.stderr)
            return 2
        path = paths[1]
    else:
        path = paths[0]
    current = _read(path)
    merged = merge(current, product)
    if merged == current:
        print(f'console-permissions: {path} already has every rule')
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(json.dumps(merged, indent=2) + '\n')
    print(f'console-permissions: wrote {path}')
    return 0


def register(subparsers):
    p = subparsers.add_parser(
        'console-permissions',
        help="print or write the console's own allow/deny rules (B-0131)")
    p.add_argument('console_permissions_command', choices=['offer', 'install'])
    p.add_argument('--product')
    p.add_argument('--scope', choices=['user', 'repo'], default='user',
                    help='install only: user-level settings (default) or the product repo\'s')
    p.set_defaults(run=cmd_console_permissions)
    return p
