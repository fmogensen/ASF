"""Every flag the source reads is a name in ``conventions.KNOWN_FLAGS``.

``asf doctor`` lists a flag a product file sets that is not in the registry as unknown, so a
flag read in ``asf/`` but missing from it would be called unknown on a product that sets it.
"""
import ast
import os
import re
import unittest

from asf import conventions

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: module constants that end in FLAG but are not ``flags:`` names
NOT_FLAGS = {'UNCHECKED_FLAG'}
#: string values that are not ``flags:`` names (asf.kernel.waits.FLAG is the over-target mark)
NOT_FLAG_VALUES = {'⚠'}


def flags_read():
    """``{flag name: file}`` for every ``<x>.flag('name')`` call and every ``FLAG = 'name'`` /
    ``*_FLAG = 'name'`` constant under ``asf/``."""
    found = {}
    for here, _dirs, files in os.walk(os.path.join(ROOT, 'asf')):
        for fn in files:
            if not fn.endswith('.py'):
                continue
            path = os.path.join(here, fn)
            with open(path, encoding='utf-8') as f:
                tree = ast.parse(f.read(), path)
            rel = os.path.relpath(path, ROOT)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == 'flag' and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)):
                    found[node.args[0].value] = rel
                elif (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)):
                    for t in node.targets:
                        if (isinstance(t, ast.Name) and re.fullmatch(r'(\w+_)?FLAG', t.id)
                                and t.id not in NOT_FLAGS):
                            found[node.value.value] = rel
    return found


class KnownFlags(unittest.TestCase):
    def test_the_scan_finds_flags(self):
        self.assertIn('queue_store', flags_read())
        self.assertIn('store_shared', flags_read())

    def test_every_flag_read_in_asf_is_known(self):
        missing = {n: f for n, f in flags_read().items() if n not in conventions.KNOWN_FLAGS
               and n not in NOT_FLAG_VALUES}
        self.assertEqual(missing, {}, 'add each to conventions.KNOWN_FLAGS and the flags docs')


if __name__ == '__main__':
    unittest.main()
