"""asf.release — the release-readiness gate: settings, each fact reader, the criteria, the
CLI and the status row. Every fact source is a fixture: git and the forge are fakes, the tick and
install logs are files in a temp dir, the record is cards on disk."""
import datetime
import io
import json
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
        'ci_steps': [('tests', 'asf install, zero to green (a temp HOME, sample/, doctor, a dry run, '
                               'then again)', 'success'),
                     ('tests', 'check generic', 'success'),
                     ('tests', 'the sample product, end to end', 'success')],
        'docs': {'headings': ['Install', 'Quick start', 'Configuration', 'Upgrade'], 'tag': 'v0.1.0',
                 'changelog_section': True, 'changelog_notes': True, 'changelog_kept': True},
        'seats': [seat_tick(m, 8, 8, 3) for m in (0, 5, 10)],   # seats in use: criterion 10 has data
    }
    f.update(over)
    return f


def cfg(**block):
    """A release block that configures every criterion (the way a framework's own product file
    does): the stability rule on, the CI steps, upgrades and README sections named.
    :func:`product_cfg` is a product's bare block."""
    block.setdefault('max_hand_fixes', 0)
    block.setdefault('min_upgrades', 3)
    block.setdefault('readme_sections', list(release.DEFAULTS['readme_sections']))
    block['ci_steps'] = {**release.DEFAULTS['ci_steps'], **(block.get('ci_steps') or {})}
    return release.settings(env.Product('p', {'repo_slug': 'o/r', 'release': block}))


def product_cfg(**block):
    return release.settings(env.Product('p', {'repo_slug': 'o/r', 'release': block}))


