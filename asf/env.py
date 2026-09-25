"""asf.env — the operator's configuration: ``~/.ASF/config.yaml`` + ``~/.ASF/products/<name>.yaml``.

Nothing product-specific lives in this repo (see ``docs/config.example.yaml`` and
``docs/products.example.yaml`` for the documented shape). Every literal that once named a
product, a path, a vendor or an account is a lookup through this module instead.

The reader below is an 80-line YAML subset, like ``asf.record.frontmatter``'s: no PyYAML, no
third-party module — python3 stdlib only. It understands nested maps by indentation, ``- item``
block lists, inline (flow-style) ``[a, b, c]`` lists and ``{a: 1, b: [x, y]}`` maps, nested
either way, bare/quoted string scalars, ints, floats, bools and ``#`` comments. A flow value it
cannot read — unbalanced, or a map entry with no ``key:`` — raises :class:`ConfigError` naming
the key and the line; it is never loaded as a string. Anything else outside that subset is a bug
in the config file, not a feature to add here.
"""
import os
import re

from asf import conventions as conventions_mod
from asf.conventions import Conventions

ASF_HOME = os.environ.get('ASF_HOME') or os.path.expanduser('~/.ASF')


class ConfigError(Exception):
    pass


# ---- the YAML subset reader -------------------------------------------------

_INT_RE = re.compile(r'^-?\d+$')
_FLOAT_RE = re.compile(r'^-?\d+\.\d+$')
_BOOL = {'true': True, 'false': False, 'null': None, '~': None}


def _scalar(token):
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ('"', "'"):
        return token[1:-1]
    if token in _BOOL:
        return _BOOL[token]
    if _INT_RE.match(token):
        return int(token)
    if _FLOAT_RE.match(token):
        return float(token)
    return token


def _split_inline_list(token):
    inner = token.strip()[1:-1].strip()
    if not inner:
        return []
    return [_flow(p) for p in _split_top_commas(inner)]


def _balanced(token):
    """True when every ``[``/``{`` outside quotes in ``token`` is closed, in order."""
    stack, quote = [], None
    for ch in token:
        if quote:
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
        elif ch in '[{':
            stack.append(ch)
        elif ch in ']}':
            if not stack or {'[': ']', '{': '}'}[stack.pop()] != ch:
                return False
    return not stack and quote is None


def _flow(token):
    """One flow-style value: ``[…]`` a list, ``{…}`` a map (each nested either way), else a
    scalar. Raises ``ValueError`` for a map entry that is not ``key: value``."""
    token = token.strip()
    if token.startswith('[') and token.endswith(']'):
        return _split_inline_list(token)
    if token.startswith('{') and token.endswith('}'):
        out = {}
        inner = token[1:-1].strip()
        for part in (_split_top_commas(inner) if inner else []):
            m = _match_key(part) or (_match_key(part + ' ') if part.endswith(':') else None)
            if not m:
                raise ValueError(f'{part!r} is not a `key: value` entry')
            out[m[0]] = _flow(m[1]) if m[1].strip() else None
        return out
    return _scalar(token)


def _value(rest, key, lineno):
    """The value after ``key:`` on line ``lineno``: a flow list or map parsed, a scalar read —
    and a flow value that is unbalanced or malformed refused with the key and the line, never
    loaded as a string (a guard reading ``quota_guards: {…}`` as text crashed on it)."""
    if rest[:1] not in ('[', '{'):
        return _scalar(rest)
    if not _balanced(rest):
        raise ConfigError(f"line {lineno}: {key}: an unbalanced flow-style value {rest!r} — "
                          f"close it, or write it as a block")
    if _closes_at(rest) != len(rest) - 1:
        return _scalar(rest)  # `{reviews_dir}/{n}-{slug}.md`: a template, not one flow value
    try:
        return _flow(rest)
    except ValueError as e:
        raise ConfigError(f"line {lineno}: {key}: a flow-style map it cannot read ({e}) — "
                          f"write it as a block") from None


