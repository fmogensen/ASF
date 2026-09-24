"""asf.harvest.refine: the verdict on a document that was regenerated instead of refined, and
the harvest that holds such a spec/plan branch and hands it back as a correction (F-0023)."""
import unittest

from asf import briefs
from asf.conventions import Conventions
from asf.feeder import rows
from asf.harvest import refine
from asf.workers.stall import CORRECTION_HEAD
from tests import test_harvest   # the module, not its classes: discovery would rerun them here
from tests.test_harvest import sh

SPEC = 'docs/specs/f-0001.md'
HEADINGS = ('Decisions', 'The design', 'Records', 'Acceptance tests')


def document(stories=3, headings=HEADINGS, filler=6):
    """A spec that names ``stories`` Stories under ``headings``, ``filler`` prose lines each."""
    lines = ['# Spec F-0001', '']
    for i, heading in enumerate(headings):
        lines += [f'## {heading}', '']
        if i == 0:
            lines += [f'- S-{n:04d} is one of the stories' for n in range(1, stories + 1)] + ['']
        lines += [f'{heading}: prose line {n}' for n in range(filler)] + ['']
    return '\n'.join(lines)


class VerdictTest(unittest.TestCase):
    def test_no_document_on_the_trunk_is_not_a_rewrite(self):
        self.assertIsNone(refine.verdict('', document()))
        self.assertIsNone(refine.verdict('\n  \n', 'anything'))

    def test_an_addition_is_a_refinement(self):
        before = document()
        after = before + '\n## Appendix\n\nmore\n\n### Also\n\n- S-0001 again\n'
        self.assertIsNone(refine.verdict(before, after, 0.5))

    def test_an_edit_is_a_refinement(self):
        before = document()
        after = before.replace('prose line 1', 'prose line one, said better')
        self.assertIsNone(refine.verdict(before, after, 0.5))

    def test_a_dropped_heading_is_a_rewrite(self):
        before = document()
        after = before.replace('## Records', '## Ledger')
        kind, text = refine.verdict(before, after, 0.5)
        self.assertEqual(kind, 'rewrite')
        self.assertIn('"## Records"', text)
        self.assertTrue(text.startswith('1 anchor(s) the document on main carries are gone'), text)
        self.assertIn('never regenerates it', text)

    def test_a_dropped_story_id_is_a_rewrite(self):
        before = document(stories=8)
        after = before
        for n in range(4, 9):
            after = after.replace(f'S-{n:04d}', 'a story')
        kind, text = refine.verdict(before, after, 0.5)
        self.assertEqual(kind, 'rewrite')
        for n in range(4, 9):
            self.assertIn(f'"S-{n:04d}"', text)
        self.assertNotIn('"S-0003"', text)
        self.assertIn('5 anchor(s)', text)
        self.assertIn('12 anchors on main, 7 on the branch', text)

    def test_at_most_five_anchors_are_quoted(self):
        kind, text = refine.verdict(document(stories=9), 'a short rewrite\n', 0.5)
        self.assertEqual(kind, 'rewrite')
        self.assertEqual(text.count('"'), 10)
        self.assertIn('…', text)

    def test_half_the_lines_deleted_is_a_rewrite(self):
        before = document(filler=20)
        lines = before.splitlines()
        # keep every anchor line: 60 % of the document goes, all of it prose
        prose = [i for i, l in enumerate(lines) if ': prose line' in l]
        cut = set(prose[: int(len(lines) * 0.6)])
        after = '\n'.join(l for i, l in enumerate(lines) if i not in cut) + '\n'
        self.assertFalse(refine.anchors(before) - refine.anchors(after))
        kind, text = refine.verdict(before, after, 0.5, path=SPEC)
        self.assertEqual(kind, 'rewrite')
        self.assertIn('%', text)
        self.assertIn(f'of {SPEC} on main are deleted', text)
        self.assertIn('a regeneration, not a refinement', text)

    def test_the_ratio_is_the_product_s(self):
        before = document(filler=20)
        lines = before.splitlines()
        prose = [i for i, l in enumerate(lines) if ': prose line' in l]
        cut = set(prose[: int(len(lines) * 0.6)])
        after = '\n'.join(l for i, l in enumerate(lines) if i not in cut) + '\n'
        self.assertIsNotNone(refine.verdict(before, after, 0.5))
        self.assertIsNone(refine.verdict(before, after, 0.9))

    def test_the_default_ratio_is_the_conventions(self):
        self.assertEqual(refine.DEFAULT_REWRITE_RATIO, Conventions().rewrite_ratio)
        self.assertEqual(Conventions.from_mapping({'harvest': {'rewrite_ratio': 0.9}}).rewrite_ratio, 0.9)

    def test_headings_are_compared_by_their_words(self):
        self.assertEqual(refine.anchors('##   The  design \n#### Deep\n# Title\n##### Too deep\n'),
                         {'## The design', '#### Deep'})


