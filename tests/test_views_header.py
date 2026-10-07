"""asf.views.header — the registry, head() and command_key(), against no caller yet (Task 1)."""
import unittest

from asf.cli import build_parser
from asf.views import header

#: `TITLES` carries `health` and `performance` from Task 1, and neither is a registered command
#: until Task 5 (`asf/cli.py`) registers them. Task 7 empties this set and asserts the full rule.
PENDING = {'health', 'performance'}


def registered_commands():
    sub = [a for a in build_parser()._actions if a.dest == 'command'][0]
    return set(sub.choices)


class HeaderTests(unittest.TestCase):
    def test_clause_appends_after_an_em_dash(self):
        self.assertEqual(header.head('status', 'p', '04:11'),
                          f"**{header.TITLES['status']} p** — 04:11")

    def test_empty_clause_appends_nothing(self):
        self.assertEqual(header.head('status', 'p', ''), f"**{header.TITLES['status']} p**")

    def test_no_clause_appends_nothing(self):
        self.assertEqual(header.head('status', 'p'), f"**{header.TITLES['status']} p**")

    def test_none_ish_clause_appends_nothing(self):
        self.assertEqual(header.head('status', 'p', None), f"**{header.TITLES['status']} p**")

    def test_product_sits_inside_the_bold(self):
        line = header.head('status', 'p', '04:11')
        self.assertIn(f"{header.TITLES['status']} p**", line)

    def test_credentials_check(self):
        self.assertEqual(header.TITLES['credentials check'], 'CREDENTIALS')

    def test_every_title_is_read_from_titles(self):
        for command, title in header.TITLES.items():
            self.assertEqual(header.head(command, 'p'), f"**{title} p**")

    def test_unregistered_command_raises_key_error(self):
        with self.assertRaises(KeyError):
            header.head('no-such-command', 'p')


class CommandKeyTests(unittest.TestCase):
    def key_of(self, argv):
        return header.command_key(build_parser().parse_args(argv))

    def test_ci_queue(self):
        self.assertEqual(self.key_of(['ci', 'queue']), 'ci queue')

    def test_ci_reconcile(self):
        self.assertEqual(self.key_of(['ci', 'reconcile']), 'ci reconcile')

    def test_ci_reserve(self):
        self.assertEqual(self.key_of(['ci', 'reserve']), 'ci reserve')

    def test_rules_check(self):
        self.assertEqual(self.key_of(['rules', 'check']), 'rules check')

    def test_status(self):
        self.assertEqual(self.key_of(['status']), 'status')

    def test_release_readiness(self):
        self.assertEqual(self.key_of(['release-readiness']), 'release-readiness')


class RegistryTests(unittest.TestCase):
    def test_plugin_views_are_a_subset_of_titles(self):
        from asf import plugin_build
        self.assertTrue(set(plugin_build.VIEWS) <= set(header.TITLES))

    def test_every_title_key_first_word_is_registered_or_pending(self):
        commands = registered_commands()
        for key in header.TITLES:
            first_word = key.split(' ')[0]
            self.assertTrue(first_word in commands or key in PENDING,
                             f"{key!r} is neither registered nor in PENDING")