def seat_tick(minute, busy, avail, launchable, cause='', base=datetime.datetime(2026, 9, 24, 10, 0, tzinfo=UTC)):
    """One tick line carrying the wave's seat reading, ``minute`` minutes after ``base``."""
    ts = (base + datetime.timedelta(minutes=minute)).strftime('%Y-%m-%dT%H:%M:%SZ')
    return {'ts': ts, 'tick': 1, 'seats': {'local_busy': busy, 'local_seats': avail, 'cloud_busy': 0,
                                           'cloud_seats': 0, 'launchable': launchable, 'cause': cause}}


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

    def test_product_file_accepts_release_gate_floor_and_seats(self):
        """B-0038: release_preview.gate_for reads release.gate, release.floor_criterion and
        seats_criterion read release.floor / release.seats — none of the three may be reported
        as an unknown field, the false 'is not a field of the product file' doctor warning."""
        text = ('product: p\nrepo_dir: /tmp/x\nrelease:\n  gate: preview\n'
                '  floor:\n    stale_pr_days: 5\n  seats:\n    min_pct: 70\n')
        self.assertEqual(env.validate_product_text(text), [])


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

    def test_an_upgrade_skipped_offline_is_neither_an_upgrade_nor_a_failure(self):
        log = '\n'.join([
            'tick: ran asf upgrade (0.1.0@3333333 → 0.1.0@4444444), exit 0; the next tick runs the new one',
            'tick: asf upgrade skipped — offline (DNS: nodename nor servname provided, or not known)',
        ])
        ev = release.parse_tick_log(log)
        self.assertEqual(ev[1], ('upgrade-skipped', 'DNS: nodename nor servname provided, or not known'))
        dates = {'3333333': '2026-09-19T00:00:00Z', '4444444': '2026-09-21T00:00:00Z'}
        f = release.upgrade_facts([('tick-p.log', ev)], dates, SINCE)
        self.assertEqual((len(f['upgrades']), len(f['failed']), len(f['skipped'])), (1, 0, 1))
        out = {x.key: x for x in release.evaluate(good_facts(upgrade=dict(
            good_facts()['upgrade'], skipped=['DNS: x'])), cfg())}
        self.assertTrue(out['upgrade'].met)
        self.assertIn('0 failed, 1 skipped offline', out['upgrade'].evidence)

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

    def test_the_install_criterion_matches_the_ci_step_the_workflow_names(self):
        """The default ``install_from_zero`` pattern finds the step the workflow really runs."""
        wf = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          '.github', 'workflows', 'tests.yml')
        with open(wf, encoding='utf-8') as f:
            names = [ln.split('name:', 1)[1].strip() for ln in f if ln.strip().startswith('- name:')]
        steps = [('tests (3.12)', n, 'success') for n in names]
        pat = release.DEFAULTS['ci_steps']['install_from_zero']
        self.assertEqual(release.step_state(steps, pat), (True, True), names)
        out = {x.key: x for x in release.evaluate(good_facts(ci_steps=steps), cfg())}
        self.assertIn(f"'{pat}' green", out['install'].evidence)

    def test_a_cancelled_trunk_run_is_a_non_verdict(self):
        runs = [{'status': 'completed', 'conclusion': 'cancelled', 'headSha': 'c' * 40}] \
            + [{'status': 'completed', 'conclusion': 'success'}] * 12
        got = release.ci_runs('o/r', 'main', 10, lambda a: runs)
        self.assertEqual(len(got), 10)
        self.assertNotIn('cancelled', [r['conclusion'] for r in got])
        out = {x.key: x for x in release.evaluate(good_facts(ci_runs=got), cfg())}
        self.assertTrue(out['ci'].met)

    def test_steps_come_from_the_newest_run_that_ran_them(self):
        """The trunk's newest commit (a release/changelog commit) gets a light run only: criteria 4
        and 6 read the newest run that ran the named steps, not 'absent'."""
        runs = [{'databaseId': 2, 'conclusion': 'success'}, {'databaseId': 1, 'conclusion': 'success'}]
        jobs = {2: {'jobs': [{'name': 'release', 'steps': [{'name': 'tag it', 'conclusion': 'success'}]}]},
                1: {'jobs': [{'name': 'tests', 'steps': [
                    {'name': 'check generic', 'conclusion': 'success'},
                    {'name': 'the sample product, end to end', 'conclusion': 'success'},
                    {'name': 'asf install, zero to green', 'conclusion': 'success'}]}]}}
        c = cfg()
        steps = release.step_run_steps('o/r', runs, list(c['ci_steps'].values()),
                                       lambda a: jobs[int(a[2])])
        out = {x.key: x for x in release.evaluate(good_facts(ci_steps=steps), c)}
        self.assertIn("'asf install, zero to green' green", out['install'].evidence)
        self.assertIn("'check generic' green", out['generic'].evidence)
        none = release.step_run_steps('o/r', runs[:1], list(c['ci_steps'].values()),
                                      lambda a: jobs[int(a[2])])
        self.assertEqual(none, [('release', 'tag it', 'success')])   # nothing ran them: the newest

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
                  'changelog_notes': False},
            floor={'stale_prs': [('PR #9', 5 * 1440)]},
            seats=[seat_tick(m, 2, 8, 3) for m in range(0, 32, 5)])
        out = self.crit(f, c)
        self.assertEqual([k for k, v in out.items() if v.met], [])
        self.assertIn('1 hand fix commit(s)', out['stability'].evidence)
        self.assertIn('abc1234', out['stability'].evidence)
        self.assertIn('= 9.1', out['repair'].evidence)
        self.assertIn('9/10 green', out['ci'].evidence)
        self.assertIn("'asf install, zero to green' absent", out['install'].evidence)
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

    def test_no_blocking_list_is_na(self):
        out = self.crit(good_facts(), cfg())['blocking']
        self.assertTrue(out.met)
        self.assertEqual(out.evidence, 'n/a — no release.blocking list')


def minimal_facts(**over):
    """What a minimal product reads: nothing landed, no upgrade, no CI step of the framework's,
    a README with no release sections, no tag."""
    base = dict(items={}, repair={'sessions': 0, 'landed': 0, 'per_feature': None},
                upgrade={'upgrades': [], 'failed': [], 'torn': [], 'rollbacks': [], 'chain_breaks': []},
                ci_steps=[('tests', 'tests', 'success')],
                docs={'headings': ['Usage'], 'tag': None, 'changelog_section': False,
                      'changelog_notes': False})
    base.update(over)
    return good_facts(**base)


