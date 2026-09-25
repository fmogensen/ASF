"""asf.approvals — the approval matrix as code (F-0031).

The catalogue (:data:`CLASSES`) names every action class the factory recognises, its default
level and where it is read. The matrix (:func:`matrix`) is that catalogue crossed with a
product's ``approvals:`` yaml, an operator's one file for widening or narrowing standing
authority. :func:`classify` turns one Claude Code tool call into the classes it matches;
:func:`signals` reads a product's own extra recognisers. The ledger (:func:`refuse`,
:func:`resolve`, :func:`holds`, :func:`open_holds`, :func:`is_granted`) is the append-only record
of every hold and its resolution, in ``~/.ASF/state/<product>/approvals.jsonl`` (D14).

:func:`run_hook` is the ``PreToolUse`` hook itself — ``asf hook approvals``, dispatched by
:mod:`asf.hooks` — which refuses a factory session's tool call whose class is not ``auto`` and
records the hold. It governs only a session (``ASF_JOB`` in the environment, D4), never edits to
the operator's own config (:func:`operator_config_target`, D8), and fails closed (D7). A refusal
tells the session it may not, and what to do instead; it asks no person and parks nothing — the
wave relaunches the item with the refusal in its brief (:func:`refusal_text`), and an item
refused one class on repeat relaunches becomes a question for the groom's adjudicator
(:func:`escalations`). Only the harvest's merge classes park an item (:func:`parked`).

:func:`cmd_approvals` is the operator's side of the same matrix — ``asf approvals`` prints it,
``asf approvals list`` the open holds, ``asf approvals resolve`` closes one — and
:func:`check_doctor` is the ``approvals`` row of ``asf doctor`` (§2.5).
"""
import dataclasses
import datetime
import fnmatch
import json
import os
import re
import sys

from asf import amendable, env, hooks

LEVELS = ('auto', 'groom', 'human-now')

RESOLUTIONS = ('granted', 'done', 'dropped', 'proposed')


@dataclasses.dataclass(frozen=True)
class ActionClass:
    name: str
    covers: str          # one line, printed by `asf approvals`
    default: str          # one of LEVELS
    read_by: tuple        # ('hook',) | ('harvest',) | ('hook', 'file_bugs') ...
    levels: tuple = LEVELS  # the levels a product yaml may map this class to
    # An open hold on it stops the wave launching the item. Only the harvest's merge classes
    # park (the branch waits to land either way); a refusal from the hook never parks an item —
    # the session is told to finish another way and the next brief names the refusal.
    parks: bool = False
    grantable: bool = True  # `granted` releases it
    why: str = ''           # the refusal's "you may not": why this is not a session's to do
    instead: str = ''       # what the session does instead
    # money, credentials or an action that cannot be undone: the only classes the adjudicator
    # may answer NEEDS OPERATOR on when an item keeps being refused for one
    operator: bool = False


CLASSES = (
    ActionClass(
        'spend_money', 'buying, subscribing, raising a paid tier or a spend limit',
        'human-now', ('hook',),
        why='spending money is never a session\'s to do',
        instead='finish with what is already paid for, and name the paid step under'
                ' `left out:` in your report',
        operator=True),
    ActionClass(
        'touch_production', 'deploying, pushing to the trunk',
        'human-now', ('hook',),
        why='pushing to the trunk and deploying are the harvest\'s job, not a session\'s',
        instead='push your own branch (`git push origin <your branch>`) and end there — the'
                ' harvest lands and deploys it'),
    ActionClass(
        'touch_security',
        "secrets, credentials, the runtime's hook and permission settings, the repo's git hooks",
        'human-now', ('hook',),
        why="secrets, credentials, the runtime's hook and permission settings and the repo's"
            ' git hooks are not yours to edit, and `--no-verify` is not yours to use',
        instead='fix what the hook or check flags in your own change, or leave the change out'
                ' and name it under `left out:`',
        operator=True),
    ActionClass(
        'touch_customer_data', 'reading or changing customer records, exports, production databases',
        'human-now', ('hook',),
        why='customer records, exports and production databases are not yours to read or'
            ' change',
        instead='work against fixtures or test data',
        operator=True),
    ActionClass(
        'touch_legal', 'licences, notices, terms, privacy texts',
        'human-now', ('hook',),
        why='licence, notice, terms and privacy texts are not yours to change',
        instead='leave them as they are and name the change you would have made under'
                ' `left out:`'),
    ActionClass(
        'touch_amendable_set',
        "a session writing the factory's own rules — rule cards, checks, hooks, role agents,"
        ' briefs, evals',
        'human-now', ('hook',), levels=('human-now',), grantable=False),
    ActionClass(
        'new_epic', 'opening a new Epic',
        'human-now', ('hook',),
        why='opening an Epic is the groom\'s decision, not a session\'s',
        instead='file the idea with `asf inbox --title "<the idea>"` and finish the work in'
                ' hand'),
    ActionClass(
        'merge_amendable_set', "landing a branch that touches the factory's own rules",
        'human-now', ('harvest',), parks=True),
    ActionClass(
        'merge_routine_pr', 'landing any other finished branch',
        'auto', ('harvest',), parks=True),
    ActionClass(
        'file_bug', 'filing or bumping a Bug',
        'auto', ('hook', 'file_bugs'),
        why='filing a Bug is the bug filer\'s and the groom\'s job, not a session\'s',
        instead='name the defect under `left out:` in your report'),
    ActionClass(
        'decide_feature', 'deciding an undecided Feature under a live Epic, by rule',
        'human-now', ('groom',)),
    ActionClass(
        'decide_bug', 'deciding an undecided Bug under a live Epic, by rule',
        'human-now', ('groom',)),
)

