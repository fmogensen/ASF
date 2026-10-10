"""tests.test_rulepass — the disputes code rules before any adjudicate session is spawned.

Pure, over built ``Dispute``s and a fake product with a ``docs/decisions/`` folder, the fixture
shape ``tests/test_precedent.py`` already uses (F-0300, S-78054). No ``screen`` cases here: those
are a later Task's, in this same module.
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import env
from asf.evidence import rulepass as rp
from asf.evidence import rulings
from asf.workers import lifecycle


class _TempProduct(unittest.TestCase):
    """A fresh ``backlog_dir`` and ``docs/decisions/`` under a fresh ``tempfile.mkdtemp``, the
    fixture shape :mod:`tests.test_precedent` already uses."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='rulepass_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._n = 0

    def product(self, flag='on', decisions_files=None):
        self._n += 1
        backlog = os.path.join(self.tmp, f'backlog{self._n}')
        repo = os.path.join(self.tmp, f'repo{self._n}')
        os.makedirs(backlog, exist_ok=True)
        os.makedirs(os.path.join(repo, 'docs', 'decisions'), exist_ok=True)
        for name, content in (decisions_files or {}).items():
            with open(os.path.join(repo, 'docs', 'decisions', name), 'w', encoding='utf-8') as f:
                f.write(content)
        flags = {'rule_pass': flag} if flag is not None else {}
        data = {'repo_dir': repo, 'backlog_dir': backlog, 'conventions': {'flags': flags}}
        return env.Product(f'rulepass-test-{self._n}', data)

    def write_card(self, product, item, history_lines=(), folder='tasks'):
        d = os.path.join(product.backlog_dir, folder)
        os.makedirs(d, exist_ok=True)
        body = '## History\n' + '\n'.join(history_lines) + '\n' if history_lines else ''
        with open(os.path.join(d, f'{item}.md'), 'w', encoding='utf-8') as f:
            f.write(f'---\nid: {item}\ntype: task\ntitle: "x"\nstate: New\n---\n\n{body}')


def _dispute(finding, entries, where='correction', item='T-0001', branch='worker/T-0001'):
    return rp.Dispute(item=item, branch=branch, finding=tuple(finding), entries=tuple(entries),
                      where=where)


class ClassifyTests(unittest.TestCase):
    def test_each_cosmetic_class(self):
        self.assertEqual(rp.classify('squash these three commits into one'), 'commit-shape')
        self.assertEqual(
            rp.classify("the docstring says the reader where it should say the reader's"),
            'wording')
        self.assertEqual(rp.classify('two blank lines between the defs'), 'formatting')

    def test_never_waive_beats_every_cosmetic_word(self):  # C6, P14
        texts = ['reword the licence header',
                 'fix the typo in the secret name',
                 'the wording of the customer-facing error',
                 'rename it — it is also wrong for `None`',
                 'rephrase it, and there is no test for the branch']
        for t in texts:
            self.assertEqual(rp.classify(t), '', t)
        self.assertEqual(rp.classify(None), '')  # also wrong for None


class RuleTests(_TempProduct):
    def test_a_two_item_all_cosmetic_list_is_ruled_cosmetic(self):  # C4, P11
        entries = (('C1', 'squash these commits into one over asf/flake.py'),
                   ('C2', 'two blank lines between the defs over asf/other.py'))
        d = _dispute(finding=('asf/flake.py', 'asf/other.py'), entries=entries)
        product = self.product()
        ruling = rp.rule(product, d, items={})
        self.assertEqual(ruling.rules, ('cosmetic', 'cosmetic'))
        sentences = [s for s in ruling.text.split('. ') if s]
        self.assertEqual(len(sentences), 2)
        self.assertIn('asf/flake.py', sentences[0])
        self.assertIn('asf/other.py', sentences[1])
        self.assertTrue(ruling.text.rstrip().endswith('.'))
        self.assertEqual(ruling.finding,
                         tuple(lifecycle.finding_of(lifecycle.REVIEW, '',
                                                    keys=['asf/flake.py', 'asf/other.py'])))

    def test_one_correctness_item_blocks_the_whole_dispute(self):  # C5
        entries = (('C1', 'squash these commits into one over asf/flake.py'),
                   ('C2', 'this raises an exception under load'))
        d = _dispute(finding=('asf/flake.py', 'asf/other.py'), entries=entries)
        product = self.product()
        self.assertIsNone(rp.rule(product, d, items={}))

    def test_a_standing_ruling_the_item_already_carries_is_precedent(self):
        product = self.product()
        d = _dispute(finding=('asf/flake.py',),
                     entries=(('C1', 'this point was already adjudicated; see the prior run.'),))
        with mock.patch.object(rp, 'standing',
                               return_value=[{'at': '2026-10-01 10:00',
                                              'job': 'adjudicate-t-0001', 'text': 'prior ruling.'}]):
            ruling = rp.rule(product, d, items={})
        self.assertEqual(ruling.rules, ('precedent',))
        self.assertEqual(len(ruling.cites), 1)
        self.assertIn('adjudicate-t-0001', ruling.cites[0])
        self.assertIn('2026-10-01 10:00', ruling.cites[0])

    def test_a_decision_the_register_confirms_accepted_is_precedent(self):
        product = self.product(decisions_files={
            'd-0117.md': '---\nid: D-0117\ntitle: "Queue retries are idempotent"\n'
                         'status: accepted\n---\nBody.\n'})
        d = _dispute(finding=('asf/queue.py',),
                     entries=(('C2', 'this repeats the point D-0117 already settled'),))
        ruling = rp.rule(product, d, items={})
        self.assertEqual(ruling.rules, ('precedent',))
        self.assertEqual(ruling.cites,
                         ('D-0117 — Queue retries are idempotent · docs/decisions/d-0117.md',))

    def test_a_superseded_or_absent_decision_is_not_precedent(self):
        superseded = self.product(decisions_files={
            'd-0117.md': '---\nid: D-0117\ntitle: "Queue retries are idempotent"\n'
                         'status: superseded\n---\nBody.\n'})
        d = _dispute(finding=('asf/queue.py',),
                     entries=(('C2', 'this repeats the point D-0117 already settled'),))
        self.assertIsNone(rp.rule(superseded, d, items={}))  # STALE_STATUS

        absent = self.product()
        self.assertIsNone(rp.rule(absent, d, items={}))

    def test_no_decision_and_no_cosmetic_class_is_not_ruled(self):  # Out: no lexical guess
        product = self.product()
        d = _dispute(finding=('asf/retry.py',),
                     entries=(('C3', 'the retry logic needs a second look'),))
        self.assertIsNone(rp.rule(product, d, items={}))


