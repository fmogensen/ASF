"""asf.approvals — the approval matrix as code (F-0031).

The catalogue (:data:`CLASSES`) names every action class the factory recognises, its default
level and where it is read. The matrix (:func:`matrix`) is that catalogue crossed with a
product's ``approvals:`` yaml, an operator's one file for widening or narrowing standing
authority. :func:`classify` turns one Claude Code tool call into the classes it matches;
:func:`signals` reads a product's own extra recognisers. The ledger (:func:`refuse`,
:func:`resolve`, :func:`holds`, :func:`open_holds`, :func:`is_granted`) is the append-only record
of every hold and its resolution, in ``~/.ASF/state/<product>/approvals.jsonl`` (D14).

Nothing here runs a hook or reads a session's job yet — that is ``asf hook approvals``
(:mod:`asf.hooks`), built on top of this module in a later Task of the same plan.
"""
import dataclasses
import datetime
import fnmatch
import json
import os
import re

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
        r'\bgh\s+secret\b', r'\bgh\s+auth\b', r'\bgit\s+config\s+core\.hooksPath\b', r'--no-verify\b',
    ),
    'new_epic': (r'\basf\s+new\s+epic\b',),
    'file_bug': (r'\basf\s+new\s+bug\b',),
}


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# ---- the matrix and the signals ---------------------------------------------

def matrix(product):
    """``{class: (level, 'yaml'|'default')}`` for every class in :data:`CLASSES`.

    Raises :class:`env.ConfigError` naming every key of ``product.approvals`` that is not a
    catalogue class, and every mapped level that is not in :data:`LEVELS`."""
    raw = dict(product.approvals or {})
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