def _closes_at(token):
    """The index of the bracket that closes ``token``'s opening one (quotes respected)."""
    depth, quote = 0, None
    for i, ch in enumerate(token):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
        elif ch in '[{':
            depth += 1
        elif ch in ']}':
            depth -= 1
            if depth == 0:
                return i
    return -1


def _split_top_commas(s):
    parts, depth, cur, quote = [], 0, [], None
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
            cur.append(ch)
        elif ch in '[{':
            depth += 1
            cur.append(ch)
        elif ch in ']}':
            depth -= 1
            cur.append(ch)
        elif ch == ',' and depth == 0:
            parts.append(''.join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append(''.join(cur))
    return [p.strip() for p in parts]


def _strip_comment(line):
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
        elif ch == '#':
            return line[:i]
    return line


KEY_RE = re.compile(r'^(?:([A-Za-z_][A-Za-z0-9_.-]*)|"([^"]*)"|\'([^\']*)\'):\s*(.*)$')


def _match_key(content):
    """(key, rest) off a ``key: value`` line — bare (``[A-Za-z_][\\w.-]*``) or quoted (so a key
    like ``"*e2e*"`` works too, e.g. a glob pattern used as a budget-table key)."""
    m = KEY_RE.match(content)
    if not m:
        return None
    key = m.group(1) or m.group(2) or m.group(3)
    return key, m.group(4)


def loads(text):
    """Parse the YAML subset into nested dict/list/scalar data."""
    lines = []
    for n, raw in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw).rstrip()
        if line.strip() == '':
            continue
        indent = len(line) - len(line.lstrip(' '))
        lines.append((indent, line.strip(), n))
    root = {}
    _parse_block(lines, 0, len(lines), 0, root)
    return root


def _parse_block(lines, start, end, indent, target):
    i = start
    while i < end:
        cur_indent, content, lineno = lines[i]
        if cur_indent != indent:
            raise ConfigError(f"unexpected indent: {content!r}")
        if content.startswith('- '):
            raise ConfigError(f"top-level list item without a key: {content!r}")
        m = _match_key(content)
        if not m:
            raise ConfigError(f"not a key: line {content!r}")
        key, rest = m
        j = i + 1
        block_end = j
        while block_end < end and lines[block_end][0] > cur_indent:
            block_end += 1
        if rest == '':
            if block_end > j and lines[j][1].startswith('- '):
                target[key] = _parse_list(lines, j, block_end, lines[j][0])
            elif block_end > j:
                sub = {}
                _parse_block(lines, j, block_end, lines[j][0], sub)
                target[key] = sub
            else:
                target[key] = None
        else:
            target[key] = _value(rest, key, lineno)
        i = block_end
    return i


def _parse_list(lines, start, end, indent):
    items = []
    i = start
    while i < end:
        cur_indent, content, lineno = lines[i]
        if cur_indent != indent or not content.startswith('- '):
            raise ConfigError(f"expected list item: {content!r}")
        rest = content[2:].strip()
        j = i + 1
        block_end = j
        while block_end < end and lines[block_end][0] > indent:
            block_end += 1
        m = _match_key(rest) if rest and rest[:1] not in ('[', '{') else None
        if m and (m[1] != '' or block_end > j):
            sub = {}
            if m[1] != '':
                sub[m[0]] = _value(m[1], m[0], lineno)
            if block_end > j:
                _parse_block(lines, j, block_end, lines[j][0], sub)
            items.append(sub)
        else:
            items.append(_value(rest, '-', lineno))
        i = block_end
    return items


def load_file(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding='utf-8') as f:
        return loads(f.read())


# ---- the operator config ----------------------------------------------------

def config_path():
    return os.path.join(ASF_HOME, 'config.yaml')


def product_path(name):
    return os.path.join(ASF_HOME, 'products', f'{name}.yaml')


