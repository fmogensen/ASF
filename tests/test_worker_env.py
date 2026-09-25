"""env.worker_env: the fixed variables every local worker session gets (host-load caps)."""
import types
import unittest

from asf import env


def _product(worker_env=None):
    conv = {} if worker_env is None else {'worker_env': worker_env}
    return types.SimpleNamespace(conventions=conv)


class WorkerEnv(unittest.TestCase):
    def test_default_caps_vitest_workers(self):
        self.assertEqual(env.worker_env({}, _product())['VITEST_MAX_WORKERS'], '2')

    def test_operator_then_product_override_and_remove(self):
        cfg = {'worker_pool': {'env': {'VITEST_MAX_WORKERS': 3, 'JOBS': '4'}}}
        self.assertEqual(env.worker_env(cfg, _product()),
                         {'VITEST_MAX_WORKERS': '3', 'JOBS': '4'})
        got = env.worker_env(cfg, _product({'VITEST_MAX_WORKERS': '', 'X': 1}))
        self.assertEqual(got, {'JOBS': '4', 'X': '1'})

    def test_no_product_and_bad_shapes_keep_the_default(self):
        cfg = {'worker_pool': {'env': ['not', 'a', 'map']}}
        self.assertEqual(env.worker_env(cfg, None), {'VITEST_MAX_WORKERS': '2'})


if __name__ == '__main__':
    unittest.main()
