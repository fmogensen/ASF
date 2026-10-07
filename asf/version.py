"""ASF's own release version: ``x.y.z``, one patch version per merge to main that changes the
package, read off the ``v<x.y.z>`` git tag — never a hand-edited constant.

Three parts, one rule each:

* **reading** a version: :func:`of_commit` (the tag on a commit, or the nearest one plus the
  commits past it), :func:`label` (``0.1.108 (96fa0feca)`` — the version, the sha only as a
  detail), :func:`pin_label` for any install record.
* **cutting** releases: :func:`plan` gives every merge on main's first-parent line since the
  newest tag that changed ``asf/`` the next patch version, oldest first; :func:`cut` tags them,
  pushes the tags and files one CHANGELOG entry per version on main. The ``release`` workflow
  runs it on every push to main, so a merge is versioned however it landed (a direct
  ``gh pr merge``, the factory's lane or merge queue, the native landing).
* **enforcing** it: :func:`health` is red when main's newest package change carries no version,
  or ``CHANGELOG.md`` has no entry for the newest tag — the doctor's ``release`` row and the
  tick's version check both raise it. :func:`pr_entry` is what the workflow's PR job runs: the
  release-notes line a change to ``asf/`` will get, refused when it would be empty.

Stdlib only (the workflow runs it before anything is installed): ``python3 -m asf.version``.
"""
import argparse
import datetime
import os
import re
import subprocess
import sys
import tempfile

from asf.record.core import ID_DIGITS

#: A release tag.
TAG_RE = re.compile(r'^v(\d+)\.(\d+)\.(\d+)$')
#: A pre-release tag (``v0.1.0-preview``): cut by hand through the release workflow, never by
#: :func:`cut`, and never read as a version — every ``git describe`` here excludes ``*-*``.
PRERELEASE_RE = re.compile(r'^v(\d+)\.(\d+)\.(\d+)-([0-9A-Za-z][0-9A-Za-z.]*)$')
#: What ``--to`` accepts as a version: ``0.1.108`` or ``v0.1.108``.
VERSION_ARG_RE = re.compile(r'^v?(\d+\.\d+\.\d+)$')
#: ``git describe --tags`` off a release tag: ``v0.1.9`` or ``v0.1.9-4-g205123f``.
DESCRIBE_RE = re.compile(r'^v(\d+\.\d+\.\d+)(?:-(\d+)-g([0-9a-f]+))?$')
#: How much of a sha a label shows.
SHA_LEN = 9
#: A merge that changes these paths changes what an install runs: it gets a version.
PACKAGE_PATHS = ('asf/',)
#: The version a build with no git and no stamp reports.
FALLBACK = '0.0.0'
CHANGELOG = 'CHANGELOG.md'
CHANGELOG_PREAMBLE = '# Changelog\n\nOne entry per released version, newest first.\n'
#: The subject of the commit that files versions in the changelog — never a release by itself.
CHANGELOG_SUBJECT = 'docs(release): {tags} in ' + CHANGELOG
INSTALL_LINE = 'pipx install --force "git+https://github.com/{slug}.git@{tag}"'
#: How long a merge may wait for its version (and a tag for its changelog entry) before
#: :func:`health` is red: the release workflow takes a few minutes.
GRACE_S = 30 * 60
#: The line of a PR body that is its release note, verbatim.
NOTE_LINE_RE = re.compile(r'(?im)^\s*[-*]?\s*\**what changed for you\**\s*:\s*\**\s*(.+?)\s*$')
#: A subject's conventional-commit or item prefix: ``fix(lane): ``, ``S6: ``, ``T-0659 — ``.
PREFIX_RE = re.compile(rf'^(?:[a-z]+(?:\([^)]*\))?!?:\s*|[A-Z]+\d*:\s*|[A-Z]-{ID_DIGITS}\s*[—:-]\s*)')
PR_RE = re.compile(r'\s*\(#(\d+)\)\s*$')
SECTIONS = ('Features landed', 'Bugs fixed', 'In progress')
#: What a forbidden name becomes in a generated note.
SCRUB_TOKEN = 'a product'