def load_config():
    """``~/.ASF/config.yaml``: scheduler, worker pool, quota guards, defaults, paths.

    The worker pool's environment keys (:func:`validate_worker_pool`) are checked on load: a
    malformed one raises :class:`ConfigError` naming it, rather than a worker session starting
    with an environment nobody asked for."""
    cfg = load_file(config_path())
    problems = validate_worker_pool(cfg)
    if problems:
        raise ConfigError(f"{config_path()}: {'; '.join(f'{k} {why}' for k, why in problems)}")
    return cfg


# ---- the worker pool's environment keys --------------------------------------

#: ``worker_pool.accounts[].isolate_home`` when the account leaves it out: a worker session gets
#: a HOME of its own, never the operator's. ``isolate_home: false`` keeps the operator's HOME
#: (every CLI login on the machine). A key of its own, not a value of ``home:``, so a v0.1.2
#: binary — which reads ``home:`` as a path and ignores unknown account keys — rolls back
#: cleanly.
DEFAULT_ISOLATE_HOME = True
_VAR_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def validate_worker_pool(cfg):
    """``worker_pool.env_passthrough`` (a list of environment variable names) and each
    account's ``home`` (a path), ``isolate_home`` (true | false), ``home_seed`` (a list of
    paths) and ``auth_env`` (``{VARIABLE: file}``) checked: ``[(dotted key, problem)]``, empty when well-formed or absent."""
    pool = (cfg or {}).get('worker_pool')
    if not isinstance(pool, dict):
        return []
    problems = []
    passthrough = pool.get('env_passthrough')
    if passthrough is not None:
        if not isinstance(passthrough, list):
            problems.append(('worker_pool.env_passthrough',
                             f'must be a list of variable names, not {passthrough!r}'))
        else:
            for name in passthrough:
                if not isinstance(name, str) or not _VAR_RE.match(name):
                    problems.append(('worker_pool.env_passthrough',
                                     f'must be a list of variable names, and {name!r} is not one'))
    for i, acct in enumerate(pool.get('accounts') or []):
        if not isinstance(acct, dict):
            continue
        label = f"worker_pool.accounts[{acct.get('name') or i}]"
        home = acct.get('home')
        if home is not None and (not isinstance(home, str) or not home.strip()):
            problems.append((label + '.home', f'must be a path, not {home!r}'))
        isolate = acct.get('isolate_home')
        if isolate is not None and not isinstance(isolate, bool):
            problems.append((label + '.isolate_home', f'must be true or false, not {isolate!r}'))
        seed = acct.get('home_seed')
        if seed is not None:
            if not isinstance(seed, list):
                problems.append((label + '.home_seed', f'must be a list of paths, not {seed!r}'))
            else:
                for path in seed:
                    if not isinstance(path, str) or not path.strip():
                        problems.append((label + '.home_seed',
                                         f'must be a list of paths, and {path!r} is not one'))
            if isolate is False and home is None:
                problems.append((label + '.home_seed',
                                 'has no home to seed: isolate_home is false and home is unset'))
        auth = acct.get('auth_env')
        if auth is not None:
            if not isinstance(auth, dict):
                problems.append((label + '.auth_env',
                                 f'must map variable names to files, not {auth!r}'))
            else:
                for name, path in auth.items():
                    if not isinstance(name, str) or not _VAR_RE.match(name):
                        problems.append((label + '.auth_env',
                                         f'must map variable names to files, and {name!r} is not one'))
                    elif not isinstance(path, str) or not path.strip():
                        problems.append((label + '.auth_env',
                                         f'{name} must name a file, not {path!r}'))
    return problems


def env_passthrough(cfg):
    """``worker_pool.env_passthrough``: the variable names a worker session keeps from the
    tick's environment beside the fixed allow-list. ``()`` when unset."""
    pool = (cfg or {}).get('worker_pool') or {}
    return tuple(pool.get('env_passthrough') or ())


