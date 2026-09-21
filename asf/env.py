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
    """``~/.ASF/config.yaml``: scheduler, worker pool, quota guards, defaults, paths."""
    return load_file(config_path())


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
    def backlog_dir(self):
        v = self._get('backlog_dir')
        return os.path.expanduser(v) if v else None

    @property
    def conventions(self):
        """The product's :class:`asf.conventions.Conventions`, defaults filled in.

        Built from the yaml's ``conventions:`` block, plus three values that live at the top
        level of a product file because more than the conventions read them: ``main``,
        ``stage_limits`` and ``ci.test_command`` (the gate harvest runs). A ``conventions:``
        key of the same name wins. Still answers ``.get()``/``[]``, so the callers that read it
        as a mapping — and an operator's extra keys — keep working."""
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

    def branch_prefix(self, kind):
        """The prefix *without* its separator (``worker``), for the callers that compose
        ``<prefix>/<job>`` themselves. :meth:`Conventions.branch` builds the whole name."""
        return self.conventions.prefix(kind).rstrip('/')


def load_product(name=None):
    name = name or default_product_name()
    data = load_file(product_path(name))
    if not data:
        raise ConfigError(f'no product config at {product_path(name)}')
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