# ---- reading a version ---------------------------------------------------------------------

def tag_of(ref):
    """``v0.1.108`` for ``0.1.108`` or ``v0.1.108``; ``None`` for anything else (a sha, a branch)."""
    m = VERSION_ARG_RE.match(str(ref or '').strip())
    return f'v{m.group(1)}' if m else None


def from_describe(described):
    """``0.1.9`` / ``0.1.9+4`` off a ``git describe --tags`` line, else ``None``."""
    m = DESCRIBE_RE.match(str(described or '').strip())
    if not m:
        return None
    return f'{m.group(1)}+{m.group(2)}' if m.group(2) not in (None, '0') else m.group(1)


def pep440(described):
    """The package version a build stamps: ``0.1.9`` on a tag, ``0.1.9.post4+g205123f`` past one,
    :data:`FALLBACK` with no tag at all."""
    m = DESCRIBE_RE.match(str(described or '').strip())
    if not m:
        return FALLBACK
    if m.group(2) in (None, '0'):
        return m.group(1)
    return f'{m.group(1)}.post{m.group(2)}+g{m.group(3)}'


def label(version, sha):
    """``0.1.108 (96fa0feca)``: the version, the sha only as a detail. No version: the sha alone."""
    sha = str(sha or '')[:SHA_LEN]
    if version and sha:
        return f'{version} ({sha})'
    return version or sha or 'unknown'


def _git(repo, *args, run=subprocess.run):
    try:
        p = run(['git', '-C', repo, *args], capture_output=True, text=True, timeout=30)  # client-exempt: stdlib-only: the release workflow runs this before asf is installed
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def is_factory_source(repo):
    try:
        with open(os.path.join(repo, 'pyproject.toml'), encoding='utf-8') as f:
            return re.search(r'^name\s*=\s*"asf-factory"', f.read(), re.M) is not None
    except (OSError, TypeError):
        return False


def factory_repo():
    """A git repo holding the factory's source and its tags: the running package's own checkout,
    else the repo of the product that is the factory itself; ``None`` when neither is known."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.exists(os.path.join(root, '.git')):
        return root
    try:
        from asf import env
        for name in env.list_products():
            try:
                repo = env.load_product(name).repo_dir
            except Exception:  # noqa: BLE001 — an unreadable product file is the doctor's
                continue
            if repo and is_factory_source(repo):
                return repo
    except Exception:  # noqa: BLE001 — no config: no repo to read tags from
        pass
    return None


def of_commit(repo, sha, run=subprocess.run):
    """The version of ``sha`` in ``repo``: its tag (``0.1.108``), else the nearest tag and the
    commits past it (``0.1.107+15``); ``None`` when no tag reaches it or it is unknown there."""
    if not repo or not sha:
        return None
    return from_describe(_git(repo, 'describe', '--tags', '--match', 'v[0-9]*', '--exclude', '*-*', sha,
                              run=run))


def pin_label(sha, repo=None, recorded=None):
    """A pin as the operator reads it: ``0.1.121 (267264dbe)``. The version comes from the
    factory's tags (``repo``, default :func:`factory_repo`), else what the record wrote."""
    if not sha:
        return 'unknown'
    version = of_commit(repo or factory_repo(), sha) or recorded
    return label(version, sha)


# ---- cutting releases ----------------------------------------------------------------------

def version_tags(repo, run=subprocess.run):
    """``{(x, y, z): name}`` of every release tag in ``repo``."""
    out = {}
    for name in (_git(repo, 'tag', '-l', 'v*', run=run) or '').split():
        m = TAG_RE.match(name)
        if m:
            out[tuple(int(g) for g in m.groups())] = name
    return out


def tags_on(repo, sha, run=subprocess.run):
    return [n for n in (_git(repo, 'tag', '--points-at', sha, run=run) or '').split()
            if TAG_RE.match(n)]


def changes_package(repo, sha, run=subprocess.run):
    """True when the commit ``sha`` changed a :data:`PACKAGE_PATHS` path against its first parent."""
    names = _git(repo, 'diff', '--name-only', f'{sha}^1', sha, run=run)
    if names is None:   # a root commit
        names = _git(repo, 'show', '--format=', '--name-only', sha, run=run) or ''
    return any(n.startswith(PACKAGE_PATHS) for n in names.splitlines())