CLASSES_BY_NAME = {c.name: c for c in CLASSES}

#: Built-in path recognisers (§2.1): a glob with ``/`` matches the whole repo-relative path, a
#: glob without one matches the file name (see :func:`_match_glob`). `touch_security` also
#: carries `hooks.RUNTIME_SETTINGS_GLOBS` — the runtime adapter names its own path.
_PATH_GLOBS = {
    'touch_security': (
        '.env', '.env.*', '*.pem', '*.key', 'id_rsa*', 'id_ed25519*',
    ) + hooks.GIT_HOOK_GLOBS,
    'touch_legal': ('LICENSE*', 'LICENCE*', 'COPYING*', 'NOTICE*', 'TERMS*', 'PRIVACY*'),
}

#: Built-in Bash command recognisers (§2.1): a regular expression searched in the command text.
#: `touch_production` (the trunk push, the deploy workflow) is matched in :func:`_command_matches`
#: instead, since it needs the product's own `main` and `deploy_sha.workflow`.
_COMMAND_PATTERNS = {
    'touch_security': (
        r'\bgh\s+secret\b', r'\bgh\s+auth\b',
        # setting or unsetting the hooks path, not reading it (`git config core.hooksPath` prints it)
        r'\bgit\s+config\s+(?:--(?:global|local|system|worktree|replace-all|add)\s+)*core\.hooksPath\s+[^\s;&|)]',
        r'\bgit\s+config\s+(?:--\S+\s+)*--unset(?:-all)?\s+core\.hooksPath\b', r'--no-verify\b',
    ),
    'new_epic': (r'\basf\s+new\s+epic\b',),
    'file_bug': (r'\basf\s+new\s+bug\b',),
    'touch_amendable_set': (
        r'\basf\s+new\s+rule\b', r'\basf\s+set\s+R-\d{4}\b', r'\basf\s+hooks\s+install\b',
    ),
}


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# ---- the matrix and the signals ---------------------------------------------

# Keys of ``approvals:`` that are switches, not classes: ``groom: auto`` turns on F-0085's groom
# (asf.groom.policy.groom_auto); ``upgrade: auto`` lets the tick run ``asf upgrade`` itself when
# the trunk's package is ahead of the install (asf.drift). The matrix skips them; an unknown one
# is still an error.
GATES = ('groom', 'upgrade')


def matrix(product):
    """``{class: (level, 'yaml'|'default')}`` for every class in :data:`CLASSES`.

    Raises :class:`env.ConfigError` naming every key of ``product.approvals`` that is not a
    catalogue class, and every mapped level that is not in :data:`LEVELS`."""
    raw = {k: v for k, v in (product.approvals or {}).items() if k not in GATES}
    unknown = sorted(k for k in raw if k not in CLASSES_BY_NAME)
    bad_levels = sorted(
        f'{k}={v!r}' for k, v in raw.items() if k in CLASSES_BY_NAME and v not in LEVELS)
    if unknown or bad_levels:
        problems = []
        if unknown:
            problems.append('unknown class(es): ' + ', '.join(unknown))
        if bad_levels:
            problems.append(f'level(s) not in {LEVELS}: ' + ', '.join(bad_levels))
        raise env.ConfigError('approvals: ' + '; '.join(problems))
    for k, v in raw.items():
        c = CLASSES_BY_NAME[k]
        if v not in c.levels:
            own = '(' + ', '.join(repr(l) for l in c.levels) + ')'
            raise env.ConfigError(
                f'approvals: {k}={v!r} is not a level this class takes {own} — no session'
                ' edits the amendable set (F-0024)')
    return {
        c.name: (raw[c.name], 'yaml') if c.name in raw else (c.default, 'default')
        for c in CLASSES
    }


def signals(product):
    """``{class: {'paths': [...], 'commands': [...]}}`` from ``product.approval_signals``.

    Raises :class:`env.ConfigError` for a class not in the catalogue, or a key other than
    ``paths``/``commands``. A class the product does not name is simply absent — its built-ins
    are unaffected (signals only ever add)."""
    raw = dict(getattr(product, 'approval_signals', None) or {})
    out = {}
    for cls, spec in raw.items():
        if cls not in CLASSES_BY_NAME:
            raise env.ConfigError(f'approval_signals: unknown class {cls!r}')
        spec = spec or {}
        bad_keys = sorted(set(spec) - {'paths', 'commands'}) if isinstance(spec, dict) else None
        if not isinstance(spec, dict) or bad_keys:
            raise env.ConfigError(
                f"approval_signals.{cls}: keys must be 'paths'/'commands', not {bad_keys!r}")
        out[cls] = {
            'paths': list(spec.get('paths') or []),
            'commands': list(spec.get('commands') or []),
        }
    return out


# ---- the classifier -----------------------------------------------------------

def _match_glob(glob, relpath):
    target = relpath if '/' in glob else os.path.basename(relpath)
    return fnmatch.fnmatchcase(target, glob)


def _toplevel(cwd):
    import subprocess
    try:
        out = subprocess.run(
            ['git', 'rev-parse', '--show-toplevel'], cwd=cwd, capture_output=True, text=True,
        )
    except OSError:
        return cwd
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else cwd


def _repo_relpath(path, cwd):
    toplevel = os.path.realpath(_toplevel(cwd))
    abspath = os.path.realpath(path if os.path.isabs(path) else os.path.join(cwd or '', path))
    try:
        rel = os.path.relpath(abspath, toplevel)
    except ValueError:
        rel = abspath
    return rel.replace(os.sep, '/')


