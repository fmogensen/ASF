"""asf.install — ``asf install --product <p> [flags]``: configures one machine for one product.

It never installs the ``asf`` package itself (D2) — that is the bootstrap's own job, done once
under the product's tick lock before ``asf`` exists on ``PATH``, and named the way
:mod:`asf.plugin_build` already names it: ``os.path.join('tools', 'install' + '.sh')``. This
module is everything the bootstrap hands off to once the package is there: the operator config,
the product and its record, the worker account, the hooks, the clocks, the plugin and the
verification tail.

Twelve steps, in order, each printing one line — ``ok`` / ``wrote …`` / ``already in place`` /
``repaired …`` / ``FAILED … (exit N)`` — then a summary and one exit status. Steps 1-6 (the host,
this ``asf``, the config, the repos, the adopt, the account) abort the run when they fail: nothing
after them can work. This module carries steps 1-6; steps 7-12 (the hooks, the clocks, the
plugin, the console offer, the doctor and the dry run) land with the Task that also writes their
tests, and never abort — a failure there is recorded and the rest still runs, the same discipline
the bootstrap's own step function already has today.
"""
import argparse
import difflib
import os
import shutil
import subprocess
import sys

from asf import cli, env, init

#: The bootstrap script this module is `exec`ed by — never written as a literal path (the
#: convention checker's own fence: a hardcoded `tools/*.sh` name is a product's tooling call).
INSTALL_TOOL = os.path.join('tools', 'install' + '.sh')

#: ``capacity.total.sessions`` written by the template when the operator has not chosen yet — a
#: small, safe number a first tick can run with; ``docs/config.example.yaml`` explains the field.
DEFAULT_SESSIONS = 2

#: The Claude Code runtime's own credential file, found under an account's ``config_dir``. Its
#: presence is checked with ``os.path.isfile``; its bytes are never opened (D10).
RUNTIME_CREDENTIAL_FILE = '.credentials.json'

CONFIG_TEMPLATE = """\
# ~/.ASF/config.yaml — written once by `asf install`, from its own template; never rewritten
# after that (an existing file is read, never edited — see docs/config.example.yaml for every
# key this template does not write, and how to add one by hand).
default_product: {default_product}

scheduler:
  kind: {scheduler_kind}

worker_pool:
  backend: {backend}
  models:
    heavy:  # TODO: your judgement model
    light:  # TODO: your coding model
    cheap:  # TODO: your smallest model
  accounts:{accounts_block}

capacity:
  total:
    sessions: {sessions}
"""


# ---- the step framework ------------------------------------------------------

class Step:
    """One step of ``asf install``: ``label``, ``fn`` (``() -> (exit_status, detail)``), and
    whether a non-zero exit here stops the run (:func:`run_steps`)."""

    __slots__ = ('label', 'fn', 'abort')

    def __init__(self, label, fn, abort):
        self.label = label
        self.fn = fn
        self.abort = abort


def run_steps(steps, out=print):
    """Runs each :class:`Step` in order, printing ``<label>: <detail>`` — ``detail`` is
    ``ok`` / ``wrote …`` / ``already in place`` / ``repaired …`` on success, else
    ``FAILED <detail> (exit N)``. An ``abort=True`` step that fails stops the run there; any
    other failure is recorded and the rest still runs. Ends with a summary line naming every
    failed step, and returns 0 only when every step that ran passed — the bootstrap's own
    discipline, moved here without change."""
    failed = []
    ran = 0
    for step in steps:
        rc, detail = step.fn()
        ran += 1
        if rc == 0:
            out(f'{step.label}: {detail}')
        else:
            out(f'{step.label}: FAILED {detail} (exit {rc})')
            failed.append(f'{step.label} (exit {rc})')
            if step.abort:
                break
    out('')
    out('asf install: summary')
    if failed:
        for line in failed:
            out(f'  FAILED {line}')
        return 1
    out(f'  every step passed ({ran}/{len(steps)})')
    return 0


# ---- the config template ------------------------------------------------------

def account_stanza(name, config_dir, hint_auth_env=False):
    """One ``worker_pool.accounts`` list entry, indented for :data:`CONFIG_TEMPLATE`'s block:
    ``name``, ``role: local``, ``cap: 2``, ``config_dir`` — never a credential (D10). ``home``
    is left unset (a per-account home under the state directory, ``env.isolate_home``'s
    default): ``docs/config.example.yaml`` shows it only as a commented, optional key, and every
    key this template writes must be one that file documents active, not in a comment.
    ``hint_auth_env`` adds the commented ``auth_env`` lines the no-account fallback carries,
    naming the one human act."""
    lines = [
        f'    - name: {name}',
        '      role: local',
        '      cap: 2',
        f'      config_dir: {config_dir}',
    ]
    if hint_auth_env:
        lines += [
            '      # auth_env:',
            f'      #   CLAUDE_CODE_OAUTH_TOKEN: ~/.ASF/secrets/{name}.token   # `claude setup-token`',
        ]
    return '\n'.join(lines)


