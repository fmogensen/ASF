import unittest

from asf.record import frontmatter
from asf.groom import conflicts

FOLDER = {'decision': 'decisions', 'rule': 'rules', 'feature': 'features', 'task': 'tasks'}

BODY = '## Statement\nStuff.\n\n## Children\n\n## Backlinks\n'


def make(id_, type_, title, body, typed=(), machine=('state: New',), relpath=None):
    """One in-memory record, parsed with the record's own frontmatter reader — no file, no
    disk."""
    folder = FOLDER[type_]
    relpath = relpath or f"{folder}/{id_}.md"
    lines = [f"id: {id_}", f"type: {type_}", f"title: {title}"]
    lines.extend(typed)
    lines.append('# ---- machine ----')
    lines.extend(machine)
    text = '---\n' + '\n'.join(lines) + '\n---\n' + body
    meta, body_ = frontmatter.parse(text, path=relpath)
    return {'meta': meta, 'body': body_, 'relpath': relpath, 'text': text}


def stmt(text):
    return f"## Statement\n{text}\n\n## Children\n\n## Backlinks\n"


def shaped_body(index, statement):
    """One of the three body shapes §1.1 says this record actually holds, rotated by index."""
    shape = index % 3
    if shape == 0:
        return (f"## Statement\n{statement}\n\n## Context\nBackground for this card.\n\n"
                 "## Children\n\n## Backlinks\n")
    if shape == 1:
        return (f"## Statement\n{statement}\n\n## Why\nBecause the record says so.\n\n"
                 "## Check\nchecked by hand.\n\n## Source\nhand-written.\n\n"
                 "## Children\n\n## Backlinks\n")
    return (f"## Description\n{statement}\n\n## Acceptance\n- [ ] done\n\n## Non-goals\n\n"
            "## Children\n\n## Backlinks\n")


def overlap_pair(prefix, shared_count):
    """Two statements whose Jaccard is exactly ``shared_count / (shared_count + 2)`` — one shared
    run of tokens plus one token unique to each side, all prefixed so two different calls never
    share a token."""
    shared = ' '.join(f"{prefix}tok{k}" for k in range(shared_count))
    a_text = f"{shared} {prefix}uniquea"
    b_text = f"{shared} {prefix}uniqueb"
    score = shared_count / (shared_count + 2)
    return a_text, b_text, score


def scope_pair(prefix):
    """Two statements sharing nothing but one named object — a scope match's overlap stays low,
    its named-object Jaccard is 1.0 (one object, identical set on both sides)."""
    obj = f"{prefix}object"
    a_text = f"{prefix}alpha {prefix}bravo {prefix}charlie {prefix}delta {prefix}echo `{obj}`"
    b_text = f"{prefix}foxtrot {prefix}golf {prefix}hotel {prefix}india {prefix}juliet `{obj}`"
    return a_text, b_text