class ProductGateTest(unittest.TestCase):
    """F-0248 amendments 6 and 7: release readiness is a per-product gate — the framework's own
    criteria and the hand-fix rule are opt-in for a product; not applicable reads n/a, never red."""

    def crit(self, facts, c):
        return {x.key: x for x in release.evaluate(facts, c)}

    def test_max_hand_fixes_defaults_off(self):
        self.assertIsNone(release.DEFAULTS['max_hand_fixes'])
        self.assertIsNone(product_cfg()['max_hand_fixes'])
        hand = [{'sha': 'abc1234', 'date': '2026-09-24T10:00:00Z', 'author': 'Dev', 'subject': 'fix: by hand'}]
        out = self.crit(good_facts(hand_commits=hand), product_cfg())['stability']
        self.assertTrue(out.met)
        self.assertTrue(out.evidence.startswith('n/a — release.max_hand_fixes is off'), out.evidence)
        for off in ('off', False):
            self.assertIsNone(product_cfg(max_hand_fixes=off)['max_hand_fixes'])

    def test_a_release_block_that_sets_max_hand_fixes_keeps_the_rule(self):
        # the factory's own product file sets `max_hand_fixes: 0`: the rule stays on there
        hand = [{'sha': 'abc1234', 'date': '2026-09-24T10:00:00Z', 'author': 'Dev', 'subject': 'fix: by hand'}]
        for c in (product_cfg(max_hand_fixes=0), cfg()):
            out = self.crit(good_facts(hand_commits=hand), c)['stability']
            self.assertFalse(out.met)
            self.assertIn('1 hand fix commit(s)', out.evidence)

    def test_a_minimal_product_is_ready_with_every_framework_criterion_na(self):
        out = self.crit(minimal_facts(), product_cfg())
        self.assertEqual([k for k, v in out.items() if not v.met], [])
        na = sorted(k for k, v in out.items() if v.evidence.startswith('n/a'))
        self.assertEqual(na, ['blocking', 'docs', 'generic', 'install', 'repair', 'stability', 'upgrade'])
        self.assertEqual(out['ci'].evidence, '10/10 green')

    def test_no_forge_reads_ci_na(self):
        c = release.settings(env.Product('p', {}))
        out = self.crit(minimal_facts(ci_runs=None, ci_steps=None), c)['ci']
        self.assertTrue(out.met)
        self.assertIn('n/a — no hosted CI', out.evidence)
        c = release.settings(env.Product('p', {'repo_slug': 'o/r', 'ci': 'none'}))
        self.assertTrue(self.crit(minimal_facts(ci_runs=None, ci_steps=None), c)['ci'].met)
        # a hosted CI that did not answer is still red: it applies
        self.assertFalse(self.crit(minimal_facts(ci_runs=None), product_cfg())['ci'].met)

    def test_a_criterion_the_product_configures_applies(self):
        c = product_cfg(requires={'install': ['F-0001']}, ci_steps={'generic': 'scan names'},
                        min_upgrades=1, blocking=['F-0009'], readme_sections=['Install'])
        out = self.crit(minimal_facts(), c)
        self.assertEqual(sorted(k for k, v in out.items() if not v.met),
                         ['blocking', 'docs', 'generic', 'install', 'upgrade'])
        self.assertIn("'scan names' absent", out['generic'].evidence)

    def test_release_notes_are_checked_only_with_a_changelog(self):
        facts = minimal_facts(docs={'headings': ['Install'], 'tag': None, 'changelog_section': False,
                                    'changelog_notes': False})
        out = self.crit(facts, product_cfg(readme_sections=['Install']))['docs']
        self.assertTrue(out.met, out.evidence)
        self.assertIn('release notes n/a', out.evidence)
        opted = release.settings(env.Product('p', {'release': {'readme_sections': ['Install']},
                                                   'conventions': {'version': {'changelog': True}}}))
        self.assertFalse(self.crit(facts, opted)['docs'].met)
        # a product whose trunk keeps a changelog (its own release workflow writes it) is held to it
        kept = dict(facts['docs'], changelog_kept=True)
        self.assertFalse(self.crit(dict(facts, docs=kept), product_cfg(readme_sections=['Install']))['docs'].met)

    def test_self_tuning_off_is_na_unless_required(self):
        from asf import tune
        with mock.patch('asf.tune.criterion', return_value=(False, 'tune.enabled is off')), \
                mock.patch('asf.env.load_config', return_value={}):
            p = release.tune_criterion(env.Product('p', {}), 7, '2026-09-25T12:00:00Z')
        self.assertEqual((p.met, p.evidence), (True, 'n/a — tune.enabled is off'))
        req = {'tune': {'products': {'p': {'required': True}}}}
        with mock.patch('asf.tune.criterion', return_value=(False, 'tune.enabled is off')), \
                mock.patch('asf.env.load_config', return_value=req):
            f = release.tune_criterion(env.Product('p', {}), 7, '2026-09-25T12:00:00Z')
        self.assertEqual((f.met, f.evidence), (False, 'tune.enabled is off'))
        self.assertTrue(tune.required(env.Product('p', {}), {'tune': {'required': 'on'}}))
        self.assertFalse(tune.required(env.Product('p', {}), {}))

    def test_the_factorys_own_repo_and_a_fixture_with_the_same_config_gate_the_same(self):
        # "generic for any product, starting with itself": no criterion reads which repo it is
        block = {'max_hand_fixes': 0, 'blocking': ['F-0001'], 'requires': {'install': ['F-0001']}}
        with tempfile.TemporaryDirectory() as own, tempfile.TemporaryDirectory() as other:
            with open(os.path.join(own, 'pyproject.toml'), 'w') as f:
                f.write('[project]\nname = "asf-factory"\n')
            a = release.settings(env.Product('asf', {'repo_dir': own, 'repo_slug': 'o/r', 'release': block}))
            b = release.settings(env.Product('fixture', {'repo_dir': other, 'repo_slug': 'o/r', 'release': block}))
            self.assertEqual(a, b)
            for facts in (good_facts(), minimal_facts()):
                self.assertEqual([vars(c) for c in release.evaluate(facts, a)],
                                 [vars(c) for c in release.evaluate(facts, b)])


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
                                        'release': {'blocking': ['F-0001', 'F-0002'], 'min_upgrades': 3,
                                                    'readme_sections': ['Install', 'Upgrade'],
                                                    'requires': {'install': ['F-0001'],
                                                                 'generic': ['F-0001']}}})
            facts = Facts(items={}, sessions=[], ci=[], gates=[], runs=[], clutter={}, as_of='2026-09-25T12:00:00Z')
            out = release.compute(rec, product, now=NOW, git=git, gh_json=gh, log_dir=logs, facts=facts)
            met = {c['key']: c['met'] for c in out['criteria']}
            self.assertFalse(out['ready'])
            self.assertEqual(met, {'stability': True, 'repair': True, 'ci': True, 'install': False,
                                   'upgrade': False, 'generic': False, 'docs': False, 'blocking': False,
                                   'floor': True, 'seats': False, 'tune': True,
                                   'pr_ci': False})
            ev = {c['key']: c['evidence'] for c in out['criteria']}
            self.assertIn('1 auto-upgrade(s)', ev['upgrade'])       # dated 2026-09-24 12:00 UTC: in the window
            self.assertIn('F-0002 card', ev['blocking'])
            self.assertIn('CHANGELOG has v0.1.0 notes', ev['docs'])
            text = release.render(out)
            self.assertIn('NOT READY — 5/12 met', text)
            self.assertTrue(ev['repair'].startswith('n/a — nothing landed'), ev['repair'])
            self.assertTrue(ev['stability'].startswith('n/a — release.max_hand_fixes is off'))
            self.assertEqual(ev['tune'], 'n/a — tune.enabled is off')
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

    def test_status_row_only_with_a_release_block_for_every_product(self):
        from asf.views import status
        with tempfile.TemporaryDirectory() as d:
            plain = env.Product('p', {'repo_dir': d})
            self.assertIsNone(status.release_cell('/r', plain))
            with open(os.path.join(d, 'pyproject.toml'), 'w') as f:
                f.write('[project]\nname = "asf-factory"\n')
            self.assertIsNone(status.release_cell('/r', plain))    # the factory's own repo too
            gated = env.Product('p', {'repo_dir': d, 'release': {'window_days': 7}})
            with mock.patch('asf.release.cell', return_value='NOT READY — 3/8 met') as cell:
                self.assertEqual(status.release_cell('/r', gated), 'NOT READY — 3/8 met')
                cell.assert_called_once()



