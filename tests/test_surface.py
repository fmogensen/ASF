"""asf.surface — the declared table's own shape, with no reach into the parser (F-0084, S-33750).

``TableTests`` is the table checked in isolation: every value is a 3-tuple, every effect is one
of the four declared targets, every flag is ``None`` or a non-empty string, no key repeats, no
description carries the marker's own text, ``VERBS`` is a lowercase frozenset, every ``RENAMED``
value is a key of ``COMMANDS`` and no ``RENAMED`` key is, and the module imports nothing from the
rest of ``asf`` — the one fact that lets ``build_parser`` read it with no import cycle.
"""
import argparse
import ast
import contextlib
import io
import os
import unittest
from unittest import mock

from asf import cli, schema, surface

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
        # `unpark`), `correct` and `reset`, rather than trusting len(dict).
        self.assertEqual(len(surface.COMMANDS), 59)  # + `facts` (W6-PR2), `untick`, `audit-proofs` (S6), `retire`, `undeliver`

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


class EveryRecordWriterChecksTheSchemaTests(unittest.TestCase):
    """F-0114 §1's dispatch guard (``asf/cli.py:_main``): every command whose ``COMMANDS`` effect
    is ``RECORD`` calls ``schema.require`` before it writes — seventeen-plus at the dispatch,
    ``tick`` inside itself (PD6) — and no command that merely reads ever does."""

    def _fill(self, sub, argv):
        """Every value ``sub`` requires, filled with a placeholder, nothing it does not —
        recursing into a required sub-subparser (``deploy record``) the same way."""
        for action in sub._actions:
            if not action.required or action.dest == 'help':
                continue
            if isinstance(action, argparse._SubParsersAction):
                choice = next(iter(action.choices))
                argv.append(choice)
                self._fill(action.choices[choice], argv)
                continue
            value = next(iter(action.choices)) if action.choices else '1'
            if action.option_strings:
                argv.append(action.option_strings[0])
                if action.nargs != 0:
                    argv.append(value)
            else:
                argv.append(value)

    def _argv(self, name):
        """A minimal argv that satisfies ``name``'s own parser: every value it requires, filled
        with a placeholder, nothing it does not. A table name the parser does not register under
        (``import-items``, registered as the pre-rename ``migrate`` — F-0084, outside this
        card's footprint) runs under its own registered spelling instead."""
        parser = cli.build_parser()
        command = next(a for a in parser._subparsers._group_actions if a.dest == 'command')
        registered = name if name in command.choices else next(
            (old for old, new in surface.RENAMED.items() if new == name), name)
        argv = [registered]
        self._fill(command.choices[registered], argv)
        return argv

    def _ran_require(self, argv):
        from asf import env
        product = env.Product('sample', {'repo_slug': 'x/y', 'backlog_dir': '/nonexistent'})
        with mock.patch.object(schema, 'require', return_value=True) as req, \
                mock.patch.object(env, 'default_product_name', return_value='sample'), \
                mock.patch.object(env, 'load_product', return_value=product), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                cli.main(argv)
            except SystemExit:
                pass
            except Exception:  # noqa: BLE001 — only whether the guard fired is under test here
                pass
        return req.called

    def test_every_dispatch_record_command_calls_require_tick_is_the_only_exemption(self):
        record_commands = {name for name, (_t, effect, _f) in surface.COMMANDS.items()
                           if effect == surface.RECORD}
        self.assertIn('tick', record_commands)
        exempt = set()
        for name in sorted(record_commands):
            _text, _effect, flag = surface.COMMANDS[name]
            argv = self._argv(name)
            if name == 'tick':
                with mock.patch('asf.tick.tick.cmd_tick', return_value=0):
                    called = self._ran_require(argv)
                if not called:
                    exempt.add(name)
                continue
            if flag is None:
                self.assertTrue(self._ran_require(argv), name)
            else:
                # a flagged RECORD command only writes — and is only guarded — with its own
                # flag/subcommand given; bare (``_argv``'s minimal form) never carries it
                self.assertFalse(self._ran_require(argv), name)
                self.assertTrue(self._ran_require(argv + [flag]), name)
        self.assertEqual(exempt, {'tick'})

    def test_every_read_only_command_never_calls_require(self):
        for name, (_text, effect, _flag) in surface.COMMANDS.items():
            if effect is not None:
                continue
            with mock.patch.object(schema, 'require', return_value=True) as req, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    cli.main(self._argv(name))
                except SystemExit:
                    pass
                except Exception:  # noqa: BLE001 — only the guard's silence is under test
                    pass
            self.assertFalse(req.called, name)


if __name__ == '__main__':
    unittest.main()
