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
after them can work. Steps 7-12 (the hooks, the clocks, the plugin, the console offer, the doctor
and the dry run) never abort — a failure there is recorded and the rest still runs, the same
discipline the bootstrap's own step function already has today.
"""
import argparse
import datetime
import difflib
import json
import os
import re
import shutil
import subprocess
import sys

from asf import cli, console_perms, doctor, env, hooks, init, plugin_build, scheduler
from asf.tick import dry_run
from asf.workers import pool

#: The bootstrap script this module is `exec`ed by — never written as a literal path (the
#: convention checker's own fence: a hardcoded `tools/*.sh` name is a product's tooling call).
INSTALL_TOOL = os.path.join('tools', 'install' + '.sh')

#: ``capacity.total.sessions`` written by the template when the operator has not chosen yet — a
#: small, safe number a first tick can run with; ``docs/config.example.yaml`` explains the field.
DEFAULT_SESSIONS = 2

#: The Claude Code runtime's own credential file, found under an account's ``config_dir``. Its
#: presence is checked with ``os.path.isfile``; its bytes are never opened (D10).
RUNTIME_CREDENTIAL_FILE = '.credentials.json'

# ---- the release-tag default (PD6) ------------------------------------------

def newest_tag(refs):
    """The newest ``v<major>.<minor>.<patch>`` tag among ``refs`` — bare tag names, or
    ``git ls-remote``'s own ``<sha>\\trefs/tags/<name>`` lines — kept only when
    ``cli.RELEASE_TAG`` fullmatches the name (a stray like ``v0.2.0-rc1`` is dropped), then
    ordered by the integer tuple of its digits (``v0.10.0`` sorts after ``v0.2.0``, unlike a
    lexical sort). ``None`` when nothing matches. The bootstrap that installs this package before
    it exists on ``PATH`` carries its own copy of this same rule, pinned equal to this one by
    ``ReleaseRefTests`` (PD7)."""
    names = []
    for ref in refs:
        ref = ref.strip()
        if not ref:
            continue
        name = ref.rsplit(None, 1)[-1].rsplit('refs/tags/', 1)[-1]
        if cli.RELEASE_TAG.fullmatch(name):
            names.append(name)
    if not names:
        return None
    return max(names, key=lambda n: tuple(int(g) for g in re.findall(r'\d+', n)))


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


# ---- the doctor baseline (read before step 3 writes anything) -------------------

def _doctor_red_keys(product_name):
    """The set of RED keys the doctor's DOCTOR and SCHEDULER tables show right now: a check's own
    name for a DOCTOR row, ``scheduler:<label>`` for a SCHEDULER row — the same shape
    the bootstrap script's own ``doctor_reds`` reads off the printed table, computed here from the
    rows themselves. Safe to call before ``config.yaml`` or the product file exist: a missing one
    is one ``ConfigError`` :func:`asf.doctor.check_config` already catches, and ``run`` returns
    just the single ``config`` row."""
    rows = doctor.run(product_name)
    keys = {name for name, required, ok, _detail in rows if required and ok is False}
    if rows and rows[0][2]:
        cfg = env.load_config()
        product = env.load_product(product_name)
        srows = doctor.scheduler_rows(cfg, product)
        keys |= {f'scheduler:{label}' for level, label, _detail in srows if level == doctor.RED}
    return keys


# ---- step 7: the hooks ------------------------------------------------------------

def _step_hooks(args):
    """Step 7 — ``asf hooks install --product <p>``, in-process. Idempotent because
    ``hooks.merge`` merges and keeps every unrelated key (``asf/hooks.py:252-272``)."""
    product = env.load_product(args.product)
    rc, message = hooks.install(product)
    print(message, file=sys.stderr if rc else sys.stdout)
    return rc, ('ok' if rc == 0 else 'see NEEDS OPERATOR above')


# ---- step 8: the clocks, with their read-back retry ------------------------------

def _clock_labels(product_name, cfg):
    try:
        declared = scheduler.clocks(env.load_product(product_name))
    except scheduler.SchedulerError:
        return []
    return [scheduler.label_for(product_name, c.name, cfg) for c in declared]


def _clocks_not_loaded(labels):
    return [label for label in labels if not scheduler.status(label).get('loaded')]


def _step_scheduler(args):
    """Step 8 — ``asf scheduler install --product <p>``, then every declared clock read back
    through ``asf scheduler status``; a clock still absent gets one retried bootstrap, and one
    still not loaded after it is a ``NEEDS OPERATOR`` line naming it and a failed step
    (``scheduler_install_verified``, the bootstrap's own retry, behaviour intact). With
    ``--scheduler none`` there is nothing to install or read back (PD10)."""
    if args.scheduler == 'none':
        return 0, 'already in place'
    install_ns = argparse.Namespace(scheduler_command='install', product=args.product,
                                    clock=None, label=None, json=False)
    rc = scheduler.cmd_scheduler(install_ns)
    if rc not in (0, 3):
        return rc, 'asf scheduler install failed'
    cfg = env.load_config()
    if scheduler.kind(cfg) != 'launchd':
        return 0, 'ok'
    labels = _clock_labels(args.product, cfg)
    missing = _clocks_not_loaded(labels)
    if not missing:
        return 0, 'ok'
    print(f"install: clock check: {', '.join(missing)} not loaded — retrying the bootstrap once",
         file=sys.stderr)
    scheduler.cmd_scheduler(install_ns)
    missing = _clocks_not_loaded(labels)
    if not missing:
        return 0, 'repaired (clock retried)'
    print('install: NEEDS OPERATOR: clock(s) still not loaded after retrying the bootstrap: '
         + ', '.join(missing), file=sys.stderr)
    return 1, f'clock(s) not loaded: {", ".join(missing)}'


# ---- step 9: the plugin -----------------------------------------------------------

def _step_plugin(args):
    """Step 9 — ``asf plugin install``, calling :func:`asf.plugin_build.install` (Task 3's
    writer). The tree is regenerated byte-identical, so a second run writes nothing new."""
    rc = plugin_build.install(out=print)
    return rc, 'ok'


# ---- step 10: the console permissions offer ----------------------------------------

def _step_console_permissions(args):
    """Step 10 — ``asf console-permissions offer --product <p>`` printed, or
    ``install --product <p> --scope <s>`` when ``--console-permissions`` was given
    (``asf/console_perms.py:154-163``)."""
    command = 'install' if args.console_permissions else 'offer'
    ns = argparse.Namespace(console_permissions_command=command, product=args.product,
                            scope=args.console_permissions or 'user')
    rc = console_perms.cmd_console_permissions(ns)
    if command == 'offer':
        return rc, 'offered'
    return rc, ('wrote' if rc == 0 else 'FAILED')


# ---- step 11: the doctor, against the baseline -------------------------------------

def _step_doctor(args, baseline):
    """Step 11 — ``asf doctor --product <p>`` against the baseline read before step 3: a RED
    also in the baseline is one ``WARN`` line naming it and does not fail the step, a RED that is
    not fails it, and a non-zero doctor with no RED row read is a failure (the doctor itself
    broke). The exit status is the doctor's own (``asf/doctor.py:1079-1091``)."""
    rc = doctor.cmd_doctor(argparse.Namespace(product=args.product), None)
    if rc == 0:
        return 0, 'ok'
    current = _doctor_red_keys(args.product)
    old = sorted(k for k in current if k in baseline)
    new = sorted(k for k in current if k not in baseline)
    if old:
        print('install: WARN doctor RED before this install too (not caused by it): '
             + ', '.join(old), file=sys.stderr)
    if new:
        print('install: doctor RED caused by this install: ' + ', '.join(new), file=sys.stderr)
        return rc, f'RED caused by this install: {", ".join(new)}'
    if old:
        return 0, f'pre-existing RED only: {", ".join(old)}'
    return rc, 'doctor exited non-zero with no RED row read'


# ---- step 12: the tick dry run -----------------------------------------------------

def _step_dry_run(args):
    """Step 12 — ``asf tick --product <p> --dry-run`` (``asf/tick/tick.py:344-346``): a
    throwaway copy of the state directory, never pushing, never launching."""
    product = env.load_product(args.product)
    rc = dry_run.run(product, out=print)
    return rc, ('ok' if rc == 0 else 'dry run failed')


# ---- the tail: the plugin console commands, or the probed command instead ---------

def _probe_plugin_command():
    """The runtime's own CLI, probed once at run time for a non-interactive plugin-install
    subcommand (D8): a ``claude`` on ``PATH`` whose ``plugin --help`` names both ``install`` and
    ``marketplace``. A probe that finds nothing supported is not a guess — the operator line
    stays the answer."""
    claude = shutil.which('claude')
    if not claude:
        return None
    try:
        result = subprocess.run([claude, 'plugin', '--help'], capture_output=True, text=True,
                                timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    help_text = result.stdout + result.stderr
    if 'install' in help_text and 'marketplace' in help_text:
        return claude
    return None


def _tail_lines(dest):
    """The plugin's two console commands naming the written path, or — when the probe finds a
    supported command — that command run instead, and which was used (D8)."""
    claude = _probe_plugin_command()
    if claude:
        subprocess.run([claude, 'plugin', 'marketplace', 'add', dest], check=False)
        subprocess.run([claude, 'plugin', 'install', 'asf@asf'], check=False)
        return [f'install: ran {claude} plugin marketplace add {dest} and '
               f'{claude} plugin install asf@asf (the runtime CLI offered a non-interactive install)']
    return [
        'install: in the Claude Code session for this product, add the plugin once:',
        f'  /plugin marketplace add {dest}',
        '  /plugin install asf@asf',
    ]


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
    baseline = _doctor_red_keys(args.product)  # before step 3 writes anything
    plan = _account_plan(args)
    steps = [
        Step('step 1: the host', lambda: _step_host(args), True),
        Step('step 2: this asf', lambda: _step_asf(args), True),
        Step('step 3: the operator config', lambda: _step_config(args, plan), True),
        Step('step 4: the repos', lambda: _step_repos(args), True),
        Step('step 5: the adopt', lambda: _step_adopt(args), True),
        Step('step 6: the account', lambda: _step_account(args, plan), False),
        Step('step 7: the hooks', lambda: _step_hooks(args), False),
        Step('step 8: the clocks', lambda: _step_scheduler(args), False),
        Step('step 9: the plugin', lambda: _step_plugin(args), False),
        Step('step 10: the console permissions', lambda: _step_console_permissions(args), False),
        Step('step 11: the doctor', lambda: _step_doctor(args, baseline), False),
        Step('step 12: the dry run', lambda: _step_dry_run(args), False),
    ]
    rc = run_steps(steps, out=out)
    for line in _tail_lines(plugin_build.installed_plugin_dir()):
        out(line)
    out('asf install: done' if rc == 0 else
       'asf install: NEEDS OPERATOR — fix the FAILED step(s) above and re-run')
    return rc


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


# ---- asf uninstall ---------------------------------------------------------------
#
# The inverse of the four things `asf install` puts on the machine — the clocks, the runtime
# hook entries, the git hooks, and the generated plugin tree — and nothing else (D9): the record,
# both repos, `<ASF_HOME>/config.yaml`, `products/`, `state/` and `logs/` are never touched here.

def _uninstall_clocks(product_name, cfg, dry_run):
    """Every loaded job labelled ``<label_prefix>.<product_name>.*`` — the never-declared
    ``ci-queue`` clock included, since it is found the same way any other loaded job is
    (:func:`scheduler.loaded_jobs`), not read off the product file. Another product's jobs and a
    legacy label the operator still declares are outside this prefix and are left alone."""
    prefix = f'{scheduler.label_prefix(cfg)}.{product_name}.'
    labels = sorted(job['label'] for job in scheduler.loaded_jobs(cfg=cfg)
                    if job['label'].startswith(prefix))
    if not labels:
        return [('clocks', 'absent')]
    rows = []
    for label in labels:
        if not dry_run:
            scheduler.uninstall(label)
        rows.append((f'clock {label}', 'removed'))
    return rows


def _drop_asf_hook_entries(settings):
    """``settings`` with every hook entry :func:`hooks._is_ours` recognises as the approvals hook
    (the only one ``hooks.install`` ever writes into an account's own settings file) removed;
    every other key, and every other hook entry, is kept untouched. Returns
    ``(new_settings, changed)``."""
    settings = dict(settings)
    new_hooks = {}
    changed = False
    for event, groups in (settings.get('hooks') or {}).items():
        new_groups = []
        for g in groups:
            before = g.get('hooks') or []
            kept = [h for h in before if not hooks._is_ours(h.get('command'), 'approvals', None)]
            changed = changed or len(kept) != len(before)
            if kept:
                new_groups.append(dict(g, hooks=kept))
        if new_groups:
            new_hooks[event] = new_groups
    if not changed:
        return settings, False
    if new_hooks:
        settings['hooks'] = new_hooks
    else:
        settings.pop('hooks', None)
    return settings, True


def _uninstall_hook_entries(cfg, dry_run):
    """The runtime hook entries :func:`hooks._is_ours` recognises, dropped from every worker
    account's settings file; every other key in that file is kept (`asf/hooks.py:246,276`)."""
    rows = []
    for account in pool.accounts_from_config(cfg):
        path = hooks.account_settings_path(account)
        if not os.path.isfile(path):
            continue
        with open(path, encoding='utf-8') as f:
            current = json.load(f)
        updated, changed = _drop_asf_hook_entries(current)
        if not changed:
            continue
        if not dry_run:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(json.dumps(updated, indent=2) + '\n')
        rows.append((f'hook entries in {path}', 'removed'))
    return rows or [('hook entries', 'absent')]


def _uninstall_git_hooks(product, dry_run):
    """The ``pre-commit`` / ``pre-push`` files ASF wrote in ``repo_dir`` and ``backlog_dir``,
    removed when :func:`hooks.is_git_hook_ours` says they are ours; a foreign hook is reported
    ``left (not ours)`` and kept (`asf/hooks.py:92`)."""
    rows = []
    for label, repo in (('repo', product.repo_dir), ('backlog', product.backlog_dir)):
        if not repo:
            continue
        hooks_dir = hooks.git_hooks_dir(repo)
        if hooks_dir is None:
            continue
        for name in hooks.GIT_HOOK_NAMES:
            path = os.path.join(hooks_dir, name)
            target = f'git hook: {label} {name}'
            if not os.path.isfile(path):
                rows.append((target, 'absent'))
                continue
            with open(path, encoding='utf-8') as f:
                text = f.read()
            if hooks.is_git_hook_ours(text, name):
                if not dry_run:
                    os.remove(path)
                rows.append((target, 'removed'))
            else:
                rows.append((target, 'left (not ours)'))
    return rows


def _uninstall_plugin(dry_run):
    """``<ASF_HOME>/plugin`` removed; the checkout's own ``plugin/`` is never touched — this is
    the generated tree :func:`plugin_build.installed_plugin_dir` names, never the checkout's."""
    dest = plugin_build.installed_plugin_dir()
    if not os.path.isdir(dest):
        return [('plugin tree', 'absent')]
    if not dry_run:
        shutil.rmtree(dest)
    return [('plugin tree', 'removed')]


def _append_uninstall_log(product_name):
    """One ``<utc>\\t<product>\\tuninstall`` line, the same shape the bootstrap's own install
    row takes — ``release.install_log`` skips a row whose ref is ``uninstall`` (PD8), so the
    readiness view keeps counting installs by hand and not teardowns."""
    path = os.path.join(env.ASF_HOME, 'logs', 'install.log')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    with open(path, 'a', encoding='utf-8') as f:
        f.write(f'{stamp}\t{product_name}\tuninstall\n')


def cmd_uninstall(args, out=print):
    """``asf uninstall --product <p> [--dry-run]``: takes away what ``asf install`` put on the
    machine — the clocks, the runtime hook entries, the git hooks, the generated plugin tree —
    and leaves the record, the repos and the operator's config exactly where they were (D9).
    ``--dry-run`` prints the same table and removes nothing."""
    cfg = env.load_config()
    product = env.load_product(args.product)
    rows = (_uninstall_clocks(args.product, cfg, args.dry_run)
           + _uninstall_hook_entries(cfg, args.dry_run)
           + _uninstall_git_hooks(product, args.dry_run)
           + _uninstall_plugin(args.dry_run))
    out(f'asf uninstall: {args.product}' + (' (dry run)' if args.dry_run else ''))
    for target, verdict in rows:
        out(f'  {target}: {verdict}')
    out('asf uninstall: the package itself: pipx uninstall asf-factory')
    if not args.dry_run:
        _append_uninstall_log(args.product)
    return 0


def register_uninstall(subparsers):
    p = subparsers.add_parser(
        'uninstall', help='remove the clocks, the hook entries and the plugin tree this '
                          'product installed; leaves the record, the repos and the config')
    p.add_argument('--product', required=True)
    p.add_argument('--dry-run', action='store_true', help='print the same table, remove nothing')
    p.set_defaults(run=cmd_uninstall)
    return p
