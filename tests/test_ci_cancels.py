"""asf.ci_cancels — the nine causes and their groups, and the ledger this module reads (never
writes). `Claims` writes through the landed `ci_queue.claim_cancel` into a tempfile state dir and
reads back through `ci_cancels.claims()`; no `gh` and no network anywhere in this file."""
import datetime
import json
import os
import re
import shutil
import tempfile
import unittest

from asf import ci_cancels, ci_queue


def _literal_causes(path):
    """Every cause string a `claim_cancel(...)` call in `path` passes literally — the third
    positional argument, which may be a plain string or an `'a' if cond else 'b'` ternary. A call
    whose cause is a variable (the `def claim_cancel(...)` line itself) is not matched."""
    with open(path, encoding='utf-8') as f:
        text = f.read()
    found = set()
    for m in re.finditer(
            r"claim_cancel\(\s*[^,\n]+,\s*[^,\n]+,\s*"
            r"('[^']*'(?:\s+if\s+.+?\s+else\s+'[^']*')?)", text):
        found |= set(re.findall(r"'([^']+)'", m.group(1)))
    return found


class Causes(unittest.TestCase):
    def test_the_nine_causes_and_their_groups(self):
        self.assertEqual(ci_cancels.CAUSES,
                          ('relief', 'stall', 'dedupe', 'merged', 'timeout', 'rewrite', 'newhead',
                           'unresolved', 'unclaimed'))
        seen = set()
        for causes in ci_cancels.GROUPS.values():
            self.assertFalse(seen & set(causes), 'a cause named in two groups')
            seen |= set(causes)
        self.assertEqual(seen, set(ci_cancels.CAUSES), 'GROUPS does not partition CAUSES exactly')

    def test_every_landed_cause_string_is_honoured_or_reread(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        found = set()
        for rel in ('asf/ci_queue.py', 'asf/harvest/lane.py'):
            found |= _literal_causes(os.path.join(root, rel))
        self.assertTrue(found, 'no claim_cancel(...) cause literal was found — the regex needs a '
                         'second look, not a green test on an empty match')
        known = set(ci_cancels.HONOURED) | ci_cancels.REREAD
        self.assertEqual(found, known,
                          f'{found ^ known} — a cause string landed in asf/ci_queue.py or '
                          f'asf/harvest/lane.py that F-0230 PD2 has not decided the partition of')


class Claims(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_claim_round_trips_and_the_newest_line_per_run_wins(self):
        ci_queue.claim_cancel(self.tmp, 7, 'relief')
        ci_queue.claim_cancel(self.tmp, 7, 'stall')
        got = ci_cancels.claims(self.tmp)
        self.assertEqual(got['7']['cause'], 'stall')

    def test_the_ledger_is_bounded_by_age_not_line_count(self):
        """Replaces the spec's line-count fence (F-0230 PD4): the landed ledger prunes by
        ci_queue.CLAIM_TTL_S, not by a line count, so this asserts age-based pruning instead and
        that this module mints no MAX_CLAIM_LINES of its own."""
        self.assertFalse(hasattr(ci_cancels, 'MAX_CLAIM_LINES'))
        t0 = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        ci_queue.claim_cancel(self.tmp, 1, 'relief', t0)
        inside = t0 + datetime.timedelta(seconds=ci_queue.CLAIM_TTL_S - 60)
        ci_queue.claim_cancel(self.tmp, 2, 'stall', inside)
        past = t0 + datetime.timedelta(seconds=ci_queue.CLAIM_TTL_S + 60)
        ci_queue.claim_cancel(self.tmp, 3, 'relief', past)
        # run 1 is older than CLAIM_TTL_S as of the write at `past` and is dropped by it; run 2
        # is still inside the window at that same write and survives.
        self.assertEqual(set(ci_cancels.claims(self.tmp)), {'2', '3'})

    def test_an_unwritable_state_dir_never_raises_and_reads_back_empty(self):
        path = os.path.join(self.tmp, 'not-a-dir')
        open(path, 'w', encoding='utf-8').close()
        ci_queue.claim_cancel(path, 1, 'relief')
        self.assertEqual(ci_cancels.claims(os.path.join(self.tmp, 'missing')), {})

    def test_a_corrupt_line_is_dropped_not_fatal(self):
        """Rewritten to the landed format (F-0230 PD1): the store is one JSON object, not JSONL,
        so the fence's subject is a corrupt file and a corrupt entry, not a corrupt line."""
        path = os.path.join(self.tmp, ci_queue.CANCELS_FILE)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('{oops')
        self.assertEqual(ci_cancels.claims(self.tmp), {})
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'1': {'cause': 'relief', 'at': '2026-01-01T00:00:00Z'}, '2': 'not-a-dict'},
                      f)
        self.assertEqual(set(ci_cancels.claims(self.tmp)), {'1'})
