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
                       'scorecard.py', 'throughput.py', 'reds.py'}


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


def _documented(key, text):
    """``key`` is in ``docs/config.example.yaml``: by its dotted path, or each part as a key."""
    import re
    k = key.replace('[]', '')
    if k.endswith('.*'):
        k = k[:-2]
    if k in text:
        return True
    return all(re.search(r'(^|[\s#{,])' + re.escape(p) + r':', text, re.M) for p in k.split('.'))


class Documented(unittest.TestCase):
    """C: every key the code reads is documented, and the documented ones are registered."""

    def test_every_registered_key_is_in_the_example_config(self):
        with open(os.path.join(ROOT, 'docs', 'config.example.yaml'), encoding='utf-8') as f:
            text = f.read()
        missing = [k for k in sorted(config_keys.KNOWN_CONFIG_KEYS) if not _documented(k, text)]
        self.assertEqual(missing, [], 'document each in docs/config.example.yaml')

    def test_the_documented_identity_key_is_registered(self):
        self.assertEqual(config_keys.unknown_keys({'factory': {'git_identity': 'A <a@b>'}}), [])

    def test_cloud_keys_are_checked_one_by_one(self):
        cfg = {'cloud': {'enabled': True, 'poll_min': 15, 'poll_mins': 15}}
        self.assertEqual(config_keys.unknown_keys(cfg), ['cloud.poll_mins'])
        self.assertNotIn('cloud.*', config_keys.KNOWN_CONFIG_KEYS)

    def test_proves_partial_markers_is_registered_typed_and_documented(self):
        self.assertEqual(config_keys.TYPED['proves.partial_markers'],
                         (config_keys.LIST, None, None))
        self.assertIn('proves.partial_markers', config_keys.KNOWN_CONFIG_KEYS)
        with open(os.path.join(ROOT, 'docs', 'config.example.yaml'), encoding='utf-8') as f:
            text = f.read()
        self.assertTrue(_documented('proves.partial_markers', text))

    def test_ci_stall_watch_pass_stale_s_is_registered_typed_and_documented(self):
        self.assertEqual(config_keys.TYPED['ci.stall_watch.pass_stale_s'],
                         (config_keys.NUM, 1, None))
        self.assertIn('ci.stall_watch.pass_stale_s', config_keys.KNOWN_CONFIG_KEYS)
        with open(os.path.join(ROOT, 'docs', 'config.example.yaml'), encoding='utf-8') as f:
            text = f.read()
        self.assertTrue(_documented('ci.stall_watch.pass_stale_s', text))


def _value_calls():
    """``{key: file}`` for every ``config_keys.value('<key>', ...)`` call in asf/."""
    found = {}
    for here, _d, files in os.walk(os.path.join(ROOT, 'asf')):
        for fn in files:
            if not fn.endswith('.py'):
                continue
            path = os.path.join(here, fn)
            with open(path, encoding='utf-8') as f:
                tree = ast.parse(f.read(), path)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == 'value' and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == 'config_keys' and node.args
                        and isinstance(node.args[0], ast.Constant)):
                    found[node.args[0].value] = os.path.relpath(path, ROOT)
    return found


class Typed(unittest.TestCase):
    def test_every_tunable_read_is_registered_and_typed(self):
        calls = _value_calls()
        self.assertIn('git.timeout_s', calls)
        self.assertEqual({k: f for k, f in calls.items() if k not in config_keys.TYPED}, {})

    def test_every_typed_key_is_read_by_some_code(self):
        text = source_text()
        self.assertEqual([k for k in sorted(config_keys.TYPED) if f"'{k}'" not in text
                          and f"'{k.split('.')[-1]}'" not in text and k not in text], [])

    def test_unset_is_the_default_and_set_is_read(self):
        self.assertEqual(config_keys.value('git.timeout_s', 120, cfg={}), 120)
        self.assertEqual(config_keys.value('git.timeout_s', 120, cfg={'git': {'timeout_s': 600}}), 600)
        self.assertEqual(config_keys.value('harvest.round_cap', 3, cfg={'harvest': {'round_cap': 5.0}}), 5)
        self.assertIsInstance(config_keys.value('harvest.round_cap', 3,
                                                cfg={'harvest': {'round_cap': 5.0}}), int)

    def test_a_malformed_value_keeps_the_default_and_is_named(self):
        cfg = {'git': {'timeout_s': -1}, 'harvest': {'round_cap': 'three'},
               'tick': {'wave_first': 'yes', 'watchdog': {'factor': 2.5}},
               'ci': {'stall': {'rerun_max': 1.5}}, 'flaky': {'title_prefix': 7}}
        self.assertEqual(config_keys.value('git.timeout_s', 120, cfg=cfg), 120)
        self.assertEqual(config_keys.value('harvest.round_cap', 3, cfg=cfg), 3)
        self.assertEqual(config_keys.value('tick.watchdog.factor', 3, cfg=cfg), 2.5)
        self.assertEqual([k for k, _ in config_keys.problems(cfg)],
                         ['ci.stall.rerun_max', 'flaky.title_prefix', 'git.timeout_s',
                          'harvest.round_cap', 'tick.wave_first'])
        self.assertIn('1 or more', dict(config_keys.problems(cfg))['git.timeout_s'])
        self.assertEqual(config_keys.problems({}), [])
        self.assertEqual(config_keys.problems({'host_guards': {'swap_pct': 150}}),
                         [('host_guards.swap_pct', 'must be 100 or less, not 150')])

    def test_an_unregistered_tunable_is_a_bug(self):
        with self.assertRaises(KeyError):
            config_keys.value('no.such_key', 1, cfg={})

    def test_the_operator_config_is_read_and_reread_when_it_changes(self):
        import tempfile
        from asf import gitops, github
        from asf.workers import lifecycle, relaunch
        home = tempfile.mkdtemp(prefix='asf-ck-')
        self.addCleanup(lambda: __import__('shutil').rmtree(home, True))
        orig = env.ASF_HOME
        env.ASF_HOME = home
        self.addCleanup(setattr, env, 'ASF_HOME', orig)
        self.assertEqual(gitops.timeout_s(), gitops.TIMEOUT_S)     # no file: the defaults
        self.assertEqual(lifecycle.round_cap(), lifecycle.ROUND_CAP)
        with open(env.config_path(), 'w') as f:
            f.write('git:\n  timeout_s: 600\nharvest:\n  round_cap: 5\n'
                    'github:\n  pr_list_limit: 1000\n')
        self.assertEqual(gitops.timeout_s(), 600)
        self.assertEqual(lifecycle.round_cap(), 5)
        self.assertEqual(github.pr_list_limit(), 1000)
        with open(env.config_path(), 'w') as f:
            f.write('worker_pool:\n  caps:\n    relaunch: 4\n')
        self.assertEqual(gitops.timeout_s(), gitops.TIMEOUT_S)
        self.assertEqual(relaunch.CAP, 2)
        self.assertEqual(config_keys.value('worker_pool.caps.relaunch', relaunch.CAP), 4)


