"""tests.test_readme — the span grammar and the renderer (asf.views.readme)."""
import builtins
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from asf import cli, env
from asf.views import readme

PAGE = ('# Title\n'
        '\n'
        'We ran <!--asf:n sessions-->1,204<!--/asf:n--> sessions.\n'
        '\n'
        'Prose between.\n'
        '\n'
        '<!--asf:block scoreboard-->\n'
        '| Metric | Value |\n'
        '| a | 1 |\n'
        '<!--/asf:block-->\n'
        '\n'
        'The end.\n')


def facts_from(found):
    return {'numbers': {s.key: {'text': s.body} for s in found}}


class SpanTests(unittest.TestCase):
    def raises(self, text, *needles):
        with self.assertRaises(ValueError) as cm:
            readme.spans(text)
        for n in needles:
            self.assertIn(n, str(cm.exception))

    def test_no_marker_gives_empty(self):
        self.assertEqual(readme.spans('just prose\n'), [])

    def test_round_trip(self):
        found = readme.spans(PAGE)
        self.assertEqual([(s.kind, s.key) for s in found],
                         [('n', 'sessions'), ('block', 'scoreboard')])
        self.assertEqual(found[0].body, '1,204')
        self.assertEqual(found[1].body, '| Metric | Value |\n| a | 1 |\n')
        self.assertEqual(readme.render(PAGE, facts_from(found)), PAGE)

    def test_unclosed(self):
        self.raises('a <!--asf:n x-->1\n', "'x'", 'line 1')

    def test_unclosed_block(self):
        self.raises('a\n<!--asf:block b-->\nrow\n', "'b'", 'line 2')

    def test_close_with_no_open(self):
        self.raises('a\nb <!--/asf:n--> c\n', 'line 2')

    def test_nested(self):
        self.raises('<!--asf:n a-->1 <!--asf:n b-->2<!--/asf:n--><!--/asf:n-->', "'b'", "'a'")

    def test_inline_inside_block(self):
        self.raises('<!--asf:block a-->\nx <!--asf:n b-->1<!--/asf:n-->\n<!--/asf:block-->\n',
                    "'b'")

    def test_duplicate_key(self):
        self.raises('<!--asf:n a-->1<!--/asf:n--> <!--asf:n a-->2<!--/asf:n-->', "'a'")

    def test_block_marker_shares_line(self):
        self.raises('text <!--asf:block a-->\nrow\n<!--/asf:block-->\n', 'line 1')

    def test_block_close_shares_line(self):
        self.raises('<!--asf:block a-->\nrow <!--/asf:block-->\n', 'line 2')


class RenderTests(unittest.TestCase):
    def test_inline_leaves_sentence(self):
        out = readme.render(PAGE, {'numbers': {'sessions': {'text': '9'},
                                               'scoreboard': {'text': '| x |\n'}}})
        self.assertIn('We ran <!--asf:n sessions-->9<!--/asf:n--> sessions.\n', out)

    def test_block_rows_replaced_markers_stay(self):
        out = readme.render(PAGE, {'numbers': {'sessions': {'text': '1,204'},
                                               'scoreboard': {'text': '| x |\n| y |\n'}}})
        self.assertIn('<!--asf:block scoreboard-->\n| x |\n| y |\n<!--/asf:block-->\n', out)
        self.assertTrue(out.startswith('# Title\n') and out.endswith('The end.\n'))

    def test_missing_fact_names_key(self):
        with self.assertRaises(ValueError) as cm:
            readme.render(PAGE, {'numbers': {'sessions': {'text': '1'}}})
        self.assertIn('scoreboard', str(cm.exception))

    def test_unused_fact_names_key(self):
        facts = facts_from(readme.spans(PAGE))
        facts['numbers']['ghost'] = {'text': '0'}
        with self.assertRaises(ValueError) as cm:
            readme.render(PAGE, facts)
        self.assertIn('ghost', str(cm.exception))

    def test_idempotent(self):
        facts = {'numbers': {'sessions': {'text': '7'}, 'scoreboard': {'text': '| z |'}}}
        once = readme.render(PAGE, facts)
        self.assertEqual(readme.render(once, facts), once)


NOW = readme.metrics.dt.datetime(2026, 9, 24, 6, 11, 4, tzinfo=readme.metrics.dt.timezone.utc)


def card(id_, type_, parent=None, stage='spec', children=()):
    return {'id': id_, 'type': type_, 'title': 'Title %s' % id_, 'parent': parent, 'stage': stage,
            'children': list(children)}


