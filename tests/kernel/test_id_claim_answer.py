"""A session's ``NEEDS OPERATOR:`` question that only asks whether an id claim covers the ids it
declared is answered by the kernel from the claim refs the facts reader looked up — never left to
a person (F-0316, F-0328): covered and no collision -> "the claim stands, keep"; else "re-mint".
Anything it is unsure of stays an operator Stuck."""
import unittest

from asf.kernel import actions as A
from asf.kernel import decide as D
from asf.kernel import facts as K
from asf.kernel import idclaims as I
from asf.kernel import settings

try:
    from kernel import builders as B
    from kernel import fakes as F
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel import fakes as F

State = B.State

#: the two questions seen live on 2026-10-10, word for word (the commands trimmed)
Q_0316 = ("S-99255 is the Story the spec declares and cites, minted by the prior session of this "
          "job from the block it was given (S:99255-99304), not from this session's range "
          "(S:101005-101054). I kept it: idclaim's block is a create-only, never-deleted ref on "
          "the record repo's origin, so the claim that launch made still covers it, and the record "
          "holds no Story for F-0316 to collide with. I could not read that ref from a cloud "
          "session — the record repo is not here. Confirm the claim stands before the plan mints "
          "its Tasks, or say to re-mint the Story as S-101005")
Q_0328 = ("whether the id claim covering this spec's four Stories is still on the record's origin. "
          "D11 keeps S-91805..S-91808, minted from the block S:91805-91854 that this item's first "
          "spec session claimed, rather than re-minting from this session's block S:100705-100754 "
          "— the record holds no Story for F-0328, so none is a reuse, and re-minting would change "
          "the four Task cards the plan mints on every re-run of the spec job. The claims live as "
          "refs/asf/ids/S-91805 on the record repo, not in this product clone")
PREFIXES = ('S', 'T')
SHA = '4b9b9bc0e0f1a2b3c4d5e6f708192a3b4c5d6e7f'


def feature(iid='F-0316'):
    return B.task(iid, state=State.BUILDING, type='feature')


def asked(iid='F-0316', q=Q_0316):
    return B.session('spec-%s-1' % iid.lower(), iid, kind='spec', alive=False, ended=True,
                     result='question', status='blocked', question=q,
                     fields={'status': 'blocked'})


def claims(**ids):
    return {i.replace('_', '-'): v for i, v in ids.items()}


COVERED = claims(S_99255=('refs/asf/ids/S-99255', SHA),
                 S_101005=('refs/asf/ids/S-101005', 'abcdef0123'))


class Detect(unittest.TestCase):

    def test_both_live_questions_are_claim_questions(self):
        self.assertTrue(I.is_claim_question(Q_0316))
        self.assertTrue(I.is_claim_question(Q_0328))

    def test_other_questions_are_not(self):
        for q in ('json or yaml?', 'Should T-0001 use sqlite or postgres?',
                  'the claim in the spec is wrong; which wins?', 're-mint the logo?'):
            self.assertFalse(I.is_claim_question(q), q)

    def test_cited_ids_expand_runs_and_skip_blocks_and_the_item(self):
        self.assertEqual(I.cited(Q_0316, PREFIXES, exclude=['F-0316']), ['S-99255', 'S-101005'])
        self.assertEqual(I.cited(Q_0328, PREFIXES),
                         ['S-91805', 'S-91806', 'S-91807', 'S-91808'])
        self.assertEqual(I.cited('T-0001..T-9999 claim re-mint', PREFIXES), None, 'too many')


