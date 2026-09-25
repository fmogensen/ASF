"""asf.redact — the one scanner (F-0075 §2.1, S-7050). Every temporary repo and ``ASF_HOME`` is
this test's own; none reads the operator's real ``~/.ASF``. Every name and token a test needs is
built from parts, so this file's own tracked source passes the gate (as ``tests/test_check_generic
.py`` already does for names)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from asf import env, redact

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git(args, cwd):
    subprocess.run(['git'] + args, cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(path):
    os.makedirs(path, exist_ok=True)
    _git(['init', '-q'], path)
    _git(['config', 'user.email', 'a@example.com'], path)
    _git(['config', 'user.name', 'a'], path)


def _write(path, rel, text):
    full = os.path.join(path, rel)
    d = os.path.dirname(full)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(full, 'w', encoding='utf-8') as f:
        f.write(text)
    return full


def _commit(path, message):
    _git(['add', '-A'], path)
    _git(['commit', '-q', '-m', message], path)


def _rev_parse(path, rev='HEAD'):
    out = subprocess.run(['git', 'rev-parse', rev], cwd=path, capture_output=True, text=True,
                         check=True)
    return out.stdout.strip()


def _cli_env(home):
    e = dict(os.environ)
    e['PYTHONPATH'] = REPO_ROOT
    e['ASF_HOME'] = home
    e.pop('ASF_PRODUCT', None)
    return e


def _run_cli(args, cwd, home, stdin=None):
    return subprocess.run([sys.executable, '-m', 'asf.redact'] + args, cwd=cwd,
                          env=_cli_env(home), input=stdin, capture_output=True, text=True)


class PatternTests(unittest.TestCase):
    def test_pool_account_names_are_patterns_without_a_list(self):
        cfg = {'worker_pool': {'accounts': [{'name': 'acct-z'}, {'name': 'acct-q'}]}}
        pats = redact.patterns(cfg=cfg, environ={})
        sources = {(p.kind, p.source) for p in pats}
        self.assertIn(('name', redact.NAME_SOURCE_POOL), sources)
        findings = redact.scan_text('f.txt', 'seen from acct-z today', pats)
        self.assertTrue(any(f.source == redact.NAME_SOURCE_POOL for f in findings))

    def test_private_list_and_repo_list_are_read_in_the_forbidden_names_format(self):
        tmp = tempfile.mkdtemp(prefix='redact_test_')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = os.path.join(tmp, 'home')
        os.makedirs(home)
        private_word = 'code' + 'name'
        _write(home, 'redact-names.txt', f'# a private list\n\n\\b{private_word}\\b\n')
        repo = os.path.join(tmp, 'repo')
        repo_word = 'proj' + 'ectx'
        _write(repo, os.path.join('tools', 'forbidden-names.txt'), f'\\b{repo_word}\\b\n')

        old_home = env.ASF_HOME
        env.ASF_HOME = home
        try:
            pats = redact.patterns(repo=repo, cfg={}, environ={})
        finally:
            env.ASF_HOME = old_home

        sources = {p.source for p in pats}
        self.assertIn(redact.NAME_SOURCE_PRIVATE, sources)
        self.assertIn(redact.NAME_SOURCE_REPO, sources)
        findings = redact.scan_text('f.txt', f'mentions {private_word} and {repo_word}', pats)
        self.assertEqual({f.source for f in findings},
                         {redact.NAME_SOURCE_PRIVATE, redact.NAME_SOURCE_REPO})

    def test_no_config_and_no_list_is_empty_not_an_error(self):
        tmp = tempfile.mkdtemp(prefix='redact_test_')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        old_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(tmp, 'no-such-home')
        try:
            pats = redact.patterns(repo=os.path.join(tmp, 'no-such-repo'), cfg={}, environ={})
        finally:
            env.ASF_HOME = old_home
        self.assertEqual([p for p in pats if p.kind == 'name'], [])
        self.assertTrue(any(p.kind == 'secret' for p in pats))  # the built-ins are not names

    def test_each_builtin_rule_matches_a_token_built_from_parts(self):
        pats = redact.patterns(cfg={}, environ={})
        tokens = {
            'private-key': '-----BEGIN ' + 'RSA PRIVATE KEY' + '-----',
            'aws-access-key': 'AKIA' + 'ABCDEFGHIJKLMNOP',
            'github-token': 'ghp_' + 'a' * 36,
            'anthropic-key': 'sk-ant-' + 'a' * 20,
            'openai-style-key': 'sk-' + 'a' * 32,
            'slack-token': 'xoxb-' + '1' * 10,
            'jwt': 'eyJ' + 'a' * 10 + '.' + 'eyJ' + 'a' * 10 + '.',
            'url-password': 'https://user' + ':' + 'pass' + '@host.example/path',
        }
        for rule_id, token in tokens.items():
            findings = redact.scan_text('f.txt', f'line has {token} inline', pats)
            self.assertTrue(
                any(f.kind == 'secret' and f.source == f'rule:{rule_id}' for f in findings),
                f'{rule_id} did not match its own token')

    def test_a_secret_env_value_is_matched_and_reported_by_name_only(self):
        secret_value = 'sekrit' + 'value123456'
        environ = {'MY_APP_TOKEN': secret_value, 'SHORT_KEY': 'abc'}
        pats = redact.patterns(cfg={}, environ=environ)
        findings = redact.scan_text('f.txt', f'oops {secret_value} leaked', pats)
        self.assertTrue(any(f.source == 'env:MY_APP_TOKEN' for f in findings))
        self.assertFalse(any(f.source == 'env:SHORT_KEY' for f in findings))
        self.assertNotIn(secret_value, [f.source for f in findings])


class ScanTests(unittest.TestCase):
    def test_staged_scans_added_lines_only_with_new_file_line_numbers(self):
        tmp = tempfile.mkdtemp(prefix='redact_test_')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        _init_repo(tmp)
        _write(tmp, 'f.txt', 'one\ntwo\n')
        _commit(tmp, 'seed')
        secret = 'AKIA' + 'ABCDEFGHIJKLMNOP'
        _write(tmp, 'f.txt', f'one\ntwo\nthree\n{secret}\n')
        _git(['add', '-A'], tmp)

        pats = redact.patterns(cfg={}, environ={})
        findings = redact.scan_staged(tmp, pats)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, 'f.txt')
        self.assertEqual(findings[0].line, 4)
        self.assertEqual(findings[0].kind, 'secret')

    def test_an_old_line_does_not_block_a_new_commit(self):
        tmp = tempfile.mkdtemp(prefix='redact_test_')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        _init_repo(tmp)
        secret = 'AKIA' + 'ABCDEFGHIJKLMNOP'
        _write(tmp, 'f.txt', f'{secret}\nharmless\n')
        _commit(tmp, 'an old secret, already committed')
        _write(tmp, 'g.txt', 'a brand new, clean file\n')
        _git(['add', '-A'], tmp)

        pats = redact.patterns(cfg={}, environ={})
        findings = redact.scan_staged(tmp, pats)

        self.assertEqual(findings, [])

    def test_unpublished_scans_every_commit_no_remote_has_and_its_message(self):
        tmp = tempfile.mkdtemp(prefix='redact_test_')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        origin = os.path.join(tmp, 'origin.git')
        _git(['init', '-q', '--bare', '-b', 'main', origin], tmp)
        clone = os.path.join(tmp, 'clone')
        _git(['clone', '-q', origin, clone], tmp)
        _git(['config', 'user.email', 'a@example.com'], clone)
        _git(['config', 'user.name', 'a'], clone)
        _write(clone, 'seed.txt', 'a published, clean seed\n')
        _commit(clone, 'seed')
        _git(['push', '-q', 'origin', 'HEAD'], clone)

        secret = 'AKIA' + 'QRSTUVWXYZABCDEF'
        _write(clone, 'a.txt', 'a clean file\n')
        _commit(clone, f'an unpublished commit whose message has {secret} in it')
        _write(clone, 'b.txt', f'and its content has {secret} too\n')
        _commit(clone, 'a second unpublished commit')

        pats = redact.patterns(cfg={}, environ={})
        findings = redact.scan_unpublished(clone, 'HEAD', pats)

        paths = {f.path for f in findings}
        self.assertIn('b.txt', paths)
        self.assertTrue(any(p.startswith('commit ') and p.endswith(' message') for p in paths))
        self.assertFalse(any('seed' in p for p in paths))  # the published commit is not scanned

    def test_signed_off_by_and_co_authored_by_trailers_are_not_scanned(self):
        tmp = tempfile.mkdtemp(prefix='redact_test_')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        _init_repo(tmp)
        account_name = 'acct-' + 'trailer'
        cfg = {'worker_pool': {'accounts': [{'name': account_name}]}}
        _write(tmp, 'f.txt', 'clean content\n')
        message = ('a clean subject\n\nbody with nothing forbidden\n\n'
                  f'Signed-off-by: {account_name} <{account_name}@example.com>\n'
                  f'Co-Authored-By: {account_name} <{account_name}@example.com>\n')
        _git(['add', '-A'], tmp)
        _git(['commit', '-q', '-m', message], tmp)

        pats = redact.patterns(cfg=cfg, environ={})
        findings = redact.scan_unpublished(tmp, 'HEAD', pats)

        self.assertEqual(findings, [])

    def test_binary_files_are_skipped(self):
        tmp = tempfile.mkdtemp(prefix='redact_test_')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        _init_repo(tmp)
        secret = 'AKIA' + 'ABCDEFGHIJKLMNOP'
        with open(os.path.join(tmp, 'bin.dat'), 'wb') as f:
            f.write(secret.encode() + b'\x00\x01\x02')
        _git(['add', '-A'], tmp)

        pats = redact.patterns(cfg={}, environ={})
        findings = redact.scan_staged(tmp, pats)

        self.assertEqual(findings, [])

    def test_tree_skips_license(self):
        tmp = tempfile.mkdtemp(prefix='redact_test_')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        _init_repo(tmp)
        account_name = 'acct-' + 'license'
        cfg = {'worker_pool': {'accounts': [{'name': account_name}]}}
        _write(tmp, 'LICENSE', f'Copyright {account_name}, all rights reserved.\n')
        _write(tmp, 'notes.md', 'a perfectly generic note\n')
        _commit(tmp, 'seed')

        pats = redact.patterns(cfg=cfg, environ={})
        findings = redact.scan_tree(tmp, pats)

        self.assertEqual(findings, [])


class OutputTests(unittest.TestCase):
    def test_a_finding_never_prints_the_matched_text(self):
        tmp = tempfile.mkdtemp(prefix='redact_test_')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = os.path.join(tmp, 'home')
        os.makedirs(home)
        repo = os.path.join(tmp, 'repo')
        _init_repo(repo)
        account_name = 'acct-' + 'noprint'
        secret = 'AKIA' + 'GHIJKLMNOPQRSTUV'
        env_value = 'topsecretvalue99'
        _write(home, 'config.yaml', f'worker_pool:\n  accounts:\n    - name: {account_name}\n')
        _write(repo, 'f.txt', f'{account_name} used {secret} and {env_value}\n')
        _git(['add', '-A'], repo)

        cli_env = _cli_env(home)
        cli_env['MY_SECRET_TOKEN'] = env_value
        r = subprocess.run([sys.executable, '-m', 'asf.redact', '--staged'], cwd=repo,
                           env=cli_env, capture_output=True, text=True)

        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        blob = r.stdout + r.stderr
        self.assertNotIn(account_name, blob)
        self.assertNotIn(secret, blob)
        self.assertNotIn(env_value, blob)

    def test_there_is_no_inline_allow_marker(self):
        tmp = tempfile.mkdtemp(prefix='redact_test_')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        _init_repo(tmp)
        secret = 'AKIA' + 'ABCDEFGHIJKLMNOP'
        _write(tmp, 'f.txt', f'{secret}  # redact: allow\n')
        _git(['add', '-A'], tmp)

        pats = redact.patterns(cfg={}, environ={})
        findings = redact.scan_staged(tmp, pats)

        self.assertEqual(len(findings), 1)


class CliTests(unittest.TestCase):
    def test_exit_codes_clean_0_refused_1_not_a_repo_2(self):
        tmp = tempfile.mkdtemp(prefix='redact_test_')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = os.path.join(tmp, 'home')
        os.makedirs(home)

        clean_repo = os.path.join(tmp, 'clean')
        _init_repo(clean_repo)
        _write(clean_repo, 'f.txt', 'a perfectly generic line\n')
        _git(['add', '-A'], clean_repo)
        r = _run_cli(['--staged'], clean_repo, home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('redact: clean', r.stdout)

        dirty_repo = os.path.join(tmp, 'dirty')
        _init_repo(dirty_repo)
        secret = 'AKIA' + 'ABCDEFGHIJKLMNOP'
        _write(dirty_repo, 'f.txt', f'{secret}\n')
        _git(['add', '-A'], dirty_repo)
        r = _run_cli(['--staged'], dirty_repo, home)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('redact: refused', r.stdout)

        not_repo = os.path.join(tmp, 'not-a-repo')
        os.makedirs(not_repo)
        r = _run_cli(['--staged'], not_repo, home)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)

    def test_pre_push_reads_stdin_and_skips_a_deletion(self):
        tmp = tempfile.mkdtemp(prefix='redact_test_')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = os.path.join(tmp, 'home')
        os.makedirs(home)
        repo = os.path.join(tmp, 'repo')
        _init_repo(repo)
        _write(repo, 'seed.txt', 'seed\n')
        _commit(repo, 'seed')

        secret = 'AKIA' + 'ABCDEFGHIJKLMNOP'
        _write(repo, 'g.txt', f'{secret}\n')
        _commit(repo, 'has a secret')
        sha = _rev_parse(repo)
        zero = '0' * 40

        stdin_text = (f'refs/heads/main {sha} refs/heads/main {zero}\n'
                      f'refs/heads/deleted {zero} refs/heads/deleted {zero}\n')
        r = _run_cli(['--pre-push'], repo, home, stdin=stdin_text)

        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('g.txt', r.stdout)


class LedgerTests(unittest.TestCase):
    def test_a_refusal_appends_one_ledger_line_per_finding_without_the_text(self):
        tmp = tempfile.mkdtemp(prefix='redact_test_')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        old_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(tmp, 'home')
        try:
            secret = 'AKIA' + 'ABCDEFGHIJKLMNOP'
            findings = [
                redact.Finding('a.txt', 1, 'secret', 'rule:aws-access-key'),
                redact.Finding('b.txt', 2, 'secret', 'rule:aws-access-key'),
            ]
            with self.assertRaises(redact.Refused):
                redact.gate(findings, 'cli', product='sample')

            ledger = os.path.join(env.ASF_HOME, 'state', 'sample', 'redactions.jsonl')
            with open(ledger, encoding='utf-8') as f:
                lines = [json.loads(l) for l in f if l.strip()]
            self.assertEqual(len(lines), 2)
            self.assertEqual([l['path'] for l in lines], ['a.txt', 'b.txt'])
            for entry in lines:
                self.assertNotIn(secret, json.dumps(entry))
        finally:
            env.ASF_HOME = old_home

    def test_no_product_writes_no_ledger(self):
        tmp = tempfile.mkdtemp(prefix='redact_test_')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        old_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(tmp, 'home')
        try:
            with self.assertRaises(redact.Refused):
                redact.gate([redact.Finding('a.txt', 1, 'secret', 'rule:x')], 'cli', product=None)
            self.assertFalse(os.path.isdir(os.path.join(env.ASF_HOME, 'state')))
        finally:
            env.ASF_HOME = old_home


if __name__ == '__main__':
    unittest.main()


class EnvSecretEdges(unittest.TestCase):
    """B-0081: a false positive here blocks every commit and every push, so a variable named for
    git's own config injection, or for a location, is not a credential — and neither is a value
    that is a path or a dotted identifier, whatever the variable is called."""

    def test_git_config_injection_is_not_a_secret(self):
        self.assertFalse(redact.env_value_is_secret('GIT_CONFIG_KEY_0', 'init.defaultBranch'))
        self.assertFalse(redact.env_value_is_secret('GIT_CONFIG_VALUE_0', 'a-branch-name-here'))
        self.assertFalse(redact.env_value_is_secret('GIT_CONFIG_COUNT', '000000000001'))

    def test_a_location_is_not_a_credential(self):
        for name in ('SSH_KEY_PATH', 'TOKEN_FILE', 'CREDENTIAL_DIR'):
            self.assertFalse(redact.env_value_is_secret(name, '/Users/x/.ssh/id_rsa'), name)

    def test_a_dotted_identifier_is_not_a_credential(self):
        self.assertFalse(redact.env_value_is_secret('MY_SECRET', 'some.dotted.identifier'))

    def test_a_real_credential_is_still_caught(self):
        self.assertTrue(redact.env_value_is_secret('AWS_SECRET_ACCESS_KEY', 'AKIA' + 'Q' * 16))
        self.assertTrue(redact.env_value_is_secret('GITHUB_TOKEN', 'ghp_' + 'a' * 36))
        self.assertTrue(redact.env_value_is_secret('API_KEY', 'sk-' + 'b' * 32))

    def test_a_short_value_is_never_a_pattern(self):
        self.assertFalse(redact.env_value_is_secret('API_KEY', 'short'))

    def test_the_tracked_tree_scans_clean_under_the_hermetic_env(self):
        # the exact shape that fired: the suite runs with GIT_CONFIG_KEY_0 set
        with mock.patch.dict(os.environ, {'GIT_CONFIG_KEY_0': 'init.defaultBranch'}):
            pats = redact.patterns(environ=os.environ)
        self.assertFalse([p for p in pats if p.source == 'env:GIT_CONFIG_KEY_0'])


# ---- pre-push against a stale view of origin -------------------------------------

_WORD = 'zorb' + 'lax'  # a protected name, built so this file never holds it whole


def _build_stale_view(root):
    """A bare ``origin``; ``other`` pushed a commit adding the protected name (already published);
    ``worker`` pulled it, then its remote-tracking ref was put back to the seed — the stale view
    of a worktree that never fetched the trunk tip it builds on."""
    origin = os.path.join(root, 'origin.git')
    _git(['init', '-q', '--bare', '-b', 'main', origin], root)
    other, worker = os.path.join(root, 'other'), os.path.join(root, 'worker')
    for path in (other, worker):
        _git(['clone', '-q', origin, path], root)
        _git(['config', 'user.email', 'a@example.com'], path)
        _git(['config', 'user.name', 'a'], path)
        _git(['checkout', '-q', '-B', 'main'], path)
    _write(other, 'seed.txt', 'seed\n')
    _commit(other, 'seed')
    _git(['push', '-q', 'origin', 'main'], other)
    seed = _rev_parse(other)
    _write(other, os.path.join('plans', 'p.md'), f'a plan naming {_WORD}\n')
    _commit(other, 'a published plan')
    _git(['push', '-q', 'origin', 'main'], other)
    _git(['pull', '-q', 'origin', 'main'], worker)
    _git(['update-ref', 'refs/remotes/origin/main', seed], worker)
    _write(root, 'names.txt', f'\\b{_WORD}\\b\n')


_STALE = None


def _stale_view():
    global _STALE
    if _STALE is None:
        from tests.gitfixture import Template
        _STALE = Template(_build_stale_view, prefix='redact_stale_')
    return _STALE.fresh()


class PrePushStaleViewTests(unittest.TestCase):
    def setUp(self):
        self.root = _stale_view()
        self.worker = os.path.join(self.root, 'worker')
        self.pats = redact.patterns(cfg={}, environ={},
                                    extra=(os.path.join(self.root, 'names.txt'),))

    def _push_line(self, remote_sha='0' * 40):
        sha = _rev_parse(self.worker)
        return [f'refs/heads/main {sha} refs/heads/feature {remote_sha}\n']

    def test_a_commit_origin_already_has_is_not_reported_though_the_local_view_is_stale(self):
        _write(self.worker, 'new.txt', 'a clean change\n')
        _commit(self.worker, 'a clean change')
        # the stale view alone counts the published plan as unpublished
        self.assertTrue(redact.scan_unpublished(self.worker, 'HEAD', self.pats))

        findings = redact._scan_pre_push_stdin(self.worker, self.pats, self._push_line())

        self.assertEqual(findings, [])

    def test_a_new_commit_adding_a_protected_name_is_still_refused(self):
        _write(self.worker, 'new.txt', f'a fresh mention of {_WORD}\n')
        _commit(self.worker, 'a new commit')

        findings = redact._scan_pre_push_stdin(self.worker, self.pats, self._push_line())

        self.assertEqual({f.path for f in findings}, {'new.txt'})

    def test_a_failed_fetch_falls_back_to_the_current_view_and_the_remote_sha(self):
        _git(['remote', 'set-url', 'origin', os.path.join(self.root, 'gone.git')], self.worker)
        published = _rev_parse(self.worker)
        _write(self.worker, 'new.txt', f'a fresh mention of {_WORD}\n')
        _commit(self.worker, 'a new commit')

        with mock.patch('sys.stderr') as err:
            findings = redact._scan_pre_push_stdin(self.worker, self.pats,
                                                   self._push_line(remote_sha=published))

        self.assertIn('may be stale', ''.join(c.args[0] for c in err.write.call_args_list))
        self.assertEqual({f.path for f in findings}, {'new.txt'})

    def test_a_timed_out_fetch_does_not_crash(self):
        with mock.patch.object(redact.subprocess, 'run',
                               side_effect=subprocess.TimeoutExpired('git', 60)) as run, \
                mock.patch.object(redact, '_run_git') as run_git, \
                mock.patch('sys.stderr'):
            run_git.return_value.returncode = 0
            self.assertFalse(redact.refresh_origin(self.worker))
        self.assertEqual(run.call_args.kwargs['timeout'], redact.FETCH_TIMEOUT_S)
