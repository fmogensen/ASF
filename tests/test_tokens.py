import argparse
import contextlib
import io
import json
import os
import re
import signal
import tempfile
import unittest
from unittest import mock

from asf import env
from asf import tokens as tk
from asf.env import Product
from asf.metrics import metrics
from asf.views import tokens
from asf.workers import lifecycle
from asf.workers import pool as pool_mod
from asf.workers import runtime as runtime_mod
from asf.workers import stall as stall_mod

from tests.test_workers import Home


def write_day(root, day, rows):
    d = os.path.join(root, 'metrics', 'sessions')
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, day + '.jsonl'), 'w', encoding='utf-8') as f:
        for r in rows:
            f.write((r if isinstance(r, str) else json.dumps(r)) + '\n')


def row(kind, in_tokens, turns):
    return {'kind': kind, 'in_tokens': in_tokens, 'turns': turns}


class TokensViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def by_kind(self, split='2026-09-23', days=14):
        return {r['kind']: r for r in tokens.compute(self.root, split, days)}

    def test_windows(self):
        before, after = tokens.windows('2026-09-23', 3)
        self.assertEqual(before, ['2026-09-20', '2026-09-21', '2026-09-22'])
        self.assertEqual(after, ['2026-09-23', '2026-09-24', '2026-09-25'])

    def test_split_day_counts_as_after(self):
        write_day(self.root, '2026-09-22', [row('code', 1000, 10)])
        write_day(self.root, '2026-09-23', [row('code', 250, 4)])
        r = self.by_kind()['code']
        self.assertEqual((r['before'], r['after']), (1, 1))
        self.assertEqual(r['delta_pct'], -75)

    def test_median_not_mean(self):
        write_day(self.root, '2026-09-20', [row('code', 100, 1), row('code', 200, 3), row('code', 12000000, 5)])
        write_day(self.root, '2026-09-24', [row('code', 100, 1)])
        r = self.by_kind()['code']
        self.assertEqual(r['before_in'], 200)
        self.assertEqual(r['before_turns'], 3)

    def test_all_row_spans_kinds(self):
        write_day(self.root, '2026-09-20', [row('code', 100, 1), row('spec', 300, 1)])
        rows = tokens.compute(self.root, '2026-09-23', 14)
        self.assertEqual([r['kind'] for r in rows], ['code', 'spec', 'all'])
        self.assertEqual(rows[-1]['before'], 2)
        self.assertEqual(rows[-1]['before_in'], 200)

    def test_empty_window_prints_dashes_and_no_delta(self):
        write_day(self.root, '2026-09-20', [row('code', 100, 1)])
        r = self.by_kind()['code']
        self.assertIsNone(r['after_in'])
        self.assertIsNone(r['delta_pct'])
        out = tokens.render(tokens.compute(self.root, '2026-09-23', 14), '2026-09-23', 14)
        self.assertIn('| code | 1 | 100 | 0 | — | — | 1 → — |', out)

    def test_zero_before_has_no_delta(self):
        write_day(self.root, '2026-09-20', [row('code', 0, 1)])
        write_day(self.root, '2026-09-24', [row('code', 50, 1)])
        self.assertIsNone(self.by_kind()['code']['delta_pct'])

    def test_no_files_and_bad_lines_are_not_errors(self):
        self.assertEqual([r['kind'] for r in tokens.compute(self.root, '2026-09-23', 14)], ['all'])
        write_day(self.root, '2026-09-20', ['not json', '', row('code', 10, 2)])
        self.assertEqual(self.by_kind()['code']['before'], 1)

    def test_rows_without_the_keys_count_but_have_no_reading(self):
        write_day(self.root, '2026-09-20', [{'kind': 'code'}])
        r = self.by_kind()['code']
        self.assertEqual(r['before'], 1)
        self.assertIsNone(r['before_in'])

    def test_table_shape(self):
        write_day(self.root, '2026-09-20', [row('code', 4118000, 78)])
        write_day(self.root, '2026-09-24', [row('code', 1050000, 31)])
        out = tokens.render(tokens.compute(self.root, '2026-09-23', 14), '2026-09-23', 14)
        self.assertTrue(out.startswith('TOKENS — input tokens per session, 14 d either side of 2026-09-23\n'))
        self.assertIn('| code | 1 | 4,118,000 | 1 | 1,050,000 | −75 % | 78 → 31 |', out)

    def test_json_is_unformatted(self):
        write_day(self.root, '2026-09-20', [row('code', 100, 1)])
        args = argparse.Namespace(split='2026-09-23', days=14, json=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(tokens.cmd_tokens(args, self.root), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data[0]['before_in'], 100)
        self.assertIsNone(data[0]['delta_pct'])


def usage(i=None, o=None, cr=None, cw=None):
    u = {'input_tokens': i, 'output_tokens': o, 'cache_read_input_tokens': cr,
         'cache_creation_input_tokens': cw}
    return {k: v for k, v in u.items() if v is not None}


def assistant(**kw):
    return {'type': 'assistant', 'message': {'usage': usage(**kw)}}


INIT = {'type': 'system', 'subtype': 'init'}
NONE4 = {'input': None, 'output': None, 'cache_read': None, 'cache_write': None}


class LogCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def log(self, lines):
        path = os.path.join(self.tmp.name, 'job.jsonl')
        with open(path, 'w', encoding='utf-8') as f:
            for line in lines:
                f.write((line if isinstance(line, str) else json.dumps(line)) + '\n')
        return path


class UsageTest(unittest.TestCase):
    FOUR = {'input_tokens': 12, 'output_tokens': 3, 'cache_read_input_tokens': 900,
            'cache_creation_input_tokens': 40}
    DIMS = {'input': 12, 'output': 3, 'cache_read': 900, 'cache_write': 40}

    def test_assistant_line(self):
        rec = {'type': 'assistant', 'message': {'usage': self.FOUR}}
        self.assertEqual(tk.usage_of(rec), self.DIMS)

    def test_result_line(self):
        self.assertEqual(tk.usage_of({'type': 'result', 'usage': self.FOUR}), self.DIMS)
        self.assertEqual(tk.of_result({'type': 'result', 'usage': self.FOUR}), self.DIMS)

    def test_missing_and_bad_values_are_none_not_zero(self):
        rec = {'type': 'result', 'usage': {'input_tokens': 'lots', 'output_tokens': -1,
                                           'cache_read_input_tokens': True}}
        self.assertEqual(tk.usage_of(rec), NONE4)
        rec = {'type': 'result', 'usage': {'input_tokens': 0}}
        self.assertEqual(tk.usage_of(rec), dict(NONE4, input=0))

    def test_a_line_that_is_not_a_usage_carrier(self):
        for rec in (INIT, [], None, 'x', {'type': 'assistant', 'message': 'x'},
                    {'type': 'result', 'usage': [1]}):
            self.assertEqual(tk.usage_of(rec), NONE4, rec)


class MeterTest(LogCase):
    def three(self):
        return [INIT, assistant(i=10, o=1, cr=100, cw=5), assistant(i=20, o=2, cr=200),
                assistant(i=30, o=3, cr=300, cw=7)]

    def test_one_pass_gives_the_result_and_the_tally(self):
        result = {'type': 'result', 'result': 'done'}
        m = tk.meter(self.log(self.three() + [result]))
        self.assertEqual(m.by_dim, {'input': 60, 'output': 6, 'cache_read': 600, 'cache_write': 12})
        self.assertEqual(m.result, result)
        self.assertEqual(m.runs, 1)
        self.assertEqual(m.lines, 5)

    def test_the_results_usage_wins(self):
        result = {'type': 'result', 'usage': usage(i=1000, o=90, cr=5000, cw=50)}
        m = tk.meter(self.log(self.three() + [result]))
        self.assertEqual(m.by_dim, {'input': 1000, 'output': 90, 'cache_read': 5000, 'cache_write': 50})

    def test_a_dimension_stays_none_until_a_number_arrives(self):
        m = tk.meter(self.log([INIT, assistant(i=5), assistant(i=0, o=0)]))
        self.assertEqual(m.by_dim, {'input': 5, 'output': 0, 'cache_read': None, 'cache_write': None})

    def test_only_the_last_run_is_metered(self):
        m = tk.meter(self.log([INIT, assistant(i=100), {'type': 'result'}, INIT, assistant(i=7)]))
        self.assertEqual(m.by_dim['input'], 7)
        self.assertEqual(m.runs, 2)
        self.assertIsNone(m.result)

    def test_garbage_and_missing_file(self):
        path = self.log([INIT, '', 'not json {', '[1, 2]', assistant(i=4), assistant(i=6)])
        m = tk.meter(path)
        self.assertEqual(m.by_dim['input'], 10)
        self.assertEqual(m.lines, 3)
        gone = tk.meter('/nope')
        self.assertEqual((gone.result, gone.by_dim, gone.lines, gone.runs), (None, NONE4, 0, 0))

    def test_invalid_utf8_is_replaced_not_raised(self):
        path = os.path.join(self.tmp.name, 'bad.jsonl')
        with open(path, 'wb') as f:
            f.write(b'\xff\xfe not json\n' + json.dumps(assistant(i=3)).encode() + b'\n')
        self.assertEqual(tk.meter(path).by_dim['input'], 3)

    def test_nothing_sums_dimensions(self):
        m = tk.meter(self.log(self.three()))
        self.assertEqual(tuple(m.by_dim), tk.DIMENSIONS)
        self.assertFalse([k for k in m.by_dim if re.search(r'total|sum|tokens$', k)])


class CapsTest(unittest.TestCase):
    def product(self, block=None):
        return Product('sample', {} if block is None else {'token_caps': block})

    def test_defaults_when_no_block(self):
        p = self.product()
        self.assertEqual(tk.caps(p), tk.DEFAULT_CAPS)
        self.assertEqual(tk.cap_for(p, 'spec')['input'], 8_000_000)

    def test_the_kinds_are_the_streams_kinds(self):
        self.assertEqual(tk.KINDS, metrics.KINDS)

    def test_a_kind_narrows_one_dimension(self):
        d = tk.DEFAULT_CAPS['default']
        p = self.product({'default': {'output': 10}, 'spec': {'input': 5}})
        self.assertEqual(tk.cap_for(p, 'spec'), dict(d, output=10, input=5))
        self.assertEqual(tk.cap_for(p, 'review'), dict(d, output=10))

    def test_a_kind_with_no_row_gets_the_default(self):
        self.assertEqual(tk.cap_for(self.product(), 'nothing-like-a-kind'), tk.DEFAULT_CAPS['default'])

    def test_off_is_uncapped(self):
        p = self.product({'spec': {'cache_read': 'off'}})
        cap = tk.cap_for(p, 'spec')
        self.assertIsNone(cap['cache_read'])
        self.assertIsNone(tk.over({'cache_read': 10 ** 12}, cap))

    def test_every_bad_entry_is_named(self):
        p = self.product({'nonsense': {'input': 1}, 'spec': {'inptu': 1}, 'review': {'input': 0},
                          'plan': {'output': 'big'}, 'code': 7})
        with self.assertRaises(tk.TokenCapError) as cm:
            tk.caps(p)
        for name in ('nonsense', 'inptu', 'review', 'plan', 'code'):
            self.assertIn(name, str(cm.exception))

    def test_a_block_that_is_not_a_map_is_refused(self):
        with self.assertRaises(tk.TokenCapError):
            tk.caps(self.product(['spec']))

    def test_a_bool_is_not_a_cap(self):
        with self.assertRaises(tk.TokenCapError):
            tk.caps(self.product({'spec': {'input': True}}))

    def test_over_is_the_first_dimension_in_order(self):
        cap = {'input': 10, 'output': 10, 'cache_read': 10, 'cache_write': 10}
        by_dim = {'input': 11, 'output': 99, 'cache_read': 5, 'cache_write': 50}
        self.assertEqual(tk.over(by_dim, cap), ('input', 11, 10))
        self.assertEqual(tk.over(dict(by_dim, input=10), cap), ('output', 99, 10))
        self.assertIsNone(tk.over({'input': 10}, cap))

    def test_none_is_never_over(self):
        self.assertIsNone(tk.over({'input': None}, {'input': 1}))
        self.assertIsNone(tk.over({'input': 10 ** 12}, {'input': None}))

    def test_the_verdict_is_written_once(self):
        text = tk.cap_text('spec', 'input', 8123456, 8000000)
        self.assertEqual(text, 'token cap: input 8123456 over the 8000000 cap for a spec job')
        by_dim = dict(NONE4, input=8123456, output=41233)
        rec = tk.cap_result('spec', 'input', 8123456, 8000000, by_dim, '2026-09-23T09:12:03Z')
        self.assertEqual(rec['type'], 'result')
        self.assertTrue(rec['is_error'])
        self.assertTrue(rec['result'].startswith(text))
        self.assertEqual(rec['usage'], {'input_tokens': 8123456, 'output_tokens': 41233})
        self.assertEqual(rec['asf']['cap'], {'kind': 'spec', 'dimension': 'input', 'tokens': 8123456,
                                            'limit': 8000000, 'at': '2026-09-23T09:12:03Z'})
        self.assertNotIn('duration_ms', rec)
        self.assertNotIn('total_cost_usd', rec)

    def test_token_caps_is_a_product_field(self):
        text = 'product: sample\ntoken_caps:\n  default:\n    input: 8000000\n  spec:\n    cache_read: off\n'
        self.assertEqual(env.validate_product_text(text), [])
        p = self.product(env.loads(text)['token_caps'])
        self.assertIsNone(tk.cap_for(p, 'spec')['cache_read'])
        self.assertEqual(tk.cap_for(p, 'spec')['input'], 8_000_000)


class CapStopTest(Home):
    """``stall.capped``: a live run over one dimension's cap is signalled, gets a cap result line
    and is marked on its registry line. ``_signal`` is patched, so no process is ever signalled."""
    PID = 424242
    JOB = 'spec-f-0001'

    def setUp(self):
        super().setUp()
        self.log_path = os.path.join(self.tmp, 'job.jsonl')
        self.signals = []
        self.on_signal = None
        self.gone = False
        for target, name, new in ((stall_mod, '_signal', self.fake_signal),
                                  (stall_mod.time, 'sleep', lambda _s: None)):
            p = mock.patch.object(target, name, new)
            p.start()
            self.addCleanup(p.stop)

    def fake_signal(self, pid, sig):
        self.signals.append((pid, sig))
        if self.on_signal:
            self.on_signal(sig)

    def alive(self, pid):
        return not self.gone and pid == self.PID

    def write_log(self, lines):
        with open(self.log_path, 'w', encoding='utf-8') as f:
            for line in lines:
                f.write((line if isinstance(line, str) else json.dumps(line)) + '\n')

    def read_bytes(self):
        with open(self.log_path, 'rb') as f:
            return f.read()

    def over_input(self):
        return [INIT, assistant(i=6_000_000, o=10), assistant(i=3_000_000, o=10)]

    def launch(self, **extra):
        rec = {'job': self.JOB, 'kind': 'spec', 'pid': self.PID, 'log': self.log_path,
               'started': '2026-09-24T10:00:00Z', 'session': f'sample/{self.JOB}@20260924T100000Z'}
        pool_mod.append_session(self.product, dict(rec, **extra))

    def run_of(self):
        return pool_mod.load_sessions(self.product)[self.JOB]

    def read_lines(self):
        with open(self.log_path, encoding='utf-8') as f:
            return [json.loads(line) for line in f if line.strip()]

    def capped(self, **kw):
        out = []
        kw.setdefault('alive', self.alive)
        return stall_mod.capped(self.product, out=out.append, **kw), out

    def reason(self):
        return runtime_mod.failure_reason(runtime_mod.read_result(self.log_path))

    def over_and_live(self, lines=None):
        self.write_log(self.over_input() if lines is None else lines)
        self.launch()

    def test_over_cap_is_stopped_and_gets_a_cap_result_line(self):
        self.over_and_live()
        rows, out = self.capped(now='2026-09-24T10:05:00Z')
        self.assertEqual(rows, [(self.JOB, 'input', 9_000_000, 8_000_000)])
        last = self.read_lines()[-1]
        self.assertEqual(last['type'], 'result')
        self.assertTrue(last['is_error'])
        self.assertEqual(last['asf']['cap'], {'kind': 'spec', 'dimension': 'input', 'tokens': 9_000_000,
                                              'limit': 8_000_000, 'at': '2026-09-24T10:05:00Z'})
        self.assertEqual(self.signals[0], (self.PID, signal.SIGTERM))
        self.assertEqual(out, [f'CAP   {self.JOB}  input 9000000 over 8000000 (spec)'])

    def test_the_cap_line_is_what_every_reader_sees(self):
        self.over_and_live()
        self.capped()
        result = runtime_mod.read_result(self.log_path)
        self.assertIsNotNone(result)
        self.assertFalse(runtime_mod.result_ok(result))
        self.assertEqual(runtime_mod.failure_reason(result), 'token cap')
        self.assertEqual(lifecycle.judge(self.run_of(), lifecycle.Evidence(result=result, alive=False)),
                         'failed: token cap')

    def test_the_marker_is_structured_not_text(self):
        forged = {'type': 'result', 'subtype': 'success', 'is_error': False,
                  'result': 'token cap: input 9999999 over the 8000000 cap for a spec job'}
        self.assertNotEqual(runtime_mod.failure_reason(forged), 'token cap')
        self.assertNotEqual(runtime_mod.failure_reason(dict(forged, asf='cap')), 'token cap')
        self.assertNotEqual(runtime_mod.failure_reason(dict(forged, asf={'cap': 'input'})), 'token cap')

    def test_sigkill_after_the_grace(self):
        self.over_and_live()
        rows, _ = self.capped()
        self.assertEqual([sig for _pid, sig in self.signals], [signal.SIGTERM, signal.SIGKILL])
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.reason(), 'token cap')

    def test_a_run_that_ends_in_the_grace_is_not_killed(self):
        self.over_and_live()
        self.on_signal = lambda sig: setattr(self, 'gone', True)
        self.capped()
        self.assertEqual([sig for _pid, sig in self.signals], [signal.SIGTERM])
        self.assertEqual(self.reason(), 'token cap')

    def test_a_signal_that_finds_no_process_still_writes_the_line(self):
        self.over_and_live()

        def refuse(sig):
            raise ProcessLookupError()
        self.on_signal = refuse
        rows, _ = self.capped()
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.reason(), 'token cap')

    def test_a_partial_last_line_is_not_run_into(self):
        self.over_and_live()
        with open(self.log_path, 'a', encoding='utf-8') as f:
            f.write('{"type": "assis')
        self.capped()
        self.assertEqual(self.reason(), 'token cap')

    def test_registry_records_the_cap(self):
        self.over_and_live()
        self.capped(now='2026-09-24T10:05:00Z')
        self.assertEqual(self.run_of()['capped'], {'dimension': 'input', 'tokens': 9_000_000,
                                                   'limit': 8_000_000, 'at': '2026-09-24T10:05:00Z'})
        self.assertIn('capped', lifecycle.RUN_FIELDS)
        self.launch(started='2026-09-24T11:00:00Z')  # the next run of the job
        self.assertNotIn('capped', self.run_of())

    def test_under_cap_is_untouched(self):
        self.over_and_live([INIT, assistant(i=10, o=1)])
        before = self.read_bytes()
        rows, out = self.capped()
        self.assertEqual(rows, [])
        self.assertEqual(out, ['cap: none'])
        self.assertEqual(self.read_bytes(), before)
        self.assertEqual(self.signals, [])
        self.assertNotIn('capped', self.run_of())

    def test_a_run_that_already_has_a_result_is_never_stopped(self):
        self.over_and_live(self.over_input() + [{'type': 'result', 'subtype': 'success', 'result': 'done'}])
        before = self.read_bytes()
        rows, _ = self.capped()
        self.assertEqual(rows, [])
        self.assertEqual(self.signals, [])
        self.assertEqual(self.read_bytes(), before)

    def test_a_finish_in_the_race_is_not_overwritten(self):
        self.over_and_live()
        own = {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'done'}

        def finish(sig):
            with open(self.log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(own) + '\n')
            self.gone = True
        self.on_signal = finish
        rows, out = self.capped()
        self.assertEqual(self.read_lines()[-1], own)
        self.assertEqual(sum(1 for r in self.read_lines() if r['type'] == 'result'), 1)
        self.assertIn('finished first', out[0])
        self.assertIsNone(self.reason())
        self.assertEqual(len(rows), 1)

    def test_a_foreign_pid_is_never_signalled(self):
        self.over_and_live()
        before = self.read_bytes()
        rows, _ = self.capped(alive=lambda pid: False)
        self.assertEqual(rows, [])
        self.assertEqual(self.signals, [])
        self.assertEqual(self.read_bytes(), before)

    def test_a_bad_caps_block_stops_nothing(self):
        self.over_and_live()
        before = self.read_bytes()
        bad = env.Product('sample', {'repo_dir': self.repo, 'token_caps': {'nonsense': {'input': 1}}})
        out = []
        self.assertEqual(stall_mod.capped(bad, alive=self.alive, out=out.append), [])
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith('token cap: '))
        self.assertIn('nonsense', out[0])
        self.assertEqual(self.signals, [])
        self.assertEqual(self.read_bytes(), before)

    def test_stop_false_judges_and_touches_nothing(self):
        self.over_and_live()
        before = self.read_bytes()
        rows, _ = self.capped(stop=False)
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.signals, [])
        self.assertEqual(self.read_bytes(), before)
        self.assertNotIn('capped', self.run_of())

    def test_the_kind_narrows_the_cap(self):
        self.over_and_live([INIT, assistant(i=10)])
        caps = tk.caps(env.Product('sample', {'token_caps': {'spec': {'input': 5}}}))
        rows, _ = self.capped(caps=caps)
        self.assertEqual(rows, [(self.JOB, 'input', 10, 5)])


if __name__ == '__main__':
    unittest.main()
