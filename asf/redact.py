"""asf.redact — the one scanner: no operator name and no secret reaches a record or a public
repo (F-0075). Every commit and every push runs this before it publishes anything; a session's
worktree and ASF's own repo run it through a git hook (:mod:`asf.hooks`); an operator runs it by
hand as ``asf redact``.

A finding never carries the matched text (D8) — only where it was found and which pattern found
it — so a refusal can be printed into a tick log or a session transcript without repeating the
leak it is reporting.

:func:`patterns` merges three kinds of pattern: operator names (the worker pool's account names,
an optional private list, and a repo's own tracked ``tools/forbidden-names.txt``), built-in
secret shapes, the literal value of every environment variable that looks like a secret, and the
value in every worker account's and every product's ``auth_env`` file
(:func:`auth_env_secrets`). No
config, no private list and no repo list is an empty pattern set, not an error — the scanner
still runs, it simply has nothing of that kind to look for.

:func:`scan_staged`, :func:`scan_unpublished` and :func:`scan_tree` read a git repo three
different ways (a staged diff, every commit a remote does not have yet, the whole tracked tree);
:func:`gate` is what a caller runs a scan's findings through — clean is a no-op, a finding raises
:class:`Refused` and, when a product resolves, appends the local refusal ledger (never the
matched text).
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from typing import NamedTuple

from asf import env


class Finding(NamedTuple):
    path: str      # repo-relative file, or 'commit <sha7> message'
    line: int      # 1-based line in the new file / the message
    kind: str      # 'name' | 'secret'
    source: str    # 'worker_pool.accounts' | 'redact-names.txt' | 'tools/forbidden-names.txt'
                   # | 'env:<VAR>' | 'rule:<rule-id>' | an extra --names path


class Pattern(NamedTuple):
    kind: str
    source: str
    regex: 're.Pattern'


class Refused(Exception):
    """Raised by :func:`gate` when a scan finds something; carries the findings that caused it."""

    def __init__(self, findings):
        self.findings = list(findings)
        super().__init__(f'{len(self.findings)} finding(s)')


class RedactError(Exception):
    """The repo cannot be scanned, or a ``--names`` list cannot be read (exit 2 at the CLI)."""


# ---- patterns ----------------------------------------------------------------

NAME_SOURCE_POOL = 'worker_pool.accounts'
NAME_SOURCE_PRIVATE = 'redact-names.txt'
NAME_SOURCE_REPO = 'tools/forbidden-names.txt'

#: the eight built-in secret shapes (D6), each written so its own source text does not match it
SECRET_RULES = (
    ('private-key', r'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
    ('aws-access-key', r'AKIA[0-9A-Z]{16}'),
    ('github-token', r'gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,}'),
    ('anthropic-key', r'sk-ant-[A-Za-z0-9_-]{20,}'),
    ('openai-style-key', r'sk-[A-Za-z0-9]{32,}'),
    ('slack-token', r'xox[abprs]-[A-Za-z0-9-]{10,}'),
    ('jwt', r'eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.'),
    ('url-password', r'[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s:@]+@'),
)

#: an environment variable whose name matches this is a secret when its value is long enough (D6)
ENV_SECRET_NAME_RE = re.compile(r'TOKEN|SECRET|KEY|PASSWORD|PASS|CREDENTIAL', re.IGNORECASE)
ENV_SECRET_MIN_LEN = 12
#: …unless the name is one of git's own config-injection variables, or names a location rather
#: than a credential (B-0081: `GIT_CONFIG_KEY_0=init.defaultBranch` flagged every line of the
#: repo that mentions `init.defaultBranch`)
ENV_NAME_EXEMPT_RE = re.compile(r'^GIT_CONFIG_(KEY|VALUE)_\d+$|^GIT_CONFIG_COUNT$'
                                r'|_(PATH|FILE|DIR)$', re.IGNORECASE)
#: …and whatever the variable is called, a value that is a path or a dotted identifier is not a
#: secret. A credential does not look like `init.defaultBranch` or `/etc/ssl/private`.
ENV_VALUE_BENIGN_RE = re.compile(r'^(?:[~.]?/|[A-Za-z]:\\)'
                                 r'|^[A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z][A-Za-z0-9]*)+$')


def env_value_is_secret(var_name, value):
    """Is this environment variable's value worth searching for? The name must look like a
    credential's and not be exempt, and the value must be long enough and not obviously benign
    (B-0081). A false positive here blocks every commit and every push, so the edges matter."""
    if not value or len(value) < ENV_SECRET_MIN_LEN:
        return False
    if ENV_NAME_EXEMPT_RE.search(var_name) or not ENV_SECRET_NAME_RE.search(var_name):
        return False
    return not ENV_VALUE_BENIGN_RE.search(value)


def _names_from_lines(text):
    """Blank lines and ``#`` comments skipped — the format ``check_generic.sh`` reads today."""
    out = []
    for line in text.splitlines():
        line = line.split('#', 1)[0].strip()
        if line:
            out.append(line)
    return out