#: Set in every local worker session unless the operator or the product says otherwise: a
#: session shares the host with the others, so a test runner that sizes its pool to the core
#: count saturates the machine and holds every product's wave on host pressure (2026-09-25:
#: load 71 on 10 cores from one vitest run). CI never sees these; they are worker-only.
DEFAULT_WORKER_ENV = {'VITEST_MAX_WORKERS': '2'}


def worker_env(cfg, product=None):
    """The fixed variables a worker session gets: :data:`DEFAULT_WORKER_ENV`, then
    ``worker_pool.env`` from the operator config, then the product's ``conventions.worker_env``
    (the later wins; a value of None or "" removes the variable). Values are strings."""
    out = dict(DEFAULT_WORKER_ENV)
    layers = [((cfg or {}).get('worker_pool') or {}).get('env') or {}]
    conv = getattr(product, 'conventions', None) or {}
    layers.append(conv.get('worker_env') or {})
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        for k, v in layer.items():
            if v is None or v == '':
                out.pop(str(k), None)
            else:
                out[str(k)] = str(v)
    return out


def account_home(acct):
    """An account's (a ``worker_pool.accounts`` entry's) ``home``: the expanded path, or None
    when unset — the per-account home under the state directory when :func:`isolate_home`,
    else the operator's own."""
    home = (acct or {}).get('home')
    return os.path.expanduser(str(home)) if home is not None else None


def isolate_home(acct):
    """An account's ``isolate_home``, :data:`DEFAULT_ISOLATE_HOME` when unset."""
    value = (acct or {}).get('isolate_home')
    return DEFAULT_ISOLATE_HOME if value is None else bool(value)


def account_home_seed(acct):
    """An account's ``home_seed``: the expanded paths copied into its home. ``[]`` when unset."""
    return [os.path.expanduser(str(p)) for p in (acct or {}).get('home_seed') or ()]


def account_auth_env(acct):
    """An account's ``auth_env``: ``{VARIABLE: expanded file path}`` — each file's content
    (stripped) is that variable in the account's sessions, and nowhere else. ``{}`` when unset.
    The mapping names files, never values: a secret never sits in config.yaml."""
    auth = (acct or {}).get('auth_env') or {}
    return {str(k): os.path.expanduser(str(v)) for k, v in auth.items()} if isinstance(auth, dict) else {}


def product_auth_env(product):
    """A product's ``conventions.auth_env``: ``{VARIABLE: expanded file path}`` — GitHub access
    is per product (a product can live under a GitHub owner none of its account's other products
    share), so a spawn merges this over the account's own ``auth_env``, the product's file
    winning for a variable both name (:func:`asf.workers.runtime.auth_env_values`). ``{}`` when
    the product declares none, or ``product`` is None. It lives under ``conventions:`` (not a
    top-level key) so a v0.1.2 reader — which keeps unknown ``conventions`` keys but rejects an
    unknown top-level one — tolerates a product file that sets it (R23)."""
    if product is None:
        return {}
    auth = product.conventions.get('auth_env') if hasattr(product, 'conventions') else None
    return {str(k): os.path.expanduser(str(v)) for k, v in auth.items()} if isinstance(auth, dict) else {}


def default_product_name():
    """``$ASF_PRODUCT`` first, else ``config.yaml``'s ``default_product``."""
    if os.environ.get('ASF_PRODUCT'):
        return os.environ['ASF_PRODUCT']
    cfg = load_config()
    name = cfg.get('default_product')
    if not name:
        raise ConfigError(
            'no product given (--product), no $ASF_PRODUCT, and config.yaml has no default_product'
        )
    return name


# ---- the product file's declared fields ------------------------------------

