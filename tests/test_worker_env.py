"""env.worker_env: the fixed variables every local worker session gets — none by default (F-0247:
a test runner's knob is its product's ``conventions.worker_env``, never every product's)."""
import types
import unittest

from asf import env


def _product(worker_env=None):
    conv = {} if worker_env is None else {'worker_env': worker_env}
    return types.SimpleNamespace(conventions=conv)


class WorkerEnv(unittest.TestCase):
    def test_no_product_knob_by_default(self):
        self.assertEqual(env.DEFAULT_WORKER_ENV, {})
        self.assertEqual(env.worker_env({}, _product()), {})
        self.assertEqual(env.worker_env({}, None), {})

    def test_a_product_sets_its_own_test_runner_knob(self):
        got = env.worker_env({}, _product({'VITEST_MAX_WORKERS': 2}))
        self.assertEqual(got, {'VITEST_MAX_WORKERS': '2'})

    def test_operator_then_product_override_and_remove(self):
        cfg = {'worker_pool': {'env': {'VITEST_MAX_WORKERS': 3, 'JOBS': '4'}}}
        self.assertEqual(env.worker_env(cfg, _product()),
                         {'VITEST_MAX_WORKERS': '3', 'JOBS': '4'})
        got = env.worker_env(cfg, _product({'VITEST_MAX_WORKERS': '', 'X': 1}))
        self.assertEqual(got, {'JOBS': '4', 'X': '1'})

    def test_no_product_and_bad_shapes_keep_the_default(self):
        cfg = {'worker_pool': {'env': ['not', 'a', 'map']}}
        self.assertEqual(env.worker_env(cfg, None), {})


if __name__ == '__main__':
    unittest.main()