def _read_names_file(path):
    """Absent → empty, not an error (Preconditions)."""
    if not os.path.isfile(path):
        return []
    with open(path, encoding='utf-8') as f:
        return _names_from_lines(f.read())


def patterns(repo=None, cfg=None, environ=None, extra=()):
    """Every pattern a scan checks a line against: pool account names, the operator's private
    list, the scanned repo's own ``tools/forbidden-names.txt``, ``extra`` (``--names FILE``), the
    built-in secret rules, and the operator's own secret environment values. Each pattern's regex
    is case-insensitive. ``extra`` entries that cannot be read raise :class:`RedactError` — an
    operator naming a private list on the command line expects it to exist; the implicit sources
    (the private list, the repo list) do not."""
    if cfg is None:
        cfg = env.load_config()
    if environ is None:
        environ = os.environ
    pats = []

    accounts = ((cfg or {}).get('worker_pool') or {}).get('accounts') or []
    for account in accounts:
        name = (account or {}).get('name')
        if name:
            pats.append(Pattern('name', NAME_SOURCE_POOL,
                                 re.compile(rf'\b{re.escape(str(name))}\b', re.IGNORECASE)))

    private_list = os.path.join(env.ASF_HOME, 'redact-names.txt')
    for text in _read_names_file(private_list):
        pats.append(Pattern('name', NAME_SOURCE_PRIVATE, re.compile(text, re.IGNORECASE)))

    if repo:
        repo_list = os.path.join(repo, 'tools', 'forbidden-names.txt')
        for text in _read_names_file(repo_list):
            pats.append(Pattern('name', NAME_SOURCE_REPO, re.compile(text, re.IGNORECASE)))

    for path in extra:
        if not os.path.isfile(path):
            raise RedactError(f'cannot read {path}')
        with open(path, encoding='utf-8') as f:
            lines = _names_from_lines(f.read())
        for text in lines:
            pats.append(Pattern('name', path, re.compile(text, re.IGNORECASE)))

    for rule_id, rule_re in SECRET_RULES:
        pats.append(Pattern('secret', f'rule:{rule_id}', re.compile(rule_re, re.IGNORECASE)))

    for var_name, value in environ.items():
        if env_value_is_secret(var_name, value):
            pats.append(Pattern('secret', f'env:{var_name}',
                                 re.compile(re.escape(value), re.IGNORECASE)))

    for var_name, value in auth_env_secrets(cfg):
        pats.append(Pattern('secret', f'auth_env:{var_name}',
                             re.compile(re.escape(value), re.IGNORECASE)))

    return pats


def _read_secret(path):
    try:
        with open(path, encoding='utf-8') as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        return None