class GraphTests(unittest.TestCase):
    def test_edge_written_both_ways_is_one_edge(self):
        canonical = {
            'D-0001': make('D-0001', 'decision', 'A', BODY, typed=['superseded_by: D-0002']),
            'D-0002': make('D-0002', 'decision', 'B', BODY, typed=['supersedes: [D-0001]']),
        }
        self.assertEqual(conflicts.supersession_edges(canonical), {('D-0001', 'D-0002')})

    def test_edge_written_only_as_superseded_by(self):
        canonical = {
            'D-0001': make('D-0001', 'decision', 'A', BODY, typed=['superseded_by: D-0002']),
            'D-0002': make('D-0002', 'decision', 'B', BODY),
        }
        self.assertEqual(conflicts.supersession_edges(canonical), {('D-0001', 'D-0002')})

    def test_edge_written_only_as_supersedes(self):
        canonical = {
            'D-0002': make('D-0002', 'decision', 'B', BODY, typed=['supersedes: [D-0001]']),
        }
        self.assertEqual(conflicts.supersession_edges(canonical), {('D-0001', 'D-0002')})

    def test_edges_are_the_union_when_a_card_supersedes_three(self):
        canonical = {
            'D-0004': make('D-0004', 'decision', 'D', BODY,
                            typed=['supersedes: [D-0001, D-0002, D-0003]']),
        }
        self.assertEqual(conflicts.supersession_edges(canonical),
                          {('D-0001', 'D-0004'), ('D-0002', 'D-0004'), ('D-0003', 'D-0004')})

    def test_cycle_of_two_is_reported_once(self):
        # both entry points reach the same ring; sorted-node traversal always starts at D-0001.
        edges = {('D-0001', 'D-0002'), ('D-0002', 'D-0001')}
        self.assertEqual(conflicts.cycles(edges), [['D-0001', 'D-0002', 'D-0001']])

    def test_self_edge_is_a_cycle_of_one(self):
        self.assertEqual(conflicts.cycles({('D-0001', 'D-0001')}), [['D-0001', 'D-0001']])

    def test_three_card_ring_starts_at_its_lowest_id(self):
        edges = {('D-0001', 'D-0002'), ('D-0002', 'D-0003'), ('D-0003', 'D-0001')}
        self.assertEqual(conflicts.cycles(edges), [['D-0001', 'D-0002', 'D-0003', 'D-0001']])

    def test_two_disjoint_rings_are_two_entries(self):
        edges = {('D-0001', 'D-0002'), ('D-0002', 'D-0001'),
                 ('D-0003', 'D-0004'), ('D-0004', 'D-0003')}
        self.assertEqual(conflicts.cycles(edges),
                          [['D-0001', 'D-0002', 'D-0001'], ['D-0003', 'D-0004', 'D-0003']])

    def test_chain_of_five_is_not_a_cycle(self):
        edges = {('D-0001', 'D-0002'), ('D-0002', 'D-0003'),
                 ('D-0003', 'D-0004'), ('D-0004', 'D-0005')}
        self.assertEqual(conflicts.cycles(edges), [])

    def test_diamond_is_not_a_cycle(self):
        # D-0004 superseded by both D-0001 and D-0002, which both supersede D-0003: a DAG.
        edges = {('D-0004', 'D-0001'), ('D-0004', 'D-0002'),
                 ('D-0001', 'D-0003'), ('D-0002', 'D-0003')}
        self.assertEqual(conflicts.cycles(edges), [])

    def test_is_superseded_true_with_a_missing_successor(self):
        self.assertTrue(conflicts.is_superseded({'superseded_by': 'D-0099'}))
        self.assertFalse(conflicts.is_superseded({}))
        self.assertFalse(conflicts.is_superseded({'superseded_by': None}))