def _is_new_epic_write(tool_name, path, relpath, cwd):
    from asf.harvest import harvest  # local: harvest.py reads this module too (Task 6)
    if tool_name != 'Write' or not _match_glob('epics/*.md', relpath):
        return False
    if not harvest.is_record_repo(_toplevel(cwd)):
        return False
    abspath = path if os.path.isabs(path) else os.path.join(cwd or '', path)
    return not os.path.exists(abspath)


def _classify_path(product, tool_name, path, cwd):
    if not path:
        return []
    relpath = _repo_relpath(path, cwd)
    sig = signals(product)
    out = []
    for c in CLASSES:
        globs = list(_PATH_GLOBS.get(c.name, ()))
        if c.name == 'touch_security':
            globs += list(hooks.RUNTIME_SETTINGS_GLOBS)
        globs += sig.get(c.name, {}).get('paths', [])
        matched = any(_match_glob(g, relpath) for g in globs)
        if not matched and c.name == 'new_epic':
            matched = _is_new_epic_write(tool_name, path, relpath, cwd)
        if matched:
            out.append((c.name, relpath))
    return out


_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?^[ \t]*\2[ \t]*$\n?", re.S | re.M)


def _strip_heredocs(command):
    """``command`` with every heredoc body removed (the ``<<EOF`` operator stays, its lines up to
    the closing word go): a body is data written to a file or a pipe — a review quoting a push,
    a doc with an apostrophe — never a command of the session's own."""
    return _HEREDOC.sub(' \n', command)


def _pushes_trunk(command, main):
    """True when one of the command's own ``git push`` invocations names the trunk as a refspec.
    Each simple command is split off the compound (``&&``, ``||``, ``;``, ``|``) and read with
    shlex, so ``main`` in a commit message, a fetch or a log range never counts."""
    import shlex
    command = _strip_heredocs(command)
    if not re.search(r'\bgit\b.*\bpush\b', command):
        return False
    command = command.replace('\n', ' ; ')  # a line is a command of its own
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:  # unbalanced quotes: fall back to refusing, the safe side
        return True
    trunk = {main, f'refs/heads/{main}'}
    out = [[]]
    for t in tokens:
        if t in ('&&', '||', ';', '|', '&', ';;'):
            out.append([])
        else:
            out[-1].append(t)
    for argv in out:
        # skip `git -C dir` style globals before the verb
        if len(argv) < 2 or argv[0] != 'git':
            continue
        i = 1
        while i < len(argv) and argv[i].startswith('-'):
            i += 2 if argv[i] in ('-C', '-c') else 1
        if i >= len(argv) or argv[i] != 'push':
            continue
        refspecs = [a for a in argv[i + 1:] if not a.startswith('-')][1:]  # drop the remote
        for ref in refspecs:
            dst = ref.lstrip('+').rsplit(':', 1)[-1]
            if dst in trunk:
                return True
    return False


def _runs_deploy_workflow(command, product):
    deploy = product.deploy_sha
    workflow = deploy.get('workflow') if isinstance(deploy, dict) else None
    if not workflow:
        return False
    return bool(re.search(rf'\bgh\s+workflow\s+run\b.*\b{re.escape(workflow)}\b', command))


def _command_matches(cls_name, command, product, extra_patterns):
    if cls_name == 'touch_production' and (
            _pushes_trunk(command, product.main) or _runs_deploy_workflow(command, product)):
        return True
    patterns = list(_COMMAND_PATTERNS.get(cls_name, ())) + list(extra_patterns)
    return any(re.search(p, command) for p in patterns)


def _classify_command(product, command):
    sig = signals(product)
    detail = command[:120]
    out = []
    for c in CLASSES:
        if _command_matches(c.name, command, product, sig.get(c.name, {}).get('commands', [])):
            out.append((c.name, detail))
    return out


def classify(product, tool_name, tool_input, cwd):
    """``[(class, detail)]`` in catalogue order — the classes ``tool_name``/``tool_input``
    matches. ``Write``/``Edit``/``MultiEdit`` read ``file_path``, ``NotebookEdit``
    ``notebook_path``, ``Bash`` ``command``; every other tool classifies as nothing."""
    tool_input = tool_input or {}
    if tool_name in ('Write', 'Edit', 'MultiEdit'):
        return _classify_path(product, tool_name, tool_input.get('file_path'), cwd)
    if tool_name == 'NotebookEdit':
        return _classify_path(product, tool_name, tool_input.get('notebook_path'), cwd)
    if tool_name == 'Bash':
        return _classify_command(product, tool_input.get('command') or '')
    return []


# ---- the harvest's merge classes (§2.4) ----------------------------------------

def merge_class(product, files):
    """``(class, first matched file or None)`` — ``merge_amendable_set`` when any of ``files``
    (repo-relative, as :func:`asf.harvest.lane.touched_files` returns them) matches
    ``amendable.paths(product)`` (the §2.1 glob rule, :func:`_match_glob`), else
    ``merge_routine_pr`` with no matched file."""
    globs = list(amendable.paths(product))
    for f in files:
        if any(_match_glob(g, f) for g in globs):
            return 'merge_amendable_set', f
    return 'merge_routine_pr', None