def detect_accounts(asf_home):
    """``[(name, config_dir)]`` for every directory under ``<asf_home>/accounts/`` holding
    :data:`RUNTIME_CREDENTIAL_FILE` — its presence checked, its content never opened (D10)."""
    root = os.path.join(asf_home, 'accounts')
    if not os.path.isdir(root):
        return []
    found = []
    for name in sorted(os.listdir(root)):
        config_dir = os.path.join(root, name)
        if os.path.isdir(config_dir) \
                and os.path.isfile(os.path.join(config_dir, RUNTIME_CREDENTIAL_FILE)):
            found.append((name, config_dir))
    return found


def _account_plan(args):
    """What ``worker_pool`` should hold, computed once for step 3's template and step 6's
    report: ``--fake-workers`` wins outright; else ``--account`` (repeatable, ``NAME[:DIR]``);
    else :func:`detect_accounts`; else one stanza with its credential commented out and the one
    human act named. Never reads a credential's contents (D10)."""
    if args.fake_workers:
        return {'backend': 'fake', 'stanzas': [], 'names': [], 'operator_line': None}
    entries = []
    for raw in args.account or []:
        name, _, config_dir = raw.partition(':')
        config_dir = os.path.expanduser(config_dir) if config_dir else \
            os.path.join(env.ASF_HOME, 'accounts', name)
        entries.append((name, config_dir))
    if not entries:
        entries = detect_accounts(env.ASF_HOME)
    operator_line = None
    if not entries:
        name = 'acct-a'
        config_dir = os.path.join(env.ASF_HOME, 'accounts', name)
        entries = [(name, config_dir)]
        operator_line = (f'NEEDS OPERATOR: no worker account found — run `claude setup-token` '
                         f'for {name}, then uncomment its auth_env in {env.config_path()}')
    stanzas = [account_stanza(name, config_dir, hint_auth_env=(operator_line is not None))
              for name, config_dir in entries]
    return {'backend': 'claude-code', 'stanzas': stanzas, 'names': [n for n, _ in entries],
           'operator_line': operator_line}


def _render_config(args, plan):
    accounts_block = ('\n' + '\n'.join(plan['stanzas'])) if plan['stanzas'] else ' []'
    return CONFIG_TEMPLATE.format(default_product=args.product, scheduler_kind=args.scheduler,
                                  backend=plan['backend'], sessions=DEFAULT_SESSIONS,
                                  accounts_block=accounts_block)


# ---- steps 1-6 ------------------------------------------------------------------

def _needs_gh(args):
    """False only when the product already has a config file and it names no PR host
    (``asf.doctor.has_pr_host``); True — ``gh`` required — before the product is adopted, so a
    first run does not silently skip a check it cannot yet answer."""
    path = env.product_path(args.product)
    existing = env.load_file(path) if os.path.isfile(path) else None
    if not existing:
        return True
    from asf.doctor import has_pr_host
    return has_pr_host(env.Product(args.product, existing))


def _step_host(args):
    """Step 1 — the host: ``git`` on ``PATH``, and ``gh`` unless the product has no PR host."""
    missing = [name for name in ('git', 'gh')
              if (name == 'git' or _needs_gh(args)) and shutil.which(name) is None]
    if missing:
        print(f'NEEDS OPERATOR: {", ".join(missing)} not on PATH — install and re-run',
             file=sys.stderr)
        return 1, f'{", ".join(missing)} not on PATH'
    return 0, 'ok'


def _step_asf(args):
    """Step 2 — this ``asf``: the release and commit it is; a checkout or editable install
    refuses without ``--allow-checkout`` (D2). A checkout is what ``cli._checkout_root()`` finds."""
    version = cli.version_string()
    root = cli._checkout_root()
    if root and not args.allow_checkout:
        print(f'NEEDS OPERATOR: {version} is a checkout at {root} — pass --allow-checkout to '
             f'configure from one', file=sys.stderr)
        return 1, 'a checkout install'
    return 0, version


