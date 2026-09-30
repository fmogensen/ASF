"""asf.surface — the declared table's own shape, with no reach into the parser (F-0084, S-33750).

``TableTests`` is the table checked in isolation: every value is a 3-tuple, every effect is one
of the four declared targets, every flag is ``None`` or a non-empty string, no key repeats, no
description carries the marker's own text, ``VERBS`` is a lowercase frozenset, every ``RENAMED``
value is a key of ``COMMANDS`` and no ``RENAMED`` key is, and the module imports nothing from the
rest of ``asf`` — the one fact that lets ``build_parser`` read it with no import cycle.
"""
import ast
import os
import unittest

from asf import surface

EFFECTS = {surface.RECORD, surface.REPO, surface.HOST, surface.PRS}


class TableTests(unittest.TestCase):
    def test_module_imports_nothing_from_asf(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             'asf', 'surface.py')
        with open(path, encoding='utf-8') as f:
            tree = ast.parse(f.read(), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name == 'asf' or alias.name.startswith('asf.'),
                                      alias.name)
            elif isinstance(node, ast.ImportFrom):
                self.assertFalse(node.module and
                                  (node.module == 'asf' or node.module.startswith('asf.')),
                                  node.module)

    def test_every_row_is_a_three_tuple(self):
        for name, row in surface.COMMANDS.items():
            self.assertEqual(len(row), 3, name)

    def test_every_effect_is_none_or_a_declared_target(self):
        for name, (text, effect, flag) in surface.COMMANDS.items():
            self.assertTrue(effect is None or effect in EFFECTS, name)

    def test_every_flag_is_none_or_a_non_empty_string(self):
        for name, (text, effect, flag) in surface.COMMANDS.items():
            self.assertTrue(flag is None or (isinstance(flag, str) and flag), name)

    def test_no_key_repeats(self):
        # COMMANDS is a dict literal: a duplicate key silently overwrites the first, so this
        # checks the count against the 49 the spec tabulates, plus `park` (the counterpart of
        # `unpark`), rather than trusting len(dict).
        self.assertEqual(len(surface.COMMANDS), 50)

    def test_no_description_contains_the_marker_text(self):
        for name, (text, effect, flag) in surface.COMMANDS.items():
            self.assertNotIn('[writes', text, name)

    def test_verbs_is_a_frozenset_of_lowercase_alphabetic_words(self):
        self.assertIsInstance(surface.VERBS, frozenset)
        for word in surface.VERBS:
            self.assertTrue(word.isalpha() and word.islower(), word)

    def test_renamed_values_are_declared_and_renamed_keys_are_not(self):
        for old, new in surface.RENAMED.items():
            self.assertIn(new, surface.COMMANDS, new)
            self.assertNotIn(old, surface.COMMANDS, old)

    def test_marker_renders_the_three_shapes(self):
        self.assertEqual(surface.marker(None, None), '')
        self.assertEqual(surface.marker(surface.RECORD, None), ' [writes the record]')
        self.assertEqual(surface.marker(surface.HOST, '--write'),
                          ' [writes this host with --write]')


if __name__ == '__main__':
    unittest.main()