def path_class(product, relpath):
    """``(class, level)`` of the first class in catalogue order whose path globs match
    ``relpath`` and whose level is not ``auto`` — the built-in recognisers, the runtime settings
    files, the product's ``approval_signals`` paths, and the amendable set
    (``merge_amendable_set``) — or None: the path is not approvals-protected."""
    try:
        sig = signals(product)
    except env.ConfigError:
        sig = {}
    for c in CLASSES:
        globs = list(_PATH_GLOBS.get(c.name, ()))
        if c.name == 'touch_security':
            globs += list(hooks.RUNTIME_SETTINGS_GLOBS)
        if c.name == 'merge_amendable_set':
            globs += list(amendable.paths(product))
        globs += sig.get(c.name, {}).get('paths', [])
        if any(_match_glob(g, relpath) for g in globs):
            level = level_of(product, c.name)
            if level != 'auto':
                return c.name, level
    return None


def level_of(product, cls):
    """``matrix(product)[cls]``'s level; an invalid matrix makes it ``human-now`` (D7) rather
    than raise, since a harvest or a bug filer that cannot read the matrix must still fail
    closed, not crash."""
    try:
        return matrix(product)[cls][0]
    except env.ConfigError:
        return 'human-now'


# ---- the ledger ----------------------------------------------------------------

def ledger_path(product):
    return os.path.join(env.state_dir(product), 'approvals.jsonl')


def append(product, record):
    with open(ledger_path(product), 'a', encoding='utf-8') as f:
        f.write(json.dumps(record, sort_keys=True) + '\n')


def read(product):
    path = ledger_path(product)
    out = []
    if not os.path.isfile(path):
        return out
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def refuse(product, item, cls, level, job, tool, detail, **extra):
    """Appends a ``refused`` line (§4) and returns its hold id, ``<item>/<class>``.

    ``extra`` rides the same line — ``kind`` and ``patch`` for a refused write to the amendable
    set (§2.3), nothing for every other class."""
    hold = f'{item}/{cls}'
    append(product, {
        'event': 'refused', 'hold': hold, 'item': item, 'class': cls, 'level': level,
        'job': job, 'tool': tool, 'detail': detail, 'ts': _now_iso(), **extra,
    })
    return hold


def resolve(product, hold, resolution):
    if resolution not in RESOLUTIONS:
        raise ValueError(f'resolution must be one of {RESOLUTIONS}, not {resolution!r}')
    if resolution == 'granted':
        c = CLASSES_BY_NAME.get(hold.split('/', 1)[-1])
        if c and not c.grantable:
            raise ValueError(
                f'{c.name} is not grantable — no session edits the amendable set (F-0024)')
    append(product, {'event': 'resolved', 'hold': hold, 'resolution': resolution, 'ts': _now_iso()})


def _fold(product):
    """One entry per hold, in file order, plus the internal ``open`` flag :func:`open_holds`
    reads — true from a ``refused`` line until the next ``resolved`` line for that hold."""
    entries = {}
    for rec in read(product):
        hold = rec.get('hold')
        if not hold or '/' not in hold:
            continue
        item, cls = hold.split('/', 1)
        entry = entries.setdefault(hold, {
            'hold': hold, 'item': item, 'class': cls, 'level': None, 'detail': None,
            'first': None, 'last': None, 'count': 0, 'resolution': None,
            'announced': None, 'closed': None, 'open': False,
        })
        event = rec.get('event')
        ts = rec.get('ts')
        if event == 'refused':
            entry['level'] = rec.get('level')
            entry['detail'] = rec.get('detail')
            entry['first'] = entry['first'] or ts
            entry['last'] = ts
            entry['count'] += 1
            entry['open'] = True
        elif event == 'resolved':
            entry['resolution'] = rec.get('resolution')
            entry['open'] = False
        elif event == 'announced':
            entry['announced'] = ts
        elif event == 'closed':
            entry['closed'] = ts
    return entries


def holds(product):
    """``{hold: {item, class, level, detail, first, last, count, resolution, announced,
    closed}}`` — every hold the ledger has ever recorded, open or not."""
    return {h: {k: v for k, v in e.items() if k not in ('hold', 'open')}
            for h, e in _fold(product).items()}


def open_holds(product):
    """Every hold whose latest ``refused`` has no later ``resolved``, oldest first."""
    out = [{k: v for k, v in e.items() if k != 'open'}
           for e in _fold(product).values() if e['open']]
    out.sort(key=lambda e: e['first'] or '')
    return out


def is_granted(product, hold):
    """Whether ``hold``'s latest ``resolved`` line's resolution is ``granted``."""
    return _fold(product).get(hold, {}).get('resolution') == 'granted'


# ---- the hook — `asf hook approvals` (§2.3) ------------------------------------

def item_of_job(product, job):
    """The item ``job`` runs on, from the session ledger (P3); ``job`` itself when the ledger
    does not name one, so a hold is always attributable to something typable."""
    from asf.workers import lifecycle, pool  # local: pool reads this module's product paths
    try:
        run = lifecycle.latest(pool.sessions_path(product)).get(job) or {}
    except OSError:
        return job
    return run.get('item') or job


#: The Bash tokens that make a named path a *write* (§2.3 step 2). A command that names an
#: operator-config path without one of these only reads it.
#: ``>`` to /dev/null or onto another descriptor (``2>&1``) writes no file.
_WRITING_TOKENS = (
    r'>(?!>?\s*(?:/dev/null\b|&\d))', r'\btee\b', r'\bsed\s+-i\b', r'\bperl\s+-i\b', r'\bcp\b', r'\bmv\b', r'\brm\b',
)

#: The words a shell command is split into when looking for a path in it.
_BASH_WORD = re.compile(r"[^\s'\"|&;<>()]+")