class RepairEvidenceTest(unittest.TestCase):
    def test_the_evidence_breaks_the_repair_load_down_by_kind(self):
        f = good_facts(repair={'sessions': 7, 'landed': 2, 'per_feature': 3.5,
                               'by_kind': {'correct': 4, 'review': 2, 'rebase': 1}})
        out = {x.key: x for x in release.evaluate(f, cfg())}
        self.assertFalse(out['repair'].met)
        self.assertIn('(correct 4, review 2, rebase 1)', out['repair'].evidence)


class FloorTest(unittest.TestCase):
    """Criterion 9: each leftover kind red past its limit, green under it, n/a when the product
    has no such thing, off by its key."""

    def crit(self, floor, c=None):
        return {x.key: x for x in release.evaluate(good_facts(floor=floor), c or cfg(blocking=['F-0001']))}['floor']

    def test_defaults(self):
        self.assertEqual(cfg()['floor'], {'stale_pr_days': 3, 'run_min': 30, 'heartbeat_factor': 2,
                                          'stage_factor': 3, 'branches': True})

    def test_a_stale_pr_past_three_days_is_red_under_it_green(self):
        self.assertFalse(self.crit({'stale_prs': [('PR #4', 3 * 1440 + 1)]}).met)
        ok = self.crit({'stale_prs': [('PR #4', 3 * 1440 - 1)]})
        self.assertTrue(ok.met)
        self.assertIn('0 stale PR(s)', ok.evidence)

    def test_an_orphan_run_past_thirty_minutes_is_red(self):
        red = self.crit({'runs': [('run 7 (batch/x, ref gone)', 31)]})
        self.assertFalse(red.met)
        self.assertIn('1 orphan CI run(s) (oldest run 7 (batch/x, ref gone) 31 min)', red.evidence)
        self.assertTrue(self.crit({'runs': [('run 7', 29)]}).met)

    def test_a_cloud_run_silent_past_two_heartbeats_is_red(self):
        self.assertFalse(self.crit({'cloud': [('code-t-0001', 11, 5.0)]}).met)
        self.assertTrue(self.crit({'cloud': [('code-t-0001', 9, 5.0)]}).met)

    def test_a_task_past_three_times_its_stage_limit_is_red(self):
        self.assertFalse(self.crit({'tasks': [('T-0001', 136, 45.0)]}).met)
        self.assertTrue(self.crit({'tasks': [('T-0001', 134, 45.0)]}).met)

    def test_an_expired_head_is_red(self):
        self.assertFalse(self.crit({'branches': [('census 2026-09-25', None)]}).met)
        self.assertTrue(self.crit({'branches': []}).met)

    def test_limits_come_from_config(self):
        c = cfg(blocking=['F-0001'], floor={'stale_pr_days': 10, 'run_min': '60'})
        self.assertTrue(self.crit({'stale_prs': [('PR #4', 5 * 1440)], 'runs': [('run 1', 45)]}, c).met)

    def test_a_kind_set_off_is_not_checked(self):
        c = cfg(blocking=['F-0001'], floor={'stale_pr_days': 'off', 'branches': 'off'})
        self.assertIsNone(c['floor']['stale_pr_days'])
        out = self.crit({'stale_prs': [('PR #4', 9 * 1440)], 'branches': [('x', None)]}, c)
        self.assertTrue(out.met)
        self.assertIn('stale PR(s) n/a', out.evidence)

    def test_a_minimal_product_is_all_na_never_red(self):
        out = self.crit({})
        self.assertTrue(out.met)
        self.assertEqual(out.evidence.count('n/a'), 5)

    def test_no_forge_reads_no_pr_and_no_run(self):
        with tempfile.TemporaryDirectory() as d:
            product = env.Product('p', {'repo_dir': d})       # no repo_slug: no forge
            gh = mock.Mock(side_effect=AssertionError('no forge call'))
            found = release.floor_facts(d, product, cfg(), NOW, git=lambda *a: '', gh_json=gh)
        self.assertIsNone(found['stale_prs'])
        self.assertIsNone(found['runs'])
        gh.assert_not_called()

    def test_orphan_runs_reads_gone_refs_and_closed_prs(self):
        runs = {'in_progress': [{'databaseId': 1, 'headBranch': 'batch/gone', 'event': 'push',
                                 'createdAt': '2026-09-25T11:00:00Z'},
                                {'databaseId': 2, 'headBranch': 'task/T-1', 'event': 'pull_request',
                                 'createdAt': '2026-09-25T11:00:00Z'},
                                {'databaseId': 3, 'headBranch': 'task/T-2', 'event': 'pull_request',
                                 'createdAt': '2026-09-25T11:00:00Z'}],
                'queued': []}

        def gh(args):
            if args[:2] == ['run', 'list']:
                return runs[args[args.index('--status') + 1]]
            return [{'headRefName': 'task/T-2'}]
        product = env.Product('p', {'repo_dir': '/x', 'repo_slug': 'o/r'})
        heads = 'a\trefs/heads/main\nb\trefs/heads/task/T-1\nc\trefs/heads/task/T-2\n'
        got = release._orphan_runs(product, NOW, lambda repo, *a: heads, gh)
        self.assertEqual([n for n, _a in got], ['run 1 (batch/gone, ref gone)', 'run 2 (task/T-1, PR closed)'])
        self.assertEqual(got[0][1], 60.0)


