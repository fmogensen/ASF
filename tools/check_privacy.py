"""tools/check_privacy.py — the privacy sweep: no operator path, e-mail address or private link in
a tracked file. ``check_generic.sh`` covers names; this covers the shapes a name hides in.

  python3 tools/check_privacy.py [--root <dir>] [--allow <file>]

Three kinds, each a regex over every tracked text file (``git ls-files``):

- ``path``  — a home directory with a real user in it: ``/Users/<u>/`` or ``/home/<u>/``, where
  ``<u>`` is not a placeholder (``<you>``, ``$USER``, ``runner``, ``user`` …).
- ``email`` — an address outside the reserved and no-reply domains (``example.com``,
  ``*.invalid``, ``*.test``, ``localhost``, ``users.noreply.github.com``, ``noreply@…``).
- ``link``  — a link into a private space: a chat or session URL with a real id, a shared
  document, a team chat.

A finding prints ``<path>:<line>: <kind>`` and never the matched text. ``tools/privacy-allow.txt``
(one ``<path glob>`` or ``<path glob>:<kind>`` per line, ``#`` comments) exempts a file. Exit 1 on
any finding, 0 when clean.
"""
import argparse
import fnmatch
import os
import re
import subprocess
import sys

PLACEHOLDER_USERS = {'runner', 'user', 'username', 'you', 'me', 'name', 'shared', 'example',
                     'operator', 'someone', 'jane', 'john', 'alice', 'bob', 'ci', 'sample', 'x'}
HOME_RE = re.compile(r'(?<![\w.>})\]-])/(?:Users|home)/([A-Za-z0-9._-]+)/')
EMAIL_RE = re.compile(r'\b[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})\b')
EMAIL_OK_DOMAINS = re.compile(
    r'(^|\.)(example\.(com|org|net)|[a-z0-9-]+\.(invalid|test|example|localhost|local)|localhost|'
    r'users\.noreply\.github\.com|noreply\.github\.com)$', re.I)
EMAIL_OK_LOCAL = re.compile(r'^(no-?reply|git|ci|asf-release)$', re.I)
LINK_RE = re.compile(
    r'https?://(?:claude\.ai/(?:code/session_[A-Za-z0-9]{12,}|chat/|project/|share/)|docs\.google\.com/|'
    r'drive\.google\.com/|[a-z0-9-]+\.slack\.com/|app\.slack\.com/|(?:www\.)?notion\.so/|'
    r'linear\.app/[^/\s]+/issue/|[a-z0-9-]+\.atlassian\.net/|mail\.google\.com/|'
    r'calendar\.google\.com/|(?:www\.)?dropbox\.com/s/|1drv\.ms/|onedrive\.live\.com/)', re.I)


def tracked(root):
    out = subprocess.run(['git', '-C', root, 'ls-files', '-z'], capture_output=True, check=True)  # client-exempt: a stdlib-only CI tool
    return [p for p in out.stdout.decode('utf-8', 'replace').split('\0') if p]


def read_allow(path):
    rules = []
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.split('#', 1)[0].strip()
                if line:
                    glob, _, kind = line.partition(':')
                    rules.append((glob.strip(), kind.strip() or None))
    except OSError:
        pass
    return rules


def allowed(rules, path, kind):
    return any(fnmatch.fnmatchcase(path, g) and (k is None or k == kind) for g, k in rules)


def scan_line(line):
    """The kinds one line holds: ``['path' | 'email' | 'link', …]``."""
    kinds = []
    for m in HOME_RE.finditer(line):
        u = m.group(1)
        if u.lower() not in PLACEHOLDER_USERS and not u.startswith(('$', '<', '{')):
            kinds.append('path')
            break
    for m in EMAIL_RE.finditer(line):
        local = m.group(0).split('@', 1)[0]
        if not EMAIL_OK_DOMAINS.search(m.group(1)) and not EMAIL_OK_LOCAL.match(local):
            kinds.append('email')
            break
    if LINK_RE.search(line):
        kinds.append('link')
    return kinds


def scan(root, rules):
    findings = []
    for rel in tracked(root):
        path = os.path.join(root, rel)
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except OSError:
            continue
        if b'\0' in data[:4096]:
            continue
        for n, line in enumerate(data.decode('utf-8', 'replace').splitlines(), 1):
            for kind in scan_line(line):
                if not allowed(rules, rel, kind):
                    findings.append((rel, n, kind))
    return findings


def main(argv=None):
    p = argparse.ArgumentParser(prog='check_privacy')
    here = os.path.dirname(os.path.abspath(__file__))
    p.add_argument('--root', default=os.path.dirname(here))
    p.add_argument('--allow', default=os.path.join(here, 'privacy-allow.txt'))
    a = p.parse_args(argv)
    findings = scan(a.root, read_allow(a.allow))
    for rel, n, kind in findings:
        print(f'{rel}:{n}: {kind}')
    print(f'check_privacy: {len(findings)} finding(s)' if findings else 'check_privacy: clean')
    return 1 if findings else 0


if __name__ == '__main__':
    sys.exit(main())
