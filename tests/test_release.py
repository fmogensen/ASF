"""asf.release — the release-readiness gate: settings, each fact reader, the eight criteria, the
CLI and the status row. Every fact source is a fixture: git and the forge are fakes, the tick and
install logs are files in a temp dir, the record is cards on disk."""
import datetime
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from asf import env, release
from asf.scorecard.facts import Facts

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
SINCE = '2026-09-18T12:00:00Z'


def card_md(iid, stage, state, history=()):
    hist = '\n'.join(f'- {h}' for h in history)
    return (f"---\nid: {iid}\ntype: feature\ntitle: \"{iid}\"\nparent: E-0001\n"
            f"# ---- machine ----\nschema_version: 1\nstate: {state}\nstage: {stage}\n"
            f"stage_since: 2026-09-20T12:00:00Z\nupdated: 2026-09-20T12:00:00Z\n---\n"
            f"## Description\n{iid}\n\n## History\n{hist}\n\n## Children\n\n## Backlinks\n")


def good_facts(**over):
    f = {
        'items': {'F-0001': {'landed': '2026-09-20T12:00:00Z', 'stage': 'landed'},
                  'F-0002': {'landed': '2026-09-21T12:00:00Z', 'stage': 'landed'}},
        'hand_commits': [], 'installs': [],
        'repair': {'sessions': 4, 'landed': 2, 'per_feature': 2.0},
        'upgrade': {'upgrades': [{'to': 'a'}, {'to': 'b'}, {'to': 'c'}], 'failed': [], 'torn': [],
                    'rollbacks': [], 'chain_breaks': []},
        'ci_runs': [{'conclusion': 'success', 'headSha': 'x' * 40}] * 10,
        'ci_steps': [('tests', 'bash tools/install.sh sample v0.1.0', 'success'),
                     ('tests', 'check generic', 'success'),
                     ('tests', 'the sample product, end to end', 'success')],
        'docs': {'headings': ['Install', 'Quick start', 'Configuration', 'Upgrade'], 'tag': 'v0.1.0',
                 'changelog_section': True, 'changelog_notes': True},
    }
    f.update(over)
    return f


def cfg(**block):
    return release.settings(env.Product('p', {'release': block}))


class SettingsTest(unittest.TestCase):
    def test_defaults_when_unset(self):
        c = release.settings(env.Product('p', {}))
        self.assertEqual(c['window_days'], 7)
        self.assertEqual(c['max_repair_per_feature'], 3.0)
        self.assertEqual(c['ci_runs'], 10)
        self.assertEqual(c['blocking'], [])

    def test_overrides_merge(self):
        c = cfg(window_days='14', max_repair_per_feature=2.5, blocking=['F-0001'],
                ci_steps={'generic': 'scan names'}, requires={'upgrade': 'F-0003'})
        self.assertEqual(c['window_days'], 14)
        self.assertEqual(c['max_repair_per_feature'], 2.5)
        self.assertEqual(c['blocking'], ['F-0001'])
        self.assertEqual(c['ci_steps']['generic'], 'scan names')
        self.assertEqual(c['ci_steps']['second_product'], 'sample product')   # default kept
        self.assertEqual(c['requires'], {'upgrade': ['F-0003']})
        self.assertEqual(release.DEFAULTS['ci_steps']['generic'], 'check generic')  # not mutated

    def test_product_file_accepts_release_block(self):
        text = ('product: p\nrepo_dir: /tmp/x\nrelease:\n  window_days: 7\n  blocking: [F-0001]\n'
                '  ci_steps:\n    generic: check generic\n')
        self.assertEqual(env.validate_product_text(text), [])
        bad = 'product: p\nrelease:\n  nonsense: 1\n'
        self.assertTrue(any(k == 'release.nonsense' for _l, k, _p in env.validate_product_text(bad)))


