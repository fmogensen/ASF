"""asf.env — the operator's configuration: ``~/.ASF/config.yaml`` + ``~/.ASF/products/<name>.yaml``.

Nothing product-specific lives in this repo (see ``docs/config.example.yaml`` and
``docs/products.example.yaml`` for the documented shape). Every literal that once named a
product, a path, a vendor or an account is a lookup through this module instead.

The reader below is an 80-line YAML subset, like ``asf.record.frontmatter``'s: no PyYAML, no
third-party module — python3 stdlib only. It understands nested maps by indentation, ``- item``
block lists, inline ``[a, b, c]`` lists, bare/quoted string scalars, ints, floats, bools and
``#`` comments. Anything outside that subset is a bug in the config file, not a feature to add
here.
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
    return [_scalar(p) for p in _split_top_commas(inner)]


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
    for raw in text.splitlines():
        line = _strip_comment(raw).rstrip()
        if line.strip() == '':
            continue
        indent = len(line) - len(line.lstrip(' '))
        lines.append((indent, line.strip()))
    root = {}
    _parse_block(lines, 0, len(lines), 0, root)
    return root


def _parse_block(lines, start, end, indent, target):
    i = start
    while i < end:
        cur_indent, content = lines[i]
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
        elif rest.startswith('[') and rest.endswith(']'):
            target[key] = _split_inline_list(rest)
        else:
            target[key] = _scalar(rest)
        i = block_end
    return i


def _parse_list(lines, start, end, indent):
    items = []
    i = start
    while i < end:
        cur_indent, content = lines[i]
        if cur_indent != indent or not content.startswith('- '):
            raise ConfigError(f"expected list item: {content!r}")
        rest = content[2:].strip()
        j = i + 1
        block_end = j
        while block_end < end and lines[block_end][0] > indent:
            block_end += 1
        m = _match_key(rest) if rest else None
        if m and (m[1] != '' or block_end > j):
            sub = {}
            if m[1] != '':
                sub[m[0]] = _scalar(m[1]) if not (
                    m[1].startswith('[') and m[1].endswith(']')
                ) else _split_inline_list(m[1])
            if block_end > j:
                _parse_block(lines, j, block_end, lines[j][0], sub)
            items.append(sub)
        elif rest.startswith('[') and rest.endswith(']'):
            items.append(_split_inline_list(rest))
        else:
            items.append(_scalar(rest))
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
    account's ``home`` (a path), ``isolate_home`` (true | false) and ``home_seed`` (a list of
    paths) checked: ``[(dotted key, problem)]``, empty when well-formed or absent."""
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
    return problems


def env_passthrough(cfg):
    """``worker_pool.env_passthrough``: the variable names a worker session keeps from the
    tick's environment beside the fixed allow-list. ``()`` when unset."""
    pool = (cfg or {}).get('worker_pool') or {}
    return tuple(pool.get('env_passthrough') or ())


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
    'capacity': _MAP, 'clocks': _MAP, 'token_caps': _MAP, 'feeder': _MAP,
}
# `ci:` is a map (or the bare word `none`, a product without CI); these are its keys.
CI_FIELDS = {
    'provider': _STR, 'workflow': _STR, 'test_command': _STR, 'budgets': _MAP,
    'runner_org': _STR, 'labels': _LIST, 'dev_job': _STR,
}
# `capacity:` is a map: this product's session/CI ceilings and its batch shape.
CAPACITY_FIELDS = {'sessions': _STR, 'ci': _STR, 'weight': _STR, 'batch': _MAP}
# `feeder:` is a map: ``hold`` lists the work classes the feeder starts no session for.
FEEDER_FIELDS = {'hold': _LIST}
#: what ``feeder.hold`` may name (:attr:`Product.feeder_hold`)
FEEDER_HOLDS = ('features', 'bugs')
# every product-file section whose own keys are checked, keyed by its own field table.
NESTED_FIELDS = {'ci': CI_FIELDS, 'capacity': CAPACITY_FIELDS, 'feeder': FEEDER_FIELDS}


def _shape_ok(value, shape):
    if value is None or shape is None:
        return True
    if shape == _MAP:
        return isinstance(value, dict)
    if shape == _LIST:
        return isinstance(value, list)
    return not isinstance(value, (dict, list))


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
    # `conventions:` keeps unknown keys (asf.conventions), but the shaped ones are checked
    for key, why in conventions_mod.validate_mapping(data.get('conventions')):
        dotted = 'conventions.' + key
        problems.append((lines.get('conventions.' + key.split('.')[0], 0), dotted, why))
    return sorted(problems)


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
        (``deploy_workflow``). A ``conventions:`` key of the same name wins. Still answers
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
