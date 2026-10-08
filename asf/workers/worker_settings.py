"""asf.workers.worker_settings — the per-role worker settings file (F-0062 §2.7).

One generated ``--settings`` file per role, built on :mod:`asf.console_perms`'s shape (P17): a
fixed set of deny rules every worker session gets, plus what the product's own conventions and
the role's own launch row add, merged **over** the operator's own ``worker_pool.settings_file``
so an operator rule is never dropped (D6). `asf doctor`'s ``worker-deny`` row
(:func:`asf.doctor.check_worker_deny`) is red while a role's file is missing a rule this module
would write.

The one note that matters: ``hermetic.build`` in worker mode strips the environment to the
allow-list and then ``build_env`` puts the live credentials back (``asf/workers/runtime.py:434``,
``:390`` at the spec's own read), because the session must push. So a session that prints its
environment prints a usable token today — ``Bash(env)``/``Bash(printenv:*)`` are the single
highest-value rule in :data:`FIXED_DENY`.
"""
import json
import os

from asf import env

#: The deny rules every worker session gets, whatever its role — §2.7's tuple verbatim. Each is
#: a shape a glob can state; the shapes it cannot (a key-shaped fetch argument, a secret in a
#: command's text) are the ``redactout`` hook's, not this file's.
FIXED_DENY = (
    'Read(**/.env)', 'Read(**/.env.*)', 'Read(**/*.pem)', 'Read(**/*.key)',
    'Read(**/.aws/**)', 'Read(**/.ssh/**)', 'Read(**/.gnupg/**)', 'Read(**/.netrc)',
    'Bash(env)', 'Bash(env:*)', 'Bash(printenv:*)', 'Bash(set)', 'Bash(export -p)',
    'Bash(cat .env*)', 'Bash(git push --no-verify:*)', 'Bash(git commit --no-verify:*)',
    'Bash(git push -f:*)', 'Bash(git push --force:*)',
    'WebFetch', 'WebSearch',
)


def deny_rules(product, launch):
    """:data:`FIXED_DENY`, plus a push of the product's own trunk (never the literal — read from
    ``product.conventions.main``), plus one entry per tool in ``launch.deny_tools``, plus one
    ``Read`` rule per secret directory the product's own config names (``conventions.flag
    ('secret_dirs')`` — the unvalidated passthrough every other product-specific list already
    reads through, :func:`asf.conventions.Conventions.flag`)."""
    main = product.conventions.main if product is not None else 'main'
    rules = FIXED_DENY + (f'Bash(git push origin {main}:*)',)
    rules += tuple(launch.deny_tools)
    if product is not None:
        secret_dirs = product.conventions.flag('secret_dirs', ()) or ()
        rules += tuple(f'Read({d}/**)' for d in secret_dirs)
    return rules


def allow_rules(product, launch):
    """The role's own tools as ``allow`` entries, so the file states what the role *is* for as
    well as what it is not. ``launch.tools`` empty means "every tool" (Task 1's own docstring) —
    there is nothing specific to allow, so this is ``()``."""
    return tuple(launch.tools)


def read_settings(path):
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def settings(product, launch, operator_path=None):
    """The role's settings object: the operator's own file (``operator_path`` —
    ``spawn.settings_file(wp)``'s path) read first, then this role's allow and deny rules merged
    over it — ``console_perms.merge``'s discipline: append what is missing, keep order, touch no
    other key (D6). A second call over the same ``operator_path`` returns the same bytes."""
    out = dict(read_settings(operator_path))
    perms = dict(out.get('permissions') or {})
    allow, deny = list(perms.get('allow') or []), list(perms.get('deny') or [])
    for r in allow_rules(product, launch):
        if r not in allow:
            allow.append(r)
    for r in deny_rules(product, launch):
        if r not in deny:
            deny.append(r)
    perms['allow'], perms['deny'] = allow, deny
    out['permissions'] = perms
    return out


def missing(settings_obj, product, launch):
    """``(missing_allow, missing_deny)`` — the rules of :func:`allow_rules`/:func:`deny_rules`
    not already in ``settings_obj``'s ``permissions.allow``/``permissions.deny``, in order
    (``console_perms.missing``'s shape) — what the ``worker-deny`` doctor row reads."""
    perms = (settings_obj or {}).get('permissions') or {}
    have_allow, have_deny = set(perms.get('allow') or []), set(perms.get('deny') or [])
    return ([r for r in allow_rules(product, launch) if r not in have_allow],
            [r for r in deny_rules(product, launch) if r not in have_deny])


def settings_path(product, launch):
    """``<state>/<product>/worker-settings/<role>.json`` — computed only, no I/O. Two roles never
    share one: the file name is the role's own."""
    return os.path.join(env.state_dir(product), 'worker-settings', f'{launch.role}.json')


def path(product, launch, wp):
    """:func:`settings_path`, written only when it differs from what is already there. Returns
    the path ``build_command`` passes as ``--settings``. ``wp`` is the operator's
    ``worker_pool`` config block; its own ``settings_file`` (``spawn.settings_file(wp)``, which
    already refuses a path that does not exist) is read first and merged under this role's own
    rules — a rule the operator already has is never duplicated and never dropped."""
    from asf.workers import spawn
    operator_path = spawn.settings_file(wp or {})
    target = settings_path(product, launch)
    new = settings(product, launch, operator_path)
    if new != read_settings(target):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'w', encoding='utf-8') as f:
            f.write(json.dumps(new, indent=2) + '\n')
    return target


def offer_text(product):
    """Every rule, per role, in full — the same discipline ``console_perms.offer_text``
    established, for the same reason: a confirmation of a list has to be of the actual list.
    Printed by ``asf worker-permissions offer`` before anything is written."""
    from asf.roles import launch as role_launch
    lines = ["worker-permissions: the per-role deny rules every worker session gets — nothing is "
             "written until you run `asf worker-permissions install`:"]
    for role in sorted(role_launch.TABLE):
        lau = role_launch.TABLE[role]
        lines.append(f'  role {role}')
        lines += [f'    allow  {r}' for r in allow_rules(product, lau)]
        lines += [f'    deny   {r}' for r in deny_rules(product, lau)]
    return '\n'.join(lines)


def cmd_worker_permissions(args):
    from asf import env as env_mod
    from asf.roles import launch as role_launch
    product = env_mod.load_product(args.product) if getattr(args, 'product', None) else None
    if args.worker_permissions_command == 'offer':
        print(offer_text(product))
        return 0
    cfg = env_mod.load_config()
    wp = (cfg or {}).get('worker_pool') or {}
    for role in sorted(role_launch.TABLE):
        lau = role_launch.TABLE[role]
        target = settings_path(product, lau)
        before = read_settings(target)
        written = path(product, lau, wp)
        if read_settings(written) != before:
            print(f'worker-permissions: wrote {written}')
        else:
            print(f'worker-permissions: {written} already has every rule')
    return 0


def register(subparsers):
    p = subparsers.add_parser(
        'worker-permissions',
        help="print or write each role's generated deny rules (F-0062 §2.7)")
    p.add_argument('worker_permissions_command', choices=['offer', 'install'])
    p.add_argument('--product')
    p.set_defaults(run=cmd_worker_permissions)
    return p
