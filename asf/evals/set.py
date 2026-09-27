"""asf/evals/set.py — load, validate and hash the frozen eval set.

``evals/`` is not the test suite: it is a set of *tasks*, one JSON file per assertion, grouped
into *levers* (a matcher, a kind detector, one lever per enforced rule) by ``evals/manifest.json``.
``load()`` reads the manifest, every task under it, and ``evals/exemptions.json``, and refuses a
malformed set by naming the file and the key that is wrong — never by guessing what was meant.

The set's identity is its hash, computed on every read from the tasks' own canonical bytes and
stored in no file (:func:`set_hash`, :func:`lever_hash`): a reformat changes nothing, any change
to a value changes it, and there is nothing on disk that can go stale. python3 stdlib only.
"""
import dataclasses
import glob
import hashlib
import importlib
import inspect
import json
import os

#: The adapter names a lever may declare. The functions themselves live in
#: ``asf/evals/adapters.py`` (a later Task); this module only needs the roster to refuse an
#: unknown one at load time.
ADAPTERS = ('matcher', 'brief-kind', 'rule-check')

#: Required manifest keys per lever entry, both plain and family (``"tasks"`` is a template
#: containing ``{rule}`` for a family entry).
_LEVER_KEYS = ('id', 'adapter', 'judges', 'implements', 'tasks')


class EvalError(Exception):
    """A malformed eval set. The message names the file and the key that is wrong."""


@dataclasses.dataclass(frozen=True)
class Task:
    id: str
    lever: str
    expect: str            #: 'fire' | 'no-fire'
    why: str
    given: dict
    want: dict
    path: str              #: repo-relative, for the freeze and for a failure line


@dataclasses.dataclass(frozen=True)
class Lever:
    id: str
    adapter: str
    judges: str
    implements: tuple      #: globs, repo-relative
    tasks: tuple           #: Task, in path order
    exempt_reason: str = ''   #: from exemptions.json; '' when not exempt
    exempt_card: str = ''


@dataclasses.dataclass(frozen=True)
class Set:
    levers: tuple
    root: str
    set_hash: str          #: 12 hex


# ------------------------------------------------------------------------------------- root --

def default_root():
    """The core set's root: ``$ASF_EVALS_DIR``, else ``evals/`` beside the package's parent
    directory — the shape ``rules.core_rules_dir`` already has. ``None`` when that path is not a
    directory, which the caller reports: a product's own set is not this function's business, it
    is ``os.path.join(record_root, conventions.evals_dir)``, passed in by the caller."""
    env = os.environ.get('ASF_EVALS_DIR')
    path = env or os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), 'evals')
    return path if os.path.isdir(path) else None


# ------------------------------------------------------------------------------------- hash --

def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':')).encode('utf-8')


def lever_hash(lever):
    """sha256 over, per task in path order, the task's repo-relative path and the canonical
    bytes of its JSON, then the lever's own manifest entry canonicalised the same way. First 12
    hex characters. A lever with no tasks (exempt, or a gap not yet recorded) still hashes: only
    its manifest entry folds in."""
    h = hashlib.sha256()
    for task in sorted(lever.tasks, key=lambda t: t.path):
        h.update(task.path.encode('utf-8'))
        h.update(_canonical({
            'id': task.id, 'lever': task.lever, 'expect': task.expect,
            'why': task.why, 'given': task.given, 'want': task.want,
        }))
    h.update(_canonical({
        'id': lever.id, 'adapter': lever.adapter, 'judges': lever.judges,
        'implements': list(lever.implements),
    }))
    return h.hexdigest()[:12]


def set_hash(levers):
    """Folds every lever's hash, in lever-id order, the same way ``lever_hash`` folds a task's."""
    h = hashlib.sha256()
    for lever in sorted(levers, key=lambda l: l.id):
        h.update(lever_hash(lever).encode('utf-8'))
    return h.hexdigest()[:12]


# ------------------------------------------------------------------------------------- load --