class SeatsTest(unittest.TestCase):
    """Criterion 10 over fixture tick streams."""

    def crit(self, ticks, c=None):
        return {x.key: x for x in release.evaluate(good_facts(seats=ticks), c or cfg(blocking=['F-0001']))}['seats']

    def test_a_31_minute_2_of_8_stretch_with_launchable_rows_is_red(self):
        ticks = [seat_tick(m, 2, 8, 3, cause='WAITS ON host load') for m in (0, 5, 10, 15, 20, 25, 31)]
        out = self.crit(ticks)
        self.assertFalse(out.met)
        self.assertIn('1 idle stretch(es); longest 2026-09-24T10:00 31 min at 2/8', out.evidence)
        self.assertIn('top cause: WAITS ON host load', out.evidence)

    def test_the_same_with_nothing_launchable_is_green(self):
        self.assertTrue(self.crit([seat_tick(m, 2, 8, 0) for m in (0, 5, 10, 15, 20, 25, 31)]).met)

    def test_29_minutes_is_green(self):
        self.assertTrue(self.crit([seat_tick(m, 2, 8, 3) for m in (0, 5, 10, 15, 20, 25, 29)]).met)

    def test_thresholds_come_from_config(self):
        ticks = [seat_tick(m, 2, 8, 3) for m in (0, 5, 10, 15, 20, 25, 31)]
        self.assertTrue(self.crit(ticks, cfg(blocking=['F-0001'], seats={'idle_min': 45})).met)
        self.assertTrue(self.crit(ticks, cfg(blocking=['F-0001'], seats={'min_pct': 20})).met)
        five = [seat_tick(m, 5, 8, 3) for m in (0, 5, 10, 15, 20, 25, 31)]   # 62 %
        self.assertFalse(self.crit(five, cfg(blocking=['F-0001'], seats={'min_pct': 70})).met)

    def test_one_local_seat_and_no_cloud_is_a_valid_shape(self):
        self.assertFalse(self.crit([seat_tick(m, 0, 1, 1) for m in range(0, 35, 5)]).met)
        self.assertTrue(self.crit([seat_tick(m, 1, 1, 1) for m in range(0, 35, 5)]).met)

    def test_no_reading_is_pending_never_met(self):
        out = self.crit([{'ts': '2026-09-24T10:00:00Z', 'tick': 1}])
        self.assertFalse(out.met)
        self.assertIn('pending', out.evidence)
        self.assertNotIn('n/a', out.evidence)

    def test_a_reading_before_the_window_is_not_read(self):
        f = good_facts(seats=[seat_tick(m, 2, 8, 3) for m in range(0, 40, 5)], since='2026-09-24T11:00:00Z')
        out = {x.key: x for x in release.evaluate(f, cfg(blocking=['F-0001']))}['seats']
        self.assertFalse(out.met)                  # the red stretch is not read: nothing in the window
        self.assertIn('pending', out.evidence)

    def test_the_row_is_printed(self):
        d = {'product': 'p', 'as_of': 'x', 'ready': False, 'criteria': [
            vars(c) for c in release.evaluate(good_facts(seats=[seat_tick(m, 2, 8, 3) for m in range(0, 35, 5)]),
                                              cfg(blocking=['F-0001']))]}
        text = release.render(d)
        self.assertIn('| 9 | Floor clean', text)
        self.assertIn('| 10 | Seats used', text)