class HandCommitsTest(unittest.TestCase):
    def fake_git(self, records):
        def git(repo, *args):
            if args[0] == 'log':
                return ''.join('\x1f'.join(r) + '\x1e\n' for r in records)
            return None
        return git

    def test_only_unsessioned_fix_types(self):
        recs = [
            # sha, date, author, subject, ASF-Session, Signed-off-by, Claude-Session
            ('a' * 40, '2026-09-24T10:00:00+02:00', 'C', 'fix(x): console fix', '', '', 'https://s/1'),
            ('b' * 40, '2026-09-24T09:00:00+02:00', 'C', 'fix(B-1): worker fix', 'asf/fix-b-1@t', 'C', ''),
            ('c' * 40, '2026-09-24T08:00:00+02:00', 'C', 'fix(T-1): older worker', '', 'C', ''),
            ('d' * 40, '2026-09-24T07:00:00+02:00', 'Op', 'hotfix: by hand', '', '', ''),
            ('e' * 40, '2026-09-24T06:00:00+02:00', 'C', 'feat(y): console feature', '', '', 'https://s/1'),
            ('f' * 40, '2026-09-24T05:00:00+02:00', 'C', 'hotfix(t): console, signed', '', 'C', 'https://s/2'),
        ]
        out = release.hand_commits('/r', 'origin/main', SINCE, ['fix', 'hotfix', 'revert'],
                                   git=self.fake_git(recs))
        # a: console fix; d: operator hotfix; f: signed but a console trailer — 3.
        # b: ASF-Session; c: signed-off, no console trailer (a factory session); e: a feat — 0.
        self.assertEqual([c['sha'] for c in out], ['aaaaaaa', 'ddddddd', 'fffffff'])


class TickLogTest(unittest.TestCase):
    LOG = '\n'.join([
        'ImportError: cannot import name x',                                          # before any upgrade
        'tick: ran asf upgrade (0.1.0@1111111 → 0.1.0@2222222), exit 0; the next tick runs the new one',
        'tick: ran asf upgrade (0.1.0@2222222 → 0.1.0@3333333), exit 0; the next tick runs the new one',
        'tick: ran asf upgrade (0.1.0@9999999 → 0.1.0@4444444), exit 0; the next tick runs the new one',
        'ModuleNotFoundError: No module named asf.x',
        'tick: ran asf upgrade (0.1.0@4444444 → 0.1.0@5555555), exit 1',
        'upgrade: rollback: reinstalled 4444444',
    ])

    def test_parse(self):
        ev = release.parse_tick_log(self.LOG)
        self.assertEqual([e[0] for e in ev],
                         ['torn', 'upgrade', 'upgrade', 'upgrade', 'torn', 'upgrade', 'rollback'])
        self.assertEqual(ev[1], ('upgrade', '1111111', '2222222', 0))

    def test_upgrade_facts_window_and_chain(self):
        dates = {'1111111': '2026-09-10T00:00:00Z', '2222222': '2026-09-17T00:00:00Z',   # out of window
                 '3333333': '2026-09-19T00:00:00Z', '9999999': '2026-09-20T00:00:00Z',
                 '4444444': '2026-09-21T00:00:00Z', '5555555': '2026-09-22T00:00:00Z'}
        f = release.upgrade_facts([('tick-p.log', release.parse_tick_log(self.LOG))], dates, SINCE)
        self.assertEqual([u['to'] for u in f['upgrades']], ['3333333', '4444444'])
        self.assertEqual([u['to'] for u in f['failed']], ['5555555'])
        self.assertEqual(len(f['torn']), 1)          # the first ImportError precedes the window
        self.assertEqual(len(f['rollbacks']), 1)
        # 3333333 was installed, the next upgrade starts from 9999999: installed by hand
        self.assertEqual(f['chain_breaks'], [{'installed': '9999999', 'after': '3333333',
                                              'date': '2026-09-20T00:00:00Z'}])

    def test_logs_and_install_log_from_disk(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'tick-p-a.log'), 'w') as f:
                f.write(self.LOG)
            with open(os.path.join(d, 'install.log'), 'w') as f:
                f.write('2026-09-10T00:00:00Z\tp\tabc\n2026-09-24T00:00:00Z\tp\tdef1234567890abc\n')
            logs = release.read_tick_logs(d)
            self.assertEqual(logs[0][0], 'tick-p-a.log')
            self.assertEqual(release.install_log(d, SINCE),
                             [{'date': '2026-09-24T00:00:00Z', 'product': 'p', 'ref': 'def123456789'}])
            self.assertEqual(release.install_log(os.path.join(d, 'none'), SINCE), [])


class DocsTest(unittest.TestCase):
    def test_readme_headings(self):
        self.assertEqual(release.readme_headings('# T\n## Install\ntext\n### Quick start ##\n'),
                         ['Install', 'Quick start'])

    def test_changelog_notes(self):
        text = '# Changelog\n\n## v0.2.0 — 2026-09-25\n\n- a fix\n\n## v0.1.0\n\n'
        self.assertEqual(release.changelog_notes(text, 'v0.2.0'), (True, True))
        self.assertEqual(release.changelog_notes(text, 'v0.1.0'), (True, False))
        self.assertEqual(release.changelog_notes(text, 'v0.3.0'), (False, False))
        self.assertEqual(release.changelog_notes(text, None), (False, False))


