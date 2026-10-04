"""``asf.config_keys``: the registry of ``~/.ASF/config.yaml`` keys the code reads, and the
doctor row that names the rest."""
import ast
import os
import unittest

from asf import config_keys, doctor, env

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_NAMES = {'cfg'}
#: modules whose ``cfg`` is a product-file block (``release:``, ``flake:`` ...), not config.yaml
PRODUCT_BLOCK_FILES = {'release.py', 'flake.py', 'deploy.py', 'prod.py', 'loop.py', 'shadow.py',
                       'scorecard.py'}


def _is_cfg(node):
    """``cfg``, ``config`` or ``(cfg or {})``."""
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        node = node.values[0]
    return isinstance(node, ast.Name) and node.id in CFG_NAMES


def _is_load_config(node):
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'load_config')


def top_level_reads():
    """``{key: file}`` for every ``cfg.get('k')`` / ``cfg['k']`` / ``load_config().get('k')``."""
    found = {}
    for here, _d, files in os.walk(os.path.join(ROOT, 'asf')):
        for fn in files:
            if not fn.endswith('.py') or fn in PRODUCT_BLOCK_FILES:
                continue
            path = os.path.join(here, fn)
            with open(path, encoding='utf-8') as f:
                tree = ast.parse(f.read(), path)
            for node in ast.walk(tree):
                key = None
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == 'get' and node.args
                        and (_is_cfg(node.func.value) or _is_load_config(node.func.value))):
                    arg = node.args[0]
                elif (isinstance(node, ast.Subscript)
                        and (_is_cfg(node.value) or _is_load_config(node.value))):
                    arg = node.slice
                else:
                    continue
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    key = arg.value
                if key:
                    found[key] = os.path.relpath(path, ROOT)
    return found


def _chain(node):
    """The dotted path a ``cfg.get('a').get('b')`` / ``(cfg.get('a') or {}).get('b')`` /
    ``cfg['a']['b']`` expression reads, or None when it is not rooted at the config."""
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        node = node.values[0]
    if _is_cfg(node) or _is_load_config(node):
        return []
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'get' and node.args
            and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
        base = _chain(node.func.value)
        return None if base is None else base + [node.args[0].value]
    if (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)):
        base = _chain(node.value)
        return None if base is None else base + [node.slice.value]
    return None


def nested_reads():
    """``{dotted.path: file}`` for every chained read two or more keys deep."""
    found = {}
    for here, _d, files in os.walk(os.path.join(ROOT, 'asf')):
        for fn in files:
            if not fn.endswith('.py') or fn in PRODUCT_BLOCK_FILES:
                continue
            path = os.path.join(here, fn)
            with open(path, encoding='utf-8') as f:
                tree = ast.parse(f.read(), path)
            for node in ast.walk(tree):
                chain = _chain(node) if isinstance(node, (ast.Call, ast.Subscript)) else None
                if chain and len(chain) >= 2:
                    found['.'.join(chain)] = os.path.relpath(path, ROOT)
    return found


def source_text():
    out = []
    for here, _d, files in os.walk(os.path.join(ROOT, 'asf')):
        for fn in files:
            if fn.endswith('.py') and fn != 'config_keys.py':
                with open(os.path.join(here, fn), encoding='utf-8') as f:
                    out.append(f.read())
    for fn in os.listdir(os.path.join(ROOT, 'tools')):    # cutover.sh reads config keys too
        if fn.endswith('.sh'):
            with open(os.path.join(ROOT, 'tools', fn), encoding='utf-8') as f:
                out.append(f.read())
    return '\n'.join(out)


class Registry(unittest.TestCase):
    def test_the_scan_finds_reads(self):
        self.assertIn('worker_pool', top_level_reads())
        self.assertIn('default_product', top_level_reads())

    def test_every_top_level_key_the_code_reads_is_registered(self):
        known = {k.split('.')[0].split('[')[0] for k in config_keys.KNOWN_CONFIG_KEYS}
        missing = {k: f for k, f in top_level_reads().items() if k not in known}
        self.assertEqual(missing, {}, 'register each in asf.config_keys.KNOWN_CONFIG_KEYS')

    def test_every_nested_key_the_code_reads_is_registered(self):
        reads = nested_reads()
        self.assertIn('worker_pool.backend', reads)
        missing = {k: f for k, f in reads.items()
                   if not config_keys._known(k, config_keys.KNOWN_CONFIG_KEYS)
                   and not any(r.startswith(k + '.') or r.startswith(k + '[]')
                               for r in config_keys.KNOWN_CONFIG_KEYS)}
        self.assertEqual(missing, {}, 'register each in asf.config_keys.KNOWN_CONFIG_KEYS')

    def test_every_registered_key_is_mentioned_by_some_code(self):
        text = source_text()
        dead = [k for k in sorted(config_keys.KNOWN_CONFIG_KEYS)
                for leaf in [k.replace('[]', '').split('.')[-1]]
                if leaf != '*' and f"'{leaf}'" not in text and f'`{leaf}`' not in text
                and f'.{leaf}' not in text and f'{leaf}:' not in text]
        self.assertEqual(dead, [], 'a registered key nothing reads: remove it')

    def test_the_example_config_has_no_unknown_key(self):
        cfg = env.load_file(os.path.join(ROOT, 'docs', 'config.example.yaml'))
        self.assertEqual(config_keys.unknown_keys(cfg), [])


class Unknown(unittest.TestCase):
    def test_a_key_nothing_reads_is_named_by_its_path(self):
        cfg = {'worker_pool': {'caps': {'local_max_inflight': 8, 'cloud_max_inflight': 16}},
               'default_product': 'asf'}
        self.assertEqual(config_keys.unknown_keys(cfg), ['worker_pool.caps.local_max_inflight'])

    def test_list_items_and_free_form_maps(self):
        cfg = {'worker_pool': {'models': {'anything': 'x'},
                               'accounts': [{'name': 'a', 'cap': 1, 'window_7d_reset': 'n'}]},
               'bogus': {'a': 1}}
        self.assertEqual(config_keys.unknown_keys(cfg),
                         ['bogus', 'worker_pool.accounts[].window_7d_reset'])

    def test_empty_config_has_none(self):
        self.assertEqual(config_keys.unknown_keys(None), [])


class DoctorRow(unittest.TestCase):
    def test_unknown_keys_are_one_warn_row(self):
        rows = doctor.check_config_keys({'worker_pool': {'caps': {'local_max_inflight': 8}}})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 'warn')
        self.assertIn('worker_pool.caps.local_max_inflight', rows[0][1])

    def test_a_clean_config_has_no_row(self):
        self.assertEqual(doctor.check_config_keys({'default_product': 'asf'}), [])


if __name__ == '__main__':
    unittest.main()
