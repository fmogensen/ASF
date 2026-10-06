"""`events` is the sixth validated stream (F-0064 Task 1 / T-0332): the schema, the natural key,
`product` on every line, one product per file, and `asf.metrics.log.emit` as the one append path.
"""
import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import env
from asf.metrics import log, metrics
from asf.tick import tick
from tests.test_tick import TickTestCase

ITEMS = {'T-0001': {'id': 'T-0001', 'type': 'task'}}


def ev(**kw):
    base = {'kind': 'launch', 'key': 'k1'}
    base.update(kw)
    return base


class EventSchemaTests(unittest.TestCase):
    def test_kind_and_key_are_required(self):
        with self.assertRaises(metrics.SchemaError) as cm:
            metrics.validate('events', {}, {})
        self.assertIn("missing required key 'kind'", str(cm.exception))
        with self.assertRaises(metrics.SchemaError) as cm:
            metrics.validate('events', {'kind': 'launch'}, {})
        self.assertIn("missing required key 'key'", str(cm.exception))

    def test_every_head_key_is_typed(self):
        for key, bad, needle in (
            ('kind', 1, "'kind' must be str"),
            ('key', 1, "'key' must be str"),
            ('product', 1, "'product' must be str|null"),
            ('tick', 'x', "'tick' must be int|null"),
            ('item', 1, "'item' must be str|null"),
            ('job', 1, "'job' must be str|null"),
            ('account', 1, "'account' must be str|null"),
            ('branch', 1, "'branch' must be str|null"),
            ('text', 1, "'text' must be str|null"),
            ('fields', 1, "'fields' must be dict"),
        ):
            with self.assertRaises(metrics.SchemaError) as cm:
                metrics.validate('events', ev(**{key: bad}), {})
            self.assertIn(needle, str(cm.exception))

    def test_kind_outside_kinds_is_refused(self):
        with self.assertRaises(metrics.SchemaError) as cm:
            metrics.validate('events', ev(kind='bogus'), {})
        self.assertIn("'kind' must be one of", str(cm.exception))

    def test_fields_defaults_to_empty_and_accepts_anything_json(self):
        out = metrics.validate('events', ev(), {})
        self.assertEqual(out['fields'], {})
        out = metrics.validate('events', ev(fields={'a': [1, 'x', None, {'b': 2}]}), {})
        self.assertEqual(out['fields'], {'a': [1, 'x', None, {'b': 2}]})

    def test_product_is_filled_from_the_argument_and_none_without_one(self):
        out = metrics.validate('events', ev(), {})
        self.assertIsNone(out['product'])
        out = metrics.validate('events', ev(), {}, product='sample')
        self.assertEqual(out['product'], 'sample')

    def test_tick_is_nullable(self):
        out = metrics.validate('events', ev(), {})
        self.assertIsNone(out['tick'])
        out = metrics.validate('events', ev(tick=640), {})
        self.assertEqual(out['tick'], 640)

    def test_a_four_digit_item_id_is_checked_against_the_index(self):
        out = metrics.validate('events', ev(item='T-0001'), ITEMS)
        self.assertEqual(out['item'], 'T-0001')
        with self.assertRaises(metrics.SchemaError) as cm:
            metrics.validate('events', ev(item='T-9999'), ITEMS)
        self.assertIn('is not in index.json', str(cm.exception))

    def test_an_inbox_item_and_a_five_digit_id_are_carried_through_unrefused(self):
        out = metrics.validate('events', ev(item='inbox:some-file.md'), ITEMS)
        self.assertEqual(out['item'], 'inbox:some-file.md')
        out = metrics.validate('events', ev(item='T-10000'), ITEMS)
        self.assertEqual(out['item'], 'T-10000')


class NaturalKeyTests(unittest.TestCase):
    def test_the_natural_key_is_kind_and_key(self):
        out = metrics.validate('events', ev(kind='launch', key='job-1'), {})
        self.assertEqual(metrics.natural_key('events', out), ('launch', 'job-1'))

    def test_ticks_still_falls_through_to_its_own_branch(self):
        tick_ev = {'tick': 640, 'launches': 0, 'merges': 0, 'stalls': 0, 'refusals': 0, 'relaunches': 0}
        out = metrics.validate('ticks', tick_ev, {})
        self.assertEqual(metrics.natural_key('ticks', out), (640,))


class OneProductPerFileTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='events_test_')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_two_products_in_one_day_file_are_refused(self):
        a = metrics.validate('events', ev(key='a'), {}, product='alpha')
        metrics.append_event(self.root, 'events', a)
        b = metrics.validate('events', ev(key='b'), {}, product='beta')
        with self.assertRaises(metrics.SchemaError) as cm:
            metrics.append_event(self.root, 'events', b)
        rel = os.path.join('metrics', 'events', f"{a['ts'][:10]}.jsonl")
        self.assertEqual(str(cm.exception),
                          f"events: {rel} holds product 'alpha' — a stream file never mixes products")

    def test_a_legacy_null_product_line_is_ignored_by_the_guard(self):
        legacy = metrics.validate('events', ev(key='legacy'), {})
        metrics.append_event(self.root, 'events', legacy)
        b = metrics.validate('events', ev(key='b'), {}, product='beta')
        metrics.append_event(self.root, 'events', b)  # does not raise
        path = metrics.stream_path(self.root, 'events', b['ts'][:10])
        with open(path, encoding='utf-8') as f:
            self.assertEqual(len([l for l in f if l.strip()]), 2)

    def test_the_guard_reads_the_file_no_more_than_before(self):
        a = metrics.validate('events', ev(key='a'), {}, product='alpha')
        metrics.append_event(self.root, 'events', a)
        b = metrics.validate('events', ev(key='b'), {}, product='alpha')
        calls = []
        orig = metrics.read_file

        def counting(path):
            calls.append(path)
            return orig(path)

        with mock.patch.object(metrics, 'read_file', counting):
            metrics.append_event(self.root, 'events', b)
        self.assertEqual(len(calls), 1)


class OneAppendPathTests(TickTestCase):
    """`asf.metrics.log.emit` as the one append path: validate, swallow a `SchemaError`, append."""

    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix='events_test_')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_a_bad_kind_is_printed_and_swallowed(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = log.emit(self.root, 'sample', 'bogus', 'k1')
        self.assertIsNone(out)
        self.assertIn("'kind' must be one of", err.getvalue())
        self.assertFalse(os.path.isdir(os.path.join(self.root, 'metrics', 'events')))

    def test_a_tick_s_event_carries_the_tick_and_product(self):
        out = log.emit(self.root, 'sample', 'launch', 'job-1', tick=640, item='T-0001')
        self.assertEqual((out['tick'], out['product']), (640, 'sample'))

    def test_an_event_with_no_tick_carries_tick_null(self):
        out = log.emit(self.root, 'sample', 'launch', 'job-2')
        self.assertIsNone(out['tick'])

    def test_two_identical_events_in_one_day_leave_one_line(self):
        first = log.emit(self.root, 'sample', 'launch', 'job-3')
        second = log.emit(self.root, 'sample', 'launch', 'job-3')
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        path = metrics.stream_path(self.root, 'events', first['ts'][:10])
        with open(path, encoding='utf-8') as f:
            self.assertEqual(len([l for l in f if l.strip()]), 1)

    def test_the_stored_event_is_what_ctx_events_holds(self):
        product = env.load_product('sample')
        ctx = tick.Context(product)
        out = ctx.event('launch', key='job-9', item='T-0001')
        self.assertIsNotNone(out)
        self.assertEqual(ctx.events, [out])
        self.assertEqual((out['kind'], out['key'], out['item'], out['product']),
                         ('launch', 'job-9', 'T-0001', 'sample'))

    def test_a_refused_event_is_not_added_to_ctx_events(self):
        product = env.load_product('sample')
        ctx = tick.Context(product)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = ctx.event('bogus', key='job-10')
        self.assertIsNone(out)
        self.assertEqual(ctx.events, [])


if __name__ == '__main__':
    unittest.main()