def plan(repo, ref='HEAD', run=subprocess.run):
    """``[(sha, 'v0.1.N')]``, oldest first: every first-parent commit after the newest tagged
    one on ``ref`` that changed the package, each the next patch version. A commit that already
    carries a tag ends the walk (so ``plan`` after :func:`cut` is empty)."""
    tags = version_tags(repo, run)
    shas = (_git(repo, 'rev-list', '--first-parent', ref, run=run) or '').split()
    todo = []
    for sha in shas:            # newest first, back to the newest tagged commit
        if tags_on(repo, sha, run):
            break
        if changes_package(repo, sha, run):
            todo.append(sha)
    todo.reverse()
    newest = max(tags) if tags else (0, 1, -1)
    out = []
    for i, sha in enumerate(todo, start=1):
        out.append((sha, 'v%d.%d.%d' % (newest[0], newest[1], newest[2] + i)))
    return out


def section_of(subject):
    s = (subject or '').lower()
    if re.match(r'^(spec|plan)\b', s):
        return 'In progress'
    if re.match(r'^(fix|hotfix|revert)\b', s):
        return 'Bugs fixed'
    return 'Features landed'


def scrub(text, repo=None):
    """``text`` with every forbidden name (``tools/forbidden-names.txt``, the operator's private
    list) replaced: release notes are public."""
    try:
        from asf import redact
        pats = [p for p in redact.patterns(repo=repo, cfg={}, environ={}) if p.kind == 'name']
    except Exception:  # noqa: BLE001 — no patterns readable: the text as it is
        return text
    return redact.scrub(text, pats, token=SCRUB_TOKEN)


def note_line(subject, body='', repo=None):
    """The release-notes line of one merge: the body's ``What changed for you:`` line when it has
    one, else the subject without its ``type(scope):`` prefix; the PR number after it. ``''``
    when nothing is left to say."""
    pr = PR_RE.search(subject or '')
    m = NOTE_LINE_RE.search(body or '')
    text = m.group(1) if m else PR_RE.sub('', subject or '')
    text = PREFIX_RE.sub('', text.strip()).strip().rstrip('.')
    if not text:
        return ''
    text = scrub(text[0].upper() + text[1:], repo)
    return f'{text} (#{pr.group(1)})' if pr else text


def entry(tag, day, lines, slug='fmogensen/ASF'):
    """One CHANGELOG entry: ``## <tag> — <day>``, the sections (``lines``: ``[(section, line)]``)
    and the upgrade command."""
    out = [f'## {tag} — {day}', '']
    for section in SECTIONS:
        rows = [line for sec, line in lines if sec == section and line]
        if rows:
            out += [f'### {section}', ''] + [f'- {r}' for r in rows] + ['']
    if not any(line for _s, line in lines):
        out += ['Maintenance only.', '']
    out += ['### Upgrade', '', f'`{INSTALL_LINE.format(slug=slug, tag=tag)}`']
    return '\n'.join(out) + '\n'


def merge_changelog(current, entries):
    """``current`` with every entry of ``{tag: text}`` it lacks, newest version first; an entry
    already there (by its ``## v…`` heading) is kept as written."""
    parts = re.split(r'(?m)^(?=## )', current or '')
    preamble = '' if parts[0].startswith('## ') else parts[0]
    have = {}
    for block in (p for p in parts if p.startswith('## ')):
        m = re.match(r'## (v\d+\.\d+\.\d+)\b', block)
        have[m.group(1) if m else block] = block
    missing = [t for t in entries if t not in have]
    if not missing:
        return current
    have.update({t: entries[t] for t in missing})

    def key(t):
        m = TAG_RE.match(t)
        return (1, tuple(int(g) for g in m.groups())) if m else (0, ())
    preamble = preamble or CHANGELOG_PREAMBLE
    body = ''.join(have[t].rstrip('\n') + '\n\n' for t in sorted(have, key=key, reverse=True))
    return (preamble.rstrip('\n') + '\n\n' + body).rstrip('\n') + '\n'