_MAP, _LIST, _STR = 'a map', 'a list', 'a scalar'
# key -> the shape its value must have; a None value (`key:   # TODO`) is "not filled in yet",
# which `asf doctor` reports separately, so it is not a schema error here.
PRODUCT_FIELDS = {
    'product': _STR, 'repo_slug': _STR, 'repo_dir': _STR, 'main': _STR, 'backlog_dir': _STR,
    'app_host': _STR, 'conventions': _MAP, 'ci': None, 'deploy_sha': None,
    'customer_paths': _LIST, 'stage_limits': _MAP, 'size_classes': _MAP, 'approvals': _MAP,
    'approval_signals': _MAP, 'steps': _MAP, 'job_grants': _LIST, 'groom': _MAP,
    'capacity': _MAP, 'clocks': _MAP, 'token_caps': _MAP, 'feeder': _MAP, 'improve': _MAP,
    'release': _MAP,
}
# `ci:` is a map (or the bare word `none`, a product without CI); these are its keys.
# `deploy_workflow` is a read-only alias of the documented `deploy_sha.workflow`: the status
# Prod row once named it, so a file that followed that hint loads (and is read) rather than
# refusing every product load.
CI_FIELDS = {
    'provider': _STR, 'workflow': _STR, 'test_command': _STR, 'budgets': _MAP,
    'runner_org': _STR, 'labels': _LIST, 'dev_job': _STR, 'deploy_workflow': _STR,
    'pool': _LIST,
}
# `capacity:` is a map: this product's session/CI ceilings and its batch shape.
CAPACITY_FIELDS = {'sessions': _STR, 'ci': _STR, 'weight': _STR, 'batch': _MAP}
# `feeder:` is a map: ``hold`` lists the work classes the feeder starts no session for.
FEEDER_FIELDS = {'hold': _LIST}
#: what ``feeder.hold`` may name (:attr:`Product.feeder_hold`)
FEEDER_HOLDS = ('features', 'bugs')
# `improve:` is a map: the improve pass's threshold overrides, its Epic, its window and its premium models.
IMPROVE_FIELDS = {'thresholds': _MAP, 'epic': _STR, 'window_days': None, 'premium_models': _LIST,
                  # the value loop's settings (asf.scorecard.loop): thresholds, window_days,
                  # verify_weeks, min_move, file_to, epic
                  'scorecard': _MAP}
# `release:` is a map: the release-readiness gate's thresholds (asf.release.DEFAULTS).
RELEASE_FIELDS = {'window_days': None, 'max_hand_fixes': None, 'max_repair_per_feature': None,
                  'ci_runs': None, 'min_upgrades': None, 'hand_types': _LIST,
                  'readme_sections': _LIST, 'ci_steps': _MAP, 'requires': _MAP, 'blocking': _LIST}
# every product-file section whose own keys are checked, keyed by its own field table.
NESTED_FIELDS = {
    'ci': CI_FIELDS, 'capacity': CAPACITY_FIELDS, 'feeder': FEEDER_FIELDS, 'improve': IMPROVE_FIELDS,
    'release': RELEASE_FIELDS,
}


def _shape_ok(value, shape):
    if value is None or shape is None:
        return True
    if shape == _MAP:
        return isinstance(value, dict)
    if shape == _LIST:
        return isinstance(value, list)
    return not isinstance(value, (dict, list))


#: ``deploy_sha.<env>.mode`` — what the deploy pass (asf.harvest.deploy) does for each
#: environment; ``deploy_sha.prod.from`` — where prod's candidate sha comes from. The rest of
#: ``deploy_sha`` (its discovery rules: ``source``, ``rule``, ``branch``, other environments)
#: is not shape-checked.
DEPLOY_MODES = {'dev': ('auto', 'manual', 'ci'), 'prod': ('auto', 'manual')}
DEPLOY_SOURCES = ('ci', 'dev')
#: the modes a named target (``deploy_sha.targets.<name>.mode``) may take
DEPLOY_TARGET_MODES = ('auto', 'manual', 'ci')


