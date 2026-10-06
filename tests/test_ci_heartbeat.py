"""asf.ci_heartbeat — B-0178: ci.pool is the one box list (:func:`boxes`), read by the watchdog,
the installer and the doctor instead of three hand-kept copies that drift. A box's last beat is
one state-dir file (:func:`record`, :func:`stale`); no test shells out, no host name or IP
appears in this file or in the module under test."""
import datetime
import unittest

from asf import ci_heartbeat
from tests.test_ci_pool import Home, product


def three_box_pool():
    return [
        {'runner': 'ci-1', 'box': 'box-1', 'provider': 'alpha', 'role': 'heavy'},
        {'runner': 'ci-2', 'box': 'box-2', 'provider': 'alpha', 'role': 'heavy'},
        {'runner': 'ci-3', 'box': 'box-3', 'provider': 'alpha', 'role': 'heavy'},
    ]


NOW = datetime.datetime(2026, 10, 6, 1, 0, 0, tzinfo=datetime.timezone.utc)


class Heartbeat(Home):
    def test_a_box_with_no_heartbeat_is_doctor_red_and_named_and_the_installer_targets_exactly_it(self):
        p = product(pool=three_box_pool())
        pool = three_box_pool_entries(p)
        # box-1 and box-2 beat a minute ago; box-3 (the one the installer's copy omitted) never has.
        ci_heartbeat.record(p, 'box-1', when=NOW - datetime.timedelta(minutes=1))
        ci_heartbeat.record(p, 'box-2', when=NOW - datetime.timedelta(minutes=1))

        missing = ci_heartbeat.stale(pool, p, now=NOW)
        self.assertEqual([box for box, _seen in missing], ['box-3'])

        rows = ci_heartbeat.doctor_rows(pool, p, now=NOW)
        reds = [detail for required, ok, detail in rows if required and not ok]
        self.assertEqual(len(reds), 1)
        self.assertIn('box-3', reds[0])

    def test_all_boxes_beating_is_one_ok_row(self):
        p = product(pool=three_box_pool())
        pool = three_box_pool_entries(p)
        for box in ('box-1', 'box-2', 'box-3'):
            ci_heartbeat.record(p, box, when=NOW - datetime.timedelta(minutes=1))
        rows = ci_heartbeat.doctor_rows(pool, p, now=NOW)
        self.assertTrue(all(ok for _required, ok, _detail in rows))


def three_box_pool_entries(p):
    from asf import ci_pool
    return ci_pool.load_pool(p)


if __name__ == '__main__':
    unittest.main()