def write_record(root, ci=(), sessions=(), ticks=(), items=None):
    if items is None:
        items = {'E-0001': card('E-0001', 'epic', children=['F-0001', 'F-0002']),
                 'F-0001': card('F-0001', 'feature', 'E-0001', 'landed', ['T-0001']),
                 'F-0002': card('F-0002', 'feature', 'E-0001', 'spec'),
                 'T-0001': card('T-0001', 'task', 'F-0001')}
    with open(os.path.join(root, 'index.json'), 'w', encoding='utf-8') as f:
        json.dump({'generated': '2026-09-24T05:52:11Z', 'items': items}, f)
    for stream, events in (('ci', ci), ('sessions', sessions), ('ticks', ticks)):
        if not events:
            continue
        os.makedirs(os.path.join(root, 'metrics', stream))
        for day in sorted({e['ts'][:10] for e in events}):
            with open(os.path.join(root, 'metrics', stream, day + '.jsonl'), 'w') as f:
                for e in events:
                    if e['ts'][:10] == day:
                        f.write(json.dumps(e) + '\n')


CI = [{'ts': '2026-09-20T09:00:00Z', 'run': 1, 'attempt': 1, 'conclusion': 'success',
       'minutes': 4, 'items': ['T-0001']},
      {'ts': '2026-09-21T09:00:00Z', 'run': 2, 'attempt': 1, 'conclusion': 'failure',
       'minutes': 6, 'items': None}]
SESSIONS = [{'ts': '2026-09-20T08:00:00Z', 'task': 'a', 'item': 'T-0001', 'kind': 'code',
             'result': 'done', 'minutes': 90, 'usd': 30.0},
            {'ts': '2026-09-22T08:00:00Z', 'task': 'b', 'item': 'F-0002', 'kind': 'spec',
             'result': 'done', 'minutes': 30, 'usd': 10.0}]
TICKS = [{'ts': '2026-09-23T08:00:00Z', 'tick': 1}]
KEYS = ('window', 'items', 'features_shipped', 'sessions', 'session_hours', 'spend_usd',
        'usd_per_feature', 'ci_runs', 'ci_green_pct', 'ticks', 'scoreboard', 'cost_per_feature',
        'commands')


class FactsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.join(tmp.name, 'ASF-backlog')
        os.makedirs(self.root)
        self.repo = os.path.join(tmp.name, 'repo')

    def facts(self):
        return readme.facts(self.root, self.repo, now=NOW)

    def test_envelope(self):
        write_record(self.root, CI, SESSIONS, TICKS)
        f = self.facts()
        self.assertEqual((f['generated'], f['record'], f['record_generated'], f['day']),
                         ('2026-09-24T06:11:04Z', 'ASF-backlog', '2026-09-24T05:52:11Z',
                          '2026-09-24'))
        json.dumps(f)

    def test_every_key_and_no_other(self):
        write_record(self.root, CI, SESSIONS, TICKS)
        self.assertEqual(sorted(self.facts()['numbers']), sorted(KEYS))

    def test_values(self):
        write_record(self.root, CI, SESSIONS, TICKS)
        n = self.facts()['numbers']
        text = {k: v['text'] for k, v in n.items()}
        self.assertEqual(text['window'], '2026-09-20 … 2026-09-23 (4 days)')
        self.assertEqual((text['items'], text['features_shipped'], text['sessions']), ('4', '1', '2'))
        self.assertEqual((text['session_hours'], text['spend_usd']), ('2.0', '$40'))
        self.assertEqual(n['usd_per_feature']['value'], 30.0)
        self.assertEqual(text['usd_per_feature'], '$30')
        self.assertEqual((text['ci_runs'], text['ci_green_pct'], text['ticks']), ('2', '50%', '1'))

    def test_every_entry_has_a_source_naming_a_record_path(self):
        write_record(self.root, CI, SESSIONS, TICKS)
        for key, entry in self.facts()['numbers'].items():
            self.assertTrue(entry['source'], key)
            if key == 'commands':
                self.assertEqual(entry['source'], 'cli')
                continue
            for part in entry['source'].split(' + '):
                while not os.path.exists(os.path.join(self.root, part)) and '.' in part:
                    part = part.rsplit('.', 1)[0]
                self.assertTrue(os.path.exists(os.path.join(self.root, part)), (key, part))

    def test_empty_streams_render_dash_not_zero(self):
        write_record(self.root)
        n = self.facts()['numbers']
        for key in ('window', 'sessions', 'session_hours', 'spend_usd', 'usd_per_feature',
                    'ci_runs', 'ci_green_pct', 'ticks', 'cost_per_feature'):
            self.assertEqual((n[key]['text'], n[key]['value']), ('—', None), key)
        self.assertIn('| — |', n['scoreboard']['text'])

    def test_empty_record_has_no_items(self):
        write_record(self.root, items={})
        n = self.facts()['numbers']
        self.assertEqual((n['items']['text'], n['features_shipped']['text']), ('—', '—'))

    def test_no_shipped_feature_has_no_price(self):
        items = {'F-0002': card('F-0002', 'feature', None, 'spec')}
        write_record(self.root, CI, SESSIONS, TICKS, items=items)
        n = self.facts()['numbers']
        self.assertEqual(n['features_shipped']['value'], 0)
        self.assertEqual(n['usd_per_feature']['text'], '—')

    def test_cost_per_feature_is_shipped_only_and_trimmed(self):
        items = {'E-0001': card('E-0001', 'epic', children=['F-%04d' % i for i in range(1, 8)])}
        sessions = []
        for i in range(1, 8):
            fid = 'F-%04d' % i
            items[fid] = card(fid, 'feature', 'E-0001', 'landed' if i != 7 else 'spec')
            sessions.append({'ts': '2026-09-20T08:00:00Z', 'task': 't%d' % i, 'item': fid,
                             'kind': 'code', 'result': 'done', 'usd': float(i)})
        write_record(self.root, sessions=sessions, items=items)
        entry = self.facts()['numbers']['cost_per_feature']
        lines = entry['text'].splitlines()
        self.assertTrue(lines[0].startswith('| Feature |'))
        self.assertTrue(lines[1].startswith('|---|'))
        self.assertEqual(entry['value'], ['F-0006', 'F-0005', 'F-0004', 'F-0003', 'F-0002'])
        self.assertEqual(len(lines), 7)
        self.assertNotIn('All', entry['text'])

    def test_commands_reads_the_parser(self):
        write_record(self.root)
        entry = self.facts()['numbers']['commands']
        rows = readme._commands_rows(cli.build_parser())
        self.assertEqual(entry['value'], [n for n, _h in rows])
        self.assertIn('new', entry['value'])
        self.assertEqual(len(entry['text'].splitlines()), len(rows) + 2)

    def test_scoreboard_lists_the_scalars(self):
        write_record(self.root, CI, SESSIONS, TICKS)
        text = self.facts()['numbers']['scoreboard']['text']
        self.assertEqual(len(text.splitlines()), len(readme.SCORE_ROWS) + 2)
        self.assertIn('| Spend | $40 | `metrics/sessions.usd` |', text)

    def test_renders_into_a_page(self):
        write_record(self.root, CI, SESSIONS, TICKS)
        f = self.facts()
        page = ''.join('<!--asf:n %s-->x<!--/asf:n-->\n' % k for k in KEYS
                       if not f['numbers'][k]['text'].endswith('\n'))
        page += ''.join('<!--asf:block %s-->\nx\n<!--/asf:block-->\n' % k for k in KEYS
                        if f['numbers'][k]['text'].endswith('\n'))
        out = readme.render(page, f)
        self.assertIn('2026-09-20 … 2026-09-23 (4 days)', out)

    def test_opens_nothing_outside_the_record(self):
        write_record(self.root, CI, SESSIONS, TICKS)
        real_open, real_walk = builtins.open, os.walk
        root = os.path.realpath(self.root)

        def guard(path):
            if isinstance(path, (str, bytes, os.PathLike)) and not isinstance(path, int):
                p = os.path.realpath(os.fsdecode(path))
                self.assertTrue(p == root or p.startswith(root + os.sep), p)

        def fake_open(path, *a, **kw):
            guard(path)
            return real_open(path, *a, **kw)

        def fake_walk(path, *a, **kw):
            guard(path)
            return real_walk(path, *a, **kw)

        with mock.patch('builtins.open', fake_open), mock.patch('os.walk', fake_walk):
            self.facts()


