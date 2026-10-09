"""tests/test_release_rehearsal_criterion.py — the release rehearsal criterion: ``asf.release``'s
13th (the ``1.0`` gate) and ``asf.release_preview``'s 6th (the ``preview`` gate), each read off
its own step lookup so criteria 4 and 6 keep reading ``tests.yml``'s steps when a ``release.yml``
run is the newest on the trunk."""
import datetime
import os
import tempfile
import unittest

from asf import env, release, release_preview
from asf.scorecard.facts import Facts

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def product(release_block=None):
    data = {'repo_dir': '/repo', 'repo_slug': 'o/r', 'main': 'main'}
    if release_block is not None:
        data['release'] = release_block
    return env.Product('p', data)


def facts(items=None, rehearsal_steps=None):
    return {'items': items or {}, 'rehearsal_steps': rehearsal_steps}


class DefaultsTest(unittest.TestCase):
    def test_both_gates_carry_the_key(self):
        self.assertEqual(release.DEFAULTS['ci_steps']['rehearsal'], 'release rehearsal')
        self.assertEqual(release_preview.STEPS['rehearsal'], 'release rehearsal')


class RehearsalCriterionTest(unittest.TestCase):
    """``asf.release.rehearsal_criterion`` — the ``1.0`` gate's 13th."""

    def test_not_configured_is_na(self):
        cfg = release.settings(product())
        c = release.rehearsal_criterion(facts(), cfg)
        self.assertEqual(c.key, 'rehearsal')
        self.assertTrue(c.met)
        self.assertEqual(c.evidence, 'n/a — not configured (release.requires.rehearsal, '
                                     'release.ci_steps.rehearsal)')

    def test_configured_and_green(self):
        cfg = release.settings(product({'ci_steps': {'rehearsal': 'release rehearsal'}}))
        steps = [('rehearsal', 'release rehearsal', 'success')]
        c = release.rehearsal_criterion(facts(rehearsal_steps=steps), cfg)
        self.assertTrue(c.met, c.evidence)
        self.assertIn("'release rehearsal' green", c.evidence)
        self.assertIn('on the latest main test run', c.evidence)

    def test_configured_and_red(self):
        cfg = release.settings(product({'ci_steps': {'rehearsal': 'release rehearsal'}}))
        steps = [('rehearsal', 'release rehearsal', 'failure')]
        c = release.rehearsal_criterion(facts(rehearsal_steps=steps), cfg)
        self.assertFalse(c.met)
        self.assertIn("'release rehearsal' red", c.evidence)

    def test_configured_and_absent(self):
        cfg = release.settings(product({'ci_steps': {'rehearsal': 'release rehearsal'}}))
        c = release.rehearsal_criterion(facts(rehearsal_steps=[]), cfg)
        self.assertFalse(c.met)
        self.assertIn("'release rehearsal' absent", c.evidence)

    def test_requires_rehearsal_must_also_land(self):
        cfg = release.settings(product({'ci_steps': {'rehearsal': 'release rehearsal'},
                                        'requires': {'rehearsal': ['F-0001']}}))
        steps = [('rehearsal', 'release rehearsal', 'success')]
        unlanded = {'F-0001': {'landed': None, 'stage': 'card'}}
        c = release.rehearsal_criterion(facts(items=unlanded, rehearsal_steps=steps), cfg)
        self.assertFalse(c.met, c.evidence)
        self.assertIn('F-0001 card', c.evidence)
        landed = {'F-0001': {'landed': '2026-10-01T00:00:00Z', 'stage': 'landed'}}
        c = release.rehearsal_criterion(facts(items=landed, rehearsal_steps=steps), cfg)
        self.assertTrue(c.met, c.evidence)

    def test_the_requires_id_alone_configures_it(self):
        cfg = release.settings(product({'requires': {'rehearsal': ['F-0001']}}))
        self.assertTrue(release.applies(cfg, 'rehearsal'))