class ReaderTests(unittest.TestCase):
    def test_statement_from_migrated_decision(self):
        rec = make('D-0001', 'decision', 'A decision',
                    "## Statement\nEvery commit carries a sign-off.\n\n"
                    "## Context\nBackground.\n\n## Children\n\n## Backlinks\n")
        self.assertEqual(conflicts.statement_of(rec), 'Every commit carries a sign-off.')

    def test_statement_from_asf_new_description(self):
        rec = make('T-0001', 'task', 'A task',
                    "## Description\nDo the thing.\n\n## Acceptance\n- [ ] x\n\n"
                    "## Non-goals\n\n## Children\n\n## Backlinks\n")
        self.assertEqual(conflicts.statement_of(rec), 'Do the thing.')

    def test_statement_prefers_statement_over_description(self):
        rec = make('D-0002', 'decision', 'B',
                    "## Statement\nThe statement wins.\n\n"
                    "## Description\nThe description loses.\n\n## Children\n\n## Backlinks\n")
        self.assertEqual(conflicts.statement_of(rec), 'The statement wins.')

    def test_statement_falls_back_to_title_when_every_section_is_blank(self):
        rec = make('D-0003', 'decision', 'The title text',
                    "## Statement\n\n## Description\n\n## Children\n\n## Backlinks\n")
        self.assertEqual(conflicts.statement_of(rec), 'The title text')

    def test_statement_never_falls_back_when_one_section_has_text(self):
        rec = make('D-0004', 'decision', 'The title text',
                    "## Statement\n\n## Description\nActual text.\n\n## Children\n\n## Backlinks\n")
        self.assertEqual(conflicts.statement_of(rec), 'Actual text.')

    def test_statement_collapses_whitespace(self):
        rec = make('D-0005', 'decision', 'C',
                    "## Statement\nLine one.\n  Line two.\n\n## Children\n\n## Backlinks\n")
        self.assertEqual(conflicts.statement_of(rec), 'Line one. Line two.')

    def test_source_from_source_section(self):
        rec = make('R-0001', 'rule', 'A rule',
                    "## Statement\nStuff.\n\n## Source\nthe pre-commit hook\n\n"
                    "## Children\n\n## Backlinks\n")
        self.assertEqual(conflicts.source_of(rec), 'the pre-commit hook')

    def test_source_falls_back_to_date(self):
        rec = make('D-0006', 'decision', 'D', BODY, typed=['date: 2026-09-20'])
        self.assertEqual(conflicts.source_of(rec), '2026-09-20')

    def test_source_falls_back_to_decided_by(self):
        rec = make('D-0007', 'decision', 'D', BODY, typed=['decided_by: the operator'])
        self.assertEqual(conflicts.source_of(rec), 'the operator')

    def test_source_falls_back_to_relpath_and_is_never_empty(self):
        rec = make('D-0008', 'decision', 'D', BODY)
        self.assertEqual(conflicts.source_of(rec), 'decisions/D-0008.md')
        self.assertTrue(conflicts.source_of(rec))

    def test_scope_from_scope_field(self):
        rec = make('R-0002', 'rule', 'R', BODY, typed=['scope: tick'])
        self.assertEqual(conflicts.scope_of(rec), 'tick')

    def test_scope_falls_back_to_area(self):
        rec = make('D-0009', 'decision', 'D', BODY, typed=['area: asf/groom'])
        self.assertEqual(conflicts.scope_of(rec), 'asf/groom')

    def test_scope_casefolds(self):
        a = make('R-0003', 'rule', 'R', BODY, typed=['scope: Tick'])
        b = make('R-0004', 'rule', 'R2', BODY, typed=['scope: tick'])
        self.assertEqual(conflicts.scope_of(a), conflicts.scope_of(b))

    def test_scope_none_when_neither_is_set(self):
        rec = make('D-0010', 'decision', 'D', BODY)
        self.assertIsNone(conflicts.scope_of(rec))

    def test_named_objects_finds_the_three_shapes(self):
        text = 'see `harvest.rebase_and_resolve` and [[D-0042]] and asf/harvest/harvest.py'
        self.assertEqual(conflicts.named_objects(text),
                          {'harvest.rebase_and_resolve', 'd-0042', 'asf/harvest/harvest.py'})

    def test_named_objects_excludes_a_bare_word(self):
        self.assertEqual(conflicts.named_objects('the harvest is in'), set())

    def test_named_objects_excludes_a_float(self):
        self.assertEqual(conflicts.named_objects('a value of 3.14 here'), set())

    def test_named_objects_casefolds(self):
        self.assertEqual(conflicts.named_objects('`Harvest.py`'), conflicts.named_objects('`harvest.py`'))


