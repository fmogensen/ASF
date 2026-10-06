"""B-0178: the CI box list has one source — the product's ``ci.pool`` — that the heartbeat
watchdog, its fleet installer and ``asf doctor`` all read (:mod:`asf.ci_heartbeat`). Hermetic: a
temp ASF home, a fixture pool, a fixture seen file; no ssh, no host."""
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from asf import ci_heartbeat, doctor, env

NOW = 1_800_000_000.0


def product(pool=None):
    pool = pool if pool is not None else [
        {'runner': 'r-1', 'box': 'box-1', 'provider': 'alpha', 'role': 'heavy'},
        {'runner': 'r-1b', 'box': 'box-1', 'provider': 'alpha', 'role': 'light'},
        {'runner': 'r-2', 'box': 'box-2', 'provider': 'alpha', 'role': 'heavy'},
        {'runner': 'r-3', 'box': 'box-3', 'provider': 'beta', 'role': 'heavy'},
    ]
    return env.Product('p', {'repo_slug': 'o/r', 'ci': {'provider': 'github-actions',
                                                         'pool': pool}})


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ci_heartbeat_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        home = mock.patch.object(env, 'ASF_HOME', self.tmp)
        home.start()
        self.addCleanup(home.stop)

    def seen(self, data):
        path = ci_heartbeat.seen_path({})
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f)


class BoxesTest(Case):
    def test_the_pool_is_the_box_list_one_per_box(self):
        self.assertEqual(ci_heartbeat.boxes(product()), ['box-1', 'box-2', 'box-3'])

    def test_a_target_is_the_operator_configs_else_the_box_name(self):
        cfg = {'ci_heartbeat': {'targets': {'box-2': 'u@addr'}}}
        self.assertEqual(ci_heartbeat.target('box-2', cfg), 'u@addr')
        self.assertEqual(ci_heartbeat.target('box-1', cfg), 'box-1')

    def test_no_pool_no_boxes(self):
        self.assertEqual(ci_heartbeat.boxes(product(pool=[])), [])
        self.assertEqual(ci_heartbeat.boxes(env.Product('p', {})), [])


class MissingTest(Case):
    def test_a_box_missing_its_heartbeat_is_red_in_doctor_and_the_installers_only_target(self):
        # one box never sent a heartbeat (the 2026-10-06 case: rebuilt, never installed)
        self.seen({'box-1': NOW - 60, 'box-2': {'at': NOW - 120}})
        p, cfg = product(), {}
        rows = ci_heartbeat.doctor_rows(p, now=NOW, cfg=cfg)
        red = [d for _req, ok, d in rows if not ok]
        self.assertEqual(len(red), 1, rows)
        self.assertIn('box-3', red[0])
        self.assertIn('no heartbeat', red[0])
        self.assertEqual(ci_heartbeat.missing(p, now=NOW, cfg=cfg), ['box-3'])
        out = io.StringIO()
        args = SimpleNamespace(product='p', missing=True, json=False)
        with mock.patch.object(env, 'load_product', return_value=p), \
                mock.patch.object(env, 'load_config', return_value=cfg), \
                mock.patch.object(ci_heartbeat, '_now', return_value=NOW), redirect_stdout(out):
            self.assertEqual(ci_heartbeat.cmd_boxes(args), 0)
        self.assertEqual(out.getvalue(), 'box-3=box-3\n')

    def test_a_heartbeat_older_than_ten_minutes_is_red_and_the_limit_is_config(self):
        self.seen({'box-1': NOW - 11 * 60, 'box-2': NOW, 'box-3': NOW})
        p = product()
        rows = ci_heartbeat.doctor_rows(p, now=NOW, cfg={})
        self.assertEqual([d.split(':')[0] for _r, ok, d in rows if not ok], ['box-1'])
        self.assertTrue(any('11 min' in d for _r, ok, d in rows if not ok), rows)
        rows = ci_heartbeat.doctor_rows(p, now=NOW, cfg={'ci_heartbeat': {'stale_min': 15}})
        self.assertTrue(all(ok for _r, ok, _d in rows), rows)

    def test_all_fresh_is_one_green_row(self):
        self.seen({'box-1': NOW, 'box-2': NOW, 'box-3': NOW})
        rows = ci_heartbeat.doctor_rows(product(), now=NOW, cfg={})
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0][1])
        self.assertIn('3 box(es)', rows[0][2])

    def test_a_minimal_product_or_no_watchdog_gives_no_row(self):
        # no pool (host-run CI) or no seen file (no heartbeat watchdog runs): nothing to say
        self.assertEqual(ci_heartbeat.doctor_rows(product(pool=[]), now=NOW, cfg={}), [])
        self.assertEqual(ci_heartbeat.doctor_rows(product(), now=NOW, cfg={}), [])

    def test_the_doctor_carries_the_rows(self):
        self.seen({'box-1': NOW, 'box-2': NOW})
        with mock.patch.object(ci_heartbeat, '_now', return_value=NOW), \
                mock.patch.object(env, 'load_config', return_value={}):
            rows = doctor.check_ci_heartbeat(product())
        self.assertTrue(any(not ok and 'box-3' in d for _r, ok, d in rows), rows)


class SelfReportTest(Case):
    """A box migrated off the watchdog's polling reports its own beat (:func:`ci_heartbeat.record`,
    ``asf ci heartbeat <box>``); :func:`ci_heartbeat.ages` takes whichever of the two sources is
    newer, box by box, so a box moving between mechanisms never reads stale while either still has
    a recent beat."""

    def test_a_self_reported_beat_is_fresh_with_no_watchdog_seen_file_at_all(self):
        p = product()
        for box in ('box-1', 'box-2', 'box-3'):
            ci_heartbeat.record(p, box, when=NOW)
        rows = ci_heartbeat.doctor_rows(p, now=NOW, cfg={})
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0][1], rows)

    def test_whichever_beat_is_newer_wins_per_box(self):
        # the watchdog's own copy has box-1 stale, but box-1 has since self-reported fresh
        self.seen({'box-1': NOW - 20 * 60, 'box-2': NOW, 'box-3': NOW})
        p = product()
        ci_heartbeat.record(p, 'box-1', when=NOW)
        rows = ci_heartbeat.doctor_rows(p, now=NOW, cfg={})
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0][1], rows)

    def test_cmd_heartbeat_records_the_box_the_doctor_then_sees(self):
        p = product(pool=[{'runner': 'r-1', 'box': 'box-1', 'provider': 'alpha', 'role': 'heavy'}])
        args = SimpleNamespace(product='p', box='box-1')
        with mock.patch.object(env, 'load_product', return_value=p):
            self.assertEqual(ci_heartbeat.cmd_heartbeat(args), 0)
        rows = ci_heartbeat.doctor_rows(p, now=ci_heartbeat._now(), cfg={})
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0][1], rows)


if __name__ == '__main__':
    unittest.main()