class SharedStepLookupRegressionTest(unittest.TestCase):
    """The PD3 regression: the rehearsal step lives in ``release.yml``, not ``tests.yml`` like
    criteria 4 and 6's three steps — the newest trunk run carrying only the rehearsal step must
    not make criteria 4 and 6 read 'absent'."""

    def test_criteria_4_and_6_keep_their_own_steps(self):
        runs = [{'databaseId': 2, 'status': 'completed', 'conclusion': 'success',
                'headSha': 'b' * 40, 'workflowName': 'release'},
               {'databaseId': 1, 'status': 'completed', 'conclusion': 'success',
                'headSha': 'a' * 40, 'workflowName': 'tests'}]
        jobs = {2: {'jobs': [{'name': 'rehearsal', 'steps': [
                    {'name': 'release rehearsal', 'conclusion': 'success'}]}]},
               1: {'jobs': [{'name': 'tests', 'steps': [
                   {'name': 'asf install, zero to green', 'conclusion': 'success'},
                   {'name': 'check generic', 'conclusion': 'success'},
                   {'name': 'sample product', 'conclusion': 'success'}]}]}}

        def git(repo, *args):
            if args[0] == 'rev-parse':
                return 'sha\n'
            if args[0] == 'show' and args[1] == '-s':
                return '2026-10-07T14:00:00+02:00\n'
            if args[0] == 'show':
                return {'origin/main:README.md': '# P\n',
                        'origin/main:CHANGELOG.md': ''}.get(args[1])
            if args[0] == 'describe':
                return 'v0.1.0\n'
            return ''

        def gh(args):
            if args[:2] == ['run', 'list']:
                return runs
            return jobs[int(args[2])]

        block = {'ci_steps': {'install_from_zero': 'asf install, zero to green',
                              'generic': 'check generic', 'second_product': 'sample product',
                              'rehearsal': 'release rehearsal'}}
        with tempfile.TemporaryDirectory() as d:
            rec = os.path.join(d, 'record')
            os.makedirs(rec)
            logs = os.path.join(d, 'logs')
            os.makedirs(logs)
            prod = env.Product('p', {'repo_dir': d, 'repo_slug': 'o/r', 'main': 'main',
                                     'release': block})
            fx = Facts(items={}, sessions=[], ci=[], gates=[], runs=[], clutter={},
                      as_of='2026-10-08T12:00:00Z')
            out = release.compute(rec, prod, now=NOW, git=git, gh_json=gh, log_dir=logs, facts=fx)
            met = {c['key']: c['met'] for c in out['criteria']}
            ev = {c['key']: c['evidence'] for c in out['criteria']}
            self.assertTrue(met['install'], ev['install'])
            self.assertTrue(met['generic'], ev['generic'])
            self.assertTrue(met['rehearsal'], ev['rehearsal'])
            self.assertIn("'asf install, zero to green' green", ev['install'])
            self.assertIn("'check generic' green", ev['generic'])
            self.assertIn("'release rehearsal' green", ev['rehearsal'])


def _preview_fakes(steps):
    def git(repo, *args):
        if args[0] == 'rev-parse':
            return 'sha\n'
        if args[0] == 'describe':
            return 'v0.1.0\n'
        if args[0] == 'ls-tree':
            return ''
        return ''

    def gh(args):
        if args[:2] == ['run', 'list']:
            return [{'databaseId': 1, 'status': 'completed', 'conclusion': 'success',
                    'headSha': 'a' * 40}]
        return {'jobs': [{'name': 'j', 'steps': [{'name': n, 'conclusion': c} for n, c in steps]}]}
    return git, gh


class PreviewRehearsalTest(unittest.TestCase):
    """``asf.release_preview``'s 6th criterion."""

    def test_green_is_named_by_run_step_and_conclusion(self):
        git, gh = _preview_fakes([('release rehearsal', 'success')])
        d = release_preview.compute(product(), now=NOW, git=git, gh_json=gh)
        keys = [c['key'] for c in d['criteria']]
        self.assertEqual(keys, ['install', 'minimal', 'readme', 'privacy', 'ship', 'rehearsal'])
        row = next(c for c in d['criteria'] if c['key'] == 'rehearsal')
        self.assertTrue(row['met'], row['evidence'])
        self.assertIn("'release rehearsal' green", row['evidence'])
        self.assertIn('at aaaaaaa', row['evidence'])

    def test_absent_is_red(self):
        git, gh = _preview_fakes([])
        d = release_preview.compute(product(), now=NOW, git=git, gh_json=gh)
        met = {c['key']: c['met'] for c in d['criteria']}
        self.assertFalse(met['rehearsal'])


if __name__ == '__main__':
    unittest.main()