def _operator_config_path(path):
    """``path`` resolved, when it is the operator's config — ``<ASF_HOME>/config.yaml`` or
    anything under ``<ASF_HOME>/products/`` — else ``None`` (D8).

    Both sides are expanded and realpath'd, and compared case-insensitively so the home spelled
    ``~/.ASF`` and the same directory spelled ``~/.asf`` are one place."""
    if not path:
        return None
    resolved = os.path.realpath(os.path.expanduser(str(path)))
    home = os.path.realpath(os.path.expanduser(env.ASF_HOME)).lower()
    low = resolved.lower()
    if low == os.path.join(home, 'config.yaml'):
        return resolved
    if low.startswith(os.path.join(home, 'products') + os.sep):
        return resolved
    return None


def operator_config_target(tool_name, tool_input):
    """The operator-config path this call would write, or ``None`` (D8).

    A ``Write``/``Edit``/``MultiEdit`` target, or a path named in a ``Bash`` command next to a
    writing token (:data:`_WRITING_TOKENS`). The factory never edits the file that holds its own
    authority, whatever the matrix in it says."""
    tool_input = tool_input or {}
    if tool_name in ('Write', 'Edit', 'MultiEdit'):
        return _operator_config_path(tool_input.get('file_path'))
    if tool_name == 'Bash':
        command = tool_input.get('command') or ''
        if not any(re.search(t, command) for t in _WRITING_TOKENS):
            return None
        for word in _BASH_WORD.findall(command):
            hit = _operator_config_path(word)
            if hit:
                return hit
    return None


#: The 8 KB captured-patch ceiling of §2.3 — the ledger is machine-local, but it is still a file.
_PATCH_LIMIT = 8 * 1024


def _edit_diff(old, new):
    """One ``Edit``/``MultiEdit`` edit as a two-hunk diff — what was there, then what replaces
    it — the shape ``asf propose`` later reads out of the ledger (§2.4)."""
    old, new = old or '', new or ''
    removed = '\n'.join(f'-{line}' for line in old.splitlines())
    added = '\n'.join(f'+{line}' for line in new.splitlines())
    return '\n'.join(part for part in (removed, added) if part)


def _intended_change(tool_name, tool_input):
    """The call's own payload — what §2.3's captured ``patch`` holds — truncated to 8 KB:
    ``content`` for ``Write``, ``old_string``/``new_string`` as a two-hunk diff for
    ``Edit``/``MultiEdit``, ``new_source`` for ``NotebookEdit``, the command text for ``Bash``."""
    tool_input = tool_input or {}
    if tool_name == 'Write':
        text = tool_input.get('content') or ''
    elif tool_name == 'Edit':
        text = _edit_diff(tool_input.get('old_string'), tool_input.get('new_string'))
    elif tool_name == 'MultiEdit':
        text = '\n'.join(
            _edit_diff(e.get('old_string'), e.get('new_string'))
            for e in tool_input.get('edits') or [])
    elif tool_name == 'NotebookEdit':
        text = tool_input.get('new_source') or ''
    elif tool_name == 'Bash':
        text = tool_input.get('command') or ''
    else:
        text = ''
    return text[:_PATCH_LIMIT]


def _amendable_refusal_lines(item, relpath, kind):
    """§2.3's five lines, byte for byte — what a session sees when it tries to write the
    amendable set. The other nine classes keep :func:`_refusal_lines`."""
    return [
        f'REFUSED touch_amendable_set (human-now) on {item} — {relpath} '
        f'({kind.name}: {kind.why})',
        '  No session edits the amendable set. The factory proposes; a person approves and'
        ' merges.',
        f'  Propose it: asf propose --from-hold {item}/touch_amendable_set --why "<one line>"',
        f'  Then print: NEEDS OPERATOR: {item} touch_amendable_set — <the proposal card asf'
        ' propose names>',
        '  and carry on with every part of the job that does not depend on it.',
    ]


def _refusal_lines(item, cls, level, detail):
    """What a session sees when the hook refuses it an action of ``cls``: that it may not, why,
    and what to do instead. Never a question for a person — the hold is recorded for the audit
    trail, nothing waits on it, and the item's next brief names it (:func:`refusal_text`).
    ``products/<p>.yaml`` is literal: the session is told where authority lives, not which file
    to go and edit (D8)."""
    c = CLASSES_BY_NAME.get(cls)
    why = (c.why if c and c.why else f"{c.covers if c else cls} is not a session's to do")
    instead = (c.instead if c and c.instead
               else 'leave it out and name it under `left out:` in your report')
    return [
        f'REFUSED {cls} ({level}) on {item} — {detail}',
        f'  You may not do this: {why} (approvals: in products/<p>.yaml).',
        f'  Do not retry it or work around it. Instead: {instead}.',
        '  This is not a question for a person: do not print NEEDS OPERATOR for it. Finish'
        ' every part of the job another way; the refusal is recorded and your next brief'
        ' names it.',
    ]


def run_hook(stdin_text, environ, out=sys.stderr, product=None):
    """``asf hook approvals`` (§2.3): the rc the runtime reads — 0 lets the call through, 2 blocks
    it and feeds ``out`` back to the model.

    ``product`` defaults to ``environ['ASF_PRODUCT']``. Every exception from the parse on refuses
    (D7): a boundary that opens when it breaks is not one."""
    if not (environ or {}).get('ASF_JOB'):
        return 0                                    # not a factory session (D4)
    try:
        return _enforce(stdin_text, environ, out, product)
    except Exception as e:                          # fail closed (D7)
        print(f'approvals hook failed: {e or type(e).__name__} — refused', file=out)
        return 2