class CiTest(unittest.TestCase):
    def test_runs_completed_only(self):
        runs = [{'status': 'in_progress'}] + [{'status': 'completed', 'conclusion': 'success'}] * 12
        seen = []

        def gh(args):
            seen.append(args)
            return runs
        self.assertEqual(len(release.ci_runs('o/r', 'main', 10, gh)), 10)
        self.assertIn('--branch', seen[0])
        self.assertIsNone(release.ci_runs('o/r', 'main', 10, lambda a: None))

    def test_steps_and_state(self):
        data = {'jobs': [{'name': 't (3.12)', 'steps': [{'name': 'check generic', 'conclusion': 'success'}]},
                         {'name': 't (3.13)', 'steps': [{'name': 'check generic', 'conclusion': 'failure'}]}]}
        steps = release.ci_steps('o/r', 1, lambda a: data)
        self.assertEqual(release.step_state(steps, 'Check Generic'), (True, False))   # one matrix leg red
        self.assertEqual(release.step_state(steps, 'install.sh'), (False, False))
        self.assertIsNone(release.ci_steps('o/r', 1, lambda a: None))


class EvaluateTest(unittest.TestCase):
    def crit(self, facts, c=None):
        return {x.key: x for x in release.evaluate(facts, c or cfg(blocking=['F-0001', 'F-0002']))}

    def test_all_met(self):
        out = self.crit(good_facts())
        self.assertEqual([k for k, v in out.items() if not v.met], [])
        self.assertEqual(list(out), list(release.KEYS))

    def test_each_unmet(self):
        c = cfg(blocking=['F-0001', 'F-0009'], requires={'upgrade': ['F-0002'], 'install': ['F-0009']})
        f = good_facts(
            hand_commits=[{'sha': 'abc1234', 'date': '2026-09-24T10:00:00+02:00', 'author': 'Op',
                           'subject': 'fix: by hand'}],
            repair={'sessions': 91, 'landed': 10, 'per_feature': 9.1},
            ci_runs=[{'conclusion': 'failure', 'headSha': 'deadbeef00'}] + [{'conclusion': 'success'}] * 9,
            ci_steps=[('tests', 'check generic', 'success')],
            upgrade={'upgrades': [{'to': 'a'}], 'failed': [], 'torn': ['ImportError: x'], 'rollbacks': [],
                     'chain_breaks': []},
            docs={'headings': ['Install'], 'tag': 'v0.1.0', 'changelog_section': False,
                  'changelog_notes': False})
        out = self.crit(f, c)
        self.assertEqual([k for k, v in out.items() if v.met], [])
        self.assertIn('1 hand fix commit(s)', out['stability'].evidence)
        self.assertIn('abc1234', out['stability'].evidence)
        self.assertIn('= 9.1', out['repair'].evidence)
        self.assertIn('9/10 green', out['ci'].evidence)
        self.assertIn("'install.sh' absent", out['install'].evidence)
        self.assertIn('F-0009 not in the record', out['install'].evidence)
        self.assertIn('1 torn tick', out['upgrade'].evidence)
        self.assertIn("'sample product' absent", out['generic'].evidence)
        self.assertIn('README lacks Quick start, Configuration, Upgrade', out['docs'].evidence)
        self.assertIn('1/2 landed; open: F-0009', out['blocking'].evidence)

    def test_hand_install_breaks_stability(self):
        up = dict(good_facts()['upgrade'], chain_breaks=[{'installed': '9999999', 'after': 'x',
                                                          'date': '2026-09-20T00:00:00Z'}])
        out = self.crit(good_facts(upgrade=up))
        self.assertFalse(out['stability'].met)
        self.assertIn('1 hand install(s)', out['stability'].evidence)
        self.assertIn('9999999', out['stability'].evidence)

    def test_threshold_is_configurable(self):
        f = good_facts(repair={'sessions': 9, 'landed': 3, 'per_feature': 3.0})
        self.assertTrue(self.crit(f)['repair'].met)                      # 3.0 ≤ 3
        c = cfg(blocking=['F-0001'], max_repair_per_feature=2)
        self.assertFalse(self.crit(f, c)['repair'].met)

    def test_nothing_landed_and_no_forge_are_unmet(self):
        out = self.crit(good_facts(repair={'sessions': 5, 'landed': 0, 'per_feature': None},
                                   ci_runs=None, ci_steps=None))
        self.assertFalse(out['repair'].met)
        self.assertFalse(out['ci'].met)
        self.assertIn('did not answer', out['ci'].evidence)
        self.assertFalse(out['install'].met)

    def test_no_blocking_list_is_unmet(self):
        self.assertFalse(self.crit(good_facts(), cfg())['blocking'].met)


