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
the operator's own config (:func:`operator_config_target`, D8), and fails closed (D7).

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

from asf import env, hooks

LEVELS = ('auto', 'groom', 'human-now')

RESOLUTIONS = ('granted', 'done', 'dropped')


@dataclasses.dataclass(frozen=True)
class ActionClass:
    name: str
    covers: str          # one line, printed by `asf approvals`
    default: str          # one of LEVELS
    read_by: tuple        # ('hook',) | ('harvest',) | ('hook', 'file_bugs') ...


CLASSES = (
    ActionClass(
        'spend_money', 'buying, subscribing, raising a paid tier or a spend limit',
        'human-now', ('hook',)),
    ActionClass(
        'touch_production', 'deploying, pushing to the trunk',
        'human-now', ('hook',)),
    ActionClass(
        'touch_security',
        "secrets, credentials, the runtime's hook and permission settings, the repo's git hooks",
        'human-now', ('hook',)),
    ActionClass(
        'touch_customer_data', 'reading or changing customer records, exports, production databases',
        'human-now', ('hook',)),
    ActionClass(
        'touch_legal', 'licences, notices, terms, privacy texts',
        'human-now', ('hook',)),
    ActionClass(
        'new_epic', 'opening a new Epic',
        'human-now', ('hook',)),
    ActionClass(
        'merge_amendable_set', "landing a branch that touches the factory's own rules",
        'human-now', ('harvest',)),
    ActionClass(
        'merge_routine_pr', 'landing any other finished branch',
        'auto', ('harvest',)),
    ActionClass(
        'file_bug', 'filing or bumping a Bug',
        'auto', ('hook', 'file_bugs')),
)

CLASSES_BY_NAME = {c.name: c for c in CLASSES}

#: Built-in path recognisers (§2.1): a glob with ``/`` matches the whole repo-relative path, a
#: glob without one matches the file name (see :func:`_match_glob`). `touch_security` also
#: carries `hooks.RUNTIME_SETTINGS_GLOBS` — the runtime adapter names its own path.
_PATH_GLOBS = {
    'touch_security': (
        '.env', '.env.*', '*.pem', '*.key', 'id_rsa*', 'id_ed25519*', '.githooks/*',
    ),
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


def _pushes_trunk(command, main):
    if not re.search(r'\bgit\s+push\b', command):
        return False
    escaped = re.escape(main)
    forms = rf'(?:^|\s)(?:HEAD:{escaped}|refs/heads/{escaped}|[^\s:]+:{escaped}|{escaped})(?=\s|$)'
    return bool(re.search(forms, command))


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
    (repo-relative, as :func:`asf.harvest.harvest.touched_files` returns them) matches
    ``conventions.amendable_paths`` (the §2.1 glob rule, :func:`_match_glob`), else
    ``merge_routine_pr`` with no matched file."""
    globs = list(product.conventions.amendable_paths or [])
    for f in files:
        if any(_match_glob(g, f) for g in globs):
            return 'merge_amendable_set', f
    return 'merge_routine_pr', None


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


def refuse(product, item, cls, level, job, tool, detail):
    """Appends a ``refused`` line (§4) and returns its hold id, ``<item>/<class>``."""
    hold = f'{item}/{cls}'
    append(product, {
        'event': 'refused', 'hold': hold, 'item': item, 'class': cls, 'level': level,
        'job': job, 'tool': tool, 'detail': detail, 'ts': _now_iso(),
    })
    return hold


def resolve(product, hold, resolution):
    if resolution not in RESOLUTIONS:
        raise ValueError(f'resolution must be one of {RESOLUTIONS}, not {resolution!r}')
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
_WRITING_TOKENS = (
    r'>', r'\btee\b', r'\bsed\s+-i\b', r'\bperl\s+-i\b', r'\bcp\b', r'\bmv\b', r'\brm\b',
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


def _refusal_lines(item, cls, level, detail):
    """The four lines of §2.3 step 7. ``products/<p>.yaml`` is literal: the session is told where
    authority lives, not which file to go and edit (D8)."""
    return [
        f'REFUSED {cls} ({level}) on {item} — {detail}',
        '  This action needs a person (approvals: in products/<p>.yaml). Do not retry it or work'
        ' around it.',
        f'  Print: NEEDS OPERATOR: {item} {cls} — asf approvals resolve {item}/{cls}'
        ' granted|done|dropped',
        '  and carry on with every part of the job that does not depend on it.',
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
    matched = classify(prod, tool_name, tool_input, cwd)
    if not matched:
        return 0

    invalid = None
    try:
        levels = {name: level for name, (level, _) in matrix(prod).items()}
    except env.ConfigError as e:
        invalid, levels = str(e), None               # every matched class is human-now (D7)

    item = item_of_job(prod, job)
    refused = []
    for cls, detail in matched:                      # catalogue order
        level = 'human-now' if levels is None else levels[cls]
        if level == 'auto':
            continue
        if is_granted(prod, f'{item}/{cls}'):
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

def raise_holds(ctx, out):
    """The ``wave`` step's first act: say what is held, record each hold once, and answer with
    ``{item: (class, level)}`` — the items a launching row must not be started on.

    One ``NEEDS OPERATOR`` line per open ``human-now`` hold, oldest first, and one summary line
    however many ``groom`` holds are open: a person reads the first, the groom session the
    second. The ``announced`` and ``closed`` markers live in the ledger beside the refusals
    (§4), so the ``held`` and ``hold-resolved`` events are written once over a hold's life
    however many ticks see it — the fold's ``first`` never moves, so one marker is one
    announcement, and a repeat refusal bumps ``count`` without raising a second event.
    """
    product = ctx.product
    open_ = open_holds(product)                      # oldest first

    for e in open_:
        if e['level'] == 'human-now':
            out(f"NEEDS OPERATOR: held {e['class']} on {e['item']} — {e['detail']} —"
                f" asf approvals resolve {e['item']}/{e['class']} granted|done|dropped")
    groom = sum(1 for e in open_ if e['level'] == 'groom')
    if groom:
        out(f'approvals: {groom} held for groom — asf approvals list')

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

    order = {c.name: i for i, c in enumerate(CLASSES)}
    held = {}
    for e in open_:                                  # first in catalogue order wins the item
        rank = order.get(e['class'], len(order))
        if e['item'] not in held or rank < held[e['item']][0]:
            held[e['item']] = (rank, e['class'], e['level'])
    return {item: (cls, level) for item, (_, cls, level) in held.items()}


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
    'merge_amendable_set': ("the branch's changed files against conventions.amendable_paths",),
    'merge_routine_pr': ('every branch that is not merge_amendable_set',),
    'file_bug': ("the tick's bug filer",),
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
    """The nine catalogue rows: ``class | level | from | read by | recognisers`` (§2.5)."""
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
        resolve(name, hold, resolution)
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

    if levels['merge_amendable_set'][0] != 'auto' and not product.conventions.amendable_paths:
        notes.append('merge_amendable_set is held but conventions.amendable_paths is empty —'
                     ' no branch can ever match it')

    if not notes:
        return True, f'{len(CLASSES)} classes mapped, every held class has a recogniser'
    return True, '; '.join(notes)