class CommandTests(unittest.TestCase):
    """``asf readme`` — refresh, check, json (§2.3, PD10)."""

    SPANNED = ('# Title\n\n'
               '## The argument\n\n'
               'We ran <!--asf:n sessions-->9<!--/asf:n--> sessions.\n\n'
               '## The mental model\n\n'
               'Some prose.\n\n'
               '## The manual\n\n'
               '<!--asf:block scoreboard-->\n'
               '| Metric | Value |\n'
               '| a | 1 |\n'
               '<!--/asf:block-->\n')
    SPANLESS = '# Title\n\nNo spans here.\n'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, 'repo')
        os.makedirs(os.path.join(self.repo, 'docs'))
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp.name, 'ASF_HOME')
        os.makedirs(env.ASF_HOME)
        self.addCleanup(setattr, env, 'ASF_HOME', self._orig_home)
        orig_product_env = os.environ.pop('ASF_PRODUCT', None)
        self.addCleanup(lambda: orig_product_env is not None
                        and os.environ.__setitem__('ASF_PRODUCT', orig_product_env))

    def _write(self, name, text):
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)

    def _facts(self):
        return {'generated': '2026-09-24T06:11:04Z', 'record': 'x', 'record_generated': 'y',
                'day': '2026-09-24',
                'numbers': {'sessions': {'value': 9, 'text': '9', 'source': 'metrics/sessions'},
                            'scoreboard': {'value': None,
                                          'text': '| Metric | Value |\n| a | 1 |\n',
                                          'source': 'index.json'}}}

    def _run(self, argv):
        out = io.StringIO()
        args = cli.build_parser().parse_args(['readme'] + argv)
        with contextlib.redirect_stdout(out):
            rc = readme.cmd_readme(args, root=self.repo)
        return rc, out.getvalue()

    def test_check_on_a_sound_page_is_silent_and_green(self):
        self._write('README.md', self.SPANNED)
        self._write('docs/readme-numbers.json', json.dumps(self._facts()))
        self.assertEqual(self._run(['--check']), (0, ''))

    def test_check_on_a_hand_edited_span_exits_1_naming_the_key(self):
        self._write('README.md', self.SPANNED.replace('-->9<!--', '-->99<!--'))
        self._write('docs/readme-numbers.json', json.dumps(self._facts()))
        rc, out = self._run(['--check'])
        self.assertEqual(rc, 1)
        self.assertIn('sessions', out)

    def test_check_needs_no_record(self):
        """No ``config.yaml``, no ``products/``: ``env.load_product`` cannot resolve, and
        ``--check`` still runs off the committed facts alone (PD7)."""
        self._write('README.md', self.SPANNED)
        self._write('docs/readme-numbers.json', json.dumps(self._facts()))
        self.assertEqual(self._run([]), (0, ''))

    def test_json_is_the_documented_shape(self):
        self._write('README.md', self.SPANNED)
        self._write('docs/readme-numbers.json', json.dumps(self._facts()))
        rc, out = self._run(['--json'])
        data = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertEqual(set(data), {'ok', 'complaints', 'day', 'stale_days'})
        self.assertEqual((data['ok'], data['complaints'], data['day']), (True, [], '2026-09-24'))
        self.assertIsInstance(data['stale_days'], int)

    def test_spanless_readme_is_exit_0_on_every_form(self):
        self._write('README.md', self.SPANLESS)
        for argv in ([], ['--check'], ['--json'], ['--refresh']):
            rc, out = self._run(argv)
            self.assertEqual(rc, 0, argv)
            if '--json' in argv:
                self.assertEqual(json.loads(out), {'ok': True, 'complaints': [], 'day': None,
                                                    'stale_days': None})
            else:
                self.assertIn('no spans', out)

    def test_refresh_writes_both_files_and_is_a_no_op_the_second_time(self):
        record = os.path.join(self.tmp.name, 'backlog')
        os.makedirs(record)
        write_record(record, CI, SESSIONS, TICKS)
        f = readme.facts(record, self.repo, now=NOW)
        page = ''.join('<!--asf:n %s-->x<!--/asf:n-->\n' % k for k in KEYS
                       if not f['numbers'][k]['text'].endswith('\n'))
        page += ''.join('<!--asf:block %s-->\nx\n<!--/asf:block-->\n' % k for k in KEYS
                        if f['numbers'][k]['text'].endswith('\n'))
        self._write('README.md', page)
        os.makedirs(os.path.join(env.ASF_HOME, 'products'))
        with open(env.product_path('sample'), 'w') as fh:
            fh.write('product: sample\nrepo_slug: x/y\nrepo_dir: %s\n'
                     'main: main\nbacklog_dir: %s\n' % (self.repo, record))
        argv = ['--product', 'sample', '--refresh']
        with mock.patch.object(readme.metrics, 'now_utc', return_value=NOW):
            rc1, out1 = self._run(argv)
            rc2, out2 = self._run(argv)
        self.assertEqual(rc1, 0)
        self.assertIn('rewritten', out1)
        facts_path = os.path.join(self.repo, 'docs', 'readme-numbers.json')
        self.assertTrue(os.path.isfile(facts_path))
        with open(os.path.join(self.repo, 'README.md'), encoding='utf-8') as fh:
            rendered = fh.read()
        with open(facts_path, encoding='utf-8') as fh:
            facts_data = json.load(fh)
        self.assertEqual(readme.render(rendered, facts_data), rendered)
        self.assertEqual(rc2, 0)
        self.assertIn('unchanged', out2)
        self.assertNotIn('rewritten', out2)


if __name__ == '__main__':
    unittest.main()
