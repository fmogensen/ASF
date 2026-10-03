"""Every recorded ``gh`` fixture is redacted (W3-PR3, S-m18): no run/job/account id of six digits
or more, no 40-hex sha outside the deterministic set, no repo but ``example/repo``, no branch but
``main`` or ``worker/T-<n>``, no key the redactor does not keep."""
import glob
import json
import os
import re
import unittest

from tests import contracts

LONG_ID = re.compile(r'[0-9]{6,}')
HEX40 = re.compile(r'(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])')
#: a repo named in a URL, an api path or a ``-R`` argument
REPO = re.compile(r'(?:(?<![.\w])github\.com/|/repos/|^repos/)([^/\s?]+/[^/\s?]+)')
BRANCH_KEYS = ('head_branch', 'headRefName', 'headBranch')
BRANCH = re.compile(r'^(main|worker/T-\d{4})$')


def strings(v, key=None):
    if isinstance(v, dict):
        for k, x in v.items():
            yield from strings(x, k)
    elif isinstance(v, list):
        for x in v:
            yield from strings(x, key)
    elif isinstance(v, (str, int)) and not isinstance(v, bool):
        yield key, str(v)


def keys(v):
    if isinstance(v, dict):
        for k, x in v.items():
            yield k
            yield from keys(x)
    elif isinstance(v, list):
        for x in v:
            yield from keys(x)


def lint(fx):
    """The problems of one fixture's parsed JSON (``[]`` when it is clean)."""
    problems = []
    if set(fx) != {'argv', 'rc', 'stdout', 'stderr'}:
        return [f'keys {sorted(fx)}']
    if not (isinstance(fx['argv'], list) and fx['argv'] and
            all(isinstance(a, str) for a in fx['argv'])):
        problems.append('argv is not a list of strings')
    elif fx['argv'][0] == 'gh':
        problems.append('argv carries gh itself')
    if fx['argv'] and '-R' in fx['argv']:
        i = fx['argv'].index('-R')
        if fx['argv'][i + 1:i + 2] != [contracts.SLUG]:
            problems.append(f'-R {fx["argv"][i + 1:i + 2]}')
    if not isinstance(fx['rc'], int) or not isinstance(fx['stderr'], str):
        problems.append('rc/stderr type')
    extra = sorted(set(keys(fx['stdout'])) - contracts.KEEP)
    if extra:
        problems.append(f'unredacted keys {extra}')
    shas = set(contracts.SHAS)
    for key, s in strings({'argv': fx['argv'], 'stdout': fx['stdout'], 'stderr': fx['stderr']}):
        if LONG_ID.search(HEX40.sub('', s)):
            problems.append(f'long id in {key}')
        for h in HEX40.findall(s):
            if h not in shas:
                problems.append(f'sha {h[:7]} outside the deterministic set in {key}')
        for repo in REPO.findall(s):
            if repo != contracts.SLUG:
                problems.append(f'repo other than {contracts.SLUG} in {key}')
        if key in BRANCH_KEYS and not BRANCH.match(s):
            problems.append(f'branch not main/worker/T-<n> in {key}')
    return problems


class FixtureShapes(unittest.TestCase):
    def test_every_fixture_is_redacted(self):
        paths = sorted(glob.glob(os.path.join(contracts.ROOT, '**', '*.json'), recursive=True))
        self.assertTrue(paths)
        for p in paths:
            with open(p, encoding='utf-8') as f:
                fx = json.load(f)
            self.assertEqual(lint(fx), [], os.path.relpath(p, contracts.ROOT))

    def test_the_lint_catches_each_leak(self):
        ok = {'argv': ['api', f'repos/{contracts.SLUG}/commits/{contracts.SHAS[0]}'], 'rc': 0,
              'stdout': {'id': 10001, 'head_branch': 'worker/T-0001'}, 'stderr': ''}
        self.assertEqual(lint(ok), [])
        leaks = {
            'long id': {**ok, 'stdout': {'id': 36585027621}},
            'outside the deterministic set': {**ok, 'stdout': {'sha': 'a' * 40}},
            'repo other than': {**ok, 'argv': ['api', 'repos/acme/product/actions/runs/1']},
            'branch not main': {**ok, 'stdout': {'head_branch': 'feature/x'}},
            'unredacted keys': {**ok, 'stdout': {'author': {'login': 'someone'}}},
            '-R': {**ok, 'argv': ['pr', 'view', '1', '-R', 'acme/product']},
        }
        for what, fx in leaks.items():
            self.assertTrue(any(what in p for p in lint(fx)), (what, lint(fx)))


class Redactor(unittest.TestCase):
    def test_the_same_input_redacts_the_same_way_every_time(self):
        real = json.dumps({'check_runs': [{
            'id': 110784508014, 'name': 'unit-suite (ubuntu)', 'conclusion': 'skipped',
            'head_sha': 'f' * 40, 'node_id': 'CR_kwDO', 'app': {'owner': {'login': 'acme'}},
            'html_url': 'https://github.com/Acme/Product/actions/runs/36989872241/job/110784508014'}]})
        one = contracts.Redactor('acme/product', names={'unit-suite': 'tests'}).call(
            ['api', 'repos/acme/product/commits/' + 'f' * 40 + '/check-runs'], 0, real, '')
        two = contracts.Redactor('acme/product', names={'unit-suite': 'tests'}).call(
            ['api', 'repos/acme/product/commits/' + 'f' * 40 + '/check-runs'], 0, real, '')
        self.assertEqual(one, two)
        self.assertEqual(lint(one), [])
        run = one['stdout']['check_runs'][0]
        self.assertEqual(run['name'], 'tests (ubuntu)')
        self.assertEqual(run['head_sha'], contracts.SHAS[0])
        self.assertEqual(run['id'], contracts.FIRST_ID)
        self.assertEqual(run['html_url'], 'https://github.com/example/repo/actions/runs/10002/job/10001')
        self.assertNotIn('node_id', run)
        self.assertNotIn('app', run)
        self.assertIn(contracts.SHAS[0], one['argv'][1])


if __name__ == '__main__':
    unittest.main()