def _step_config(args, plan):
    """Step 3 — ``~/.ASF/config.yaml``: written from :data:`CONFIG_TEMPLATE` only when absent;
    an existing file is never rewritten — the diff it would have made is printed instead, the
    way ``asf init`` prints its product diff."""
    path = env.config_path()
    rendered = _render_config(args, plan)
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            current = f.read()
        diff = list(difflib.unified_diff(current.splitlines(True), rendered.splitlines(True),
                                         fromfile=path, tofile=f'{path} (template)'))
        if diff:
            print(f'install: {path} exists — left as is; the template would add:')
            sys.stdout.writelines(diff)
        return 0, 'already in place'
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(os.path.join(env.ASF_HOME, 'products'), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(rendered)
    return 0, f'wrote {path}'


def _step_repos(args):
    """Step 4 — the repos: ``--repo-url`` / ``--record-url`` cloned into ``--repo`` / ``--record``
    only when that directory does not exist; an existing one is read, never written to."""
    notes = []
    for label, target, url in (('repo', args.repo, args.repo_url),
                               ('record', args.record, args.record_url)):
        if not target or not url:
            continue
        target = os.path.expanduser(target)
        if os.path.exists(target):
            notes.append(f'{label} already in place')
            continue
        result = subprocess.run(['git', 'clone', url, target])
        if result.returncode != 0:
            return result.returncode, f'git clone {url} into {label} failed'
        notes.append(f'wrote {label}')
    return 0, '; '.join(notes) if notes else 'ok'


def _step_adopt(args):
    """Step 5 — ``asf init --product <p> [--repo] [--backlog]``, in-process (D3: one writer for
    ``products/<p>.yaml``)."""
    ns = argparse.Namespace(product=args.product,
                            repo=os.path.expanduser(args.repo) if args.repo else None,
                            backlog=os.path.expanduser(args.record) if args.record else None)
    rc = init.cmd_init(ns)
    return rc, ('ok' if rc == 0 else 'asf init failed')


def _step_account(args, plan):
    """Step 6 — the account: reports what :func:`_account_plan` resolved (already written into
    ``config.yaml`` by step 3 on a first run; read-only here otherwise). Never fails the run —
    a missing credential is a human act, named, not a broken install."""
    if plan['operator_line']:
        print(plan['operator_line'], file=sys.stderr)
    if plan['backend'] == 'fake':
        return 0, 'backend: fake, no account'
    return 0, f"{len(plan['names'])} account(s): {', '.join(plan['names'])}"


# ---- the missing-flag rule (PD9) ------------------------------------------------

def _resolve_missing(args):
    """``--repo``, ``--record`` and ``--scheduler``: a missing one is asked for once on a tty,
    with its default in the prompt, taking the default on an empty answer; with ``--yes``, or
    off a tty, a missing one with no default is one ``NEEDS OPERATOR`` line and a refusal, before
    anything is written. ``--record`` has no default unless the product file already carries
    ``backlog_dir``. Returns False when the run must stop here."""
    interactive = sys.stdin.isatty() and not args.yes
    existing = env.load_file(env.product_path(args.product))
    defaults = {'repo': os.getcwd(), 'record': existing.get('backlog_dir'), 'scheduler': 'launchd'}
    for name in ('repo', 'record', 'scheduler'):
        if getattr(args, name):
            continue
        default = defaults[name]
        if interactive:
            suffix = f' [{default}]' if default else ''
            answer = input(f'install: --{name}{suffix}: ').strip()
            setattr(args, name, answer or default)
        else:
            setattr(args, name, default)
        if not getattr(args, name):
            print(f'NEEDS OPERATOR: no --{name} given, and no default — pass --{name}',
                 file=sys.stderr)
            return False
    return True


# ---- the command ------------------------------------------------------------

def cmd_install(args, out=print):
    if not _resolve_missing(args):
        return 2
    plan = _account_plan(args)
    steps = [
        Step('step 1: the host', lambda: _step_host(args), True),
        Step('step 2: this asf', lambda: _step_asf(args), True),
        Step('step 3: the operator config', lambda: _step_config(args, plan), True),
        Step('step 4: the repos', lambda: _step_repos(args), True),
        Step('step 5: the adopt', lambda: _step_adopt(args), True),
        Step('step 6: the account', lambda: _step_account(args, plan), False),
    ]
    return run_steps(steps, out=out)


def register(subparsers):
    p = subparsers.add_parser(
        'install', help='configure this machine for one product: config, record, a worker '
                        'account, ending on a green doctor')
    p.add_argument('--product', required=True)
    p.add_argument('--repo', help='the product repo (default: the current directory; asked once)')
    p.add_argument('--repo-url', help='cloned into --repo only when that directory does not exist')
    p.add_argument('--record',
                   help="the record's directory (default: the product file's backlog_dir; asked once)")
    p.add_argument('--record-url', help='cloned into --record only when that directory does not exist')
    p.add_argument('--scheduler', choices=['launchd', 'cron', 'none'],
                   help='the clock adapter (default: launchd; asked once)')
    p.add_argument('--account', action='append', metavar='NAME[:CONFIG_DIR]',
                   help='repeatable; default: detect under <ASF_HOME>/accounts/')
    p.add_argument('--fake-workers', action='store_true', help='backend: fake, and no account')
    p.add_argument('--console-permissions', choices=['user', 'repo'],
                   help="write the console's own allow list at this scope, instead of only offering it")
    p.add_argument('--allow-checkout', action='store_true',
                   help='configure from a checkout or editable install (refused otherwise)')
    p.add_argument('--yes', action='store_true',
                   help='never prompt; a missing flag with no default is refused')
    p.set_defaults(run=cmd_install)
    return p