def _enforce(stdin_text, environ, out, product):
    job = environ['ASF_JOB']
    call = json.loads(stdin_text or '{}') or {}
    tool_name = call.get('tool_name') or ''
    tool_input = call.get('tool_input') or {}
    cwd = call.get('cwd') or os.getcwd()

    if operator_config_target(tool_name, tool_input):
        print('REFUSED operator config — the factory never edits its own authority'
              ' (products/<p>.yaml)', file=out)
        return 2

    prod = env.load_product(product or environ.get('ASF_PRODUCT'))
    item = item_of_job(prod, job)

    hit = amendable.write_target(prod, tool_name, tool_input, cwd)   # §2.3 D7: before the matrix
    if hit:
        relpath, kind = hit
        refuse(prod, item, 'touch_amendable_set', 'human-now', job, tool_name, relpath,
               kind=kind.name, patch=_intended_change(tool_name, tool_input))
        print('\n'.join(_amendable_refusal_lines(item, relpath, kind)), file=out)
        return 2

    matched = classify(prod, tool_name, tool_input, cwd)
    if not matched:
        return 0

    invalid = None
    try:
        levels = {name: level for name, (level, _) in matrix(prod).items()}
    except env.ConfigError as e:
        invalid, levels = str(e), None               # every matched class is human-now (D7)

    refused = []
    for cls, detail in matched:                      # catalogue order
        level = 'human-now' if levels is None else levels[cls]
        if level == 'auto':
            continue
        c = CLASSES_BY_NAME[cls]
        if c.grantable and is_granted(prod, f'{item}/{cls}'):
            continue
        refuse(prod, item, cls, level, job, tool_name, detail)
        refused.append((cls, level, detail))
    if not refused:
        return 0
    if invalid:
        print(f'approvals: the matrix is invalid ({invalid})'
              ' — every classified action is human-now', file=out)
    for cls, level, detail in refused:
        for line in _refusal_lines(item, cls, level, detail):
            print(line, file=out)
    return 2


# ---- the tick's raise and the parking (§2.4) ----------------------------------

def session_refusal(cls):
    """A class the hook refuses a session and tells it why: it never parks and asks no one —
    every hook class but ``touch_amendable_set``, whose refusal is a proposal (F-0024)."""
    c = CLASSES_BY_NAME.get(cls)
    return bool(c and 'hook' in c.read_by and c.name != 'touch_amendable_set')


def raise_holds(ctx, out):
    """The ``wave`` step's first act: say what is held, record each hold once, and answer with
    ``{item: (class, level)}`` — the items a launching row must not be started on.

    A refusal the hook told a session about (:func:`session_refusal`) is no one's question: it
    is counted in one line and parks nothing — the wave relaunches the item, its brief names
    the refusal, and a repeat goes to the groom's adjudicator (:func:`escalations`). One ``NEEDS
    OPERATOR`` line per other open ``human-now`` hold (the harvest's merge classes, a proposal
    to the amendable set), oldest first, and one summary line however many ``groom`` holds are
    open. The ``announced`` and ``closed`` markers live in the ledger beside the refusals (§4),
    so the ``held`` and ``hold-resolved`` events are written once over a hold's life however
    many ticks see it — the fold's ``first`` never moves, so one marker is one announcement,
    and a repeat refusal bumps ``count`` without raising a second event.
    """
    product = ctx.product
    open_ = open_holds(product)                      # oldest first

    refused = [e for e in open_ if session_refusal(e['class'])]
    for e in open_:
        if e['level'] == 'human-now' and not session_refusal(e['class']):
            out(f"NEEDS OPERATOR: held {e['class']} on {e['item']} — {e['detail']} —"
                f" asf approvals resolve {e['item']}/{e['class']} granted|done|dropped")
    groom = sum(1 for e in open_ if e['level'] == 'groom' and not session_refusal(e['class']))
    if groom:
        out(f'approvals: {groom} held for groom — asf approvals list')
    if refused:
        items = len({e['item'] for e in refused})
        out(f'approvals: {len(refused)} refused action(s) on {items} item(s) recorded — none'
            ' parks its item; asf approvals list')

    for e in open_:
        if e['announced']:
            continue
        hold = f"{e['item']}/{e['class']}"
        ctx.event('held', hold=hold, item=e['item'], level=e['level'], count=e['count'],
                  **{'class': e['class']})           # PD9: `class` is not a keyword argument
        append(product, {'event': 'announced', 'hold': hold, 'ts': _now_iso()})
    for hold, e in _fold(product).items():
        if e['open'] or not e['resolution'] or not e['announced'] or e['closed']:
            continue
        ctx.event('hold-resolved', hold=hold, item=e['item'], resolution=e['resolution'],
                  **{'class': e['class']})
        append(product, {'event': 'closed', 'hold': hold, 'ts': _now_iso()})

    return parked(product, open_)


def parked(product, open_=None):
    """``{item: (class, level)}`` — the items an open hold parks: a launching row on one waits
    for a person, not a slot (:func:`asf.feeder.tiers.select` gives it none). Only a class with
    ``parks`` (the harvest's merge classes) parks; a hook refusal never does. Read-only, so
    ``asf next`` and the status cell plan with the same holds the tick's wave does."""
    open_ = open_holds(product) if open_ is None else open_
    order = {c.name: i for i, c in enumerate(CLASSES)}
    held = {}
    for e in open_:                                  # first in catalogue order wins the item
        c = CLASSES_BY_NAME.get(e['class'])
        if c and not c.parks:                         # named but the wave still launches on it
            continue
        rank = order.get(e['class'], len(order))
        if e['item'] not in held or rank < held[e['item']][0]:
            held[e['item']] = (rank, e['class'], e['level'])
    return {item: (cls, level) for item, (_, cls, level) in held.items()}