class RewriteBounceTest(unittest.TestCase):
    """The harvest half, on ``tests/test_harvest.py``'s product fixture: a bare origin, the
    product's checkout and a worker clone that pushes ``spec/F-0001`` as a session does."""
    setUp = test_harvest.ProductHarvestTests.setUp
    write = test_harvest.ProductHarvestTests.write
    product = test_harvest.ProductHarvestTests.product
    push_lane = test_harvest.ProductHarvestTests.push_lane
    push_main = test_harvest.ProductHarvestTests.push_main
    session = test_harvest.ProductHarvestTests.session
    harvest = test_harvest.ProductHarvestTests.harvest
    origin_main = test_harvest.ProductHarvestTests.origin_main
    origin_has = test_harvest.ProductHarvestTests.origin_has
    record = test_harvest.ProductHarvestTests.record

    def spec_on_main(self, text=None):
        self.push_main('spec(F-0001): the spec', {SPEC: text or document()})

    def held(self, lines):
        return [l for l in lines if l.startswith('held ')]

    def test_a_rewriting_spec_branch_is_held_and_never_lands(self):
        self.spec_on_main()
        self.push_lane('spec/F-0001', [('spec(F-0001): start again', {SPEC: '# Spec\n\na\nb\n'})])
        self.session('spec-f-0001', 'F-0001', 'spec/F-0001')
        before = self.origin_main()
        results, lines = self.harvest(self.product())
        self.assertEqual(results, {'spec/F-0001': 'held'})
        held = self.held(lines)
        self.assertEqual(len(held), 1, lines)
        self.assertTrue(held[0].startswith('held spec/F-0001: '), held)
        self.assertIn('anchor(s) the document on main carries are gone', held[0])
        self.assertIn('"## Records"', held[0])
        self.assertIn('"S-0001"', held[0])
        self.assertTrue(held[0].endswith(' — back to its session (round 1)'), held)
        self.assertEqual(self.origin_main(), before)
        self.assertTrue(self.origin_has('spec/F-0001'))
        rec = self.record('spec/F-0001')
        self.assertEqual(rec['correction']['kind'], 'rewrite')
        self.assertFalse(rec.get('harvested'))

    def test_the_held_branch_comes_back_as_a_correction_row(self):
        self.spec_on_main()
        self.push_lane('spec/F-0001', [('spec(F-0001): start again', {SPEC: '# Spec\n\na\nb\n'})])
        self.session('spec-f-0001', 'F-0001', 'spec/F-0001')
        self.harvest(self.product())
        correction = dict(self.record('spec/F-0001')['correction'], rounds=1, branch='spec/F-0001')
        index = {'items': {'F-0001': {'id': 'F-0001', 'type': 'feature', 'title': 'The grill',
                                      'state': 'Active', 'decided': True, 'stage': 'spec-draft'}}}
        p = self.product()
        found = rows.candidates(index, p, [], corrections={'F-0001': correction})
        corrections = [r for r in found if r.kind == rows.FIX_CORRECT]
        self.assertEqual([(r.item_id, r.brief_kind, r.branch) for r in corrections],
                         [('F-0001', 'correct', 'spec/F-0001')])
        brief = briefs.build(p, corrections[0], index, [], {})
        self.assertEqual(brief.kind, 'correct')
        self.assertIn(CORRECTION_HEAD + correction['text'], brief.text)
        self.assertIn('anchor(s) the document on main carries are gone', brief.text)

    def test_a_refining_spec_branch_lands(self):
        self.spec_on_main()
        refined = (document().replace('prose line 1', 'prose line one, said better')
                   + '\n## Appendix\n\nThe cards this spec mints.\n')
        self.push_lane('spec/F-0001', [('spec(F-0001): refine it', {SPEC: refined})])
        self.session('spec-f-0001', 'F-0001', 'spec/F-0001')
        before = self.origin_main()
        results, _lines = self.harvest(self.product())
        self.assertEqual(results, {'spec/F-0001': 'landed'})
        self.assertNotEqual(self.origin_main(), before)

    def test_a_first_spec_is_never_bounced(self):
        self.push_lane('spec/F-0001', [('spec(F-0001): the first cut', {SPEC: '# Spec\n\na\n'})])
        self.session('spec-f-0001', 'F-0001', 'spec/F-0001')
        before = self.origin_main()
        results, _lines = self.harvest(self.product())
        self.assertEqual(results, {'spec/F-0001': 'landed'})
        self.assertNotEqual(self.origin_main(), before)

    def test_a_code_branch_that_rewrites_a_file_is_not_bounced(self):
        self.push_main('seed', {'a.txt': ''.join(f'line {n}\n' for n in range(20))})
        self.push_lane('worker/T-0001', [('task(T-0001): replace it', {'a.txt': 'new\n'})])
        self.session('coder-t-0001', 'T-0001', 'worker/T-0001')
        results, _lines = self.harvest(self.product())
        self.assertEqual(results, {'worker/T-0001': 'landed'})

    def test_the_merge_refusal_still_wins(self):
        self.spec_on_main()
        self.push_lane('spec/F-0001', [('spec(F-0001): start again', {SPEC: '# Spec\n\na\nb\n'})])
        sh(['git', 'checkout', '-q', 'main'], cwd=self.worker)
        self.write(self.worker, 'm.txt', 'm\n')
        sh(['git', 'add', '-A'], cwd=self.worker)
        sh(['git', 'commit', '-qm', 'trunk moves'], cwd=self.worker)
        sh(['git', 'push', '-q', 'origin', 'main'], cwd=self.worker)
        sh(['git', 'checkout', '-q', 'spec/F-0001'], cwd=self.worker)
        sh(['git', 'merge', '-q', '--no-edit', 'main', '-m', 'spec(F-0001): merge main'],
           cwd=self.worker)
        sh(['git', 'push', '-q', 'origin', 'spec/F-0001'], cwd=self.worker)
        self.session('spec-f-0001', 'F-0001', 'spec/F-0001')
        results, lines = self.harvest(self.product())
        self.assertEqual(results, {'spec/F-0001': 'held'})
        held = self.held(lines)
        self.assertIn('merge commit on a lane branch', held[0])
        self.assertEqual(self.record('spec/F-0001')['correction']['kind'], 'merge')


if __name__ == '__main__':
    unittest.main()