def _deploy_problems(deploy):
    """[(dotted key, problem)] for a ``deploy_sha`` whose modes the deploy pass cannot read. A
    typo here would silently mean ``manual`` (or an unmanaged dev), so it refuses the load."""
    if not isinstance(deploy, dict):
        return []
    out = []
    auto = deploy.get('auto')
    if auto is not None and not isinstance(auto, bool):
        out.append(('deploy_sha.auto', f'must be true or false, not {auto!r}'))
    for env, modes in DEPLOY_MODES.items():
        block = deploy.get(env)
        if block is None:
            continue
        if not isinstance(block, dict):
            out.append((f'deploy_sha.{env}', f'must be {_MAP}, not {block!r}'))
            continue
        if block.get('mode') is not None and block['mode'] not in modes:
            out.append((f'deploy_sha.{env}.mode',
                        f"must be one of {' | '.join(modes)}, not {block['mode']!r}"))
    src = (deploy.get('prod') or {}).get('from') if isinstance(deploy.get('prod'), dict) else None
    if src is not None and src not in DEPLOY_SOURCES:
        out.append(('deploy_sha.prod.from',
                    f"must be one of {' | '.join(DEPLOY_SOURCES)}, not {src!r}"))
    return out + _target_problems(deploy.get('targets'))


def _target_problems(targets):
    """[(dotted key, problem)] for ``deploy_sha.targets`` — the named deploy targets beside dev
    and prod (asf.harvest.deploy)."""
    if targets is None:
        return []
    if not isinstance(targets, dict):
        return [('deploy_sha.targets', f'must be {_MAP}, not {targets!r}')]
    out = []
    for name, block in targets.items():
        k = f'deploy_sha.targets.{name}'
        if name in DEPLOY_MODES:
            out.append((k, f'{name} is not a target name — write deploy_sha.{name}'))
            continue
        if not isinstance(block, dict):
            out.append((k, f'must be {_MAP}, not {block!r}'))
            continue
        if block.get('mode') is not None and block['mode'] not in DEPLOY_TARGET_MODES:
            out.append((k + '.mode', f"must be one of {' | '.join(DEPLOY_TARGET_MODES)},"
                                     f" not {block['mode']!r}"))
        if block.get('from') is not None and block['from'] not in DEPLOY_SOURCES:
            out.append((k + '.from', f"must be one of {' | '.join(DEPLOY_SOURCES)},"
                                     f" not {block['from']!r}"))
        globs = block.get('paths')
        if globs is not None and not (isinstance(globs, list)
                                      and all(isinstance(g, str) and g for g in globs)):
            out.append((k + '.paths', f'must be {_LIST} of path globs, not {globs!r}'))
    return out


def validate_product_text(text):
    """The product file checked against the declared field list: a sorted list of
    ``(line, key, problem)``, empty when the file is well-formed. ``key`` is dotted for a
    nested one (``ci.runner_labels``); ``line`` is 1-based in ``text``."""
    try:
        data = loads(text)
    except ConfigError as e:
        return [(0, '', str(e))]
    lines = {}
    section = None
    for n, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw).rstrip()
        m = _match_key(line.strip()) if line.strip() else None
        if not m:
            continue
        if len(line) == len(line.lstrip(' ')):
            section = m[0]
            lines.setdefault(section, n)
        elif section in NESTED_FIELDS or section == 'conventions':
            lines.setdefault(section + '.' + m[0], n)
    problems = []

    def check(fields, mapping, prefix):
        for key, value in mapping.items():
            dotted = prefix + key
            if key not in fields:
                problems.append((lines.get(dotted, 0), dotted, 'is not a field of the product file'))
            elif not _shape_ok(value, fields[key]):
                problems.append((lines.get(dotted, 0), dotted, f'must be {fields[key]}, not {value!r}'))

    check(PRODUCT_FIELDS, data, '')
    for section, fields in NESTED_FIELDS.items():
        if isinstance(data.get(section), dict):
            check(fields, data[section], section + '.')
    for dotted, why in _deploy_problems(data.get('deploy_sha')):
        problems.append((lines.get('deploy_sha', 0), dotted, why))
    from asf import ci_pool  # `ci.pool`: every runner's fields, its role a capability (asf.ci_pool)
    for dotted, why in ci_pool.pool_problems(data.get('ci')):
        problems.append((lines.get('ci.pool', lines.get('ci', 0)), dotted, why))
    # `conventions:` keeps unknown keys (asf.conventions), but the shaped ones are checked
    for key, why in conventions_mod.validate_mapping(data.get('conventions')):
        dotted = 'conventions.' + key
        problems.append((lines.get('conventions.' + key.split('.')[0], 0), dotted, why))
    return sorted(problems)