class GatherTest(unittest.TestCase):
    """:func:`release.gather` → :func:`release.compute` over a record on disk, fake git and forge."""

    def test_compute_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            rec = os.path.join(d, 'record')
            os.makedirs(os.path.join(rec, 'features'))
            with open(os.path.join(rec, 'features', 'F-0001.md'), 'w') as f:
                f.write(card_md('F-0001', 'landed', 'Closed'))
            with open(os.path.join(rec, 'features', 'F-0002.md'), 'w') as f:
                f.write(card_md('F-0002', 'card', 'New'))
            logs = os.path.join(d, 'logs')
            os.makedirs(logs)
            with open(os.path.join(logs, 'tick-p.log'), 'w') as f:
                f.write('tick: ran asf upgrade (0.1.0@1111111 → 0.1.0@2222222), exit 0\n')

            def git(repo, *args):
                if args[0] == 'rev-parse':
                    return 'sha\n'
                if args[0] == 'show' and args[1] == '-s':
                    return '2026-09-24T14:00:00+02:00\n'
                if args[0] == 'show':
                    return {'origin/main:README.md': '# P\n## Install\n',
                            'origin/main:CHANGELOG.md': '## v0.1.0\n- first\n'}.get(args[1])
                if args[0] == 'describe':
                    return 'v0.1.0\n'
                return ''

            def gh(args):
                if args[:2] == ['run', 'list']:
                    return [{'status': 'completed', 'conclusion': 'success', 'databaseId': 7}] * 10
                return {'jobs': [{'name': 't', 'steps': [{'name': 'check generic', 'conclusion': 'success'}]}]}

            product = env.Product('p', {'repo_dir': d, 'repo_slug': 'o/r', 'main': 'main',
                                        'release': {'blocking': ['F-0001', 'F-0002']}})
            facts = Facts(items={}, sessions=[], ci=[], gates=[], runs=[], clutter={}, as_of='2026-09-25T12:00:00Z')
            out = release.compute(rec, product, now=NOW, git=git, gh_json=gh, log_dir=logs, facts=facts)
            met = {c['key']: c['met'] for c in out['criteria']}
            self.assertFalse(out['ready'])
            self.assertEqual(met, {'stability': True, 'repair': False, 'ci': True, 'install': False,
                                   'upgrade': False, 'generic': False, 'docs': False, 'blocking': False})
            ev = {c['key']: c['evidence'] for c in out['criteria']}
            self.assertIn('1 auto-upgrade(s)', ev['upgrade'])       # dated 2026-09-24 12:00 UTC: in the window
            self.assertIn('F-0002 card', ev['blocking'])
            self.assertIn('CHANGELOG has v0.1.0 notes', ev['docs'])
            text = release.render(out)
            self.assertIn('NOT READY — 2/8 met', text)
            self.assertIn('| 1 | Stability (no hand hotfix for 7 d) | yes |', text)


class SurfaceTest(unittest.TestCase):
    def test_cli_parses(self):
        from asf.cli import build_parser
        args = build_parser().parse_args(['release-readiness', '--product', 'p', '--json'])
        self.assertEqual((args.command, args.product, args.json), ('release-readiness', 'p', True))

    def test_exit_code_follows_the_verdict(self):
        d = {'product': 'p', 'as_of': 'now', 'ready': True, 'window_days': 7,
             'criteria': [{'key': 'ci', 'name': 'CI', 'met': True, 'evidence': 'ok'}]}
        args = mock.Mock(product='p', json=False)
        with mock.patch.object(env, 'load_product', return_value=env.Product('p', {})), \
                mock.patch.object(release, 'compute', return_value=d), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(release.cmd_release_readiness(args, '/r'), 0)
            d['ready'] = False
            self.assertEqual(release.cmd_release_readiness(args, '/r'), 1)
        self.assertIn('READY — 1/1 met', out.getvalue())

    def test_status_row_only_for_the_factory_or_a_release_block(self):
        from asf.views import status
        with tempfile.TemporaryDirectory() as d:
            plain = env.Product('p', {'repo_dir': d})
            self.assertIsNone(status.release_cell('/r', plain))
            with open(os.path.join(d, 'pyproject.toml'), 'w') as f:
                f.write('[project]\nname = "asf-factory"\n')
            with mock.patch('asf.release.cell', return_value='NOT READY — 3/8 met') as cell:
                self.assertEqual(status.release_cell('/r', plain), 'NOT READY — 3/8 met')
                cell.assert_called_once()


if __name__ == '__main__':
    unittest.main()