# ---- a refusal on the next run: the relaunch brief and the escalation ----------

#: An item refused for one class on this many relaunches in a row (each briefed with the
#: refusal, each refused again) goes to the groom's adjudicator as a question.
ESCALATE_RELAUNCHES = 2


def _refused_runs(product, item=None):
    """``{item: [(run, {class: [refused record]})]}`` — every run of each item (of ``item``
    alone when given) off the session ledger, oldest first, with the hook refusals recorded
    during it. A refusal belongs to the latest run of its job that started at or before it."""
    from asf.workers import lifecycle, pool  # local: pool reads this module's product paths
    name = product.name if isinstance(product, env.Product) else product
    if not name or not os.path.isfile(os.path.join(env.ASF_HOME, 'state', name,
                                                   'approvals.jsonl')):
        return {}                                   # read-only: no state dir made to find out
    try:
        recs = [r for r in read(product) if r.get('event') == 'refused'
                and session_refusal(r.get('class')) and (item is None or r.get('item') == item)]
    except OSError:
        return {}
    items = {r.get('item') for r in recs if r.get('item')}
    if not items:
        return {}
    try:
        all_runs = lifecycle.runs(pool.sessions_path(product))
    except OSError:
        return {}
    by_item = {}
    for rs in all_runs.values():
        for run in rs:
            if run.get('item') in items and run.get('started'):
                by_item.setdefault(run['item'], []).append(run)
    out = {}
    for iid, rs in by_item.items():
        rs.sort(key=lambda r: r['started'])
        slots = [(run, {}) for run in rs]
        for rec in recs:
            if rec.get('item') != iid:
                continue
            ts = rec.get('ts') or ''
            at = None
            for i, (run, _) in enumerate(slots):
                if run.get('job') == rec.get('job') and run['started'] <= ts:
                    at = i
            if at is not None:
                slots[at][1].setdefault(rec['class'], []).append(rec)
        out[iid] = slots
    return out


def refusal_text(product, item):
    """The relaunch brief's paragraph: what the hook refused ``item``'s latest run and why, so
    the next session does not repeat it — ``''`` when that run was refused nothing (or ``item``
    has no run). Read before the new run is recorded, the latest run is the one before it."""
    slots = _refused_runs(product, item).get(item) or []
    if not slots or not slots[-1][1]:
        return ''
    lines = ['REFUSED LAST RUN — the approvals hook refused the previous session on this item'
             ' these actions. They are not yours to do; doing them again ends in the same'
             ' refusal, so finish the work another way:']
    for cls in sorted(slots[-1][1], key=lambda n: [c.name for c in CLASSES].index(n)):
        c = CLASSES_BY_NAME[cls]
        details = sorted({(r.get('detail') or '').strip() for r in slots[-1][1][cls]} - {''})
        what = '; '.join(f'`{d}`' for d in details[:3]) or cls
        why = c.why or f"{c.covers} is not a session's to do"
        instead = c.instead or 'leave it out and name it under `left out:`'
        lines.append(f'- {cls}: {what} — {why}. Instead: {instead}.')
    lines.append('None of this is a question for a person: do not print NEEDS OPERATOR for it.')
    return '\n'.join(lines)


def escalations(product):
    """``{item: (class, runs)}`` — each item whose latest ``runs`` runs were all refused
    ``class``, the first of them plus :data:`ESCALATE_RELAUNCHES` relaunches or more: a question
    for the groom's adjudicator (drop, reshape or close), never the operator's. The class first
    in catalogue order wins an item refused for two."""
    out = {}
    order = [c.name for c in CLASSES]
    for iid, slots in _refused_runs(product).items():
        best = None
        for cls in {k for _, refused in slots for k in refused}:
            n = 0
            for _run, refused in reversed(slots):
                if cls not in refused:
                    break
                n += 1
            if n >= ESCALATE_RELAUNCHES + 1 and (
                    best is None or order.index(cls) < order.index(best[0])):
                best = (cls, n)
        if best:
            out[iid] = best
    return out


# ---- the operator's side — `asf approvals` and the doctor row (§2.5) -----------

#: The built-in recognisers that are code, not a glob or a pattern in the two tables above —
#: named here in the words :func:`recognisers` prints. A class absent from both this map and
#: those tables has no built-in at all: only a product's own `approval_signals` can match it.
_CODE_RECOGNISERS = {
    'touch_production': (
        "Bash: a git push whose refspec targets the product's main",
        'Bash: gh workflow run the deploy_sha.workflow, when one is set',
    ),
    'new_epic': ('Write of an epics/*.md that does not exist yet, in a record repo',),
    'touch_amendable_set': ('the amendable set — asf approvals names it',),
    'merge_amendable_set': ("the branch's changed files against conventions.amendable_paths",),
    'merge_routine_pr': ('every branch that is not merge_amendable_set',),
    'file_bug': ("the tick's bug filer",),
    'decide_feature': ("the groom's decide_by_approval policy, over undecided Features",),
    'decide_bug': ("the groom's decide_by_approval policy, over undecided Bugs",),
}


def _pattern_in_words(pattern):
    r"""One :data:`_COMMAND_PATTERNS` regex as the command it recognises, for the table:
    ``\bgh\s+secret\b`` -> ``gh secret``."""
    return re.sub(r'\\s\+', ' ', pattern).replace(r'\b', '').replace('\\', '')