def _read_json(path):
    if not os.path.isfile(path):
        raise EvalError(f'{path}: missing')
    with open(path, encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            raise EvalError(f'{path}: not valid JSON: {e}')


def _import_dotted(dotted):
    mod_name, _, attr = dotted.rpartition('.')
    mod = importlib.import_module(mod_name)
    return getattr(mod, attr)


def _validate_given_keys(path, adapter, judges, given):
    """A ``given`` key the judged function does not take is a loader error, not a silently
    ignored one — checked generically from the function's own signature. The rule-check adapter
    is exempted: its ``given`` builds a temporary record (``files``, ``index``, ``scripts``,
    ``timeout_s``), a fixed schema unrelated to ``run_check``'s own parameters."""
    if adapter == 'rule-check':
        return
    try:
        func = _import_dotted(judges)
    except (ImportError, AttributeError, ValueError):
        return
    valid = set(inspect.signature(func).parameters.keys())
    bad = sorted(set(given.keys()) - valid)
    if bad:
        raise EvalError(f'{path}: "given" has a key {judges} does not take: {bad}')


def _expand_lever_entry(root, manifest_path, index, entry):
    """One manifest entry to one or more ``(lever_id, adapter, judges, implements, tasks_dir)``.
    A family entry (``"family": true``) expands to one lever per subdirectory of its ``tasks``
    template's parent, the id being ``rule:<dir name>`` — dead code until a family entry ships,
    which it does not in this Task (PD5); written once so it is not written bent."""
    if not isinstance(entry, dict):
        raise EvalError(f'{manifest_path}: levers[{index}] is not an object')
    missing = [k for k in _LEVER_KEYS if not entry.get(k)]
    if missing:
        raise EvalError(f'{manifest_path}: levers[{index}] missing {missing[0]!r}')
    lever_id, adapter, judges, implements, tasks_tpl = (
        entry['id'], entry['adapter'], entry['judges'], entry['implements'], entry['tasks'])
    if adapter not in ADAPTERS:
        raise EvalError(f'{manifest_path}: levers[{index}] ({lever_id}) has unknown adapter {adapter!r}')
    if not isinstance(implements, list) or not implements:
        raise EvalError(f'{manifest_path}: levers[{index}] ({lever_id}) "implements" must be a non-empty list')
    if not entry.get('family'):
        return [(lever_id, adapter, judges, tuple(implements), os.path.join(root, tasks_tpl))]
    if '{rule}' not in tasks_tpl:
        raise EvalError(f'{manifest_path}: levers[{index}] ({lever_id}) family "tasks" must contain "{{rule}}"')
    parent = os.path.join(root, tasks_tpl.split('{rule}')[0].rstrip('/'))
    out = []
    if os.path.isdir(parent):
        for name in sorted(os.listdir(parent)):
            tasks_dir = os.path.join(parent, name)
            if os.path.isdir(tasks_dir):
                out.append((f'rule:{name}', adapter, judges, tuple(implements), tasks_dir))
    return out


def _parse_task(path, repo_rel, lever_id, obj):
    tid = obj.get('id')
    if not tid or not isinstance(tid, str):
        raise EvalError(f'{path}: missing "id"')
    lever_field = obj.get('lever')
    if lever_field != lever_id:
        raise EvalError(f'{path}: "lever" ({lever_field!r}) disagrees with its directory ({lever_id!r})')
    expect = obj.get('expect')
    if expect not in ('fire', 'no-fire'):
        raise EvalError(f'{path}: "expect" must be "fire" or "no-fire", got {expect!r}')
    why = obj.get('why')
    if not why or not isinstance(why, str):
        raise EvalError(f'{path}: missing "why"')
    given = obj.get('given')
    if not given or not isinstance(given, dict):
        raise EvalError(f'{path}: missing "given"')
    want = obj.get('want')
    if not want or not isinstance(want, dict):
        raise EvalError(f'{path}: missing "want"')
    return Task(id=tid, lever=lever_field, expect=expect, why=why, given=given, want=want, path=repo_rel)


def _load_tasks(repo_rel_parent, tasks_dir, lever_id, adapter, judges, all_task_ids):
    if not os.path.isdir(tasks_dir):
        return []
    tasks = []
    for path in sorted(glob.glob(os.path.join(tasks_dir, '*.json'))):
        obj = _read_json(path)
        repo_rel = os.path.relpath(path, repo_rel_parent)
        task = _parse_task(path, repo_rel, lever_id, obj)
        _validate_given_keys(path, adapter, judges, task.given)
        if task.id in all_task_ids:
            raise EvalError(f'{path}: duplicate task id {task.id!r}')
        all_task_ids.add(task.id)
        tasks.append(task)
    return tasks


def _has_pair(lever):
    expects = {t.expect for t in lever.tasks}
    return 'fire' in expects and 'no-fire' in expects


def _apply_exemptions(levers, exemptions, path):
    entries = exemptions.get('exemptions')
    if entries is None:
        raise EvalError(f'{path}: missing "exemptions"')
    if not isinstance(entries, list):
        raise EvalError(f'{path}: "exemptions" must be a list')
    by_id = {l.id: l for l in levers}
    granted = {}
    for i, entry in enumerate(entries):
        lever_id = entry.get('lever') if isinstance(entry, dict) else None
        if not lever_id or not isinstance(lever_id, str):
            raise EvalError(f'{path}: exemptions[{i}] missing "lever"')
        reason = entry.get('reason')
        if not reason or not isinstance(reason, str):
            raise EvalError(f'{path}: exemptions[{i}] ({lever_id}) missing "reason"')
        card = entry.get('card')
        if not card or not isinstance(card, str):
            raise EvalError(f'{path}: exemptions[{i}] ({lever_id}) missing "card"')
        lever = by_id.get(lever_id)
        if lever is not None and _has_pair(lever):
            raise EvalError(
                f'{path}: exemptions[{i}] lever {lever_id!r} already has a must-fire and a '
                'must-not-fire task')
        granted[lever_id] = (reason, card)
    result = []
    for lever in levers:
        if lever.id in granted:
            reason, card = granted[lever.id]
            lever = dataclasses.replace(lever, exempt_reason=reason, exempt_card=card)
        result.append(lever)
    return result


def _check_no_orphans(root, claimed_dirs, manifest_path, exemptions_path):
    excluded = {os.path.normpath(manifest_path), os.path.normpath(exemptions_path)}
    for path in sorted(glob.glob(os.path.join(root, '**', '*.json'), recursive=True)):
        if os.path.normpath(path) in excluded:
            continue
        if os.path.normpath(os.path.dirname(path)) not in claimed_dirs:
            raise EvalError(f'{path}: under no lever\'s directory')


def load(root=None):
    """Read the manifest, every task, and the exemptions, in that order. Raises
    :class:`EvalError`, naming the file and the key, for any of §2.2's refusals. Returns a
    :class:`Set` whose hash is computed, never stored."""
    if root is None:
        root = default_root()
    if root is None or not os.path.isdir(root):
        raise EvalError(f'no eval set on disk ({root or "$ASF_EVALS_DIR or evals/"})')

    repo_rel_parent = os.path.dirname(os.path.abspath(root))
    manifest_path = os.path.join(root, 'manifest.json')
    manifest = _read_json(manifest_path)
    entries = manifest.get('levers')
    if not isinstance(entries, list):
        raise EvalError(f'{manifest_path}: "levers" must be a list')

    expanded = []
    for i, entry in enumerate(entries):
        expanded.extend(_expand_lever_entry(root, manifest_path, i, entry))

    seen_ids = set()
    claimed_dirs = set()
    all_task_ids = set()
    levers = []
    for lever_id, adapter, judges, implements, tasks_dir in expanded:
        if lever_id in seen_ids:
            raise EvalError(f'{manifest_path}: duplicate lever id {lever_id!r}')
        seen_ids.add(lever_id)
        claimed_dirs.add(os.path.normpath(tasks_dir))
        tasks = _load_tasks(repo_rel_parent, tasks_dir, lever_id, adapter, judges, all_task_ids)
        levers.append(Lever(id=lever_id, adapter=adapter, judges=judges,
                             implements=implements, tasks=tuple(tasks)))

    exemptions_path = os.path.join(root, 'exemptions.json')
    exemptions = _read_json(exemptions_path)
    _check_no_orphans(root, claimed_dirs, manifest_path, exemptions_path)
    levers = _apply_exemptions(levers, exemptions, exemptions_path)

    return Set(levers=tuple(levers), root=root, set_hash=set_hash(levers))