class HistoryLineTests(unittest.TestCase):
    def test_the_exact_shape_and_readback(self):  # P4
        ruling = rp.Ruling(rules=('cosmetic',),
                           text='C1 is overruled as cosmetic (commit-shape): a sentence.',
                           cites=(), finding=('asf/flake.py',), why='rule pass: cosmetic',
                           where='correction')
        line = rp.history_line(ruling, now='2026-10-08T10:30:00')
        self.assertEqual(
            line,
            '- 2026-10-08 10:30 adjudicate (rule-pass): C1 is overruled as cosmetic '
            '(commit-shape): a sentence. [rule: cosmetic; finding: asf/flake.py] '
            '[precedent: none]')
        parsed = rulings.parse(f'## History\n{line}\n')
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]['job'], 'rule-pass')
        self.assertIn('[precedent: none]', parsed[0]['text'])


class ReadBackTests(unittest.TestCase):
    def test_the_filed_line_is_read_back_by_the_landed_readers(self):  # P4, P5
        entries = (('C1', 'squash these commits into one over asf/flake.py'),
                   ('C2', 'two blank lines between the defs over asf/other.py'))
        d = _dispute(finding=('asf/flake.py', 'asf/other.py'), entries=entries)
        ruling = rp.rule(env.Product('x', {}), d, items={})
        line = rp.history_line(ruling, now='2026-10-08T10:30:00')
        std = rulings.parse(f'## History\n{line}\n')

        body = ('## C\n'
                '1. squash these commits into one over asf/flake.py\n'
                '2. two blank lines between the defs over asf/other.py\n')
        self.assertEqual(rulings.covers(std, 'C1', 'squash these over asf/flake.py'), 'rule-pass')
        self.assertEqual(rulings.covers(std, 'C2', 'two blanks over asf/other.py'), 'rule-pass')
        self.assertEqual(rulings.reraised_only(body, std), ['rule-pass'])

        extra = body + '3. a new problem over asf/newfile.py\n'
        self.assertIsNone(rulings.reraised_only(extra, std))


class WaivedTests(_TempProduct):
    def test_finds_its_own_line_by_finding_key_and_not_a_different_key(self):  # C7
        product = self.product()
        entries = (('C1', 'squash these commits into one over asf/flake.py'),)
        d = _dispute(finding=('asf/flake.py',), entries=entries)
        ruling = rp.rule(product, d, items={})
        line = rp.history_line(ruling, now='2026-10-08T10:30:00')
        self.write_card(product, 'T-0001', history_lines=[line])

        self.assertEqual(rp.waived(product, 'T-0001', ('asf/flake.py',)), '2026-10-08 10:30')
        self.assertEqual(rp.waived(product, 'T-0001', ('asf/other.py',)), '')


class EnabledTests(unittest.TestCase):
    def test_default_off_and_the_on_words(self):  # P10, C9
        self.assertFalse(rp.enabled(None))
        for value, want in (('off', False), ('', False), ('on', True), ('true', True),
                            ('yes', True), ('1', True), (True, True)):
            product = env.Product('x', {'conventions': {'flags': {'rule_pass': value}}})
            self.assertEqual(rp.enabled(product), want, value)
        self.assertFalse(rp.enabled(env.Product('x', {})))


class ApplyTests(unittest.TestCase):
    def test_flag_off_reads_and_writes_nothing(self):
        product = env.Product('x', {'conventions': {'flags': {'rule_pass': 'off'}}})
        row = object()
        with mock.patch.object(rp, 'dispute', side_effect=AssertionError('must not be called')):
            out = []
            self.assertIsNone(rp.apply(product, row, {}, out.append))
        self.assertEqual(out, [])

    def test_an_unreadable_review_returns_none_and_one_line_never_an_exception(self):
        product = env.Product('x', {'conventions': {'flags': {'rule_pass': 'on'}}})
        row = type('Row', (), {'item_id': 'T-0001', 'branch': 'worker/T-0001'})()
        out = []
        with mock.patch.object(rp, 'dispute', side_effect=OSError('review unreadable')):
            result = rp.apply(product, row, {}, out.append)
        self.assertIsNone(result)
        self.assertEqual(len(out), 1)
        self.assertIn('T-0001', out[0])


if __name__ == '__main__':
    unittest.main()