def key_line(path, dotted):
    """The 1-based line of ``dotted`` (``conventions.models``) in the yaml file at ``path``, or 0
    when the file or the key is not there."""
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except OSError:
        return 0
    want = dotted.split('.')
    stack = []  # [(indent, key)]
    for n, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw).rstrip()
        m = _match_key(line.strip()) if line.strip() and not line.strip().startswith('- ') else None
        if not m:
            continue
        indent = len(line) - len(line.lstrip(' '))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, m[0]))
        if [k for _i, k in stack] == want:
            return n
    return 0


def format_problems(problems):
    return '; '.join(
        f"line {ln}: '{key}' {why}" if ln else f"'{key}' {why}" if key else why
        for ln, key, why in problems)


class Product:
    """One product's convention set — the only place a path, repo or vendor name lives.

    Construct via :func:`load_product`; tests build one directly from a plain dict, e.g.
    ``Product('sample', {'repo_dir': tmp, 'repo_slug': 'x/y', 'main': 'main'})``, so every
    ported function that used to read a module-level constant instead takes a ``Product`` (or
    None, in which case it calls :func:`load_product` itself).
    """

    def __init__(self, name, data):
        self.name = name
        self._data = data or {}
        self._conventions = None

    def _get(self, key, default=None):
        return self._data.get(key, default)

    @property
    def repo_slug(self):
        return self._get('repo_slug')

    @property
    def repo_dir(self):
        v = self._get('repo_dir')
        return os.path.expanduser(v) if v else None

    @property
    def main(self):
        return self._get('main', 'main')

    @property
    def feeder_hold(self):
        """``feeder.hold``: the work classes (:data:`FEEDER_HOLDS`) whose new sessions the
        feeder holds — their rows wait on ``hold: <class>``. Empty unless set."""
        feeder = self._get('feeder')
        hold = feeder.get('hold') if isinstance(feeder, dict) else None
        if isinstance(hold, str):
            hold = [hold]
        return frozenset(str(h).strip().lower() for h in hold or ()
                         if str(h).strip().lower() in FEEDER_HOLDS)

    @property
    def backlog_dir(self):
        v = self._get('backlog_dir')
        return os.path.expanduser(v) if v else None

    @property
    def conventions(self):
        """The product's :class:`asf.conventions.Conventions`, defaults filled in.

        Built from the yaml's ``conventions:`` block, plus six values that live at the top
        level of a product file because more than the conventions read them: ``main``,
        ``stage_limits``, ``ci.test_command`` (the gate harvest runs), ``ci.workflow`` and
        ``ci.dev_job`` (``ci_workflow``/``ci_dev_job``), and ``deploy_sha.workflow``
        (``deploy_workflow``; ``ci.deploy_workflow`` is its read-only alias, read last). A
        ``conventions:`` key of the same name wins. Still answers
        ``.get()``/``[]``, so the callers that read it as a mapping — and an operator's extra
        keys — keep working."""
        if self._conventions is None:
            data = dict(self._get('conventions') or {})
            data.setdefault('main', self._get('main') or 'main')
            if self._get('stage_limits') is not None:
                data.setdefault('stage_limits', self._get('stage_limits'))
            ci = self._get('ci')
            # `ci: none` (a product without CI) is a string, not a mapping
            test_command = ci.get('test_command') if isinstance(ci, dict) else None
            if test_command:
                data.setdefault('test_command', test_command)
            if isinstance(ci, dict):
                for src, dst in (('workflow', 'ci_workflow'), ('dev_job', 'ci_dev_job')):
                    if ci.get(src):
                        data.setdefault(dst, ci[src])
            deploy = self._get('deploy_sha')
            if isinstance(deploy, dict) and deploy.get('workflow'):
                data.setdefault('deploy_workflow', deploy['workflow'])
            prod_env = deploy.get('prod') if isinstance(deploy, dict) else None
            if isinstance(prod_env, dict) and prod_env.get('workflow'):
                data.setdefault('deploy_workflow', prod_env['workflow'])
            if isinstance(ci, dict) and ci.get('deploy_workflow'):  # the read-only alias
                data.setdefault('deploy_workflow', ci['deploy_workflow'])
            self._conventions = Conventions.from_mapping(data)
        return self._conventions

    @property
    def ci(self):
        return self._get('ci', {})

    @property
    def deploy_sha(self):
        return self._get('deploy_sha', {})

    @property
    def app_host(self):
        return self._get('app_host')

    @property
    def customer_paths(self):
        return self._get('customer_paths', [])

    @property
    def stage_limits(self):
        return self._get('stage_limits', {})

    @property
    def size_classes(self):
        return self._get('size_classes', {})

    @property
    def approvals(self):
        return self._get('approvals', {})

    @property
    def approval_signals(self):
        """The ``approval_signals:`` map (F-0031): product-specific recognisers ``asf.approvals
        .signals`` adds to a class's built-ins. ``{}`` when the product declares none."""
        return self._get('approval_signals', {})

    @property
    def groom(self):
        """The ``groom:`` block (F-0085/D-0049): the four policies' thresholds. ``{}`` when the
        product sets none — every reader of it (:mod:`asf.groom.policy`) then falls back to the
        documented default for the key it wants."""
        return self._get('groom', {})

    @property
    def improve(self):
        """The ``improve:`` block: threshold overrides, ``epic``, ``window_days``,
        ``premium_models`` (:mod:`asf.improve.classes` fills the defaults). ``{}`` when unset."""
        return self._get('improve', {})

    @property
    def release(self):
        """The ``release:`` block: the release-readiness gate's thresholds and its ``blocking``
        Features (:mod:`asf.release` fills the defaults). ``{}`` when unset."""
        return self._get('release', {})

    def branch_prefix(self, kind):
        """The prefix *without* its separator (``worker``), for the callers that compose
        ``<prefix>/<job>`` themselves. :meth:`Conventions.branch` builds the whole name."""
        return self.conventions.prefix(kind).rstrip('/')


def load_product(name=None):
    name = name or default_product_name()
    path = product_path(name)
    data = load_file(path)
    if not data:
        raise ConfigError(f'no product config at {path}')
    with open(path, encoding='utf-8') as f:
        problems = validate_product_text(f.read())
    if problems:
        raise ConfigError(f'{path}: {format_problems(problems)}')
    return Product(name, data)


def state_dir(product=None):
    name = product.name if isinstance(product, Product) else (product or default_product_name())
    path = os.path.join(ASF_HOME, 'state', name)
    os.makedirs(path, exist_ok=True)
    return path


def log_dir():
    path = os.path.join(ASF_HOME, 'logs')
    os.makedirs(path, exist_ok=True)
    return path


def add_product_arg(parser, default=None):
    """The ``--product`` flag every ``asf`` subcommand takes."""
    parser.add_argument('--product', default=default, help='product name (see ~/.ASF/products/)')