class PairingTests(unittest.TestCase):
    def test_overlap_above_half_pairs(self):
        a = make('D-0101', 'decision', 'A', stmt('alpha bravo charlie delta echo'))
        b = make('D-0102', 'decision', 'B', stmt('alpha bravo charlie delta foxtrot golf'))
        score, reason = conflicts.pair_score(a, b)
        self.assertEqual(reason, 'overlap')
        self.assertAlmostEqual(score, 4 / 7)

    def test_overlap_exactly_half_does_not_pair(self):
        a = make('D-0103', 'decision', 'A',
                  stmt('shared1 shared2 shared3 shared4 uniquea1 uniquea2'))
        b = make('D-0104', 'decision', 'B',
                  stmt('shared1 shared2 shared3 shared4 uniqueb1 uniqueb2'))
        self.assertEqual(conflicts.pair_score(a, b), (0.0, None))

    def test_scope_and_shared_object_pairs_even_at_low_overlap(self):
        a_text, b_text = scope_pair('tw')
        a = make('D-0105', 'decision', 'A', stmt(a_text), typed=['scope: tick'])
        b = make('D-0106', 'decision', 'B', stmt(b_text), typed=['scope: tick'])
        score, reason = conflicts.pair_score(a, b)
        self.assertEqual(reason, 'scope')
        self.assertGreater(score, 0.0)

    def test_different_scopes_do_not_pair(self):
        a_text, b_text = scope_pair('ds')
        a = make('D-0107', 'decision', 'A', stmt(a_text), typed=['scope: tick'])
        b = make('D-0108', 'decision', 'B', stmt(b_text), typed=['scope: build'])
        self.assertEqual(conflicts.pair_score(a, b), (0.0, None))

    def test_same_scope_no_shared_object_does_not_pair(self):
        a = make('D-0109', 'decision', 'A', stmt('nsalpha nsone nstwo'), typed=['scope: tick'])
        b = make('D-0110', 'decision', 'B', stmt('nsbravo nsthree nsfour'), typed=['scope: tick'])
        self.assertEqual(conflicts.pair_score(a, b), (0.0, None))

    def test_overlap_and_scope_reason_scores_the_larger(self):
        a = make('D-0111', 'decision', 'A', stmt('alpha bravo charlie delta echo `helper.run`'),
                  typed=['scope: x'])
        b = make('D-0112', 'decision', 'B',
                  stmt('alpha bravo charlie delta foxtrot golf `helper.run`'), typed=['scope: x'])
        score, reason = conflicts.pair_score(a, b)
        self.assertEqual(reason, 'overlap+scope')
        self.assertEqual(score, 1.0)  # the named-object Jaccard, the larger of the two sub-scores

    def test_closed_card_is_not_live(self):
        a = make('D-0113', 'decision', 'A', stmt('cca alpha bravo charlie delta echo'))
        b = make('D-0114', 'decision', 'B', stmt('cca alpha bravo charlie delta foxtrot golf'),
                  machine=['state: Closed'])
        self.assertEqual(conflicts.pairs({'D-0113': a, 'D-0114': b}), [])

    def test_removed_card_is_not_live(self):
        a = make('D-0115', 'decision', 'A', stmt('crm alpha bravo charlie delta echo'))
        b = make('D-0116', 'decision', 'B', stmt('crm alpha bravo charlie delta foxtrot golf'),
                  typed=['removed: true'])
        self.assertEqual(conflicts.pairs({'D-0115': a, 'D-0116': b}), [])

    def test_superseded_card_is_not_live(self):
        a = make('D-0117', 'decision', 'A', stmt('csu alpha bravo charlie delta echo'))
        b = make('D-0118', 'decision', 'B', stmt('csu alpha bravo charlie delta foxtrot golf'),
                  typed=['superseded_by: D-0999'])
        self.assertEqual(conflicts.pairs({'D-0117': a, 'D-0118': b}), [])

    def test_feature_and_task_are_not_live(self):
        a = make('D-0119', 'decision', 'A', stmt('cft alpha bravo charlie delta echo'))
        f = make('F-0001', 'feature', 'F', stmt('cft alpha bravo charlie delta foxtrot golf'))
        t = make('T-0001', 'task', 'T', stmt('cft alpha bravo charlie delta foxtrot hotel'))
        self.assertEqual(conflicts.pairs({'D-0119': a, 'F-0001': f, 'T-0001': t}), [])

    def test_pair_joined_by_supersession_edge_is_not_proposed(self):
        a = make('D-0120', 'decision', 'A', stmt('cje alpha bravo charlie delta echo'))
        b = make('D-0121', 'decision', 'B', stmt('cje alpha bravo charlie delta foxtrot golf'),
                  typed=['supersedes: [D-0120]'])
        # D-0120 carries no superseded_by, so is_superseded(D-0120) is False and it stays live —
        # it is the pairs()-only join filter that must exclude this pair, not live().
        self.assertFalse(conflicts.is_superseded(a['meta']))
        self.assertEqual(conflicts.pairs({'D-0120': a, 'D-0121': b}), [])

    def test_pair_declined_by_one_card_is_not_proposed(self):
        a = make('D-0122', 'decision', 'A', stmt('cd1 alpha bravo charlie delta echo'),
                  typed=['conflict_declined: [D-0122+D-0123]'])
        b = make('D-0123', 'decision', 'B', stmt('cd1 alpha bravo charlie delta foxtrot golf'))
        self.assertEqual(conflicts.pairs({'D-0122': a, 'D-0123': b}), [])

    def test_pair_declined_by_both_cards_is_not_proposed(self):
        a = make('D-0124', 'decision', 'A', stmt('cd2 alpha bravo charlie delta echo'),
                  typed=['conflict_declined: [D-0124+D-0125]'])
        b = make('D-0125', 'decision', 'B', stmt('cd2 alpha bravo charlie delta foxtrot golf'),
                  typed=['conflict_declined: [D-0124+D-0125]'])
        self.assertEqual(conflicts.pairs({'D-0124': a, 'D-0125': b}), [])