def has_entry(text, tag):
    return re.search(rf'(?m)^## {re.escape(tag)}\b', text or '') is not None


def _commit_file(repo, base, path, text, message, run=subprocess.run):
    """A commit on ``base`` setting ``path`` to ``text``, built in a scratch index (the working
    tree is never touched). Its sha, or ``None``."""
    def g(args, env_vars=None, stdin=None):
        p = run(['git', '-C', repo, *args], capture_output=True, text=True, input=stdin,  # client-exempt: stdlib-only: the release workflow runs this before asf is installed
                env=env_vars)
        return p.stdout.strip() if p.returncode == 0 else None
    with tempfile.TemporaryDirectory(prefix='changelog_') as tmp:
        scratch = {**os.environ, 'GIT_INDEX_FILE': os.path.join(tmp, 'index')}
        blob = g(['hash-object', '-w', '--stdin'], stdin=text)
        if not blob or g(['read-tree', base], scratch) is None:
            return None
        if g(['update-index', '--add', '--cacheinfo', f'100644,{blob},{path}'], scratch) is None:
            return None
        tree = g(['write-tree'], scratch)
        return (tree and g(['commit-tree', tree, '-p', base, '-m', message])) or None


def _pr_body(slug, number, run=subprocess.run):
    """The body of PR ``number`` on ``slug`` via ``gh``, or ``''``."""
    if not slug or not number:
        return ''
    try:
        p = run(['gh', 'pr', 'view', str(number), '-R', slug, '--json', 'body', '-q', '.body'],  # client-exempt: stdlib-only: the release workflow runs this before asf is installed
                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ''
    return p.stdout if p.returncode == 0 else ''


def cut(repo, ref='HEAD', remote='origin', main='main', push=False, slug='fmogensen/ASF',
        pr_body=None, today=None, run=subprocess.run, out=print, attempts=3):
    """Version every merge :func:`plan` names: an annotated tag each (its message: the entry),
    pushed when ``push``; then the CHANGELOG entries, one commit on ``remote/main`` (rebuilt on
    the new tip and pushed again when main moved meanwhile). Returns the tags cut."""
    pr_body = pr_body or (lambda n: _pr_body(slug, n, run))
    cut_tags, entries = [], {}
    for sha, tag in plan(repo, ref, run):
        subject = _git(repo, 'log', '-1', '--format=%s', sha, run=run) or ''
        pr = PR_RE.search(subject)
        body = (pr_body(pr.group(1)) if pr else '') or \
            (_git(repo, 'log', '-1', '--format=%b', sha, run=run) or '')
        day = today or (_git(repo, 'log', '-1', '--format=%cs', sha, run=run) or
                        datetime.date.today().isoformat())
        text = entry(tag, day, [(section_of(subject), note_line(subject, body, repo))], slug)
        if _git(repo, 'tag', '-a', '--cleanup=whitespace', tag, sha, '-m', text, run=run) is None:
            out(f'release: could not tag {sha[:SHA_LEN]} as {tag}')
            return cut_tags
        if push and _git(repo, 'push', '-q', remote, f'refs/tags/{tag}', run=run) is None:
            _git(repo, 'tag', '-d', tag, run=run)
            out(f'release: could not push {tag} (another run cut it?) — stopping')
            return cut_tags
        out(f'release: {tag} is {sha[:SHA_LEN]} — {subject}')
        cut_tags.append(tag)
        entries[tag] = text
    sync_changelog(repo, entries, remote, main, push, run, out, attempts)
    return cut_tags


def tag_entries(repo, run=subprocess.run):
    """``{tag: entry}`` for every release tag whose annotation is a changelog entry."""
    out = {}
    for tag in version_tags(repo, run).values():
        text = _git(repo, 'for-each-ref', '--format=%(contents)', f'refs/tags/{tag}', run=run)
        if text and text.startswith(f'## {tag}'):
            out[tag] = text.rstrip('\n') + '\n'
    return out


def sync_changelog(repo, entries, remote='origin', main='main', push=False, run=subprocess.run,
                   out=print, attempts=3):
    """File ``entries`` (and any tag annotation that is an entry the changelog lacks) in
    ``CHANGELOG.md`` on ``remote/main``. Returns the commit's sha, or ``None`` when nothing was
    missing or it could not land."""
    entries = {**tag_entries(repo, run), **entries}
    for _ in range(max(1, attempts)):
        if push:
            _git(repo, 'fetch', '-q', remote, main, run=run)
        base = _git(repo, 'rev-parse', '--verify', '-q', f'{remote}/{main}^{{commit}}', run=run) \
            or _git(repo, 'rev-parse', 'HEAD', run=run)
        current = _git(repo, 'show', f'{base}:{CHANGELOG}', run=run)
        current = current + '\n' if current else ''
        text = merge_changelog(current, entries)
        if text == current:
            return None
        added = [t for t in entries if not has_entry(current, t)]
        sha = _commit_file(repo, base, CHANGELOG, text,
                           CHANGELOG_SUBJECT.format(tags=', '.join(added)), run)
        if not sha:
            out('release: could not build the changelog commit')
            return None
        if not push:
            out(f'release: {", ".join(added)} in {CHANGELOG} ({sha[:SHA_LEN]}, not pushed)')
            return sha
        if _git(repo, 'push', '-q', remote, f'{sha}:refs/heads/{main}', run=run) is not None:
            out(f'release: {", ".join(added)} in {CHANGELOG}')
            return sha
        out(f'release: {main} moved under the changelog commit; rebuilding it')
    out(f'release: could not push {CHANGELOG} to {main}')
    return None


# ---- enforcing it --------------------------------------------------------------------------

def _age_s(repo, ref, fmt, now, run):
    stamp = _git(repo, 'log', '-1', f'--format={fmt}', ref, run=run)
    try:
        return now.timestamp() - int(stamp)
    except (TypeError, ValueError):
        return None


def health(repo, ref='origin/main', now=None, grace_s=GRACE_S, run=subprocess.run):
    """``(ok, detail)``: red when a merge on ``ref`` that changed the package has gone more than
    ``grace_s`` without a version, or ``CHANGELOG.md`` at ``ref`` has no entry for the newest
    tag reachable from it past the same grace."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    head = _git(repo, 'rev-parse', '--verify', '-q', f'{ref}^{{commit}}', run=run)
    if not head:
        return True, f'{ref} unreadable (nothing to check)'
    newest = _git(repo, 'describe', '--tags', '--abbrev=0', '--match', 'v[0-9]*', '--exclude', '*-*',
                  head, run=run)
    problems = []
    late = []
    for sha, tag in plan(repo, head, run):
        age = _age_s(repo, sha, '%ct', now, run)
        if age is None or age > grace_s:
            late.append((sha, tag, age))
    if late:
        sha, _tag, age = late[0]
        problems.append(f'{len(late)} merge(s) on {ref} changed asf/ with no version tag '
                        f'(oldest {sha[:SHA_LEN]}, {int((age or 0) // 60)} min) — the release '
                        f'workflow did not run: python3 -m asf.version cut --push')
    if newest:
        changelog = _git(repo, 'show', f'{head}:{CHANGELOG}', run=run) or ''
        age = _tag_age_s(repo, newest, now, run)
        if not has_entry(changelog, newest) and (age is None or age > grace_s):
            problems.append(f'{CHANGELOG} on {ref} has no entry for {newest}')
    if problems:
        return False, '; '.join(problems)
    if not newest:
        return False, f'{ref} carries no release tag'
    version = of_commit(repo, head, run)
    return True, f'{ref} is {label(version, head)}; {CHANGELOG} has {newest}'


def _tag_age_s(repo, tag, now, run):
    stamp = _git(repo, 'for-each-ref', '--format=%(creatordate:unix)', f'refs/tags/{tag}', run=run)
    try:
        return now.timestamp() - int(stamp)
    except (TypeError, ValueError):
        return None


def pr_entry(repo, base, title, body='', run=subprocess.run):
    """``(ok, text)`` for a PR: the release-notes line its merge will get when it changes
    ``asf/``. Refused when the generator has nothing to say (an empty title and no
    ``What changed for you:`` line)."""
    names = _git(repo, 'diff', '--name-only', f'{base}...HEAD', run=run)
    if names is None:
        return False, f'cannot diff against {base}'
    if not any(n.startswith(PACKAGE_PATHS) for n in names.splitlines()):
        return True, 'no change to asf/: this merge gets no version'
    line = note_line(title, body, repo)
    if not line:
        return False, ('this change to asf/ has no release-notes line: give the PR a title, or a '
                       '"What changed for you: …" line in its body')
    return True, f'{section_of(title)}: - {line}'


# ---- the command ---------------------------------------------------------------------------

def prerelease(repo, tag, ref='HEAD', remote='origin', push=False, run=subprocess.run):
    """Tag ``ref`` as the pre-release ``tag`` (annotated) and, with ``push``, push it. The
    caller has already proved the gate green (the release workflow runs ``asf release-readiness
    --gate preview`` first). ``(ok, detail)``; a tag already on ``ref`` is ok, on another commit
    a refusal — a pre-release tag never moves."""
    if not PRERELEASE_RE.match(tag or ''):
        return False, f'{tag!r} is not a pre-release tag (v<x.y.z>-<label>)'
    sha = _git(repo, 'rev-parse', '--verify', '-q', f'{ref}^{{commit}}', run=run)
    if not sha:
        return False, f'{ref} is not a commit'
    sha = sha.strip()
    at = (_git(repo, 'rev-parse', '--verify', '-q', f'refs/tags/{tag}^{{commit}}', run=run) or '').strip()
    if at and at != sha:
        return False, f'{tag} is already on {at[:9]}, not {sha[:9]}: a pre-release tag never moves'
    if not at and _git(repo, 'tag', '-a', tag, '-m', f'{tag}: pre-release', sha, run=run) is None:
        return False, f'could not tag {sha[:9]} as {tag}'
    if push and _git(repo, 'push', '-q', remote, f'refs/tags/{tag}', run=run) is None:
        return False, f'could not push {tag}'
    return True, f'{tag} on {sha[:9]}' + (' (pushed)' if push else '')


def main(argv=None):
    p = argparse.ArgumentParser(prog='python3 -m asf.version')
    sub = p.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('cut', help='tag every unversioned merge and file its changelog entry')
    c.add_argument('--repo', default='.')
    c.add_argument('--ref', default='HEAD')
    c.add_argument('--main', default='main')
    c.add_argument('--slug', default=os.environ.get('GITHUB_REPOSITORY') or 'fmogensen/ASF')
    c.add_argument('--push', action='store_true')
    h = sub.add_parser('check', help='red when a merge has no version or the changelog no entry')
    h.add_argument('--repo', default='.')
    h.add_argument('--ref', default='origin/main')
    r = sub.add_parser('pr', help="the release-notes line a PR's merge will get")
    r.add_argument('--repo', default='.')
    r.add_argument('--base', required=True)
    r.add_argument('--title', required=True)
    r.add_argument('--body-file')
    x = sub.add_parser('prerelease', help='tag a commit as a pre-release (v<x.y.z>-<label>)')
    x.add_argument('--repo', default='.')
    x.add_argument('--tag', required=True)
    x.add_argument('--ref', default='HEAD')
    x.add_argument('--push', action='store_true')
    a = p.parse_args(argv)
    if a.cmd == 'prerelease':
        ok, detail = prerelease(a.repo, a.tag, a.ref, push=a.push)
        print(('ok: ' if ok else 'RED: ') + detail)
        return 0 if ok else 1
    if a.cmd == 'cut':
        cut(a.repo, a.ref, main=a.main, push=a.push, slug=a.slug)
        return 0
    if a.cmd == 'check':
        ok, detail = health(a.repo, a.ref)
        print(('ok: ' if ok else 'RED: ') + detail)
        return 0 if ok else 1
    body = ''
    if a.body_file:
        with open(a.body_file, encoding='utf-8') as f:
            body = f.read()
    ok, detail = pr_entry(a.repo, a.base, a.title, body)
    print(('release notes: ' if ok else 'RED: ') + detail)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