def auth_env_secrets(cfg):
    """``[(VARIABLE, value)]`` for every worker account's ``auth_env`` file, and every
    configured product's ``conventions.auth_env`` file (GitHub access is per product, not per
    account), that can be read — whatever the variable is called, the value is a credential. An
    unreadable file is skipped here (the launch refuses it; the scanner still runs)."""
    out = []
    for account in ((cfg or {}).get('worker_pool') or {}).get('accounts') or []:
        for var_name, path in env.account_auth_env(account if isinstance(account, dict) else {}).items():
            value = _read_secret(path)
            if value and len(value) >= 8:
                out.append((var_name, value))
    for name in _all_product_names():
        try:
            product = env.load_product(name)
        except env.ConfigError:
            continue
        for var_name, path in env.product_auth_env(product).items():
            value = _read_secret(path)
            if value and len(value) >= 8:
                out.append((var_name, value))
    return out


def _all_product_names():
    """Every ``products/<name>.yaml`` under ``ASF_HOME`` — a malformed one is skipped by its
    caller, not raised here (the scanner still runs with whatever it can read)."""
    d = os.path.join(env.ASF_HOME, 'products')
    if not os.path.isdir(d):
        return []
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith('.yaml'))


# ---- scanning ------------------------------------------------------------------

def _scan_numbered_lines(path, numbered, pats):
    findings = []
    for line_no, content in numbered:
        for pat in pats:
            if pat.regex.search(content):
                findings.append(Finding(path, line_no, pat.kind, pat.source))
    return findings


def scan_text(path, text, pats):
    return _scan_numbered_lines(path, enumerate(text.splitlines(), start=1), pats)


#: What a protected name or a secret becomes in text ASF derives (Backlinks, Children, an intake
#: title): neutral, and matched by no pattern.
SCRUB_TOKEN = '[redacted]'


def scrub(text, pats, token=SCRUB_TOKEN):
    """``text`` with every match of every pattern replaced by ``token`` — derived text passes a
    title through this before it is written, so a protected name in one card is never copied into
    another (the redaction gate would then refuse pushes nobody made)."""
    out = str(text or '')
    for pat in pats or ():
        out = pat.regex.sub(token, out)
    return out


_DEFAULT_CACHE = {}


def default_patterns(repo=None):
    """:func:`patterns` for the operator's config (and ``repo``'s own list), cached while the
    config, the private list, the repo list and the environment are unchanged — derived text is
    rendered card by card, and reading the config per card would be most of the index's time.
    An unreadable config is no patterns of that kind, never an error: the scrub never blocks a
    write."""
    def mtime(path):
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return None
    key = (env.ASF_HOME, repo, mtime(env.config_path()),
           mtime(os.path.join(env.ASF_HOME, 'redact-names.txt')),
           mtime(os.path.join(repo, 'tools', 'forbidden-names.txt')) if repo else None,
           hash(frozenset(os.environ.items())))
    if key not in _DEFAULT_CACHE:
        try:
            cfg = env.load_config()
        except Exception:  # a malformed config is the doctor's to report, not the index's
            cfg = {}
        try:
            pats = patterns(repo=repo, cfg=cfg)
        except Exception:
            pats = []
        _DEFAULT_CACHE.clear()
        _DEFAULT_CACHE[key] = pats
    return _DEFAULT_CACHE[key]


_DIFFGIT_RE = re.compile(r'^diff --git a/.* b/(.*)$')
_HUNK_RE = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@')


def _parse_diff_added_lines(diff_text):
    """``[(path, [(line_no, content), ...])]`` — the ``+`` lines of a ``-U0`` diff, with their
    new-file line numbers taken from the hunk headers. Binary files are skipped."""
    files = []
    path = None
    added = None
    binary = False
    line_no = None
    for line in diff_text.split('\n'):
        m = _DIFFGIT_RE.match(line)
        if m:
            if path is not None and added:
                files.append((path, added))
            path = m.group(1)
            added = []
            binary = False
            line_no = None
            continue
        if line.startswith('Binary files '):
            binary = True
            continue
        if line.startswith('+++ '):
            if line[4:] == '/dev/null':
                path = None
            continue
        hm = _HUNK_RE.match(line)
        if hm:
            line_no = int(hm.group(1))
            continue
        if path is None or binary or line_no is None:
            continue
        if line.startswith('+') and not line.startswith('+++'):
            added.append((line_no, line[1:]))
            line_no += 1
        elif line.startswith('-') and not line.startswith('---'):
            pass
    if path is not None and added:
        files.append((path, added))
    return files