def _builtin_recognisers(cls_name):
    """``cls_name``'s built-ins in words, derived from the tables the classifier itself reads so
    the two cannot drift apart."""
    out = []
    globs = list(_PATH_GLOBS.get(cls_name, ()))
    if cls_name == 'touch_security':
        # The runtime settings globs are `hooks.RUNTIME_SETTINGS_GLOBS` — named, not spelled:
        # that path is the runtime adapter's convention and does not belong in this module.
        globs.append("the runtime's own settings files")
    if globs:
        out.append('paths: ' + ', '.join(globs))
    patterns = _COMMAND_PATTERNS.get(cls_name, ())
    if patterns:
        out.append('Bash: ' + ', '.join(_pattern_in_words(p) for p in patterns))
    out.extend(_CODE_RECOGNISERS.get(cls_name, ()))
    return out


def recognisers(product):
    """``{class: [recogniser in words]}`` — every built-in named, then the product's own
    ``approval_signals`` listed. A class whose list is empty matches nothing at all, which is
    what the doctor row warns about when that class is not ``auto``."""
    sig = signals(product)
    out = {}
    for c in CLASSES:
        words = _builtin_recognisers(c.name)
        spec = sig.get(c.name) or {}
        words += [f'signal path: {p}' for p in spec.get('paths') or []]
        words += [f'signal command: {p}' for p in spec.get('commands') or []]
        out[c.name] = words
    return out


_TABLE_HEADER = ('class', 'level', 'from', 'read by', 'recognisers')


def _table(rows):
    """``rows`` (the header first) as aligned columns, the last one left to run on."""
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]) - 1)]
    return [
        '  '.join(cell.ljust(w) for cell, w in zip(row, widths)) + '  ' + row[-1]
        for row in rows
    ]


def format_matrix(product):
    """One row per catalogue class: ``class | level | from | read by | recognisers`` (§2.5)."""
    levels = matrix(product)
    known = recognisers(product)
    rows = [_TABLE_HEADER]
    for c in CLASSES:
        level, source = levels[c.name]
        rows.append((c.name, level, source, ', '.join(c.read_by),
                     '; '.join(known[c.name]) or 'none — approval_signals only'))
    return _table(rows)


_LIST_HEADER = ('hold', 'level', 'first', 'last', 'count', 'detail')


def format_open_holds(product_name):
    """The open holds: ``hold | level | first | last | count | detail`` (§2.5)."""
    open_ = open_holds(product_name)
    if not open_:
        return ['no open holds']
    rows = [_LIST_HEADER] + [
        (h['hold'], h['level'] or '', h['first'] or '', h['last'] or '', str(h['count']),
         h['detail'] or '')
        for h in open_
    ]
    return _table(rows)


def cmd_approvals(args):
    name = args.product or env.default_product_name()
    action = getattr(args, 'approvals_command', None)

    if action == 'list':
        print('\n'.join(format_open_holds(name)))
        return 0

    if action == 'resolve':
        hold, resolution = args.hold, args.resolution
        if hold not in {h['hold'] for h in open_holds(name)}:
            print(f'{hold} is not an open hold — asf approvals list', file=sys.stderr)
            return 2
        try:
            resolve(name, hold, resolution)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        print(f'resolved {hold} as {resolution}')
        return 0

    print('\n'.join(format_matrix(env.load_product(name))))
    return 0


def register(subparsers):
    p = subparsers.add_parser(
        'approvals', help="the approval matrix, the open holds, and resolving one")
    p.add_argument('approvals_command', nargs='?', choices=['list', 'resolve'],
                   help='omitted: print the effective matrix')
    p.add_argument('hold', nargs='?', help='resolve: <item>/<class>')
    p.add_argument('resolution', nargs='?', choices=list(RESOLUTIONS),
                   help='resolve: what happened to the held action')
    p.add_argument('--product')
    p.set_defaults(run=cmd_approvals)
    return p


def check_doctor(cfg, product):
    """The doctor's ``approvals`` row: ``(ok, detail)``.

    Not ok when the matrix does not load at all — an unknown class or an unknown level is a
    config error a person must fix. Otherwise ok, with a detail naming everything that is legal
    but probably not meant: a class left to its default, a class the factory can never recognise,
    and ``merge_amendable_set`` mapped over an empty amendable set.

    Spec f-0031 §2.5 has this row name one more thing — every worker account whose
    ``hooks.account_settings_path`` lacks the approvals entry. That function is F-0031 Task 3's
    and is not on this branch (``hooks.install`` still returns early on ``0 rules declare a
    hook``, and ``_is_ours`` has no product-less form to match the account entry against), so the
    clause is left out rather than guessed at — ``cfg``, where those accounts are declared, is in
    the signature for it. It is one ``for`` loop to add here once Task 3 lands; §3 A5 does not
    cover it."""
    try:
        levels = matrix(product)
    except env.ConfigError as e:
        return False, str(e)

    notes = []
    defaulted = [c.name for c in CLASSES if levels[c.name][1] == 'default']
    if defaulted:
        notes.append(f'unmapped (taking the catalogue default): {", ".join(defaulted)}')

    try:
        known = recognisers(product)
    except env.ConfigError as e:
        return False, str(e)
    blind = [c.name for c in CLASSES
             if levels[c.name][0] in ('groom', 'human-now') and not known[c.name]]
    if blind:
        notes.append(f'held but unrecognisable — add approval_signals: {", ".join(blind)}')

    if levels['merge_amendable_set'][0] != 'auto' and not amendable.paths(product):
        notes.append('merge_amendable_set is held but conventions.amendable_paths is empty —'
                     ' no branch can ever match it')

    if not notes:
        return True, f'{len(CLASSES)} classes mapped, every held class has a recogniser'
    return True, '; '.join(notes)