class DoctorTypes(unittest.TestCase):
    def test_a_malformed_value_is_a_warn_row(self):
        rows = doctor.check_config_keys({'git': {'timeout_s': 0}})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 'warn')
        self.assertIn('git.timeout_s must be 1 or more, not 0', rows[0][1])


_SAMPLE = {config_keys.INT: '2', config_keys.NUM: '90', config_keys.STR: "'x: '",
           config_keys.BOOL: 'false', config_keys.LIST: '[a, b]'}


def every_typed_key_config():
    """A ``config.yaml`` text setting every :data:`asf.config_keys.TYPED` key (a map kind as a
    one-row map), nested as block maps."""
    tree = {}
    for key, (kind, _lo, hi) in sorted(config_keys.TYPED.items()):
        node = tree
        parts = key.split('.')
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        sample = _SAMPLE[kind] if kind != config_keys.MAP else {'k': '1'}
        if kind in (config_keys.INT, config_keys.NUM) and hi is not None:
            sample = str(hi)
        node[parts[-1]] = sample
    lines = []

    def emit(node, depth):
        for k, v in node.items():
            if isinstance(v, dict):
                lines.append('  ' * depth + f'{k}:')
                emit(v, depth + 1)
            else:
                lines.append('  ' * depth + f'{k}: {v}')
    emit(tree, 0)
    return '\n'.join(lines) + '\n'


_PINNED_CONFIG = r"""
import json, os, sys
from asf import env
with open(env.config_path(), 'w') as f:
    f.write(sys.stdin.read())
cfg = env.load_config()
print(json.dumps({'keys': sorted(cfg)}))
"""


class PinnedReader(unittest.TestCase):
    """Products pinned to an older asf load the same ``config.yaml``: every new key must load
    there (its loader checks ``worker_pool`` and ``cloud`` only, and ignores the rest)."""

    def test_every_typed_key_loads_here_and_under_the_pinned_loader(self):
        text = every_typed_key_config()
        cfg = env.loads(text)
        self.assertEqual(config_keys.unknown_keys(cfg), [])
        self.assertEqual(config_keys.problems(cfg), [])
        from tests import pinned
        for sha in pinned.pinned_shas():
            with self.subTest(sha=sha):
                proc = pinned.run_pinned(sha, _PINNED_CONFIG, text)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                import json
                out = json.loads(proc.stdout.strip().splitlines()[-1])
                self.assertIn('git', out['keys'])


def tunable_modules():
    """``{module name: TUNABLES}`` for every asf module that routes constants through config."""
    import importlib
    out = {}
    for here, _d, files in os.walk(os.path.join(ROOT, 'asf')):
        for fn in files:
            if not fn.endswith('.py'):
                continue
            path = os.path.join(here, fn)
            with open(path, encoding='utf-8') as f:
                if '\nTUNABLES = {' not in f.read():
                    continue
            name = os.path.relpath(path, ROOT)[:-3].replace(os.sep, '.')
            out[name] = importlib.import_module(name)
    return out


class Tunables(unittest.TestCase):
    """Each module's ``TUNABLES``: a registered key per constant, whose value is a valid default."""

    def test_every_constant_maps_to_a_typed_key_its_value_satisfies(self):
        mods = tunable_modules()
        self.assertIn('asf.ci_queue', mods)
        self.assertIn('asf.trunk_red', mods)
        for name, mod in mods.items():
            for const, key in mod.TUNABLES.items():
                with self.subTest(module=name, const=const):
                    self.assertIn(key, config_keys.TYPED)
                    self.assertIsNone(config_keys.check(key, getattr(mod, const)))
                    self.assertEqual(mod.tunable(const), getattr(mod, const))

    def test_a_configured_value_reaches_the_reader(self):
        cfg = {'trunk_red': {'max_tries': 5}, 'merge': {'timeout_s': 1800},
               'changelog': {'branch': 'release/notes'}}
        self.assertEqual(config_keys.value('trunk_red.max_tries', 2, cfg=cfg), 5)
        self.assertEqual(config_keys.value('merge.timeout_s', 900, cfg=cfg), 1800)
        self.assertEqual(config_keys.value('changelog.branch', 'asf/changelog', cfg=cfg),
                         'release/notes')


if __name__ == '__main__':
    unittest.main()
