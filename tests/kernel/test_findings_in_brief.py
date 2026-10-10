"""Review findings reach a fix round whole: each FINDINGS line in full in its own section, never
cut by the cap on the brief's appended text (live 2026-10-10, F-0331: three long findings, the
fix brief cut mid-sentence after the first, the cloud session could not read the review)."""
import unittest

from asf.kernel import actions as A
from asf.kernel import briefs as KB

try:
    from kernel import builders as B
    from kernel.test_go_live import _briefer, _product
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel.test_go_live import _briefer, _product

State = B.State
FINDINGS = ['asf/a.py:%d — %s ENDS-%d' % (n, ('long reasoning about the defect ' * 45), n)
            for n in (1, 2, 3)]


class FindingsWhole(unittest.TestCase):

    def brief(self, findings, answers=()):
        item = B.task('T-0001', state=State.REVIEW)
        item.answers = list(answers)
        return _briefer(_product())(item, A.Launch('build', 'T-0001', 'worker/T-0001'),
                                    list(findings), B.pr(7, 'T-0001')).text

    def test_three_long_findings_all_arrive_in_full_in_their_own_section(self):
        self.assertGreater(sum(map(len, FINDINGS)), 4000)
        text = self.brief(FINDINGS)
        self.assertIn('## Review findings', text)
        for f in FINDINGS:
            self.assertIn(f, text)

    def test_answers_are_trimmed_before_the_findings_are(self):
        answers = ['answer %d %s' % (n, 'x' * 900) for n in range(8)]
        text = self.brief(FINDINGS[:1], answers)
        self.assertIn(FINDINGS[0], text)
        self.assertIn(answers[-1], text)
        self.assertNotIn(answers[0], text)

    def test_parse_verdict_keeps_every_line_whole(self):
        v, got = KB.parse_verdict('VERDICT: changes\n' + '\n'.join('FINDINGS: ' + f
                                                                   for f in FINDINGS))
        self.assertEqual((v, got), ('changes', FINDINGS))


if __name__ == '__main__':
    unittest.main()