class TopTwentyTests(unittest.TestCase):
    def test_six_planted_conflicts_and_nothing_else(self):
        canonical = {}
        index = [0]

        def add(id_, type_, statement, typed=()):
            canonical[id_] = make(id_, type_, f"Card {id_}", shaped_body(index[0], statement),
                                   typed=typed)
            index[0] += 1

        planted = set()
        for i, shared_count in enumerate((10, 7, 5)):
            a_text, b_text, _score = overlap_pair(f"ov{i}", shared_count)
            a_id, b_id = f"D-40{i}0", f"D-40{i}1"
            add(a_id, 'decision', a_text)
            add(b_id, 'decision', b_text)
            planted.add(tuple(sorted((a_id, b_id))))

        for i in range(3):
            a_text, b_text = scope_pair(f"sc{i}")
            a_id, b_id = f"R-41{i}0", f"R-41{i}1"
            add(a_id, 'rule', a_text, typed=[f"scope: scope{i}"])
            add(b_id, 'rule', b_text, typed=[f"scope: scope{i}"])
            planted.add(tuple(sorted((a_id, b_id))))

        for i in range(18):
            words = ' '.join(f"ctrl{i}w{k}" for k in range(5))
            add(f"D-42{i:02d}", 'decision', words)
        for i in range(2):
            words = ' '.join(f"ctrlr{i}w{k}" for k in range(5))
            add(f"R-43{i:02d}", 'rule', words)

        self.assertEqual(len(canonical), 32)
        self.assertEqual(sum(1 for r in canonical.values() if r['meta']['type'] == 'decision'), 24)
        self.assertEqual(sum(1 for r in canonical.values() if r['meta']['type'] == 'rule'), 8)

        result = conflicts.pairs(canonical)
        self.assertEqual({(p.a, p.b) for p in result}, planted)
        scores = [p.score for p in result]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_top_twenty_at_known_distinct_scores(self):
        canonical = {}
        planted = []
        for j in range(25):
            shared_count = 10 + j
            a_text, b_text, score = overlap_pair(f"p{j}", shared_count)
            a_id, b_id = f"D-50{j:02d}", f"D-51{j:02d}"
            canonical[a_id] = make(a_id, 'decision', f"Card {a_id}", shaped_body(j, a_text))
            canonical[b_id] = make(b_id, 'decision', f"Card {b_id}", shaped_body(j, b_text))
            planted.append((tuple(sorted((a_id, b_id))), score))

        planted.sort(key=lambda kv: -kv[1])
        expected_top20 = [pair for pair, _score in planted[:20]]

        result = conflicts.pairs(canonical)
        self.assertEqual({(p.a, p.b) for p in result}, {pair for pair, _s in planted})
        self.assertEqual([(p.a, p.b) for p in result[:20]], expected_top20)
        scores = [p.score for p in result]
        self.assertEqual(scores, sorted(scores, reverse=True))


if __name__ == '__main__':
    unittest.main()
