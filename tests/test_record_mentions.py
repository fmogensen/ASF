"""compute_derived's backlinks: one token scan per text gives exactly what the pairwise
per-id regex gave (the record step's former hot spot)."""
import re
import unittest

from asf.record import core


def _rec(iid, body='', parent=None, **meta):
    m = {'id': iid, 'type': 'task', 'title': iid}
    if parent:
        m['parent'] = parent
    m.update(meta)
    return {'meta': m, 'body': body, 'folder': 'tasks'}


def _pairwise(canonical):
    """The former implementation: one regex per id, searched in every other item's text."""
    texts = {iid: core.scan_text(rec) for iid, rec in canonical.items()}
    out = {}
    for iid in canonical:
        rx = re.compile(r'(?<![A-Za-z0-9])' + re.escape(iid) + r'(?![A-Za-z0-9])')
        out[iid] = sorted((o for o in canonical if o != iid and rx.search(texts[o])),
                          key=lambda b: core.sort_key(canonical, b))
    return out


class BacklinkTests(unittest.TestCase):
    def canonical(self):
        items = [
            _rec('F-0001', 'the feature'),
            _rec('T-0001', 'after F-0001; see T-0002.', parent='F-0001'),
            _rec('T-0002', 'xT-0001 T-00012 T-0001a F-0001-2 (T-0003)'),
            _rec('T-0003', '## Backlinks\n- T-0002\n', notes='mentions T-0002 in meta'),
            _rec('T-0004', 'F-0001_x and "T-0003" and t-0002'),
            _rec('odd-id', 'names T-0004'),
            _rec('T-0005', 'the odd-id card, and odd-idx is not it'),
        ]
        return {r['meta']['id']: r for r in items}

    def test_token_scan_equals_the_pairwise_regex(self):
        canonical = self.canonical()
        derived = core.compute_derived(canonical)
        want = _pairwise(canonical)
        for iid, d in derived.items():
            excluded = set(d['children'])
            self.assertEqual(d['backlinks'], [b for b in want[iid] if b not in excluded], iid)

    def test_boundaries(self):
        d = core.compute_derived(self.canonical())
        self.assertEqual(d['T-0001']['backlinks'], [])  # xT-0001, T-00012, T-0001a are not it
        self.assertEqual(d['F-0001']['children'], ['T-0001'])
        self.assertEqual(d['F-0001']['backlinks'], ['T-0002', 'T-0004'])  # F-0001-2, F-0001_x
        self.assertEqual(d['T-0002']['backlinks'], ['T-0001', 'T-0003'])  # meta counts
        self.assertEqual(d['T-0003']['backlinks'], ['T-0002', 'T-0004'])
        self.assertEqual(d['odd-id']['backlinks'], ['T-0005'])  # an id of another shape: its regex


if __name__ == '__main__':
    unittest.main()