class DoctorMirrorTest(unittest.TestCase):
    def test_doctor_mirrors_criteria_9_and_10(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, 'metrics', 'ticks'))
            with open(os.path.join(d, 'metrics', 'ticks', '2026-09-24.jsonl'), 'w') as fh:
                for m in range(0, 35, 5):
                    fh.write(json.dumps(seat_tick(m, 2, 8, 3)) + '\n')
            product = env.Product('p', {'repo_dir': d})
            rows = release.doctor_rows(product, root=d, now=NOW, git=lambda *a: '')
        self.assertEqual([r[0] for r in rows], ['release floor', 'release seats'])
        self.assertTrue(rows[0][1])
        self.assertFalse(rows[1][1])
        self.assertIn('1 idle stretch(es)', rows[1][2])

    def test_doctor_runs_the_mirror(self):
        from asf import doctor
        with mock.patch.object(release, 'doctor_rows', return_value=[('release floor', True, 'x')]):
            self.assertEqual(doctor.check_release_floor_seats(env.Product('p', {})), [('release floor', True, 'x')])
        with mock.patch.object(release, 'doctor_rows', side_effect=OSError('boom')):
            self.assertIn('not read', doctor.check_release_floor_seats(env.Product('p', {}))[0][2])


if __name__ == '__main__':
    unittest.main()