def _run_git(repo, args, input_text=None, git_env=None):
    """``git <args>`` in ``repo``; ``git_env`` (``{name: value}``) is laid over the process's own
    environment — ``GIT_INDEX_FILE`` for a command that must read or write a scratch index."""
    return subprocess.run(['git'] + args, cwd=repo, capture_output=True, text=True,
                          input=input_text,
                          env=dict(os.environ, **git_env) if git_env else None)


def scan_staged(repo, pats):
    """The ``+`` lines of ``git diff --cached -U0``, their new-file line numbers taken from the
    hunk headers; binary files skipped."""
    out = _run_git(repo, ['diff', '--cached', '-U0', '--no-color', '--no-ext-diff'])
    findings = []
    for path, numbered in _parse_diff_added_lines(out.stdout):
        findings += _scan_numbered_lines(path, numbered, pats)
    return findings


def _message_without_trailers(repo, sha):
    """The commit message (``%B``) with the trailer block removed — so the sign-off and
    ``Co-Authored-By:`` are never scanned (Out)."""
    msg = _run_git(repo, ['log', '-1', '--format=%B', sha]).stdout.rstrip('\n')
    trailers = _run_git(repo, ['interpret-trailers', '--parse'], input_text=msg).stdout
    trailer_lines = [l for l in trailers.splitlines() if l.strip()]
    lines = msg.splitlines()
    n = len(trailer_lines)
    if n and lines[-n:] == trailer_lines:
        lines = lines[:-n]
        while lines and lines[-1].strip() == '':
            lines.pop()
    return '\n'.join(lines)


def _is_commit(repo, sha):
    return _run_git(repo, ['cat-file', '-e', f'{sha}^{{commit}}']).returncode == 0


def scan_unpublished(repo, head, pats, published=()):
    """Every commit reachable from ``head`` and from no ``refs/remotes/origin/*`` ref (D4) — nor
    from any sha of ``published`` (a pre-push line's remote sha: the remote already has it) —
    each against its parent, plus its message with the trailer block removed."""
    exclude = [s for s in published if s and set(s) != {'0'} and _is_commit(repo, s)]
    shas = [s for s in _run_git(repo, ['rev-list', head, '--not', '--remotes=origin']
                                + exclude).stdout.split() if s]
    findings = []
    for sha in shas:
        diff = _run_git(repo, ['show', '-U0', '--format=', sha]).stdout
        for path, numbered in _parse_diff_added_lines(diff):
            findings += _scan_numbered_lines(path, numbered, pats)
        message = _message_without_trailers(repo, sha)
        findings += scan_text(f'commit {sha[:7]} message', message, pats)
    return findings


def scan_tree(repo, pats):
    """Every file of ``git ls-files`` but ``LICENSE``, every line."""
    out = _run_git(repo, ['ls-files']).stdout
    findings = []
    for rel in out.splitlines():
        if rel == 'LICENSE':
            continue
        try:
            with open(os.path.join(repo, rel), encoding='utf-8') as f:
                text = f.read()
        except (UnicodeDecodeError, OSError):
            continue
        findings += scan_text(rel, text, pats)
    return findings


# ---- the gate and its output ---------------------------------------------------

def gate(findings, where, product=None):
    """Clean is a no-op. Otherwise, when ``product`` resolves, appends one ledger line per
    finding (never the matched text) to ``<env.state_dir(product)>/redactions.jsonl``, then
    raises :class:`Refused`. No product → no file, and the scan still ran."""
    if not findings:
        return
    if product:
        ts = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        ledger = os.path.join(env.state_dir(product), 'redactions.jsonl')
        with open(ledger, 'a', encoding='utf-8') as f:
            for finding in findings:
                f.write(json.dumps({
                    'ts': ts, 'where': where, 'path': finding.path, 'line': finding.line,
                    'kind': finding.kind, 'source': finding.source,
                }) + '\n')
    raise Refused(findings)