class Decide(unittest.TestCase):

    def plan(self, items=None, id_claims=COVERED, q=Q_0316, **cfg):
        f = B.facts(items or [feature()], sessions=[asked(q=q)], id_claims=id_claims)
        return D.decide(f, B.config(**cfg))

    def test_covered_ids_are_answered_and_the_item_moves_on(self):
        plan = self.plan()
        texts = [a.text for a in B.of(plan, A.ApplyAnswer)]
        self.assertEqual(len(texts), 1)
        self.assertIn('the claim stands', texts[0])
        self.assertIn('S-99255 by refs/asf/ids/S-99255 4b9b9bc', texts[0])
        self.assertEqual(B.of(plan, A.MarkStuck), [])
        self.assertNotEqual(B.state(plan, 'F-0316'), State.STUCK)
        self.assertEqual([c.attempt for c in B.of(plan, A.ClearStuck)],
                         [D.answer_attempt(texts[0])], 'relaunched carrying the answer')

    def test_one_claim_covering_a_run_is_named_once(self):
        f = B.facts([feature('F-0328')], sessions=[asked('F-0328', Q_0328)],
                    id_claims={'S-%d' % n: ('refs/asf/ids/S-91805', SHA)
                               for n in range(91805, 91809)})
        text = B.of(D.decide(f, B.config()), A.ApplyAnswer)[0].text
        self.assertIn('S-91805, S-91806, S-91807, S-91808 by refs/asf/ids/S-91805 4b9b9bc', text)

    def test_an_uncovered_id_is_answered_re_mint(self):
        plan = self.plan(id_claims=dict(COVERED, **{'S-99255': ''}))
        text = B.of(plan, A.ApplyAnswer)[0].text
        self.assertIn('S-99255 is covered by no claim', text)
        self.assertIn('re-mint from your current block', text)
        self.assertEqual(B.of(plan, A.MarkStuck), [])

    def test_a_record_item_under_another_parent_collides(self):
        other = B.item('S-99255', parent='F-0001', state=State.NEW)
        plan = self.plan(items=[feature(), other])
        text = B.of(plan, A.ApplyAnswer)[0].text
        self.assertIn('S-99255 already is on the record under another item', text)

    def test_its_own_minted_story_is_no_collision(self):
        own = B.item('S-99255', parent='F-0316', state=State.NEW)
        text = B.of(self.plan(items=[feature(), own]), A.ApplyAnswer)[0].text
        self.assertIn('the claim stands', text)

    def test_unread_claims_leave_the_operator_stuck(self):
        for unread in ({}, None, {'S-99255': ('refs/asf/ids/S-99255', SHA)}):
            plan = self.plan(id_claims=unread)
            self.assertEqual(B.of(plan, A.ApplyAnswer), [], unread)
            self.assertEqual(B.stuck(plan, 'F-0316').owner, 'operator')

    def test_the_knob_off_leaves_the_operator_stuck(self):
        plan = self.plan(id_claim_answer=False)
        self.assertEqual(B.of(plan, A.ApplyAnswer), [])
        self.assertEqual(B.state(plan, 'F-0316'), State.STUCK)

    def test_another_question_stays_with_the_operator(self):
        plan = self.plan(q='json or yaml for S-99255?')
        self.assertEqual(B.of(plan, A.ApplyAnswer), [])
        self.assertEqual(B.stuck(plan, 'F-0316').owner, 'operator')

    def test_a_recorded_operator_stuck_on_such_a_question_is_answered(self):
        it = B.task('F-0316', type='feature', state=State.STUCK, question=Q_0316,
                    stuck=B.M.Stuck('blocked: NEEDS OPERATOR: S-99255 is the Story…', 'operator'))
        plan = D.decide(B.facts([it], id_claims=COVERED), B.config())
        self.assertEqual(len(B.of(plan, A.ApplyAnswer)), 1)
        self.assertEqual(B.of(plan, A.ClearStuck), [], 'the applier resets a Stuck card')
        self.assertNotEqual(B.state(plan, 'F-0316'), State.STUCK)

    def test_a_recorded_reason_cut_before_the_re_mint_is_still_read(self):
        reason = ("done: NEEDS OPERATOR: the four Story ids S-99305..S-99308 come from the PRIOR "
                  "session's claimed block (S:99305-99354, session spec-f-0317-1791607356), not "
                  "this session's (S:101405-101454). They are a…")
        self.assertTrue(I.is_claim_question(reason))
        it = B.task('F-0317', type='feature', state=State.STUCK,
                    stuck=B.M.Stuck(reason, 'operator'))
        ids = {'S-%d' % n: ('refs/asf/ids/S-99305', SHA) for n in range(99305, 99309)}
        plan = D.decide(B.facts([it], id_claims=ids), B.config())
        self.assertIn('S-99305, S-99306, S-99307, S-99308 by refs/asf/ids/S-99305',
                      B.of(plan, A.ApplyAnswer)[0].text)

    def test_an_answer_already_on_the_card_is_not_given_twice(self):
        text = B.of(self.plan(), A.ApplyAnswer)[0].text
        it = B.task('F-0316', type='feature', state=State.STUCK, question=Q_0316, answers=[text],
                    stuck=B.M.Stuck('blocked: NEEDS OPERATOR: S-99255 …', 'operator'))
        plan = D.decide(B.facts([it], id_claims=COVERED), B.config())
        self.assertEqual(B.of(plan, A.ApplyAnswer), [])
        self.assertEqual(B.state(plan, 'F-0316'), State.STUCK)


class Facts(unittest.TestCase):

    def test_the_reader_looks_up_only_the_ids_a_claim_question_cites(self):
        asked_for = []

        class Record(F.FakeRecord):
            def id_claims(self, ids):
                asked_for.append(list(ids))
                return {i: ('refs/asf/ids/%s' % i, SHA) for i in ids}

        rec = Record([feature()])
        f = K.read_facts(F.ports(rec, sessions=F.FakeSessions([asked()])))
        self.assertEqual(asked_for, [['S-99255', 'S-101005']])
        self.assertEqual(f.id_claims['S-99255'], ('refs/asf/ids/S-99255', SHA))
        asked_for.clear()
        K.read_facts(F.ports(Record([feature()]),
                             sessions=F.FakeSessions([asked(q='json or yaml?')])))
        self.assertEqual(asked_for, [], 'no claim question, no lookup')

    def test_a_record_port_without_the_reader_reads_nothing(self):
        f = K.read_facts(F.ports(F.FakeRecord([feature()]),
                                 sessions=F.FakeSessions([asked()])))
        self.assertEqual(f.id_claims, {})


class Settings(unittest.TestCase):

    def test_defaults(self):
        s = settings.read(None)['stuck']
        self.assertIs(s['id_claim_answer'], True)
        self.assertEqual(list(s['id_claim_prefixes']), ['S', 'T'])


if __name__ == '__main__':
    unittest.main()
