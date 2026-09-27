"""asf.failure — the classifier: four classes, three patterns, one policy (F-0054 §2.1, §3.1)."""
import ast
import os
import unittest

from asf import failure


class ClassTests(unittest.TestCase):
    """F-0054 §3.1: the four classes, the table and the policy."""

    def test_of_signature_over_every_known_name(self):
        for name, cls in failure.SIGNATURE_CLASS.items():
            self.assertEqual(failure.of_signature(name), cls, name)

    def test_of_signature_none_for_unknown(self):
        for name in ('', None, 'finished', 'wat'):
            self.assertIsNone(failure.of_signature(name), name)

    def test_of_text(self):
        cases = {
            'API Error: 429': failure.QUOTA,
            'connection reset by peer': failure.TRANSPORT,
            'Invalid API key · Please run /login': failure.CREDENTIAL,
            'auth_env CLAUDE_CODE_OAUTH_TOKEN file /x/y does not exist': failure.CREDENTIAL,
            'model claude-nope does not exist': failure.CREDENTIAL,
            'the test failed': None,
        }
        for text, cls in cases.items():
            self.assertEqual(failure.of_text(text), cls, text)

    def test_of_text_ranking(self):
        self.assertEqual(
            failure.of_text('authentication_error and connection reset'), failure.CREDENTIAL)
        self.assertEqual(failure.of_text('API Error: 429, operation timed out'), failure.QUOTA)

    def test_classify_signature_outranks_text(self):
        cls, why = failure.classify(signature='auth', text='connection reset')
        self.assertEqual(cls, failure.CREDENTIAL)
        self.assertEqual(why, 'signature auth')

    def test_classify_no_signature_no_text(self):
        self.assertEqual(failure.classify(text=''), (failure.WORK, 'no signature'))

    def test_classify_text_only(self):
        cls, why = failure.classify(text='connection reset by peer')
        self.assertEqual(cls, failure.TRANSPORT)
        self.assertEqual(why, 'text: transport')

    def test_retries(self):
        self.assertEqual(failure.RETRIES, {
            failure.CREDENTIAL: 0, failure.QUOTA: 0, failure.TRANSPORT: 1, failure.WORK: None,
        })

    def test_may_relaunch(self):
        self.assertFalse(failure.may_relaunch(failure.CREDENTIAL, 0))
        self.assertFalse(failure.may_relaunch(failure.QUOTA, 0))
        self.assertTrue(failure.may_relaunch(failure.TRANSPORT, 0))
        self.assertFalse(failure.may_relaunch(failure.TRANSPORT, 1))
        self.assertTrue(failure.may_relaunch(failure.WORK, 0))
        self.assertTrue(failure.may_relaunch(failure.WORK, 99))

    def test_label(self):
        self.assertEqual(
            failure.label(failure.CREDENTIAL, 'signature auth'), 'class credential (signature auth)')

    def test_operator_text_names_product_and_lane_never_an_account(self):
        home = os.path.expanduser('~/secret-account-home')
        account_name = 'jsmith-primary'
        msg = (f'NEEDS OPERATOR: worker account {account_name}: auth_env CLAUDE_CODE_OAUTH_TOKEN '
               f'file {home}/.auth does not exist — write the value into it')
        cls, why = failure.classify(text=msg)
        self.assertEqual(cls, failure.CREDENTIAL)
        text = failure.operator_text(cls, 'asf', 'T-0244', 'task-t-0244', lane='lane-2', why=why)
        self.assertIn('asf', text)
        self.assertIn('lane-2', text)
        self.assertIn('T-0244', text)
        self.assertIn('task-t-0244', text)
        self.assertNotIn(account_name, text)
        self.assertNotIn(home, text)
        self.assertNotIn(msg, text)

    def test_no_why_ever_carries_matched_text(self):
        texts = [
            'API Error: 429', 'connection reset by peer', 'Please run /login',
            'auth_env FOO file /home/x/.auth does not exist', 'model claude-nope does not exist',
        ]
        for text in texts:
            _, why = failure.classify(text=text)
            self.assertNotIn(text, why)

    def test_imports_nothing_of_asf(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(here, 'asf', 'failure.py')
        with open(path, encoding='utf-8') as f:
            tree = ast.parse(f.read(), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name == 'asf' or alias.name.startswith('asf.'), alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ''
                self.assertFalse(module == 'asf' or module.startswith('asf.'), module)


if __name__ == '__main__':
    unittest.main()