def format_findings(findings):
    """``<file>:<line>: <kind> (<source>)`` — never the matched text (D8)."""
    return [f'{f.path}:{f.line}: {f.kind} ({f.source})' for f in findings]


# ---- the command ----------------------------------------------------------------

def _repo_root(cwd):
    try:
        out = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=cwd,
                             capture_output=True, text=True)
    except OSError as e:
        raise RedactError(f'not a git repo: {e}') from e
    if out.returncode != 0:
        raise RedactError('not a git repo')
    return out.stdout.strip()


#: How long the pre-push refresh of origin's refs may take before the scan goes on without it.
FETCH_TIMEOUT_S = 60


def refresh_origin(repo, timeout=FETCH_TIMEOUT_S):
    """``git fetch --no-tags origin``, quietly, so a worktree whose remote-tracking refs are stale
    does not count commits origin already has as unpublished. No ``origin`` remote → nothing to
    refresh. A failed or timed-out fetch prints one line and the scan uses the current view.
    Returns True when the view was refreshed."""
    if _run_git(repo, ['remote', 'get-url', 'origin']).returncode != 0:
        return False
    try:
        out = subprocess.run(['git', 'fetch', '--quiet', '--no-tags', 'origin'], cwd=repo,
                             capture_output=True, text=True, timeout=timeout,
                             stdin=subprocess.DEVNULL)
        ok = out.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        ok = False
    if not ok:
        print('redact: could not fetch origin — its view may be stale; scanning against it',
              file=sys.stderr)
    return ok


def _scan_pre_push_stdin(repo, pats, stdin):
    """``<local ref> <local sha> <remote ref> <remote sha>`` lines; a deletion (local sha all
    zeros) is skipped. Origin's refs are refreshed first, and each line's remote sha counts as
    published — only commits origin does not have are scanned."""
    lines = [line.split() for line in stdin]
    lines = [p for p in lines if len(p) == 4 and set(p[1]) != {'0'}]
    if not lines:
        return []
    refresh_origin(repo)
    findings = []
    for _local_ref, local_sha, _remote_ref, remote_sha in lines:
        findings += scan_unpublished(repo, local_sha, pats, published=(remote_sha,))
    return findings


def add_arguments(p):
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--staged', action='store_true')
    mode.add_argument('--pre-commit', action='store_true')
    mode.add_argument('--unpublished', nargs='?', const='HEAD', default=None, metavar='REV')
    mode.add_argument('--pre-push', action='store_true')
    mode.add_argument('--tree', action='store_true')
    p.add_argument('--product')
    p.add_argument('--names', action='append', default=[], metavar='FILE')


def cmd_redact(args):
    cwd = os.getcwd()
    try:
        repo = _repo_root(cwd)
        pats = patterns(repo=repo, extra=args.names)
    except RedactError as e:
        print(f'redact: {e}', file=sys.stderr)
        return 2

    if args.staged:
        findings, where = scan_staged(repo, pats), 'cli'
    elif args.pre_commit:
        findings, where = scan_staged(repo, pats), 'hook-pre-commit'
    elif args.unpublished is not None:
        findings, where = scan_unpublished(repo, args.unpublished, pats), 'cli'
    elif args.pre_push:
        findings, where = _scan_pre_push_stdin(repo, pats, sys.stdin), 'hook-pre-push'
    else:
        findings, where = scan_tree(repo, pats), 'cli'

    if not findings:
        print('redact: clean')
        return 0

    for line in format_findings(findings):
        print(line)
    try:
        gate(findings, where, product=args.product)
    except Refused:
        pass
    print(f'redact: refused — {len(findings)} finding(s); no line above is printed as it was')
    return 1


def register(subparsers):
    p = subparsers.add_parser(
        'redact', help='scan for operator names and secrets before they are committed or pushed')
    add_arguments(p)
    p.set_defaults(run=cmd_redact)
    return p


def main(argv=None):
    parser = argparse.ArgumentParser(prog='asf.redact')
    add_arguments(parser)
    args = parser.parse_args(argv)
    return cmd_redact(args)


if __name__ == '__main__':
    sys.exit(main())
